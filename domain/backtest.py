"""九转信号历史回测：统计完成九转(±9)后 N 日收益分布。纯函数，无IO。

口径：
- 信号点：带符号计数恰为 ±9 的K线（结构完成）
- 方向：-9=买入（底部衰竭后看涨），+9=卖出（顶部衰竭后看跌）
- 胜率：买入 N日收益>0 为胜；卖出 N日收益<0 为胜
"""
import pandas as pd

from domain.nine_turns import calc_turn_counts
from domain.resampler import resample

HORIZONS = (5, 10, 20)


def backtest_turns(df, horizons=HORIZONS) -> dict:
    """df: 标准K线DataFrame(date,close,...)。返回 {signals, stats}。"""
    closes = [float(c) for c in df["close"]]
    dates = [str(d) for d in df["date"]]
    counts = calc_turn_counts(closes)
    signals = []
    for i, c in enumerate(counts):
        if c not in (9, -9):
            continue
        sig = {"date": dates[i], "type": "buy" if c == -9 else "sell"}
        for h in horizons:
            j = i + h
            sig[f"ret_{h}"] = round((closes[j] / closes[i] - 1) * 100, 2) if j < len(closes) else None
        signals.append(sig)

    def _agg(tp):
        rows = [s for s in signals if s["type"] == tp and s.get("ret_10") is not None]
        out = {"count": len([s for s in signals if s["type"] == tp])}
        for h in horizons:
            vals = [s[f"ret_{h}"] for s in signals
                    if s["type"] == tp and s.get(f"ret_{h}") is not None]
            win = sum(1 for v in vals if (v > 0 if tp == "buy" else v < 0))
            out[f"win_rate_{h}d"] = round(win / len(vals), 3) if vals else None
            out[f"avg_ret_{h}d"] = round(sum(vals) / len(vals), 2) if vals else None
        return out

    return {
        "n_bars": len(closes),
        "signals": signals[-30:],   # 最近30个信号（明细表用）
        "stats": {"buy": _agg("buy"), "sell": _agg("sell")},
    }


def _pct(v, signed=True):
    if v is None:
        return "-"
    return f"{v:+.1f}%" if signed else f"{v:.0%}"


# ---------- 份额制交易模拟（底仓 + 九转加减仓） ----------

def _period_signal_dates(df: pd.DataFrame, period: str) -> dict:
    """周期(week/month)九转信号 → 映射到该周期桶实际最后交易日 {交易日: 带符号计数}。"""
    src = resample(df, period) if period != "day" else df
    counts = calc_turn_counts([float(c) for c in src["close"]])
    out = {}
    if period == "day":
        for i, c in enumerate(counts):
            if c in (9, -9):
                out[str(src["date"].iloc[i])] = c
        return out
    freq = "W-FRI" if period == "week" else "M"
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    last_day = d.groupby(d["date"].dt.to_period(freq))["date"].max()  # 桶→实际最后交易日
    for i, c in enumerate(counts):
        if c not in (9, -9):
            continue
        bucket = pd.Period(pd.Timestamp(src["date"].iloc[i]), freq=freq)
        real = last_day.get(bucket)
        if real is not None:
            out[str(real.date())] = c
    return out


def current_period_turns(df) -> dict:
    """最新一根周/月K线的九转计数（带符号；±9=结构完成，末桶含当前未完成周期）。"""
    out = {}
    for p in ("week", "month"):
        src = resample(df, p)
        counts = calc_turn_counts([float(c) for c in src["close"]])
        out[p] = counts[-1] if counts else 0
    return out


def simulate_shares(df, initial=10000, unit_day=1000, unit_week=3000, unit_month=5000) -> dict:
    """金额制交易模拟：初始资金首日全仓买入，九转信号按固定金额加减仓（按收盘价成交）。

    - 初始：资金 initial 元首日全仓买入；基准权益 = initial
    - 日线低9买/高9卖 unit_day 元；周线 unit_week；月线 unit_month（同日信号金额合并）
    - 卖出受限：持仓市值不足时只卖到 0；买入允许现金为负（融资加仓，标注）
    - 权益 = 现金 + 持仓 × 收盘价；曲线含现金/股票市值，便于观察资金结构
    """
    units = {"day": unit_day, "week": unit_week, "month": unit_month}
    sigs = {p: _period_signal_dates(df, p) for p in ("day", "week", "month")}
    dates = [str(x) for x in df["date"]]
    prices = [float(x) for x in df["close"]]
    base0 = float(initial)
    p0 = prices[0] if prices else 0.0   # 首日收盘价（持有不动基准）
    pos = base0 / prices[0] if prices[0] > 0 else 0.0   # 首日全仓买入（内部记份额）
    cash = 0.0
    invested = base0          # 累计净投入 = 初始资金 + 买入 - 卖出（供参考展示）
    ret_of = lambda pnl: round(pnl / base0 * 100, 2) if base0 else 0.0   # 收益率统一以初始资金为分母
    trades = [{"date": dates[0], "action": "buy", "amount": round(base0, 2),
               "price": round(p0, 3), "mv_after": round(base0, 2),
               "invested_after": round(base0, 2), "pnl_after": 0.0, "ret_after": 0.0,
               "bh_ret_after": 0.0, "excess_after": 0.0, "why": "初始建仓(全仓)"}]
    curve = []
    peak = base0
    max_dd = 0.0
    for dt, px in zip(dates, prices):
        amt = 0.0   # 当日净买卖金额（正=买 负=卖）
        detail = []
        for p in ("day", "week", "month"):
            c = sigs[p].get(dt)
            if c:  # 低9买入记正金额，高9卖出记负金额
                amt += units[p] if c == -9 else -units[p]
                detail.append(f"{p}{'低9' if c == -9 else '高9'}×{units[p]}元")
        if abs(amt) > 1e-9 and px > 0 and dt != dates[0]:   # 首日已建仓，跳过当日信号
            # 口径：策略累计收益 = 权益 - 初始资金（买卖仅为股票/现金间转移，融资为负现金负债）
            # 持有不动收益率 = 当日价/首日价 - 1；超额收益率 = 策略收益率 - 持有不动收益率
            eq_t = lambda: cash + pos * px
            pnl_t = round(eq_t() - base0, 2)
            bh_rt_t = round((px / p0 - 1) * 100, 2) if p0 > 0 else 0.0
            if amt > 0:                     # 净买入（现金可为负=融资）
                cash -= amt
                pos += amt / px
                invested += amt
                trades.append({"date": dt, "action": "buy", "amount": round(amt, 2),
                               "price": round(px, 3), "mv_after": round(pos * px, 2),
                               "invested_after": round(invested, 2),
                               "pnl_after": pnl_t, "ret_after": ret_of(pnl_t),
                               "bh_ret_after": bh_rt_t,
                               "excess_after": round(ret_of(pnl_t) - bh_rt_t, 2),
                               "why": "+".join(detail)})
            else:                           # 净卖出，持仓市值不足则清仓
                q = min(-amt / px, pos)
                sold = q * px
                if q > 0:
                    cash += sold
                    pos -= q
                    invested -= sold
                    trades.append({"date": dt, "action": "sell", "amount": round(sold, 2),
                                   "price": round(px, 3), "mv_after": round(pos * px, 2),
                                   "invested_after": round(invested, 2),
                                   "pnl_after": pnl_t, "ret_after": ret_of(pnl_t),
                                   "bh_ret_after": bh_rt_t,
                                   "excess_after": round(ret_of(pnl_t) - bh_rt_t, 2),
                                   "why": "+".join(detail) + ("(持仓不足部分未卖)" if sold < -amt - 1e-6 else "")})
        mv = pos * px
        eq = cash + mv
        curve.append({"date": dt, "cash": round(cash, 2), "mv": round(mv, 2),
                      "equity": round(eq, 2), "invested": round(invested, 2),
                      "pnl": round(eq - base0, 2),
                      "ret": ret_of(eq - base0)})
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak if peak > 0 else 0)
    eq_end = curve[-1]["equity"] if curve else base0
    pnl_end = eq_end - base0
    total_ret = pnl_end / base0 if base0 else 0
    bh_ret = prices[-1] / prices[0] - 1 if prices else 0
    n_days = len(curve)
    annual = (1 + total_ret) ** (252 / max(n_days, 1)) - 1 if n_days > 30 and total_ret > -1 else None
    return {
        "params": {"initial": initial, "unit_day": unit_day,
                   "unit_week": unit_week, "unit_month": unit_month},
        "stats": {"total_ret": round(total_ret * 100, 2),
                  "buy_hold_ret": round(bh_ret * 100, 2),
                  "excess": round((total_ret - bh_ret) * 100, 2),
                  "annual_ret": None if annual is None else round(annual * 100, 2),
                  "max_drawdown": round(max_dd * 100, 2),
                  "n_trades": len(trades),
                  "mv_end": round(pos * prices[-1], 2), "cash_end": round(cash, 2),
                  "equity_end": round(eq_end, 2),
                  "invested_end": round(invested, 2),
                  "pnl_end": round(pnl_end, 2),
                  "initial_mv": round(base0, 2),   # 初始投入时股票价值（首日全仓市值）
                  "n_days": n_days},
        "curve": curve,            # 全量权益曲线（前端折线图，含现金/市值）
        "bh_curve": [{"date": dt, "ret": round(px / prices[0] * 100 - 100, 2)}
                     for dt, px in zip(dates, prices)],  # 买入持有基准
        "trades": trades,          # 全量交易（折线图标点用；明细表由API截取）
        "margin_used": cash < 0,   # 是否动用融资
    }


def format_backtest_report(code: str, name: str, bt: dict) -> str:
    """文本报告（运维页全池回测输出用）。"""
    lines = [f"── {name} {code}（{bt['n_bars']}根K线）──"]
    for tp, cn in (("buy", "买入(低9)"), ("sell", "卖出(高9)")):
        s = bt["stats"][tp]
        if not s["count"]:
            lines.append(f"  {cn}: 无信号")
            continue
        lines.append(f"  {cn}: {s['count']}次 10日胜率{_pct(s.get('win_rate_10d'), False)} "
                     f"均收益{_pct(s.get('avg_ret_10d'))} | "
                     f"5日均{_pct(s.get('avg_ret_5d'))} 20日均{_pct(s.get('avg_ret_20d'))}")
    return "\n".join(lines)
