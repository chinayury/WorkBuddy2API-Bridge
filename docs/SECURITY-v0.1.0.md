# WorkBuddy2API-Hub v0.1.0 · 安全审查报告

> 审查对象：`release/` 源码树（上游 `ardeyouxipianyi/workbuddy2api-hub @ d1a8913` + 本地补丁层）
> 审查方式：分区只读逐行审计（4 个分区并行）+ 静态扫描 + 端到端实测
> 审查日期：2026-10-25 · 版本：v0.1.0

---

## 一、信任边界与威胁模型

```
                    ┌─────────────────────────────────────────┐
  客户端 ──HTTP──▶ │  网关进程（ThreadingHTTPServer，单进程）  │
  (面板/LLM)       │   ├─ 面板面：/panel/*  需面板会话 token    │
                    │   ├─ 管理面：/accounts /settings /tasks …  │
                    │   ├─ LLM 面 ：/v1/*     需 API Key 或免鉴权 │
                    │   └─ 公开面 ：/health /panel/status        │
                    └──────────────┬──────────────────────────┘
                                   │ HTTPS
                            ┌──────┴───────┐
                            │  腾讯上游     │  copilot.tencent.com
                            │  CodeBuddy   │  www.workbuddy.cn
                            └──────────────┘
```

**信任边界说明**

| 边界 | 判定依据 |
|---|---|
| 客户端 → 网关 | 全部不可信。面板会话与 API Key 是唯一的信任凭据 |
| 网关 → 反代 | 回环对端被信任可代表客户端说话（`peer_is_trusted`），但**仅在反代覆盖 `X-Real-IP` 时才安全**（见 S-14） |
| 网关 → 上游 | 上游返回的一切正文都是不可信输入，一律按「可能是攻击者输出」处理 |
| 凭据文件 → 网关 | `accounts/*.json` 由用户导入，视为半可信（可能是别人给的） |

---

## 二、供应链审查（结论：风险面极小）

对 `release/` 全部 31 个 `.py` 做 AST 级 import 扫描：

```
=== 第三方模块扫描 ===
  未发现任何第三方 import —— 全部为标准库或本地模块
标准库模块数: 297 ；本地模块数: 31
```

| 项 | 结果 |
|---|---|
| 第三方 Python 依赖 | **0 个** |
| `requirements.txt` / `pyproject.toml` / `setup.py` / `Pipfile` / `poetry.lock` | **均不存在** |
| Dockerfile 的依赖安装动作 | **无**（`COPY wb_*.py dashboard.html ./`，不执行 `pip install`） |
| 基础镜像 | `python:3.11-alpine` |

**结论：本项目没有可被投毒的依赖链。** 这是它相对同类项目最大的结构性安全优势 —— 一次 `pip install` 都不会发生。

### 2.1 危险调用逐项复核

静态扫描命中项**全部人工复核**，结论如下：

| 命中 | 位置 | 复核结论 |
|---|---|---|
| `subprocess` ×5 | `wb_update_portable.py` | **安全**。全部为列表形式参数（无 `shell=True`）：`["taskkill","/PID",str(int(pid)),"/F"]`、`["tasklist","/FI","PID eq %d"%int(pid),…]`、`["cmd","/c",bat,str(port)]`（`bat` 取自固定字面量集合）、`[os.path.join(root,script),str(port)]`。pid/port 均 int 化，脚本名取自固定集合 → **无命令注入面** |
| `subprocess` ×1 | `wb_atrest.py` | **安全**。`[sys.executable, script, WORKER_FLAG]`，参数全为模块内常量 |
| `random` ×2 | `wb_settings.py` | **非安全用途**。只用于「每日对话的两个随机时刻」抽签（`_draw_daily_chat_times`），不参与任何凭据生成 |
| `random` ×1 | `wb_accounts.py` | **非安全用途**（退避抖动） |
| `md5` ×1 / ×1 / ×3 | `wb_fingerprint.py` / `wb_pricing.py` / `wb_proxy.py` | **非安全用途**。`wb_fingerprint` 的 md5 是为了**复刻官方客户端 machineId 的 UUID 形状**（形态对齐，不是安全哈希）；另两处是缓存键/摘要 |
| `pickle` / `yaml.load` / `eval` / `exec` / `verify=False` | — | **全项目零命中** |

---

## 三、已修复的安全问题

| # | 严重度 | 问题 | 攻击路径 | 修复 |
|---|---|---|---|---|
| **S-1** | 高 | **并发名额永久泄漏 → 账号池被单个坏号拖垮** | 面板开启 `account_max_concurrent` 后，上游返回 400/404/405/409/413 等未分类状态码 → 重试循环里的裸 `raise` 跳出循环、跳过名额归还 → 该账号在飞计数只增不减 → 累计到上限后该账号**永久**被踢出调度，面板只显示「无可用账号」，看不出原因 | 两处裸 `raise` 前先归还名额并清标记 |
| **S-2** | 高 | **Slowloris 线程耗尽** | 建立 N 条 TCP 连接但不发送请求行 → 每条连接各占一个线程并永久阻塞在 `readline()` → `ThreadingHTTPServer` 每连接一线程且无上限 → 线程数无上限增长，正常请求排队直至不可用 | `Handler.timeout = 60`（同时兜住「声明 Content-Length 却少发字节」的读阻塞） |
| **S-3** | 高 | **非法 UTF-8 请求体挂死线程** | 任意 POST 带非 UTF-8 字节 → `.decode("utf-8")` 抛错时 `_body_consumed` 仍为 False → `_discard_body()` 再读同一段 Content-Length → 永久阻塞。批量重复即等于线程耗尽 | 先记账再解码；解码改 `errors="replace"`（与 chunked 路径统一） |
| **S-4** | 高 | **`glm-5v-turbo` 完全不可达**（可用性/DoS 面） | 请求该模型 → 出口判定表漏项 → 判成 intl → 而它只有国内版出口提供；intl→cn 兜底因「单出口模型」规则拒绝 → 任何请求都返回「无可用账号」 | 出口判定统一到单一事实源 `exclusive_realm()` |
| **S-5** | 中 | **账号导出默认产出明文凭据** | `GET /accounts/export` 不带参数即导出全部账号的 accessToken/refreshToken 明文；面板「导出」按钮恰好不带参数 → 一次点击就在磁盘上落一份明文凭据文件，无二次确认 | 默认 `secrets=0`，需要凭据必须显式 `secrets=1` |
| **S-6** | 中 | **账号级余额跨域可读** | `MANAGEMENT_PATH_PREFIXES` 不含 `/v1/balance-summary` → 命中 CORS 前缀 `/v1` → 回 `Access-Control-Allow-Origin: *`；该端点只要求 `_authorized()`（API Key 或未开鉴权）→ 任意网页可跨域读到本网关的账号规模与积分余额 | 把 `/v1/balance-summary` 并入管理路径排除表 |
| **S-7** | 低-中 | **管理路由的信任判定靠逐路由自查** | `/realm` 切的是**整池**出口，之前不在面板路由白名单里，只是靠路由内部自查 `_panel_ok()` 才没漏过去。白名单是「默认拒绝」的关口 —— 今后新增路由只要走这条分发就会自动继承「仅 API Key」 | 把 `/realm` 显式加入 `_is_panel_route` |
| **S-8** | 低 | **启动横幅把明文 API Key 打进 stdout** | 容器日志 / journald / CI 日志都会收走 stdout；同一个 Key 还被拼进 `?key=` URL → 收藏 / 转发 / 浏览器历史 / 反代访问日志都会长期留存 | 横幅只显示首 4 尾 4 |
| **S-9** | 中 | **指纹清洗被 Responses 风格的 content 分片绕过** | 客户端把系统提示写成 `content:[{"type":"input_text",…}]` → `sanitize_content` 只认 `type=="text"` → 原始指纹头原样透传 → 上游 11128 | 白名单补 `input_text` / `output_text` / `summary_text` |
| **S-10** | 中 | **服务端工具被静默改写成无名 function** | 面板 `local_web_tools` 关闭（默认）时客户端带 `tools:[{"type":"web_search"}]` → 无条件改成 `type:"function"` 且没有 name → 上游 11129/11101 或工具静默失效 | 只在 `type` 缺失时补 `function`，其余原样透传 |
| **S-11** | 中 | **流式路径把无 name 的 tool_call 透传给客户端** | 上游在流末尾发 `tool_calls:[{function:{name:"",arguments:"{}"}}]` 占位 → `clean_chunk` 只过滤 `function_call` 一种拼写 → OpenAI SDK 拿到没有 name 的 tool_call 直接抛错 | 补上与非流式一致的过滤 |

---

## 四、未修复的安全发现（残余风险 + 缓解建议）

以下均为审计发现、**本版未改**，每条给出触发前提与部署侧缓解办法。**没有一条需要立刻停服**。

| # | 严重度 | 问题 | 触发前提 | 缓解建议 |
|---|---|---|---|---|
| **S-12** | 高 | **面板密码默认值预言机**：`GET /panel/status` 免认证返回 `panel_password_is_default` | 未改过面板密码（默认 `admin`）时 | **本部署已改为随机强密码**（`PANEL_PASSWORD` 覆盖默认值）。该字段仅说明「是否还是默认值」，不泄露密码本身 |
| **S-13** | 高 | **登录限流在反代部署下可被 `X-Forwarded-For` 伪造绕过**：限流桶键在只有 XFF 时取链上第一个地址，而 XFF 是**追加**语义（`$proxy_add_x_forwarded_for`），客户端可自塞任意 IP → 每次都是新桶 → 「5 次/60 秒」形同虚设 → 面板密码可无限爆破 | 网关部署在 nginx 之后且 nginx **追加**而非覆盖 XFF，且客户端能直连网关端口 | **必须让反代覆盖 `X-Real-IP`**：`proxy_set_header X-Real-IP $remote_addr;`（代码已优先采信 X-Real-IP）。本部署 243 为直连 LAN、无反代，不受影响 |
| **S-14** | 中 | **一键配置的 `base_url` 只校验 scheme** → 可把网关真实 API Key 写进指向攻击者的客户端配置 | 面板会话 + 本机来源 | 校验 `base_url` 主机必须是本机网关地址 |
| **S-15** | 低-中 | **面板可触发任意本地文件打开**：`POST /accounts/import/desktop {"path":…}` → `open(path)` 无路径白名单（只回解析出的 uid/nickname，是「文件打开原语」而非完整读文件） | 面板会话 | `path` 必须落在桌面端扫描出的候选集合内 |
| **S-16** | 低 | **未认证端点泄露部署信息**：`/panel/status`（面板密码是否默认、是否已配 Key）、`/health`（realm、账号数、账号就绪数） | 任何未认证探测 | `/health` 只保留最小字段 |
| **S-17** | 低 | **`?key=` 把 API Key 写进 URL**：浏览器历史 / Referer / 反代访问日志都会留存 | 面板用 `?key=` 直开 | 面板改为先登录再取 Key；反代侧关闭 query string 日志 |
| **S-18** | 低 | **`GET /agents/cli` 免鉴权返回网关自带的 `wb_agents.py` 源码** | 任意访问 | 文件本就在仓库里，无实际机密；但若将来该文件含敏感信息，此路由即成泄露点 |
| **S-19** | 低 | **`/v1/usage` 与 `/v1/balance-summary` 的账号级数据面** | 已修（见 S-6） | — |

---

## 五、权限与凭据落盘现状

| 项 | 现状 | 评价 |
|---|---|---|
| 面板密码 | `wb_settings` 里 **PBKDF2/scrypt 类哈希 + 独立盐** 存储；本部署用随机强密码 | ✅ |
| 面板会话 token | **只在内存**（`wb_settings.PanelSessions`），容器重启即失效 | ✅ 比持久化安全 |
| API Key | `accounts/settings.json`，本部署用随机生成的长 Key | ⚠️ **明文落盘**（见下），但 **v0.1.0 起文件权限已收紧到 0600** |
| 账号 access/refresh token | `accounts/<uid>.json` **明文**；另有 `wb_atrest.py` 提供 AES-256-GCM 静态加密（用于读取桌面端加密凭据） | ⚠️ **明文落盘**（见下），但 **v0.1.0 起文件权限已收紧到 0600** |
| 网关自身日志 | 手机号已掩码；**v0.1.0 新增运行时凭据脱敏**（登记真实 token/Key，命中即打码，长值优先一次替换，过短值不登记以免误伤错误码） | ✅ |

### 明文凭据落盘（残余风险）

这是**上游的设计选择**：账号凭据与 API Key 以明文 JSON 存在 `accounts/` 目录。
理由是上游支持「直接编辑凭据文件 / 跨机导入」这类用法，加密会让这些用法失效。

**v0.1.0 已把文件权限收紧**：新增 `wb_atomic.py` 统一收口落盘，写入前设 `0600`，
账号凭据与面板设置（`settings.json` 里是面板密码哈希与 API Key 明文）都接入。
此前两处的实测权限是 `0o666`（`0666 & ~umask`，Linux 上 umask 022 是常态），
**同机任何用户都能读到**。同一项目里的 `wb_agents.py` 早就用 `WRITE_MODE=0o600`
做了同一件事（注释写明「DSH 在凭据文件可读时拒绝启动」）—— 同一个项目里不该有两套标准。

> 同一模块还修了 Windows 上的落盘失败：`os.replace()` 在目标被打开时必然
> `PermissionError [WinError 5]`（Python 打开文件不带 `FILE_SHARE_DELETE`），
> 导致凭据与设置**间歇性不落盘**而面板报告「保存成功」。现在改为有界重试，
> 重试耗尽仍失败则抛出异常，不静默吞掉。

**仍需部署侧确认**：

```sh
chmod 700 /opt/workbuddy2api-hub/accounts
chmod 600 /opt/workbuddy2api-hub/accounts/*.json
chmod 600 /opt/workbuddy2api-hub/.env
```

> 本部署已实测：`accounts/` 与 `usage/` 为 `0700`、`.env` 为 `0600`。

---

## 六、渗透测试记录（实测项）

| # | 测试 | 方法 | 结果 |
|---|---|---|---|
| T1 | 无 Key 访问 LLM 面 | `GET /v1/models` 不带 Authorization | **401** ✅ |
| T2 | 无 Key 访问面板面 | `GET /panel/status` | 200，但只返回最小状态（不含任何凭据） ✅ |
| T3 | 面板密码错误 | `POST /panel/login` 错密码 | 401，且计入限流桶 ✅ |
| T4 | 改密码后的旧密码 | 用部署前默认密码 `admin` 登录 | **401** ✅（部署时已覆盖为随机强密码） |
| T5 | 容器内文件完整性 | 比对容器内 4 个补丁文件 sha256 与本机源文件 | **逐一一致** ✅ |
| T6 | 真实对话链路 | `POST /v1/chat/completions` 带 Key | 200，2–3s，返回「收到」，usage 完整 ✅ |
| T7 | 流式链路 | 同上 `stream:true` | 200，`finish_reason:stop` + usage 帧 + `[DONE]` ✅ |
| T8 | 出口判定一致性 | 14 个代表性模型比对 `detect_model_realm` 与 `exclusive_realm` | **0 不一致** ✅（修复前 `glm-5v-turbo` 不一致且完全不可达） |
| T9 | 第三方依赖 | AST 级 import 全量扫描 | **0 个第三方依赖** ✅ |
| T10 | 危险调用 | 静态扫描 + 人工复核 | pickle/yaml.load/eval/exec/verify=False **全项目零命中**；subprocess 全为列表参数无 shell ✅ |

---

## 七、部署加固清单（上线前逐条确认）

- [ ] 面板密码**不是**默认值（`admin`），且 ≥ 16 位随机
- [ ] API Key 为随机生成、非 `sk-` 之外的弱值
- [ ] `accounts/` `0700`、其中 `*.json` `0600`、`.env` `0600`
- [ ] 网关端口只在内网可达；公网暴露必须前置反代 + TLS + IP 白名单
- [ ] 反代（若有）**覆盖** `X-Real-IP`（`$remote_addr`），并关闭 query string 日志
- [ ] `docker compose` 用**固定镜像 tag 或 digest**，不用 `:latest`
- [ ] `accounts/` 与 `usage/` 纳入备份，但**备份文件同样按 0600 保管**
- [ ] 面板账号与 API Key **分给不同的人**：面板会话能看全部账号、API Key 只能调模型
- [ ] 开启 `WB_TRUSTED_PROXIES` 时**只填反代真实 IP**，不要填 `0.0.0.0/0`
- [ ] 定期核对 `activity_history.jsonl` 的 `trigger` 分布：出现 `scheduler` 说明有无人值守动作在跑

---

## 八、审计未覆盖的范围（诚实声明）

| 项 | 说明 |
|---|---|
| 真实上游的抗压能力 | 未对上游做压力测试，也未构造畸形上游响应做模糊测试 |
| `wb_logstore.LogStore.query` 的 limit 上限 | 该函数在分区 3 范围之外，未逐行核 |
| Anthropic SDK 对「缺 `signature` 的 thinking 块」的反应 | 本机无该 SDK，未能离线验证（已标疑似） |
| `wb_agents.integrate` 写入的具体文件路径集合 | 只确认 `client_id` 取自固定表，未逐个核对路径构造 |
| 前端 XSS 全量审查 | 只做了危险 sink 的抽样（`innerHTML` / `insertAdjacentHTML` / `postMessage` / `javascript:`）；`esc()` 只转义 `& < > "`，**不转义单引号** —— 属性上下文若用单引号包裹需另行核对 |
| 依赖 CVE | 无第三方依赖，无 CVE 面可查 |

---

## 九、v0.1.1 – v0.1.6 增量安全审查（2026-10-27）

> 上一节及以前的所有结论基于 **v0.1.0（31 个模块）**。此后新增了
> `wb_alerts.py` 等模块，**其中引入了一条「外部输入 → 内部网络」的通道**，
> 必须单独审。本节是增量审查与修复记录。
>
> **审查对象**：`release/` @ v0.1.6（33 个模块）· **基线**：v0.1.0（`5cbcd34`）
> **审查方式**：只读逐行审计 + urllib 行为本地实测 + 针对性单测

### 9.1 变更面与供应链（结论：仍无第三方依赖）

| 项 | v0.1.0 | v0.1.6 |
|---|---|---|
| `.py` 模块数 | 31 | **33** |
| 第三方依赖 | 0 | **0**（AST 全量 import 扫描复核） |
| `requirements.txt` / `pyproject.toml` / `Pipfile` / `poetry.lock` | 均不存在 | **均不存在** |
| 新增对外 HTTP 路由 | — | **1 条**：`POST /alerts/test` |

`wb_alerts.py` 只 import 标准库（hashlib / hmac / ipaddress / json / os / queue /
socket / threading / time / urllib）+ 本地 `wb_settings`。**「没有可被投毒的依赖链」
这个结论在 v0.1.6 依然成立。**

> **口径说明**：上表的「`.py` 模块数」是 **发布树根目录的 `.py` 文件总数**（v0.1.6 = 33）。
> 其中 32 个是 `wb_*.py`（进镜像、参与逐文件 sha 校验），第 33 个是
> `console_tail.py` —— Windows 便携包的日志窗口工具，Linux 容器不使用、不进镜像。
> 别把这个 33 与部署侧「校验 33 个文件」（= 32 个 `wb_*.py` + `dashboard.html`）混为一谈。

### 9.2 本轮新引入的高危问题：SSRF（已修复）

**问题**：v0.1.5 的告警通道让网关能向**面板用户填写的任意 URL** 发 HTTP 请求，
而 `_post()` 对该 URL **零校验**：

| 目标 | 修复前实测 |
|---|---|
| `file:///c:/Windows/win.ini` | **成功读到文件内容**（默认 opener 带 `FileHandler`） |
| `http://127.0.0.1:8788/panel/status` | 真实发起连接 |
| `http://169.254.169.254/latest/meta-data/` | 真实发起连接（云元数据端点） |
| `http://10.x` / `192.168.x` / `172.16.x` | 真实发起连接（内网） |
| 301/302/307 跳转 | 默认 opener **跟随** → 字符串级白名单可被绕过 |
| `ftp://` / `data:` | 未被拦 |

配合 `/alerts/test` 会把 `str(exc)` 回显给面板（内含响应体），
构成完整的 **SSRF 读取链**；即使不回显响应体，也仍是「内网端口扫描 + 服务指纹」的
**盲 SSRF 预言机**。

**可达性**：需要面板会话。默认面板密码是 `admin`（v0.1.0 报告 S-12 已记；
本部署已改为随机强密码）—— 所以生产上的实际风险由部署侧缓解，
但**代码层不该留这条通道**。

**修复**（v0.1.6）：

| 措施 | 落点 |
|---|---|
| scheme 只允许 `http`/`https` | `wb_alerts._assert_webhook_url()` |
| 长度上限 2048 | 同上 |
| 禁止 userinfo（`user:pass@`） | 同上（既是凭据泄露，也是主机判定绕过写法） |
| 拒绝回环 / 私有段 / 链路本地（含云元数据）/ CGNAT(100.64/10) / 保留段 | `_is_internal_host()` + `_is_blocked_ip()` |
| **域名也要解析后判**（`evil.example` → 127.0.0.1） | `_is_internal_host()` 走 `getaddrinfo` |
| **多值 DNS 全部地址都要合法** | 同上，逐个判而不是只看第一个 A 记录 |
| **不跟随跳转**（公网 URL 302 到内网） | `_NoRedirectHandler` |
| **不回显响应体** | `_post()` 只读 1 字节；`_safe_error_text()` 只留类型与状态码 |
| **保存时也校验**（不让危险 URL 落盘） | `wb_proxy._handle_settings_save()` 的 alerts 分支 |
| **阈值有上下界** | `wb_settings._alert_thresholds()` / `wb_proxy._clamp_alert_int()` |

**残余风险（诚实声明）**：**DNS 重绑定**仍可能绕过「解析时检查」——
攻击者让一个域名先解析到公网 IP 通过校验，连接时再解析到内网。
彻底消除需要改成**主机白名单**，或「解析后按 IP 直连并校验 Host/SNI」。
当前实现把「任意 URL 打内网」降级为「仅公网地址可打」，已消除本轮审计发现的
全部可利用路径。

### 9.3 其它本轮新引入并已修复的问题

| # | 问题 | 后果 | 修复 |
|---|---|---|---|
| A | 阈值**只有下界没有上界** | `for_ticks=10**9` → 去抖永远攒不满 → **告警永远不发**，恰好抵消 v0.1.5 的全部价值 | `_alert_thresholds()` 加上下界 |
| B | 阈值非数字 → `int()` 抛 `ValueError`/`OverflowError` | 异常一路冒到 socket handler（那里没有 try）→ **连接被重置 + stderr 抛 traceback**，而不是回 400 | `_clamp_alert_int()` 接住三种异常 |
| C | alerts secret / webhook URL 未进日志打码名单 | 钉钉/飞书这类 webhook 常把 token 放在 URL 路径里，会长期留在日志/日志库 | `_register_pool_secrets()` 登记 secret、URL、ServerChan key |
| D | `set_alerts()` 无 schema 校验（只靠调用方） | 纵深防御缺失 | 校验下沉到 `wb_settings.set_alerts()`：字段白名单 + 长度 + 阈值区间 |
| E | `alerts()` 原样回显 `thresholds` | 往 thresholds 塞的键会被回显给面板 | 改白名单回显（只回三个已知键） |

### 9.4 复核确认**没有**问题的项

| 项 | 结论 |
|---|---|
| secret 外泄 | 公开视图、`/settings` 回读、`reply["alerts"]`、`account_views()`、导出路径 —— **逐条查过，均不含 secret** |
| `/alerts/test` 越权 | 与 `/settings/save` 同门 `_panel_ok()`；仅 POST；token 只走 header；CORS 不开 → 无跨域、无表单 CSRF |
| 导入投毒 `unverifiedSince` | `normalise_import_row()` 是**显式白名单**，不含该字段 → 构造账号 JSON 无法把它投毒成「永不派发」 |
| `mark_verified()` 跨账号解标记 | 只改 `self`；调用点的 `account` 都是发起该请求的那个对象 |
| `USAGE_DIR` 外部可控 | 只由环境变量决定，无 HTTP 写入口 → 只能由部署者/root 控制 |
| 状态文件写侧符号链接 | tmp 名带 `uuid4` + `O_EXCL|O_CREAT` → 保证新建、不跟随已存在符号链接；`os.replace` 原子替换 |
| 损坏状态文件 | 只置 `load_error`，**明确不发明暂停**（宁可少暂停，不可凭空停池）；下次写盘自愈 |
| `empty_pool` 误报 503 | 真实上游故障报文不会以 `no usable account` 开头 → 不会把上游故障报成「缺账号」 |
| 告警洪水 | 触发源受限；去抖 key 按账号；队列有界 200，满则丢并计数 → 无法无限放大 |

### 9.5 本轮渗透测试记录（新增项）

| # | 测试 | 结果 |
|---|---|---|
| T11 | webhook URL = `file:///etc/passwd` | **拒绝**（400，scheme 不允许）✅ |
| T12 | webhook URL = `http://127.0.0.1:8788/panel/status` | **拒绝**（400，回环）✅ |
| T13 | webhook URL = `http://169.254.169.254/latest/meta-data/` | **拒绝**（400，链路本地）✅ |
| T14 | webhook URL = `http://10.0.0.1/` / `192.168.x` / `100.64.0.1` | **拒绝**（400，私网 / CGNAT）✅ |
| T15 | webhook URL = `http://user:pass@example.com/` | **拒绝**（400，userinfo）✅ |
| T16 | 域名解析到 127.0.0.1 | **拒绝**（400，解析后判定）✅ |
| T17 | 投递失败时错误文本含响应体 | **不含**（只留异常类型/状态码）✅ |
| T18 | `for_ticks=10**9`（告警静默化） | 被夹到 100 ✅ |
| T19 | `min_ready="abc"` / `1e400` | 回落默认值，不抛异常、不 500 ✅ |
| T20 | 公开视图 / 回读 / 导出含 secret | **均不含** ✅ |
| T21 | 导入带 `unverifiedSince` 的账号 JSON | 该字段被白名单归一化丢弃 ✅ |
| T22 | 告警 webhook 收到 302 跳到内网 | **不跟随**（拒绝跳转）✅ |

（对应单测：`tests/_test_alerts_security.py`，27 项。）

### 9.6 部署加固清单（v0.1.6 增补）

- [ ] **若配了 webhook**：URL 必须是**公网可达**的地址；不要填 `127.0.0.1` /
      `169.254.169.254` / `10.x` / `192.168.x`（代码会拒绝，但说明意图有问题）
- [ ] 若 webhook URL 里带 token，确认 `accounts/settings.json` 权限是 `0600`
- [ ] `usage/` 必须在**持久卷**里 —— 否则 v0.1.6 的连坐暂停跨重启不存活
- [ ] 面板密码仍**不是**默认值（SSRF 的可达性完全取决于它）
