"""心跳与死信告警：指定时刻检查当日收盘扫描是否完成。"""
from datetime import datetime

from infrastructure.push import Pusher
from infrastructure.cache import Cache


def check_heartbeat(cache: Cache, pusher: Pusher, mode: str = "close") -> dict:
    """16:00 检查：今日 close 模式扫描是否存在。缺失即告警（走告警通道，不受限额）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    last = cache.last_scan_of_mode(mode)
    ok = bool(last and str(last[0]).startswith(today))
    result = {"ok": ok, "mode": mode, "date": today}
    if not ok:
        pusher.send("系统告警：收盘扫描未完成",
                    f"今日 {today} 的 {mode} 扫描记录缺失。\n"
                    f"可能原因：进程未运行 / 数据源全部失败 / 调度异常。\n"
                    f"请检查服务状态与日志。", level="ALERT", is_alert=True)
    else:
        print(f"[心跳] {today} {mode} 扫描正常 (signals={last[1]}, errors={last[2]})")
    return result
