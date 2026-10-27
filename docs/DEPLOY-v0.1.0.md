# WorkBuddy2API-Hub v0.1.0 · 环境部署教程

> 本文所有命令、参数、路径、端口均取自 v0.1.0 的实际代码与部署配置。

---

## 一、这是什么

把**多个**腾讯 WorkBuddy / CodeBuddy 账号变成一个 **OpenAI 兼容接口**：
客户端只连网关，网关负责选号、换号、限流退避、用量记账；顺带把账号的**成长任务中心**自动化跑完。

- **纯标准库**，零第三方依赖（见安全审查报告第二节）
- 单文件前端面板，纯 vanilla HTML/CSS/JS，**零外部 CDN**
- 自带 225 个测试套件 + 3 个补丁层测试

---

## 二、环境要求

| 项 | 要求 |
|---|---|
| Python | **3.9+**（上游 README 声明；官方镜像用 3.11） |
| 操作系统 | Linux / Windows / macOS；OpenWrt 有专门打包 |
| Docker | 可选。有 Docker 推荐用 Docker |
| 依赖 | **无需 pip install** —— 没有第三方依赖 |
| 内存 | 建议 ≥ 512MB（单请求体上限默认 16MB） |
| 端口 | 默认 8788（可改） |

---

## 三、Docker 部署（推荐）

### 3.1 取源码

```sh
git clone https://github.com/ardeyouxipianyi/workbuddy2api-hub.git
cd workbuddy2api-hub
```

或直接用 Release 包（`workbuddy2api-hub-v0.1.0.zip`，含 `release/` 完整发布树）。

### 3.2 配置

```sh
cd release

# 生成一个高强度面板密码（**不要**用默认的 admin）
PANEL_PWD=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
echo "面板密码: $PANEL_PWD"

cat > .env <<EOF
PANEL_PASSWORD=$PANEL_PWD
EOF
chmod 600 .env
```

> 面板密码会写进 `accounts/settings.json`（哈希存储），`--panel-password` 只在**首次启动**生效；
> 之后改密码请在面板里改，或用 `POST /panel/password`。

### 3.3 构建镜像

v0.1.0 的镜像由**官方镜像 + 本地补丁层**构成，不从源码 pip 安装任何东西：

```sh
cd release
docker build -t workbuddy2api-hub:0.1.0 .
```

构建只做几层 `COPY`，**秒级完成**，不需要网络。

> `release/Dockerfile` 用 `FROM ghcr.io/ardeyouxipianyi/workbuddy2api-hub@sha256:1d7d85fa…`
> 按 digest 钉死基础镜像 —— 换成具体 digest 而不是 `:latest`，构建结果才可复现。

#### 3.3.1 v0.1.3 起的推荐形态：**在部署服务器上构建 + 逐文件 sha 硬校验**

补丁层现在是**全部 32+ 个应用模块**（不只是改过的那几个），所以「只 COPY 补丁文件」
的做法已经过时 —— 那样镜像里的其余模块还是旧版，行为不可预期。

推荐流程（**镜像在服务器上构建，不要在本地工作站构建**）：

```sh
# 1. 上传发布包（含 tests/）
pscp workbuddy2api-hub-v0.1.6.tar.gz root@<host>:/tmp/

# 2. 解包到发布树
rm -rf /opt/workbuddy2api-hub/release && mkdir -p /opt/workbuddy2api-hub/release
tar -xzf /tmp/workbuddy2api-hub-v0.1.6.tar.gz -C /opt/workbuddy2api-hub/release

# 3. 在服务器上构建
cd /opt/workbuddy2api-hub/release
docker build -f Dockerfile.local -t workbuddy2api-hub:0.1.6 .

# 4. 逐文件 sha 校验（不一致即中止部署）
for f in $(ls wb_*.py dashboard.html); do
  ctx=$(sha256sum "$f" | awk '{print $1}')
  img=$(docker run --rm --entrypoint sha256sum workbuddy2api-hub:0.1.6 "/app/$f" | awk '{print $1}')
  [ "$ctx" = "$img" ] || { echo "不一致: $f"; exit 1; }
done

# 5. 切 compose 的 image 行并重启
```

`Dockerfile.local` 是 `FROM <官方镜像 digest> + COPY wb_*.py dashboard.html ./`，
**覆盖全部应用模块**（通配符自动包含新增模块，不需要改 Dockerfile）。

> **「33 个文件」怎么来的，别数错**：上面那条 `for` 循环校验的是
> `wb_*.py`（32 个）+ `dashboard.html`（1 个）= **33 个文件**。
> 发布树根目录另有 `console_tail.py`，它是 **Windows 便携包专用**的工具
> （`start-wb-proxy.bat` 用 `pythonw` 静默起服务、再由它开一个日志窗口），
> Linux 容器不需要，因此 **不进镜像、不参与 sha 校验** —— 这是设计如此，不是漏拷。
> 核对方式：`ls wb_*.py | wc -l` 应为 32；容器内 `ls /app/*.py | wc -l` 应同为 32。
> 反过来，`release/` 目录里的 `.py` 总数是 33（含 `console_tail.py`），
> 与「校验 33 个文件」不是一回事，别混着数。

> 为什么不建议在本地构建镜像：构建负载压在工作站上会拖垮它（实测曾导致整机重启、
> 后台任务丢失）。本地只做源码编辑与推送，镜像在部署服务器上构建。

### 3.4 写 compose

```yaml
# release/docker-compose.yml
services:
  wb-proxy:
    image: workbuddy2api-hub:0.1.0
    container_name: wb-proxy
    restart: unless-stopped
    user: "${PUID:-0}:${PGID:-0}"
    ports:
      - "8788:8788"
    environment:
      - HOST=0.0.0.0
      - PORT=8788
      - TZ=Asia/Shanghai
    volumes:
      - ./accounts:/app/accounts
      - ./usage:/app/usage
    command: ["python", "wb_proxy.py", "--host", "0.0.0.0", "--lan", "--panel-password", "${PANEL_PASSWORD}"]
```

三个要点：

| 配置 | 为什么 |
|---|---|
| `--lan` | 监听所有网卡；同时**强制要求 API Key**（不配 `--lan` 时密钥校验是关的，端口一暴露就是裸奔） |
| `--panel-password "${PANEL_PASSWORD}"` | 覆盖默认面板密码 `admin`。值从 `.env` 插值，`.env` 权限 0600 |
| 只挂 `accounts` 与 `usage` | 这两个目录是**运行时数据**（凭据 / 设置 / 用量日志），升级重建容器时绝不能丢 |

### 3.5 启动

```sh
docker compose up -d
docker compose logs -f wb-proxy     # 看启动日志
```

启动日志会打印：

```
    API       : http://<host>:8788/v1
    Dashboard : http://<host>:8788/
    API Key   : wb-xxxx...xxxx       ← v0.1.0 只显示首 4 尾 4
```

> v0.1.0 起横幅**不再打印明文 API Key**（会被容器日志 / journald / CI 收走）。
> 完整 Key 在 `accounts/settings.json` 的 `launcher_key`（权限 0600）。

### 3.6 验收

```sh
curl -s http://127.0.0.1:8788/health
# {"ok": true, "realm": "cn", "accounts": 18, "accounts_ready": 18, "api_key_required": true}

KEY=$(python3 -c "import json;print(json.load(open('accounts/settings.json'))['launcher_key'])")
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8788/v1/models | head -c 200

# 真实对话
curl -s http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"只回复两个字：收到"}]}'
```

---

## 四、免 Docker 部署

```sh
cd release
python3 wb_proxy.py                      # 绑 127.0.0.1:8788，只本机可连
python3 wb_proxy.py --lan                # 监听 0.0.0.0，强制 API Key
python3 wb_proxy.py --host 0.0.0.0 --port 9000 --lan
python3 wb_proxy.py --panel-password '你的面板密码' --lan
python3 wb_proxy.py --import-desktop     # 导入本机已登录客户端的凭据（见 §7）
```

Windows：`start-wb-proxy.bat` / `start-wb-proxy-lan.bat`
macOS/Linux：`./start-wb-proxy.sh` / `./start-wb-proxy-lan.sh`

### 4.1 全部命令行参数

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，默认取环境变量 `HOST`，再默认 `127.0.0.1` |
| `--port` | 监听端口，默认取环境变量 `PORT`，再默认 `8788`。**非数字会直接报一句人话退出**（v0.1.0） |
| `--lan` | 监听所有网卡；**同时强制要求 API Key**。与 `--host 127.0.0.1` 同时给时自动切到 `0.0.0.0` |
| `--api-key` | 要求这个 Bearer Token 才能调 `/v1/*`；默认取环境变量 `API_KEY` 或 `WB_PROXY_KEY` |
| `--panel-password` | 首次启动时设置面板密码 |
| `--accounts-dir` | 凭据目录，默认取环境变量 `ACCOUNTS_DIR` |
| `--usage-dir` | 用量目录 |
| `--import-desktop` | 导入本机已登录客户端的凭据 |
| `--info` | 导入指定的 `*.info` 凭据文件 |
| `--user-agent` | 覆盖出站 UA |
| `--system-prompt` | 覆盖系统提示词 |
| `--probe-only` | 只探测本机环境后退出 |

### 4.2 全部环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8788` | 监听地址与端口 |
| `API_KEY` / `WB_PROXY_KEY` | 空 | 出站 API Key；`--lan` 时为空会自动生成并写入 `accounts/settings.json` |
| `ACCOUNTS_DIR` | `./accounts` | 凭据目录 |
| `TZ` | `Asia/Shanghai` | 时区 |
| `WB_MAX_PAYLOAD_BYTES` | `16777216`（16MB） | 单请求体上限 |
| `WB_MAX_CONCURRENT_CHAT` | `32` | 并发对话上限；设 `auto` 时按就绪账号数自适应 |
| `WB_CHAT_SLOT_WAIT` | `30` | 并发槽位等待秒数 |
| `WB_AFFINITY_MAX_MSGS` | `400` | 亲和路由的消息数上限 |
| `WB_AFFINITY_PAGE_SIZE` | `3` | 超限对话的页内轮转大小 |
| `WB_STATS_TTL` / `WB_REMAINING_TTL` / `WB_COUNT_TTL` | `15` / `15` / `30` | 统计缓存秒数 |
| `WB_TASKS_CACHE_TTL` | `20` | 任务清单缓存秒数 |
| `WB_TRUSTED_PROXIES` | 空 | 逗号分隔的可信反代 IP/CIDR。**只在有反代时才填** |
| `WB_PROXY_DEFAULT_REALM` | `intl` | 默认出口（`cn` / `intl`） |
| `WB_PROXY_CATALOG_DIR` | `./catalog` | 模型目录缓存目录 |
| `WB_PROXY_USAGE_DIR` | `./usage` | 用量目录 |
| `WB_MAX_TOOL_DEFINITION_BYTES` | `65536` | 超过则压扁该工具定义（Kimi 11133 防护） |
| `WB_ENABLE_ANTHROPIC_MESSAGES` | `0` | 开启 Anthropic Messages 端点（**默认关**，见 §6.3） |
| `WB_LOCAL_WEB_TOOLS` | `0` | 由网关代跑客户端声明的 `web_search` / `web_fetch`（**默认关**） |
| `WB_DESKTOP_CHAT_COMPAT` | `0` | 桌面 chat 请求头一致性实验开关（**默认关**，需重启） |
| `WB_OPS_*` | — | 运维补丁层的开关（循环守卫、混合停滞、观测文件等），详见 `docs/PATCHES.md` |
| `WB_RESTRICTION_HOLD_SECONDS` | `1800` | 上游风控连坐时**整池暂停**的秒数。**设为 0 即彻底关闭这个保护**（重启后依然生效） |
| `WB_RESTRICTION_WAVE_MIN` | `2` | 多少个**不同**账号在窗口内被 11140 限制才算触发整池暂停 |
| `WB_RESTRICTION_WAVE_WINDOW` | `900` | 上面的判定窗口（秒） |
| `WB_RESTRICTED_403_ROTATE_MAX` | `0` | 账号被 11140 后，同一条请求最多再换几个号重试。**默认 0 = 不换**（社区复盘的红线：解封前严禁换号继续打同一台设备） |

> **所有整型/浮点型环境变量都做了容错**：值写错（如 `16m`）会回退默认值并继续启动，
> 不会让整个网关在 import 阶段崩掉（v0.1.0 起）。

---

## 五、数据目录

| 路径 | 内容 | 备份必要性 |
|---|---|---|
| `accounts/` | 凭据 JSON、`settings.json`（面板密码哈希、API Key、各类设置）、`accounts/` 下的客户端凭据 | **必须** |
| `usage/` | 用量日志 `usage.jsonl`、活动历史 `activity_history.jsonl`、SQLite 日志库（可选）、模型目录缓存、**连坐状态 `restriction-state.json`（v0.1.6 起）** | 建议 |

```sh
chmod 700 accounts usage
chmod 600 accounts/*.json .env
```

### 5.1 v0.1.6 新增：`usage/restriction-state.json`

上游风控连坐（整池暂停 30 分钟）的状态文件。**它必须落在持久卷里** ——
`usage/` 若不是持久目录，容器重启后暂停就丢了，池子会立刻恢复派发，
冷账号重新上桌可能触发**第二轮**连坐。

| 项 | 值 |
|---|---|
| 内容 | 只有元数据：账号哈希（`sha256(uid)`，**不是**原始 uid）、事件时刻、原始到期时刻。**不含任何令牌** |
| 权限 | 0600（`O_EXCL` 创建） |
| 写入 | tmp（`path.tmp-<uuid4>`）+ `fsync` + `os.replace` |
| 上限 | 1 MiB / 每区域 4096 条事件 |

**回滚到旧版时**：旧代码不认识这个文件，会静默忽略 —— 留着无害；要彻底清掉就删它。
删掉意味着**放弃当前尚未到期的暂停**，重启后池子会恢复派发。

### 5.2 v0.1.5 新增：告警配置（在 `accounts/settings.json` 里）

```json
{
  "alerts": {
    "webhook": {"url": "https://example.com/hook", "secret": "..."},
    "serverchan": {"key": "SCT..."},
    "thresholds": {"min_ready": 8, "for_ticks": 2, "clear_ticks": 2}
  }
}
```

`secret` 与 ServerChan key **只写不读**：面板回读只有「配没配」两个布尔值，
输入框留空即「保持原值」。`settings.json` 权限 0600（与 API Key 同一把锁）。

**不配就完全不发请求** —— 默认关闭，`wb_alerts` 一个网络请求都不发。

> ⚠️ **webhook URL 会让网关主动向该地址发 HTTP 请求**。请只填你自己控制的地址：
> 填成 `http://127.0.0.1:<内网端口>/` 或云元数据地址等于让网关替你去访问内网。

---

## 六、客户端接入

### 6.1 OpenAI 兼容

```
Base URL : http://<host>:8788/v1
API Key  : accounts/settings.json 的 launcher_key（或面板里创建的 Key）
Model    : 见 /v1/models
```

### 6.2 Codex / Claude Code 等

面板「设置 → 一键配置」会写好客户端配置文件。**注意**：一键配置的 `base_url` 必须指向网关自己的地址 ——
不要填成外部主机（见安全审查报告 S-14）。

### 6.3 Anthropic Messages（默认关闭）

`/v1/messages` 与 `/v1/messages/count_tokens` **默认返回 400**（`WB_ENABLE_ANTHROPIC_MESSAGES` 未开）。
原因是转换层不透传 `cache_control`，打开可能白烧积分。确需 Claude Code 直连时：

```sh
WB_ENABLE_ANTHROPIC_MESSAGES=1
```

> 客户端第一个请求往往就是 `count_tokens`，会直接拿到 400 —— 这是预期行为，不是故障。

---

## 七、导入本机已登录的客户端凭据

```sh
python3 wb_proxy.py --import-desktop
```

Windows 上扫描 `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`；
macOS 上扫描 `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/`。

也支持直接指定文件：`python3 wb_proxy.py --info /path/to/xxx.info`。

> 面板的「本机桌面客户端账号」按钮做的是同一件事（只读检测 + 显式导入）。

---

## 八、升级

### 8.1 升级到新版本

```sh
cd release
git pull                                  # 取新代码
docker build -t workbuddy2api-hub:0.2.0 . # 重新构建
# 改 compose 的 image 行
docker compose up -d --no-deps --no-build --pull never
```

**不要**在 compose 里写 `:latest` + `docker compose pull`：那会把基础镜像换成未知的版本，
而本地补丁是按某个确定的 digest rebase 的 —— 两者对不上时行为不可预期。

### 8.2 回滚

```sh
docker tag workbuddy2api-hub:0.1.0 workbuddy2api-hub:rollback   # 先给当前版本留个标签
# 改回 compose 里的 image 行
docker compose up -d --no-deps --no-build --pull never
```

更稳的做法是升级前先给容器拍个快照：

```sh
docker commit wb-proxy workbuddy2api-hub:rollback-$(date +%Y%m%d-%H%M%S)
docker image save -o rollback.tar workbuddy2api-hub:rollback-$(date +%Y%m%d-%H%M%S)
```

---

## 九、升级前必跑的回归

```sh
cd release
python tests/run_all.py --jobs 4 --logs suite-logs
```

改动补丁层后还要跑本项目的三个本地测试（**每个都带控制组**）：

```sh
python deploy/test_task_patch.py                        # 任务引擎
python deploy/test_log_mask.py                          # 凭据脱敏
node   deploy/test_multifile_import.js                  # 面板多选导入
python deploy/test_task_patch.py _src/workbuddy2api-hub-main   # 控制组：必须 exit 1
```

---

## 十、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即 `ImportError` / `ValueError` | 某个环境变量写了非法值 | v0.1.0 起这类值会自动回退默认；若仍崩，看启动日志第一行 |
| 面板 401 | 密码不对 / 容器重启后会话失效 | 面板会话只在内存，重启需重新登录；密码用面板「设置」改 |
| `/v1/models` 返回 401 | 没带 API Key，或 `--lan` 未启用时 Key 校验关闭 | 带 `Authorization: Bearer <launcher_key>` |
| 请求返回「无可用账号」 | 账号全在冷却 / 被风控限制 / 出口判定不匹配 | 面板「账号」页看每个号的 `inCooldown` / `restrictedFor` / `lastError` |
| 任务一直「进行中」 | 该任务是 `unforgeable`（需真人操作，如关注公众号满 24h） | v0.1.0 起这类行显示「需人工」并写明原因 |
| 面板显示的版本是 `1.7.33` | 跑的还是旧镜像 | v0.1.0 应显示 `0.1.0`；`docker compose up -d --no-build --pull never` 切到新镜像 |
| 容器起来但端口不通 | 反代覆盖了 `X-Real-IP` 或未监听 `0.0.0.0` | `--lan` 会自动切到 `0.0.0.0`；检查 `docker port wb-proxy` |
| **客户端一直拿 429，重启后又好了** | 上游风控连坐：整池暂停 30 分钟 | v0.1.6 起暂停**落盘**（`usage/restriction-state.json`），重启不丢。查 `/health` 的 `restriction_pause.remaining_seconds` |
| **面板上某个号标着「未验证」，不参与派发** | 该凭据从未被上游接受过（`credits.used == 0`） | 点该号的「测试」验证一次：通过（上游回 14003）即自动放行；被上游判非法（403/11140）则需先去官方渠道解封 |
| **冷账号被重新启用后整池连坐** | v0.1.4 之前没有入池闸门，启用即派发 | v0.1.4 起未验证账号不参与派发；v0.1.6 起暂停跨重启存活。详见 `CHANGELOG-v0.1.4.md` / `CHANGELOG-v0.1.6.md` |
| **告警收不到** | 没配通道 / 阈值没到 / 去抖没满 | 面板「设置 → 账号池告警」看「已开启」状态与投递统计；点「发送测试通知」自测 |

---

## 十一、OpenWrt

`wrt/openwrt/workbuddy2api/` 下有 OpenWrt 包定义（`PKG_VERSION` 已同步为 `0.1.6`）。

---

## 十二、相关文档

| 文档 | 内容 |
|---|---|
| [README-v0.1.0.md](README-v0.1.0.md) | 项目介绍、架构、模块地图、能力边界 |
| [USAGE-v0.1.0.md](USAGE-v0.1.0.md) | 面板与 API 的功能使用说明 |
| [BUGFIXES-v0.1.0.md](BUGFIXES-v0.1.0.md) | 全部 BUG 的症状/触发条件/根因/改法，以及审计发现但未修的排队清单 |
| [CHANGELOG-v0.1.0.md](CHANGELOG-v0.1.0.md) | 功能优化与新增日志、兼容性变化、版本路线图 |
| [CHANGELOG-v0.1.1.md](CHANGELOG-v0.1.1.md) … [CHANGELOG-v0.1.6.md](CHANGELOG-v0.1.6.md) | 各增量版本的变更记录（流式工具调用修复 / 上游合并 / 未验证护栏 / 告警通道 / 暂停持久化） |
| [SECURITY-v0.1.0.md](SECURITY-v0.1.0.md) | 安全审查报告：供应链、已修复安全问题、残余风险、渗透测试记录、加固清单 |
