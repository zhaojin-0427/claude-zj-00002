"""策略只读试跑：在完全隔离的内存库中复用现有去噪与检测管线回放历史数据。

约束：
- 不向正式库写入任何数据、不产生正式告警、不改变设备状态（last_seen_at 等）；
- 数据来源是该电表已入库的历史读数（按请求时间范围过滤），不接收外部数据；
- 试跑期间按每条读数当时的策略版本（当前已发布版本）解析分层策略，
  返回预计异常、命中策略与判定依据。
"""
import json
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..constants import AnomalyType
from ..database import Base
from ..models import (Alert, Device, DetectionStrategy, DetectionStrategyVersion,
                      MeterReading)
from ..exceptions import ApiException
from ..utils import to_naive_utc
from . import detection, policy_service
from .alert_service import raise_alert


def _predicted_status(alert: Alert) -> str:
    """试跑结束时告警的预计状态：与生产一致，被去噪撤销的为误报，其余待确认。"""
    return alert.status


def _alert_brief(alert: Alert) -> dict:
    snapshot = json.loads(alert.strategy_snapshot_json) if alert.strategy_snapshot_json else None
    rule = snapshot["rule"] if snapshot else None
    return {
        "alert_no": alert.alert_no,
        "anomaly_type": alert.anomaly_type,
        "status": _predicted_status(alert),
        "title": alert.title,
        "detail": alert.detail,
        "meter_no": alert.meter_no,
        "room_no": alert.room_no,
        "building": alert.building,
        "detected_at": alert.first_detected_at.isoformat(),
        "hit_strategy": {
            "scope": snapshot["scope"] if snapshot else "global",
            "is_default": snapshot["is_default"] if snapshot else True,
            "strategy_id": snapshot.get("strategy_id") if snapshot else None,
            "strategy_version_id": snapshot.get("strategy_version_id") if snapshot else None,
            "version": snapshot.get("version") if snapshot else None,
            "strategy_name": snapshot.get("strategy_name") if snapshot else "系统默认策略",
            "scope_target": snapshot.get("scope_target", {}) if snapshot else {},
            "in_effective_period": snapshot.get("in_effective_period") if snapshot else True,
            "rule": rule,
        } if snapshot else None,
        "strategy_snapshot": snapshot,
        "evidence": alert.detail,
    }


def trial_run(db: Session, meter_no: str, start: datetime,
              end: datetime) -> dict:
    start = to_naive_utc(start)
    end = to_naive_utc(end)
    if start > end:
        raise ApiException(40000, "start 不能晚于 end")

    device = db.query(Device).filter(Device.meter_no == meter_no).first()
    if device is None:
        raise ApiException(40401, f"电表 {meter_no} 不存在", http_status=404)

    source_readings = (db.query(MeterReading)
                       .filter(MeterReading.meter_no == meter_no)
                       .order_by(MeterReading.reported_at.asc())
                       .all())
    in_range = [r for r in source_readings if start <= r.reported_at <= end]
    if not in_range:
        raise ApiException(40401, "指定时间范围内该电表没有历史读数", http_status=404)

    # ---- 隔离的内存沙箱：与正式库完全隔离 ----
    sandbox_engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=sandbox_engine)
    SandboxSession = sessionmaker(bind=sandbox_engine, autoflush=False,
                                  expire_on_commit=False)
    sb = SandboxSession()
    try:
        sb.add(Device(
            id=device.id, meter_no=device.meter_no, room_no=device.room_no,
            building=device.building, status=device.status,
            last_seen_at=device.last_seen_at, created_at=device.created_at,
        ))
        # 按真实到达顺序（时间序）逐条回放；评估标记全部重置，去噪/检测重新执行
        for r in source_readings:
            sb.add(MeterReading(
                meter_no=r.meter_no, room_no=r.room_no, building=r.building,
                reading=r.reading, power=r.power, device_status=r.device_status,
                reported_at=r.reported_at, received_at=r.received_at,
            ))
        sb.flush()

        # 复制当前全部已发布策略版本（检测只认已发布版本）
        for s in db.query(DetectionStrategy).all():
            sb.add(DetectionStrategy(
                id=s.id, name=s.name, scope=s.scope, building=s.building,
                room_no=s.room_no, meter_no=s.meter_no, rules_json=s.rules_json,
                periods_json=s.periods_json, enabled=s.enabled,
                current_version_id=s.current_version_id,
                published_version=s.published_version,
                created_at=s.created_at, updated_at=s.updated_at,
            ))
        for v in db.query(DetectionStrategyVersion).all():
            sb.add(DetectionStrategyVersion(
                id=v.id, strategy_id=v.strategy_id, version=v.version,
                name=v.name, scope=v.scope, building=v.building,
                room_no=v.room_no, meter_no=v.meter_no,
                rules_json=v.rules_json, periods_json=v.periods_json,
                published_by=v.published_by, published_at=v.published_at,
            ))
        sb.flush()

        sb_device = sb.query(Device).filter(Device.meter_no == meter_no).first()
        rows = (sb.query(MeterReading)
                .filter(MeterReading.meter_no == meter_no)
                .order_by(MeterReading.reported_at.asc(), MeterReading.id.asc())
                .all())

        # 逐条回放，完整复用生产检测管线（含乱序补评估/去噪撤销逻辑）
        for i in range(len(rows)):
            sub = rows[:i + 1]
            current = sub[-1]
            detection.process_reading(sb, sb_device, current)

        # 设备离线：以 end 时刻为"现在"模拟一次离线判定（只读，不改动设备状态）
        offline_hits = _evaluate_offline_at(sb, sb_device, end)

        sb_alerts = (sb.query(Alert)
                     .filter(Alert.meter_no == meter_no).all())
        outliers = (sb.query(MeterReading)
                    .filter(MeterReading.meter_no == meter_no,
                            MeterReading.is_outlier.is_(True)).count())

        # 仅汇报落在请求时间范围内的检出
        predicted = [a for a in sb_alerts
                     if start <= a.first_detected_at <= end]
        for a in offline_hits:
            if start <= a.first_detected_at <= end and \
                    not any(x.anomaly_type == AnomalyType.DEVICE_OFFLINE
                            for x in predicted):
                predicted.append(a)
        predicted.sort(key=lambda a: a.first_detected_at)

        return {
            "meter_no": meter_no,
            "room_no": device.room_no,
            "building": device.building,
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "readings_in_range": len(in_range),
            "readings_replayed": len(rows),
            "outliers_marked": outliers,
            "evaluated_at": end.isoformat(),
            "predicted_anomalies": [_alert_brief(a) for a in predicted],
            "anomaly_count": len([a for a in predicted
                                  if a.status != "false_positive"]),
            "note": "试跑结果为只读模拟，未写入正式告警，未改变任何设备状态",
        }
    finally:
        sb.close()
        sandbox_engine.dispose()


def _evaluate_offline_at(sb: Session, device: Device,
                         at: datetime) -> List[Alert]:
    """在沙箱中按 at 时刻的策略判定设备离线（不提交、不改设备）。"""
    policy = policy_service.resolve_policy(
        sb, device.meter_no, device.room_no, device.building, at)
    rule = policy.rule(AnomalyType.DEVICE_OFFLINE)
    if not rule.get("enabled", True) or not policy.is_effective_at(at):
        return []
    last_seen = (sb.query(MeterReading)
                 .filter(MeterReading.meter_no == device.meter_no)
                 .order_by(MeterReading.reported_at.desc()).first())
    if last_seen is None:
        return []
    cutoff = at - timedelta(minutes=rule["offline_minutes"])
    if last_seen.reported_at >= cutoff:
        return []
    tag = ("系统默认阈值" if policy.is_default
           else f"命中{policy.scope}级策略 v{policy.version}")
    alert, created = raise_alert(
        sb, device, AnomalyType.DEVICE_OFFLINE,
        detail=f"设备最后上报时间 {last_seen.reported_at:%Y-%m-%d %H:%M:%S}，"
               f"截至 {at:%Y-%m-%d %H:%M:%S} 已超过 {rule['offline_minutes']} 分钟"
               f"未上报（{tag}，试跑模拟）",
        detected_at=at,
        policy=policy,
    )
    return [alert] if created else []
