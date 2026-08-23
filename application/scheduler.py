"""调度引擎：APScheduler（可用时）+ 纯循环兜底。开机补跑当日错过的任务。"""
import time as _time
from datetime import datetime, timedelta

from .heartbeat import check_heartbeat


def _parse_hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def _due(last_run_str: str, now: datetime, hh: int, mm: int) -> bool:
    """当日该时刻已到且未跑过。"""
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now < target:
        return False
    if last_run_str and last_run_str.startswith(now.strftime("%Y-%m-%d")):
        return False
    return True


def run_loop(orchestrator, cache, pusher, cfg, once: bool = False):
    """主循环。config schedule 定义的时刻点触发；错过则开机补跑。"""
    sch = cfg.get("schedule", {})
    close_hm = _parse_hm(sch.get("close_scan_time", "15:35"))
    intra_hms = [_parse_hm(t) for t in sch.get("intraday_times", ["10:30", "14:00"])]
    hb_hm = _parse_hm(sch.get("heartbeat_time", "16:00"))

    def _mark_done(key):
        cache.log_scan(f"_done_{key}", 0, 0, 0, "")

    def _is_done(key):
        today = datetime.now().strftime("%Y-%m-%d")
        row = cache.last_scan_of_mode(f"_done_{key}")
        return bool(row and str(row[0]).startswith(today))

    print("[调度] 启动，等待任务时刻…（Ctrl+C 退出）")
    while True:
        now = datetime.now()
        # 收盘扫描
        if _due("" if _is_done("close") else "", now, *close_hm):
            if not _is_done("close"):
                print(f"[调度] {now:%H:%M} 触发收盘全量扫描")
                try:
                    r = orchestrator.scan_close()
                    print(f"[扫描完成] {r['total']}标的 信号{r['signals']} 错误{r['errors']} ({r['source']})")
                except Exception as e:
                    pusher.send("系统告警：收盘扫描异常", str(e), level="ALERT", is_alert=True)
                _mark_done("close")
        # 盘中增量（重刷快照）
        for idx, (hh, mm) in enumerate(intra_hms):
            if _due("" if _is_done(f"intra{idx}") else "", now, hh, mm):
                if not _is_done(f"intra{idx}"):
                    print(f"[调度] {now:%H:%M} 盘中增量扫描")
                    try:
                        orchestrator.scan_close()
                    except Exception as e:
                        print(f"[盘中扫描失败] {e}")
                    _mark_done(f"intra{idx}")
        # 心跳
        if _due("" if _is_done("hb") else "", now, *hb_hm):
            if not _is_done("hb"):
                check_heartbeat(cache, pusher)
                _mark_done("hb")
        if once:
            break
        # 跨天重置：日期变更后 done 标记自然失效（按日期前缀判断）
        _time.sleep(30)


def run_with_apscheduler(orchestrator, cache, pusher, cfg):
    """优先使用 APScheduler 的 cron 调度（服务器常驻模式）。"""
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        print("[调度] 未安装 APScheduler，退化为内置循环")
        return run_loop(orchestrator, cache, pusher, cfg)

    sch = cfg.get("schedule", {})
    sched = BlockingScheduler()

    def close_job():
        r = orchestrator.scan_close()
        print(f"[扫描完成] {r['total']}标的 信号{r['signals']} 错误{r['errors']}")

    def intraday_job():
        orchestrator.scan_close()

    def heartbeat_job():
        check_heartbeat(cache, pusher)

    hh, mm = _parse_hm(sch.get("close_scan_time", "15:35"))
    sched.add_job(close_job, "cron", day_of_week="mon-fri", hour=hh, minute=mm, id="close")
    for i, t in enumerate(sch.get("intraday_times", ["10:30", "14:00"])):
        h, m = _parse_hm(t)
        sched.add_job(intraday_job, "cron", day_of_week="mon-fri", hour=h, minute=m, id=f"intra{i}")
    hh, mm = _parse_hm(sch.get("heartbeat_time", "16:00"))
    sched.add_job(heartbeat_job, "cron", day_of_week="mon-fri", hour=hh, minute=mm, id="hb")

    print("[调度] APScheduler 已启动（mon-fri）")
    sched.start()
