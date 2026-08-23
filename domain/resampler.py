"""重采样器：日线 → 周线/月线（本地派生，不重复拉取）。纯函数。"""
import pandas as pd


def _resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    for col in ("volume", "amount"):
        if col in d.columns:
            agg[col] = "sum"
    out = d.resample(rule, on="date").agg(agg).dropna(subset=["open", "close"])
    out["date"] = out.index.strftime("%Y-%m-%d")
    return out.reset_index(drop=True)


def to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    return _resample(df, "W-FRI")


def to_monthly(df: pd.DataFrame) -> pd.DataFrame:
    return _resample(df, "ME")


def resample(df: pd.DataFrame, period: str) -> pd.DataFrame:
    return {"day": df, "week": to_weekly(df), "month": to_monthly(df)}[period]
