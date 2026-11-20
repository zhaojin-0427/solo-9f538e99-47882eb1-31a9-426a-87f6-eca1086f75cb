"""SQLite 连接与建表脚本。

设计要点：
- ``scoring_slice`` 保存不可变的评分切片版本；同一 (风机, 切片名) 版本严格递增。
- ``slice_alarm`` 保存随该版切片一并落盘的触发告警 UTC 整秒明细。
  ``has_detail`` 区分"省略列表（缺明细）"与"空列表（确无告警）"两种语义。
- ``trial_run`` 保存每次核算的入参与结论，重启后选旧版切片仍读该版明细。
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 评分切片版本（不可变）
CREATE TABLE IF NOT EXISTS scoring_slice (
    id            INTEGER PRIMARY KEY,
    turbine_id    TEXT    NOT NULL,
    slice_name    TEXT    NOT NULL,
    version       INTEGER NOT NULL,
    has_detail    INTEGER NOT NULL,              -- 0=省略列表（缺明细），1=提供了列表（可为空）
    alarm_count   INTEGER NOT NULL,              -- 入库的去重告警条数
    created_at    TEXT    NOT NULL,
    UNIQUE (turbine_id, slice_name, version)
);

-- 切片版本携带的触发告警 UTC 整秒（同秒只存一条）
CREATE TABLE IF NOT EXISTS slice_alarm (
    id          INTEGER PRIMARY KEY,
    slice_id    INTEGER NOT NULL REFERENCES scoring_slice(id),
    alarm_ts    INTEGER NOT NULL,                -- Unix epoch UTC 整秒
    UNIQUE (slice_id, alarm_ts)
);
CREATE INDEX IF NOT EXISTS idx_slice_alarm_ts ON slice_alarm(slice_id, alarm_ts);

-- 核算试算记录
CREATE TABLE IF NOT EXISTS trial_run (
    id                INTEGER PRIMARY KEY,
    slice_id          INTEGER NOT NULL REFERENCES scoring_slice(id),
    turbine_id        TEXT    NOT NULL,
    start_date        TEXT    NOT NULL,          -- 起始 UTC 日期 YYYY-MM-DD
    window_start_ts   INTEGER NOT NULL,          -- 起始日零点 epoch
    notify_cap        INTEGER NOT NULL,
    work_orders_json  TEXT    NOT NULL,          -- 工单号 + 预警起止整秒
    status            TEXT    NOT NULL,          -- solved | unsolved
    interval_s        INTEGER,
    reason            TEXT,
    notifications_json TEXT,                     -- 通知时刻整秒列表
    uncovered_json    TEXT,
    probes            INTEGER,
    scanned           INTEGER,
    created_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trial_slice ON trial_run(slice_id);
"""


def db_path() -> str:
    return os.environ.get("APP_DB_PATH", "/data/alarm.db")


def get_conn() -> sqlite3.Connection:
    """每个请求一个连接，开启外键与 WAL。"""
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
