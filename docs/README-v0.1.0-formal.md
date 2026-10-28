# WorkBuddy2API-Bridge · 项目介绍（v0.1.0 正式版）

> 版本 **0.1.0** · 2026-10-28 · MIT License
> 上游：[`ardeyouxipianyi/workbuddy2api-hub`](https://github.com/ardeyouxipianyi/workbuddy2api-hub)（v1.8.0）

---

## 一、它解决什么问题

**WorkBuddy / codebuddy.ai 是一套好用的 AI 编程客户端，但它没有 API。**

于是很多人自己搭了一个反向代理：拿客户端的登录态，换成一个 OpenAI 兼容的地址，
让 Claude Code / Codex CLI / 任何支持自定义 API 的工具都能接上。

**这个项目就是那个反向代理，但做得比较细**：

| 你想要 | 这里怎么做 |
|---|---|
| 一个 OpenAI 兼容地址 | `/v1/chat/completions`、`/v1/responses`（Codex / Claude Code）、`/v1/messages`（Anthropic，可选） |
| 多个账号轮流用 | 账号池 + 权重 + 会话亲和 + **在途感知**（并发请求自动分摊） |
| 账号别被上游封 | 设备指纹隔离、凭据三态预验证（坏号进池前就被拦下） |
| 每天自动领积分 | 后台调度器（签到 / 猫猫旅行 / 成长任务 / Token 保活） |
| 知道池子在不在健康状态 | 告警通道（HMAC 签名 webhook + ServerChan） |
| 知道花了多少钱 | OpenRouter 等价 token 估算 + 按 Key / 账号的用量透视 |
| 出问题时能查 | 结构化活动历史 + 逐请求用量日志 + 面板运行日志 |
| 不装一堆依赖 | **零第三方依赖**（标准库就够），绿色包自带 Python 运行时 |
| 换个地方部署 | Docker / 裸机 / OpenWrt（ipk + apk） |

---

## 二、它不是什么

诚实说明能力边界（详见 [`SECURITY-v0.1.0-formal.md`](SECURITY-v0.1.0-formal.md)）：

- **不是官方产品**，不隶属于腾讯 / WorkBuddy；
- **不保证上游不封号** —— 用非官方客户端的风险自负；
- **不支持「用满额度」的玩法** —— 它是把你已有的账号接出来，不是刷量工具；
- **部分任务无法自动化** —— 需要真人动作的（公众号关注、捐款）会诚实跳过；
- **Anthropic Messages 适配默认关闭** —— 上游对 `cache_control` 的缓存命中率只有约 11%，
  长会话单次消耗可能超过 100 积分。与其假装兼容，不如默认关掉。

---

## 三、技术特点

### 3.1 零第三方依赖

AST 全量扫描 226 个 `.py`：运行时 32 个模块**只 import 标准库**。
没有 `requirements.txt` / `pyproject.toml` / `Pipfile` / `poetry.lock`。
CI 里也没有任何 `pip install`。

⇒ **没有可被投毒的依赖链。** 绿色包自带 Python 运行时，拷过去就能跑。

### 3.2 三协议，一套内核

| 客户端 | 接法 |
|---|---|
| OpenAI SDK / 通用工具 | `POST /v1/chat/completions` |
| Codex CLI / Claude Code | `POST /v1/responses` |
| Anthropic SDK | `POST /v1/messages`（默认关闭，需显式开启） |

三条路最终**收敛到同一次上游 Chat Completions 调用** ——
上游侧的行为、护栏、计费对三条协议完全一致。

### 3.3 账号池不是简单的轮询

```
手动权重  → 临期积分  → 剩余用量  → 纯轮询
   ↑ 每一层内部都按「在途数」分组，最闲的组先被选
```

- **在途感知**：池子的偏好若全是静态属性，并发请求会自发收敛到同一个账号上。
  而上游按「账号 + 分钟」计 token，两个会话落在同一账号上很容易冲破配额。
  网关看**当前谁在忙**，把请求分摊开。
- **未验证护栏**：从未被上游消费过的凭据，第一次派发就会被判 403/11140，
  而 900 秒内两次就触发整池连坐。护栏在**派发之前**就把它隔离。
  判据是 `credits.used == 0`（生产实测分离度 4/4 命中、0/14 误判）。
- **半开探测**：冷却刚到期的账号只放一个请求去试探，
  避免「冷却到期 → 全员压上 → 再撞 → 再冷却」的锯齿。

### 3.4 流式工具调用的形状被钉住了

上游把一次工具调用拆成多帧：头帧带 `id`/`name`，参数按字符切成后续帧，
后续帧的 `function.name` 是**空串**。

两个真实事故：

- **参数被网关吃掉** —— 「空 name 就是占位噪音」的过滤把承载参数的帧整帧删掉
  ⇒ agent 每次都报「缺少必选参数」；
- **函数名被拼成一串** —— 把空名字「回填」成真名后每一帧都带 name
  ⇒ 按 name 累加的客户端（WorkBuddy CLI）得到 `TaskCreateTaskCreate…`
  ⇒ `Tool Not Found`，整轮 agent 失败、界面卡在「思考中」。

⇒ 唯一两边都对的形状是 **OpenAI 原生流式的样子：头帧带 name，续帧整个不带 name 键**。

v0.1.0 把真实上游帧**固化成文件**（`tests/fixtures/upstream_sse/`），
回放测试守住这个形状 —— 上游改形状时，红灯会亮。

---

## 四、代码规模（实测）

| 项 | 数量 |
|---|---|
| 应用模块 | **32** 个 `wb_*.py` + `console_tail.py` |
| 最大单文件 | `wb_proxy.py` ≈ 24,000 行 |
| 测试套件 | **241**（182 Python + 59 JS） |
| 便携包托管文件 | **62** 条 |
| 看板 | `dashboard.html` ≈ 1.0 MB / 18,000 行，原生 JS + 内联 CSS，零外部资源 |
| 第三方依赖 | **0** |

---

## 五、快速开始

```sh
git clone https://github.com/chinayury/workbuddy2api-hub.git
cd workbuddy2api-hub/release

# 面板密码：生成随机强口令，不要用默认的 admin
PANEL_PWD=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
echo "面板密码: $PANEL_PWD"
printf 'PANEL_PASSWORD=%s\n' "$PANEL_PWD" > .env
chmod 600 .env

# 构建并启动
docker build -f Dockerfile.local -t workbuddy2api-hub:0.1.0 .
docker compose up -d

# 验收
curl -s http://127.0.0.1:8788/health
```

面板：<http://127.0.0.1:8788/>

客户端：

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="面板里生成的 Key"
```

免 Docker：`start-wb-proxy.bat`（Windows）/ `./start-wb-proxy.sh`（macOS/Linux），
需要 Python 3.9+。

---

## 六、文档

| 文档 | 内容 |
|---|---|
| [`ARCHITECTURE-v0.1.0.md`](ARCHITECTURE-v0.1.0.md) | **技术架构** —— 请求生命周期、协议适配、流式管线、账号池、调度器、控制面、发布工程；每条结论锚定真实行号 |
| [`CHANGELOG-v0.1.0-formal.md`](CHANGELOG-v0.1.0-formal.md) | 功能优化与新增日志 |
| [`BUGFIXES-v0.1.0-formal.md`](BUGFIXES-v0.1.0-formal.md) | BUG 修复清单（含未修项与原因） |
| [`SECURITY-v0.1.0-formal.md`](SECURITY-v0.1.0-formal.md) | 安全审查报告（路由矩阵、出站清单、凭据面、残余风险） |
| [`DEPLOY-v0.1.0-formal.md`](DEPLOY-v0.1.0-formal.md) | 部署与升级指南 |
| [`USAGE-v0.1.0-formal.md`](USAGE-v0.1.0-formal.md) | 使用说明 |
| [`DEPLOY-v0.1.0.md`](DEPLOY-v0.1.0.md) · [`USAGE-v0.1.0.md`](USAGE-v0.1.0.md) | 完整部署教程 / 使用说明（长期维护） |
| `research/可借鉴要点清单-v0.1.0.md` | 同类项目借鉴过程（148 候选 → 18 条借鉴点） |

---

## 七、与上游的关系

- **上游**：[`ardeyouxipianyi/workbuddy2api-hub`](https://github.com/ardeyouxipianyi/workbuddy2api-hub)（MIT）
  —— 本仓库的基线代码整体来自它；上游 `main` 分支原样保留在 fork 内，可随时比对与同步。
- **本项目**使用自有版本线；`origin` = fork，`upstream` = 官方。
- **同步上游**：`git fetch upstream && git merge upstream/main`

**本 fork 的增量**（相对上游 v1.8.0）：

| 方向 | 内容 |
|---|---|
| 协议正确性 | 流式工具调用的参数与函数名修复（两次线上事故）、跨协议推理凭据处理 |
| 账号池治理 | 未验证凭据入池闸门、区域暂停持久化、在途感知选号、半开探测、错误分类 |
| 安全 | 告警通道 SSRF 五道闸门、凭据落盘脱敏、配置损坏 fail-closed、更新包体积限制 |
| 工程 | 金丝雀 SSE 固件、241 套件回归网、便携运行时裁剪与契约校验 |

---

## 八、许可与来源

- 本项目 MIT（见 `LICENSE`）。
- 基线代码来自上游 MIT 项目，保留其版权声明与许可声明。
- **本项目不授予任何上游（CodeBuddy / WorkBuddy）接口或服务的权利**；
  使用者需自行遵守上游服务条款。
- 本项目仅供协议学习与个人自用；请优先使用官方客户端，不使用主账号，不共享账号，
  不用于商业牟利。

---

## 九、致谢

- 上游 [`ardeyouxipianyi/workbuddy2api-hub`](https://github.com/ardeyouxipianyi/workbuddy2api-hub)
- [`linguo2625469/workbuddy2api-panel`](https://github.com/linguo2625469/workbuddy2api-panel) 与
  [`misakano7545/workbuddy2api-panel`](https://github.com/misakano7545/workbuddy2api-panel)（桌面端指纹与事件链）
- [`CangShui/workbuddy-gateway`](https://github.com/CangShui/workbuddy-gateway)（Go 实现参考）
- [`HanawaBanana/workbuddy2api`](https://github.com/HanawaBanana/workbuddy2api)（Python 原生任务自动化）
- [`RobbsLuo/Coding2API`](https://github.com/RobbsLuo/Coding2API)（健康度调度与错误分类）
- [`seaavey/SRouter`](https://github.com/seaavey/SRouter)（配额预占与结构化请求日志）
- [`XDTrees/dsh-xdpool`](https://github.com/XDTrees/dsh-xdpool)（声明式 provider 注册表）
- [`wushuai0802/workbuddy2api-hub-ops-patch`](https://github.com/wushuai0802/workbuddy2api-hub-ops-patch)（运维补丁线）
