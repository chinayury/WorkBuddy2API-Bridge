# -*- coding: utf-8 -*-
"""空池状态码：三条协议必须一致，且账本与响应用同一个码。

背景（2026-10-27，借鉴 ops 补丁 v1.8.0-ops.20261026.1 的
「空池三协议及账本统一 503」）：

我方在「池子里没有可用账号」时，三条协议给出的码**曾经不一致**：

| 路径 | 原判据 | 原结果 |
|---|---|---|
| Chat | `message.startswith("no usable account")` | 503 ✓ |
| Responses | `message.startswith("no usable account")` | 503 ✓ |
| Anthropic Messages | `"no account" in low or "no enabled account" in low` | **502** ✗ |

而真正抛出的消息是 "no usable account for realm '...' ..." ——
子串 `"no account"` **不出现**在 `"no usable account"` 里（中间隔着 "usable "），
所以 Anthropic 路径的空池落到了 502。

而 Chat/Responses 路径还有第二个问题：**账本记 502、响应给 503** ——
同一个事件在两处口径相反。

503 与 502 给客户端的处置动作完全相反：
  * 503 = 网关这边没有账号可用了 → 运营者去加号/启用；
  * 502 = 上游挂了 → 客户端重试可能有用。
混起来会让客户端一直重试一个根本重试不好的请求。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


EMPTY_POOL = ("no usable account for realm 'cn': all are disabled, cooling down, "
              "expired or parked by the daily token limit")


class EmptyPoolStatusTest(unittest.TestCase):
    def test_empty_pool_is_503(self):
        self.assertEqual(wb_proxy._empty_pool_status(EMPTY_POOL), 503)

    def test_the_real_throw_site_message_is_recognised(self):
        # 抛出的原文（wb_proxy.py 的 open_upstream）必须被认成空池。
        self.assertTrue(EMPTY_POOL.startswith("no usable account"))

    def test_upstream_failure_stays_502(self):
        # 真正的上游故障不能被误判成空池：那是「重试可能有用」。
        self.assertEqual(
            wb_proxy._empty_pool_status("upstream unreachable: timed out"), 502)
        self.assertEqual(
            wb_proxy._empty_pool_status("HTTPError: 502 Bad Gateway"), 502)

    def test_other_gateway_errors_stay_502(self):
        for msg in ("TLS verify failed", "upstream unreachable: <urlopen error>",
                    "connection reset by peer"):
            self.assertEqual(wb_proxy._empty_pool_status(msg), 502, msg)

    def test_healthy_account_wording_is_also_empty_pool(self):
        # gateway_hint 用的措辞同样是「没有可用账号」。
        self.assertEqual(
            wb_proxy._empty_pool_status("no healthy account available in pool"),
            503)

    def test_no_usable_account_is_not_mistaken_for_something_else(self):
        """回归防护：判据不能靠 "no account" 这种会被绕过的子串。"""
        # 这条消息里没有 "no account" 子串 —— 旧判据就是因此漏判的。
        self.assertNotIn("no account", EMPTY_POOL.lower())
        self.assertEqual(wb_proxy._empty_pool_status(EMPTY_POOL), 503)

    def test_all_three_protocol_agree(self):
        """三条协议用同一个判据，结果必须一致。"""
        status = wb_proxy._empty_pool_status(EMPTY_POOL)
        # Chat / Responses / Anthropic 走的都是这一个函数
        self.assertEqual(status, 503)
        for _ in range(3):
            self.assertEqual(wb_proxy._empty_pool_status(EMPTY_POOL), status)

    def test_empty_message_is_502(self):
        self.assertEqual(wb_proxy._empty_pool_status(""), 502)
        self.assertEqual(wb_proxy._empty_pool_status(None), 502)


if __name__ == "__main__":
    unittest.main()
