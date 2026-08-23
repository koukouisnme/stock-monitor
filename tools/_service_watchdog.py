# -*- coding: utf-8 -*-
"""服务看护进程：确保 web(:8000) + cloudflared 隧道 常活，死亡自动拉起。

- 由计划任务 StockMonitorWatchdog（登录时触发）启动，进程归 Task Scheduler 所有，
  不随终端/会话退出而死亡；重启后随登录自动恢复。
- 单实例锁：data/watchdog.lock（PID 存活检测，防双看护）。
- 子进程输出落 data/web_service.log（崩溃可查栈）。
- 日志：data/watchdog.log（自动截断至 ~200KB）。
"""
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "py312", "python.exe")
if not os.path.exists(PY):
    PY = sys.executable
TUNNEL = os.path.join(ROOT, "tools", "tunnel_web.py")
DATA = os.path.join(ROOT, "data")
WLOG = os.path.join(DATA, "watchdog.log")
SLOG = os.path.join(DATA, "web_service.log")
LOCK = os.path.join(DATA, "watchdog.lock")
CHECK_INTERVAL = 60        # 常规巡检间隔（秒）
BOOT_WAIT = 90            # 拉起后等待隧道+web 启动（秒）


def log(msg):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(WLOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
        if os.path.getsize(WLOG) > 1_000_000:
            with open(WLOG, "r", encoding="utf-8") as f:
                tail = f.read()[-200_000:]
            with open(WLOG, "w", encoding="utf-8") as f:
                f.write(tail)
    except Exception:
        pass


def port_pids():
    """:8000 监听中的 PID 集合。"""
    try:
        out = subprocess.run(["netstat", "-aon", "-p", "tcp"], capture_output=True,
                             text=True, timeout=20).stdout
        return {l.split()[-1] for l in out.splitlines()
                if ":8000 " in l and "LISTENING" in l.upper()}
    except Exception:
        return set()


def proc_alive(pid, name_hint=""):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True, timeout=15).stdout
        return str(pid) in out and (not name_hint or name_hint.lower() in out.lower())
    except Exception:
        return False


def image_exists(name):
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                             capture_output=True, text=True, timeout=15).stdout
        return name.lower() in out.lower()
    except Exception:
        return True   # 查询失败时保守认为存在，避免误杀重建


def acquire_lock():
    """单实例锁：lock 文件记录 PID；持有者存活则本实例退出。"""
    try:
        os.makedirs(DATA, exist_ok=True)
        if os.path.exists(LOCK):
            with open(LOCK, "r") as f:
                old = (f.read() or "").strip()
            if old.isdigit() and proc_alive(old, "python"):
                log(f"watchdog already running pid={old}, exit")
                return False
        with open(LOCK, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True    # 锁机制故障不阻塞看护


def kill_stale():
    """清理 :8000 旧监听与孤儿 cloudflared，防双实例/新旧代码并存。"""
    for pid in port_pids():
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
        log(f"killed stale listener pid={pid}")
    if image_exists("cloudflared.exe"):
        subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True)
        log("killed stale cloudflared")


def launch():
    log("launching tunnel_web.py")
    os.makedirs(DATA, exist_ok=True)
    lf = open(SLOG, "a", encoding="utf-8", errors="replace")
    lf.write("\n" + "=" * 50 + "\n" + time.strftime("%Y-%m-%d %H:%M:%S") +
             " service start\n" + "=" * 50 + "\n")
    lf.flush()
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    subprocess.Popen([PY, "-u", TUNNEL], cwd=ROOT, stdout=lf, stderr=lf,
                     stdin=subprocess.DEVNULL, env=env,
                     creationflags=subprocess.CREATE_NO_WINDOW |
                     subprocess.CREATE_NEW_PROCESS_GROUP)


def main():
    # 抗误杀：忽略控制台 Ctrl+C / Ctrl+Break（历史教训：任务实例曾被控制台事件杀死，
    # Last Result=0xC000013A）。更强的终止（taskkill/任务计划器停任务）由5分钟保活触发器兜底。
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass
    if not acquire_lock():
        return
    log(f"watchdog started pid={os.getpid()}")
    while True:
        try:
            up = bool(port_pids())
            tunnel_ok = image_exists("cloudflared.exe")
            if not up:
                log("service DOWN (port 8000 not listening)")
                kill_stale()
                launch()
                time.sleep(BOOT_WAIT)
            elif not tunnel_ok:
                # web 活着但隧道死了：整树重启换取新公网地址
                log("tunnel DOWN (cloudflared missing), full restart")
                kill_stale()
                launch()
                time.sleep(BOOT_WAIT)
        except Exception as e:
            log(f"watchdog error: {type(e).__name__}: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
