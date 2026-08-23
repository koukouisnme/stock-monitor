"""量能分析：三级放量检测 + 量价四大模型 + 双口径量比。纯函数。"""
import numpy as np
import pandas as pd

from .models import VolumeProfile


def volume_profile(df: pd.DataFrame, cfg: dict, period_label: str = "day") -> VolumeProfile:
    """收盘后量能画像（当周期 vs 自身历史）。"""
    vol = df["volume"].astype(float)
    amt = df["amount"].astype(float) if "amount" in df else vol * df["close"]
    n = len(df)
    if n < 25:
        return VolumeProfile()

    ma5v = vol.rolling(5).mean().iloc[-2]           # 前5日均量（不含当日）
    ma5a = amt.rolling(5).mean().iloc[-2]
    vol_ratio = float(vol.iloc[-1] / ma5v) if ma5v else 0.0
    amt_ratio = float(amt.iloc[-1] / ma5a) if ma5a else 0.0

    win = min(int(cfg.get("percentile_window", 60)), n - 1)
    hist_vol = vol.iloc[-win - 1:-1]
    pct = float((hist_vol < vol.iloc[-1]).mean()) if len(hist_vol) else 0.0

    strong = float(cfg.get("vol_ratio_strong", 1.5))
    chg = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) if n >= 2 else 0.0
    is_surge = vol_ratio >= strong or pct >= float(cfg.get("percentile_threshold", 0.9))

    surge_type = ""
    if is_surge:
        if chg > 0.03:
            surge_type = "up"
        elif chg < -0.03:
            surge_type = "down"
        elif abs(chg) <= 0.01:
            surge_type = "stagnant"
    elif vol_ratio < 0.3:
        surge_type = "shrink"

    vp = VolumeProfile(vol_ratio=vol_ratio, vol_ratio_period=vol_ratio,
                       amt_ratio=amt_ratio, amount=float(amt.iloc[-1]),
                       volume_percentile=pct, is_surge=is_surge, surge_type=surge_type)
    vp.models = volume_price_models(df)
    return vp


def coarse_filter(snapshot_rows: list, cfg: dict) -> list:
    """第一级粗筛：全市场快照行(dict)过滤，返回候选池。"""
    out = []
    for r in snapshot_rows:
        vr = float(r.get("vol_ratio") or 0)
        tx = float(r.get("turnover_x") or 0)
        ax = float(r.get("amount_x") or 0)
        if (vr >= float(cfg.get("vol_ratio_coarse", 2.0))
                or tx >= float(cfg.get("turnover_x_coarse", 3.0))
                or ax >= float(cfg.get("amount_x_coarse", 2.5))):
            out.append(r)
    return out


def volume_price_models(df: pd.DataFrame) -> dict:
    """量价四大模型（设计第三层）。返回命中的模型字典。"""
    n = len(df)
    if n < 25:
        return {}
    close = df["close"].astype(float).values
    vol = df["volume"].astype(float).values
    ma20v = df["volume"].astype(float).rolling(20).mean().values
    models = {}

    # 模型1: 缩量回调后放量收涨（标准启动）
    if (np.nanmean(vol[-5:-1]) < np.nanmean(ma20v[-5:-1]) * 0.75
            and vol[-1] > ma20v[-1] * 1.4 and close[-1] > close[-2]):
        models["model_1_shrink_then_break"] = True

    # 模型2: 低位持续放量而价格横盘（吸筹）
    rng = (close[-10:].max() - close[-10:].min()) / max(close[-10:].mean(), 1e-9)
    if np.nanmean(vol[-10:]) > np.nanmean(ma20v[-10:]) * 1.2 and rng < 0.06:
        models["model_2_accumulation"] = True

    # 模型3: 高位放量滞涨（出货嫌疑）
    if (close[-1] > np.max(close[-20:]) * 0.95 and vol[-1] > ma20v[-1] * 1.8
            and abs(close[-1] / close[-2] - 1) < 0.01):
        models["model_3_top_stagnant"] = True

    # 模型4: 天量无天价（顶部确认/变盘）
    if vol[-1] == np.max(vol[-min(120, n):]) and close[-1] < close[-2]:
        models["model_4_climax_no_high"] = True

    return models
