"""数据接收服务：幂等写入 + 触发检测。"""
from typing import List

from sqlalchemy.orm import Session

from ..models import Alert, Device, MeterReading
from ..schemas import ReadingIn
from ..utils import utcnow
from . import detection
from .alert_service import recover_offline_alerts


def _upsert_device(db: Session, item: ReadingIn) -> Device:
    device = db.query(Device).filter(Device.meter_no == item.meter_no).first()
    if device is None:
        device = Device(meter_no=item.meter_no, room_no=item.room_no,
                        building=item.building, last_seen_at=item.reported_at)
        db.add(device)
        db.flush()
    else:
        device.room_no = item.room_no
        device.building = item.building
        if device.last_seen_at is None or item.reported_at > device.last_seen_at:
            device.last_seen_at = item.reported_at
    return device


def ingest_readings(db: Session, items: List[ReadingIn]) -> dict:
    """
    幂等接收：(meter_no, reported_at) 唯一约束去重，重复上报返回 duplicates 计数，
    不会重复触发检测。
    """
    items = sorted(items, key=lambda x: (x.meter_no, x.reported_at))
    accepted, duplicates = 0, 0
    new_alerts: List[Alert] = []

    for item in items:
        exists = (db.query(MeterReading)
                  .filter(MeterReading.meter_no == item.meter_no,
                          MeterReading.reported_at == item.reported_at)
                  .first())
        if exists:
            duplicates += 1
            continue

        device = _upsert_device(db, item)
        recover_offline_alerts(db, device, item.reported_at)

        reading = MeterReading(
            meter_no=item.meter_no, room_no=item.room_no, building=item.building,
            reading=item.reading, power=item.power,
            device_status=item.device_status, reported_at=item.reported_at,
            received_at=utcnow(),
        )
        db.add(reading)
        db.flush()
        new_alerts += detection.process_reading(db, device, reading)
        accepted += 1

    db.commit()
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "alerts_triggered": [
            {"alert_no": a.alert_no, "anomaly_type": a.anomaly_type,
             "room_no": a.room_no, "building": a.building, "title": a.title}
            for a in new_alerts
        ],
    }
