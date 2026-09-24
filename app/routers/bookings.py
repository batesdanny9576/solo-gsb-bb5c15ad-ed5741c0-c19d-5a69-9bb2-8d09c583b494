"""校准工位预约路由：可预约时段查询与预约确认。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlite3 import Connection

from .. import booking, crud
from ..database import get_db, get_write_tx
from ..schemas import (
    CalibrationBookingIn,
    CalibrationBookingOut,
    CalibrationSlotsResult,
)

router = APIRouter(prefix="/api", tags=["calibration-bookings"])


@router.get(
    "/calibration-slots",
    response_model=CalibrationSlotsResult,
    summary="查询校准工位最早可预约时段",
)
def calibration_slots(
    instrument: str = Query(
        ..., description="待校准仪器编码", min_length=1
    ),
    start: datetime = Query(
        ...,
        alias="start",
        description="查询窗起点（UTC，含），如 2026-06-01T00:00:00Z",
    ),
    end: datetime = Query(
        ...,
        alias="end",
        description="查询窗终点（UTC，不含；半开区间），如 2026-06-02T00:00:00Z",
    ),
    duration_minutes: int = Query(
        ...,
        alias="duration_minutes",
        gt=0,
        description="所需时长（正整数分钟）",
    ),
    db: Connection = Depends(get_db),
):
    """在给定 UTC 半开时间窗 ``[start, end)`` 内返回单个校准工位的最早可用时段。

    计算步骤：

    1. 汇总时间窗内该工位的全部预约占用（占用按半开区间
       ``[start_at, end_at)`` 判定，首尾相接不冲突）；
    2. 在**查询窗起点**评估该仪器的唯一有效溯源链：链不唯一、断裂、起点
       证书缺失/多重或唯一链校验失败时，``available=false`` 并在
       ``traceability`` 中给出原因码与中文说明；
    3. 链可用时取其**起点证书到期时间**（valid_until）为预约结束截止，
       实际搜索上限为 ``min(end, 到期时间)``；在该上限内寻找能容纳所需
       时长的最早空档并返回 ``slot``，无空档时说明原因。
    """
    try:
        return booking.find_slots(
            db, instrument, start, end, duration_minutes
        )
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))


@router.post(
    "/calibration-bookings",
    response_model=CalibrationBookingOut,
    status_code=201,
    summary="确认校准工位预约（同一写事务内重查占用与证书截止）",
)
def create_calibration_booking(body: CalibrationBookingIn):
    """确认预约：在**同一写事务**（``BEGIN IMMEDIATE``）内重新评估溯源链
    与起点证书截止时间、重新检查工位半开区间占用，全部通过后才插入预约，
    检查与插入随该事务原子提交/回滚。

    冲突（溯源链不可用 / 预约结束晚于起点证书到期 / 时段已被占用）返回
    **409**，响应体含机器码 ``code``、中文 ``message`` 与结构化 ``detail``
    （占用冲突时附 ``conflicting_bookings``）；仪器不存在返回 **404**；
    入参非法（空仪器编码、非正时长）返回 **422**。
    """
    with get_write_tx() as conn:
        try:
            return booking.confirm_booking(
                conn,
                body.instrument_code,
                body.start_at,
                body.duration_minutes,
                body.note,
            )
        except booking.BookingConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                },
            )
        except KeyError:
            raise HTTPException(404, f"仪器不存在: {body.instrument_code}")
        except crud.ValidationError as exc:
            raise HTTPException(422, str(exc))


@router.get(
    "/calibration-bookings",
    response_model=list[CalibrationBookingOut],
    summary="查询预约（可按仪器过滤）",
)
def list_calibration_bookings(
    instrument: Optional[str] = Query(
        None, description="仅返回该仪器编码的预约；留空返回全部"
    ),
    db: Connection = Depends(get_db),
):
    """列出持久化在 SQLite 中的校准工位预约，按起始时刻升序。"""
    return booking.list_bookings(db, instrument)
