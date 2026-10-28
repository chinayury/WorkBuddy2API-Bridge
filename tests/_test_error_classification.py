# -*- coding: utf-8 -*-
"""上游错误分类（借鉴 Coding2API 的 ErrKind 体系）。

核心主张：**不是所有失败都该让账号背锅。**

改动的起因是 401：旧实现在刷新失败后无条件给账号记 60 秒冷却，
但「刷新因为网络抖动没成」与「上游明确判这个凭据失效」是完全不同的两件事。
把它们一视同仁地惩罚，代价是救火的时候又摘掉一批号 ——
2026-10-26 晚那次连坐事故里「停用正常账号」那一段就是这么来的。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_accounts as A  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_403_with_11140_is_a_credential_problem(self):
        """账号级风控：上游判这个凭据非法。"""
        detail = '{"code":11140,"msg":"request illegal"}'
        self.assertEqual(A.ERR_CREDENTIAL, A.classify_upstream_error(403, detail=detail))

    def test_429_is_a_model_problem(self):
        """限流是额度/频率维度，账号本身没问题。"""
        self.assertEqual(A.ERR_MODEL, A.classify_upstream_error(429, detail=""))

    def test_429_with_14003_is_still_a_model_problem(self):
        detail = '{"code":14003,"msg":"too many requests"}'
        self.assertEqual(A.ERR_MODEL, A.classify_upstream_error(429, detail=detail))

    def test_6004_is_a_model_problem(self):
        detail = '{"code":6004,"msg":"usage exceeds frequency limit"}'
        self.assertEqual(A.ERR_MODEL, A.classify_upstream_error(429, detail=detail))

    def test_400_is_a_request_problem(self):
        """客户端请求本身有问题 —— 账号完全无辜。"""
        self.assertEqual(A.ERR_REQUEST, A.classify_upstream_error(400, detail=""))

    def test_401_is_a_credential_problem(self):
        self.assertEqual(A.ERR_CREDENTIAL, A.classify_upstream_error(401, detail=""))

    def test_402_is_a_credential_problem(self):
        self.assertEqual(A.ERR_CREDENTIAL, A.classify_upstream_error(402, detail=""))

    def test_5xx_is_transient(self):
        for status in (500, 502, 503, 504):
            self.assertEqual(A.ERR_TRANSIENT,
                             A.classify_upstream_error(status, detail=""))

    def test_business_code_wins_over_the_http_status(self):
        """429 + 11140 与 403 + 14003 都不该按状态码判。"""
        # 限流优先：状态码 429 但 code 是额度类
        self.assertEqual(A.ERR_MODEL,
                         A.classify_upstream_error(429, code=14003, detail=""))

    def test_non_json_body_yields_no_code(self):
        self.assertIsNone(A._upstream_code_from_detail("not json at all"))
        self.assertIsNone(A._upstream_code_from_detail(""))
        self.assertIsNone(A._upstream_code_from_detail("[1,2,3]"))

    def test_code_is_read_from_json_not_from_a_bare_substring(self):
        """正文里出现「11140」这几个字符不等于 code 是 11140。"""
        self.assertIsNone(A._upstream_code_from_detail('{"msg":"error 11140 happened"}'))
        self.assertEqual(11140, A._upstream_code_from_detail('{"code":11140}'))

    def test_non_penalizing_set_covers_request_transient_local(self):
        self.assertIn(A.ERR_REQUEST, A.NON_PENALIZING)
        self.assertIn(A.ERR_TRANSIENT, A.NON_PENALIZING)
        self.assertIn(A.ERR_LOCAL_NETWORK, A.NON_PENALIZING)
        self.assertNotIn(A.ERR_CREDENTIAL, A.NON_PENALIZING)
        self.assertNotIn(A.ERR_MODEL, A.NON_PENALIZING)


class PenaltyApplicationTests(unittest.TestCase):
    """note_error 的 penalty 参数：不惩罚的分类真的不冷却。"""

    def _account(self):
        import base64
        import json as _json

        def part(obj):
            raw = base64.urlsafe_b64encode(_json.dumps(obj).encode("utf-8")).decode("ascii")
            return raw.rstrip("=")
        token = "%s.%s.sig" % (
            part({"alg": "RS256", "typ": "JWT"}),
            part({"iss": "https://workbuddy.ai", "sub": "u-1", "exp": 4102444800}))
        return A.Account({
            "uid": "u-penalty",
            "realm": "cn",
            "domain": "www.workbuddy.ai",
            "accessToken": token,
            "enabled": True,
            "tasksEnabled": True,
            "verifiedAt": 1700000000,
        })

    def test_request_error_does_not_cool_the_account(self):
        account = self._account()
        account.note_error("HTTP 400: bad request", cooldown=60,
                           penalty=A.ERR_REQUEST)
        self.assertEqual(0.0, account.cooldown_until)
        # 失败要看得见
        self.assertIn("400", account.last_error)

    def test_credential_error_does_cool_the_account(self):
        account = self._account()
        account.note_error("HTTP 401", cooldown=60, penalty=A.ERR_CREDENTIAL)
        self.assertGreater(account.cooldown_until, 0.0)

    def test_transient_error_does_not_cool_the_account(self):
        account = self._account()
        account.note_error("connection reset", cooldown=60,
                           penalty=A.ERR_TRANSIENT)
        self.assertEqual(0.0, account.cooldown_until)

    def test_local_network_error_does_not_cool_the_account(self):
        account = self._account()
        account.note_error("network down", cooldown=60,
                           penalty=A.ERR_LOCAL_NETWORK)
        self.assertEqual(0.0, account.cooldown_until)

    def test_non_penalizing_does_not_expire_the_token(self):
        """401 文本在非惩罚分类下不该把凭据标成过期。

        否则一次网络抖动就会把好凭据踢出池子 —— 那正是我们要修的误伤。
        """
        account = self._account()
        account.note_error("HTTP 401", cooldown=60, penalty=A.ERR_TRANSIENT)
        self.assertFalse(account.token_expired)

    def test_credential_penalty_does_expire_the_token(self):
        account = self._account()
        account.note_error("HTTP 401", cooldown=60, penalty=A.ERR_CREDENTIAL)
        self.assertTrue(account.token_expired)

    def test_default_penalty_is_credential(self):
        """不传 penalty 时保持旧行为 —— 分类是显式的 opt-in，不是行为变更。"""
        account = self._account()
        account.note_error("HTTP 401", cooldown=60)
        self.assertGreater(account.cooldown_until, 0.0)
        self.assertTrue(account.token_expired)


if __name__ == "__main__":
    unittest.main()
