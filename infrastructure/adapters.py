"""数据源适配层：统一接口 + 主备降级 + 确定性合成数据（离线可运行）。

统一K线列: date(str YYYY-MM-DD), open, high, low, close, volume, amount
"""
import math
import random
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

# 代码段 → 标的类型（16/50/51/52/55/56/58 均为场内基金：LOF/ETF）
LOF_PREFIX = ("16", "50", "51", "52", "55", "56", "58")
LOF_ONLY_PREFIX = ("16", "50")   # 真LOF段：16深 + 50沪（51/52/55/56/58为ETF，不做溢价监控）


def is_lof(code: str) -> bool:
    """真LOF判定（16/50）：仅此类标的参与溢价监控与提醒。"""
    return str(code).startswith(LOF_ONLY_PREFIX)


def detect_type(code: str) -> str:
    c = str(code)
    if c.startswith(LOF_PREFIX):
        return "lof"
    return "stock"


class BaseSource:
    name = "base"

    def fetch_kline(self, code: str, days: int = 2500, start: str = None) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_index(self, days: int = 300) -> pd.DataFrame:
        raise NotImplementedError


# ---------------- akshare ----------------
class AkshareSource(BaseSource):
    name = "akshare"

    def __init__(self):
        import akshare as ak  # 延迟导入
        self.ak = ak

    def fetch_kline(self, code, days=2500, start=None):
        code = str(code)
        end = datetime.now().strftime("%Y%m%d")
        start = start or (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
        if detect_type(code) == "lof":
            df = self.ak.fund_etf_fund_daily_em()
            df = df[df["基金代码"] == code]
            if df.empty:
                return pd.DataFrame()
            raw = self.ak.fund_etf_hist_sina(symbol=code)
            if raw is None or raw.empty:
                return pd.DataFrame()
            out = raw.reset_index()
            out.columns = ["date", "open", "high", "low", "close", "volume"]
            out["amount"] = out["close"] * out["volume"]
        else:
            raw = self.ak.stock_zh_a_hist(symbol=code, period="daily",
                                          start_date=start, end_date=end, adjust="qfq")
            if raw is None or raw.empty:
                return pd.DataFrame()
            out = raw.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                "最低": "low", "成交量": "volume", "成交额": "amount"})
        cols = ["date", "open", "high", "low", "close", "volume", "amount"]
        for c in cols:
            if c not in out:
                out[c] = 0.0
        out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
        return out[cols].astype({"open": float, "high": float, "low": float,
                                 "close": float, "volume": float, "amount": float})

    def fetch_index(self, days=300):
        raw = self.ak.index_zh_a_hist(symbol="000300", period="daily")
        if raw is None or raw.empty:
            return pd.DataFrame()
        out = raw.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount"})
        cols = ["date", "open", "high", "low", "close", "volume", "amount"]
        out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
        return out[cols].tail(days)


# ---------------- baostock ----------------
class BaostockSource(BaseSource):
    name = "baostock"

    def __init__(self):
        import baostock as bs
        self.bs = bs
        self._logged = False

    def _login(self):
        if not self._logged:
            self.bs.login()
            self._logged = True

    def fetch_kline(self, code, days=2500, start=None):
        code = str(code)
        if detect_type(code) == "lof":
            return pd.DataFrame()  # baostock不覆盖场内基金
        self._login()
        bs_code = f"sh.{code}" if code.startswith("6") else (
            f"sz.{code}" if code.startswith(("0", "3")) else f"sh.{code}")
        start = start or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rs = self.bs.query_history_k_data_plus(
            bs_code, "date,open,high,low,close,volume,amount",
            start_date=start, frequency="d", adjustflag="2")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        if df.empty:
            return df
        return df.astype({"open": float, "high": float, "low": float,
                          "close": float, "volume": float, "amount": float})

    def fetch_index(self, days=300):
        self._login()
        start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")
        rs = self.bs.query_history_k_data_plus(
            "sh.000300", "date,open,high,low,close,volume,amount",
            start_date=start, frequency="d", adjustflag="3")
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        if df.empty:
            return df
        return df.astype({"open": float, "high": float, "low": float,
                          "close": float, "volume": float, "amount": float}).tail(days)


# ---------------- 合成数据（离线确定性） ----------------
class SyntheticSource(BaseSource):
    """确定性随机游走：种子由code派生，保证每次结果一致。联网失败时的兜底。"""
    name = "synthetic"

    def _gen(self, code: str, days: int, base_price: float, vol_daily: float,
             vol_base: float, drift: float = 0.0):
        rng = random.Random(f"seed-{code}")
        n = days
        calendar_days = int(n * 1.5) + 10
        start = datetime.now() - timedelta(days=calendar_days)
        dates, opens, highs, lows, closes, volumes, amounts = [], [], [], [], [], [], []
        price = base_price
        # 预置一段"下跌9转后放量反转"形态，保证演示能看到信号
        trend_seq = [math.sin(i / n * math.pi * 2) * 0.01 + drift for i in range(calendar_days)]
        for i in range(calendar_days):
            d = start + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            ret = trend_seq[i] + rng.gauss(0, vol_daily)
            price = max(price * (1 + ret), 0.1)
            o = price * (1 + rng.gauss(0, vol_daily * 0.3))
            h = max(o, price) * (1 + abs(rng.gauss(0, vol_daily * 0.4)))
            l = min(o, price) * (1 - abs(rng.gauss(0, vol_daily * 0.4)))
            v = max(vol_base * (1 + rng.gauss(0, 0.4)) * (2.0 if i == n - 1 else 1.0), 1.0)
            dates.append(d.strftime("%Y-%m-%d"))
            opens.append(round(o, 3)); highs.append(round(h, 3)); lows.append(round(l, 3))
            closes.append(round(price, 3)); volumes.append(round(v, 0))
            amounts.append(round(v * price, 0))
        return pd.DataFrame({"date": dates, "open": opens, "high": highs, "low": lows,
                             "close": closes, "volume": volumes, "amount": amounts})

    def fetch_kline(self, code, days=2500, start=None):
        code = str(code)
        base = 8.0 if detect_type(code) == "lof" else 100.0
        vol = 0.02 if detect_type(code) == "lof" else 0.018
        volbase = 2e7 if detect_type(code) == "lof" else 5e7
        df = self._gen(code, min(days, 1200), base, vol, volbase,
                       drift=0.0004 if code.endswith("9") else 0.0)
        # 演示形态：代码以9结尾的标的，尾部10日构造"下跌九转+末日放量"
        if code.endswith("9"):
            df = self._tail_bottom_nine(df)
        return df

    @staticmethod
    def _tail_bottom_nine(df: pd.DataFrame) -> pd.DataFrame:
        """尾部10根构造连续下跌（每日约-0.9%），末根完成低9并放量。确定性。"""
        n = len(df)
        if n < 30:
            return df
        p0 = float(df["close"].iloc[n - 11])
        for k in range(10):
            i = n - 10 + k
            c = p0 * (1 - 0.009 * (k + 1))
            df.at[df.index[i], "close"] = round(c, 3)
            df.at[df.index[i], "open"] = round(c * 1.002, 3)
            df.at[df.index[i], "high"] = round(c * 1.010, 3)
            df.at[df.index[i], "low"] = round(c * 0.991, 3)
            v = float(df["volume"].iloc[i])
            df.at[df.index[i], "amount"] = round(v * c, 0)
        last = n - 1
        df.at[df.index[last], "volume"] = round(float(df["volume"].iloc[last]) * 2.6, 0)
        df.at[df.index[last], "amount"] = (round(float(df["volume"].iloc[last])
                                                 * float(df["close"].iloc[last]), 0))
        return df

    def fetch_index(self, days=300):
        return self._gen("INDEX-300", days, 3800, 0.008, 2e8)


# ---------------- 腾讯直连（股票/LOF/指数全覆盖） ----------------
class TencentSource(BaseSource):
    """ifzq.gtimg.cn 日线。返回 [date, open, close, high, low, volume(手)]。
    成交额以 volume×100×close 近似（排序口径足够）。指数用 day 键。
    双主机：web.ifzq 被WAF拦截(501跳转)时自动切 ifzq。"""
    name = "tencent"
    BASES = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
             "https://ifzq.gtimg.cn/appstock/app/fqkline/get")

    @staticmethod
    def _symbol(code: str) -> str:
        code = str(code)
        if code.startswith(("6", "5")):
            return f"sh{code}"
        return f"sz{code}"

    def _pull(self, symbol: str, days: int) -> pd.DataFrame:
        """分页拉取：腾讯单次上限640根，以结束日锚定向前翻页。"""
        all_rows: list = []
        end_date = datetime.now().strftime("%Y-%m-%d")
        seen_dates = set()
        for _ in range(math.ceil(days / 640) + 1):
            q = f"param={symbol},day,,{end_date},640,qfq"
            r = None
            last_exc = None
            for base, backoff in ((self.BASES[0], 0), (self.BASES[0], 0.8),
                                  (self.BASES[1], 0.5), (self.BASES[1], 3.0),
                                  (self.BASES[1], 8.0)):   # 限流退避+双主机切换
                if backoff:
                    time.sleep(backoff)
                try:
                    r = requests.get(f"{base}?{q}", timeout=15)
                    r.raise_for_status()
                    break
                except Exception as e:
                    last_exc, r = e, None
            if r is None:
                raise last_exc
            data = r.json().get("data", {}).get(symbol, {})
            rows = data.get("qfqday") or data.get("day") or []
            if not rows:
                break
            fresh = [k for k in rows if k[0] not in seen_dates]
            if not fresh:
                break
            for k in fresh:
                seen_dates.add(k[0])
            all_rows = fresh + all_rows
            earliest = rows[0][0]
            if len(all_rows) >= days:
                break
            end_date = (datetime.strptime(earliest, "%Y-%m-%d")
                        - timedelta(days=1)).strftime("%Y-%m-%d")
            time.sleep(0.12)          # 翻页间小睡，降低触发限流的概率
        out = []
        for k in all_rows[-days:]:   # 保留最近 days 根
            d, o, c, h, l, v = k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
            out.append({"date": d, "open": o, "high": h, "low": l, "close": c,
                        "volume": v * 100, "amount": v * 100 * c})
        return pd.DataFrame(out).sort_values("date").reset_index(drop=True)

    def fetch_kline(self, code, days=2500, start=None):
        try:
            return self._pull(self._symbol(code), min(days, 2000))
        except Exception:
            return pd.DataFrame()

    def fetch_index(self, days=300):
        for sym in ("sh000300",):
            try:
                df = self._pull(sym, days)
                if not df.empty:
                    return df
            except Exception:
                continue
        return pd.DataFrame()


# ---------------- 多源管理 ----------------
class MultiSourceManager:
    def __init__(self, cfg: dict):
        order = cfg.get("data_sources", ["akshare", "baostock", "synthetic"])
        self.allow_synthetic = cfg.get("allow_synthetic_fallback", True)
        self.sources: list = []
        self._instantiated = []
        for name in order:
            if name == "synthetic" and not self.allow_synthetic:
                continue
            self._instantiated.append(name)
        self._fallback_used = None

    def _get(self, name: str):
        for s in self.sources:
            if s.name == name:
                return s
        try:
            if name == "akshare":
                src = AkshareSource()
            elif name == "baostock":
                src = BaostockSource()
            elif name == "tencent":
                src = TencentSource()
            elif name == "synthetic":
                src = SyntheticSource()
            else:
                return None
            self.sources.append(src)
            return src
        except Exception:
            return None

    def fetch_kline(self, code: str, days: int = 2500, reject_synthetic: bool = False) -> pd.DataFrame:
        """reject_synthetic=True 时禁用合成数据兜底（回测等场景绝不用假价格）。"""
        self._fallback_used = None
        for name in self._instantiated:
            src = self._get(name)
            if not src:
                continue
            try:
                df = src.fetch_kline(code, days)
                if df is not None and not df.empty:
                    if name == "synthetic":
                        if reject_synthetic:
                            return pd.DataFrame()
                        self._fallback_used = "synthetic"
                    return df
            except Exception:
                continue
        return pd.DataFrame()

    def fetch_index(self, days: int = 300) -> pd.DataFrame:
        for name in self._instantiated:
            src = self._get(name)
            if not src:
                continue
            try:
                df = src.fetch_index(days)
                if df is not None and not df.empty:
                    return df
            except Exception:
                continue
        return pd.DataFrame()

    @property
    def using_synthetic(self) -> bool:
        return self._fallback_used == "synthetic"
