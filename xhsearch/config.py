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
    pinned_state: str = "置顶状态"            # 单选，比一个布尔标签能说清楚得多
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
            self.pinned_state,            # 判断置顶是不是刚掉的
        ]


@dataclass
class Tags:
    """机器管辖的标签。必须穷举——漏一个，那个标签就再也撤不回来。

    分两类：

    * 粘性（sticky）：记录「曾经发生过的事实」，只增不删。爆过就是爆过，
      评论数回落不代表它没爆过，要摘由人工摘。
    * 易变（volatile）：反映「当前状态」，每轮重算。恢复正常要能自动摘掉，
      否则表会越来越红，最后没人看。
    """

    hot: str = "爆文"
    risk: str = "风控"
    warning: str = "预警"
    pinned_ok: str = "置顶成功"
    gone: str = "已失效"

    def namespace(self) -> list[str]:
        return [self.hot, self.risk, self.warning, self.pinned_ok, self.gone]

    def sticky(self) -> list[str]:
        return [self.hot]

    def volatile(self) -> list[str]:
        return [self.risk, self.warning, self.pinned_ok, self.gone]


@dataclass
class PinnedStates:
    """置顶状态单选字段的取值。固定枚举，**绝不含变量**。

    带变量的单选值（比如「置顶在第 3 条」）会让飞书静默新建选项，几周后这个
    字段会长出几十个只差一个数字的选项，且只能人工清理。位次信息一律写进
    诊断信息，不进单选值。
    """

    unknown: str = "未刷新"
    no_seed: str = "未配置种子"
    success: str = "置顶成功"
    replaced: str = "置顶被顶替"       # 有置顶，但不是我们那条 —— 品牌方最该立刻知道的一种
    lost: str = "置顶丢失"             # 我们那条还在，但没被置顶
    seed_missing: str = "未找到种子评论"
    none_pinned: str = "无置顶评论"
    douyin_unsupported: str = "抖音·不支持判定"

    def all(self) -> list[str]:
        return [
            self.unknown, self.no_seed, self.success, self.replaced,
            self.lost, self.seed_missing, self.none_pinned, self.douyin_unsupported,
        ]


@dataclass
class Thresholds:
    """判定口径。**默认值是占位符，必须按你们自己的数据校准。**"""

    # 评论数达到多少算爆文。小红书和抖音的基线差一个量级。
    hot_comment_count_xhs: int = 50
    hot_comment_count_douyin: int = 200
    # 单轮增量达到这个数且相对上次涨幅过半，也算爆（抓突然起飞的那一刻）
    hot_delta: int = 20

    # 评论数相对上次下跌超过这个比例，判定疑似风控（限流/删评/折叠）。
    risk_drop_ratio: float = 0.5
    # 上次评论数低于这个值时不做掉量判定——从 3 掉到 1 没有意义。
    risk_drop_min_baseline: int = 20
    # 轻微掉量（评论在被悄悄删）只打预警，不打风控。
    warn_drop_ratio: float = 0.1
    warn_drop_min_absolute: int = 5

    # 发布多久之后仍然零评论，判定疑似限流。太短会把正常冷启动误报成风控。
    risk_zero_comment_hours: int = 48

    def hot_threshold(self, platform: str) -> int:
        return self.hot_comment_count_douyin if platform == "douyin" else self.hot_comment_count_xhs


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
    pinned_states: PinnedStates = field(default_factory=PinnedStates)
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
