"""Web 界面：Flask 仪表盘。
功能：自选池排行(日/周/月) / K线九转(多周期切换) / 在线搜索(股票·ETF·LOF) /
     数据来源 / 信号与扫描日志 / 运行日志 / 功能测试(自测·演示·心跳·手动扫描)。
"""
import contextlib
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd
import requests
from flask import Flask, jsonify, render_template_string, request

from domain.nine_turns import calc_turn_counts
from domain.resampler import resample
from infrastructure.adapters import detect_type, is_lof

_CODE_RE = re.compile(r"^\d{6}$")
_LOG = {"scan": "", "demo": "", "heartbeat": "", "selftest": "", "backtest": ""}  # 最近一次运行输出

# ---------- 全市场回测榜：列表缓存 + 批量任务状态（进程级单例） ----------
_MK = {"list": None, "list_ts": 0.0, "job": None, "dbjob": None,
       "klines": OrderedDict()}  # klines: FIFO缓存
_MK_LIST_TTL = 24 * 3600          # 行情列表缓存1天
_MK_KLINE_TTL = 12 * 3600         # 单标的K线缓存12小时
_MK_KLINE_MAX = 600               # K线缓存条数上限（约35MB）


def _mk_type(code: str) -> str:
    """代码前缀分类：ETF(51/52/55/56/58/15) / LOF(16/50) / 股票(其余)。"""
    c = str(code)
    if c.startswith(("51", "52", "55", "56", "58", "15")):
        return "etf"
    if c.startswith(("16", "50")):
        return "lof"
    return "stock"


def _http_get(url: str, headers: dict = None, timeout: int = 15, tries: int = 3):
    """GET + 指数退避重试（应对偶发 RemoteDisconnected/限流）。失败抛最后一次异常。"""
    last = None
    for i in range(tries):
        if i:
            time.sleep(1.2 * i)
        try:
            r = requests.get(url, headers=headers or {}, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
    raise last


def _sina_node(node: str, mtype: str) -> list:
    """新浪 Market_Center 拉一个板块全量列表（按成交额降序）。node: hs_a/etf_hq_fund/lof_hq_fund。
    https 被反爬(456)时自动降级 http 通道。"""
    bases = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
             "/Market_Center.",
             "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
             "/Market_Center.")
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://finance.sina.com.cn/"}
    base, total = None, 0
    for b in bases:
        try:
            cnt = _http_get(b + f"getHQNodeStockCount?node={node}",
                            headers=hdr, timeout=12, tries=2).text.strip().strip('"')
            if cnt.isdigit():
                base, total = b, int(cnt)
                break
        except Exception:
            continue
    if not base:
        raise RuntimeError(f"新浪 {node} count 接口不可达(https/http)")
    rows, page = [], 1
    while len(rows) < total:
        try:
            r = _http_get(base + f"getHQNodeData?page={page}&num=100&sort=amount&asc=0"
                          f"&node={node}", headers=hdr, timeout=15, tries=2)
            batch = json.loads(r.text) if r.text.strip() else []
        except Exception:
            if rows:
                break             # 中途被反爬：保留已拉到的部分（排序口径仍够用）
            raise
        if not batch:
            break
        for x in batch:
            code, name = str(x.get("code", "")), str(x.get("name", ""))
            if not _CODE_RE.match(code) or any(ch in name for ch in ("退", "ST")):
                continue
            try:
                close = float(x.get("trade") or 0) or None
                amount = float(x.get("amount") or 0)
            except (TypeError, ValueError):
                continue
            rows.append({"code": code, "name": name, "close": close,
                         "amount": amount,
                         "type": mtype if mtype else _mk_type(code)})
        page += 1
        time.sleep(0.15)          # 翻页限速：连续快翻易触发456反爬
    return rows


_EM_HOSTS = ("82.push2.eastmoney.com", "push2.eastmoney.com",
             "push2delay.eastmoney.com")   # 实时优先，限流时delay主机兜底
_EM_GOOD_HOST = {"h": None}


def _em_pick_host() -> str:
    """探测可用东财主机（优先上次成功的）。"""
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://quote.eastmoney.com/"}
    hosts = ((_EM_GOOD_HOST["h"],) if _EM_GOOD_HOST["h"] else ()) + _EM_HOSTS
    for h in hosts:
        try:
            r = _http_get(f"https://{h}/api/qt/clist/get?pn=1&pz=5&po=1&np=1"
                          "&fltt=2&invt=2&fid=f6&fs=m:0+t:6&fields=f12,f14",
                          headers=hdr, timeout=10, tries=2)
            if (r.json() or {}).get("data"):
                _EM_GOOD_HOST["h"] = h
                return h
        except Exception:
            continue
    raise RuntimeError("东财clist各主机均不可达")


_EM_KEEP = {1: ("60", "68", "51", "52", "55", "56", "58", "50"),   # 沪：A股+ETF/LOF
            0: ("00", "30", "15", "16")}                            # 深：A股+ETF/LOF


def _em_is_index(code: str, mkt) -> bool:
    """白名单过滤：不在保留前缀的一律视为指数/债券等剔除。"""
    return not str(code).startswith(_EM_KEEP.get(mkt, ()))


def _em_fetch_groups() -> dict:
    """东财兜底：返回 {'stock': [...], 'etf': [...], 'lof': [...]}，失败组为空列表。"""
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
           "Referer": "https://quote.eastmoney.com/"}
    host = _em_pick_host()
    out = {"stock": [], "etf": [], "lof": []}
    for fs in ("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",   # 沪深A股
               "b:MK0021,b:MK0022,b:MK0023,b:MK0024,b:MK0025"):   # 场内基金板块(ETF/LOF)
        got, pn = [], 1
        while True:
            try:
                url = (f"https://{host}/api/qt/clist/get"
                       f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f6&fs={fs}"
                       "&fields=f12,f13,f14,f2,f6")
                r = _http_get(url, headers=hdr, tries=2)
                data = (r.json() or {}).get("data") or {}
            except Exception:
                break                     # 该组中途失败：保留已拉部分
            rows = data.get("diff") or []
            if not rows:
                break
            for x in rows:
                code, name = str(x.get("f12", "")), str(x.get("f14", ""))
                mkt = x.get("f13")
                if (not _CODE_RE.match(code) or any(ch in name for ch in ("退", "ST"))
                        or _em_is_index(code, mkt)):
                    continue
                try:
                    price, amt = x.get("f2"), x.get("f6")
                    got.append({"code": code, "name": name,
                                "close": None if price in ("-", None) else float(price),
                                "amount": 0.0 if amt in ("-", None) else float(amt or 0),
                                "type": _mk_type(code)})
                except (TypeError, ValueError):
                    continue
            total = int(data.get("total") or 0)
            if pn * 100 >= total:
                break
            pn += 1
            time.sleep(0.1)
        for x in got:
            out[x["type"]].append(x)
    return out


def _mk_fetch_list() -> list:
    """全市场列表（按成交额降序）。分节点容错：新浪逐节点，缺的类型走东财补。"""
    parts = {}
    sina_skip = time.time() < _MK.get("sina_skip_until", 0)
    for node, t in (("hs_a", "stock"), ("etf_hq_fund", "etf"), ("lof_hq_fund", "lof")):
        if sina_skip:
            parts[t] = []
            continue
        try:
            parts[t] = _sina_node(node, t)
        except Exception:
            parts[t] = []
            _MK["sina_skip_until"] = time.time() + 300   # 熔断：5分钟内直连东财
    if not any(parts.values()):        # 新浪整体失败 → 东财补全部
        try:
            parts = _em_fetch_groups()
        except Exception as e:
            raise RuntimeError(f"新浪与东财全市场列表均拉取失败: {e}")
    else:                               # 只补缺失的类型（etf/lof 东财无独立分组，按需）
        try:
            em = _em_fetch_groups()
            for t in ("stock", "etf", "lof"):
                if not parts.get(t):
                    parts[t] = em.get(t, [])
        except Exception:
            pass
    out = [x for t in ("stock", "etf", "lof") for x in parts.get(t, [])]
    if not out:
        raise RuntimeError("全市场列表各数据源均无数据")
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out


def _mk_load_persisted():
    """启动时从SQLite加载上次列表（重启不再重拉网络）。"""
    cache = _MK.get("cache")
    if not cache or _MK["list"]:
        return
    try:
        rows, updated = cache.mk_list_load()
        if rows:
            _MK["list"] = rows
            try:
                _MK["list_ts"] = datetime.strptime(updated, "%Y-%m-%d %H:%M").timestamp()
            except Exception:
                _MK["list_ts"] = 0.0
    except Exception:
        pass


def _mk_get_list(force: bool = False) -> list:
    if not force and _MK["list"] and time.time() - _MK["list_ts"] < _MK_LIST_TTL:
        return _MK["list"]
    try:
        lst = _mk_fetch_list()      # 拉取成功才覆盖缓存
    except Exception:
        if not force and _MK["list"]:
            return _MK["list"]      # 网络失败：旧列表兜底（重启时来自SQLite）
        raise
    _MK["list"] = lst
    _MK["list_ts"] = time.time()
    cache = _MK.get("cache")
    if cache:
        try:
            cache.mk_list_save(lst)   # 持久化：重启直接用
        except Exception:
            pass
    return lst


def _mk_kline_fresh(last_date: str) -> bool:
    """库内K线是否够新：最后交易日距今天 <= 5天（容错周末+小长假）。"""
    if not last_date:
        return False
    try:
        return (datetime.now() - datetime.strptime(last_date, "%Y-%m-%d")).days <= 5
    except ValueError:
        return False


def _mk_cache_df(code: str, df) -> None:
    _MK["klines"][code] = (time.time(), df)
    _MK["klines"].move_to_end(code)
    while len(_MK["klines"]) > _MK_KLINE_MAX:
        _MK["klines"].popitem(last=False)   # 淘汰最旧


def _mk_kline(sources, cache, code: str, force: bool = False):
    """统一口径K线（行内榜与展开图同源）：固定1000天窗口，自选池 kline_day 表。
    读取顺序：内存缓存 → kline_day(够新) → 网络拉取(成功落kline_day)。
    reject_synthetic：绝不允许合成数据进排行榜；网络失败时回退旧数据。"""
    hit = _MK["klines"].get(code)
    if not force and hit and time.time() - hit[0] < _MK_KLINE_TTL and len(hit[1]) >= 240:
        _MK["klines"].move_to_end(code)
        return hit[1]
    if cache is not None and not force:
        try:
            if _mk_kline_fresh(cache.last_kline_date(code)):
                db = cache.get_klines(code).tail(1000).reset_index(drop=True)
                if len(db) >= 240:
                    _mk_cache_df(code, db)
                    return db
        except Exception:
            pass
    try:
        df = sources.fetch_kline(code, days=1000, reject_synthetic=True)
    except Exception:
        df = pd.DataFrame()
    if not df.empty:
        if cache is not None:
            try:
                cache.upsert_klines(code, df)   # 落kline_day（下次不再请求）
            except Exception:
                pass
        _mk_cache_df(code, df)
        return df
    if force:
        return pd.DataFrame()    # 强制更新模式：网络失败即失败（不回退旧数据）
    if hit:                      # 网络失败：内存旧数据兜底
        _MK["klines"].move_to_end(code)
        return hit[1]
    if cache is not None:        # 再退kline_day旧数据
        try:
            db = cache.get_klines(code).tail(1000).reset_index(drop=True)
            if len(db) >= 30:
                _mk_cache_df(code, db)
                return db
        except Exception:
            pass
    return pd.DataFrame()


def _mk_db_update_job(sources, cache, targets: list, years: float):
    """手动更新K线库任务：逐只强制拉网络并落kline_day（幂等覆盖）。"""
    job = _MK["dbjob"]
    lock = threading.Lock()

    def work(item):
        while job.get("paused") and not job["stop"]:
            time.sleep(0.3)
        if job["stop"]:
            return
        try:
            df = _mk_kline(sources, cache, item["code"], force=True)
            if df.empty:
                raise ValueError("拉取失败")
            with lock:
                job["ok"] += 1
        except Exception as e:
            with lock:
                job["errors"] = (job["errors"] + [f"{item['code']} {item['name']}: {e}"])[-100:]
        finally:
            with lock:
                job["done"] += 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(work, targets))
    job["running"] = False
    job["finished_at"] = datetime.now().strftime("%H:%M:%S")


def _mk_sim_persist(cache, job):
    """回测任务快照落库（重启后恢复上次榜单，免重跑）。失败静默。"""
    if cache is None:
        return
    try:
        cache.mk_sim_save({k: job.get(k) for k in
                           ("params", "total", "done", "ok", "results",
                            "errors", "started_at", "finished_at")})
    except Exception:
        pass


def _mk_run_job(sources, cache, codes_names: list, params: dict):
    """批量回测工作线程：线程池并发拉K线+模拟，实时写进度。"""
    from domain.backtest import simulate_shares, current_period_turns
    job = _MK["job"]
    lock = threading.Lock()

    def work(item):
        while job.get("paused") and not job["stop"]:
            time.sleep(0.3)          # 暂停：等待恢复或停止
        if job["stop"]:
            return
        code, name, mtype = item["code"], item["name"], item["type"]
        try:
            df = _mk_kline(sources, cache, code)
            if df.empty or len(df) < 30:
                raise ValueError("无真实K线(已跳过合成数据)")
            turns = current_period_turns(df)   # 当前周/月九转状态（截断前算，反映最新结构）
            if params["years"] > 0:
                d = pd.to_datetime(df["date"])
                df = df[d >= d.max() - pd.Timedelta(days=int(365 * params["years"]))] \
                    .reset_index(drop=True)
                if len(df) < 30:
                    raise ValueError("区间K线不足")
            sim = simulate_shares(df, params["initial"], params["ud"], params["uw"], params["um"])
            st = sim["stats"]
            # 现价优先取列表实时行情（K线为前复权口径，与现价存在系统性偏差）
            px = item.get("close") or round(float(df["close"].iloc[-1]), 3)
            row = {"code": code, "name": name, "type": mtype,
                   "close": px,
                   "total_ret": st["total_ret"], "buy_hold_ret": st["buy_hold_ret"],
                   "excess": st["excess"], "annual_ret": st["annual_ret"],
                   "max_drawdown": st["max_drawdown"], "n_trades": st["n_trades"],
                   "turn_week": turns["week"], "turn_month": turns["month"]}
            with lock:
                job["results"].append(row)
                job["ok"] += 1
        except Exception as e:
            with lock:
                job["errors"] = (job["errors"] + [f"{code} {name}: {e}"])[-200:]
        finally:
            with lock:
                job["done"] += 1
                if job["done"] % 20 == 0:
                    _mk_sim_persist(cache, job)   # 周期落库：中断也有部分结果

    with ThreadPoolExecutor(max_workers=2) as pool:   # 低并发+重试退避防行情源限流
        list(pool.map(work, codes_names))
    job["running"] = False
    job["finished_at"] = datetime.now().strftime("%H:%M:%S")
    _mk_sim_persist(cache, job)       # 完成落库：下次进站直接展示

_PERIODS = {"day": "日线", "week": "周线", "month": "月线"}
_STATE_CN = {"bull": "牛市", "bear": "熊市", "range": "震荡"}
_SRC_CN = {"tencent": "腾讯行情", "akshare": "东财(akshare)", "baostock": "宝砂(baostock)",
           "synthetic": "合成数据(离线)"}
# 腾讯联想接口返回的类别代码 → 中文
_TYPE_CN = {"GP-A": "股票", "GP-B": "股票", "ETF": "ETF", "LOF": "LOF",
            "ZS": "指数", "QZ": "指数", "BK": "板块", "HK": "港股", "US": "美股"}


def _clean(v):
    """NaN→None：sqlite NULL 经 pandas 读出为 NaN，直接 jsonify 会产出非法JSON。"""
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _unescape_u(s: str) -> str:
    """腾讯联想接口现在把中文转成 \\uXXXX 字面量，这里还原为中文。"""
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股监控台</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  :root { --bg:#0d1117; --card:#161b22; --bd:#30363d; --fg:#e6edf3; --dim:#8b949e;
          --up:#f85149; --dn:#3fb950; --acc:#58a6ff; --warn:#d29922; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.6 "Microsoft YaHei",sans-serif; padding:20px; }
  h1 { font-size:20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  .badge { font-size:13px; padding:2px 10px; border-radius:10px; background:#21262d; border:1px solid var(--bd); }
  .badge.bull { color:var(--up); border-color:var(--up); }
  .badge.bear { color:var(--dn); border-color:var(--dn); }
  .badge.range { color:var(--warn); border-color:var(--warn); }
  .toolbar { margin-left:auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  .search { position:relative; }
  .search input { background:#0d1117; color:var(--fg); border:1px solid var(--bd); border-radius:6px;
           padding:6px 10px; width:200px; font-size:13px; }
  .search .drop { position:absolute; top:34px; left:0; z-index:9; background:#161b22;
           border:1px solid var(--bd); border-radius:6px; min-width:260px; display:none; max-height:300px; overflow:auto; }
  .search .drop div { padding:6px 12px; cursor:pointer; font-size:13px; }
  .search .drop div:hover { background:#1c2a3a; }
  button, select { background:#21262d; color:var(--fg); border:1px solid var(--bd); border-radius:6px;
           padding:6px 14px; cursor:pointer; font-size:13px; }
  button:hover, select:hover { border-color:var(--acc); color:var(--acc); }
  button:disabled { opacity:.5; cursor:wait; }
  button.on { border-color:var(--acc); color:var(--acc); }
  .grid { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:16px 0; }
  .stat { background:var(--card); border:1px solid var(--bd); border-radius:8px; padding:12px 16px; }
  .stat .k { color:var(--dim); font-size:12px; }
  .stat .v { font-size:20px; font-weight:600; margin-top:2px; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:8px;
          padding:14px; margin-bottom:16px; overflow-x:auto; }
  .card h2 { font-size:15px; margin-bottom:10px; color:var(--acc); display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--dim); font-weight:500; padding:6px 10px; border-bottom:1px solid var(--bd); }
  td { padding:7px 10px; border-bottom:1px solid #21262d; white-space:nowrap; }
  tr.row { cursor:pointer; } tr.row:hover td { background:#1c2129; } tr.sel td { background:#1c2a3a; }
  .up { color:var(--up); } .dn { color:var(--dn); } .dim { color:var(--dim); }
  .lv { padding:1px 8px; border-radius:8px; font-size:12px; }
  .lv.S{background:#4a1113;color:#ff7b72} .lv.A{background:#4a2c0d;color:#ffa657}
  .lv.B{background:#4a380d;color:#d29922} .lv.LOF{background:#0d4a44;color:#56d4c8}
  .tag { padding:1px 8px; border-radius:8px; font-size:12px; background:#21262d; border:1px solid var(--bd); color:var(--dim); }
  .tag.live { color:var(--dn); border-color:var(--dn); }
  .tag.syn { color:var(--warn); border-color:var(--warn); }
  #chart { width:100%; height:380px; }
  pre { background:#0d1117; border:1px solid var(--bd); border-radius:6px; padding:10px;
        font-size:12px; overflow:auto; max-height:260px; white-space:pre-wrap; }
  #toast { position:fixed; right:20px; bottom:20px; background:#21262d; border:1px solid var(--bd);
           border-radius:8px; padding:10px 18px; display:none; z-index:99; }
  .delpool { display:inline-block; width:16px; height:16px; line-height:14px; text-align:center;
             margin-right:6px; border-radius:50%; border:1px solid #f85149; color:#f85149;
             font-size:13px; cursor:pointer; user-select:none; vertical-align:middle; }
  .delpool:hover { background:#f85149; color:#0d1117; }
  .lofcard { background:#0d1117; border:1px solid var(--bd); border-radius:6px; padding:10px 14px;
             font:13px/1.8 Consolas,monospace; white-space:pre-wrap; display:none; margin-top:10px; }
  .cols2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
  .pill { font-size:12px; color:var(--dim); }
  .simbox input, .simbox select { background:#0d1117; color:var(--fg); border:1px solid var(--bd); border-radius:4px;
                  padding:3px 6px; font-size:12px; }
  .qbar { display:flex; gap:10px; flex-wrap:wrap; background:#0d1117; border:1px solid var(--bd);
          border-radius:6px; padding:8px 14px; margin-bottom:10px; font-size:13px; }
  .qbar .qi { display:flex; gap:6px; align-items:baseline; padding-right:10px;
              border-right:1px solid #21262d; }
  .qbar .qi:last-child { border-right:none; }
  .qbar .qk { color:var(--dim); font-size:12px; }
  .qbar .qv { font-weight:600; }
  .rtabs { display:flex; gap:6px; margin-bottom:8px; }
  .rtabs button { background:#21262d; color:var(--dim); border:1px solid var(--bd);
                  padding:4px 14px; border-radius:14px; font-size:13px; cursor:pointer; }
  .rtabs button.on { color:#0d1117; background:var(--acc); border-color:var(--acc); font-weight:600; }
  #opsOverlay { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:90; display:none; }
  #opsOverlay.show { display:block; }
  #opsFrameBox { position:absolute; right:0; top:0; bottom:0; width:min(880px,92vw);
                 background:var(--bg); border-left:1px solid var(--bd); display:flex; flex-direction:column; }
  #opsFrame { flex:1; width:100%; border:none; }
  #opsClose { position:absolute; right:12px; top:10px; z-index:2; }
</style>
</head>
<body>
<h1>📈 A股监控台
  <span class="toolbar">
    <button style="font-size:13px;color:var(--acc);border:1px solid var(--bd);padding:5px 12px;border-radius:6px;background:#21262d;cursor:pointer"
            onclick="toggleOps()">⚙ 运维中心</button>
    <a href="/market" style="font-size:13px;color:#d29922;text-decoration:none;border:1px solid var(--bd);padding:5px 12px;border-radius:6px;background:#21262d">🏆 全市场回测榜</a>
    <span class="search">
      <input id="q" placeholder="搜索 代码/名称/拼音 (股票·ETF·LOF)" oninput="suggest()">
      <div class="drop" id="drop"></div>
    </span>
    <select id="selPeriod" onchange="reloadRank()">
      <option value="day">日线</option><option value="week">周线</option><option value="month">月线</option>
    </select>
    <select id="selKey" onchange="reloadRank()">
      <option value="vol_ratio">量比</option><option value="amount">成交额</option>
      <option value="pct_chg">涨跌幅</option><option value="turn_abs">九转计数</option>
      <option value="premium">溢价率</option>
    </select>
    <button onclick="loadAll()">刷新</button>
  </span>
</h1>

<div class="grid">
  <div class="stat"><div class="k">监控标的</div><div class="v" id="nStocks">-</div>
    <div class="pill" id="poolCn">-</div></div>
  <div class="stat"><div class="k">自选池构成</div><div class="v" style="font-size:16px" id="pool">-</div>
    <div class="pill" id="srcNote"></div></div>
  <div class="stat"><div class="k">最近扫描</div><div class="v" style="font-size:16px" id="lastScan">-</div></div>
  <div class="stat"><div class="k">今日信号 / 推送</div><div class="v" id="nSignals">-</div></div>
</div>

<div class="card">
  <h2>自选池排行 · <span id="rankLabel">日线·量比</span>（点击行查看K线）</h2>
  <div class="rtabs" id="rankTabs">
    <button id="rtStock" class="on" onclick="setRankTab('stock')">股票</button>
    <button id="rtEtf" onclick="setRankTab('etf')">ETF</button>
    <button id="rtLof" onclick="setRankTab('lof')">LOF</button>
    <span class="dim" style="margin:0 4px;align-self:center;font-size:12px">｜九转筛选：</span>
    <button id="rtW8" onclick="toggleRankTurn('w8')">周八</button>
    <button id="rtW9" onclick="toggleRankTurn('w9')">周九</button>
    <button id="rtM8" onclick="toggleRankTurn('m8')">月八</button>
    <button id="rtM9" onclick="toggleRankTurn('m9')">月九</button>
  </div>
  <div class="pill" style="margin-bottom:6px">入选依据：各行业高流动性龙头股 + 宽基/行业ETF + 跨境商品LOF（多资产覆盖，便于同时验证九转信号与溢价监控；可自行在 config.yaml 替换）</div>
  <table id="tbl"><thead><tr>
    <th>名称-代码</th><th id="thBasis">依据---量比</th><th>收盘</th><th>涨跌</th><th>量比</th><th>成交额</th>
    <th>九转</th><th>溢价</th><th>放量</th><th>行情</th>
  </tr></thead><tbody></tbody></table>
</div>

<div class="card">
  <h2><span id="chartTitle">K线 · 神奇九转</span>
    <span class="toolbar" style="margin-left:0">
      <button id="pd" class="on" onclick="setPeriod('day')">日线</button>
      <button id="pw" onclick="setPeriod('week')">周线</button>
      <button id="pm" onclick="setPeriod('month')">月线</button>
      <button id="poolBtn" style="display:none;border-color:var(--dn);color:var(--dn)"
              onclick="togglePool()">＋加入自选</button>
    </span>
    <span class="pill" id="chartSrc"></span>
  </h2>
  <div class="qbar" id="qbar" style="display:none"></div>
  <div id="chart"></div>
  <div class="lofcard" id="lofcard"></div>
  <div id="premChart" style="width:100%;height:220px;display:none;margin-top:10px"></div>
  <div style="margin-top:12px;border-top:1px dashed var(--bd);padding-top:10px">
    <h3 style="font-size:13px;margin-bottom:8px">金额模拟回测（底仓 + 九转加减仓）</h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--dim)" class="simbox">
      <label>初始资金(元) <input id="simInit" type="number" value="10000" min="1000" max="10000000" step="1000"
        onblur="snapK(this)" style="width:96px"></label>
      <label>回测区间
        <select id="simYears">
          <option value="0">全部</option>
          <option value="1">近1年</option>
          <option value="2">近2年</option>
          <option value="3" selected>近3年</option>
          <option value="5">近5年</option>
        </select></label>
      <label>日线低9买/高9卖 <input id="simUd" type="number" value="1000" min="0" max="1000000" step="1000"
        onblur="snapK(this)" style="width:72px">元</label>
      <label>周线 <input id="simUw" type="number" value="3000" min="0" max="1000000" step="1000"
        onblur="snapK(this)" style="width:72px">元</label>
      <label>月线 <input id="simUm" type="number" value="5000" min="0" max="1000000" step="1000"
        onblur="snapK(this)" style="width:72px">元</label>
      <button id="simBtn" onclick="runSim()">运行模拟</button>
    </div>
    <div id="simStats" style="display:none;gap:14px;flex-wrap:wrap;font-size:13px;margin:10px 0"></div>
    <div id="simChart" style="width:100%;height:260px;display:none"></div>
    <table id="simTbl" style="display:none"><thead><tr>
      <th>日期</th><th>方向</th><th>金额(元)</th><th>价格</th><th>信号</th><th>持仓市值(元)</th>
      <th>策略累计投入</th><th>策略累计收益</th><th>策略收益率(按初始资金)</th>
      <th>持有不动收益率</th><th>超额收益率</th>
    </tr></thead><tbody></tbody></table>
    <div class="pill" id="simNote">口径：初始资金首日全仓买入（初始建仓市值=初始资金，明细首行可见）；日线低9买/高9卖各1000元，周线3000元，月线5000元（同日信号金额合并，按收盘价折算份额成交；卖出持仓不足则清仓；买入允许现金为负=融资）。策略累计收益=权益-初始资金（买卖仅为股票/现金间转移，不含投入规模差异干扰）；所有收益率统一以初始资金为分母；超额收益率=策略收益率-持有不动收益率。折线图悬停可看当日资金结构。最近50笔（初始建仓行始终保留）。</div>
  </div>
</div>

<div class="cols2">
  <div class="card"><h2>信号记录（推送历史）</h2>
    <table id="sigTbl"><thead><tr><th>时间</th><th>标的</th><th>级别</th><th>方向</th></tr></thead>
    <tbody></tbody></table></div>
  <div class="card"><h2>数据 / 统计 / 扫描日志
      <a href="/ops" style="margin-left:auto;font-size:12px;color:var(--acc);text-decoration:none">在运维中心查看 →</a></h2>
    <div class="pill" style="line-height:2">
      数据来源降级顺序、各标的缓存状态、扫描日志、信号胜率统计（10日窗口）、全池九转回测<br>
      已集中至运维中心，保持看板清爽。</div>
  </div>
</div>

<div class="card"><h2>策略说明</h2>
<details open>
<summary style="cursor:pointer;color:var(--acc)">① 神奇九转（TD DeMark）</summary>
<pre style="max-height:none">计数规则：当日收盘 vs 4根前收盘，连续9根同向即完成结构。
  上涨结构（高9）→ 顶部衰竭，卖出信号；下跌结构（低9）→ 底部衰竭，买入信号。
极值验证(perfection)：第8/9根的极值需包住第6/7根，否则计数顺延，减少假信号。
计数≥6 开始在K线上标注（高转标上方/低转标下方），≥6 进入深度分析池。</pre>
</details>
<details>
<summary style="cursor:pointer;color:var(--acc)">② 六层信号过滤</summary>
<pre style="max-height:none">L1 趋势过滤   ADX&lt;40 且布林带宽&gt;5%：趋势过强/过窄时九转失效，直接降级。
L2 周期共振   周线九转同向 +2分；月线计数仅作背景参考。
L3 量价模型   放量上攻/缩量回调/滞涨放量/恐慌砸盘 四模型匹配加分。
L4 指标共振   MACD背离、RSI超买超卖、布林轨触碰、KDJ金叉死叉 共振加分。
L5 分级+止损  综合得分 S≥8 / A≥5 / B≥2 / C；止损=结构极值外2%。
L6 动态阈值   按市场状态(牛/熊/震)调整量比与RSI门槛：牛市宽松、熊市严格。</pre>
</details>
<details>
<summary style="cursor:pointer;color:var(--acc)">③ 量能三级过滤</summary>
<pre style="max-height:none">粗筛  量比≥2.0 或 换手率≥3倍20日均 或 成交额≥2.5倍20日均，任一命中。
分位  当日成交量进入60日窗口的90%分位以上。
精筛  量比≥1.5 确认有效放量；三级全过才判定"放量"，避免单日脉冲误判。</pre>
</details>
<details>
<summary style="cursor:pointer;color:var(--acc)">④ LOF 溢价监控（仅16/50真LOF）</summary>
<pre style="max-height:none">净值三级降级  IOPV(精度最高) &gt; 双口径估算净值 &gt; 昨官方净值。
双口径        官方(中间价汇率) vs 参考(离岸CNH)，两轨差=不确定性提示。
估算模型      净值 ≈ 昨净值 ×(1 + 底层资产涨跌×仓位系数 + 汇率变动×外币敞口)
提醒规则      溢价≥3%提醒 / ≥5%警告；折价≤-2%套利提醒；60日分位≥95%极端。
份额流向      高溢价 + 份额单日+5% → 套利盘进场，溢价收敛在即。
仓位自校准    官方净值披露后回测误差，自动修正仓位系数。
溢价历史      每日快照落库，分位优先用真实落库序列计算（下方走势图数据源）。</pre>
</details>
<details>
<summary style="cursor:pointer;color:var(--acc)">⑤ 信号分级与推送纪律</summary>
<pre style="max-height:none">S/A级直推微信（附K线卡：MA5/止损线/量比高亮/九转标注）；B级进晚报聚合。
冷却：同标的同方向两次推送间隔≥5个交易日；每日推送限额12条（系统告警除外）。
信号卡附同型历史胜率（10日窗口，n=样本数），回填自5/10/20日收益跟踪表。</pre>
</details>
<details>
<summary style="cursor:pointer;color:var(--acc)">⑥ 运行时序与告警</summary>
<pre style="max-height:none">09:35 市场状态判定（沪深300基准） → 10:30/14:00 盘中巡检 → 15:35 收盘全量扫描
→ 16:00 心跳检查（当日扫描缺失即告警）；扫描过半标的失败触发独立告警通道。</pre>
</details>
</div>

<div id="toast"></div>
<script>
let chart = echarts.init(document.getElementById('chart'));
let premChart = null;   // 惰性init：容器display:none时init宽度为0，首次渲染会挤成一团
let simChart = null;
window.onresize = () => { chart.resize(); if (premChart) premChart.resize(); if (simChart) simChart.resize(); };
let curCode = null, curName = '', curPeriod = 'day';
const fmtAmt = v => v == null ? '-' : v >= 1e8 ? (v/1e8).toFixed(1)+'亿' : v >= 1e4 ? (v/1e4).toFixed(0)+'万' : v;
const cls = v => v > 0 ? 'up' : v < 0 ? 'dn' : 'dim';
const sgn = v => v == null ? '-' : (v > 0 ? '+' : '') + v;
const mktSym = c => (c[0]=='6'||c[0]=='5'?'sh':'sz') + c;
const PERIOD_CN = {day:'日线', week:'周线', month:'月线'};

function toast(msg) { const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 5000); }

async function loadAll() {
  const [ov, sigs] = await Promise.all(
    ['/api/overview','/api/signals'].map(u => fetch(u).then(r => r.json())));
  document.getElementById('nStocks').textContent = ov.total;
  document.getElementById('poolCn').textContent = ov.pool_cn || '';
  document.getElementById('lastScan').textContent = (ov.last_scan || '未扫描').replace('T',' ');
  document.getElementById('srcNote').textContent = ov.source_note || '';
  document.getElementById('nSignals').textContent = ov.today_signals + ' / ' + ov.today_pushes;

  const gb = document.querySelector('#sigTbl tbody'); gb.innerHTML = '';
  // 方向以九转结构表述：高9=上涨结构完成(卖出信号/绿)，低9=下跌结构完成(买入信号/红)
  (sigs.signals || []).forEach(x => gb.insertAdjacentHTML('beforeend',
    `<tr><td class="dim">${x.push_time}</td><td>${x.name||''} ${x.code}</td>
     <td><span class="lv ${x.level}">${x.level}</span></td>
     <td class="${x.direction=='up'?'dn':'up'}" style="font-weight:600">${x.direction=='up'?'高9':'低9'}</td></tr>`));

  reloadRank();
  openFromQuery();   // 深链定位：/?code=600519&period=week 自动打开该标的
}

// 推送卡看板深链：?code=xxx&period=day|week|month（微信点开直达对应标的K线）
function openFromQuery() {
  const qs = new URLSearchParams(location.search);
  const code = (qs.get('code') || '').trim();
  const period = qs.get('period');
  if (!code || !/^\\d{6}$/.test(code)) return;
  if (period && PERIOD_CN[period]) setPeriod(period);
  const row = document.querySelector(`tr.row[data-code="${code}"]`);
  if (row) pick(row);                       // 自选池：带名称选中并联动排行高亮
  else loadCode(code, code);                // 非自选：在线拉取，名称暂显示代码
  window.scrollTo({top: document.getElementById('chart').offsetTop - 80, behavior: 'smooth'});
}

// 排序键 → 依据列取值（列头动态显示当前排序指标）
const BASIS = {
  vol_ratio:        s => s.vol_ratio != null ? s.vol_ratio.toFixed(2) : '-',
  vol_ratio_period: s => s.vol_ratio_period != null ? s.vol_ratio_period.toFixed(2) : '-',
  amt_ratio:        s => s.amt_ratio != null ? s.amt_ratio.toFixed(2) : '-',
  amount:           s => s.amount != null ? fmtAmt(s.amount) : '-',
  premium:          s => s.premium != null ? sgn(s.premium) + s.premium.toFixed(2) + '%' : '-',
  turn_abs:         s => { const t = s.turn_count || 0; return t ? (t > 0 ? '高' : '低') + Math.abs(t) : '-'; },
  pct_chg:          s => s.pct_chg != null ? sgn(s.pct_chg) + s.pct_chg.toFixed(2) + '%' : '-'
};
const BASIS_CLS = new Set(['premium', 'pct_chg']);  // 这些依据带涨跌方向，用红绿着色

// 金额输入就近取整到1000
function snapK(el) { el.value = Math.max(0, Math.round((+el.value || 0) / 1000) * 1000); }

// 排行分类：按代码前缀区分 股票/ETF/LOF
const rankType = c => /^(51|52|55|56|58|15)/.test(c) ? 'etf' : /^(16|50)/.test(c) ? 'lof' : 'stock';
let rankTab = 'stock', rankRows = [];
const rT = {w8: false, w9: false, m8: false, m9: false};   // 九转筛选开关（或关系）

function setRankTab(t) {
  rankTab = t;
  ['stock', 'etf', 'lof'].forEach(k =>
    document.getElementById('rt' + k[0].toUpperCase() + k.slice(1)).classList.remove('on'));
  document.getElementById('rt' + t[0].toUpperCase() + t.slice(1)).classList.add('on');
  renderRankRows();
}

// 九转筛选：周八/周九/月八/月九独立开关（或关系；8=结构进行中，9=结构完成，取自周/月快照）
function toggleRankTurn(k) {
  rT[k] = !rT[k];
  document.getElementById('rt' + k[0].toUpperCase() + k[1]).classList.toggle('on', rT[k]);
  renderRankRows();
}
const rankTurnHit = s => (rT.w8 && Math.abs(s.turn_week || 0) === 8)
  || (rT.w9 && Math.abs(s.turn_week || 0) === 9)
  || (rT.m8 && Math.abs(s.turn_month || 0) === 8)
  || (rT.m9 && Math.abs(s.turn_month || 0) === 9);

function renderRankRows() {
  const key = document.getElementById('selKey').value;
  const basis = BASIS[key] || BASIS.vol_ratio;
  const tb = document.querySelector('#tbl tbody'); tb.innerHTML = '';
  const anyT = rT.w8 || rT.w9 || rT.m8 || rT.m9;
  const rows = rankRows.filter(s => rankType(String(s.code)) === rankTab
    && (!anyT || rankTurnHit(s)));
  const cnt = {stock: 0, etf: 0, lof: 0};
  rankRows.forEach(s => cnt[rankType(String(s.code))]++);
  document.getElementById('rtStock').textContent = `股票(${cnt.stock})`;
  document.getElementById('rtEtf').textContent = `ETF(${cnt.etf})`;
  document.getElementById('rtLof').textContent = `LOF(${cnt.lof})`;
  rows.forEach(s => {
    const tc = s.turn_count || 0;
    tb.insertAdjacentHTML('beforeend', `<tr class="row ${s.code===curCode?'sel':''}" data-code="${s.code}" data-name="${s.name||''}" onclick="pick(this)">
      <td style="white-space:nowrap"><span class="delpool" title="移出自选池"
        onclick="event.stopPropagation();poolDel('${s.code}','${(s.name||'').replace(/'/g,'')}')">−</span>${(s.name || '')}-${s.code}</td>
      <td class="${BASIS_CLS.has(key) ? cls(key==='premium' ? s.premium : s.pct_chg) : ''}" style="font-weight:600">${basis(s)}</td>
      <td>${s.close != null ? s.close.toFixed(2) : '-'}</td>
      <td class="${cls(s.pct_chg)}">${sgn(s.pct_chg)}${s.pct_chg!=null?'%':''}</td>
      <td>${s.vol_ratio ?? '-'}</td><td>${fmtAmt(s.amount)}</td>
      <td class="${cls(tc)}">${tc ? (tc>0?'高':'低')+Math.abs(tc)+(s.turn_complete?'✓':'') : '-'}</td>
      <td class="${cls(s.premium)}">${s.premium!=null?sgn(s.premium)+'%':'-'}</td>
      <td>${s.surge_type || '-'}</td>
      <td><a href="https://gu.qq.com/${mktSym(s.code)}" target="_blank">查看</a></td>
    </tr>`);
  });
}

async function reloadRank() {
  const period = document.getElementById('selPeriod').value;
  const key = document.getElementById('selKey').value;
  const keyLabel = document.querySelector(`#selKey option[value=${key}]`).textContent;
  const d = await fetch(`/api/rank?key=${key}&period=${period}`).then(r => r.json());
  document.getElementById('rankLabel').textContent = PERIOD_CN[period] + '·' + keyLabel;
  document.getElementById('thBasis').textContent = '依据---' + keyLabel;
  rankRows = d.rows || [];
  renderRankRows();
}

function setPeriod(p) {
  curPeriod = p;
  ['pd','pw','pm'].forEach(id => document.getElementById(id).classList.remove('on'));
  document.getElementById(p[0] === 'd' ? 'pd' : p[0] === 'w' ? 'pw' : 'pm').classList.add('on');
  if (curCode) loadCode(curCode, curName);
}

function pick(tr) { loadCode(tr.dataset.code, tr.dataset.name); }

async function loadCode(code, name) {
  curCode = code; curName = name || code;
  document.querySelectorAll('tr.row').forEach(r =>
    r.classList.toggle('sel', r.dataset.code === code));
  // 隐藏上一标的的模拟结果（需重跑）
  document.getElementById('simChart').style.display = 'none';
  document.getElementById('simTbl').style.display = 'none';
  document.getElementById('simStats').style.display = 'none';
  document.getElementById('chartTitle').textContent =
    `K线 · ${curName} ${code} · ${PERIOD_CN[curPeriod]}（九转≥6标注）`;
  const d = await fetch(`/api/kline/${code}?period=${curPeriod}`).then(r => r.json());
  document.getElementById('chartSrc').textContent = d.source ? `数据来源：${d.source}` : '';
  renderQbar(d.quote);                 // 信息条：收盘/涨跌/量比/成交额/九转
  renderPoolBtn(d.in_pool, name);      // 自选池按钮
  const lofEl = document.getElementById('lofcard');
  const premEl = document.getElementById('premChart');
  if (d.is_lof) {
    const st = await fetch(`/api/lof/${code}`).then(r => r.json()).catch(() => null);
    if (st && st.card) { lofEl.textContent = st.card; lofEl.style.display = 'block'; }
    loadPremium(code, name || code);   // 溢价走势图（落库序列优先）
  } else { lofEl.style.display = 'none'; premEl.style.display = 'none'; if (premChart) premChart.clear(); }
  if (!d.dates || !d.dates.length) { chart.clear(); toast(`${code} 无K线数据`); return; }
  const n = d.dates.length;
  const marks = [];
  d.turns.forEach((t, i) => { if (Math.abs(t) >= 6)
    marks.push({ coord: [i, d.ohlc[i][3]],
      label: { show:true, formatter:String(Math.abs(t)), position: t > 0 ? 'top' : 'bottom',
               color: t > 0 ? '#f85149' : '#3fb950', fontSize:11, fontWeight:600 } }); });
  const fmtVol = v => v == null ? '-' : v >= 1e8 ? (v/1e8).toFixed(1)+'亿'
                       : v >= 1e4 ? (v/1e4).toFixed(0)+'万' : String(Math.round(v));
  const MA_STYLE = {ma5:'#d29922', ma10:'#bc8cff', ma20:'#58a6ff', ma60:'#8b949e'};
  const maSeries = Object.entries(d.mas || {}).filter(([,v]) => v && v.some(x => x != null))
    .map(([k, v]) => ({ type:'line', data:v, symbol:'none', smooth:true,
      lineStyle:{width:1.2, color:MA_STYLE[k] || '#8b949e'},
      itemStyle:{color:MA_STYLE[k] || '#8b949e'}, name:k.toUpperCase() }));
  chart.setOption({
    backgroundColor:'transparent',
    legend: maSeries.length ? {data: maSeries.map(s => s.name), top:0, right:20,
      textStyle:{color:'#8b949e', fontSize:11}, itemWidth:14} : undefined,
    tooltip:{trigger:'axis', axisPointer:{type:'cross'},
      formatter: ps => { const k = ps[0]; const c = d.ohlc[k.dataIndex];
        return `${k.name}<br/>开${c[0]} 收${c[1]} 低${c[2]} 高${c[3]}<br/>量 ${fmtVol(d.vols[k.dataIndex])}`; }},
    grid:[{left:60,right:20,top:maSeries.length?28:20,height:maSeries.length?222:230},
          {left:60,right:20,top:280,height:60}],
    xAxis:[{type:'category',data:d.dates,boundaryGap:true,axisLine:{lineStyle:{color:'#30363d'}}},
           {type:'category',gridIndex:1,data:d.dates,axisLabel:{show:false}}],
    yAxis:[{scale:true,splitLine:{lineStyle:{color:'#21262d'}}},
           {gridIndex:1,scale:true,splitLine:{show:false},
            axisLabel:{formatter:fmtVol, color:'#8b949e', fontSize:11}}],
    series:[
      { type:'candlestick', data:d.ohlc,
        itemStyle:{color:'#f85149',color0:'#3fb950',borderColor:'#f85149',borderColor0:'#3fb950'},
        markPoint:{data:marks,animation:false,symbol:'circle',symbolSize:0} },
      ...maSeries,
      { type:'bar', xAxisIndex:1, yAxisIndex:1, data:d.vols,
        itemStyle:{color:p => p.dataIndex === n-1 ? '#d29922' : '#4d5661'} }
    ]
  }, true);
}

// 金额模拟回测：初始资金全仓买入 + 多周期九转按金额加减仓，权益折线 vs 买入持有
const fmtY = v => (v==null?'-':'¥' + Math.round(v).toLocaleString());

async function runSim() {
  if (!curCode) { toast('请先在排行中点击一个标的'); return; }
  const btn = document.getElementById('simBtn');
  btn.disabled = true; btn.textContent = '模拟中…';
  try {
    const q = `initial=${+document.getElementById('simInit').value||10000}` +
              `&years=${+document.getElementById('simYears').value||0}` +
              `&ud=${+document.getElementById('simUd').value||0}` +
              `&uw=${+document.getElementById('simUw').value||0}` +
              `&um=${+document.getElementById('simUm').value||0}`;
    const s = await fetch(`/api/backtest_sim/${curCode}?${q}`).then(r => r.ok ? r.json() : null);
    if (!s || s.error) { toast('模拟失败：' + (s && s.error || '无数据')); return; }
    const st = s.stats;
    const kpi = (k, v, c='') => `<span>${k}：<b class="${c}">${v}</b></span>`;
    document.getElementById('simStats').style.display = 'flex';
    document.getElementById('simStats').innerHTML =
      kpi('区间收益', sgn(st.total_ret)+'%', cls(st.total_ret)) +
      kpi('买入持有', sgn(st.buy_hold_ret)+'%', cls(st.buy_hold_ret)) +
      kpi('超额', sgn(st.excess)+'%', cls(st.excess)) +
      kpi('年化', st.annual_ret==null?'-':sgn(st.annual_ret)+'%', cls(st.annual_ret)) +
      kpi('最大回撤', '-'+st.max_drawdown+'%', 'dn') +
      kpi('交易', st.n_trades+'次') +
      kpi('初始资金', fmtY(s.params.initial)) +
      kpi('初始建仓市值', fmtY(st.initial_mv)) +
      kpi('累计投入', fmtY(st.invested_end)) +
      kpi('累计收益', fmtY(st.pnl_end), cls(st.pnl_end)) +
      kpi('期末', `股票${fmtY(st.mv_end)}/现金${fmtY(st.cash_end)}`) +
      kpi('期末权益', fmtY(st.equity_end), cls(st.equity_end - s.params.initial)) +
      (s.margin_used ? '<span class="dn">⚠ 动用融资</span>' : '') +
      `<span class="dim">${s.name} ${st.n_days}个交易日</span>`;
    // 权益折线：模拟 vs 买入持有 + 买卖点标注（红▲买 绿▼卖），悬停展示资金结构
    const el = document.getElementById('simChart');
    el.style.display = 'block';
    simChart = simChart || echarts.init(el);   // 可见后再init，宽度才正确
    const dates = s.curve.map(p => p.date);
    const byDate = Object.fromEntries(s.curve.map(p => [p.date, p]));
    const mk = s.marks || [];
    const buys = mk.filter(m => m.action === 'buy');
    const sells = mk.filter(m => m.action === 'sell');
    const mkTip = ms => ps => { const p = ps[0];
      const q = byDate[p.name] || {};
      const b = buys.filter(m => m.date === p.name);
      const sl = sells.filter(m => m.date === p.name);
      return `${p.name}<br/>` +
        (b.length ? `<span style="color:#f85149">▲ 买入 ${fmtY(b.reduce((a,m)=>a+m.amount,0))}</span><br/>` : '') +
        (sl.length ? `<span style="color:#3fb950">▼ 卖出 ${fmtY(sl.reduce((a,m)=>a+m.amount,0))}</span><br/>` : '') +
        `权益 ${fmtY(q.equity)}｜累计收益率 ${p.value == null ? '-' : p.value.toFixed(2)}%（按初始资金）<br/>` +
        `累计投入 ${fmtY(q.invested)}｜累计收益 ${fmtY(q.pnl)}<br/>` +
        `初始资金 ${fmtY(s.params.initial)}｜股票市值 ${fmtY(q.mv)}<br/>现金 ${fmtY(q.cash)}`; };
    simChart.setOption({
      backgroundColor:'transparent',
      tooltip:{trigger:'axis', formatter: mkTip(mk)},
      legend:{data:['金额模拟','买入持有','买入','卖出'], top:0, textStyle:{color:'#8b949e', fontSize:11},
              itemWidth:14},
      grid:{left:56,right:16,top:26,bottom:24},
      xAxis:{type:'category', data:dates, axisLabel:{color:'#8b949e', fontSize:10}},
      yAxis:{type:'value', axisLabel:{formatter:'{value}%', color:'#8b949e', fontSize:10},
             splitLine:{lineStyle:{color:'#21262d'}}},
      series:[
        {name:'金额模拟', type:'line', data:s.curve.map(p => p.ret), symbol:'none',
         lineStyle:{width:1.8, color:'#d29922'}, itemStyle:{color:'#d29922'},
         areaStyle:{color:'rgba(210,153,34,.08)'},
         z:3},
        {name:'买入持有', type:'line', data:s.bh_curve.map(p => p.ret), symbol:'none',
         lineStyle:{width:1.2, color:'#58a6ff'}, itemStyle:{color:'#58a6ff'}},
        {name:'买入', type:'scatter', data:buys.map(m => [m.date, m.ret]),
         symbol:'triangle', symbolSize:11, symbolOffset:[0,'-55%'],
         itemStyle:{color:'#f85149', borderColor:'#0d1117', borderWidth:1}, z:5},
        {name:'卖出', type:'scatter', data:sells.map(m => [m.date, m.ret]),
         symbol:'triangle', symbolSize:11, symbolRotate:180, symbolOffset:[0,'55%'],
         itemStyle:{color:'#3fb950', borderColor:'#0d1117', borderWidth:1}, z:5}
      ]
    }, true);
    // 交易明细（金额口径：不看数量，持仓显示市值）
    const tb = document.querySelector('#simTbl tbody'); tb.innerHTML = '';
    document.getElementById('simTbl').style.display = 'table';
    [...s.trades].reverse().forEach(t => tb.insertAdjacentHTML('beforeend',
      `<tr><td class="dim">${t.date}</td>
       <td class="${t.action==='buy'?'dn':'up'}">${t.action==='buy'?'买入':'卖出'}</td>
       <td>${t.amount.toLocaleString()}</td><td>${t.price}</td>
       <td class="dim">${t.why}</td>
       <td>${fmtY(t.mv_after)}</td>
       <td>${fmtY(t.invested_after)}</td>
       <td class="${cls(t.pnl_after)}">${fmtY(t.pnl_after)}</td>
       <td class="${cls(t.ret_after)}">${sgn(t.ret_after)}%</td>
       <td class="${cls(t.bh_ret_after)}">${sgn(t.bh_ret_after)}%</td>
       <td class="${cls(t.excess_after)}">${sgn(t.excess_after)}%</td></tr>`));
    toast('模拟完成');
  } catch (e) { toast('模拟失败：' + e); }
  btn.disabled = false; btn.textContent = '运行模拟';
}

// K线信息条：收盘/涨跌/量比/成交额/九转
function renderQbar(q) {
  const el = document.getElementById('qbar');
  if (!q) { el.style.display = 'none'; return; }
  const tc = q.turn_count || 0;
  const item = (k, v, c='') => `<span class="qi"><span class="qk">${k}</span><span class="qv ${c}">${v}</span></span>`;
  el.innerHTML =
    item('收盘', q.close != null ? q.close.toFixed(2) : '-') +
    item('涨跌', q.pct_chg != null ? sgn(q.pct_chg) + q.pct_chg.toFixed(2) + '%' : '-', cls(q.pct_chg)) +
    item('量比', q.vol_ratio != null ? q.vol_ratio.toFixed(2) : '-') +
    item('成交额', fmtAmt(q.amount)) +
    item('九转', tc ? (tc>0?'高':'低') + Math.abs(tc) + (q.turn_complete?' ✓完成':'') : '-', cls(tc)) +
    `<span class="qi"><span class="qk">日期</span><span class="qv dim">${String(q.date).slice(0,10)}</span></span>`;
  el.style.display = 'flex';
}

// 自选池按钮（加入/移出）
let inPool = false;
function renderPoolBtn(v, name) {
  inPool = v;
  const b = document.getElementById('poolBtn');
  b.style.display = 'inline-block';
  b.textContent = v ? '✓ 已在自选 · 点击移出' : '＋加入自选';
  b.style.borderColor = v ? 'var(--warn)' : 'var(--dn)';
  b.style.color = v ? 'var(--warn)' : 'var(--dn)';
}
async function togglePool() {
  if (!curCode) return;
  const r = await fetch('/api/pool', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action: inPool ? 'remove' : 'add', code: curCode, name: curName})})
    .then(x => x.json()).catch(() => ({error:'请求失败'}));
  if (r.error) { toast('操作失败：' + r.error); return; }
  toast(r.msg || '操作成功');
  renderPoolBtn(!inPool, curName);
  loadAll();   // 刷新概览/排行（监控标的数变化）
}

// 排行行内−号：直接移出自选池
async function poolDel(code, name) {
  if (!confirm(`确认将 ${name || code} ${code} 移出自选池？`)) return;
  const r = await fetch('/api/pool', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'remove', code, name})})
    .then(x => x.json()).catch(() => ({error:'请求失败'}));
  if (r.error) { toast('移出失败：' + r.error); return; }
  toast(r.msg || '已移出自选池');
  if (code === curCode) renderPoolBtn(false, name);
  loadAll();   // 刷新概览/排行
}

let tmr = null;
async function loadPremium(code, name) {
  const el = document.getElementById('premChart');
  const d = await fetch(`/api/premium/${code}`).then(r => r.json()).catch(() => null);
  const rows = (d && d.rows) || [];
  if (!rows.length) { el.style.display = 'none'; return; }
  el.style.display = 'block';
  premChart = premChart || echarts.init(el);   // 可见后再init，宽度才正确
  const dates = rows.map(r => String(r.date).slice(5));
  const prem = rows.map(r => r.premium_official);
  const markLine = { silent:true, symbol:'none', lineStyle:{type:'dashed',color:'#8b949e'},
                     label:{show:true,position:'insideEndTop',fontSize:10,color:'#8b949e'},
                     data:[{yAxis:0,label:{formatter:'0%'}},
                           {yAxis:3,label:{formatter:'+3%'}},
                           {yAxis:-2,label:{formatter:'-2%'}}] };
  premChart.setOption({
    backgroundColor:'transparent',
    title:{text:`溢价走势(T-1口径) · ${name} ${code}（近${rows.length}日）`, textStyle:{color:'#58a6ff',fontSize:13}, left:0},
    tooltip:{trigger:'axis', valueFormatter:v=>v==null?'-':v.toFixed(2)+'%'},
    grid:{left:50,right:20,top:34,height:140},
    xAxis:{type:'category',data:dates,axisLine:{lineStyle:{color:'#30363d'}},axisLabel:{color:'#8b949e'}},
    yAxis:{scale:true,splitLine:{lineStyle:{color:'#21262d'}},axisLabel:{formatter:'{value}%',color:'#8b949e'}},
    series:[{type:'line',data:prem,smooth:true,symbol:'none',
             lineStyle:{color:'#d29922',width:1.6},
             areaStyle:{color:{type:'linear',x:0,y:0,x2:0,y2:1,
               colorStops:[{offset:0,color:'rgba(210,153,34,.35)'},{offset:1,color:'rgba(210,153,34,0)'}]}},
             markLine}]
  }, true);
}

async function suggest() {
  clearTimeout(tmr);
  const q = document.getElementById('q').value.trim();
  const drop = document.getElementById('drop');
  if (!q) { drop.style.display = 'none'; return; }
  tmr = setTimeout(async () => {
    const d = await fetch('/api/search?q=' + encodeURIComponent(q)).then(r => r.json()).catch(() => ({items:[]}));
    drop.innerHTML = (d.items || []).map(x =>
      `<div style="display:flex;align-items:center;gap:6px">
         <span style="flex:1;cursor:pointer" onclick="loadCode('${x.code}','${(x.name||'').replace(/'/g,'')}');document.getElementById('drop').style.display='none'">
           ${x.name} <span class="dim">${x.code}</span> <span class="tag">${x.type}</span></span>
         ${x.in_pool
           ? '<span class="tag live">自选</span>'
           : `<button style="padding:2px 8px;font-size:12px" onclick="poolAddQuick('${x.code}','${(x.name||'').replace(/'/g,'')}')">＋自选</button>`}
       </div>`).join('')
      || '<div class="dim">无匹配</div>';
    drop.style.display = 'block';
  }, 300);
}
async function poolAddQuick(code, name) {
  const r = await fetch('/api/pool', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'add', code, name})}).then(x => x.json()).catch(() => ({error:'失败'}));
  toast(r.msg || r.error);
  if (r.ok) suggest();
}
document.addEventListener('click', e => {
  if (!e.target.closest('.search')) document.getElementById('drop').style.display = 'none';
});

loadAll();

// 运维中心浮层：iframe隔离，开关不影响主页状态
function toggleOps() {
  const ov = document.getElementById('opsOverlay');
  const on = ov.classList.toggle('show');
  if (on) document.getElementById('opsFrame').contentWindow?.postMessage('refresh', '*');
}
</script>
<div id="opsOverlay" onclick="if(event.target===this)toggleOps()">
  <div id="opsFrameBox">
    <button id="opsClose" onclick="toggleOps()">✕ 关闭</button>
    <iframe id="opsFrame" src="/ops" title="运维中心"></iframe>
  </div>
</div>
</body>
</html>
"""


_OPS_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>运维中心 · A股监控台</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --bd:#30363d; --fg:#e6edf3; --dim:#8b949e;
          --up:#f85149; --dn:#3fb950; --acc:#58a6ff; --warn:#d29922; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.6 "Microsoft YaHei",sans-serif; padding:20px; }
  h1 { font-size:20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:16px; }
  .toolbar { margin-left:auto; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
  a.back { font-size:13px; color:var(--acc); text-decoration:none; border:1px solid var(--bd);
           padding:5px 12px; border-radius:6px; background:#21262d; }
  button { background:#21262d; color:var(--fg); border:1px solid var(--bd); border-radius:6px;
           padding:8px 18px; cursor:pointer; font-size:14px; }
  button:hover { border-color:var(--acc); color:var(--acc); }
  button:disabled { opacity:.5; cursor:wait; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:8px;
          padding:16px; margin-bottom:16px; }
  .card h2 { font-size:15px; margin-bottom:12px; color:var(--acc); }
  .ops-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
  .ops-layout { display:grid; grid-template-columns:1fr 560px; gap:12px; align-items:start; }
  .ops-right { position:sticky; top:12px; }
  .ops-right pre { max-height:none; min-height:70vh; overflow:visible; }
  .ops-item { background:#0d1117; border:1px solid var(--bd); border-radius:8px; padding:14px; }
  .ops-item h3 { font-size:14px; margin-bottom:6px; }
  .ops-item p { color:var(--dim); font-size:12px; margin-bottom:10px; }
  pre { background:#0d1117; border:1px solid var(--bd); border-radius:6px; padding:10px;
        font-size:12px; overflow:auto; max-height:300px; white-space:pre-wrap; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--dim); font-weight:500; padding:6px 10px; border-bottom:1px solid var(--bd); }
  td { padding:6px 10px; border-bottom:1px solid #21262d; white-space:nowrap; }
  .dim { color:var(--dim); }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--dim); margin-right:6px; }
  .dot.on { background:var(--dn); }
  .src-item { display:flex; gap:8px; align-items:center; padding:3px 0; font-size:13px; }
  #toast { position:fixed; right:20px; bottom:20px; background:#21262d; border:1px solid var(--bd);
           border-radius:8px; padding:10px 18px; display:none; z-index:99; }
  @media (max-width:800px){ .ops-grid { grid-template-columns:1fr; }
    .ops-layout { grid-template-columns:1fr; } .ops-right { position:static; } }
</style>
</head>
<body>
<h1>⚙ 运维中心
  <span class="toolbar"><a class="back" href="/">‹ 返回看板</a></span>
</h1>

<div class="ops-layout">
<div class="ops-left">

<div class="card">
  <h2>功能操作</h2>
  <div class="ops-grid">
    <div class="ops-item">
      <h3>立即扫描</h3>
      <p>联网收盘全量扫描：拉K线→九转→量能→LOF溢价→信号推送→落库。</p>
      <button id="scanBtn" onclick="doScan(this)">立即扫描</button>
    </div>
    <div class="ops-item">
      <h3>离线演示</h3>
      <p>确定性合成数据跑完整管线（独立演示库，不污染实盘缓存）。</p>
      <button id="dmBtn" onclick="doDemo(this)">离线演示扫描</button>
    </div>
    <div class="ops-item">
      <h3>心跳检查</h3>
      <p>校验当日扫描是否按时执行；缺失即触发系统告警。</p>
      <button id="hbBtn" onclick="doHeartbeat(this)">心跳检查</button>
    </div>
    <div class="ops-item">
      <h3>功能自测</h3>
      <p>九转/重采样/指标/量能/信号/LOF/排序/端到端全链路回归。</p>
      <button id="stBtn" onclick="runSelftest(this)">运行自测(约10秒)</button>
    </div>
    <div class="ops-item" style="grid-column:1/-1">
      <h3>全池九转回测</h3>
      <p>对自选池全部标的做"完成九转(±9)"信号回测：信号后5/10/20日收益、胜率汇总（日线口径）。</p>
      <button id="btBtn" onclick="runBacktestAll(this)">全池回测</button>
      <pre id="btOut" style="display:none;margin-top:10px"></pre>
    </div>
    <div class="ops-item" style="grid-column:1/-1">
      <h3>强制推送测试</h3>
      <p>绕过分级/去重/每日限额，把当前满足条件的标的直推企业微信验证通道（标记[TEST]，不写推送历史、不占限额）。九转测试与正式推送同格式：🆕新增/⏳原有分组、日┃周┃月分割线。</p>
      <button onclick="doPushTest('nine', this)">推送·九转满足标的</button>
      <button onclick="doPushTest('premium', this)">推送·LOF溢价满足标的</button>
      <pre id="ptOut" style="display:none;margin-top:10px"></pre>
    </div>
  </div>
</div>

<div class="card">
  <h2>数据库查询（指定标的）</h2>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
    <input id="dbq" placeholder="输入 6位代码 或 名称（如 000001 / 平安银行）"
           style="flex:1;min-width:220px;background:#0d1117;color:var(--fg);
                  border:1px solid var(--bd);border-radius:6px;padding:8px 12px;font-size:14px"
           onkeydown="if(event.key==='Enter')dbQuery()">
    <button onclick="dbQuery()">查询</button>
  </div>
  <div id="dbOut" class="dim" style="font-size:13px">查询 kline_day / 九转状态 / 指标快照(日/周/月) / 推送历史 / 信号跟踪 / 溢价历史 / 净值参数。</div>
</div>

<div class="card">
  <h2>任务输出（最近一次）</h2>
  <pre id="hbOut" style="display:none"></pre>
  <pre id="stOut">自测覆盖：九转/重采样/指标/量能/信号/LOF/排序/网址/端到端/推送图片增强/推送新增原有与日周月分割线。</pre>
</div>

<div class="ops-grid">
  <div class="ops-item">
    <h3 style="color:var(--acc)">数据来源</h3>
    <p>拉取顺序（依次降级）：</p>
    <div id="srcOrder" style="margin:6px 0 10px;font-size:13px"></div>
  </div>
  <div class="ops-item">
    <h3 style="color:var(--acc)">信号胜率统计（10日窗口）</h3>
    <div id="stats" style="font-size:13px;line-height:2">-</div>
  </div>
</div>

<div class="card">
  <h2>扫描日志</h2>
  <table id="logTbl"><thead><tr><th>时间</th><th>信号</th><th>错误</th><th>备注</th></tr></thead>
  <tbody></tbody></table>
</div>

</div>
<div class="ops-right">
  <div class="card">
    <h2>运行日志（本页触发的全部任务）</h2>
    <pre id="runLog">（暂无，触发左侧任务后显示）</pre>
  </div>
</div>
</div>

<div id="toast"></div>
<script>
function toast(msg) { const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 6000); }

async function doScan(btn) {
  btn.disabled = true; btn.textContent = '扫描中…';
  try {
    const r = await fetch('/api/scan', {method:'POST'}).then(x => x.json());
    toast(`扫描完成：标的${r.total} 信号${r.signals} 错误${r.errors}（${r.source}）`);
    loadRunLog();
  } catch (e) { toast('扫描失败：' + e); }
  btn.disabled = false; btn.textContent = '立即扫描';
}

async function doDemo(btn) {
  btn.disabled = true; btn.textContent = '演示中…';
  try {
    const r = await fetch('/api/demo', {method:'POST'}).then(x => x.json());
    toast(`离线演示完成：信号${r.signals} 错误${r.errors}（输出见运行日志）`);
    loadRunLog();
  } catch (e) { toast('演示失败：' + e); }
  btn.disabled = false; btn.textContent = '离线演示扫描';
}

async function doHeartbeat(btn) {
  btn.disabled = true; btn.textContent = '检查中…';
  try {
    const r = await fetch('/api/heartbeat', {method:'POST'}).then(x => x.json());
    const el = document.getElementById('hbOut');
    el.style.display = 'block';
    el.textContent = (r.output || '') + '\\n结论：' + (r.ok ? '✓ 正常' : '✗ 异常：' + (r.detail || ''));
    loadRunLog();
  } catch (e) { toast('心跳失败：' + e); }
  btn.disabled = false; btn.textContent = '心跳检查';
}

async function runSelftest(btn) {
  btn.disabled = true; btn.textContent = '自测中…';
  try {
    const r = await fetch('/api/selftest', {method:'POST'}).then(x => x.json());
    document.getElementById('stOut').textContent = r.output || '';
    toast(`自测完成：${r.summary || ''} 退出码 ${r.exit_code}`);
    loadRunLog();
  } catch (e) { toast('自测失败：' + e); }
  btn.disabled = false; btn.textContent = '运行自测(约10秒)';
}

async function doPushTest(kind, btn) {
  btn.disabled = true; const old = btn.textContent; btn.textContent = '推送中…';
  try {
    const r = await fetch('/api/ops/push_test', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({kind})})
      .then(x => x.json());
    const el = document.getElementById('ptOut');
    el.style.display = 'block';
    if (r.error) { el.textContent = r.error; toast(r.error); }
    else {
      const icon = r.ok ? '✓' : '✗';
      const head = `${icon} ${r.msg}` + (r.errors ? `\\n  ↳ ${r.errors}` : '');
      const rows = (r.items || []).map(i => {
        const v = i.tags || (i.premium != null ? `溢价${i.premium > 0 ? '+' : ''}${i.premium}%` : '');
        return `  ${i.name} ${i.code}  ${v}`;
      }).join('\\n');
      el.textContent = head + '\\n' + (rows ? `满足标的 ${r.items.length} 只：\\n${rows}` : '（无满足条件的标的，仅推送了通道验证消息）');
      toast(r.msg);
    }
  } catch (e) { toast('推送测试失败：' + e); }
  btn.disabled = false; btn.textContent = old;
}

async function runBacktestAll(btn) {
  btn.disabled = true; btn.textContent = '回测中…';
  try {
    const r = await fetch('/api/backtest_all', {method:'POST'}).then(x => x.json());
    const d = await fetch('/api/runlog').then(x => x.json());
    const el = document.getElementById('btOut');
    el.style.display = 'block';
    el.textContent = (d.backtest || '').split('────').pop() || '完成';
    const s = r.summary || {};
    const parts = [];
    if (s.buy) parts.push(`买入(低9) ${s.buy.stocks}只/${s.buy.total_signals}信号 胜率均值${(s.buy.avg_win_rate_10d*100).toFixed(0)}%`);
    if (s.sell) parts.push(`卖出(高9) ${s.sell.stocks}只/${s.sell.total_signals}信号 胜率均值${(s.sell.avg_win_rate_10d*100).toFixed(0)}%`);
    toast('全池回测完成：' + (parts.join(' ｜ ') || '无有效样本'));
    loadRunLog();
  } catch (e) { toast('回测失败：' + e); }
  btn.disabled = false; btn.textContent = '全池回测';
}

// ---------- 数据库查询（指定标的） ----------
const dbTbl = (title, cols, rows) => {
  if (!rows || !rows.length) return '';
  const th = cols.map(c => `<th>${c[1]}</th>`).join('');
  const tr = rows.map(r => `<tr>${cols.map(c => {
    let v = r[c[0]];
    if (v == null) v = '-';
    else if (typeof v === 'number') v = Math.round(v * 100) / 100;
    return `<td>${v}</td>`; }).join('')}</tr>`).join('');
  return `<div style="margin-top:12px"><h3 style="color:var(--acc);font-size:13px">${title}（${rows.length}行）</h3>
    <div style="overflow:auto"><table><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div></div>`;
};

async function dbQuery() {
  const q = document.getElementById('dbq').value.trim();
  const out = document.getElementById('dbOut');
  if (!q) { out.className = ''; out.textContent = '请输入代码或名称'; return; }
  out.className = 'dim'; out.textContent = '查询中…';
  try {
    const d = await fetch('/api/ops/db?q=' + encodeURIComponent(q)).then(r => r.json());
    if (d.error) { out.className = ''; out.textContent = '查询失败：' + d.error; return; }
    const rp = d.report, k = rp.kline;
    const PERIOD_CN = {day: '日', week: '周', month: '月'};
    out.className = '';
    out.innerHTML =
      `<h3 style="color:var(--acc);font-size:14px">${d.name} ${d.code}</h3>
       <p class="dim" style="margin:4px 0 2px">K线库：${k.count} 根（${k.first || '-'} ~ ${k.last || '-'}）` +
      (rp.fund_params.length ? `｜仓位系数 ${rp.fund_params[0].position}（误差 ${rp.fund_params[0].last_error}，${rp.fund_params[0].updated_at}）` : '') + `</p>` +
      (rp.stock_state.length ? (() => { const s = rp.stock_state[0];
        return `<p class="dim" style="margin:2px 0">九转状态：${s.turn_count > 0 ? '高' : '低'}${Math.abs(s.turn_count)}（${s.direction}，更新 ${s.last_update}）</p>`; })() : '') +
      dbTbl('K线最近10根', [['date','日期'],['open','开'],['high','高'],['low','低'],['close','收'],
        ['volume','量'],['amount','额']], k.latest) +
      dbTbl('指标快照（日/周/月）', [['period','周期'],['trade_date','交易日'],['turn_count','九转'],
        ['turn_complete','完成'],['vol_ratio','量比'],['amt_ratio','额比'],['amount','成交额'],
        ['premium','溢价%'],['pct_chg','涨跌%'],['close','收盘'],['snapshot_time','快照时间']],
        rp.snapshots.map(s => ({...s, period: PERIOD_CN[s.period] || s.period}))) +
      dbTbl('推送历史（近20条）', [['direction','方向'],['trade_date','交易日'],['level','级别'],['push_time','推送时间']],
        rp.push_history.map(p => ({...p, direction: p.direction === 'up' ? '高9' : '低9'}))) +
      dbTbl('信号跟踪（近20条）', [['level','级别'],['action','动作'],['signal_date','信号日'],['ref_close','基准收'],
        ['close_5d','5日收'],['close_10d','10日收'],['close_20d','20日收'],
        ['ret_5d','5日%'],['ret_10d','10日%'],['ret_20d','20日%']], rp.signal_tracking) +
      dbTbl('溢价历史（近10日）', [['date','日期'],['price','价'],['nav_official_est','官方估净'],
        ['nav_reference_est','参考估净'],['premium_official','官方溢价%'],['premium_reference','参考溢价%'],['percentile','分位']], rp.premium_hist);
  } catch (e) { out.className = ''; out.textContent = '查询失败：' + e; }
}

async function loadOps() {
  const [src, logs, stats] = await Promise.all(
    ['/api/sources','/api/scanlog','/api/stats'].map(u => fetch(u).then(r => r.json())));
  document.getElementById('srcOrder').innerHTML = (src.order || []).map((s, i) =>
    `<span class="src-item"><span class="dot ${i===0?'on':''}"></span>${i+1}. ${src.order_cn[s] || s}</span>`).join('');
  const lb = document.querySelector('#logTbl tbody'); lb.innerHTML = '';
  (logs.logs || []).forEach(x => lb.insertAdjacentHTML('beforeend',
    `<tr><td class="dim">${x.scan_time}</td><td>${x.signals}</td><td>${x.errors}</td>
     <td class="dim">${x.note||''}</td></tr>`));
  const ks = Object.entries(stats.stats || {});
  document.getElementById('stats').innerHTML = ks.length ? ks.map(([k, v]) =>
    `${k}：${v.count}次 胜率${(v.win_rate_10d*100).toFixed(0)}% 均收益${v.avg_ret_10d > 0 ? '+' : ''}${v.avg_ret_10d}%`
  ).join('<br>') : '暂无已回填统计（需信号满10个交易日后回填）';
}

async function loadRunLog() {
  const d = await fetch('/api/runlog').then(r => r.json()).catch(() => ({}));
  const parts = Object.entries(d).filter(([k, v]) => v).map(([k, v]) =>
    `── ${k} ──\\n${v}`).join('\\n\\n');
  document.getElementById('runLog').textContent = parts || '（暂无，触发上述任务后显示）';
}

loadRunLog();
loadOps();
// 主页浮层打开时收到通知 → 刷新数据
window.addEventListener('message', e => { if (e.data === 'refresh') { loadRunLog(); loadOps(); } });
</script>
</body>
</html>
"""


_MARKET_PAGE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>全市场回测榜 · A股监控台</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  :root { --bg:#0d1117; --card:#161b22; --bd:#30363d; --fg:#e6edf3; --dim:#8b949e;
          --up:#f85149; --dn:#3fb950; --acc:#58a6ff; --warn:#d29922; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.6 "Microsoft YaHei",sans-serif; padding:20px; }
  h1 { font-size:20px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
  a.back { font-size:13px; color:var(--acc); text-decoration:none; border:1px solid var(--bd);
           padding:5px 12px; border-radius:6px; background:#21262d; }
  button { background:#21262d; color:var(--fg); border:1px solid var(--bd); border-radius:6px;
           padding:7px 16px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--acc); color:var(--acc); }
  button:disabled { opacity:.5; cursor:wait; }
  .card { background:var(--card); border:1px solid var(--bd); border-radius:8px;
          padding:16px; margin-bottom:14px; }
  .card h2 { font-size:15px; margin-bottom:10px; color:var(--acc); }
  .pill { background:#0d1117; border:1px solid var(--bd); border-radius:12px;
          padding:2px 10px; font-size:12px; color:var(--dim); }
  .simbox label { display:flex; gap:5px; align-items:center; }
  .simbox input, .simbox select { background:#0d1117; color:var(--fg); border:1px solid var(--bd);
           border-radius:4px; padding:3px 6px; font-size:12px; }
  .simbox input[type=checkbox] { accent-color:var(--acc); }
  .prog { height:8px; background:#0d1117; border:1px solid var(--bd); border-radius:6px;
          overflow:hidden; margin:8px 0; }
  .prog > div { height:100%; background:var(--acc); width:0%; transition:width .5s; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--dim); font-weight:500; padding:6px 10px;
       border-bottom:1px solid var(--bd); white-space:nowrap; cursor:pointer; }
  th:hover { color:var(--acc); }
  td { padding:6px 10px; border-bottom:1px solid #21262d; white-space:nowrap; }
  tr.row:hover { background:#161b22; cursor:pointer; }
  .dim { color:var(--dim); } .up { color:var(--up); } .dn { color:var(--dn); }
  .tabs { display:flex; gap:6px; margin-bottom:8px; }
  .tabs button { border-radius:14px; padding:3px 14px; color:var(--dim); }
  .tabs button.on { color:#0d1117; background:var(--acc); border-color:var(--acc); font-weight:600; }
  #toast { position:fixed; right:20px; bottom:20px; background:#21262d; border:1px solid var(--bd);
           border-radius:8px; padding:10px 18px; display:none; z-index:99; }
  .err { color:var(--up); font-size:12px; margin-top:6px; max-height:100px; overflow:auto; }
  tr.xr td { background:#11161d; padding:10px 14px; white-space:normal; }
  .xhead { font-size:12px; margin-bottom:6px; }
</style>
</head>
<body>
<h1>🏆 全市场金额模拟回测榜
  <a class="back" href="/">‹ 返回看板</a>
  <span class="pill" id="listInfo">列表加载中…</span>
  <button onclick="refreshList(this)" style="font-size:12px;padding:4px 10px">刷新列表</button>
  <span class="pill" id="dbInfo">K线库：-</span>
</h1>

<div class="card">
  <h2>回测设置（口径与看板"金额模拟回测"一致）</h2>
  <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:12px;color:var(--dim)" class="simbox">
    <label><input type="checkbox" id="tStock" checked> 股票</label>
    <label><input type="checkbox" id="tEtf" checked> ETF</label>
    <label><input type="checkbox" id="tLof" checked> LOF</label>
    <label><input type="checkbox" id="tWatch" checked> 含自选池</label>
    <label>初始资金(元) <input id="mInit" type="number" value="10000" min="1000" max="10000000" step="1000" onblur="snapK(this)" style="width:92px"></label>
    <label>回测区间 <select id="mYears">
      <option value="1">近1年</option><option value="2">近2年</option>
      <option value="3" selected>近3年</option><option value="5">近5年</option>
      <option value="0">全部</option></select></label>
    <label>日低9买/高9卖 <input id="mUd" type="number" value="1000" min="0" max="1000000" step="1000" onblur="snapK(this)" style="width:70px">元</label>
    <label>周线 <input id="mUw" type="number" value="3000" min="0" max="1000000" step="1000" onblur="snapK(this)" style="width:70px">元</label>
    <label>月线 <input id="mUm" type="number" value="5000" min="0" max="1000000" step="1000" onblur="snapK(this)" style="width:70px">元</label>
    <label>标的数量 <input id="mLimit" type="number" value="300" min="10" max="12000" step="10" style="width:76px"> 只</label>
    <span style="color:var(--dim);font-size:12px">各类型按成交额轮询取前N（确保LOF/ETF入榜），重复运行走缓存更快</span>
    <button id="mRun" onclick="startJob()">开始回测</button>
    <button id="mPause" onclick="pauseJob()" style="display:none">暂停</button>
    <button id="mStop" onclick="stopJob()" style="display:none;border-color:var(--up);color:var(--up)">停止</button>
    <button id="mDb" onclick="updateDb()" style="border-color:var(--warn);color:var(--warn)">更新K线库</button>
    <button id="mDbStop" onclick="stopDb()" style="display:none;border-color:var(--up);color:var(--up)">停止更新</button>
  </div>
  <div class="prog"><div id="progBar"></div></div>
  <div id="progTxt" class="dim" style="font-size:12px">未运行（K线自动落库，够新不重复请求；手动更新用"更新K线库"）</div>
  <div class="prog"><div id="dbBar" style="background:var(--warn)"></div></div>
  <div id="dbTxt" class="dim" style="font-size:12px">K线库更新：未运行</div>
  <div id="errBox" class="err"></div>
</div>

<div class="card">
  <h2>排行榜 · <span id="sortLabel">策略收益率</span>（点击表头排序，点击行展开收益折线图）</h2>
  <div class="tabs">
    <button id="fAll" class="on" onclick="setFTab('all')">全部</button>
    <button id="fStock" onclick="setFTab('stock')">股票</button>
    <button id="fEtf" onclick="setFTab('etf')">ETF</button>
    <button id="fLof" onclick="setFTab('lof')">LOF</button>
    <span class="dim" style="margin:0 6px;align-self:center">｜九转筛选：</span>
    <button id="fW8" onclick="toggleTurn('w8')">周八</button>
    <button id="fW9" onclick="toggleTurn('w9')">周九</button>
    <button id="fM8" onclick="toggleTurn('m8')">月八</button>
    <button id="fM9" onclick="toggleTurn('m9')">月九</button>
  </div>
  <table id="mTbl"><thead><tr>
    <th>#</th><th>名称-代码</th><th>类型</th><th>九转状态</th><th>现价</th>
    <th data-k="total_ret">策略收益率</th><th data-k="buy_hold_ret">持有不动收益率</th>
    <th data-k="excess">超额收益率</th><th data-k="annual_ret">年化</th>
    <th data-k="max_drawdown">最大回撤</th><th data-k="n_trades">交易次数</th>
  </tr></thead><tbody></tbody></table>
</div>

<div id="toast"></div>
<script>
function toast(msg) { const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 5000); }
function snapK(el) { el.value = Math.max(0, Math.round((+el.value || 0) / 1000) * 1000); }
const cls = v => v == null ? '' : v > 0 ? 'up' : v < 0 ? 'dn' : '';
const sgn = v => v == null ? '-' : (v > 0 ? '+' : '') + v.toFixed(2);
const TYPE_CN = {stock:'股票', etf:'ETF', lof:'LOF'};
let allRows = [], fTab = 'all', sortKey = 'total_ret', timer = null;
let paramsRestored = false;   // 上次回测参数已回填标记（进站免重跑）
const fT = {w8: false, w9: false, m8: false, m9: false};   // 九转筛选开关（或关系）

async function loadList() {
  const d = await fetch('/api/market_list').then(r => r.json()).catch(() => ({error:'网络失败'}));
  const el = document.getElementById('listInfo');
  if (d.error) { el.textContent = '列表拉取失败：' + d.error; el.style.color = 'var(--up)'; return; }
  el.style.color = '';
  el.textContent = `全市场 ${d.total} 只（股票${d.counts.stock} · ETF${d.counts.etf} · LOF${d.counts.lof}） 更新于 ${d.updated_at}`;
  document.getElementById('dbInfo').textContent =
    `K线库：${d.db_codes || 0}只 / ${(d.db_rows / 10000).toFixed(1)}万根` +
    (d.db_latest ? ` 至 ${d.db_latest}` : '');
}

async function refreshList(b) {
  b.disabled = true; b.textContent = '刷新中…';
  const r = await fetch('/api/market_list/refresh', {method: 'POST'})
    .then(x => x.json()).catch(() => ({error: '请求失败'}));
  b.disabled = false; b.textContent = '刷新列表';
  toast(r.error ? '刷新失败：' + r.error : `列表已刷新（${r.total}只）`);
  if (!r.error) loadList();
}

function setFTab(t) {
  fTab = t;
  ['all', 'stock', 'etf', 'lof'].forEach(k =>
    document.getElementById('f' + k[0].toUpperCase() + k.slice(1)).classList.remove('on'));
  document.getElementById('f' + t[0].toUpperCase() + t.slice(1)).classList.add('on');
  render();
}

// 九转筛选：周八/周九/月八/月九独立开关，开启任一后只显示命中的标的（或关系；8=结构进行中，9=结构完成）
function toggleTurn(k) {
  fT[k] = !fT[k];
  document.getElementById('f' + k[0].toUpperCase() + k[1]).classList.toggle('on', fT[k]);
  render();
}
const turnHit = r => (fT.w8 && Math.abs(r.turn_week || 0) === 8)
  || (fT.w9 && Math.abs(r.turn_week || 0) === 9)
  || (fT.m8 && Math.abs(r.turn_month || 0) === 8)
  || (fT.m9 && Math.abs(r.turn_month || 0) === 9);

const turnTag = (r, key, cn) => [8, 9].includes(Math.abs(r[key] || 0))
  ? `<span class="${r[key] > 0 ? 'dn' : 'up'}" style="font-weight:600;${Math.abs(r[key]) === 8 ? 'opacity:.65' : ''}">${cn}${r[key] > 0 ? '高' : '低'}${Math.abs(r[key])}</span>`
  : '';

function render() {
  const tb = document.querySelector('#mTbl tbody'); tb.innerHTML = '';
  const anyT = fT.w8 || fT.w9 || fT.m8 || fT.m9;
  let rows = allRows.filter(r => (fTab === 'all' || r.type === fTab) && (!anyT || turnHit(r)));
  rows.sort((a, b) => (b[sortKey] ?? -1e9) - (a[sortKey] ?? -1e9));
  rows = rows.slice(0, 500);
  rows.forEach((r, i) => tb.insertAdjacentHTML('beforeend',
    `<tr class="row${r.code === mExpCode ? ' sel' : ''}" data-code="${r.code}" onclick="toggleRow('${r.code}')">
      <td class="dim">${i + 1}</td>
      <td style="white-space:nowrap">${poolCodes.has(r.code)
        ? '<span class="dim" style="font-size:11px;border:1px solid var(--bd);border-radius:4px;padding:1px 5px;margin-right:5px">自选</span>'
        : `<span title="加入自选池" style="cursor:pointer;color:var(--acc);margin-right:5px;font-weight:700" onclick="event.stopPropagation();poolAddM('${r.code}','${(r.name || '').replace(/'/g, '')}')">＋</span>`}${r.name}-${r.code}</td>
      <td class="dim">${TYPE_CN[r.type] || r.type}</td>
      <td>${turnTag(r, 'turn_week', '周')}${[8, 9].includes(Math.abs(r.turn_week || 0)) && [8, 9].includes(Math.abs(r.turn_month || 0)) ? '·' : ''}${turnTag(r, 'turn_month', '月')}</td>
      <td>${r.close ?? '-'}</td>
      <td class="${cls(r.total_ret)}" style="font-weight:600">${sgn(r.total_ret)}%</td>
      <td class="${cls(r.buy_hold_ret)}">${sgn(r.buy_hold_ret)}%</td>
      <td class="${cls(r.excess)}">${sgn(r.excess)}%</td>
      <td class="${cls(r.annual_ret)}">${r.annual_ret == null ? '-' : sgn(r.annual_ret) + '%'}</td>
      <td class="dn">-${r.max_drawdown}%</td>
      <td class="dim">${r.n_trades}</td>
    </tr>`));
  document.getElementById('sortLabel').textContent =
    document.querySelector(`th[data-k=${sortKey}]`).textContent;
  if (mExpCode && !rows.some(r => r.code === mExpCode)) mExpCode = null;  // 展开行已被过滤/移除
  if (mExpCode) openDetail(mExpCode);   // render重建DOM后恢复展开图（有option缓存，不重复请求）
}

// ---------- 行内收益折线图（与首页"金额模拟回测"同口径） ----------
let mExpCode = null;            // 当前展开的代码
const mChartOpt = {};           // code -> echarts option 缓存（参数变化时清空）
const mCharts = {};             // code -> echarts 实例（render重建DOM后需重新init）

function toggleRow(code) {
  mExpCode = (mExpCode === code) ? null : code;
  render();
}

function simQ() {
  return `initial=${+document.getElementById('mInit').value || 10000}` +
         `&years=${+document.getElementById('mYears').value || 0}` +
         `&ud=${+document.getElementById('mUd').value || 0}` +
         `&uw=${+document.getElementById('mUw').value || 0}` +
         `&um=${+document.getElementById('mUm').value || 0}`;
}

async function openDetail(code) {
  const tr = document.querySelector(`#mTbl tbody tr.row[data-code="${code}"]`);
  if (!tr) return;
  const row = allRows.find(r => r.code === code) || {};
  const name = row.name || code;
  tr.insertAdjacentHTML('afterend',
    `<tr class="xr"><td colspan="11">
      <div class="xhead dim">收益折线 · ${name}-${code}（口径同上，买卖点：红▲买 绿▼卖）</div>
      <div id="mx_${code}" style="width:100%;height:280px"></div>
      <div id="mxMsg_${code}" class="dim" style="font-size:12px;padding:4px 2px">加载模拟数据…</div>
    </td></tr>`);
  const el = document.getElementById('mx_' + code);
  const msg = document.getElementById('mxMsg_' + code);
  try {
    let opt = mChartOpt[code];
    if (!opt) {
      const s = await fetch(`/api/backtest_sim/${code}?${simQ()}`).then(r => r.ok ? r.json() : null);
      if (!s || s.error) throw new Error(s && s.error || '无数据（行情源繁忙时可收起后稍等重试）');
      const st = s.stats;
      const fmtY = v => (v == null ? '-' : '¥' + Math.round(v).toLocaleString());
      const byDate = Object.fromEntries(s.curve.map(p => [p.date, p]));
      const buys = (s.marks || []).filter(m => m.action === 'buy');
      const sells = (s.marks || []).filter(m => m.action === 'sell');
      const kpi = (k, v, c = '') => `<span style="margin-right:14px">${k}：<b class="${c}">${v}</b></span>`;
      msg.innerHTML =
        kpi('区间收益', sgn(st.total_ret) + '%', cls(st.total_ret)) +
        kpi('买入持有', sgn(st.buy_hold_ret) + '%', cls(st.buy_hold_ret)) +
        kpi('超额', sgn(st.excess) + '%', cls(st.excess)) +
        kpi('年化', st.annual_ret == null ? '-' : sgn(st.annual_ret) + '%', cls(st.annual_ret)) +
        kpi('最大回撤', '-' + st.max_drawdown + '%', 'dn') +
        kpi('交易', st.n_trades + '次') +
        kpi('初始资金', fmtY(s.params.initial)) +
        kpi('累计投入', fmtY(st.invested_end)) +
        kpi('累计收益', fmtY(st.pnl_end), cls(st.pnl_end)) +
        `<span>${s.name} ${st.n_days}个交易日</span>`;
      opt = {
        backgroundColor: 'transparent',
        tooltip: { trigger: 'axis', formatter: ps => {
          const p = ps[0]; const q = byDate[p.name] || {};
          const b = buys.filter(m => m.date === p.name);
          const sl = sells.filter(m => m.date === p.name);
          return `${p.name}<br/>` +
            (b.length ? `<span style="color:#f85149">▲ 买入 ${fmtY(b.reduce((a, m) => a + m.amount, 0))}</span><br/>` : '') +
            (sl.length ? `<span style="color:#3fb950">▼ 卖出 ${fmtY(sl.reduce((a, m) => a + m.amount, 0))}</span><br/>` : '') +
            `累计收益率 ${p.value == null ? '-' : p.value.toFixed(2)}%（按初始资金）<br/>` +
            `累计投入 ${fmtY(q.invested)}｜累计收益 ${fmtY(q.pnl)}<br/>` +
            `股票市值 ${fmtY(q.mv)}｜现金 ${fmtY(q.cash)}`; } },
        legend: { data: ['金额模拟', '买入持有', '买入', '卖出'], top: 0,
                  textStyle: { color: '#8b949e', fontSize: 11 }, itemWidth: 14 },
        grid: { left: 56, right: 16, top: 26, bottom: 24 },
        xAxis: { type: 'category', data: s.curve.map(p => p.date),
                 axisLabel: { color: '#8b949e', fontSize: 10 } },
        yAxis: { type: 'value', axisLabel: { formatter: '{value}%', color: '#8b949e', fontSize: 10 },
                 splitLine: { lineStyle: { color: '#21262d' } } },
        series: [
          { name: '金额模拟', type: 'line', data: s.curve.map(p => p.ret), symbol: 'none',
            lineStyle: { width: 1.8, color: '#d29922' }, itemStyle: { color: '#d29922' },
            areaStyle: { color: 'rgba(210,153,34,.08)' }, z: 3 },
          { name: '买入持有', type: 'line', data: s.bh_curve.map(p => p.ret), symbol: 'none',
            lineStyle: { width: 1.2, color: '#58a6ff' }, itemStyle: { color: '#58a6ff' } },
          { name: '买入', type: 'scatter', data: buys.map(m => [m.date, m.ret]),
            symbol: 'triangle', symbolSize: 11, symbolOffset: [0, '-55%'],
            itemStyle: { color: '#f85149', borderColor: '#0d1117', borderWidth: 1 }, z: 5 },
          { name: '卖出', type: 'scatter', data: sells.map(m => [m.date, m.ret]),
            symbol: 'triangle', symbolSize: 11, symbolRotate: 180, symbolOffset: [0, '55%'],
            itemStyle: { color: '#3fb950', borderColor: '#0d1117', borderWidth: 1 }, z: 5 }
        ]
      };
      mChartOpt[code] = opt;
    }
    if (mExpCode !== code) return;                    // 等待期间已收起
    if (mCharts[code]) { mCharts[code].dispose(); delete mCharts[code]; }
    const chart = echarts.init(el);                    // DOM可见后再init，宽度才正确
    mCharts[code] = chart;
    chart.setOption(opt, true);
  } catch (e) {
    if (msg) msg.textContent = '模拟失败：' + (e.message || e);
  }
}

window.onresize = () => { Object.values(mCharts).forEach(c => { try { c.resize(); } catch (e) {} }); };

document.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => {
  sortKey = th.dataset.k; render();
});

async function poll() {
  const d = await fetch('/api/market_sim/status').then(r => r.json()).catch(() => null);
  if (!d) return;
  const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
  document.getElementById('progBar').style.width = pct + '%';
  document.getElementById('progTxt').textContent =
    `${d.running ? (d.paused ? '已暂停' : '运行中') : (d.finished_at ? '已完成 ' + d.finished_at : '未运行')} · ` +
    `${d.done}/${d.total}（成功${d.ok}） ${pct}%`;
  document.getElementById('errBox').textContent =
    (d.errors || []).length ? '最近错误：' + d.errors.slice(-3).join('；') : '';
  allRows = d.results || [];
  if (!d.running && d.params && !paramsRestored) {   // 恢复上次参数（含展开折线图口径）
    paramsRestored = true;
    const p = d.params;
    document.getElementById('tStock').checked = p.types.includes('stock');
    document.getElementById('tEtf').checked = p.types.includes('etf');
    document.getElementById('tLof').checked = p.types.includes('lof');
    document.getElementById('tWatch').checked = !!p.watch;
    document.getElementById('mInit').value = p.initial;
    document.getElementById('mYears').value = String(p.years);
    document.getElementById('mUd').value = p.ud;
    document.getElementById('mUw').value = p.uw;
    document.getElementById('mUm').value = p.um;
    document.getElementById('mLimit').value = p.limit;
  }
  render();
  const pb = document.getElementById('mPause');
  pb.style.display = d.running ? '' : 'none';
  pb.textContent = d.paused ? '继续' : '暂停';
  if (!d.running) {
    clearInterval(timer); timer = null;
    document.getElementById('mRun').disabled = false;
    document.getElementById('mStop').style.display = 'none';
  }
}

async function startJob() {
  const types = [];
  if (document.getElementById('tStock').checked) types.push('stock');
  if (document.getElementById('tEtf').checked) types.push('etf');
  if (document.getElementById('tLof').checked) types.push('lof');
  if (!types.length) { toast('请至少选择一种类型'); return; }
  const body = {
    types,
    watch: document.getElementById('tWatch').checked,
    initial: +document.getElementById('mInit').value || 10000,
    years: +document.getElementById('mYears').value,
    ud: +document.getElementById('mUd').value || 0,
    uw: +document.getElementById('mUw').value || 0,
    um: +document.getElementById('mUm').value || 0,
    limit: +document.getElementById('mLimit').value,
  };
  const btn = document.getElementById('mRun');
  btn.disabled = true; btn.textContent = '启动中…';
  for (const k of Object.keys(mChartOpt)) delete mChartOpt[k];   // 回测口径可能变化，旧折线图失效
  const r = await fetch('/api/market_sim', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
    .then(x => x.json()).catch(() => ({error: '请求失败'}));
  btn.textContent = '开始回测';
  if (r.error) { toast(r.error); btn.disabled = false; return; }
  paramsRestored = true;   // 本次参数即当前控件值，完成后不再回填
  toast(`已启动：${r.total}只标的（首次运行需逐只拉K线，请耐心等待）`);
  document.getElementById('mStop').style.display = '';
  if (timer) clearInterval(timer);
  timer = setInterval(poll, 2000);
  poll();
}

async function pauseJob() {
  const b = document.getElementById('mPause');
  const act = b.textContent.trim() === '继续' ? 'resume' : 'pause';
  const r = await fetch('/api/market_sim/' + act, {method: 'POST'})
    .then(x => x.json()).catch(() => null);
  if (r && r.msg) { b.textContent = act === 'pause' ? '继续' : '暂停'; toast(r.msg); poll(); }
}

async function stopJob() {
  await fetch('/api/market_sim/stop', {method: 'POST'});
  toast('已请求停止');
}

// ---------- K线库手动更新 ----------
let dbTimer = null;

function dbBody() {
  const types = [];
  if (document.getElementById('tStock').checked) types.push('stock');
  if (document.getElementById('tEtf').checked) types.push('etf');
  if (document.getElementById('tLof').checked) types.push('lof');
  return { types,
    watch: document.getElementById('tWatch').checked,
    years: +document.getElementById('mYears').value,
    limit: +document.getElementById('mLimit').value };
}

async function updateDb() {
  const b = document.getElementById('mDb');
  b.disabled = true; b.textContent = '启动中…';
  const r = await fetch('/api/market_db/update', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(dbBody())})
    .then(x => x.json()).catch(() => ({error: '请求失败'}));
  b.textContent = '更新K线库';
  if (r.error) { toast(r.error); b.disabled = false; return; }
  toast(`已启动K线库更新：${r.total}只（增量拉取并落库）`);
  document.getElementById('mDbStop').style.display = '';
  if (dbTimer) clearInterval(dbTimer);
  dbTimer = setInterval(dbPoll, 2000);
  dbPoll();
}

async function stopDb() {
  await fetch('/api/market_db/stop', {method: 'POST'});
  toast('已请求停止更新');
}

async function dbPoll() {
  const d = await fetch('/api/market_db/status').then(r => r.json()).catch(() => null);
  if (!d) return;
  const pct = d.total ? Math.round(d.done / d.total * 100) : 0;
  document.getElementById('dbBar').style.width = (d.running ? pct : 0) + '%';
  document.getElementById('dbTxt').textContent =
    `K线库更新：${d.running ? (d.paused ? '已暂停' : '运行中') : (d.finished_at ? '已完成 ' + d.finished_at : '未运行')}` +
    (d.total ? ` · ${d.done}/${d.total}（成功${d.ok}） ${pct}%` : '') +
    (d.db && d.db.codes ? ` · 库内 ${d.db.codes}只/${(d.db.rows/10000).toFixed(1)}万根 至 ${d.db.latest || '-'}` : '') +
    ((d.errors || []).length ? ' · 最近错误：' + d.errors.slice(-1)[0] : '');
  if (!d.running && dbTimer) {
    clearInterval(dbTimer); dbTimer = null;
    document.getElementById('mDb').disabled = false;
    document.getElementById('mDbStop').style.display = 'none';
    loadList();   // 刷新库统计
  }
}

loadList();
poll();
dbPoll();

// ---------- 自选池操作（行内＋号） ----------
let poolCodes = new Set();
async function loadPool() {
  const d = await fetch('/api/stocks').then(r => r.json()).catch(() => ({}));
  poolCodes = new Set(d.pool_codes || (d.stocks || []).map(s => String(s.code)));
  render();
}
async function poolAddM(code, name) {
  const r = await fetch('/api/pool', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'add', code, name})})
    .then(x => x.json()).catch(() => ({error:'请求失败'}));
  toast(r.error ? '加入失败：' + r.error : (r.msg || '已加入自选池'));
  if (r.ok) { poolCodes.add(code); render(); }
}
loadPool();
</script>
</body>
</html>
"""


def create_app(cfg: dict, cache, sources, pusher, orch) -> Flask:
    app = Flask(__name__)
    _MK["cache"] = cache           # 全市场列表/K线持久化句柄
    _mk_load_persisted()           # 启动加载上次列表（重启不重拉网络）
    if _MK["job"] is None:         # 启动恢复上次回测榜单（免重跑）
        try:
            _MK["job"] = cache.mk_sim_load()
        except Exception:
            _MK["job"] = None

    def _capture(kind: str, fn):
        """执行 fn 并捕获 print 输出到运行日志。"""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                res = fn()
            return res, buf.getvalue()
        finally:
            _LOG[kind] = (_LOG[kind] + "\n────\n" + buf.getvalue()).strip()[-16000:]

    def _last_note():
        row = cache.conn.execute(
            "SELECT scan_time, note FROM scan_log WHERE mode='close' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "", "未扫描"
        note = row[1] or ""
        state = next((k for k in _STATE_CN if _STATE_CN[k] in note), "range")
        return note, row[0]

    @app.get("/")
    def index():
        return render_template_string(_PAGE)

    @app.get("/ops")
    def ops():
        return render_template_string(_OPS_PAGE)

    @app.get("/market")
    def market():
        return render_template_string(_MARKET_PAGE)

    # ---------- 概览 / 排行 ----------
    @app.get("/api/overview")
    def overview():
        note, last_scan = _last_note()
        state = next((k for k in _STATE_CN if _STATE_CN[k] in note), "range")
        today = datetime.now().strftime("%Y-%m-%d")
        pushes = cache.conn.execute(
            "SELECT COUNT(*) FROM push_history WHERE push_time LIKE ?", (today + "%",)).fetchone()[0]
        rows = cache.conn.execute(
            "SELECT COUNT(*) FROM signal_tracking WHERE signal_date LIKE ?", (today + "%",)).fetchone()[0]
        # 自选池构成（股票/场内基金分类统计）
        codes = [str(i["code"]) for i in cfg.get("watchlist", [])]
        n_fund = sum(1 for c in codes if detect_type(c) == "lof")
        pool_cn = f"{len(codes) - n_fund}股票 · {n_fund}ETF/LOF"
        return jsonify(market_state=state, market_label=_STATE_CN.get(state, state),
                       last_scan=last_scan, source_note=note, total=len(codes),
                       pool_cn=pool_cn, today_signals=rows, today_pushes=pushes)

    @app.get("/api/rank")
    def rank_rows():
        from domain.ranking import rank
        key = request.args.get("key", "vol_ratio")
        period = request.args.get("period", "day")
        if key not in ("vol_ratio", "vol_ratio_period", "amt_ratio", "amount",
                       "premium", "turn_abs", "pct_chg") or period not in _PERIODS:
            return jsonify(error="参数不合法"), 400
        rows = cache.latest_snapshots(period)
        # 自选池排行展示全部持仓：不做流动性漏斗过滤（该过滤仅用于全市场筛选）
        rows = rank(rows, key=key, top_n=100)
        pool_name = {str(i["code"]): str(i.get("name", "")) for i in cfg.get("watchlist", [])}
        # 附带周/月九转最新计数（前端 周八/周九/月八/月九 筛选用，与所选排序周期无关）
        tw = {str(r.get("code")): r.get("turn_count") for r in cache.latest_snapshots("week")}
        tm = {str(r.get("code")): r.get("turn_count") for r in cache.latest_snapshots("month")}
        out = []
        for r in rows:
            r = {k: _clean(v) for k, v in r.items()}
            code = str(r.get("code", ""))
            if pool_name.get(code):  # 名称以 config 为准（快照名可能滞后/错位）
                r["name"] = pool_name[code]
            r["turn_week"] = _clean(tw.get(code))
            r["turn_month"] = _clean(tm.get(code))
            out.append(r)
        return jsonify(rows=out)

    @app.get("/api/stocks")
    def stocks():
        rows = [{k: _clean(v) for k, v in r.items()} for r in cache.latest_snapshots("day")]
        rows.sort(key=lambda r: (r.get("vol_ratio") or 0), reverse=True)
        # 自选池代码直读config（新加标的未扫描也能被/market行内＋号正确标记）
        return jsonify(stocks=rows,
                       pool_codes=[str(i["code"]) for i in cfg.get("watchlist", [])])

    # ---------- K线（多周期，支持非自选代码在线拉取） ----------
    @app.get("/api/kline/<code>")
    def kline(code):
        if not _CODE_RE.match(code):
            return jsonify(error="代码不合法"), 400
        period = request.args.get("period", "day")
        if period not in _PERIODS:
            return jsonify(error="周期不合法"), 400
        df = cache.get_klines(code)
        src = "本地缓存"
        if df.empty:  # 非自选：直接在线拉取，不落库
            df = sources.fetch_kline(code, days=400)
            src = _SRC_CN.get(getattr(sources, "_last_src", "") or "tencent", "在线拉取")
        if df.empty:
            return jsonify(dates=[], ohlc=[], vols=[], turns=[], source="",
                           is_lof=False, quote=None, in_pool=False, mas=None)
        raw_day = df  # 日线原值用于量比/额计算（周期重采样后失真）
        import pandas as pd
        df_full = resample(df, period)
        for w in (5, 10, 20, 60):  # 均线：全量序列上滚动计算，避免窗口首部缺值
            df_full[f"ma{w}"] = df_full["close"].astype(float).rolling(w).mean()
        df = df_full.tail(180).reset_index(drop=True)
        turns = calc_turn_counts([float(c) for c in df["close"]])
        # 信息条：收盘/涨跌/量比/成交额/九转（用原始日线末端计算）
        last = raw_day.iloc[-1]
        prev = raw_day["close"].iloc[-2] if len(raw_day) > 1 else None
        v5 = raw_day["volume"].astype(float).iloc[-6:-1].mean() if len(raw_day) > 6 else None
        amt = float(last["amount"]) if "amount" in raw_day.columns and last["amount"] else None
        pct = (float(last["close"]) / float(prev) - 1) * 100 if prev else None
        # 周期视图下涨跌按该周期末根 vs 前一根
        if period != "day" and len(df) > 1:
            pct = (float(df["close"].iloc[-1]) / float(df["close"].iloc[-2]) - 1) * 100
        turn_last = next((t for t in reversed(turns) if t), 0)
        quote = {"date": str(last["date"]), "close": round(float(last["close"]), 3),
                 "pct_chg": None if pct is None or math.isnan(pct) else round(pct, 2),
                 "vol_ratio": None if not v5 else round(float(last["volume"]) / v5, 2),
                 "amount": amt, "turn_count": turn_last,
                 "turn_complete": abs(turn_last) == 9}
        in_pool = any(str(i["code"]) == code for i in cfg.get("watchlist", []))
        return jsonify(
            dates=df["date"].tolist(),
            ohlc=[[round(float(r.open), 3), round(float(r.close), 3),
                   round(float(r.low), 3), round(float(r.high), 3)] for r in df.itertuples()],
            vols=[float(v) for v in df["volume"]],
            turns=turns, source=src,
            is_lof=detect_type(code) == "lof", quote=quote, in_pool=in_pool,
            mas={f"ma{w}": [None if pd.isna(v) else round(float(v), 3) for v in df[f"ma{w}"]]
                 for w in (5, 10, 20, 60)})

    # ---------- 搜索（本地自选 + 腾讯联想） ----------
    @app.get("/api/search")
    def search():
        q = (request.args.get("q") or "").strip()[:20]
        if not q:
            return jsonify(items=[])
        items, seen = [], set()

        def add(code, name, typ, in_pool):
            if code not in seen:
                seen.add(code)
                items.append({"code": code, "name": name, "type": typ, "in_pool": in_pool})

        for it in cfg.get("watchlist", []):  # 本地自选池
            code, name = str(it["code"]), it.get("name", "")
            if q.lower() in code.lower() or q.lower() in name.lower():
                add(code, name, "LOF" if detect_type(code) == "lof" else "股票", True)
        try:  # 腾讯智能联想（覆盖全市场股票/ETF/LOF/指数）
            r = requests.get("https://smartbox.gtimg.cn/s3/",
                             params={"v": 2, "q": q, "t": "all"}, timeout=6)
            r.encoding = "gbk"
            m = re.search(r'v_hint="([^"]*)"', r.text)
            if m and m.group(1) and m.group(1) != "0":
                watch_codes = {str(i["code"]) for i in cfg.get("watchlist", [])}
                for ent in m.group(1).split("^"):
                    f = ent.split("~")
                    if len(f) >= 3 and f[1].isdigit() and len(f[1]) == 6:
                        name = _unescape_u(f[2]) or f[1]
                        typ = _unescape_u(f[4]) if len(f) > 4 else ""
                        add(f[1], name, _TYPE_CN.get(typ, typ), f[1] in watch_codes)
        except Exception:
            pass
        return jsonify(items=items[:10])

    # ---------- 自选池管理（增删，写回 config.yaml 保留注释） ----------
    def _pool_entry_re(code: str):
        return re.compile(r'^\s*-\s*\{code:\s*"%s",\s*name:\s*"[^"]*"\}\s*$\n?' % re.escape(code), re.M)

    def _pool_add(code: str, name: str):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.yaml")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        entries = list(re.finditer(r'^\s*-\s*\{code:\s*"\d+",\s*name:\s*"[^"]*"\}\s*$', text, re.M))
        line = f'  - {{code: "{code}", name: "{name}"}}'
        if entries:  # 插到自选池列表最后一项之后
            end = entries[-1].end()
            text = text[:end] + "\n" + line + text[end:]
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        import yaml as _y
        _y.safe_load(text)  # 校验可解析

    def _pool_remove(code: str):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.yaml")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        text = _pool_entry_re(code).sub("", text, count=1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        import yaml as _y
        _y.safe_load(text)

    def _pool_scan_one(code: str, name: str) -> bool:
        """单标的即时快照：新加自选后不等收盘扫描，排行立即可见。"""
        from domain.nine_turns import calc_nine_turns
        from domain.ranking import build_snapshot_row
        try:
            df = orch.load_kline(code)
            if df.empty or len(df) < 60:
                return False
            premium = None
            if is_lof(code):
                try:
                    st = orch._eval_lof(code, name, df)
                    st.trade_date = str(df["date"].iloc[-1])
                    cache.upsert_premium(st)
                    premium = st.premium_official
                except Exception:
                    pass
            for period in ("day", "week", "month"):
                cache.upsert_snapshot(build_snapshot_row(
                    df, code, name, period, cfg, premium if period == "day" else None))
            cache.set_state(code, calc_nine_turns(df).count)
            return True
        except Exception:
            return False

    @app.post("/api/pool")
    def pool_manage():
        d = request.get_json(silent=True) or {}
        action, code = d.get("action"), str(d.get("code", ""))
        name = str(d.get("name") or code).strip()[:20]
        if action not in ("add", "remove") or not _CODE_RE.match(code):
            return jsonify(error="参数不合法"), 400
        pool = cfg.setdefault("watchlist", [])
        if action == "add":
            if any(str(i["code"]) == code for i in pool):
                return jsonify(ok=True, msg="已在自选池")
            try:
                _pool_add(code, name)
            except Exception as e:
                return jsonify(error=f"写入配置失败: {e}"), 500
            pool.append({"code": code, "name": name})
            # 即时扫描：拉K线+生成三周期快照，自选池排行马上出现
            snap_ok = _pool_scan_one(code, name)
            msg = f"{name} 已加入自选池（排行立即可见）" if snap_ok else \
                f"{name} 已加入自选池（暂无足够K线，排行将在下次扫描后出现）"
            return jsonify(ok=True, msg=msg)
        else:
            if not any(str(i["code"]) == code for i in pool):
                return jsonify(ok=True, msg="不在自选池")
            try:
                _pool_remove(code)
            except Exception as e:
                return jsonify(error=f"写入配置失败: {e}"), 500
            cfg["watchlist"] = [i for i in pool if str(i["code"]) != code]
            # 清理快照：排行立即消失；顺带清历史移除残留的孤儿快照
            purged = 0
            try:
                cache.del_snapshots(code)
                purged = cache.purge_non_pool_snapshots(
                    [i["code"] for i in cfg["watchlist"]])
            except Exception:
                pass
            extra = f"，清理残留快照{purged}行" if purged else ""
            return jsonify(ok=True, msg=f"{name} 已移出自选池{extra}")

    # ---------- 数据来源 ----------
    @app.get("/api/sources")
    def sources_info():
        order = cfg.get("data_sources", [])
        pool = {str(i["code"]): i.get("name", "") for i in cfg.get("watchlist", [])}
        rows = cache.conn.execute(
            "SELECT code, MAX(date), COUNT(*) FROM kline_day GROUP BY code").fetchall()
        note, _ = _last_note()
        return jsonify(order=order, order_cn=_SRC_CN, last_note=note,
                       per_code=[{"code": c, "name": pool.get(c, ""), "last_date": d,
                                  "bars": n} for c, d, n in sorted(rows)])

    # ---------- 信号 / 日志 / 统计 ----------
    @app.get("/api/signals")
    def signals():
        df = cache.conn.execute(
            "SELECT p.push_time, p.code, p.direction, p.level, s.name FROM push_history p "
            "LEFT JOIN (SELECT code, MAX(trade_date) d, name FROM indicator_snapshot "
            "           WHERE period='day' GROUP BY code) s ON s.code = p.code "
            "ORDER BY p.push_time DESC LIMIT 20").fetchall()
        pool_name = {str(i["code"]): str(i.get("name", "")) for i in cfg.get("watchlist", [])}
        return jsonify(signals=[dict(push_time=r[0], code=r[1], direction=r[2], level=r[3],
                                     name=pool_name.get(str(r[1]), r[4] or "")) for r in df])

    # ---------- 九转回测 ----------
    @app.get("/api/backtest/<code>")
    def backtest_one(code):
        from domain.backtest import backtest_turns
        if not _CODE_RE.match(code):
            return jsonify(error="代码不合法"), 400
        period = request.args.get("period", "day")
        if period not in _PERIODS:
            return jsonify(error="周期不合法"), 400
        df = cache.get_klines(code)
        if df.empty:
            df = sources.fetch_kline(code, days=1000)
        if df.empty:
            return jsonify(error="无K线数据"), 404
        bt = backtest_turns(resample(df, period))
        name = next((str(i["name"]) for i in cfg.get("watchlist", [])
                     if str(i["code"]) == code), code)
        return jsonify(name=name, period=period, **bt)

    @app.post("/api/backtest_all")
    def backtest_all():
        """全池九转回测：日线口径，逐标的统计并汇总。"""
        from domain.backtest import backtest_turns, format_backtest_report

        def run():
            lines = ["══ 全池九转回测（日线，信号后5/10/20日收益）══"]
            agg = {"buy": [], "sell": []}
            for it in cfg.get("watchlist", []):
                code, name = str(it["code"]), str(it.get("name", ""))
                df = cache.get_klines(code)
                if df is None or df.empty or len(df) < 30:
                    lines.append(f"── {name} {code}: 数据不足，跳过")
                    continue
                bt = backtest_turns(resample(df, "day"))
                lines.append(format_backtest_report(code, name, bt))
                for tp in ("buy", "sell"):
                    s = bt["stats"][tp]
                    if s.get("win_rate_10d") is not None:
                        agg[tp].append(s)
            lines.append("══ 汇总（各标的等权平均）══")
            summary = {}
            for tp, cn in (("buy", "买入(低9)"), ("sell", "卖出(高9)")):
                rows = agg[tp]
                if not rows:
                    lines.append(f"  {cn}: 无有效样本")
                    continue
                summary[tp] = {
                    "stocks": len(rows),
                    "avg_win_rate_10d": round(sum(r["win_rate_10d"] for r in rows) / len(rows), 3),
                    "avg_ret_10d": round(sum(r["avg_ret_10d"] for r in rows) / len(rows), 2),
                    "total_signals": sum(r["count"] for r in rows)}
                s = summary[tp]
                lines.append(f"  {cn}: {s['stocks']}只标的共{s['total_signals']}信号 "
                             f"10日胜率均值{s['avg_win_rate_10d']:.0%} "
                             f"均收益均值{s['avg_ret_10d']:+.1f}%")
            print("\n".join(lines))
            return summary
        r, _ = _capture("backtest", run)
        return jsonify(ok=True, summary=r)

    @app.get("/api/backtest_sim/<code>")
    def backtest_sim(code):
        """金额制交易模拟：初始资金首日全仓买入，九转信号按固定金额加减仓。"""
        from domain.backtest import simulate_shares
        if not _CODE_RE.match(code):
            return jsonify(error="代码不合法"), 400
        try:
            initial = max(1000, min(10_000_000, int(request.args.get("initial", 10000))))
            ud = max(0, min(1_000_000, int(request.args.get("ud", 1000))))
            uw = max(0, min(1_000_000, int(request.args.get("uw", 3000))))
            um = max(0, min(1_000_000, int(request.args.get("um", 5000))))
            years = max(0.0, min(30.0, float(request.args.get("years", 3) or 0)))
        except ValueError:
            return jsonify(error="参数不合法"), 400
        # 统一口径：kline_day表 + 固定1000天窗口（行内榜与展开图同源）
        df = _mk_kline(sources, cache, code)
        if df.empty or len(df) < 30:
            return jsonify(error="K线数据不足(真实行情源暂不可用)"), 404
        if years > 0:   # 只回测最近N年（九转信号基于截断后数据重算）
            import pandas as pd
            d = pd.to_datetime(df["date"])
            df = df[d >= d.max() - pd.Timedelta(days=int(365 * years))].reset_index(drop=True)
            if len(df) < 30:
                return jsonify(error="截取后K线数据不足"), 404
        sim = simulate_shares(df, initial, ud, uw, um)
        name = next((str(i["name"]) for i in cfg.get("watchlist", [])
                     if str(i["code"]) == code), code)
        # 降采样曲线（>400点抽稀，前端折线图更流畅），交易点吸附到抽稀后日期
        c = sim["curve"]
        date_idx = {t["date"]: i for i, t in enumerate(c)}
        full_trades = sim["trades"]
        if len(c) > 400:
            step = len(c) // 400
            idx = list(range(0, len(c), step))
            if idx[-1] != len(c) - 1:
                idx.append(len(c) - 1)
            marks = []
            for t in full_trades:
                i = date_idx.get(t["date"])
                if i is None:
                    continue
                j = next((k for k in reversed(idx) if k <= i), idx[0])  # 吸附到≤i的最近抽稀点
                marks.append({"date": c[j]["date"], "ret": c[j]["ret"],
                              "action": t["action"], "amount": t["amount"]})
            sim["curve"] = [c[i] for i in idx]
            sim["bh_curve"] = [sim["bh_curve"][i] for i in idx]
        else:
            marks = [{"date": t["date"], "ret": c[date_idx[t["date"]]]["ret"],
                      "action": t["action"], "amount": t["amount"]}
                     for t in full_trades if t["date"] in date_idx]
        sim["marks"] = marks          # 买卖点（坐标已在曲线上，前端直接画）
        sim["trades"] = full_trades[-50:]  # 明细表只留最近50笔
        if full_trades and full_trades[0]["why"] == "初始建仓(全仓)" \
                and (not sim["trades"] or sim["trades"][0]["why"] != "初始建仓(全仓)"):
            sim["trades"] = [full_trades[0]] + sim["trades"]  # 初始建仓行始终保留
        sim["name"] = name
        return jsonify(sim)

    # ---------- 全市场回测榜 ----------
    def _mk_targets(params: dict):
        """按类型轮询配额选目标 + 可选并入自选池。返回 (targets, err)。"""
        try:
            lst = _mk_get_list()
        except Exception as e:
            return None, f"全市场列表拉取失败: {e}"
        # 各类型按成交额轮询取名额：保证股票/ETF/LOF都能入榜（纯降序会被股票淹没）
        by_type = {t: [x for x in lst if x["type"] == t] for t in params["types"]}
        targets, rank = [], 0
        while len(targets) < params["limit"]:
            added = False
            for t in params["types"]:
                if rank < len(by_type[t]) and len(targets) < params["limit"]:
                    targets.append(by_type[t][rank])
                    added = True
            if not added:
                break
            rank += 1
        if params["watch"]:   # 并入自选池（去重，现价取列表实时价）
            seen = {x["code"] for x in targets}
            close_map = {x["code"]: x.get("close") for x in lst}
            for w in cfg.get("watchlist", []):
                c = str(w.get("code", "")).strip()
                if not c or c in seen:
                    continue
                seen.add(c)
                targets.append({"code": c, "name": str(w.get("name") or c),
                                "type": _mk_type(c), "close": close_map.get(c),
                                "amount": 0})
        return targets, None

    def _mk_parse_params(b: dict):
        """解析并钳制回测/更新任务的公共参数。"""
        return {
            "initial": max(1000, min(10_000_000, int(b.get("initial", 10000)))),
            "ud": max(0, min(1_000_000, int(b.get("ud", 1000)))),
            "uw": max(0, min(1_000_000, int(b.get("uw", 3000)))),
            "um": max(0, min(1_000_000, int(b.get("um", 5000)))),
            "years": max(0.0, min(30.0, float(b.get("years", 3) or 0))),
            "limit": max(10, min(12000, int(b.get("limit", 300)))),
            "watch": bool(b.get("watch", False)),
            "types": [t for t in b.get("types", ["stock", "etf", "lof"])
                      if t in ("stock", "etf", "lof")] or ["stock", "etf", "lof"],
        }

    @app.get("/api/market_list")
    def market_list():
        try:
            lst = _mk_get_list()
        except Exception as e:
            return jsonify(error=f"全市场列表拉取失败: {e}"), 502
        cnt = {"stock": 0, "etf": 0, "lof": 0}
        for x in lst:
            cnt[x["type"]] += 1
        try:
            db = cache.mk_stats()
        except Exception:
            db = {}
        return jsonify(total=len(lst), counts=cnt,
                       db_codes=db.get("codes", 0), db_rows=db.get("rows", 0),
                       db_latest=db.get("latest"),
                       updated_at=datetime.fromtimestamp(_MK["list_ts"]).strftime("%m-%d %H:%M")
                       if _MK["list_ts"] else "", rows=lst[:100])

    @app.post("/api/market_list/refresh")
    def market_list_refresh():
        """强制重拉全市场列表（成功才覆盖缓存，失败保留旧列表）。"""
        try:
            lst = _mk_get_list(force=True)
        except Exception as e:
            return jsonify(error=f"全市场列表拉取失败: {e}"), 502
        return jsonify(msg="列表已刷新", total=len(lst))

    @app.post("/api/market_db/update")
    def market_db_update():
        """手动更新K线数据库：按当前类型+数量选取目标，逐只强制拉取落库。"""
        if _MK["dbjob"] and _MK["dbjob"]["running"]:
            return jsonify(error="更新任务已在运行"), 409
        b = request.get_json(silent=True) or {}
        try:
            params = _mk_parse_params(b)
        except (TypeError, ValueError):
            return jsonify(error="参数不合法"), 400
        targets, err = _mk_targets(params)
        if err:
            return jsonify(error=err), 502
        if not targets:
            return jsonify(error="无符合条件的标的"), 400
        _MK["dbjob"] = {"running": True, "stop": False, "paused": False,
                        "total": len(targets), "done": 0, "ok": 0, "errors": [],
                        "started_at": datetime.now().strftime("%H:%M:%S"),
                        "finished_at": ""}
        threading.Thread(target=_mk_db_update_job,
                         args=(sources, cache, targets, params["years"]),
                         daemon=True).start()
        return jsonify(msg="已启动K线库更新", total=len(targets))

    @app.get("/api/market_db/status")
    def market_db_status():
        job = _MK["dbjob"] or {}
        try:
            db = cache.mk_stats()
        except Exception:
            db = {}
        return jsonify(running=job.get("running", False),
                       paused=job.get("paused", False),
                       total=job.get("total", 0), done=job.get("done", 0),
                       ok=job.get("ok", 0), errors=(job.get("errors") or [])[-10:],
                       started_at=job.get("started_at", ""),
                       finished_at=job.get("finished_at", ""), db=db)

    @app.post("/api/market_db/stop")
    def market_db_stop():
        if _MK["dbjob"]:
            _MK["dbjob"]["stop"] = True
            _MK["dbjob"]["paused"] = False
            return jsonify(msg="已请求停止更新")
        return jsonify(msg="无更新任务")

    @app.post("/api/market_sim")
    def market_sim_start():
        if _MK["job"] and _MK["job"]["running"]:
            return jsonify(error="已有任务在运行，请等待完成或先停止"), 409
        b = request.get_json(silent=True) or {}
        try:
            params = _mk_parse_params(b)
        except (TypeError, ValueError):
            return jsonify(error="参数不合法"), 400
        targets, err = _mk_targets(params)
        if err:
            return jsonify(error=err), 502
        if not targets:
            return jsonify(error="无符合条件的标的"), 400
        _MK["job"] = {"running": True, "stop": False, "paused": False, "params": params,
                      "total": len(targets), "done": 0, "ok": 0,
                      "results": [], "errors": [],
                      "started_at": datetime.now().strftime("%H:%M:%S"), "finished_at": ""}
        threading.Thread(target=_mk_run_job, args=(sources, cache, targets, params),
                         daemon=True).start()
        return jsonify(msg="已启动", total=len(targets), params=params)

    @app.get("/api/market_sim/status")
    def market_sim_status():
        job = _MK["job"]
        if not job:
            return jsonify(running=False, paused=False, results=[], done=0,
                           total=0, ok=0, errors=[])
        return jsonify(running=job["running"], paused=job.get("paused", False),
                       total=job["total"], done=job["done"],
                       ok=job["ok"], errors=job["errors"][-20:],
                       results=job["results"], params=job["params"],
                       started_at=job["started_at"], finished_at=job["finished_at"])

    @app.post("/api/market_sim/pause")
    def market_sim_pause():
        if _MK["job"] and _MK["job"]["running"]:
            _MK["job"]["paused"] = True
            return jsonify(msg="已暂停")
        return jsonify(msg="无运行中任务")

    @app.post("/api/market_sim/resume")
    def market_sim_resume():
        if _MK["job"]:
            _MK["job"]["paused"] = False
            return jsonify(msg="已继续")
        return jsonify(msg="无任务")

    @app.post("/api/market_sim/stop")
    def market_sim_stop():
        if _MK["job"]:
            _MK["job"]["stop"] = True
            _MK["job"]["paused"] = False
            return jsonify(msg="已请求停止，正在收尾…")
        return jsonify(msg="无任务")

    @app.get("/api/ops/db")
    def ops_db():
        """运维中心：按代码或名称查询单标的数据库数据（kline_day/状态/快照/推送/信号跟踪/溢价）。"""
        q = (request.args.get("q") or "").strip()
        code, name = q, ""
        if not _CODE_RE.match(code):    # 非合法代码 → 按名称模糊解析（自选池config优先）
            hit = next((i for i in cfg.get("watchlist", [])
                        if q and q in (str(i.get("name", "")), str(i.get("code", "")))), None)
            if hit:
                code, name = str(hit["code"]), str(hit.get("name", ""))
            else:
                mk = cache.code_by_name(q) if q else None
                if mk:
                    code, name = mk
        if not _CODE_RE.match(code):
            return jsonify(error="请输入 6位代码 或 名称关键词"), 400
        if not name:
            name = next((str(i.get("name", "")) for i in cfg.get("watchlist", [])
                         if str(i["code"]) == code), "")
        rep = cache.code_report(code)
        if not any([rep["kline"]["count"], rep["stock_state"], rep["snapshots"],
                    rep["push_history"], rep["signal_tracking"], rep["premium_hist"]]):
            return jsonify(error=f"{code} 库内无数据（未扫描/未回测过）"), 404
        return jsonify(code=code, name=name or code, report=rep)

    @app.post("/api/ops/push_test")
    def ops_push_test():
        """运维中心·强制推送测试：绕过分级/去重/限额闸门，直推微信验证通道（标记TEST，不写推送历史）。
        kind=nine → 单一策略·神奇九转：自选池任一周期|九转计数|≥8，按推送历史区分新增/原有，日周月以分割线分隔；
        kind=premium → 真LOF(16/50)溢价触发提醒线。"""
        d = request.get_json(silent=True) or {}
        kind = d.get("kind")
        if kind not in ("nine", "premium"):
            return jsonify(error="kind 必须为 nine/premium"), 400
        pool = {str(i["code"]): str(i.get("name", "")) for i in cfg.get("watchlist", [])}
        snaps = {p: {str(r.get("code")): r for r in cache.latest_snapshots(p)}
                 for p in ("day", "week", "month")}
        items, lines = [], []
        now = datetime.now().strftime("%m-%d %H:%M")

        def tag_md(cn: str, c: int) -> str:
            # 企微markdown着色：高9/高8橙(warning)、低9/低8绿(info)
            color = "warning" if c > 0 else "info"
            return f'<font color="{color}">{cn}线{"高" if c > 0 else "低"}{abs(c)}</font>'

        if kind == "nine":
            # 推送历史视角：同方向推送过=⏳原有，未推送过=🆕新增（与正式推送的新增/原有口径一致）
            hist_dirs = {}
            for r_code, r_dir in cache.conn.execute(
                    "SELECT code, direction FROM push_history").fetchall():
                hist_dirs.setdefault(str(r_code), set()).add(str(r_dir))
            fresh_lines, keep_lines = [], []
            for code, name in pool.items():
                tags, plain = [], []
                for p, cn in (("day", "日"), ("week", "周"), ("month", "月")):
                    c = (snaps[p].get(code) or {}).get("turn_count") or 0
                    if abs(c) >= 8:
                        tags.append(tag_md(cn, c))
                        plain.append(f"{cn}{'高' if c > 0 else '低'}{abs(c)}")
                if not tags:
                    continue
                items.append({"code": code, "name": name, "tags": "┃".join(plain)})
                # 日线方向为正式推送的判定口径
                day_c = (snaps["day"].get(code) or {}).get("turn_count") or 0
                direction = "up" if day_c > 0 else "down"
                row = f"> **{name}** {code}：{' ┃ '.join(tags)}"
                (keep_lines if direction in hist_dirs.get(code, set())
                 else fresh_lines).append(row)
            title = f"【测试推送】单一策略·九转满足标的 {len(items)}只"
            parts = []
            if fresh_lines:
                parts.append(f"\n🆕 **新增（{len(fresh_lines)}只）**\n" + "\n".join(fresh_lines))
            if keep_lines:
                div = "─" * 16
                parts.append((f"\n{div}\n" if parts else "")
                             + f"⏳ **原有维持（{len(keep_lines)}只）**\n" + "\n".join(keep_lines))
            body = "\n".join(parts) if parts \
                else "\n<font color=\"comment\">当前自选池无九转计数≥8的标的</font>\n本条仅验证推送通道。"
        else:
            lof_cfg = cfg.get("lof", {})
            watch = float(lof_cfg.get("premium_watch", 3.0))
            disc = float(lof_cfg.get("discount_watch", 2.0))
            for code, name in pool.items():
                if not is_lof(code):
                    continue
                r = snaps["day"].get(code) or {}
                p = r.get("premium")
                if p is not None and (p >= watch or p <= -disc):
                    items.append({"code": code, "name": name, "premium": p})
                    color = "warning" if p > 0 else "info"   # 溢价橙、折价绿
                    lines.append(f"> **{name}** {code}：<font color=\"{color}\">"
                                 f"溢价 {p:+.2f}%</font>")
            title = f"【测试推送】LOF溢价满足标的 {len(items)}只"
            body = (f"\n真LOF(16/50) 溢价≥{watch}%或折价≤-{disc}%：\n" + "\n".join(lines)) if lines \
                else f"\n<font color=\"comment\">当前真LOF无溢价≥{watch}%或折价≤-{disc}%的标的</font>\n本条仅验证推送通道。"
        body += f"\n<font color=\"comment\">TEST · 通道验证 · {now}</font>"
        ok = pusher.send(title, body, level="TEST", is_alert=True)
        err_detail = "；".join(pusher.last_errors) if pusher.last_errors else ""
        msg = "已推送到微信，请查收" if ok else ("推送失败：" + err_detail if err_detail
                                               else "推送失败（通道未配置或网络异常）")
        return jsonify(ok=ok, msg=msg, errors=err_detail, items=items)

    @app.get("/api/scanlog")
    def scanlog():
        rows = cache.conn.execute(
            "SELECT scan_time, signals, errors, note FROM scan_log ORDER BY id DESC LIMIT 10").fetchall()
        return jsonify(logs=[dict(scan_time=r[0], signals=r[1], errors=r[2], note=r[3]) for r in rows])

    @app.get("/api/stats")
    def stats():
        return jsonify(stats=cache.tracking_stats())

    @app.get("/api/lof/<code>")
    def lof(code):
        if not _CODE_RE.match(code):
            return jsonify(error="代码不合法"), 400
        df = cache.get_klines(code)
        if df.empty:
            df = sources.fetch_kline(code, days=120)
        if df.empty:
            return jsonify(card=None)
        name = next((str(i["name"]) for i in cfg.get("watchlist", []) if str(i["code"]) == code), code)
        st = orch._eval_lof(code, name, df)
        from presentation.formatter import format_lof_card
        return jsonify(card=format_lof_card(st), premium=st.premium_official,
                       premium_rt=st.premium_official, premium_t1=st.premium_t1)

    @app.get("/api/premium/<code>")
    def premium_hist(code):
        """LOF 溢价历史走势（落库序列优先；不足时用K线近似回填）。"""
        if not _CODE_RE.match(code):
            return jsonify(error="代码不合法"), 400
        days = min(90, max(30, int(request.args.get("days", 60))))
        rows = cache.get_premium_hist(code, days)
        if len(rows) < 10:  # 落库不足：用K线收盘/昨收近似生成（本地无缓存则在线拉取）
            df = cache.get_klines(code)
            if df.empty:
                df = sources.fetch_kline(code, days=days + 10)
            if not df.empty:
                import pandas as pd
                c = df["close"].astype(float)
                nav = c.shift(1)
                prem = ((c / nav - 1) * 100)
                tail = df.tail(days)
                tail_idx = tail.index
                rows = [{"date": str(d), "premium_official": round(float(prem.loc[i]), 2),
                         "premium_reference": None, "percentile": None,
                         "price": float(c.loc[i])} for i, d in zip(tail_idx, tail["date"])
                        if pd.notna(prem.loc[i])]
        return jsonify(rows=[{k: _clean(v) for k, v in r.items()} for r in rows])

    # ---------- 功能测试 / 运行日志 ----------
    @app.post("/api/scan")
    def do_scan():
        r, _ = _capture("scan", orch.scan_close)
        return jsonify(total=r["total"], signals=r["signals"], errors=r["errors"],
                       market_state=r["market_state"], source=r["source"])

    @app.post("/api/demo")
    def do_demo():
        import yaml as _yaml
        with open("config.yaml", encoding="utf-8") as f:
            dcfg = _yaml.safe_load(f)
        dcfg["data_sources"] = ["synthetic"]
        dcfg["db_path"] = "data/demo.db"

        def run():
            from infrastructure.adapters import MultiSourceManager
            from infrastructure.cache import Cache as _Cache
            from infrastructure.push import Pusher as _Pusher
            from application.orchestrator import Orchestrator
            dcache = _Cache(dcfg["db_path"])
            try:
                print("离线演示模式（确定性合成数据，独立演示库）")
                r = Orchestrator(dcfg, dcache, MultiSourceManager(dcfg), _Pusher(dcfg)).scan_close()
                print(Orchestrator(dcfg, dcache, MultiSourceManager(dcfg),
                                   _Pusher(dcfg)).rank_report("vol_ratio", "day"))
                return r
            finally:
                dcache.close()
        r, _ = _capture("demo", run)
        return jsonify(total=r["total"], signals=r["signals"], errors=r["errors"],
                       market_state=r["market_state"], source=r["source"])

    @app.post("/api/heartbeat")
    def do_heartbeat():
        from application.heartbeat import check_heartbeat

        def run():
            return check_heartbeat(cache, pusher)
        r, out = _capture("heartbeat", run)
        return jsonify(ok=bool(r.get("ok")), detail=str(r), output=out.strip())

    @app.post("/api/selftest")
    def do_selftest():
        def run():
            return subprocess.run(
                [sys.executable, os.path.join("tests", "selftest.py")],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300)
        p, _ = _capture("selftest", run)
        out = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"通过 (\d+) 项 / 失败 (\d+) 项", out)
        summary = f"通过{m.group(1)}项/失败{m.group(2)}项" if m else "未解析到统计行"
        return jsonify(exit_code=p.returncode, summary=summary,
                       output=out.strip()[-4000:])

    @app.get("/api/runlog")
    def runlog():
        return jsonify(_LOG)

    return app
