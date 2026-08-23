"""市场状态检测：沪深300的60日均线斜率 + 布林带宽 → 牛/熊/震荡。"""
import pandas as pd

from domain.indicators import ma, boll


def detect_market_state(index_df: pd.DataFrame) -> str:
    if index_df is None or len(index_df) < 60:
        return "range"
    close = index_df["close"].astype(float)
    ma60 = ma(close, 60)
    if pd.isna(ma60.iloc[-1]) or pd.isna(ma60.iloc[-21]):
        return "range"
    slope = (ma60.iloc[-1] - ma60.iloc[-21]) / ma60.iloc[-21]
    _, _, _, bw = boll(close)
    bw_now = float(bw.iloc[-1]) if not pd.isna(bw.iloc[-1]) else 0.1
    if slope > 0.01:
        return "bull"
    if slope < -0.01:
        return "bear"
    if bw_now < 0.06:
        return "range"
    return "range"


def market_state_label(state: str) -> str:
    return {"bull": "牛市", "bear": "熊市", "range": "震荡"}.get(state, "震荡")
