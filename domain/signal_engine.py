"""信号融合分级引擎：六层过滤 → S/A/B/C 分级。纯函数。"""
from typing import Optional

import pandas as pd

from . import indicators as ind
from .models import SignalResult, TurnResult, VolumeProfile
from .nine_turns import structure_extreme


def trend_filter(df: pd.DataFrame, turn: TurnResult, cfg: dict) -> tuple:
    """第一层：趋势过滤。返回(通过, 原因)。"""
    sig_cfg = cfg.get("signal", {})
    n = len(df)
    if n < 60:
        return True, "数据不足60日，仅作参考"

    close = df["close"].astype(float)
    adx_v = float(ind.adx(df).iloc[-1])
    if adx_v > float(sig_cfg.get("adx_max", 40)):
        return False, f"ADX={adx_v:.0f} 单边强趋势，九转易失效"

    _, _, _, bw = ind.boll(close)
    bw_v = float(bw.iloc[-1]) if not pd.isna(bw.iloc[-1]) else 0.2
    if bw_v < float(sig_cfg.get("boll_bw_min", 0.05)):
        return False, f"布林带宽{bw_v:.2f}极度收口，方向不明"

    ma20 = float(ind.ma(close, 20).iloc[-1])
    ma60 = float(ind.ma(close, 60).iloc[-1])
    # 上升趋势中的下跌九转仅为回调，降级处理（不硬拒，返回标记）
    if turn.count < 0 and ma20 > ma60 and close.iloc[-1] > ma20:
        return True, "注意：上升趋势中的低9，仅为回调信号(降半级)"
    if turn.count > 0 and ma20 < ma60 and close.iloc[-1] < ma20:
        return True, "注意：下降趋势中的高9，仅为反弹衰竭(降半级)"
    return True, "趋势过滤通过"


def multi_period_score(turn_day: int, turn_week: Optional[int] = None) -> tuple:
    """第二层：多周期共振。返回(得分, 说明列表)。"""
    score, notes = 0, []
    if turn_day in (9, -9):
        if turn_week is not None:
            same_sign = (turn_day > 0 and turn_week > 0) or (turn_day < 0 and turn_week < 0)
            if same_sign and abs(turn_week) >= 6:
                score += 4
                notes.append(f"周线共振(计数{turn_week}) +4")
            elif same_sign:
                score += 2
                notes.append(f"周线同向(计数{turn_week}) +2")
            elif turn_week != 0:
                score -= 2
                notes.append(f"周线冲突(计数{turn_week}) -2")
    return score, notes


def volume_score(vp: VolumeProfile, adaptive_cfg: dict) -> tuple:
    """第三层：量价验证。返回(得分, 说明列表)。"""
    score, notes = 0, []
    vr_th = float(adaptive_cfg.get("vol_ratio", 1.5))
    if vp.is_surge:
        score += 2
        notes.append(f"放量确认 量比{vp.vol_ratio:.1f}/分位{vp.volume_percentile:.0%} +2")
    elif vp.vol_ratio >= vr_th * 0.8:
        score += 1
        notes.append(f"量能温和放大 量比{vp.vol_ratio:.1f} +1")
    else:
        notes.append(f"量能未确认 量比{vp.vol_ratio:.1f} 0")

    if vp.models.get("model_1_shrink_then_break"):
        score += 2
        notes.append("量价模型1：缩量回调后放量突破 +2")
    if vp.models.get("model_2_accumulation"):
        score += 2
        notes.append("量价模型2：低位持续放量横盘吸筹 +2")
    if vp.models.get("model_4_climax_no_high"):
        score += 1
        notes.append("量价模型4：天量无天价 +1")
    return score, notes


def indicator_score(df: pd.DataFrame, turn: TurnResult, adaptive_cfg: dict) -> tuple:
    """第四层：多指标共振。返回(得分, 说明列表)。"""
    score, notes = 0, []
    close = df["close"].astype(float)
    rsi_low = float(adaptive_cfg.get("rsi_low", 30))
    rsi_high = float(adaptive_cfg.get("rsi_high", 70))

    dif, dea, hist = ind.macd(close)
    if turn.count < 0:
        if ind.macd_bottom_divergence(close, hist):
            score += 2
            notes.append("MACD底背离 +2")
        rsi_v = float(ind.rsi(close).iloc[-1])
        if rsi_v < rsi_low:
            score += 2
            notes.append(f"RSI={rsi_v:.0f} 超卖 +2")
        mid, up, low, _ = ind.boll(close)
        if not pd.isna(low.iloc[-1]) and close.iloc[-1] <= low.iloc[-1] * 1.02:
            score += 2
            notes.append("触及布林下轨 +2")
        k, d, j = ind.kdj(df)
        if j.iloc[-1] < 0 and j.iloc[-1] > j.iloc[-2]:
            score += 1
            notes.append("KDJ负值拐头 +1")
    elif turn.count > 0:
        if ind.macd_top_divergence(close, hist):
            score += 2
            notes.append("MACD顶背离 +2")
        rsi_v = float(ind.rsi(close).iloc[-1])
        if rsi_v > rsi_high:
            score += 2
            notes.append(f"RSI={rsi_v:.0f} 超买 +2")
        mid, up, low, _ = ind.boll(close)
        if not pd.isna(up.iloc[-1]) and close.iloc[-1] >= up.iloc[-1] * 0.98:
            score += 2
            notes.append("触及布林上轨 +2")
        k, d, j = ind.kdj(df)
        if j.iloc[-1] > 100 and j.iloc[-1] < j.iloc[-2]:
            score += 1
            notes.append("KDJ超买拐头 +1")
    return score, notes


def fuse(df: pd.DataFrame, code: str, name: str, turn: TurnResult, vp: VolumeProfile,
         cfg: dict, market_state: str = "range",
         turn_week: Optional[int] = None) -> SignalResult:
    """六层融合主入口。"""
    adaptive = cfg.get("adaptive", {}).get(market_state, {"vol_ratio": 1.5, "rsi_low": 30, "rsi_high": 70})
    sig_cfg = cfg.get("signal", {})
    res = SignalResult(code=code, name=name, turn=turn.count, ref_price=float(df["close"].iloc[-1]))

    # 无结构 → C
    if turn.count == 0:
        res.level, res.action = "C", "hold"
        res.reasons.append("无九转结构")
        return res

    direction_buy = turn.count < 0
    res.action = "buy" if direction_buy else "sell"

    # 第一层：趋势过滤（硬拒）
    ok, note = trend_filter(df, turn, cfg)
    res.reasons.append(f"[趋势] {note}")
    if not ok:
        res.level = "C"
        return res

    # 第二~四层：得分
    s2, n2 = multi_period_score(turn.count, turn_week)
    s3, n3 = volume_score(vp, adaptive)
    s4, n4 = indicator_score(df, turn, adaptive)
    res.reasons.extend([f"[周期] {x}" for x in n2] or ["[周期] 无周线数据"])
    res.reasons.extend(n3 and [f"[量价] {x}" for x in n3] or ["[量价] 无"])
    res.reasons.extend(n4 and [f"[指标] {x}" for x in n4] or ["[指标] 无"])

    score = s2 + s3 + s4
    # 结构完成基础分
    if turn.structure_complete:
        score += 3
        res.reasons.append(f"[结构] 九转完成({turn.count:+d}){'含极值验证' if turn.perfected else ''} +3")
    elif abs(turn.count) >= 7:
        score += 1
        res.reasons.append(f"[结构] 九转计数中({turn.count:+d}) +1")

    # 上升趋势中的低9回调降半级
    if "降半级" in res.reasons[0]:
        score -= 2

    res.score = max(score, 0)

    # 第五层：分级 + 仓位 + 止损
    s_th = int(sig_cfg.get("level_s_score", 8))
    a_th = int(sig_cfg.get("level_a_score", 5))
    b_th = int(sig_cfg.get("level_b_score", 2))
    if res.score >= s_th and turn.structure_complete:
        res.level = "S"
        res.position_ratio = 1.0 if direction_buy else 0.0
    elif res.score >= a_th and turn.structure_complete:
        res.level = "A"
        res.position_ratio = 0.7 if direction_buy else 0.3
    elif res.score >= b_th:
        res.level = "B"
        res.position_ratio = 0.3 if direction_buy else 0.7
    else:
        res.level, res.action = "C", "hold"

    lo, hi = structure_extreme(df, turn)
    stop_pct = float(sig_cfg.get("stop_loss_pct", 0.02))
    if direction_buy and lo:
        res.stop_loss = round(lo * (1 - stop_pct), 2)
    elif not direction_buy and hi:
        res.stop_loss = round(hi * (1 + stop_pct), 2)
    return res
