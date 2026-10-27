# CHANGELOG v0.1.7

## 告警通道安全加固：SSRF 防护 + 阈值有界 + 凭据打码

v0.1.5 加的告警通道是本项目第一条「**外部输入 → 内部网络**」的通道 —— 网关会向
面板用户填写的 URL 真的发 HTTP 请求。本版对这条通道做安全审计与加固。

### 一、审计范围

审计对象：`release/` @ v0.1.6（33 个模块）· 基线：v0.1.0（`5cbcd34`）

新增模块 `wb_alerts.py`；新增对外路由 **1 条**：`POST /alerts/test`。

**供应链结论仍成立**：AST 全量 import 扫描 → **0 个第三方依赖**，
`requirements.txt` / `pyproject.toml` / `Pipfile` / `poetry.lock` 均不存在。
「没有可被投毒的依赖链」这个 v0.1.0 的结构性优势没有被削弱。

### 二、SSRF（最高危，已修复）

**问题**：`_post()` 对面板填的 URL **零校验**。实测（本地 Python 3.14）：

| 目标 | 修复前 |
|---|---|
| `file:///c:/Windows/win.ini` | **成功读到文件内容**（默认 opener 带 `FileHandler`） |
| `http://127.0.0.1:8788/panel/status` | 真实发起连接 |
| `http://169.254.169.254/latest/meta-data/` | 真实发起连接（云元数据） |
| `10.x` / `192.168.x` / `172.16.x` / `100.64.0.1` | 真实发起连接（内网） |
| 301/302/307 跳转 | 默认 opener **跟随** → 字符串级白名单可被绕过 |
| `ftp://` / `data:` | 未被拦 |

配合 `/alerts/test` 把 `str(exc)` 回显给面板（内含响应体），
构成完整的 **SSRF 读取链**；即使不回显响应体，也仍是「内网端口扫描 + 服务指纹」的
**盲 SSRF 预言机**。

**可达性**：需要面板会话。默认面板密码是 `admin`（v0.1.0 报告 S-12 已记，
本部署已改为随机强密码）—— 生产实际风险由部署侧缓解，**但代码层不该留这条通道**。

**修复**：

| 措施 | 落点 |
|---|---|
| scheme 只允许 `http`/`https` | `wb_alerts._assert_webhook_url()` |
| 长度上限 2048 | 同上 |
| 禁止 userinfo（`user:pass@`） | 同上（既是凭据泄露，也是主机判定绕过写法） |
| 拒绝回环 / 私有段 / 链路本地（含云元数据）/ CGNAT(100.64/10) / 保留段 | `_is_internal_host()` + `_is_blocked_ip()` |
| **域名也要解析后判**（`evil.example` → 127.0.0.1） | `_is_internal_host()` 走 `getaddrinfo` |
| **多值 DNS 全部地址都要合法** | 逐个判，不只看第一个 A 记录 |
| **不跟随跳转**（公网 URL 302 到内网） | `_NoRedirectHandler` |
| **不回显响应体** | `_post()` 只读 1 字节；`_safe_error_text()` 只留类型与状态码 |
| **保存时也校验**（不让危险 URL 落盘） | `/settings/save` 的 alerts 分支 |

> **残余风险（诚实声明）**：**DNS 重绑定**仍可能绕过「解析时检查」。
> 彻底消除需要改成主机白名单，或「解析后按 IP 直连并校验 Host/SNI」。
> 当前实现把「任意 URL 打内网」降级为「仅公网地址可打」，
> 已消除本轮审计发现的**全部可利用路径**。

### 三、另外四个本轮新引入的问题（已修复）

| # | 问题 | 后果 | 修复 |
|---|---|---|---|
| A | 阈值**只有下界没有上界** | `for_ticks=10**9` → 去抖永远攒不满 → **告警永远不发**，恰好抵消 v0.1.5 的全部价值 | `_alert_thresholds()` 加上下界 |
| B | 阈值非数字 → `int()` 抛 `ValueError`/`OverflowError` | 异常一路冒到 socket handler（那里没有 try）→ **连接被重置 + stderr 抛 traceback**，而不是回 400 | `_clamp_alert_int()` 接住三种异常 |
| C | alerts secret / webhook URL 未进日志打码名单 | 钉钉/飞书这类 webhook 常把 token 放在 URL 路径里，会长期留在日志/日志库 | `_register_pool_secrets()` 登记 secret、URL、ServerChan key |
| D | `set_alerts()` 无 schema 校验、`alerts()` 原样回显 thresholds | 纵深防御缺失；塞进去的键会被回显给面板 | 校验下沉到 `wb_settings.set_alerts()`；回显改白名单 |

### 四、复核确认**没有**问题的项

| 项 | 结论 |
|---|---|
| secret 外泄 | 公开视图 / `/settings` 回读 / `reply["alerts"]` / `account_views()` / 导出路径 —— 逐条查过，**均不含 secret** |
| `/alerts/test` 越权 | 与 `/settings/save` 同门 `_panel_ok()`；仅 POST；token 只走 header；CORS 不开 → 无跨域、无表单 CSRF |
| 导入投毒 `unverifiedSince` | `normalise_import_row()` 是**显式白名单**，不含该字段 → 构造账号 JSON 无法把它投毒成「永不派发」 |
| `mark_verified()` 跨账号解标记 | 只改 `self`；调用点的 `account` 都是发起该请求的那个对象 |
| `USAGE_DIR` 外部可控 | 只由环境变量决定，无 HTTP 写入口 → 只能由部署者/root 控制 |
| 状态文件写侧符号链接 | tmp 名带 `uuid4` + `O_EXCL|O_CREAT` → 不跟随已存在符号链接；`os.replace` 原子替换 |
| 损坏状态文件 | 只置 `load_error`，**明确不发明暂停**；下次写盘自愈 |
| `empty_pool` 误报 503 | 真实上游故障报文不会以 `no usable account` 开头 |
| 告警洪水 | 触发源受限；去抖 key 按账号；队列有界 200，满则丢并计数 |

### 五、验证

- **新增单测** `tests/_test_alerts_security.py`（27 项）—— SSRF 各类目标、
  域名解析后判定、userinfo、超长 URL、跳转拒绝、错误文本不含响应体、
  阈值上下界与非数字回落、落盘前 schema 校验、secret 不回显。
- **回归**：`python tests/run_all.py --jobs 4` → **232 passed / 0 failed / 0 skipped**
  （套件 231 → 232）。
- **文档**：`SECURITY-v0.1.0.md` 增第九节（增量安全审查 + 12 项新增渗透测试记录 +
  部署加固清单增补）；`DEPLOY-v0.1.0.md` 与 `USAGE-v0.1.0.md` 同步 v0.1.1–v0.1.6
  的新能力（告警配置、连坐状态文件、未验证账号、暂停持久化）。

### 六、本版没有做的事

- **不做主机白名单**（那要改配置形态：从「填 URL」变成「填主机名」）。
  当前实现已消除全部可利用路径，白名单是更彻底的形态，留待需要时再做。
- **不做 DNS 重绑定防护**（需要「解析后按 IP 直连 + 校验 Host/SNI」，
  属于出站连接层的改造，超出本模块职责）。
- 不引入任何第三方依赖来做 URL 校验 —— 保持「0 第三方依赖」这个结构性优势。
