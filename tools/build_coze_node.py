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
MODULES = ["config.py", "links.py", "protocol.py", "tags.py", "analyze.py", "rows.py"]

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
    api_key     String   SocialDataX API Key
    app_id      String   飞书自建应用 app_id
    app_secret  String   飞书自建应用 app_secret
    app_token   String   多维表格 app_token（URL 里 /base/ 后面那段）
    table_id    String   数据表 id

出参：全部是扁平标量。刻意不返回数组——飞书 HTTP 节点只能整体引用数组，
无法引用数组里的单个元素，返回数组等于在飞书侧不可用。
    ok          Boolean
    processed   Number
    credits     Number
    balance     Number
    message     String

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


async def sdx_call(api_key: str, call: ToolCall, semaphore, deadline: float):
    """打一次 SocialDataX。

    两件必须做对的事：
    1. Accept 必须同时含 application/json 和 text/event-stream，否则硬报 406
    2. 成功走 SSE 分帧，失败走纯 JSON —— 同一接口两种 content-type，都要能解
    """
    async with semaphore:
        if time.monotonic() >= deadline:
            return Err(Failure.TRANSPORT, "deadline", "已到软截止，留给下一轮")
        try:
            response = await requests.post(
                endpoint(call.platform),
                headers=headers(api_key),
                data=build_call(call.tool, call.arguments).encode("utf-8"),
                timeout=25.0,
            )
        except Exception as exc:                       # noqa: BLE001
            return Err(Failure.TRANSPORT, "network", f"{type(exc).__name__}: {exc}")

        content_type = ""
        try:
            content_type = response.headers.get("content-type", "") or ""
        except Exception:                              # noqa: BLE001
            pass
        return parse_response(getattr(response, "status_code", 0), content_type, response.text)


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
            pinned_state=_cell_text(cells.get(f.pinned_state)),
            queued=bool(cells.get(f.queued)),
        )
        if wanted or row.queued or row.is_due(settings, now):
            rows.append(row)
    rows = rows[:BATCH_LIMIT]

    if not rows:
        return {"ok": True, "processed": 0, "credits": 0, "balance": 0, "message": "没有到期的行"}

    semaphore = asyncio.Semaphore(settings.max_concurrency)
    results = await asyncio.gather(
        *[_process(row, p["api_key"], settings, now, semaphore, deadline, wanted=bool(wanted))
          for row in rows],
        return_exceptions=True,
    )

    updates, credits, balance, gone = [], 0, 0, 0
    for row, result in zip(rows, results):
        if isinstance(result, Exception):
            continue
        fields, spent, bal, is_gone = result
        credits += spent
        balance = bal or balance
        gone += 1 if is_gone else 0
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
        return {"ok": False, "processed": 0, "credits": credits, "balance": balance,
                "message": f"算完了但写回失败：{exc}"}

    message = f"刷新 {written} 行，消耗 {credits} 积分 ≈ ¥{credits / 100:.2f}"
    if tripped:
        message += "；⚠ 本批失效比例异常，已熔断，未改流量状态"
    return {"ok": True, "processed": written, "credits": credits,
            "balance": balance, "message": message}


async def _process(row, api_key, settings, now, semaphore, deadline, *, wanted):
    """单行处理。返回（待写字段, 消耗积分, 余额, 是否判定失效）。"""
    f = settings.fields
    if not row.parsed.usable:
        return (_base_fields(settings, "跳过", [row.parsed.describe_failure()], now), 0, 0, False)
    if not wanted and row.in_cooldown(settings, now):
        return (None, 0, 0, False)

    snapshot, error, credits, balance = None, None, 0, 0
    for call in plan_calls(row, settings, now):
        result = await sdx_call(api_key, call, semaphore, deadline)
        if isinstance(result, Err):
            if result.kind in FATAL:
                return (_base_fields(settings, "刷新失败", [str(result)], now), credits, balance, False)
            if call.purpose == "comments":
                error = result
                break
            continue
        credits += result.points_cost or 10
        balance = result.points_balance or balance
        if call.purpose == "comments":
            snapshot = read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            merge_detail(snapshot, result.data)

    if snapshot is None and error is not None and error.kind is Failure.GONE:
        strikes = (row.consecutive_failures or 0) + 1
        convicted = strikes >= settings.safety.strikes_before_gone
        verdict = gone_verdict(settings, error.message) if convicted \\
            else suspect_verdict(settings, strikes, error.message)
        # 第一击不碰流量状态：这一轮没获得关于这篇笔记的任何新信息，
        # 摘掉上一轮的标签等于用一次失败抹掉真实结论。
        fields = _render(row, verdict, None, settings, now,
                         "已失效" if convicted else "疑似受限", touch_tags=convicted)
        fields[f.consecutive_failures] = strikes
        return (fields, credits, balance, convicted)

    if snapshot is None:
        reason = str(error) if error else "没有拿到任何数据"
        fields = _base_fields(settings, "刷新失败", [reason], now)
        fields[f.consecutive_failures] = (row.consecutive_failures or 0) + 1
        return (fields, credits, balance, False)

    verdict = decide(snapshot, settings,
                     previous_comment_count=row.previous_comment_count,
                     age_hours=row.age_hours(now),
                     expected_pinned=row.expected_pinned,
                     previous_pinned_state=row.pinned_state)
    fields = _render(row, verdict, snapshot, settings, now, "正常")
    fields[f.consecutive_failures] = 0
    return (fields, credits, balance, False)


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
        merged = merge(row.current_tags, verdict.tags, settings.tags.namespace(),
                       sticky=settings.tags.sticky())
        if merged.changed:
            fields[f.traffic_status] = merged.final
    if verdict.pinned_state:
        fields[f.pinned_state] = verdict.pinned_state
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
