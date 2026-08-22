#!/usr/bin/env python3
"""命令行入口。

    python3 cli.py doctor              # 体检：不花积分，检查配置/权限/字段是否齐全
    python3 cli.py sweep               # 分层巡检：只刷到期的行
    python3 cli.py queue               # 只刷勾了「排队刷新」的行
    python3 cli.py row <record_id>...  # 刷指定行（无视冷却和分层节流）
    python3 cli.py estimate            # 只估算这一轮要花多少钱，不发请求

配置走环境变量，见 .env.example。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from xhsearch import feishu, providers, rows as rows_mod, runner
from xhsearch.config import Channels, Settings


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        sys.exit(f"缺少环境变量 {name}（参考 .env.example）")
    return value


def _apply_endpoint_overrides() -> None:
    """按部署位置切 TikHub 的接入域名。

    对方文档要求「请勿跨区使用，会影响访问速度」：
        境内（扣子、国内 VPS）  → api.tikhub.dev（默认，不用设）
        境外（Railway、Actions）→ TIKHUB_BASE=https://api.tikhub.io
    """
    providers.set_tikhub_base(os.environ.get("TIKHUB_BASE", ""))


def _api_keys() -> dict[str, str]:
    """双通道的凭据。两个都配就自动开降级；只配一个也能跑。

    环境变量名和供应商名一一对应，加第三家时不用改这里的结构。
    """
    keys = {
        providers.TIKHUB: os.environ.get("TIKHUB_API_KEY", "").strip(),
        providers.SOCIALDATAX: os.environ.get("SOCIALDATAX_API_KEY", "").strip(),
    }
    return {k: v for k, v in keys.items() if v}


def _table() -> feishu.Bitable:
    return feishu.Bitable(
        app_id=_env("FEISHU_APP_ID"),
        app_secret=_env("FEISHU_APP_SECRET"),
        app_token=_env("FEISHU_APP_TOKEN"),
        table_id=_env("FEISHU_TABLE_ID"),
    )


def _settings() -> Settings:
    _apply_endpoint_overrides()
    settings = Settings()
    # 独立服务跑批量不需要软截止（那是给扣子 60 秒硬上限准备的）。
    settings.soft_deadline_seconds = float(os.environ.get("SOFT_DEADLINE_SECONDS", "0") or 0)
    if os.environ.get("MAX_CONCURRENCY"):
        settings.max_concurrency = int(os.environ["MAX_CONCURRENCY"])
    if os.environ.get("DETAIL_WITHIN_DAYS"):
        settings.detail_within_days = int(os.environ["DETAIL_WITHIN_DAYS"])
    # CHANNEL_ORDER="xhs=tikhub,socialdatax; douyin=tikhub"
    # 想把某个平台钉死在一家时用，不改代码。
    spec = os.environ.get("CHANNEL_ORDER", "").strip()
    if spec:
        order = {}
        for chunk in spec.split(";"):
            if "=" not in chunk:
                continue
            platform, names = chunk.split("=", 1)
            picked = [n.strip().lower() for n in names.split(",") if n.strip()]
            if picked:
                order[platform.strip()] = picked
        if order:
            settings.channels = Channels(order=order)
    return settings


def cmd_doctor() -> int:
    """上线前体检。不花一分钱，但能挡掉九成的「配好了跑不通」。"""
    settings = _settings()
    f = settings.fields
    table = _table()
    problems: list[str] = []

    print("① 取 tenant_access_token …", end=" ", flush=True)
    try:
        table.token()
        print("OK")
    except Exception as exc:
        print("失败")
        print(f"   {exc}")
        return 1

    print("② 读表字段 …", end=" ", flush=True)
    options = table.list_field_options(f.traffic_status)
    if options is None:
        print("读不到字段列表")
        problems.append(
            f"读不到字段列表。多半是应用没被加进这张多维表格："
            f"表格右上角「…」→「添加文档应用」把应用加成协作者。"
            f"若这张表开了「高级权限」，还要在高级权限里给应用「可管理」——"
            f"漏这一步的表现是读到空结果而不是报错。"
        )
    else:
        print(f"OK（「{f.traffic_status}」有 {len(options)} 个选项：{'、'.join(options)}）")
        missing = [t for t in settings.tags.namespace() if t not in options]
        if missing:
            problems.append(
                f"「{f.traffic_status}」缺这些选项，请先在飞书里手工建好：{'、'.join(missing)}。"
                f"没建的话机器会跳过它们（不会误写），但对应的判定就等于没生效。"
            )

    status_options = table.list_field_options(f.comment_status)
    if status_options is not None:
        print(f"   「{f.comment_status}」有 {len(status_options)} 个选项："
              f"{'、'.join(status_options)}")
        missing = [v for v in settings.comment_status.namespace() if v not in status_options]
        if missing:
            problems.append(
                f"「{f.comment_status}」缺这些选项，请先在飞书里手工建好：{'、'.join(missing)}。"
                f"机器要往这一列写它们，没建就写不进去（不会误写，但置顶判定等于没生效）。"
            )

    print("③ 试读一行 …", end=" ", flush=True)
    try:
        sample = table.search(f.must_read(), max_records=1)
        print(f"OK（读到 {len(sample)} 行）")
        if sample:
            present = set((sample[0].get("fields") or {}).keys())
            # 飞书不会为空单元格返回键，所以只能提示而不能断言。
            print(f"   本行有值的列：{'、'.join(sorted(present)) or '（全空）'}")
    except Exception as exc:
        print("失败")
        problems.append(str(exc))

    print("④ 数据通道 …")
    keys = _api_keys()
    for platform in ("xhs", "douyin"):
        order = settings.channels.for_platform(platform)
        live = providers.usable_order(settings.channels, platform, keys)
        label = "小红书" if platform == "xhs" else "抖音"
        if not live:
            print(f"   {label}：❌ 配置的是 {'、'.join(order)}，但一个 Key 都没有")
            problems.append(f"{label}没有可用的数据通道，任何刷新都会立刻失败")
        elif len(live) == 1:
            print(f"   {label}：⚠ 只有 {live[0]} 一条通道，它挂了这一轮就全丢")
        else:
            print(f"   {label}：✅ {live[0]} 为主，{'、'.join(live[1:])} 兜底")
    if keys:
        print(f"   已配置的 Key：{'、'.join(sorted(keys))}"
              "（是否有效需要真实调用一次才知道）")

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for i, problem in enumerate(problems, 1):
            print(f"  {i}. {problem}")
        return 1
    print("✅ 全部通过，可以开跑")
    return 0


def _run(mode: str, record_ids: list[str] | None) -> int:
    settings = _settings()
    table = _table()
    api_keys = _api_keys()
    if not api_keys:
        sys.exit("一个数据通道的 Key 都没配：需要 TIKHUB_API_KEY 或 SOCIALDATAX_API_KEY"
                 "（参考 .env.example）")
    now = datetime.now(timezone.utc)

    print(f"读表（模式：{mode}）…")
    row_list = runner.load_rows(
        table,
        settings,
        only_record_ids=record_ids,
        only_due=(mode == "sweep"),
        only_queued=(mode == "queue"),
        now=now,
    )
    if not row_list:
        print("没有需要刷新的行。")
        return 0

    yuan = rows_mod.estimate_yuan(row_list, settings, now)
    print(f"待刷 {len(row_list)} 行，预计花费 ≈ ¥{yuan:.2f}"
          f"（按每个平台的主通道单价算："
          f"小红书走 {settings.channels.primary('xhs')}，"
          f"抖音走 {settings.channels.primary('douyin')}）")

    if mode == "estimate":
        return 0

    report = runner.refresh(
        row_list, api_keys, settings,
        now=now,
        known_options=table.list_field_options(settings.fields.traffic_status),
        comment_status_options=table.list_field_options(settings.fields.comment_status),
        forced=(record_ids is not None),
        progress=print,
    )
    print()
    print(report.summary())

    written = runner.write_back(table, report)
    print(f"已写回 {written} 行")
    # 只有真故障才返回非零。到软截止后「留给下一轮」是正常运行，
    # 返回非零会让 cron / 云平台的重启策略把它当失败反复重启。
    return 1 if report.fatal else 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    command = argv[1]
    if command == "doctor":
        return cmd_doctor()
    if command in ("sweep", "queue", "estimate"):
        return _run(command, None)
    if command == "row":
        if len(argv) < 3:
            sys.exit("用法：python3 cli.py row <record_id> [<record_id> ...]")
        return _run("row", argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
