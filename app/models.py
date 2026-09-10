from sqlalchemy import (Boolean, DateTime, Float, Index, Integer, String,
                        Text, UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .utils import utcnow


class Device(Base):
    """电表设备档案，首次上报时自动建档。"""
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    room_no: Mapped[str] = mapped_column(String(64), index=True)
    building: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="normal")
    last_seen_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)


class MeterReading(Base):
    """电表上报读数。(meter_no, reported_at) 唯一约束保证幂等接收。"""
    __tablename__ = "meter_readings"
    __table_args__ = (
        UniqueConstraint("meter_no", "reported_at", name="uq_reading_meter_ts"),
        Index("ix_reading_room_ts", "room_no", "reported_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meter_no: Mapped[str] = mapped_column(String(64), index=True)
    room_no: Mapped[str] = mapped_column(String(64), index=True)
    building: Mapped[str] = mapped_column(String(64), index=True)
    reading: Mapped[float] = mapped_column(Float)          # 累计电量 kWh
    power: Mapped[float] = mapped_column(Float)            # 当前功率 kW
    device_status: Mapped[str] = mapped_column(String(32), default="normal")
    reported_at: Mapped[DateTime] = mapped_column(DateTime, index=True)
    received_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    is_outlier: Mapped[bool] = mapped_column(Boolean, default=False)      # 被去噪标记的离群点
    jump_evaluated: Mapped[bool] = mapped_column(Boolean, default=False)  # 跳变检测已评估


class Alert(Base):
    """异常告警，带状态机流转与响应时长记录。"""
    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alert_room_type", "room_no", "anomaly_type"),
        Index("ix_alert_building_status", "building", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    meter_no: Mapped[str] = mapped_column(String(64), index=True)
    room_no: Mapped[str] = mapped_column(String(64), index=True)
    building: Mapped[str] = mapped_column(String(64), index=True)
    anomaly_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    title: Mapped[str] = mapped_column(String(128))
    detail: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")          # 处置备注（追加）
    first_detected_at: Mapped[DateTime] = mapped_column(DateTime)
    last_detected_at: Mapped[DateTime] = mapped_column(DateTime)
    acknowledged_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    response_seconds: Mapped[float] = mapped_column(Float, nullable=True)  # 首次响应耗时
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
