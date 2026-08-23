"""添加/查询防火墙入站规则（端口8000），供手机局域网访问看板。"""
import subprocess
import sys

subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule",
                "name=stock_monitor_web", "dir=in", "action=allow",
                "protocol=TCP", "localport=8000"], capture_output=True, text=True)
out = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule",
                      "name=stock_monitor_web"], capture_output=True, text=True).stdout
ok = "8000" in out and "允许" in out or "Allow" in out
print("rule exists:", "stock_monitor_web" in out and "8000" in out)
print(out.strip()[:400])
sys.exit(0)
