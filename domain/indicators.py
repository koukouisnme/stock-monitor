"""技术指标库：MA/MACD/RSI/BOLL/KDJ/ADX/布林带宽。纯函数。"""
import numpy as np
import pandas as pd


def ma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close: pd.Series, n=14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50)


def boll(close: pd.Series, n=20, k=2.0):
    mid = ma(close, n)
    std = close.rolling(n, min_periods=n).std()
    upper = mid + k * std
    lower = mid - k * std
    bw = (upper - lower) / mid.replace(0, np.nan)   # 带宽
    return mid, upper, lower, bw


def kdj(df: pd.DataFrame, n=9, m1=3, m2=3):
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def adx(df: pd.DataFrame, n=14) -> pd.Series:
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
                        index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
                         index=df.index)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0)


def macd_bottom_divergence(close: pd.Series, hist: pd.Series, lookback: int = 20) -> bool:
    """底背离：价格创20日新低而hist未创新低。"""
    if len(close) < lookback or hist.isna().any():
        return False
    c_now, c_min = close.iloc[-1], close.iloc[-lookback:].min()
    h_now, h_min = hist.iloc[-1], hist.iloc[-lookback:].min()
    return c_now <= c_min * 1.001 and h_now > h_min


def macd_top_divergence(close: pd.Series, hist: pd.Series, lookback: int = 20) -> bool:
    if len(close) < lookback or hist.isna().any():
        return False
    c_now, c_max = close.iloc[-1], close.iloc[-lookback:].max()
    h_now, h_max = hist.iloc[-1], hist.iloc[-lookback:].max()
    return c_now >= c_max * 0.999 and h_now < h_max
