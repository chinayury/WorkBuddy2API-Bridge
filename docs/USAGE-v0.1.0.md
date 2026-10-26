# WorkBuddy2API-Hub v0.1.0 · 功能使用说明

> 面板与接口的实际行为说明。所有路径取自 v0.1.0 的前端实际调用（69 个）与后端路由。

---

## 一、面板总览

面板是**单文件**前端（`dashboard.html`，纯 vanilla HTML/CSS/JS，零外部 CDN），
由网关自己托管。浏览器打开 `http://<host>:8788/` 即可。

顶部主导航共 6 个分区：

| 分区 | 用途 |
|---|---|
| **网关** | 出口（国内版 / 国际版）、模型清单与勾选、API Key 管理、代理出口与端口发现、调用量概览 |
| **账号** | 账号池的导入 / 登录 / 测试 / 权重 / 禁用模型 / 代理槽位 / 积分与套餐明细 |
| **任务与福利** | 成长任务中心自动化、猫猫旅行、连登管窥与抽奖、每日活跃打卡 |
| **数据看板** | 用量统计、费用估算、时间序列、最近请求、性能分位 |
| **设置** | 各类限额、模型别名、定价映射、目录来源、主题、语言、SQLite 日志 |
| **运行日志** | 网关日志（服务端落库）+ 浏览器控制台 |

---

## 二、面板会话

1. 打开面板 → 输入**面板密码**（默认 `admin`，**必须改**；见部署教程 §3.2）
2. `POST /panel/login {"password": "..."}` → 返回 `{"ok":true,"token":"..."}`
3. 之后所有面板请求带 `X-Panel-Token: <token>`

**会话 token 只在内存**：容器重启后所有标签页同时 401，需要重新登录。这是刻意的
（持久化的面板会话是长期凭据泄露面）。

改密码：`POST /panel/password {"current":"...","new":"..."}`

---

## 三、账号管理

### 3.1 导入账号

| 方式 | 说明 |
|---|---|
| **多选文件导入**（v0.1.0 新增） | 文件选择器支持多选；各文件的账号行**合并成一次**原子提交（服务端 `/accounts/import` 接受裸数组），提交前可预览 |
| 面板「导入账号」 | 单文件，兼容旧行为 |
| 面板「本机桌面客户端账号」 | 只读检测本机已登录客户端的凭据目录，确认后再显式导入 |
| 命令行 | `python wb_proxy.py --import-desktop` / `--info <file>` |

导入的文件可以是：本网关导出的格式、账号数组、单个账号对象、或桌面客户端的
`{"account":…,"auth":…}` 格式。

### 3.2 扫码 / OAuth 登录

| 路径 | 作用 |
|---|---|
| `POST /accounts/login/start` | 生成官方登录链接 |
| `POST /accounts/login/poll` | 轮询登录结果 |
| `POST /accounts/login/cancel` | 取消 |

### 3.3 单账号操作

| 路径 | 作用 |
|---|---|
| `POST /accounts/test` | 对单个账号发一条最小请求做实测（**真实调用上游**，会消耗极少量额度） |
| `POST /accounts/refresh` | 刷新凭据 |
| `POST /accounts/checkin` | 立即签到 |
| `POST /accounts/daily-chat` / `daily-chat-web` | 立即做一次每日活跃对话 |
| `POST /accounts/set` | 改单个账号（权重、备注、禁用模型、代理槽位…） |
| `POST /accounts/set-all` | 批量改 |
| `POST /accounts/delete` | 删除账号（v0.1.0 起不存在的 uid 返回 404） |
| `POST /accounts/product` | 切换出站身份（桌面 / VSCode / CLI） |
| `POST /accounts/sync-profile` | 同步账号资料 |
| `GET /accounts/credits` / `credits/detail` / `credits/grants` | 积分余额、套餐明细、权益包 |
| `GET /accounts/export?uid=…&secrets=1` | 导出账号。**v0.1.0 起默认不带明文凭据**，需要凭据必须显式 `secrets=1` |

### 3.4 代理出口

| 路径 | 作用 |
|---|---|
| `GET /proxy/slots` | 列出代理槽位 |
| `POST /proxy/slots/save` | 保存槽位 |
| `POST /proxy/slots/test` | 测一个槽位 |
| `POST /proxy/discover` | 扫描本机代理端口（默认 `17901-17910`） |

---

## 四、任务与福利

这是本项目相对同类网关的主要差异点：把账号的**成长任务中心**自动化跑完。

### 4.1 成长任务

`GET /tasks?uid=<uid>` 取任务清单，每条含
`task_code / name / description / status / current / target / reward_credit / reward_energy / unforgeable / reason`。

**状态含义**

| status | 含义 |
|---|---|
| `claimed` | 已领取（终态） |
| `accepted` | 已接取、进度未满 |
| `not_accepted` | 未接取 |
| `completed` | 上游标记完成 |

**v0.1.0 的显示改进**：上游标记 `unforgeable: true` 的任务（如「关注腾讯 WorkBuddy 官方公众号满 24 小时」）
现在显示为琥珀色 **「需人工」** 徽章并写出原因，**不再显示「进行中」** —— 那一档对可自动化任务是对的
（引擎正在推进度），对这类任务则是误导。

**触发一次全量任务**：`POST /tasks/run {"uid": "<uid>"}`（`uid` 省略则全部账号）。
流程是：批量接取 → 事件上报 → 领奖；日志会逐条说明每个任务的结果。

**领取单条奖励**：`POST /tasks/claim`

### 4.2 自动化的边界（哪些能自动、哪些不能）

| 任务类型 | 能否自动 | 机制 |
|---|---|---|
| 桌面端对话类（`RichMeow_Chat`） | ✅ | 6 连桌面端指纹事件链 |
| 页面行为类（`Library_read`） | ✅ | 网页端指纹单事件（`web_element_click`） |
| Buddy 应用类（`Buddy_App` / `Buddy_App_QQ`） | ✅ | 5 连 Buddy 事件链 |
| 「体验某专家」类（`expert_use_*`） | ✅ | 从 `jump_url` 通用解析专家 id |
| 计数类（`chat_5` / `template_5` / `expert_5` …） | ✅ | 事件上报；专家/团队类**每次必须换不同 id**（上游按 `(eventCode, id)` 去重） |
| 夜猫子（`black_cat`） | ⚠️ | 仅 23:00–08:00 窗口计数，每日 01:00 调度器自动跑 |
| 猫猫旅行 | ✅ | 09:00 派出 / 21:00 领奖闭环 |
| 连登管窥与抽奖 | ✅ | 补签保连登 → 礼包/补偿 → 连登兑换 → 自动抽奖 |
| **需真人操作**（`unforgeable: true`） | ❌ | 面板显示「需人工」并写明原因 |

> 不可伪造任务的领奖路径是通的：**真人做完之后上游会标成已达成，调度器会在下一轮自动补领** ——
> 判据只用 progress 口径（`current >= target`），不看会短暂回滚的 `accept_status`。

### 4.3 每日自动化（调度器）

面板「设置 → 调度」可查看与控制。后台调度器按整点排程运行：

| 时刻 | 动作 |
|---|---|
| 01:00 | **成长任务队列**（v0.1.0 新增）+ 夜猫子任务 |
| 09:00 / 21:00 | 自动签到 + 猫猫旅行（派出 / 领奖闭环）+ 连登管窥 |
| 22:00 | Token 保活（临近过期时主动刷新） |
| 每天两个随机时刻 | 每日活跃对话（国际版福利） |

| 路径 | 作用 |
|---|---|
| `GET /scheduler` | 调度器状态（各任务的时刻表、上次/下次运行时间、日志） |
| `POST /scheduler/trigger` | 立即跑一轮 |
| `POST /scheduler/toggle` | 开关调度器 |

> 「今天已经做过」的判定：上游状态优先（重复签到不算失败），
> 加上按日期粒度的补跑标记 —— 进程重启后不会重复打上游。

---

## 五、模型与出口

### 5.1 出口（realm）

国内版 `copilot.tencent.com` / 国际版 `www.workbuddy.ai`。
面板「网关」区可切换（`POST /realm`）。

**出口判定是单一事实源**（v0.1.0 修复）：单出口模型（只有一侧提供）按表走，
双出口模型回落到当前默认出口。此前有两份互相矛盾的表，导致
`glm-5v-turbo` **完全不可达**、`minimax-*` 被前缀一刀切钉到国内出口。

### 5.2 模型清单

| 路径 | 作用 |
|---|---|
| `GET /v1/models` | 对客户端的标准模型清单（**只需 API Key**，不需要面板会话） |
| `GET /catalog/refresh` | 强制从上游 `/v3/config` 重新拉目录 |
| `GET /pricing` / `pricing/refresh` / `pricing/mapping` | 计价表、刷新、模型别名映射 |

面板可勾选哪些模型写入客户端配置（DSH / Crush 等需要规格才会生成控件）。

### 5.3 超大工具定义

超过 `WB_MAX_TOOL_DEFINITION_BYTES`（默认 64KB）的**单个**工具定义会被压扁后再出站：
保留顶层属性名、类型与 `required`，更深的 object 只留 `{"type":"object"}`，长字符串截断。
解决 MCP 工具把整份 JSON Schema 塞进 `parameters`（实测 180KB / 3093 节点 / 32 层嵌套）
导致 **Kimi 系每次调用都 400（code 11133）**、而其他模型同一 payload 正常的问题。

---

## 六、用量与计费

| 路径 | 作用 |
|---|---|
| `GET /usage` | 用量汇总（按模型 / 出口 / 账号） |
| `GET /usage/analytics` | 分析视图 |
| `GET /usage/by-account` | 按账号 |
| `GET /usage/timeseries` | 时间序列 |
| `GET /usage/recent` | 最近请求 |
| `GET /usage/perf` | 性能分位（首字延迟、吞吐） |
| `GET /usage/remaining` | 剩余额度估算 |
| `GET /activity/history` | 活动历史（签到等） |

费用按 OpenRouter 公布的价折算成**等价 token**，再按汇率换算人民币/美元。
面板可切换币种（记在 localStorage）。

---

## 七、日志

| 路径 | 作用 |
|---|---|
| `GET /logs?limit=…&level=…&tag=…&search=…` | 取日志 |
| `POST /logs/clear` | 清空 |

**v0.1.0 新增运行时凭据脱敏**：账号 token 与 API Key 在进入日志前会被登记并打码
（长值优先、一次替换；过短的值不登记以免把错误码也打掉）。
手机号掩码沿用上游行为。

日志级别与标签是**自动推断**的，且推断用的是**原始串** —— 掩码发生在推断之后，
否则含手机号的日志行其级别分类会被改变。

SQLite 落库默认关闭（面板设置里开），关闭时日志只进内存环形缓冲。

---

## 八、设置项

`GET /settings` / `POST /settings/save`。主要分组：

| 组 | 内容 |
|---|---|
| 限额 | 每日 token 上限、每日积分上限、每模型每日 token 上限、额度保留 |
| 凭据 | API Key 增删（可绑账号 / 出口 / 模型白名单 / 并发上限）、面板密码 |
| 路由 | 模型别名、定价映射、后台模型改道名单 |
| 目录 | 模型目录来源与刷新、倍率显示 |
| 外观 | 主题（浅色/深色/跟随系统）、界面语言（中文/English） |
| 运行 | 调度器开关、SQLite 日志、每日活跃随机时刻、窗口探针 |

---

## 九、接口速查（面板面）

以下接口需要**面板会话**（`X-Panel-Token`）：

```
/panel/login  /panel/logout  /panel/status  /panel/password
/accounts*  /usage*  /v1/usage  /activity*  /tasks*  /scheduler*
/settings*  /updates*  /logs*  /console*  /pricing*  /agents*  /realm
/catalog/refresh  /proxy/*  /quota-scan/run  /shutdown
```

以下接口只需 **API Key**（或不鉴权）：

```
/v1/models  /v1/chat/completions  /v1/responses  /v1/messages(默认关)
/health  /panel/status
```

`/v1/balance-summary` 与 `/v1/usage` 已在 v0.1.0 排除出 CORS 白名单，
跨域页面读不到账号级数据。

---

## 十、常见操作流程

### 把本机已登录客户端的账号接进网关

1. 面板 → 账号 → 「本机桌面客户端账号」→ 检测 → 选中 → 导入
2. 每个账号点「测试」确认可用
3. 需要的话设置权重与代理槽位

### 每天自动领成长任务积分

**不需要任何操作** —— 01:00 调度器自动跑。
想立刻试一次：任务与福利区 → 「一键做任务」。

### 排查某个号为什么不出现在选号里

面板 → 账号 → 看该号的 `inCooldown` / `restrictedFor` / `lastError`：

| 字段 | 含义 |
|---|---|
| `inCooldown` | 普通冷却中（失败/限流的短窗口） |
| `restrictedFor` | **账号级风控**（上游 403 / code 11140）—— 固定 2 小时且**持久化**，重启不解。官方工单口径 1–3 个工作日自动解除，**期间严禁换号继续打同一台设备** |
| `modelCooldowns` | 只有某个模型被限（6004），其他模型照常可用 |
| `lastError` | 最近一次失败原因原文 |

---

## 十一、相关文档

| 文档 | 内容 |
|---|---|
| [README-v0.1.0.md](README-v0.1.0.md) | 项目介绍与架构 |
| [DEPLOY-v0.1.0.md](DEPLOY-v0.1.0.md) | 环境部署教程 |
| [BUGFIXES-v0.1.0.md](BUGFIXES-v0.1.0.md) | BUG 修复清单 |
| [CHANGELOG-v0.1.0.md](CHANGELOG-v0.1.0.md) | 优化日志与版本路线图 |
| [SECURITY-v0.1.0.md](SECURITY-v0.1.0.md) | 安全审查报告 |
