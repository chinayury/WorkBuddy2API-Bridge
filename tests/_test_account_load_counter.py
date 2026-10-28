# -*- coding: utf-8 -*-
"""实时在途计数（_ACCOUNT_LOAD）的生命周期。

这个计数器是**永远打开**的（与「每账号并发上限」开关无关），
账号池用它把并发请求分摊到不同账号上。

泄漏的后果比名额泄漏更隐蔽：
  - 名额有上限可见（面板显示「无可用账号」）；
  - 负载计数只增不减，那个账号的「忙」值永久虚高 ⇒ 被选号永久排到最后
    ⇒ 等于被**静默**踢出池子，面板上看不出任何异常。

所以这里钉住三条：
  1. acquire 与 release 配对；
  2. 响应 close 钩子会归还（且只归还一次）；
  3. 失败路径也会归还。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_proxy as P  # noqa: E402


class _FakeAccount(object):
    def __init__(self, uid):
        self.uid = uid


class _FakeResponse(object):
    """一个可以被 close 的响应对象。"""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class LoadCounterTests(unittest.TestCase):
    def setUp(self):
        P._ACCOUNT_LOAD.clear()

    def tearDown(self):
        P._ACCOUNT_LOAD.clear()

    def test_acquire_and_release_balance(self):
        P.account_load_acquire("u1")
        P.account_load_acquire("u1")
        self.assertEqual(2, P.account_load_count("u1"))
        P.account_load_release("u1")
        self.assertEqual(1, P.account_load_count("u1"))
        P.account_load_release("u1")
        self.assertEqual(0, P.account_load_count("u1"))

    def test_the_entry_is_removed_at_zero(self):
        """归零后不留条目 —— 否则 _ACCOUNT_LOAD 会随 uid 无限增长。"""
        P.account_load_acquire("u1")
        P.account_load_release("u1")
        self.assertNotIn("u1", P._ACCOUNT_LOAD)

    def test_release_without_acquire_is_harmless(self):
        P.account_load_release("never")
        self.assertEqual(0, P.account_load_count("never"))

    def test_counts_are_per_account(self):
        P.account_load_acquire("a")
        P.account_load_acquire("b")
        P.account_load_acquire("b")
        self.assertEqual(1, P.account_load_count("a"))
        self.assertEqual(2, P.account_load_count("b"))


class BindReleaseTests(unittest.TestCase):
    """名额与负载计数绑到响应 close 上。"""

    def setUp(self):
        P._ACCOUNT_LOAD.clear()

    def tearDown(self):
        P._ACCOUNT_LOAD.clear()

    def test_closing_the_response_releases_the_load(self):
        """核心回归：响应关闭时负载计数必须归零。

        曾经的真实缺陷：回调去读一个已经被置空的变量，归还变成空操作，
        计数只增不减 ⇒ 账号被选号永久排到最后。
        """
        account = _FakeAccount("u-close")
        resp = _FakeResponse()
        P.account_load_acquire(account.uid)
        P.bind_account_slot_to_response(
            resp, account,
            on_release=lambda: P.account_load_release(account.uid))
        self.assertEqual(1, P.account_load_count("u-close"))
        resp.close()
        self.assertEqual(0, P.account_load_count("u-close"))

    def test_the_release_happens_only_once(self):
        """close 被调用多次也不能重复扣减（扣成负数更糟）。"""
        account = _FakeAccount("u-once")
        resp = _FakeResponse()
        P.account_load_acquire(account.uid)
        P.bind_account_slot_to_response(
            resp, account,
            on_release=lambda: P.account_load_release(account.uid))
        resp.close()
        resp.close()
        resp.close()
        self.assertEqual(0, P.account_load_count("u-close".replace("u-close", "u-once")))
        self.assertNotIn("u-once", P._ACCOUNT_LOAD)

    def test_a_none_response_releases_immediately(self):
        """没有响应对象可挂时立刻归还，否则名额与负载都会永久泄漏。"""
        account = _FakeAccount("u-none")
        P.account_load_acquire(account.uid)
        P.bind_account_slot_to_response(
            None, account,
            on_release=lambda: P.account_load_release(account.uid))
        self.assertEqual(0, P.account_load_count("u-none"))

    def test_a_raising_callback_does_not_break_the_slot_release(self):
        """回调抛异常不能影响名额的归还（响应关闭是主路径）。"""
        account = _FakeAccount("u-raise")
        resp = _FakeResponse()

        def boom():
            raise RuntimeError("release callback exploded")

        P.bind_account_slot_to_response(resp, account, on_release=boom)
        resp.close()          # 不能抛
        self.assertEqual(1, resp.closed)

    def test_no_callback_means_no_error(self):
        account = _FakeAccount("u-plain")
        resp = _FakeResponse()
        P.bind_account_slot_to_response(resp, account)
        resp.close()
        self.assertEqual(1, resp.closed)


if __name__ == "__main__":
    unittest.main()
