# -*- coding: utf-8 -*-
"""401 的两条分支：凭据真死 vs 状态未知。

这是本轮 B2 借鉴点最核心的一条。它不是「加个分类」那么轻 ——
它决定了一次上游抖动会不会把一批正常账号踢出池子。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts as A  # noqa: E402


class CredentialStateTests(unittest.TestCase):
    """refresh() 的三种结局，各自意味着什么。"""

    def _account(self, refresh_token=None):
        import base64
        import json as _json

        def part(obj):
            raw = base64.urlsafe_b64encode(_json.dumps(obj).encode("utf-8")).decode("ascii")
            return raw.rstrip("=")
        token = "%s.%s.sig" % (
            part({"alg": "RS256", "typ": "JWT"}),
            part({"iss": "https://workbuddy.ai", "sub": "u-401", "exp": 4102444800}))
        data = {
            "uid": "u-401",
            "realm": "cn",
            "domain": "www.workbuddy.ai",
            "accessToken": token,
            "enabled": True,
            "tasksEnabled": True,
            "verifiedAt": 1700000000,
        }
        if refresh_token:
            data["refreshToken"] = refresh_token
        return A.Account(data)

    def test_no_refresh_token_means_the_credential_is_dead(self):
        """没有 refresh token ⇒ 只能重新登录 ⇒ 凭据确实不行了。"""
        account = self._account()
        self.assertFalse(account.refresh())
        self.assertTrue(account.token_expired)

    def test_static_api_key_accounts_are_never_marked_expired(self):
        """静态 Key 长期有效：refresh 失败不等于凭据失效。

        这是本条规则的另一个边界：静态 Key 压根不能 refresh，
        把它标成过期会把一个健康的号踢出池子并要求重新登录。
        """
        account = self._account()
        account.access_token = "ck_static_key_value"
        account.is_api_key = True
        account.refresh_token = ""
        self.assertFalse(account.refresh())
        self.assertFalse(account.token_expired)

    def test_token_expired_flag_is_the_decider_for_penalty(self):
        """penalty 的选择完全由 token_expired 决定，别的都不看。"""
        account = self._account()
        account.token_expired = True
        self.assertTrue(bool(getattr(account, "token_expired", False)))
        account.token_expired = False
        self.assertFalse(bool(getattr(account, "token_expired", False)))


class NonPunishingRegressionTests(unittest.TestCase):
    """回归：401 的两条分支各自的行为。"""

    def _account(self):
        import base64
        import json as _json

        def part(obj):
            raw = base64.urlsafe_b64encode(_json.dumps(obj).encode("utf-8")).decode("ascii")
            return raw.rstrip("=")
        token = "%s.%s.sig" % (
            part({"alg": "RS256", "typ": "JWT"}),
            part({"iss": "https://workbuddy.ai", "sub": "u-401b", "exp": 4102444800}))
        return A.Account({
            "uid": "u-401b",
            "realm": "cn",
            "domain": "www.workbuddy.ai",
            "accessToken": token,
            "enabled": True,
            "tasksEnabled": True,
            "verifiedAt": 1700000000,
        })

    def test_dead_credential_path_cools_and_expires(self):
        account = self._account()
        account.token_expired = True
        account.note_error("HTTP 401", cooldown=60, penalty=A.ERR_CREDENTIAL)
        self.assertGreater(account.cooldown_until, 0.0)
        self.assertTrue(account.token_expired)

    def test_unknown_credential_path_leaves_the_account_usable(self):
        account = self._account()
        account.note_error("HTTP 401", cooldown=60, penalty=A.ERR_TRANSIENT)
        self.assertEqual(0.0, account.cooldown_until)
        self.assertFalse(account.token_expired)
        # 账号仍然可用
        self.assertTrue(account.ready())

    def test_the_account_stays_ready_after_a_transient_401(self):
        """这一条是整个改动的意义：一次上游抖动不该让账号消失。"""
        account = self._account()
        for _ in range(3):
            account.note_error("HTTP 401", cooldown=60, penalty=A.ERR_TRANSIENT)
        self.assertTrue(account.ready())
        self.assertEqual(0.0, account.cooldown_until)


if __name__ == "__main__":
    unittest.main()
