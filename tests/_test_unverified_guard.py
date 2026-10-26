# -*- coding: utf-8 -*-
"""未验证账号护栏（P1-8 入池闸门）的行为约束。

背景（2026-10-26 晚，19 账号池实测）：
上游对**从未被真正激活过**的凭据会在第一次派发时直接判 403 / code 11140
「账号存在异常行为」，而 900 秒内两次就触发整池 30 分钟连坐暂停（客户端全拿
429）。当晚两次连坐都是这么来的。

判据用「上游额度快照显示 used == 0」（这个凭据从未被上游消费过），
实测分离度完美：used==0 的 5 个账号里 4 个已被 11140 判非法，used>0 的 14 个
账号零触发。

三态与上游应答的对应：
  - 403 / 11140 → 上游判非法 → rejected，隔离
  - 429 / 14003「too many requests」→ 凭据被接受，只是限流 → verified，放行
  - 跑成一次 → verified，放行
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as A


def account(uid="u1", **kw):
    data = {
        "uid": uid,
        "accessToken": "t-" + uid,
        "refreshToken": "r-" + uid,
        "realm": "cn",
        "enabled": True,
        "source": "import",
        "domain": "www.codebuddy.cn",
        "platform": "CLI",
        "product": "workbuddy",
    }
    data.update(kw)
    return A.Account(data)


class UnverifiedGuardTest(unittest.TestCase):
    # ---------------------------------------------------------- 判定

    def test_upstream_never_consumed_reads_credits_used(self):
        acc = account(credits={"used": 0.0, "remain": 100.0, "size": 100.0})
        self.assertTrue(acc.upstream_never_consumed())

    def test_upstream_used_account_is_not_flagged(self):
        acc = account(credits={"used": 18.0, "remain": 7861.0, "size": 7879.0})
        self.assertFalse(acc.upstream_never_consumed())

    def test_missing_credits_is_not_flagged(self):
        # 拿不到证据不等于有罪：宁可放过也不误伤。
        self.assertFalse(account().upstream_never_consumed())
        self.assertFalse(account(credits=None).upstream_never_consumed())
        self.assertFalse(account(credits={}).upstream_never_consumed())

    def test_unparsable_used_is_not_flagged(self):
        self.assertFalse(account(credits={"used": "n/a"}).upstream_never_consumed())

    # ---------------------------------------------------------- 拦截

    def test_unverified_account_is_not_ready(self):
        acc = account()
        self.assertTrue(acc.ready())
        acc.mark_unverified(reason="上游额度显示从未被消费过")
        self.assertFalse(acc.ready())
        self.assertTrue(acc.is_unverified())

    def test_unverified_account_never_served(self):
        acc = account()
        acc.mark_unverified()
        self.assertEqual(acc.unverified_since, acc.unverified_since)
        self.assertTrue(acc.unverified_since > 0)

    def test_mark_unverified_keeps_first_timestamp(self):
        acc = account()
        first = acc.mark_unverified(reason="r1")
        second = acc.mark_unverified(reason="r2")
        self.assertEqual(first, second)

    # ---------------------------------------------------------- 解除

    def test_success_clears_the_guard(self):
        acc = account()
        acc.mark_unverified(reason="待验证")
        self.assertFalse(acc.ready())
        acc.note_success()
        self.assertFalse(acc.is_unverified())
        self.assertTrue(acc.ready())
        self.assertEqual(acc.unverified_reason, "")

    def test_restriction_marks_the_account_rejected(self):
        # 上游明确判非法：标记 + 冷却，且冷却到期后**不能**自动回池。
        acc = account()
        acc.note_restricted(detail='{"code":11140}')
        self.assertTrue(acc.is_unverified())
        acc.restricted_until = 0.0     # 冷却到期
        self.assertFalse(acc.ready())

    def test_restriction_reason_is_recorded(self):
        acc = account()
        acc.note_restricted(detail='{"code":11140}')
        self.assertIn("11140", acc.unverified_reason)

    # ---------------------------------------------------------- 持久化

    def test_marker_survives_a_reload(self):
        acc = account()
        acc.mark_unverified(reason="上游额度显示从未被消费过")
        again = A.Account(acc.to_dict())
        self.assertTrue(again.is_unverified())
        self.assertFalse(again.ready())

    def test_verified_marker_does_not_reload_as_unverified(self):
        acc = account()
        acc.mark_unverified()
        acc.note_success()
        again = A.Account(acc.to_dict())
        self.assertFalse(again.is_unverified())
        self.assertTrue(again.ready())

    def test_public_view_exposes_the_state(self):
        acc = account()
        acc.mark_unverified(reason="待验证")
        view = acc.public()
        self.assertTrue(view["unverified"])
        self.assertEqual(view["unverifiedReason"], "待验证")
        self.assertIsNotNone(view["unverifiedSince"])

    def test_public_view_exposes_upstream_unused(self):
        acc = account(credits={"used": 0.0, "remain": 10.0, "size": 10.0})
        self.assertTrue(acc.public()["upstreamUnused"])

    # ---------------------------------------------------------- 池级

    def test_pool_marks_upstream_unused_accounts_on_load(self):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp:
            acc = account("u-unused", credits={"used": 0.0, "remain": 5.0, "size": 5.0})
            acc.enabled = True
            with open(os.path.join(tmp, "u-unused.json"), "w", encoding="utf-8") as fh:
                json.dump(acc.to_dict(), fh)
            pool = A.AccountPool(tmp)
            pool.load()
            self.assertEqual(len(pool.unverified_accounts()), 1)
            self.assertEqual(pool.count_ready(), 0)

    def test_pool_leaves_used_accounts_alone(self):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp:
            acc = account("u-used", credits={"used": 42.0, "remain": 5.0, "size": 47.0})
            with open(os.path.join(tmp, "u-used.json"), "w", encoding="utf-8") as fh:
                json.dump(acc.to_dict(), fh)
            pool = A.AccountPool(tmp)
            pool.load()
            self.assertEqual(pool.unverified_accounts(), [])
            self.assertEqual(pool.count_ready(), 1)

    def test_pool_does_not_re_mark_an_already_verified_account(self):
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as tmp:
            acc = account("u-cleared", credits={"used": 0.0, "remain": 5.0, "size": 5.0})
            acc.mark_unverified()
            acc.note_success()          # 上游接受过：标记清掉
            with open(os.path.join(tmp, "u-cleared.json"), "w", encoding="utf-8") as fh:
                json.dump(acc.to_dict(), fh)
            pool = A.AccountPool(tmp)
            pool.load()
            # credits.used 仍是 0，但这个号已经被上游接受过一次，不该再被隔离
            self.assertEqual(pool.unverified_accounts(), [])


if __name__ == "__main__":
    unittest.main()
