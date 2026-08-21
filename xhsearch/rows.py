"""一行的模型：从表里读到什么、要发哪几个请求、最后写回什么。

调用计划是这里最值钱的部分，因为它直接等于钱：

* 小红书：1 次评论调用就拿到 评论数 + 置顶 + 前 N 条。只有需要点赞/收藏
  （爆文的另一个维度）时才追加 1 次 detail，且只对新笔记追加。
* 抖音：评论接口的 comment_count 是 integer|null，为了「评论数」这一列不
  间歇性变空，detail 必须恒定兜底 —— 抖音单篇成本是小红书的两倍。

计费单位是页不是条，页大小服务端定（接口没有 page_size 参数），所以
「只要前 5 条」和「要前 20 条」一个价，整页存下来即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .config import Settings
from .links import ParsedLink, parse


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
        target = {"note_id": link.content_id} if link.content_id else {"url": link.url}
        # sort=default 是唯一正确的选择：它对应 App 里默认看到的综合排序，
        # 也是置顶评论最可能出现在第一页的排序。换成按时间倒序
        # 会把老的置顶评论压到最后。
        # 参数名是抽象的——两家叫法不同（sort_type / sort_strategy），
        # 由 providers 层各自翻译。
        calls.append(ToolCall("xhs", "comments", {**target, "sort": "default"}))

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
    """预估这一批要花多少积分（按 SocialDataX 计价：10 积分/次，1 积分 = 0.01 元）。

    批量跑之前先报数给人看，比事后对账单强。
    走 TikHub 时积分这个单位不成立，用 estimate_yuan() 看钱。
    """
    return sum(len(plan_calls(row, settings, now)) for row in rows) * 10


def estimate_yuan(rows: list[Row], settings: Settings, now: Optional[datetime] = None) -> float:
    """预估这一批要花多少钱，按每个平台**实际会走的那家**的单价算。

    双通道之后两家单价差 10 倍（抖音），继续用「积分」这一个单位报数就是骗人。
    """
    from . import providers

    total = 0.0
    for row in rows:
        for call in plan_calls(row, settings, now):
            name = settings.channels.primary(call.platform)
            try:
                total += providers.get_provider(name).yuan_per_call(call.platform, call.purpose)
            except ValueError:
                total += providers.SOCIALDATAX_YUAN
    return total
