"""微信消息模板：信号决策卡 / LOF溢价卡 / 排行榜 / 晚报 / 聚合推送HTML报告。"""
import html as _html
import math
import os
import re

from domain.models import SignalResult, VolumeProfile, LOFState


def _num(v):
    """NaN/None 安全取值：返回是否有效。"""
    return v is not None and not (isinstance(v, float) and math.isnan(v))

_LEVEL_ICON = {"S": "🔴", "A": "🟠", "B": "🟡", "C": "⚪"}
_ACTION_CN = {"buy": "买入", "sell": "卖出", "hold": "观望"}
_SURGE_CN = {"up": "放量上攻", "down": "恐慌砸盘", "stagnant": "滞涨放量", "shrink": "缩量"}
_NAV_SRC_CN = {"iopv": "IOPV实时", "estimate": "双口径估算", "official": "昨官方净值"}


def stock_url(code: str) -> str:
    """行情查看链接（腾讯）：6/5开头=沪市，其余=深市，与数据源符号规则一致。"""
    code = str(code)
    symbol = f"sh{code}" if code.startswith(("6", "5")) else f"sz{code}"
    return f"https://gu.qq.com/{symbol}"


# ---- Web看板深链（配置 web.public_url 后推送卡附带，微信点开直达对应标的） ----
_web_base = ""


def set_web_base(url: str) -> None:
    global _web_base
    _web_base = (url or "").rstrip("/")


def web_link(code: str, period: str = "day") -> str:
    """看板深链：?code=xxx&period=day|week|month，页面自动打开该标的K线。空配置返回""。"""
    return f"{_web_base}/?code={code}&period={period}" if _web_base else ""


def fmt_turn(v) -> str:
    """九转计数展示：None→无，0→0，其余带符号。"""
    if v is None:
        return "无"
    return f"{v:+d}" if v else "0"


def _turn_line(label: str, v) -> str:
    txt = fmt_turn(v)
    if v is None or v == 0:
        pass
    elif abs(v) == 9:
        txt += "（完成）"
    else:
        txt += "（计数中）"
    return f"  {label}：{txt}"


def format_turn_brief(day, week=None, month=None) -> str:
    """九转日周月紧凑展示（汇总消息用）：三周期以分割线样式分隔。"""
    return f"日{fmt_turn(day)} ┃ 周{fmt_turn(week)} ┃ 月{fmt_turn(month)}"


# 单一策略·神奇九转：方向含义（正计数=上涨结构=顶部预警；负计数=下跌结构=底部预警）
_TURN_MEANING = {"up": "高位九转计数：上涨结构接近完成，警惕趋势反转向下",
                 "down": "低位九转计数：下跌结构接近完成，关注趋势反转向上"}


def format_nine_turn_card(code: str, name: str, turn_day, turn_week, turn_month,
                          direction: str, fresh: bool = True,
                          trade_date: str = "") -> str:
    """单一策略·神奇九转卡片：只呈现九转结构本身（日/周/月计数+方向含义），
    不含信号分级/买卖动作。fresh=新增（首次出现或计数推进），否则原有维持。"""
    head = "顶部预警" if direction == "up" else "底部预警"
    lines = [
        "━" * 22,
        f"🌀 神奇九转 · {head} | {name} {code}",
        "━" * 22,
        f"• 状态：{'🆕 新增' if fresh else '⏳ 原有（维持）'}",
        "• 九转结构：",
        _turn_line("日线", turn_day),
        "  " + "─" * 10,
        _turn_line("周线", turn_week),
        "  " + "─" * 10,
        _turn_line("月线", turn_month),
        f"• 含义：{_TURN_MEANING.get(direction, '')}",
        "─" * 22,
        f"• 行情查看：{stock_url(code)}",
    ]
    wl = web_link(code, "day")
    if wl:
        lines.append(f"• 看板详情：{wl}")
    if trade_date:
        lines.append(f"触发：{trade_date}")
    return "\n".join(lines)


def format_signal_card(sig: SignalResult, vp: VolumeProfile, market_state: str,
                       hist_stats: dict = None,
                       turn_week: int = None, turn_month: int = None) -> str:
    """hist_stats: 同型信号历史统计 {count, win_rate_10d}，无则不显示。
    turn_week / turn_month：周线、月线九转计数（日周月多周期展示）。"""
    lines = [
        "━" * 22,
        f"{_LEVEL_ICON.get(sig.level, '')} {sig.level}级{_ACTION_CN.get(sig.action)}信号 | {sig.name} {sig.code}",
        "━" * 22,
        "• 九转结构：",
        _turn_line("日线", sig.turn),
        "  " + "─" * 10,
        _turn_line("周线", turn_week),
        "  " + "─" * 10,
        _turn_line("月线", turn_month),
        f"• 量能：量比 {vp.vol_ratio:.1f} / 分位 {vp.volume_percentile:.0%}"
        + (f" / {_SURGE_CN.get(vp.surge_type, vp.surge_type)}" if vp.surge_type else ""),
        f"• 综合得分：{sig.score}",
    ]
    if sig.position_ratio:
        lines.append(f"• 建议仓位：{sig.position_ratio:.0%}" if sig.action == "buy"
                     else f"• 建议减仓至：{1 - sig.position_ratio:.0%}")
    if sig.stop_loss:
        lines.append(f"• 止损参考：¥{sig.stop_loss:,.2f}")
    if hist_stats and hist_stats.get("count"):
        lines.append(f"• 历史同型胜率：{hist_stats['win_rate_10d']:.0%}"
                     f"（n={hist_stats['count']}）")
    if sig.reasons:
        lines.append("─" * 22)
        lines.extend(f"  · {r}" for r in sig.reasons[:8])
    lines.append("─" * 22)
    lines.append(f"• 行情查看：{stock_url(sig.code)}")
    wl = web_link(sig.code, sig.period if sig.period in ("day", "week", "month") else "day")
    if wl:
        lines.append(f"• 看板详情：{wl}")
    lines.append(f"触发：{sig.trade_date} ｜ 回复 1详情 2跟踪 3忽略")
    return "\n".join(lines)


def format_lof_card(st: LOFState) -> str:
    lines = [
        "━" * 22,
        f"💠 LOF溢价监控 | {st.name} {st.code}",
        "━" * 22,
        f"• 场内价：{st.price:.3f}",
        f"• 估算净值：{st.nav_official_est:.4f}（口径：{_NAV_SRC_CN.get(st.nav_source, st.nav_source)}）",
        f"• 溢价(实时)：{st.premium_official:+.2f}%（参考口径 {st.premium_reference:+.2f}%）",
        f"• 溢价(T-1)：{st.premium_t1:+.2f}%（按昨净值，未含当日底层变动）",
        f"• 溢价分位：{st.premium_percentile:.0%}",
    ]
    if st.share_chg_pct:
        lines.append(f"• 份额变动：{st.share_chg_pct:+.1f}%")
    if st.note:
        lines.append("─" * 22)
        lines.append(f"⚠ {st.note}")
    lines.append("─" * 22)
    lines.append(f"• 行情查看：{stock_url(st.code)}")
    wl = web_link(st.code)
    if wl:
        lines.append(f"• 看板详情：{wl}")
    return "\n".join(lines)


def format_rank_list(ranked: list, key: str, period: str) -> str:
    key_cn = {"vol_ratio": "量比", "vol_ratio_period": "周期量比", "amt_ratio": "额比",
              "amount": "成交额", "premium": "溢价率", "turn_abs": "九转计数",
              "pct_chg": "涨跌幅"}.get(key, key)
    period_cn = {"day": "日线", "week": "周线", "month": "月线"}.get(period, period)
    lines = [f"━━ 自选池 · {period_cn}{key_cn}排行 ━━"]
    for i, r in enumerate(ranked, 1):
        parts = [f"{i}. {r.get('name', '')} {r.get('code', '')}"]
        if _num(r.get("pct_chg")):
            parts.append(f"{r['pct_chg']:+.1f}%")
        parts.append(f"{key_cn}{_fmt_val(r, key)}")
        if _num(r.get("premium")):
            parts.append(f"溢价{r['premium']:+.1f}%")
        tc = r.get("turn_count") or 0
        if tc:
            parts.append(f"九转:{'高' if tc > 0 else '低'}{abs(tc)}")
        lines.append("  ".join(str(p) for p in parts))
    lines.append(f"共{len(ranked)}只 ｜ {period_cn}视图")
    return "\n".join(lines)


def _fmt_val(r: dict, key: str) -> str:
    v = r.get(key)
    if not _num(v):
        return "-"
    if key == "amount":
        return f"{v / 1e8:.1f}亿" if v >= 1e8 else f"{v / 1e4:.0f}万"
    if key == "turn_abs":
        tc = r.get("turn_count") or 0
        return str(abs(tc))
    return f"{v:.2f}"


def format_evening_report(signal_results: list, stats: dict, market_state: str) -> str:
    """晚报：B级聚合 + 胜率统计。market_state 参数保留（内部阈值用），不再展示标签。"""
    lines = ["━━ 晚间摘要 ━━", ""]
    b_rows = [s for s in signal_results if s.level == "B"]
    if b_rows:
        lines.append("【观察级信号(B)】")
        for s in b_rows[:8]:
            lines.append(f"  · {s.name} {s.code} 九转{s.turn:+d} 得分{s.score}")
    if stats:
        lines.append("")
        lines.append("【信号胜率统计(10日)】")
        for k, v in stats.items():
            lines.append(f"  · {k}: {v['count']}次 胜率{v['win_rate_10d']:.0%} 均收益{v['avg_ret_10d']:+.1f}%")
    if not b_rows and not stats:
        lines.append("今日无观察级信号，暂无胜率统计。")
    return "\n".join(lines)


# ============ 聚合推送HTML报告：同一次扫描的全部推送内容（信号卡/LOF卡/K线图）写入同一个文件 ============
_PUSH_FEED_OPEN = '<div id="feed">'
_PUSH_SECTION_MARK = "<!--SCAN-->"
_PUSH_MAX_SECTIONS = 30          # 最多保留最近30次扫描，防止文件无限膨胀
_PUSH_TAIL = "\n</div>\n</main>\n</body>\n</html>\n"

_PUSH_DOC_TMPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股监控 · 推送报告</title>
<style>
  :root {
    --up:#e8463a; --down:#2e9e5b; --muted:#6b7280; --line:#e5e7eb;
    --card-r:12px; --shadow:0 2px 10px rgba(17,24,39,.07), 0 1px 3px rgba(17,24,39,.06);
  }
  body { margin:0; background:#eef1f6; font-family:"Microsoft YaHei",-apple-system,"Segoe UI",sans-serif; color:#222; }
  header { background:linear-gradient(135deg,#111827 0%,#1f2937 55%,#312e81 100%); color:#fff; padding:20px 24px 18px; }
  header h1 { margin:0; font-size:20px; letter-spacing:.5px; }
  header h1::before { content:"📈 "; }
  header p { margin:6px 0 0; font-size:12.5px; color:#9ca3af; }
  main { max-width:920px; margin:0 auto; padding:18px 14px 40px; }
  section.scan { margin-bottom:26px; }
  section.scan h2 { font-size:15px; color:#6b7280; border-left:4px solid #9ca3af; padding-left:10px; margin:0 0 12px; }
  section.scan h2 .cnt { color:#fff; background:#9ca3af; border-radius:10px; font-size:12px; padding:1px 9px; margin-left:8px; }
  section.scan h2 .sub { font-size:12px; font-weight:600; margin-left:8px; }
  section.scan h2 .sub .n { color:var(--up); }
  section.scan h2 .sub .k { color:var(--muted); }
  section.scan.new { background:#fffaf9; border:1px solid #f3d4d0; border-radius:12px; padding:14px 14px 2px; box-shadow:var(--shadow); }
  section.scan.new h2 { color:#111827; border-left-color:#e8463a; }
  section.scan.new h2::before { content:"🆕 本次新增 · "; color:#e8463a; }
  section.scan.new h2 .cnt { background:#e8463a; }
  .hist-div { display:flex; align-items:center; gap:12px; margin:2px 0 26px; color:#9ca3af; font-size:12.5px; }
  .hist-div::before, .hist-div::after { content:""; flex:1; height:1px; background:#d1d5db; }
  .card { background:#fff; border-radius:var(--card-r); box-shadow:var(--shadow); margin-bottom:14px; overflow:hidden; transition:transform .15s ease, box-shadow .15s ease; }
  .card:hover { transform:translateY(-2px); box-shadow:0 6px 18px rgba(17,24,39,.12); }
  .card-head { display:flex; align-items:center; gap:10px; padding:11px 14px; border-bottom:1px solid #eef0f3; }
  .badge { font-size:12px; font-weight:700; color:#fff; border-radius:6px; padding:2px 8px; flex:none; letter-spacing:.5px; }
  .lv-S { border-top:3px solid #e8463a; } .lv-S .badge { background:linear-gradient(135deg,#f0655a,#e8463a); }
  .lv-A { border-top:3px solid #f0a400; } .lv-A .badge { background:linear-gradient(135deg,#f7b733,#f0a400); }
  .lv-B { border-top:3px solid #d1b60a; } .lv-B .badge { background:linear-gradient(135deg,#ddd83a,#d1b60a); }
  .lv-LOF { border-top:3px solid #3b7dd8; } .lv-LOF .badge { background:linear-gradient(135deg,#5a97e8,#3b7dd8); }
  .lv-TURN { border-top:3px solid #0d9488; } .lv-TURN .badge { background:linear-gradient(135deg,#2dd4bf,#0d9488); }
  .lv-ALERT { border-top:3px solid #7c3aed; } .lv-ALERT .badge { background:linear-gradient(135deg,#a271f2,#7c3aed); }
  .lv-INFO { border-top:3px solid #6b7280; } .lv-INFO .badge { background:linear-gradient(135deg,#8b95a5,#6b7280); }
  .card-title { font-size:14.5px; font-weight:700; }
  .tag { font-size:11px; font-weight:700; border-radius:999px; padding:2px 10px; margin-left:auto; flex:none; }
  .tag-new { color:#fff; background:linear-gradient(135deg,#ff7d70,#e8463a); box-shadow:0 1px 4px rgba(232,70,58,.35); }
  .tag-keep { color:#5b6472; background:#eceff3; border:1px solid #dde1e8; }
  .turn-strip { display:flex; align-items:stretch; border-bottom:1px solid #eef0f3; background:linear-gradient(180deg,#fafbfd,#f3f5f9); }
  .turn-cell { flex:1; text-align:center; padding:10px 4px 8px; }
  .turn-sep { width:0; flex:none; border-left:1px dashed #b9c1cf; margin:9px 0; }
  .turn-cell .lbl { display:block; font-size:11px; color:var(--muted); margin-bottom:4px; letter-spacing:2px; }
  .turn-cell .val { display:inline-block; font-size:16px; font-weight:800; font-family:Consolas,"Microsoft YaHei",monospace; min-width:44px; }
  .turn-cell .done { display:block; font-size:10.5px; font-weight:700; margin-top:3px; color:#b45309; }
  .turn-cell .done::before { content:"✓ "; }
  .tv-up { color:var(--up); } .tv-down { color:var(--down); } .tv-zero { color:#9ca3af; }
  pre.card-body { margin:0; padding:12px 14px; font-size:12.8px; line-height:1.75; white-space:pre-wrap; word-break:break-all; font-family:Consolas,"Microsoft YaHei",monospace; color:#333; }
  pre.card-body a { color:#3b7dd8; }
  img.chart { display:block; width:100%; border-top:1px solid #eef0f3; }
</style>
</head>
<body>
<header><h1>A股监控 · 推送报告</h1><p>每次扫描的全部推送内容（信号卡 / LOF卡 / K线图）聚合在此；九转条目标注「新增 / 原有」，日·周·月以分割线分隔</p></header>
<main>
<div id="feed">
</div>
</main>
</body>
</html>
"""

_URL_RE = re.compile(r"(https?://[^\s<]+)")


def _linkify(escaped_text: str) -> str:
    """转义后的文本里把URL变成可点链接。"""
    return _URL_RE.sub(r'<a href="\1" target="_blank">\1</a>', escaped_text)


def _turn_strip_html(e: dict) -> str:
    """九转日周月条：日/周/月三格并列，格间以竖向虚线分割（信号卡专属）。
    完成(±9)附加✓标记，新增/原有以底色区分。"""
    if "turn_day" not in e:
        return ""

    def cell(label, v):
        done = v is not None and abs(v) == 9
        if v is None:
            cls, txt = "tv-zero", "无"
        elif v > 0:
            cls, txt = "tv-up", f"{v:+d}"
        elif v < 0:
            cls, txt = "tv-down", f"{v:+d}"
        else:
            cls, txt = "tv-zero", "0"
        state = '<span class="done">完成</span>' if done else ""
        return (f'<div class="turn-cell"><span class="lbl">{label}</span>'
                f'<span class="val {cls}">{txt}</span>{state}</div>')

    sep = '<div class="turn-sep" aria-hidden="true"></div>'
    return ('<div class="turn-strip">'
            + cell("日线", e.get("turn_day")) + sep
            + cell("周线", e.get("turn_week")) + sep
            + cell("月线", e.get("turn_month")) + "</div>\n")


def _push_card_html(e: dict) -> str:
    level = _html.escape(str(e.get("level", "INFO")))
    img = ""
    if e.get("image") and os.path.exists(e["image"]):
        src = _html.escape(str(e["image"]).replace("\\", "/"))
        img = f'\n<img class="chart" src="{src}" alt="chart" loading="lazy">'
    body = _linkify(_html.escape(str(e.get("text", ""))))
    # 九转条目：新增(红底白字) / 原有(灰底) 徽标；其余类型不标
    if "turn_day" in e:
        tag = ('<span class="tag tag-new">🆕 新增</span>' if e.get("fresh")
               else '<span class="tag tag-keep">⏳ 原有</span>')
    else:
        tag = ""
    return (f'<div class="card lv-{level}">\n'
            f'  <div class="card-head"><span class="badge">{level}</span>'
            f'<span class="card-title">{_html.escape(str(e.get("title", "")))}</span>{tag}</div>\n'
            + _turn_strip_html(e)
            + f'  <pre class="card-body">{body}</pre>{img}\n'
            f'</div>')


def append_push_report(scan_time: str, entries: list, path: str = "push_report.html") -> str:
    """把一次扫描的全部推送条目写入同一个HTML报告：新扫描置顶并标注“本次新增”，
    与历史推送之间加分割线；九转条目标注新增/原有；保留最近30次。返回文件路径。"""
    if not entries:
        return ""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n_new = sum(1 for e in entries if e.get("fresh") and "turn_day" in e)
    n_keep = sum(1 for e in entries if not e.get("fresh") and "turn_day" in e)
    sub = (f'<span class="sub">🆕新增<span class="n">{n_new}</span>'
           f' ／ ⏳原有<span class="k">{n_keep}</span></span>' if n_new + n_keep else "")
    section = (f'\n{_PUSH_SECTION_MARK}\n<section class="scan new">\n'
               f'<h2>收盘扫描 · {_html.escape(scan_time)}'
               f'<span class="cnt">{len(entries)}条</span>{sub}</h2>\n'
               + "\n".join(_push_card_html(e) for e in entries)
               + "\n</section>\n")
    doc = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = f.read()
        except Exception:
            doc = ""
    if _PUSH_FEED_OPEN not in doc:   # 首次生成或文件损坏 → 重建骨架
        doc = _PUSH_DOC_TMPL
    head = doc.split(_PUSH_FEED_OPEN, 1)[0] + _PUSH_FEED_OPEN
    old_sections = re.findall(
        r"\n<!--SCAN-->\n<section class=\"scan(?: new)?\">.*?</section>", doc, re.S)
    # 历史段去掉“本次新增”高亮：只有最新一段保持 new 标注
    old_html = "".join(old_sections[:_PUSH_MAX_SECTIONS - 1]).replace(
        '<section class="scan new">', '<section class="scan">')
    hist_div = '\n<div class="hist-div">以下为历史推送</div>\n' if old_html else ""
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + section + hist_div + old_html + _PUSH_TAIL)
    return path


_PUSH_LEVEL_ICON = {"S": "🔴", "A": "🟠", "B": "🟡", "LOF": "💠", "TURN": "🌀", "ALERT": "⚠️", "INFO": "ℹ️"}
_SUMMARY_DIV = "─" * 20


def _summary_line(e: dict) -> str:
    icon = _PUSH_LEVEL_ICON.get(str(e.get("level", "INFO")), "ℹ️")
    info = f"（{e['info']}）" if e.get("info") else ""
    return f"{icon} {e.get('title', '')}{info}"


def format_push_summary(entries: list, report_path: str = "") -> tuple:
    """聚合推送的汇总消息（一次扫描只发一条）：返回(标题, 正文)。
    九转条目按「新增 / 原有」分组，组间以分割线分隔；完整内容在HTML报告里。"""
    turn_fresh = [e for e in entries if e.get("fresh") and "turn_day" in e]
    turn_keep = [e for e in entries if not e.get("fresh") and "turn_day" in e]
    others = [e for e in entries if "turn_day" not in e]
    lines = []
    if turn_fresh:
        lines.append(f"🆕 新增九转 {len(turn_fresh)}条")
        lines.extend(_summary_line(e) for e in turn_fresh)
    if turn_keep:
        if lines:
            lines.append(_SUMMARY_DIV)
        lines.append(f"⏳ 原有九转 {len(turn_keep)}条（维持）")
        lines.extend(_summary_line(e) for e in turn_keep)
    if others:
        if lines:
            lines.append(_SUMMARY_DIV)
        lines.extend(_summary_line(e) for e in others)
    body = "\n".join(lines)
    if report_path:
        body += f"\n\n完整详情与K线图：{report_path}"
    return f"收盘扫描报告 · {len(entries)}条", body


def push_summary_level(entries: list) -> str:
    """汇总消息的级别：取本次最高级别（决定企微消息颜色）。"""
    lvs = {str(e.get("level", "INFO")) for e in entries}
    for lv in ("S", "A", "LOF", "TURN"):
        if lv in lvs:
            return lv
    return "INFO"
