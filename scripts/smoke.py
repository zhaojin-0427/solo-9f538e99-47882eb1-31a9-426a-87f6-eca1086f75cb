#!/usr/bin/env python3
"""端到端冒烟脚本：用标准库 urllib 走完整业务剧情。

剧情：
1. 登记带明细的切片 v1（含同秒重复，验去重）与缺明细切片 v2；
2. 缺明细切片发起核算 -> 422，列出缺项并停止，不留核算记录；
3. 正常切片试算 -> 得到最短间隔、通知时刻、每张工单区间内首次通知；
4. 无解场景（工单区间无告警可覆盖）-> 明确提示无解与原因；
5. "重启"视角下用 GET 接口读旧版明细与历史核算，结论一致。

用法: python scripts/smoke.py [base_url]
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE_DATE = "2026-11-09"


def epoch(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


T0 = epoch(2026, 11, 9)


def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}{(' -> ' + detail) if detail else ''}")
    if not ok:
        raise SystemExit(1)


def trial_body(slice_id: int, cap: int, orders: list[dict]) -> dict:
    return {
        "slice_id": slice_id,
        "start_date": BASE_DATE,
        "notify_cap": cap,
        "work_orders": orders,
    }


def main(base: str) -> None:
    # 1. 切片 v1：带明细（含同秒重复与窗口外告警）
    alarms = [T0, T0 + 60, T0 + 60, T0 + 120, T0 + 3600,
              T0 + 7200, T0 + 604800]  # 最后一条落在第八日零点，应被窗口裁掉
    status, s1 = call("POST", f"{base}/api/slices", {
        "turbine_id": "WT-01", "slice_name": "weekly_score",
        "alarm_timestamps": alarms})
    check("登记带明细切片 v1", status == 201 and s1["version"] == 1)
    check("同秒告警去重、窗口外数据仍随版保存",
          s1["alarm_count"] == 6, str(s1["alarm_count"]))

    # 2. 缺明细切片（省略 alarm_timestamps 字段）
    status, s_missing = call("POST", f"{base}/api/slices", {
        "turbine_id": "WT-01", "slice_name": "no_detail"})
    check("登记缺明细切片", status == 201 and s_missing["has_detail"] is False)

    # 显式空列表 = 确无告警
    status, s_empty = call("POST", f"{base}/api/slices", {
        "turbine_id": "WT-01", "slice_name": "explicit_empty",
        "alarm_timestamps": []})
    check("空列表切片标记为有明细且零告警",
          status == 201 and s_empty["has_detail"] is True
          and s_empty["alarm_count"] == 0)

    # 3. 缺明细核算 -> 422，列出缺项并停止
    status, body = call("POST", f"{base}/api/trials", trial_body(
        s_missing["id"], 10,
        [{"order_no": "WO-1", "start_ts": T0, "end_ts": T0 + 3600}]))
    check("缺明细停止核算(422)", status == 422
          and body["error"]["code"] == "slice_detail_missing")
    check("响应列出缺项",
          body["error"]["details"]["missing"][0]["slice_id"] == s_missing["id"])
    status, trials = call("GET", f"{base}/api/trials")
    check("缺明细未产生核算记录", trials == [])

    # 4. 正常试算：cap=3 -> 间隔压制后通知不超过 3 条，工单须被覆盖
    orders = [
        {"order_no": "WO-1001", "start_ts": T0, "end_ts": T0 + 300},
        {"order_no": "WO-1002", "start_ts": T0 + 3600, "end_ts": T0 + 7200},
    ]
    status, solved = call("POST", f"{base}/api/trials",
                          trial_body(s1["id"], 3, orders))
    check("试算有解", status == 201 and solved["status"] == "solved",
          f"interval={solved.get('interval_s')}")
    check("通知数在上限内", solved["notification_count"] <= 3)
    check("回放窗口为七日且第八日零点告警被裁",
          solved["alarm_total_in_window"] == 5
          and solved["window_end_ts"] == T0 + 604800)
    check("每张工单都给出区间内首次通知",
          all(w["first_notification_ts"] is not None for w in solved["work_orders"]))
    check("当次列出间隔与通知时刻",
          solved["interval_s"] is not None and len(solved["notifications"]) >= 1)
    check("未逐间隔重扫：探活次数远小于 21601", solved["probes"] <= 20)
    trial_id = solved["trial_id"]

    # 5. 无解场景：工单预警区间内不可能有任何通知
    status, unsolved = call("POST", f"{base}/api/trials", trial_body(
        s1["id"], 10,
        [{"order_no": "WO-9999", "start_ts": T0 + 100000,
          "end_ts": T0 + 100800}]))
    check("无解明确提示",
          status == 201 and unsolved["status"] == "unsolved"
          and "无解" in unsolved["reason_message"]
          and unsolved["uncovered"][0]["order_no"] == "WO-9999")

    # 上限无法满足的无解：三条间隔 >21600s 的告警，cap=1 时即使最大间隔也有 3 条通知
    status, s_wide = call("POST", f"{base}/api/slices", {
        "turbine_id": "WT-01", "slice_name": "wide_gap",
        "alarm_timestamps": [T0, T0 + 21601, T0 + 21601 * 2]})
    check("登记宽间隔告警切片", status == 201)
    status, cap_fail = call("POST", f"{base}/api/trials", trial_body(
        s_wide["id"], 1,
        [{"order_no": "WO-1", "start_ts": T0,
          "end_ts": T0 + 21601 * 2}]))
    check("上限不可达无解",
          cap_fail["status"] == "unsolved"
          and cap_fail["reason"] == "cap_exceeded"
          and cap_fail["notification_count_at_max_interval"] == 3)

    # 6. 重启视角：旧版明细与历史核算仍可读，结论一致
    status, old_detail = call(
        "GET", f"{base}/api/slices/{s1['id']}?include_alarms=true")
    check("旧版切片明细重启后仍可读",
          status == 200 and len(old_detail["alarm_timestamps"]) == 6)
    status, history = call("GET", f"{base}/api/trials/{trial_id}")
    check("历史核算结论一致",
          status == 200
          and history["interval_s"] == solved["interval_s"]
          and [n["ts"] for n in history["notifications"]]
          == [n["ts"] for n in solved["notifications"]])

    # 7. 参数校验：上限越界
    status, _ = call("POST", f"{base}/api/trials", trial_body(
        s1["id"], 101, orders))
    check("通知上限越界被拒(422)", status == 422)

    print(f"\n冒烟通过：最短静默间隔 {solved['interval_s']}s，"
          f"通知 {solved['notification_count']} 条，探活 {solved['probes']} 次")


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    main(base_url.rstrip("/"))
