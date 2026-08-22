"""SocialDataX 协议层：组包、解包、错误归类。

全部是纯函数，不发网络请求——传输层单独放在 transport.py，
这样同一份解析逻辑可以直接粘进扣子代码节点（那里只能用 requests_async）。

走的是 REST 接口而不是 MCP 端点。两条路同一个后端、同样的字段名，但 REST
明显更好：纯 JSON（MCP 那条成功响应是 SSE 分帧，还要求 Accept 同时带两种
类型否则硬报 406），而且**有一份公开可读、不需要 Key 的 OpenAPI 规范**，
里面带完整的错误码契约。

规范地址（实测可直接 GET，无需鉴权）：
    https://api.52choujiang.cn/socialdatax/api/v1/docs/openapi.json

⚠️ 最要命的一条协议特性：**业务错误也返回 HTTP 200**，靠 body 里的 `code`
字段区分。规范原文：「请求成功；业务错误也会返回 200 + {code, message}。」
只看 HTTP 状态码的客户端，会把每一个「笔记已删除」当成成功。

已实地验证（2026-08，无 key / 假 key 探测）：
    无 Authorization → HTTP 401 {"code":1401,"message":"API Key 缺失，请通过 Authorization 或 X-API-Key 传入。"}
    Bearer 无效      → HTTP 401 {"code":1401,"message":"API Key 无效或已失效。"}
    响应头带 x-request-id —— 找厂商排查的唯一凭据，日志里必须记
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

BASE = "https://mcp.socialdatax.com/socialdatax/api/v1"

# 端点。请求体就是参数本身，不需要 JSON-RPC 信封。
ENDPOINTS = {
    ("xhs", "comments"): "/xhs/note/comment/list",
    ("xhs", "detail"): "/xhs/note/detail",
    ("douyin", "comments"): "/douyin/video/comment/list",
    ("douyin", "detail"): "/douyin/video/detail",
}


def endpoint(platform: str, purpose: str) -> str:
    path = ENDPOINTS.get((platform, purpose))
    if path is None:
        raise ValueError(f"没有 {platform}/{purpose} 的端点")
    return BASE + path


def headers(api_key: str) -> dict[str, str]:
    # 规范明确：Authorization 和 X-API-Key 只能用一种，同时传会被判为配置冲突。
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def build_body(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False)


class Failure(Enum):
    """错误分类。决定的是「这次失败该怎么办」，而不是「错在哪」。"""

    AUTH = "auth"              # key 缺失/失效 —— 整批停，重试无意义
    QUOTA = "quota"            # 积分不足 —— 整批停，保住已完成的结果
    RATE_LIMIT = "rate_limit"  # 限流 —— 退避后重试同一条
    GONE = "gone"              # 内容不存在/已删除/被限制 —— 行级结论，不是故障
    TRANSPORT = "transport"    # 超时、5xx、服务暂时不可用 —— 重试
    UNKNOWN = "unknown"        # 没见过的错 —— 行级失败，把原文写回表里给人看


# 整批必须立刻停下的错误。
FATAL = frozenset({Failure.AUTH, Failure.QUOTA})
# 值得退避重试的错误。
RETRYABLE = frozenset({Failure.RATE_LIMIT, Failure.TRANSPORT})

# 官方错误码 → 分类。全部来自 OpenAPI 规范的 x-socialdatax-error-contract。
# 第三个元素表示「这个结论是否权威」：权威的可以直接定罪，不用等第二次确认。
_CODES: dict[int, tuple[Failure, bool]] = {
    1400: (Failure.UNKNOWN, True),      # validation_error   请求体参数不正确（HTTP 400）
    1401: (Failure.AUTH, True),         # authentication_error（HTTP 401）
    1429: (Failure.RATE_LIMIT, True),   # rate_limited（HTTP 429）
    1001: (Failure.UNKNOWN, True),      # invalid_argument   ID/链接不符合接口要求
    1002: (Failure.UNKNOWN, True),      # invalid_pagination_state（我们从不翻页，不该出现）
    1003: (Failure.GONE, False),        # not_found          不存在，或公开访问不可见
    1004: (Failure.QUOTA, True),        # insufficient_balance
    1005: (Failure.TRANSPORT, True),    # service_failure    服务暂时不可用
    1006: (Failure.GONE, False),        # content_unavailable 权限/状态/平台限制导致不可读 ← 封控
    1007: (Failure.TRANSPORT, True),    # surface_unavailable 页面暂时不可访问
    1008: (Failure.GONE, True),         # content_deleted    规范原文「不要重试」→ 可直接定罪
}

# 兜底：错误码没命中时，从描述文案里找线索。有了官方错误码表之后，
# 这里只是防御——正常情况下不该走到。
_MESSAGE_HINTS: list[tuple[tuple[str, ...], Failure]] = [
    (("积分不足", "余额不足", "insufficient"), Failure.QUOTA),
    (("过于频繁", "频率", "rate limit"), Failure.RATE_LIMIT),
    (("不存在", "已删除", "已下架", "不可用", "违规", "not found", "deleted"), Failure.GONE),
    (("API Key", "鉴权", "unauthorized"), Failure.AUTH),
    (("暂时不可用", "稍后重试", "service"), Failure.TRANSPORT),
]


@dataclass
class Ok:
    data: dict[str, Any]
    points_cost: Optional[int] = None
    points_balance: Optional[int] = None
    request_id: str = ""


@dataclass
class Err:
    kind: Failure
    code: str
    message: str
    retry_after_seconds: Optional[float] = None
    http_status: Optional[int] = None
    request_id: str = ""
    # 上游明确给出结论（比如 1008 内容已删除），可以直接定罪不用等第二次确认。
    definitive: bool = False

    def __str__(self) -> str:
        text = f"[{self.kind.value}/{self.code}] {self.message}"
        return f"{text}（request_id={self.request_id}）" if self.request_id else text

    def operator_text(self) -> str:
        """写进表里给运营看的版本。

        必须带 request_id ——那是找厂商排查的唯一凭据，运营看不懂错误内容也没关系，
        直接截图发过去就行。省掉它，一个真实故障可能要多花几天才定位。
        """
        parts = [self.message]
        if self.code and self.code != "unknown":
            parts.append(f"错误码 {self.code}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return f"{parts[0]}（{'，'.join(parts[1:])}）" if len(parts) > 1 else parts[0]


def _classify(code: Any, message: str, http_status: Optional[int]) -> tuple[Failure, bool]:
    try:
        known = _CODES.get(int(code))
    except (TypeError, ValueError):
        known = None
    if known:
        return known

    haystack = f"{code} {message}".lower()
    for needles, kind in _MESSAGE_HINTS:
        if any(n.lower() in haystack for n in needles):
            return kind, False

    if http_status is not None:
        if http_status in (401, 403):
            return Failure.AUTH, True
        if http_status == 402:
            return Failure.QUOTA, True
        if http_status == 404:
            return Failure.GONE, False
        if http_status == 429:
            return Failure.RATE_LIMIT, True
        if http_status >= 500:
            return Failure.TRANSPORT, True

    return Failure.UNKNOWN, False


def _err(payload: dict[str, Any], http_status: Optional[int], request_id: str) -> Err:
    code = payload.get("code")
    message = str(payload.get("message") or json.dumps(payload, ensure_ascii=False)[:300])
    kind, definitive = _classify(code, message, http_status)
    # 字段名来自官方规范，不是猜的。429 还会同步返回 Retry-After 响应头。
    retry_after = payload.get("retry_after_seconds")
    return Err(
        kind=kind,
        code=str(code if code is not None else "unknown"),
        message=message,
        retry_after_seconds=float(retry_after) if isinstance(retry_after, (int, float)) else None,
        http_status=http_status,
        request_id=request_id,
        definitive=definitive,
    )


def _ok(data: dict[str, Any], request_id: str) -> Ok:
    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    return Ok(
        data=data,
        points_cost=points.get("cost"),
        points_balance=points.get("balance"),
        request_id=request_id,
    )


def iter_sse_payloads(body: str):
    """从 SSE 响应体里把每个 data: 帧的 JSON 抠出来。

    REST 接口不返回 SSE，这里留着是防御：万一哪天端点换了行为，或者有人把
    这套解析复用到 MCP 端点上（那条路的成功响应确实是 SSE 分帧）。
    按 SSE 规范，一帧可能有多行 data:，需要用换行拼接后再解析。
    """
    buffer: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
            continue
        if not line.strip() and buffer:
            chunk = "\n".join(buffer)
            buffer = []
            try:
                yield json.loads(chunk)
            except json.JSONDecodeError:
                continue
    if buffer:
        try:
            yield json.loads("\n".join(buffer))
        except json.JSONDecodeError:
            pass


Result = Ok | Err


def parse_response(
    http_status: int,
    content_type: str,
    body: str,
    request_id: str = "",
) -> Result:
    """把一次 HTTP 响应解析成 Ok 或 Err。

    关键：**HTTP 200 不等于成功**。业务错误也走 200，靠 body 里有没有 `code`
    字段来区分。成功响应不带 `code`（规范原文：「成功响应不会返回该字段」）。
    """
    body = body or ""

    # 防御分支：SSE（REST 不会走到，MCP 端点会）
    if "text/event-stream" in (content_type or "").lower():
        for frame in iter_sse_payloads(body):
            if isinstance(frame, dict):
                inner = frame.get("result")
                if isinstance(inner, dict):
                    structured = inner.get("structuredContent")
                    if isinstance(structured, dict):
                        return _ok(structured, request_id)
        return Err(Failure.UNKNOWN, "no_data_frame",
                   f"SSE 响应里没有可用的 data 帧（前 300 字符：{body[:300]}）",
                   http_status=http_status, request_id=request_id)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        kind, definitive = _classify("", body, http_status)
        return Err(kind, f"http_{http_status}", body[:500] or f"HTTP {http_status}",
                   http_status=http_status, request_id=request_id, definitive=definitive)

    if not isinstance(payload, dict):
        return Err(Failure.UNKNOWN, "unexpected_body", body[:500],
                   http_status=http_status, request_id=request_id)

    # 有 code 就是错误 —— 无论 HTTP 状态是不是 200。
    if "code" in payload:
        return _err(payload, http_status, request_id)

    if http_status >= 400:
        kind, definitive = _classify("", body, http_status)
        return Err(kind, f"http_{http_status}", body[:500],
                   http_status=http_status, request_id=request_id, definitive=definitive)

    return _ok(payload, request_id)
