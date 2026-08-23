"""渲染典型微信推送卡片：S级买入信号 / A级卖出信号 / LOF溢价预警 / 晚报。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from domain.models import LOFState, SignalResult, VolumeProfile
from presentation.formatter import (format_evening_report, format_lof_card,
                                    format_signal_card, set_web_base)

cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
set_web_base(cfg.get("web", {}).get("public_url", ""))  # 深链用当前隧道地址

# --- 卡1：S级买入（底部九转完成 + 放量） ---
sig = SignalResult(code="601899", name="紫金矿业", level="S", action="buy", score=8,
                   turn=-9, period="day", trade_date="2026-08-14", position_ratio=0.3,
                   stop_loss=17.42, ref_price=18.86,
                   reasons=["九转: 日线低9完成，结构反转", "量能: 放量上攻(模型2)，量分位92%",
                            "趋势: 站上20日线，MACD金叉", "日/周共振: 周线低6同向"])
vp = VolumeProfile(vol_ratio=2.6, volume_percentile=0.92, surge_type="up")
print(format_signal_card(sig, vp, "bear", {"count": 14, "win_rate_10d": 0.71}))

# --- 卡2：A级卖出（顶部九转 + 恐慌砸盘） ---
sig2 = SignalResult(code="300059", name="东方财富", level="A", action="sell", score=6,
                    turn=9, period="day", trade_date="2026-08-14", position_ratio=0.4,
                    ref_price=21.35, reasons=["九转: 日线高9完成，见顶结构",
                                              "量能: 恐慌砸盘(模型3)", "日线RSI顶背离"])
vp2 = VolumeProfile(vol_ratio=1.9, volume_percentile=0.87, surge_type="down")
print("\n" + format_signal_card(sig2, vp2, "bull"))

# --- 卡3：LOF溢价预警 ---
lof = LOFState(code="501018", name="南方原油LOF", price=1.486,
               nav_official_est=1.4123, nav_reference_est=1.4180, nav_source="estimate",
               premium_official=5.22, premium_reference=4.80, premium_percentile=0.97,
               share_chg_pct=8.4, note="溢价处于近一年97%分位，且份额一周激增8.4%（资金高位申购），警惕溢价回落双杀")
print("\n" + format_lof_card(lof))

# --- 卡4：晚报（B级聚合+胜率统计） ---
b_sigs = [SignalResult(code="600519", name="贵州茅台", level="B", turn=-7, score=5),
          SignalResult(code="600036", name="招商银行", level="B", turn=-7, score=3),
          SignalResult(code="601318", name="中国平安", level="B", turn=4, score=3)]
stats = {"S级买入": {"count": 14, "win_rate_10d": 0.71, "avg_ret_10d": 3.2},
         "A级卖出": {"count": 9, "win_rate_10d": 0.78, "avg_ret_10d": -2.1}}
print("\n" + format_evening_report(b_sigs, stats, "range"))
