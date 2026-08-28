"""在桌面创建快捷方式（带专属图标）：
  - 「A股监控台」→ run_monitor.bat：常驻调度（收盘扫描/盘中扫描/心跳）
  - 「启动监控台」→ 启动监控台.bat：Web看板 + Cloudflare隧道（手机可访问）

用法（任意机器、项目移动后均可重跑）:
  py312\\python.exe tools\\make_shortcut.py
"""
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON = os.path.join(BASE, "tools", "monitor.ico")

SHORTCUTS = [
    {"lnk": "A股监控台.lnk", "target": os.path.join(BASE, "run_monitor.bat"),
     "desc": "A股监控系统 · 一键启动常驻调度（收盘扫描/盘中扫描/心跳）"},
    {"lnk": "启动监控台.lnk", "target": os.path.join(BASE, "启动监控台.bat"),
     "desc": "A股监控系统 · Web看板 + 云隧道（手机访问）"},
]

_VBS_TMPL = (
    'Set s = CreateObject("WScript.Shell")\n'
    'desk = s.SpecialFolders("Desktop")\n'
    'Set lnk = s.CreateShortcut(desk & "\\{lnk}")\n'
    'lnk.TargetPath = "{target}"\n'
    'lnk.WorkingDirectory = "{base}"\n'
    'lnk.IconLocation = "{icon},0"\n'
    'lnk.Description = "{desc}"\n'
    'lnk.WindowStyle = 1\n'
    'lnk.Save\n')


def main():
    if not os.path.exists(ICON):
        print(f"[失败] 未找到图标: {ICON}")
        sys.exit(1)
    for sc in SHORTCUTS:
        if not os.path.exists(sc["target"]):
            print(f"[失败] 未找到入口: {sc['target']}")
            sys.exit(1)
        # 通过临时VBS调WScript.Shell创建.lnk（Windows自带，无需pywin32）
        vbs = os.path.join(tempfile.gettempdir(), "_sm_shortcut.vbs")
        with open(vbs, "w", encoding="gbk", errors="replace") as f:
            f.write(_VBS_TMPL.format(lnk=sc["lnk"], target=sc["target"],
                                     base=BASE, icon=ICON, desc=sc["desc"]))
        r = subprocess.run(["cscript", "//nologo", vbs], capture_output=True, text=True)
        os.remove(vbs)
        if r.returncode != 0:
            print(f"[失败] {sc['lnk']}: {r.stderr or r.stdout}")
            sys.exit(1)
        print(f"[完成] 桌面快捷方式已创建: {sc['lnk']}")
        print(f"  目标: {sc['target']}")
        print(f"  图标: {ICON}")


if __name__ == "__main__":
    main()
