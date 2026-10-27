# WorkBuddy2API-Hub v0.1.0 · BUG 修复清单

> 本清单**严格对应 v0.1.0 的最终代码**。每条给出：症状 → 触发条件 → 根因 → 改法 → 证据位置。
> 「已修复」条目均已通过上游自带 225 个测试套件（`python tests/run_all.py`）与本项目自建的 3 个本地测试。
> 「已回退」条目是审计提出、但经回归证明是上游**刻意设计**的条目，保留记录以免后人重复踩。

- 基线：上游 `ardeyouxipianyi/workbuddy2api-hub @ d1a8913`（2026-10-24）
- 本地补丁层：`dashboard.html` / `wb_tasks.py` / `wb_scheduler.py` / `wb_proxy.py`
- 回归网：225 个套件（166 Python + 59 JS），`python tests/run_all.py --jobs 4`

---

## 一、审计范围与方法

按模块分区做只读审计（每个分区独立进行，避免基线判断互相污染）：

| 分区 | 范围 | 结论条数 |
|---|---|---|
| 分区 1 | `wb_proxy.py` 1–8000（基础设施/鉴权/用量统计/限额守卫） | 31 |
| 分区 2 | `wb_proxy.py` 8000–17000（上游调用/协议转换/重试/选号） | 39 |
| 分区 3 | `wb_proxy.py` 17000–23310（Anthropic/Responses 转换/HTTP 处理器/面板 API） | 39 + 11 条安全 |
| 分区 4 | 其余 28 个模块（账号池/凭据/配置/目录/计价/任务/工具） | 见附录 |

审计方法要求：每条结论必须给 `文件:行号` 并附关键代码片段；不确定的标「疑似」并写明不确定点；
找不到的写「未发现」并列出搜索关键词，**不允许断言某能力不存在**。

---

## 二、已修复清单

### 2.1 任务引擎（`wb_tasks.py`）

| # | 症状 | 触发条件 | 根因 | 修复 |
|---|---|---|---|---|
| T1 | **任务上报的桌面指纹与主对话链路自曝两个版本号**：任务链路说 5.5.6、主链路说 5.8.3 | 每次成长任务上报 | 补丁里把 `DESKTOP_UA` 硬编码成 `"WorkBuddy/5.5.6 … CLI/2.137.1"`，覆盖了同一文件顶部引用的 `wb_identity.DESKTOP_UA_CN` | 删除硬编码，UA 唯一真源改为 `wb_identity.DESKTOP_UA_CN`（国内版 5.8.3，2026-10-18 两区桌面端实跑记录） |
| T2 | **设备标识形态是「网关特征」**：machineId/sessionId 为 36 位裸 hex | 同上 | 补丁自建了 `_derive_device_id`（sha256 前 36 hex） | 改调上游 `wb_fingerprint.get_desktop_fingerprint()`：machineId/sessionId 为**带连字符 UUID**（官方客户端取自 OS 硬件 UUID / 进程 randomUUID） |
| T3 | **请求 id 含稳定前缀**：42 字符且前 36 位由 uid 派生 | 同上 | `X-Request-ID` 用了 `_derive_device_id(uid,"req")` + 6 位毫秒尾巴 | 改用上游 `wb_fingerprint.generate_request_id()`：32 位**每条随机** hex，不掺 uid（掺 uid 派生前缀本身就是一条稳定的网关特征） |
| T4 | **领奖判据可能在上游没记账时就去领奖**，必然吃 400 `task not completed` | `accept_status` 短暂变 `completed` 而进度未落账（awaiting_progress） | 判据是 `status == "completed" or cur >= tgt`，而 `status` 就是 `accept_status` | 判据改为只用 progress 口径：`if tgt > 0 and cur >= tgt`。依据 workbuddy2api-panel 的补领清扫（`internal/panel/tasksweep.go:3-5`、`internal/upstream/tasks.go:273`：`Claimable = !claimed && tgt > 0 && cur >= tgt`） |
| T5 | **「未知但已达标」的任务会抛 TypeError**，补领清扫链路整条崩 | 上游新增了不在 `TASK_SPECS` 里的任务，且它已达标 | 日志取 `spec['name']`，而未知任务的 `spec` 是 `None` | 日志统一用 `name` 变量（已知任务取表名，未知任务取上游返回的名字，再兜底 code） |
| T6 | **「未知任务」判断排在「已达标→领奖」之前**，陌生任务永远扫不进领奖队列 | 同上 | 两个分支顺序颠倒 | 领奖分支前移；未知任务分支保留但挪到其后。依据同款 Go 实现的「补领清扫」语义 |
| T7 | 账号对象缺少 `nickname` 属性时调度器整轮崩（`AttributeError`） | 上游测试的最小账号桩；真实场景是部分初始化的账号对象 | 4 处直接 `account.nickname` | 改为 `getattr(account, "nickname", "")` 兜底 |

### 2.2 调度器（`wb_scheduler.py`）

| # | 症状 | 触发条件 | 根因 | 修复 |
|---|---|---|---|---|
| S1 | **一个账号的任务抛异常，后面所有账号都不再跑** | 任一账号的签到/旅行/连登/成长任务抛出未捕获异常 | 账号循环内 8 处任务调用**全都没有 try/except**，异常直接冒泡出整轮巡检 | 每账号的任务块整体包一层 `try/except`，记一行日志后继续下一个账号（「这一轮没跑」与「跑了但这个账号失败」在日志里必须能区分开） |

> 这条是被上游测试 `_test_schedule_hours.py` 逼出来的：上游测试用的账号桩没有 `headers` 属性，
> 而成长队列在 01:00 轮被调用 → 暴露了「单账号失败打断整轮」这个一直存在、但此前无人触发的问题。

### 2.3 网关主文件（`wb_proxy.py`）

| # | 严重度 | 症状 | 触发条件 | 根因 | 修复 |
|---|---|---|---|---|---|
| P1 | 高 | **账号并发名额永久泄漏**：某账号的 `_ACCOUNT_INFLIGHT` 只增不减，累计到上限后该账号被永久踢出调度，面板只显示「无可用账号」 | 面板开启 `account_max_concurrent` 后，上游返回 HTTP 400 / 404 / 405 / 409 / 413 等未分类状态码 | 重试循环里 `except HTTPError` 的裸 `raise` **跳出整个 `while` 循环**，而名额归还写在循环之后 → 被跳过 | 两处裸 `raise`（400 透传、未分类状态码）之前先归还名额并清空标记 |
| P2 | 高 | **非法 UTF-8 请求体让处理线程永久挂起** | POST 带 `Content-Length: N`，body 含非 UTF-8 字节 | `.decode("utf-8")` 抛 `UnicodeDecodeError` 时 `_body_consumed` 仍是 `False`，而 body 已从 socket 读完 → `_discard_body()` 再读同一段 Content-Length → 永久阻塞 | 先记账再解码；解码改 `errors="replace"`（与 chunked 路径统一） |
| P3 | 高 | **Slowloris：连上不发数据即可无限占线程** | 任意 TCP 连接握手后不发请求行 | `Handler` 未设 `timeout`，`socketserver` 因此不 `settimeout`；`ThreadingHTTPServer` 每连接一线程且无上限 | `Handler.timeout = 60` |
| P4 | 高 | **`glm-5v-turbo` 完全无法被服务**：任何请求都返回「无可用账号」 | 请求该模型且未指定 realm | `detect_model_realm()` 的 cn_only 集合**漏了** `glm-5v-turbo` → 判成 intl；而它只有国内版出口提供；intl→cn 兜底又因 `exclusive_realm()` 判它是 cn-only 而拒绝 → 死路 | `detect_model_realm()` 改为查 `exclusive_realm()`（唯一事实源），删除自己那份互相矛盾的硬编码集合 |
| P5 | 高 | **`minimax-*` 被前缀一刀切钉到 cn 出口**，只有国际账号的部署直接失败且无法兜底 | 请求 `minimax-m3.1-flash` 等 | `detect_model_realm()` 用 `startswith("minimax-")` 一刀切，而 `exclusive_realm()` 认为它是双出口模型 | 同 P4，统一查表 |
| P6 | 中 | **流式与非流式给出的工具调用不一样**：流式会把没有 `function.name` 的 tool_call 透传给客户端（OpenAI SDK 直接抛错），非流式会剔除 | 上游在流末尾发 `tool_calls:[{function:{name:"",arguments:"{}"}}]` 占位 | `clean_chunk` 只处理 `function_call` 一种拼写，漏了 `tool_calls[].function.name` | 在 `clean_chunk` 里补上与非流式一致的过滤 |
| P7 | 中 | **客户端传 `n>=2` 时，后面 choice 的内容被整帧丢弃** | 上游真的回多条 choice，其中一条是空占位 | 早退写成 `return ""`（丢整帧）而不是 `continue`（只丢这条 choice） | 改为逐条丢弃；全部 choice 都是空占位时才丢整帧 |
| P8 | 中 | **客户端声明的服务端工具被静默改写成没有 name 的 function**，上游返回 11129/11101 或工具静默失效 | 面板 `local_web_tools` 关闭（默认）时客户端带 `tools:[{"type":"web_search"}]` | `else` 分支无条件把 `type` 改成 `"function"` | 只在 `type` **缺失**时补 `"function"`，其余类型原样透传 |
| P9 | 中 | **Responses 风格的 `input_text` 分片绕过指纹清洗** → 上游 11128 | 客户端把系统提示写成 `content:[{"type":"input_text",…}]` | `sanitize_content` 只认 `type == "text"` | 白名单补 `input_text` / `output_text` / `summary_text` |
| P10 | 中 | **SSE 首帧带 UTF-8 BOM 时整帧丢失** | 上游或中间 CDN 在首字节前加 BOM | BOM 不是空白字符，`strip()` 去不掉 → 该帧既不匹配 `data:` 也不匹配注释前缀 | `strip_data_prefix()` 额外 `lstrip("\ufeff")` |
| P11 | 中 | **429 正文里的重置时刻解析不出来** → 退化成「整个凭证冷却 60s→2h」而不是「只冷却该模型」 | 响应体同时含 `\uXXXX` 转义与原生 UTF-8 中文 | `unicode_escape` 按 latin-1 逐字节解码，原生中文变乱码 → 按「将在」匹配重置时刻永远失配 | 只在正文无非 ASCII 字符时整体解码；混合情况改为逐个还原 `\uXXXX` |
| P12 | 中 | **60 秒内单个账号可切换超过 4 次身分**（`MAX_PRODUCT_SWITCHES` 被绕过） | `_SWITCH_LOG` 累计到 500 条后触发全清 | 全清等于把所有账号的切换计数一起归零 | 改为 LRU 淘汰最老的一批 |
| P13 | 中 | **整池暂停（30 分钟）被面板「测试账号」按钮反复续期**，永远红着 | 暂停期间运营者点测试 | 每次触发都 `max(held, now + HOLD)` 往后推 | 暂停生效期间保持原到期时刻，不延长 |
| P14 | 中 | **一次环境变量笔误就让整个网关起不来**（`ImportError`，不是请求期失败） | `WB_MAX_PAYLOAD_BYTES=16m` / `WB_CHAT_SLOT_WAIT=30s` / `WB_AFFINITY_MAX_MSGS=400条` 等 | 模块级裸 `int()` / `float()` | 新增 `_env_int()` / `_env_float()` 容错助手（非法值回落默认），并替换全部 8 处裸解析 |
| P15 | 中 | **`PORT` 非数字时 argparse 之前直接崩**，只看到一串栈 | `PORT=abc` | `--port` 的 default 在 `parse_args()` 里求值，异常无人接 | 提前解析，给一句人话并 `SystemExit` |
| P16 | 中 | **面板上编辑任意一个 API Key 行并保存，会把该 Key 的并发上限静默清零** | 面板 → 设置 → API Key → 改名字 → 保存 | 前端总是提交 `concurrency_limit`，后端「字段缺省才沿用已存值」的保护因此失效 | 见附录「待办」（本轮未改，涉及前后端字段契约） |
| P17 | 低 | `gateway_hint` 把 requestId 里的数字串误判成账号风控 | 403 正文的 requestId 恰好含 "11140" | 裸子串匹配 | 改用结构化正则 `_RESTRICTED_403_CODE_RE` |
| P18 | 低 | `is_transient` 用裸数字子串判 5xx，端口号 / 请求 id 里的 "502" 会被当成网关错误 | 错误文本里恰好含这些数字 | 裸子串匹配 | 5xx 数字改成有边界的正则匹配 |
| P19 | 低 | `normalize_usage_cache_aliases` 尾部有一段**永远执行不到的死代码** | 无 | `return usage` 之后又写了一遍同样的逻辑 | 删除重复块 |
| P20 | 低 | `enrich_agent_models` 对非 dict 条目直接 `AttributeError` | 看板/客户端传 `models=["gpt-5.5"]` | `(item or {}).get("id")` 对字符串调用 `.get` | 非 dict 条目原样带过去 |
| P21 | 低 | `get_desktop_fingerprint()` 是**死代码**：定义了完整的 5.8.4 桌面指纹，全仓零调用 | 无 | 上游写了但没接 | 本地补丁改为调用它（见 T2） |
| P22 | 低 | `drop_truncated_tool_calls` 生产代码零调用，逻辑与内联版重复 | 无 | 复制粘贴 | 本轮未合并（见附录「待办」） |

### 2.4 凭据与设置的落盘（`wb_atomic.py` 新增，`wb_accounts.py` / `wb_settings.py` 接入）

| # | 严重度 | 症状 | 触发条件 | 根因 | 修复 |
|---|---|---|---|---|---|
| P23 | 高 | **凭据文件同机其他用户可读**：账号 token 明文写在 `accounts/<uid>.json`，权限实测 `0o666` | Linux 上 umask 022 是常态（Docker volume 权限同样默认） | `open(tmp, "w")` 的权限是 `0666 & ~umask`；同项目 `wb_agents.py` 早已用 `WRITE_MODE=0o600` 做了同一件事（注释：「DSH 在凭据文件可读时拒绝启动」），但最关键的两个文件没跟上 | 新增 `wb_atomic.py` 统一收口：写 tmp 后 `tighten_mode(tmp)` 设 0600，替换后文件即 0600。账号凭据与面板设置（`settings.json` 里是面板密码哈希与 API Key 明文）都接入 |
| P24 | 高 | **Windows 上凭据/设置间歇性不落盘**，面板却报告「保存成功」 | Windows 下任何进程/线程正持有目标文件的读句柄（另一个线程、杀毒软件、资源管理器预览） | `os.replace()` 在 Windows 是 `MoveFileEx`，而 Python 打开文件**不带 `FILE_SHARE_DELETE`**，目标被打开时替换必然 `PermissionError [WinError 5]`。刷新后的新 token 没写下去、面板改完 Key 旧 Key 仍生效 | `wb_atomic.replace_atomic()`：对 `PermissionError` 做有界重试（40 × 25ms = 最长 1 秒）。**重试耗尽仍失败则把异常抛出**，不静默吞掉 |
| P25 | 低 | `wb_window_probe` / `wb_quota_scan` / `wb_probes` 的状态文件写失败被 `except Exception` 吞掉，**调用方不检查返回值** → 返回「成功」但窗口记录没落盘 | Windows 上目标被占用 | 同一族问题的其它实例 | 本轮未改（见附录「待办」B3/B4/B7） |

> **为什么单独立一个模块**：审计给出的最高优先级重构建议就是「把落盘层抽成统一的 `wb_atomic.py`」——
> 一次改动同时覆盖 10 处 `os.replace` 的权限与重试语义，也消掉「同一个项目里两套凭据文件标准」这个设计缺陷。

### 2.5 上游 v1.8.0 的两个修复（v0.1.3 并入）

本项目的基线是上游 `d1a8913`。上游 v1.8.0 相对该基线只有 3 个提交、13 个文件、
+710 −59（其中 +500 余为测试），其中两个是**功能修复**，已并入 v0.1.3；
第三个是发版提交（只改版本号与 CHANGELOG），不并入 —— 本项目版本号是自有版本线。

| # | 修复 | 现象 | 根因 | 修复 | 回归 |
|---|---|---|---|---|---|
| U1 | 上游 #644：stderr 日志通道不再阻塞请求线程 | 外部看「服务健在、只有面板坏了」：`/health`、`/realm`、`/panel/status` 照常 200，其余端点全部空响应 | `log()` 在请求线程里直接 `sys.stderr.write()` + `flush()`。已有的 `try/except` 接得住「写抛异常」，接不住「写阻塞」——stderr 是「读端还在却没人消费」的管道时，缓冲写满后 `write()` 永久不返回且不抛异常。而 `send_response()` 先调 `log_request()`，于是每个请求都停在响应头发出之前 | 请求线程只对有界队列（2000 行）`put_nowait()`，真正的写交给唯一守护线程；它阻塞也只阻塞它自己。队列满则丢行并计数（首次与每 1000 行以 WARN 记进看板日志，不再往堵住的通道上写）。退出路径用 0.5s 超时刷完已入队的行 | `tests/_test_dead_stderr_keeps_serving.py`（补「没人读的管道」与「stderr 的写不在调用 `log()` 的线程上」两组断言） |
| U2 | 上游 #647：国际版沙箱证书失败时降级到活跃上报 | 国际版每日活跃整轮判 `ok: false`，面板上一片「集体封号」的错觉（实际积分照发，丢的只是连登） | 上游给网页通道分配的 ACP 沙箱域名 `*.workbuddy.cloudagent-app.net` 解析到腾讯云 EdgeOne，边缘节点下发的是 `*.cdn.myqcloud.com` 通配证书，Python ssl 必然 `Hostname mismatch` —— 这一轮重试永远不会成功。旧实现却把它当成「这一轮没跑完」按预算重试到底 | ① `report_activity()` 不再只限国内版，国际版按自己的 base（`www.workbuddy.ai`）打 `POST /v2/report`；② `fetch_streak_days()` 按账号出口选 base，连登天数正确回显；③ 证书类失败且新开关「证书失败改走活跃上报」打开时，改走一次活跃上报并把当天算完成（**开关默认关，关着时行为与升级前完全一致**：仍如实报失败、仍不写 `lastDailyChat`）；④ 非证书类失败（沙箱没就绪、轮次超时、业务拒绝）一律不降级 | `tests/_test_daily_chat_activity_fallback.py`（12 项：证书文本判定、开关默认关与往返、国际版 base 选择、降级成功 / 上报被拒 / 非证书失败不降级 / 国内版不受影响） |
| U3 | U1 与本项目凭据打码的合并（`wb_proxy.py` 的 `log()`，唯一冲突点） | 若照搬上游写法，日志里会漏掉已登记的运行时凭据 | 上游 `log()` 是 `f"... {_mask_text(str(msg))}"`，本项目 v0.1.0 起是 `f"... {_mask_text(_mask_secrets(str(msg)))}"` —— 上游没有 `_mask_secrets`（账号 token、面板 API Key 不打码），本项目没有异步队列 | **取上游的异步队列写法 + 保留本项目的 `_mask_secrets`**。前者保证写 stderr 卡住时不阻塞请求线程，后者保证日志不漏凭据，缺任何一边都会在对应场景出问题 | 与 U1 同一套件；另由 `tests/_test_nickname_privacy.py` 覆盖打码口径 |

> **U2 的开关默认关**：这是本项目对「行为变更」的一贯取舍 —— 新能力默认不改变既有行为，
> 由运维在设置页显式开启。升级后不开这个开关，国际版每日活跃的失败表现与升级前完全一致。

### 2.6 账号池治理：冷账号停用 + 已验证账号恢复（2026-10-26 晚，非代码修复）

这不是代码 BUG，而是**部署侧配置治理**，记录在此以免下次重复踩。

| # | 问题 | 现象 | 根因 | 处置 |
|---|---|---|---|---|
| P1 | **冷账号仍留在派发池** | 客户端连续收到 429，日志 `upstream account restriction in effect`；两次连坐（19:50:47、20:33:22）整池暂停 1800s | 19 个账号里 **9 个从未成功服务过**（`usage.jsonl` 中 `completed` 次数 = 0）。上游对这些「从未被激活的凭证」判「异常行为」→ 403 11140 → 账号冷却 2h → 900s 内 2 次即触发整池连坐 | **停用 9 个冷账号**。派发只走 `enabled=True`（`pick_account` 要求 `a.enabled and a.access_token and a.ready(model)`），停用即消除炸弹，**容量不降反升** |
| P2 | **已验证可服务的账号被误停用** | 可派发账号一度只剩 5 个 | 批量停用时把「成功 16~54 次」的正常账号一起停了 | **恢复 5 个**（有 `completed` 记录且 `restrictedFor = 0`）。池子恢复到 10 个可用 |
| P3 | **超长会话持续膨胀上下文** | 单账号 12h 烧掉 159.8 万 token（第二名才 3633）；会话 in-token 达 6.6 万且在涨 | 用户会话一直不重开，上下文每轮累积 | 需**开新会话**（客户端侧操作，网关无法代劳）。已加巡检告警：启用账号积分余量 < 20% 即报 |

**判据**（脚本 `ops-scripts/disable_remaining_cold.sh` / `restore_verified_accounts.sh`）：

| 类别 | 判据 | 动作 |
|---|---|---|
| 冷账号 | `usage.jsonl` 中 `completed` = 0 | 停用 |
| 已验证可服务 | 有 `completed` 记录 **且** `restrictedFor = 0` | 可恢复启用 |
| 11140 受限 | `restrictedFor > 0` | 保持停用，等冷却到期再评估 |

**易混口径**：面板的 `inCooldown` / `restrictedFor` 与磁盘的 `enabled` 是两回事
——`enabled=False` 的账号仍会出现在 `inCooldown` 列表里；
判「冷账号」要看 `usage.jsonl` 的 `completed` 计数，不是看 `enabled`。

**回滚点**：`/opt/workbuddy2api-hub/backups/accounts-before-{cold-disable,cold-disable2,restore}-*.tar.gz`。

### 2.7 未验证账号护栏（v0.1.4，代码修复）

2.6 节是**人工治理**——判据正确，但护栏本身不存在：任何账号只要在面板上被点一下
「启用」就会重新进池派发。这一点在 2026-10-26 晚到 10-27 凌晨被验证了两次。

| # | 问题 | 现象 | 根因 | 修复 |
|---|---|---|---|---|
| P4 | **账号池里没有「这个号能不能用」的概念** | `ready()` 只看 enabled / 凭据 / 冷却 / 额度 / 模型限制，**不看上游接不接受这个凭据** | 从未实现 P1-8 的凭据三态预验证 / 入池闸门 | `Account` 新增 `unverifiedSince` / `unverifiedReason` / `verifiedAt`（持久化）；`ready()` 最前面拦截未验证账号 |
| P5 | **冷账号判据本身不准** | 「网关侧 `completed` = 0」在 19 个账号上命中 4/9，**漏判 5 个、误判 5 个** | 把「没经本网关派发过」当成了「没被上游激活过」 | 改用**上游额度 `credits.used == 0`**（凭据从未被上游消费过）：命中 **4/4**，**0 误判** |
| P6 | **11140 冷却到期即自动回池 → 连坐循环** | 账号被判非法 → 冷却 2h → 到期自动回池 → 再被派发 → 再 11140 → 再触发一次整池连坐 | `restricted_until` 到期只代表「本地不再拦」，不代表「上游已解封」 | 11140 账号**隔离到有一次成功请求为止**（`note_success` 或被上游接受的 429/14003）。语义变更，测试同步更新（`_test_restricted_403.py`） |
| P7 | **验证通过的号会被重新隔离（死锁）** | 单测 `test_pool_does_not_re_mark_an_already_verified_account` 抓出 | `mark_verified()` 只清标记，而上游额度快照的 `used` 不保证立刻更新 → 重启后按 `used == 0` 又被隔离 | 新增 `verifiedAt` 字段**把「上游接受过它」这个事实本身持久化**；`upstream_never_consumed()` 遇 `verifiedAt > 0` 一律返回 False |
| P8 | **护栏状态不可见** | 池子「可用数少于启用数」时，运维只能看到数字对不上，猜不出原因 | `/health` 与面板都没有这个维度 | `/health` 新增 `accounts_unverified` / `accounts_upstream_unused`；面板账号徽章新增「未验证」（红色，排在「可用」之前）+「当前禁用」清单新增对应条目 |

**三态与上游应答的对应**（借鉴 `CangShui/workbuddy-gateway` 的 `credverify.go`）：

| 上游应答 | 含义 | 网关动作 |
|---|---|---|
| HTTP 403 / code 11140 | 上游判非法 | 隔离，不参与派发 |
| HTTP 429 / code 14003「too many requests」 | **凭据被接受**，只是限流 | 解除隔离 |
| 跑成一次 | 凭据被接受 | 解除隔离 |
| `credits.used == 0` 且从未验证过 | 可能尚未被上游激活 | 隔离，等待验证 |

14003 这一条是实测得来的（2026-10-27 00:19，对两个从未经本网关派发过的账号各打
**一个**最小请求，得到的正是 429/14003 而非 11140）。

**护栏不做的事**：不改 `enabled`（面板显示与磁盘永远一致，「停用/启用」仍是运维的决定
权）；不主动向上游发探针（探针本身消耗配额且可能触发 11140）；额度快照缺失时不隔离
（拿不到证据不等于有罪）。

**验证**：新增 `tests/_test_unverified_guard.py`（17 项断言）+ 回归 **228 passed / 0 failed**。
生产侧按新判据处置：停用 `f7102b2e`（`used == 0` 且已被启用），恢复 `292b43a8` /
`0472cc9f` / `6d3fa971`（被误停的正常账号）。

**回滚点**：`/opt/workbuddy2api-hub/backups/accounts-before-pool-restore-20261027-001814.tar.gz`。

### 2.8 连坐暂停跨重启存活 + 两个真 bug（v0.1.6）

本节的四条来自对**上游 `ardeyouxipianyi/workbuddy2api-hub`** 与**第三方运维补丁线
`wushuai0802/workbuddy2api-hub-ops-patch`**（新版本 `v1.8.0-ops.20261026.1`）的复核。
完整分析见 `research/可借鉴要点清单.md` 第十节。

| # | 严重度 | 症状 | 触发条件 | 根因 | 修复 |
|---|---|---|---|---|---|
| P22 | **高** | **连坐暂停在容器重启后消失** → 冷账号重新上桌 → 触发**第二轮**整池连坐 | 连坐期间重启容器（升级 / 崩溃 / OOM kill） | 区域暂停（`_RESTRICTION_HOLD`）**只在内存里**。「上游刚把这一区域的账号判非法」这个事实在重启后消失，池子立刻恢复派发 | 暂停落盘到 `usage/restriction-state.json`（原子写 + 0600 + 账号哈希 + 有界 + 坏文件不发明暂停），重启后恢复**原始到期时刻**。落地前提已核实：243 的 `usage/` 是持久卷 |
| P23 | 中 | **Anthropic 协议空池返回 502**（Chat/Responses 返回 503） | 池子里没有可用账号时用 Claude Code / Anthropic SDK 调网关 | 该路径判空池用 `"no account" in low`，而真实抛出的消息是 `"no usable account for realm ..."` —— 子串 `"no account"` 不出现在 `"no usable account"` 里（中间隔着 `"usable "`） | 抽成共享的 `_empty_pool_status()`，三条协议共用；**账本记的码与响应的码也统一**（原来账本记 502、响应给 503） |
| P24 | **高** | **上游的配额类 code 被误判为账号级风控** → 冷却一个健康账号 + 触发整池 30 分钟暂停 | 上游返回 `{"code":111400,...}` 这类相似 code | `11140` 的 code 匹配**没有右边界** | 正则收紧为 `11140(?=["\s,}]|$)`；措辞标记另外要求 403 语境（429/6004 的正文里也有「已被限制调用频率」） |
| P25 | 中 | **网页活跃主动探出 403 11140** —— 事故链上的「帮凶」 | 未验证账号被 daily-chat 选中 | v0.1.4 的护栏只挡**模型请求**路径；网页活跃是独立链路，`wb_accounts.py` 的 HTTPError 分支只把 11140 变成一个错误字符串（不冷却、不隔离、不计连坐） | `wb_scheduler.py` 选 daily-chat 目标时加 `is_unverified()` 闸门，未验证账号直接跳过 |

**为什么 P22 不是「加固」而是「修复同一条链的第二段」**：
2026-10-26 晚生产上的连坐在容器内 **15:33~15:35 连续触发 7 次**，涉及
`f7102b2e`（即 10-27 凌晨被拆掉的那颗炸弹）；**同一晚我们部署 v0.1.4 重启了容器**。
那 7 次里只要有任何一次跨过重启点，事故就会重演。

**顺带修掉的既有 flaky test**：
`_test_daily_chat_web_retry_backoff.py::test_upstream_500_is_a_create_phase_failure`
本身偶发失败（5 次跑里红 1 次）—— 它断言错误正文含 `'14220'`，但该用例会**真的发
HTTP 请求**，Windows 上连接偶尔被网络栈截断（`WinError 10054`），正文里就没有
14220 了。失败仍在 `create` 阶段，而用例要验的正是这个分类 ⇒ 改为断言
`phase` / `attempts` / `error` 非空，**不再断言错误文本内容**。

**验证**：新增 `tests/_test_restriction_persistence.py`（17 项）与
`tests/_test_empty_pool_status.py`（8 项）；回归 **231 passed / 0 failed / 0 skipped**。

### 2.9 告警通道安全加固（v0.1.7）

v0.1.5 新增的告警通道是本项目第一条「**外部输入 → 内部网络**」的通道
（网关会向面板用户填的 URL 真的发 HTTP 请求）。本版做了增量安全审计并加固。

| # | 严重度 | 症状 | 触发条件 | 根因 | 修复 |
|---|---|---|---|---|---|
| P26 | **高** | **SSRF**：面板用户填的 webhook URL 可指向 `file://`（读本地文件）、回环、私网、云元数据；配合 `/alerts/test` 回显异常文本构成完整读取链 | 已授权面板用户点「发送测试通知」 | `_post()` 对 URL **零校验**；默认 opener 带 `FileHandler` 且**跟随跳转** | `_assert_webhook_url()`：只允许 http/https、限长、禁 userinfo、拒绝回环/私有段/链路本地/CGNAT/保留段；**域名解析后判**（多值 DNS 全查）；`_NoRedirectHandler` 不跟随跳转；**不回显响应体**（`_safe_error_text()` 只留类型与状态码）；**保存时也校验** |
| P27 | 中 | **告警静默化**：`for_ticks=10**9` → 去抖永远攒不满 → 告警**永远不发** | 面板里把阈值设成极大值 | 阈值**只有下界没有上界** | `wb_settings._alert_thresholds()` 加上下界（min_ready 1–1000，ticks 1–100） |
| P28 | 中 | **异常导致连接被重置 + stderr 抛 traceback**（而不是 400） | 阈值填非数字（`"abc"`）或 `1e400` | `int()` 的 `ValueError`/`OverflowError` 未接住，一路冒到没有 try 的 socket handler | `_clamp_alert_int()` 接住三种异常并回落默认值 |
| P29 | 低 | 带 token 的 webhook URL 长期留在日志/日志库 | 钉钉/飞书式 URL 里带 access_token | alerts secret / URL 没进日志打码名单 | `_register_pool_secrets()` 登记 secret、URL、ServerChan key |
| P30 | 低 | 往 `thresholds` 里塞的键会被回显给面板；未校验的 dict 可落盘 | 直接调 `/settings/save` | `set_alerts()` 无 schema 校验、`alerts()` 原样回显 | 校验下沉到 `wb_settings.set_alerts()`（字段白名单 + 长度 + 阈值区间）；回显改白名单 |

**残余风险（诚实声明）**：**DNS 重绑定**仍可能绕过「解析时检查」
（先解析到公网 IP 通过校验，连接时再解析到内网）。彻底消除需要改成主机白名单，
或「解析后按 IP 直连并校验 Host/SNI」。当前实现把「任意 URL 打内网」降级为
「仅公网地址可打」，已消除本轮审计发现的全部可利用路径。

**验证**：新增 `tests/_test_alerts_security.py`（27 项）；回归
**232 passed / 0 failed / 0 skipped**。完整审计记录见
`SECURITY-v0.1.0.md` 第九节（含 12 项新增渗透测试记录）。

---

## 三、已回退（审计提出但经回归证明是上游刻意设计）

| # | 审计提出的「缺陷」 | 回退原因 | 证据 |
|---|---|---|---|
| R1 | `estimate_tokens` 对纯中文凭空多算 1 个 token | 上游 `max(1, int(other/3.6))` 是**护栏**：1~3 字符的短请求不能估成 0 token | `_test_hotpath_savings.py::test_matches_the_character_loop` 逐字比对字符循环参考实现 |
| R2 | 登录限流在反代部署下可被 `X-Forwarded-For` 伪造绕过 | 上游**刻意支持**「只设 XFF 的反代形态」；伪造防护靠的是「非回环对端一律按 peer IP 分桶」，不是砍掉 XFF 回落 | `_test_login_ratelimit_proxy.py` 4 条用例（首跳取用、host:port 形态、畸形头回落链） |
| R3 | 工具结果与调用取交集 → 孤儿结果被整条丢弃 | 上游**刻意**剔除孤儿结果（有专门测试钉住） | `_test_upstream_repairs.py` 的「a result with no call is reported as a change」「the orphan result is removed」 |

> **这三条的残余风险**见安全审查报告 S-14：R2 的前提是反代必须**覆盖**（而非追加）`X-Real-IP`。

---

## 四、版本锁定

上游 CI（`.github/workflows/tests.yml` 的 `version-assert` 任务）要求：源码里的两处版本串必须一致，
且 release tag 必须等于源码版本。v0.1.0 沿用同一约束：

| 位置 | 改前 | 改后 |
|---|---|---|
| `wb_proxy.py` overview 的 `"version"` | `1.7.33` | **`0.1.0`** |
| `wb_proxy.py` 的 `server_version` | `wb-proxy/1.7.33` | **`wb-proxy/0.1.0`** |
| `VERSION` 文件 | 不存在 | **`0.1.0`** |
| `wrt/openwrt/workbuddy2api/Makefile` 的 `PKG_VERSION` | `1.7.33` | **`0.1.0`** |

---

## 五、附录：审计发现但本轮未修（按优先级排队）

### A 级（建议下一版处理，需要设计或跨前后端改动）

| # | 描述 | 出处 |
|---|---|---|
| A1 | **面板 Key 的 `concurrency_limit` 字段契约断裂**：后端 `runtime_settings_view()` 的 key 条目没有该字段 → 前端恒读成 0；前端又总是提交该字段 → 后端「缺省沿用已存值」的保护失效，保存即清零 | `wb_proxy.py` runtime_settings_view / `_handle_settings_save` |
| A2 | **`open_upstream` 660 行 / 7 个 except / 5 套并行状态**，是全文件最高风险改动点；P1 的名额泄漏正是它的结构问题 | `wb_proxy.py` 约 14600–15300 |
| A3 | **三套并行的上游帧清洗实现**（chat 流 / Responses 流 / 非流式聚合），同一套语义已经漂移出可复现的不一致 | `wb_proxy.py` 的 `clean_chunk` / `clean_responses_frame` / `aggregate_stream` |
| A4 | **模型归属仍有第三份事实源**（`wb_catalog` 的 `STATIC_*_MODELS`）。P4/P5 修好了 `detect_model_realm` ↔ `exclusive_realm`，但目录快照仍可能与这两张表不同步 | `wb_catalog.py` vs `wb_proxy.py` |
| A5 | **面板无按 API Key 维度的用量聚合**：日志逐行记了 `key`，但所有聚合口径都只按 model/realm/account 切分 → 多人共用网关时无法回答「哪个 Key 花了多少钱」 | `wb_proxy.py` 的 `_empty_stats` / `_fold_usage_snapshot` |
| A6 | **面板长耗时动作同步占用请求线程**（一键跑成长任务 / 一键测试 / 全部刷新），且可被并发重复触发 → 重复领取、重复打上游 | 面板任务/账号相关路由 |
| A7 | **`_handle_settings_save` 690 行 / 约 70 个平铺分支**；`Handler` 类 4612 行 / 约 80 个方法 | `wb_proxy.py` |
| A8 | **鉴权是两套并行判定**：`_is_panel_route` 白名单 + 逐路由自查 `_panel_ok()`，四档信任语义（公开 / API Key / 面板会话）交叉 | `wb_proxy.py` |

### B 级（低风险、可随手做）

| # | 描述 |
|---|---|
| B1 | `_pct` 定义后从未被调用（死代码） |
| B2 | `rewards_ran` 只赋值、从不被读（死代码） |
| B3 | `find_namespace_tool` / `NAMESPACE_TOOL_MESSAGE` / `REJECT_NAMESPACE_TOOLS` 三个符号全仓零引用 |
| B4 | `drop_truncated_tool_calls` 生产路径零调用，与内联实现重复 |
| B5 | `_perf_stats_uncached` 与 `_perf_payload` 是两份同构的渲染代码 |
| B6 | 窗口折叠口径三份重复，`since` 判空口径不统一（`if since` vs `if since is not None`） |
| B7 | 跨天归档先写 `state["day"]` 再做归档，归档异常时该天被永久跳过 |
| B8 | 写盘临时文件名固定（`X.tmp`），两个进程共用同一目录会互相踩 |
| B9 | `GET /logs` 的 `limit` 未夹上限（对比 `/usage/recent` 有 `min(1000,…)`） |
| B10 | `POST /accounts/delete` 对不存在的 uid 也返回 200 |
| B11 | `GET /accounts/credits/detail?uid=<不存在>` 静默回退到账号池第一个账号（面板显示的是别人的数据） |
| B12 | `--import-desktop` 不会退出（帮助文本写的是 "then exit"） |
| B13 | `/v1/responses` 与 `/v1/chat/completions` 流式响应在上游未回 usage 时，prompt_tokens 被**猜**（恒 1 或 completion 的半数） |

---

## 六、验证方式

```sh
cd release
python tests/run_all.py --jobs 4 --logs suite-logs   # 225 个套件
```

本项目自建的三个本地测试（补丁层的回归网）：

```sh
python deploy/test_task_patch.py       # 任务引擎补丁，40+ 断言
python deploy/test_log_mask.py         # 运行时凭据脱敏，15 断言
node   deploy/test_multifile_import.js # 面板多选导入，13 断言
```

三个本地测试都带**控制组**：拿纯净上游的同名文件去跑必须失败，否则测试不敏感。

```sh
python deploy/test_task_patch.py _src/workbuddy2api-hub-main   # 必须 exit 1
python deploy/test_log_mask.py   _src/workbuddy2api-hub-main   # 必须 exit 1
```
