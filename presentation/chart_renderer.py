"""图表渲染器：信号K线卡 / LOF溢价卡 → PNG（2倍分辨率，微信可直发）。

设计规格（与推送设计稿一致）：
- 信号K线卡：主图(K线+MA5+止损线) + 量能图(信号日量比高亮) + 九转计数标注(高转上方/低转下方)
  + 底部信息胶囊(量比/仓位/止损)。标注数据全部来自领域层计算结果，渲染器只画不算。
- LOF溢价卡：主图(场内价 vs 估算净值双线) + 溢价%副图(带0轴) + 信息胶囊(当前溢价/分位)。
mplfinance 不可用时优雅降级返回 None。
"""
import os
from datetime import datetime

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.font_manager as _fm
    import matplotlib.pyplot as plt
    import mplfinance as mpf
    import pandas as pd
    # 显式注册系统中文字体（嵌入Python的matplotlib可能不扫描系统字体目录）
    for _fp in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                "C:/Windows/Fonts/simsun.ttc"):
        if os.path.exists(_fp):
            _fm.fontManager.addfont(_fp)
    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei",
                                              "SimSun", "Arial Unicode MS"]
    matplotlib.rcParams["axes.unicode_minus"] = False
    HAS_MPF = True
except Exception:
    HAS_MPF = False

OUTPUT_DIR = os.path.join("data", "charts")
_UP, _DN, _ACC = "#E8463A", "#2E9E5B", "#3B7DD8"  # A股红涨绿跌


def render_kline_chart(df, sig=None, lookback: int = 40, vol_ratio=None,
                       code: str = None, name: str = "") -> str:
    """信号K线卡。返回PNG路径；依赖缺失/数据不足/异常返回None。
    code/name：sig为None时的图命名与标题（单一策略九转卡场景）。"""
    try:
        return _render_kline(df, sig, lookback, vol_ratio, code, name)
    except Exception:
        return None


def render_premium_chart(df, st=None, lookback: int = 90) -> str:
    """LOF溢价卡：场内价 vs 估算净值 + 溢价%副图。"""
    try:
        return _render_premium(df, st, lookback)
    except Exception:
        return None


def _prep(df, lookback: int):
    d = df.tail(lookback).copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.set_index("date")


def _capsule(fig, text: str):
    """底部信息胶囊行。"""
    fig.text(0.5, 0.012, text, ha="center", va="bottom", fontsize=10.5,
             color="#333333", bbox=dict(boxstyle="round,pad=0.45", fc="#F2F4F7",
                                        ec="#C9CED6", lw=0.8))


def _render_kline(df, sig, lookback: int, vol_ratio, code=None, name="") -> str:
    if not HAS_MPF or df is None or len(df) < 30:
        return None
    d = _prep(df, lookback)
    n = len(d)

    from domain.nine_turns import calc_turn_counts
    counts = calc_turn_counts(df["close"].astype(float).tolist())[-n:]

    ma5 = d["close"].rolling(5).mean()
    apds = [mpf.make_addplot(ma5, color=_ACC, width=1.2, label="MA5")]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    code = code or getattr(sig, "code", "chart")
    name = name or getattr(sig, "name", "")
    path = os.path.join(OUTPUT_DIR, f"{code}_{datetime.now():%Y%m%d_%H%M%S}.png")

    mc = mpf.make_marketcolors(up=_UP, down=_DN, edge="inherit", wick="inherit", volume="inherit")
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle="--", gridcolor="#E8E8E8",
                               facecolor="white")

    title = f"{name} {code}"
    if sig:
        act = {"buy": "买入", "sell": "卖出", "hold": "观望"}.get(sig.action, sig.action)
        title += f"  {sig.level}级{act} · 得分{sig.score}"
        if abs(getattr(sig, "turn", 0)) == 9:
            title += f" · {'低' if sig.turn < 0 else '高'}9完成"

    fig, axes = mpf.plot(d, type="candle", volume=True, style=style, addplot=apds,
                         returnfig=True, figsize=(12.8, 7.2),  # savefig时2倍分辨率输出
                         title=title, tight_layout=False)
    ax, axv = axes[0], axes[2]

    # 九转计数：高转标K线上方(红)，低转标K线下方(绿)，6+加粗，9徽标收尾
    hi = d["high"].astype(float).tolist()
    lo = d["low"].astype(float).tolist()
    pad = (max(hi) - min(lo)) * 0.025
    for i, c in enumerate(counts):
        if not c:
            continue
        strong = abs(c) >= 6
        if c > 0:  # 高转（卖出结构）→ 上方
            ax.text(i, hi[i] + pad, str(c), ha="center", va="bottom", fontsize=9,
                    color=_UP if strong else "#999999",
                    fontweight="bold" if strong else "normal")
        else:      # 低转（买入结构）→ 下方
            ax.text(i, lo[i] - pad, str(-c), ha="center", va="top", fontsize=9,
                    color=_DN if strong else "#999999",
                    fontweight="bold" if strong else "normal")

    # 止损线（结构低点外2%，来自信号引擎）
    if sig and getattr(sig, "stop_loss", None):
        ax.axhline(sig.stop_loss, color=_UP, linestyle="--", lw=1.1, alpha=0.85)
        ax.text(n - 0.5, sig.stop_loss, f" 止损 {sig.stop_loss:.2f}",
                ha="right", va="bottom", fontsize=9.5, color=_UP)

    # 信号日量柱高亮 + 量比标注
    vols = d["volume"].astype(float).tolist()
    if n >= 2:
        avg5 = sum(vols[-6:-1]) / 5 if n >= 6 else sum(vols[:-1]) / (n - 1)
        if avg5 > 0:
            ratio = vol_ratio or (vols[-1] / avg5)
            axv.bar(n - 1, vols[-1] * 1.0, color="#F0A400", width=0.7, zorder=3)
            ymax = max(vols) * 1.12
            axv.set_ylim(0, ymax)
            axv.text(n - 1, vols[-1] + ymax * 0.03, f"{ratio:.1f}×",
                     ha="center", fontsize=10, color="#B57A00", fontweight="bold")

    # 底部信息胶囊：量比 / 仓位 / 止损
    caps = []
    if vol_ratio:
        caps.append(f"量比 {vol_ratio:.1f}")
    elif sig and getattr(sig, "position_ratio", None):
        caps.append(f"仓位 {sig.position_ratio:.0%}")
    if sig and getattr(sig, "stop_loss", None):
        caps.append(f"止损 ¥{sig.stop_loss:,.2f}")
    if sig and getattr(sig, "turn", 0):
        caps.append(f"九转 {sig.turn:+d}")
    if caps:
        _capsule(fig, "   ·   ".join(caps) + "   ·   示意以实盘为准")

    fig.subplots_adjust(bottom=0.09)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


def _render_premium(df, st, lookback: int) -> str:
    if not HAS_MPF or df is None or len(df) < 30:
        return None
    d = _prep(df, lookback)
    c = d["close"].astype(float)
    nav = c.shift(1)  # 估算净值（昨收近似口径，与领域层一致）
    prem = ((c / nav - 1) * 100).dropna()

    code = getattr(st, "code", "chart")
    name = getattr(st, "name", "")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"prem_{code}_{datetime.now():%Y%m%d_%H%M%S}.png")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12.8, 7.2), dpi=150,
                                   gridspec_kw={"height_ratios": [2.4, 1]},
                                   sharex=True)
    x = range(len(d))
    ax1.plot(x, c, color="#333333", lw=1.5, label="场内价")
    ax1.plot(x, nav, color=_ACC, lw=1.3, ls="--", label="估算净值")
    ax1.fill_between(x, c, nav, where=(c > nav).tolist(),
                     color=_UP, alpha=0.10, interpolate=True)
    ax1.fill_between(x, c, nav, where=(c <= nav).tolist(),
                     color=_DN, alpha=0.10, interpolate=True)
    ax1.set_title(f"{name} {code} · 场内价 vs 估算净值", fontsize=13)
    ax1.legend(loc="upper left", fontsize=9, frameon=False)
    ax1.grid(ls="--", color="#E8E8E8")

    colors = [_UP if v >= 0 else _DN for v in prem]
    ax2.bar(list(prem.index), prem.tolist(), color=colors, width=0.8)
    ax2.axhline(0, color="#666666", lw=0.8)
    if st is not None and getattr(st, "premium_official", None) is not None:
        ax2.axhline(st.premium_official, color="#F0A400", ls=":", lw=1.2)
        ax2.text(0.5, st.premium_official, f" 当前{st.premium_official:+.2f}%",
                 fontsize=9.5, color="#B57A00", va="bottom")
    ax2.set_ylabel("溢价 %")
    ax2.grid(ls="--", color="#E8E8E8")

    # x轴：稀疏日期标签
    ticks = list(range(0, len(d), max(1, len(d) // 8)))
    ax2.set_xticks(ticks)
    ax2.set_xticklabels([d.index[i].strftime("%m-%d") for i in ticks], fontsize=9)

    caps = []
    if st is not None:
        if getattr(st, "premium_official", None) is not None:
            caps.append(f"官方溢价 {st.premium_official:+.2f}%")
        if getattr(st, "premium_reference", None) is not None:
            caps.append(f"参考 {st.premium_reference:+.2f}%")
        if getattr(st, "premium_percentile", None) is not None:
            caps.append(f"分位 {st.premium_percentile:.0%}")
    if caps:
        _capsule(fig, "   ·   ".join(caps))

    fig.subplots_adjust(bottom=0.10, hspace=0.12)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path
