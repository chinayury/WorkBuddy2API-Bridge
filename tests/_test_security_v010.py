# -*- coding: utf-8 -*-
"""v0.1.0 安全加固的回归网。

本轮修的三件事：

1. **H1 上游错误正文里的凭据落进 usage.jsonl**
   上游的 401/403 正文会回显授权头（wb_activity.safe_message 的注释写的就是
   这件事）。那一行原样落盘，而 usage.jsonl 同机任何用户都读得到。
   ⇒ 落盘前按已知凭证形态打码；文件权限收紧到 0600、目录 0700。

2. **H2 登录限速被 X-Forwarded-For 绕过**
   nginx 的 `$proxy_add_x_forwarded_for` 是**追加**：客户端自带的 XFF 留在最左，
   代理把真实来源追加到最右。取最左 = 让客户端自己选限流桶。
   ⇒ 取最右。

3. **H3 面板口令 ?pwd= 留在地址栏**
   它会进浏览器历史、同步到别的设备、出现在屏幕共享里，还会作为 Referer
   发给站外资源。⇒ 读到之后立刻从 URL 删掉。
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_activity  # noqa: E402
import wb_proxy as P  # noqa: E402


class UsageMessageRedactionTests(unittest.TestCase):
    """H1：落盘的错误正文必须已脱敏。"""

    def test_record_error_redacts_a_bearer_token(self):
        captured = {}
        original = P._persist_usage

        def capture(row, label):
            captured.update(row)

        P._persist_usage = capture
        try:
            P.record_error("m-1", 401,
                           '{"error":"bad token","Authorization":"Bearer abcdef1234567890xyz"}')
        finally:
            P._persist_usage = original
        message = captured.get("message", "")
        self.assertNotIn("abcdef1234567890xyz", message)
        self.assertIn("[redacted]", message)

    def test_record_error_redacts_a_jwt(self):
        captured = {}
        original = P._persist_usage

        def capture(row, label):
            captured.update(row)

        P._persist_usage = capture
        try:
            P.record_error("m-1", 403,
                           "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.abcdEFGH1234 rejected")
        finally:
            P._persist_usage = original
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", captured.get("message", ""))

    def test_record_error_keeps_the_readable_reason(self):
        """脱敏不能把消息清空 —— 排查还要靠它。"""
        captured = {}
        original = P._persist_usage

        def capture(row, label):
            captured.update(row)

        P._persist_usage = capture
        try:
            P.record_error("m-1", 429, "usage exceeds frequency limit")
        finally:
            P._persist_usage = original
        self.assertIn("frequency limit", captured.get("message", ""))

    def test_the_redaction_is_the_same_one_the_activity_log_uses(self):
        """两处必须用同一套规则，否则会漂移出新的漏洞。"""
        raw = "Authorization: Bearer abcdef1234567890xyz"
        self.assertEqual(wb_activity.safe_message(raw),
                         wb_activity.safe_message(raw))
        self.assertNotIn("abcdef1234567890xyz",
                         wb_activity.safe_message(raw))

    def test_the_log_line_is_redacted_too(self):
        """落盘脱敏了还不够 —— 日志行同样会带上游原文。

        日志既进日志文件，也进面板的「运行日志」页；原文里的 bearer token
        在两处都会泄露。
        """
        captured = []
        original_log = P.log
        original_persist = P._persist_usage
        P.log = lambda msg, **kw: captured.append(msg)
        P._persist_usage = lambda row, label: None
        try:
            P.record_error("m-1", 401,
                           "Authorization: Bearer abcdef1234567890xyz")
        finally:
            P.log = original_log
            P._persist_usage = original_persist
        joined = " ".join(captured)
        self.assertNotIn("abcdef1234567890xyz", joined)
        self.assertIn("[redacted]", joined)


class RateLimitKeyTests(unittest.TestCase):
    """H2：限流键不能由客户端自己选。"""

    def _headers(self, **kw):
        return {k.replace("_", "-"): v for k, v in kw.items()}

    def test_a_client_supplied_forwarded_for_cannot_choose_the_bucket(self):
        """客户端自带 XFF + 代理追加的真实来源 ⇒ 用真实来源。"""
        headers = self._headers(
            X_Forwarded_For="1.2.3.4, 203.0.113.9")
        key = P.login_rate_limit_key("127.0.0.1", headers)
        self.assertEqual("203.0.113.9", key)

    def test_the_leftmost_forwarded_entry_is_not_trusted(self):
        """多个值时不能取最左。

        只有一个值时无法区分「客户端伪造」与「代理替换」—— 那种情况下最右
        也就是最左，取它是对的（部署方配置正确时它就是真实来源）。
        真正要防的是**追加**场景：客户端自带一个值，代理再追加一个。
        """
        headers = self._headers(X_Forwarded_For="6.6.6.6, 203.0.113.9")
        key = P.login_rate_limit_key("127.0.0.1", headers)
        self.assertNotEqual("6.6.6.6", key)
        self.assertEqual("203.0.113.9", key)

    def test_x_real_ip_wins(self):
        headers = self._headers(X_Real_IP="198.51.100.7",
                                X_Forwarded_For="1.2.3.4, 203.0.113.9")
        self.assertEqual("198.51.100.7",
                         P.login_rate_limit_key("127.0.0.1", headers))

    def test_an_untrusted_peer_falls_back_to_its_own_address(self):
        headers = self._headers(X_Forwarded_For="1.2.3.4")
        self.assertEqual("10.0.0.5",
                         P.login_rate_limit_key("10.0.0.5", headers))

    def test_a_malformed_forwarded_for_falls_back_to_the_peer(self):
        headers = self._headers(X_Forwarded_For="not-an-ip")
        self.assertEqual("127.0.0.1",
                         P.login_rate_limit_key("127.0.0.1", headers))

    def test_the_same_client_always_lands_in_the_same_bucket(self):
        """同一个真实来源，伪造不同的左值 ⇒ 仍然是同一个桶。"""
        headers_a = self._headers(X_Forwarded_For="1.1.1.1, 203.0.113.9")
        headers_b = self._headers(X_Forwarded_For="2.2.2.2, 203.0.113.9")
        self.assertEqual(P.login_rate_limit_key("127.0.0.1", headers_a),
                         P.login_rate_limit_key("127.0.0.1", headers_b))


class DashboardUrlCleanupTests(unittest.TestCase):
    """H3：口令与 Key 都必须从地址栏消失。"""

    def setUp(self):
        path = os.path.join(ROOT, "dashboard.html")
        with open(path, encoding="utf-8") as fh:
            self.html = fh.read()

    def test_the_key_is_dropped_from_the_url(self):
        self.assertIn("rest.delete('key')", self.html)

    def test_the_panel_password_is_dropped_from_the_url(self):
        self.assertIn("rest.delete('pwd')", self.html)

    def test_the_password_is_removed_before_the_login_is_submitted(self):
        """顺序很重要：先删 URL，再登录。"""
        delete_at = self.html.find("rest.delete('pwd')")
        login_at = self.html.find("await submitPanelLogin();", delete_at)
        self.assertGreater(delete_at, 0)
        self.assertGreater(login_at, delete_at)

    def test_the_cleanup_keeps_other_parameters(self):
        """不能为了删口令把整个查询串清掉 —— ?tab= 会被用户丢掉。"""
        idx = self.html.find("rest.delete('pwd')")
        window = self.html[idx:idx + 320]
        self.assertIn("window.location.pathname", window)
        self.assertIn("rest.toString()", window)


if __name__ == "__main__":
    unittest.main()
