# CHANGELOG v0.1.1

## 修复：流式工具调用的参数被网关吃掉（agent 侧表现为「缺少必填参数」）

### 现象

用本网关驱动 agent（浏览器抓取 / Bash / 文件检索 / 建任务等工具）时，模型每次
发出的工具调用都带着**空参数**到达执行层，执行层一律报「缺少必选参数」。
同一段对话走非流式则完全正常。

### 根因

上游把一次工具调用拆成多帧发（OpenAI 标准形状）：

```
帧1  tool_calls:[{id, type, function:{name:"get_weather", arguments:""}}]   ← 头帧
帧2  tool_calls:[{function:{name:"", arguments:"{"}}]                        ← 参数分片，name 为空
帧3  tool_calls:[{function:{name:"", arguments:"\""}}]
...
末帧 function_call:{name:"", arguments:""}                                  ← 真占位
```

网关的 `clean_chunk` 有一道「空占位」过滤，判据是 **function.name 为空就删帧**
（注释写的是「name 与 arguments 均空才视为占位噪音」，实现只判了 name）。
于是**每一个承载参数分片的帧都被整帧删掉**，客户端最终只收到帧1 那个
`arguments: ""` 的头帧——工具名在、参数全空。执行层解析出空参数对象，报
「缺少必选参数」。

非流式路径不受影响：`aggregate_stream` 对空 name 的帧照样累加 arguments。
所以同一条对话，流式与非流式给出的工具调用不一样，而 agent 客户端几乎都走
流式。

### 修复

1. **占位判据修正**：只有「既没有函数名、也没有任何参数文本」的帧才算占位。
   带参数分片的帧一律转发。`clean_chunk` 与 Responses 路径的 `_strip_empty_fc`
   同步修正。
2. **函数名回填**：上游在参数分片帧里把 `function.name` 置空。OpenAI SDK 的
   累积规则是「name 不为 None 就覆盖」，空串会把真名抹掉；朴素客户端从最后
   一个 delta 取名字，拿到空串一样认不出工具。现在按 index 记住出现过的函数名，
   后续帧一律补回去——对 SDK 幂等，对朴素客户端救回它要的名字。
3. **占位帧仍然照删**：真正空的尾帧（`function_call: {"name":"","arguments":""}`）
   与空 `tool_calls` 列表继续被剔除，不给客户端添噪音。

### 回归

新增 `tests/_test_stream_tool_call_args.py`（9 项），用 **2026-10-25 从 243 实测
抓到的纯净上游帧**做固定样本，钉住五件事：

1. 上游原始帧经过网关清洗后参数必须完整（`{"city": "Beijing"}`）；
2. 真占位帧仍然要删掉；
3. 每个转发出去的帧都必须带非空函数名；
4. 流式与非流式对同一段上游帧给出同样的参数；
5. Responses 路径同样不能删掉带参数的帧。

### 影响面

- 触发条件：`stream=true` **且**模型发起工具调用。
- 之前所有流式工具调用都受影响（参数一律为空），不流式的不受影响。
- Anthropic Messages 路径不受影响：它自己按 index 维护状态、累加 arguments，
  不经过 `clean_chunk` 的占位过滤。

### 版本

- 版本串 `0.1.0` → `0.1.1`（`VERSION` 文件 + `wb_proxy.py` 两处 + compose 镜像行）。
- 部署形态不变：发布树 → 服务器本地构建镜像 → 逐文件 sha 校验 → 只改 compose
  的 image 行。
