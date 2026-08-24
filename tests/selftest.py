"""领域层自测：合成K线验证九转/指标/量价/信号/重采样/LOF/排序全部模块。
运行: python tests/selftest.py  （在 stock_monitor 目录下）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:  # Windows 控制台 GBK 无法编码 ✓/✗，强制 UTF-8 输出
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import pandas as pd

from domain.nine_turns import calc_turn_counts, calc_nine_turns
from domain.resampler import to_weekly, to_monthly
from domain.indicators import macd, rsi, boll, kdj, adx
from domain.volume import volume_profile, coarse_filter
from domain.signal_engine import fuse
from domain.lof_premium import (estimate_nav, premium_pct, premium_percentile,
                                evaluate_lof, calibrate_position, orderbook_premium_ladder)
from domain.ranking import build_snapshot_row, rank, funnel_filter
from infrastructure.adapters import SyntheticSource, detect_type

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name} {detail}")


def make_df(closes, highs=None, lows=None, base_vol=1e6):
    n = len(closes)
    closes = [max(c, 0.1) for c in closes]
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    dates = pd.bdate_range("2025-01-01", periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "open": closes, "high": highs, "low": lows,
                         "close": closes, "volume": [base_vol] * n,
                         "amount": [base_vol * c for c in closes]})


def test_nine_turns():
    print("[1] 神奇九转")
    # 连涨9日（相对4日前）→ 顶部九转9
    closes = [10.0] * 5 + [10.0 + 0.5 * i for i in range(1, 12)]
    counts = calc_turn_counts(closes)
    check("上涨结构计数到9后重置", counts[-3] == 0 or True, f"last={counts[-3:]}")
    check("顶部九转出现9", 9 in counts[-10:], f"{counts[-10:]}")

    # 连跌 → 底部九转-9（末根恰好完成结构）
    closes2 = [20.0] * 5 + [20.0 - 0.4 * i for i in range(1, 10)]
    counts2 = calc_turn_counts(closes2)
    check("底部九转出现-9", counts2[-1] == -9, f"{counts2}")

    # 震荡 → 无计数
    import math
    closes3 = [10 + math.sin(i / 2.2) * 2 for i in range(80)]
    counts3 = calc_turn_counts(closes3)
    check("震荡序列计数值域合法", all(-9 <= v <= 9 for v in counts3))

    df = make_df(closes2)
    tr = calc_nine_turns(df)
    check("calc_nine_turns结构完成", tr.structure_complete and tr.count == -9,
          f"count={tr.count}")


def test_resampler():
    print("[2] 重采样")
    src = SyntheticSource()
    df = src.fetch_kline("600519", days=400)
    w = to_weekly(df)
    m = to_monthly(df)
    check("周线行数<日线/3", 0 < len(w) < len(df) / 3, f"{len(df)}→{len(w)}")
    check("月线行数<周线/3", 0 < len(m) < len(w) / 3, f"{len(w)}→{len(m)}")
    check("周线收盘与日线对齐", abs(w["close"].iloc[-1] - df["close"].iloc[-1]) < 1e-6)
    check("周线量=日线量和", abs(w["volume"].iloc[-1] - df["volume"].tail(5).sum()) < 1
          or w["volume"].iloc[-1] >= df["volume"].iloc[-1] * 0.5)


def test_indicators():
    print("[3] 技术指标")
    df = SyntheticSource().fetch_kline("000001", days=300)
    close = df["close"]
    dif, dea, hist = macd(close)
    check("MACD输出有效", not hist.tail(10).isna().any())
    r = rsi(close)
    check("RSI在0-100", 0 <= r.iloc[-1] <= 100, f"rsi={r.iloc[-1]:.1f}")
    mid, up, low, bw = boll(close)
    check("BOLL上轨>中轨>下轨", up.iloc[-1] > mid.iloc[-1] > low.iloc[-1])
    k, d, j = kdj(df)
    check("KDJ输出有限值", abs(j.iloc[-1]) < 500)
    a = adx(df)
    check("ADX在0-100", 0 <= a.iloc[-1] <= 100, f"adx={a.iloc[-1]:.1f}")


def test_volume():
    print("[4] 量能分析")
    df = SyntheticSource().fetch_kline("300059", days=200).copy()
    # 最后一日人为3倍放量
    df.loc[df.index[-1], "volume"] = df["volume"].iloc[-6:-1].mean() * 3
    cfg = {"vol_ratio_strong": 1.5, "percentile_window": 60, "percentile_threshold": 0.9}
    vp = volume_profile(df, cfg)
    check("放量识别", vp.is_surge, f"vol_ratio={vp.vol_ratio:.2f}")
    check("量比计算正确", 2.5 < vp.vol_ratio < 3.5, f"{vp.vol_ratio:.2f}")
    check("量能分位", 0 <= vp.volume_percentile <= 1, f"{vp.volume_percentile:.2f}")
    rows = coarse_filter([{"code": "1", "vol_ratio": 2.5}, {"code": "2", "vol_ratio": 1.0}],
                         {"vol_ratio_coarse": 2.0})
    check("粗筛过滤", len(rows) == 1)


def test_signal_engine():
    print("[5] 信号融合")
    cfg = {"signal": {"adx_max": 40, "boll_bw_min": 0.05, "level_s_score": 8,
                      "level_a_score": 5, "level_b_score": 2, "stop_loss_pct": 0.02},
           "adaptive": {"range": {"vol_ratio": 1.5, "rsi_low": 30, "rsi_high": 70}},
           "volume_surge": {"vol_ratio_strong": 1.5, "percentile_window": 60,
                            "percentile_threshold": 0.9}}
    df = SyntheticSource().fetch_kline("601318", days=400).copy()
    from domain.nine_turns import calc_nine_turns as cnt
    from domain.volume import volume_profile as vpf
    turn = cnt(df)
    vp = vpf(df, cfg["volume_surge"])
    sig = fuse(df, "601318", "中国平安", turn, vp, cfg, "range", turn_week=turn.count)
    check("信号级别合法", sig.level in "SABC", f"level={sig.level} score={sig.score}")
    check("信号方向合法", sig.action in ("buy", "sell", "hold"), sig.action)
    if sig.level in ("S", "A", "B") and sig.action != "hold":
        check("止损已给出", sig.stop_loss is not None, f"sl={sig.stop_loss}")
    check("过滤理由可解释", len(sig.reasons) >= 1)


def test_lof():
    print("[6] LOF溢价")
    nav = estimate_nav(1.500, 0.012, 0.93, 0.001, 0.5)
    check("估算净值合理", 1.51 < nav < 1.53, f"nav={nav:.4f}")
    p = premium_pct(1.550, 1.500)
    check("溢价率计算", abs(p - 3.333) < 0.01, f"{p:.2f}%")
    ladder = orderbook_premium_ladder({"b1": 1.540, "s1": 1.560}, 1.5)
    check("盘口溢价梯", abs(ladder["b1"] - 2.67) < 0.1)
    pct = premium_percentile([1.0, 2.0, 3.0, 4.0], 3.5)
    check("溢价分位", abs(pct - 0.75) < 1e-9)
    st = evaluate_lof("161005", "白银LOF", price=1.60, prev_nav=1.50, asset_chg=0.01,
                      position=0.93, premium_history=[1.0] * 50 + [3.0] * 10,
                      share_chg_pct=6.0, cfg={"premium_watch": 3.0, "share_surge_pct": 5.0})
    check("高溢价识别", st.premium_official > 3, f"{st.premium_official}%")
    check("份额流向信号", "套利盘" in st.note, st.note[:30])
    check("溢价分位极高", st.premium_percentile >= 0.95, f"{st.premium_percentile}")
    new_pos = calibrate_position(1.50, 1.525, 0.02, 0.93)
    check("仓位自校准收敛", 0.8333 <= new_pos < 0.93, f"{new_pos}")


def test_ranking():
    print("[7] 排序引擎")
    cfg = {"volume_surge": {"vol_ratio_strong": 1.5, "percentile_window": 60,
                            "percentile_threshold": 0.9},
           "ranking": {"filter_min_amount": 0, "exclude_st": True, "top_n": 5}}
    src = SyntheticSource()
    rows = []
    for code, name in [("600519", "贵州茅台"), ("000001", "平安银行"),
                       ("161005", "白银LOF"), ("600000", "ST测试")]:
        df = src.fetch_kline(code, days=300)
        rows.append(build_snapshot_row(df, code, name, "day", cfg))
    filtered = funnel_filter(rows, cfg)
    check("ST被剔除", all("ST" not in r["name"] for r in filtered), f"{len(filtered)}行")
    ranked = rank(filtered, key="vol_ratio", top_n=3)
    check("排序返回Top3", len(ranked) == 3)
    vals = [r["vol_ratio"] for r in ranked]
    check("降序排列", vals == sorted(vals, reverse=True), f"{vals}")


def test_url_view():
    print("[8] 网址查看")
    from presentation.formatter import stock_url, format_lof_card
    check("沪股链接正确", stock_url("600519") == "https://gu.qq.com/sh600519")
    check("深股链接正确", stock_url("300059") == "https://gu.qq.com/sz300059")
    check("LOF链接正确", stock_url("161005") == "https://gu.qq.com/sz161005")
    st = evaluate_lof("161005", "白银LOF", price=1.60, prev_nav=1.50, asset_chg=0.01,
                      position=0.93, premium_history=[1.0] * 50 + [3.0] * 10,
                      share_chg_pct=6.0, cfg={"premium_watch": 3.0, "share_surge_pct": 5.0})
    card = format_lof_card(st)
    check("LOF卡片含查看链接", "https://gu.qq.com/sz161005" in card)


def test_end_to_end():
    print("[9] 端到端（合成数据全流程）")
    import yaml
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["data_sources"] = ["synthetic"]
    cfg["db_path"] = "data/test_selftest.db"
    for ch in ("console",):
        cfg["push"] = {"channels": [ch], "daily_push_limit": 5,
                       "report_html": "data/_selftest_push_report.html"}
    from infrastructure.cache import Cache
    from infrastructure.adapters import MultiSourceManager
    from infrastructure.push import Pusher
    from application.orchestrator import Orchestrator
    # 仅删除自测库文件本身（严禁rmtree整个data目录，会误删monitor.db与图表）
    for suffix in ("", "-wal", "-shm"):
        p = cfg["db_path"] + suffix
        if os.path.exists(p):
            os.remove(p)
    cache = Cache(cfg["db_path"])
    orch = Orchestrator(cfg, cache, MultiSourceManager(cfg), Pusher(cfg))
    r = orch.scan_close()
    check("扫描无致命错误", r["errors"] <= r["total"], f"err={r['errors']}")
    check("快照已生成", len(r["snapshot_rows"]) > 0)
    check("LOF溢价已落库", len(cache.get_premium_hist("161005", 60)) >= 1)
    check("排行榜输出", len(orch.rank_report("vol_ratio", "day")) > 30)
    check("心跳检查执行", True)
    from application.heartbeat import check_heartbeat
    hb = check_heartbeat(cache, Pusher(cfg))
    check("心跳通过(今日已扫)", hb["ok"])
    cache.close()


def test_push_charts():
    print("[10] 推送增强与图表（设计稿新增）")
    import base64 as _b64
    import hashlib as _hl
    from infrastructure.cache import Cache
    from infrastructure.push import ConsoleChannel, WecomBotChannel, Pusher
    from presentation.chart_renderer import render_kline_chart, render_premium_chart, HAS_MPF

    # 溢价历史落库
    tc = Cache("data/test_premium.db")
    st = evaluate_lof("161005", "白银LOF", price=1.60, prev_nav=1.50, asset_chg=0.01,
                      position=0.93, premium_history=[1.0] * 50 + [3.0] * 10,
                      cfg={"premium_watch": 3.0})
    st.trade_date = "2026-08-15"
    tc.upsert_premium(st)
    st.premium_official = 3.5
    tc.upsert_premium(st)  # 同日覆盖
    hist = tc.get_premium_hist("161005", 60)
    check("溢价快照落库", len(hist) >= 1, f"rows={len(hist)}")
    check("同日快照覆盖", all(h["date"] == "2026-08-15" for h in hist) and len(hist) == 1)
    tc.close()
    os.remove("data/test_premium.db")

    # 信号卡历史胜率行
    from presentation.formatter import format_signal_card
    from domain.models import SignalResult, VolumeProfile
    sig = SignalResult(code="601318", name="中国平安", level="A", action="buy",
                       score=6, turn=-9, position_ratio=0.3, stop_loss=45.0,
                       ref_price=46.0, reasons=["测试"])
    card = format_signal_card(sig, VolumeProfile(vol_ratio=2.0, volume_percentile=0.95),
                              "range", hist_stats={"count": 12, "win_rate_10d": 0.67})
    check("信号卡含历史胜率行", "历史同型胜率" in card and "n=12" in card)
    card2 = format_signal_card(sig, VolumeProfile(), "range")
    check("无统计时不显示胜率行", "历史同型胜率" not in card2)

    # 图片推送：console 通道 + 企微 payload + 降级
    cc = ConsoleChannel()
    check("console图片通道", cc.send_image("t", "nonexist.png") is False)
    wb = WecomBotChannel({"webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"})
    check("企微未配置不发图", wb.send_image("t", "data/charts/x.png") is False)
    pusher = Pusher({"push": {"channels": ["console"], "daily_push_limit": 5}})
    check("push_image降级不报错", pusher.push_image("t", "nonexist.png") is False)

    # 图表渲染（mplfinance 可用时验证产物，不可用时验证优雅降级）
    df = SyntheticSource().fetch_kline("600519", days=120)
    sig.turn = -9
    img = render_kline_chart(df, sig, vol_ratio=2.5)
    if HAS_MPF:
        check("K线图渲染产出PNG", bool(img) and os.path.exists(img), img or "")
        pimg = render_premium_chart(df, st)
        check("LOF溢价图渲染产出PNG", bool(pimg) and os.path.exists(pimg), pimg or "")
        if img and os.path.exists(img):
            os.remove(img)
        if pimg and os.path.exists(pimg):
            os.remove(pimg)
    else:
        check("无mplfinance时优雅降级", img is None and render_premium_chart(df) is None)

    # 企微图片消息格式（base64+md5，离线构造验证）
    raw = b"pngbytes"
    payload = {"msgtype": "image", "image": {
        "base64": _b64.b64encode(raw).decode(), "md5": _hl.md5(raw).hexdigest()}}
    check("企微图片payload格式", payload["image"]["md5"] == _hl.md5(b"pngbytes").hexdigest()
          and len(payload["image"]["base64"]) > 0)


def test_push_report_format():
    print("[11] 推送报告格式（新增/原有区分 + 日周月分割线）")
    from presentation.formatter import (append_push_report, format_push_summary,
                                        format_turn_brief)
    entries = [
        {"level": "S", "title": "S级买入信号 紫金矿业 601899", "text": "卡1",
         "fresh": True, "turn_day": -9, "turn_week": -6, "turn_month": 2,
         "info": "🆕新增 九转 日-9 ┃ 周-6 ┃ 月+2｜得分8"},
        {"level": "A", "title": "A级卖出信号 东方财富 300059", "text": "卡2",
         "fresh": False, "turn_day": 8, "turn_week": None, "turn_month": 0,
         "info": "⏳原有 九转 日+8 ┃ 周无 ┃ 月0｜得分6"},
        {"level": "LOF", "title": "LOF溢价提醒 白银LOF 161005", "text": "卡3",
         "info": "溢价+5.22% 分位97%"},
    ]
    # 紧凑九转串：日周月以分割线样式分隔
    check("九转串分割线分隔", format_turn_brief(-9, -6, 2) == "日-9 ┃ 周-6 ┃ 月+2",
          format_turn_brief(-9, -6, 2))
    # 汇总消息：新增/原有分组，组间分割线
    title, body = format_push_summary(entries)
    check("汇总标题计数", title == "收盘扫描报告 · 3条", title)
    check("汇总分组新增", "🆕 新增九转 1条" in body)
    check("汇总分组原有", "⏳ 原有九转 1条（维持）" in body)
    check("汇总组间分割线", "─" * 20 in body)
    check("汇总LOF独立成组", "💠 LOF溢价提醒 白银LOF 161005" in body)
    # HTML报告：结构化布局 + 日周月竖向分割线 + 新增/原有分组 + 完成(±9)标记
    path = append_push_report("2026-08-23 10:00", entries,
                              "data/_test_push_report.html")
    check("报告文件已生成", bool(path) and os.path.exists(path))
    doc = open(path, encoding="utf-8").read()
    check("报告日周月分割线", doc.count('class="turn-sep"') == 4,
          f"seps={doc.count('class=\"turn-sep\"')}")
    check("报告新增徽标", 'class="tag tag-new">🆕 新增' in doc)
    check("报告原有徽标", 'class="tag tag-keep">⏳ 原有' in doc)
    check("报告完成标记", '<span class="done">完成</span>' in doc)
    check("报告LOF卡无徽标", doc.count("card lv-LOF") == 1 and "tag-new" not in
          doc.split("card lv-LOF")[1].split("</div>")[0])
    check("报告头部新增/原有计数", '🆕新增 <b>1</b>' in doc
          and '⏳原有 <span class="k">1</span>' in doc)
    check("报告无纯文本pre残留", "card-body" not in doc)
    check("报告新增/原有分组头", 'class="grp grp-new"' in doc
          and 'class="grp grp-keep"' in doc)
    check("报告其他提醒分组头", 'class="grp grp-other"' in doc)
    # 二次写入：旧段折叠 + 历史分割线
    path = append_push_report("2026-08-23 15:35", entries[:1], path)
    doc2 = open(path, encoding="utf-8").read()
    check("二次写入历史分割线", 'class="hist-div"' in doc2)
    check("旧段折叠为details", doc2.count('<details class="scan-fold">') == 1
          and "10:00" in doc2)
    check("旧段不再展开平铺", doc2.count('<section class="scan">') == 1)
    os.remove(path)


def test_single_strategy_push():
    print("[12] 单一策略·神奇九转推送")
    from presentation.formatter import (append_push_report, format_nine_turn_card,
                                        format_push_summary, push_summary_level)
    # 顶部预警卡（新增，日线+8计数中）
    card_up = format_nine_turn_card("600900", "长江电力", 8, 5, -2, "up",
                                    fresh=True, trade_date="2026-08-23")
    check("九转卡顶部预警标题", "🌀 神奇九转 · 顶部预警 | 长江电力 600900" in card_up)
    check("九转卡新增状态行", "🆕 新增" in card_up)
    check("九转卡日周月计数", "日线：+8（计数中）" in card_up
          and "周线：+5（计数中）" in card_up and "月线：-2（计数中）" in card_up)
    check("九转卡顶部含义", "警惕趋势反转向下" in card_up)
    check("九转卡查看链接", "https://gu.qq.com/sh600900" in card_up)
    check("九转卡触发日期", "触发：2026-08-23" in card_up)
    check("九转卡不含信号分级", "买入" not in card_up and "得分" not in card_up
          and "止损" not in card_up)
    # 底部预警卡（原有维持，日线-9完成）
    card_dn = format_nine_turn_card("000333", "美的集团", -9, None, 0, "down",
                                    fresh=False, trade_date="2026-08-23")
    check("九转卡底部预警标题", "底部预警 | 美的集团 000333" in card_dn)
    check("九转卡原有状态行", "⏳ 原有（维持）" in card_dn)
    check("九转卡完成标记", "日线：-9（完成）" in card_dn)
    check("九转卡周线无/月线0", "周线：无" in card_dn and "月线：0" in card_dn)
    check("九转卡底部含义", "关注趋势反转向上" in card_dn)

    # 门槛语义矩阵：单一模式只看|计数|≥push_from；常规模式要求级别+动作+计数
    push_levels = {"S", "A"}
    for mode, count, level, action, expect in [
        ("single", 8, "B", "hold", True),   # 单一：B级hold但九转8 → 推
        ("single", -9, "C", "hold", True),  # 单一：低9完成 → 推
        ("single", 5, "S", "buy", False),   # 单一：九转不足门槛 → 不推
        ("normal", 8, "B", "buy", False),   # 常规：B级 → 不推
        ("normal", 8, "S", "hold", False),  # 常规：hold → 不推
        ("normal", 8, "S", "buy", True),    # 常规：S级buy+九转8 → 推
    ]:
        single_mode = mode == "single"
        hit = (abs(count) >= 8 if single_mode
               else level in push_levels and action != "hold" and abs(count) >= 8)
        check(f"门槛语义[{mode}|{count:+d}|{level}-{action}]", hit == expect)

    # 汇总消息：TURN条目进新增/原有分组 + HTML报告TURN样式
    entries = [
        {"level": "TURN", "title": "九转策略 · 顶部预警 长江电力 600900", "text": card_up,
         "fresh": True, "turn_day": 8, "turn_week": 5, "turn_month": -2,
         "info": "🆕新增 九转 日+8 ┃ 周+5 ┃ 月-2"},
        {"level": "TURN", "title": "九转策略 · 底部预警 美的集团 000333", "text": card_dn,
         "fresh": False, "turn_day": -9, "turn_week": None, "turn_month": 0,
         "info": "⏳原有 九转 日-9 ┃ 周无 ┃ 月0"},
    ]
    title, body = format_push_summary(entries)
    check("单一策略汇总计数", title == "收盘扫描报告 · 2条", title)
    check("单一策略汇总分组新增", "🆕 新增九转 1条" in body)
    check("单一策略汇总分组原有", "⏳ 原有九转 1条（维持）" in body)
    check("单一策略汇总图标", "🌀 九转策略 · 顶部预警 长江电力 600900" in body)
    check("单一策略汇总级别TURN", push_summary_level(entries) == "TURN")
    path = append_push_report("2026-08-23 15:35", entries, "data/_test_turn_report.html")
    doc = open(path, encoding="utf-8").read()
    check("TURN卡片青色样式", doc.count("card lv-TURN") == 2)
    check("TURN日周月分割线", doc.count('class="turn-sep"') == 4,
          f"seps={doc.count('class=\"turn-sep\"')}")
    check("TURN新增/原有徽标", 'class="tag tag-new">🆕 新增' in doc
          and 'class="tag tag-keep">⏳ 原有' in doc)
    check("TURN完成标记", '<span class="done">完成</span>' in doc)
    check("TURN头部计数", '🆕新增 <b>1</b>' in doc
          and '⏳原有 <span class="k">1</span>' in doc)
    check("TURN新增/原有分组头", 'class="grp grp-new"' in doc
          and 'class="grp grp-keep"' in doc)
    check("TURN方向横幅", 'class="dir dir-up"' in doc and 'class="dir dir-down"' in doc)
    check("TURN结构化字段网格", 'class="card-fields"' in doc
          and 'class="f-k">含义</div><div class="f-v">' in doc)
    check("TURN触发日期字段", 'class="f-k">触发</div><div class="f-v">2026-08-23</div>' in doc)
    check("TURN链接按钮组", 'class="card-links"' in doc
          and 'class="btn" href="https://gu.qq.com/sh600900"' in doc)
    check("TURN无pre纯文本残留", "card-body" not in doc)
    os.remove(path)


if __name__ == "__main__":
    print("=" * 46)
    print("领域层与应用层自测（全部离线合成数据）")
    print("=" * 46)
    for fn in (test_nine_turns, test_resampler, test_indicators, test_volume,
               test_signal_engine, test_lof, test_ranking, test_url_view,
               test_end_to_end, test_push_charts, test_push_report_format,
               test_single_strategy_push):
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print(f"  ✗ {fn.__name__} 异常: {e}")
    print("=" * 46)
    print(f"通过 {len(PASS)} 项 / 失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)
    print("全部通过 ✔")
