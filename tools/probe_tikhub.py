#!/usr/bin/env python3
"""花两分钱，验完 TikHub 能不能替掉 SocialDataX。

    python3 tools/probe_tikhub.py <你的小红书笔记ID或链接> [抖音视频ID或链接]

    # 只验小红书（1 次调用，$0.01）
    TIKHUB_KEY=xxx python3 tools/probe_tikhub.py 697c0eee000000000a03c308

要回答的问题只有一个：**TikHub 的小红书评论接口到底返回不返回置顶标记。**

为什么必须实测：TikHub 的 OpenAPI 里，评论接口的响应被声明成一个无字段约束的
通用 `data` 对象（`ResponseModel`），也就是**官方没有承诺任何字段**。
SocialDataX 那边是有 typed schema 的，`is_pinned` 白纸黑字写在规范里。
所以这一条不能靠读文档定，只能打一次真请求看。

⚠️ 小红书的接口 `allow_free_credit = 0`——签到送的免费额度用不了，
必须先充值才能跑这个脚本。抖音接口可以用免费额度。

⚠️ 大陆网络请用 api.tikhub.dev（脚本默认就是它）；主域名 api.tikhub.io 被墙。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("TIKHUB_BASE", "https://api.tikhub.dev")

# 我们真正会用到的三个端点，价格取自 TikHub 自己的公开计价接口
# https://api.tikhub.dev/api/v1/tikhub/user/get_all_endpoints_info
XHS_COMMENTS = "/api/v1/xiaohongshu/app_v2/get_note_comments"      # $0.01
XHS_DETAIL = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"    # $0.01（图文/视频通吃）
DY_COMMENTS = "/api/v1/douyin/app/v3/fetch_video_comments"         # $0.001


def call(path: str, params: dict, key: str) -> dict:
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        # 必须伪装 UA。TikHub 挂在 Cloudflare 后面，urllib 的默认 UA
        # （Python-urllib/3.x）会被直接判成机器人：HTTP 403 + error_code 1010
        # browser_signature_banned，连业务层都到不了。
        # 这一点 SocialDataX 没有——那边裸 urllib 就能通。
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return {"http": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return {"http": exc.code, "body": json.loads(raw)}
        except json.JSONDecodeError:
            return {"http": exc.code, "body": {"_raw": raw[:500]}}
    except Exception as exc:  # noqa: BLE001
        return {"http": -1, "body": {"_error": f"{type(exc).__name__}: {exc}"}}


def walk_keys(obj, prefix="", out=None, depth=0):
    """把嵌套结构里出现过的字段路径全铺平，方便肉眼找 is_pinned。"""
    out = {} if out is None else out
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            out.setdefault(path, type(v).__name__)
            walk_keys(v, path, out, depth + 1)
    elif isinstance(obj, list) and obj:
        walk_keys(obj[0], f"{prefix}[]", out, depth + 1)
    return out


def hunt(paths: dict, words: list[str]) -> list[str]:
    return [p for p in paths if any(w in p.lower() for w in words)]


def report(label: str, result: dict, words: list[str]) -> None:
    print(f"\n{'=' * 68}\n{label}\n{'=' * 68}")
    body = result["body"]
    # 成功和失败是两套信封：成功时 code/message 在顶层，失败时整个塞进 detail。
    # ⚠️ 失败信封的 message 里会把你提交的 API Key 原样回显出来。
    #    我们的 runner 会把错误文案写进飞书的「最后错误」列——照抄就等于把 Key
    #    写进表里给全公司看。接 TikHub 的话，这一段必须先脱敏再落库。
    envelope = body.get("detail") if isinstance(body.get("detail"), dict) else body
    print(f"HTTP {result['http']}  code={envelope.get('code')}  "
          f"message={str(envelope.get('message_zh') or envelope.get('message'))[:120]}")
    if "_error" in body:
        print(f"  ❌ 连不上：{body['_error']}")
        print("     大陆网络确认用的是 api.tikhub.dev，不是 api.tikhub.io")
        return

    data = body.get("data")
    if data is None:
        print("  ❌ 没有 data，整体响应：", json.dumps(body, ensure_ascii=False)[:400])
        return

    paths = walk_keys(data)
    print(f"  data 里共 {len(paths)} 个字段路径")

    hits = hunt(paths, words)
    if hits:
        print(f"  ✅ 命中关键字段：")
        for h in sorted(hits):
            print(f"       {h}  ({paths[h]})")
    else:
        print(f"  ❌ 没有任何字段名包含 {words}")
        print(f"     —— 这一条如果是 is_pinned，说明 TikHub 做不了置顶监控（R2/R7）")

    print("  前 40 个字段路径（自己扫一眼有没有别的名字表达同一含义）：")
    for p in sorted(paths)[:40]:
        print(f"       {p}")


def main() -> int:
    key = os.environ.get("TIKHUB_KEY", "").strip()
    if not key:
        print("先设环境变量：export TIKHUB_KEY=你的key", file=sys.stderr)
        return 2
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    note = sys.argv[1]
    is_link = note.startswith("http") or "xhslink" in note
    params = {"share_text": note} if is_link else {"note_id": note}

    # 关键：我们只要第一页，永远不翻页。
    # TikHub 文档说 sort_strategy="default" 不推荐，理由是「翻页时会丢失或重复评论」——
    # 那是分页场景的问题，对只取第一页的我们不成立。综合排序才是运营眼里的评论区，
    # 也才是置顶评论会出现的那一屏，所以这里就是要 default。
    report(
        "① 小红书评论（sort_strategy=default，只取第一页）",
        call(XHS_COMMENTS, {**params, "index": 0, "sort_strategy": "default"}, key),
        ["pin", "top", "stick", "sticky", "置顶"],
    )

    print("\n" + "-" * 68)
    print("② 顺带看一眼评论总数字段（爆文判定要靠它）")
    print("-" * 68)
    r = call(XHS_COMMENTS, {**params, "index": 0, "sort_strategy": "latest_v2"}, key)
    paths = walk_keys(r["body"].get("data") or {})
    cnt = hunt(paths, ["comment_count", "total", "count"])
    print("  评论数候选字段：", sorted(cnt) or "❌ 一个都没有")

    if len(sys.argv) > 2:
        report(
            "③ 抖音评论（$0.001，只验能不能拿到总数）",
            call(DY_COMMENTS, {"aweme_id": sys.argv[2], "cursor": 0, "count": 20}, key),
            ["total", "comment_count", "stick", "pin"],
        )

    print("\n" + "=" * 68)
    print("怎么判：")
    print("  ①里有 is_pinned / stick 之类  →  TikHub 可以整套替换，省一半钱")
    print("  ①里没有                      →  小红书必须留在 SocialDataX，")
    print("                                  抖音那 224 次/天可以搬去 TikHub（贵 10 倍→便宜 10 倍）")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
