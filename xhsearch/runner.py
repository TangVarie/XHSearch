"""编排：读表 → 调接口 → 判定 → 写回。

两条触发路径（手动刷单行 / 定时刷全表）走的是同一个 refresh()，只是传进来的
行不同。判定口径因此永远只有一份——否则必然出现「白天手动刷出爆文，夜里
批量跑完又变回去」这种最伤信任的 bug。

这个文件里有三处专门用来防「写坏表」的设计，改动时请先读懂它们：

1. 两击定罪：第一次取不到内容不判失效，只计数。
2. 全局熔断：一批里失效比例过高就整批作废标签写入。
3. 失败不清空：任何失败路径都保留上一次的数据，只更新状态列。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import analyze, feishu, protocol, tags, transport
from .config import Settings
from .rows import Row, ToolCall, plan_calls

STATUS_OK = "正常"
STATUS_SUSPECT = "疑似受限"
STATUS_GONE = "已失效"
STATUS_FAILED = "刷新失败"
STATUS_SKIPPED = "跳过"
STATUS_COOLDOWN = "冷却跳过"


@dataclass
class Outcome:
    record_id: str
    status: str
    fields: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    credits: int = 0


@dataclass
class RunReport:
    outcomes: list[Outcome] = field(default_factory=list)
    aborted_reason: str = ""
    breaker_tripped: bool = False
    points_balance: Optional[int] = None

    @property
    def credits(self) -> int:
        return sum(o.credits for o in self.outcomes)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for outcome in self.outcomes:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    def summary(self) -> str:
        parts = [f"{k} {v}" for k, v in sorted(self.counts().items())]
        line = f"处理 {len(self.outcomes)} 行（{'，'.join(parts) or '无'}），"
        line += f"消耗 {self.credits} 积分 ≈ ¥{self.credits / 100:.2f}"
        if self.points_balance is not None:
            line += f"，余额 {self.points_balance} 积分 ≈ ¥{self.points_balance / 100:.2f}"
        if self.breaker_tripped:
            line += "\n🛑 已熔断：本批失效比例异常偏高，所有流量状态写入已作废"
        if self.aborted_reason:
            line += f"\n⚠ 提前中止：{self.aborted_reason}"
        return line


class _Abort(Exception):
    """整批必须停下（key 失效 / 积分耗尽）。已完成的结果照常写回。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _call(api_key: str, call: ToolCall, *, deadline: Optional[float], timeout: float) -> protocol.Result:
    response = transport.post_with_retry(
        protocol.endpoint(call.platform, call.purpose),
        protocol.headers(api_key),
        protocol.build_body(call.arguments),
        timeout=timeout,
        deadline=deadline,
        # 传输层只按 HTTP 状态判重试。业务错误走的是 HTTP 200 + body 里的 code，
        # 传输层看不见，必须由下面按解析结果处理。
        should_retry=lambda r: r.status == 0 or r.status >= 500,
    )
    return protocol.parse_response(
        response.status, response.content_type, response.body, response.request_id
    )


def _fetch_one(
    row: Row,
    api_key: str,
    settings: Settings,
    *,
    now: datetime,
    deadline: Optional[float],
    timeout: float,
) -> tuple[Optional[analyze.Snapshot], Optional[protocol.Err], int]:
    """跑完一行需要的全部调用。返回（快照, 终局错误, 消耗积分）。"""
    calls = plan_calls(row, settings, now)
    if not calls:
        return None, protocol.Err(
            protocol.Failure.UNKNOWN, "bad_link", row.parsed.describe_failure()
        ), 0

    snapshot: Optional[analyze.Snapshot] = None
    credits = 0

    for call in calls:
        result = _call(api_key, call, deadline=deadline, timeout=timeout)

        if isinstance(result, protocol.Err):
            if result.kind in protocol.FATAL:
                raise _Abort(str(result))
            if result.kind is protocol.Failure.RATE_LIMIT:
                # 退避一次再试。再限流就把这一行留给下一轮，别拖垮整批。
                time.sleep(result.retry_after_seconds or 5.0)
                result = _call(api_key, call, deadline=deadline, timeout=timeout)
                if isinstance(result, protocol.Err):
                    if result.kind in protocol.FATAL:
                        raise _Abort(str(result))
                    return snapshot, result, credits
            else:
                # 评论接口失败 = 这一行的结论；detail 失败只是少几个数字，
                # 已经拿到的评论数据仍然有效，不该整行判死。
                if call.purpose == "comments":
                    return snapshot, result, credits
                continue

        assert isinstance(result, protocol.Ok)
        credits += result.points_cost or 10
        if call.purpose == "comments":
            snapshot = analyze.read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            analyze.merge_detail(snapshot, result.data)

    return snapshot, None, credits


def _base_fields(settings: Settings, *, status: str, notes: list[str], now: datetime) -> dict[str, Any]:
    f = settings.fields
    return {
        f.refresh_status: status,
        f.failure_reason: "；".join(n for n in notes if n)[:500],
        f.last_updated: int(now.timestamp() * 1000),
        # 处理完就把「排队刷新」的勾去掉 —— 勾自动消失就是「已完成」的视觉信号，
        # 不需要向运营解释任何东西。
        f.queued: False,
    }


def refresh(
    rows: list[Row],
    api_key: str,
    settings: Settings,
    *,
    now: Optional[datetime] = None,
    known_options: Optional[list[str]] = None,
    forced: bool = False,
    timeout: float = 30.0,
    progress: Optional[Callable[[str], None]] = None,
) -> RunReport:
    """刷新一批行。不写回，只算结果——写回由调用方决定时机。

    forced=True 表示这是人明确要求的刷新（手动触发），会跳过冷却检查。

    到软截止就停止派发新行；没跑到的行**不会**被写回，因此它们的
    「最后更新时间」保持原样，下一轮自然会被重新捞起来。这是断点续跑的全部机制。
    """
    now = now or datetime.now(timezone.utc)
    deadline = (
        time.monotonic() + settings.soft_deadline_seconds
        if settings.soft_deadline_seconds
        else None
    )
    report = RunReport()
    say = progress or (lambda _: None)
    f = settings.fields

    def finish(
        row: Row,
        verdict: analyze.Verdict,
        snapshot,
        *,
        status: str,
        credits: int,
        touch_tags: bool = True,
    ) -> Outcome:
        """把判定结果落成待写字段。所有路径都汇总到这里，保证列的写法一致。

        touch_tags=False 用于「这一轮没获得关于这篇笔记的任何新信息」的情况
        （链接识别不了、第一次取不到内容）。这时必须完全不碰流量状态——
        否则易变标签会被当成「本轮判定为不需要」而摘掉，等于用一次失败
        抹掉上一次的真实结论。
        """
        fields = _base_fields(settings, status=status, notes=verdict.notes, now=now)

        if touch_tags:
            merged = tags.merge(
                row.current_tags,
                verdict.tags,
                settings.tags.namespace(),
                known_options=known_options,
            )
            if merged.dropped_unknown:
                fields[f.failure_reason] = (
                    fields[f.failure_reason]
                    + f"；这些标签在「{f.traffic_status}」里还没建选项，已跳过："
                    + "、".join(merged.dropped_unknown)
                )[:500]
            # 无变化就根本不进 payload：省一次写、避开选项冲突、
            # 也避免让人看到这一行被反复改动。
            if merged.changed:
                fields[f.traffic_status] = merged.final

        # 「评论状态」是运营手工维护的单选列，机器只在确认置顶成功时覆盖。
        # 掉置顶时默认不动这一列（见 PinnedPolicy.overwrite_on_lost 的说明），
        # 事实只写进诊断信息。
        if verdict.pin is analyze.Pin.SUCCESS:
            fields[f.comment_status] = settings.pinned.success_value
        elif settings.pinned.overwrite_on_lost and verdict.pin in (
            analyze.Pin.REPLACED, analyze.Pin.LOST, analyze.Pin.SEED_MISSING
        ):
            fields[f.comment_status] = settings.pinned.lost_value

        if row.parsed.platform:
            fields[f.platform] = "小红书" if row.parsed.platform == "xhs" else "抖音"

        if snapshot is not None:
            if snapshot.comment_count is not None:
                # 先把旧值搬到「上次评论数」，公式列才能算出增量。
                if row.previous_comment_count is not None:
                    fields[f.previous_comment_count] = row.previous_comment_count
                fields[f.comment_count] = snapshot.comment_count
            if snapshot.like_count is not None:
                fields[f.like_count] = snapshot.like_count
            if snapshot.collect_count is not None:
                fields[f.collect_count] = snapshot.collect_count
            fields[f.pinned_comment] = analyze.format_pinned(snapshot)
            fields[f.comment_digest] = analyze.format_digest(snapshot, settings.digest)

        return Outcome(row.record_id, status, fields, "；".join(verdict.notes)[:200], credits)

    def work(row: Row) -> Outcome:
        if not row.parsed.usable:
            verdict = analyze.Verdict(notes=[row.parsed.describe_failure()])
            return finish(row, verdict, None, status=STATUS_SKIPPED, credits=0, touch_tags=False)

        if not forced and row.in_cooldown(settings, now):
            return Outcome(row.record_id, STATUS_COOLDOWN, {},
                           f"{settings.safety.cooldown_seconds} 秒内刚刷过，跳过（不计费）", 0)

        snapshot, error, credits = _fetch_one(
            row, api_key, settings, now=now, deadline=deadline, timeout=timeout
        )

        # —— 取不到内容：两击定罪 ——
        if snapshot is None and error is not None and error.kind is protocol.Failure.GONE:
            strikes = (row.consecutive_failures or 0) + 1
            # 上游给出权威结论时（错误码 1008「内容已删除」，规范明写「不要重试」）
            # 不必等第二次——它已经确定了，再等一轮只是让运营晚一天看到。
            if error.definitive or strikes >= settings.safety.strikes_before_gone:
                verdict = analyze.gone_verdict(settings, error.operator_text())
                outcome = finish(row, verdict, None, status=STATUS_GONE, credits=credits)
            else:
                verdict = analyze.suspect_verdict(settings, strikes, error.operator_text())
                outcome = finish(row, verdict, None, status=STATUS_SUSPECT,
                                 credits=credits, touch_tags=False)
            outcome.fields[f.consecutive_failures] = strikes
            return outcome

        # —— 取不到内容且不是「确认不存在」：只记，绝不打标签 ——
        if snapshot is None:
            reason = error.operator_text() if error else "没有拿到任何数据"
            outcome = Outcome(
                row.record_id,
                STATUS_FAILED,
                _base_fields(settings, status=STATUS_FAILED, notes=[reason], now=now),
                reason,
                credits,
            )
            outcome.fields[f.consecutive_failures] = (row.consecutive_failures or 0) + 1
            return outcome

        verdict = analyze.decide(
            snapshot,
            settings,
            previous_comment_count=row.previous_comment_count,
            age_hours=row.age_hours(now),
            expected_pinned=row.expected_pinned,
            current_tags=row.current_tags,          # 热度档位的棘轮要看现有档位
            previous_comment_status=row.comment_status,
        )
        if error is not None:
            verdict.notes.append(f"（detail 未取到：{error.operator_text()[:120]}）")
        if snapshot.points_balance is not None:
            report.points_balance = snapshot.points_balance

        outcome = finish(row, verdict, snapshot, status=STATUS_OK, credits=credits)
        outcome.fields[f.consecutive_failures] = 0
        return outcome

    pending = list(rows)
    try:
        with ThreadPoolExecutor(max_workers=max(1, settings.max_concurrency)) as pool:
            for outcome in pool.map(work, pending):
                report.outcomes.append(outcome)
                say(f"  {outcome.record_id} → {outcome.status} {outcome.reason}".rstrip())
    except _Abort as abort:
        report.aborted_reason = abort.reason

    done = {o.record_id for o in report.outcomes}
    missed = [r for r in pending if r.record_id not in done]
    if missed and not report.aborted_reason:
        report.aborted_reason = f"{len(missed)} 行未处理（到达软截止），留给下一轮"

    _apply_circuit_breaker(report, settings)
    return report


def _apply_circuit_breaker(report: RunReport, settings: Settings) -> None:
    """一批里失效比例异常偏高 → 判定为上游故障，撤销所有标签写入。

    几百条笔记不可能在同一小时里被集体删除。真发生这种事，一定是上游挂了或者
    错误话术改版了，而不是内容真出事。宁可这一轮什么都不写，也不能把整张表刷红。
    """
    total = len(report.outcomes)
    if total < settings.safety.breaker_min_sample:
        return
    gone = sum(1 for o in report.outcomes if o.status in (STATUS_GONE, STATUS_SUSPECT))
    if gone / total <= settings.safety.breaker_gone_ratio:
        return

    report.breaker_tripped = True
    field_name = settings.fields.traffic_status
    for outcome in report.outcomes:
        outcome.fields.pop(field_name, None)
        if outcome.status in (STATUS_GONE, STATUS_SUSPECT):
            outcome.status = STATUS_FAILED
            outcome.fields[settings.fields.refresh_status] = STATUS_FAILED
            outcome.fields[settings.fields.failure_reason] = (
                f"本批 {gone}/{total} 行都取不到内容，疑似上游故障而非内容失效，"
                "本轮不改流量状态，请稍后复查"
            )


# ---------- 与飞书表的对接 ----------


def load_rows(
    table: feishu.Bitable,
    settings: Settings,
    *,
    only_record_ids: Optional[list[str]] = None,
    only_due: bool = True,
    only_queued: bool = False,
    now: Optional[datetime] = None,
    max_records: Optional[int] = None,
) -> list[Row]:
    """从表里读出待刷新的行。

    分层刷新在这里落地，而且只是一个过滤条件，不是一套调度代码。
    """
    f = settings.fields
    now = now or datetime.now(timezone.utc)

    filter_spec: Optional[dict[str, Any]] = None
    if only_record_ids is None:
        conditions = [{"field_name": f.monitoring, "operator": "is", "value": ["true"]}]
        if only_queued:
            conditions.append({"field_name": f.queued, "operator": "is", "value": ["true"]})
        filter_spec = {"conjunction": "and", "conditions": conditions}

    records = table.search(f.must_read(), filter_spec=filter_spec, max_records=max_records)

    wanted = set(only_record_ids or [])
    result: list[Row] = []
    for record in records:
        record_id = record.get("record_id") or ""
        if wanted and record_id not in wanted:
            continue
        cells = record.get("fields") or {}
        row = Row(
            record_id=record_id,
            link_cell=feishu.read_text(cells.get(f.link)),
            publish_time_ms=feishu.read_timestamp_ms(cells.get(f.publish_time)),
            expected_pinned=feishu.read_text(cells.get(f.expected_pinned)),
            current_tags=feishu.read_multi_select(cells.get(f.traffic_status)),
            previous_comment_count=feishu.read_int(cells.get(f.comment_count)),
            last_updated_ms=feishu.read_timestamp_ms(cells.get(f.last_updated)),
            consecutive_failures=feishu.read_int(cells.get(f.consecutive_failures)) or 0,
            comment_status=feishu.read_text(cells.get(f.comment_status)),
            queued=feishu.read_bool(cells.get(f.queued)),
        )
        # 手动触发时无视分层节流——人明确要求刷新，就该刷。
        if wanted or row.queued or not only_due or row.is_due(settings, now):
            result.append(row)
    return result


def write_back(table: feishu.Bitable, report: RunReport) -> int:
    updates = [
        {"record_id": o.record_id, "fields": o.fields}
        for o in report.outcomes
        if o.fields
    ]
    return table.batch_update(updates) if updates else 0
