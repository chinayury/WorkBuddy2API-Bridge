# -*- coding: utf-8 -*-
"""在途感知选号：并发请求应该被分摊到不同账号上。

背景（借鉴来源：Coding2API 的调度器排序链）
池子原有的偏好 —— 手动权重、临期积分、剩余用量 —— 全都是**静态**属性。
静态偏好之下，并发请求会自发收敛到同一个账号上：上游按「账号 + 分钟」计
token，两个会话落在同一账号上很容易冲破配额然后 429，换号之后又因为丢了
前缀缓存而更慢。

本组用例钉住三件事：
  1. 注入在途 provider 后，选号会避开在途数高的账号；
  2. 没有注入 provider 时，选号与改动前完全一致（纯轮询）；
  3. provider 抛异常 / 返回垃圾值时，选号不崩，退回纯轮询。
"""
import base64
import json
import os
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts  # noqa: E402

INTL_ISS = "https://workbuddy.ai"


def _jwt(sub="u-1"):
    """一个带 exp 的最小 JWT —— 账号的 access_token 必须能被解析出有效期，
    否则 Account 会认为它已过期，ready() 恒假。"""
    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
        return raw.rstrip("=")
    return "%s.%s.sig" % (part({"alg": "RS256", "typ": "JWT"}),
                          part({"iss": INTL_ISS, "sub": sub, "exp": 4102444800}))


def _make_account(uid, realm="cn", weight=1):
    return wb_accounts.Account({
        "uid": uid,
        "realm": realm,
        "domain": "www.workbuddy.ai",
        "accessToken": _jwt(uid),
        "enabled": True,
        "tasksEnabled": True,
        "weight": weight,
        # verifiedAt > 0 让账号绕过「未验证」闸门（那个闸门看的是
        # credits.used == 0 且 verifiedAt == 0）。
        "verifiedAt": 1700000000,
    })


class InflightAwarePickTests(unittest.TestCase):
    def _pool(self, uids):
        pool = wb_accounts.AccountPool.__new__(wb_accounts.AccountPool)
        pool.accounts = [_make_account(u) for u in uids]
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

    def test_picks_the_least_busy_account(self):
        pool = self._pool(["a", "b", "c"])
        load = {"a": 5, "b": 0, "c": 2}
        pool.set_inflight_provider(lambda uid: load.get(uid, 0))
        chosen = pool.pick(realm="cn")
        self.assertIsNotNone(chosen)
        self.assertEqual("b", chosen.uid)

    def test_no_provider_keeps_plain_rotation(self):
        """没注入 provider 时，行为必须与改动前一字不差。"""
        pool = self._pool(["a", "b", "c"])
        seen = [pool.pick(realm="cn").uid for _ in range(6)]
        # 纯轮询：每个账号轮着来
        self.assertEqual(["a", "b", "c", "a", "b", "c"], seen)

    def test_broken_provider_does_not_crash_the_pick(self):
        pool = self._pool(["a", "b"])

        def boom(uid):
            raise RuntimeError("provider exploded")

        pool.set_inflight_provider(boom)
        chosen = pool.pick(realm="cn")
        self.assertIsNotNone(chosen)
        self.assertIn(chosen.uid, ("a", "b"))

    def test_garbage_provider_value_is_treated_as_zero(self):
        pool = self._pool(["a", "b"])
        pool.set_inflight_provider(lambda uid: "not-a-number")
        chosen = pool.pick(realm="cn")
        self.assertIsNotNone(chosen)

    def test_negative_inflight_is_clamped_to_zero(self):
        pool = self._pool(["a", "b"])
        pool.set_inflight_provider(lambda uid: -5)
        chosen = pool.pick(realm="cn")
        self.assertIsNotNone(chosen)

    def test_all_busy_still_returns_an_account(self):
        """全都忙的时候也必须返回一个账号 —— 池子不能因为忙就空转。"""
        pool = self._pool(["a", "b"])
        pool.set_inflight_provider(lambda uid: 7)
        chosen = pool.pick(realm="cn")
        self.assertIsNotNone(chosen)

    def test_excluded_account_is_not_picked(self):
        pool = self._pool(["a", "b"])
        pool.set_inflight_provider(lambda uid: 0)
        chosen = pool.pick(realm="cn", exclude={"a"})
        self.assertIsNotNone(chosen)
        self.assertNotEqual("a", chosen.uid)

    def test_unregistering_the_provider_restores_plain_rotation(self):
        pool = self._pool(["a", "b"])
        pool.set_inflight_provider(lambda uid: 9)
        pool.set_inflight_provider(None)
        seen = [pool.pick(realm="cn").uid for _ in range(4)]
        self.assertEqual(["a", "b", "a", "b"], seen)


class LoadCounterTests(unittest.TestCase):
    """wb_proxy 侧的实时在途计数器。"""

    def setUp(self):
        import wb_proxy
        self.wb_proxy = wb_proxy
        wb_proxy._ACCOUNT_LOAD.clear()

    def test_acquire_and_release_balance(self):
        wp = self.wb_proxy
        wp.account_load_acquire("u1")
        wp.account_load_acquire("u1")
        self.assertEqual(2, wp.account_load_count("u1"))
        wp.account_load_release("u1")
        self.assertEqual(1, wp.account_load_count("u1"))
        wp.account_load_release("u1")
        self.assertEqual(0, wp.account_load_count("u1"))
        # 归零后不应留下条目
        self.assertNotIn("u1", wp._ACCOUNT_LOAD)

    def test_release_without_acquire_is_harmless(self):
        wp = self.wb_proxy
        wp.account_load_release("never-acquired")
        self.assertEqual(0, wp.account_load_count("never-acquired"))

    def test_count_of_unknown_account_is_zero(self):
        self.assertEqual(0, self.wb_proxy.account_load_count("nobody"))

    def test_inflight_count_reads_the_load_counter(self):
        """account_inflight_count 必须读 _ACCOUNT_LOAD，而不是名额计数器。

        名额计数器在上限关闭时根本不记账；负载计数必须始终可用，
        否则整个在途感知选号形同虚设。
        """
        wp = self.wb_proxy
        wp.account_load_acquire("u2")
        self.assertEqual(1, wp.account_inflight_count("u2"))
        # 名额计数器此刻是空的
        self.assertEqual(0, wp._ACCOUNT_INFLIGHT.get("u2", 0))


if __name__ == "__main__":
    unittest.main()
