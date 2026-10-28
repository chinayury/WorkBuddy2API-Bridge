# -*- coding: utf-8 -*-
"""wb_alerts.py —— 账号池可用性告警（webhook / ServerChan）

为什么要有这个模块
------------------
2026-10-26 晚到 10-27 凌晨，生产上出了两次「几乎出事」的事故：
面板上有人（或某个流程）把**从未被上游接受过**的凭据重新启用，
它们随时可能被派发到、触发 403 11140，而 900 秒内两次就触发整池 30 分钟
连坐暂停（客户端全拿 429）。

两次都**不是靠网关发现的** —— 是靠人写的一次性脚本才看见的。
这正是本模块要补的洞：**账号池可用性跌到阈值以下时，网关自己说一声**。

借鉴
----
* `misakano7545/workbuddy2api-panel` `internal/alert/alert.go`：
  阈值型告警 + `defaultForTicks=2` / `defaultClearTicks=2` 去抖 +
  HMAC-SHA256 签名（`X-WB2A-Signature` 头）。
* gitee `li-luoqiang/workbuddy-checkin`：ServerChan 推送
  （`https://sctapi.ftqq.com/<key>.send`）。

三条设计约束
------------
1. **默认全关**。没有配置 webhook / ServerChan 时，本模块不做任何网络请求。
2. **不阻塞请求线程**。告警走后台守护线程 + 有界队列；队列满就丢并计数。
   告警失败绝不能拖慢或搞挂业务请求。
3. **去抖**：同一条件连续 `for_ticks` 轮命中才发一次「故障」，
   连续 `clear_ticks` 轮不命中才发一次「恢复」。默认 2/2 ——
   实测单次抖动很常见，1/1 会变成噪音。

只依赖标准库。Python 3.9 兼容。
"""

import hashlib
import hmac
import ipaddress
import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

#: 告警签名头（与 misakano 版一致，便于对端验签）
SIGNATURE_HEADER = "X-WB2A-Signature"
#: 告警事件类型
EV_POOL_DEGRADED = "pool_degraded"
EV_POOL_RECOVERED = "pool_recovered"
EV_RESTRICTION_WAVE = "restriction_wave"
EV_UNVERIFIED_REARMED = "unverified_rearmed"

#: 后台队列上限。满了就丢 —— 告警是「尽力而为」，绝不能反压业务。
QUEUE_MAX = 200
#: 单次 HTTP 请求超时（秒）
HTTP_TIMEOUT = 8
#: 后台线程轮询间隔（秒）
WORKER_INTERVAL = 1.0

_lock = threading.RLock()
_queue = queue.Queue(maxsize=QUEUE_MAX)
#: 已投递但因队列满被丢弃的条数（面板上可见）
_dropped = 0
#: 已成功投递的条数
_sent = 0
#: 投递失败次数
_failed = 0
#: 去抖状态：key -> {"ticks": int, "active": bool}
_debounce = {}
_worker = None
_stop = threading.Event()
#: 最近一次投递结果，供面板查看
_last_result = {"at": 0.0, "ok": False, "error": ""}


# --------------------------------------------------------------- 配置读取

def config(accounts_dir):
    """读告警配置。

    形状（全部可选，缺省即关闭）::

        {
          "webhook": {"url": "https://...", "secret": "..."},
          "serverchan": {"key": "SCT..."},
          "thresholds": {"min_ready": 8, "for_ticks": 2, "clear_ticks": 2}
        }
    """
    try:
        import wb_settings
        data = wb_settings.load(accounts_dir) or {}
    except Exception:
        data = {}
    raw = data.get("alerts")
    if not isinstance(raw, dict):
        return {}
    out = {}
    hook = raw.get("webhook")
    if isinstance(hook, dict) and str(hook.get("url") or "").strip():
        out["webhook"] = {
            "url": str(hook.get("url")).strip(),
            "secret": str(hook.get("secret") or ""),
        }
    sc = raw.get("serverchan")
    if isinstance(sc, dict) and str(sc.get("key") or "").strip():
        out["serverchan"] = {"key": str(sc.get("key")).strip()}
    th = raw.get("thresholds")
    if isinstance(th, dict):
        out["thresholds"] = {
            "min_ready": _int(th.get("min_ready"), 8),
            "for_ticks": _int(th.get("for_ticks"), 2),
            "clear_ticks": _int(th.get("clear_ticks"), 2),
        }
    return out


def _int(value, fallback):
    try:
        if isinstance(value, bool):
            return fallback
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    return out if out > 0 else fallback


# --------------------------------------------------------------- 去抖

def _debounce_step(key, hit, for_ticks, clear_ticks):
    """推进一次去抖状态；返回要发的事件名（None = 本轮不发）。

    `hit` 为 True 表示「条件仍然成立」。连续 for_ticks 轮成立才发故障，
    连续 clear_ticks 轮不成立才发恢复。中间状态不发任何东西。

    **命中与未命中各用独立的计数器**。共用一个计数器的写法会在
    「命中-未命中-命中」这种抖动下把计数推过阈值，于是单次抖动就发出一次
    故障通知 —— 告警一旦变成噪音，运维就会开始无视它，那等于没有告警。
    独立的计数器让「连续」这个词名副其实：中间只要断一次，计数就归零。

    这是单测 `test_flicker_never_fires` 抓出来的真实缺陷。
    """
    with _lock:
        state = _debounce.get(key)
        if state is None:
            state = {"hit_ticks": 0, "miss_ticks": 0, "active": False}
            _debounce[key] = state
        if hit:
            state["miss_ticks"] = 0        # 条件又成立了，之前的「未命中」作废
            state["hit_ticks"] += 1
            if not state["active"] and state["hit_ticks"] >= for_ticks:
                state["active"] = True
                state["hit_ticks"] = 0
                return "fault"
            return None
        state["hit_ticks"] = 0            # 条件断了，之前的「命中」作废
        state["miss_ticks"] += 1
        if state["active"] and state["miss_ticks"] >= clear_ticks:
            state["active"] = False
            state["miss_ticks"] = 0
            return "clear"
        return None


# --------------------------------------------------------------- 投递

def _sign(secret, body):
    """HMAC-SHA256 签名（hex）。没有 secret 时不签名。"""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _post(url, body, headers=None, timeout=HTTP_TIMEOUT):
    """向 webhook 发一次 POST。

    **只允许 http/https**，且**不回显响应体**：这条 URL 由面板用户填，网关会
    真的去请求它。不限制 scheme 就能用 `file://` 读本地文件；把响应体带回来
    就成了「让网关代读内网资源」的通道（SSRF 读）。详见 `_assert_webhook_url`。

    **不跟随跳转**：默认 opener 带 `HTTPRedirectHandler`，一个公网 URL 用 302
    跳到 `http://127.0.0.1:8788/` 就能绕过字符串级的地址检查。
    """
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=headers or {})
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        # 只读状态码与长度，不读正文 —— 正文可能正是内网服务的敏感内容。
        try:
            resp.read(1)
        except Exception:
            pass
        return b""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝一切跳转：跳转目标可能是内网地址。

    字符串级的地址检查挡不住「先给一个公网 URL，让它 302 到回环/私网」。
    宁可让对端返回 3xx 自己处理，也不要替它跳。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: 允许的 webhook scheme。file:// 能读本地文件，ftp:// 之类能触发别的协议栈，
#: 都没有理由出现在「接收告警的 HTTP 地址」这个场景里。
_ALLOWED_WEBHOOK_SCHEMES = ("http", "https")

#: 单个 webhook URL 的长度上限（正常地址 < 1KB）。
_MAX_WEBHOOK_URL_LEN = 2048


def _assert_webhook_url(url):
    """校验 webhook URL；不合法就抛 ValueError。

    拦的是**明显不是「外部告警接收端」的地址**：

    * scheme 必须是 http/https（`file://` 能读本地文件）；
    * host 不能是回环 / 链路本地 / 私有段地址 —— 这些是网关**自己**或
      **它的内网邻居**，让网关去请求它们等于把它变成内网探测器
      （`127.0.0.1` 面板口、`169.254.169.254` 云元数据、`10.x` 内网服务）。

    这不是「防住所有 SSRF」—— DNS 重绑定与「公网 IP 解析到内网」都超出静态
    判定的能力。目的是**拦住手滑和明显恶意**的那一大类，而不是声称万无一失。
    真正的强隔离需要出站代理或网络策略，那超出本模块的职责。
    """
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("webhook URL 为空")
    if len(raw) > _MAX_WEBHOOK_URL_LEN:
        raise ValueError("webhook URL 过长（上限 %d 字符）" % _MAX_WEBHOOK_URL_LEN)
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme.lower() not in _ALLOWED_WEBHOOK_SCHEMES:
        raise ValueError("webhook URL 只允许 http/https（收到 %r）" % parts.scheme)
    if parts.username or parts.password:
        # URL 里带 userinfo：既是凭据泄露（会进日志），也是绕过主机判定的写法
        # （`http://127.0.0.1@evil.com/` 里真正的主机是 evil.com，
        #  但 `http://evil.com@127.0.0.1/` 里真正的主机是 127.0.0.1）。
        raise ValueError("webhook URL 不得携带 userinfo（user:pass@）")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValueError("webhook URL 缺少主机名")
    if _is_internal_host(host):
        raise ValueError("webhook URL 指向内网/回环地址（%s）—— "
                         "网关不应代为访问自己的内网" % host)
    return raw


#: 额外要拦的网段。`ipaddress.is_private` **不含** CGNAT（100.64.0.0/10）
#: 与部分保留段 —— 那些地址在运营商网络里是「内网」，指过去同样是探测内网。
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT（运营商内网）
    ipaddress.ip_network("192.0.0.0/24"),    # IETF 协议分配
    ipaddress.ip_network("198.18.0.0/15"),   # 基准测试
)


def _is_blocked_ip(ip):
    """True when the address is one the gateway must not reach."""
    if (ip.is_loopback or ip.is_link_local or ip.is_private
            or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
        return True
    return any(ip in net for net in _EXTRA_BLOCKED_NETWORKS)


def _is_internal_host(host):
    """True when the host names the gateway itself or a private network.

    **域名也要解析后判**：只做字符串检查的话，`evil.example` 解析到
    `127.0.0.1` 就能通过 —— 那是同一件事换个写法。解析时**所有**地址都必须
    合法（多值 DNS：只查第一个 A 记录会被绕过）。
    """
    if host in ("localhost", "localhost.localdomain", "ip6-localhost"):
        return True
    if host == "::1" or host == "0:0:0:0:0:0:0:1":
        return True
    # .local / .internal 之类是内网名字
    if host.endswith((".local", ".internal", ".localdomain")):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None
    if addr is not None:
        return _is_blocked_ip(addr)
    # 是个域名：解析后逐个判 —— 有一个指向内网就整体拒绝。
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        # 解析不了 ⇒ **拒绝**（fail closed）。
        #
        # 旧实现在这里 return False（不阻断），理由是「DNS 抖动不该让告警彻底
        # 不能用」。但这把守与连接是**两次独立的解析**：一个域名可以第一次
        # 解析失败、第二次解析到 127.0.0.1 —— 校验通过、连接打内网。
        # 也就是说 fail-open 让这道闸门可以被绕过，而它挡的正是「网关代为
        # 访问内网」这件事。
        #
        # 代价只是「DNS 抖一下时告警发不出去」—— 那是**安全的一侧**：
        # 收不到告警比被 SSRF 打穿好得多。同仓库的 wb_webtools._guard_url
        # 本来就是 fail closed 的（:256-259），两处策略现在一致了。
        return True
    for info in infos:
        raw = str(info[4][0]).split("%")[0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            return True     # 解析出看不懂的地址 → 按不安全处理
        if _is_blocked_ip(ip):
            return True
    return False


def _safe_error_text(exc):
    """把投递异常压成**不含响应体**的一行短文本。

    urllib 的 HTTPError / URLError 在 `str(exc)` 里会带上**响��正文** ——
    直接回显给面板就等于把内网服务的返回内容泄露出去（SSRF 读）。
    这里只保留异常类型与状态码，正文一律丢弃。
    """
    status = getattr(exc, "code", None)
    if status is not None:
        return "HTTP %s" % status
    reason = getattr(exc, "reason", None)
    if reason:
        return str(reason)[:120]
    return type(exc).__name__


def _deliver(cfg, event, payload):
    """把一条告警投到所有已配置的通道。返回 (ok, error)。"""
    body = json.dumps({
        "event": event,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": os.uname().nodename if hasattr(os, "uname") else "",
        "payload": payload,
    }, ensure_ascii=False).encode("utf-8")

    errors = []
    hook = cfg.get("webhook")
    if hook:
        headers = {"Content-Type": "application/json"}
        sig = _sign(hook.get("secret") or "", body)
        if sig:
            headers[SIGNATURE_HEADER] = sig
        try:
            # 先校验 URL 再发 —— 面板用户可能填了内网地址（手滑或恶意）。
            url = _assert_webhook_url(hook.get("url"))
            _post(url, body, headers)
        except ValueError as exc:
            errors.append("webhook: %s" % exc)
        except Exception as exc:
            # 错误文本里可能带响应体 —— 用安全版本，不回显正文。
            errors.append("webhook: %s" % _safe_error_text(exc))

    sc = cfg.get("serverchan")
    if sc:
        # ServerChan 走 GET，参数放在 query 里。key 会被 quote 成路径段，
        # 所以 key 里塞 `../` 或 `@` 改不了 host。
        title = "WorkBuddy2API-Hub 告警: %s" % event
        text = json.dumps(payload, ensure_ascii=False)[:1800]
        params = urllib.parse.urlencode({"title": title, "desp": text})
        url = "https://sctapi.ftqq.com/%s.send?%s" % (
            urllib.parse.quote(sc["key"], safe=""), params)
        try:
            _post(url, b"", {"Content-Type": "text/plain"})
        except Exception as exc:
            errors.append("serverchan: %s" % _safe_error_text(exc))

    return (not errors), ("; ".join(errors) if errors else "")


# --------------------------------------------------------------- 后台投递

def _worker_loop():
    while not _stop.is_set():
        try:
            item = _queue.get(timeout=WORKER_INTERVAL)
        except queue.Empty:
            continue
        if item is None:
            break
        cfg, event, payload = item
        try:
            ok, err = _deliver(cfg, event, payload)
        except Exception as exc:
            ok, err = False, str(exc)
        with _lock:
            global _sent, _failed
            if ok:
                _sent += 1
            else:
                _failed += 1
            _last_result = {"at": time.time(), "ok": ok, "error": err[:300]}


def ensure_worker():
    """确保后台投递线程已启动（幂等）。"""
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return False
        _stop.clear()
        _worker = threading.Thread(target=_worker_loop, name="wb-alerts", daemon=True)
        _worker.start()
        return True


def emit(accounts_dir, event, payload=None, for_ticks=None, clear_ticks=None,
         key=None, hit=True):
    """提交一条告警（去抖后）。

    `hit` 为 False 表示「该条件已不成立」，此时走恢复分支。
    返回 True 表示这一轮真的排入了队列。
    """
    cfg = config(accounts_dir)
    if not cfg:
        return False
    th = cfg.get("thresholds") or {}
    f_ticks = for_ticks if for_ticks is not None else th.get("for_ticks", 2)
    c_ticks = clear_ticks if clear_ticks is not None else th.get("clear_ticks", 2)
    debounce_key = key or event
    action = _debounce_step(debounce_key, bool(hit), int(f_ticks), int(c_ticks))
    if action is None:
        return False
    if action == "clear":
        event = EV_POOL_RECOVERED
    ensure_worker()
    body = dict(payload or {})
    body.setdefault("event", event)
    try:
        _queue.put_nowait((cfg, event, body))
    except queue.Full:
        with _lock:
            global _dropped
            _dropped += 1
        return False
    return True


def check_pool(accounts_dir, ready, total, enabled=None, isolated=0):
    """账号池可用性检查（阈值型告警的入口）。

    判据用「**可派发**账号数」而不是启用数：v0.1.4 的未验证护栏会让
    enabled=True 的账号不参与派发，只看启用数会高估容量。
    """
    th = (config(accounts_dir).get("thresholds") or {})
    min_ready = th.get("min_ready", 8)
    degraded = ready < min_ready
    emit(accounts_dir, EV_POOL_DEGRADED,
         {"ready": ready, "enabled": enabled if enabled is not None else total,
          "total": total, "isolated": isolated, "min_ready": min_ready},
         key="pool_ready", hit=degraded)
    return degraded


def status():
    """面板用的状态视图。"""
    with _lock:
        return {
            "queued": _queue.qsize(),
            "sent": _sent,
            "failed": _failed,
            "dropped": _dropped,
            "last": dict(_last_result),
            "debounce": {k: dict(v) for k, v in _debounce.items()},
        }


def reset():
    """清空去抖状态与计数（测试用）。"""
    global _dropped, _sent, _failed
    with _lock:
        _debounce.clear()
        _dropped = 0
        _sent = 0
        _failed = 0
        _last_result = {"at": 0.0, "ok": False, "error": ""}
