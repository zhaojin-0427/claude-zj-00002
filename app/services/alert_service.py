"""告警服务：创建/去重、状态机流转、离线扫描与自动恢复。"""
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, TYPE_CHECKING

from sqlalchemy.orm import Session

from ..constants import (ANOMALY_LABELS, AlertStatus, AnomalyType, TRANSITIONS)
from ..exceptions import ApiException
from ..models import Alert, Device
from ..utils import utcnow

if TYPE_CHECKING:
    from .policy_service import EffectivePolicy


def _gen_alert_no() -> str:
    return f"A{utcnow():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def raise_alert(db: Session, device: Device, anomaly_type: str,
                detail: str, detected_at: datetime,
                policy: "Optional[EffectivePolicy]" = None) -> Tuple[Alert, bool]:
    """
    触发告警。同一电表同一异常类型存在未闭环告警时，仅刷新最近检出时间（去重），
    返回 (alert, 是否新建)。

    新建告警时冻结实际命中的策略版本与完整参数快照；已存在的告警保留首次命中的
    快照（历史告警不受后续策略变更影响），不会被新策略覆盖。
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
    if policy is not None:
        snapshot = policy.snapshot_for(anomaly_type, detected_at)
        alert.strategy_version_id = policy.version_id
        alert.strategy_scope = policy.scope
        alert.strategy_snapshot_json = json.dumps(snapshot, ensure_ascii=False)
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
    else:
        alert.resolved_at = None  # 重新打开（如已整改->核查中），清除原解决时间
    if note:
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        alert.note = (alert.note + "\n" if alert.note else "") + f"[{stamp}] {note}"
    db.commit()
    db.refresh(alert)
    return alert


def scan_offline_devices(db: Session, now: Optional[datetime] = None) -> List[Alert]:
    """扫描超过策略阈值未上报的设备，生成设备离线告警。返回新建告警。

    离线阈值与启停按分层策略逐设备解析（电表＞房间＞楼栋＞全局），
    设备在其离线策略的生效时段外则不判定离线。
    """
    from . import policy_service

    now = now or utcnow()
    devices = db.query(Device).filter(Device.last_seen_at.isnot(None)).all()
    created = []
    for d in devices:
        policy = policy_service.resolve_policy(
            db, d.meter_no, d.room_no, d.building, now)
        rule = policy.rule(AnomalyType.DEVICE_OFFLINE)
        if not rule.get("enabled", True) or not policy.is_effective_at(now):
            continue
        cutoff = now - timedelta(minutes=rule["offline_minutes"])
        if d.last_seen_at >= cutoff:
            continue
        alert, is_new = raise_alert(
            db, d, AnomalyType.DEVICE_OFFLINE,
            detail=f"设备最后上报时间 {d.last_seen_at:%Y-%m-%d %H:%M:%S}，"
                   f"已超过 {rule['offline_minutes']} 分钟未上报"
                   f"（命中{policy.scope}级策略 v{policy.version}）"
                   if not policy.is_default else
                   f"设备最后上报时间 {d.last_seen_at:%Y-%m-%d %H:%M:%S}，"
                   f"已超过 {rule['offline_minutes']} 分钟未上报（系统默认阈值）",
            detected_at=now,
            policy=policy,
        )
        if is_new:
            created.append(alert)
    db.commit()
    return created


def recover_offline_alerts(db: Session, device: Device, at: datetime) -> None:
    """设备重新上报时，自动将其未闭环的离线告警置为已恢复。

    仅应由"新鲜"读数（reported_at 在离线窗口内）触发；陈旧补报不会调用本函数。
    resolved_at 不早于 first_detected_at，避免出现"解决早于检出"。
    """
    alerts = (db.query(Alert)
              .filter(Alert.meter_no == device.meter_no,
                      Alert.anomaly_type == AnomalyType.DEVICE_OFFLINE,
                      Alert.status.in_(AlertStatus.OPEN))
              .all())
    for a in alerts:
        resolved_at = max(at, a.first_detected_at)
        a.status = AlertStatus.RECOVERED
        a.resolved_at = resolved_at
        # 系统自动确认恢复：响应时长 = 检出到恢复的耗时，不计入"未响应"
        a.acknowledged_at = resolved_at
        a.response_seconds = max(0.0, (resolved_at - a.first_detected_at).total_seconds())
        a.note = (a.note + "\n" if a.note else "") + \
                 f"[{resolved_at:%Y-%m-%d %H:%M:%S}] 设备恢复上报，系统自动置为已恢复"


def retract_alerts_caused_by_reading(db: Session, reading) -> List[Alert]:
    """读数被判定为离群点后，撤销由它触发的未闭环告警（置为误报）。"""
    now = utcnow()
    alerts = (db.query(Alert)
              .filter(Alert.meter_no == reading.meter_no,
                      Alert.first_detected_at == reading.reported_at,
                      Alert.anomaly_type != AnomalyType.DEVICE_OFFLINE,
                      Alert.status.in_(AlertStatus.OPEN))
              .all())
    for a in alerts:
        a.status = AlertStatus.FALSE_POSITIVE
        a.resolved_at = now
        a.note = (a.note + "\n" if a.note else "") + \
                 f"[{now:%Y-%m-%d %H:%M:%S}] 触发读数被判定为短时离群点，告警自动撤销"
    return alerts
