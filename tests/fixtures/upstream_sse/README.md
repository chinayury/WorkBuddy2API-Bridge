# 上游 SSE 金丝雀固件（golden fixtures）
#
# 这些文件是**从上游实测抓下来的原始帧**，一个字都没改。
# 用途：把「上游到底发什么形状」变成仓库里的固定资产，
# 而不是散落在某个测试文件的代码里。
#
# 为什么要固化（借鉴 Coding2API 的 src/provider/fixtures/{codebuddy,trae}/*.sse）：
#
#   v0.1.1 与 v0.1.2 两次线上事故都出在流式工具调用上：
#     - v0.1.1：网关的「空占位」过滤把承载 arguments 分片的帧整帧删掉
#       → 客户端只收到一个 arguments 为空的头帧 → agent 报「缺少必选参数」；
#     - v0.1.2：上一版的「函数名回填」让每一帧都带 name
#       → 按 name 累加的客户端（WorkBuddy CLI）把名字拼成
#         "TaskCreateTaskCreate…" → Tool Not Found → 整轮 agent 失败。
#
#   两次都靠「真实上游帧的固定样本」才复现。但样本当时只写进了测试代码里：
#     - 换测试文件就没有了；
#     - 别的协议路径（Responses / Anthropic）想用同一份样本得复制粘贴；
#     - 上游改形状时，没有一个「上游形状变了」的地方会亮红灯。
#
# 固化之后：一份样本，多处引用；上游形状变了，固件与断言一起暴露。
#
# 格式：一个文件一行，一行一个完整的 SSE 帧（含 `data: ` 前缀），
# 以空行结束一段流。回放时按行拆开即可，不需要解析器。

chat-basic.sse
    最普通的流式回复：role 开头 → 正文分片 → finish_reason。
    用来守住「正文不被误删」「usage 帧不被吞」这类基础形状。

tool-calls.sse
    一次完整的工具调用：头帧（id/type/name）→ 参数分片帧（name 为空串）→
    收尾占位帧（name 与 arguments 都空）。
    2026-10-25 从 243 实测抓取，模型 deepseek-v4-pro。
    **这是最重要的一份**：v0.1.1 与 v0.1.2 的两个 bug 都只在它上面显形。

tool-calls-parallel.sse
    三个并行工具调用（各自的 index 独立）。
    用来守住「并行调用不串名」——v0.1.2 的拼接 bug 在单调用场景下看不出来。

error-11140.sse
    上游账号级风控：HTTP 403 + code 11140。
    用来守住「11140 必须先于 401/403 判定」——判错会把被限流的号当坏凭据停掉。

error-14003.sse
    上游软限流：HTTP 429 + code 14003（too many requests）。
    用来守住「429 + 14003 证明凭据被接受 ⇒ 解除未验证标记」。
