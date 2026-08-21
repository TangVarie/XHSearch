"""双通道：组包、归一化、错误分类。

夹具的形状全部照抄 2026-08 的真实响应（用真 Key 打出来的），只是把无关字段
删掉了。这一点很重要——归一化层的价值就在于它认得上游的真实形状，
拿一个想当然的假 JSON 去测，等于什么都没测。
"""

import json
import unittest

from xhsearch import analyze, providers
from xhsearch.config import Channels, Settings
from xhsearch.protocol import Err, Failure, Ok

KEY = "SECRET-abcdefgh-12345678"


def wrap(inner: dict) -> str:
    """TikHub 的成功信封：顶层是它自己的元信息，data 里才是上游原文。"""
    return json.dumps({
        "code": 200,
        "request_id": "rid-1",
        "message": "Request successful. This request will incur a charge.",
        "data": inner,
    }, ensure_ascii=False)


def xhs_comments_body(*, comments, comment_count=193, l1=154,
                      user_id="5efc6e66", sorts=("default", "latest_v2", "like_count")) -> str:
    return wrap({
        "code": 0, "success": True, "msg": "",
        "data": {
            "comments": comments,
            "cursor": "{}",
            "comment_count_l1": l1,
            "comment_count": comment_count,
            "all_sort_strategies": [{"type": t} for t in sorts],
            "current_sort_strategy": "default",
            "user_id": user_id,
            "has_more": True,
        },
    })


PINNED_COMMENT = {
    "content": "恭喜中奖",
    "like_count": 12,
    "ip_location": "Shanghai",
    "show_tags": [],
    # 实测：置顶标记只出现在 show_tags_v2，type 是 user_top，text 是英文 "Pinned"。
    "show_tags_v2": [
        {"style_id": "red", "type": "user_top", "pos": "cmt_bottom", "text": "Pinned"},
        {"text": "Author", "pos": "post_name", "type": "is_author", "style_id": "red"},
    ],
    "user": {"nickname": "思念食品", "userid": "u1"},
}

PLAIN_COMMENT = {
    "content": "好用",
    "like_count": 3,
    "ip_location": "Guangdong",
    "show_tags": [],
    "show_tags_v2": [],
    "user": {"nickname": "路人", "userid": "u2"},
}


class TestTikhubBuild(unittest.TestCase):
    def test_xhs_comments_is_get_with_default_sort(self):
        req = providers.get_provider("tikhub").build(KEY, "xhs", "comments",
                                            {"note_id": "n" * 24, "sort": "default"})
        self.assertEqual(req.method, "GET")
        self.assertIn("sort_strategy=default", req.url)
        self.assertIn("note_id=" + "n" * 24, req.url)
        self.assertIn("index=0", req.url)
        self.assertEqual(req.body, "")

    def test_browser_user_agent_is_mandatory(self):
        """裸 urllib 的 UA 会被 Cloudflare 判成机器人，403 连业务层都到不了。

        这一条实测踩过，没有 UA 就是整个通道不通，所以用测试钉死。
        """
        req = providers.get_provider("tikhub").build(KEY, "xhs", "comments", {"note_id": "x"})
        self.assertIn("Mozilla/", req.headers.get("User-Agent", ""))

    def test_default_domain_is_the_one_that_works_everywhere(self):
        """默认必须是 .dev：api.tikhub.io 在大陆被墙，而 .dev 境内境外都通。

        默认值配错的后果不对称——.dev 在境外只是慢一点，.io 在境内是根本不通。
        """
        req = providers.get_provider("tikhub").build(KEY, "douyin", "comments", {"aweme_id": "7"})
        self.assertTrue(req.url.startswith("https://api.tikhub.dev/"))

    def test_domain_is_switchable_for_overseas_hosts(self):
        """对方文档要求「请勿跨区使用」，跑在 Railway 上要切到主域名。"""
        original = providers.TIKHUB_BASE
        try:
            providers.set_tikhub_base("https://api.tikhub.io/")   # 末尾斜杠也要吃掉
            req = providers.get_provider("tikhub").build(KEY, "xhs", "comments", {"note_id": "n"})
            self.assertTrue(req.url.startswith("https://api.tikhub.io/api/v1/"))
            providers.set_tikhub_base("")                          # 空值 = 不改
            self.assertEqual(providers.TIKHUB_BASE, "https://api.tikhub.io")
        finally:
            providers.set_tikhub_base(original)
        self.assertEqual(providers.TIKHUB_BASE, "https://api.tikhub.dev")

    def test_share_link_falls_back_to_share_text(self):
        req = providers.get_provider("tikhub").build(KEY, "xhs", "comments",
                                            {"url": "https://xhslink.com/abc"})
        self.assertIn("share_text=", req.url)

    def test_socialdatax_still_posts_json(self):
        req = providers.get_provider("socialdatax").build(KEY, "xhs", "comments",
                                                 {"note_id": "n", "sort": "default"})
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.body)["sort_type"], "default")


class TestTikhubXhsComments(unittest.TestCase):
    def parse(self, body, status=200):
        return providers.get_provider("tikhub").parse(
            "xhs", "comments", status, "application/json", body, "rid", KEY)

    def test_pinned_comes_from_show_tags_v2(self):
        res = self.parse(xhs_comments_body(comments=[PINNED_COMMENT, PLAIN_COMMENT]))
        self.assertIsInstance(res, Ok)
        snap = analyze.read_comment_page("xhs", res.data)
        self.assertEqual(snap.comment_count, 193)
        self.assertEqual(snap.top_level_comment_count, 154)
        self.assertIsNotNone(snap.pinned)
        self.assertEqual(snap.pinned.author_name, "思念食品")
        self.assertTrue(snap.pinned.is_author)
        self.assertFalse(snap.comments[1].is_pinned)

    def test_english_ip_is_translated(self):
        """快照是运营天天扫的一列，中英混排读着别扭。"""
        res = self.parse(xhs_comments_body(comments=[PINNED_COMMENT, PLAIN_COMMENT]))
        snap = analyze.read_comment_page("xhs", res.data)
        self.assertEqual(snap.comments[0].ip_location, "上海")
        self.assertEqual(snap.comments[1].ip_location, "广东")

    def test_unknown_location_is_kept_verbatim(self):
        comment = dict(PLAIN_COMMENT, ip_location="Narnia")
        snap = analyze.read_comment_page(
            "xhs", self.parse(xhs_comments_body(comments=[comment])).data)
        self.assertEqual(snap.comments[0].ip_location, "Narnia")

    def test_empty_shell_is_a_suspicion_not_a_verdict(self):
        """不存在的笔记返回 200 + success:true + 空壳。

        唯二的线索是 user_id 为空、all_sort_strategies 为空——这是启发式，
        不是厂商契约，所以绝不能 definitive，必须交给两击定罪。
        """
        res = self.parse(xhs_comments_body(comments=[], comment_count=0, l1=0,
                                           user_id="", sorts=()))
        self.assertIsInstance(res, Err)
        self.assertIs(res.kind, Failure.GONE)
        self.assertFalse(res.definitive)

    def test_live_note_with_zero_comments_is_not_gone(self):
        """真实存在但零评论的笔记：作者 id 还在，就不能判死。"""
        res = self.parse(xhs_comments_body(comments=[], comment_count=0, l1=0))
        self.assertIsInstance(res, Ok)
        self.assertEqual(res.data["comment_count"], 0)


class TestTikhubXhsDetail(unittest.TestCase):
    def parse(self, body):
        return providers.get_provider("tikhub").parse(
            "xhs", "detail", 200, "application/json", body, "rid", KEY)

    def test_counters(self):
        res = self.parse(wrap({"code": 0, "success": True, "data": [{"note_list": [{
            "liked_count": 8877, "collected_count": 4258, "shared_count": 2837,
            "comments_count": 193, "in_censor": False, "type": "video",
        }]}]}))
        self.assertIsInstance(res, Ok)
        snap = analyze.Snapshot(platform="xhs")
        analyze.merge_detail(snap, res.data)
        self.assertEqual(snap.like_count, 8877)
        self.assertEqual(snap.collect_count, 4258)
        self.assertEqual(snap.comment_count, 193)
        self.assertIs(snap.censored, False)

    def test_empty_data_list_is_a_definitive_death(self):
        """实测：笔记不存在时 detail 的 data 直接是 []。这个信号是干净的。"""
        res = self.parse(wrap({"code": 0, "success": True, "data": []}))
        self.assertIsInstance(res, Err)
        self.assertIs(res.kind, Failure.GONE)
        self.assertTrue(res.definitive)


class TestTikhubDouyin(unittest.TestCase):
    def parse(self, purpose, body):
        return providers.get_provider("tikhub").parse(
            "douyin", purpose, 200, "application/json", body, "rid", KEY)

    def test_comments_total_is_a_real_int(self):
        """SocialDataX 那边 comment_count 是 integer|null，这边实测是实打实的整数。"""
        res = self.parse("comments", wrap({
            "status_code": 0, "total": 57, "has_more": 1,
            "comments": [{"text": "第一", "digg_count": 9, "ip_label": "Sichuan",
                          "user": {"nickname": "阿三"}}],
        }))
        snap = analyze.read_comment_page("douyin", res.data)
        self.assertEqual(snap.comment_count, 57)
        self.assertEqual(snap.comments[0].ip_location, "四川")

    def test_douyin_never_claims_pinned(self):
        """抖音一律不判置顶——label_type 的语义没验过，错判比不判伤害大。"""
        res = self.parse("comments", wrap({
            "total": 3,
            "comments": [{"text": "x", "label_type": 3, "label_text": "置顶",
                          "user": {"nickname": "谁"}}],
        }))
        snap = analyze.read_comment_page("douyin", res.data)
        self.assertFalse(snap.comments[0].is_pinned)
        self.assertFalse(snap.supports_pinned)

    def test_filter_list_is_a_definitive_death(self):
        res = self.parse("detail", wrap({
            "aweme_detail": None,
            "filter_list": [{"aweme_id": "744811", "reason": 8}],
            "status_code": 0,
        }))
        self.assertIsInstance(res, Err)
        self.assertIs(res.kind, Failure.GONE)
        self.assertTrue(res.definitive)
        self.assertIn("reason=8", res.message)

    def test_detail_counters_and_censor_flag(self):
        res = self.parse("detail", wrap({"aweme_detail": {
            "statistics": {"digg_count": 4557, "collect_count": 2549,
                           "share_count": 345, "comment_count": 57},
            "status": {"is_prohibited": True, "in_reviewing": False},
        }}))
        snap = analyze.Snapshot(platform="douyin")
        analyze.merge_detail(snap, res.data)
        self.assertEqual(snap.like_count, 4557)
        self.assertIs(snap.censored, True)


class TestTikhubErrors(unittest.TestCase):
    def parse(self, status, body):
        return providers.get_provider("tikhub").parse(
            "xhs", "comments", status, "application/json", body, "", KEY)

    def test_api_key_is_redacted(self):
        """TikHub 会把你提交的 Key 原样回显。这段文案会被写进飞书诊断列。"""
        body = json.dumps({"detail": {
            "code": 401,
            "message_zh": f"无效的API令牌，您提交的API令牌为 {KEY}。",
            "request_id": "rid-9",
        }}, ensure_ascii=False)
        res = self.parse(401, body)
        self.assertIsInstance(res, Err)
        self.assertIs(res.kind, Failure.AUTH)
        self.assertNotIn(KEY, res.message)
        self.assertNotIn(KEY, res.operator_text())
        self.assertIn("***", res.message)

    def test_quota_is_recognised(self):
        body = json.dumps({"detail": {"code": 403, "message_zh": "账户余额不足，请充值。"}},
                          ensure_ascii=False)
        self.assertIs(self.parse(403, body).kind, Failure.QUOTA)

    def test_rate_limit(self):
        body = json.dumps({"detail": {"code": 429, "message": "Too Many Requests"}})
        self.assertIs(self.parse(429, body).kind, Failure.RATE_LIMIT)

    def test_cloudflare_block_is_transport_not_auth(self):
        """Cloudflare 的 1010 是拦 UA/出口 IP，不是 Key 的问题。

        它返回 403，而 403 默认归 AUTH——归错了这个通道会被整轮标死，
        可它换个网络就好了。必须靠 error_code 这个特征把它挑出来。
        """
        body = json.dumps({"error_code": 1010, "title": "Error 1010: Access denied",
                           "error_name": "browser_signature_banned",
                           "detail": "The site owner has blocked access", "status": 403})
        res = self.parse(403, body)
        self.assertIsInstance(res, Err)
        self.assertIs(res.kind, Failure.TRANSPORT)
        self.assertIn("Cloudflare", res.message)
        self.assertIn("不是 API Key 的问题", res.message)

    def test_garbage_body(self):
        res = providers.get_provider("tikhub").parse(
            "xhs", "comments", 502, "text/html", "<html>bad gateway</html>", "", KEY)
        self.assertIs(res.kind, Failure.TRANSPORT)


class TestChannels(unittest.TestCase):
    def test_default_prefers_tikhub_but_keeps_a_spare(self):
        ch = Channels()
        self.assertEqual(ch.primary("xhs"), "tikhub")
        self.assertEqual(ch.for_platform("douyin"), ["tikhub", "socialdatax"])

    def test_unknown_platform_falls_back(self):
        self.assertEqual(Channels().for_platform("weibo"), ["socialdatax"])

    def test_bare_string_key_stays_socialdatax_only(self):
        """老调用方传裸字符串，行为必须和改造前一模一样：只打 SocialDataX。"""
        keys = providers.credentials("sdx-key")
        self.assertEqual(keys, {"socialdatax": "sdx-key"})
        self.assertEqual(
            providers.usable_order(Channels(), "xhs", keys), ["socialdatax"])

    def test_dict_key_opens_both_channels(self):
        keys = providers.credentials({"TikHub": "t", "socialdatax": "s"})
        self.assertEqual(
            providers.usable_order(Channels(), "xhs", keys), ["tikhub", "socialdatax"])

    def test_disabled_channel_is_skipped(self):
        keys = {"tikhub": "t", "socialdatax": "s"}
        self.assertEqual(
            providers.usable_order(Channels(), "xhs", keys, disabled={"tikhub"}),
            ["socialdatax"])

    def test_empty_key_is_not_a_channel(self):
        keys = providers.credentials({"tikhub": "", "socialdatax": "s"})
        self.assertEqual(providers.usable_order(Channels(), "xhs", keys), ["socialdatax"])


class TestPricing(unittest.TestCase):
    def test_douyin_is_ten_times_cheaper_on_tikhub(self):
        p = providers.get_provider("tikhub")
        self.assertAlmostEqual(p.yuan_per_call("douyin", "comments") * 10,
                               p.yuan_per_call("xhs", "comments"), places=6)

    def test_socialdatax_is_flat(self):
        p = providers.get_provider("socialdatax")
        self.assertEqual(p.yuan_per_call("xhs", "comments"),
                         p.yuan_per_call("douyin", "detail"))

    def test_estimate_uses_the_channel_actually_configured(self):
        from datetime import datetime, timedelta, timezone

        from xhsearch.rows import Row, estimate_yuan

        now = datetime(2026, 8, 21, tzinfo=timezone.utc)
        row = Row(record_id="r1",
                  link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
                  publish_time_ms=int((now - timedelta(days=1)).timestamp() * 1000))

        cheap = Settings()
        cheap.channels = Channels(order={"xhs": ["tikhub"]})
        pricey = Settings()
        pricey.channels = Channels(order={"xhs": ["socialdatax"]})

        self.assertLess(estimate_yuan([row], cheap, now), estimate_yuan([row], pricey, now))

    def test_only_tikhub_admits_to_billing_failed_lookups(self):
        """TikHub 文档明写「传错 ID 照样计费」；SocialDataX 那边还没验过。

        一刀切当作都收费会让账单虚高，而天天虚报成本的监控没人会信。
        """
        self.assertTrue(providers.get_provider("tikhub").bills_failed_lookups)
        self.assertFalse(providers.get_provider("socialdatax").bills_failed_lookups)

    def test_unknown_provider_raises_with_a_helpful_message(self):
        with self.assertRaises(ValueError) as ctx:
            providers.get_provider("nosuchvendor")
        self.assertIn("tikhub", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
