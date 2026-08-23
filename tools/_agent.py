"""Agent helper: service status / restart / health-poll for the web dashboard.

Usage (from project root):
  py312\\python.exe tools\\_agent.py status    # port 8000 PID + HTTP health
  py312\\python.exe tools\\_agent.py restart   # kill old service+tunnel, relaunch detached
"""
import os
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "py312", "python.exe")
LOG = os.path.join(ROOT, "tools", "_web.log")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def port_pid(port=8000):
    try:
        out = subprocess.run(["netstat", "-aon", "-p", "tcp"], capture_output=True,
                             text=True, timeout=15).stdout
    except Exception as e:
        return None, f"netstat failed: {e}"
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            return line.split()[-1], line.strip()
    return None, "no listener"


def http_ok(timeout=5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/api/overview",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def status():
    pid, info = port_pid()
    print(f"port8000 pid={pid} info={info}")
    print(f"http /api/overview -> {http_ok()}")


def restart():
    # 1) kill old listener (whole tree) + stale tunnels
    pid, _ = port_pid()
    if pid:
        subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True)
    subprocess.run(["taskkill", "/F", "/IM", "cloudflared.exe"], capture_output=True)
    time.sleep(2)
    # 2) relaunch via `cmd /c start`（经验证可脱离工具宿主 Job 存活），
    #    输出重定向追加到 tools\_web.log
    inner = "py312\\python.exe -u tools\\tunnel_web.py >> tools\\_web.log 2>&1"
    subprocess.Popen(["cmd", "/c", "start", "StockMonitor", "/min",
                      "cmd", "/c", inner], cwd=ROOT,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)
    # 3) poll health up to 90s, then confirm listener pid alive
    for i in range(45):
        time.sleep(2)
        r = http_ok(4)
        print(f"[{i*2+2:03d}s] health={r}")
        if r is True:
            npid, _ = port_pid()
            print(f"listener pid={npid}")
            print("RESTART_OK" if npid else "RESTART_FAIL: no listener")
            return
    print("RESTART_FAIL: service not healthy in 90s, tail of log:")
    try:
        with open(LOG, "rb") as fh:
            print(fh.read()[-2000:].decode("utf-8", "replace"))
    except Exception:
        pass


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    status() if mode == "status" else restart() if mode == "restart" else print("usage: status|restart")
