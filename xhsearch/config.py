"""列名映射与判定口径。

这个文件是整套东西唯一需要按你们实际表结构改的地方。列名必须与多维表格
表头**逐字相同**（飞书按名字寻址，差一个空格就报 1254045 FieldNameNotFound）。

判定阈值（爆文/风控）没有通用答案，默认值是占位的，上线前必须按你们自己的
历史数据校准一次。口径没定死就上生产，是这类监控表最大的返工来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
class Channels:
    """双通道：每个平台走哪几家数据供应商，按顺序降级。

    默认两个平台都优先 TikHub，SocialDataX 作备胎。理由（都是实测的，
    完整对比见 docs/供应商对比.md）：

    * 抖音便宜 93%（¥0.10 → ¥0.0072 一次），小红书便宜 28%
    * 两家需要的字段都拿得到——TikHub 的置顶标记藏在 `show_tags_v2` 里，
      名字不叫 is_pinned，但确实有
    * SocialDataX 的错误码是有契约的（1006 封控 / 1008 已删除，
      厂商自己标了「不要重试」），TikHub 没有这张表。所以它更适合当备胎：
      平时不花钱，主通道挂了或余额空了立刻顶上

    降级只在**主通道自己有问题**时发生（网络故障、Key 失效、余额耗尽），
    不会因为「这条笔记没了」而去第二家再花一次钱——那是行级结论，不是故障。

    只想用一家：把列表写成单元素即可，行为和改造前完全一致。
        settings.channels.order = {"xhs": ["socialdatax"], "douyin": ["socialdatax"]}
    """

    order: dict[str, list[str]] = field(default_factory=lambda: {
        "xhs": ["tikhub", "socialdatax"],
        "douyin": ["tikhub", "socialdatax"],
    })

    def for_platform(self, platform: str) -> list[str]:
        return list(self.order.get(platform) or ["socialdatax"])

    def all_names(self) -> list[str]:
        seen: list[str] = []
        for names in self.order.values():
            for name in names:
                if name not in seen:
                    seen.append(name)
        return seen

    def primary(self, platform: str) -> str:
        names = self.for_platform(platform)
        return names[0] if names else "socialdatax"


@dataclass
class Settings:
    fields: FieldNames = field(default_factory=FieldNames)
    tags: Tags = field(default_factory=Tags)
    comment_status: CommentStatus = field(default_factory=CommentStatus)
    thresholds: Thresholds = field(default_factory=Thresholds)
    digest: DigestFormat = field(default_factory=DigestFormat)
    refresh: RefreshTiers = field(default_factory=RefreshTiers)
    safety: Safety = field(default_factory=Safety)
    channels: Channels = field(default_factory=Channels)

    # 小红书笔记发布多少天内额外调一次 detail 拿点赞/收藏。
    # 设为 0 表示完全不调 detail（省一半钱，代价是没有爆文的点赞维度）。
    detail_within_days: int = 7

    # SocialDataX 官方 skill 明文要求最多 3 并发，不要突发请求。留一档余量。
    max_concurrency: int = 2

    # 单次运行的软截止。扣子代码节点硬上限 60 秒，留足写回时间。
    # 独立服务跑批量时设成 0（不限）。
    soft_deadline_seconds: float = 45.0
