#!/usr/bin/env python3
"""拿一条真实的笔记/视频，把某条数据通道从头到尾走一遍。

    # 两条通道都验（各花一次调用）
    export TIKHUB_API_KEY=... SOCIALDATAX_API_KEY=...
    python3 tools/probe_channel.py https://www.xiaohongshu.com/explore/xxxxxxxx

    # 只验一条
    python3 tools/probe_channel.py <链接> --only tikhub

和 `cli.py doctor` 的分工：doctor 不花钱，只查飞书那边的权限和列名；
这个脚本**会真的发请求、真的扣费**，验的是「数据通道能不能用、字段全不全」。
一次小红书调用 ≈ ¥0.07（TikHub）或 ¥0.10（SocialDataX），抖音便宜十倍。

走的是 `xhsearch/providers.py` 里那一份真代码，不是另写一遍——
所以它验的就是线上会跑的东西，包括归一化、置顶识别、脱敏、错误分类。
"""

from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from xhsearch import analyze, providers, transport  # noqa: E402
from xhsearch.config import Settings  # noqa: E402
from xhsearch.links import parse  # noqa: E402
from xhsearch.protocol import Err  # noqa: E402
from xhsearch.rows import Row, plan_calls  # noqa: E402

ENV = {
    providers.TIKHUB: "TIKHUB_API_KEY",
    providers.SOCIALDATAX: "SOCIALDATAX_API_KEY",
}


def probe(name: str, key: str, link: str, settings: Settings) -> bool:
    provider = providers.get_provider(name)
    row = Row(record_id="probe", link_cell=link)
    calls = plan_calls(row, settings)

    print(f"\n{'=' * 68}\n{provider.label}\n{'=' * 68}")
    if not calls:
        print(f"  ❌ 这个链接解析不出来：{row.parsed.describe_failure()}")
        return False

    print(f"  链接识别为：{row.parsed.platform} / {row.parsed.content_id or row.parsed.url}")
    print(f"  本次会发 {len(calls)} 个请求，预计花费 ¥"
          f"{sum(provider.yuan_per_call(c.platform, c.purpose) for c in calls):.3f}")

    snapshot = None
    ok = True
    for call in calls:
        request = provider.build(key, call.platform, call.purpose, call.arguments)
        response = transport.request(request.method, request.url, request.headers,
                                     request.body, timeout=45)
        result = provider.parse(call.platform, call.purpose, response.status,
                                response.content_type, response.body,
                                response.request_id, key)

        label = "评论" if call.purpose == "comments" else "详情"
        if isinstance(result, Err):
            print(f"  {label}：❌ [{result.kind.value}] {result.operator_text()[:150]}")
            if result.kind.value == "auth":
                print(f"     → 检查 {ENV[name]} 是否填对")
            if name == providers.TIKHUB and result.kind.value == "transport":
                print(f"     → 确认这台机器能出网到 {providers.TIKHUB_BASE}")
                print("       境内用 api.tikhub.dev，境外设 TIKHUB_BASE=https://api.tikhub.io")
            ok = False
            if call.purpose == "comments":
                break
            continue

        print(f"  {label}：✅ {request.method} {response.status}")
        if call.purpose == "comments":
            snapshot = analyze.read_comment_page(call.platform, result.data)
        elif snapshot is not None:
            analyze.merge_detail(snapshot, result.data)

    if snapshot is None:
        return False

    print()
    print(f"  评论总数   {snapshot.comment_count}")
    print(f"  一级评论   {snapshot.top_level_comment_count}")
    print(f"  点赞/收藏  {snapshot.like_count} / {snapshot.collect_count}")
    print(f"  上游审核标记 {snapshot.censored}"
          "（None = 这家不提供；只写进诊断信息，不参与打标签）")
    print(f"  置顶评论   {analyze.format_pinned(snapshot) or '（无）'}")
    print("  评论区快照：")
    for line in analyze.format_digest(snapshot, settings.digest).splitlines():
        print(f"    {line}")

    if snapshot.supports_pinned and snapshot.pinned is None:
        print("\n  ⚠ 没识别到置顶评论。如果这条笔记**确实有**置顶，说明上游改字段了，"
              "\n    去 providers.py 的 _tag_types() 看一眼。"
              "\n    如果这条本来就没置顶，那是正常的——换一条有置顶的再验一次。")
    return ok


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    link = sys.argv[1]

    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].strip().lower()

    providers.set_tikhub_base(os.environ.get("TIKHUB_BASE", ""))
    settings = Settings()
    if not parse(link).usable:
        print(f"链接识别不了：{parse(link).describe_failure()}", file=sys.stderr)
        return 2

    ran = False
    all_ok = True
    for name, env_name in ENV.items():
        if only and name != only:
            continue
        key = os.environ.get(env_name, "").strip()
        if not key:
            print(f"\n（跳过 {name}：没有 {env_name}）")
            continue
        ran = True
        all_ok = probe(name, key, link, settings) and all_ok

    if not ran:
        print("一个 Key 都没配。至少设一个：", " / ".join(ENV.values()), file=sys.stderr)
        return 2

    print(f"\n{'=' * 68}")
    print("✅ 通道可用" if all_ok else "❌ 有通道不可用，看上面的报错")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
