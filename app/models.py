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
    denoise_evaluated: Mapped[bool] = mapped_column(Boolean, default=False)  # 去噪已评估
    jump_evaluated: Mapped[bool] = mapped_column(Boolean, default=False)  # 跳变检测已评估


class DetectionStrategy(Base):
    """分层检测策略（工作副本/草稿）。

    每个作用域（全局/楼栋/房间/电表）最多一条；规则参数与生效时段保存在 JSON 中。
    发布后生成不可变的 DetectionStrategyVersion，检测/告警始终使用已发布版本；
    发布前对该记录的修改不影响线上检测。
    """
    __tablename__ = "detection_strategies"
    __table_args__ = (
        UniqueConstraint("scope", "building", "room_no", "meter_no",
                         name="uq_strategy_scope"),
        Index("ix_strategy_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(16), index=True)  # global/building/room/meter
    building: Mapped[str] = mapped_column(String(64), default="")
    room_no: Mapped[str] = mapped_column(String(64), default="")
    meter_no: Mapped[str] = mapped_column(String(64), default="")
    rules_json: Mapped[str] = mapped_column(Text, default="{}")  # 五类异常阈值/连续次数
    periods_json: Mapped[str] = mapped_column(Text, default="[]")  # 生效时段（空=全天）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    current_version_id: Mapped[int] = mapped_column(Integer, nullable=True)  # 当前发布版本
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DetectionStrategyVersion(Base):
    """已发布的不可变策略版本。发布即冻结，任何修改都必须新建版本。"""
    __tablename__ = "detection_strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_version"),
        Index("ix_version_published", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(16), index=True)
    building: Mapped[str] = mapped_column(String(64), default="")
    room_no: Mapped[str] = mapped_column(String(64), default="")
    meter_no: Mapped[str] = mapped_column(String(64), default="")
    rules_json: Mapped[str] = mapped_column(Text)       # 冻结时的完整规则参数
    periods_json: Mapped[str] = mapped_column(Text)     # 冻结时的生效时段
    published_by: Mapped[str] = mapped_column(String(64), default="")
    published_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # ---- 命中追溯：新告警冻结实际命中的策略版本与完整参数快照 ----
    strategy_version_id: Mapped[int] = mapped_column(Integer, nullable=True)
    strategy_snapshot_json: Mapped[str] = mapped_column(Text, nullable=True)
    strategy_scope: Mapped[str] = mapped_column(String(16), nullable=True)
    first_detected_at: Mapped[DateTime] = mapped_column(DateTime)
    last_detected_at: Mapped[DateTime] = mapped_column(DateTime)
    acknowledged_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    response_seconds: Mapped[float] = mapped_column(Float, nullable=True)  # 首次响应耗时
    created_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[DateTime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
