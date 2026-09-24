"""校准工位预约：占用查询、最早可用时段与事务内确认。

工位模型
--------
全实验室共用**单个校准工位**（``STATION_CODE``）。任一仪器的校准预约都占用
同一工位，占用区间按**半开区间** ``[start_at, end_at)`` 判定：

* 两段在端点首尾相接（前段 end == 后段 start）**不冲突**，可背靠背预约；
* 仅当 ``a.start < b.end and b.start < a.end`` 时才算占用重叠。

溯源约束
--------
预约（含查询与确认两个环节）只允许在“查询起点该仪器**唯一有效溯源链**的
**起点证书**到期时间（``valid_until``）之前结束”。链路唯一性/有效性复用
``engine.get_unique_chain_start``：链不唯一、断裂、起点证书缺失或多重、
唯一链校验失败均视为链不可用，并给出可解释原因。

并发正确性
----------
``confirm_booking`` 在由 ``database.get_write_tx`` 提供的同一写事务
（``BEGIN IMMEDIATE``）内**重查**占用与证书截止后再插入预约：写锁使并发
预约在检查阶段即被串行化，杜绝“各自读到不冲突、随后双双写入”的竞态；
插入或冲突均随该事务一并提交或回滚。
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from .crud import ValidationError, get_instrument
from .engine import get_unique_chain_start
from .schemas import CALIBRATION_STATION_CODE
from .utils import fmt, to_utc_naive, utcnow_str

# 全实验室唯一校准工位
STATION_CODE = CALIBRATION_STATION_CODE

# 链不可用 / 时间窗内无可用时段 的对外原因码
REASON_TRACE_CHAIN_UNAVAILABLE = "TRACE_CHAIN_UNAVAILABLE"
REASON_NO_SLOT_IN_WINDOW = "NO_AVAILABLE_SLOT_IN_WINDOW"
# 确认预约时占用冲突
REASON_SLOT_OCCUPIED = "SLOT_OCCUPIED"


class BookingConflict(Exception):
    """预约确认冲突（路由层映射为 HTTP 409）。"""

    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


# ---------------------------------------------------------------- 时间与占用

def _normalize_window(start: datetime, end: datetime) -> tuple[str, str]:
    """校验并规范化查询窗（半开区间 [start, end)），返回 UTC 整秒文本对。"""
    start_s = fmt(start)
    end_s = fmt(end)
    if start_s >= end_s:
        raise ValidationError(
            f"查询窗起点 {start_s} 必须早于终点 {end_s}"
            "（半开区间 [start, end) 至少包含 1 秒）"
        )
    return start_s, end_s


def _list_busy(
    conn: sqlite3.Connection,
    start_s: str,
    end_s: str,
) -> list[sqlite3.Row]:
    """返回与半开查询窗 [start_s, end_s) 相交的工位占用，按起点升序。"""
    return conn.execute(
        """
        SELECT * FROM calibration_bookings
        WHERE station_code = ?
          AND start_at < ? AND ? < end_at
        ORDER BY start_at, id
        """,
        (STATION_CODE, end_s, start_s),
    ).fetchall()


def _busy_payload(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [
        {
            "booking_id": r["id"],
            "instrument_code": r["instrument_code"],
            "start_at": r["start_at"],
            "end_at": r["end_at"],
        }
        for r in rows
    ]


def _find_earliest_slot(
    busy: list[sqlite3.Row],
    window_start_s: str,
    window_end_s: str,
    duration: timedelta,
) -> Optional[tuple[str, str]]:
    """在半开窗口 [window_start, window_end) 内寻找长度足够的最早空档。

    ``busy`` 为与窗口相交、按起点升序的占用（同样按半开区间）。
    从窗口起点起逐个跳过与之重叠的占用，首个能容纳 ``duration`` 的
    间隙即为最早可用时段；返回 (slot_start, slot_end)，无则返回 None。
    """
    duration_s = int(duration.total_seconds())
    cursor = window_start_s
    for b in busy:
        b_start, b_end = b["start_at"], b["end_at"]
        if b_end <= cursor:
            # 占用完全落在游标之前（被此前占用吸收），跳过
            continue
        if b_start >= cursor:
            # cursor 与该占用起点之间是一个空档
            if _seconds_between(cursor, b_start) >= duration_s:
                return _slot_at(cursor, duration)
        # 空档不够或占用自 cursor 之前即开始：游标推进到占用终点之后
        if b_end > cursor:
            cursor = b_end
    # 全部占用之后到窗口终点的剩余空档
    if _seconds_between(cursor, window_end_s) >= duration_s:
        return _slot_at(cursor, duration)
    return None


def _seconds_between(start_s: str, end_s: str) -> int:
    """两个 UTC 整秒文本之间的秒数（end - start，调用方保证 end >= start）。"""
    fmt_ts = "%Y-%m-%dT%H:%M:%SZ"
    return int(
        (
            datetime.strptime(end_s, fmt_ts) - datetime.strptime(start_s, fmt_ts)
        ).total_seconds()
    )


def _slot_at(start_s: str, duration: timedelta) -> tuple[str, str]:
    fmt_ts = "%Y-%m-%dT%H:%M:%SZ"
    start_dt = datetime.strptime(start_s, fmt_ts)
    end_dt = start_dt + duration
    return start_s, end_dt.strftime(fmt_ts)


def _trace_brief(chain: dict[str, Any]) -> dict[str, Any]:
    """溯源结论 -> 对外 SlotTraceability 结构。"""
    return {
        "at": chain["at"],
        "available": chain["available"],
        "resolution": chain["resolution"],
        "valid": chain["valid"],
        "reason_code": None if chain["available"] else chain["reason_code"],
        "reason": None if chain["available"] else chain["reason"],
        "starts": chain["starts"],
    }


# ---------------------------------------------------------------- 查询最早时段

def find_slots(
    conn: sqlite3.Connection,
    instrument_code: str,
    start: datetime,
    end: datetime,
    duration_minutes: int,
) -> dict[str, Any]:
    """GET /api/calibration-slots 的核心计算（只读）。

    1. 校验半开查询窗与仪器存在性；
    2. 在**查询起点**评估该仪器的唯一有效溯源链，取其起点证书到期时间为
       预约结束截止；链不可用时直接说明原因（不计算工位占用）；
    3. 在 [查询窗起点, min(查询窗终点, 证书到期)) 内按半开区间汇总单个
       工位的占用，返回能容纳所需时长的最早可用时段。
    """
    window_start_s, window_end_s = _normalize_window(start, end)

    if get_instrument(conn, instrument_code) is None:
        raise KeyError(instrument_code)

    base: dict[str, Any] = {
        "station_code": STATION_CODE,
        "instrument": instrument_code,
        "window_start": window_start_s,
        "window_end": window_end_s,
        "duration_minutes": duration_minutes,
        "interval_semantics": "half_open [start_at, end_at)",
        "busy_intervals": [],
    }

    window_start_dt = to_utc_naive(start)
    chain = get_unique_chain_start(conn, instrument_code, window_start_dt)
    base["traceability"] = _trace_brief(chain)

    if not chain["available"]:
        base.update(
            {
                "deadline": None,
                "effective_window_end": None,
                "available": False,
                "reason_code": REASON_TRACE_CHAIN_UNAVAILABLE,
                "reason": (
                    f"仪器 {instrument_code} 在查询起点 "
                    f"{chain['at']} 的溯源链不可用，不能预约校准："
                    f"{chain['reason']}"
                ),
                "slot": None,
            }
        )
        return base

    deadline_s = chain["certificate_valid_until"]
    effective_end_s = min(window_end_s, deadline_s)
    base["deadline"] = {
        "start_certificate_id": chain["start_certificate_id"],
        "start_cert_number": chain["start_cert_number"],
        "certificate_valid_until": deadline_s,
        "chain_certificate_ids": chain["chain_certificate_ids"],
    }
    base["effective_window_end"] = effective_end_s

    if effective_end_s <= window_start_s:
        busy = _list_busy(conn, window_start_s, window_end_s)
        base["busy_intervals"] = _busy_payload(busy)
        base.update(
            {
                "available": False,
                "reason_code": REASON_NO_SLOT_IN_WINDOW,
                "reason": (
                    f"仪器 {instrument_code} 唯一有效链起点证书 "
                    f"{chain['start_certificate_id']} 已于 {deadline_s} 到期"
                    "（不晚于查询窗起点），查询窗内不存在允许预约的时段"
                ),
                "slot": None,
            }
        )
        return base

    busy = _list_busy(conn, window_start_s, effective_end_s)
    base["busy_intervals"] = _busy_payload(busy)
    duration = timedelta(minutes=duration_minutes)
    found = _find_earliest_slot(
        busy, window_start_s, effective_end_s, duration
    )
    if found is None:
        base.update(
            {
                "available": False,
                "reason_code": REASON_NO_SLOT_IN_WINDOW,
                "reason": (
                    f"仪器 {instrument_code} 在时间窗 "
                    f"[{window_start_s}, {effective_end_s}) 内不存在长度 "
                    f"{duration_minutes} 分钟的空闲工位时段"
                    "（已计入起点证书到期约束与工位占用）"
                ),
                "slot": None,
            }
        )
    else:
        slot_start_s, slot_end_s = found
        base.update(
            {
                "available": True,
                "reason_code": None,
                "reason": None,
                "slot": {
                    "start_at": slot_start_s,
                    "end_at": slot_end_s,
                    "duration_minutes": duration_minutes,
                },
            }
        )
    return base


# ---------------------------------------------------------------- 事务内确认

def _new_booking_id(conn: sqlite3.Connection) -> str:
    """生成 BK-XXXXXXXXXXXX 形式的预约 id（极小概率碰撞时重试）。"""
    for _ in range(5):
        candidate = "BK-" + uuid.uuid4().hex[:12].upper()
        row = conn.execute(
            "SELECT 1 FROM calibration_bookings WHERE id = ?", (candidate,)
        ).fetchone()
        if row is None:
            return candidate
    raise RuntimeError("无法生成唯一预约 id，请重试")


def confirm_booking(
    conn: sqlite3.Connection,
    instrument_code: str,
    start_at: datetime,
    duration_minutes: int,
    note: str = "",
) -> dict[str, Any]:
    """在调用方提供的同一写事务内重查占用与证书截止并插入预约。

    ``conn`` 必须由 ``database.get_write_tx()`` 提供（``BEGIN IMMEDIATE``）。
    流程全部在该事务内完成：

    1. 校验仪器存在、时长为正；
    2. 在预约起始时点重查唯一有效溯源链及其起点证书到期时间；
       链不可用 -> BookingConflict（409）；
    3. 计算结束时刻，要求 end_at <= 证书 valid_until，否则 409；
    4. 重查半开区间占用重叠，存在重叠 -> 409（附冲突预约）；
    5. 插入预约记录并返回落库行。
    """
    if not instrument_code or not instrument_code.strip():
        raise ValidationError("仪器编码不能为空字符串")
    if duration_minutes <= 0:
        raise ValidationError("所需时长（分钟）必须为正整数")

    if get_instrument(conn, instrument_code) is None:
        raise KeyError(instrument_code)

    start_naive = to_utc_naive(start_at)
    start_s = fmt(start_naive)
    end_s = (start_naive + timedelta(minutes=duration_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    # 1) 事务内重查溯源链与截止时间
    chain = get_unique_chain_start(conn, instrument_code, start_naive)
    if not chain["available"]:
        raise BookingConflict(
            REASON_TRACE_CHAIN_UNAVAILABLE,
            f"仪器 {instrument_code} 在预约起始时刻 {chain['at']} 的溯源链"
            f"不可用，预约被拒绝：{chain['reason']}",
            detail={
                "instrument_code": instrument_code,
                "start_at": start_s,
                "end_at": end_s,
                "resolution": chain["resolution"],
                "valid": chain["valid"],
                "starts": chain["starts"],
                "reason_code": chain["reason_code"],
                "conflicts": chain["conflicts"],
                "error_count": chain["error_count"],
                "warning_count": chain["warning_count"],
            },
        )

    deadline_s = chain["certificate_valid_until"]
    if end_s > deadline_s:
        raise BookingConflict(
            REASON_TRACE_CHAIN_UNAVAILABLE,
            f"预约结束时刻 {end_s} 晚于仪器 {instrument_code} 唯一有效溯源链"
            f"起点证书 {chain['start_certificate_id']} 的到期时间 "
            f"{deadline_s}，预约必须在证书到期前结束",
            detail={
                "instrument_code": instrument_code,
                "start_at": start_s,
                "end_at": end_s,
                "start_certificate_id": chain["start_certificate_id"],
                "certificate_valid_until": deadline_s,
                "chain_certificate_ids": chain["chain_certificate_ids"],
            },
        )

    # 2) 事务内重查工位占用（半开区间重叠判定）
    clash = conn.execute(
        """
        SELECT * FROM calibration_bookings
        WHERE station_code = ?
          AND start_at < ? AND ? < end_at
        ORDER BY start_at, id
        """,
        (STATION_CODE, end_s, start_s),
    ).fetchall()
    if clash:
        raise BookingConflict(
            REASON_SLOT_OCCUPIED,
            f"校准工位在 [{start_s}, {end_s}) 已被 {len(clash)} 个预约占用"
            "（半开区间重叠；首尾相接不冲突），预约被拒绝",
            detail={
                "station_code": STATION_CODE,
                "instrument_code": instrument_code,
                "start_at": start_s,
                "end_at": end_s,
                "conflicting_bookings": [
                    {
                        "booking_id": r["id"],
                        "instrument_code": r["instrument_code"],
                        "start_at": r["start_at"],
                        "end_at": r["end_at"],
                    }
                    for r in clash
                ],
            },
        )

    # 3) 插入（与上述检查同一事务提交/回滚）
    booking_id = _new_booking_id(conn)
    created_at = utcnow_str()
    conn.execute(
        """
        INSERT INTO calibration_bookings(
            id, station_code, instrument_code, start_at, end_at,
            duration_minutes, start_certificate_id, certificate_valid_until,
            note, created_at)
        VALUES (:id, :station_code, :instrument_code, :start_at, :end_at,
                :duration_minutes, :start_certificate_id,
                :certificate_valid_until, :note, :created_at)
        """,
        {
            "id": booking_id,
            "station_code": STATION_CODE,
            "instrument_code": instrument_code,
            "start_at": start_s,
            "end_at": end_s,
            "duration_minutes": duration_minutes,
            "start_certificate_id": chain["start_certificate_id"],
            "certificate_valid_until": deadline_s,
            "note": note,
            "created_at": created_at,
        },
    )
    row = conn.execute(
        "SELECT * FROM calibration_bookings WHERE id = ?", (booking_id,)
    ).fetchone()
    return _booking_row(row)


def _booking_row(r: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": r["id"],
        "station_code": r["station_code"],
        "instrument_code": r["instrument_code"],
        "start_at": r["start_at"],
        "end_at": r["end_at"],
        "duration_minutes": r["duration_minutes"],
        "start_certificate_id": r["start_certificate_id"],
        "certificate_valid_until": r["certificate_valid_until"],
        "note": r["note"],
        "created_at": r["created_at"],
    }


def list_bookings(
    conn: sqlite3.Connection,
    instrument_code: Optional[str] = None,
) -> list[dict[str, Any]]:
    """列出预约（可按仪器过滤），按起始时刻升序。"""
    if instrument_code is not None:
        rows = conn.execute(
            """
            SELECT * FROM calibration_bookings
            WHERE instrument_code = ?
            ORDER BY start_at, id
            """,
            (instrument_code,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calibration_bookings ORDER BY start_at, id"
        ).fetchall()
    return [_booking_row(r) for r in rows]
