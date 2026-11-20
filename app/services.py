"""核心领域逻辑与事务服务。

关键规则：
1. 切片版本不可变，同 (风机, 切片名) 版本严格递增；告警明细随该版一并保存。
   省略 alarm_timestamps -> has_detail=0（缺明细）；传 [] -> has_detail=1 且零告警。
2. 核算必须选择已存在的切片版本；缺明细时列出缺项并停止核算（422）。
3. 回放窗口为起始 UTC 日期零点起的七整日 [t0, t0+604800)，同秒告警合为一次。
4. 起初无静默，只有回放中真正发出的通知才开始计时；间隔在 0..21600 整数中寻优。
5. 寻优在引擎内用二分完成，本层只从库中取一次窗口告警，不做逐间隔重扫。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone

from . import engine
from .errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from .schemas import SliceIn, TrialIn, WorkOrderIn


# --------------------------------------------------------------------------- #
# 时间工具
# --------------------------------------------------------------------------- #
def parse_utc_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise BadRequestError(f"非法 UTC 日期: {value}") from exc


def day_start_epoch(utc_date: str) -> int:
    d = parse_utc_date(utc_date)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def epoch_to_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# 切片登记（版本化、不可变）
# --------------------------------------------------------------------------- #
def register_slice(conn: sqlite3.Connection, payload: SliceIn) -> dict:
    row = conn.execute(
        "SELECT MAX(version) AS max_ver FROM scoring_slice "
        "WHERE turbine_id = ? AND slice_name = ?",
        (payload.turbine_id, payload.slice_name),
    ).fetchone()
    max_ver = row["max_ver"]
    version = payload.version if payload.version is not None else (max_ver or 0) + 1
    if max_ver is not None and version <= max_ver:
        raise ConflictError(
            f"切片 {payload.turbine_id}/{payload.slice_name} 版本必须严格递增，"
            f"当前最大为 {max_ver}，收到 {version}"
        )

    has_detail = payload.alarm_timestamps is not None
    unique_ts = sorted(set(payload.alarm_timestamps)) if has_detail else []

    cur = conn.execute(
        "INSERT INTO scoring_slice (turbine_id, slice_name, version, has_detail, "
        "alarm_count, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            payload.turbine_id,
            payload.slice_name,
            version,
            1 if has_detail else 0,
            len(unique_ts),
            now_iso(),
        ),
    )
    slice_id = cur.lastrowid
    if has_detail and unique_ts:
        conn.executemany(
            "INSERT INTO slice_alarm (slice_id, alarm_ts) VALUES (?, ?)",
            [(slice_id, ts) for ts in unique_ts],
        )
    result = get_slice(conn, slice_id, include_alarms=True)
    return result


def get_slice(
    conn: sqlite3.Connection, slice_id: int, include_alarms: bool = False
) -> dict:
    row = conn.execute(
        "SELECT * FROM scoring_slice WHERE id = ?", (slice_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"切片版本 id={slice_id} 不存在")
    record = dict(row)
    record["has_detail"] = bool(row["has_detail"])
    if include_alarms:
        record["alarm_timestamps"] = [
            r["alarm_ts"]
            for r in conn.execute(
                "SELECT alarm_ts FROM slice_alarm WHERE slice_id = ? ORDER BY alarm_ts",
                (slice_id,),
            ).fetchall()
        ]
    return record


def list_slices(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, turbine_id, slice_name, version, has_detail, alarm_count, "
        "created_at FROM scoring_slice ORDER BY id"
    ).fetchall()
    result = []
    for row in rows:
        record = dict(row)
        record["has_detail"] = bool(row["has_detail"])
        result.append(record)
    return result


# --------------------------------------------------------------------------- #
# 核算试算
# --------------------------------------------------------------------------- #
def _load_window_alarms(
    conn: sqlite3.Connection, slice_id: int, window_start: int, window_end: int
) -> list[int]:
    """只读取一次回放窗口内、已去重的告警整秒（窗口左闭右开）。"""
    rows = conn.execute(
        "SELECT alarm_ts FROM slice_alarm "
        "WHERE slice_id = ? AND alarm_ts >= ? AND alarm_ts < ? ORDER BY alarm_ts",
        (slice_id, window_start, window_end),
    ).fetchall()
    return [r["alarm_ts"] for r in rows]


def _validate_work_orders(
    work_orders: list[WorkOrderIn], window_start: int, window_end: int
) -> list[dict]:
    seen: set[str] = set()
    normalized: list[dict] = []
    for order in work_orders:
        if order.order_no in seen:
            raise BadRequestError(f"工单号重复: {order.order_no}")
        seen.add(order.order_no)
        if order.start_ts > order.end_ts:
            raise BadRequestError(
                f"工单 {order.order_no} 的预警起始整秒不能晚于结束整秒"
            )
        # 闭区间必须落在七日内（末日 23:59:59 即 window_end - 1）
        if not (window_start <= order.start_ts <= window_end - 1) or not (
            window_start <= order.end_ts <= window_end - 1
        ):
            raise BadRequestError(
                f"工单 {order.order_no} 的预警区间 [{order.start_ts}, {order.end_ts}] "
                f"超出回放窗口 [{window_start}, {window_end - 1}]"
            )
        normalized.append(
            {
                "order_no": order.order_no,
                "start_ts": order.start_ts,
                "end_ts": order.end_ts,
            }
        )
    return normalized


def run_trial(conn: sqlite3.Connection, payload: TrialIn) -> dict:
    slice_row = conn.execute(
        "SELECT * FROM scoring_slice WHERE id = ?", (payload.slice_id,)
    ).fetchone()
    if slice_row is None:
        raise NotFoundError(f"切片版本 id={payload.slice_id} 不存在")

    # 缺明细：列出缺项并停止核算
    if not slice_row["has_detail"]:
        raise UnprocessableError(
            code="slice_detail_missing",
            message=(
                f"切片 {slice_row['turbine_id']}/{slice_row['slice_name']} "
                f"v{slice_row['version']} 登记时省略了告警明细，无法核算；"
                "请改用带明细的切片版本"
            ),
            extra={
                "missing": [
                    {
                        "slice_id": slice_row["id"],
                        "turbine_id": slice_row["turbine_id"],
                        "slice_name": slice_row["slice_name"],
                        "version": slice_row["version"],
                        "missing_field": "alarm_timestamps",
                    }
                ]
            },
        )

    window_start = day_start_epoch(payload.start_date)
    window_end = window_start + engine.WINDOW_SECONDS
    work_orders = _validate_work_orders(payload.work_orders, window_start, window_end)

    # 全流程仅在此处读取一次告警明细（已按整秒去重、按时间升序）
    alarms = _load_window_alarms(
        conn, payload.slice_id, window_start, window_end
    )

    outcome = engine.find_min_interval(alarms, payload.notify_cap, work_orders)

    # 每张工单区间内的首次通知时刻（成功时必有）
    first_by_order: list[dict] = []
    for order in work_orders:
        first_ts = engine.first_notification_in(
            outcome["notifications"], order["start_ts"], order["end_ts"]
        )
        first_by_order.append(
            {
                "order_no": order["order_no"],
                "start_ts": order["start_ts"],
                "end_ts": order["end_ts"],
                "start_iso": epoch_to_iso(order["start_ts"]),
                "end_iso": epoch_to_iso(order["end_ts"]),
                "first_notification_ts": first_ts,
                "first_notification_iso": (
                    epoch_to_iso(first_ts) if first_ts is not None else None
                ),
            }
        )

    notifications = [
        {"ts": ts, "iso": epoch_to_iso(ts)} for ts in outcome["notifications"]
    ]

    result = {
        "trial_id": None,  # 落库后回填
        "slice_id": payload.slice_id,
        "turbine_id": slice_row["turbine_id"],
        "slice_name": slice_row["slice_name"],
        "slice_version": slice_row["version"],
        "start_date": payload.start_date,
        "window_start_ts": window_start,
        "window_end_ts": window_end,
        "window_start_iso": epoch_to_iso(window_start),
        "window_end_iso": epoch_to_iso(window_end),
        "notify_cap": payload.notify_cap,
        "alarm_total_in_window": len(alarms),
        "status": outcome["status"],
        "interval_s": outcome["interval"],
        "notification_count": len(outcome["notifications"]),
        "notifications": notifications,
        "work_orders": first_by_order,
        "probes": outcome["probes"],
        "alarm_scans": outcome["scanned"],
        "reason": outcome["reason"],
        "reason_message": _reason_message(outcome, payload.notify_cap),
        "uncovered": outcome["uncovered"],
        "created_at": now_iso(),
    }
    if outcome["reason"] == "cap_exceeded":
        result["notification_count_at_max_interval"] = outcome[
            "notification_count_at_max"
        ]

    trial_id = _persist_trial(conn, payload, result, work_orders)
    result["trial_id"] = trial_id
    return result


def _reason_message(outcome: dict, cap: int) -> str | None:
    if outcome["status"] == "solved":
        return None
    if outcome["reason"] == "cap_exceeded":
        return (
            f"无解：即使取最大间隔 {engine.MAX_INTERVAL}s，"
            f"通知总数仍为 {outcome['notification_count_at_max']} 条，"
            f"超过上限 {cap} 条"
        )
    missing_no = "、".join(o["order_no"] for o in outcome["uncovered"])
    return (
        f"无解：满足通知上限的最短间隔为 {outcome['candidate_interval']}s，"
        f"但工单 {missing_no} 的预警闭区间内没有任何通知；"
        "更大间隔只会进一步压制通知，故 0~21600s 内无可行整数间隔"
    )


def _persist_trial(
    conn: sqlite3.Connection,
    payload: TrialIn,
    result: dict,
    work_orders: list[dict],
) -> int:
    cur = conn.execute(
        "INSERT INTO trial_run (slice_id, turbine_id, start_date, window_start_ts, "
        "notify_cap, work_orders_json, status, interval_s, reason, "
        "notifications_json, uncovered_json, probes, scanned, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            payload.slice_id,
            result["turbine_id"],
            payload.start_date,
            result["window_start_ts"],
            payload.notify_cap,
            json.dumps(work_orders, ensure_ascii=False),
            result["status"],
            result["interval_s"],
            result["reason"],
            json.dumps([n["ts"] for n in result["notifications"]]),
            json.dumps(result["uncovered"], ensure_ascii=False),
            result["probes"],
            result["alarm_scans"],
            result["created_at"],
        ),
    )
    return cur.lastrowid


def list_trials(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, slice_id, turbine_id, start_date, notify_cap, status, "
        "interval_s, reason, notifications_json, probes, scanned, created_at "
        "FROM trial_run ORDER BY id"
    ).fetchall()
    result = []
    for row in rows:
        record = {k: row[k] for k in row.keys() if k != "notifications_json"}
        record["notification_count"] = len(json.loads(row["notifications_json"]))
        result.append(record)
    return result


def get_trial(conn: sqlite3.Connection, trial_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM trial_run WHERE id = ?", (trial_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"核算记录 id={trial_id} 不存在")
    work_orders = json.loads(row["work_orders_json"])
    notification_ts = json.loads(row["notifications_json"])
    first_by_order = []
    for order in work_orders:
        first_ts = engine.first_notification_in(
            notification_ts, order["start_ts"], order["end_ts"]
        )
        first_by_order.append(
            {
                "order_no": order["order_no"],
                "start_ts": order["start_ts"],
                "end_ts": order["end_ts"],
                "start_iso": epoch_to_iso(order["start_ts"]),
                "end_iso": epoch_to_iso(order["end_ts"]),
                "first_notification_ts": first_ts,
                "first_notification_iso": (
                    epoch_to_iso(first_ts) if first_ts is not None else None
                ),
            }
        )
    window_end = row["window_start_ts"] + engine.WINDOW_SECONDS
    return {
        "trial_id": row["id"],
        "slice_id": row["slice_id"],
        "turbine_id": row["turbine_id"],
        "start_date": row["start_date"],
        "window_start_ts": row["window_start_ts"],
        "window_end_ts": window_end,
        "window_start_iso": epoch_to_iso(row["window_start_ts"]),
        "window_end_iso": epoch_to_iso(window_end),
        "notify_cap": row["notify_cap"],
        "status": row["status"],
        "interval_s": row["interval_s"],
        "notification_count": len(notification_ts),
        "notifications": [
            {"ts": ts, "iso": epoch_to_iso(ts)} for ts in notification_ts
        ],
        "work_orders": first_by_order,
        "probes": row["probes"],
        "alarm_scans": row["scanned"],
        "reason": row["reason"],
        "uncovered": json.loads(row["uncovered_json"]),
        "created_at": row["created_at"],
    }
