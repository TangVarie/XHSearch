"""双通道：同一个业务逻辑，可以走 SocialDataX，也可以走 TikHub。

**这一层存在的唯一理由是把两家的响应差异挡在这里。**
`analyze.py` 只认一种形状的字典，两家的原始响应都在这里归一化成那一种。
换供应商、加供应商，都不该让判定逻辑改一行。

    build(...)  → Request   组包（两家一个用 POST+JSON，一个用 GET+query）
    parse(...)  → Ok | Err  解包 + 归一化 + 错误分类

归一化后的形状（就是 SocialDataX 的原生形状，因为 analyze 是照它写的）：

    评论页  {"items": [{content, like_count, is_pinned, is_author_comment,
                        ip_location, author}],
             "comment_count": int|None, "top_level_comment_count": int|None}
    详情    {"like_count", "collect_count", "share_count", "comment_count",
             "_censored": bool|None}

以下全部是 2026-08 用真 Key 实测得到的，不是读文档推的：

* TikHub 的小红书评论接口**有置顶标记**，但不叫 `is_pinned`——
  在 `show_tags_v2` 里，形如 `{"type": "user_top", "text": "Pinned"}`。
  作者本人的评论是 `{"type": "is_author"}`。
  ⚠️ 认 `type` 不要认 `text`：`text` 实测返回的是英文 "Pinned"/"Author"。
* TikHub 的评论接口对**不存在的笔记**返回 HTTP 200 + `code:0` + `success:true`，
  评论列表为空、`comment_count` 为 0 —— 和「一条真实存在但零评论的笔记」
  长得几乎一样。唯二的区别是 `user_id` 为空串、`all_sort_strategies` 为空数组。
  这是启发式判断，不是厂商契约，所以只当**嫌疑**，交给两击定罪去确认。
* 真正干净的死亡信号在 detail 接口：小红书 `data` 直接是 `[]`；
  抖音 `aweme_detail` 为 null 且 `filter_list` 里带着那条 aweme_id。
  这两个是确定性的，可以一次定罪。
* TikHub 的错误响应会把**你提交的 API Key 原样回显**在 message 里。
  错误文案会被写进飞书的诊断列，所以 `_redact()` 必须在落库前把它抹掉。
* TikHub 挂在 Cloudflare 后面，**不带浏览器 UA 会被判成机器人**
  （HTTP 403 / error_code 1010 browser_signature_banned），连业务层都到不了。
* 大陆网络必须用 `api.tikhub.dev`，`api.tikhub.io` 被墙。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

# 直接导名字，不要 `from . import protocol` —— 打包进扣子时模块会被拼成一个
# 扁平文件，那里根本没有 protocol 这个名字，用 protocol.xxx 会当场 NameError。
# 也必须写成一行：打包脚本是按行剥 import 的，跨行的括号形式会留下半截语法。
from .protocol import Err, Failure, Ok, Result, build_body, endpoint, headers, parse_response  # noqa: E501

SOCIALDATAX = "socialdatax"
TIKHUB = "tikhub"

# TikHub 有两个同功能的域名，**按你的服务器在哪选**（对方文档要求「请勿跨区使用」）：
#   api.tikhub.dev   境内可直连（api.tikhub.io 在大陆被防火墙拦截）
#   api.tikhub.io    主域名，境外服务器（Railway / GitHub Actions）用这个
# 默认取 .dev：它两边都通（境外也实测可用），配错了最多是慢一点，不会不通。
# 跑在境外就设环境变量 TIKHUB_BASE=https://api.tikhub.io。
TIKHUB_BASE = "https://api.tikhub.dev"


def set_tikhub_base(url: str) -> None:
    """改 TikHub 的接入域名。

    做成函数而不是读环境变量，是因为这份代码要整段粘进扣子代码节点——
    那边没有环境变量这回事。宿主（cli.py）负责从环境里读，扣子那边用默认值。
    """
    global TIKHUB_BASE
    cleaned = (url or "").strip().rstrip("/")
    if cleaned:
        TIKHUB_BASE = cleaned

# Cloudflare 会按 UA 拦截，裸的 Python-urllib/3.x 直接 403。
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

TIKHUB_PATHS = {
    ("xhs", "comments"): "/api/v1/xiaohongshu/app_v2/get_note_comments",
    # 图文接口对视频笔记同样返回完整互动数据（只是没有播放地址，我们不需要），
    # 所以两种笔记共用一个端点，不必先判类型再选接口。实测已确认。
    ("xhs", "detail"): "/api/v1/xiaohongshu/app_v2/get_image_note_detail",
    ("douyin", "comments"): "/api/v1/douyin/app/v3/fetch_video_comments",
    ("douyin", "detail"): "/api/v1/douyin/app/v3/fetch_one_video",
}

# 单价（元/次）。TikHub 的数字来自它自己公开免鉴权的计价接口
# https://api.tikhub.dev/api/v1/tikhub/user/get_all_endpoints_info
# ⚠️ 小红书那两个端点 allow_discount=0，走量折扣对它们不生效。
_USD_TO_CNY = 7.2
TIKHUB_USD = {"xhs": 0.010, "douyin": 0.001}
SOCIALDATAX_YUAN = 0.10   # 10 积分 × ¥0.01，全平台统一价


_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~"
)


def _quote(value: Any) -> str:
    """百分号编码。刻意不用 urllib.parse——这份代码要整段粘进扣子代码节点，
    那边禁用了 http.client，围绕 urllib 有多少能用、多少不能用没有文档保证。
    一个六行的编码器换掉一整个不确定性，很划算。"""
    out = []
    for byte in str(value).encode("utf-8"):
        char = chr(byte)
        out.append(char if char in _SAFE else f"%{byte:02X}")
    return "".join(out)


def _query(params: dict[str, Any]) -> str:
    return "&".join(f"{_quote(k)}={_quote(v)}" for k, v in params.items())


@dataclass
class Request:
    method: str
    url: str
    headers: dict[str, str]
    body: str = ""


@dataclass
class Provider:
    name: str
    label: str
    build: Callable[[str, str, str, dict[str, Any]], Request]
    parse: Callable[..., Result]
    yuan_per_call: Callable[[str, str], float]
    # 「查不到内容」这种业务失败，这家收不收钱。
    #
    # TikHub：**收**。它自己的接口文档写着「传入错误或不存在的笔记ID……
    # 该请求同样会正常计费扣费」，实测余额确实少了 $0.01。
    # SocialDataX：**未知**，只有蒲公英那组工具明文写了失败不扣费。
    # 这里保守当作不收——宁可少报一点点备胎的钱，也不要凭猜测把账单虚高，
    # 一个天天虚报成本的监控没人会信。验完见 docs/待验证清单.md 第 6 项。
    bills_failed_lookups: bool = False


# =============================================================================
#  SocialDataX：原样透传，因为 analyze 就是照它的字段写的
# =============================================================================


def _sdx_build(api_key: str, platform: str, purpose: str, arguments: dict[str, Any]) -> Request:
    args = dict(arguments)
    # 抽象参数 → SocialDataX 的参数名
    if platform == "xhs" and purpose == "comments":
        args["sort_type"] = args.pop("sort", "default")
    else:
        args.pop("sort", None)
    if "url" in args and platform == "xhs":
        args["note_url"] = args.pop("url")
    return Request(
        "POST",
        endpoint(platform, purpose),
        headers(api_key),
        build_body(args),
    )


def _sdx_parse(platform: str, purpose: str, http_status: int, content_type: str,
               body: str, request_id: str = "", api_key: str = "") -> Result:
    return parse_response(http_status, content_type, body, request_id)


# =============================================================================
#  TikHub：原始上游响应，需要归一化
# =============================================================================


def _tikhub_build(api_key: str, platform: str, purpose: str, arguments: dict[str, Any]) -> Request:
    path = TIKHUB_PATHS.get((platform, purpose))
    if path is None:
        raise ValueError(f"TikHub 没有 {platform}/{purpose} 的端点")

    args = dict(arguments)
    params: dict[str, Any] = {}

    if platform == "xhs":
        if args.get("note_id"):
            params["note_id"] = args["note_id"]
        elif args.get("url"):
            # 分享链接走 share_text，长链短链都吃。
            params["share_text"] = args["url"]
        if purpose == "comments":
            params["index"] = 0
            # 只取第一页，永远不翻页。对方文档说 default 排序「翻页时会丢失或
            # 重复评论」——那是分页场景的问题，对我们不成立。而综合排序才是
            # 运营眼里的评论区，也是置顶评论出现的那一屏。
            params["sort_strategy"] = args.get("sort", "default")
    else:
        if args.get("aweme_id"):
            params["aweme_id"] = args["aweme_id"]
        elif args.get("url"):
            params["aweme_id"] = args["url"]
        if purpose == "comments":
            params["cursor"] = 0
            # 对方文档明写「count 请保持默认，否则会出现 BUG」。
            params["count"] = 20

    return Request(
        "GET",
        f"{TIKHUB_BASE}{path}?{_query(params)}",
        {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": _BROWSER_UA,
        },
        "",
    )


def _redact(text: str, api_key: str) -> str:
    """把回显的 API Key 从错误文案里抹掉。

    TikHub 的 401 响应原文长这样：
        "无效的API令牌，您提交的API令牌为 <你的key>。..."
    这段文案会被 runner 写进飞书的诊断列。不抹掉就等于把 Key 贴在表里。
    """
    if api_key and len(api_key) >= 6:
        text = text.replace(api_key, "***")
        # 有些实现会截断后再回显，把前后缀也一起挡掉。
        text = text.replace(api_key[:8], "***").replace(api_key[-8:], "***")
    return text


_TIKHUB_HINTS: list[tuple[tuple[str, ...], Failure, bool]] = [
    (("余额", "充值", "insufficient", "balance"), Failure.QUOTA, True),
    (("令牌", "token", "unauthorized", "api key"), Failure.AUTH, True),
    (("频繁", "rate limit", "too many"), Failure.RATE_LIMIT, True),
]


def _tikhub_error(payload: dict[str, Any], http_status: int, api_key: str) -> Err:
    # Cloudflare 的拦截长得完全不像 TikHub 的业务错误：没有 detail 信封，
    # 带的是 error_code / error_name / ray_id，而且 HTTP 是 403。
    # 403 默认会被归成 AUTH，那是错的——这不是 Key 的问题，是这条出口被拦了。
    # 归错的代价很大：AUTH 会把这个通道整轮标死，而它换个网络就好了。
    if payload.get("error_code") or payload.get("ray_id"):
        return Err(
            Failure.TRANSPORT,
            str(payload.get("error_code") or f"http_{http_status}"),
            f"被 Cloudflare 拦截（{payload.get('error_name') or payload.get('title') or '未知原因'}）"
            "：多半是 User-Agent 或出口 IP 的问题，不是 API Key 的问题",
            http_status=http_status,
            definitive=True,
        )

    # 成功和失败是两套信封：成功时 code/message 在顶层，失败时整个塞进 detail。
    envelope = payload.get("detail") if isinstance(payload.get("detail"), dict) else payload
    code = envelope.get("code", http_status)
    message = _redact(
        str(envelope.get("message_zh") or envelope.get("message") or json.dumps(payload, ensure_ascii=False)[:300]),
        api_key,
    )
    request_id = str(envelope.get("request_id") or "")

    # TikHub 没有公开的业务错误码表，只能按 HTTP 状态 + 文案归类。
    # 这一点比 SocialDataX 差：那边每个码的语义和「要不要重试」都是写在规范里的。
    haystack = message.lower()
    for needles, kind, definitive in _TIKHUB_HINTS:
        if any(n.lower() in haystack for n in needles):
            return Err(kind, str(code), message, http_status=http_status,
                       request_id=request_id, definitive=definitive)

    if http_status in (401, 403):
        kind, definitive = Failure.AUTH, True
    elif http_status == 402:
        kind, definitive = Failure.QUOTA, True
    elif http_status == 404:
        kind, definitive = Failure.GONE, False
    elif http_status == 429:
        kind, definitive = Failure.RATE_LIMIT, True
    elif http_status >= 500 or http_status == 0:
        kind, definitive = Failure.TRANSPORT, True
    else:
        kind, definitive = Failure.UNKNOWN, False

    return Err(kind, str(code), message, http_status=http_status,
               request_id=request_id, definitive=definitive)


def _tag_types(comment: dict[str, Any]) -> set[str]:
    """把 show_tags / show_tags_v2 里的 type 值收成一个集合。

    置顶 = "user_top"，作者 = "is_author"。实测 v1 的 show_tags 一直是空数组，
    标记全在 v2 里，但两个都收，免得哪天上游改回去。
    """
    out: set[str] = set()
    for key in ("show_tags", "show_tags_v2"):
        for tag in comment.get(key) or []:
            if isinstance(tag, dict) and tag.get("type"):
                out.add(str(tag["type"]))
            elif isinstance(tag, str):
                out.add(tag)
    return out


# TikHub 透传的是小红书 App 原始响应，IP 属地实测是英文（"Shanghai"、"Guizhou"）。
# 「评论区快照」是运营每天要扫的一列，中英混排读起来很别扭，所以在这里翻回中文。
# 查不到的原样保留——宁可显示 "Shanghai" 也别显示空白。
_CN_LOCATION = {
    "beijing": "北京", "tianjin": "天津", "shanghai": "上海", "chongqing": "重庆",
    "hebei": "河北", "shanxi": "山西", "liaoning": "辽宁", "jilin": "吉林",
    "heilongjiang": "黑龙江", "jiangsu": "江苏", "zhejiang": "浙江", "anhui": "安徽",
    "fujian": "福建", "jiangxi": "江西", "shandong": "山东", "henan": "河南",
    "hubei": "湖北", "hunan": "湖南", "guangdong": "广东", "hainan": "海南",
    "sichuan": "四川", "guizhou": "贵州", "yunnan": "云南", "shaanxi": "陕西",
    "gansu": "甘肃", "qinghai": "青海", "taiwan": "台湾",
    "inner mongolia": "内蒙古", "guangxi": "广西", "tibet": "西藏", "xizang": "西藏",
    "ningxia": "宁夏", "xinjiang": "新疆", "hong kong": "香港", "macao": "澳门",
    "macau": "澳门", "china": "中国",
}


def _cn_location(value: Any) -> str:
    text = str(value or "").strip()
    return _CN_LOCATION.get(text.lower(), text)


def _xhs_comment_items(comments: list[Any]) -> list[dict[str, Any]]:
    items = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        types = _tag_types(c)
        user = c.get("user") if isinstance(c.get("user"), dict) else {}
        items.append({
            "content": c.get("content") or "",
            "like_count": c.get("like_count") or 0,
            "is_pinned": "user_top" in types,
            "is_author_comment": "is_author" in types,
            # 实测返回的是英文地名（"Shanghai" / "Guizhou"），翻回中文，
            # 好让两家通道在「评论区快照」这一列里长得一样。
            "ip_location": _cn_location(c.get("ip_location")),
            "author": {"nickname": user.get("nickname") or ""},
        })
    return items


def _tikhub_normalize(platform: str, purpose: str, payload: dict[str, Any],
                      http_status: int, request_id: str, api_key: str) -> Result:
    inner = payload.get("data")
    if not isinstance(inner, dict):
        return Err(Failure.UNKNOWN, "no_data",
                   _redact(json.dumps(payload, ensure_ascii=False)[:300], api_key),
                   http_status=http_status, request_id=request_id)

    if platform == "xhs" and purpose == "comments":
        core = inner.get("data") if isinstance(inner.get("data"), dict) else {}
        comments = core.get("comments") if isinstance(core.get("comments"), list) else []
        # 死亡嫌疑：活笔记一定带着作者 user_id 和三个排序策略；
        # 不存在的笔记这两样都是空的。启发式，所以不 definitive——交给两击定罪。
        alive = bool(core.get("user_id")) or bool(core.get("all_sort_strategies"))
        if not alive and not comments:
            return Err(Failure.GONE, "empty_shell",
                       "评论接口返回了空壳（无作者、无排序策略、无评论），疑似笔记已不存在",
                       http_status=http_status, request_id=request_id, definitive=False)
        return Ok({
            "items": _xhs_comment_items(comments),
            "comment_count": core.get("comment_count"),
            # comment_count 是含楼中楼的总数，和 detail 的 comments_count 一致（实测）；
            # comment_count_l1 只算一级评论。阈值用总数，跟 App 里显示的口径对齐。
            "top_level_comment_count": core.get("comment_count_l1"),
        }, request_id=request_id)

    if platform == "xhs" and purpose == "detail":
        core = inner.get("data")
        note_list = core[0].get("note_list") if isinstance(core, list) and core and isinstance(core[0], dict) else None
        if not note_list:
            # 干净的死亡信号：不存在的笔记，data 直接是 []（实测）。
            return Err(Failure.GONE, "no_note", "详情接口没有返回任何笔记，笔记已不存在或不可见",
                       http_status=http_status, request_id=request_id, definitive=True)
        note = note_list[0] if isinstance(note_list[0], dict) else {}
        return Ok({
            "like_count": note.get("liked_count"),
            "collect_count": note.get("collected_count"),
            "share_count": note.get("shared_count"),
            "comment_count": note.get("comments_count"),
            # 上游自己的审核标记。语义还没实地验过（只见过 false），
            # 所以只写进诊断信息给人看，不参与打标签。见 docs/待验证清单.md。
            "_censored": note.get("in_censor"),
        }, request_id=request_id)

    if platform == "douyin" and purpose == "comments":
        comments = inner.get("comments") if isinstance(inner.get("comments"), list) else []
        items = []
        for c in comments:
            if not isinstance(c, dict):
                continue
            user = c.get("user") if isinstance(c.get("user"), dict) else {}
            items.append({
                "content": c.get("text") or "",
                "like_count": c.get("digg_count") or 0,
                # 抖音一律不判置顶——接口里那个 label_type 的语义没验过，
                # 而错判置顶比不判置顶伤害大得多。见 config.CommentStatus。
                "is_pinned": False,
                "is_author_comment": False,
                "ip_location": _cn_location(c.get("ip_label")),
                "author": {"nickname": user.get("nickname") or ""},
            })
        # 实测 total 是实打实的整数，不像 SocialDataX 那边是 integer|null。
        return Ok({"items": items, "comment_count": inner.get("total")}, request_id=request_id)

    if platform == "douyin" and purpose == "detail":
        aweme = inner.get("aweme_detail")
        if not isinstance(aweme, dict):
            # 干净的死亡信号：视频没了的时候 aweme_detail 是 null，
            # 同时 filter_list 里会带着这条 aweme_id 和一个 reason（实测）。
            filtered = inner.get("filter_list")
            if isinstance(filtered, list) and filtered:
                reason = filtered[0].get("reason") if isinstance(filtered[0], dict) else ""
                return Err(Failure.GONE, "filtered",
                           f"视频已被下架或不可见（上游 filter reason={reason}）",
                           http_status=http_status, request_id=request_id, definitive=True)
            return Err(Failure.GONE, "no_aweme", "详情接口没有返回视频数据",
                       http_status=http_status, request_id=request_id, definitive=False)
        stats = aweme.get("statistics") if isinstance(aweme.get("statistics"), dict) else {}
        status = aweme.get("status") if isinstance(aweme.get("status"), dict) else {}
        censored = None
        if status:
            censored = bool(status.get("is_prohibited") or status.get("in_reviewing"))
        return Ok({
            "like_count": stats.get("digg_count"),
            "collect_count": stats.get("collect_count"),
            "share_count": stats.get("share_count"),
            "comment_count": stats.get("comment_count"),
            "_censored": censored,
        }, request_id=request_id)

    return Err(Failure.UNKNOWN, "unsupported", f"TikHub 不支持 {platform}/{purpose}",
               http_status=http_status, request_id=request_id)


def _tikhub_parse(platform: str, purpose: str, http_status: int, content_type: str,
                  body: str, request_id: str = "", api_key: str = "") -> Result:
    try:
        payload = json.loads(body or "")
    except json.JSONDecodeError:
        return Err(
            Failure.TRANSPORT if http_status >= 500 or http_status == 0 else Failure.UNKNOWN,
            f"http_{http_status}",
            _redact((body or f"HTTP {http_status}")[:500], api_key),
            http_status=http_status, request_id=request_id,
        )

    if not isinstance(payload, dict):
        return Err(Failure.UNKNOWN, "unexpected_body", (body or "")[:500],
                   http_status=http_status, request_id=request_id)

    # 失败信封：整个错误对象塞在 detail 里。也兜住 Cloudflare 拦截时那种
    # 完全不同形状的响应（有 error_code / title，没有 detail）。
    if isinstance(payload.get("detail"), dict) or http_status >= 400:
        return _tikhub_error(payload, http_status, api_key)

    rid = str(payload.get("request_id") or request_id or "")
    return _tikhub_normalize(platform, purpose, payload, http_status, rid, api_key)


def _tikhub_yuan(platform: str, purpose: str) -> float:
    return TIKHUB_USD.get(platform, 0.010) * _USD_TO_CNY


def _sdx_yuan(platform: str, purpose: str) -> float:
    return SOCIALDATAX_YUAN


REGISTRY: dict[str, Provider] = {
    SOCIALDATAX: Provider(SOCIALDATAX, "SocialDataX", _sdx_build, _sdx_parse, _sdx_yuan,
                          bills_failed_lookups=False),
    TIKHUB: Provider(TIKHUB, "TikHub", _tikhub_build, _tikhub_parse, _tikhub_yuan,
                     bills_failed_lookups=True),
}


def get_provider(name: str) -> Provider:
    """按名字取供应商。

    名字刻意叫 get_provider 而不是 get——这份代码会被打成一个扁平的单文件粘进
    扣子，一个叫 get 的模块级函数在那种命名空间里迟早撞车。
    """
    provider = REGISTRY.get((name or "").strip().lower())
    if provider is None:
        raise ValueError(f"没有叫 {name!r} 的供应商，可选：{'、'.join(sorted(REGISTRY))}")
    return provider


# 值得换一家再试的失败。**GONE 不在里面**：那是行级结论，不是通道故障，
# 换一家只会再花一次钱得到同一个答案。UNKNOWN 也不在——没看懂的错误
# 换个供应商多半还是看不懂，白烧钱。
FAILOVER_KINDS = frozenset({Failure.TRANSPORT, Failure.AUTH, Failure.QUOTA})


def credentials(api_key: Any) -> dict[str, str]:
    """把调用方传进来的 key 归一成 {供应商名: key}。

    裸字符串**只登记给 SocialDataX**——那是它在只有一家供应商时的历史含义，
    保持原样，老调用方的行为一个字都不会变。想开双通道就传字典：

        {"tikhub": "...", "socialdatax": "..."}

    只配一家 key 时，`usable_order()` 会自动把另一家从顺序里滤掉，
    所以 channels 里默认写着两家也不会去打一个没配 key 的通道。
    """
    if isinstance(api_key, dict):
        return {str(k).strip().lower(): str(v) for k, v in api_key.items() if v}
    key = str(api_key or "")
    return {SOCIALDATAX: key} if key else {}


def usable_order(channels: Any, platform: str, keys: dict[str, str],
                 disabled: Optional[set[str]] = None) -> list[str]:
    """这个平台此刻真正可用的供应商顺序：有 key、且本轮没被判死。

    channels 是 config.Channels；这里刻意用 Any 接住，好让 providers 不反过来
    依赖 config——打包进扣子时模块是按依赖顺序拼接的，一旦成环就拼不出来。
    """
    dead = disabled or set()
    return [n for n in channels.for_platform(platform) if keys.get(n) and n not in dead]
