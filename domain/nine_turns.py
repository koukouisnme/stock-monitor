"""神奇九转（TD Sequential 简化版）计算。

规则（与设计一致）：
- 上涨结构（顶部九转候选）：Close[i] > Close[i-4] 连续成立，计数+1
- 下跌结构（底部九转候选）：Close[i] < Close[i-4] 连续成立，计数+1
- 计数到 9 结构完成，随后重置
- perfection 增强：上涨结构第8/9根最高价 > 第6/7根最高价；下跌结构对称
纯函数，无IO。
"""
from typing import List
import pandas as pd

from .models import TurnResult


def calc_turn_counts(closes: List[float]) -> List[int]:
    """返回带符号计数序列（正=上涨结构计数，负=下跌结构计数，0=无）。"""
    n = len(closes)
    counts = [0] * n
    up = down = 0
    for i in range(4, n):
        if closes[i] > closes[i - 4]:
            up += 1
            down = 0
        elif closes[i] < closes[i - 4]:
            down += 1
            up = 0
        else:
            up = down = 0
        if up >= 9:
            counts[i] = 9
            up = 0
        elif down >= 9:
            counts[i] = -9
            down = 0
        else:
            counts[i] = up if up > 0 else (-down if down > 0 else 0)
    return counts


def _check_perfection(df: pd.DataFrame, counts: List[int], i: int) -> bool:
    """完成9时的极值验证：结构内第8/9根 vs 第6/7根。"""
    if i < 9:
        return False
    seg = counts[i - 8: i + 1]
    idxs = [i - 8 + k for k, v in enumerate(seg) if abs(v) >= 1]
    if len(idxs) < 9:
        return False
    highs = df["high"].values
    lows = df["low"].values
    if counts[i] == 9:      # 顶部结构：8/9高 需突破 6/7高
        return max(highs[idxs[7]], highs[idxs[8]]) > max(highs[idxs[5]], highs[idxs[6]])
    if counts[i] == -9:     # 底部结构：8/9低 需跌破 6/7低
        return min(lows[idxs[7]], lows[idxs[8]]) < min(lows[idxs[5]], lows[idxs[6]])
    return False


def calc_nine_turns(df: pd.DataFrame) -> TurnResult:
    """输入标准K线DataFrame(date,open,high,low,close,volume,amount)。"""
    closes = df["close"].astype(float).tolist()
    counts = calc_turn_counts(closes)
    last = counts[-1] if counts else 0
    complete = last == 9 or last == -9
    perfected = _check_perfection(df, counts, len(counts) - 1) if complete else False
    return TurnResult(count=last, structure_complete=complete,
                      perfected=perfected, history=counts)


def structure_extreme(df: pd.DataFrame, turn: TurnResult) -> tuple:
    """返回(极值低点, 极值高点)：结构窗口内（计数≥1的最后9根）。"""
    hist = turn.history
    n = len(hist)
    start = None
    for k in range(n - 1, -1, -1):
        v = hist[k]
        if (turn.count > 0 and v > 0) or (turn.count < 0 and v < 0) or v == 0:
            if v == 0:
                start = k + 1
                break
        # 连续同向段起点
        seg_vals = hist[max(0, n - 12):]
        # 找连续同号段起点
        sign = 1 if turn.count > 0 else -1
        s = n - 1
        while s > 0 and hist[s - 1] * sign > 0 and hist[s - 1] != 0:
            s -= 1
        start = s
        break
    if start is None or start >= n:
        start = max(0, n - 9)
    window = df.iloc[start:n]
    if window.empty:
        return (None, None)
    return (float(window["low"].min()), float(window["high"].max()))
