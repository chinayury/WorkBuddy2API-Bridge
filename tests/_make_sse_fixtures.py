# -*- coding: utf-8 -*-
"""从 tests/_test_stream_tool_call_args.py 的真实上游帧导出 golden 固件。

这些帧是 2026-10-25 从 243 实测抓到的上游原始 SSE，一字未改。
导出成文件之后：
  - 别的协议路径（Responses / Anthropic）能引用同一份样本，不用复制粘贴；
  - 上游改形状时有一个地方会亮红灯。

运行：python tests/_make_sse_fixtures.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FIXTURES = os.path.join(HERE, "fixtures", "upstream_sse")


def _frame(delta, finish=""):
    return json.dumps({
        "id": "chatcmpl-wb-test", "model": "deepseek-v4-pro",
        "object": "chat.completion.chunk", "created": 1,
        "choices": [{"index": 0, "delta": delta, "logprobs": None,
                     "finish_reason": finish}],
        "usage": None,
    }, ensure_ascii=False)


def _write(name, lines):
    path = os.path.join(FIXTURES, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for line in lines:
            fh.write("data: " + line + "\n\n")
    print("wrote %s (%d frames)" % (name, len(lines)))


def main():
    if not os.path.isdir(FIXTURES):
        os.makedirs(FIXTURES)

    # ---- chat-basic：role 开头 → 正文 → finish ----
    basic = [
        _frame({"role": "assistant", "content": "", "reasoning_content": "",
                "function_call": None, "refusal": "", "tool_calls": [],
                "extra_fields": None}),
        _frame({"content": "北京", "reasoning_content": "", "function_call": None,
                "refusal": "", "tool_calls": [], "extra_fields": None}),
        _frame({"content": "今天晴。", "reasoning_content": "", "function_call": None,
                "refusal": "", "tool_calls": [], "extra_fields": None},
               finish="stop"),
    ]
    _write("chat-basic.sse", basic)

    # ---- tool-calls：与 _test_stream_tool_call_args.py 的 UPSTREAM_FRAMES 同源 ----
    fragments = ["{", '"', "city", '"', ": ", '"', "Beijing", '"', "}"]
    tool_calls = [
        _frame({"role": "assistant", "content": "", "reasoning_content": "",
                "function_call": None, "refusal": "", "tool_calls": [],
                "extra_fields": None}),
        _frame({"content": "", "reasoning_content": "", "function_call": None,
                "refusal": "",
                "tool_calls": [{"id": "call_00_NvkTuuUwPIEj1pagyvWn6548",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                                "index": 0}],
                "extra_fields": None}),
    ]
    for frag in fragments:
        tool_calls.append(
            _frame({"content": "", "reasoning_content": "", "function_call": None,
                    "refusal": "",
                    "tool_calls": [{"function": {"name": "", "arguments": frag},
                                    "index": 0}],
                    "extra_fields": None}))
    tool_calls.append(
        _frame({"role": "assistant", "content": "", "reasoning_content": "",
                "function_call": {"name": "", "arguments": ""}, "refusal": "",
                "tool_calls": [], "extra_fields": None}, finish=None))
    _write("tool-calls.sse", tool_calls)

    # ---- tool-calls-parallel：三个并行调用，各自 index 独立 ----
    parallel = [
        _frame({"role": "assistant", "content": "", "reasoning_content": "",
                "function_call": None, "refusal": "", "tool_calls": [],
                "extra_fields": None}),
    ]
    names = [("TaskCreate", "call_0_A"), ("TaskCreate", "call_1_B"),
             ("TaskUpdate", "call_2_C")]
    for index, (name, call_id) in enumerate(names):
        parallel.append(
            _frame({"content": "", "reasoning_content": "", "function_call": None,
                    "refusal": "",
                    "tool_calls": [{"id": call_id, "type": "function",
                                    "function": {"name": name, "arguments": ""},
                                    "index": index}],
                    "extra_fields": None}))
        # 每个调用的参数分片（name 为空串）
        for frag in ('{"', "title", '":"', "x", '"}'):
            parallel.append(
                _frame({"content": "", "reasoning_content": "",
                        "function_call": None, "refusal": "",
                        "tool_calls": [{"function": {"name": "", "arguments": frag},
                                        "index": index}],
                        "extra_fields": None}))
    parallel.append(
        _frame({"role": "assistant", "content": "", "reasoning_content": "",
                "function_call": {"name": "", "arguments": ""}, "refusal": "",
                "tool_calls": [], "extra_fields": None}, finish=None))
    _write("tool-calls-parallel.sse", parallel)

    # ---- 错误形状：不是 SSE 流，是 HTTP 错误正文 ----
    # 上游的错误是 HTTP 状态码 + JSON 正文，不是 SSE。固件存正文，
    # 回放时由测试自己配上状态码。
    errors = {
        "error-11140.body.json": json.dumps({
            "code": 11140, "msg": "request illegal",
            "displayMsg": "账号存在异常行为，已被限制使用",
        }, ensure_ascii=False),
        "error-14003.body.json": json.dumps({
            "code": 14003, "msg": "too many requests", "retry_after": 13,
        }, ensure_ascii=False),
        "error-6004.body.json": json.dumps({
            "code": 6004,
            "msg": "您的使用量已超出频率限制，将在 2026-10-02 16:00:34 UTC+8 重置。",
        }, ensure_ascii=False),
    }
    for name, body in errors.items():
        path = os.path.join(FIXTURES, name)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body + "\n")
        print("wrote %s" % name)


if __name__ == "__main__":
    main()
