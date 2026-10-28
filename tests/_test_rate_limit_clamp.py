# -*- coding: utf-8 -*-
"""上游恢复时刻的双侧夹取。

借鉴：Coding2API `src/engine/scheduler.py:129-152`。

上游给的恢复时刻是**上游的事实**，但它可能不可信：
  - 已经过去（时钟偏移、上游写了刚过去的时刻）⇒ 算出来的等待是 0 或负数，
    账号立刻重新上线、立刻再撞 —— 冷却等于没设；
  - 荒唐地远（配额月结、上游自己算错）⇒ 账号被停放一个月，
    等于从池子里静默消失，面板只显示「无可用账号」。
"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_proxy as P  # noqa: E402


class ResetClampTests(unittest.TestCase):
    def test_the_cap_is_a_day(self):
        self.assertEqual(24 * 3600.0, P.RATE_LIMIT_RESET_MAX_WAIT)

    def test_the_cap_is_positive_and_sane(self):
        self.assertGreater(P.RATE_LIMIT_RESET_MAX_WAIT, 3600.0)
        self.assertLessEqual(P.RATE_LIMIT_RESET_MAX_WAIT, 7 * 24 * 3600.0)


class ResetParsingTests(unittest.TestCase):
    """恢复时刻的解析本身。"""

    def test_an_english_reset_stamp_is_parsed(self):
        detail = '{"code":6004,"msg":"your usage will reset at 2026-09-19 18:29:03 UTC+8"}'
        stamp = P.parse_rate_limit_reset(detail)
        self.assertIsNotNone(stamp)
        self.assertGreater(stamp, 0)

    def test_a_chinese_reset_stamp_is_parsed(self):
        detail = '{"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-10-02 16:00:34 UTC+8 重置"}'
        stamp = P.parse_rate_limit_reset(detail)
        self.assertIsNotNone(stamp)
        self.assertGreater(stamp, 0)

    def test_an_unparseable_body_yields_none(self):
        """容错：坏正文不能打断请求路径。"""
        self.assertIsNone(P.parse_rate_limit_reset(""))
        self.assertIsNone(P.parse_rate_limit_reset("not json"))
        self.assertIsNone(P.parse_rate_limit_reset("no clock here"))

    def test_the_stamp_is_utc_plus_8_not_the_host_timezone(self):
        """正文里的时间戳带自己的 UTC 偏移，必须按偏移换算。"""
        detail = '{"msg":"reset at 2026-09-19 18:29:03 UTC+8"}'
        stamp = P.parse_rate_limit_reset(detail)
        # 2026-09-19 18:29:03 +0800 == 10:29:03 UTC
        import calendar
        expect = calendar.timegm((2026, 9, 19, 10, 29, 3, 0, 0, 0))
        self.assertEqual(expect, stamp)

    def test_a_negative_offset_is_handled(self):
        detail = '{"msg":"reset at 2026-09-19 18:29:03 UTC-5"}'
        stamp = P.parse_rate_limit_reset(detail)
        self.assertIsNotNone(stamp)
        import calendar
        expect = calendar.timegm((2026, 9, 19, 23, 29, 3, 0, 0, 0))
        self.assertEqual(expect, stamp)


class ClampBehaviourTests(unittest.TestCase):
    """把夹取逻辑抽出来单独验证（与线上同一套常量）。"""

    def _clamp(self, reset_at):
        """复刻 wb_proxy 里 429 分支的夹取逻辑。"""
        now = time.time()
        wait = max(1.0, reset_at - now) if reset_at else 60.0
        if reset_at is not None:
            wait = min(wait, P.RATE_LIMIT_RESET_MAX_WAIT)
            reset_at = now + wait
        return wait, reset_at

    def test_a_past_stamp_still_gets_at_least_one_second(self):
        wait, reset_at = self._clamp(time.time() - 3600)
        self.assertGreaterEqual(wait, 1.0)
        self.assertGreater(reset_at, time.time())

    def test_a_far_future_stamp_is_capped(self):
        wait, reset_at = self._clamp(time.time() + 365 * 24 * 3600)
        self.assertLessEqual(wait, P.RATE_LIMIT_RESET_MAX_WAIT)
        self.assertLessEqual(reset_at - time.time(), P.RATE_LIMIT_RESET_MAX_WAIT + 1)

    def test_a_normal_stamp_is_left_alone(self):
        base = time.time() + 600
        wait, reset_at = self._clamp(base)
        self.assertAlmostEqual(600.0, wait, delta=2.0)
        self.assertAlmostEqual(base, reset_at, delta=2.0)

    def test_no_stamp_keeps_the_default(self):
        wait, reset_at = self._clamp(None)
        self.assertEqual(60.0, wait)
        self.assertIsNone(reset_at)


if __name__ == "__main__":
    unittest.main()
