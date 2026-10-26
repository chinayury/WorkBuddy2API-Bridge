#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""流式工具调用的参数与函数名都不能坏（clean_chunk 的占位过滤 / 名字重复）。

背景
----
上游把一次工具调用拆成多帧发：头帧带 id/type/name，参数按字符切成后续帧，
后续帧的 function.name 是空串。网关的「name 为空就是占位噪音」过滤把承载参数的
帧**整帧删掉**，客户端于是只收到一个 arguments 为空的头帧 —— 模型明明给出了
调用，执行层拿到的却是空参数，报「缺少必选参数」。

修好参数之后又暴露出第二个坑：**函数名必须整段流里只出现一次**。
两种客户端对 name 的处理方式正好相反：
  * OpenAI SDK：「name 不为 None 就覆盖」—— 空串会把真名抹掉；
  * 按 name 累加的客户端（实测 WorkBuddy CLI 就是这样）：每次出现的名字都会接上，
    5 个并行的 TaskCreate 被累成 "TaskCreateTaskCreate…"，客户端据此找不到工具，
    报 Tool Not Found，整轮 agent 直接失败、界面永远停在「思考中」。

唯一两边都对的形状就是 OpenAI 原生流式的样子：**头帧带 name，续帧整个不带 name 键**。

这个套件钉住：
  1. 上游原始帧（纯净上游，实测抓取）经过网关的清洗后，参数必须完整；
  2. 真占位帧（既无函数名、也无参数文本）仍然要删掉；
  3. **函数名只出现在头帧**，续帧不得带 name 键；
  4. 两种客户端的累积规则（覆盖式 / 累加式）都必须得到同一个正确函数名；
  5. 并行调用各自按 index 独立，不串名；
  6. 流式与非流式对同一段上游帧给出同样的参数；
  7. Responses 路径的 _strip_empty_fc 同样不能删掉带参数的帧。

运行：python3 tests/_test_stream_tool_call_args.py
"""
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy

# --------------------------------------------------------------------------
# 纯净上游帧：2026-10-25 从 243 实测抓到的上游原始 SSE 帧（deepseek-v4-pro）。
# 头帧带 id/type/name，后续帧的 function.name 是空串、只有 arguments 分片。
# --------------------------------------------------------------------------
ARG_FRAGMENTS = ["{", '"', "city", '"', ": ", '"', "Beijing", '"', "}"]


def _frame(delta, finish=""):
    return json.dumps({
        "id": "chatcmpl-wb-test", "model": "deepseek-v4-pro",
        "object": "chat.completion.chunk", "created": 1,
        "choices": [{"index": 0, "delta": delta, "logprobs": None,
                     "finish_reason": finish}],
        "usage": None,
    }, ensure_ascii=False)


UPSTREAM_FRAMES = (
    [_frame({"role": "assistant", "content": "", "reasoning_content": "",
             "function_call": None, "refusal": "", "tool_calls": [],
             "extra_fields": None})]
    # 头帧：id + type + name，参数为空串
    + [_frame({"content": "", "reasoning_content": "", "function_call": None,
               "refusal": "",
               "tool_calls": [{"id": "call_00_NvkTuuUwPIEj1pagyvWn6548",
                               "type": "function",
                               "function": {"name": "get_weather", "arguments": ""},
                               "index": 0}],
               "extra_fields": None})]
    # 参数分片帧：name 为空串，只有 arguments
    + [_frame({"content": "", "reasoning_content": "", "function_call": None,
               "refusal": "",
               "tool_calls": [{"function": {"name": "", "arguments": frag},
                               "index": 0}],
               "extra_fields": None}) for frag in ARG_FRAGMENTS]
    # 收尾占位：function_call 名字与参数都空
    + [_frame({"role": "assistant", "content": "", "reasoning_content": "",
               "function_call": {"name": "", "arguments": ""}, "refusal": "",
               "tool_calls": [], "extra_fields": None}, finish=None)]
)


def gateway_stream(frames):
    """模拟网关的 Chat 流式转发：逐帧清洗后交给客户端。

    顺序与 wb_proxy._chat_stream_response 一致：先按 choice 整理工具调用帧
    （删真占位 + 保证函数名只出现一次），再过 clean_chunk。
    """
    seen = {}
    out = []
    for raw in frames:
        maybe = json.loads(raw)
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


def _parallel_frames(names):
    """n 个并行调用：头帧带 id/name，续帧 name 为空串。"""
    frames = []
    for i, tool in enumerate(names):
        frames.append(_frame({"tool_calls": [
            {"id": "%s_%d_abc" % (tool, i), "type": "function",
             "function": {"name": tool, "arguments": ""}, "index": i}]}))
        for frag in ('{"q":', '"x"}'):
            frames.append(_frame({"tool_calls": [
                {"function": {"name": "", "arguments": frag}, "index": i}]}))
    return frames


class TestStreamToolCallArgs(unittest.TestCase):
    def test_arguments_survive_the_stream(self):
        calls = accumulate_overwrite(gateway_stream(UPSTREAM_FRAMES))
        self.assertIn(0, calls, "客户端一个工具调用都没收到")
        self.assertEqual(calls[0]["arguments"], '{"city": "Beijing"}')
        self.assertEqual(json.loads(calls[0]["arguments"]), {"city": "Beijing"})

    def test_name_appears_only_in_the_header_frame(self):
        """函数名必须整段流里只出现一次：头帧带 name，续帧不得带 name 键。"""
        for chunk in gateway_stream(UPSTREAM_FRAMES):
            for choice in chunk.get("choices") or []:
                for tc in (choice.get("delta") or {}).get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    if tc.get("id"):
                        # 头帧：必须带真名
                        self.assertEqual(fn.get("name"), "get_weather")
                    else:
                        # 续帧：整个 name 键都不能在（空串会把真名抹掉，
                        # 真名会把累加式客户端的名字接成一长串）
                        self.assertNotIn("name", fn,
                                         "续帧不该带 name 键: %r" % fn)

    def test_both_client_accumulation_rules_get_the_same_name(self):
        chunks = gateway_stream(UPSTREAM_FRAMES)
        self.assertEqual(accumulate_overwrite(chunks)[0]["name"], "get_weather")
        self.assertEqual(accumulate_append(chunks)[0]["name"], "get_weather")

    def test_true_placeholder_is_dropped(self):
        for chunk in gateway_stream(UPSTREAM_FRAMES):
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                fc = delta.get("function_call")
                # 上游原样带下来的 function_call: null 不算占位；只有带内容的
                # function_call 才是真占位，而它必须被删掉。
                if isinstance(fc, dict):
                    self.fail("空的 function_call 占位没有被删掉: %r" % fc)
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    # 转发出去的帧必须带名字（头帧）或带参数（分片帧），
                    # 两者都没有的才是占位，必须已经被删掉。
                    self.assertTrue(fn.get("name") or fn.get("arguments"),
                                    "既没有名字也没有参数的占位帧没有被删掉: %r" % tc)

    def test_stream_and_nonstream_agree(self):
        """同一段上游帧，流式与非流式必须给出同样的参数。"""
        sse = "".join("data: %s\n\n" % f for f in UPSTREAM_FRAMES)
        sse += "data: [DONE]\n\n"
        obj = wb_proxy.aggregate_stream(io.BytesIO(sse.encode("utf-8")),
                                        "deepseek-v4-pro", "chatcmpl-wb-test")
        calls = (obj["choices"][0]["message"].get("tool_calls") or [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["arguments"], '{"city": "Beijing"}')
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        # 非流式给的是完整参数，流式累出来的也必须是同一份
        self.assertEqual(calls[0]["function"]["arguments"],
                         accumulate_overwrite(gateway_stream(UPSTREAM_FRAMES))[0]["arguments"])

    def test_parallel_calls_keep_their_own_name(self):
        """并行调用各自按 index 独立，两种累积规则都不能串名。"""
        frames = _parallel_frames(["TaskCreate", "Bash", "ReadFile"])
        chunks = gateway_stream(frames)
        overwrite = accumulate_overwrite(chunks)
        append = accumulate_append(chunks)
        self.assertEqual(sorted(overwrite), [0, 1, 2])
        self.assertEqual([overwrite[i]["name"] for i in (0, 1, 2)],
                         ["TaskCreate", "Bash", "ReadFile"])
        self.assertEqual([append[i]["name"] for i in (0, 1, 2)],
                         ["TaskCreate", "Bash", "ReadFile"])
        for i, expect in ((0, '{"q":'), (1, '{"q":'), (2, '{"q":')):
            self.assertTrue(overwrite[i]["arguments"].startswith(expect))

    def test_clean_chunk_keeps_argument_frames(self):
        raw = _frame({"tool_calls": [{"function": {"name": "", "arguments": '{"a":'},
                                    "index": 0}]})
        cleaned = json.loads(wb_proxy.clean_chunk(raw))
        tcs = cleaned["choices"][0]["delta"]["tool_calls"]
        self.assertEqual(len(tcs), 1, "承载参数的帧被 clean_chunk 删掉了")

    def test_clean_chunk_drops_real_placeholder(self):
        raw = _frame({"tool_calls": [{"function": {"name": "", "arguments": ""},
                                    "index": 0}]})
        cleaned = wb_proxy.clean_chunk(raw)
        # 占位帧删完后这一帧本身就没有内容了，整帧被丢掉（clean_chunk 返回空串）。
        if cleaned:
            self.assertNotIn("tool_calls", json.loads(cleaned)["choices"][0]["delta"])

    def test_strip_empty_fc_keeps_argument_frames(self):
        obj = {"type": "message_delta",
               "delta": {"function_call": {"name": "", "arguments": '{"a":'}}}
        wb_proxy._strip_empty_fc(obj)
        self.assertIn("function_call", obj["delta"],
                      "Responses 路径把承载参数的 function_call 删掉了")

    def test_call_payload_is_empty_semantics(self):
        empty = wb_proxy._call_payload_is_empty
        self.assertTrue(empty({"name": "", "arguments": ""}))
        self.assertTrue(empty({"name": "  ", "arguments": "   "}))
        self.assertFalse(empty({"name": "", "arguments": "{"}))
        self.assertFalse(empty({"name": "f", "arguments": ""}))
        self.assertFalse(empty({"function": {"name": "", "arguments": "x"}}))
        self.assertTrue(empty({"function": {"name": "", "arguments": ""}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
