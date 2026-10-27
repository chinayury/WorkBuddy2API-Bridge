# v0.1.7 维护手册（243 网关）

> 生成时间：2026-10-28（v0.1.7 上线当天） · 适用：/opt/workbuddy2api-hub 的 WorkBuddy2API-Hub 部署
> 前一版：v0.1.6（见 `MAINTENANCE-v0.1.6.md`）。本版只讲 v0.1.7 新增的东西。

## 一、v0.1.7 改了什么（一句话）

**给告警通道加了 SSRF 防护** —— v0.1.5 那个「让网关去请求你填的 URL」的能力，
原本能打内网、回环、云元数据甚至读本地文件。

改动涉及 4 个文件：

| 文件 | 改动 | 行数量级 |
|---|---|---|
| `wb_alerts.py` | URL 校验（scheme / 长度 / userinfo / 内网判定 / 域名解析后判定）、禁跳转、不回显响应体、安全错误文本 | ~120 |
| `wb_proxy.py` | 保存时也校验；阈值上下界；alerts secret/URL 进日志打码名单 | ~40 |
| `wb_settings.py` | `set_alerts()` 校验下沉（字段白名单 + 长度 + 阈值区间）；`alerts()` 白名单回显 | ~50 |
| `tests/_test_alerts_security.py` | **新增**（27 项） | 新文件 |

## 二、SSRF 防护的五道闸门

配 webhook URL 时，**这五条会同时生效**：

| # | 闸门 | 挡什么 |
|---|---|---|
| 1 | scheme 只允许 `http` / `https` | `file://` 读本地文件 |
| 2 | 拒绝回环 / 私有段 / 链路本地 / CGNAT / 保留段 | `127.0.0.1`、`169.254.169.254`、`10.x` |
| 3 | **域名也要解析后判**（多值 DNS 全查） | `evil.example` 解析到 127.0.0.1 |
| 4 | **不跟随跳转** | 公网 URL 302 到内网 |
| 5 | **不回显响应体** | 响应正文被回显 = 完整读取链 |

外加：**保存时也校验**（危险 URL 根本落不了盘）、**投递时再校验一次**（老配置也挡得住）。

### 你会看到什么

配一个内网地址，面板会明确告诉你为什么被拒：

```
webhook URL 不合法: webhook URL 指向内网/回环地址（127.0.0.1）—— 网关不应代为访问自己的内网
```

**这不是故障** —— 是防护在正常工作。

## 三、告警阈值的上界（新增）

| 阈值 | 默认 | 范围 | 含义 |
|---|---|---|---|
| `min_ready` | 8 | 1–1000 | 可派发账号数低于它算降级 |
| `for_ticks` | 2 | 1–100 | 连续多少轮成立才发故障通知 |
| `clear_ticks` | 2 | 1–100 | 连续多少轮恢复才发恢复通知 |

**上界不是装饰**：`for_ticks` 设成极大值 → 去抖永远攒不满 → **告警永远不发**，
那等于白配。超界的值会被**夹到上界**，不是报错。

非数字（如 `"abc"`）会**回落默认值**，不会 500。

## 四、排查

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 面板说「webhook URL 不合法」 | 指向内网/回环/元数据，或带了 userinfo，或超长 | 换成你自己的**公网**告警接收地址 |
| 投递统计里 `failed` 一直涨 | 目标地址不可达 / 签名不匹配 | 看面板「最近一次」的 error 字段（**只含异常类型与状态码，不含响应正文**） |
| 投递统计里 `dropped` 涨 | 队列满（200）—— 说明告警在疯狂触发 | 看是哪个事件在刷；多半是 `min_ready` 设得太高 |
| 告警一直不发 | `for_ticks` 太大 / 阈值没到 | 面板看「已开启」状态与投递统计 |
| 告警发得太频繁 | 去抖的 `for_ticks` 太小 | 调大到 3–5 |

## 五、一个方法论上的提醒

v0.1.7 的 SSRF 是**两遍独立检查各自发现**的：一遍是安全审计 agent 的只读走查，
一遍是我自己的代码走查，互不依赖，指向同一处。

⇒ **新增「让服务端按用户输入发起请求」这类能力时，做两遍独立检查**：
一遍读代码找危险调用（会看到「URL 是用户填的」），一遍从「外部输入能到达哪里」
倒推（才会想到「默认 opener 带 FileHandler 且跟随跳转」）。

**单遍会漏**。

## 六、部署与回滚

```sh
# 部署
bash /tmp/deploy_v017.sh

# 回滚代码
cp -p /opt/workbuddy2api-hub/docker-compose.yml.bak-<TS> \
      /opt/workbuddy2api-hub/docker-compose.yml
cd /opt/workbuddy2api-hub && docker compose up -d --no-deps --no-build --pull never
```

**注意**：v0.1.6 之前的版本**没有** SSRF 防护。若回滚到 v0.1.5 或更早，
告警通道会恢复成「任意 URL 可打」—— 那就把 `settings.json` 里的 `alerts` 段删掉，
或确保面板密码足够强。

**回滚点**：
- 代码：镜像 `workbuddy2api-hub:0.1.6` 留存
- 数据：`accounts-before-deploy-<TS>.tar.gz`、`settings-before-deploy-<TS>.tar.gz`

## 七、验证（生产实测）

```sh
# SSRF 防护自测（容器内跑，只读）
docker exec wb-proxy python3 /opt/workbuddy2api-hub/ops-scripts/ssrf_probe.py
# => 15 类恶意目标全拒、2 个合法公网地址放行、投递阶段第二道闸门生效

# 告警配置自测（验证服务端拒绝内网地址 + 不残留）
docker exec wb-proxy python3 /opt/workbuddy2api-hub/ops-scripts/alert_config_selftest.py
```

## 八、目录索引

| 路径 | 说明 |
|---|---|
| `/opt/workbuddy2api-hub/release/` | 发布树（源码 + `tests/`） |
| `/opt/workbuddy2api-hub/ops-scripts/` | 运维脚本（含 `ssrf_probe.py`、`alert_config_selftest.py`） |
| `/opt/workbuddy2api-hub/backups/` | 回滚点 |
| `/opt/workbuddy2api-hub/accounts/settings.json` | 含告警配置（secret 只写不读），权限 0600 |
| 本机 `research/watch_243.py` | 只读巡检（唯一权威判据） |
| 本机 `research/可借鉴要点清单.md` | 借鉴清单（第十一节是本次安全审查） |
| 本机 `research/MAINTENANCE-v0.1.6.md` | 上一版手册（连坐暂停持久化） |
