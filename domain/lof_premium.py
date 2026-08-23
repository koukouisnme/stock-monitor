"""LOF/ETF 溢价引擎：双口径估算净值、盘口溢价梯、历史分位、份额流向、自校准。

估算模型（与设计一致）：
  估算净值 = 昨官方净值 × (1 + 底层资产变动 × 仓位系数 + 汇率变动 × 外币敞口)
- 官方口径：汇率用中间价
- 参考口径：汇率用离岸CNH（反映场内定价预期）
- IOPV 可用时直接替代（精度最高）
纯函数。
"""
from typing import Optional

from .models import LOFState


def estimate_nav(prev_nav: float, asset_chg: float, position: float,
                 fx_chg: float = 0.0, fx_exposure: float = 0.0) -> float:
    """参数化净值估算。asset_chg: 底层资产当日涨跌; fx_chg: 汇率变动(人民币计价)。"""
    return prev_nav * (1.0 + asset_chg * position + fx_chg * fx_exposure)


def premium_pct(price: float, nav: float) -> float:
    if not nav:
        return 0.0
    return (price - nav) / nav * 100.0


def orderbook_premium_ladder(price_levels: dict, nav: float) -> dict:
    """盘口溢价梯：{档位: 价格} → {档位: 溢价%}。price_levels 如 {'b1':..,'s1':..}"""
    return {k: round(premium_pct(v, nav), 2) for k, v in price_levels.items() if nav}


def premium_percentile(premium_series: list, current: float) -> float:
    """当前溢价在历史序列中的分位。"""
    if not premium_series:
        return 0.5
    below = sum(1 for x in premium_series if x <= current)
    return below / len(premium_series)


def share_flow_signal(premium_pct_val: float, share_chg_pct: float, cfg: dict) -> tuple:
    """份额流向 × 溢价状态 → (信号类型, 说明)。"""
    watch = float(cfg.get("premium_watch", 3.0))
    surge = float(cfg.get("share_surge_pct", 5.0))
    if premium_pct_val >= watch and share_chg_pct >= surge:
        return ("converge_risk", f"高溢价{premium_pct_val:.1f}%且份额+{share_chg_pct:.1f}%，套利盘进场，溢价收敛在即")
    if premium_pct_val >= watch and abs(share_chg_pct) < surge:
        return ("premium_holds", f"高溢价{premium_pct_val:.1f}%且份额冻结，溢价或维持")
    if premium_pct_val <= -float(cfg.get("discount_watch", 2.0)) and share_chg_pct <= -surge:
        return ("discount_deepening", f"折价{premium_pct_val:.1f}%且份额-{share_chg_pct:.1f}%，赎回压制或加深")
    return ("", "")


def evaluate_lof(code: str, name: str, price: float,
                 prev_nav: float, asset_chg: float,
                 position: Optional[float] = None,
                 fx_chg_onshore: float = 0.0, fx_chg_offshore: float = 0.0,
                 fx_exposure: float = 0.0, iopv: Optional[float] = None,
                 premium_history: Optional[list] = None,
                 share_chg_pct: float = 0.0, cfg: Optional[dict] = None) -> LOFState:
    """LOF 溢价评估主入口（三级降级：IOPV > 估算 > 昨净值）。"""
    cfg = cfg or {}
    if position is None:
        position = float(cfg.get("default_position", 0.93))

    st = LOFState(code=code, name=name, price=price)
    if iopv and iopv > 0:
        st.nav_official_est = st.nav_reference_est = iopv
        st.nav_source = "iopv"
    else:
        st.nav_official_est = estimate_nav(prev_nav, asset_chg, position, fx_chg_onshore, fx_exposure)
        st.nav_reference_est = estimate_nav(prev_nav, asset_chg, position, fx_chg_offshore, fx_exposure)
        st.nav_source = "estimate"
        if asset_chg == 0.0:      # 无指数数据时降级昨净值
            st.nav_official_est = st.nav_reference_est = prev_nav
            st.nav_source = "official"

    st.premium_official = round(premium_pct(price, st.nav_official_est), 2)
    st.premium_reference = round(premium_pct(price, st.nav_reference_est), 2)
    st.premium_t1 = round(premium_pct(price, prev_nav), 2) if prev_nav > 0 else 0.0
    st.spread_gap = round(abs(st.premium_official - st.premium_reference), 2)
    st.premium_percentile = round(
        premium_percentile(premium_history or [], st.premium_official), 2)
    st.share_chg_pct = share_chg_pct

    flow, flow_note = share_flow_signal(st.premium_official, share_chg_pct, cfg)
    notes = []
    p = st.premium_official
    if p >= float(cfg.get("premium_warn", 5.0)):
        notes.append(f"溢价{p:.1f}%≥警告线{cfg.get('premium_warn', 5.0)}%")
    elif p >= float(cfg.get("premium_watch", 3.0)):
        notes.append(f"溢价{p:.1f}%≥关注线")
    elif p <= -float(cfg.get("discount_watch", 2.0)):
        notes.append(f"折价{abs(p):.1f}%，潜在套利空间")
    if st.premium_percentile >= float(cfg.get("percentile_warn", 0.95)):
        notes.append(f"溢价分位{st.premium_percentile:.0%}，历史极端")
    if st.spread_gap > 0.5:
        notes.append(f"双口径差{st.spread_gap:.2f}%，估算不确定性高，信号降级")
    if flow:
        notes.append(flow_note)
    st.note = "；".join(notes)
    return st


def calibrate_position(prev_nav: float, official_nav: float, asset_chg: float,
                       current_position: float, lr: float = 0.3) -> float:
    """自校准闭环：用当晚官方净值回归修正仓位系数。"""
    if prev_nav <= 0:
        return current_position
    actual = official_nav / prev_nav - 1.0
    if abs(asset_chg) < 1e-6:
        return current_position
    implied = actual / asset_chg
    new_pos = current_position + lr * (implied - current_position)
    return round(max(0.5, min(1.0, new_pos)), 4)
