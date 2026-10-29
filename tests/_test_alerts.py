# -*- coding: utf-8 -*-
"""账号池告警通道（wb_alerts）的行为约束。

背景：2026-10-26 晚到 10-27 凌晨，生产上两次「几乎出事」的事故都是靠人写的
一次性脚本才发现的 —— 网关自己一声不吭。这个模块补的就是这个洞。

四条底线：
1. **默认全关** —— 没配 webhook / ServerChan 时，绝不发任何网络请求。
2. **不阻塞** —— 告警走后台线程 + 有界队列；队列满就丢并计数。
3. **去抖** —— 同一条件连续 N 轮命中才发故障，连续 M 轮恢复才发恢复。
4. **secret 只写不读** —— 面板回读永远拿不到 secret / key 的明文。
"""
import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_alerts
import wb_settings

#: 解析 `example.com` 时给出的**公网**地址。
#:
#: 这里必须固定住，原因有两个：
#:
#: 1. **测试不该联网。** 告警的后台投递线程是异步的 —— 某个测试把
#:    `_post` 换回真实实现之后，worker 才从队列里取走那一项，于是真的去
#:    连 example.com。测试因此要等真实的 HTTPS 往返（CI 上 4 秒），
#:    进程退出码还会被解释器收尾时的网络错误带成非 0
#:    （套件自己报 OK，run_all 却判失败）。
#: 2. **DNS 失败现在会被拒绝。** v0.1.0 把告警的域名解析改成 fail closed
#:    （解析不出来 ⇒ 判定为内网 ⇒ 拒绝），所以测试用的域名必须**解析得出来**，
#:    又不能是真实解析 —— 于是固定成一个公网地址。
#:
#: 93.184.216.34 是 example.com 的公网地址段，判定上与真实解析结果一致。
FAKE_PUBLIC_ADDR = "93.184.216.34"


def fake_getaddrinfo(host, port, *args, **kwargs):
    """把域名解析固定到一个公网地址，不做真实的 DNS 查询。"""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (FAKE_PUBLIC_ADDR, 0))]


#: 真实投递函数在模块运行期间被换掉；这是换掉之前的那一份。
_real_post = None


def _safe_post(url, body, headers=None, timeout=None):
    """兜底的假投递：worker 线程即使晚到，也绝不会真的发请求。"""
    return b"ok"


def setUpModule():
    """整个模块期间，投递一律走假实现。

    告警的投递是**后台线程**做的：某个测试把 `_post` 换回真实实现之后，
    worker 才从队列里取走那一项，于是真的去连 example.com。测试因此要等
    真实的 HTTPS 往返，进程退出码还会被解释器收尾时的网络错误带成非 0
    （套件自己报 OK，run_all 却判失败）。

    ⇒ 模块级别把 `_post` 换成假的：worker 无论什么时候取到队列项，
    都不会出网。投递行为由各测试自己替换 `_post` 来观察。
    """
    global _real_post
    _real_post = wb_alerts._post
    wb_alerts._post = _safe_post


def tearDownModule():
    wb_alerts._post = _real_post


def _quiesce():
    """停掉后台投递线程并清空队列，让每个测试从干净状态开始。

    告警是**异步**投递的：`emit()` 只是把一项放进队列，真正发送的是
    后台 worker。上一个测试 enqueue 的项可能还没被取走，下一个测试的
    setUp 就已经装好了自己的假 `_post` —— 于是那一项落进下一个测试的
    `sent` 里，「什么都没发」这类断言就会莫名失败。

    这个竞态一直都在，只是以前被掩盖着：那时每个测试结束后 `_post` 被换回
    真实实现，迟到的项打到真实网络上，进不了任何测试的 `sent`。改成模块级
    假投递（为了不出网）之后它才显形。

    ⇒ 每个测试开始前先收尾：让 worker 退出、把队列倒空、再把它标记为需要
    重启（`ensure_worker()` 会按需重新起一个）。
    """
    wb_alerts._stop.set()
    worker = wb_alerts._worker
    if worker is not None:
        worker.join(timeout=2.0)
    while True:
        try:
            wb_alerts._queue.get_nowait()
        except Exception:
            break
    wb_alerts._worker = None


class AlertsTestBase(unittest.TestCase):
    """所有告警套件共用的前置：DNS 固定、投递不出网、状态干净。"""

    def setUp(self):
        _quiesce()
        patcher = mock.patch.object(wb_alerts.socket, "getaddrinfo",
                                    fake_getaddrinfo)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(_quiesce)


def write_settings(directory, alerts):
    """写一份带 alerts 配置的 settings.json。"""
    data = {}
    path = os.path.join(directory, "settings.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            data = {}
    if alerts is None:
        data.pop("alerts", None)
    else:
        data["alerts"] = alerts
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


class AlertsConfigTest(AlertsTestBase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-alerts-cfg-")
        self.addCleanup(wb_alerts.reset)

    def test_no_config_means_disabled(self):
        write_settings(self.tmp, None)
        self.assertEqual(wb_alerts.config(self.tmp), {})

    def test_empty_config_means_disabled(self):
        write_settings(self.tmp, {})
        self.assertEqual(wb_alerts.config(self.tmp), {})

    def test_webhook_is_read_back_without_the_secret(self):
        write_settings(self.tmp, {"webhook": {"url": "https://example.com/h",
                                              "secret": "s3cr3t"}})
        cfg = wb_alerts.config(self.tmp)
        self.assertEqual(cfg["webhook"]["url"], "https://example.com/h")
        self.assertEqual(cfg["webhook"]["secret"], "s3cr3t")
        # 面板读的那一份必须不含 secret
        public = wb_settings.alerts(self.tmp)
        self.assertTrue(public["webhook"])
        self.assertNotIn("s3cr3t", json.dumps(public))

    def test_serverchan_key_is_not_echoed(self):
        write_settings(self.tmp, {"serverchan": {"key": "SCTabc"}})
        public = wb_settings.alerts(self.tmp)
        self.assertTrue(public["serverchan"])
        self.assertNotIn("SCTabc", json.dumps(public))

    def test_thresholds_fall_back_to_defaults(self):
        write_settings(self.tmp, {"thresholds": {"min_ready": "not-a-number"}})
        cfg = wb_alerts.config(self.tmp)
        self.assertEqual(cfg["thresholds"]["min_ready"], 8)

    def test_thresholds_accept_valid_values(self):
        write_settings(self.tmp, {"thresholds": {"min_ready": 3, "for_ticks": 1,
                                                 "clear_ticks": 3}})
        cfg = wb_alerts.config(self.tmp)
        self.assertEqual(cfg["thresholds"]["min_ready"], 3)
        self.assertEqual(cfg["thresholds"]["for_ticks"], 1)
        self.assertEqual(cfg["thresholds"]["clear_ticks"], 3)


class AlertsDebounceTest(AlertsTestBase):
    """去抖：连续命中才发，连续恢复才发恢复。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-alerts-deb-")
        write_settings(self.tmp, {"webhook": {"url": "https://example.com/h"}})
        wb_alerts.reset()

    def test_single_hit_does_not_fire(self):
        # 默认 for_ticks=2：一次抖动不该发通知
        self.assertIsNone(wb_alerts._debounce_step("k", True, 2, 2))

    def test_two_hits_fire_once(self):
        self.assertIsNone(wb_alerts._debounce_step("k", True, 2, 2))
        self.assertEqual(wb_alerts._debounce_step("k", True, 2, 2), "fault")
        # 已经在故障态，后续命中不再重复发
        self.assertIsNone(wb_alerts._debounce_step("k", True, 2, 2))

    def test_clear_needs_two_ticks(self):
        wb_alerts._debounce_step("k", True, 2, 2)
        wb_alerts._debounce_step("k", True, 2, 2)
        self.assertIsNone(wb_alerts._debounce_step("k", False, 2, 2))
        self.assertEqual(wb_alerts._debounce_step("k", False, 2, 2), "clear")

    def test_flicker_never_fires(self):
        """命中-恢复-命中-恢复：每次都不满 2 轮，永远不该发。"""
        fired = False
        for _ in range(10):
            if wb_alerts._debounce_step("k", True, 2, 2) == "fault":
                fired = True
            if wb_alerts._debounce_step("k", False, 2, 2) == "clear":
                fired = True
        self.assertFalse(fired)


class AlertsDeliveryTest(AlertsTestBase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-alerts-del-")
        write_settings(self.tmp, {"webhook": {"url": "https://example.com/h",
                                              "secret": "s3cr3t"}})
        wb_alerts.reset()
        self.sent = []
        self._orig = wb_alerts._post

        def fake_post(url, body, headers=None, timeout=None):
            self.sent.append((url, body, headers or {}))
            return b"ok"

        wb_alerts._post = fake_post
        self.addCleanup(self._restore)

    def _restore(self):
        wb_alerts._post = self._orig

    def test_emit_without_config_sends_nothing(self):
        write_settings(self.tmp, None)
        self.assertFalse(wb_alerts.emit(self.tmp, wb_alerts.EV_POOL_DEGRADED, {}))
        self.assertEqual(self.sent, [])

    def test_deliver_signs_the_body(self):
        cfg = wb_alerts.config(self.tmp)
        ok, err = wb_alerts._deliver(cfg, "test", {"a": 1})
        self.assertTrue(ok, err)
        self.assertEqual(len(self.sent), 1)
        _url, body, headers = self.sent[0]
        self.assertIn(wb_alerts.SIGNATURE_HEADER, headers)
        self.assertTrue(headers[wb_alerts.SIGNATURE_HEADER])

    def test_signature_matches_the_body(self):
        cfg = wb_alerts.config(self.tmp)
        wb_alerts._deliver(cfg, "test", {"a": 1})
        _url, body, headers = self.sent[0]
        import hashlib
        import hmac
        expected = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
        self.assertEqual(headers[wb_alerts.SIGNATURE_HEADER], expected)

    def test_body_carries_the_event_and_payload(self):
        cfg = wb_alerts.config(self.tmp)
        wb_alerts._deliver(cfg, "pool_degraded", {"ready": 3})
        _url, body, _headers = self.sent[0]
        parsed = json.loads(body.decode("utf-8"))
        self.assertEqual(parsed["event"], "pool_degraded")
        self.assertEqual(parsed["payload"]["ready"], 3)

    def test_delivery_failure_is_reported_not_raised(self):
        """投递失败要**报出来**，但不能带出响应体。

        v0.1.6 安全加固：webhook URL 是面板用户填的，网关会真的去请求它。
        urllib 的异常 `str(exc)` 里带**响应正文** —— 直接回显给面板，
        就是一条「让网关代读内网资源」的通道（SSRF 读）。
        ⇒ 错误文本只保留异常类型与状态码。
        """
        def boom(url, body, headers=None, timeout=None):
            raise RuntimeError("network down")

        wb_alerts._post = boom
        cfg = wb_alerts.config(self.tmp)
        ok, err = wb_alerts._deliver(cfg, "test", {})
        self.assertFalse(ok)
        # 仍然报告了失败，但**不含**原始异常文本
        self.assertIn("webhook", err)
        self.assertNotIn("network down", err)

    def test_check_pool_uses_dispatchable_count(self):
        """判据是「可派发」数，不是启用数 —— 护栏隔离的号不算容量。"""
        # 12 个启用但其中 5 个被护栏隔离 -> 可派发 7，低于阈值 8 -> 降级
        degraded = wb_alerts.check_pool(self.tmp, 7, 19, enabled=12, isolated=5)
        self.assertTrue(degraded)

    def test_check_pool_ok_when_healthy(self):
        wb_alerts.check_pool(self.tmp, 12, 19, enabled=12, isolated=0)
        # 第一次命中不触发（for_ticks=2），第二次才触发
        degraded = wb_alerts.check_pool(self.tmp, 12, 19, enabled=12, isolated=0)
        self.assertFalse(degraded)

    def test_queue_full_drops_instead_of_blocking(self):
        """队列满就丢，绝不阻塞调用方（告警是尽力而为）。

        契约：`emit` 返回 False（这一条没排进去），且丢弃计数 +1。
        注意去抖要先过 —— 用 for_ticks=1 让它一次性过闸，
        这样测的才是「队列满」这一条路径。
        """
        import queue as _queue
        original = wb_alerts._queue
        wb_alerts._queue = _queue.Queue(maxsize=1)
        self.addCleanup(lambda: setattr(wb_alerts, "_queue", original))
        wb_alerts._queue.put(("x", "y", "z"))       # 占满
        before = wb_alerts.status()["dropped"]
        fired = wb_alerts.emit(self.tmp, wb_alerts.EV_POOL_DEGRADED, {},
                               for_ticks=1, clear_ticks=1, key="q")
        self.assertFalse(fired)
        self.assertEqual(wb_alerts.status()["dropped"], before + 1)

    def test_emit_never_raises_when_the_queue_is_full(self):
        """丢就丢了 —— 绝不能把异常抛回业务请求线程。"""
        import queue as _queue
        original = wb_alerts._queue
        wb_alerts._queue = _queue.Queue(maxsize=1)
        self.addCleanup(lambda: setattr(wb_alerts, "_queue", original))
        wb_alerts._queue.put(("x", "y", "z"))
        for i in range(50):
            wb_alerts.emit(self.tmp, wb_alerts.EV_POOL_DEGRADED, {},
                           for_ticks=1, clear_ticks=1, key="q%d" % i)


class AlertsSettingsWriteTest(AlertsTestBase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb-alerts-set-")
        self.addCleanup(wb_alerts.reset)

    def test_set_and_clear(self):
        wb_settings.set_alerts(self.tmp, {"webhook": {"url": "https://example.com/h",
                                                       "secret": "s"}})
        self.assertTrue(wb_settings.alerts(self.tmp)["webhook"])
        wb_settings.set_alerts(self.tmp, {})
        self.assertFalse(wb_settings.alerts(self.tmp)["webhook"])

    def test_settings_file_stays_private(self):
        """settings.json 含 secret，权限必须是 0600。"""
        wb_settings.set_alerts(self.tmp, {"webhook": {"url": "https://example.com/h",
                                                       "secret": "s"}})
        path = os.path.join(self.tmp, "settings.json")
        mode = os.stat(path).st_mode & 0o777
        if os.name != "nt":
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
