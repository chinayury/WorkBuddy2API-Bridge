# v0.1.6 维护手册（243 网关）

> 生成时间：2026-10-27（v0.1.6 上线当天） · 适用：/opt/workbuddy2api-hub 的 WorkBuddy2API-Hub 部署
> 前一版：v0.1.5（见 `MAINTENANCE-v0.1.5.md`）。本版只讲 v0.1.6 新增的东西。

## 一、v0.1.6 改了什么（一句话）

**连坐暂停落盘了，容器重启不再让暂停消失**；顺带修掉两个真 bug（三协议空池状态码不一致、11140 相似 code 误判）。

改动涉及 5 个文件：

| 文件 | 改动 | 行数量级 |
|---|---|---|
| `wb_proxy.py` | 连坐暂停的持久化（load/save/snapshot）；`note_account_restriction` 改用账号哈希 + 落盘；`reset_restriction_state` 连落盘一起清；`restriction_hold_remaining` 加显式关闭语义；`_empty_pool_status()` 三协议共用；11140 code 右边界；`/health` 暴露 `restriction_pause` | ~285 |
| `wb_scheduler.py` | 网页活跃（daily-chat）跳过未验证账号 | ~12 |
| `tests/_test_restriction_persistence.py` | **新增**（17 项） | 新文件 |
| `tests/_test_empty_pool_status.py` | **新增**（8 项） | 新文件 |
| `tests/_test_daily_chat_web_retry_backoff.py` | 修掉一个既有的 flaky 断言 | ~5 |

## 二、为什么「暂停持久化」很重要（先读这段）

区域暂停 = 整池 30 分钟不向上游发任何请求（客户端拿 429）。

**原先它只在内存里**（`wb_proxy.py` 的 `_RESTRICTION_HOLD`）。容器一重启 ——
升级、崩溃、被 OOM kill —— 30 分钟的暂停就没了。

**为什么这会引发第二轮事故**：上游刚把这一区域的账号判非法（403 11140）这个
事实被丢掉 → 池子立刻恢复派发 → 冷账号重新上桌 → 再吃一次 11140 →
900 秒内凑够两次 → 触发**第二轮**整池连坐。

**这不是假想**：2026-10-26 晚生产上的连坐在容器内 **15:33~15:35 连续触发 7 次**，
涉及 `f7102b2e`（10-27 凌晨被拆掉的那颗炸弹）。**同一晚我们部署 v0.1.4 重启了容器。**
那 7 次里只要有任何一次跨过重启点，事故就会重演。

## 三、状态文件

| 项 | 值 |
|---|---|
| 位置 | `/opt/workbuddy2api-hub/usage/restriction-state.json`（容器内 `/app/usage/`） |
| 格式 | `{"schema":1,"holds":{"cn":<epoch>,"intl":<epoch>},"events":{"cn":{"<64hex>":<epoch>}}}` |
| 内容 | **只有元数据**：账号哈希（`sha256(uid)`，不是原始 uid）、事件时刻、原始到期时刻。**不含任何令牌** |
| 权限 | 0600（`O_EXCL` 创建） |
| 写入 | tmp（`path.tmp-<uuid4>`）+ `fsync` + `os.replace` |
| 上限 | 1 MiB / 每区域 4096 条事件 |

**`usage/` 必须是持久卷**才能跨重启存活 —— 243 上已核实（compose 里
`./usage:/app/usage`）。若哪天改成临时卷，这条就失效了。

## 四、怎么用

### 看暂停状态

```sh
curl -s http://127.0.0.1:8788/health | python3 -m json.tool
# "restriction_pause": {
#   "scope": "local_dispatch_pause",
#   "enabled": true,
#   "load_error": false,        # 状态文件读坏了（会记诊断，不发明暂停）
#   "write_failures": 0,        # 写盘失败次数（内存里的暂停仍然生效）
#   "realms": {"cn": {"remaining_seconds": 1234}, "intl": {...}}
# }
```

### 紧急关闭整池暂停

```sh
# 环境变量设为 0 即彻底关闭，重启后依然生效
WB_RESTRICTION_HOLD_SECONDS=0
```

这是**唯一**的「彻底关掉」开关 —— 与它配套的「暂停生效期间不续期」逻辑保证
面板上的「测试账号」按钮每次探测不会把暂停往后推 30 分钟。

### 人工确认「已申诉、可以恢复派发」

面板上有对应操作；它会**清内存并把空状态落盘** —— 否则重启后旧状态又被读回来。

### 排查

| 现象 | 原因 | 怎么查 |
|---|---|---|
| `/health` 的 `load_error: true` | 状态文件坏了 | 删掉 `usage/restriction-state.json` 重启；坏文件只记诊断，不会凭空停池 |
| `write_failures` 一直涨 | usage 目录不可写 | `ls -ld usage/`；**内存里的暂停仍然生效**，只是重启后不保 |
| 重启后暂停没了 | usage 不是持久卷 | `docker inspect wb-proxy` 看挂载 |

## 五、本版修掉的两个真 bug

### 1. 三条协议的空池状态码不一致

| 路径 | 改前 | 改后 |
|---|---|---|
| Chat | 503 ✓ | 503 ✓ |
| Responses | 503 ✓ | 503 ✓ |
| Anthropic Messages | **502** ✗ | 503 ✓ |

Anthropic 路径原来判空池用 `"no account" in low`，而真实抛出的消息是
`"no usable account for realm ..."` —— 子串 `"no account"` 不出现在
`"no usable account"` 里（中间隔着 `"usable "`）。

503 与 502 给客户端的处置动作**完全相反**：503 = 网关没账号了（去加号），
502 = 上游挂了（重试可能有用）。混起来会让客户端一直重试一个根本重试不好的请求。

**账本也一并对齐**：原来 Chat/Responses 路径**账本记 502、响应给 503** ——
同一个事件在两处口径相反。现在账本与响应用同一个码。

### 2. `11140` 的 code 匹配收紧右边界

原正则 `r'"code"\s*:\s*"?11140"?'` 会把 `{"code":111400}` 也判成账号级风控。
**误判的代价**：冷却一个健康账号 + 把它记进连坐事件 —— 一个上游的配额类 code
就能引发整池故障。

现在：`11140(?=["\s,}]|$)`。措辞标记另外要求 403 语境（429/6004 的正文里
也有「已被限制调用频率」）。

## 六、网页活跃现在跳过未验证账号

网页活跃（daily-chat）是**唯一会主动向上游发请求**的路径。v0.1.4 的护栏只挡
**模型请求**路径，冷账号会被 daily-chat 主动探出 403 11140。

现在 `wb_scheduler.py` 选目标时加 `is_unverified()` 闸门，未验证账号直接跳过，
并在调度日志里留一行 `⏭ 账号 [xxxx] 未通过上游验证，跳过网页活跃`。

## 七、这次顺手修掉的既有 flaky test

`_test_daily_chat_web_retry_backoff.py::test_upstream_500_is_a_create_phase_failure`
**本身偶发失败**（5 次跑里红 1 次）。根因：它断言错误正文含 `'14220'`，
但该用例会**真的发 HTTP 请求**，Windows 上连接偶尔被网络栈截断
（`WinError 10054`），正文里就没有 14220 了。

失败仍在 `create` 阶段（发请求本身就在 create 阶段），而用例要验的正是这个分类
⇒ 改为断言 `phase` / `attempts` / `error` 非空，**不再断言错误文本内容**。

**给后续维护者的一条**：断言**错误文本内容**的用例，只要它真的发网络请求，
就会是 flaky。断言**分类与计数**（phase / attempts / 状态码）才稳。

## 八、部署与回滚

```sh
# 部署
bash /tmp/deploy_v016.sh

# 回滚代码
cp -p /opt/workbuddy2api-hub/docker-compose.yml.bak-<TS> \
      /opt/workbuddy2api-hub/docker-compose.yml
cd /opt/workbuddy2api-hub && docker compose up -d --no-deps --no-build --pull never
```

**回滚时的数据注意**：旧版代码**不认识** `usage/restriction-state.json`，
所以它会被静默忽略 —— 留着无害，但要彻底清掉就删该文件。
旧版也不会读它，所以回滚后暂停**回到纯内存**语义。

**回滚点**：
- 代码：单文件 `wb_proxy.py` + `wb_scheduler.py`
- 数据：`accounts-before-deploy-<TS>.tar.gz`、`settings-before-deploy-<TS>.tar.gz`
- 镜像：`workbuddy2api-hub:0.1.5` 留存

## 九、目录索引

| 路径 | 说明 |
|---|---|
| `/opt/workbuddy2api-hub/release/` | 发布树（源码 + `tests/`） |
| `/opt/workbuddy2api-hub/ops-scripts/` | 运维脚本 |
| `/opt/workbuddy2api-hub/backups/` | 回滚点 |
| `/opt/workbuddy2api-hub/accounts/` | 凭据（600）/ 目录（700） |
| `/opt/workbuddy2api-hub/usage/restriction-state.json` | 连坐暂停状态（0600） |
| 本机 `research/watch_243.py` | 只读巡检（唯一权威判据） |
| 本机 `research/可借鉴要点清单.md` | 同类项目借鉴清单（第十节是本次上游/补丁线复核） |
| 本机 `research/MAINTENANCE-v0.1.5.md` | 上一版手册（告警通道） |
