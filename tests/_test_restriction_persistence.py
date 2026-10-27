# -*- coding: utf-8 -*-
"""连坐暂停持久化 + 11140 判别收紧。

背景（2026-10-27，借鉴 ops 补丁 v1.8.0-ops.20261026.1）：

1. **区域暂停原先只在内存里** —— 容器一重启，30 分钟的整池暂停就没了，
   于是「上游刚把这一区域的账号判非法」这个事实被丢掉，池子立刻恢复派发，
   冷账号重新上桌再吃一次 11140，凑够两次触发**第二轮**连坐。
2. **11140 的 code 匹配没有右边界** —— `{"code":111400}`（另一个 code）
   会被误判成账号级风控，代价是冷却一个健康账号并把它记进连坐事件。
"""
import importlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class RestrictionCodeTest(unittest.TestCase):
    """11140 的 code 必须带右边界。"""

    def test_real_11140_is_recognised(self):
        for body in ('{"code":11140,"msg":"request illegal"}',
                     '{"code":11140}',
                     '{"code":"11140"}',
                     '{"code":11140,"msg":"x","requestId":"01a1780c41db7f289f0a00ec2630b29b"}'):
            self.assertTrue(wb_proxy.restriction_is_account_level(body), body)

    def test_similar_code_is_not_a_restriction(self):
        """111400 / 111401 是别的 code，不能当成账号级封禁。"""
        for body in ('{"code":111400,"msg":"rate limited"}',
                     '{"code":111401}',
                     '{"code":1114000}'):
            self.assertFalse(wb_proxy.restriction_is_account_level(body), body)

    def test_quota_wording_without_403_is_not_a_restriction(self):
        """429 / 6004 的正文不含风控措辞 —— 不能当成账号级封禁。

        注意判别的前提是「调用方已经在 403 语境里」（两个调用点都是
        HTTPError 的 403 分支），所以这里验的是**正文本身**不含风控特征。
        """
        self.assertFalse(wb_proxy.restriction_is_account_level(
            '{"code":6004,"msg":"您的使用量已超出频率限制"}'))
        self.assertFalse(wb_proxy.restriction_is_account_level(
            '{"code":14003,"msg":"too many requests"}'))
        self.assertFalse(wb_proxy.restriction_is_account_level(
            'usage exceeds frequency limit'))

    def test_markers_with_403_context_are_a_restriction(self):
        self.assertTrue(wb_proxy.restriction_is_account_level(
            'HTTP 403: Your account has been restricted due to unusual activity'))
        self.assertTrue(wb_proxy.restriction_is_account_level(
            '账号存在异常行为，已被限制使用'))

    def test_content_review_403_is_not_account_level(self):
        """内容审核的 403 走降级提示词那条路，不能当成账号封禁。"""
        self.assertFalse(wb_proxy.restriction_is_account_level(
            '{"code":1004,"msg":"content review"}'))
        self.assertFalse(wb_proxy.restriction_is_account_level(""))


class RestrictionPersistenceTest(unittest.TestCase):
    """暂停跨重启存活。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-restrict-")
        self._old_usage = wb_proxy.USAGE_DIR
        wb_proxy.USAGE_DIR = self.tmp
        wb_proxy._RESTRICTION_STATE_PATH = None
        wb_proxy._RESTRICTION_LOAD_ERROR = False
        wb_proxy._RESTRICTION_WRITE_FAILURES = 0
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()

    def tearDown(self):
        wb_proxy.USAGE_DIR = self._old_usage
        wb_proxy._RESTRICTION_STATE_PATH = None
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()

    def _state_file(self):
        return os.path.join(self.tmp, "restriction-state.json")

    def test_state_file_is_created(self):
        wb_proxy.note_account_restriction("cn", "uid-a")
        self.assertTrue(os.path.exists(self._state_file()))

    def test_state_file_is_private(self):
        wb_proxy.note_account_restriction("cn", "uid-a")
        if os.name != "nt":
            mode = os.stat(self._state_file()).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_raw_uid_is_not_persisted(self):
        """落盘只存账号哈希 —— 明文 uid 属账号标识。"""
        wb_proxy.note_account_restriction("cn", "uid-sensitive")
        with open(self._state_file(), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("uid-sensitive", raw)

    def test_pause_survives_a_restart(self):
        """核心：重启后暂停还在，且**不重置**原到期时刻。"""
        wb_proxy.note_account_restriction("cn", "uid-a")
        wb_proxy.note_account_restriction("cn", "uid-b")
        before = wb_proxy.restriction_hold_remaining("cn")
        self.assertGreater(before, 0)
        # 模拟重启：清内存，重新加载
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()
        wb_proxy._RESTRICTION_STATE_PATH = None
        after = wb_proxy.restriction_hold_remaining("cn")
        self.assertGreater(after, 0)
        self.assertAlmostEqual(before, after, delta=1.0)

    def test_expired_pause_is_not_restored(self):
        """已过期的窗口不该被读回来 —— 挂上等于凭空多等一段。"""
        wb_proxy.note_account_restriction("cn", "uid-a")
        wb_proxy.note_account_restriction("cn", "uid-b")
        # 把落盘里的到期改成过去
        with open(self._state_file(), encoding="utf-8") as fh:
            data = json.load(fh)
        data["holds"]["cn"] = 1.0
        with open(self._state_file(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()
        wb_proxy._RESTRICTION_STATE_PATH = None
        self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)

    def test_same_account_is_not_counted_twice(self):
        """同一个号被反复探测不能把连坐骗出来。"""
        wb_proxy.note_account_restriction("cn", "uid-a")
        wb_proxy.note_account_restriction("cn", "uid-a")
        wb_proxy.note_account_restriction("cn", "uid-a")
        self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)

    def test_two_accounts_trigger_the_pause(self):
        wb_proxy.note_account_restriction("cn", "uid-a")
        wb_proxy.note_account_restriction("cn", "uid-b")
        self.assertGreater(wb_proxy.restriction_hold_remaining("cn"), 0)

    def test_reset_clears_the_persisted_state_too(self):
        """人工「已申诉、可以恢复」必须连落盘一起清，否则重启后又被读回来。"""
        wb_proxy.note_account_restriction("cn", "uid-a")
        wb_proxy.note_account_restriction("cn", "uid-b")
        self.assertGreater(wb_proxy.restriction_hold_remaining("cn"), 0)
        wb_proxy.reset_restriction_state()
        self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)
        wb_proxy._RESTRICTION_STATE_PATH = None
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()
        self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)

    def test_corrupt_file_does_not_invent_a_pause(self):
        """坏文件只记诊断，**不发明暂停** —— 宁可少暂停，不可凭空停池。"""
        with open(self._state_file(), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        wb_proxy._RESTRICTION_STATE_PATH = None
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()
        self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)
        self.assertTrue(wb_proxy.restriction_state_snapshot()["load_error"])

    def test_oversized_file_is_rejected(self):
        with open(self._state_file(), "wb") as fh:
            fh.write(b"x" * (1024 * 1024 + 10))
        wb_proxy._RESTRICTION_STATE_PATH = None
        wb_proxy._RESTRICTION_EVENTS.clear()
        wb_proxy._RESTRICTION_HOLD.clear()
        self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)
        self.assertTrue(wb_proxy.restriction_state_snapshot()["load_error"])

    def test_snapshot_shape(self):
        snap = wb_proxy.restriction_state_snapshot()
        for key in ("scope", "enabled", "load_error", "write_failures", "realms"):
            self.assertIn(key, snap)
        self.assertIn("cn", snap["realms"])
        self.assertIn("remaining_seconds", snap["realms"]["cn"])

    def test_explicit_off_stays_off(self):
        """WB_RESTRICTION_HOLD_SECONDS=0 时永远不暂停，重启后依然如此。"""
        old = wb_proxy.RESTRICTION_HOLD_SECONDS
        wb_proxy.RESTRICTION_HOLD_SECONDS = 0
        try:
            wb_proxy.note_account_restriction("cn", "uid-a")
            wb_proxy.note_account_restriction("cn", "uid-b")
            self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)
            wb_proxy._RESTRICTION_STATE_PATH = None
            wb_proxy._RESTRICTION_EVENTS.clear()
            wb_proxy._RESTRICTION_HOLD.clear()
            self.assertEqual(wb_proxy.restriction_hold_remaining("cn"), 0.0)
        finally:
            wb_proxy.RESTRICTION_HOLD_SECONDS = old


if __name__ == "__main__":
    unittest.main()
