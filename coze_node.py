"""
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



# =============================================================================
#  来自 xhsearch/config.py
# =============================================================================

@dataclass
class FieldNames:
    """多维表格列名。左边是代码里的角色，右边是表头文字。"""

    # —— 人工维护 ——
    link: str = "链接"
    publish_time: str = "发布时间"
    expected_pinned: str = "种子评论关键词"   # 选填；填了才做置顶内容比对
    monitoring: str = "监控中"                # 复选框；取消勾选即停止刷新
    queued: str = "排队刷新"                  # 复选框；勾上=手动请求刷新，机器处理完自动清掉

    # —— 机器写入 · 运营主视图 ——
    platform: str = "平台"
    comment_count: str = "评论数"
    previous_comment_count: str = "上次评论数"
    like_count: str = "点赞数"
    collect_count: str = "收藏数"
    pinned_comment: str = "置顶评论"
    comment_status: str = "评论状态"          # 多选，人机共用：机器管置顶三值，人工值不碰
    comment_digest: str = "评论区快照"
    traffic_status: str = "流量状态"          # 多选，人机共用
    refresh_status: str = "刷新状态"
    failure_reason: str = "诊断信息"
    last_updated: str = "最后更新时间"

    # —— 机器写入 · 系统列（建议在运营视图里隐藏）——
    consecutive_failures: str = "连续失败次数"   # 两击定罪的计数器

    def must_read(self) -> list[str]:
        """search 时必须拉回来的列。少读一个就会写错。"""
        return [
            self.link,
            self.publish_time,
            self.expected_pinned,
            self.monitoring,
            self.queued,
            self.traffic_status,          # 合并多选必须先读现值
            self.comment_count,           # 判定掉量必须知道上一次的数
            self.last_updated,            # 分层刷新靠它判断到期
            self.consecutive_failures,    # 两击定罪
            self.comment_status,          # 判断置顶是不是刚掉的
        ]


@dataclass
class Tags:
    """机器管辖的标签。必须穷举——漏一个，那个标签就再也撤不回来。

    热度三档（评估中 / 爆贴 / 大爆）是**互斥**的：一条帖子同时挂着三个没有意义，
    所以每轮只留最高的那一个。而且只升不降（棘轮）——爆过就是爆过，
    评论被删导致数字掉下去不该让它从「大爆」退回「爆贴」，
    那种情况该由 风控 标签来表达。

    风控 / 已失效反映**当前状态**，每轮重算，恢复正常要能自动摘掉，
    否则表会越来越红，最后没人看。
    """

    evaluating: str = "评估中"
    hot: str = "爆贴"
    super_hot: str = "大爆"
    risk: str = "风控中"
    gone: str = "已失效"

    def heat_tiers(self) -> list[str]:
        """热度档位，由低到高。互斥，同时只留一个。"""
        return [self.evaluating, self.hot, self.super_hot]

    def namespace(self) -> list[str]:
        return [*self.heat_tiers(), self.risk, self.gone]

    def rank(self, tag: str) -> int:
        """热度档位的高低。不是热度标签返回 -1。"""
        tiers = self.heat_tiers()
        return tiers.index(tag) if tag in tiers else -1


@dataclass
class CommentStatus:
    """「评论状态」多选列里**机器管辖**的三个值。

    这一列是人机共用的：置顶结论由机器每轮重算并覆盖，而「评论是否显示」
    这类人工维护的值并列在同一列里，机器读得到但永远不碰。
    用的是和 流量状态 完全相同的合并算法。

    三个值互斥，每轮只写一个：

        置顶成功  —— 置顶的确认是我方种子评论
        置顶掉了  —— 之前置顶成功过，现在我方的置顶不在了
        没有置顶  —— 从来没成功过，现在也没有

    只有小红书能判（抖音评论接口没有 is_pinned 字段），抖音行完全不碰这一列。
    """

    pinned_ok: str = "置顶成功"
    pinned_lost: str = "置顶掉了"
    never_pinned: str = "没有置顶"

    def namespace(self) -> list[str]:
        return [self.pinned_ok, self.pinned_lost, self.never_pinned]

    def ever_pinned(self, current: list[str] | None) -> bool:
        """这一行历史上有没有成功置顶过。

        「置顶掉了」本身也算证据——掉了之后一直没恢复，下一轮不该退回
        「没有置顶」，那等于把曾经置顶过这件事抹掉。
        """
        return bool({self.pinned_ok, self.pinned_lost} & set(current or []))


@dataclass
class Thresholds:
    """判定口径。

    热度三档互斥，取最高：
        ≥ 20 → 评估中
        ≥ 50 → 爆贴
        ≥ 100 → 大爆
    """

    tier_evaluating: int = 20
    tier_hot: int = 50
    tier_super_hot: int = 100

    # 评论数相对上次下跌超过这个比例，判定疑似风控（限流/删评/折叠）。
    risk_drop_ratio: float = 0.5
    # 上次评论数低于这个值时不做掉量判定——从 3 掉到 1 没有意义。
    risk_drop_min_baseline: int = 20

    # 发布多久之后仍然零评论，判定疑似限流。太短会把正常冷启动误报成风控。
    risk_zero_comment_hours: int = 48

    def heat_tier(self, count: int, tags: "Tags") -> str | None:
        """按评论数算热度档位。返回 None 表示还够不上最低档。"""
        if count >= self.tier_super_hot:
            return tags.super_hot
        if count >= self.tier_hot:
            return tags.hot
        if count >= self.tier_evaluating:
            return tags.evaluating
        return None


@dataclass
class DigestFormat:
    """评论区快照的排版。计费按页不按条，所以能存多少存多少。"""

    max_comments: int = 8
    per_comment_chars: int = 40
    total_chars: int = 700
    show_like_count: bool = True
    show_ip_location: bool = True


@dataclass
class RefreshTiers:
    """分层刷新：越新的帖子刷得越勤。

    这不是一套调度代码，就是筛选时的一个条件：
    「发布时间落在这一层 且 最后更新时间早于 now - interval_hours」= 该刷了。
    """

    tiers: list[tuple[int, int]] = field(
        default_factory=lambda: [
            (2, 8),      # 发布 0-2 天：每 8 小时一次（风控和置顶的关键窗口）
            (7, 24),     # 3-7 天：每天一次
            (30, 72),    # 8-30 天：每 3 天一次
        ]
    )
    archive_after_days: int = 30     # 超过就不再自动刷，只保留手动触发

    def interval_hours_for_age(self, age_days: float) -> int | None:
        """返回该年龄的帖子应有的刷新间隔；None 表示已归档。"""
        for max_age_days, interval_hours in self.tiers:
            if age_days <= max_age_days:
                return interval_hours
        return None


@dataclass
class Safety:
    """防误伤参数。这一组的每个默认值都是为了「宁可少报，不可错报」。"""

    # 连续多少次取不到才敢判「已失效」。一次网络抖动就把好帖子标成风控，
    # 运营会全线停投，信任一旦丢了就补不回来。
    strikes_before_gone: int = 2

    # 一批里判定为「失效」的比例超过这个数（且样本够大）就整批作废。
    # 几百条笔记不可能在同一小时里被集体删除 —— 那是上游故障或话术改版。
    breaker_gone_ratio: float = 0.2
    breaker_min_sample: int = 10

    # 同一行在这个时间窗内刚成功刷过就跳过，不花积分。
    # 有人连点 200 次按钮 = 1 次真实调用。
    cooldown_seconds: int = 90


@dataclass
class Settings:
    fields: FieldNames = field(default_factory=FieldNames)
    tags: Tags = field(default_factory=Tags)
    comment_status: CommentStatus = field(default_factory=CommentStatus)
    thresholds: Thresholds = field(default_factory=Thresholds)
    digest: DigestFormat = field(default_factory=DigestFormat)
    refresh: RefreshTiers = field(default_factory=RefreshTiers)
    safety: Safety = field(default_factory=Safety)

    # 小红书笔记发布多少天内额外调一次 detail 拿点赞/收藏。
    # 设为 0 表示完全不调 detail（省一半钱，代价是没有爆文的点赞维度）。
    detail_within_days: int = 7

    # SocialDataX 官方 skill 明文要求最多 3 并发，不要突发请求。留一档余量。
    max_concurrency: int = 2

    # 单次运行的软截止。扣子代码节点硬上限 60 秒，留足写回时间。
    # 独立服务跑批量时设成 0（不限）。
    soft_deadline_seconds: float = 45.0


# =============================================================================
#  来自 xhsearch/links.py
# =============================================================================

Platform = Literal["xhs", "douyin"]

# 小红书笔记 ID 固定 24 位小写十六进制。
_XHS_NOTE_ID = r"[0-9a-f]{24}"

_XHS_DOMAINS = re.compile(
    r"(?:xiaohongshu\.com|xhslink\.com|xhslink\.cn|xhsurl\.com|xhsurl\.cn)",
    re.I,
)
_DOUYIN_DOMAINS = re.compile(
    r"(?:douyin\.com|iesdouyin\.com)",
    re.I,
)

# 按优先级排列：越靠前的形式越明确。
_XHS_ID_PATTERNS = [
    re.compile(rf"/explore/({_XHS_NOTE_ID})", re.I),
    re.compile(rf"/discovery/item/({_XHS_NOTE_ID})", re.I),
    re.compile(rf"/search_result/({_XHS_NOTE_ID})", re.I),
    # 主页内笔记链接 /user/profile/<uid>/<note_id>
    re.compile(rf"/user/profile/[0-9a-f]{{24}}/({_XHS_NOTE_ID})", re.I),
]

_DOUYIN_ID_PATTERNS = [
    re.compile(r"douyin\.com/video/(\d{6,})", re.I),
    re.compile(r"douyin\.com/note/(\d{6,})", re.I),
    re.compile(r"[?&]modal_id=(\d{6,})", re.I),
    re.compile(r"/share/video/(\d{6,})", re.I),
]

# 从一段分享文案里把 URL 抠出来。中文分享文案常把链接和标点黏在一起，
# 所以右边界要排掉中文标点和常见收尾符号。
_URL_RE = re.compile(r"https?://[^\s，。、！？；：）】》\"'<>]+", re.I)

# 裸 ID：整格就是一个 ID 的情况（运营有时只贴 ID）。
_BARE_XHS_ID = re.compile(rf"^\s*({_XHS_NOTE_ID})\s*$", re.I)
# 抖音 aweme_id 是纯数字，歧义太大（订单号、手机号、其它平台 ID 都长这样），
# 所以只在文本里出现「抖音」二字时才认，且要求是独立的一串数字。
_STANDALONE_DOUYIN_ID = re.compile(r"(?<!\d)(\d{15,25})(?!\d)")


@dataclass(frozen=True)
class ParsedLink:
    """一格链接的解析结果。

    platform 为 None 表示无法判定平台，调用方应把该行标记为「链接无法识别」
    而不是硬猜——猜错会去调错平台的接口，白花积分还写回错数据。
    """

    platform: Optional[Platform]
    content_id: Optional[str]
    url: Optional[str]
    raw: str

    @property
    def usable(self) -> bool:
        return self.platform is not None and (self.content_id or self.url) is not None

    def describe_failure(self) -> str:
        if self.platform is None:
            return "无法判定平台：既没匹配到小红书域名/ID，也没匹配到抖音域名/ID"
        return "已识别平台但没有可用的 ID 或链接"


def _first_url(text: str, domain_re: re.Pattern[str]) -> Optional[str]:
    for candidate in _URL_RE.findall(text):
        if domain_re.search(candidate):
            return candidate.rstrip(".,;")
    return None


def _match_id(text: str, patterns: list[re.Pattern[str]]) -> Optional[str]:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return found.group(1)
    return None


def parse(cell: str) -> ParsedLink:
    """把一格原始文本解析成平台 + ID/链接。

    单格里出现多个链接时取第一个能识别的；这是刻意的——一行代表一篇笔记，
    一格塞多个链接本身就是数据录入问题，应该在表里暴露出来而不是在这里猜。
    """
    raw = (cell or "").strip()
    if not raw:
        return ParsedLink(None, None, None, raw)

    # 全角字符会让域名匹配失败，先归一化一遍常见的几个。
    text = raw.replace("：", ":").replace("／", "/").replace("？", "?")

    bare_xhs = _BARE_XHS_ID.match(text)
    if bare_xhs:
        return ParsedLink("xhs", bare_xhs.group(1).lower(), None, raw)

    has_xhs_domain = bool(_XHS_DOMAINS.search(text))
    has_douyin_domain = bool(_DOUYIN_DOMAINS.search(text))

    # 同时命中两个平台的域名 —— 拒绝猜测。
    if has_xhs_domain and has_douyin_domain:
        return ParsedLink(None, None, None, raw)

    if has_xhs_domain:
        note_id = _match_id(text, _XHS_ID_PATTERNS)
        return ParsedLink(
            "xhs",
            note_id.lower() if note_id else None,
            None if note_id else (_first_url(text, _XHS_DOMAINS) or raw),
            raw,
        )

    if has_douyin_domain:
        aweme_id = _match_id(text, _DOUYIN_ID_PATTERNS)
        return ParsedLink(
            "douyin",
            aweme_id,
            None if aweme_id else (_first_url(text, _DOUYIN_DOMAINS) or raw),
            raw,
        )

    # 没有域名。裸抖音 ID 只在文本里明确提到「抖音」时才认。
    if "抖音" in raw:
        bare_douyin = _STANDALONE_DOUYIN_ID.search(text)
        if bare_douyin:
            return ParsedLink("douyin", bare_douyin.group(1), None, raw)

    return ParsedLink(None, None, None, raw)


# =============================================================================
#  来自 xhsearch/protocol.py
# =============================================================================

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


# =============================================================================
#  来自 xhsearch/tags.py
# =============================================================================

@dataclass(frozen=True)
class TagMerge:
    """一次合并的结果，保留过程信息以便写回「失败原因」列时能解释清楚。"""

    final: list[str]
    added: list[str]
    removed: list[str]
    dropped_unknown: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def merge(
    current: Sequence[str] | None,
    computed: Iterable[str],
    machine_namespace: Iterable[str],
    known_options: Iterable[str] | None = None,
) -> TagMerge:
    """把机器算出的标签并进现有标签，不碰人工标签。

    参数
    ----
    current:
        表里该行 流量状态 的现值。多选为空时飞书可能整个不返回这个字段，
        所以 None 和 [] 要一视同仁。
    computed:
        本次判定得出的机器标签，必须是 machine_namespace 的子集。
    machine_namespace:
        机器管辖的全部标签。不在这个集合里的一律视为人工标签，原样保留。
    known_options:
        该多选字段实际配置了哪些选项。给了就做过滤——飞书 batch_update 是
        全成功或全失败，一个字段里没有的选项名可能让整批几百行一起回滚，
        与其赌服务端会自动建选项，不如在这里挡掉并把它记进 dropped_unknown。
    """
    current_set = [t.strip() for t in (current or []) if t and t.strip()]
    machine = set(machine_namespace)
    computed_set = {t for t in computed if t}

    unexpected = computed_set - machine
    if unexpected:
        raise ValueError(
            f"算出的标签不在机器命名空间内：{sorted(unexpected)}。"
            "要么把它加进 machine_namespace，要么它就是个笔误——"
            "放行会导致这个标签之后永远无法撤回。"
        )

    dropped: list[str] = []
    if known_options is not None:
        options = set(known_options)
        allowed = {t for t in computed_set if t in options}
        dropped = sorted(computed_set - allowed)
        computed_set = allowed

    # 保序：先按原顺序留下人工标签，再追加机器标签，表里看起来才稳定。
    human = [t for t in current_set if t not in machine]
    seen: set[str] = set()
    final: list[str] = []
    for tag in human + sorted(computed_set):
        if tag not in seen:
            seen.add(tag)
            final.append(tag)

    previous_machine = {t for t in current_set if t in machine}
    return TagMerge(
        final=final,
        added=sorted(computed_set - previous_machine),
        removed=sorted(previous_machine - computed_set),
        dropped_unknown=dropped,
    )


# =============================================================================
#  来自 xhsearch/analyze.py
# =============================================================================

DOUYIN_PINNED_UNSUPPORTED = "—（抖音不支持置顶监控）"


@dataclass
class CommentView:
    content: str
    like_count: int = 0
    is_pinned: bool = False
    is_author: bool = False
    ip_location: str = ""
    author_name: str = ""

    def one_line(self, fmt: DigestFormat) -> str:
        text = re.sub(r"\s+", " ", self.content or "").strip()
        if len(text) > fmt.per_comment_chars:
            text = text[: fmt.per_comment_chars - 1] + "…"

        marks = []
        if self.is_pinned:
            marks.append("置顶")
        if self.is_author:
            marks.append("作者")
        if fmt.show_like_count and self.like_count:
            marks.append(f"{self.like_count}赞")
        if fmt.show_ip_location and self.ip_location:
            marks.append(self.ip_location)

        prefix = f"[{' · '.join(marks)}] " if marks else ""
        who = f"{self.author_name}: " if self.author_name else ""
        return f"{prefix}{who}{text}"


@dataclass
class Snapshot:
    """一篇笔记这一次刷新拿到的全部事实。"""

    platform: str
    comment_count: Optional[int] = None
    top_level_comment_count: Optional[int] = None
    comments: list[CommentView] = field(default_factory=list)
    like_count: Optional[int] = None
    collect_count: Optional[int] = None
    share_count: Optional[int] = None
    points_balance: Optional[int] = None

    @property
    def pinned(self) -> Optional[CommentView]:
        return next((c for c in self.comments if c.is_pinned), None)

    @property
    def supports_pinned(self) -> bool:
        return self.platform == "xhs"


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) else None


def _author_name(author: Any) -> str:
    if not isinstance(author, dict):
        return ""
    for key in ("name", "nickname", "nick_name"):
        value = author.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def read_comment_page(platform: str, data: dict[str, Any]) -> Snapshot:
    """解析评论接口的一页返回。

    小红书这一次调用就同时给到评论总数、置顶评论和综合排序的前一页评论——
    R1/R2/R3 三个需求一次拿全，不需要再调 detail。
    抖音的 comment_count 类型是 integer|null，拿不到时留 None，由上层决定兜底。
    """
    items = data.get("items")
    comments: list[CommentView] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            comments.append(
                CommentView(
                    content=str(item.get("content") or ""),
                    like_count=item.get("like_count") or 0,
                    # 抖音没有这个字段，get 返回 None → False，语义正确：
                    # 「未标记为置顶」而不是「已知未置顶」，两者的区别由
                    # Snapshot.supports_pinned 承担。
                    is_pinned=bool(item.get("is_pinned")),
                    is_author=bool(item.get("is_author_comment")),
                    ip_location=str(item.get("ip_location") or ""),
                    author_name=_author_name(item.get("author")),
                )
            )

    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    return Snapshot(
        platform=platform,
        comment_count=_int_or_none(data.get("comment_count")),
        top_level_comment_count=_int_or_none(data.get("top_level_comment_count")),
        comments=comments,
        points_balance=points.get("balance"),
    )


def merge_detail(snapshot: Snapshot, data: dict[str, Any]) -> Snapshot:
    """把 detail 接口的互动数并进快照。

    detail 的 comment_count 是 required integer，所以它同时充当抖音
    comment_count 为 null 时的兜底。
    """
    snapshot.like_count = _int_or_none(data.get("like_count"))
    snapshot.collect_count = _int_or_none(data.get("collect_count"))
    snapshot.share_count = _int_or_none(data.get("share_count"))
    if snapshot.comment_count is None:
        snapshot.comment_count = _int_or_none(data.get("comment_count"))
    points = data.get("points") if isinstance(data.get("points"), dict) else {}
    if points.get("balance") is not None:
        snapshot.points_balance = points["balance"]
    return snapshot


def format_pinned(snapshot: Snapshot) -> str:
    if not snapshot.supports_pinned:
        return DOUYIN_PINNED_UNSUPPORTED
    pinned = snapshot.pinned
    if pinned is None:
        return ""
    who = f"{pinned.author_name}: " if pinned.author_name else ""
    body = re.sub(r"[ \t]+", " ", pinned.content or "").strip()
    return f"{who}{body}"


def format_digest(snapshot: Snapshot, fmt: DigestFormat) -> str:
    """把前 N 条评论排成一格能读的文本。

    置顶评论排在最前面——它在综合排序里通常就在第一位，但接口没有保证，
    这里显式提到最前，免得运营在第 7 行才看到置顶。
    """
    if not snapshot.comments:
        return "（暂无评论）"

    ordered = sorted(snapshot.comments, key=lambda c: not c.is_pinned)
    lines: list[str] = []
    used = 0
    for index, comment in enumerate(ordered[: fmt.max_comments], start=1):
        line = f"{index}. {comment.one_line(fmt)}"
        if used + len(line) + 1 > fmt.total_chars:
            lines.append(f"…（还有 {len(ordered) - index + 1} 条未显示）")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


def _normalize(text: str) -> str:
    """比对置顶文案用的归一化：去空白、去标点、统一大小写。

    运营在小红书 App 里发的置顶评论，跟表里登记的「期望置顶文案」几乎不可能
    逐字一致（emoji、换行、被平台吞掉的符号），所以只做宽松包含匹配。
    """
    return re.sub(r"[\s\W_]+", "", (text or "")).lower()


def _looks_like_seed(comment: CommentView, expected: str) -> bool:
    """这条评论是不是我们种下去的那条。

    运营在 App 里发的置顶评论，跟表里登记的关键词几乎不可能逐字一致
    （emoji、换行、被平台吞掉的符号、事后追加编辑），所以做宽松匹配。
    """
    needle = _normalize(expected)
    if len(needle) < 4:
        # 太短的关键词误命中概率太高，宁可判不出也不要认错人。
        return False
    haystack = _normalize(comment.content)
    if not haystack:
        return False
    return needle in haystack or (len(haystack) >= 8 and haystack in needle)


class Pin(Enum):
    """置顶判定结果。"""

    UNSUPPORTED = "unsupported"   # 抖音：接口没有 is_pinned，判不了
    NO_SEED = "no_seed"           # 没填种子关键词，无从比对
    SUCCESS = "success"           # 置顶的就是我们那条
    REPLACED = "replaced"         # 有置顶，但被换成了别人的
    LOST = "lost"                 # 置顶没了，但我方评论还在首页
    SEED_MISSING = "seed_missing"  # 首页找不到我方评论
    NONE_PINNED = "none_pinned"   # 压根没有置顶评论


def decide_pin(snapshot: Snapshot, expected: str) -> tuple[Pin, str]:
    """判定置顶。返回（结果, 写进诊断信息的补充说明）。

    这里刻意不只回答「置顶成功了吗」。对品牌方来说最该立刻知道的那种情况是
    **置顶还在，但被换成了别人的评论**——只看「我们的置顶在不在」会完全错过这一幕。
    """
    if not snapshot.supports_pinned:
        return Pin.UNSUPPORTED, ""

    pinned = snapshot.pinned
    seeded = (expected or "").strip()

    if not seeded:
        if pinned is None:
            return Pin.NONE_PINNED, ""
        return Pin.NO_SEED, "未填写种子评论关键词，只能确认存在置顶评论，无法确认是不是我方的"

    position = next(
        (i for i, c in enumerate(snapshot.comments, start=1) if _looks_like_seed(c, seeded)),
        None,
    )

    if pinned is not None:
        if _looks_like_seed(pinned, seeded):
            return Pin.SUCCESS, ""
        if position:
            return Pin.REPLACED, f"⚠ 置顶位被他人占据，我方评论掉到第 {position} 条"
        return Pin.REPLACED, "⚠ 置顶位被他人占据，且首页未找到我方评论"

    if position:
        return Pin.LOST, f"⚠ 置顶已掉，我方评论现在排在第 {position} 条"
    return Pin.SEED_MISSING, "⚠ 首页未找到我方种子评论（可能已被删除，或不在第一页）"


def comment_status_values(
    pin: Pin,
    current: Optional[list[str]],
    settings: Settings,
) -> Optional[set[str]]:
    """算出「评论状态」这一列里机器该写的值。

    返回 None 表示**这一轮不该碰这一列**，和「写一个空集合」完全不是一回事：
    空集合会把机器上一轮写的置顶结论摘掉，None 是原样保留。

    两种必须返回 None 的情况：
      * 抖音 —— 接口没有 is_pinned，判不了
      * 有置顶但没填种子关键词 —— 分不清是我方的还是别人的，
        写「置顶成功」是撒谎，写「没有置顶」也是撒谎
    """
    cs = settings.comment_status
    if pin in (Pin.UNSUPPORTED, Pin.NO_SEED):
        return None
    if pin is Pin.SUCCESS:
        return {cs.pinned_ok}
    # 剩下的都是「我方置顶现在不在」：置顶被别人顶了、掉了、种子评论找不到、
    # 压根没有置顶。区分只看历史——成功过就是掉了，没成功过就是从来没有。
    return {cs.pinned_lost} if cs.ever_pinned(current) else {cs.never_pinned}


@dataclass
class Verdict:
    tags: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    pin: Pin = Pin.UNSUPPORTED


def decide(
    snapshot: Snapshot,
    settings: Settings,
    *,
    previous_comment_count: Optional[int],
    age_hours: Optional[float],
    expected_pinned: str = "",
    current_tags: Optional[list[str]] = None,
    current_comment_status: Optional[list[str]] = None,
) -> Verdict:
    """算出这一行本次应有的机器标签和置顶判定。

    只产出 settings.tags.namespace() 里的标签。人工标签由 tags.merge 保护，
    这里完全不需要知道它们的存在。
    """
    t = settings.tags
    th: Thresholds = settings.thresholds
    verdict = Verdict()

    count = snapshot.comment_count

    # —— 热度档位：互斥，取最高，且只升不降 ——
    if count is not None:
        tier = th.heat_tier(count, t)
        # 棘轮：算上表里已有的档位取最高。评论被删导致数字掉下去，不该让
        # 一条帖子从「大爆」退回「爆贴」——那是风控信号，由风控标签表达。
        previous_best = max(
            (tag for tag in (current_tags or []) if t.rank(tag) >= 0),
            key=t.rank,
            default=None,
        )
        best = max(
            (x for x in (tier, previous_best) if x),
            key=t.rank,
            default=None,
        )
        if best:
            verdict.tags.add(best)
            if best == tier:
                verdict.notes.append(f"评论数 {count} → {best}")
            else:
                verdict.notes.append(f"评论数 {count}，但曾达到「{best}」，保留高档位")

    # —— 掉量：评论被平台悄悄批量删除，往往比笔记整个失效早得多 ——
    if (
        count is not None
        and previous_comment_count is not None
        and previous_comment_count >= th.risk_drop_min_baseline
        and count <= previous_comment_count * (1 - th.risk_drop_ratio)
    ):
        verdict.tags.add(t.risk)
        verdict.notes.append(
            f"⚠ 评论数从 {previous_comment_count} 掉到 {count}，"
            f"跌幅超过 {int(th.risk_drop_ratio * 100)}%，疑似限流或删评"
        )

    # 发出去够久了还是零评论，疑似限流。
    if count == 0 and age_hours is not None and age_hours >= th.risk_zero_comment_hours:
        verdict.tags.add(t.risk)
        verdict.notes.append(
            f"⚠ 发布 {age_hours:.0f} 小时仍为 0 评论（阈值 {th.risk_zero_comment_hours} 小时）"
        )

    # —— 置顶 ——
    verdict.pin, note = decide_pin(snapshot, expected_pinned)
    if note:
        verdict.notes.append(note)
    # 之前置顶成功过、现在掉了 —— 这是种草投放里最该被立刻发现的事之一。
    if (
        settings.comment_status.ever_pinned(current_comment_status)
        and verdict.pin is not Pin.SUCCESS
        and verdict.pin is not Pin.UNSUPPORTED
    ):
        verdict.notes.append("⚠ 此前已确认置顶成功，本轮我方置顶已不在")

    return verdict


def gone_verdict(settings: Settings, reason: str = "") -> Verdict:
    """帖子确认取不到时的结论（已经过两击确认，不是第一次失败就走这里）。

    同时打 已失效 和 风控 ——「已失效」说明事实，「风控」是运营真正会去筛的那一列。
    """
    verdict = Verdict()
    verdict.tags.add(settings.tags.gone)
    verdict.tags.add(settings.tags.risk)
    verdict.notes.append(reason or "接口返回内容不存在/已删除/无法访问")
    return verdict


def suspect_verdict(settings: Settings, strikes: int, reason: str = "") -> Verdict:
    """第一次取不到时的结论：只记，不定罪。

    刻意**不打任何标签**——一次网络抖动或上游抽风就把好帖子标成风控，
    运营会全线停投，而这种信任一旦丢了补不回来。
    """
    verdict = Verdict()
    verdict.notes.append(
        (reason or "本轮未取到内容") + f"（第 {strikes} 次，达到 2 次才判定失效，稍后自动复检）"
    )
    return verdict


# =============================================================================
#  来自 xhsearch/rows.py
# =============================================================================

@dataclass
class ToolCall:
    platform: str            # "xhs" | "douyin"
    purpose: str             # "comments" | "detail"
    arguments: dict[str, Any]


@dataclass
class Row:
    record_id: str
    link_cell: str
    publish_time_ms: Optional[int] = None
    expected_pinned: str = ""
    current_tags: list[str] = field(default_factory=list)
    previous_comment_count: Optional[int] = None
    last_updated_ms: Optional[int] = None
    consecutive_failures: int = 0
    comment_status: list[str] = field(default_factory=list)
    queued: bool = False

    _parsed: Optional[ParsedLink] = field(default=None, repr=False, compare=False)

    @property
    def parsed(self) -> ParsedLink:
        if self._parsed is None:
            self._parsed = parse(self.link_cell)
        return self._parsed

    def age_hours(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.publish_time_ms is None:
            return None
        now = now or datetime.now(timezone.utc)
        published = datetime.fromtimestamp(self.publish_time_ms / 1000, tz=timezone.utc)
        return (now - published).total_seconds() / 3600

    def age_days(self, now: Optional[datetime] = None) -> Optional[float]:
        hours = self.age_hours(now)
        return None if hours is None else hours / 24

    def in_cooldown(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """刚刷过就别再刷。

        这是对「有人连点 200 次按钮」的完整回答：连点 200 次 = 1 次真实调用。
        """
        window = settings.safety.cooldown_seconds
        if not window or self.last_updated_ms is None:
            return False
        now = now or datetime.now(timezone.utc)
        updated = datetime.fromtimestamp(self.last_updated_ms / 1000, tz=timezone.utc)
        return (now - updated).total_seconds() < window

    def is_due(self, settings: Settings, now: Optional[datetime] = None) -> bool:
        """按分层策略判断这一行现在该不该刷。

        发布时间未知时按「该刷」处理——宁可多花一毛钱，也别让一行永远不更新
        而没人发现。
        """
        age = self.age_days(now)
        if age is None:
            return True
        interval = settings.refresh.interval_hours_for_age(age)
        if interval is None:
            return False  # 已归档
        if self.last_updated_ms is None:
            return True
        now = now or datetime.now(timezone.utc)
        updated = datetime.fromtimestamp(self.last_updated_ms / 1000, tz=timezone.utc)
        return (now - updated).total_seconds() / 3600 >= interval


def plan_calls(row: Row, settings: Settings, now: Optional[datetime] = None) -> list[ToolCall]:
    """算出这一行需要发哪些请求。空列表 = 链接不可用，不该花钱。

    优先用 ID 而不是 URL：小红书分享链接带 `xsec_token`，会过期；ID 不会。
    """
    link = row.parsed
    if not link.usable:
        return []

    calls: list[ToolCall] = []
    age_days = row.age_days(now)

    if link.platform == "xhs":
        target = {"note_id": link.content_id} if link.content_id else {"note_url": link.url}
        # sort_type=default 是唯一正确的选择：它对应 App 里默认看到的综合排序，
        # 也是置顶评论最可能出现在第一页的排序。换成 time_descending
        # 会把老的置顶评论压到最后。
        calls.append(ToolCall("xhs", "comments", {**target, "sort_type": "default"}))

        want_detail = settings.detail_within_days > 0 and (
            age_days is None or age_days <= settings.detail_within_days
        )
        if want_detail:
            calls.append(ToolCall("xhs", "detail", dict(target)))

    elif link.platform == "douyin":
        target = {"aweme_id": link.content_id} if link.content_id else {"url": link.url}
        calls.append(ToolCall("douyin", "comments", dict(target)))
        # 恒定追加：抖音评论接口的 comment_count 类型是 integer|null，
        # 不兜底的话「评论数」这列会间歇性变空，比没有更糟。
        calls.append(ToolCall("douyin", "detail", dict(target)))

    return calls


def estimate_credits(rows: list[Row], settings: Settings, now: Optional[datetime] = None) -> int:
    """预估这一批要花多少积分。10 积分/次，1 积分 = 0.01 元。

    批量跑之前先报数给人看，比事后对账单强。
    """
    return sum(len(plan_calls(row, settings, now)) for row in rows) * 10


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
    """打一次 SocialDataX REST 接口。

    ⚠️ 业务错误也返回 HTTP 200，靠 body 里的 `code` 字段区分。
    只看 status_code 的写法会把每一个「笔记已删除」当成成功。
    """
    async with semaphore:
        if time.monotonic() >= deadline:
            return Err(Failure.TRANSPORT, "deadline", "已到软截止，留给下一轮")
        try:
            response = await requests.post(
                endpoint(call.platform, call.purpose),
                headers=headers(api_key),
                data=build_body(call.arguments).encode("utf-8"),
                timeout=25.0,
            )
        except Exception as exc:                       # noqa: BLE001
            return Err(Failure.TRANSPORT, "network", f"{type(exc).__name__}: {exc}")

        content_type, request_id = "", ""
        try:
            content_type = response.headers.get("content-type", "") or ""
            request_id = response.headers.get("x-request-id", "") or ""
        except Exception:                              # noqa: BLE001
            pass
        return parse_response(getattr(response, "status_code", 0), content_type,
                              response.text, request_id)


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
    tripped = len(updates) >= settings.safety.breaker_min_sample and \
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
        # 错误码 1008「内容已删除」是权威结论，规范明写不要重试 —— 直接定罪。
        convicted = error.definitive or strikes >= settings.safety.strikes_before_gone
        verdict = gone_verdict(settings, error.operator_text()) if convicted \
            else suspect_verdict(settings, strikes, error.operator_text())
        # 第一击不碰流量状态：这一轮没获得关于这篇笔记的任何新信息，
        # 摘掉上一轮的标签等于用一次失败抹掉真实结论。
        fields = _render(row, verdict, None, settings, now,
                         "已失效" if convicted else "疑似受限", touch_tags=convicted)
        fields[f.consecutive_failures] = strikes
        return (fields, credits, balance, convicted)

    if snapshot is None:
        reason = error.operator_text() if error else "没有拿到任何数据"
        fields = _base_fields(settings, "刷新失败", [reason], now)
        fields[f.consecutive_failures] = (row.consecutive_failures or 0) + 1
        return (fields, credits, balance, False)

    verdict = decide(snapshot, settings,
                     previous_comment_count=row.previous_comment_count,
                     age_hours=row.age_hours(now),
                     expected_pinned=row.expected_pinned,
                     current_tags=row.current_tags,
                     current_comment_status=row.comment_status)
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

