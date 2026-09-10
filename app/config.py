"""全局配置：所有检测阈值均可通过环境变量覆盖。"""
import os


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


class Settings:
    # ---- 数据接收 ----
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./smartmeter.db")
    # 本地时区相对 UTC 的偏移小时数（夜间时段按本地时间判定）
    LOCAL_UTC_OFFSET_HOURS: int = _int("LOCAL_UTC_OFFSET_HOURS", 8)

    # ---- 持续高负荷 ----
    HIGH_POWER_THRESHOLD_KW: float = _float("HIGH_POWER_THRESHOLD_KW", 5.0)   # 高功率阈值(kW)
    HIGH_POWER_CONSECUTIVE: int = _int("HIGH_POWER_CONSECUTIVE", 3)           # 连续次数

    # ---- 夜间异常活跃 ----
    NIGHT_START_HOUR: int = _int("NIGHT_START_HOUR", 23)   # 夜间开始(含)
    NIGHT_END_HOUR: int = _int("NIGHT_END_HOUR", 6)        # 夜间结束(不含)
    NIGHT_POWER_THRESHOLD_KW: float = _float("NIGHT_POWER_THRESHOLD_KW", 1.0)
    NIGHT_CONSECUTIVE: int = _int("NIGHT_CONSECUTIVE", 2)

    # ---- 功率跳变 ----
    POWER_JUMP_THRESHOLD_KW: float = _float("POWER_JUMP_THRESHOLD_KW", 3.0)

    # ---- 短时离群值去噪 ----
    OUTLIER_NEIGHBOR_MAX_KW: float = _float("OUTLIER_NEIGHBOR_MAX_KW", 2.0)  # 两侧邻点需处于正常区间
    OUTLIER_MIN_SPIKE_KW: float = _float("OUTLIER_MIN_SPIKE_KW", 3.0)        # 尖峰最小绝对值
    OUTLIER_FACTOR: float = _float("OUTLIER_FACTOR", 5.0)                    # 或超过邻点均值倍数

    # ---- 疑似窃电 ----
    THEFT_MIN_EXPECTED_KWH: float = _float("THEFT_MIN_EXPECTED_KWH", 0.5)  # 期望电量下限，避免误报
    THEFT_DROP_TOLERANCE: float = _float("THEFT_DROP_TOLERANCE", 0.6)      # 实际电量低于期望*(1-该值) 判定异常

    # ---- 设备离线 ----
    OFFLINE_MINUTES: int = _int("OFFLINE_MINUTES", 30)
    # 上报时间允许的未来时钟偏差（超出视为非新鲜数据，不恢复离线告警；
    # 设备 last_seen_at 也以此钳制，避免未来时间戳导致设备永久在线）
    MAX_FUTURE_SKEW_MINUTES: int = _int("MAX_FUTURE_SKEW_MINUTES", 5)

    # ---- MQTT（可选） ----
    MQTT_ENABLED: bool = os.getenv("MQTT_ENABLED", "false").lower() == "true"
    MQTT_HOST: str = os.getenv("MQTT_HOST", "127.0.0.1")
    MQTT_PORT: int = _int("MQTT_PORT", 1883)
    MQTT_TOPIC: str = os.getenv("MQTT_TOPIC", "smartmeter/+/reading")
    MQTT_USERNAME: str = os.getenv("MQTT_USERNAME", "")
    MQTT_PASSWORD: str = os.getenv("MQTT_PASSWORD", "")


settings = Settings()
