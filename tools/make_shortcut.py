"""在桌面创建「A股监控台」快捷方式：双击即启动常驻调度（run_monitor.bat），带专属图标。

用法（任意机器、项目移动后均可重跑）:
  py312\\python.exe tools\\make_shortcut.py
"""
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(BASE, "run_monitor.bat")
ICON = os.path.join(BASE, "tools", "monitor.ico")
LNK_NAME = "A股监控台.lnk"


def main():
    if not os.path.exists(TARGET):
        print(f"[失败] 未找到入口: {TARGET}")
        sys.exit(1)
    if not os.path.exists(ICON):
        print(f"[失败] 未找到图标: {ICON}")
        sys.exit(1)
    # 通过临时VBS调WScript.Shell创建.lnk（Windows自带，无需pywin32）
    vbs = os.path.join(tempfile.gettempdir(), "_sm_shortcut.vbs")
    with open(vbs, "w", encoding="gbk", errors="replace") as f:
        f.write(
            'Set s = CreateObject("WScript.Shell")\n'
            'desk = s.SpecialFolders("Desktop")\n'
            f'Set lnk = s.CreateShortcut(desk & "\\{LNK_NAME}")\n'
            f'lnk.TargetPath = "{TARGET}"\n'
            f'lnk.WorkingDirectory = "{BASE}"\n'
            f'lnk.IconLocation = "{ICON},0"\n'
            'lnk.Description = "A股监控系统 · 一键启动常驻调度（收盘扫描/盘中扫描/心跳）"\n'
            'lnk.WindowStyle = 1\n'
            'lnk.Save\n')
    r = subprocess.run(["cscript", "//nologo", vbs], capture_output=True, text=True)
    os.remove(vbs)
    if r.returncode != 0:
        print(f"[失败] {r.stderr or r.stdout}")
        sys.exit(1)
    print(f"[完成] 桌面快捷方式已创建: {LNK_NAME}")
    print(f"  目标: {TARGET}")
    print(f"  图标: {ICON}")


if __name__ == "__main__":
    main()
