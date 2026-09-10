from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Device, MeterReading
from ..response import ok
from ..schemas import DeviceOut, ReadingOut
from ..utils import to_naive_utc, utcnow

router = APIRouter(tags=["设备与读数"])


@router.get("/devices", summary="设备列表（含在线状态）")
def list_devices(building: Optional[str] = None, room_no: Optional[str] = None,
                 db: Session = Depends(get_db)):
    q = db.query(Device)
    if building:
        q = q.filter(Device.building == building)
    if room_no:
        q = q.filter(Device.room_no == room_no)
    now = utcnow()
    offline_before = now - timedelta(minutes=settings.OFFLINE_MINUTES)
    items = []
    for d in q.order_by(Device.building, Device.room_no).all():
        item = DeviceOut.model_validate(d).model_dump(mode="json")
        item["online"] = bool(d.last_seen_at and d.last_seen_at >= offline_before)
        items.append(item)
    return ok({"total": len(items), "items": items})


@router.get("/readings", summary="上报读数查询")
def list_readings(
    meter_no: Optional[str] = None,
    room_no: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = db.query(MeterReading)
    if meter_no:
        q = q.filter(MeterReading.meter_no == meter_no)
    if room_no:
        q = q.filter(MeterReading.room_no == room_no)
    if start:
        q = q.filter(MeterReading.reported_at >= to_naive_utc(start))
    if end:
        q = q.filter(MeterReading.reported_at <= to_naive_utc(end))
    total = q.count()
    items = (q.order_by(MeterReading.reported_at.desc())
             .offset((page - 1) * size).limit(size).all())
    return ok({"total": total, "page": page, "size": size,
               "items": [ReadingOut.model_validate(r).model_dump(mode="json") for r in items]})
