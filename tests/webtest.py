"""web_ui 新接口冒烟测试。"""
import requests

B = "http://127.0.0.1:8000"


def j(method, path, **kw):
    r = requests.request(method, B + path, timeout=300, **kw)
    return r.status_code, r.json()


# 1. 搜索：名称（在线）、自选、代码
c, d = j("GET", "/api/search?q=" + requests.utils.quote("茅台"))
print("[1] search 茅台:", c, [(i["code"], i["name"], i["type"], i["in_pool"]) for i in d["items"][:3]])
c, d = j("GET", "/api/search?q=161005")
print("[2] search 161005:", c, [(i["code"], i["name"], i["in_pool"]) for i in d["items"]])
c, d = j("GET", "/api/search?q=" + requests.utils.quote("白酒LOF"))
print("[3] search 白酒LOF:", c, [(i["code"], i["name"], i["type"]) for i in d["items"][:3]])

# 2. K线多周期：自选600519 + 非自选ETF(510300)在线拉取
for code, period in [("600519", "day"), ("600519", "week"), ("600519", "month"), ("510300", "day")]:
    c, d = j("GET", f"/api/kline/{code}?period={period}")
    t6 = sum(1 for t in d["turns"] if abs(t) >= 6)
    print(f"[4] kline {code} {period}:", c, f"bars={len(d['dates'])} src={d['source']} 九转≥6={t6} is_lof={d['is_lof']}")

# 3. 排行（周期+键）
for key, period in [("vol_ratio", "day"), ("amount", "day"), ("turn_abs", "week"), ("premium", "day")]:
    c, d = j("GET", f"/api/rank?key={key}&period={period}")
    top = d["rows"][0] if d.get("rows") else {}
    print(f"[5] rank {key}/{period}:", c, f"rows={len(d.get('rows', []))} top={top.get('name')} {top.get('code')}")

# 4. 数据来源 / 统计
c, d = j("GET", "/api/sources")
print("[6] sources:", c, "order=", d["order"], "per_code=", [(x["code"], x["last_date"], x["bars"]) for x in d["per_code"]])
c, d = j("GET", "/api/stats")
print("[7] stats:", c, d)

# 5. LOF 评估 + 溢价历史走势
c, d = j("GET", "/api/lof/161005")
print("[8] lof 161005:", c, (d.get("card") or "")[:80].replace("\n", " | "))
c, d = j("GET", "/api/premium/161005")
rows = d.get("rows") or []
first = rows[0] if rows else {}
print("[8b] premium 161005:", c, f"rows={len(rows)} first={first.get('date')} "
      f"prem={first.get('premium_official')}% asc={rows[0]['date'] <= rows[-1]['date'] if len(rows) > 1 else '-'}")

# 6. 心跳 / 演示 / 自测（POST）
c, d = j("POST", "/api/heartbeat")
print("[9] heartbeat:", c, "ok=", d["ok"])
c, d = j("POST", "/api/demo")
print("[10] demo:", c, "signals=", d["signals"], "src=", d["source"])
c, d = j("POST", "/api/selftest")
print("[11] selftest:", c, "exit=", d["exit_code"], d["summary"])
c, d = j("GET", "/api/runlog")
print("[12] runlog kinds:", c, [k for k, v in d.items() if v])
