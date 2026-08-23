"""按端口杀监听进程（Windows）。用法: python _kill_port.py [port]"""
import subprocess
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
pids = set()
for line in out.splitlines():
    if f"127.0.0.1:{port}" in line and "LISTENING" in line:
        pids.add(line.split()[-1])
    if f"0.0.0.0:{port}" in line and "LISTENING" in line:
        pids.add(line.split()[-1])
for pid in pids:
    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)
    print(f"killed pid {pid} on port {port}")
print("done" if pids else "no listener found")
