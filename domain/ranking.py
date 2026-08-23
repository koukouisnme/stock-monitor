"""排序引擎：漏斗过滤 + 多键排序（量比/额比/成交额/溢价/九转/涨跌幅）。纯函数。"""
import pandas as pd

from .resampler import resample
from .nine_turns import calc_nine_turns
from .volume import volume_profile

SORT_KEYS = ["vol_ratio", "vol_ratio_period", "amt_ratio", "amount",
             "premium", "turn_abs", "pct_chg"]


def build_snapshot_row(df: pd.DataFrame, code: str, name: str, period: str,
                       cfg: dict, premium: float = None, pct_chg: float = None) -> dict:
    """为单标的单周期构建快照行。"""
    rdf = resample(df, period)
    turn = calc_nine_turns(rdf)
    vp = volume_profile(rdf, cfg.get("volume_surge", {}), period)
    if pct_chg is None and len(rdf) >= 2:
        pct_chg = float(rdf["close"].iloc[-1] / rdf["close"].iloc[-2] - 1) * 100
    return {
        "code": code, "name": name, "period": period,
        "trade_date": str(rdf["date"].iloc[-1]),
        "turn_count": turn.count, "turn_complete": turn.structure_complete,
        "vol_ratio": round(vp.vol_ratio, 2),
        "vol_ratio_period": round(vp.vol_ratio_period, 2),
        "amt_ratio": round(vp.amt_ratio, 2),
        "amount": round(vp.amount, 0),
        "premium": round(premium, 2) if premium is not None else None,
        "pct_chg": round(pct_chg, 2) if pct_chg is not None else None,
        "surge_type": vp.surge_type,
        "close": float(rdf["close"].iloc[-1]),
    }


def funnel_filter(rows: list, cfg: dict) -> list:
    """过滤层：流动性门槛 + 剔除ST。"""
    rk = cfg.get("ranking", {})
    min_amt = float(rk.get("filter_min_amount", 0.0))
    exclude_st = bool(rk.get("exclude_st", True))
    out = []
    for r in rows:
        if r.get("amount") and r["amount"] < min_amt:
            continue
        if exclude_st and "ST" in str(r.get("name", "")):
            continue
        out.append(r)
    return out


def rank(rows: list, key: str = "vol_ratio", ascending: bool = False, top_n: int = 10) -> list:
    """排序层：按指定键排序。turn_abs为九转计数绝对值。"""
    if key not in SORT_KEYS:
        raise ValueError(f"不支持的排序键: {key}，可选: {SORT_KEYS}")

    def val(r):
        if key == "turn_abs":
            v = abs(r.get("turn_count") or 0)
        else:
            v = r.get(key)
        return float("-inf") if v is None else float(v)

    return sorted(rows, key=val, reverse=not ascending)[:top_n]
