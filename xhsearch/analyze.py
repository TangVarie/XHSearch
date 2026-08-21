"""把接口返回的评论页/详情，变成可以直接写进多维表格的值。

全部纯函数，不碰网络也不碰飞书——方便单测，也方便整段粘进扣子代码节点。

两个平台的能力差异必须在这一层显式处理，不能糊过去：

* 小红书评论条目有 is_pinned / is_author_comment，且支持 sort_type=default
* 抖音评论条目**没有 is_pinned**（只有 is_hot / is_folded），也**没有 sort_type**

所以「置顶评论」和「置顶成功」在抖音侧做不到。这里不静默返回空值——
空值会被运营读成「没置顶」，而真相是「这个接口根本看不到置顶」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .config import DigestFormat, Settings, Thresholds

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

    # 上游自己给的审核/封禁标记（小红书 in_censor、抖音 is_prohibited/in_reviewing）。
    # 只有 TikHub 通道拿得到，SocialDataX 那边没有这个字段，所以永远可能是 None。
    # ⚠️ 刻意**不参与打标签**：这两个字段的语义还没在真实被封的帖子上验过，
    # 只见过 false。凭没验过的字段打「风控中」，一旦误报就是运营全线停投——
    # 那正是这个项目最不能犯的错。现在只写进诊断信息给人看。
    # 验过之后再决定要不要提升成判定依据，见 docs/待验证清单.md。
    censored: Optional[bool] = None

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
    if isinstance(data.get("_censored"), bool):
        snapshot.censored = data["_censored"]
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
