"""分层检测策略管理、版本发布、命中追溯与只读试跑接口。"""
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..constants import StrategyScope
from ..database import get_db
from ..exceptions import ApiException
from ..models import DetectionStrategy, DetectionStrategyVersion
from ..response import ok
from ..schemas import (StrategyCreateIn, StrategyEnabledIn, StrategyPublishIn,
                       StrategyUpdateIn, TrialRunIn)
from ..services import policy_service, trial_service
from ..utils import to_naive_utc

router = APIRouter(prefix="/detection-strategies", tags=["分层检测策略"])


def _periods_dumps(p: str) -> list:
    return json.loads(p or "[]")


def _serialize_strategy(s: DetectionStrategy, db: Optional[Session] = None) -> dict:
    unpublished = False
    if s.current_version_id is None:
        unpublished = True
    elif db is not None:
        v = db.get(DetectionStrategyVersion, s.current_version_id)
        unpublished = (v is None or
                       s.rules_json != v.rules_json or
                       s.periods_json != v.periods_json or
                       s.name != v.name)
    return {
        "id": s.id,
        "name": s.name,
        "scope": s.scope,
        "building": s.building,
        "room_no": s.room_no,
        "meter_no": s.meter_no,
        "rules": json.loads(s.rules_json or "{}"),
        "effective_periods": _periods_dumps(s.periods_json),
        "enabled": s.enabled,
        "published_version": s.published_version,
        "current_version_id": s.current_version_id,
        "is_published": s.current_version_id is not None,
        "has_unpublished_changes": unpublished,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _serialize_version(v: DetectionStrategyVersion) -> dict:
    return {
        "id": v.id,
        "strategy_id": v.strategy_id,
        "version": v.version,
        "name": v.name,
        "scope": v.scope,
        "building": v.building,
        "room_no": v.room_no,
        "meter_no": v.meter_no,
        "rules": json.loads(v.rules_json),
        "effective_periods": _periods_dumps(v.periods_json),
        "published_by": v.published_by,
        "published_at": v.published_at.isoformat() if v.published_at else None,
        "immutable": True,
    }


@router.post("", summary="创建策略草稿（全局/楼栋/房间/电表四级）")
def create_strategy(body: StrategyCreateIn, db: Session = Depends(get_db)):
    strategy = policy_service.create_strategy(
        db,
        name=body.name,
        scope=body.scope,
        building=body.building or "",
        room_no=body.room_no or "",
        meter_no=body.meter_no or "",
        rules=body.rules,
        effective_periods=[p.model_dump() for p in body.effective_periods]
        if body.effective_periods is not None else None,
        enabled=body.enabled,
    )
    return ok(_serialize_strategy(strategy, db), message="策略草稿已创建，发布后生效")


@router.get("", summary="策略查询（可按层级/楼栋/房间/电表/启停过滤）")
def list_strategies(
    scope: Optional[str] = Query(None, description="global/building/room/meter"),
    building: Optional[str] = None,
    room_no: Optional[str] = None,
    meter_no: Optional[str] = None,
    enabled: Optional[bool] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    if scope is not None and scope not in StrategyScope.ALL:
        raise ApiException(40000, f"非法策略层级，可选: {StrategyScope.ALL}")
    q = db.query(DetectionStrategy)
    if scope:
        q = q.filter(DetectionStrategy.scope == scope)
    if building:
        q = q.filter(DetectionStrategy.building == building)
    if room_no:
        q = q.filter(DetectionStrategy.room_no == room_no)
    if meter_no:
        q = q.filter(DetectionStrategy.meter_no == meter_no)
    if enabled is not None:
        q = q.filter(DetectionStrategy.enabled.is_(enabled))
    total = q.count()
    items = (q.order_by(DetectionStrategy.scope, DetectionStrategy.id)
             .offset((page - 1) * size).limit(size).all())
    return ok({"total": total, "page": page, "size": size,
               "items": [_serialize_strategy(s, db) for s in items]})


@router.get("/effective", summary="查询指定电表在某时刻的有效策略选择链")
def get_effective_policy(
    meter_no: str,
    room_no: Optional[str] = None,
    building: Optional[str] = None,
    at: Optional[datetime] = Query(None, description="判定时刻 ISO8601，默认当前"),
    db: Session = Depends(get_db),
):
    from ..utils import utcnow
    moment = to_naive_utc(at) if at else utcnow()
    # 未显式传房间/楼栋时以设备档案为准
    if room_no is None or building is None:
        from ..models import Device
        dev = db.query(Device).filter(Device.meter_no == meter_no).first()
        if dev is None:
            raise ApiException(40401, f"电表 {meter_no} 不存在", http_status=404)
        room_no = room_no if room_no is not None else dev.room_no
        building = building if building is not None else dev.building
    chain = policy_service.effective_chain(db, meter_no, room_no, building, moment)
    return ok(chain)


@router.post("/trial-run", summary="只读试跑：回放指定电表历史区间，不写正式告警")
def trial_run(body: TrialRunIn, db: Session = Depends(get_db)):
    result = trial_service.trial_run(db, body.meter_no, body.start, body.end)
    return ok(result, message="试跑完成（只读，未产生任何副作用）")


@router.get("/{strategy_id}", summary="策略详情（草稿当前内容）")
def get_strategy(strategy_id: int, db: Session = Depends(get_db)):
    strategy = policy_service.get_strategy(db, strategy_id)
    return ok(_serialize_strategy(strategy, db))


@router.put("/{strategy_id}", summary="编辑策略草稿（已发布版本不可变，改后需重新发布）")
def update_strategy(strategy_id: int, body: StrategyUpdateIn,
                    db: Session = Depends(get_db)):
    strategy = policy_service.update_strategy(
        db, strategy_id,
        name=body.name,
        rules=body.rules,
        effective_periods=[p.model_dump() for p in body.effective_periods]
        if body.effective_periods is not None else None,
    )
    return ok(_serialize_strategy(strategy, db),
              message="草稿已更新，需重新发布后生效；历史告警不受影响")


@router.post("/{strategy_id}/enabled", summary="启用/停用策略")
def set_strategy_enabled(strategy_id: int, body: StrategyEnabledIn,
                         db: Session = Depends(get_db)):
    strategy = policy_service.set_enabled(db, strategy_id, body.enabled)
    return ok(_serialize_strategy(strategy, db),
              message=f"策略已{'启用' if body.enabled else '停用'}")


@router.post("/{strategy_id}/publish", summary="发布新版本（版本不可变，自动递增）")
def publish_strategy(strategy_id: int, body: StrategyPublishIn,
                     db: Session = Depends(get_db)):
    version = policy_service.publish_strategy(db, strategy_id, body.published_by or "")
    return ok(_serialize_version(version),
              message=f"已发布 v{version.version}，检测即刻使用该版本")


@router.get("/{strategy_id}/versions", summary="策略历史版本列表（仅读）")
def list_versions(strategy_id: int, db: Session = Depends(get_db)):
    versions = policy_service.list_versions(db, strategy_id)
    return ok({"total": len(versions),
               "items": [_serialize_version(v) for v in versions]})
