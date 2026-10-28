# 部署与升级指南（v0.1.0 正式版）

> 本文件是 **v0.1.0 的部署与升级增量说明**。
> 完整的部署教程见 [`DEPLOY-v0.1.0.md`](DEPLOY-v0.1.0.md)（那份是长期维护的主文档）。
>
> 本版变化集中在两处：**升级动作**（从 v0.1.7 到 v0.1.0）与**新增的配置项**。

---

## 一、环境要求

| 项 | 要求 | 备注 |
|---|---|---|
| Python | **3.9+** | 官方镜像用 3.11 |
| 操作系统 | Linux / Windows / macOS；OpenWrt 有专门打包 | |
| Docker | 可选，推荐 | `Dockerfile`（自包含）或 `Dockerfile.local`（基于官方镜像 digest） |
| 依赖 | **无需 pip install** | 本项目**零第三方依赖**（AST 全量扫描确认） |
| 内存 | 建议 ≥ 512MB | 单请求体上限默认 16MB |
| 端口 | 默认 8788 | 可改 |
| 时区 | **建议显式设置 `TZ`** | 见 §4.3 |

---

## 二、首次部署（Docker）

```sh
git clone https://github.com/chinayury/workbuddy2api-hub.git
cd workbuddy2api-hub/release

# 面板密码：生成随机强口令，不要用默认的 admin
PANEL_PWD=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
echo "面板密码: $PANEL_PWD"
cat > .env <<EOF
PANEL_PASSWORD=$PANEL_PWD
EOF
chmod 600 .env

# 构建并启动
docker build -f Dockerfile.local -t workbuddy2api-hub:0.1.0 .   # 只通 GHCR 时用这个
docker compose up -d
```

**验收**：

```sh
curl -s http://127.0.0.1:8788/health
# {"ok": true, "realm": "cn", "accounts": 19, "accounts_ready": 19, "api_key_required": true, ...}

curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer <你的 API Key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"ping"}]}'
```

> **⚠️ 面板密码务必改掉。** 默认口令 `admin` 是既有设计（README 明写「首次登录后请立即改掉」），
> 本版没有改成强制 —— 因为那会让忘记改密码的存量部署直接起不来。
> 但面板会话能导出全部账号令牌、铸造 API Key、停服务、装更新。
> 详见 [`SECURITY-v0.1.0-formal.md`](SECURITY-v0.1.0-formal.md) §4.1。

---

## 三、从 v0.1.7 升级到 v0.1.0

### 3.1 版本号是**降序**的

v0.1.0 是**正式稳定版**的重新起版，版本号比 v0.1.7 小。
这不是笔误，是有意为之：正式版从 0.1.0 重新计数，历史版本线保留在
`docs/CHANGELOG-v0.1.*.md` 里。

⇒ **git tag 要打 `v0.1.0`**（CI 的 `version-assert` 会校验 tag == 源码版本）。

### 3.2 升级步骤

```sh
# 1. 备份（配置与用量是 precious 的）
cd /opt/workbuddy2api-hub
tar czf backups/pre-v010-$(date +%Y%m%d-%H%M%S).tar.gz accounts usage

# 2. 更新发布树（改 release/ 目录的内容）
#    3a. 本地构建：
#        cd release && python tests/run_all.py --jobs 4   # 241 套件全绿再打包
#    3b. 服务器上构建（推荐：构建负载不要压工作站）：
#        docker build -f Dockerfile.local -t workbuddy2api-hub:0.1.0 .
#        # 逐文件 sha 校验（不一致即中止）

# 3. 改 compose 的 image 行 —— **CI 不管这一处，必须手工改**
sed -i 's|image: workbuddy2api-hub:.*|image: workbuddy2api-hub:0.1.0|' docker-compose.yml

# 4. 切换
docker compose up -d --no-deps --no-build --pull never

# 5. 验收
docker logs --tail 50 wb-proxy
curl -s http://127.0.0.1:8788/health
```

> **最容易踩的坑**：`docker-compose.yml` 的 image 行**没有任何自动化校验**。
> 忘了改 ⇒ 容器继续跑旧镜像，而 CI 全绿、测试全绿，一切看起来都正常。
> 升级后第一件事就是 `curl /health` 看版本。

### 3.3 回滚

```sh
# 改回 compose 的 image 行
sed -i 's|image: workbuddy2api-hub:.*|image: workbuddy2api-hub:0.1.7|' docker-compose.yml
docker compose up -d --no-deps --no-build --pull never
```

镜像 `workbuddy2api-hub:0.1.7` 请**保留**（可回滚点）。
数据回滚用 §3.1 步骤 1 的备份。

---

## 四、升级后的行为变化（会影响你）

### 4.1 `settings.json` 坏了 ⇒ 面板登不上（这是保护，不是故障）

旧行为：配置损坏 ⇒ 面板密码回落 `admin`、API Key 鉴权整体失效（网关对所有人开放）。

新行为：配置损坏 ⇒ **面板登录被拒绝、API Key 鉴权被强制要求**，
坏文件被留档成 `accounts/settings.json.corrupt`（不删、不改）。

**如果升级后面板登不上**：检查 `accounts/settings.json` 能不能解析。
这是保护在生效，不是回归。

### 4.2 `?pwd=` 不再留在地址栏

用它自动登录**不受影响**（口令仍从参数读取），只是地址栏不再留痕。
如果你的脚本依赖「地址栏里还有 pwd」，需要调整。

### 4.3 建议显式设置 `TZ`

本项目有三处「日界」语义不同：

| 位置 | 语义 |
|---|---|
| 签到 / 活跃上报（`wb_accounts`） | 宿主机本地时区 |
| 提示词降级重置（`wb_prompt`） | 固定 +08:00 |
| 价格时段（`wb_pricing`） | UTC |

compose 与 Dockerfile 已经设了 `TZ=Asia/Shanghai`，**裸机启动脚本没有**。
非 CST 的机器上建议显式设置，否则「今天」的概念会漂。

### 4.4 上游恢复时刻被夹到 24 小时以内

上游 429 正文里若写「下个月恢复」，旧实现会把账号停放一个月（等于静默消失）。
新实现夹到 24 小时，到点再试。

### 4.5 夜猫子任务每晚只上报一次

01:00 槽位原本有两条路都上报夜猫子事件（一晚 3~4 次，而上游契约是每天 1 次）。
现在夜猫子由夜间那条路独占。

### 4.6 任务进度等待窗口从 3 秒延长到 20 秒

旧实现实际只等 3.0 秒（代码写着 `range(6)` 但在第 3 次就 break），
而实测落账要 3~4 秒 ⇒ **一半任务当天领不到奖，顺延到明天**。
现在等到进度落账为止（上限 20 秒）。一轮任务会多花几秒，换当天就能领奖。

---

## 五、配置项

### 5.1 本版新增

| 项 | 默认 | 说明 |
|---|---|---|
| （无新增配置项） | — | 本版的改动都是**行为修正**，不需要新配置 |

### 5.2 值得注意的既有配置

| 项 | 默认 | 说明 |
|---|---|---|
| `PANEL_PASSWORD`（`.env`） | `admin` | **务必改**。面板口令，见 §2 |
| `WB_MAX_CONCURRENT_CHAT` | `32` | 全局 chat 并发上限；`auto` = 按就绪账号数自动定容 |
| 账号限额 → 每账号并发上限 | `0`（不限） | 与「在途感知选号」**是两件事**：那个永远开着，这个是硬上限 |
| `WB_FAIL_WINDOW` | `600` | 两次失败间隔超过它就不算「连续」（熔断计数衰减） |
| `WB_MAX_PAYLOAD_BYTES` | 16MB | 单请求体上限 |
| `WB_ENABLE_ANTHROPIC_MESSAGES` | 关 | 开启前请读 §4 的缓存命中率说明 |

### 5.3 数据目录

| 路径 | 内容 | 权限 |
|---|---|---|
| `accounts/settings.json` | 全局设置（面板口令哈希、API Key、限额、排程） | **0600** |
| `accounts/<uid>.json` | 单账号凭据 | **0600** |
| `accounts/` | 目录 | **0700** |
| `usage/usage.jsonl` | 逐请求用量与失败记录 | **0600**（本版收紧） |
| `usage/activity_history.jsonl` | 签到 / 每日活跃历史 | — |
| `usage/` | 目录 | **0700**（本版收紧） |
| `updates/` | 更新状态与备份 | — |

---

## 六、免 Docker 部署

```sh
# Windows：双击 start-wb-proxy.bat（静默后台 + 独立日志窗口）
# macOS/Linux：./start-wb-proxy.sh [端口]
```

需要 **Python 3.9+**（系统 Python 即可，便携包不含 POSIX 运行时）。

局域网共享：`start-wb-proxy-lan.sh` / `.bat`（`--lan` 会**强制要求 API Key**）。

常用参数：

```sh
python wb_proxy.py --host 127.0.0.1 --port 8788 --lan --panel-password '你的口令'
```

| 环境变量 | 说明 |
|---|---|
| `HOST` / `PORT` | 监听地址与端口（默认 127.0.0.1:8788） |
| `API_KEY` | 启动时生成/指定 API Key（写入 `accounts/settings.json`） |
| `TZ` | **建议设置**，见 §4.3 |
| `ACCOUNTS_DIR` / `WB_PROXY_USAGE_DIR` | 账号与用量目录 |
| `WB_MAX_CONCURRENT_CHAT` | 全局并发上限 |
| `WB_ENABLE_ANTHROPIC_MESSAGES=1` | 开启 Anthropic Messages（默认关） |

---

## 七、客户端接入

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="面板里生成的 Key"
```

- **Codex CLI / Claude Code**：走 `/v1/responses`
- **Anthropic SDK**：走 `/v1/messages`（**默认关闭**，需显式开启并接受缓存命中率风险）

一键配置客户端：`GET /agents/cli` 取配置器，或面板「智能体配置」页一键写入（自动备份原件）。

---

## 八、升级前必跑的回归

```sh
cd release
python tests/run_all.py --jobs 4
# 期望：241 passed, 0 failed, 0 skipped
```

**不要跳过这步。** 本版改动了派发逻辑（在途感知、半开探测、错误分类）——
它们直接影响「哪个账号接这个请求」。回归网是唯一能证明「没有把核心功能改坏」的东西。

---

## 九、常见问题

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 面板登不上，提示密码错误 | `settings.json` 损坏 ⇒ 本版**拒绝**默认口令 | 看 `accounts/settings.json.corrupt`；修好配置或从备份恢复 |
| 容器跑的还是旧版本 | compose 的 image 行没改（**CI 不校验这一处**） | `curl /health` 看版本；改 image 行后 `up -d` |
| `/health` 说 `api_key_required: false` | 配置损坏且本版未生效（未重启） | 重启网关 |
| 账号全部显示冷却 | 上游限流或刚恢复（半开态会让路） | 等冷却到期；面板「账号」页有到期倒计时 |
| 客户端偶发 429 | 上游按「账号+分钟」计 token；本版的在途感知会主动分摊 | 调低 `WB_MAX_CONCURRENT_CHAT` 或开「每账号并发上限」 |
| 任务当天没领到奖 | 本版前是进度窗口只有 3 秒；本版已修 | 确认已升级到 0.1.0 |
| 告警发不出去（提示 URL 不合法） | **保护**：URL 指向内网/回环/元数据地址 | 换成你自己的**公网**接收地址 |
| 告警在 DNS 抖动时不发 | **保护**：解析失败一律拒绝（fail closed） | 稍后重试；这是有意的取舍 |

---

## 十、OpenWrt

```sh
# 需要 OpenWrt 25.12.x SDK（glibc x86-64 Linux 宿主）
bash wrt/openwrt/build-ipk.sh     # 无需 SDK
bash wrt/openwrt/build-apk.sh     # 需要 SDK
```

产物：`workbuddy2api-0.1.0-r1.ipk` / `.apk`（架构 all）。

---

## 十一、相关文档

| 文档 | 内容 |
|---|---|
| [`ARCHITECTURE-v0.1.0.md`](ARCHITECTURE-v0.1.0.md) | 技术架构（请求生命周期、协议适配、流式管线、账号池、调度器、控制面、发布工程） |
| [`BUGFIXES-v0.1.0-formal.md`](BUGFIXES-v0.1.0-formal.md) | 本版 BUG 修复清单（含未修项与原因） |
| [`SECURITY-v0.1.0-formal.md`](SECURITY-v0.1.0-formal.md) | 安全审查报告（路由矩阵、出站清单、凭据面、残余风险） |
| [`CHANGELOG-v0.1.0-formal.md`](CHANGELOG-v0.1.0-formal.md) | 功能优化与新增日志 |
| [`USAGE-v0.1.0.md`](USAGE-v0.1.0.md) | 使用说明（面板各页、API、自动化） |
| [`DEPLOY-v0.1.0.md`](DEPLOY-v0.1.0.md) | 完整部署教程（长期维护） |
