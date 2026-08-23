# -*- coding: utf-8 -*-
r"""一键导出迁移包：把本项目打包成 zip，拷到其他 Windows 电脑即可运行。

用法（在本项目根目录下执行）：
  py312\python.exe tools\export_bundle.py              # 完整包（含历史数据库，约几百MB）
  py312\python.exe tools\export_bundle.py --no-data    # 精简包（不含数据库，约250MB；新电脑首次扫描自动重新拉数据）
  py312\python.exe tools\export_bundle.py --out D:\xx  # 指定输出目录

包内容：
  - 全部代码 + config.yaml（含企微推送配置，拷走即用）
  - py312 内嵌Python（目标电脑无需安装任何环境）
  - tools/cloudflared.exe（公网隧道）
  - data/monitor.db（可选：K线/快照/推送历史，用SQLite在线备份，服务运行中打包也安全）

自动排除：__pycache__、日志、看护锁、K线图缓存、临时探测脚本。
"""
import argparse
import os
import sqlite3
import sys
import tempfile
import time
import zipfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDE_DIRS = {"__pycache__", ".git", "charts"}
EXCLUDE_FILES = {"watchdog.lock", "watchdog.log", "web_service.log",
                 "test_selftest.db", "_web.log"}
EXCLUDE_PREFIX = ("_probe_", "_verify_", "_logtail", "_rb2", "_killweb")


def backup_db(src_db: str, dst_db: str) -> bool:
    """SQLite 在线备份（服务运行中调用也安全）。"""
    if not os.path.exists(src_db):
        return False
    src = dst = None
    try:
        src = sqlite3.connect(src_db)
        dst = sqlite3.connect(dst_db)
        src.backup(dst)
        return True
    except Exception as e:
        print(f"  [警告] 数据库备份失败（将跳过数据）：{e}")
        return False
    finally:
        for c in (src, dst):
            if c:
                c.close()


def iter_files(with_data: bool, db_copy: str):
    """生成 (zip内相对路径, 磁盘绝对路径)。"""
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        for fn in fns:
            if fn in EXCLUDE_FILES or fn.startswith(EXCLUDE_PREFIX) \
                    or fn.endswith((".pyc", ".log")):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, ROOT)
            # 数据库：用备份副本替换（排除 wal/shm 伴生文件）
            if rel == os.path.join("data", "monitor.db"):
                if with_data and os.path.exists(db_copy):
                    yield rel, db_copy
                continue
            if rel.startswith("data" + os.sep) and rel.endswith(("-wal", "-shm")):
                continue
            yield rel, full


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-data", action="store_true", help="不含历史数据库")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(ROOT), "Desktop"),
                    help="输出目录")
    args = ap.parse_args()
    with_data = not args.no_data

    os.makedirs(args.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M")
    tag = "full" if with_data else "lite"
    zip_path = os.path.join(args.out, f"stock_monitor_{tag}_{stamp}.zip")
    print(f"输出: {zip_path}")
    print(f"模式: {'完整包（含数据库）' if with_data else '精简包（不含数据库）'}")

    db_copy = ""
    if with_data:
        print("备份数据库（在线安全备份）...")
        db_copy = os.path.join(tempfile.gettempdir(), "sm_export_db.db")
        if os.path.exists(db_copy):
            os.remove(db_copy)
        if backup_db(os.path.join(ROOT, "data", "monitor.db"), db_copy):
            sz = os.path.getsize(db_copy) / 1024 / 1024
            print(f"  数据库备份完成 {sz:.0f} MB")
        else:
            with_data = False
            print("  改为精简包")

    n, total = 0, 0
    t0 = time.time()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, full in iter_files(with_data, db_copy):
            z.write(full, os.path.join("stock_monitor", rel))
            n += 1
            total += os.path.getsize(full)
            if n % 500 == 0:
                print(f"  已打包 {n} 个文件...")
    if db_copy and os.path.exists(db_copy):
        os.remove(db_copy)

    zs = os.path.getsize(zip_path) / 1024 / 1024
    print(f"\n完成：{n} 个文件，原始 {total/1024/1024:.0f} MB → 压缩 {zs:.0f} MB"
          f"，耗时 {time.time()-t0:.0f} 秒")
    print(f"\n目标电脑操作：")
    print(f"  1. 解压 zip 到任意目录（如 D:\\stock_monitor）")
    print(f"  2. 双击「启动监控台.bat」即可运行（无需安装Python）")
    print(f"  3. 如需开机自启+崩溃自愈，管理员CMD执行：")
    print(f"     cd /d 解压目录 && py312\\python.exe tools\\_install_autostart.py install")


if __name__ == "__main__":
    main()
