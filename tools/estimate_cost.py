#!/usr/bin/env python3
"""按当前配置算月度成本，两家供应商分开报。

    python3 tools/estimate_cost.py [每天新发条数] [小红书占比]

计价（都已实地核实）：
    SocialDataX  全平台 10 积分/页，1 积分 = ¥0.01 → 一次调用 ¥0.10
    TikHub       小红书 $0.010/次（不参与走量折扣），抖音 $0.001/次
                 数字来自它自己公开免鉴权的计价接口 get_all_endpoints_info

调用次数（已从接口实测核实）：
    小红书 = 1 次评论调用（评论数 + 置顶 + 前 N 条一次拿全）
             + 新笔记额外 1 次 detail（点赞/收藏）
    抖音   = 1 次评论调用 + 恒定 1 次 detail
             （SocialDataX 的 comment_count 是 integer|null，必须兜底；
               TikHub 的 total 实测是实数，但 detail 还要拿点赞收藏，照调）
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from xhsearch import providers  # noqa: E402
from xhsearch.config import Settings  # noqa: E402

YUAN_PER_CALL = 0.10  # SocialDataX 单价，用作对照基准


def tier_population(settings: Settings, per_day: float) -> list[tuple[str, float, int, float]]:
    """返回 [(档位说明, 该档笔记数, 刷新间隔小时, 每天刷新次数)]"""
    out = []
    previous_max = 0
    for max_age_days, interval_hours in settings.refresh.tiers:
        span = max_age_days - previous_max
        population = per_day * span
        per_day_refreshes = 24 / interval_hours
        out.append((f"{previous_max}-{max_age_days} 天", population, interval_hours, per_day_refreshes))
        previous_max = max_age_days
    return out


def estimate(per_day: float, xhs_share: float, settings: Settings) -> dict:
    tiers = tier_population(settings, per_day)
    total_posts = sum(t[1] for t in tiers)

    comment_calls = 0.0
    detail_calls = 0.0
    rows = []
    for label, population, interval_hours, refreshes in tiers:
        age_mid = 0.0
        for max_age, _ in settings.refresh.tiers:
            if label.endswith(f"{max_age} 天"):
                age_mid = max_age
                break

        xhs_posts = population * xhs_share
        dy_posts = population * (1 - xhs_share)

        tier_comments = population * refreshes
        # 小红书只在 detail_within_days 内补 detail；抖音恒定补。
        xhs_detail = xhs_posts * refreshes if age_mid <= settings.detail_within_days else 0.0
        dy_detail = dy_posts * refreshes
        tier_detail = xhs_detail + dy_detail

        comment_calls += tier_comments
        detail_calls += tier_detail
        rows.append({
            "label": label,
            "posts": population,
            "interval": interval_hours,
            "calls": tier_comments + tier_detail,
        })

    total_calls = comment_calls + detail_calls
    return {
        "posts": total_posts,
        "rows": rows,
        "comment_calls": comment_calls,
        "detail_calls": detail_calls,
        "calls_per_day": total_calls,
        "yuan_per_day": total_calls * YUAN_PER_CALL,
        "yuan_per_month": total_calls * YUAN_PER_CALL * 30,
    }


def _split_by_platform(per_day: float, xhs_share: float, settings: Settings):
    """把每天的调用次数按平台拆开。双通道之后两家单价差 10 倍，不拆就算不出钱。"""
    xhs_calls = dy_calls = 0.0
    previous_max = 0
    for max_age_days, interval_hours in settings.refresh.tiers:
        population = per_day * (max_age_days - previous_max)
        refreshes = 24 / interval_hours
        xhs_posts = population * xhs_share
        dy_posts = population * (1 - xhs_share)
        xhs_calls += xhs_posts * refreshes
        if max_age_days <= settings.detail_within_days:
            xhs_calls += xhs_posts * refreshes         # detail
        dy_calls += dy_posts * refreshes * 2           # 评论 + 恒定 detail
        previous_max = max_age_days
    return xhs_calls, dy_calls


def flat_daily(total_posts: float, xhs_share: float, with_detail: bool) -> dict:
    """对照组：不分层，每天全表刷一遍。"""
    xhs = total_posts * xhs_share
    dy = total_posts * (1 - xhs_share)
    calls = xhs * (2 if with_detail else 1) + dy * 2
    return {"calls_per_day": calls, "yuan_per_month": calls * YUAN_PER_CALL * 30}


def main() -> int:
    per_day = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    xhs_share = float(sys.argv[2]) if len(sys.argv) > 2 else 0.7

    settings = Settings()
    result = estimate(per_day, xhs_share, settings)

    print(f"假设：每天新发 {per_day:.0f} 条，小红书占 {xhs_share:.0%}，"
          f"{settings.refresh.archive_after_days} 天后归档停刷")
    print(f"稳态在管：{result['posts']:.0f} 条\n")

    print(f"{'档位':<14}{'条数':>8}{'刷新间隔':>10}{'调用/天':>10}")
    print("-" * 44)
    for row in result["rows"]:
        print(f"{row['label']:<14}{row['posts']:>8.0f}{row['interval']:>8}h{row['calls']:>10.0f}")
    print("-" * 44)
    print(f"{'合计':<14}{result['posts']:>8.0f}{'':>10}{result['calls_per_day']:>10.0f}")
    print(f"\n  其中评论调用 {result['comment_calls']:.0f}／detail 调用 {result['detail_calls']:.0f}")
    xhs_calls, dy_calls = _split_by_platform(per_day, xhs_share, settings)
    sdx = providers.get_provider("socialdatax")
    tik = providers.get_provider("tikhub")
    sdx_month = (xhs_calls * sdx.yuan_per_call("xhs", "comments")
                 + dy_calls * sdx.yuan_per_call("douyin", "comments")) * 30
    tik_month = (xhs_calls * tik.yuan_per_call("xhs", "comments")
                 + dy_calls * tik.yuan_per_call("douyin", "comments")) * 30

    print(f"\n  按平台拆：小红书 {xhs_calls:.0f} 次/天，抖音 {dy_calls:.0f} 次/天")
    print(f"\n【分层刷新 · 全走 SocialDataX】 {result['calls_per_day']:.0f} 次/天 "
          f"= ¥{sdx_month:,.0f}/月")
    print(f"【分层刷新 · 全走 TikHub】     同样次数 = ¥{tik_month:,.0f}/月"
          f"（省 {100 * (1 - tik_month / sdx_month):.0f}%）")
    print(f"  其中抖音那段：¥{dy_calls * 0.10 * 30:,.0f} → "
          f"¥{dy_calls * tik.yuan_per_call('douyin', 'comments') * 30:,.0f}/月"
          f"（这是省得最狠的一块）")
    print(f"\n  当前配置：小红书主通道 {settings.channels.primary('xhs')}，"
          f"抖音主通道 {settings.channels.primary('douyin')}")

    flat_full = flat_daily(result["posts"], xhs_share, with_detail=True)
    flat_lean = flat_daily(result["posts"], xhs_share, with_detail=False)
    print(f"【全表日刷】 {flat_full['calls_per_day']:.0f} 次/天 "
          f"= ¥{flat_full['yuan_per_month']:,.0f}/月"
          f"（省掉小红书 detail 则 ¥{flat_lean['yuan_per_month']:,.0f}/月）")

    manual = result["posts"] * 0.15 * 1.4
    print(f"【纯手动点】 {manual:.0f} 次/天（按 15% 行被点到估）"
          f" = ¥{manual * YUAN_PER_CALL * 30:,.0f}/月"
          f"  ⚠ 没被点到的行永远不更新，最坏陈旧度无穷大")

    no_detail = estimate(per_day, xhs_share, _without_xhs_detail(settings))
    print(f"【分层 + 关掉小红书 detail】 {no_detail['calls_per_day']:.0f} 次/天 "
          f"= ¥{no_detail['yuan_per_month']:,.0f}/月"
          f"  （代价：没有点赞/收藏，爆文只能靠评论数判）")
    return 0


def _without_xhs_detail(settings: Settings) -> Settings:
    lean = Settings()
    lean.refresh = settings.refresh
    lean.detail_within_days = 0
    return lean


if __name__ == "__main__":
    raise SystemExit(main())
