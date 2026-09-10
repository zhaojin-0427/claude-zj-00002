from datetime import datetime
from typing import Optional
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..constants import AlertStatus, AnomalyType
from ..database import get_db
from ..exceptions import ApiException
from ..models import Alert
from ..response import ok
from ..schemas import AlertOut, StatusUpdateIn
from ..services import alert_service
from ..utils import to_naive_utc

router = APIRouter(prefix="/alerts", tags=["告警管理"])


def _serialize(a: Alert) -> dict:
    data = AlertOut.model_validate(a).model_dump(mode="json")
    data["strategy_snapshot"] = (
        json.loads(a.strategy_snapshot_json) if a.strategy_snapshot_json else None)
    return data


@router.get("", summary="告警查询（按房间/楼栋/异常类型/状态/时间范围）")
def list_alerts(
    room_no: Optional[str] = Query(None, description="房间号"),
    building: Optional[str] = Query(None, description="楼栋"),
    anomaly_type: Optional[str] = Query(None, description="异常类型"),
    status: Optional[str] = Query(None, description="告警状态"),
    start: Optional[datetime] = Query(None, description="起始时间 ISO8601"),
    end: Optional[datetime] = Query(None, description="结束时间 ISO8601"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if anomaly_type and anomaly_type not in AnomalyType.ALL:
        raise ApiException(40002, f"非法异常类型，可选值: {AnomalyType.ALL}")
    if status and status not in AlertStatus.ALL:
        raise ApiException(40003, f"非法告警状态，可选值: {AlertStatus.ALL}")

    q = db.query(Alert)
    if room_no:
        q = q.filter(Alert.room_no == room_no)
    if building:
        q = q.filter(Alert.building == building)
    if anomaly_type:
        q = q.filter(Alert.anomaly_type == anomaly_type)
    if status:
        q = q.filter(Alert.status == status)
    if start:
        q = q.filter(Alert.first_detected_at >= to_naive_utc(start))
    if end:
        q = q.filter(Alert.first_detected_at <= to_naive_utc(end))

    total = q.count()
    items = (q.order_by(Alert.created_at.desc())
             .offset((page - 1) * size).limit(size).all())
    return ok({"total": total, "page": page, "size": size,
               "items": [_serialize(a) for a in items]})


@router.post("/scan-offline", summary="触发设备离线扫描")
def scan_offline(db: Session = Depends(get_db)):
    created = alert_service.scan_offline_devices(db)
    return ok({"new_alerts": [_serialize(a) for a in created], "count": len(created)},
              message=f"扫描完成，新增离线告警 {len(created)} 条")


@router.get("/{alert_id}", summary="告警详情")
def get_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise ApiException(40401, "告警不存在", http_status=404)
    return ok(_serialize(alert))


@router.post("/{alert_id}/status", summary="告警状态流转（待确认/核查中/已整改/已恢复/误报）")
def update_status(alert_id: int, body: StatusUpdateIn, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if not alert:
        raise ApiException(40401, "告警不存在", http_status=404)
    alert = alert_service.transition_alert(db, alert, body.status, body.note)
    return ok(_serialize(alert), message=f"状态已更新为 {body.status}")
