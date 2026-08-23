"""扫描编排器：收盘全量扫描主流程。

流程：拉指数→市场状态→逐标的(增量拉K线→九转→量能→LOF溢价→六层融合)
     →去重推送→快照落库→信号跟踪注册→扫描日志。
"""
from datetime import datetime

import pandas as pd

from application.market_state import detect_market_state
from domain.lof_premium import evaluate_lof, premium_percentile
from domain.nine_turns import calc_nine_turns
from domain.ranking import build_snapshot_row, funnel_filter, rank
from domain.resampler import to_monthly, to_weekly
from domain.signal_engine import fuse
from domain.volume import volume_profile
from infrastructure.adapters import is_lof
from presentation.chart_renderer import render_kline_chart, render_premium_chart
from presentation.formatter import (append_push_report, format_lof_card,
                                    format_push_summary, format_rank_list,
                                    format_signal_card, format_turn_brief,
                                    push_summary_level, set_web_base)


class Orchestrator:
    def __init__(self, cfg: dict, cache, sources, pusher):
        self.cfg = cfg
        self.cache = cache
        self.sources = sources
        self.pusher = pusher
        self.market_state = "range"
        set_web_base(cfg.get("web", {}).get("public_url", ""))  # 推送卡附带看板深链

    # ---------- 数据加载（缓存优先，增量拉取） ----------
    def load_kline(self, code: str) -> pd.DataFrame:
        local = self.cache.get_klines(code)
        if local is not None and not local.empty:
            last = self.cache.last_kline_date(code)
            if last and last >= datetime.now().strftime("%Y-%m-%d"):
                return local
            remote = self.sources.fetch_kline(code, days=30)
            if self.sources.using_synthetic:
                return local  # 合成兜底数据不覆盖已有缓存
            if remote is not None and not remote.empty:
                merged = pd.concat([local, remote]).drop_duplicates(
                    subset=["date"], keep="last").sort_values("date")
                self.cache.upsert_klines(code, merged)
                return merged.reset_index(drop=True)
            return local
        remote = self.sources.fetch_kline(code, days=int(self.cfg.get("kline_history_days", 2500)))
        if remote is not None and not remote.empty:
            self.cache.upsert_klines(code, remote)
            return remote.reset_index(drop=True)
        return pd.DataFrame()

    def refresh_market_state(self) -> str:
        idx = self.sources.fetch_index(days=300)
        self.market_state = detect_market_state(idx)
        return self.market_state

    # ---------- 收盘全量扫描 ----------
    def scan_close(self) -> dict:
        errors = signals_pushed = 0
        watchlist = self.cfg.get("watchlist", [])
        self.refresh_market_state()
        trade_date = datetime.now().strftime("%Y-%m-%d")
        push_levels = set(self.cfg.get("signal", {}).get("push_levels", ["S", "A"]))
        # 九转推送门槛：|计数| ≥ push_from 才推送（默认8）
        push_min_count = int(self.cfg.get("nine_turns", {}).get("push_from", 8))
        snapshot_rows, lof_states, signal_results = [], [], []
        push_entries = []   # 本次扫描全部推送内容 → 聚合进同一个HTML报告，只发一条汇总
        df_cache = {}  # code → K线DataFrame（LOF溢价图渲染用）
        hist_stats = self.cache.tracking_stats()  # 同型信号历史胜率（信号卡展示）

        for item in watchlist:
            code, name = str(item["code"]), item.get("name", "")
            try:
                df = self.load_kline(code)
                if df.empty or len(df) < 60:
                    errors += 1
                    continue
                df_cache[code] = df

                turn = calc_nine_turns(df)
                vp = volume_profile(df, self.cfg.get("volume_surge", {}))
                turn_weekly = calc_nine_turns(to_weekly(df)).count
                turn_monthly = calc_nine_turns(to_monthly(df)).count

                # LOF 溢价（股票/ETF为None，仅真LOF 16/50）：优先落库序列算分位，随后当日快照落库
                premium = None
                if is_lof(code):
                    st = self._eval_lof(code, name, df)
                    st.trade_date = str(df["date"].iloc[-1])
                    hist = self.cache.get_premium_hist(
                        code, int(self.cfg.get("lof", {}).get("percentile_window", 60)))
                    if len(hist) >= 10:  # 落库序列充足时用真实历史重算分位
                        st.premium_percentile = round(premium_percentile(
                            [h["premium_official"] for h in hist if h["premium_official"] is not None],
                            st.premium_official), 2)
                    self.cache.upsert_premium(st)
                    lof_states.append(st)
                    premium = st.premium_official

                sig = fuse(df, code, name, turn, vp, self.cfg,
                           self.market_state, turn_weekly)
                sig.trade_date = str(df["date"].iloc[-1])
                signal_results.append(sig)

                # 快照（三周期）
                for period in ("day", "week", "month"):
                    row = build_snapshot_row(df, code, name, period, self.cfg,
                                             premium if period == "day" else None)
                    self.cache.upsert_snapshot(row)
                    if period == "day":
                        snapshot_rows.append(row)

                # 九转状态续存（先读上次计数，用于推送区分新增/原有）
                prev_turn = self.cache.get_state(code)
                self.cache.set_state(code, turn.count)

                # 推送收集（级别过滤 + 九转门槛，不去重：每次扫描命中即收）：
                # 全部内容聚合进同一个HTML报告，扫描结束后只发一条汇总消息
                if (sig.level in push_levels and sig.action != "hold"
                        and abs(turn.count) >= push_min_count):
                    direction = "up" if turn.count > 0 else "down"
                    # 新增=上次无计数或计数有变化（新触发/计数推进）；原有=计数与上次一致（维持）
                    fresh = prev_turn is None or prev_turn != turn.count
                    card = format_signal_card(sig, vp, self.market_state,
                                              hist_stats.get(f"{sig.level}-{sig.action}"),
                                              turn_week=turn_weekly, turn_month=turn_monthly)
                    image = render_kline_chart(df, sig) if self.cfg.get("chart_enabled", True) else None
                    push_entries.append({
                        "level": sig.level,
                        "title": f"{sig.level}级{'买入' if sig.action == 'buy' else '卖出'}信号 {name} {code}",
                        "text": card, "image": image, "fresh": fresh,
                        "turn_day": sig.turn, "turn_week": turn_weekly,
                        "turn_month": turn_monthly,
                        "info": f"{'🆕新增' if fresh else '⏳原有'} 九转 "
                                f"{format_turn_brief(sig.turn, turn_weekly, turn_monthly)}"
                                f"｜得分{sig.score}"})
                    self.cache.record_push(code, direction, trade_date, sig.level)
                    self.cache.add_tracking(sig)
                    signals_pushed += 1
            except Exception as e:  # 单标的失败不影响整体
                errors += 1
                print(f"  [错误] {code}: {e}")

        # LOF 溢价提醒 → 同样聚合进HTML报告（文字卡 + 溢价走势图）
        lof_pushed = 0
        for st in lof_states:
            if st.note:
                img = None
                if self.cfg.get("chart_enabled", True) and df_cache.get(st.code) is not None:
                    img = render_premium_chart(df_cache.get(st.code), st)
                push_entries.append({
                    "level": "LOF",
                    "title": f"LOF溢价提醒 {st.name} {st.code}",
                    "text": format_lof_card(st), "image": img,
                    "info": f"溢价{st.premium_official:+.2f}% 分位{st.premium_percentile:.0%}"})
                lof_pushed += 1

        # 聚合推送：本次扫描全部内容写入同一个HTML报告，只发一条汇总消息
        if push_entries:
            scan_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            report_path = append_push_report(
                scan_time, push_entries,
                self.cfg.get("push", {}).get("report_html", "push_report.html"))
            title, body = format_push_summary(push_entries, report_path)
            self.pusher.send(title, body, level=push_summary_level(push_entries))

        source_note = "合成数据(离线)" if self.sources.using_synthetic else "实盘数据"
        note = f"数据:{source_note}"  # 状态标签不外显（用户要求），仅内部用于动态阈值
        self.cache.log_scan("close", len(watchlist), signals_pushed + lof_pushed, errors, note)
        # 扫描异常告警：过半标的失败视为系统性故障（独立告警通道，不受限额）
        if errors and errors >= max(1, len(watchlist) // 2):
            self.pusher.send("系统告警：收盘扫描异常",
                             f"今日 close 扫描 {errors}/{len(watchlist)} 个标的失败。\n"
                             f"数据源: {source_note}\n请检查网络与数据源配置。",
                             level="ALERT", is_alert=True)
        # 更新历史跟踪收益
        self._update_tracking()
        return {"total": len(watchlist), "signals": signals_pushed + lof_pushed,
                "errors": errors, "market_state": self.market_state,
                "snapshot_rows": snapshot_rows, "lof_states": lof_states,
                "signal_results": signal_results, "source": source_note}

    def _eval_lof(self, code, name, df):
        cfg = self.cfg.get("lof", {})
        prev_nav = float(df["close"].iloc[-2]) if len(df) >= 2 else 1.0
        asset_chg = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) if len(df) >= 2 else 0.0
        position = self.cache.get_fund_position(code, float(cfg.get("default_position", 0.93)))
        # 简化：场内价=当日收盘；历史溢价序列用收盘/滚动净值近似
        premium_hist = []
        c = df["close"].astype(float)
        if len(c) > 60:
            nav_approx = c.shift(1)
            premium_hist = ((c / nav_approx - 1) * 100).dropna().tail(
                int(cfg.get("percentile_window", 60))).tolist()
        share_chg_pct = 0.0  # 真实环境接交易所份额披露；离线为0
        return evaluate_lof(code, name, float(c.iloc[-1]), prev_nav, asset_chg,
                            position=position, premium_history=premium_hist,
                            share_chg_pct=share_chg_pct, cfg=cfg)

    def _update_tracking(self):
        try:
            for t in self.cache.pending_tracking():
                df = self.cache.get_klines(t["code"])
                if not df.empty:
                    self.cache.update_tracking(t["id"], df)
        except Exception as e:
            print(f"  [跟踪更新失败] {e}")

    # ---------- 排行报告 ----------
    def rank_report(self, key: str = "vol_ratio", period: str = "day", top_n: int = None):
        rows = self.cache.latest_snapshots(period)
        rows = funnel_filter(rows, self.cfg)
        top_n = top_n or int(self.cfg.get("ranking", {}).get("top_n", 10))
        ranked = rank(rows, key=key, top_n=top_n)
        return format_rank_list(ranked, key, period)
