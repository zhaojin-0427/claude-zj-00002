"""异常类型、告警状态与状态机定义。"""


class AnomalyType:
    SUSTAINED_HIGH_LOAD = "sustained_high_load"   # 持续高负荷
    NIGHT_ACTIVE = "night_active"                 # 夜间异常活跃
    POWER_JUMP = "power_jump"                     # 功率跳变
    SUSPECTED_THEFT = "suspected_theft"           # 疑似窃电
    DEVICE_OFFLINE = "device_offline"             # 设备离线

    ALL = [SUSTAINED_HIGH_LOAD, NIGHT_ACTIVE, POWER_JUMP, SUSPECTED_THEFT, DEVICE_OFFLINE]


class StrategyScope:
    """分层策略层级，数值越大优先级越高（电表＞房间＞楼栋＞全局）。"""
    GLOBAL = "global"     # 全局
    BUILDING = "building" # 楼栋
    ROOM = "room"         # 房间
    METER = "meter"       # 电表

    ALL = [GLOBAL, BUILDING, ROOM, METER]
    PRIORITY = {GLOBAL: 0, BUILDING: 1, ROOM: 2, METER: 3}
    LABELS = {GLOBAL: "全局", BUILDING: "楼栋", ROOM: "房间", METER: "电表"}


class AlertStatus:
    PENDING = "pending"               # 待确认
    INVESTIGATING = "investigating"   # 核查中
    RECTIFIED = "rectified"           # 已整改
    RECOVERED = "recovered"           # 已恢复
    FALSE_POSITIVE = "false_positive" # 误报

    ALL = [PENDING, INVESTIGATING, RECTIFIED, RECOVERED, FALSE_POSITIVE]
    OPEN = (PENDING, INVESTIGATING)                    # 未闭环（用于告警去重）
    TERMINAL = (RECTIFIED, RECOVERED, FALSE_POSITIVE)  # 终态


# 状态机：允许的流转路径
TRANSITIONS = {
    AlertStatus.PENDING: {AlertStatus.INVESTIGATING, AlertStatus.RECOVERED, AlertStatus.FALSE_POSITIVE},
    AlertStatus.INVESTIGATING: {AlertStatus.RECTIFIED, AlertStatus.RECOVERED, AlertStatus.FALSE_POSITIVE},
    AlertStatus.RECTIFIED: {AlertStatus.INVESTIGATING},  # 整改后复发可重新核查
    AlertStatus.RECOVERED: set(),
    AlertStatus.FALSE_POSITIVE: set(),
}

ANOMALY_LABELS = {
    AnomalyType.SUSTAINED_HIGH_LOAD: "持续高负荷",
    AnomalyType.NIGHT_ACTIVE: "夜间异常活跃",
    AnomalyType.POWER_JUMP: "功率跳变",
    AnomalyType.SUSPECTED_THEFT: "疑似窃电",
    AnomalyType.DEVICE_OFFLINE: "设备离线",
}

STATUS_LABELS = {
    AlertStatus.PENDING: "待确认",
    AlertStatus.INVESTIGATING: "核查中",
    AlertStatus.RECTIFIED: "已整改",
    AlertStatus.RECOVERED: "已恢复",
    AlertStatus.FALSE_POSITIVE: "误报",
}
