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

from . import analyze, feishu, protocol, providers, tags, transport
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
    # 积分只对 SocialDataX 有意义。双通道之后两家单价差 10 倍（抖音），
    # 唯一能跨供应商相加的单位是钱，所以真正的账在 cost_yuan 上。
    credits: int = 0
    cost_yuan: float = 0.0


@dataclass
class RunReport:
    outcomes: list[Outcome] = field(default_factory=list)
    aborted_reason: str = ""
    # 真正的故障（Key 失效、积分耗尽）才置 True。
    # 「有几行没跑完，留给下一轮」也会填 aborted_reason，但那是正常运行——
    # 两者必须分开，否则定时任务会把正常的分批执行当成失败反复重启。
    fatal: bool = False
    breaker_tripped: bool = False
    points_balance: Optional[int] = None
    # 本轮实际用过的供应商，以及降级次数——写进日志，免得「怎么突然贵/便宜了」查不出来。
    used_providers: dict[str, int] = field(default_factory=dict)
    failovers: int = 0

    @property
    def credits(self) -> int:
        return sum(o.credits for o in self.outcomes)

    @property
    def cost_yuan(self) -> float:
        return sum(o.cost_yuan for o in self.outcomes)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for outcome in self.outcomes:
            tally[outcome.status] = tally.get(outcome.status, 0) + 1
        return tally

    def summary(self) -> str:
        parts = [f"{k} {v}" for k, v in sorted(self.counts().items())]
        line = f"处理 {len(self.outcomes)} 行（{'，'.join(parts) or '无'}），"
        line += f"花费 ≈ ¥{self.cost_yuan:.2f}"
        if self.used_providers:
            via = "、".join(f"{k} {v} 次" for k, v in sorted(self.used_providers.items()))
            line += f"（{via}）"
        if self.failovers:
            line += f"，其中 {self.failovers} 次是主通道失败后降级完成的"
        if self.points_balance is not None:
            line += f"，SocialDataX 余额 {self.points_balance} 积分 ≈ ¥{self.points_balance / 100:.2f}"
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


def _looks_alive(snapshot: Optional[analyze.Snapshot]) -> bool:
    """这一轮有没有拿到「这篇内容还活着」的正面证据。

    有评论、或者评论数大于 0，就算活着。评论数为 0 不算证据——
    刚发的帖子和已经没了的帖子都是 0。
    """
    if snapshot is None:
        return False
    return bool(snapshot.comments) or bool(snapshot.comment_count)


@dataclass
class _Spend:
    """这一次「取数」总共花掉了什么。跨供应商唯一能相加的单位是钱。

    是累加而不是覆盖：一次降级会真的产生两笔调用，其中失败的那笔也可能已经扣了钱。
    只记成功的那笔，账面就会比账单少，而少报成本的监控系统没有任何价值。
    """

    credits: int = 0
    yuan: float = 0.0
    provider: str = ""
    tally: dict[str, int] = field(default_factory=dict)


def _was_billed(provider_name: str, result: protocol.Result) -> bool:
    """这次失败是不是照样扣了钱。

    TikHub 自己的接口文档写着：传入不存在的笔记 ID，接口**正常响应、正常计费**，
    只是 data 里是个空壳。我们把那个空壳翻译成 GONE，但钱是真花了。
    判据是「HTTP 层面成功、业务层面失败」——鉴权失败(401)、参数非法(400)
    这类根本没进业务层的，对方明说不计费。

    是不是收费按供应商定（Provider.bills_failed_lookups），不能一刀切：
    SocialDataX 那边这件事还没验过，凭猜测把账单虚高比少报更糟。
    """
    if not isinstance(result, protocol.Err):
        return False
    if not providers.get_provider(provider_name).bills_failed_lookups:
        return False
    status = result.http_status
    return status is not None and 200 <= status < 300


def _call_once(
    provider_name: str,
    api_key: str,
    call: ToolCall,
    *,
    deadline: Optional[float],
    timeout: float,
) -> protocol.Result:
    provider = providers.get_provider(provider_name)
    request = provider.build(api_key, call.platform, call.purpose, call.arguments)
    response = transport.request_with_retry(
        request.method,
        request.url,
        request.headers,
        request.body,
        timeout=timeout,
        deadline=deadline,
        # 传输层只按 HTTP 状态判重试。业务错误可能走 HTTP 200 + body 里的字段，
        # 传输层看不见，必须由下面按解析结果处理。
        should_retry=lambda r: r.status == 0 or r.status >= 500,
    )
    return provider.parse(
        call.platform, call.purpose,
        response.status, response.content_type, response.body,
        response.request_id, api_key,
    )


def _call(
    call: ToolCall,
    keys: dict[str, str],
    settings: Settings,
    *,
    deadline: Optional[float],
    timeout: float,
    disabled: set[str],
    spend: _Spend,
) -> protocol.Result:
    """双通道：主通道失败就降到备胎，而不是让整批停下。

    AUTH / QUOTA 在单通道时代是「整批立刻停」，因为再打也是白打。有了备胎之后
    它们的正确处置变成「把这家标记为本轮不可用，换一家继续」——只有**所有**
    通道都倒下才轮到 _Abort。这就是双通道真正值钱的地方：
    半夜 TikHub 余额见底不再意味着整晚的监控全丢。
    """
    order = providers.usable_order(settings.channels, call.platform, keys, disabled)
    if not order:
        return protocol.Err(
            protocol.Failure.AUTH, "no_channel",
            f"{call.platform} 没有可用的数据通道了（key 没配，或本轮全部失效）",
            definitive=True,
        )

    last: protocol.Result = protocol.Err(protocol.Failure.UNKNOWN, "no_attempt", "没有发起任何请求")
    for index, name in enumerate(order):
        result = _call_once(name, keys[name], call, deadline=deadline, timeout=timeout)

        if isinstance(result, protocol.Ok) or _was_billed(name, result):
            spend.provider = name
            spend.yuan += providers.get_provider(name).yuan_per_call(call.platform, call.purpose)
            spend.credits += (getattr(result, "points_cost", None)
                              or (10 if name == providers.SOCIALDATAX else 0))
            spend.tally[name] = spend.tally.get(name, 0) + 1

        if isinstance(result, protocol.Ok):
            return result

        last = result
        if result.kind in (protocol.Failure.AUTH, protocol.Failure.QUOTA):
            # 这家这一轮别再用了：600 行 × 每行都撞一次同样的 401，
            # 既慢又会把限流打出来。
            disabled.add(name)
        if result.kind not in providers.FAILOVER_KINDS or index == len(order) - 1:
            return result
        # 换下一家。上面已经把这笔（如果被计费了的话）记进账了。

    return last


def _fetch_one(
    row: Row,
    keys: dict[str, str],
    settings: Settings,
    *,
    now: datetime,
    deadline: Optional[float],
    timeout: float,
    disabled: set[str],
    tally: dict[str, int],
) -> tuple[Optional[analyze.Snapshot], Optional[protocol.Err], int, float, int]:
    """跑完一行需要的全部调用。

    返回（快照, 终局错误, 消耗积分, 花费元, 降级次数）。
    """
    calls = plan_calls(row, settings, now)
    if not calls:
        return None, protocol.Err(
            protocol.Failure.UNKNOWN, "bad_link", row.parsed.describe_failure()
        ), 0, 0.0, 0

    snapshot: Optional[analyze.Snapshot] = None
    credits = 0
    yuan = 0.0
    failovers = 0

    def attempt(c: ToolCall) -> protocol.Result:
        nonlocal credits, yuan, failovers
        spend = _Spend()
        primary = settings.channels.primary(c.platform)
        result = _call(c, keys, settings, deadline=deadline, timeout=timeout,
                       disabled=disabled, spend=spend)
        credits += spend.credits
        yuan += spend.yuan
        for name, count in spend.tally.items():
            tally[name] = tally.get(name, 0) + count
        if spend.provider and spend.provider != primary:
            failovers += 1
        return result

    for call in calls:
        result = attempt(call)

        if isinstance(result, protocol.Err):
            if result.kind in protocol.FATAL:
                # 走到这里说明**所有**通道都是 AUTH/QUOTA，备胎也没了。
                raise _Abort(str(result))
            if result.kind is protocol.Failure.RATE_LIMIT:
                # 退避一次再试。再限流就把这一行留给下一轮，别拖垮整批。
                time.sleep(result.retry_after_seconds or 5.0)
                result = attempt(call)
                if isinstance(result, protocol.Err):
                    if result.kind in protocol.FATAL:
                        raise _Abort(str(result))
                    return snapshot, result, credits, yuan, failovers
            elif result.kind is protocol.Failure.GONE and not _looks_alive(snapshot):
                # 「内容没了」是行级结论，不管它是从评论接口还是 detail 冒出来的。
                # TikHub 通道上最干净的死亡信号恰恰在 detail 里（小红书 data 为 []、
                # 抖音 aweme_detail 为 null + filter_list 命中），漏掉它
                # 等于把双通道最好的一个能力扔了。
                #
                # 但只在**没有活着的证据**时才认：评论接口刚拿回 200 条评论、
                # detail 却说笔记不存在，那是上游自相矛盾，这时候信 detail
                # 就是拿一次上游抖动去杀一条好帖子。
                return None, result, credits, yuan, failovers
            else:
                # 评论接口失败 = 这一行的结论；detail 失败只是少几个数字，
                # 已经拿到的评论数据仍然有效，不该整行判死。
                if call.purpose == "comments":
                    return snapshot, result, credits, yuan, failovers
                continue

        assert isinstance(result, protocol.Ok)
        if call.purpose == "comments":
            snapshot = analyze.read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            analyze.merge_detail(snapshot, result.data)

    return snapshot, None, credits, yuan, failovers


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
    api_key: Any,
    settings: Settings,
    *,
    now: Optional[datetime] = None,
    known_options: Optional[list[str]] = None,
    comment_status_options: Optional[list[str]] = None,
    forced: bool = False,
    timeout: float = 30.0,
    progress: Optional[Callable[[str], None]] = None,
) -> RunReport:
    """刷新一批行。不写回，只算结果——写回由调用方决定时机。

    api_key 可以是一个字符串（只有一家供应商时的老写法，继续支持），
    也可以是 {"tikhub": "...", "socialdatax": "..."}——双通道要用后者。

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

    keys = providers.credentials(api_key)
    # 本轮已经确认不可用的通道（Key 失效、余额耗尽）。多线程共享，
    # 但只做「加一个字符串」这一种写入，GIL 下天然安全，不值得上锁。
    disabled: set[str] = set()
    tally: dict[str, int] = {}

    def finish(
        row: Row,
        verdict: analyze.Verdict,
        snapshot,
        *,
        status: str,
        credits: int,
        cost_yuan: float = 0.0,
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

        # 「评论状态」也是人机共用的多选列，走和流量状态一模一样的合并算法：
        # 机器管置顶那三个值，人工维护的「评论是否显示」之类原样保留。
        if touch_tags:
            wanted = analyze.comment_status_values(verdict.pin, row.comment_status, settings)
            # None 表示这一轮判不了（抖音、或没填种子关键词）——原样保留，
            # 而不是写个空集合把上一轮的置顶结论摘掉。
            if wanted is not None:
                merged_status = tags.merge(
                    row.comment_status,
                    wanted,
                    settings.comment_status.namespace(),
                    known_options=comment_status_options,
                )
                if merged_status.changed:
                    fields[f.comment_status] = merged_status.final
                if merged_status.dropped_unknown:
                    fields[f.failure_reason] = (
                        fields[f.failure_reason]
                        + f"；这些值在「{f.comment_status}」里还没建选项，已跳过："
                        + "、".join(merged_status.dropped_unknown)
                    )[:500]

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

        return Outcome(row.record_id, status, fields, "；".join(verdict.notes)[:200],
                       credits, cost_yuan)

    def work(row: Row) -> Outcome:
        if not row.parsed.usable:
            verdict = analyze.Verdict(notes=[row.parsed.describe_failure()])
            return finish(row, verdict, None, status=STATUS_SKIPPED, credits=0, touch_tags=False)

        if not forced and row.in_cooldown(settings, now):
            return Outcome(row.record_id, STATUS_COOLDOWN, {},
                           f"{settings.safety.cooldown_seconds} 秒内刚刷过，跳过（不计费）", 0)

        snapshot, error, credits, cost_yuan, failovers = _fetch_one(
            row, keys, settings, now=now, deadline=deadline, timeout=timeout,
            disabled=disabled, tally=tally,
        )
        report.failovers += failovers

        # —— 取不到内容：两击定罪 ——
        if snapshot is None and error is not None and error.kind is protocol.Failure.GONE:
            strikes = (row.consecutive_failures or 0) + 1
            # 上游给出权威结论时（错误码 1008「内容已删除」，规范明写「不要重试」）
            # 不必等第二次——它已经确定了，再等一轮只是让运营晚一天看到。
            if error.definitive or strikes >= settings.safety.strikes_before_gone:
                verdict = analyze.gone_verdict(settings, error.operator_text())
                outcome = finish(row, verdict, None, status=STATUS_GONE,
                                 credits=credits, cost_yuan=cost_yuan)
            else:
                verdict = analyze.suspect_verdict(settings, strikes, error.operator_text())
                outcome = finish(row, verdict, None, status=STATUS_SUSPECT,
                                 credits=credits, cost_yuan=cost_yuan, touch_tags=False)
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
                cost_yuan,
            )
            outcome.fields[f.consecutive_failures] = (row.consecutive_failures or 0) + 1
            return outcome

        verdict = analyze.decide(
            snapshot,
            settings,
            previous_comment_count=row.previous_comment_count,
            age_hours=row.age_hours(now),
            expected_pinned=row.expected_pinned,
            current_tags=row.current_tags,                    # 热度档位的棘轮要看现有档位
            current_comment_status=row.comment_status,        # 区分「掉了」和「从来没有」
        )
        if error is not None:
            verdict.notes.append(f"（detail 未取到：{error.operator_text()[:120]}）")
        if snapshot.censored:
            # 只是提醒，不打标签——这个字段的语义还没在真被封的帖子上验过。
            verdict.notes.append("⚠ 上游把这条标成了审核中/受限，请人工确认")
        if snapshot.points_balance is not None:
            report.points_balance = snapshot.points_balance

        outcome = finish(row, verdict, snapshot, status=STATUS_OK,
                         credits=credits, cost_yuan=cost_yuan)
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
        report.fatal = True

    done = {o.record_id for o in report.outcomes}
    missed = [r for r in pending if r.record_id not in done]
    if missed and not report.aborted_reason:
        report.aborted_reason = f"{len(missed)} 行未处理（到达软截止），留给下一轮"

    report.used_providers = dict(tally)
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
            comment_status=feishu.read_multi_select(cells.get(f.comment_status)),
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
