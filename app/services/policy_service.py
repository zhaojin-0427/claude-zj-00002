"""分层检测策略服务：草稿 CRUD、版本发布、按"电表＞房间＞楼栋＞全局"解析有效策略。

设计要点：
- DetectionStrategy 为可编辑的工作副本（草稿）；发布生成不可变的 DetectionStrategyVersion，
  线上检测与告警只引用已发布版本。再次编辑草稿不影响已发布版本，需重新发布才生效。
- 解析有效策略时，按层级优先级依次查找"已启用 + 已发布 + 在生效时段内"的策略；
  任一层级未命中（未配置/未发布/已停用/不在生效时段）即向下回退，全部未命中使用系统默认。
- 命中后由 EffectivePolicy.snapshot_for 生成完整参数快照，冻结到告警上，
  后续策略变更/重新发布不会影响历史告警。
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from ..config import settings
from ..constants import AnomalyType, StrategyScope
from ..exceptions import ApiException
from ..models import DetectionStrategy, DetectionStrategyVersion
from ..utils import utcnow

# 各异常类型的规则字段：(字段, 类型, 最小值, 最大值)；enabled 统一处理
_RULE_SPECS: Dict[str, Dict[str, Tuple[type, float, float]]] = {
    AnomalyType.SUSTAINED_HIGH_LOAD: {
        "threshold_kw": (float, 0.0, None),
        "consecutive": (int, 1, None),
    },
    AnomalyType.NIGHT_ACTIVE: {
        "threshold_kw": (float, 0.0, None),
        "consecutive": (int, 1, None),
        "night_start_hour": (int, 0, 23),
        "night_end_hour": (int, 0, 23),
    },
    AnomalyType.POWER_JUMP: {
        "threshold_kw": (float, 0.0, None),
    },
    AnomalyType.SUSPECTED_THEFT: {
        "min_expected_kwh": (float, 0.0, None),
        "drop_tolerance": (float, 0.0, 1.0),
    },
    AnomalyType.DEVICE_OFFLINE: {
        "offline_minutes": (int, 1, None),
    },
}

# 层级 -> 优先级（高 -> 低）
_PRIORITY_ORDER = (StrategyScope.METER, StrategyScope.ROOM,
                   StrategyScope.BUILDING, StrategyScope.GLOBAL)


def default_rules() -> dict:
    """系统默认规则（来自环境变量配置），任何层级都未命中时使用。"""
    return {
        AnomalyType.SUSTAINED_HIGH_LOAD: {
            "enabled": True,
            "threshold_kw": settings.HIGH_POWER_THRESHOLD_KW,
            "consecutive": settings.HIGH_POWER_CONSECUTIVE,
        },
        AnomalyType.NIGHT_ACTIVE: {
            "enabled": True,
            "threshold_kw": settings.NIGHT_POWER_THRESHOLD_KW,
            "consecutive": settings.NIGHT_CONSECUTIVE,
            "night_start_hour": settings.NIGHT_START_HOUR,
            "night_end_hour": settings.NIGHT_END_HOUR,
        },
        AnomalyType.POWER_JUMP: {
            "enabled": True,
            "threshold_kw": settings.POWER_JUMP_THRESHOLD_KW,
        },
        AnomalyType.SUSPECTED_THEFT: {
            "enabled": True,
            "min_expected_kwh": settings.THEFT_MIN_EXPECTED_KWH,
            "drop_tolerance": settings.THEFT_DROP_TOLERANCE,
        },
        AnomalyType.DEVICE_OFFLINE: {
            "enabled": True,
            "offline_minutes": settings.OFFLINE_MINUTES,
        },
    }


def normalize_rules(rules_in: Optional[dict]) -> dict:
    """校验并归一化规则：按异常类型部分覆盖默认值，未知类型/非法字段直接拒绝。"""
    if rules_in is None:
        rules_in = {}
    if not isinstance(rules_in, dict):
        raise ApiException(40000, "rules 必须为对象，键为异常类型")
    unknown = set(rules_in) - set(AnomalyType.ALL)
    if unknown:
        raise ApiException(40000, f"未知异常类型: {sorted(unknown)}，可选: {AnomalyType.ALL}")

    merged = default_rules()
    for atype, patch in rules_in.items():
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            raise ApiException(40000, f"rules.{atype} 必须为对象")
        rule = dict(merged[atype])
        spec = _RULE_SPECS[atype]
        for key, value in patch.items():
            if key == "enabled":
                if not isinstance(value, bool):
                    raise ApiException(40000, f"rules.{atype}.enabled 必须为布尔值")
                rule["enabled"] = value
                continue
            if key not in spec:
                raise ApiException(40000, f"rules.{atype} 含未知字段 {key}，"
                                          f"可选: ['enabled', *{sorted(spec)}]")
            kind, lo, hi = spec[key]
            if isinstance(value, bool):
                raise ApiException(40000, f"rules.{atype}.{key} 必须为 {kind.__name__}")
            if kind is int and isinstance(value, float) and not value.is_integer():
                raise ApiException(40000, f"rules.{atype}.{key} 必须为整数")
            try:
                value = kind(value)
            except (TypeError, ValueError):
                raise ApiException(40000, f"rules.{atype}.{key} 必须为 {kind.__name__}")
            if lo is not None and value < lo:
                raise ApiException(40000, f"rules.{atype}.{key} 不能小于 {lo}")
            if hi is not None and value > hi:
                raise ApiException(40000, f"rules.{atype}.{key} 不能大于 {hi}")
            rule[key] = value
        merged[atype] = rule
    return merged


def _parse_hhmm(value: str) -> int:
    try:
        hh, mm = str(value).split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        raise ApiException(40000, f"生效时段时间格式非法: {value}，应为 HH:MM")
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ApiException(40000, f"生效时段时间越界: {value}")
    return h * 60 + m


def normalize_periods(periods_in: Optional[list]) -> List[dict]:
    """校验生效时段。空列表表示全天生效。

    每项：{"days_of_week": [1..7]（周一=1）, "start": "HH:MM", "end": "HH:MM"}；
    start > end 表示跨夜时段（如 23:00-06:00）。
    """
    if not periods_in:
        return []
    if not isinstance(periods_in, list):
        raise ApiException(40000, "effective_periods 必须为数组")
    result = []
    for p in periods_in:
        if not isinstance(p, dict):
            raise ApiException(40000, "生效时段每一项必须为对象")
        days = p.get("days_of_week", list(range(1, 8)))
        if not isinstance(days, list) or not days:
            raise ApiException(40000, "days_of_week 必须为非空数组（1=周一 ... 7=周日）")
        days_norm = []
        for d in days:
            if not isinstance(d, int) or isinstance(d, bool) or not 1 <= d <= 7:
                raise ApiException(40000, "days_of_week 取值范围 1-7（周一=1）")
            if d not in days_norm:
                days_norm.append(d)
        start_min = _parse_hhmm(p.get("start", "00:00"))
        end_min = _parse_hhmm(p.get("end", "00:00"))
        if start_min == end_min:
            raise ApiException(40000, "生效时段起止时间不能相同；全天生效请留空 effective_periods")
        result.append({"days_of_week": sorted(days_norm),
                       "start": f"{start_min // 60:02d}:{start_min % 60:02d}",
                       "end": f"{end_min // 60:02d}:{end_min % 60:02d}"})
    return result


def period_is_effective(periods: List[dict], ts: datetime) -> bool:
    """判断 ts（naive UTC）是否落在生效时段内；periods 为空表示全天。"""
    if not periods:
        return True
    local = ts + timedelta(hours=settings.LOCAL_UTC_OFFSET_HOURS)
    minutes = local.hour * 60 + local.minute
    dow = local.isoweekday()  # 周一=1 ... 周日=7
    for p in periods:
        if dow not in p["days_of_week"]:
            continue
        start = _parse_hhmm(p["start"])
        end = _parse_hhmm(p["end"])
        if start < end:
            if start <= minutes < end:
                return True
        else:  # 跨夜：23:00-06:00 => >=23:00 或 <06:00
            if minutes >= start or minutes < end:
                return True
    return False


@dataclass(frozen=True)
class EffectivePolicy:
    """一次检测实际使用的有效策略（可能是系统默认）。"""
    scope: str
    rules: dict
    periods: Tuple[dict, ...]
    strategy_id: Optional[int] = None
    version_id: Optional[int] = None
    version: Optional[int] = None
    strategy_name: str = "系统默认策略"
    scope_target: dict = None
    is_default: bool = False

    def rule(self, anomaly_type: str) -> dict:
        return self.rules[anomaly_type]

    def is_enabled(self, anomaly_type: str) -> bool:
        return bool(self.rules[anomaly_type].get("enabled", True))

    def is_effective_at(self, ts: datetime) -> bool:
        return period_is_effective(list(self.periods), ts)

    def snapshot_for(self, anomaly_type: str, at: datetime) -> dict:
        """生成冻结到告警上的完整参数快照。"""
        return {
            "anomaly_type": anomaly_type,
            "rule": self.rules[anomaly_type],
            "rules": self.rules,
            "effective_periods": list(self.periods),
            "effective_at": at.isoformat(),
            "in_effective_period": self.is_effective_at(at),
            "scope": self.scope,
            "scope_target": self.scope_target or {},
            "is_default": self.is_default,
            "strategy_id": self.strategy_id,
            "strategy_version_id": self.version_id,
            "version": self.version,
            "strategy_name": self.strategy_name,
        }


def _default_policy() -> EffectivePolicy:
    return EffectivePolicy(
        scope=StrategyScope.GLOBAL,
        rules=default_rules(),
        periods=(),
        scope_target={},
        is_default=True,
    )


def _invalidate_cache(db: Session) -> None:
    db.info.pop("policy_active_pairs", None)


def _active_pairs(db: Session) -> List[Tuple[DetectionStrategy, DetectionStrategyVersion]]:
    """当前"已启用且已发布"的策略与其当前版本（按会话缓存；发布/启停时失效）。"""
    if "policy_active_pairs" not in db.info:
        pairs = (db.query(DetectionStrategy, DetectionStrategyVersion)
                 .join(DetectionStrategyVersion,
                       DetectionStrategy.current_version_id == DetectionStrategyVersion.id)
                 .filter(DetectionStrategy.enabled.is_(True))
                 .all())
        db.info["policy_active_pairs"] = pairs
    return db.info["policy_active_pairs"]


def _version_matches(version: DetectionStrategyVersion, scope: str,
                     meter_no: str, room_no: str, building: str) -> bool:
    if version.scope != scope:
        return False
    if scope == StrategyScope.GLOBAL:
        return True
    if scope == StrategyScope.BUILDING:
        return bool(building) and version.building == building
    if scope == StrategyScope.ROOM:
        return bool(building) and bool(room_no) and \
            version.building == building and version.room_no == room_no
    if scope == StrategyScope.METER:
        return bool(meter_no) and version.meter_no == meter_no
    return False


def _build_policy(strategy: DetectionStrategy,
                  version: DetectionStrategyVersion) -> EffectivePolicy:
    target = {}
    if version.building:
        target["building"] = version.building
    if version.room_no:
        target["room_no"] = version.room_no
    if version.meter_no:
        target["meter_no"] = version.meter_no
    return EffectivePolicy(
        scope=version.scope,
        rules=json.loads(version.rules_json),
        periods=tuple(json.loads(version.periods_json or "[]")),
        strategy_id=strategy.id,
        version_id=version.id,
        version=version.version,
        strategy_name=version.name,
        scope_target=target,
        is_default=False,
    )


def resolve_policy(db: Session, meter_no: str, room_no: str,
                   building: str, at: datetime) -> EffectivePolicy:
    """按 电表＞房间＞楼栋＞全局 解析 at 时刻的有效策略；未命中返回系统默认。"""
    for scope in _PRIORITY_ORDER:
        for strategy, version in _active_pairs(db):
            if not _version_matches(version, scope, meter_no, room_no, building):
                continue
            periods = json.loads(version.periods_json or "[]")
            if not period_is_effective(periods, at):
                break  # 该层级已配置但不在生效时段：不与同层其他策略重复匹配
            return _build_policy(strategy, version)
    return _default_policy()


# ----------------------------- 草稿 CRUD -----------------------------

def _scope_targets(scope: str, building: str, room_no: str, meter_no: str) -> dict:
    if scope == StrategyScope.GLOBAL:
        return {"building": "", "room_no": "", "meter_no": ""}
    if scope == StrategyScope.BUILDING:
        if not building:
            raise ApiException(40000, "楼栋级策略必须提供 building")
        return {"building": building, "room_no": "", "meter_no": ""}
    if scope == StrategyScope.ROOM:
        if not building or not room_no:
            raise ApiException(40000, "房间级策略必须同时提供 building 和 room_no")
        return {"building": building, "room_no": room_no, "meter_no": ""}
    if scope == StrategyScope.METER:
        if not meter_no:
            raise ApiException(40000, "电表级策略必须提供 meter_no")
        return {"building": building or "", "room_no": room_no or "", "meter_no": meter_no}
    raise ApiException(40000, f"非法策略层级，可选: {StrategyScope.ALL}")


def create_strategy(db: Session, *, name: str, scope: str, building: str = "",
                    room_no: str = "", meter_no: str = "", rules: Optional[dict] = None,
                    effective_periods: Optional[list] = None,
                    enabled: bool = True) -> DetectionStrategy:
    if scope not in StrategyScope.ALL:
        raise ApiException(40000, f"非法策略层级，可选: {StrategyScope.ALL}")
    targets = _scope_targets(scope, building, room_no, meter_no)
    rules_norm = normalize_rules(rules)
    periods_norm = normalize_periods(effective_periods)

    dup = (db.query(DetectionStrategy)
           .filter(DetectionStrategy.scope == scope,
                   DetectionStrategy.building == targets["building"],
                   DetectionStrategy.room_no == targets["room_no"],
                   DetectionStrategy.meter_no == targets["meter_no"])
           .first())
    if dup:
        raise ApiException(40000, "该作用域已存在策略，请直接编辑或使用查询接口定位")

    strategy = DetectionStrategy(
        name=name, scope=scope,
        building=targets["building"], room_no=targets["room_no"],
        meter_no=targets["meter_no"],
        rules_json=json.dumps(rules_norm, ensure_ascii=False),
        periods_json=json.dumps(periods_norm, ensure_ascii=False),
        enabled=enabled, published_version=0, current_version_id=None,
    )
    db.add(strategy)
    db.commit()
    db.refresh(strategy)
    return strategy


def get_strategy(db: Session, strategy_id: int) -> DetectionStrategy:
    strategy = db.get(DetectionStrategy, strategy_id)
    if strategy is None:
        raise ApiException(40401, "策略不存在", http_status=404)
    return strategy


def update_strategy(db: Session, strategy_id: int, *, name: Optional[str] = None,
                    rules: Optional[dict] = None,
                    effective_periods: Optional[list] = None) -> DetectionStrategy:
    """编辑草稿（工作副本）。已发布版本不可变，编辑只改动草稿，需重新发布才生效。"""
    strategy = get_strategy(db, strategy_id)
    if name is not None:
        if not name.strip():
            raise ApiException(40000, "策略名称不能为空")
        strategy.name = name
    if rules is not None:
        strategy.rules_json = json.dumps(normalize_rules(rules), ensure_ascii=False)
    if effective_periods is not None:
        strategy.periods_json = json.dumps(normalize_periods(effective_periods),
                                           ensure_ascii=False)
    _invalidate_cache(db)
    db.commit()
    db.refresh(strategy)
    return strategy


def set_enabled(db: Session, strategy_id: int, enabled: bool) -> DetectionStrategy:
    strategy = get_strategy(db, strategy_id)
    strategy.enabled = enabled
    _invalidate_cache(db)
    db.commit()
    db.refresh(strategy)
    return strategy


def publish_strategy(db: Session, strategy_id: int,
                     published_by: str = "") -> DetectionStrategyVersion:
    """发布草稿为不可变新版本（版本号递增），并切换为检测使用的当前版本。"""
    strategy = get_strategy(db, strategy_id)
    version = DetectionStrategyVersion(
        strategy_id=strategy.id,
        version=strategy.published_version + 1,
        name=strategy.name,
        scope=strategy.scope,
        building=strategy.building,
        room_no=strategy.room_no,
        meter_no=strategy.meter_no,
        rules_json=strategy.rules_json,
        periods_json=strategy.periods_json,
        published_by=published_by or "",
        published_at=utcnow(),
    )
    db.add(version)
    db.flush()
    strategy.published_version = version.version
    strategy.current_version_id = version.id
    _invalidate_cache(db)
    db.commit()
    db.refresh(version)
    return version


def list_versions(db: Session, strategy_id: int) -> List[DetectionStrategyVersion]:
    get_strategy(db, strategy_id)  # 404 校验
    return (db.query(DetectionStrategyVersion)
            .filter(DetectionStrategyVersion.strategy_id == strategy_id)
            .order_by(DetectionStrategyVersion.version.desc()).all())


def effective_chain(db: Session, meter_no: str, room_no: str,
                    building: str, at: datetime) -> dict:
    """返回各层级候选与最终命中的策略，用于解释优先级选择过程。"""
    chain = []
    for scope in _PRIORITY_ORDER:
        entry = {"scope": scope, "configured": False, "published": False,
                 "enabled": False, "in_effective_period": None, "version": None,
                 "strategy_id": None, "name": None}
        # 展示该层级的原始配置（即使停用/未发布/未到生效时段）
        q = db.query(DetectionStrategy).filter(DetectionStrategy.scope == scope)
        if scope == StrategyScope.BUILDING:
            q = q.filter(DetectionStrategy.building == building,
                         DetectionStrategy.room_no == "")
        elif scope == StrategyScope.ROOM:
            q = q.filter(DetectionStrategy.building == building,
                         DetectionStrategy.room_no == room_no)
        elif scope == StrategyScope.METER:
            q = q.filter(DetectionStrategy.meter_no == meter_no)
        raw_strategy = q.first()
        if raw_strategy is not None:
            entry.update({
                "configured": True,
                "enabled": raw_strategy.enabled,
                "published": raw_strategy.current_version_id is not None,
                "strategy_id": raw_strategy.id,
                "name": raw_strategy.name,
            })
            if raw_strategy.current_version_id is not None:
                v = db.get(DetectionStrategyVersion, raw_strategy.current_version_id)
                periods = json.loads(v.periods_json or "[]")
                entry["version"] = v.version
                entry["in_effective_period"] = period_is_effective(periods, at)
        chain.append(entry)
    policy = resolve_policy(db, meter_no, room_no, building, at)
    return {
        "meter_no": meter_no, "room_no": room_no, "building": building,
        "at": at.isoformat(),
        "chain": chain,
        "selected_scope": policy.scope,
        "selected_is_default": policy.is_default,
        "selected_version_id": policy.version_id,
        "selected_version": policy.version,
    }
