"""模拟双击：启动 bat（输出重定向到文件），轮询 8000，失败则 dump 日志。"""
import subprocess
import time
import urllib.request

bat = r"c:\Users\Administrator\Desktop\stock_monitor\启动监控台.bat"
log = open(r"c:\Users\Administrator\Desktop\stock_monitor\data\_bat_launch.log", "w", encoding="utf-8", errors="replace")
subprocess.Popen(["cmd", "/c", bat], cwd=r"c:\Users\Administrator\Desktop\stock_monitor", stdout=log, stderr=log)
print("bat launched, waiting...")
for i in range(40):
    time.sleep(2)
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8000/api/overview", timeout=5)
        if r.status == 200:
            print(f"[OK] web ready in ~{(i+1)*2}s")
            break
    except Exception:
        pass
else:
    print("[FAIL] web not ready in 80s")
log.flush()
print("--- bat log head ---")
print(open(r"c:\Users\Administrator\Desktop\stock_monitor\data\_bat_launch.log", encoding="utf-8", errors="replace").read()[:1500])
