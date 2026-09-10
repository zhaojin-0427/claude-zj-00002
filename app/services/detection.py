"""异常检测引擎：每条新读数到达时触发。

检测规则：
1. 短时离群值去噪 —— 新读数到达后回溯评估上一条读数：若其两侧邻点均处于正常区间，
   而中间点为大幅尖峰，则标记为离群点，后续所有检测忽略该点（单点毛刺不告警）。
2. 持续高负荷 —— 连续 N 条有效读数功率 >= 阈值。
3. 夜间异常活跃 —— 夜间时段连续 M 条有效读数功率 >= 夜间阈值。
4. 功率跳变 —— 延迟一个采样点评估：上一条读数（经过去噪判定）相对再前一条的功率差
   >= 阈值。延迟评估保证单点毛刺先被去噪，不会误报跳变。
5. 疑似窃电 —— 累计读数回退，或读数增量与功率积分电量严重不符。
"""
from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from ..config import settings
from ..constants import AnomalyType
from ..models import Alert, Device, MeterReading
from .alert_service import raise_alert


def _recent_valid(db: Session, meter_no: str, end_at: datetime,
                  limit: int, include_end: bool = True) -> List[MeterReading]:
    """取 end_at 之前（含）的最近 limit 条非离群读数，按时间倒序。"""
    q = db.query(MeterReading).filter(
        MeterReading.meter_no == meter_no,
        MeterReading.is_outlier.is_(False),
    )
    if include_end:
        q = q.filter(MeterReading.reported_at <= end_at)
    else:
        q = q.filter(MeterReading.reported_at < end_at)
    return q.order_by(MeterReading.reported_at.desc()).limit(limit).all()


def _previous_two(db: Session, current: MeterReading) -> List[MeterReading]:
    return (db.query(MeterReading)
            .filter(MeterReading.meter_no == current.meter_no,
                    MeterReading.reported_at < current.reported_at)
            .order_by(MeterReading.reported_at.desc())
            .limit(2).all())


def _is_night(ts: datetime) -> bool:
    h = ts.hour
    return h >= settings.NIGHT_START_HOUR or h < settings.NIGHT_END_HOUR


def denoise_previous(db: Session, current: MeterReading) -> None:
    """去噪：若上一条读数是单点尖峰（两侧邻点正常），标记为离群点。"""
    prev_two = _previous_two(db, current)
    if len(prev_two) < 2:
        return
    middle, before = prev_two[0], prev_two[1]
    if middle.is_outlier:
        return
    neighbor_avg = (before.power + current.power) / 2
    spike_floor = max(settings.OUTLIER_MIN_SPIKE_KW,
                      settings.OUTLIER_FACTOR * max(neighbor_avg, 0.01))
    if neighbor_avg <= settings.OUTLIER_NEIGHBOR_MAX_KW and middle.power >= spike_floor:
        middle.is_outlier = True


def check_sustained_high_load(db: Session, device: Device,
                              current: MeterReading) -> List[Alert]:
    if current.is_outlier or current.power < settings.HIGH_POWER_THRESHOLD_KW:
        return []
    n = settings.HIGH_POWER_CONSECUTIVE
    recents = _recent_valid(db, current.meter_no, current.reported_at, n)
    if len(recents) < n:
        return []
    if not all(r.power >= settings.HIGH_POWER_THRESHOLD_KW for r in recents):
        return []
    alert, created = raise_alert(
        db, device, AnomalyType.SUSTAINED_HIGH_LOAD,
        detail=f"连续 {n} 次上报功率超过 {settings.HIGH_POWER_THRESHOLD_KW}kW"
               f"（{recents[-1].reported_at:%m-%d %H:%M} ~ {current.reported_at:%m-%d %H:%M}，"
               f"当前 {current.power}kW）",
        detected_at=current.reported_at,
    )
    return [alert] if created else []


def check_night_active(db: Session, device: Device,
                       current: MeterReading) -> List[Alert]:
    if current.is_outlier or not _is_night(current.reported_at):
        return []
    if current.power < settings.NIGHT_POWER_THRESHOLD_KW:
        return []
    n = settings.NIGHT_CONSECUTIVE
    recents = _recent_valid(db, current.meter_no, current.reported_at, n)
    if len(recents) < n:
        return []
    if not all(_is_night(r.reported_at) and r.power >= settings.NIGHT_POWER_THRESHOLD_KW
               for r in recents):
        return []
    alert, created = raise_alert(
        db, device, AnomalyType.NIGHT_ACTIVE,
        detail=f"夜间时段（{settings.NIGHT_START_HOUR}:00-次日{settings.NIGHT_END_HOUR}:00）"
               f"连续 {n} 次功率超过 {settings.NIGHT_POWER_THRESHOLD_KW}kW，"
               f"当前 {current.power}kW",
        detected_at=current.reported_at,
    )
    return [alert] if created else []


def check_power_jump(db: Session, device: Device,
                     current: MeterReading) -> List[Alert]:
    """延迟评估上一条读数的功率跳变（此时它已完成去噪判定）。"""
    prev_two = _previous_two(db, current)
    if len(prev_two) < 2:
        return []
    prev, before = prev_two[0], prev_two[1]
    if prev.jump_evaluated:
        return []
    prev.jump_evaluated = True
    if prev.is_outlier or before.is_outlier:
        return []
    delta = abs(prev.power - before.power)
    if delta < settings.POWER_JUMP_THRESHOLD_KW:
        return []
    alert, created = raise_alert(
        db, device, AnomalyType.POWER_JUMP,
        detail=f"功率由 {before.power}kW 跳变至 {prev.power}kW"
               f"（变化 {delta:.2f}kW，阈值 {settings.POWER_JUMP_THRESHOLD_KW}kW）",
        detected_at=prev.reported_at,
    )
    return [alert] if created else []


def check_suspected_theft(db: Session, device: Device,
                          current: MeterReading) -> List[Alert]:
    if current.is_outlier:
        return []
    prevs = _recent_valid(db, current.meter_no, current.reported_at, 1, include_end=False)
    if not prevs:
        return []
    prev = prevs[0]

    # 规则1：累计读数回退（表计被篡改的典型特征）
    if current.reading < prev.reading - 1e-6:
        alert, created = raise_alert(
            db, device, AnomalyType.SUSPECTED_THEFT,
            detail=f"累计读数回退：{prev.reading}kWh -> {current.reading}kWh，疑似表计被篡改",
            detected_at=current.reported_at,
        )
        return [alert] if created else []

    # 规则2：读数增量与功率积分电量严重不符（实际用电远大于计量）
    hours = (current.reported_at - prev.reported_at).total_seconds() / 3600
    if hours <= 0 or hours > 3:
        return []
    expected_kwh = (prev.power + current.power) / 2 * hours
    actual_kwh = current.reading - prev.reading
    if expected_kwh >= settings.THEFT_MIN_EXPECTED_KWH and \
            actual_kwh < expected_kwh * (1 - settings.THEFT_DROP_TOLERANCE):
        alert, created = raise_alert(
            db, device, AnomalyType.SUSPECTED_THEFT,
            detail=f"计量异常：按功率估算电量约 {expected_kwh:.2f}kWh，"
                   f"表计增量仅 {actual_kwh:.2f}kWh，疑似窃电",
            detected_at=current.reported_at,
        )
        return [alert] if created else []
    return []


def process_reading(db: Session, device: Device, current: MeterReading) -> List[Alert]:
    """对一条新读数执行全部检测，返回本次新建的告警。"""
    denoise_previous(db, current)
    triggered: List[Alert] = []
    triggered += check_sustained_high_load(db, device, current)
    triggered += check_night_active(db, device, current)
    triggered += check_power_jump(db, device, current)
    triggered += check_suspected_theft(db, device, current)
    return triggered
