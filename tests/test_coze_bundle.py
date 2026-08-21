"""扣子打包产物的冒烟测试。

打包脚本要剥掉包内相对 import 和重复的 import，很容易多剥一个——语法照样通过，
但一跑就 NameError。而扣子那边没有本地调试，报错只能在网页运行记录里看，
排查成本极高。所以这里把整包 exec 起来，真的调几个函数。
"""

import ast
import sys
import types
import unittest

from tools.build_coze_node import build


class _FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


class _FakeRequestsAsync(types.ModuleType):
    """扣子内置 requests_async 的占位实现，本地没有这个包。"""

    async def post(self, *args, **kwargs):
        return _FakeResponse()

    async def get(self, *args, **kwargs):
        return _FakeResponse()


class TestCozeBundle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = build()
        sys.modules.setdefault("requests_async", _FakeRequestsAsync("requests_async"))
        cls.namespace: dict = {}
        exec(compile(cls.source, "coze_node.py", "exec"), cls.namespace)

    def test_parses(self):
        ast.parse(self.source)

    def test_no_relative_imports_survive(self):
        for line in self.source.splitlines():
            self.assertFalse(line.strip().startswith("from ."), f"漏剥了相对 import：{line}")

    def test_core_symbols_present(self):
        for name in [
            "Settings", "Row", "parse", "plan_calls", "read_comment_page", "merge_detail",
            "decide", "decide_pin", "Pin", "gone_verdict", "suspect_verdict",
            "merge", "format_digest", "format_pinned", "parse_response", "build_body",
            "headers", "endpoint", "Failure", "FATAL", "Err", "Ok", "main",
            # 双通道：这几个漏剥一个，扣子那边会直接 NameError，
            # 而且只能在网页运行记录里看到，排查成本极高。
            "get_provider", "usable_order", "FAILOVER_KINDS", "Channels",
            "channel_call", "TIKHUB_PATHS", "REGISTRY",
        ]:
            self.assertIn(name, self.namespace, f"打包产物里缺 {name}")

    def test_both_channels_are_wired_inside_bundle(self):
        """打包产物里两家都要能组包——这一条是 providers 层被剥坏的第一现场。"""
        ns = self.namespace
        tik = ns["get_provider"]("tikhub").build(
            "k", "xhs", "comments", {"note_id": "n" * 24, "sort": "default"})
        self.assertEqual(tik.method, "GET")
        self.assertIn("api.tikhub.dev", tik.url)
        self.assertIn("Mozilla/", tik.headers["User-Agent"])

        sdx = ns["get_provider"]("socialdatax").build(
            "k", "xhs", "comments", {"note_id": "n" * 24, "sort": "default"})
        self.assertEqual(sdx.method, "POST")

    def test_tikhub_normalisation_works_inside_bundle(self):
        """置顶标记藏在 show_tags_v2 里。归一化被剥坏的话这里立刻炸。"""
        import json as _json

        ns = self.namespace
        body = _json.dumps({"code": 200, "data": {"code": 0, "success": True, "data": {
            "comments": [{"content": "戳主页", "like_count": 9, "ip_location": "Shanghai",
                          "show_tags_v2": [{"type": "user_top"}],
                          "user": {"nickname": "官号"}}],
            "comment_count": 88, "comment_count_l1": 80,
            "all_sort_strategies": [{"type": "default"}], "user_id": "u1",
        }}}, ensure_ascii=False)
        res = ns["get_provider"]("tikhub").parse(
            "xhs", "comments", 200, "application/json", body, "rid", "k")
        snapshot = ns["read_comment_page"]("xhs", res.data)
        self.assertEqual(snapshot.comment_count, 88)
        self.assertIsNotNone(snapshot.pinned)
        self.assertEqual(snapshot.comments[0].ip_location, "上海")

    def test_failover_never_fires_on_gone(self):
        ns = self.namespace
        self.assertNotIn(ns["Failure"].GONE, ns["FAILOVER_KINDS"])

    def test_link_parsing_works_inside_bundle(self):
        parsed = self.namespace["parse"]("https://www.xiaohongshu.com/explore/" + "b" * 24)
        self.assertEqual(parsed.platform, "xhs")
        self.assertEqual(parsed.content_id, "b" * 24)

    def test_full_decision_path_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        snapshot = ns["read_comment_page"]("xhs", {
            "items": [{"content": "戳主页领券", "is_pinned": True, "is_author_comment": True,
                       "like_count": 9, "ip_location": "上海", "author": {"name": "官号"}}],
            "comment_count": 88,
            "points": {"cost": 10, "balance": 500},
        })
        verdict = ns["decide"](snapshot, settings, previous_comment_count=10,
                               age_hours=20, expected_pinned="戳主页领券")
        self.assertIn("爆贴", verdict.tags)          # 88 条落在 50–99 档
        self.assertNotIn("大爆", verdict.tags)       # 档位互斥
        self.assertIs(verdict.pin, ns["Pin"].SUCCESS)
        self.assertIn("置顶", ns["format_digest"](snapshot, settings.digest))

    def test_heat_tiers_match_the_agreed_thresholds(self):
        """≥20 评估中，≥50 爆贴，≥100 大爆。改动阈值会静默改变全表结论，
        所以在打包产物里也钉一遍。"""
        ns = self.namespace
        settings = ns["Settings"]()
        for count, expected in [(19, None), (20, "评估中"), (50, "爆贴"), (100, "大爆")]:
            snap = ns["read_comment_page"]("xhs", {"items": [], "comment_count": count})
            verdict = ns["decide"](snap, settings, previous_comment_count=None, age_hours=10)
            heat = {t for t in verdict.tags if settings.tags.rank(t) >= 0}
            self.assertEqual(heat, {expected} if expected else set(), f"评论数 {count}")

    def test_tag_merge_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        merged = ns["merge"](["已复盘", "风控中"], {"爆贴"}, settings.tags.namespace())
        self.assertIn("已复盘", merged.final)
        self.assertIn("爆贴", merged.final)
        self.assertNotIn("风控中", merged.final)

    def test_protocol_parsing_works_inside_bundle(self):
        ns = self.namespace
        result = ns["parse_response"](
            401, "application/json", '{"code":1401,"message":"API Key 无效或已失效。"}')
        self.assertIs(result.kind, ns["Failure"].AUTH)
        self.assertIn(result.kind, ns["FATAL"])

    def test_http_200_business_error_is_caught_inside_bundle(self):
        """业务错误走 HTTP 200 + code。这条在扣子里错了，
        每一个「笔记已删除」都会被当成成功。"""
        ns = self.namespace
        result = ns["parse_response"](200, "application/json",
                                      '{"code":1008,"message":"当前作品已删除。"}')
        self.assertIs(result.kind, ns["Failure"].GONE)
        self.assertTrue(result.definitive)

    def test_rest_endpoints_inside_bundle(self):
        ns = self.namespace
        self.assertTrue(ns["endpoint"]("xhs", "comments").endswith("/xhs/note/comment/list"))

    def test_call_planning_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        row = ns["Row"](record_id="r", link_cell="https://www.douyin.com/video/7123456789012345678")
        plan = ns["plan_calls"](row, settings)
        self.assertEqual([c.purpose for c in plan], ["comments", "detail"])

    def test_soft_deadline_is_under_coze_hard_limit(self):
        # 扣子代码节点硬上限 60 秒，超时会把已经算完的几十行结果一起丢掉
        self.assertLess(self.namespace["SOFT_DEADLINE"], 60)


if __name__ == "__main__":
    unittest.main()
