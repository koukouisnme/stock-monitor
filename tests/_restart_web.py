"""重启隧道+Web：杀掉旧 cloudflared/8000端口进程，由外部重新启动 tunnel_web.py。"""
import subprocess
import sys

out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
pids = set()
for line in out.splitlines():
    if ":8000" in line and "LISTENING" in line:
        pids.add(line.split()[-1])
tl = subprocess.run(["tasklist", "/FO", "CSV"], capture_output=True, text=True).stdout
for line in tl.splitlines():
    if "cloudflared" in line.lower():
        pids.add(line.split('","')[1].rstrip('"'))
for pid in pids:
    if pid and pid.isdigit():
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        print(f"killed {pid}")
print("done" if pids else "no-process")
sys.exit(0)
