#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""金丝雀 SSE 固件回放：上游形状变了，这里会亮红灯。

固件在 `tests/fixtures/upstream_sse/`，是从上游实测抓下来的原始帧。
这个套件做两件事：
  1. **回放**：把固件喂给网关的清洗逻辑，断言出来的东西符合 OpenAI 契约；
  2. **守门**：断言固件本身没被改坏（帧数、名字出现次数、参数完整性）——
     有人手改了固件来「让测试过」，这里会拦住。

背景：v0.1.1 与 v0.1.2 两次线上事故都出在流式工具调用上。
  - v0.1.1：占位过滤把承载 arguments 分片的帧整帧删掉 → agent 报「缺少必选参数」；
  - v0.1.2：函数名回填让每帧都带 name → WorkBuddy CLI 把名字拼成一长串
    → Tool Not Found → 整轮 agent 失败、界面卡在「思考中」。

两份都靠「真实上游帧」才复现。样本现在固化在固件目录里，不再只活在代码里。

运行：python3 tests/_test_sse_fixtures.py
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import wb_proxy  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "upstream_sse")


def load_sse(name):
    """读一个固件，返回原始帧的 JSON 列表。"""
    path = os.path.join(FIXTURES, name)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    frames = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload and payload != "[DONE]":
                    frames.append(json.loads(payload))
    return frames


def load_body(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read().strip()


def gateway_stream(frames):
    """按 wb_proxy._chat_stream_response 的顺序清洗一遍。

    顺序与线上一致：先按 choice 整理工具调用帧（_normalize_tool_call_frames），
    再过 clean_chunk。顺序反了就会重演 v0.1.1。
    """
    seen = {}
    out = []
    for raw in frames:
        maybe = json.loads(raw) if isinstance(raw, str) else raw
        for choice_idx, ch in enumerate(maybe.get("choices") or []):
            delta = ch.get("delta") or {}
            wb_proxy._normalize_tool_call_frames(
                delta, seen.setdefault(choice_idx, {}))
        cleaned = wb_proxy.clean_chunk(json.dumps(maybe, ensure_ascii=False))
        if not cleaned:
            continue
        out.append(json.loads(cleaned))
    return out


def accumulate_overwrite(chunks):
    """OpenAI SDK 的规则：name 不为 None 就覆盖，arguments 逐帧拼接。"""
    calls = {}
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                idx = tc.get("index")
                slot = calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name") is not None:
                    slot["name"] = fn["name"]
                if fn.get("arguments") is not None:
                    slot["arguments"] += fn["arguments"]
    return calls


def accumulate_append(chunks):
    """按 name 累加的客户端（实测 WorkBuddy CLI）：名字也会被接成一长串。"""
    calls = {}
    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                idx = tc.get("index")
                slot = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name") is not None:
                    slot["name"] += fn["name"]
                if fn.get("arguments") is not None:
                    slot["arguments"] += fn["arguments"]
    return calls


class FixtureIntegrityTests(unittest.TestCase):
    """固件本身没被改坏。"""

    def test_tool_calls_fixture_has_the_expected_shape(self):
        frames = load_sse("tool-calls.sse")
        # 1 帧 role + 1 帧头帧 + 8 帧参数分片 + 1 帧收尾占位
        self.assertEqual(12, len(frames))

        header = frames[1]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual("get_weather", header["function"]["name"])
        self.assertTrue(header["id"])

        # 参数分片帧的 name 是空串 —— 这正是 v0.1.1 踩的坑
        for frag in frames[2:10]:
            tc = frag["choices"][0]["delta"]["tool_calls"][0]
            self.assertEqual("", tc["function"]["name"])
            self.assertTrue(tc["function"]["arguments"])

        # 收尾占位：name 与 arguments 都空
        tail = frames[11]["choices"][0]["delta"]["function_call"]
        self.assertEqual("", tail["name"])
        self.assertEqual("", tail["arguments"])

    def test_parallel_fixture_has_three_calls(self):
        frames = load_sse("tool-calls-parallel.sse")
        indices = set()
        for frame in frames:
            for tc in frame["choices"][0]["delta"].get("tool_calls") or []:
                indices.add(tc.get("index"))
        self.assertEqual({0, 1, 2}, indices)

    def test_error_bodies_carry_their_codes(self):
        self.assertEqual(11140, json.loads(load_body("error-11140.body.json"))["code"])
        self.assertEqual(14003, json.loads(load_body("error-14003.body.json"))["code"])
        self.assertEqual(6004, json.loads(load_body("error-6004.body.json"))["code"])


class ReplayTests(unittest.TestCase):
    """回放：固件喂给网关的清洗逻辑，出来的东西必须符合契约。"""

    def test_tool_call_arguments_survive(self):
        """v0.1.1 的回归：参数必须完整。"""
        out = gateway_stream(load_sse("tool-calls.sse"))
        calls = accumulate_overwrite(out)
        self.assertEqual(1, len(calls))
        # 分片里带着 ": "（冒号+空格），拼回来就是这个形状
        self.assertEqual('{"city": "Beijing"}', calls[0]["arguments"])

    def test_the_function_name_appears_exactly_once(self):
        """v0.1.2 的回归：名字整段流里只出现一次。"""
        out = gateway_stream(load_sse("tool-calls.sse"))
        with_name = 0
        for chunk in out:
            for choice in chunk.get("choices") or []:
                for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                    if (tc.get("function") or {}).get("name"):
                        with_name += 1
        self.assertEqual(1, with_name)

    def test_both_client_accumulation_rules_agree(self):
        """覆盖式（SDK）与累加式（WorkBuddy CLI）必须得到同一个名字。"""
        out = gateway_stream(load_sse("tool-calls.sse"))
        self.assertEqual("get_weather", accumulate_overwrite(out)[0]["name"])
        self.assertEqual("get_weather", accumulate_append(out)[0]["name"])

    def test_parallel_calls_keep_their_own_name(self):
        """并行调用按 index 独立，不串名。"""
        out = gateway_stream(load_sse("tool-calls-parallel.sse"))
        calls = accumulate_append(out)
        self.assertEqual(3, len(calls))
        self.assertEqual("TaskCreate", calls[0]["name"])
        self.assertEqual("TaskCreate", calls[1]["name"])
        self.assertEqual("TaskUpdate", calls[2]["name"])
        # 参数也各自独立（这份固件的分片是紧凑 JSON，没有空格）
        for idx in (0, 1, 2):
            self.assertEqual('{"title":"x"}', calls[idx]["arguments"])

    def test_plain_text_is_not_mangled(self):
        """普通回复的正文要原样送达。"""
        out = gateway_stream(load_sse("chat-basic.sse"))
        text = "".join((choice.get("delta") or {}).get("content") or ""
                        for chunk in out
                        for choice in chunk.get("choices") or [])
        self.assertIn("北京", text)
        self.assertIn("今天晴。", text)

    def test_the_placeholder_tail_frame_is_dropped(self):
        """真占位帧仍然要删 —— 固件里那一帧不能漏到客户端。"""
        out = gateway_stream(load_sse("tool-calls.sse"))
        for chunk in out:
            for choice in chunk.get("choices") or []:
                fc = (choice.get("delta") or {}).get("function_call")
                if fc is not None:
                    self.assertNotEqual("", fc.get("arguments") or "")


class ErrorFixtureTests(unittest.TestCase):
    """错误固件喂给分类器。"""

    def test_11140_is_classified_as_a_credential_problem(self):
        import wb_accounts
        body = load_body("error-11140.body.json")
        self.assertEqual(wb_accounts.ERR_CREDENTIAL,
                         wb_accounts.classify_upstream_error(403, detail=body))

    def test_14003_is_classified_as_a_model_problem(self):
        import wb_accounts
        body = load_body("error-14003.body.json")
        self.assertEqual(wb_accounts.ERR_MODEL,
                         wb_accounts.classify_upstream_error(429, detail=body))

    def test_6004_is_classified_as_a_model_problem(self):
        import wb_accounts
        body = load_body("error-6004.body.json")
        self.assertEqual(wb_accounts.ERR_MODEL,
                         wb_accounts.classify_upstream_error(429, detail=body))

    def test_the_6004_body_carries_a_parseable_reset_clock(self):
        body = load_body("error-6004.body.json")
        stamp = wb_proxy.parse_rate_limit_reset(body)
        self.assertIsNotNone(stamp)


if __name__ == "__main__":
    unittest.main()
