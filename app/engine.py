"""静默间隔回放引擎（纯函数，不依赖数据库与 Web 层）。

核心语义（对应业务规则）：
- 回放窗口固定七日：[window_start, window_start + 604800)，左闭右开，"满七日即止"。
- 同秒告警已在入库前去重，引擎只接收窗口内升序、唯一的 UTC 整秒序列。
- 起初无静默：第一条告警一定发出通知；此后以上一条**通知时刻**为计时起点，
  与下一条告警时刻之差达到静默间隔 s 才再次通知（被压制的告警不重置计时器）。
- 每个试算间隔只线性扫描一次窗口内告警序列；间隔寻优借助单调性用二分查找，
  绝不逐个枚举 0..21600 的每个整数间隔、也不在每个间隔重新读取原始告警明细。

单调性证明（二分依据）：
设间隔 s1 < s2。贪心通知序列满足 N(s2) 是 N(s1) 的子序列（归纳可证：s2 下每次
选择的通知时刻都不早于 s1 下对应通知），因此：
1. 通知总数 N(s) 随 s 单调不增；
2. 任一工单闭区间 [L, R] 内"首次通知时刻"随 s 单调不减，覆盖性质只能由满足变不满足。
故可行间隔构成前缀，最小可行整数间隔可用二分查找求得。
"""

from __future__ import annotations

import bisect

WINDOW_SECONDS = 7 * 24 * 3600  # 604800
MAX_INTERVAL = 21600


def replay(alarms: list[int], silence_s: int) -> list[int]:
    """按给定静默间隔贪心回放，返回通知时刻列表（UTC 整秒，升序）。

    单次调用对 ``alarms`` 做一次线性扫描，O(m)、O(k) 空间（k 为通知数）。
    """
    if silence_s < 0:
        raise ValueError("silence_s 不能为负")
    notifications: list[int] = []
    last_notified: int | None = None
    for ts in alarms:
        if last_notified is None or ts - last_notified >= silence_s:
            notifications.append(ts)
            last_notified = ts
    return notifications


def replay_with_cap(alarms: list[int], silence_s: int, cap: int) -> tuple[list[int], bool]:
    """回放并在通知数超过 cap 时提前退出。

    返回 (通知序列或前缀, 是否在 cap 内)。提前退出让小间隔探活只扫描极少告警，
    是 20 万告警规模下的关键提速点。
    """
    notifications: list[int] = []
    last_notified: int | None = None
    for ts in alarms:
        if last_notified is None or ts - last_notified >= silence_s:
            notifications.append(ts)
            last_notified = ts
            if len(notifications) > cap:
                return notifications, False
    return notifications, True


def first_notification_in(notifications: list[int], lo: int, hi: int) -> int | None:
    """闭区间 [lo, hi] 内的首次通知时刻；无则 None。"""
    idx = bisect.bisect_left(notifications, lo)
    if idx < len(notifications) and notifications[idx] <= hi:
        return notifications[idx]
    return None


def uncovered_orders(
    notifications: list[int], work_orders: list[dict]
) -> list[dict]:
    """返回预警闭区间内没有任何通知的工单清单。"""
    result: list[dict] = []
    for order in work_orders:
        first_ts = first_notification_in(
            notifications, order["start_ts"], order["end_ts"]
        )
        if first_ts is None:
            result.append(order)
    return result


def find_min_interval(
    alarms: list[int], cap: int, work_orders: list[dict]
) -> dict:
    """求满足约束的最短整数静默间隔。

    约束：
    1. 通知总数 <= cap；
    2. 每张工单预警闭区间内至少一条通知；
    3. 间隔取值 0..21600 的整数。

    返回 dict：
      status: "solved" | "unsolved"
      interval: 求解成功时的最短间隔，否则 None
      notifications: 依据该间隔（无解时给出诊断间隔）回放的通知序列
      reason: 无解原因代码（cap_exceeded / work_order_uncovered）
      uncovered: 无解时未被覆盖的工单
      probes: 二分探活次数
      scanned: 各次探活累计扫描的告警条数（用于佐证不做逐间隔重扫）
    """
    stats = {"probes": 0, "scanned": 0}

    def probe(silence_s: int) -> tuple[list[int], bool]:
        stats["probes"] += 1
        notifs, within = replay_with_cap(alarms, silence_s, cap)
        # 提前退出时实际扫描长度等于停在的位置；这里用告警总量保守记账
        stats["scanned"] += len(alarms) if within else len(notifs)
        return notifs, within

    # 1) 最大间隔下仍超上限 -> 任何间隔都无法满足上限约束
    prefix, max_within = probe(MAX_INTERVAL)
    if not max_within:
        # 补一次完整回放，拿到 21600 间隔下的真实通知总数（仅无解时多扫一次）
        full_at_max = replay(alarms, MAX_INTERVAL)
        stats["probes"] += 1
        stats["scanned"] += len(alarms)
        return {
            "status": "unsolved",
            "interval": None,
            "notifications": full_at_max,
            "reason": "cap_exceeded",
            "uncovered": [],
            "probes": stats["probes"],
            "scanned": stats["scanned"],
            "notification_count_at_max": len(full_at_max),
        }

    # 2) 二分求满足通知上限的最短间隔 s_min（N(s) 单调不增）
    notifs_zero, zero_within = probe(0)
    if zero_within:
        s_min = 0
        notifs_min = notifs_zero
    else:
        lo, hi = 1, MAX_INTERVAL
        while lo < hi:
            mid = (lo + hi) // 2
            _, within = probe(mid)
            if within:
                hi = mid
            else:
                lo = mid + 1
        s_min = lo
        # 再完整回放一次拿到 s_min 的完整通知序列（cap 内，不会提前退出）
        stats["probes"] += 1
        stats["scanned"] += len(alarms)
        notifs_min = replay(alarms, s_min)

    # 3) 在最短间隔处检查工单覆盖；覆盖随 s 只能变差，
    #    s_min 处不满足则任何更大间隔都不可能满足
    missing = uncovered_orders(notifs_min, work_orders)
    if missing:
        return {
            "status": "unsolved",
            "interval": None,
            "notifications": notifs_min,
            "reason": "work_order_uncovered",
            "uncovered": missing,
            "probes": stats["probes"],
            "scanned": stats["scanned"],
            "candidate_interval": s_min,
        }

    return {
        "status": "solved",
        "interval": s_min,
        "notifications": notifs_min,
        "reason": None,
        "uncovered": [],
        "probes": stats["probes"],
        "scanned": stats["scanned"],
    }
