# WorkBuddy2API-Bridge 技术架构文档（v0.1.0）

> **文档基准**：本仓库 `release/` 发布树，版本 `0.1.0`（`VERSION` 文件）。
> **项目名称**：WorkBuddy2API-Bridge（上游为 WorkBuddy2API-Hub）。代码标识符、镜像名 `workbuddy2api-hub`、
> compose 服务名 `wb-proxy`、HTTP `Server` 头 `wb-proxy/<版本>` 保持不变，以免影响既有部署与升级链路。
> **验证时间**：2026-10-28，实跑 `python tests/run_all.py --jobs 4` → **241 passed / 0 failed / 0 skipped**。
> **文档定位**：这是写给维护者看的**代码事实**文档 —— 每一条结论都对应真实文件与行号，不是产品宣传。
>
> **与上游的关系**：本仓库 fork 自 [`ardeyouxipianyi/workbuddy2api-hub`](https://github.com/ardeyouxipianyi/workbuddy2api-hub)（上游 v1.8.0，MIT）。
> 本项目使用自有版本线；上游 `main` 分支原样保留在 fork 内供比对同步。
> 上游地址：`origin` = fork，`upstream` = 官方。
>
> **本版新增机制**（在途感知选号、半开探测、错误分类、恢复时刻夹取、金丝雀固件）
> 见 [`CHANGELOG-v0.1.0-formal.md`](CHANGELOG-v0.1.0-formal.md)；
> 缺陷与安全项见 [`BUGFIXES-v0.1.0-formal.md`](BUGFIXES-v0.1.0-formal.md) 与
> [`SECURITY-v0.1.0-formal.md`](SECURITY-v0.1.0-formal.md)。

---

## 一、项目是什么

一个**多账号 AI 网关**：把 WorkBuddy / codebuddy.ai（原 WorkBuddy.ai 国际版 + codebuddy.cn 国内版）的账号，
包装成 **OpenAI 兼容接口**（`/v1/chat/completions`、`/v1/responses`），并附带一个 Web 看板做监控与管理。

三个协议入口最终**收敛到同一次上游 Chat Completions 调用**（见第三节）——
网关自身没有 Responses 上游，`/v1/responses` 是 Chat Completions 双向翻译的结果。

**零第三方依赖**：AST 全量 import 扫描 → 0 个第三方依赖，`requirements.txt` / `pyproject.toml` / `Pipfile` / `poetry.lock` 均不存在。
这是本项目最重要的结构性优势之一：**没有可被投毒的依赖链**。

代码规模（实测）：

| 项 | 数量 |
|---|---|
| 应用模块 `wb_*.py` | **32** 个 |
| 辅助脚本 | `console_tail.py`（1 个） |
| 最大单文件 | `wb_proxy.py` **24,138 行** |
| 测试套件 | **232** 个（173 Python + 59 JS） |
| 便携包托管文件 | **62** 条（`release/portable.txt`） |
| 看板 | `dashboard.html` ≈ 1.0 MB / 18,154 行，**原生 JS + 内联 CSS，零外部资源** |

---

## 二、请求生命周期（最重要的一张图）

```
客户端 (OpenAI SDK / Codex CLI / Claude Code)
   │
   │  ① /u/<ref>/ 前缀剥离 → 解析定址账号      wb_proxy.py:19810 / :19827
   │  ② 区域(realm)判定：显式 → Key绑定 → X-Realm → ?realm → 默认   :19726
   │  ③ 并发闸门：Per-Key 槽位 + 全局 chat 槽位                    :23560 / :23575
   ▼
┌──────────────────────────────────────────────────────────┐
│ 协议适配层（三入口，同一收敛点）                              │
│  Chat      :23598 → build_upstream_body   :13781           │
│  Responses :22839 → responses_to_chat     :17621           │
│  Anthropic :23218 → messages_to_chat      :18037           │
│     ↓ 三者都走 ↓                                              │
│  open_upstream_with_fallback  :16190                        │
└──────────────────────────────────────────────────────────┘
   │
   │  ④ 选账号：AccountPool.pick()  wb_accounts.py:5064
   │     4 层偏好：加权 → 临期 → 剩余额度 → 轮转
   │  ⑤ 构造身份头 + 指纹  wb_identity.build_identity_headers :458
   │     wb_fingerprint.derive_id（每账号独立设备身份）  :22
   │  ⑥ 出口：account.proxy ← proxy_slots 绑定  wb_accounts.py:4786
   ▼
上游  https://www.workbuddy.ai  或  copilot.tencent.com  或  workbuddy.cn
   │        ↑ CHAT_PATH = "/v2/chat/completions"   wb_proxy.py:329
   ▼
   ⑦ SSE 回流 → 协议形状还原 → 客户端
```

**关键设计**：上游**永远以流式形式请求**（`build_upstream_body` 无条件设 `body["stream"] = True`，`wb_proxy.py:13872`）。
即使客户端要非流式，网关也自己在客户端侧聚合（`aggregate_stream` `:16590`）。
⇒ 这保证了流式与非流式**共用同一条上游路径**，两条路的工具调用形状天然一致。

---

## 三、协议适配

### 3.1 三入口收敛到一点

| 协议 | 入口 | 翻译 | 回译 | 流式输出 | 非流式输出 |
|---|---|---|---|---|---|
| Chat Completions | `_dispatch_chat_post` `:23598` | — | — | `_chat_stream_response` `:23718` | `_chat_nonstream_response` `:23952` |
| Responses | `_handle_responses` `:22839` | `responses_to_chat` `:17621` | `chat_to_response` `:18449` | `_responses_stream_response` `:23064` | `_responses_nonstream_response` `:23151` |
| Anthropic Messages | `_handle_messages` `:23218` | `messages_to_chat` `:18037` | `chat_to_messages` `:18173` | `_messages_stream_response` `:23339` | `_messages_nonstream_response` `:23318` |

三者在 `open_upstream_with_fallback`（`:16190`）**汇合**，因此上游侧的行为、护栏、计费对三条协议完全一致。

### 3.2 Anthropic Messages 默认关闭

`_anthropic_messages_enabled()`（`:23539`）默认**拒绝**。原因写在 README：
上游对 `cache_control` 的缓存命中率只有约 11%，单次会话消耗可能超过 100 积分。
⇒ 与其假装兼容，不如默认关掉。启用需显式设 `WB_ENABLE_ANTHROPIC_MESSAGES=1`。

**已知边界**：服务端工具（`web_search` 等）上游不支持会被丢弃；`thinking` / `redacted_thinking` 不回放；
`top_k` / `cache_control` / `context_management` / `betas` 被忽略；`count_tokens` 只是网关的 CJK 感知估算，**不是**官方分词器。

### 3.3 区域（realm）选择与回退

- 判定优先级（`_request_realm` `:19726`）：显式参数 → Key 绑定区域 → `X-Realm` 头 → `?realm=` → `CURRENT_REALM`（默认 `intl`）。
- 上游地址由 `wb_identity.endpoint_for(realm, product)`（`wb_identity.py:312`）解析，表在 `:298-309`：
  国际版 → `workbuddy.ai`；国内版桌面/CLI → `copilot.tencent.com`；国内版 VSCode → `workbuddy.cn`。
- **跨区域回退有严格条件**（`:16200-16202`）：只在 `RateLimited` 或 `no usable account` 时才试另一个区域。
  **403-11140 绝不跨出口** —— 账号级风控不能靠换区域绕过。

---

## 四、流式管线（两次线上事故都在这里）

### 4.1 处理顺序（顺序本身就是修复）

`_chat_stream_response`（`:23718`）每帧的处理顺序：

1. `iter_sse_lines` `:16477` 读上游块
2. `strip_data_prefix` `:11793` 去 BOM/注释；上游 `[DONE]` 被吞掉 `:23791`
3. `usage` 摘出 `:23798-23809`
4. **`_normalize_tool_call_frames(delta, seen)` `:23815`** ← 在 clean_chunk 之前
5. `LeakedToolCallStreamFilter` `:13325` 暂扣正文 `:23827`
6. `clean_chunk` `:11908` 丢弃空 tool_calls 噪音 `:23844`
7. 收尾：`chat_finish_chunk` → `chat_usage_chunk` → `data: [DONE]` `:23852-23863`

> **第 4 步必须在第 6 步之前**。顺序反了，占位过滤会把承载 `arguments` 分片的帧整帧删掉。

### 4.2 工具调用帧规则（v0.1.1 → v0.1.2 的血泪）

上游把一次工具调用拆成多帧：

```
帧1  tool_calls:[{id, type, function:{name:"get_weather", arguments:""}}]   ← 头帧
帧2  tool_calls:[{function:{name:"", arguments:"{"}}]                        ← 参数分片
帧3  tool_calls:[{function:{name:"", arguments:"\""}}]
...
```

`_normalize_tool_call_frames`（`:11842-11905`）的最终规则：

| 帧类型 | 处理 | 锚点 |
|---|---|---|
| 头帧（有 name 且有 id） | 保留 name，记入 `seen[idx]` | `:11879-11881` |
| 续帧（name 为空） | **整个删掉 `name` 键** | `:11882-11889` |
| 续帧（重复带真名） | **整个删掉 `name` 键** | `:11894-11897` |
| 有 name 但无 id、index 未见过 | 保留 | `:11890-11893` |
| 无 name 且无 args | 作为占位丢弃 | `:11869-11871` |

⇒ **函数名在整段流里只出现一次，且只在头帧；参数分片帧根本不带 `name` 键。**

**为什么不能回填名字**：两种客户端对 `name` 的处理正好相反 ——

| 客户端 | 规则 | 空名字 | 真名重复 |
|---|---|---|---|
| OpenAI SDK | `name is not None` 就覆盖 | 抹掉真名 → 认不出工具 | 幂等无害 |
| 按 name 累加的客户端（实测 WorkBuddy CLI） | 每次出现都接上 | 无害 | **拼成一长串 → Tool Not Found** |

⇒ 唯一两边都对的形状就是 **OpenAI 原生流式的样子**。回归测试 `_test_stream_tool_call_args.py` 专门钉住这条。

### 4.3 两条管线上的工具调用

- **Responses**：名字走 `response.output_item.added`（`:18946`），参数走 `response.function_call_arguments.delta`（`:18970`）。
- **Anthropic**：名字走 `content_block_start` + `type: tool_use`（`:18388-18396`），参数走 `content_block_delta` / `input_json_delta`（`:18398-18401`）。

---

## 五、账号池

### 5.1 记录模型

`Account`（`wb_accounts.py:960`）。核心字段：

| 类别 | 字段 |
|---|---|
| 身份 | `uid`、`nickname`、`realm`(cn/intl)、`domain`、`platform`、`product` |
| 凭据 | `access_token`、`refresh_token`、`expires_at`（静态 Key 恒为 0，永不过期 `:920`） |
| 派发开关 | `enabled` `:934`、**`tasks_enabled`** `:940-943`（与 enabled **相互独立**） |
| 选择参数 | `weight`（1–100，默认 1 `:1048`）、`disabled_models` `:966` |
| 上游事实（持久化） | `restricted_until` `:988`、`unverified_since` `:1066`、`unverified_reason` `:1072`、`verified_at` `:1083`、`model_caps` `:1088` |
| 运行时态（不落盘） | `last_error`、`cooldown_until`、`model_cooldowns`、`breaker_until` 等，由 `RUNTIME_ONLY_FIELDS` `:5376` 剥离 |

### 5.2 选号算法（4 层偏好）

`AccountPool.pick()`（`:5064`），**首个非空即返回**：

1. `_pick_weighted_first` `:5142` —— 仅 `weight > 1` 的账号，平滑加权轮转
2. `_pick_expiring_first` `:5174` —— 临期积分窗口内，权重 = `max(1, round(窗口 - 剩余天数) + 1)` `:5194`
3. `_pick_remaining_first` `:5210` —— `(uid, model)` 维度，由 wb_proxy 从用量日志推送
4. `_rotate_pick` `:5365` —— 普通轮转兜底

前三层共用 nginx 式**平滑加权轮转** `_smooth_weighted_pick` `:5248`，各自维护独立轮转状态（`:4412-4422`）。

`pick_for_session()` `:5018` 额外做**会话亲和**：绑定账号仍 `ready()` 就复用 → 否则试会话内其他温账号 → 再兜底 `pick()`。

### 5.3 未验证账号护栏（v0.1.4 引入）

**判据**：`upstream_never_consumed()` `:3662` = `credits.used <= 0` **且** `verified_at == 0`。

这个判据不是拍脑袋来的 —— 生产 19 个账号上的分离度检验：

| 判据 | 命中 11140 | 漏判 |
|---|---|---|
| `credits.used == 0`（上游从未消费过） | **4/4（100%）** | 0 |
| 网关侧成功次数 = 0 | 4/9（44%） | 5 |

**三态契约**（借鉴 `credverify.go`）：

| 上游应答 | 含义 | 网关动作 |
|---|---|---|
| 403 / code 11140 | 上游判非法 | `rejected` → 隔离 |
| 429 / code 14003 | **凭据被接受**，只是限流 | `verified` → 解除隔离 |
| 跑成一次 | 凭据被接受 | `verified` → 解除隔离 |
| `used == 0` 且从未验证 | 可能尚未激活 | `pending` → 隔离 |

**关键实现细节**：
- `apply_unverified_guard()` `:4495` 在每次 `load()` 时调用，但**只打标、不改 `enabled`** —— 护栏拦的是"派不派它"，不是"状态是什么"。面板显示与磁盘永远一致。
- 拦截点在 `ready()` `:1806` 的**最前面** —— 一个被隔离的号不该因为冷却到期就漏进派发池。
- **`verified_at` 是死锁的解药**：上游 `used` 不会立刻更新，刚验证通过的号 `used` 可能仍是 0，没有 `verified_at` 就会每次重启被重新隔离 —— 一个永远锁在池外的死锁。这条被 `_test_unverified_guard.py::test_pool_does_not_re_mark_an_already_verified_account` 抓出来。
- **额度快照缺失时返回 False** —— 拿不到证据不等于有罪，宁可放过也不误伤。

### 5.4 并发上限

**不在 `wb_accounts.py`**，而在 `wb_proxy.py`：

- 全局 `_chat_slots` 信号量，`MAX_CONCURRENT_CHAT` 默认 **32**（`:112-118`）；`WB_MAX_CONCURRENT_CHAT=auto` 时按就绪账号数自动定容（`resize_chat_slots` `:436`）
- 单账号在途上限 `account_max_concurrent`（`wb_settings.py:266`，最大 10，**0 = 不限**，默认不限），由 `account_slot_acquire/release`（`:14344`/`:14363`）在选号循环外围（`:15528-15541`）执行
- Per-API-Key 并发 `_acquire_key_slot` `:23560`

### 5.5 持久化

- 每账号一个 JSON 文件，放在 `ACCOUNTS_DIR`（默认 `<app>/accounts`），文件名 `<safe_uid>.json`
- 写入：`save()` `:1490-1525` —— 临时文件带 pid/tid 后缀 → `wb_atomic.tighten_mode` 置 0600 → `replace_atomic` 原子替换
- `settings.json`（`wb_settings.py:351`）存网关级设置：排程小时、并发上限、告警 secret、身份版本、面板密码哈希、API Key —— 同样 0600
- 目录权限期望 **700**（`build-ipk.sh:216`）

---

## 六、调度器

### 6.1 槽位表

默认值在 `Scheduler.__init__`（`wb_scheduler.py:94`）：

| 小时 | 动作 | 写 activity_history？ |
|---|---|---|
| 09:00 / 21:00 | 国内签到（`can_checkin` → `checkin(trigger="scheduler")`）`:501-510` | **是** |
| 09:00 / 21:00 | 国内活跃上报（`report_activity()`）`:512-520` | **否** |
| 09:00 / 21:00 | 猫猫旅行 + 连登奖励 `:522-535` | **否** |
| 01:00 | 夜猫子成长任务 `:537-543` | **否** |
| 01:00 | 成长任务队列（`run_growth_tasks(gap=1.0, claim=True)`）`:545-561` | **否** |
| 22:00 | Token 保活（默认提前 **48h** 刷新，`KEEPALIVE_LEAD_DEFAULT_HOURS` `:54`）`:484-497` | **否** |
| 分钟级，2 个随机槽 | 国际版网页活跃 `:564-595` | **是** |

排程小时**可配置**：`settings.json → schedule` 优先，`wb_settings.schedule_hours`（`wb_scheduler.py:341`）按 key 回退。
**例外**：`growth_hours = [1]` 硬编码 `:106`，不走配置。

### 6.2 循环机制

`_run_loop` `:176`：启动跑一轮 → 之后每约 30 秒醒一次（`_next_wait` `:287`，会被下一个探针截止时间提前）。
命中判定：`cur_min == 0 and cur_hour in self.all_hours` `:206`，命中后睡 65 秒防重入。

`last_run_time` 在 `_run_cycle` 顶部打点 `:424`；`next_run_time` 由 `_calc_next_fire` `:385` 算出。
**面板显示的"下次运行"只是整点网格** —— 网页活跃槽有独立状态块 `_daily_chat_status` `:839`。

窗口探针 `:251`、配额扫描 `:269`、池健康告警 `:229` **每个 tick 都跑**，与 `self.enabled` 无关。

### 6.3 手动 vs 排程

`trigger_now()` `:404` 起线程跑 `_execute_cycle("手动立即触发", force=True)`；
**`force=True` 会让所有槽位都跑**（`:439-454`）—— 手动触发一次，等于把 01:00 的成长队列也跑了。
面板的"手动签到"是另一条路由 `_route_accounts_checkin`（`wb_proxy.py:22332`），走 `checkin(trigger="manual")`。

### 6.4 trigger 记录

传给任务调用的 `trigger` 参数**才会**进 activity_history。
调度器自己的日志原因（`"整点排程命中 (09:00)"` vs `"手动立即触发"`）**不会**落进历史表。
合法值：`("scheduler", "manual", "account_add", "account_import", "unknown")`（`wb_activity.py:30`），不认识的一律归 `unknown` `:196`。

---

## 七、活动历史（一个高频误判点）

**格式**：JSONL，`usage/activity_history.jsonl`（`wb_activity.py:22`）。

**每行恰好 8 个字段**（`ALLOWED_FIELDS` `:41`）：`ts`、`uid`、`nickname`、`realm`、`task`、`trigger`、`ok`、`message`。
`message` 截断到 200 字符并做 secret 脱敏（`safe_message()` `:127`）。

**任务类型只有两种**：`TASK_CHECKIN` 与 `TASK_DAILY_CHAT`（`:24-26`）。

**写入方**（生产代码里的 `record_attempt` 调用点；函数**定义**在 `wb_activity.py:214`）：

| 写入方 | 调用点锚点 |
|---|---|
| `Account.checkin()` | `wb_accounts.py:2910` |
| `Account.daily_chat()` | `wb_accounts.py:2798` |
| `Account.daily_chat_web()` | `wb_accounts.py:2358`（嵌套调用时 `trigger=None` 可抑制） |
| `Scheduler._record_daily_chat_hang()` | `wb_scheduler.py:715` |

**不写入的操作**：`report_activity()` `:2952`、`run_growth_tasks` / `run_night_growth` / `run_streak_bonus` / `do_cat_travel` / `claim_task` / `accept_tasks`（全在 `wb_tasks.py`）、保活 `refresh()`、配额扫描、窗口探针。

> ⚠️ **活跃上报是最容易被误判的动作**：它更新账号记录上的 `last_activity_report` 并落盘，但**不留历史行**。
> ⇒ **"activity_history 里没有 scheduler 记录" 不能作为"调度器没跑"的证据** —— 那只是审计覆盖缺口。
> 正确判据：活跃上报看账号的 `lastActivityReport` 落点；签到看 `activity_history` 里 `trigger == "scheduler"`。

保留策略：`MAX_RECORDS = 5000`、`MAX_AGE_DAYS = 90`（`:59-60`），2 MB 或每 6 小时压缩一次（`:61-62`）。

---

## 八、任务引擎

### 8.1 任务记录

`fetch_growth_tasks()` `:144` 把上游 `/v2/activity/growth/tasks` 归一化为：
`task_code`、`name`、`description`、`jump_url`、`expert_id`（通用解析自跳转链接 `:166`）、
**`status` = `accept_status` 或 `"not_accepted"`** `:167`、`task_type`、`current`/`target`、`reward_credit`/`reward_energy`、`unforgeable`、`reason`。

状态词表：`not_accepted`（默认）、`accepted`、`already_accepted`（两者都算已接受 `:299`）、`completed`、`claimed`（奖励已领，提前返回 `:839`），以及 `task_type == "auto"`（上游不接受认领）。

### 8.2 不可自动化的任务

`unforgeable` = 完成需要**真人动作**，任何事件伪造都满足不了。`TASK_SPECS` `:69-72` 里两条：

- `wb_wechat_oa_subscribe_task`（奖励 100）—— 需在微信关注「腾讯 WorkBuddy」公众号满 24 小时
- `Expert_Philanthropy`（奖励 0）—— 真实捐款动作

`_progress_one` 跳过它们并记 `⏭`（`:888-896`）；`run_growth_tasks` 连尝试认领都拒绝（`:1039-1041`）。
未知 task code 也会跳过，但用**不同的**消息（`:876`）—— 是"跳过"，不是"失败"。

### 8.3 前置依赖

`first_buddy`（`task_type == "auto"`，奖励 300）是闸门：所有单项任务和猫猫旅行都依赖它，否则上游回 *"prerequisite not met: first_buddy"*。
⇒ `run_growth_tasks` **先跑 auto 任务**（`:1033-1036`）→ 再批量认领（`:1075-1079`）。

### 8.4 认领与领奖

- `accept_tasks()` `:247` —— 每批 20 个 code POST 到 `/v2/activity/growth/tasks/accept`，返回 `{ok, accepted[], failed[], reasons[]}`，失败**上报不吞**
- `run_growth_tasks` 重新拉取后对仍未接受的**再试一次**（`:1056-1065`），仍 `not_accepted` 则跳过（`:892-895`）
- `claim_task()` `:311` —— POST `/activity/growth/tasks/{code}/claim`；HTTP 400 时**降级到网页域名** `www.workbuddy.cn`（`:331-353`）

### 8.5 事件链（指纹）

`build_event_chain()` `:679` 三选一：

| 通道 | 实现 | 指纹来源 |
|---|---|---|
| `chat` | `report_events` `:447` | copilot 域名 `/v2/report` |
| `desktop` | `report_desktop_events` `:521` | 桌面客户端指纹 |
| `web` | `report_web_event` `:556` | 浏览器指纹 |

- 桌面链 `desktop_chat_sequence()` `:583`：`agent_task_created → chat_message_send → chat_request_send → chat_message_response(isSuccessful) → chat_message_status → chat_request_response`
- `DESKTOP_ONLY_TASKS` 现在**为空**（`:89-90`）—— 这些任务已被证明用桌面指纹事件链可以完成
- 专家/团队任务必须**轮换不同 id** —— 上游按 `(eventCode, id)` 去重，故循环使用 `EXPERT_ID_POOL`/`TEAM_ID_POOL`（`:904-916`）
- `black_cat` 只在 23:00–08:00 计数（`in_night_window` `:105`）

**进度→领奖的间隙**：`_progress_one` 在**截止时间前**一直查进度（`PROGRESS_POLL_TIMEOUT = 20.0`、间隔 `PROGRESS_POLL_INTERVAL = 2.0`，进度一到位就停）（`:971-979`）；
没落地就把领奖推迟到下一轮（`:980-983`）。**资格判定只看进度，从不看 `accept_status`**（`:869-873`）。

---

## 九、控制面

### 9.1 设置与限额

**存储**：单文件 `accounts/settings.json`。
`load()` `:404` 按 `(path, mtime, size)` 缓存；**任何解析错误都被吞掉并返回 `{}`** `:374-377`。
`save()` `:383` 写 tmp → `tighten_mode`(0600) → `replace_atomic`（Windows 上有界重试 40×25ms，`wb_atomic.py:39`，**失败会抛而不吞**）。
合并语义 `deep_merge()` `:401` 逐 key 合并，面板写入不会误删同级未知字段。

**限额维度**（`LIMIT_KEYS` `:860-862`）：

| 维度 | 键 | 作用 |
|---|---|---|
| 积分保留线 | `reserve_credits` | 低于此值不再派发 |
| 每日 Token 配额 | `daily_token_limit` | 按区域生效 |
| 每日积分消耗 | `daily_credit_limit` | 按区域生效 |
| 单模型每日 Token | `model_daily_token_limit` | 按区域生效 |
| 临期积分窗口 | `expiring_window_days` | 临期优先派发的天数窗口 |

**区域作用域**：`LIMIT_REALMS = ("intl", "cn")`，`LIMIT_SCOPES = ("global",) + LIMIT_REALMS` `:909-910`。
每条形如 `{"global": int, "intl": None|int, "cn": None|int}`；`None` = 继承全局，显式 `0` = 该区域关闭。
解析在 `limit_value()` `:1077`（区域覆盖优先于全局）。旧的扁平键在**首次读取**时由 `limits_data()` `:1048` 折叠迁移。

**Per-Key 设置**在 `api_keys` 列表，每条经 `_clean_key_entry()` `:704` 归一：
`id, name, key, realm, models, accounts, concurrency_limit, enabled, created_at, deleted_at`。
⇒ Per-Key 的能力是**模型白名单**（fnmatch）、**账号白名单**（精确 uid）、**并发上限**、**区域绑定**。
`set_api_keys()` `:833` 是软删除：secret 被抹掉但行保留，让用量日志的 key-id 归属仍可读。

**Per-账号权重**：`wb_accounts.py:1037` 夹在 1–100，`set_weight()` `:4736` 设置。

### 9.2 面板鉴权

- `DEFAULT_PANEL_PASSWORD = "admin"` `:23`，PBKDF2 轮数 **120,000** `:24`，会话 TTL **7 天** `:25`
- `_digest()` `:336` = PBKDF2-HMAC-SHA256 + 16 字节 hex salt
- `verify_panel_password()` `:426`：若 `panel_password_hash` 为空**或** `panel_password_default is True`，字面量 `"admin"` 直接通过；否则 `hmac.compare_digest` 比对
- 会话**仅在内存**（`PanelSessions` `:3259`），token = `secrets.token_urlsafe(24)`，**不持久化** ⇒ 重启即掉线
- token 只从请求头 `X-Panel-Token` 读（`wb_proxy.py:19762`），**刻意不从 query string 读**
- 登录限速：每 IP 5 次/60 秒（`:21818-21824`），失败额外 sleep 0.5s `:21830`
- **没有 admin/user 分级** —— 一个密码一个角色。`/panel/password` 需重新验证当前密码，改密后吊销其他会话（`:21846-21858`）

### 9.3 定价

**数据源优先级**：

1. 旁路文件 `WB_PRICING_FILE` 环境变量，否则 `<脚本目录>/pricing/pricing.json`（`_candidate_file` `:783`）—— 优先于内联副本
2. 在线 OpenRouter `https://openrouter.ai/api/v1/models`（`OPENROUTER_URL` `:991`），`fetch_openrouter()` `:1127`，45 秒超时
3. 内置静态目录 `hub_model_ids()` `:1140`

> models.dev **不用于定价**，只用于上下文/输出长度上限。

**名称 → 价格解析**三层链（`_resolve_once()` `:1303`）：面板运行期覆盖 → 源码表 `OVERRIDES` → `auto_match()` `:1297`。
`auto_match` 要求归一化名称**完全相等且唯一** —— 有歧义就留空不定价，**不猜**。

**估算** `compute_row()` `:918`：单位是每百万 token。
`uncached = prompt - cached`；`cny = (uncached*miss + cached*hit + output*out_rate) / unit * factor`，
`factor` 取 `meta.usd_cny`（默认 `USD_CNY = 7.10` `:992`）。

> **推理 token 不单独计费** —— 它们在上游已计入 `completion_tokens`，再计一次就是重复收费（`:941-945`）。

**加价**：`pricing-markup.json` 是 `{model: factor}` 映射，`markup_for()` `:1563` 缺失时返回 `1.0`（`:1556`、`:1569`）⇒ **打错模型名是静默无效，不是报错**。

**刷新**：`PriceRefresher` 线程（`:2369`），默认 **5 分钟**一次（`DEFAULT_REFRESH_MINUTES = 5`）。
取价失败时保留上一份好数据（`:2501-2504`）。

### 9.4 档位（tier）

`wb_tiers.py` 是**唯一**的档位枚举，词表恰好两个值：`"free"` 与 `"standard"`。
`FREE_TYPES = frozenset(("free", "personal_free_user", "personal-free-user"))` `:19`。

**只有一个生产者**（`wb_tiers.ingest` `:65`，读 `/v3/config` 的 `modelTiers[]`）和**一个消费者**（`requires_pro` `:116`）。
**不存在转换辅助函数，也不需要** —— 没有第二套词表。

> ⚠️ 但真正的静默失效向量不是"词表漂移"，而是 **fail-open**：
> `requires_pro()` 对未知模型、缺失区域一律返回 `False`（`:126-129`）。
> ⇒ 一个从未收到 `modelTiers` 的安装，会让免费账号去尝试 PRO 模型，**只有上游回 11158 才知道**。
> `ingested_at()` `:132` 是唯一的陈旧度线索。

另外：payload 里没有 `modelTiers` 时，`ingest()` 返回 False 并**保留旧映射**（`:79-80`）—— 目录刷新坏了是看不出来的。

### 9.5 出口与代理槽

**代理槽**是 `settings.json` 里的 `proxy_slots` 列表（`wb_settings.py:3136`），每条经 `_clean_slot_entry()` `:3083` 归一。
槽位 id 形如 `slot-<n>`，来自单调计数器 `proxy_slot_seq`（`:3128`）—— **id 永不回收**，
删掉的槽位 id 之后不可能被别的账号占用。`set_proxy_slots()` `:3153` 会同时从传入和现存的列表里播种计数器，
所以同一次保存里删掉的 id 不会被重新发出去。

绑定在 `account.proxy_slot`（持久化）；`account.proxy` 是**派生运行时值**，由 `apply_proxy_slots()`（`wb_accounts.py:4786`）重算。

> ⚠️ `wb_egress.py` **不是**代理槽模型 —— 它是**退避计数器**。
> `EGRESS_BACKOFF_BASE = 1.0s`，`MAX = 15.0s`，抖动 0.75–1.25（`:22-28`）。
> 理由写在 `:4-15`：账号级熔断是分钟级的，一个链路抖动就把 13 个账号一起停掉太浪费 ⇒ 出口级退避做成秒级。

**IP 情报**（`wb_ipintel.py`）：查 `http://ip-api.com/json/<ip>`，**刻意直连**不走被描述的那个代理（`:77-83`）。
`classify()` `:48`：hosting → 机房，否 → 住宅，查不到 → `empty()` `:43`，**从不猜**。

### 9.6 告警通道

**两条通道**（`wb_alerts.py:80`）：HMAC-SHA256 签名的 **webhook**（头 `X-WB2A-Signature` `:48`）+ **ServerChan**。
secret 为空时**不签名**（`:171-172`）。

**SSRF 防护** `_assert_webhook_url()` `:216`（投递时校验，`:341`）：

| 闸门 | 挡什么 | 锚点 |
|---|---|---|
| scheme 只允许 http/https | `file://` 读本地文件 | `:210` |
| 长度 ≤ 2048 | 超长 | `:213` |
| 禁 userinfo | 凭据泄露 + 绕过主机判定 | `:238-242` |
| 拒回环/私有段/链路本地/CGNAT/保留段 | `127.0.0.1`、`169.254.169.254`、`10.x` | `:261-258` |
| **域名解析后判**（多值 DNS 全查） | `evil.example` → 127.0.0.1 | `:290-303` |
| **不跟随跳转** | 公网 URL 302 到内网 | `:197` |
| **不回显响应体**（只读 1 字节） | 完整读取链 | `:175` |

**残余风险（诚实声明）**：DNS 重绑定仍可能绕过"解析时检查"；**无法解析的主机名是放行的**（`:292-295`）。
彻底消除需要改主机白名单或"解析后按 IP 直连并校验 Host/SNI"。当前实现已消除本轮审计发现的**全部可利用路径**。

**触发线**：

| 事件 | 触发点 | 去抖 |
|---|---|---|
| `pool_degraded` | 调度器巡检循环 | 2/2 |
| `restriction_wave` | `note_account_restriction()` | 不去抖 |
| `unverified_rearmed` | 面板 `/accounts/set` 启用动作 | 不去抖 |

阈值 `min_ready` / `for_ticks` / `clear_ticks`，区间 `(8,1,1000)` / `(2,1,100)` / `(2,1,100)`（`wb_settings.py:163-167`），**有上界**。
上界不是装饰：`for_ticks` 设成极大值 → 去抖永远攒不满 → **告警永远不发**，那等于白配。

判据用**可派发**账号数而非启用数 —— 未验证护栏会让 `enabled=True` 的账号不参与派发，只看启用数会**高估容量**。

**secret 只写不读**：`wb_settings.alerts()` `:188` 只返回布尔值，secret 与 key 永不回显。

### 9.7 更新引擎

**发现**：`UpdateChecker`（`wb_updates.py:214`）守护线程，GET GitHub Releases（`:46`），10 秒超时、2 MB body 上限。
`pick_latest()` `:108` 跳过草稿/预发布/非 `vX.Y.Z` 标签，取**最高版本**而非列表第一项。
`safe_url()` `:96` 只允许 `https://github.com/…`（≤300 字符），因为它会被原样渲染到面板。

> **更新发现默认关闭**（`wb_settings.py:110-113`）—— 与多数开关相反，且是刻意的：网关不该自己给上游打电话。

**校验**（`wb_update_portable.py`）：下载 zip + `SHA256SUMS` → `parse_checksums()` `:128` → `sha256_file()` `:137` 比对，
不符即 `UpdateError("SHA-256 校验失败")` `:787-788`。
`safe_extract()` `:153` 拒绝绝对路径、`..` 穿越、反斜杠和**符号链接** `:168`，保留 unix 权限位（a644 的启动脚本会 EACCES 然后回滚 `:174-180`）。
`validate_stage()` `:214` 校验 manifest 版本、root 一致性、文件齐全，并**正则抓出更新包 `wb_proxy.py` 里的 `server_version`** 确认二进制版本匹配 `wb_update_portable.py:318`。

> **没有代码签名** —— 校验只有"zip 的 SHA-256 + manifest 自洽"，两者都从同一个 release 下载。防的是损坏，不是 release 被攻破。

**应用**：状态机 `downloading → verifying → backing_up → stopping → applying → restarting → checking → succeeded`
（`run_update()` `:796`），状态原子写到 `updates/status.json`。
网关进程不能替换自己，所以 `request_install()` `:699` 拉起分离的辅助进程 `wb_update_apply.py`。

**回滚**：
- **自动** —— applied/stopped 之后任何异常 ⇒ `rolling_back` → `restore_backup()` `:298` → 重启 → 校验健康（`:824-841`）
- **手动** —— `request_rollback()` `:722` 要求该版本有**完整**备份，否则拒绝
- 备份目录 `updates/backup-<version>`，保留数量 `keep_backups`（默认 1，最大 3）
- **失败回滚还能再回滚** —— `.rollback-safety` 快照（`SAFETY_DIR` `:35`）

**便携路径 vs 容器/rootfs 路径**：

| | 便携（Windows 绿色包） | 容器 / systemd / 外部执行器 |
|---|---|---|
| 前置 | 有 `release-manifest.json` + `wb_update_apply.py`（Windows 还需 `python/python.exe`） | `WB_UPDATE_INSTALL_ENABLED=1` + systemd 单元，或 `WB_UPDATE_EXECUTOR=external` |
| 执行 | 网关直接拉起分离辅助进程 | 网关**只写一个请求文件**到 root 拥有的 sticky 目录（`O_CREAT|O_EXCL|O_NOFOLLOW`, 0600），由 root 侧执行器处理 |
| 权限模型 | 同用户 | **网关保持无特权**，升级由 root 执行器完成 |

---

## 十、部署与运维

### 10.1 容器

| | `Dockerfile` | `Dockerfile.local` |
|---|---|---|
| 基础镜像 | `python:3.11-alpine` `:12` | `ghcr.io/ardeyouxipianyi/workbuddy2api-hub@sha256:1d7d85fa…` `:15` |
| 运行时 | 装 tzdata/ca-certificates，设 TZ `:20` | 继承 |
| 应用代码 | `COPY wb_*.py dashboard.html ./` `:23` | **同样 COPY 全部** `:25` |
| 额外 | ENV / VOLUME / EXPOSE / HEALTHCHECK `:15-37` | 无（全继承） |
| 何时用 | Docker Hub 可达 | Docker Hub 不可达（只有 GHCR） |

> **两个 Dockerfile 都 COPY 全部模块**，不是只 COPY 改过的。
> 原因：基础镜像里的 1.7.33 构建**早于**本项目基线 commit `d1a8913`，两者已有文件差异（如 `wb_identity.py`）。
> 只 COPY 补丁文件会让镜像变成"新版补丁 + 旧版其余模块"的混合体 —— **那种组合跑起来行为无法预测**。
> 全量 COPY 让镜像内容 == 发布树内容，可以逐文件 sha 校验。

**compose**（`docker-compose.yml`）：服务 `wb-proxy`，容器名 `wb-proxy`，镜像 `workbuddy2api-hub:0.1.0` `:14`，
端口 `8788:8788` `:22`，卷 `./accounts:/app/accounts` + `./usage:/app/usage` `:29-32`，
命令 `python wb_proxy.py --host 0.0.0.0 --lan --panel-password ${PANEL_PASSWORD}` `:34`。

### 10.2 启动脚本

| 脚本 | 用途 | 平台 |
|---|---|---|
| `start-wb-proxy.bat` | 本机模式，默认端口 **8788** | Windows。**默认静默** —— 优先 `pythonw.exe`，另开 `console_tail.py` 窗口跟日志；先用 `--probe-only` 探测，"已在运行"才会打印出来而不是静默消失 |
| `start-wb-proxy.sh` | POSIX 对应 | macOS/Linux。**仅系统 Python 3.9+** —— ZIP 不含 POSIX 运行时 |
| `start-wb-proxy-lan.bat` / `.sh` | 局域网模式 `--lan`，**强制要求 API Key** | 端口 8788 |
| `restart-wb-proxy.bat` / `.sh` | 杀掉所有 `wb_proxy.py` 进程后重启 | 两者 |
| `allow-firewall.bat` | netsh 加 8788 入站规则（自提权） | Windows |
| `quick-deploy.sh` | 一行 Docker 安装/更新；`WB_DEPLOY_MODE=local\|lan\|public`，默认 **lan** | Linux/macOS/NAS |

> 默认是 lan 而非 public：公开安装时不能悄悄把 8788 暴露到公网（`tests/_test_deploy_modes.py` 钉住这条）。

### 10.3 生产部署流程（243 现行）

```
改 release/ 发布树
  → python tests/run_all.py          （241 套件全绿）
  → 打包上传
  → bash /tmp/deploy_v0xx.sh         （镜像在服务器本地构建 + 逐文件 sha 校验，不符即中止）
  → 只改 compose 的 image 行
  → docker compose up -d --no-deps --no-build --pull never
```

**镜像在服务器本地构建**，不在开发机构建 —— 构建负载压工作站曾导致整机重启、构建被杀。

### 10.4 OpenWrt 打包

`wrt/openwrt/workbuddy2api/Makefile`：`PKG_NAME:=workbuddy2api`，`PKG_VERSION:=0.1.0` `:23`，`PKG_RELEASE:=1`，
`DEPENDS:=+python3-light +python3-urllib +python3-uuid +python3-openssl +python3-sqlite3 +python3-codecs` `:45`，
`PKGARCH:=all`（纯 Python，架构无关）。装到 `/usr/lib/workbuddy2api`，附 init.d 脚本与 config。
`postinst`（`:102-125`）建目录（0700）、配 cron（`*/2` 缓存预热）、并**在首次安装时显式停用服务**。
**无自更新逻辑** —— 升级走 opkg/apk。

`build-ipk.sh` 是纯 POSIX sh + python3 标准库，输出 `gzip(tar(...))`（不是 `ar`），字节可复现。
`build-apk.sh` 需要 OpenWrt 25.12.x SDK（glibc x86-64 Linux 宿主机）。

### 10.5 阿里云更新器

`ops/aliyun/`：root 权限安装辅助程序到 `/usr/local/libexec/`，装 `.service`/`.path` 单元，
用 tmpfiles 建 `/run/workbuddy2api-hub-updater`（`root:wbproxy 1730`，**sticky**），
写 `/etc/workbuddy2api-hub-updater.env`（0600）。**任何 bootstrap 步骤失败就回滚全部文件**。

权限模型的核心：**无特权网关只能往那个目录写一个小请求文件，不能以 root 执行命令**。
root 执行器验请求 → 取 release JSON（拒草稿/预发布）→ 下载 + **验 SHA-256** → 验 manifest
（schema/版本/tag/root/protected ⊇ {accounts/, usage/}/无符号链接/无穿越/无重复路径/≤1200 文件/128 MB 压缩上限）
→ **跳过 `python/` 子树**（Windows 专用运行时）→ 备份 → stop → 原子逐文件拷贝 → start → 轮询 `/health` 120 秒
→ 失败则**自动恢复备份并重启** → 发布 status.json 供面板回滚列表使用。

---

## 十一、测试与发布工程

### 11.1 测试运行器

`tests/run_all.py`：扁平的目录扫描，匹配 `_test_*.py` / `_test_*.js`（`:89-92`）。**"套件" == 一个文件**，没有注册表。

- Python 腿跑在当前解释器下（`:99`）；JS 腿用 `node <file>`（`:97-98`），**缺 node 时报告为 skipped 并打印提示**（`:247, :267-272`）
- 每个套件跑在**独立进程组**（`:123-127`），超时会把整棵树杀掉（`kill_tree` `:130-157`）—— 否则它拉起的网关会占着端口影响下一个套件
- 单套件墙钟 300 秒（`:55`）；`--jobs` 默认 `min(4, cpus)`（`:222`）
- **"模式匹配不到任何东西" → 退出码 2**，绝不当作通过（`:250-255`）
- stdout/stderr 强制 UTF-8（`:44-49`）—— Windows CI runner 是 cp1252，中文摘要会让整轮挂在 `UnicodeEncodeError` 上

**数据隔离**（`tests/_isolated_dirs.py:21-36`）：`isolated_data_dirs(prefix)` 建临时目录含 `accounts/` + `usage/`，
并且是**赋值**（不是 `setdefault`）`ACCOUNTS_DIR` / `WB_PROXY_USAGE_DIR` ⇒ 套件永远写不进真实数据目录，两个套件也不会共享目录。

**实测**：241 套件（173 Python + 59 JS），`--jobs 4` 全量 **114.1 秒**。

### 11.2 高信号回归门

| 测试文件 | 钉住的不变量 |
|---|---|
| `_test_stream_tool_call_args.py` | 参数分片不被删；真占位仍删；**续帧不得带 `name` 键**；两种客户端累积规则得到同一个函数名；并行调用按 index 独立 |
| `_test_schedule_hours.py` | 默认槽位小时不被悄悄改动（#456） |
| `_test_upstream_pool.py` | keep-alive 池：复用命中、容量/空闲淘汰、探针+一次安全重试、`Connection: close`、半包丢弃、分块读满、读超时重置、按代理与按凭据分池、3xx 回落、TLS、`WB_UPSTREAM_KEEPALIVE=0` 对等 |
| `_test_pricing*.py` | 缓存命中/未命中拆分、USD 换算、条件时段（长度阈值/UTC 窗口，最紧的赢）、未知模型 → `known=False`（**从不猜**） |
| `_test_release_assets.py` | 整个发布契约（见下） |
| `_test_release_engineering.py` | 源版本串一致、workflow 仍断言 tag-vs-source、Dockerfile HEALTHCHECK 探 `/health`、compose 支持 `${PUID:-}`、**README 套件数 == 树里实际数量**、**全树无冲突标记** |
| `_test_run_all_contracts.py` | 运行器自身的承诺：非法参数是用法错误、空模式 ≠ 通过、失败行给出真实错误（不是裸的 "Node.js v20.20.2"）、`--logs` 保留完整输出、挂起的探针被限时并连树收掉、`--jobs 1` 与 `--jobs 4` 的通过集合一致 |

### 11.3 发布契约（最容易踩的坑）

1. **新增 `wb_*.py` → 必须加进 `release/portable.txt`**（`_test_release_assets.py:239-246`），否则便携包会丢模块，用户机器上 `ModuleNotFoundError`。
   启动脚本同理（`:255-268`）—— #285 的教训：清单里被同名遮蔽，ZIP 悄悄丢了 Windows LAN 启动器。
2. **新增 stdlib import → 必须进 `portable_runtime.REQUIRED_FILES`**（`:324-345`），否则裁剪后的运行时在**用户机器上**炸，而不是在 CI 里炸。
3. **便携运行时是 Windows 专用**。POSIX 退回系统 Python 3.9+（`start-wb-proxy.sh:112-116`）；systemd 更新器刻意跳过 ZIP 里的 `python/`。
4. **CI 必须留下干净的 checkout**（`scripts/check_clean_checkout.py` 是 CI 步骤）。有意的产物加进 `.gitignore` 并写注释 —— **绝不删文件来让检查通过**。
5. **行尾在打包时归一，不在 git 里**：`.sh`/`.command` → LF + 0755，`.bat` → CRLF。
   CRLF 的 `.sh` 在 macOS/Linux 上会 "bad interpreter" 直接死。
6. **发布写入者只有一个**：`write-draft` 的 `if:` 必须保持 tag-push 守卫，`workflow_dispatch` 必须只有一个输入（`tag`）。
   `check_release_writers`（`:214-230`）断言这两点，且带自检证明检查器有牙齿。
   **release 永远是草稿** —— 由人工发布。`-ci` 演练 tag 变成 prerelease 草稿，更新器会忽略 prerelease。

### 11.4 版本一致性清单

**CI 强制校验 4 处**：

| 位置 | 内容 |
|---|---|
| `wb_proxy.py`（概览 JSON） | `"version": "0.1.0"`（概览 JSON） |
| `wb_proxy.py`（HTTP 头） | `server_version = "wb-proxy/0.1.0"`（HTTP 头） |
| `wrt/openwrt/workbuddy2api/Makefile:23` | `PKG_VERSION:=0.1.0` |
| git tag | `v0.1.0` |

**无人校验、必须手工同步的 3 处**（grep 确认）：

- `VERSION` 文件（打包进 `portable.txt:87`，但**没有任何 Python 模块读它**）
- `docker-compose.yml:14` 镜像 tag
- `Dockerfile:1,4` 与 `Dockerfile.local:1,7` 的头部注释

> ⚠️ **升版本 = 3 处手工同步 + 1 个 tag**。忘了同步 compose 的 tag，容器会继续跑旧版本 —— 而 CI 全绿。

### 11.5 CI workflow

| 文件 | 触发 | 作业 |
|---|---|---|
| `tests.yml` | push `[main, master]` + tag `v*`；`pull_request` | `test`：矩阵 **ubuntu-24.04/3.9**、**ubuntu-latest/3.12**、**windows-latest/3.12**；装 node 20；跑全部套件 → `check_clean_checkout.py`；失败时上传 `suite-logs`。<br>`version-assert`（仅 tag）：tag == 源版本，`-ci` 跳过 tag 比对 |
| `release.yml` | tag `v*`；`workflow_dispatch(tag)` 仅打包 | `version → gate → portable + openwrt → draft-preview → write-draft`。<br>`gate` 按**作业名**等待目标 SHA 的**完整三腿矩阵**（`:206-218`）。<br>`portable` 下载钉死的运行时、验 SHA-256、裁剪、打 ZIP，然后**真的把它跑起来**：校验 manifest、启动网关并要求 45 秒内 `/health` 200（`:274-341`）。<br>`openwrt` 无 SDK 构建 ipk，下 SDK 构建 apk，用 SDK 自带的 `apk adbdump` 双向验证 |
| `docker-publish.yml` | `release: published` | buildx 多架构 **linux/amd64 + linux/arm64** → GHCR（+ 有密钥时 Docker Hub） |
| `release-checksums.yml` | `release: published` | 下载资产 → `sha256sum *` → 上传 |
| `revert-guard.yml` | `pull_request` | 与 base 比较，每文件缺失 ≥5 行未解释则红；`revert-confirmed` 标签转绿 |

> **门禁从 `tests.yml` 里读腿**（`release/matrix_legs.py`）—— 改那个矩阵会**静默地**放宽或收紧发布门禁。矩阵少一条腿就会失败。

---

## 十二、看板

`dashboard.html` ≈ 1.0 MB / 18,154 行。**原生 JS + 内联 CSS**，零外部 `<script src>` / `<link href>`，无任何框架特征 —— 单文件自包含。

**7 个主页签**（`MAIN_TABS` `dashboard.html:10767`）：网关、账号、任务、数据看板、智能体配置、设置、运行日志。

**鉴权**：API Key 从 `?key=` 取 → 存 `localStorage` → 从 URL 剥离但保留其他参数（`:7940-7955`）。
每个请求同时带 `Authorization: Bearer <key>` 和 `X-Panel-Token`（`:7965-7971`）；非 ASCII 的 key 被丢弃（`:7924-7926`）。
面板密码：`POST /panel/login {password}` → `{token}` → `sessionStorage`（`:10875-10890`）；启动时 `GET /panel/status`，支持 `?pwd=` 自动登录（`:10878-10897`）。

**通信模型是轮询**，页面隐藏时全部暂停、解锁后才启动（`:10962-10978`）：

| 频率 | 内容 |
|---|---|
| 5 秒 | 全局 `refresh()` + 表格重排 |
| 5 秒 | 数据看板页的统计与矩阵 |
| 30 秒 | 调度器状态 |
| 15 秒 | 账号列表 |

`getJSON` 按 URL 做**单飞去重** + 60 秒陈旧条目保护（`:7973-7982`），并用 ETag 重验证（`:8070`）。

---

## 十三、维护者速查：容易误判的地方

| # | 陷阱 | 正确判据 |
|---|---|---|
| 1 | **"activity_history 没有 scheduler 记录" ≠ 调度器没跑** | 活跃上报**根本不写** history。活跃看账号 `lastActivityReport` 落点，签到看 history 的 `trigger=='scheduler'` |
| 2 | 分母用错 | `task_accounts("cn")` 只数 `tasks_enabled`，**包含** API Key 账号、企业账号、未验证账号。要"真正能签到的账号"还得加 `can_checkin()` 的区域与企业判定 |
| 3 | `enabled` 与 `tasks_enabled` 独立 | `set_all_enabled()` `:4993-4994` **两个都写** ⇒ 批量启停会静默改写任务参与 |
| 4 | 幂等长得像故障 | `can_checkin()` 今日已签则返回 False `:2097-2100` ⇒ 签到 0 个是**正常**，不是坏了 |
| 5 | 面板"启用" ≠ 会被派发 | 未验证护栏只在 `ready()` 拦（`:1806`），不改 `enabled`。面板显示绿但从不被选中 —— `count_ready()` `:5011` 才是诚实的容量数 |
| 6 | 限额看着不生效 | `daily_limit_blocked()` 在 `daily_tokens_today is None` 时返回 False `:1562-1564` —— 首次用量折叠之前，护栏是惰性的 |
| 7 | 档位 fail-open | `requires_pro()` 对未知模型返回 False ⇒ 免费账号会真去试 PRO 模型，靠上游 11158 才发现 |
| 8 | **`settings.json` 解析失败被吞** | **v0.1.0 已修**：现在打 `_UNREADABLE` 标记，面板登录与鉴权判定见到标记就**拒绝**（fail closed）；`save()` 拒绝用空内容覆盖坏文件 |
| 9 | 加价文件打错模型名 | `markup_for()` 静默返回 1.0 `:1556` ⇒ **无效但不是错误** |
| 10 | 空 secret 的 webhook 不签名 | `:171-172`；DNS 重绑定与"公网 IP 解析到内网"明确不在防护范围 |
| 11 | 告警队列满静默丢弃 | 队列 200，满则丢并计数 `:426-430`，只有计数器能看出来 |
| 12 | 更新校验只防损坏 | SHA256SUMS 与 zip 来自**同一个 release** ⇒ 防损坏，不防 release 被攻破。**没有代码签名** |
| 13 | `keep_backups = 0` 等于关掉回滚 | 默认 1，最大 3 |
| 14 | 代理槽 id 永不回收 | `wb_settings.py:3156-3158` —— 期待复用 id 的测试会失败 |
| 15 | **半开探测是死代码**（v0.1.0 修过一次） | 冷却到期时刻必须在 `ready()` 过滤**之前**记录 —— 冷却中的账号根本过不了 `ready()`。判据：`_recovery_deadlines` 是不是恒为空 |
| 16 | **在途感知会被手动权重压过** | 权重是「同等条件下先给谁」的偏好，不是「独占」。带权重的账号比池里最闲的还忙时，让路给闲的 |
| 17 | **一次派发触发 N 次令牌刷新** | `ready(model, refresh=False)` 只看不刷；刷新挪到**选中之后**（`token_needs_refresh()` + `open_upstream`） |
| 18 | **负载计数的归还回调读了一个会被置空的变量** | 归还挂在响应 `close()` 上，但绑定后本地标记立刻置空 ⇒ 回调读到 None ⇒ **计数只增不减**。闭包必须抓住账号本身 |
| 15 | 更新发现**默认关闭** | `wb_settings.py:110-113` —— 与多数开关相反，且是刻意的（网关不该自己给上游打电话） |
| 16 | `wb_egress.py` 不是代理槽 | 它是**退避计数器**；槽在 `wb_settings.proxy_slots` |
| 17 | 版本号有 3 处没人管 | `VERSION` 文件、compose 的 image tag、两个 Dockerfile 的注释 —— 手工同步 |

---

## 十四、本项目相对上游的增量

上游 v1.8.0 之后的自有演进（完整记录见 `docs/CHANGELOG-v0.1.*.md`）：

| 版本 | 核心内容 |
|---|---|
| v0.1.1 | 修流式工具调用**参数被网关吃掉** —— 占位过滤误删承载参数的帧，症状是 agent 报"缺少必选参数" |
| v0.1.2 | 修 v0.1.1 的**函数名回填** —— 名字整段流只能出现一次，否则 WorkBuddy CLI 拼成 `TaskCreateTaskCreate…` → Tool Not Found |
| v0.1.3 | 并入上游 #644（stderr 日志通道异步化）+ #647（国际版活跃上报降级）。合并时**保留双方意图**（异步队列 + 凭据打码并存） |
| v0.1.4 | **未验证账号护栏** —— 三态预验证入池闸门，判据是 `credits.used == 0`（分离度 4/4 vs 4/9） |
| v0.1.5 | **账号池告警通道** —— HMAC 签名 webhook + ServerChan，让网关自己开口 |
| v0.1.6 | **区域暂停持久化**（跨重启存活）+ 两个真 bug（空池三协议 503 不一致、`11140` 正则缺右边界导致误伤健康账号） |
| v0.1.7 | **告警通道 SSRF 加固** —— 五道闸门 + 阈值上下界 + 凭据打码 |
| **v0.1.0**（正式版） | **同类项目借鉴落地 5 项**（在途感知选号 / 半开探测 / 错误分类 / 恢复时刻夹取 / 金丝雀固件）<br>+ **四份独立代码审计**修掉 19 个缺陷（含 1 CRITICAL、8 HIGH）<br>+ **安全加固 9 项**（配置损坏 fail-closed、凭据不落盘、XFF 限速修复、口令出 URL、更新包体积限制…）<br>+ 全量套件 **232 → 241**，新增 1 个固件目录 |

**方法论沉淀**：v0.1.7 的 SSRF 是**两遍独立检查各自发现**的（一遍读代码找危险调用，一遍从"外部输入能到达哪里"倒推）。
⇒ **新增"让服务端按用户输入发起请求"这类能力时，做两遍独立检查。单遍会漏。**

---

## 附录 A：模块职责速查（32 个 `wb_*.py`）

| 模块 | 行数 | 职责 |
|---|---|---|
| `wb_proxy.py` | 24138 | **主入口**。HTTP 路由、协议适配、流式管线、选号编排、计费记录 |
| `wb_settings.py` | 3197 | 设置持久化、限额、面板鉴权、API Key、代理槽、排程配置 |
| `wb_pricing.py` | 2619 | OpenRouter 价抓取、CNY 换算、加价、成本策略与时间线 |
| `wb_accounts.py` | 5550 | **账号池**。记录模型、选号算法、未验证护栏、凭据管理 |
| `wb_agents.py` | 1804 | 一键配置本地 AI 客户端（Claude Code / Codex / OpenCode / DSH / Crush / ZCode），带字节级备份 |
| `wb_webtools.py` | 1099 | 网关侧 `web_search`/`web_fetch` 模拟（上游没有），含 SSRF 防护 |
| `wb_update_portable.py` | 952 | 便携运行时更新：下载、校验、应用、回滚 |
| `wb_upstream_pool.py` | 852 | 上游 keep-alive 连接池（**不是账号池**）—— 按 `(scheme,host,port,proxy,凭据摘要)` 分池，保证一账号一连接 |
| `wb_atrest.py` | 915 | 桌面客户端凭据的 AES-256-GCM 静态加密封装（`{"$wbEncrypted":…}`） |
| `wb_tasks.py` | 1219 | 成长任务引擎：拉取、认领、事件链推进、领奖 |
| `wb_identity.py` | 641 | 上游身份模拟层：端点解析、请求头构造、gzip、conversation id |
| `wb_logstore.py` | 415 | 内存日志环的 SQLite 镜像 |
| `wb_window_probe.py` | 407 | 速率限制窗口探测（`model_windows.json`） |
| `wb_quota_scan.py` | 421 | 主动配额扫描（每 6 小时） |
| `wb_activity.py` | 386 | 结构化每日活动历史（**不记录任何凭据**） |
| `wb_update_request.py` | 357 | 向 root 更新器投递请求文件（无特权路径） |
| `wb_updates.py` | 421 | GitHub Releases 更新发现 |
| `wb_webprompts.py` | 316 | 300 条内置中文网页活跃提示词 |
| `wb_webagent.py` | 282 | ACP over streamable-HTTP 的网页 agent 客户端 |
| `wb_alerts.py` | 471 | 告警通道：签名、SSRF 防护、阈值去抖、有界队列 |
| `wb_modelsdev.py` | 245 | models.dev 上下文/输出长度（**不用于定价**） |
| `wb_catalog.py` | 1731 | 内置模型目录 JSON 快照（**无函数**，import 时解析）+ 实时叠加 |
| `wb_scheduler.py` | 855 | 后台调度器：槽位、循环、窗口探针、配额扫描、池健康 |
| `wb_tiers.py` | 150 | **唯一**档位枚举：`free` / `standard` |
| `wb_fingerprint.py` | 80 | 稳定设备身份：`derive_id`（盐化 md5 → UUIDv4 形状） |
| `wb_prompt.py` | 136 | 系统提示词模式（透传/自定义/追加）+ 内容门降级 |
| `wb_probes.py` | 120 | 输出 token 上限探测（`max_tokens` 截断值） |
| `wb_ipintel.py` | 107 | IP 情报：住宅/机房分类 |
| `wb_update_external.py` | 136 | 外部执行器参考实现 |
| `wb_update_apply.py` | 46 | 分离的更新辅助进程入口 |
| `wb_egress.py` | 86 | 出口级退避计数器（秒级） |
| `wb_atomic.py` | 58 | 原子写 + 权限收紧原语 |

## 附录 B：文档索引

### v0.1.0 正式版

| 文档 | 内容 |
|---|---|
| **`docs/ARCHITECTURE-v0.1.0.md`** | **本文档** —— 技术架构 |
| `docs/CHANGELOG-v0.1.0-formal.md` | 功能优化与新增日志（本版） |
| `docs/BUGFIXES-v0.1.0-formal.md` | BUG 修复清单（本版，含未修项与原因） |
| `docs/SECURITY-v0.1.0-formal.md` | 安全审查报告（本版） |
| `docs/DEPLOY-v0.1.0-formal.md` | 部署与升级指南（本版增补） |
| `docs/USAGE-v0.1.0-formal.md` | 使用说明（本版增补） |
| `research/可借鉴要点清单-v0.1.0.md` | 同类项目借鉴过程与 18 条借鉴点 |

### 长期维护的主文档

| 文档 | 内容 |
|---|---|
| `docs/CHANGELOG.md` | 全量变更史 |
| `docs/CHANGELOG-v0.1.0.md` … `-v0.1.7.md` | 历史版本线的逐版记录 |
| `docs/BUGFIXES-v0.1.0.md` | 历史缺陷修复记录 |
| `docs/SECURITY-v0.1.0.md` | 历史安全审计记录 |
| `docs/DEPLOY-v0.1.0.md` | 完整部署教程 |
| `docs/USAGE-v0.1.0.md` | 完整使用说明 |
| `docs/MAINTENANCE-v0.1.6.md` / `-v0.1.7.md` | 生产维护手册 |
| `docs/README-v0.1.0.md` | README 归档 |
| `docs/CONTRIBUTING-智能体配置.md` | 智能体配置说明 |
