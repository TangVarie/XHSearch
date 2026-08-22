"""双通道降级：主通道倒下时，备胎顶上而不是整批停摆。

这一组测的是双通道**唯一真正值钱的地方**。省钱只是顺带的——一家挂了另一家
接着跑，才是把「半夜 Key 失效 = 一整晚的监控全丢」这个故障模式消掉。

关键的反面用例也在这里：**「笔记没了」绝不能触发降级**。那是行级结论不是
通道故障，换一家只会再花一次钱得到同一个答案。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import runner, transport
from xhsearch.config import Channels, Settings
from xhsearch.rows import Row

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
KEYS = {"tikhub": "t-key", "socialdatax": "s-key"}


def tikhub_ok_comments(count=150) -> transport.Response:
    comments = [] if count == 0 else [
        {"content": "戳主页", "like_count": 9, "ip_location": "Shanghai",
         "show_tags_v2": [{"type": "user_top"}, {"type": "is_author"}],
         "user": {"nickname": "官号"}}
    ]
    return transport.Response(200, "application/json", json.dumps({
        "code": 200, "request_id": "th-1", "data": {
            "code": 0, "success": True, "data": {
                "comments": comments,
                "comment_count": count, "comment_count_l1": count,
                "all_sort_strategies": [{"type": "default"}], "user_id": "u9",
            },
        },
    }, ensure_ascii=False), "th-1")


def tikhub_ok_detail() -> transport.Response:
    return transport.Response(200, "application/json", json.dumps({
        "code": 200, "data": {"code": 0, "success": True, "data": [{"note_list": [{
            "liked_count": 500, "collected_count": 60, "shared_count": 7,
            "comments_count": 150, "in_censor": False}]}]},
    }), "th-2")


def tikhub_err(status: int, message: str) -> transport.Response:
    return transport.Response(status, "application/json", json.dumps(
        {"detail": {"code": status, "message_zh": message, "request_id": "th-e"}},
        ensure_ascii=False), "th-e")


def sdx_ok_comments(count=150) -> transport.Response:
    return transport.Response(200, "application/json", json.dumps({
        "items": [{"content": "戳主页", "like_count": 9, "is_pinned": True,
                   "is_author_comment": True, "ip_location": "上海",
                   "author": {"name": "官号"}}],
        "comment_count": count, "top_level_comment_count": count,
        "points": {"cost": 10, "balance": 8000},
    }, ensure_ascii=False), "sdx-1")


def sdx_ok_detail() -> transport.Response:
    return transport.Response(200, "application/json", json.dumps({
        "like_count": 500, "collect_count": 60, "comment_count": 150,
        "points": {"cost": 10, "balance": 7990},
    }), "sdx-2")


def sdx_err(status: int, code: int, message: str) -> transport.Response:
    return transport.Response(status, "application/json",
                              json.dumps({"code": code, "message": message}, ensure_ascii=False),
                              "sdx-e")


def xhs_row(record_id="rec1", *, age_days=1.0) -> Row:
    return Row(
        record_id=record_id,
        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
        publish_time_ms=int((NOW - timedelta(days=age_days)).timestamp() * 1000),
    )


class FailoverTest(unittest.TestCase):
    """把两家的传输层分开顶掉：GET 一定是 TikHub，POST 一定是 SocialDataX。"""

    def setUp(self):
        self.settings = Settings()
        self.settings.max_concurrency = 1
        self.settings.soft_deadline_seconds = 0
        self.get_calls: list[str] = []
        self.post_calls: list[str] = []

    def run_with(self, *, on_get, on_post, rows=None, keys=None, **kwargs):
        def _get(url, headers, timeout=30.0):
            self.get_calls.append(url)
            return on_get(len(self.get_calls) - 1)

        def _post(url, headers, body, timeout=30.0):
            self.post_calls.append(url)
            return on_post(len(self.post_calls) - 1)

        with mock.patch.object(transport, "get", side_effect=_get), \
             mock.patch.object(transport, "post", side_effect=_post), \
             mock.patch("time.sleep"):
            return runner.refresh(rows or [xhs_row()], keys or KEYS,
                                  self.settings, now=NOW, **kwargs)


class TestHappyPathUsesPrimary(FailoverTest):
    def test_only_tikhub_is_called_when_it_works(self):
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(), tikhub_ok_detail()][i],
            on_post=lambda i: self.fail("不该降级"),
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(self.post_calls, [])
        self.assertEqual(report.used_providers, {"tikhub": 2})
        self.assertEqual(report.failovers, 0)

    def test_cost_is_counted_at_the_channel_actually_used(self):
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(), tikhub_ok_detail()][i],
            on_post=lambda i: self.fail("不该降级"),
        )
        # 小红书 2 次 × $0.01 × 7.2 ≈ ¥0.144，明显低于 SocialDataX 的 ¥0.20
        self.assertLess(report.cost_yuan, 0.20)
        self.assertGreater(report.cost_yuan, 0.0)


class TestFailover(FailoverTest):
    def test_transport_failure_falls_back(self):
        report = self.run_with(
            on_get=lambda i: transport.Response(0, "", "网络错误：timed out"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.comment_count], 150)
        self.assertEqual(report.used_providers, {"socialdatax": 2})
        self.assertEqual(report.failovers, 2)
        self.assertFalse(report.fatal)

    def test_dead_key_on_primary_is_survivable(self):
        """主通道 Key 失效，在单通道时代等于整批停摆。现在只是换一家继续。"""
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertFalse(report.fatal)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_dead_primary_is_tried_once_not_once_per_row(self):
        """600 行各撞一次 401 既慢又会把限流打出来，所以要把它整轮拉黑。"""
        rows = [xhs_row(f"rec{i}") for i in range(5)]
        self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: sdx_ok_comments() if i % 2 == 0 else sdx_ok_detail(),
            rows=rows,
        )
        self.assertEqual(len(self.get_calls), 1)
        self.assertEqual(len(self.post_calls), 10)   # 5 行 × （评论 + detail）

    def test_quota_exhausted_on_primary_falls_back(self):
        report = self.run_with(
            on_get=lambda i: tikhub_err(403, "账户余额不足，请充值。"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertFalse(report.fatal)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_both_channels_down_is_finally_fatal(self):
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: sdx_err(401, 1401, "API Key 无效或已失效。"),
        )
        self.assertTrue(report.fatal)


class TestNoPointlessFailover(FailoverTest):
    def test_gone_does_not_burn_a_second_channel(self):
        """「笔记没了」是行级结论。换一家只会再花一次钱换来同一个答案。"""
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "comment_count_l1": 0,
                "all_sort_strategies": [], "user_id": "",
            }},
        }), "th-gone")
        report = self.run_with(
            on_get=lambda i: empty_shell,
            on_post=lambda i: self.fail("GONE 不该触发降级"),
        )
        self.assertEqual(self.post_calls, [])
        # 第一次不定罪，只记一笔——两击定罪没有因为双通道被绕过。
        self.assertEqual(report.outcomes[0].status, runner.STATUS_SUSPECT)
        self.assertEqual(report.failovers, 0)

    def test_a_billed_failure_still_shows_up_on_the_bill(self):
        """TikHub 对不存在的笔记「正常响应、正常计费」，只是 data 是空壳。

        只记成功的调用，账面就会比账单少——少报成本的监控系统没有价值。
        """
        empty_shell = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": {
                "comments": [], "comment_count": 0, "comment_count_l1": 0,
                "all_sort_strategies": [], "user_id": "",
            }},
        }), "th-gone")
        report = self.run_with(
            on_get=lambda i: empty_shell,
            on_post=lambda i: self.fail("GONE 不该触发降级"),
        )
        self.assertGreater(report.cost_yuan, 0.0)
        self.assertEqual(report.used_providers, {"tikhub": 1})

    def test_an_unbilled_failure_does_not_show_up_on_the_bill(self):
        """401 根本没进业务层，对方明说不计费——算进去就是虚报。"""
        report = self.run_with(
            on_get=lambda i: tikhub_err(401, "无效的API令牌"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertNotIn("tikhub", report.used_providers)
        self.assertEqual(report.used_providers, {"socialdatax": 2})

    def test_definitive_death_from_detail_convicts_in_one_round(self):
        """detail 返回空 data 是干净的死亡信号，不必等第二次。"""
        row = xhs_row()
        row.consecutive_failures = 0
        dead_detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": []},
        }), "th-dead")
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(count=0), dead_detail][i],
            on_post=lambda i: self.fail("GONE 不该触发降级"),
            rows=[row],
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_GONE)

    def test_live_note_is_not_killed_by_a_contradictory_detail(self):
        """评论接口刚拿回 150 条评论、detail 却说笔记不存在 —— 那是上游自相矛盾。

        这时候信 detail 就是拿一次抖动杀掉一条好帖子，正是这个项目最不能犯的错。
        """
        dead_detail = transport.Response(200, "application/json", json.dumps({
            "code": 200, "data": {"code": 0, "success": True, "data": []},
        }), "th-dead")
        report = self.run_with(
            on_get=lambda i: [tikhub_ok_comments(count=150), dead_detail][i],
            on_post=lambda i: self.fail("不该降级"),
        )
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.comment_count], 150)


class TestSingleChannelIsUnchanged(FailoverTest):
    def test_bare_string_key_behaves_exactly_as_before(self):
        """老调用方传裸字符串：只打 SocialDataX，一次 GET 都不该发出去。"""
        report = self.run_with(
            on_get=lambda i: self.fail("裸 key 不该去打 TikHub"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
            keys="only-sdx",
        )
        self.assertEqual(self.get_calls, [])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_channel_can_be_pinned_to_one_vendor(self):
        self.settings.channels = Channels(order={"xhs": ["socialdatax"]})
        self.run_with(
            on_get=lambda i: self.fail("已经指定只走 SocialDataX"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
        )
        self.assertEqual(self.get_calls, [])

    def test_missing_key_for_primary_silently_uses_the_spare(self):
        report = self.run_with(
            on_get=lambda i: self.fail("没配 TikHub 的 key 就不该去打它"),
            on_post=lambda i: [sdx_ok_comments(), sdx_ok_detail()][i],
            keys={"socialdatax": "s-key"},
        )
        self.assertEqual(self.get_calls, [])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)


class TestSecrets(FailoverTest):
    def test_api_key_never_reaches_the_table(self):
        """TikHub 的报错会回显 Key，而报错文案是要写进飞书诊断列的。"""
        leaky = transport.Response(401, "application/json", json.dumps({
            "detail": {"code": 401, "message_zh": "无效的API令牌，您提交的API令牌为 t-key。"},
        }, ensure_ascii=False), "th-e")
        report = self.run_with(
            on_get=lambda i: leaky,
            on_post=lambda i: sdx_err(500, 1005, "服务暂时不可用"),
        )
        blob = json.dumps([o.fields for o in report.outcomes], ensure_ascii=False)
        self.assertNotIn("t-key", blob)
        self.assertNotIn("t-key", report.summary())


if __name__ == "__main__":
    unittest.main()
