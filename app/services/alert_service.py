"""告警服务：创建/去重、状态机流转、离线扫描与自动恢复。"""
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..constants import (ANOMALY_LABELS, AlertStatus, AnomalyType, TRANSITIONS)
from ..exceptions import ApiException
from ..models import Alert, Device
from ..utils import utcnow


def _gen_alert_no() -> str:
    return f"A{utcnow():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def raise_alert(db: Session, device: Device, anomaly_type: str,
                detail: str, detected_at: datetime) -> Tuple[Alert, bool]:
    """
    触发告警。同一电表同一异常类型存在未闭环告警时，仅刷新最近检出时间（去重），
    返回 (alert, 是否新建)。
    """
    existing = (db.query(Alert)
                .filter(Alert.meter_no == device.meter_no,
                        Alert.anomaly_type == anomaly_type,
                        Alert.status.in_(AlertStatus.OPEN))
                .first())
    if existing:
        if detected_at > existing.last_detected_at:
            existing.last_detected_at = detected_at
        existing.detail = detail
        return existing, False

    alert = Alert(
        alert_no=_gen_alert_no(),
        meter_no=device.meter_no,
        room_no=device.room_no,
        building=device.building,
        anomaly_type=anomaly_type,
        status=AlertStatus.PENDING,
        title=ANOMALY_LABELS.get(anomaly_type, anomaly_type),
        detail=detail,
        first_detected_at=detected_at,
        last_detected_at=detected_at,
    )
    db.add(alert)
    db.flush()
    return alert, True


def transition_alert(db: Session, alert: Alert, target: str,
                     note: Optional[str] = None) -> Alert:
    """按状态机流转告警状态，记录响应/处置时间。"""
    allowed = TRANSITIONS.get(alert.status, set())
    if target not in allowed:
        raise ApiException(
            40001,
            f"非法状态流转: {alert.status} -> {target}，当前可流转至: {sorted(allowed) or '无（终态）'}",
        )
    now = utcnow()
    if alert.status == AlertStatus.PENDING:
        # 首次离开待确认，记录响应时长
        alert.acknowledged_at = now
        alert.response_seconds = max(0.0, (now - alert.first_detected_at).total_seconds())
    alert.status = target
    if target in AlertStatus.TERMINAL:
        alert.resolved_at = now
    if note:
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        alert.note = (alert.note + "\n" if alert.note else "") + f"[{stamp}] {note}"
    db.commit()
    db.refresh(alert)
    return alert


def scan_offline_devices(db: Session, now: Optional[datetime] = None) -> List[Alert]:
    """扫描超过 OFFLINE_MINUTES 未上报的设备，生成设备离线告警。返回新建告警。"""
    now = now or utcnow()
    cutoff = now - timedelta(minutes=settings.OFFLINE_MINUTES)
    devices = db.query(Device).filter(Device.last_seen_at.isnot(None),
                                      Device.last_seen_at < cutoff).all()
    created = []
    for d in devices:
        _, is_new = raise_alert(
            db, d, AnomalyType.DEVICE_OFFLINE,
            detail=f"设备最后上报时间 {d.last_seen_at:%Y-%m-%d %H:%M:%S}，"
                   f"已超过 {settings.OFFLINE_MINUTES} 分钟未上报",
            detected_at=now,
        )
        if is_new:
            created.append(_)
    db.commit()
    return created


def recover_offline_alerts(db: Session, device: Device, at: datetime) -> None:
    """设备重新上报时，自动将其未闭环的离线告警置为已恢复。"""
    alerts = (db.query(Alert)
              .filter(Alert.meter_no == device.meter_no,
                      Alert.anomaly_type == AnomalyType.DEVICE_OFFLINE,
                      Alert.status.in_(AlertStatus.OPEN))
              .all())
    for a in alerts:
        a.status = AlertStatus.RECOVERED
        a.resolved_at = at
        a.note = (a.note + "\n" if a.note else "") + \
                 f"[{at:%Y-%m-%d %H:%M:%S}] 设备恢复上报，系统自动置为已恢复"
