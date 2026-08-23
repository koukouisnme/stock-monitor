"""Diagnose price mismatch on /market page: compare sina realtime close vs backtest result close."""
import json
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

B = "http://127.0.0.1:8000"


def get(p):
    return json.loads(urllib.request.urlopen(B + p, timeout=90).read())


def post(p, body):
    req = urllib.request.Request(B + p, method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())


lst = get("/api/market_list")
live = {r["code"]: r["close"] for r in lst["rows"]}
by_code = {r["code"]: r for r in lst["rows"]}

# exchange prefix distribution of stock-type rows
pref = {}
for r in lst["rows"]:
    if r["type"] == "stock":
        pref[r["code"][:2]] = pref.get(r["code"][:2], 0) + 1
print("stock prefix dist:", dict(sorted(pref.items(), key=lambda x: -x[1])))

# run small backtest on stocks only
post("/api/market_sim", {"types": ["stock"], "limit": 60, "years": 3,
                         "initial": 10000, "ud": 1000, "uw": 3000, "um": 5000})
import time
while True:
    st = get("/api/market_sim/status")
    if not st["running"]:
        break
    time.sleep(2)

print(f"\ndone={st['done']} ok={st['ok']} errors={len(st.get('errors') or [])}")
bad = []
for r in st["results"]:
    lv = live.get(r["code"])
    if lv is None:
        continue
    if lv and abs(r["close"] - lv) / lv > 0.02:
        bad.append((r["name"], r["code"], r["close"], lv, round((r["close"] / lv - 1) * 100, 1)))
print(f"mismatch>2%: {len(bad)} / {len(st['results'])}")
for b in bad[:20]:
    print(f"  {b[0]}-{b[1]} sim={b[2]} live={b[3]} diff={b[4]}%")
print("\nsample errors:", (st.get("errors") or [])[:5])
