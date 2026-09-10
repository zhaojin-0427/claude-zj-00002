from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..constants import AlertStatus
from ..database import get_db
from ..models import Alert, Device
from ..response import ok
from ..utils import utcnow

router = APIRouter(prefix="/stats", tags=["统计分析"])

# 响应时长分桶（秒）
BUCKETS = [("<5分钟", 0, 300), ("5-30分钟", 300, 1800), ("30分钟-2小时", 1800, 7200),
           ("2-24小时", 7200, 86400), (">24小时", 86400, None)]


@router.get("/top-rooms", summary="近N天高异常房间排行")
def top_rooms(days: int = Query(7, ge=1, le=90),
              limit: int = Query(10, ge=1, le=100),
              building: Optional[str] = None,
              db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=days)
    base = db.query(Alert).filter(Alert.first_detected_at >= since,
                                  Alert.status != AlertStatus.FALSE_POSITIVE)
    if building:
        base = base.filter(Alert.building == building)

    rows = (base.with_entities(Alert.room_no, Alert.building,
                               func.count().label("cnt"))
            .group_by(Alert.room_no, Alert.building)
            .order_by(func.count().desc())
            .limit(limit).all())

    # 每个房间的异常类型分布
    type_rows = (base.with_entities(Alert.room_no, Alert.anomaly_type, func.count())
                 .group_by(Alert.room_no, Alert.anomaly_type).all())
    type_map = {}
    for room, atype, cnt in type_rows:
        type_map.setdefault(room, {})[atype] = cnt

    items = [
        {"rank": i + 1, "room_no": r.room_no, "building": r.building,
         "alert_count": r.cnt, "type_breakdown": type_map.get(r.room_no, {})}
        for i, r in enumerate(rows)
    ]
    return ok({"days": days, "items": items})


@router.get("/response-time", summary="告警响应时长分布")
def response_time(days: int = Query(7, ge=1, le=90),
                  building: Optional[str] = None,
                  db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=days)
    q = db.query(Alert).filter(Alert.first_detected_at >= since)
    if building:
        q = q.filter(Alert.building == building)
    alerts = q.all()

    distribution = {label: 0 for label, _, _ in BUCKETS}
    distribution["未响应"] = 0
    seconds_list = []
    for a in alerts:
        if a.response_seconds is None:
            distribution["未响应"] += 1
            continue
        seconds_list.append(a.response_seconds)
        for label, lo, hi in BUCKETS:
            if a.response_seconds >= lo and (hi is None or a.response_seconds < hi):
                distribution[label] += 1
                break

    seconds_list.sort()
    n = len(seconds_list)
    stats = None
    if n:
        median = (seconds_list[n // 2] if n % 2
                  else (seconds_list[n // 2 - 1] + seconds_list[n // 2]) / 2)
        stats = {
            "avg_seconds": round(sum(seconds_list) / n, 1),
            "median_seconds": round(median, 1),
            "max_seconds": round(seconds_list[-1], 1),
            "responded_count": n,
        }
    return ok({"days": days, "total_alerts": len(alerts),
               "distribution": distribution, "stats": stats})


@router.get("/compliance", summary="房间用电合规率")
def compliance(days: int = Query(30, ge=1, le=365),
               building: Optional[str] = None,
               db: Session = Depends(get_db)):
    since = utcnow() - timedelta(days=days)
    dev_q = db.query(Device)
    if building:
        dev_q = dev_q.filter(Device.building == building)
    devices = dev_q.all()

    # 周期内有非误报告警的电表视为不合规
    violating = {row[0] for row in
                 db.query(Alert.meter_no)
                 .filter(Alert.first_detected_at >= since,
                         Alert.status != AlertStatus.FALSE_POSITIVE)
                 .distinct().all()}

    by_building = {}
    for d in devices:
        g = by_building.setdefault(d.building, {"total_rooms": 0, "non_compliant_rooms": 0})
        g["total_rooms"] += 1
        if d.meter_no in violating:
            g["non_compliant_rooms"] += 1

    groups = []
    total = non_compliant = 0
    for b, g in sorted(by_building.items()):
        compliant = g["total_rooms"] - g["non_compliant_rooms"]
        groups.append({
            "building": b,
            "total_rooms": g["total_rooms"],
            "compliant_rooms": compliant,
            "non_compliant_rooms": g["non_compliant_rooms"],
            "compliance_rate": round(compliant / g["total_rooms"], 4),
        })
        total += g["total_rooms"]
        non_compliant += g["non_compliant_rooms"]

    overall = round((total - non_compliant) / total, 4) if total else None
    return ok({"days": days, "total_rooms": total,
               "compliant_rooms": total - non_compliant,
               "compliance_rate": overall, "by_building": groups})
