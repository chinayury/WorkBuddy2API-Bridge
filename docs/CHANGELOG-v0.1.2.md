# CHANGELOG v0.1.2

## 修正 v0.1.1 的函数名回填：函数名必须整段流里只出现一次

### 现象

v0.1.1 上线后，WorkBuddy CLI（agent=cli）驱动 5 个**并行**工具调用时，整轮 agent
直接失败，界面永远停在「思考中」：

```
[Interruption] Catch block entered, error: Tool TaskCreateTaskCreateTaskCreate…
              …TaskCreate Not Found: TaskCreateTaskCreate…
[Error] ModelBehaviorError: Tool Not Found: TaskCreateTaskCreate…
[SessionManager][Gate] willRetry=true reason=retryableModelError
[AgentService] skipRun=true for agent=cli, runState=idle, queueLen=0
```

5 个 TaskCreate 各自**执行成功**（客户端的 tool_call / tool_call_update 事件齐全），
但下一步按工具名去找工具时，名字是一长串 `TaskCreateTaskCreate…` —— 找不到，
`RUN_FAILED`；紧接着自动重试又被 `skipRun=true` 拦下，session 停在 idle，
界面既不报错也不继续。

### 根因：v0.1.1 的「函数名回填」是错的

上游把一次工具调用拆成多帧：头帧带 `id`/`type`/`name`，参数分片帧的
`function.name` 是**空串**。v0.0.x 的占位过滤把参数分片帧整帧删掉，导致参数全丢
（「缺少必选参数」）；v0.1.1 改成放行这些帧，**但同时把空名字「回填」成真名**，
于是**每一帧都带着函数名**。

两种客户端对 `name` 的处理方式正好相反：

| 客户端 | 规则 | 空名字 | 真名重复 |
|---|---|---|---|
| OpenAI SDK | `name is not None` 就覆盖 | 抹掉真名 → 认不出工具 | 幂等，无害 |
| 按 name 累加的客户端（实测 WorkBuddy CLI） | 每次出现都接上 | 无害 | **拼成一长串 → Tool Not Found** |

⇒ 唯一两边都对的形状就是 **OpenAI 原生流式的样子：头帧带 name，续帧整个不带
name 键**。

### 修复

`_normalize_tool_call_frames` 不再「回填」名字，改为：

- 头帧（带 `id`）→ 保留 `name`，并记住这个 index 的函数名；
- 续帧（`name` 为空串）→ **把 `name` 键整个删掉**；
- 续帧重复带了真名 → 同样删掉；
- 没有 `id` 却带着真名、且该 index 还没出现过 → 保留（这是唯一一处能拿到名字的地方）。

参数那半边的修复（不再删除承载参数分片的帧）保持不变。

### 回归

`tests/_test_stream_tool_call_args.py` 扩到 10 项，新增：

- `test_name_appears_only_in_the_header_frame`：头帧带 name，续帧不得带 name 键；
- `test_both_client_accumulation_rules_get_the_same_name`：覆盖式（SDK）与累加式
  （WorkBuddy CLI）两种累积规则必须得到同一个函数名；
- `test_parallel_calls_keep_their_own_name`：3 个并行调用各自按 index 独立，
  两种规则都不许串名。

其余 7 项沿用 v0.1.1（参数完整、真占位仍删、流式与非流式一致、Responses 路径同理）。

### 影响面

- 触发条件：`stream=true` **且**模型发起**并行**工具调用（单个调用时名字只出现一次，
  不受影响，所以 v0.1.1 部署后的单工具场景看起来是好的）。
- 症状：agent 侧 `Tool Not Found: <名字被拼接>`，整轮失败、界面卡在「思考中」。
- 非流式路径不受影响（`aggregate_stream` 按 index 建条目，名字不会被拼）。

### 版本

- 版本串 `0.1.1` → `0.1.2`（`VERSION` 文件 + `wb_proxy.py` 两处 + compose 镜像行）。
- 部署形态不变：发布树 → 服务器本地构建镜像 → 逐文件 sha 校验 → 只改 compose
  的 image 行。
