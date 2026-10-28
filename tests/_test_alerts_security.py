# -*- coding: utf-8 -*-
"""告警通道的安全约束（v0.1.6 安全加固）。

背景（2026-10-27 安全审计）：v0.1.5 新增的告警通道让网关能向**面板用户填的
任意 URL** 发 HTTP 请求 —— 这是全项目第一条「外部输入 → 内部网络」的通道。
审计确认可打回环 / 私网 / 链路本地（云元数据）/ `file://`，且默认 opener
**跟随跳转**（公网 URL 302 到内网即可绕过字符串检查）。

本文件锁住这些约束。
"""
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_alerts
import wb_settings

#: 解析 `example.com` 时给出的**公网**地址。
#:
#: 域名要判定成「公网、允许」，就得**解析得出来** —— 而 v0.1.0 把告警的
#: 域名解析改成 fail closed（解析不出来 ⇒ 判定为内网 ⇒ 拒绝）。于是这里
#: 不能依赖真实 DNS：解析不出来时「公网域名应当被允许」这条断言会反过来
#: 变成「被拒绝」，测试就成了「有没有网」的决定。
#:
#: 93.184.216.34 是 example.com 的公网地址段，判定上与真实解析结果一致。
FAKE_PUBLIC_ADDR = "93.184.216.34"


def fake_getaddrinfo(host, port, *args, **kwargs):
    """把域名解析固定到一个公网地址，不做真实的 DNS 查询。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FAKE_PUBLIC_ADDR, 0))]


def setUpModule():
    """整个模块期间，域名解析固定成公网地址（不做真实 DNS 查询）。"""
    patcher = mock.patch.object(wb_alerts.socket, "getaddrinfo",
                                fake_getaddrinfo)
    patcher.start()
    global _dns_patcher
    _dns_patcher = patcher


def tearDownModule():
    _dns_patcher.stop()


class WebhookUrlGuardTest(unittest.TestCase):
    """webhook URL 的地址校验。"""

    def assertRejected(self, url, why=""):
        with self.assertRaises(ValueError, msg="应当拒绝: %s (%s)" % (url, why)):
            wb_alerts._assert_webhook_url(url)

    def test_public_https_is_allowed(self):
        url = wb_alerts._assert_webhook_url("https://example.com/hook")
        self.assertEqual(url, "https://example.com/hook")

    def test_public_http_is_allowed(self):
        self.assertTrue(wb_alerts._assert_webhook_url("http://example.com/hook"))

    # ---- scheme ----

    def test_file_scheme_is_rejected(self):
        """file:// 能读本地文件 —— urllib 默认 opener 带 FileHandler。"""
        self.assertRejected("file:///etc/passwd", "本地文件读取")

    def test_ftp_and_data_schemes_are_rejected(self):
        self.assertRejected("ftp://example.com/x", "其它协议栈")
        self.assertRejected("data:text/plain,hello", "data URI")

    def test_empty_url_is_rejected(self):
        self.assertRejected("", "空")
        self.assertRejected("   ", "空白")

    # ---- 内网 / 回环 / 元数据 ----

    def test_loopback_is_rejected(self):
        self.assertRejected("http://127.0.0.1:8788/panel/status", "回环")
        self.assertRejected("http://127.0.0.1/", "回环")
        self.assertRejected("http://localhost:8788/", "回环")
        self.assertRejected("http://[::1]:8788/", "IPv6 回环")

    def test_cloud_metadata_is_rejected(self):
        self.assertRejected("http://169.254.169.254/latest/meta-data/", "云元数据")

    def test_private_ranges_are_rejected(self):
        for url in ("http://10.0.0.1/", "http://172.16.0.1/",
                    "http://192.168.1.1/", "http://100.64.0.1/"):
            self.assertRejected(url, "私网")

    def test_localhost_aliases_are_rejected(self):
        self.assertRejected("http://foo.local/hook", ".local")
        self.assertRejected("http://foo.internal/hook", ".internal")

    # ---- 结构 ----

    def test_url_with_userinfo_is_rejected(self):
        """userinfo 既可能泄露凭据，也是绕过主机判定的写法。"""
        self.assertRejected("http://user:pass@example.com/hook", "userinfo")

    def test_overlong_url_is_rejected(self):
        self.assertRejected("https://example.com/" + "a" * 4096, "超长")

    def test_url_without_host_is_rejected(self):
        self.assertRejected("https:///path", "无主机名")


class AlertThresholdTest(unittest.TestCase):
    """阈值必须有界：太大 → 告警永远不发；太小 → 永远发。"""

    def test_default_thresholds(self):
        out = wb_settings._alert_thresholds({})
        self.assertEqual(out, {"min_ready": 8, "for_ticks": 2, "clear_ticks": 2})

    def test_upper_bound_is_enforced(self):
        out = wb_settings._alert_thresholds({
            "min_ready": 10 ** 9, "for_ticks": 10 ** 9, "clear_ticks": 10 ** 9})
        self.assertEqual(out["min_ready"], 1000)
        self.assertEqual(out["for_ticks"], 100)
        self.assertEqual(out["clear_ticks"], 100)

    def test_lower_bound_is_enforced(self):
        out = wb_settings._alert_thresholds({"min_ready": -5, "for_ticks": 0})
        self.assertEqual(out["min_ready"], 1)
        self.assertEqual(out["for_ticks"], 1)

    def test_non_numeric_falls_back(self):
        out = wb_settings._alert_thresholds({"min_ready": "abc", "for_ticks": None})
        self.assertEqual(out["min_ready"], 8)
        self.assertEqual(out["for_ticks"], 2)

    def test_bool_is_not_an_int(self):
        """bool 是 int 的子类 —— True 不该被当成 1。"""
        out = wb_settings._alert_thresholds({"min_ready": True})
        self.assertEqual(out["min_ready"], 8)

    def test_unknown_keys_are_dropped(self):
        """白名单：thresholds 里塞别的键不该被回显。"""
        out = wb_settings._alert_thresholds({"min_ready": 5, "evil": "x"})
        self.assertNotIn("evil", out)
        self.assertEqual(out["min_ready"], 5)

    def test_proxy_level_int_helper_handles_overflow(self):
        """int(1e400) 抛 OverflowError —— 不接住会一路冒到 socket handler。"""
        import wb_proxy
        self.assertEqual(wb_proxy._clamp_alert_int(1e400, 8, 1, 1000), 8)
        self.assertEqual(wb_proxy._clamp_alert_int("abc", 8, 1, 1000), 8)
        self.assertEqual(wb_proxy._clamp_alert_int(True, 8, 1, 1000), 8)
        self.assertEqual(wb_proxy._clamp_alert_int(5, 8, 1, 1000), 5)
        self.assertEqual(wb_proxy._clamp_alert_int(99999, 8, 1, 1000), 1000)


class AlertSettingsTest(unittest.TestCase):
    """落盘前的纵深防御：任何调用方都拿不到未校验的 dict。"""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="wb-alert-settings-")

    def test_public_view_never_carries_the_secret(self):
        wb_settings.set_alerts(self.dir, {
            "webhook": {"url": "https://example.com/hook", "secret": "s3cr3t-value"},
            "serverchan": {"key": "SCTkeyvalue"},
        })
        view = wb_settings.alerts(self.dir)
        self.assertNotIn("s3cr3t-value", repr(view))
        self.assertNotIn("SCTkeyvalue", repr(view))
        self.assertTrue(view["webhook"])
        self.assertTrue(view["serverchan"])

    def test_saved_file_keeps_the_secret(self):
        wb_settings.set_alerts(self.dir, {
            "webhook": {"url": "https://example.com/hook", "secret": "s3cr3t-value"}})
        with open(os.path.join(self.dir, "settings.json"), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertIn("s3cr3t-value", raw)   # 要能用，所以要存

    def test_overlong_url_is_refused(self):
        with self.assertRaises(ValueError):
            wb_settings.set_alerts(self.dir, {
                "webhook": {"url": "https://example.com/" + "a" * 4096}})

    def test_overlong_secret_is_refused(self):
        with self.assertRaises(ValueError):
            wb_settings.set_alerts(self.dir, {
                "webhook": {"url": "https://example.com/h", "secret": "s" * 1000}})

    def test_unknown_threshold_keys_are_not_persisted(self):
        wb_settings.set_alerts(self.dir, {
            "webhook": {"url": "https://example.com/h"},
            "thresholds": {"min_ready": 5, "evil": "x"}})
        with open(os.path.join(self.dir, "settings.json"), encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("evil", raw)


class SafeErrorTextTest(unittest.TestCase):
    """投递错误不能带出响应体 —— 那是 SSRF 读。"""

    def test_http_error_keeps_only_the_status(self):
        import urllib.error
        exc = urllib.error.HTTPError(
            "http://x/", 500, "Internal Server Error", {}, None)
        text = wb_alerts._safe_error_text(exc)
        self.assertIn("500", text)
        self.assertNotIn("secret-body", text)

    def test_generic_exception_yields_the_type_name(self):
        text = wb_alerts._safe_error_text(RuntimeError("boom"))
        self.assertNotIn("boom", text)
        self.assertIn("RuntimeError", text)


class NoRedirectTest(unittest.TestCase):
    """默认 opener 跟随跳转 —— 公网 URL 302 到内网即可绕过地址检查。"""

    def test_redirect_handler_refuses_redirects(self):
        handler = wb_alerts._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            None, None, 302, "Found", {}, "http://127.0.0.1/"))


if __name__ == "__main__":
    unittest.main()
