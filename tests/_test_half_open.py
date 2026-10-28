# -*- coding: utf-8 -*-
"""半开探测（half-open probe）。

问题（借鉴 Coding2API src/engine/half_open.py:1-22 的原文描述）：
冷却一到期，**所有**并发请求会同时压到刚解冻的同一个账号上，形成
「冷却 60s → 到期 → 全员压上 → 再撞 → 再冷却」的锯齿。

上游按「账号 + 分钟」计 token，一个大上下文会话单请求就带十几万 token ——
这一撞就可能再破配额，然后整池进入新一轮冷却。

做法：冷却到期后账号进入半开态，只有**一个**在途请求被放去试探；
其余请求改选别的账号。试探成功 ⇒ 标记为已恢复。
"""
import base64
import json
import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts as A  # noqa: E402


def _jwt(sub):
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": "https://workbuddy.ai", "sub": sub, "exp": 4102444800}))


def _account(uid):
    return A.Account({
        "uid": uid,
        "realm": "cn",
        "domain": "www.workbuddy.ai",
        "accessToken": _jwt(uid),
        "enabled": True,
        "tasksEnabled": True,
        "verifiedAt": 1700000000,
    })


class HalfOpenTokenTests(unittest.TestCase):
    """半开准入令牌本身。"""

    def setUp(self):
        self.account = _account("u-half")

    def test_first_probe_is_allowed(self):
        self.assertTrue(self.account.half_open_probe("m1"))

    def test_only_one_probe_at_a_time(self):
        """第一个拿到令牌；第二个在第一个有结论之前拿不到。"""
        self.assertTrue(self.account.half_open_probe("m1"))
        self.assertFalse(self.account.half_open_probe("m1"))

    def test_a_failed_probe_allows_the_next_one(self):
        self.account.half_open_probe("m1")
        self.account.half_open_settled(False, model="m1")
        self.assertTrue(self.account.half_open_probe("m1"))

    def test_a_successful_probe_keeps_the_account_usable(self):
        self.account.half_open_probe("m1")
        self.account.half_open_settled(True, model="m1")
        # 恢复期内直接放行，不再重复试探
        self.assertTrue(self.account.half_open_probe("m1"))
        self.assertTrue(self.account.half_open_probe("m1"))

    def test_models_are_half_opened_independently(self):
        """模型级冷却不该把整个账号的其它模型也挡住。"""
        self.assertTrue(self.account.half_open_probe("m1"))
        # 另一个模型是另一把钥匙
        self.assertTrue(self.account.half_open_probe("m2"))

    def test_a_stale_probe_does_not_block_the_account_forever(self):
        """试探迟迟没有结论（进程被杀、客户端断开）时，下一个请求能接手。"""
        self.account.half_open_probe("m1")
        # 把探测时刻推到很久以前，模拟「上一个试探没有结论」
        key = self.account._half_open_key("m1")
        with self.account._half_open_lock:
            self.account._half_open[key] = (
                time.time() - A.HALF_OPEN_PROBE_TIMEOUT - 1.0, False)
        self.assertTrue(self.account.half_open_probe("m1"))

    def test_clear_removes_the_state(self):
        self.account.half_open_probe("m1")
        self.account.half_open_clear("m1")
        self.assertTrue(self.account.half_open_probe("m1"))


class HalfOpenSelectionTests(unittest.TestCase):
    """选号层面的半开：冷却刚恢复的账号只放一个请求出去。"""

    def _pool(self, uids):
        pool = A.AccountPool.__new__(A.AccountPool)
        pool.accounts = [_account(u) for u in uids]
        pool._lock = threading.RLock()
        pool._cursor = 0
        pool._expiry_weights = {}
        pool._remaining_weights = {}
        pool._remaining_pick_state = {}
        pool._manual_weights = {}
        pool._page_cursors = {}
        pool._page_order_cache = None
        pool._inflight_provider = None
        pool._recovery_deadlines = {}
        return pool

    def test_a_never_cooled_account_is_not_half_opened(self):
        """没被冷却过的账号不需要半开 —— 否则第一次派发就被拦。"""
        pool = self._pool(["a", "b"])
        account = pool.accounts[0]
        self.assertFalse(pool._is_freshly_recovered(account, time.time()))

    def test_an_account_coming_out_of_cooldown_is_half_opened(self):
        pool = self._pool(["a"])
        account = pool.accounts[0]
        # 先让它处于冷却中
        account.cooldown_until = time.time() + 60
        self.assertFalse(pool._is_freshly_recovered(account, time.time()))
        # 冷却到期后
        account.cooldown_until = 0
        self.assertTrue(pool._is_freshly_recovered(account, time.time()))

    def test_an_account_cooled_again_is_not_half_opened(self):
        """重新被冷却（到期时刻后移）不该被当成「刚恢复」。"""
        pool = self._pool(["a"])
        account = pool.accounts[0]
        account.cooldown_until = time.time() + 60
        pool._is_freshly_recovered(account, time.time())
        # 又被冷却了一次，窗口更长
        account.cooldown_until = time.time() + 600
        self.assertFalse(pool._is_freshly_recovered(account, time.time()))

    def test_a_healthy_account_is_still_picked(self):
        """半开机制不能影响正常派发。"""
        pool = self._pool(["a", "b"])
        chosen = pool.pick(realm="cn")
        self.assertIsNotNone(chosen)

    def test_the_pool_never_returns_none_for_healthy_accounts(self):
        pool = self._pool(["a", "b", "c"])
        for _ in range(5):
            self.assertIsNotNone(pool.pick(realm="cn"))

    def test_a_recovered_account_yields_to_healthy_ones(self):
        """半开的账号先让路 —— 它还没被证明能用。

        反过来做（只要有半开账号就选它）会让刚解冻的号**独占**所有请求，
        正好是半开探测要避免的那种「压上一个号」。
        """
        pool = self._pool(["a", "b"])
        cooled = pool.accounts[0]
        cooled.cooldown_until = time.time() + 60
        pool._is_freshly_recovered(cooled, time.time())
        cooled.cooldown_until = 0            # 冷却到期
        self.assertTrue(pool._is_freshly_recovered(cooled, time.time()))
        # b 从没冷却过，是健康的
        chosen = pool.pick(realm="cn")
        self.assertEqual("b", chosen.uid)

    def test_when_all_are_half_open_only_one_probe_gets_through(self):
        """全都半开时，每个账号各自放一个出去试探（不是一个都不放）。

        半开的令牌是**按账号**的：每个刚恢复的账号有自己的试探机会，
        因为它们是彼此独立的凭据 —— 一个账号在试探，不该挡住另一个账号。
        真正要防的是「同一个账号被反复压上」，那由按账号的令牌保证。
        """
        pool = self._pool(["a", "b"])
        for account in pool.accounts:
            account.cooldown_until = time.time() + 60
            pool._is_freshly_recovered(account, time.time())
            account.cooldown_until = 0
        first = pool.pick(realm="cn")
        self.assertIsNotNone(first)
        # 同一个账号不会被连着放两个：它的令牌已被占用。
        self.assertFalse(first.half_open_probe(None))
        # 另一个账号仍可各试探一个。
        other = [a for a in pool.accounts if a is not first][0]
        self.assertTrue(other.half_open_probe(None))

    def test_a_successful_probe_stops_the_account_from_yielding(self):
        """试探成功后账号被证明能用 ⇒ 不再是半开候选。"""
        pool = self._pool(["a", "b"])
        cooled = pool.accounts[0]
        cooled.cooldown_until = time.time() + 60
        pool._is_freshly_recovered(cooled, time.time())
        cooled.cooldown_until = 0
        self.assertTrue(pool._is_freshly_recovered(cooled, time.time()))
        # 试探成功
        cooled.half_open_settled(True, model=None)
        self.assertTrue(cooled.half_open_is_recovered())
        # 它不再是半开候选
        self.assertFalse(pool._is_freshly_recovered(cooled, time.time()))

    def test_the_recovery_marker_expires(self):
        """恢复标记有 TTL：很久没验证的账号值得再探一次。"""
        account = _account("u-exp")
        account.half_open_settled(True, model=None)
        self.assertTrue(account.half_open_is_recovered())
        key = account._half_open_key(None)
        with account._half_open_lock:
            account._half_open[key] = (
                time.time() - A.HALF_OPEN_RECOVERED_TTL - 1.0, True)
        self.assertFalse(account.half_open_is_recovered())


if __name__ == "__main__":
    unittest.main()
