# 安全审查报告（v0.1.0 正式版）

> 审查对象：`release/` @ 2026-10-28 · 版本 **0.1.0** · 32 个 `wb_*.py` + `dashboard.html` + `ops/aliyun` + 5 个 CI workflow
> 方法：4 个独立审计（代理核心与设置 / 全树安全 / 任务与告警与更新 / 账号池与调度器）
> + 对每条论断的**实测复核** + 逐条修复 + 回归网。
>
> **本报告只写经过验证的结论。** 未实施的项在第五节，逐条写明原因与影响 ——
> 不把「没做」藏起来，也不把「做了」说成「彻底解决」。

---

## 一、总体结论

| 维度 | 结论 |
|---|---|
| 第三方依赖 | **0**（AST 全量扫描 226 个 .py；运行时模块只 import 标准库）。CI 里也没有任何 `pip install` |
| 凭据静态存储 | **加密 + 0600**（`wb_atrest.py` 的 AES-256-GCM；`wb_atomic.py` 的权限收紧 + 原子替换） |
| 面板口令 | PBKDF2-HMAC-SHA256 / **120,000 轮** / 16 字节随机盐 / `hmac.compare_digest` 比较 |
| 会话令牌 | `secrets.token_urlsafe(24)`，**只在内存**，7 天 TTL，**只从请求头取**（不从 query string） |
| API Key 比较 | `hmac.compare_digest`（时序安全） |
| CORS | 只放开 `/v1` `/chat` `/completions` `/models` `/responses`；管理类前缀**明确排除** |
| 路由鉴权 | 白名单 + 默认拒绝（`_is_panel_route`）；管理类操作要**面板会话**，API Key 不够 |
| 更新链路 | HTTPS + SHA-256 校验 + manifest 交叉验证 + 逐文件 sha + 自动回滚；**无代码签名**（见 4.3） |
| 本轮修复 | 1 项 CRITICAL、8 项 HIGH、6 项 MEDIUM |

**一句话**：这不是一个「加固过」的网关，而是一个**结构上就很难被攻破**的网关 ——
零依赖、头文件鉴权、参数化 SQL、无 `eval`/`exec`/`shell=True`、
凭据静态加密、原子写 + 权限收紧。本轮的问题出在**几个具体判断**上，而不是整体架构。

---

## 二、本轮修复的高危项

### 2.1 CRITICAL　`settings.json` 损坏 ⇒ 网关 fail-open

**漏洞**：`wb_settings.load()` 把「文件不存在」与「文件存在但解析不了」当成同一件事（都返回空 dict）。
于是损坏会同时导致四件事：

1. 面板密码回落成默认的 `admin`（`verify_panel_password` 看到没有 hash 就认 admin）；
2. API Key 列表变空 ⇒ `auth_required()` 判定「不需要鉴权」；
3. `/v1/chat/completions` 对**所有能碰到端口的人开放** —— 直接消耗上游账号额度；
4. 每日限额/积分限额一并回落到默认值。

更糟的是**放大器**：面板的第一次写入会用这个空 dict **覆盖**坏文件
⇒ API Key 与密码哈希彻底丢失，回不来了。

**修复**：加载失败打 `_UNREADABLE` 标记 → 面板登录与鉴权判定见到标记就**拒绝**（fail closed）
→ `save()` 拒绝用带标记的内容覆盖原文件 → 提供 `quarantine_unreadable()` 把坏文件留档成 `.corrupt`。

**回归网**：`tests/_test_settings_fail_closed.py`（16 项）。

**残余风险**：这是**事后**的保护 —— 已经损坏过的部署需要重启网关才会读到标记。
第一次损坏与第一次重启之间，窗口仍然存在。要彻底消除，需要在写入侧也做校验
（写入前先确认能读），本轮未做。

---

### 2.2 HIGH　上游错误正文里的 bearer token 落盘 + 进日志

**漏洞**：上游的 401/403 正文**会回显授权头**（这件事 `wb_activity.safe_message`
的注释里就写着 —— 活动历史那侧早就做了脱敏，用量日志这侧漏了）。
`record_error()` 把原文截断 200 字符后：

- 原样写进 `usage/usage.jsonl`（默认权限 0644 ⇒ 同机任何用户可读）；
- 原样打进运行日志（⇒ 又进面板的「运行日志」页）。

**修复**：两处都改走 `wb_activity.safe_message()`（**与活动历史同一套规则**，
避免两处规则漂移出新的漏洞）；`usage.jsonl` 权限 0600、`usage/` 目录 0700。

**回归网**：`tests/_test_security_v010.py`（15 项，含「日志行也脱敏」）。

---

### 2.3 HIGH　面板登录限速被 `X-Forwarded-For` 绕过

**漏洞**：`login_rate_limit_key()` 取 XFF 的**最左**一段。
nginx 的 `$proxy_add_x_forwarded_for` 是**追加** —— 客户端自带的
`X-Forwarded-For: 1.2.3.4` 原样留在最左，代理把真实来源追加到最右。
⇒ 最左是**客户端自己写的** ⇒ 每次登录请求换一个伪造 IP，5 次/60 秒的上限形同虚设。
且回环对端被当成可信 ⇒ 本机任意非特权进程也能绕过。

**修复**：从右往左找第一个能解析的段（代理追加的、客户端改不动的那一段）。
`X-Real-IP` 仍优先。

**残余风险**：如果部署方用的代理**替换**整个 XFF 头，右半段也是客户端可控的。
最稳的做法是让代理写 `X-Real-IP`（nginx：`proxy_set_header X-Real-IP $remote_addr`），
本项目已优先采信它。

---

### 2.4 HIGH　面板口令 `?pwd=` 留在地址栏

**漏洞**：用 `?pwd=` 自动登录后，口令留在地址栏 ⇒ 进浏览器历史、同步到别的设备、
出现在屏幕共享里，还会作为 `Referer` 发给站外资源。
`?key=` 早就被清理了，`?pwd=` 漏了。

**修复**：读到 `pwd` 后**立刻** `history.replaceState` 删掉（保留 `?tab=` 等其它参数）。

---

### 2.5 HIGH　告警通道 SSRF 防护在 DNS 解析失败时 fail-open

**漏洞**：`wb_alerts._is_internal_host()` 在域名解析失败时 `return False`（不阻断），
而校验与连接是**两次独立的解析** ⇒ 一个域名第一次解析失败、第二次解析到
`127.0.0.1`，就能通过校验并连到内网。

**修复**：解析失败一律**拒绝**（fail closed）。收不到告警比被 SSRF 打穿好得多。
同仓库 `wb_webtools._guard_url` 本来就是 fail closed 的 —— 现在两处策略一致。

**附带**：`wb_settings.set_alerts()` 补上 URL 合法性校验，危险 URL 落不了盘。

**残余风险**：**DNS 重绑定**仍可能绕过 —— 校验时解析到公网 IP，连接时解析到内网 IP。
彻底消除需要「解析后按 IP 直连并校验 Host/SNI」，属于出站连接层的改造。
本轮未做（见 4.2）。

---

### 2.6 HIGH　便携更新路径无体积限制 ⇒ zip bomb

**漏洞**：`wb_update_portable.download()` 读到 EOF、`safe_extract()` 逐条写出，
**无任何上限** ⇒ 几百 KB 的 zip 能解压出几十 GB，把磁盘写满。
这条路径在 Docker 部署里以 **root** 运行（`docker-compose.yml` 的
`user: "${PUID:-0}:${PGID:-0}"`）。

**修复**：把 systemd 更新器已有的三道限制搬过来 ——
`ARCHIVE_LIMIT=128MB`（下载体积）、`UNPACKED_LIMIT=256MB`（**解压总量**）、
`FILE_LIMIT=1200`（文件数量）。**解压总量**才是 zip bomb 的正解。

---

### 2.7 HIGH　异常逃到 socket handler ⇒ 连接重置而非 500

**漏洞**：处理函数里的异常一路冒到标准库的 `handle_error()` ⇒
客户端看到**连接被重置**（没有状态码、没有错误正文），
用量账本里不留这一笔，运维只看到一段栈。

**修复**：分发处包一层 `try/except` ⇒ 返回正经的 500，栈照样打。

---

### 2.8 HIGH　请求构造阶段抛异常 ⇒ 两个计数器泄漏

**漏洞**：构造请求那段（缓存键注入 → 压缩 → 造头 → 建 Request）原本在 `try` 之外，
异常时循环外的统一归还走不到 ⇒ 名额与负载计数双双泄漏。

**修复**：整体包进 `try`，异常时归还并换号。相关还有一处：
`urlopen` 成功之后、`bind` 之前若抛异常，`resp` 没人关 ⇒ 连接池里泄漏一条 TCP/TLS 连接。

---

## 三、攻击面逐项复核

### 3.1 路由 × 鉴权矩阵

| 路由 | 面板会话 | API Key | 写操作 | 泄露什么 |
|---|---|---|---|---|
| `/`, `/dashboard`, `/ui` | ✗ | ✗ | — | 看板静态页（无凭据） |
| `/agents/cli` | ✗ | ✗ | — | `wb_agents.py` 源码（**有意公开**） |
| `/panel/status`, `/health` | ✗ | ✗ | — | 区域、账号数、是否仍是默认口令（LOW，见 4.1） |
| `/v1/models`, `/v1/balance-summary` | ✗ | ✓ | — | 模型表 / 积分汇总 |
| `/v1/chat/completions`, `/v1/responses`, `/v1/messages` | ✗ | ✓ | ✓ | — |
| `/panel/login` | — | — | ✓ | 发放 7 天会话令牌；5 次/60 秒限速 |
| `/accounts*`, `/usage*`, `/activity*`, `/tasks*`, `/scheduler*`, `/settings*`, `/updates*`, `/logs*`, `/pricing*`, `/agents*`, `/proxy*`, `/realm` | **✓** | ✗ | 部分 | 账号池、用量、日志、配置 |
| `/settings/reveal` | **✓** | ✗ | — | 完整 API Key（**只凭面板会话**） |
| `/accounts/export?secrets=1` | **✓** | ✗ | — | 全部账号的 accessToken/refreshToken |
| `/settings/save`, `/shutdown`, `/updates/install` | **✓** | ✗ | ✓ | 最高影响的三个写操作 |
| `/alerts/test`, `/quota-scan/run`, `/catalog/refresh`, `/pricing/*` | **✓** | ✗ | ✓ | 发起出站请求 / 花上游额度 |
| `/agents/apply`, `/agents/restore` | **✓** | ✗ | ✓ | 写本机客户端配置（**且要求回环对端**） |
| `/u/<ref>/…` | 重写到尾部路径 | — | — | 定址账号调用；`/u/<ref>/usage` 仍归面板鉴权 |

**结论**：面板会话是更强的凭据（它能满足 API Key 检查），所以「管理类操作要面板会话」
这条是对的。`/proxy/slots` 只凭面板会话而 `/accounts` 要两者 —— 这条不一致是**设计选择**
（代理槽不含账号凭据），不修。

### 3.2 出站请求清单

| 调用点 | 目标 | 可控方 | 防护 | 判定 |
|---|---|---|---|---|
| `wb_webtools._open_url` | 模型给的 URL | 提示词内容 | scheme 白名单、禁 URL 凭据、私网/回环判定 + DNS 解析后判定、跳转重验、IPv4-mapped IPv6 拆包、响应体上限 | 好（残余 DNS 重绑定，见 4.2） |
| `wb_alerts._post` | webhook | 面板操作者 | v0.1.7 五道闸门 + 本轮改为 fail closed | 好（残余同上） |
| `wb_ipintel` | `http://ip-api.com/...` | 出口 IP | URL 转义 | **明文 HTTP** ⇒ 可被中间人伪造地理位置（它决定代理槽），本轮未改 |
| `wb_updates` | `api.github.com` | `WB_UPDATE_REPO` 环境变量 | 仓库名正则、HTTPS | 可（无签名，见 4.3） |
| `wb_update_portable` | release 资产 | 同上 | HTTPS + SHA-256 + **本轮补的体积限制** | 好 |
| `ops/aliyun/…updater.py` | GitHub | 同上 | 仓库名正则、体积限制、SHA-256、路径白名单 | **好（这条是加固参考实现）** |
| `wb_pricing` | `openrouter.ai` | 不可控 | 固定主机 | 可 |
| `wb_modelsdev` | `models.dev` | 不可控 | 固定主机 | 可 |
| `wb_accounts` / `wb_tasks` | `workbuddy.ai` / `codebuddy.cn` | 不可控 | 固定来源 | 可 |
| `console_tail` | `127.0.0.1:<port>` | 不可控 | 硬编码 | 可 |

### 3.3 凭据暴露面

| 位置 | 含凭据？ | 控制 | 状态 |
|---|---|---|---|
| `accounts/settings.json` | API Key + 面板口令哈希 | `tighten_mode` 0600 + 原子替换 | **好** |
| `accounts/<uid>.json` | access/refresh token | uid 清洗为 `[A-Za-z0-9_-]` + 0600 | **好** |
| `usage/usage.jsonl` | **曾含 bearer token** | 本轮：脱敏 + 0600 | **已修** |
| `activity/history.jsonl` | 上游自由文本 | `safe_message()` 七条脱敏规则 | 好 |
| `dashboard.html` localStorage | API Key 明文 | 无 | 可讨论（见 4.4） |
| `dashboard.html` sessionStorage | 面板令牌 | 关闭标签页即失效 | 可接受 |
| `/settings/reveal` | 完整 API Key | 面板会话 | 设计如此 |
| `/accounts/export` | 全部令牌 | 面板会话 + `?secrets=1` | 设计如此（默认不带凭据） |
| `?pwd=` / `?key=` | 口令 / API Key | `?pwd=` 本轮已清理；`?key=` 早已清理 | 部分 |
| 崩溃栈 | 路径、可能的局部变量 | 标准库默认 | **未验证**（未找到栈处理器） |

### 3.4 零依赖验证

**方法**：AST 解析树里每个 `.py`（226 个文件），收集 `ast.Import` / `ast.ImportFrom`
的模块根名，减去 `sys.stdlib_module_names` 与项目自身的模块名。

**结果**：运行时 32 个 `wb_*.py` + `console_tail.py` **只 import 标准库**。
唯一的第三方命中是 `tests/_mobile_check.py:245` 的 `playwright`（可选的手动移动端检查脚本，
不在任何 workflow 里）。

**危险原语扫描**：运行时模块里 **0** 处 `eval` / `exec` / `pickle` / `yaml.load` / `sqlite3`；
**0** 处 `shell=True`；唯一的 `compile()` 在 `ops/aliyun/…updater.py:385`，是**语法检查**
（构造 code object，不执行）。

**CI**：`release/.github/workflows/*.yml` 里**没有 `pip install`** ——
零依赖这个属性在 CI 里也保持着。下载的 CPython 运行时是 **SHA-256 钉死**的。

---

## 四、已知残余风险（诚实声明）

### 4.1 默认面板口令 `admin`（未修，**部署侧必须处理**）

`DEFAULT_PANEL_PASSWORD = "admin"` 在没有哈希时直接生效。
一个面板会话 ⇒ `/accounts/export?secrets=1` 导出全部账号令牌、
`/settings/save` 铸造 API Key、`/shutdown` 停服务、`/updates/install` 装更新。

**为什么不改代码**：它是**既有设计**（README 明写「首次登录后请立即改掉」）。
改成「非回环绑定 + 默认口令 ⇒ 拒绝启动」会让忘记改密码的**存量部署直接起不来** ——
这是个会伤到现有用户的改动，不该由一次安全加固顺手做掉。

**⇒ 部署要求（必须做）**：在 `.env` 里设 `PANEL_PASSWORD=<随机强口令>`，
该文件权限 0600。compose 已经支持这个变量。

### 4.2 DNS 重绑定（未修，两处都有）

校验时解析一次、连接时再解析一次 ⇒ 一个域名可以先给公网 IP、后给内网 IP。
彻底消除要改成「解析后按 IP 直连 + 校验 Host/SNI」，属于出站连接层的改造。
`wb_alerts` 与 `wb_webtools` 都有这个窗口。

### 4.3 更新链路无代码签名（未修）

完整性只靠「从同一个 release 下载的 SHA256SUMS」——
防的是**传输损坏**，不是**发布源被攻破**。
GitHub 账号被接管 ⇒ 任意代码以 root 身份执行。
彻底解决需要 minisign / Sigstore 签名。

### 4.4 API Key 存 `localStorage`（未修）

面板令牌在 `sessionStorage`（关闭标签页即失效），API Key 在 `localStorage`（长期）。
改存储位置会让「刷新页面免重新登录」消失。

### 4.5 `?key=` 仍可从 query string 传 API Key（未修）

会进访问日志 / 历史 / Referer。改掉会破坏「一条链接直接接入」这个主推用法。

### 4.6 `wb_ipintel` 用明文 HTTP（未修）

它查的是**出口 IP 的地理位置**，而这个结果决定代理槽的选择。
中间人可以伪造 ⇒ 槽位选错。（不影响凭据本身。）

### 4.7 CI 的 `uses:` 固定到 tag 而非 commit SHA（未修）

改成 SHA 需要维护一份更新列表。本项目 CI 不跑第三方 action，风险有限。

---

## 五、这一轮的方法论沉淀

1. **自己写的代码最需要审计。** 本轮 8 个 HIGH 里有 **3 个是我这一轮自己引入的**：
   半开探测是死代码、负载计数泄漏、刷新副作用放大。
   它们全都被「独立审计 + 实测复现」抓住了。
   ⇒ **自己写的代码不能因为「我刚写的」就免检。**

2. **审计论断要实测复核，但复核本身也可能测错场景。**
   审计给出的 C1（半开探测死代码）经实测**成立**；但我第一版验证脚本测的是
   「一个账号冷却、其它健康」—— 按设计（健康优先）那种情况**本就不该**触发探测。
   ⇒ 「探测没被触发」不等于「死代码」。
   **真正的证据是内部状态恒为空**（`_recovery_deadlines == {}`）。
   ⇒ 验证脚本要能区分「设计如此」与「实现坏了」。

3. **旧测试可能固化的是漏洞。**
   `_test_hotpath_savings.py` 里有一条断言「损坏文件 → `load()` 返回 `{}`」——
   在它写下的那天是对的，但它把 CRITICAL 的 fail-open 变成了「有测试保护的行为」。
   ⇒ 改行为时要问：这条断言保护的是**意图**还是**当时的实现**。

4. **两处同类防护必须同策略。** 本仓库的 `wb_alerts`（fail open）与
   `wb_webtools`（fail closed）对「DNS 解析失败」的处理相反 ——
   一个是「解析不了就不阻断」，一个是「解析不了就拒绝」。
   ⇒ **同一类判断在代码库里必须只有一个策略**，否则总有一处是洞。

5. **失败要闭，但「收不到通知」比「被打穿」好。**
   把告警的 DNS 解析失败改成 fail closed 的代价是「DNS 抖一下时告警发不出去」，
   收益是「网关不会替攻击者打内网」。这类取舍要写进注释，
   免得下一个人又把它改回 fail open（看起来更「可用」）。
