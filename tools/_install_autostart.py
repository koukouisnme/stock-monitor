# -*- coding: utf-8 -*-
"""注册/卸载 StockMonitor 看护计划任务。

单一任务双触发器（XML 定义，绕过 schtasks CLI 默认的 72 小时强杀）：
  - LogonTrigger：登录时立即启动（开机秒级拉起服务）
  - TimeTrigger 每5分钟重复：看护进程死亡后 ≤5 分钟自动接管
    （MultipleInstancesPolicy=IgnoreNew：运行中不重复拉起；锁文件再兜底）
  - ExecutionTimeLimit=PT0S：永不因超时被任务计划器终止

用法:
  py312\python.exe tools\_install_autostart.py install   # 注册并立即启动
  py312\python.exe tools\_install_autostart.py uninstall # 停止并删除任务
"""
import os
import subprocess
import sys
import tempfile
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, "py312", "python.exe")
WD = os.path.join(ROOT, "tools", "_service_watchdog.py")
TN = "StockMonitorWatchdog"

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
    <TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition><Interval>PT5M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Actions Context="Author">
    <Exec>
      <Command>{py}</Command>
      <Arguments>{wd}</Arguments>
    </Exec>
  </Actions>
</Task>"""


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def install_xml():
    user = f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ.get('USERNAME', '')}"
    xml = TASK_XML.format(start=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                          user=user, py=PY, wd=WD)
    f = os.path.join(tempfile.gettempdir(), "sm_watchdog_task.xml")
    with open(f, "w", encoding="utf-16") as fh:
        fh.write(xml)
    rc, out = run(["schtasks", "/Create", "/F", "/TN", TN, "/XML", f])
    print(("task created(xml): " if rc == 0 else "task create(xml) fail: ") + out)
    return rc == 0


def install_cli_fallback():
    tr = f'"{PY}" "{WD}"'
    rc, out = run(["schtasks", "/Create", "/F", "/TN", TN, "/SC", "ONLOGON",
                   "/RL", "HIGHEST", "/TR", tr])
    print(("task created(cli): " if rc == 0 else "task create(cli) fail: ") + out)
    if rc == 0:
        rc2, out2 = run(["schtasks", "/Create", "/F", "/TN", "StockMonitorKeepalive",
                         "/SC", "MINUTE", "/MO", "5", "/RL", "HIGHEST", "/TR", tr])
        print(("keepalive created(cli): " if rc2 == 0 else "keepalive fail: ") + out2)
    return rc == 0


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "install"
    if action == "uninstall":
        for tn in (TN, "StockMonitorKeepalive"):
            run(["schtasks", "/End", "/TN", tn])
            rc, out = run(["schtasks", "/Delete", "/TN", tn, "/F"])
            print(f"delete {tn}: {'ok' if rc == 0 else out}")
        return

    ok = install_xml() or install_cli_fallback()
    if ok:
        rc, out = run(["schtasks", "/Run", "/TN", TN])
        print(("watchdog started: " if rc == 0 else "run fail: ") + out)
        rc, out = run(["schtasks", "/Query", "/TN", TN, "/V", "/FO", "LIST"])
        for line in out.splitlines():
            if any(k in line for k in ("TaskName", "Status", "Last Run Time",
                                       "Last Result", "Next Run Time", "Logon Mode")):
                print(line.strip())


if __name__ == "__main__":
    main()
