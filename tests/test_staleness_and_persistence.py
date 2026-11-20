"""重启持久化、参数校验、历史复算与 20 万告警 / 500 工单性能。"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.helpers import epoch, make_slice, make_trial

MON = "2026-11-09"
T0 = epoch(2026, 11, 9)
WINDOW_END = T0 + 604800


def test_restart_keeps_old_slice_detail_and_trial_result(client):
    c, db_path = client
    alarms = [T0 + 100 * i for i in range(50)]
    s = make_slice(c, alarms=alarms)
    res = make_trial(
        c, s["id"], MON, cap=5,
        work_orders=[
            {"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 4900},
            {"order_no": "WO-2", "start_ts": T0 + 1000, "end_ts": T0 + 2000},
        ],
    )
    trial_id = res["trial_id"]
    interval = res["interval_s"]
    notif_ts = [n["ts"] for n in res["notifications"]]

    # 再登记一个新版本，模拟数据继续推进
    make_slice(c, alarms=[T0 + 1], version=None)

    # 模拟重启：全新客户端，仅靠磁盘 SQLite
    from app.main import app

    restarted = TestClient(app)
    old = restarted.get(f"/api/slices/{s['id']}?include_alarms=true").json()
    assert old["version"] == 1
    assert old["alarm_timestamps"] == alarms  # 旧版明细原样可读

    got = restarted.get(f"/api/trials/{trial_id}").json()
    assert got["interval_s"] == interval
    assert [n["ts"] for n in got["notifications"]] == notif_ts
    assert {w["order_no"] for w in got["work_orders"]} == {"WO-1", "WO-2"}
    assert all(w["first_notification_ts"] is not None for w in got["work_orders"])


def test_trial_requires_same_window_and_valid_orders(client):
    c, _ = client
    s = make_slice(c, alarms=[T0 + 10])

    # 未知切片
    r = c.post(
        "/api/trials",
        json={"slice_id": 9999, "start_date": MON, "notify_cap": 5,
              "work_orders": [{"order_no": "WO-1", "start_ts": T0,
                               "end_ts": T0 + 10}]},
    )
    assert r.status_code == 404

    # 上限越界（schema 422 由 FastAPI 校验）
    r = c.post(
        "/api/trials",
        json={"slice_id": s["id"], "start_date": MON, "notify_cap": 101,
              "work_orders": [{"order_no": "WO-1", "start_ts": T0,
                               "end_ts": T0 + 10}]},
    )
    assert r.status_code == 422

    # 工单区间超出七日窗口
    r = c.post(
        "/api/trials",
        json={"slice_id": s["id"], "start_date": MON, "notify_cap": 5,
              "work_orders": [{"order_no": "WO-1", "start_ts": T0,
                               "end_ts": WINDOW_END}]},
    )
    assert r.status_code == 400 and "回放窗口" in r.json()["error"]["message"]

    # 起始晚于结束
    r = c.post(
        "/api/trials",
        json={"slice_id": s["id"], "start_date": MON, "notify_cap": 5,
              "work_orders": [{"order_no": "WO-1", "start_ts": T0 + 100,
                               "end_ts": T0 + 10}]},
    )
    assert r.status_code == 400

    # 工单号重复
    r = c.post(
        "/api/trials",
        json={"slice_id": s["id"], "start_date": MON, "notify_cap": 5,
              "work_orders": [
                  {"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 10},
                  {"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 10},
              ]},
    )
    assert r.status_code == 400 and "重复" in r.json()["error"]["message"]

    # 非法日期格式
    r = c.post(
        "/api/trials",
        json={"slice_id": s["id"], "start_date": "20261109", "notify_cap": 5,
              "work_orders": [{"order_no": "WO-1", "start_ts": T0,
                               "end_ts": T0 + 10}]},
    )
    assert r.status_code == 422


def test_500_work_orders_all_reported_and_covered(client):
    c, _ = client
    # 每分钟一条告警，覆盖整周
    alarms = list(range(T0, WINDOW_END, 60))
    s = make_slice(c, alarms=alarms)
    # 500 张工单均匀散布在七日窗口，每张 2 小时宽（大于通知间隔），保证可被覆盖
    span = WINDOW_END - T0
    orders = []
    for i in range(500):
        # 中心位置整体内缩 2 小时，保证 2 小时宽的工单不越过七日窗口
        center = T0 + 3600 + (i + 1) * (span - 7200) // 501
        orders.append(
            {"order_no": f"WO-{i:04d}",
             "start_ts": center - 3600,
             "end_ts": center + 3600}
        )
    res = make_trial(c, s["id"], MON, cap=100, work_orders=orders)
    assert res["status"] == "solved"
    assert len(res["work_orders"]) == 500
    assert all(w["first_notification_ts"] is not None for w in res["work_orders"])
    # 每分钟一条、七日共 10080 条；cap=100 约束下用较大间隔稀疏通知，
    # 30 分钟宽的工单仍能被覆盖
    assert res["notification_count"] <= 100


def test_performance_200k_alarms_no_per_interval_rescan(client):
    c, _ = client
    # 20 万条原始告警（含同秒重复），去重后约 20 万；窗口内整秒稀疏分布
    raw = []
    step = 604800 // 200000  # 约 3 秒一步
    ts = T0
    for i in range(200000):
        raw.append(ts)
        if i % 7 == 0:  # 注入同秒重复，验收入库去重
            raw.append(ts)
        ts += step
        if ts >= WINDOW_END:
            break
    s = make_slice(c, alarms=raw)
    assert s["alarm_count"] >= 190000  # 去重后仍接近 20 万

    orders = [
        {"order_no": f"WO-{i:03d}",
         "start_ts": T0 + i * 12000,
         "end_ts": T0 + i * 12000 + 6000}
        for i in range(10)
    ]
    started = time.perf_counter()
    res = make_trial(c, s["id"], MON, cap=50, work_orders=orders)
    elapsed = time.perf_counter() - started

    assert res["status"] == "solved"
    assert res["notification_count"] <= 50
    # 关键性能约束：二分探活仅十几次，累计扫描量 = O(probes × m)，
    # 而不是逐间隔（最多 21601 次）重扫
    assert res["probes"] <= 20
    naive_scans = 21601 * res["alarm_total_in_window"]
    assert res["alarm_scans"] < naive_scans // 100
    assert elapsed < 5.0, f"试算耗时 {elapsed:.2f}s 超过 5s 预算"


def test_engine_scan_count_accounting(client):
    """alarm_scans 记账只统计窗口内序列扫描，且探活次数有界。"""
    c, _ = client
    alarms = [T0 + i for i in range(0, 10000, 7)]
    s = make_slice(c, alarms=alarms)
    res = make_trial(
        c, s["id"], MON, cap=3,
        work_orders=[{"order_no": "WO-1", "start_ts": T0,
                      "end_ts": T0 + 9999}],
    )
    # 每次完整探活至多扫描窗口告警总数，总扫描量有界
    assert res["alarm_scans"] <= res["probes"] * res["alarm_total_in_window"]
    assert res["probes"] <= 20
