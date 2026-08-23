"""Wait for running market_sim job to finish (agent helper)."""
import json
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

B = "http://127.0.0.1:8000"
deadline = time.time() + 110
while time.time() < deadline:
    st = json.loads(urllib.request.urlopen(B + "/api/market_sim/status", timeout=30).read())
    if not st["running"]:
        print(f"FINISHED done={st['done']}/{st['total']} ok={st['ok']} "
              f"errors={len(st.get('errors') or [])}")
        for e in (st.get("errors") or [])[:6]:
            print("  err:", e[:130])
        sys.exit(0)
    print(f"running {st['done']}/{st['total']} ok={st['ok']}", flush=True)
    time.sleep(15)
print("STILL_RUNNING")
