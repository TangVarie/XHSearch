#!/usr/bin/env python3
"""把纯逻辑模块打包成一个自包含的文件，用于粘进扣子（Coze）代码节点。

扣子代码节点不能 import 本地模块，只能粘一整段。如果手工复制粘贴，判定口径
迟早会和服务端那份跑偏——白天点按钮和夜里批量跑结论不一致，是这类系统里最
难查也最伤信任的一类 bug。所以这里从同一份源码生成，永远只有一个真相。

    python3 tools/build_coze_node.py > coze_node.py

生成物里的网络层用 requests_async（扣子唯一允许的 HTTP 客户端；它禁用
http.client，所以 urllib 在那边不能用）。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
PKG = ROOT / "xhsearch"

# 依赖顺序。纯逻辑模块，全部不碰网络。
MODULES = ["config.py", "links.py", "protocol.py", "providers.py",
           "tags.py", "analyze.py", "rows.py"]

HEADER = '''"""
=============================================================================
  自动生成，请勿直接编辑。
  改动请改 xhsearch/ 下的源码，然后重新跑：
      python3 tools/build_coze_node.py > coze_node.py
=============================================================================

扣子（Coze）代码节点用。把整个文件粘进代码节点即可。

入参（开始节点配置）：
    mode        String   "sweep"（分层巡检）或 "row"（刷指定行）
    record_ids  String   逗号分隔的 record_id；mode=row 时必填
    tikhub_key  String   TikHub API Key（主通道；留空则只走 SocialDataX）
    api_key     String   SocialDataX API Key（备胎；留空则只走 TikHub）
    app_id      String   飞书自建应用 app_id
    app_secret  String   飞书自建应用 app_secret
    app_token   String   多维表格 app_token（URL 里 /base/ 后面那段）
    table_id    String   数据表 id

出参：全部是扁平标量。刻意不返回数组——飞书 HTTP 节点只能整体引用数组，
无法引用数组里的单个元素，返回数组等于在飞书侧不可用。
    ok          Boolean
    processed   Number
    credits     Number    本轮花费的「分」（¥0.01），跨供应商唯一能相加的单位
    balance     Number    SocialDataX 的积分余额；只走 TikHub 时是 0
    message     String

⚠️ tikhub_key 和 api_key 至少要填一个。两个都填就自动降级：
主通道网络故障 / Key 失效 / 余额耗尽时换另一家继续，而不是整轮停摆。

⚠️ 代码节点硬上限 60 秒。SOFT_DEADLINE 设成 45 秒，到点就停止派发新行，
未处理的行不写回「最后更新时间」，下一轮触发自然会重新捞起来——这就是断点续跑
的全部机制，不需要额外的队列。600 行按每轮 40 行算，约 30 分钟排干。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterable, Literal, Optional, Protocol, Sequence

import requests_async as requests  # 扣子内置；禁用 http.client，所以不能用 urllib

SOFT_DEADLINE = 45.0
BATCH_LIMIT = 40  # 每轮最多处理多少行，按 60 秒上限反推
'''

FOOTER = '''

# =============================================================================
#  网络层：扣子专用（异步）
# =============================================================================

FEISHU_BASE = "https://open.feishu.cn/open-apis"


async def _post_json(url: str, headers: dict, payload: Any, timeout: float = 25.0):
    response = await requests.post(url, headers=headers, json=payload, timeout=timeout)
    return response


async def feishu_token(app_id: str, app_secret: str) -> str:
    response = await _post_json(
        f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
        {"Content-Type": "application/json; charset=utf-8"},
        {"app_id": app_id, "app_secret": app_secret},
    )
    data = json.loads(response.text)
    if data.get("code") not in (0, None):
        raise RuntimeError(f"取飞书 token 失败 [{data.get('code')}] {data.get('msg')}")
    return data["tenant_access_token"]


async def feishu_search(token, app_token, table_id, field_names, filter_spec, page_size=BATCH_LIMIT):
    url = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/search?page_size={page_size}"
    body = {"field_names": list(field_names), "automatic_fields": False}
    if filter_spec:
        body["filter"] = filter_spec
    response = await _post_json(url, {"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json; charset=utf-8"}, body)
    data = json.loads(response.text)
    if data.get("code") not in (0, None):
        raise RuntimeError(f"读表失败 [{data.get('code')}] {data.get('msg')}")
    return (data.get("data") or {}).get("items") or []


async def feishu_batch_update(token, app_token, table_id, updates):
    if not updates:
        return 0
    url = f"{FEISHU_BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update"
    response = await _post_json(url, {"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json; charset=utf-8"},
                                {"records": updates})
    data = json.loads(response.text)
    if data.get("code") not in (0, None):
        raise RuntimeError(f"写回失败 [{data.get('code')}] {data.get('msg')}")
    return len(updates)


async def _one_shot(name: str, api_key: str, call: ToolCall, deadline: float):
    """打一次某一家的接口。

    ⚠️ 两家的业务错误都可能走 HTTP 200：SocialDataX 靠 body 里的 `code`，
    TikHub 靠 data 的形状。只看 status_code 的写法会把每一个「笔记已删除」
    当成成功。归一化和分类全在 providers 层，这里只负责发包。
    """
    provider = get_provider(name)
    request = provider.build(api_key, call.platform, call.purpose, call.arguments)
    try:
        if request.method == "GET":
            response = await requests.get(request.url, headers=request.headers, timeout=25.0)
        else:
            response = await requests.post(
                request.url, headers=request.headers,
                data=request.body.encode("utf-8"), timeout=25.0)
    except Exception as exc:                           # noqa: BLE001
        return Err(Failure.TRANSPORT, "network", f"{type(exc).__name__}: {exc}")

    content_type, request_id = "", ""
    try:
        content_type = response.headers.get("content-type", "") or ""
        request_id = response.headers.get("x-request-id", "") or ""
    except Exception:                                  # noqa: BLE001
        pass
    return provider.parse(call.platform, call.purpose,
                          getattr(response, "status_code", 0), content_type,
                          response.text, request_id, api_key)


async def channel_call(keys: dict, settings, call: ToolCall, semaphore, deadline: float,
                       disabled: set):
    """双通道：主通道倒下就换备胎，而不是让整轮停摆。

    返回 (结果, 计费明细 {供应商: 次数})。GONE 不触发降级——那是行级结论不是
    通道故障，换一家只会再花一次钱得到同一个答案。

    ⚠️ 计费明细里包含**失败但照样扣了钱**的调用：TikHub 对不存在的笔记
    「正常响应、正常计费」，只是 data 里是空壳。只记成功的话账面会比账单少。
    判据是「HTTP 成功、业务失败」+ 这家确实收这种钱（bills_failed_lookups）——
    401/400 那种没进业务层的对方明说不计费，SocialDataX 那边则是还没验过。
    """
    async with semaphore:
        if time.monotonic() >= deadline:
            return Err(Failure.TRANSPORT, "deadline", "已到软截止，留给下一轮"), {}

        order = usable_order(settings.channels, call.platform, keys, disabled)
        if not order:
            return Err(Failure.AUTH, "no_channel",
                       f"{call.platform} 没有可用的数据通道了", definitive=True), {}

        billed = {}
        last = Err(Failure.UNKNOWN, "no_attempt", "没有发起任何请求")
        for index, name in enumerate(order):
            result = await _one_shot(name, keys[name], call, deadline)
            charged = not isinstance(result, Err) or (
                get_provider(name).bills_failed_lookups
                and result.http_status is not None
                and 200 <= result.http_status < 300)
            if charged:
                billed[name] = billed.get(name, 0) + 1
            if not isinstance(result, Err):
                return result, billed
            last = result
            if result.kind in (Failure.AUTH, Failure.QUOTA):
                # 整轮拉黑：40 行各撞一次同样的 401 既慢又会把限流打出来。
                disabled.add(name)
            if result.kind not in FAILOVER_KINDS or index == len(order) - 1:
                return result, billed
        return last, billed


# =============================================================================
#  扣子入口
# =============================================================================


async def main(args: Any) -> dict:
    p = args.params
    settings = Settings()
    settings.soft_deadline_seconds = SOFT_DEADLINE
    f = settings.fields
    deadline = time.monotonic() + SOFT_DEADLINE
    now = datetime.now(timezone.utc)

    mode = (p.get("mode") or "sweep").strip()
    wanted = [x.strip() for x in (p.get("record_ids") or "").split(",") if x.strip()]

    try:
        token = await feishu_token(p["app_id"], p["app_secret"])
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0, "message": str(exc)}

    filter_spec = None
    if mode != "row":
        filter_spec = {"conjunction": "and", "conditions": [
            {"field_name": f.monitoring, "operator": "is", "value": ["true"]}]}

    try:
        records = await feishu_search(token, p["app_token"], p["table_id"],
                                      f.must_read(), filter_spec)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0, "message": str(exc)}

    rows = []
    for record in records:
        record_id = record.get("record_id") or ""
        if wanted and record_id not in wanted:
            continue
        cells = record.get("fields") or {}
        row = Row(
            record_id=record_id,
            link_cell=_cell_text(cells.get(f.link)),
            publish_time_ms=_cell_ms(cells.get(f.publish_time)),
            expected_pinned=_cell_text(cells.get(f.expected_pinned)),
            current_tags=_cell_tags(cells.get(f.traffic_status)),
            previous_comment_count=_cell_int(cells.get(f.comment_count)),
            last_updated_ms=_cell_ms(cells.get(f.last_updated)),
            consecutive_failures=_cell_int(cells.get(f.consecutive_failures)) or 0,
            comment_status=_cell_tags(cells.get(f.comment_status)),
            queued=bool(cells.get(f.queued)),
        )
        if wanted or row.queued or row.is_due(settings, now):
            rows.append(row)
    rows = rows[:BATCH_LIMIT]

    if not rows:
        return {"ok": True, "processed": 0, "credits": 0, "balance": 0, "message": "没有到期的行"}

    keys = {k: v for k, v in {
        "tikhub": (p.get("tikhub_key") or "").strip(),
        "socialdatax": (p.get("api_key") or "").strip(),
    }.items() if v}
    if not keys:
        return {"ok": False, "processed": 0, "credits": 0, "balance": 0,
                "message": "tikhub_key 和 api_key 至少要填一个"}

    semaphore = asyncio.Semaphore(settings.max_concurrency)
    # 本轮已确认不可用的通道。协程之间共享，只做「加一个字符串」这一种写入。
    disabled = set()
    results = await asyncio.gather(
        *[_process(row, keys, settings, now, semaphore, deadline, disabled,
                   wanted=bool(wanted))
          for row in rows],
        return_exceptions=True,
    )

    updates, cents, balance, gone, used = [], 0, 0, 0, {}
    for row, result in zip(rows, results):
        if isinstance(result, Exception):
            continue
        fields, spent, bal, is_gone, tally = result
        cents += spent
        balance = bal or balance
        gone += 1 if is_gone else 0
        for name, count in tally.items():
            used[name] = used.get(name, 0) + count
        if fields:
            updates.append({"record_id": row.record_id, "fields": fields})

    # 全局熔断：一批里失效比例异常偏高 → 上游故障，撤销所有标签写入。
    tripped = len(updates) >= settings.safety.breaker_min_sample and \\
        gone / max(len(updates), 1) > settings.safety.breaker_gone_ratio
    if tripped:
        for update in updates:
            update["fields"].pop(f.traffic_status, None)

    try:
        written = await feishu_batch_update(token, p["app_token"], p["table_id"], updates)
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "processed": 0, "credits": cents, "balance": balance,
                "message": f"算完了但写回失败：{exc}"}

    via = "、".join(f"{k} {v} 次" for k, v in sorted(used.items())) or "无调用"
    message = f"刷新 {written} 行，花费 ≈ ¥{cents / 100:.2f}（{via}）"
    if disabled:
        message += f"；⚠ 本轮 {'、'.join(sorted(disabled))} 通道不可用，已降级"
    if tripped:
        message += "；⚠ 本批失效比例异常，已熔断，未改流量状态"
    return {"ok": True, "processed": written, "credits": cents,
            "balance": balance, "message": message}


async def _process(row, keys, settings, now, semaphore, deadline, disabled, *, wanted):
    """单行处理。返回（待写字段, 花费的分, 余额, 是否判定失效, 各通道调用次数）。"""
    f = settings.fields
    if not row.parsed.usable:
        return (_base_fields(settings, "跳过", [row.parsed.describe_failure()], now),
                0, 0, False, {})
    if not wanted and row.in_cooldown(settings, now):
        return (None, 0, 0, False, {})

    snapshot, error, cents, balance, tally = None, None, 0, 0, {}
    for call in plan_calls(row, settings, now):
        result, billed = await channel_call(keys, settings, call, semaphore,
                                            deadline, disabled)
        for name, count in billed.items():
            tally[name] = tally.get(name, 0) + count
            cents += count * round(get_provider(name).yuan_per_call(
                call.platform, call.purpose) * 100)
        if isinstance(result, Err):
            if result.kind in FATAL:
                # 走到这里说明所有通道都是 AUTH/QUOTA，备胎也没了。
                return (_base_fields(settings, "刷新失败", [str(result)], now),
                        cents, balance, False, tally)
            if result.kind is Failure.GONE and not (
                    snapshot and (snapshot.comments or snapshot.comment_count)):
                # 死亡信号可能来自 detail（小红书 data 为 []、抖音 filter_list 命中）。
                # 但评论刚拿回一堆、detail 却说没了，那是上游自相矛盾，不能认。
                error = result
                snapshot = None
                break
            if call.purpose == "comments":
                error = result
                break
            continue
        balance = result.points_balance or balance
        if call.purpose == "comments":
            snapshot = read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            merge_detail(snapshot, result.data)

    if snapshot is None and error is not None and error.kind is Failure.GONE:
        strikes = (row.consecutive_failures or 0) + 1
        # 错误码 1008「内容已删除」是权威结论，规范明写不要重试 —— 直接定罪。
        convicted = error.definitive or strikes >= settings.safety.strikes_before_gone
        verdict = gone_verdict(settings, error.operator_text()) if convicted \\
            else suspect_verdict(settings, strikes, error.operator_text())
        # 第一击不碰流量状态：这一轮没获得关于这篇笔记的任何新信息，
        # 摘掉上一轮的标签等于用一次失败抹掉真实结论。
        fields = _render(row, verdict, None, settings, now,
                         "已失效" if convicted else "疑似受限", touch_tags=convicted)
        fields[f.consecutive_failures] = strikes
        return (fields, cents, balance, convicted, tally)

    if snapshot is None:
        reason = error.operator_text() if error else "没有拿到任何数据"
        fields = _base_fields(settings, "刷新失败", [reason], now)
        fields[f.consecutive_failures] = (row.consecutive_failures or 0) + 1
        return (fields, cents, balance, False, tally)

    verdict = decide(snapshot, settings,
                     previous_comment_count=row.previous_comment_count,
                     age_hours=row.age_hours(now),
                     expected_pinned=row.expected_pinned,
                     current_tags=row.current_tags,
                     current_comment_status=row.comment_status)
    if snapshot.censored:
        verdict.notes.append("⚠ 上游把这条标成了审核中/受限，请人工确认")
    fields = _render(row, verdict, snapshot, settings, now, "正常")
    fields[f.consecutive_failures] = 0
    return (fields, cents, balance, False, tally)


def _base_fields(settings, status, notes, now):
    f = settings.fields
    return {
        f.refresh_status: status,
        f.failure_reason: "；".join(n for n in notes if n)[:500],
        f.last_updated: int(now.timestamp() * 1000),
        f.queued: False,
    }


def _render(row, verdict, snapshot, settings, now, status, touch_tags=True):
    f = settings.fields
    fields = _base_fields(settings, status, verdict.notes, now)
    if touch_tags:
        merged = merge(row.current_tags, verdict.tags, settings.tags.namespace())
        if merged.changed:
            fields[f.traffic_status] = merged.final
        wanted = comment_status_values(verdict.pin, row.comment_status, settings)
        if wanted is not None:
            merged_status = merge(row.comment_status, wanted,
                                  settings.comment_status.namespace())
            if merged_status.changed:
                fields[f.comment_status] = merged_status.final
    if row.parsed.platform:
        fields[f.platform] = "小红书" if row.parsed.platform == "xhs" else "抖音"
    if snapshot is not None:
        if snapshot.comment_count is not None:
            if row.previous_comment_count is not None:
                fields[f.previous_comment_count] = row.previous_comment_count
            fields[f.comment_count] = snapshot.comment_count
        if snapshot.like_count is not None:
            fields[f.like_count] = snapshot.like_count
        if snapshot.collect_count is not None:
            fields[f.collect_count] = snapshot.collect_count
        fields[f.pinned_comment] = format_pinned(snapshot)
        fields[f.comment_digest] = format_digest(snapshot, settings.digest)
    return fields


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            v if isinstance(v, str) else str((v or {}).get("text") or (v or {}).get("name") or "")
            for v in value
        )
    if isinstance(value, dict):
        return str(value.get("link") or value.get("text") or "")
    return str(value)


def _cell_tags(value):
    if isinstance(value, list):
        return [v if isinstance(v, str) else str((v or {}).get("name") or "")
                for v in value if v]
    return [value] if isinstance(value, str) and value else []


def _cell_int(value):
    if isinstance(value, bool):
        return None
    return int(value) if isinstance(value, (int, float)) else None


def _cell_ms(value):
    number = _cell_int(value)
    if number is None:
        return None
    return number * 1000 if number < 10_000_000_000 else number
'''

# 打包时要剥掉的行：包内相对 import、future import、以及各模块自己的 docstring 头。
_DROP = re.compile(r"^\s*(from\s+\.\S*\s+import|from\s+__future__\s+import|import\s+re$|"
                   r"import\s+json$|import\s+time$)")


def strip_module(text: str) -> str:
    """去掉模块 docstring 和会重复的 import，保留其余全部代码。"""
    # 去掉开头的模块 docstring
    text = re.sub(r'^\s*""".*?"""\s*', "", text, count=1, flags=re.S)
    kept = []
    for line in text.splitlines():
        if _DROP.match(line):
            continue
        # dataclass / typing / enum 的 import 统一放在头部了
        if re.match(r"^\s*from\s+(dataclasses|typing|enum|datetime)\s+import", line):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def build() -> str:
    parts = [HEADER]
    for name in MODULES:
        source = (PKG / name).read_text(encoding="utf-8")
        parts.append(f"\n\n# {'=' * 77}\n#  来自 xhsearch/{name}\n# {'=' * 77}\n")
        parts.append(strip_module(source))
    parts.append(FOOTER)
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    sys.stdout.write(build())
