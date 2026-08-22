"""识别多维表格单元格里的链接属于哪个平台，并尽量抽出稳定 ID。

设计取舍：SocialDataX 的 *_by_url 接口本身就吃短链和完整分享文案，所以这里
不做短链展开、不发任何网络请求。我们只需要回答两个问题：

1. 这一格是小红书还是抖音（决定调哪个平台的端点）
2. 能不能直接拿到 ID（能拿到就走 by_id 入口，少一次链接解析，也更稳）

拿不到 ID 时把原始文本整个透传给 by_url 入口即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

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
