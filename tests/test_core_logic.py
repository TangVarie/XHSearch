"""核心逻辑的单测。全部离线，不需要 API Key，不发任何请求。

跑法：python3 -m unittest discover -s tests -v
"""

import json
import unittest
from datetime import datetime, timedelta, timezone

from xhsearch import analyze, links, protocol, rows, tags
from xhsearch.config import Settings

UTC = timezone.utc


class TestLinkParsing(unittest.TestCase):
    def test_xhs_explore_url(self):
        parsed = links.parse("https://www.xiaohongshu.com/explore/65a1b2c3d4e5f60718293a4b?xsec_token=ABC")
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, "65a1b2c3d4e5f60718293a4b")

    def test_xhs_discovery_url(self):
        parsed = links.parse("https://www.xiaohongshu.com/discovery/item/65a1b2c3d4e5f60718293a4b")
        self.assertEqual(parsed.content_id, "65a1b2c3d4e5f60718293a4b")

    def test_xhs_share_text_shortlink(self):
        cell = "12 复制本条信息，打开【小红书】App查看精彩内容！ http://xhslink.com/a/AbC123"
        parsed = links.parse(cell)
        self.assertEqual(parsed.platform, "xhs")
        self.assertIsNone(parsed.content_id)          # 短链拿不到 ID，透传给 by_url
        self.assertEqual(parsed.url, "http://xhslink.com/a/AbC123")
        self.assertTrue(parsed.usable)

    def test_xhs_bare_note_id(self):
        parsed = links.parse("  65A1B2C3D4E5F60718293A4B  ")
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, "65a1b2c3d4e5f60718293a4b")

    def test_douyin_video_url(self):
        parsed = links.parse("https://www.douyin.com/video/7123456789012345678")
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.content_id, "7123456789012345678")

    def test_douyin_modal_id(self):
        parsed = links.parse("https://www.douyin.com/discover?modal_id=7123456789012345678")
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.content_id, "7123456789012345678")

    def test_douyin_shortlink_share_text(self):
        cell = "7.86 复制打开抖音，看看【某某】的作品 https://v.douyin.com/iRxYzAb/"
        parsed = links.parse(cell)
        self.assertEqual(parsed.platform, "douyin")
        self.assertEqual(parsed.url, "https://v.douyin.com/iRxYzAb/")

    def test_trailing_chinese_punctuation_stripped(self):
        parsed = links.parse("看这条：https://www.douyin.com/video/7123456789012345678，很火")
        self.assertEqual(parsed.content_id, "7123456789012345678")

    def test_ambiguous_cell_refuses_to_guess(self):
        cell = "https://xhslink.com/a/AAA 和 https://v.douyin.com/BBB"
        parsed = links.parse(cell)
        self.assertIsNone(parsed.platform)
        self.assertFalse(parsed.usable)

    def test_empty_and_garbage(self):
        for cell in ["", "   ", "待补链接", "13800138000"]:
            self.assertFalse(links.parse(cell).usable, cell)

    def test_bare_numeric_needs_douyin_keyword(self):
        self.assertFalse(links.parse("7123456789012345678").usable)
        self.assertTrue(links.parse("抖音 7123456789012345678").usable)


class TestTagMerge(unittest.TestCase):
    NS = ["爆文", "风控", "置顶成功", "已失效"]

    def test_human_tags_survive(self):
        result = tags.merge(["已复盘", "客户确认", "爆文"], {"风控"}, self.NS)
        self.assertIn("已复盘", result.final)
        self.assertIn("客户确认", result.final)
        self.assertIn("风控", result.final)
        self.assertNotIn("爆文", result.final)   # 机器标签可以撤回
        self.assertEqual(result.removed, ["爆文"])
        self.assertEqual(result.added, ["风控"])

    def test_idempotent(self):
        first = tags.merge(["爆文"], {"爆文"}, self.NS)
        self.assertEqual(first.final, ["爆文"])
        self.assertFalse(first.changed)

    def test_unknown_option_is_dropped_not_written(self):
        result = tags.merge([], {"爆文"}, self.NS, known_options=["风控", "已失效"])
        self.assertEqual(result.final, [])
        self.assertEqual(result.dropped_unknown, ["爆文"])

    def test_computed_tag_outside_namespace_raises(self):
        with self.assertRaises(ValueError):
            tags.merge([], {"随便编的"}, self.NS)

    def test_none_current(self):
        self.assertEqual(tags.merge(None, {"风控"}, self.NS).final, ["风控"])


class TestCommentAnalysis(unittest.TestCase):
    def _xhs_page(self, count=42, with_pinned=True):
        items = []
        if with_pinned:
            items.append({
                "content": "戳这里领优惠券 www.example.com",
                "like_count": 88,
                "is_pinned": True,
                "is_author_comment": True,
                "ip_location": "上海",
                "author": {"name": "品牌官号"},
            })
        items += [
            {"content": "求链接！", "like_count": 12, "is_pinned": False,
             "is_author_comment": False, "ip_location": "广东", "author": {"name": "用户A"}},
            {"content": "踩雷了，不推荐", "like_count": 30, "is_pinned": False,
             "is_author_comment": False, "ip_location": "北京", "author": {"name": "用户B"}},
        ]
        return {"items": items, "comment_count": count, "top_level_comment_count": count,
                "next_page_token": "", "points": {"cost": 10, "balance": 5000}}

    def test_xhs_one_call_gives_count_pinned_and_top(self):
        snap = analyze.read_comment_page("xhs", self._xhs_page())
        self.assertEqual(snap.comment_count, 42)
        self.assertIsNotNone(snap.pinned)
        self.assertEqual(len(snap.comments), 3)
        self.assertEqual(snap.points_balance, 5000)

    def test_pinned_sorted_to_front_of_digest(self):
        page = self._xhs_page()
        page["items"] = list(reversed(page["items"]))   # 置顶排在最后
        snap = analyze.read_comment_page("xhs", page)
        digest = analyze.format_digest(snap, Settings().digest)
        self.assertTrue(digest.splitlines()[0].startswith("1. [置顶"))

    def test_douyin_pinned_is_explicitly_unsupported(self):
        page = {"items": [{"content": "哈哈哈", "like_count": 5, "is_hot": True,
                           "ip_location": "浙江", "author": {"nickname": "路人"}}],
                "comment_count": None, "points": {"cost": 10, "balance": 4990}}
        snap = analyze.read_comment_page("douyin", page)
        self.assertFalse(snap.supports_pinned)
        self.assertEqual(analyze.format_pinned(snap), analyze.DOUYIN_PINNED_UNSUPPORTED)
        self.assertIsNone(snap.comment_count)          # 必须由 detail 兜底

    def test_douyin_detail_backfills_null_comment_count(self):
        snap = analyze.read_comment_page("douyin", {"items": [], "comment_count": None})
        analyze.merge_detail(snap, {"like_count": 900, "comment_count": 77, "collect_count": 12})
        self.assertEqual(snap.comment_count, 77)
        self.assertEqual(snap.like_count, 900)

    def test_detail_does_not_clobber_known_comment_count(self):
        snap = analyze.read_comment_page("xhs", self._xhs_page(count=42))
        analyze.merge_detail(snap, {"comment_count": 999, "like_count": 1})
        self.assertEqual(snap.comment_count, 42)   # 评论接口的数更贴近评论区实况

    def test_digest_respects_char_budget(self):
        page = self._xhs_page()
        page["items"] = [{"content": "很长的评论" * 200, "like_count": 1, "is_pinned": False,
                          "is_author_comment": False, "ip_location": "", "author": {"name": "X"}}] * 30
        snap = analyze.read_comment_page("xhs", page)
        digest = analyze.format_digest(snap, Settings().digest)
        self.assertLessEqual(len(digest), Settings().digest.total_chars + 40)

    def test_empty_comments_is_not_an_error(self):
        snap = analyze.read_comment_page("xhs", {"items": [], "comment_count": 0})
        self.assertEqual(analyze.format_digest(snap, Settings().digest), "（暂无评论）")
        self.assertEqual(analyze.format_pinned(snap), "")


class TestPinnedState(unittest.TestCase):
    """置顶判定。品牌方最该立刻知道的不是「我们的置顶在不在」，
    而是「置顶位有没有被别人占了」——只判前者会完全错过后一幕。"""

    def setUp(self):
        self.settings = Settings()
        self.ps = self.settings.pinned_states

    def _snap(self, comments, platform="xhs"):
        items = [
            {"content": text, "is_pinned": pinned, "like_count": 0,
             "is_author_comment": pinned, "ip_location": "", "author": {"name": "某人"}}
            for text, pinned in comments
        ]
        return analyze.read_comment_page(platform, {"items": items, "comment_count": len(items)})

    def state(self, comments, expected, platform="xhs"):
        return analyze.decide_pinned_state(self._snap(comments, platform), expected, self.settings)[0]

    def test_our_comment_is_pinned(self):
        self.assertEqual(
            self.state([("戳主页领 30 元优惠券～", True)], "戳主页领30元优惠券"),
            self.ps.success)

    def test_emoji_and_whitespace_tolerated(self):
        self.assertEqual(
            self.state([("戳 主页  领 30 元优惠券 🎁✨", True)], "戳主页领30元优惠券"),
            self.ps.success)

    def test_pin_taken_over_by_someone_else(self):
        state = self.state(
            [("楼主是不是恰饭了", True), ("戳主页领30元优惠券", False)],
            "戳主页领30元优惠券")
        self.assertEqual(state, self.ps.replaced)

    def test_pin_dropped_but_our_comment_still_there(self):
        state = self.state(
            [("好用", False), ("戳主页领30元优惠券", False)],
            "戳主页领30元优惠券")
        self.assertEqual(state, self.ps.lost)

    def test_our_comment_gone_entirely(self):
        self.assertEqual(self.state([("路过", False)], "戳主页领30元优惠券"), self.ps.seed_missing)

    def test_no_seed_configured_but_pin_exists(self):
        self.assertEqual(self.state([("随便什么", True)], ""), self.ps.no_seed)

    def test_no_seed_and_no_pin(self):
        self.assertEqual(self.state([("路过", False)], ""), self.ps.none_pinned)

    def test_douyin_always_unsupported(self):
        self.assertEqual(
            self.state([("哈哈", False)], "戳主页领30元优惠券", platform="douyin"),
            self.ps.douyin_unsupported)

    def test_too_short_seed_refuses_to_match(self):
        # 「券」这种一两个字的关键词会命中一大半评论，宁可判不出也不认错人
        self.assertEqual(self.state([("求券", True)], "券"), self.ps.replaced)

    def test_position_written_into_note_not_into_option_value(self):
        _, note = analyze.decide_pinned_state(
            self._snap([("别人的置顶", True), ("x", False), ("戳主页领30元优惠券", False)]),
            "戳主页领30元优惠券", self.settings)
        self.assertIn("第 3 条", note)

    def test_every_state_is_in_the_frozen_enum(self):
        """单选值绝不能含变量——否则飞书会静默新建选项，几周后长出几十个。"""
        allowed = set(self.ps.all())
        cases = [
            ([("a", True)], "a"), ([("a", True)], ""), ([("a", False)], "zzz"),
            ([], "x"), ([], ""), ([("a", True)], "bbbbbbbbbb"),
        ]
        for comments, expected in cases:
            for platform in ("xhs", "douyin"):
                self.assertIn(self.state(comments, expected, platform), allowed)


class TestTagDecision(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def _snap(self, count, pinned=False, platform="xhs"):
        items = []
        if pinned:
            items.append({"content": "官方置顶文案在此", "is_pinned": True, "is_author_comment": True,
                          "like_count": 0, "ip_location": "", "author": {"name": "官号"}})
        return analyze.read_comment_page(platform, {"items": items, "comment_count": count})

    def decide(self, snap, **kw):
        kw.setdefault("previous_comment_count", None)
        kw.setdefault("age_hours", 10)
        return analyze.decide(snap, self.settings, **kw)

    def test_hot_threshold_is_platform_specific(self):
        # 小红书 50 / 抖音 200：同样 100 条评论，一个爆一个不爆
        self.assertIn("爆文", self.decide(self._snap(100, platform="xhs")).tags)
        self.assertNotIn("爆文", self.decide(self._snap(100, platform="douyin")).tags)

    def test_below_threshold_not_hot(self):
        self.assertNotIn("爆文", self.decide(self._snap(49)).tags)

    def test_sudden_spike_counts_as_hot_even_below_threshold(self):
        v = self.decide(self._snap(45), previous_comment_count=20)
        self.assertIn("爆文", v.tags)

    def test_comment_halving_flags_risk(self):
        self.assertIn("风控", self.decide(self._snap(20), previous_comment_count=100).tags)

    def test_mild_drop_only_warns(self):
        v = self.decide(self._snap(80), previous_comment_count=100)
        self.assertIn("预警", v.tags)
        self.assertNotIn("风控", v.tags)

    def test_small_baseline_drop_is_noise(self):
        v = self.decide(self._snap(2), previous_comment_count=8)
        self.assertNotIn("风控", v.tags)
        self.assertNotIn("预警", v.tags)

    def test_zero_comments_after_window_flags_risk(self):
        self.assertIn("风控", self.decide(self._snap(0), age_hours=72).tags)

    def test_zero_comments_during_cold_start_is_fine(self):
        self.assertNotIn("风控", self.decide(self._snap(0), age_hours=3).tags)

    def test_pinned_ok_tag(self):
        v = self.decide(self._snap(10, pinned=True), expected_pinned="官方置顶文案在此")
        self.assertIn("置顶成功", v.tags)

    def test_lost_pin_warns(self):
        v = self.decide(self._snap(10), expected_pinned="官方置顶文案在此")
        self.assertIn("预警", v.tags)
        self.assertNotIn("置顶成功", v.tags)

    def test_previously_pinned_now_lost_is_called_out(self):
        v = self.decide(self._snap(10), expected_pinned="官方置顶文案在此",
                        previous_pinned_state=self.settings.pinned_states.success)
        self.assertTrue(any("此前是成功状态" in n for n in v.notes))

    def test_gone_tags_both_gone_and_risk(self):
        self.assertEqual(analyze.gone_verdict(self.settings).tags, {"已失效", "风控"})

    def test_first_strike_tags_nothing(self):
        """一次抖动就把好帖子标成风控 = 运营全线停投。绝不能发生。"""
        v = analyze.suspect_verdict(self.settings, 1, "取不到")
        self.assertEqual(v.tags, set())

    def test_all_decided_tags_stay_inside_namespace(self):
        ns = set(self.settings.tags.namespace())
        for platform in ("xhs", "douyin"):
            for count in (0, 5, 49, 50, 5000):
                for prev in (None, 0, 10, 500):
                    for age in (1, 50, 500):
                        v = self.decide(self._snap(count, pinned=True, platform=platform),
                                        previous_comment_count=prev, age_hours=age,
                                        expected_pinned="官方置顶文案在此")
                        self.assertTrue(v.tags <= ns, f"{v.tags} 越界")


class TestCallPlanning(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _row(self, cell, age_days=1.0):
        published = self.now - timedelta(days=age_days)
        return rows.Row(record_id="rec1", link_cell=cell,
                        publish_time_ms=int(published.timestamp() * 1000))

    def test_fresh_xhs_note_costs_two_calls(self):
        plan = rows.plan_calls(self._row("https://www.xiaohongshu.com/explore/" + "a" * 24), self.settings, self.now)
        self.assertEqual([c.purpose for c in plan], ["comments", "detail"])
        self.assertEqual(plan[0].arguments["sort_type"], "default")

    def test_old_xhs_note_costs_one_call(self):
        plan = rows.plan_calls(
            self._row("https://www.xiaohongshu.com/explore/" + "a" * 24, age_days=30), self.settings, self.now)
        self.assertEqual([c.purpose for c in plan], ["comments"])

    def test_detail_can_be_switched_off_entirely(self):
        self.settings.detail_within_days = 0
        plan = rows.plan_calls(self._row("https://www.xiaohongshu.com/explore/" + "a" * 24), self.settings, self.now)
        self.assertEqual(len(plan), 1)

    def test_douyin_always_costs_two_calls(self):
        for age in (1, 30, 365):
            plan = rows.plan_calls(
                self._row("https://www.douyin.com/video/7123456789012345678", age_days=age), self.settings, self.now)
            self.assertEqual([c.purpose for c in plan], ["comments", "detail"], f"age={age}")

    def test_unusable_link_costs_nothing(self):
        self.assertEqual(rows.plan_calls(self._row("待补"), self.settings, self.now), [])

    def test_credit_estimate(self):
        batch = [self._row("https://www.xiaohongshu.com/explore/" + "a" * 24, age_days=30)] * 10
        self.assertEqual(rows.estimate_credits(batch, self.settings, self.now), 100)  # 10 篇 × 1 次 × 10 积分


class TestRefreshTiering(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def _row(self, age_days, updated_hours_ago):
        published = self.now - timedelta(days=age_days)
        updated = self.now - timedelta(hours=updated_hours_ago)
        return rows.Row(record_id="r", link_cell="x",
                        publish_time_ms=int(published.timestamp() * 1000),
                        last_updated_ms=int(updated.timestamp() * 1000))

    def test_fresh_post_due_after_8h(self):
        self.assertTrue(self._row(1, 9).is_due(self.settings, self.now))
        self.assertFalse(self._row(1, 3).is_due(self.settings, self.now))

    def test_week_old_post_due_daily(self):
        self.assertTrue(self._row(5, 25).is_due(self.settings, self.now))
        self.assertFalse(self._row(5, 9).is_due(self.settings, self.now))

    def test_month_old_post_due_every_three_days(self):
        self.assertTrue(self._row(20, 80).is_due(self.settings, self.now))
        self.assertFalse(self._row(20, 25).is_due(self.settings, self.now))

    def test_archived_post_never_due(self):
        self.assertFalse(self._row(45, 9999).is_due(self.settings, self.now))

    def test_never_updated_is_due(self):
        row = rows.Row(record_id="r", link_cell="x",
                       publish_time_ms=int((self.now - timedelta(days=3)).timestamp() * 1000))
        self.assertTrue(row.is_due(self.settings, self.now))

    def test_unknown_publish_time_is_due(self):
        self.assertTrue(rows.Row(record_id="r", link_cell="x").is_due(self.settings, self.now))


class TestProtocol(unittest.TestCase):
    def test_missing_api_key_is_fatal_auth(self):
        body = json.dumps({"error": "missing_api_key", "error_description": "未配置 API Key。"})
        result = protocol.parse_response(401, "application/json", body)
        self.assertIsInstance(result, protocol.Err)
        self.assertEqual(result.kind, protocol.Failure.AUTH)
        self.assertIn(result.kind, protocol.FATAL)

    def test_invalid_api_key(self):
        body = json.dumps({"error": "invalid_api_key", "error_description": "API Key 无效或已失效。"})
        self.assertEqual(protocol.parse_response(401, "application/json", body).kind, protocol.Failure.AUTH)

    def test_406_when_accept_header_wrong(self):
        body = json.dumps({"jsonrpc": "2.0", "id": "server-error",
                           "error": {"code": -32600, "message": "Not Acceptable: Client must accept both"}})
        result = protocol.parse_response(406, "application/json", body)
        self.assertIsInstance(result, protocol.Err)

    def test_sse_structured_content(self):
        payload = {"jsonrpc": "2.0", "id": 1,
                   "result": {"structuredContent": {"comment_count": 42, "items": [],
                                                    "points": {"cost": 10, "balance": 990}}}}
        body = f"event: message\ndata: {json.dumps(payload)}\n\n"
        result = protocol.parse_response(200, "text/event-stream", body)
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data["comment_count"], 42)
        self.assertEqual(result.points_balance, 990)

    def test_sse_content_text_fallback(self):
        inner = json.dumps({"comment_count": 7, "items": []})
        payload = {"jsonrpc": "2.0", "id": 1,
                   "result": {"content": [{"type": "text", "text": inner}]}}
        body = f"event: message\ndata: {json.dumps(payload)}\n\n"
        result = protocol.parse_response(200, "text/event-stream", body)
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data["comment_count"], 7)

    def test_multiline_sse_frame(self):
        # 按 SSE 规范，一帧的多行 data: 要用换行拼回去再解析。
        # JSON 里的换行在结构位置上是合法空白，所以拼接后仍应解析成功。
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ok": True}}}
        raw = json.dumps(payload)
        split_at = raw.index('"result"')
        body = f"event: message\ndata: {raw[:split_at]}\ndata: {raw[split_at:]}\n\n"
        result = protocol.parse_response(200, "text/event-stream", body)
        self.assertIsInstance(result, protocol.Ok)
        self.assertEqual(result.data, {"ok": True})

    def test_ignores_non_data_sse_lines(self):
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ok": True}}}
        body = (
            ": this is an SSE comment\n"
            "event: message\n"
            "id: 42\n"
            "retry: 1000\n"
            f"data: {json.dumps(payload)}\n"
            "\n"
        )
        self.assertIsInstance(protocol.parse_response(200, "text/event-stream", body), protocol.Ok)

    def test_rate_limit_is_retryable_not_fatal(self):
        body = json.dumps({"error": "rate_limited", "error_description": "请求过于频繁", "retry_after": 3})
        result = protocol.parse_response(429, "application/json", body)
        self.assertEqual(result.kind, protocol.Failure.RATE_LIMIT)
        self.assertIn(result.kind, protocol.RETRYABLE)
        self.assertNotIn(result.kind, protocol.FATAL)
        self.assertEqual(result.retry_after_seconds, 3.0)

    def test_quota_is_fatal(self):
        body = json.dumps({"error": "insufficient_balance", "error_description": "积分不足"})
        self.assertIn(protocol.parse_response(402, "application/json", body).kind, protocol.FATAL)

    def test_deleted_note_classified_as_gone(self):
        body = json.dumps({"error": "not_found", "error_description": "笔记不存在或已删除"})
        self.assertEqual(protocol.parse_response(404, "application/json", body).kind, protocol.Failure.GONE)

    def test_unknown_chinese_message_falls_back_to_hint_matching(self):
        body = json.dumps({"error": "weird_code", "error_description": "该内容因违规已下架"})
        self.assertEqual(protocol.parse_response(200, "application/json", body).kind, protocol.Failure.GONE)

    def test_server_error_is_transport_retryable(self):
        result = protocol.parse_response(503, "text/html", "<html>bad gateway</html>")
        self.assertEqual(result.kind, protocol.Failure.TRANSPORT)

    def test_tool_is_error_flag(self):
        payload = {"jsonrpc": "2.0", "id": 1,
                   "result": {"isError": True,
                              "content": [{"type": "text", "text": "笔记不存在"}]}}
        body = f"event: message\ndata: {json.dumps(payload)}\n\n"
        self.assertEqual(protocol.parse_response(200, "text/event-stream", body).kind, protocol.Failure.GONE)

    def test_accept_header_includes_both_types(self):
        # 少任何一个服务端都会 406，这是实测过的
        accept = protocol.headers("k")["Accept"]
        self.assertIn("application/json", accept)
        self.assertIn("text/event-stream", accept)

    def test_build_call_shape(self):
        body = json.loads(protocol.build_call("xhs_get_note_comments_by_note_id", {"note_id": "a" * 24}))
        self.assertEqual(body["method"], "tools/call")
        self.assertEqual(body["params"]["name"], "xhs_get_note_comments_by_note_id")


if __name__ == "__main__":
    unittest.main()
