from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import AlertStatus, AnomalyType, StrategyScope


def _reject_non_finite(obj: Any, path: str) -> Any:
    """递归拒绝 JSON 不支持的 NaN/Infinity（pydantic float 默认放行 NaN）。"""
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError(f"{path} 必须为有限数值（NaN/Infinity 非法）")
    if isinstance(obj, dict):
        for k, v in obj.items():
            _reject_non_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_non_finite(v, f"{path}[{i}]")
    return obj


def _to_naive_utc(v: datetime) -> datetime:
    if v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


class ReadingIn(BaseModel):
    """单条电表上报数据。"""
    meter_no: str = Field(min_length=1, max_length=64, description="表号")
    room_no: str = Field(min_length=1, max_length=64, description="房间号")
    building: str = Field(min_length=1, max_length=64, description="楼栋")
    reading: float = Field(ge=0, description="累计电量 kWh")
    power: float = Field(ge=0, description="当前功率 kW")
    reported_at: datetime = Field(description="上报时间 ISO8601")
    device_status: str = Field(default="normal", max_length=32, description="设备状态")

    @field_validator("reported_at")
    @classmethod
    def normalize_ts(cls, v: datetime) -> datetime:
        return _to_naive_utc(v)


class ReadingBatchIn(BaseModel):
    readings: List[ReadingIn] = Field(min_length=1, max_length=500)


class StatusUpdateIn(BaseModel):
    status: str = Field(description="目标状态")
    note: Optional[str] = Field(default=None, max_length=500, description="处置备注")

    @field_validator("status")
    @classmethod
    def check_status(cls, v: str) -> str:
        if v not in AlertStatus.ALL:
            raise ValueError(f"非法状态，可选值: {AlertStatus.ALL}")
        return v


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    alert_no: str
    meter_no: str
    room_no: str
    building: str
    anomaly_type: str
    status: str
    title: str
    detail: str
    note: str
    strategy_version_id: Optional[int] = None
    strategy_scope: Optional[str] = None
    first_detected_at: datetime
    last_detected_at: datetime
    acknowledged_at: Optional[datetime]
    resolved_at: Optional[datetime]
    response_seconds: Optional[float]
    created_at: datetime


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    meter_no: str
    room_no: str
    building: str
    status: str
    last_seen_at: Optional[datetime]
    created_at: datetime


class ReadingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    meter_no: str
    room_no: str
    building: str
    reading: float
    power: float
    device_status: str
    reported_at: datetime
    is_outlier: bool


# ----------------------------- 分层检测策略 -----------------------------

class EffectivePeriodIn(BaseModel):
    days_of_week: List[int] = Field(
        default_factory=lambda: list(range(1, 8)),
        description="生效星期，1=周一 ... 7=周日；默认整周")
    start: str = Field(default="00:00", description="生效开始 HH:MM（本地时间）")
    end: str = Field(default="23:59",
                     description="生效结束 HH:MM；start>end 表示跨夜；全天生效请省略该时段")


class StrategyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, description="策略名称")
    scope: str = Field(description="层级：global/building/room/meter")
    building: Optional[str] = Field(default=None, max_length=64)
    room_no: Optional[str] = Field(default=None, max_length=64)
    meter_no: Optional[str] = Field(default=None, max_length=64)
    rules: Optional[Dict[str, Dict[str, Any]]] = Field(
        default=None, description="按异常类型的阈值/连续次数，部分覆盖默认值")
    effective_periods: Optional[List[EffectivePeriodIn]] = Field(
        default=None, description="生效时段；空或缺省表示全天生效")
    enabled: bool = Field(default=True, description="创建后是否启用")

    @field_validator("scope")
    @classmethod
    def check_scope(cls, v: str) -> str:
        if v not in StrategyScope.ALL:
            raise ValueError(f"非法策略层级，可选值: {StrategyScope.ALL}")
        return v

    @field_validator("rules")
    @classmethod
    def check_rule_keys(cls, v):
        if v is not None:
            unknown = set(v) - set(AnomalyType.ALL)
            if unknown:
                raise ValueError(f"未知异常类型: {sorted(unknown)}")
            _reject_non_finite(v, "rules")
        return v


class StrategyUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    rules: Optional[Dict[str, Dict[str, Any]]] = None
    effective_periods: Optional[List[EffectivePeriodIn]] = None

    @field_validator("rules")
    @classmethod
    def check_rules_finite(cls, v):
        if v is not None:
            unknown = set(v) - set(AnomalyType.ALL)
            if unknown:
                raise ValueError(f"未知异常类型: {sorted(unknown)}")
            _reject_non_finite(v, "rules")
        return v


class StrategyEnabledIn(BaseModel):
    enabled: bool


class StrategyPublishIn(BaseModel):
    published_by: Optional[str] = Field(default="", max_length=64)


class TrialRunIn(BaseModel):
    meter_no: str = Field(min_length=1, max_length=64, description="指定电表")
    start: datetime = Field(description="历史时间范围起点 ISO8601")
    end: datetime = Field(description="历史时间范围终点 ISO8601")

    @field_validator("start", "end")
    @classmethod
    def normalize_ts(cls, v: datetime) -> datetime:
        return _to_naive_utc(v)
