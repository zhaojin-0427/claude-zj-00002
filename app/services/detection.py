"""异常检测引擎：每条新读数（含乱序补报）到达时触发。

处理流程：
1. 去噪评估 —— 对所有"已具备前后邻点且尚未评估"的读数做离群点判定：两侧邻点均处于
   正常区间而中间点为大幅尖峰，则标记为离群点，后续检测忽略该点。被判定为离群点的
   读数，其此前触发的未闭环告警自动撤销（置为误报）。
2. 功率跳变评估 —— 对所有"已存在后继读数且尚未评估"的读数，与最近的前序有效读数
   比较功率差。延迟评估保证单点毛刺先被去噪；乱序补报到达后，历史缺口会被补充评估。
3. 点检测（持续高负荷 / 夜间异常活跃 / 疑似窃电）—— 对当前读数及其后的所有读数
   （补报场景下序列可能发生变化）按时间序执行；告警按（电表, 异常类型）在未闭环
   期间去重，重复评估不会产生重复告警。

时间约定：所有 reported_at 统一存储为 naive UTC；夜间时段按本地时间判定，
本地时间 = UTC + LOCAL_UTC_OFFSET_HOURS（默认 8，即北京时间）。
"""
from datetime import datetime, timedelta
from typing import List

from sqlalchemy.orm import Session

from ..config import settings
from ..constants import AnomalyType
from ..models import Alert, Device, MeterReading
from .alert_service import raise_alert, retract_alerts_caused_by_reading


def _ordered_readings(db: Session, meter_no: str) -> List[MeterReading]:
    return (db.query(MeterReading)
            .filter(MeterReading.meter_no == meter_no)
            .order_by(MeterReading.reported_at.asc(), MeterReading.id.asc())
            .all())


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


def _local_hour(ts: datetime) -> int:
    return (ts + timedelta(hours=settings.LOCAL_UTC_OFFSET_HOURS)).hour


def _is_night(ts: datetime) -> bool:
    h = _local_hour(ts)
    return h >= settings.NIGHT_START_HOUR or h < settings.NIGHT_END_HOUR


def evaluate_pending_denoise(db: Session, meter_no: str) -> None:
    """对所有具备前后邻点且未评估的读数做离群点判定（兼容乱序补报）。"""
    rows = _ordered_readings(db, meter_no)
    for i, r in enumerate(rows):
        if r.denoise_evaluated or i == 0 or i == len(rows) - 1:
            continue
        before, after = rows[i - 1], rows[i + 1]
        r.denoise_evaluated = True
        neighbor_avg = (before.power + after.power) / 2
        spike_floor = max(settings.OUTLIER_MIN_SPIKE_KW,
                          settings.OUTLIER_FACTOR * max(neighbor_avg, 0.01))
        if neighbor_avg <= settings.OUTLIER_NEIGHBOR_MAX_KW and r.power >= spike_floor:
            r.is_outlier = True
            # 该读数此前（尚未被识别为离群点时）触发的未闭环告警自动撤销
            retract_alerts_caused_by_reading(db, r)


def evaluate_pending_jump(db: Session, device: Device, meter_no: str) -> List[Alert]:
    """对所有已存在后继读数且未评估的读数做功率跳变判定（兼容乱序补报）。"""
    rows = _ordered_readings(db, meter_no)
    triggered: List[Alert] = []
    for i, r in enumerate(rows):
        if r.jump_evaluated or i == len(rows) - 1:
            continue
        # 最近的前序有效读数（跳过离群点）
        j = i - 1
        while j >= 0 and rows[j].is_outlier:
            j -= 1
        if j < 0:
            continue  # 前序读数可能尚未补报，留待下次评估
        r.jump_evaluated = True
        if r.is_outlier:
            continue
        before = rows[j]
        delta = abs(r.power - before.power)
        if delta >= settings.POWER_JUMP_THRESHOLD_KW:
            alert, created = raise_alert(
                db, device, AnomalyType.POWER_JUMP,
                detail=f"功率由 {before.power}kW 跳变至 {r.power}kW"
                       f"（变化 {delta:.2f}kW，阈值 {settings.POWER_JUMP_THRESHOLD_KW}kW）",
                detected_at=r.reported_at,
            )
            if created:
                triggered.append(alert)
    return triggered


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
        detail=f"夜间时段（{settings.NIGHT_START_HOUR}:00-次日{settings.NIGHT_END_HOUR}:00，"
               f"本地时间）连续 {n} 次功率超过 {settings.NIGHT_POWER_THRESHOLD_KW}kW，"
               f"当前 {current.power}kW",
        detected_at=current.reported_at,
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
    evaluate_pending_denoise(db, current.meter_no)
    triggered = evaluate_pending_jump(db, device, current.meter_no)
    # 点检测：当前读数及其后的所有读数（乱序补报会改变后续读数的上下文，需重估；
    # 告警按未闭环去重，重复评估无副作用）
    later = (db.query(MeterReading)
             .filter(MeterReading.meter_no == current.meter_no,
                     MeterReading.reported_at >= current.reported_at)
             .order_by(MeterReading.reported_at.asc())
             .all())
    for r in later:
        triggered += check_sustained_high_load(db, device, r)
        triggered += check_night_active(db, device, r)
        triggered += check_suspected_theft(db, device, r)
    return triggered
