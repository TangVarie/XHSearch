"""编排层的端到端测试。用假传输层顶掉网络，不需要任何 Key。

这一组测的是最容易出事、也最难在生产上发现的行为：失败时不要写坏表。
"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from xhsearch import protocol, runner, transport
from xhsearch.config import Settings
from xhsearch.rows import Row

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def sse(payload: dict) -> transport.Response:
    """成功响应。REST 接口返回纯 JSON，成功时不带 code 字段。

    （函数名沿用 sse 只是历史习惯，实际是普通 JSON。）
    """
    return transport.Response(200, "application/json", json.dumps(payload), "req-ok")


def err(status: int, code: int, message: str, **extra) -> transport.Response:
    """错误响应。注意业务错误的 HTTP 状态码是 200，靠 body 里的 code 区分。"""
    return transport.Response(
        status, "application/json", json.dumps({"code": code, "message": message, **extra}), "req-err"
    )


def comment_page(count=50, pinned=True, balance=9000):
    items = []
    if pinned:
        items.append({"content": "戳主页领券", "like_count": 10, "is_pinned": True,
                      "is_author_comment": True, "ip_location": "上海", "author": {"name": "官号"}})
    items.append({"content": "好用！", "like_count": 3, "is_pinned": False,
                  "is_author_comment": False, "ip_location": "广东", "author": {"name": "路人"}})
    return {"items": items, "comment_count": count, "top_level_comment_count": count,
            "next_page_token": "", "points": {"cost": 10, "balance": balance}}


def xhs_row(record_id="rec1", *, age_days=1.0, tags=None, prev_count=None, expected=""):
    published = NOW - timedelta(days=age_days)
    return Row(
        record_id=record_id,
        link_cell="https://www.xiaohongshu.com/explore/" + "a" * 24,
        publish_time_ms=int(published.timestamp() * 1000),
        current_tags=tags or [],
        previous_comment_count=prev_count,
        expected_pinned=expected,
    )


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.max_concurrency = 1     # 让断言顺序可预期
        self.settings.soft_deadline_seconds = 0

    def run_with(self, responses, rows, **kwargs):
        """responses: 按调用顺序返回的响应列表，或一个 callable(url, headers, body)。"""
        if callable(responses):
            side_effect = responses
        else:
            queue = list(responses)
            side_effect = lambda *a, **k: queue.pop(0) if queue else err(500, 1005, "测试响应队列已耗尽")
        # 退避是真的会 sleep，测试里一律掐掉，否则重试用例会让整套跑十几秒。
        with mock.patch.object(transport, "post", side_effect=side_effect), \
             mock.patch("time.sleep"):
            return runner.refresh(rows, "fake-key", self.settings, now=NOW, **kwargs)


class TestHappyPath(RunnerTest):
    def test_writes_all_expected_columns(self):
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 8000, "collect_count": 900,
                                                "comment_count": 150, "points": {"cost": 10, "balance": 8990}})],
            [xhs_row()],
        )
        self.assertEqual(report.counts(), {runner.STATUS_OK: 1})
        fields = report.outcomes[0].fields
        f = self.settings.fields
        self.assertEqual(fields[f.comment_count], 150)
        self.assertEqual(fields[f.like_count], 8000)
        self.assertEqual(fields[f.collect_count], 900)
        self.assertIn("戳主页领券", fields[f.pinned_comment])
        self.assertIn("1. [置顶", fields[f.comment_digest])
        self.assertEqual(fields[f.platform], "小红书")
        self.assertIn("大爆", fields[f.traffic_status])
        self.assertEqual(report.credits, 20)
        self.assertEqual(report.points_balance, 8990)

    def test_credits_reported_in_yuan(self):
        report = self.run_with([sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
                               [xhs_row()])
        self.assertIn("¥0.20", report.summary())


class TestHumanTagsAreNeverClobbered(RunnerTest):
    def test_manual_tags_survive_machine_write(self):
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["已复盘", "客户确认", "风控中"])],
        )
        final = report.outcomes[0].fields[self.settings.fields.traffic_status]
        self.assertIn("已复盘", final)
        self.assertIn("客户确认", final)
        self.assertIn("大爆", final)
        self.assertNotIn("风控中", final)   # 机器标签可撤回

    def test_unknown_option_is_filtered_out(self):
        report = self.run_with(
            [sse(comment_page(count=150)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row()],
            known_options=["风控中", "已失效"],       # 表里没建热度档位的选项
        )
        fields = report.outcomes[0].fields
        # 过滤后无标签可写 → 该字段根本不进 payload，而不是写个空数组把人工标签抹掉
        self.assertNotIn(self.settings.fields.traffic_status, fields)
        self.assertIn("还没建选项", fields[self.settings.fields.failure_reason])

    def test_no_tag_change_means_no_write(self):
        """无变化不写：省一次写、避开选项冲突，也不让人看到这行被反复改动。"""
        report = self.run_with(
            [sse(comment_page(count=10)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["已复盘"])],
        )
        self.assertNotIn(self.settings.fields.traffic_status, report.outcomes[0].fields)

    def test_heat_tier_never_downgrades(self):
        """爆过就是爆过。评论被删导致数字掉下去，不该从大爆退回评估中。"""
        report = self.run_with(
            [sse(comment_page(count=25)), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row(tags=["大爆"], prev_count=25)],
        )
        fields = report.outcomes[0].fields
        # 没变化就不写；关键是没被降档
        if self.settings.fields.traffic_status in fields:
            self.assertIn("大爆", fields[self.settings.fields.traffic_status])


class TestDeadPostDetection(RunnerTest):
    def test_first_strike_does_not_convict(self):
        """两击定罪。一次抖动/一次上游话术变化就判死，会把整张表刷红。"""
        report = self.run_with([err(200, 1003, "未找到对应内容")], [xhs_row(tags=["已复盘"])])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_SUSPECT)
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)  # 一个标签都不打
        self.assertEqual(outcome.fields[self.settings.fields.consecutive_failures], 1)
        self.assertIn("第 1 次", outcome.fields[self.settings.fields.failure_reason])

    def test_first_strike_does_not_strip_existing_risk_tag(self):
        """上一轮判过风控，这一轮取不到 —— 不能因此把风控摘掉。"""
        report = self.run_with([err(200, 1003, "未找到对应内容")],
                               [xhs_row(tags=["风控中", "已复盘"])])
        self.assertNotIn(self.settings.fields.traffic_status, report.outcomes[0].fields)

    def test_second_strike_convicts(self):
        row = xhs_row(tags=["已复盘"])
        row.consecutive_failures = 1
        report = self.run_with([err(200, 1003, "未找到对应内容")], [row])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_GONE)
        final = outcome.fields[self.settings.fields.traffic_status]
        self.assertIn("已失效", final)
        self.assertIn("风控中", final)
        self.assertIn("已复盘", final)     # 人工标签照样保住
        self.assertIn("未找到对应内容", outcome.fields[self.settings.fields.failure_reason])

    def test_content_deleted_convicts_on_first_strike(self):
        """错误码 1008 是上游的权威结论（规范原文「不要重试」）——
        再等一轮只是让运营晚一天看到，没有任何收益。"""
        report = self.run_with([err(200, 1008, "当前作品已删除。")], [xhs_row()])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_GONE)
        self.assertIn("已失效", outcome.fields[self.settings.fields.traffic_status])

    def test_surface_unavailable_is_not_treated_as_dead(self):
        """1007「页面暂时不可访问」是瞬时故障，不是内容没了。
        判错这条会把好帖子标成失效。"""
        report = self.run_with([err(200, 1007, "当前页面暂时不可访问")] * 3, [xhs_row()])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)

    def test_request_id_is_written_into_the_table(self):
        """找厂商排查时唯一的凭据，运营截图就能给出去。"""
        report = self.run_with([err(200, 1003, "未找到对应内容")], [xhs_row()])
        self.assertIn("req-err", report.outcomes[0].fields[self.settings.fields.failure_reason])

    def test_success_resets_the_strike_counter(self):
        row = xhs_row()
        row.consecutive_failures = 1
        report = self.run_with(
            [sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})], [row])
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.consecutive_failures], 0)


class TestCircuitBreaker(RunnerTest):
    """几百条笔记不可能在同一小时里被集体删除。真出现这种批量失效，
    一定是上游挂了或错误话术改版了，宁可这轮什么都不写。"""

    def test_mass_failure_voids_all_tag_writes(self):
        rows = []
        for i in range(12):
            row = xhs_row(f"rec{i}")
            row.consecutive_failures = 1      # 都已经是第二击了，本该全部判死
            rows.append(row)
        report = self.run_with(lambda *a, **k: err(200, 1003, "未找到对应内容"), rows)

        self.assertTrue(report.breaker_tripped)
        for outcome in report.outcomes:
            self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)
            self.assertEqual(outcome.status, runner.STATUS_FAILED)
            self.assertIn("疑似上游故障", outcome.fields[self.settings.fields.failure_reason])

    def test_small_batch_does_not_trip_breaker(self):
        row = xhs_row()
        row.consecutive_failures = 1
        report = self.run_with([err(200, 1003, "未找到对应内容")], [row])
        self.assertFalse(report.breaker_tripped)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_GONE)


class TestCooldown(RunnerTest):
    def test_recently_refreshed_row_is_skipped_free(self):
        row = xhs_row()
        row.last_updated_ms = int((NOW - timedelta(seconds=30)).timestamp() * 1000)
        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh([row], "k", self.settings, now=NOW)
        posted.assert_not_called()
        self.assertEqual(report.outcomes[0].status, runner.STATUS_COOLDOWN)
        self.assertEqual(report.credits, 0)

    def test_forced_refresh_ignores_cooldown(self):
        row = xhs_row()
        row.last_updated_ms = int((NOW - timedelta(seconds=30)).timestamp() * 1000)
        report = self.run_with(
            [sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [row], forced=True)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)

    def test_network_blip_does_NOT_tag_as_dead(self):
        # 这是最重要的一条：一次 5xx 不能把好帖子标成风控/失效
        blip = transport.Response(0, "", "网络错误：timed out")
        report = self.run_with([blip, blip, blip], [xhs_row(tags=["大爆"])])
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_FAILED)
        # 流量状态整列不写 —— 保留上一次的结论
        self.assertNotIn(self.settings.fields.traffic_status, outcome.fields)
        self.assertEqual(outcome.fields[self.settings.fields.refresh_status], runner.STATUS_FAILED)


class TestPartialFailure(RunnerTest):
    def test_detail_failure_still_writes_comment_data(self):
        # 评论拿到了，detail 挂了 —— 不该整行判失败
        report = self.run_with(
            [sse(comment_page(count=42)), err(500, 1005, "服务暂时不可用，请稍后重试")],
            [xhs_row()],
        )
        outcome = report.outcomes[0]
        self.assertEqual(outcome.status, runner.STATUS_OK)
        self.assertEqual(outcome.fields[self.settings.fields.comment_count], 42)
        self.assertNotIn(self.settings.fields.like_count, outcome.fields)

    def test_comments_failure_fails_the_row(self):
        report = self.run_with([err(500, 1005, "服务暂时不可用，请稍后重试")] * 3, [xhs_row()])
        self.assertEqual(report.outcomes[0].status, runner.STATUS_FAILED)


class TestFatalErrorsStopTheBatch(RunnerTest):
    def test_invalid_key_aborts_but_keeps_finished_rows(self):
        calls = {"n": 0}

        def responder(url, headers, body, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                return sse(comment_page()) if calls["n"] == 1 else sse(
                    {"like_count": 1, "points": {"cost": 10, "balance": 1}})
            return err(401, 1401, "API Key 无效或已失效。")

        report = self.run_with(responder, [xhs_row("rec1"), xhs_row("rec2"), xhs_row("rec3")])
        self.assertEqual(len(report.outcomes), 1)              # 第一行的结果保住了
        self.assertEqual(report.outcomes[0].record_id, "rec1")
        self.assertIn("1401", report.aborted_reason)

    def test_insufficient_balance_aborts(self):
        report = self.run_with([err(200, 1004, "当前 API Key 积分不足。")], [xhs_row()])
        self.assertEqual(report.outcomes, [])
        self.assertIn("积分不足", report.aborted_reason)


class TestRateLimit(RunnerTest):
    def test_retries_once_then_succeeds(self):
        queue = [
            transport.Response(429, "application/json", json.dumps(
                {"error": "rate_limited", "error_description": "请求过于频繁", "retry_after": 0})),
            sse(comment_page(count=7)),
            sse({"like_count": 1, "points": {"cost": 10, "balance": 1}}),
        ]
        with mock.patch.object(transport, "post", side_effect=lambda *a, **k: queue.pop(0)), \
             mock.patch("time.sleep"):
            report = runner.refresh([xhs_row()], "k", self.settings, now=NOW)
        self.assertEqual(report.outcomes[0].status, runner.STATUS_OK)
        self.assertEqual(report.outcomes[0].fields[self.settings.fields.comment_count], 7)


class TestBadLink(RunnerTest):
    def test_unparseable_link_costs_nothing(self):
        row = Row(record_id="rec9", link_cell="待补链接")
        with mock.patch.object(transport, "post") as posted:
            report = runner.refresh([row], "k", self.settings, now=NOW)
        posted.assert_not_called()
        self.assertEqual(report.outcomes[0].status, runner.STATUS_SKIPPED)
        self.assertEqual(report.credits, 0)

    def test_unparseable_link_does_not_strip_existing_tags(self):
        """链接识别不了 ≠ 帖子没事了。这一轮没获得任何新信息，就别碰流量状态。"""
        row = Row(record_id="rec9", link_cell="待补链接", current_tags=["风控中", "已复盘"])
        with mock.patch.object(transport, "post"):
            report = runner.refresh([row], "k", self.settings, now=NOW)
        self.assertNotIn(self.settings.fields.traffic_status, report.outcomes[0].fields)


class TestDouyin(RunnerTest):
    def _row(self):
        return Row(record_id="d1", link_cell="https://www.douyin.com/video/7123456789012345678",
                   publish_time_ms=int((NOW - timedelta(days=1)).timestamp() * 1000))

    def test_null_comment_count_backfilled_from_detail(self):
        page = {"items": [{"content": "哈哈", "like_count": 2, "is_hot": True,
                           "ip_location": "浙江", "author": {"nickname": "路人"}}],
                "comment_count": None, "points": {"cost": 10, "balance": 100}}
        report = self.run_with(
            [sse(page), sse({"comment_count": 321, "like_count": 5000, "points": {"cost": 10, "balance": 90}})],
            [self._row()],
        )
        fields = report.outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_count], 321)
        self.assertEqual(fields[self.settings.fields.platform], "抖音")

    def test_pinned_column_says_unsupported_not_empty(self):
        page = {"items": [], "comment_count": 5, "points": {"cost": 10, "balance": 1}}
        report = self.run_with([sse(page), sse({"comment_count": 5, "points": {"cost": 10, "balance": 1}})],
                               [self._row()])
        pinned = report.outcomes[0].fields[self.settings.fields.pinned_comment]
        self.assertIn("抖音不支持置顶监控", pinned)

    def test_douyin_never_touches_comment_status(self):
        """抖音判不了置顶，就绝不能碰运营手工维护的「评论状态」列。"""
        page = {"items": [], "comment_count": 5, "points": {"cost": 10, "balance": 1}}
        report = self.run_with([sse(page), sse({"comment_count": 5, "points": {"cost": 10, "balance": 1}})],
                               [self._row()])
        self.assertNotIn(self.settings.fields.comment_status, report.outcomes[0].fields)


class TestPinnedTracking(RunnerTest):
    """「评论状态」多选列：机器管置顶三值，人工值原样保留。"""

    def _row(self, *, expected="戳主页领券", status=None):
        row = xhs_row(expected=expected)
        row.comment_status = status or []
        return row

    def _run(self, pinned, row):
        return self.run_with(
            [sse(comment_page(pinned=pinned)),
             sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [row],
        )

    def test_seeded_comment_still_pinned(self):
        fields = self._run(True, self._row()).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status],
                         [self.settings.comment_status.pinned_ok])

    def test_never_pinned(self):
        fields = self._run(False, self._row()).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status],
                         [self.settings.comment_status.never_pinned])

    def test_pin_fell_off_after_success(self):
        cs = self.settings.comment_status
        fields = self._run(False, self._row(status=[cs.pinned_ok])).outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status], [cs.pinned_lost])
        self.assertIn("此前已确认置顶成功", fields[self.settings.fields.failure_reason])

    def test_human_value_in_the_same_column_survives(self):
        """「评论是否显示」这类人工维护的值和置顶并列在同一列，机器绝不碰。"""
        cs = self.settings.comment_status
        row = self._row(status=["评论已显示", cs.pinned_ok])
        final = self._run(False, row).outcomes[0].fields[self.settings.fields.comment_status]
        self.assertIn("评论已显示", final)
        self.assertIn(cs.pinned_lost, final)
        self.assertNotIn(cs.pinned_ok, final)     # 三值互斥，旧值被摘掉

    def test_pin_taken_over_by_someone_else(self):
        """品牌方最该立刻知道的一种：置顶还在，但被换成了别人的评论。"""
        page = comment_page(pinned=True)
        page["items"][0]["content"] = "楼主恰饭了吧，别买"
        page["items"].append({"content": "戳主页领券", "like_count": 1, "is_pinned": False,
                              "is_author_comment": True, "ip_location": "上海",
                              "author": {"name": "官号"}})
        cs = self.settings.comment_status
        report = self.run_with(
            [sse(page), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [self._row(status=[cs.pinned_ok])],
        )
        fields = report.outcomes[0].fields
        self.assertEqual(fields[self.settings.fields.comment_status], [cs.pinned_lost])
        self.assertIn("置顶位被他人占据", fields[self.settings.fields.failure_reason])
        # 置顶评论列展示的是「实际置顶的那条」，不是我们希望置顶的那条
        self.assertIn("楼主恰饭了吧", fields[self.settings.fields.pinned_comment])

    def test_no_seed_keyword_leaves_the_column_alone(self):
        """有置顶但没填种子关键词：写「置顶成功」是撒谎，写「没有置顶」也是撒谎。"""
        row = self._row(expected="", status=["评论已显示"])
        fields = self._run(True, row).outcomes[0].fields
        self.assertNotIn(self.settings.fields.comment_status, fields)

    def test_unknown_option_is_filtered(self):
        report = self.run_with(
            [sse(comment_page(pinned=True)),
             sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [self._row()],
            comment_status_options=["评论已显示"],     # 表里没建置顶三值
        )
        fields = report.outcomes[0].fields
        self.assertNotIn(self.settings.fields.comment_status, fields)
        self.assertIn("还没建选项", fields[self.settings.fields.failure_reason])


if __name__ == "__main__":
    unittest.main()


class TestExitSignal(RunnerTest):
    """退出码要能区分「真故障」和「正常分批」——定时任务的重启策略靠它。"""

    def test_fatal_error_is_marked_fatal(self):
        report = self.run_with([err(401, 1401, "API Key 无效或已失效。")], [xhs_row()])
        self.assertTrue(report.fatal)

    def test_quota_exhausted_is_fatal(self):
        report = self.run_with([err(200, 1004, "当前 API Key 积分不足。")], [xhs_row()])
        self.assertTrue(report.fatal)

    def test_soft_deadline_is_not_fatal(self):
        """到软截止把剩余的行留给下一轮，是正常运行不是失败。
        判错这条会让云平台把每一轮正常分批都当成崩溃，反复重启。"""
        self.settings.soft_deadline_seconds = 0.0001
        rows = [xhs_row(f"rec{i}") for i in range(5)]
        report = self.run_with(lambda *a, **k: sse(comment_page()), rows)
        self.assertFalse(report.fatal)

    def test_normal_run_is_not_fatal(self):
        report = self.run_with(
            [sse(comment_page()), sse({"like_count": 1, "points": {"cost": 10, "balance": 1}})],
            [xhs_row()])
        self.assertFalse(report.fatal)
        self.assertEqual(report.aborted_reason, "")
