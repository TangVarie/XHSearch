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
            "decide", "decide_pinned_state", "gone_verdict", "suspect_verdict",
            "merge", "format_digest", "format_pinned", "parse_response", "build_call",
            "headers", "endpoint", "Failure", "FATAL", "Err", "Ok", "main",
        ]:
            self.assertIn(name, self.namespace, f"打包产物里缺 {name}")

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
        self.assertIn("爆文", verdict.tags)
        self.assertIn("置顶成功", verdict.tags)
        self.assertEqual(verdict.pinned_state, settings.pinned_states.success)
        self.assertIn("置顶", ns["format_digest"](snapshot, settings.digest))

    def test_tag_merge_works_inside_bundle(self):
        ns = self.namespace
        settings = ns["Settings"]()
        merged = ns["merge"](["已复盘", "风控"], {"爆文"},
                             settings.tags.namespace(), sticky=settings.tags.sticky())
        self.assertIn("已复盘", merged.final)
        self.assertIn("爆文", merged.final)
        self.assertNotIn("风控", merged.final)

    def test_protocol_parsing_works_inside_bundle(self):
        ns = self.namespace
        result = ns["parse_response"](
            401, "application/json",
            '{"error":"invalid_api_key","error_description":"API Key 无效或已失效。"}')
        self.assertIs(result.kind, ns["Failure"].AUTH)
        self.assertIn(result.kind, ns["FATAL"])

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
