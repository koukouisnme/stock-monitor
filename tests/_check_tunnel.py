"""验证隧道连通性 + Web服务绑定状态。"""
import json
import socket
import urllib.request

# 1) 本地服务是否可达
s = socket.socket()
local = s.connect_ex(("127.0.0.1", 8000))
s.close()
print(f"[本地] 127.0.0.1:8000 -> {'OK' if local == 0 else 'FAIL'}")

# 2) 隧道公网可达性（读 config.yaml 的 public_url）
import re
with open("config.yaml", "r", encoding="utf-8") as f:
    m = re.search(r'public_url:\s*"([^"]+)"', f.read())
url = m.group(1) if m else ""
print(f"[隧道] {url}")
try:
    req = urllib.request.Request(url.rstrip("/") + "/api/overview",
                                 headers={"User-Agent": "curl/8.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read())
    ov = d.get("overview", d)
    print(f"[隧道] 公网API OK，自选池={ov.get('pool', d.get('pool'))}")
    req2 = urllib.request.Request(url.rstrip("/") + "/",
                                  headers={"User-Agent": "curl/8.0"})
    html = urllib.request.urlopen(req2, timeout=25).read().decode("utf-8", "ignore")
    print(f"[隧道] 公网首页 OK，长度={len(html)} 标题={'A股监控台' in html}")
except Exception as e:
    print(f"[隧道] 公网访问 FAIL: {e}")
