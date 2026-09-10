from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import AlertStatus


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
