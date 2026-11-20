"""核心规则：切片版本与明细语义、回放静默计时、最短间隔寻优、无解提示。"""

from __future__ import annotations

from tests.helpers import epoch, make_slice, make_trial

MON = "2026-11-09"  # 周一，作为回放起始日
T0 = epoch(2026, 11, 9)


# --------------------------------------------------------------------------- #
# 切片登记与明细语义
# --------------------------------------------------------------------------- #
def test_slice_version_auto_increase_and_conflict(client):
    c, _ = client
    s1 = make_slice(c, alarms=[T0, T0 + 10])
    assert s1["version"] == 1 and s1["has_detail"] is True
    s2 = make_slice(c, alarms=[T0])
    assert s2["version"] == 2
    r = c.post(
        "/api/slices",
        json={"turbine_id": "WT-01", "slice_name": "weekly", "version": 2,
              "alarm_timestamps": [T0]},
    )
    assert r.status_code == 409


def test_omitted_list_means_missing_but_empty_list_means_no_alarm(client):
    c, _ = client
    # 省略字段 -> 缺明细
    missing = make_slice(c, name="s-missing", include_field=False)
    assert missing["has_detail"] is False and missing["alarm_count"] == 0
    # 显式空列表 -> 确无告警
    empty = make_slice(c, name="s-empty", alarms=[])
    assert empty["has_detail"] is True and empty["alarm_count"] == 0

    detail = c.get(f"/api/slices/{empty['id']}?include_alarms=true").json()
    assert detail["alarm_timestamps"] == []
    detail_missing = c.get(
        f"/api/slices/{missing['id']}?include_alarms=true"
    ).json()
    assert detail_missing["alarm_timestamps"] == []  # 缺明细不伪造告警


def test_alarms_deduped_and_sorted_on_registration(client):
    c, _ = client
    s = make_slice(c, alarms=[T0 + 30, T0 + 10, T0 + 10, T0])
    detail = c.get(f"/api/slices/{s['id']}?include_alarms=true").json()
    assert detail["alarm_timestamps"] == [T0, T0 + 10, T0 + 30]
    assert detail["alarm_count"] == 3


# --------------------------------------------------------------------------- #
# 回放与静默计时
# --------------------------------------------------------------------------- #
def test_first_alarm_notifies_and_only_notifications_start_timer(client):
    c, _ = client
    # 告警 0, 100, 150, 200；间隔 100：
    # t=0 通知（起初无静默）；t=100 距上次通知 100 -> 通知；
    # t=150 距 100 仅 50 -> 压制（不重置计时）；t=200 距上次通知 100 -> 通知
    alarms = [T0, T0 + 100, T0 + 150, T0 + 200]
    s = make_slice(c, alarms=alarms)
    res = make_trial(
        c, s["id"], MON, cap=10,
        work_orders=[{"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 200}],
    )
    assert res["status"] == "solved"
    # cap=10 且工单覆盖 [0,200]，最短间隔 0 即可（全部通知也不超 cap）
    assert res["interval_s"] == 0
    assert [n["ts"] for n in res["notifications"]] == alarms

    # cap=3：间隔 0 会发 4 条超上限；间隔 51 时 60s 的间距已满足静默，
    # 被压制的 t=150 不重置计时 -> 通知 [0,100,200] 恰好 3 条（间隔 50 仍为 4 条）
    res3 = make_trial(
        c, s["id"], MON, cap=3,
        work_orders=[{"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 200}],
    )
    assert res3["interval_s"] == 51
    assert [n["ts"] for n in res3["notifications"]] == [T0, T0 + 100, T0 + 200]
    assert res3["notification_count"] == 3
    # 工单首次通知
    wo = res3["work_orders"][0]
    assert wo["first_notification_ts"] == T0


def test_same_second_alarms_merged_once(client):
    c, _ = client
    s = make_slice(c, alarms=[T0 + 5, T0 + 5, T0 + 5, T0 + 60])
    res = make_trial(
        c, s["id"], MON, cap=100,
        work_orders=[{"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 60}],
    )
    assert [n["ts"] for n in res["notifications"]] == [T0 + 5, T0 + 60]
    assert res["alarm_total_in_window"] == 2


def test_min_interval_found_by_binary_search(client):
    c, _ = client
    # 每 60 秒一条，共 120 条；cap=10 -> 需要的间隔约 ceil(窗口内压制)
    alarms = [T0 + 60 * i for i in range(120)]
    s = make_slice(c, alarms=alarms)
    res = make_trial(
        c, s["id"], MON, cap=10,
        work_orders=[{"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 60 * 119}],
    )
    assert res["status"] == "solved"
    assert res["notification_count"] <= 10
    # 间隔再小 1 秒必然超上限（最短性）
    from app.engine import replay
    smaller = replay(alarms, res["interval_s"] - 1)
    assert len(smaller) > 10
    # 探活次数远小于逐间隔枚举（二分 log2(21601) 上取整 15 次 + 常数次）
    assert res["probes"] <= 20


def test_work_order_coverage_drives_no_solution(client):
    c, _ = client
    # 只有一条告警在 t=100；工单要求 [500,600] 内有通知 -> 任何间隔都无法覆盖
    s = make_slice(c, alarms=[T0 + 100])
    res = make_trial(
        c, s["id"], MON, cap=10,
        work_orders=[{"order_no": "WO-9", "start_ts": T0 + 500, "end_ts": T0 + 600}],
    )
    assert res["status"] == "unsolved"
    assert res["reason"] == "work_order_uncovered"
    assert res["interval_s"] is None
    assert [u["order_no"] for u in res["uncovered"]] == ["WO-9"]
    assert res["work_orders"][0]["first_notification_ts"] is None
    assert "无解" in res["reason_message"]


def test_cap_unsatisfiable_even_at_max_interval(client):
    c, _ = client
    # 间隔 21600 下，每隔 >21600s 放一条告警，七日可放 3 条，cap=1 仍超
    alarms = [T0, T0 + 21601, T0 + 21601 * 2]
    s = make_slice(c, alarms=alarms)
    res = make_trial(
        c, s["id"], MON, cap=1,
        work_orders=[{"order_no": "WO-1", "start_ts": T0,
                      "end_ts": T0 + 21601 * 2}],
    )
    assert res["status"] == "unsolved"
    assert res["reason"] == "cap_exceeded"
    assert res["notification_count_at_max_interval"] == 3
    assert "21600" in res["reason_message"]


def test_work_order_closed_interval_boundary(client):
    c, _ = client
    # 通知恰好落在工单闭区间端点上也算覆盖
    s = make_slice(c, alarms=[T0 + 300])
    res = make_trial(
        c, s["id"], MON, cap=5,
        work_orders=[{"order_no": "WO-1", "start_ts": T0 + 300,
                      "end_ts": T0 + 300}],
    )
    assert res["status"] == "solved"
    assert res["work_orders"][0]["first_notification_ts"] == T0 + 300


def test_seven_day_window_clip_and_start_at_midnight(client):
    c, _ = client
    # 窗口 [T0, T0+604800)：T0 整点在窗内，T0+604800（第八日零点）被裁掉
    alarms = [T0, T0 + 604799, T0 + 604800, T0 + 604801]
    s = make_slice(c, alarms=alarms)
    res = make_trial(
        c, s["id"], MON, cap=100,
        work_orders=[{"order_no": "WO-1", "start_ts": T0,
                      "end_ts": T0 + 604799}],
    )
    assert res["window_start_ts"] == T0
    assert res["window_end_ts"] == T0 + 604800
    assert res["alarm_total_in_window"] == 2
    assert [n["ts"] for n in res["notifications"]] == [T0, T0 + 604799]


def test_empty_detail_slice_is_triable_but_uncovered(client):
    c, _ = client
    # 显式空列表（确无告警）可以核算，但工单必然无通知 -> 无解而非 422
    s = make_slice(c, name="explicit-empty", alarms=[])
    res = make_trial(
        c, s["id"], MON, cap=10,
        work_orders=[{"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 100}],
    )
    assert res["status"] == "unsolved"
    assert res["reason"] == "work_order_uncovered"
    assert res["alarm_total_in_window"] == 0


def test_missing_detail_blocks_trial_and_lists_missing_item(client):
    c, _ = client
    s = make_slice(c, name="ghost", include_field=False)
    r = c.post(
        "/api/trials",
        json={
            "slice_id": s["id"],
            "start_date": MON,
            "notify_cap": 10,
            "work_orders": [
                {"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 100}
            ],
        },
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "slice_detail_missing"
    assert body["details"]["missing"][0]["slice_id"] == s["id"]
    # 没有产生任何核算记录
    assert c.get("/api/trials").json() == []
