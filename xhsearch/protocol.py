"""SocialDataX MCP 端点的协议层：组包、拆帧、错误归类。

全部是纯函数，不发网络请求——传输层单独放在 transport.py，
这样同一份解析逻辑可以直接粘进扣子代码节点（那里只能用 requests_async）。

已实地验证的协议行为（2026-08，无 key 探测）：

* 端点形如 https://mcp.socialdatax.com/<platform>/mcp，走 JSON-RPC 2.0 over HTTP
* 请求头 Accept 必须**同时**包含 application/json 和 text/event-stream，
  否则硬报 HTTP 406 且返回 {"error":{"code":-32600,"message":"Not Acceptable: ..."}}
* 无 Authorization → HTTP 401，body 是**纯 JSON**：
  {"error":"missing_api_key","error_description":"...","action":"configure_api_key",...}
* Bearer 无效 → 同样结构，error 为 "invalid_api_key"
* 成功响应是 SSE 分帧：`event: message` + `data: {json-rpc}`

尚未验证（需要真 key，见 docs/verification.md）：成功帧内 result 的确切形状、
配额耗尽与笔记被删时的 error 取值。下面的解析对这两处都做了防御。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

ENDPOINT_TEMPLATE = "https://mcp.socialdatax.com/{platform}/mcp"

# 少任何一个都会被服务端以 406 拒绝。
REQUIRED_ACCEPT = "application/json, text/event-stream"


def endpoint(platform: str) -> str:
    return ENDPOINT_TEMPLATE.format(platform=platform)


def headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": REQUIRED_ACCEPT,
    }


def build_call(tool: str, arguments: dict[str, Any], request_id: int = 1) -> str:
    """组一个 tools/call 请求体。

    这个端点是无状态的：不需要先 initialize 握手，也不需要 Mcp-Session-Id。
    """
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        ensure_ascii=False,
    )


class Failure(Enum):
    """错误分类。决定的是「这次失败该怎么办」，而不是「错在哪」。"""

    AUTH = "auth"              # key 缺失/失效 —— 整批停，重试无意义
    QUOTA = "quota"            # 积分不足 —— 整批停，保住已完成的结果
    RATE_LIMIT = "rate_limit"  # 限流 —— 退避后重试同一条
    GONE = "gone"              # 笔记不存在/已删/封控 —— 行级结论，不是故障
    TRANSPORT = "transport"    # 超时、5xx、连接错 —— 重试
    UNKNOWN = "unknown"        # 没见过的错 —— 行级失败，把原文写回表里给人看


# 整批必须立刻停下的错误。
FATAL = frozenset({Failure.AUTH, Failure.QUOTA})
# 值得退避重试的错误。
RETRYABLE = frozenset({Failure.RATE_LIMIT, Failure.TRANSPORT})

# 服务端返回的 error 字符串 → 分类。
# 前两个已实测；其余是按 skill 文档里出现过的措辞预置的，真 key 跑过之后要回来核。
_ERROR_CODES = {
    "missing_api_key": Failure.AUTH,
    "invalid_api_key": Failure.AUTH,
    "insufficient_balance": Failure.QUOTA,
    "insufficient_credits": Failure.QUOTA,
    "rate_limited": Failure.RATE_LIMIT,
    "too_many_requests": Failure.RATE_LIMIT,
    "not_found": Failure.GONE,
    "content_not_found": Failure.GONE,
    "note_not_found": Failure.GONE,
    "video_not_found": Failure.GONE,
    "content_deleted": Failure.GONE,
}

# 兜底：错误码没命中时，从描述文案里找线索。
_MESSAGE_HINTS: list[tuple[tuple[str, ...], Failure]] = [
    (("积分不足", "余额不足", "insufficient"), Failure.QUOTA),
    (("过于频繁", "频率", "rate limit", "too frequent"), Failure.RATE_LIMIT),
    (("不存在", "已删除", "已下架", "无法访问", "违规", "not found", "deleted"), Failure.GONE),
    (("API Key", "鉴权", "unauthorized"), Failure.AUTH),
]


@dataclass
class Ok:
    data: dict[str, Any]
    points_cost: Optional[int] = None
    points_balance: Optional[int] = None


@dataclass
class Err:
    kind: Failure
    code: str
    message: str
    retry_after_seconds: Optional[float] = None
    http_status: Optional[int] = None

    def __str__(self) -> str:
        return f"[{self.kind.value}/{self.code}] {self.message}"


Result = Ok | Err


def _classify(code: str, message: str, http_status: Optional[int]) -> Failure:
    known = _ERROR_CODES.get(code.strip().lower())
    if known:
        return known

    haystack = f"{code} {message}".lower()
    for needles, kind in _MESSAGE_HINTS:
        if any(n.lower() in haystack for n in needles):
            return kind

    if http_status is not None:
        if http_status in (401, 403):
            return Failure.AUTH
        if http_status == 402:
            return Failure.QUOTA
        if http_status == 404:
            return Failure.GONE
        if http_status == 429:
            return Failure.RATE_LIMIT
        if http_status >= 500:
            return Failure.TRANSPORT

    return Failure.UNKNOWN


def iter_sse_payloads(body: str):
    """从 SSE 响应体里把每个 data: 帧的 JSON 抠出来。

    按 SSE 规范，一帧可能有多行 data:，需要用换行拼接后再解析。
    """
    buffer: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            buffer.append(line[5:].lstrip())
            continue
        # 空行 = 帧结束
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


def _unwrap_tool_result(result: dict[str, Any]) -> Result:
    """从 MCP tools/call 的 result 里取出业务数据。

    这些工具都声明了 outputSchema，按 MCP 规范服务端应当返回 structuredContent。
    但为了不把整套东西押在一个没实测过的假设上，这里也接受
    content[0].text 里再套一层 JSON 字符串的经典形式。
    """
    if result.get("isError"):
        text = _first_text(result) or "工具返回 isError 但没有文本说明"
        payload = _try_json(text)
        if isinstance(payload, dict) and ("error" in payload or "error_description" in payload):
            return _err_from_payload(payload, None)
        return Err(_classify("", text, None), "tool_error", text)

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return _ok(structured)

    text = _first_text(result)
    if text is not None:
        payload = _try_json(text)
        if isinstance(payload, dict):
            return _ok(payload)
        return Err(Failure.UNKNOWN, "unparsable_content", text[:500])

    return Err(Failure.UNKNOWN, "empty_result", json.dumps(result, ensure_ascii=False)[:500])


def _first_text(result: dict[str, Any]) -> Optional[str]:
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return item["text"]
    return None


def _try_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _ok(data: dict[str, Any]) -> Ok:
    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    return Ok(
        data=data,
        points_cost=points.get("cost"),
        points_balance=points.get("balance"),
    )


def _err_from_payload(payload: dict[str, Any], http_status: Optional[int]) -> Err:
    code = str(payload.get("error") or payload.get("action") or "")
    message = str(
        payload.get("error_description")
        or payload.get("message")
        or json.dumps(payload, ensure_ascii=False)[:300]
    )
    retry_after = payload.get("retry_after") or payload.get("wait_seconds")
    return Err(
        kind=_classify(code, message, http_status),
        code=code or "unknown",
        message=message,
        retry_after_seconds=float(retry_after) if isinstance(retry_after, (int, float)) else None,
        http_status=http_status,
    )


def parse_response(http_status: int, content_type: str, body: str) -> Result:
    """把一次 HTTP 响应解析成 Ok 或 Err。

    成功走 SSE，失败走纯 JSON —— 同一个接口两种 content-type，两边都要能解。
    """
    body = body or ""

    # 纯 JSON 分支：鉴权失败、406、以及疑似的配额/限流错误都走这里。
    if "text/event-stream" not in (content_type or "").lower():
        payload = _try_json(body)
        if isinstance(payload, dict):
            # JSON-RPC 错误信封
            if isinstance(payload.get("error"), dict):
                rpc = payload["error"]
                return Err(
                    kind=_classify(str(rpc.get("code", "")), str(rpc.get("message", "")), http_status),
                    code=str(rpc.get("code", "rpc_error")),
                    message=str(rpc.get("message", "")),
                    http_status=http_status,
                )
            # 平台自定义错误信封（已实测形状）
            if "error" in payload or "error_description" in payload:
                return _err_from_payload(payload, http_status)
            # 少见：直接给了 JSON-RPC 成功体
            if isinstance(payload.get("result"), dict):
                return _unwrap_tool_result(payload["result"])

        if http_status >= 400:
            return Err(
                kind=_classify("", body, http_status),
                code=f"http_{http_status}",
                message=body[:500] or f"HTTP {http_status}",
                http_status=http_status,
            )
        return Err(Failure.UNKNOWN, "unexpected_body", body[:500], http_status=http_status)

    # SSE 分支
    for frame in iter_sse_payloads(body):
        if not isinstance(frame, dict):
            continue
        if isinstance(frame.get("error"), dict):
            rpc = frame["error"]
            return Err(
                kind=_classify(str(rpc.get("code", "")), str(rpc.get("message", "")), http_status),
                code=str(rpc.get("code", "rpc_error")),
                message=str(rpc.get("message", "")),
                http_status=http_status,
            )
        if isinstance(frame.get("result"), dict):
            return _unwrap_tool_result(frame["result"])

    return Err(
        Failure.UNKNOWN,
        "no_data_frame",
        f"SSE 响应里没有可用的 data 帧（前 300 字符：{body[:300]}）",
        http_status=http_status,
    )
