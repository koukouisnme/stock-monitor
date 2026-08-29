"""SQLite 存储层：K线增量落库、九转状态、推送去重、信号跟踪、快照、扫描日志。"""
import json
import os
import sqlite3
import threading
from datetime import datetime

import pandas as pd

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kline_day (
  code TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  PRIMARY KEY (code, date));
CREATE TABLE IF NOT EXISTS stock_state (
  code TEXT PRIMARY KEY, turn_count INTEGER, turn_direction TEXT,
  last_update TEXT, kline_hash TEXT);
CREATE TABLE IF NOT EXISTS push_history (
  code TEXT NOT NULL, direction TEXT NOT NULL, trade_date TEXT NOT NULL,
  level TEXT, push_time TEXT, period TEXT DEFAULT 'day',
  PRIMARY KEY (code, direction, trade_date));
CREATE TABLE IF NOT EXISTS signal_tracking (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT, name TEXT, level TEXT, action TEXT, signal_date TEXT,
  ref_close REAL, close_5d REAL, close_10d REAL, close_20d REAL,
  ret_5d REAL, ret_10d REAL, ret_20d REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS indicator_snapshot (
  code TEXT NOT NULL, period TEXT NOT NULL, trade_date TEXT NOT NULL,
  name TEXT, turn_count INTEGER, turn_complete INTEGER,
  vol_ratio REAL, vol_ratio_period REAL, amt_ratio REAL, amount REAL,
  premium REAL, pct_chg REAL, surge_type TEXT, close REAL, snapshot_time TEXT,
  PRIMARY KEY (code, period, trade_date));
CREATE TABLE IF NOT EXISTS scan_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, scan_time TEXT, mode TEXT,
  total INTEGER, signals INTEGER, errors INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS fund_params (
  code TEXT PRIMARY KEY, position REAL, last_error REAL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS premium_hist (
  code TEXT NOT NULL, date TEXT NOT NULL,
  price REAL, nav_official_est REAL, nav_reference_est REAL,
  premium_official REAL, premium_reference REAL, percentile REAL,
  PRIMARY KEY (code, date));
CREATE TABLE IF NOT EXISTS mk_list (
  code TEXT PRIMARY KEY, name TEXT, close REAL, amount REAL, type TEXT,
  updated_at TEXT);
CREATE TABLE IF NOT EXISTS mk_sim (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  payload TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS stock_meta (
  code TEXT PRIMARY KEY, industry TEXT, roe REAL,
  board_code TEXT, board_day_amt REAL, board_week_amt REAL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_snap_sort ON indicator_snapshot(period, trade_date);
"""


class Cache:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        # 老库迁移：push_history 补 period 列（新版建表已含）
        try:
            self.conn.execute("ALTER TABLE push_history ADD COLUMN period TEXT DEFAULT 'day'")
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    # ---------- K线 ----------
    def upsert_klines(self, code: str, df: pd.DataFrame):
        if df is None or df.empty:
            return
        rows = [(code, str(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]),
                 float(r["close"]), float(r["volume"]),
                 float(r.get("amount") or 0.0)) for _, r in df.iterrows()]
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO kline_day VALUES (?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()

    def get_klines(self, code: str) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT date, open, high, low, close, volume, amount FROM kline_day "
            "WHERE code=? ORDER BY date", self.conn, params=(code,))
        return df

    def last_kline_date(self, code: str) -> str:
        cur = self.conn.execute(
            "SELECT MAX(date) FROM kline_day WHERE code=?", (code,))
        row = cur.fetchone()
        return row[0] if row and row[0] else None

    # ---------- 全市场回测榜统计（K线已统一存自选池 kline_day 表） ----------
    def mk_stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT code), COUNT(*) FROM kline_day").fetchone()
        latest = self.conn.execute("SELECT MAX(date) FROM kline_day").fetchone()
        return {"codes": row[0] or 0, "rows": row[1] or 0,
                "latest": latest[0] if latest else None}

    # ---------- 全市场列表（持久化，重启不重拉） ----------
    def mk_list_save(self, rows: list):
        """全量覆盖：rows = [{code,name,close,amount,type}, ...]。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._lock:
            self.conn.execute("DELETE FROM mk_list")
            self.conn.executemany(
                "INSERT OR REPLACE INTO mk_list VALUES (?,?,?,?,?,?)",
                [(r.get("code"), r.get("name"), r.get("close"),
                  r.get("amount"), r.get("type"), now) for r in rows])
            self.conn.commit()

    def mk_list_load(self):
        """返回 (rows, updated_at_str)；空表返回 ([], None)。"""
        df = pd.read_sql_query(
            "SELECT code, name, close, amount, type, updated_at FROM mk_list "
            "ORDER BY amount DESC", self.conn)
        if df.empty:
            return [], None
        rows = df.to_dict("records")
        return rows, rows[0].get("updated_at")

    # ---------- 全市场回测榜：上次任务持久化（重启免重跑） ----------
    def mk_sim_save(self, job: dict):
        """单行JSON覆盖保存任务快照（params/results/进度/errors）。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO mk_sim (id, payload, updated_at) VALUES (1, ?, ?)",
                (json.dumps(job, ensure_ascii=False, default=float), now))
            self.conn.commit()

    def mk_sim_load(self):
        """返回上次任务dict（running/paused/stop 强制False）；无记录返回None。"""
        row = self.conn.execute(
            "SELECT payload, updated_at FROM mk_sim WHERE id=1").fetchone()
        if not row or not row[0]:
            return None
        try:
            job = json.loads(row[0])
        except Exception:
            return None
        job["running"] = False
        job["paused"] = False
        job["stop"] = False
        if not job.get("finished_at"):
            job["finished_at"] = row[1]      # 中断恢复：标记为最后保存时刻
        return job

    # ---------- 单标的数据库报告（运维中心查询用） ----------
    def code_report(self, code: str) -> dict:
        def rows(sql, params=(code,)):
            cur = self.conn.execute(sql, params)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
        cnt = self.conn.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM kline_day WHERE code=?",
            (code,)).fetchone()
        return {
            "kline": {"count": cnt[0] or 0, "first": cnt[1], "last": cnt[2],
                      "latest": rows("SELECT date, open, high, low, close, volume, amount "
                                     "FROM kline_day WHERE code=? ORDER BY date DESC LIMIT 10")},
            "stock_state": rows("SELECT * FROM stock_state WHERE code=?"),
            "snapshots": rows("SELECT period, trade_date, name, turn_count, turn_complete, "
                              "vol_ratio, vol_ratio_period, amt_ratio, amount, premium, "
                              "pct_chg, surge_type, close, snapshot_time "
                              "FROM indicator_snapshot WHERE code=? ORDER BY period"),
            "push_history": rows("SELECT direction, trade_date, level, push_time, period "
                                 "FROM push_history WHERE code=? ORDER BY trade_date DESC LIMIT 20"),
            "signal_tracking": rows("SELECT level, action, signal_date, ref_close, close_5d, "
                                    "close_10d, close_20d, ret_5d, ret_10d, ret_20d "
                                    "FROM signal_tracking WHERE code=? ORDER BY id DESC LIMIT 20"),
            "premium_hist": rows("SELECT date, price, nav_official_est, nav_reference_est, "
                                 "premium_official, premium_reference, percentile "
                                 "FROM premium_hist WHERE code=? ORDER BY date DESC LIMIT 10"),
            "fund_params": rows("SELECT position, last_error, updated_at "
                                "FROM fund_params WHERE code=?"),
        }

    def code_by_name(self, name: str):
        """名称模糊匹配 → (code, name)；先自选池再全市场表，无匹配返回 None。"""
        cur = self.conn.execute(
            "SELECT code, name FROM mk_list WHERE name LIKE ? LIMIT 1", (f"%{name}%",))
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    # ---------- 九转状态 ----------
    def set_state(self, code: str, turn_count: int, kline_hash: str = ""):
        direction = "up" if turn_count > 0 else ("down" if turn_count < 0 else "")
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO stock_state VALUES (?,?,?,?,?)",
                (code, turn_count, direction, datetime.now().isoformat(timespec="seconds"), kline_hash))
            self.conn.commit()

    def get_state(self, code: str):
        cur = self.conn.execute(
            "SELECT turn_count FROM stock_state WHERE code=?", (code,))
        row = cur.fetchone()
        return row[0] if row else None

    # ---------- 推送记录 ----------
    def record_push(self, code: str, direction: str, trade_date: str, level: str,
                    period: str = "day"):
        """period：触发周期 day/week/month（推送历史方向展示 日高9/周高9/月高9）。"""
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO push_history VALUES (?,?,?,?,?,?)",
                (code, direction, trade_date, level,
                 datetime.now().isoformat(timespec="seconds"), period))
            self.conn.commit()

    # ---------- 个股画像（ROE/行业板块/板块成交额，东财数据） ----------
    def upsert_stock_meta(self, rows: list):
        """rows = [{code, industry, roe, board_code, board_day_amt, board_week_amt}]"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO stock_meta VALUES (?,?,?,?,?,?,?)",
                [(r.get("code"), r.get("industry"), r.get("roe"), r.get("board_code"),
                  r.get("board_day_amt"), r.get("board_week_amt"), now) for r in rows])
            self.conn.commit()

    def stock_meta_map(self) -> dict:
        rows = self.conn.execute(
            "SELECT code, industry, roe, board_code, board_day_amt, board_week_amt "
            "FROM stock_meta").fetchall()
        return {r[0]: {"industry": r[1], "roe": r[2], "board_code": r[3],
                       "board_day_amt": r[4], "board_week_amt": r[5]} for r in rows}

    def push_count_today(self) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM push_history WHERE push_time LIKE ?",
            (datetime.now().strftime("%Y-%m-%d") + "%",))
        return cur.fetchone()[0]

    # ---------- 信号跟踪 ----------
    def add_tracking(self, sig) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO signal_tracking (code,name,level,action,signal_date,ref_close,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sig.code, sig.name, sig.level, sig.action, sig.trade_date,
                 sig.ref_price, datetime.now().isoformat(timespec="seconds")))
            self.conn.commit()

    def pending_tracking(self) -> list:
        return pd.read_sql_query(
            "SELECT id, code, name, level, action, signal_date, ref_close FROM signal_tracking "
            "WHERE ret_20d IS NULL", self.conn).to_dict("records")

    def update_tracking(self, track_id: int, kline: pd.DataFrame):
        """信号后5/10/20日收益回填。kline为信号日之后的日线。"""
        base = None
        row = pd.read_sql_query("SELECT ref_close, signal_date FROM signal_tracking WHERE id=?",
                                self.conn, params=(track_id,))
        if row.empty:
            return
        base = float(row["ref_close"].iloc[0])
        sig_date = row["signal_date"].iloc[0]
        after = kline[kline["date"] > sig_date]
        updates, params = [], []
        for n in (5, 10, 20):
            if len(after) >= n:
                c = float(after["close"].iloc[n - 1])
                updates.append(f"close_{n}d=?")
                params.append(c)
                updates.append(f"ret_{n}d=?")
                params.append(round((c / base - 1) * 100, 2))
        if updates:
            params.append(track_id)
            with self._lock:
                self.conn.execute(
                    f"UPDATE signal_tracking SET {', '.join(updates)} WHERE id=?", params)
                self.conn.commit()

    def tracking_stats(self) -> dict:
        df = pd.read_sql_query(
            "SELECT level, action, ret_5d, ret_10d, ret_20d FROM signal_tracking "
            "WHERE ret_10d IS NOT NULL", self.conn)
        if df.empty:
            return {}
        stats = {}
        for (level, action), g in df.groupby(["level", "action"]):
            win = (g["ret_10d"] > 0).mean() if action == "buy" else (g["ret_10d"] < 0).mean()
            stats[f"{level}-{action}"] = {
                "count": len(g), "win_rate_10d": round(float(win), 2),
                "avg_ret_10d": round(float(g["ret_10d"].mean()), 2)}
        return stats

    # ---------- 快照 ----------
    def upsert_snapshot(self, row: dict):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO indicator_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["code"], row["period"], row["trade_date"], row.get("name", ""),
                 row.get("turn_count", 0), 1 if row.get("turn_complete") else 0,
                 row.get("vol_ratio"), row.get("vol_ratio_period"), row.get("amt_ratio"),
                 row.get("amount"), row.get("premium"), row.get("pct_chg"),
                 row.get("surge_type", ""), row.get("close"),
                 datetime.now().isoformat(timespec="seconds")))
            self.conn.commit()

    def latest_snapshots(self, period: str = "day") -> list:
        df = pd.read_sql_query(
            "SELECT * FROM indicator_snapshot WHERE period=? ORDER BY snapshot_time DESC",
            self.conn, params=(period,))
        if df.empty:
            return []
        return df.drop_duplicates(subset=["code"], keep="first").to_dict("records")

    def del_snapshots(self, code: str):
        """移出自选池时清理快照+九转状态（排行立即消失，不留残影）。"""
        with self._lock:
            self.conn.execute("DELETE FROM indicator_snapshot WHERE code=?", (code,))
            self.conn.execute("DELETE FROM stock_state WHERE code=?", (code,))
            self.conn.commit()

    def purge_non_pool_snapshots(self, keep_codes: list) -> int:
        """清理不在自选池内的孤儿快照（历史移除残留），返回清理行数。"""
        codes = [str(c) for c in keep_codes if str(c).strip()]
        if not codes:
            return 0
        marks = ",".join("?" * len(codes))
        with self._lock:
            cur = self.conn.execute(
                f"DELETE FROM indicator_snapshot WHERE code NOT IN ({marks})", codes)
            n = cur.rowcount or 0
            self.conn.execute(
                f"DELETE FROM stock_state WHERE code NOT IN ({marks})", codes)
            self.conn.commit()
        return n

    # ---------- 扫描日志 ----------
    def log_scan(self, mode: str, total: int, signals: int, errors: int, note: str = ""):
        with self._lock:
            self.conn.execute(
                "INSERT INTO scan_log (scan_time, mode, total, signals, errors, note) VALUES (?,?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), mode, total, signals, errors, note))
            self.conn.commit()

    def last_scan_of_mode(self, mode: str):
        cur = self.conn.execute(
            "SELECT scan_time, signals, errors FROM scan_log WHERE mode=? "
            "ORDER BY id DESC LIMIT 1", (mode,))
        return cur.fetchone()

    # ---------- 基金参数（自校准） ----------
    def get_fund_position(self, code: str, default: float) -> float:
        cur = self.conn.execute("SELECT position FROM fund_params WHERE code=?", (code,))
        row = cur.fetchone()
        return float(row[0]) if row else default

    def set_fund_position(self, code: str, position: float, last_error: float):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO fund_params VALUES (?,?,?,?)",
                (code, position, last_error, datetime.now().isoformat(timespec="seconds")))
            self.conn.commit()

    # ---------- LOF溢价历史 ----------
    def upsert_premium(self, st) -> None:
        """每日落库 LOF 溢价快照（60日走廊数据源）。"""
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO premium_hist VALUES (?,?,?,?,?,?,?,?)",
                (st.code, st.trade_date, st.price, st.nav_official_est, st.nav_reference_est,
                 st.premium_official, st.premium_reference, st.premium_percentile))
            self.conn.commit()

    def get_premium_hist(self, code: str, days: int = 60) -> list:
        """近 N 日溢价快照，按日期升序（时间正序，直接用于走势图）。"""
        rows = pd.read_sql_query(
            "SELECT date, premium_official, premium_reference, percentile, price "
            "FROM premium_hist WHERE code=? ORDER BY date DESC LIMIT ?",
            self.conn, params=(code, days)).to_dict("records")
        return rows[::-1]

    def close(self):
        self.conn.close()
