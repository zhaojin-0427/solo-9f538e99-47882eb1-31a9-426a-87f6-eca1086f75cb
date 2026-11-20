"""测试辅助：快速构造切片与工单数据。"""

from __future__ import annotations

from datetime import datetime, timezone


def epoch(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> int:
    """UTC 日历时间转整秒。"""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def make_slice(
    client,
    turbine: str = "WT-01",
    name: str = "weekly",
    alarms: list[int] | None = None,
    version: int | None = None,
    include_field: bool = True,
) -> dict:
    """登记切片。alarms=None 且 include_field=False 时省略明细字段（缺明细）。"""
    body: dict = {"turbine_id": turbine, "slice_name": name}
    if version is not None:
        body["version"] = version
    if include_field:
        body["alarm_timestamps"] = [] if alarms is None else alarms
    r = client.post("/api/slices", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def make_trial(
    client,
    slice_id: int,
    start_date: str,
    cap: int,
    work_orders: list[dict],
) -> dict:
    r = client.post(
        "/api/trials",
        json={
            "slice_id": slice_id,
            "start_date": start_date,
            "notify_cap": cap,
            "work_orders": work_orders,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()
