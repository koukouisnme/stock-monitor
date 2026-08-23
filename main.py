"""A股监控系统入口。

用法：
  python main.py demo        # 离线演示（合成数据，全流程可跑通）
  python main.py scan        # 立即执行收盘扫描（联网，自动多源降级）
  python main.py rank [key] [period]   # 排行报告，如 rank vol_ratio day
  python main.py schedule    # 常驻调度（APScheduler/内置循环）
  python main.py lof <code>  # 单只LOF溢价评估
  python main.py heartbeat   # 手动心跳检查
  python main.py stats       # 信号胜率统计
  python main.py web         # Web界面（默认 http://127.0.0.1:8000）
"""
import os
import sys

import yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)


def load_config() -> dict:
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_stack(cfg: dict):
    from infrastructure.adapters import MultiSourceManager
    from infrastructure.cache import Cache
    from infrastructure.push import Pusher
    from application.orchestrator import Orchestrator
    cache = Cache(cfg.get("db_path", "data/monitor.db"))
    sources = MultiSourceManager(cfg)
    pusher = Pusher(cfg)
    orch = Orchestrator(cfg, cache, sources, pusher)
    return cache, sources, pusher, orch


def cmd_demo(cfg):
    """离线演示：强制合成数据源跑完整管线。"""
    cfg = dict(cfg)
    cfg["data_sources"] = ["synthetic"]
    cfg["allow_synthetic_fallback"] = True
    cfg["db_path"] = "data/demo.db"  # 演示库独立，避免合成数据污染实盘缓存
    cache, sources, pusher, orch = build_stack(cfg)
    print("=" * 50)
    print("离线演示模式（确定性合成数据）")
    print("=" * 50)
    r = orch.scan_close()
    print(f"\n[结果] 标的{r['total']} 信号{r['signals']} 错误{r['errors']} "
          f"市场:{r['market_state']} 数据:{r['source']}")
    print("\n" + orch.rank_report("vol_ratio", "day"))
    print("\n" + orch.rank_report("amount", "day"))
    from presentation.formatter import format_evening_report
    print("\n" + format_evening_report(r["signal_results"],
                                       cache.tracking_stats(), r["market_state"]))
    cache.close()


def cmd_scan(cfg):
    cache, sources, pusher, orch = build_stack(cfg)
    r = orch.scan_close()
    print(f"[结果] 标的{r['total']} 信号{r['signals']} 错误{r['errors']} "
          f"市场:{r['market_state']} 数据:{r['source']}")
    cache.close()


def cmd_rank(cfg, key="vol_ratio", period="day"):
    cache, sources, pusher, orch = build_stack(cfg)
    print(orch.rank_report(key, period))
    cache.close()


def cmd_schedule(cfg):
    cache, sources, pusher, orch = build_stack(cfg)
    from application.scheduler import run_with_apscheduler, run_loop
    run_with_apscheduler(orch, cache, pusher, cfg)
    cache.close()


def cmd_lof(cfg, code):
    cache, sources, pusher, orch = build_stack(cfg)
    df = orch.load_kline(code)
    if df.empty:
        print(f"无 {code} 数据")
        return
    st = orch._eval_lof(code, code, df)
    from presentation.formatter import format_lof_card
    print(format_lof_card(st))
    cache.close()


def cmd_heartbeat(cfg):
    cache, sources, pusher, _ = build_stack(cfg)
    from application.heartbeat import check_heartbeat
    print(check_heartbeat(cache, pusher))
    cache.close()


def cmd_stats(cfg):
    cache, _, _, _ = build_stack(cfg)
    stats = cache.tracking_stats()
    if not stats:
        print("暂无已回填的信号统计（需信号满10个交易日后回填）")
    for k, v in stats.items():
        print(f"{k}: {v['count']}次 胜率{v['win_rate_10d']:.0%} 平均10日收益{v['avg_ret_10d']:+.1f}%")
    cache.close()


def cmd_web(cfg, host="0.0.0.0", port=8000):
    """0.0.0.0：本机/局域网手机/内网穿透均可访问。"""
    cache, sources, pusher, orch = build_stack(cfg)
    from presentation.web_ui import create_app
    app = create_app(cfg, cache, sources, pusher, orch)
    lan = _lan_ip()
    print(f"Web界面: http://127.0.0.1:{port}  (Ctrl+C 退出)")
    if lan:
        print(f"手机同WiFi访问: http://{lan}:{port}")
    try:
        app.run(host=host, port=port, debug=False)
    finally:
        cache.close()


def _lan_ip() -> str:
    """取本机局域网IP（供手机同WiFi访问提示）。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))  # 阿里DNS，仅建连不发包
        ip = s.getsockname()[0]
        s.close()
        return ip if not ip.startswith("127.") else ""
    except Exception:
        return ""


def main():
    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd == "demo":
        cmd_demo(cfg)
    elif cmd == "scan":
        cmd_scan(cfg)
    elif cmd == "rank":
        cmd_rank(cfg, *(sys.argv[2:4] if len(sys.argv) >= 4 else ["vol_ratio", "day"]))
    elif cmd == "schedule":
        cmd_schedule(cfg)
    elif cmd == "lof":
        cmd_lof(cfg, sys.argv[2])
    elif cmd == "heartbeat":
        cmd_heartbeat(cfg)
    elif cmd == "stats":
        cmd_stats(cfg)
    elif cmd == "web":
        cmd_web(cfg, *(sys.argv[2:4] if len(sys.argv) >= 4 else ["0.0.0.0", 8000]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
