"""个股画像增强（东方财富公开接口）：ROE / 行业板块（主营类型）/ 板块日·周成交额。

数据链路：
- 个股行业：push2 stock/get f127（如"白酒Ⅱ"）
- ROE：datacenter F10 主要指标 ROEJQ（加权净资产收益率，最新报告期）
- 板块BK代码：push2delay clist 行业板块列表（行业名→BKxxxx）
- 板块日/周成交额：push2his kline 90.BKxxxx（klt=101/102，末字段=成交额）

全部容错：单只失败不影响整体；离线/超时返回空并保留旧缓存。
"""
import threading
import time

import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://quote.eastmoney.com/"}
_UT = "fa5fd1943c7b386f172d6893dbfba10b"
_board_cache = {"at": 0.0, "map": {}}          # 行业名→BK代码（进程级，1天有效）
_BOARD_TTL = 86400
_last_enrich = {"at": 0.0}                    # 12h内不重复全量刷新


def _secid(code: str) -> str:
    return f"1.{code}" if str(code).startswith(("6", "5")) else f"0.{code}"


def _get(url: str, params: dict, timeout: int = 10):
    r = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_industry(code: str) -> str:
    """个股所属行业板块名（主营类型，如"白色家电"）。push2直连易被断开，用delay域名。"""
    try:
        d = _get("https://push2delay.eastmoney.com/api/qt/stock/get",
                 {"secid": _secid(code), "fields": "f127", "ut": _UT})
        return (d.get("data") or {}).get("f127") or ""
    except Exception:
        return ""


def fetch_roe(code: str):
    """最新报告期加权净资产收益率ROE(%)。"""
    suffix = "SH" if str(code).startswith(("6", "5")) else "SZ"
    try:
        d = _get("https://datacenter.eastmoney.com/securities/api/data/get",
                 {"type": "RPT_F10_FINANCE_MAINFINADATA", "sty": "APP_F10_MAINFINADATA",
                  "filter": f'(SECUCODE="{code}.{suffix}")', "p": 1, "ps": 1,
                  "sr": -1, "st": "REPORT_DATE", "source": "HSF10", "client": "PC"},
                 timeout=12)
        rows = (d.get("result") or {}).get("data") or []
        return rows[0].get("ROEJQ") if rows else None
    except Exception:
        return None


def _board_map() -> dict:
    """行业板块名→BK代码（缓存1天）。共约500个板块，每页上限100，按代码序翻页取全量。"""
    if _board_cache["map"] and time.time() - _board_cache["at"] < _BOARD_TTL:
        return _board_cache["map"]
    m = {}
    for pn in range(1, 8):  # 7页×100 覆盖全量
        try:
            d = _get("https://push2delay.eastmoney.com/api/qt/clist/get",
                     {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                      "fid": "f12", "fs": "m:90+t:2", "fields": "f12,f14", "ut": _UT})
            diff = (d.get("data") or {}).get("diff") or []
            if not diff:
                break
            for b in diff:
                m[b["f14"]] = b["f12"]
        except Exception:
            break
    if m:
        _board_cache["map"] = m
        _board_cache["at"] = time.time()
    return m


_KLINE_HOSTS = ("push2his.eastmoney.com", "91.push2his.eastmoney.com")
_clist_amt_cache = {"at": 0.0, "map": {}}   # BK代码 → 当日成交额(实时快照，10分钟有效)


def _board_day_amt_clist() -> dict:
    """板块当日成交额（push2delay clist实时快照）：kline通道被限流时的日额兜底。"""
    if _clist_amt_cache["map"] and time.time() - _clist_amt_cache["at"] < 600:
        return _clist_amt_cache["map"]
    m = {}
    for pn in range(1, 8):
        try:
            d = _get("https://push2delay.eastmoney.com/api/qt/clist/get",
                     {"pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                      "fid": "f12", "fs": "m:90+t:2", "fields": "f12,f6", "ut": _UT})
            diff = (d.get("data") or {}).get("diff") or []
            if not diff:
                break
            for b in diff:
                v = b.get("f6")
                if v not in ("-", None):
                    m[str(b.get("f12"))] = float(v)
        except Exception:
            break
    if m:
        _clist_amt_cache["map"] = m
        _clist_amt_cache["at"] = time.time()
    return m


def fetch_board_amounts(board_code: str) -> tuple:
    """板块最近一根日K/周K成交额(元)。
    周额仅push2his kline可取（被限流时返回None，下次刷新再补）；
    日额kline失败时用push2delay clist实时成交额兜底。"""
    out = (None, None)
    for klt, idx in ((101, 0), (102, 1)):
        amt = None
        for host in _KLINE_HOSTS:
            try:
                d = _get(f"https://{host}/api/qt/stock/kline/get",
                         {"secid": f"90.{board_code}", "fields1": "f1,f2,f3,f4,f5,f6",
                          "fields2": "f51,f52,f53,f54,f55,f56,f57", "klt": klt, "fqt": 1,
                          "end": "20500101", "lmt": 1, "ut": _UT})
                ks = (d.get("data") or {}).get("klines") or []
                if ks:
                    amt = float(ks[-1].split(",")[-1])
                break
            except Exception:
                continue
        if amt is None and klt == 101:
            amt = _board_day_amt_clist().get(board_code)
        if amt is not None:
            out = (amt, out[1]) if idx == 0 else (out[0], amt)
    return out


def enrich_stock(code: str) -> dict:
    """单只股票画像：行业/ROE/板块日周成交额。"""
    industry = fetch_industry(code)
    roe = fetch_roe(code)
    board_code, day_amt, week_amt = "", None, None
    if industry:
        board_code = _board_map().get(industry, "")
        if board_code:
            day_amt, week_amt = fetch_board_amounts(board_code)
    return {"code": code, "industry": industry, "roe": roe, "board_code": board_code,
            "board_day_amt": day_amt, "board_week_amt": week_amt}


def enrich_watchlist(cfg: dict, cache) -> int:
    """自选池股票画像批量刷新（ETF/LOF无行业与ROE，跳过）。返回成功条数。
    12小时内不重复全量刷新（板块成交额每个收盘扫描周期粒度足够）。"""
    from infrastructure.adapters import detect_type
    if time.time() - _last_enrich["at"] < 43200:
        return -1
    _last_enrich["at"] = time.time()
    rows = []
    for item in cfg.get("watchlist", []):
        code = str(item.get("code", ""))
        if not code or detect_type(code) != "stock":
            continue
        try:
            rows.append(enrich_stock(code))
        except Exception:
            continue
    if rows:
        cache.upsert_stock_meta(rows)
    return len(rows)


def enrich_watchlist_async(cfg: dict, cache):
    """后台线程刷新（不阻塞请求）。"""
    t = threading.Thread(target=enrich_watchlist, args=(cfg, cache), daemon=True)
    t.start()
    return t
