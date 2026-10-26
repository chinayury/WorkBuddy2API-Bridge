# -*- coding: utf-8 -*-
"""wb_atomic.py —— 原子落盘与凭据文件权限的统一实现。

此前账号凭据（wb_accounts.py）与面板设置（wb_settings.py）各自实现
「tmp + os.replace」，带来两个问题：

1. **Windows 上 os.replace 会失败**：Python 打开文件时**不带 FILE_SHARE_DELETE**，
   目标文件只要被任何读者打开（另一个线程、杀毒软件、资源管理器预览），
   替换就必然 PermissionError [WinError 5]。凭据与设置的落盘因此变成
   「间歇性失败」—— 刷新后的新 token 没写下去、面板改完 Key 旧 Key 仍生效，
   而面板报告的是「保存成功」。Linux/macOS 上 os.replace 是原子的，不受影响。
2. **文件权限是 0666 & ~umask**：Linux 上 umask 022 是常态，凭据文件落盘后
   同机任何用户都能读到 accessToken / refreshToken / API Key。
   同一项目里的 wb_agents.py 早就用 WRITE_MODE=0o600 做了这件事
   （注释写明「DSH 在凭据文件可读时拒绝启动」），只是最关键的两个文件没跟上。

本模块把两件事收成一处：写入前设 0600，替换时对 Windows 的占用做有界重试。
"""
import os
import time

#: 凭据类文件的目标权限：仅属主可读写。
CREDENTIAL_MODE = 0o600

#: Windows 上替换被占用文件时的重试次数与间隔（秒）。
#: 40 × 25ms = 最长 1 秒，足以覆盖杀毒软件与资源管理器的短暂占用。
REPLACE_RETRIES = 40
REPLACE_RETRY_DELAY = 0.025


def tighten_mode(path, mode=CREDENTIAL_MODE):
    """把文件权限收紧到 mode。Windows 上 chmod 只改只读位，无效即忽略。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def replace_atomic(tmp, path, retries=REPLACE_RETRIES, delay=REPLACE_RETRY_DELAY):
    """把 tmp 原子替换到 path；对 Windows 的「目标被占用」做有界重试。

    Linux/macOS 上 os.replace 是原子的，不会被读者阻塞；Windows 上目标被打开
    时会 PermissionError，重试即可等到对方关闭。超过重试上限仍失败则把原异常
    抛出 —— **不静默吞掉**：落盘失败必须让调用方知道。
    """
    last = None
    for attempt in range(retries + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last = exc
            if attempt >= retries:
                break
            time.sleep(delay)
    if last is not None:
        raise last
    raise OSError("atomic replace failed: %s -> %s" % (tmp, path))
