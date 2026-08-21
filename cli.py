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

from xhsearch import feishu, rows as rows_mod, runner
from xhsearch.config import Settings


def _env(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if required and not value:
        sys.exit(f"缺少环境变量 {name}（参考 .env.example）")
    return value


def _table() -> feishu.Bitable:
    return feishu.Bitable(
        app_id=_env("FEISHU_APP_ID"),
        app_secret=_env("FEISHU_APP_SECRET"),
        app_token=_env("FEISHU_APP_TOKEN"),
        table_id=_env("FEISHU_TABLE_ID"),
    )


def _settings() -> Settings:
    settings = Settings()
    # 独立服务跑批量不需要软截止（那是给扣子 60 秒硬上限准备的）。
    settings.soft_deadline_seconds = float(os.environ.get("SOFT_DEADLINE_SECONDS", "0") or 0)
    if os.environ.get("MAX_CONCURRENCY"):
        settings.max_concurrency = int(os.environ["MAX_CONCURRENCY"])
    if os.environ.get("DETAIL_WITHIN_DAYS"):
        settings.detail_within_days = int(os.environ["DETAIL_WITHIN_DAYS"])
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

    print("④ SocialDataX Key …", end=" ", flush=True)
    if os.environ.get("SOCIALDATAX_API_KEY", "").strip():
        print("已配置（是否有效需要真实调用一次才知道）")
    else:
        print("缺失")
        problems.append("没有 SOCIALDATAX_API_KEY，任何刷新都会立刻失败")

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
    api_key = _env("SOCIALDATAX_API_KEY")
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

    credits = rows_mod.estimate_credits(row_list, settings, now)
    print(f"待刷 {len(row_list)} 行，预计消耗 {credits} 积分 ≈ ¥{credits / 100:.2f}")

    if mode == "estimate":
        return 0

    report = runner.refresh(
        row_list, api_key, settings,
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
    return 1 if report.aborted_reason else 0


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
