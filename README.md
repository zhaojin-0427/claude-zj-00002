# 智能电表用电异常检测与告警归档 API

纯后端服务：接收电表通过 **MQTT** 或 **定时 HTTP** 上报的数据，完成幂等接收、实时异常检测、
告警状态流转与统计分析。统一返回 `{code, message, data}`。

## 快速开始

```bash
pip install -r requirements.txt
python run.py                 # http://127.0.0.1:8000/docs
pytest tests/ -q              # 运行测试（19 个用例）
```

可选 MQTT 接入（与 HTTP 共用同一套幂等接收与检测管线）：

```bash
MQTT_HOST=broker.local MQTT_TOPIC='smartmeter/+/reading' python -m app.mqtt_worker
```

## 数据上报

`POST /api/v1/readings`（支持单条对象或 `{"readings": [...]}` 批量）：

```json
{
  "readings": [
    {"meter_no": "M1001", "room_no": "301", "building": "A栋",
     "reading": 1234.5, "power": 2.1, "reported_at": "2026-09-10T08:00:00",
     "device_status": "normal"}
  ]
}
```

- **幂等接收**：`(meter_no, reported_at)` 唯一约束，重复上报返回 `duplicates` 计数，不产生重复数据、不重复触发告警；
- 设备首次上报自动建档；`device_status`、房间、楼栋按"最新上报时间优先"同步（乱序补报不会用旧状态覆盖新状态）；
- 设备恢复上报时自动闭环其「设备离线」告警（置为已恢复）——仅**新鲜读数**（上报时间落在离线窗口内，且不超出 `MAX_FUTURE_SKEW_MINUTES` 未来时钟偏差）可触发恢复，陈旧补报或未来时间戳不会；自动恢复会记录 `acknowledged_at` 与 `response_seconds`（= 检出到恢复耗时），不计入"未响应"；
- 设备 `last_seen_at` 以服务器当前时间上钳制：未来时间戳的读数不会让设备永久显示在线；
- 响应中直接返回本次上报新触发的告警 `alerts_triggered`。

**时间约定**：`reported_at` 统一按 naive UTC 存储（带时区偏移的时间会转换为 UTC，如 `23:00+08:00` 存为 `15:00Z`）；夜间时段按本地时间判定，本地时间 = UTC + `LOCAL_UTC_OFFSET_HOURS`（默认 8，即北京时间）。

**乱序补报**：补报的历史读数会触发其前后窗口的补评估——去噪、功率跳变对所有"已具备评估条件但尚未评估"的读数统一补算，持续高负荷/夜间活跃/疑似窃电对补报点及其后的读数重新评估；告警按（电表, 异常类型）未闭环去重，重复评估不会产生重复告警。

## 异常检测规则

| 异常类型 | 标识 | 规则（阈值均可通过环境变量调整） |
|---|---|---|
| 持续高负荷 | `sustained_high_load` | 连续 3 次功率 ≥ 5kW |
| 夜间异常活跃 | `night_active` | 23:00–06:00 连续 2 次功率 ≥ 1kW |
| 功率跳变 | `power_jump` | 相邻两次功率差 ≥ 3kW（延迟一个采样点评估，先经过去噪） |
| 疑似窃电 | `suspected_theft` | 累计读数回退；或读数增量显著低于功率积分电量 |
| 设备离线 | `device_offline` | 超过 30 分钟未上报（`POST /alerts/scan-offline` 触发扫描） |

**短时离群值去噪**：新读数到达后回溯评估上一条读数，若其两侧邻点均处于正常区间而中间点为
大幅尖峰，则标记为离群点（`is_outlier=true`），后续所有检测忽略该点——单点毛刺不会触发
功率跳变等误报。去噪判定延迟一个采样点完成，因此跳变检测也延迟一个采样点评估。
**若尖峰在被识别为离群点之前已触发告警（如疑似窃电），该告警会自动撤销为「误报」并记录备注。**

## 告警状态机

```
pending(待确认) ──▶ investigating(核查中) ──▶ rectified(已整改)
    │                    │    ▲                    │
    ├─▶ recovered(已恢复) ├─▶ recovered            └──▶ investigating（复发可重开）
    └─▶ false_positive(误报)└─▶ false_positive
```

- 离开 `pending` 时自动记录响应时长 `response_seconds`；进入终态记录 `resolved_at`；
- 从终态重新打开（如已整改 → 核查中）时清空 `resolved_at`，保留首次响应记录；
- 非法流转返回 `40001`；同一电表同一异常类型的未闭环告警自动去重（仅刷新最近检出时间）。

## 统计口径

- 所有统计窗口为 **[now − N天, now]**：未来时间（如设备时钟错误）产生的告警不计入排行、响应时长分布与合规率；
- 高异常房间排行按 **楼栋+房间** 聚合，异常类型分布同样按楼栋隔离，不同楼栋的相同房号互不干扰；
- 合规率按 **房间** 口径统计：同一房间存在多块电表只计一个房间，任一表在周期内有非误报告警则该房间不合规。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/readings` | 电表数据上报（幂等） |
| GET | `/api/v1/readings` | 读数查询（表号/房间/时间范围/分页） |
| GET | `/api/v1/devices` | 设备列表（含在线状态） |
| GET | `/api/v1/alerts` | 告警查询：房间、楼栋、异常类型、状态、时间范围、分页 |
| GET | `/api/v1/alerts/{id}` | 告警详情 |
| POST | `/api/v1/alerts/{id}/status` | 状态流转（可附处置备注） |
| POST | `/api/v1/alerts/scan-offline` | 设备离线扫描 |
| GET | `/api/v1/stats/top-rooms?days=7` | 近 N 天高异常房间排行（含类型分布） |
| GET | `/api/v1/stats/response-time?days=7` | 告警响应时长分布（分桶 + 均值/中位数/最大值） |
| GET | `/api/v1/stats/compliance?days=30` | 房间用电合规率（整体 + 分楼栋） |

## 统一响应

```json
{"code": 0, "message": "ok", "data": {...}}
```

`code=0` 成功；`40000` 参数校验失败、`40001` 非法状态流转、`40401` 资源不存在、`50000` 服务器错误。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./smartmeter.db` | 数据库连接 |
| `LOCAL_UTC_OFFSET_HOURS` | `8` | 本地时区偏移（夜间时段按本地时间判定） |
| `HIGH_POWER_THRESHOLD_KW` / `HIGH_POWER_CONSECUTIVE` | `5.0` / `3` | 持续高负荷 |
| `NIGHT_START_HOUR` / `NIGHT_END_HOUR` / `NIGHT_POWER_THRESHOLD_KW` / `NIGHT_CONSECUTIVE` | `23`/`6`/`1.0`/`2` | 夜间异常 |
| `POWER_JUMP_THRESHOLD_KW` | `3.0` | 功率跳变 |
| `OUTLIER_NEIGHBOR_MAX_KW` / `OUTLIER_MIN_SPIKE_KW` / `OUTLIER_FACTOR` | `2.0`/`3.0`/`5.0` | 离群值去噪 |
| `THEFT_MIN_EXPECTED_KWH` / `THEFT_DROP_TOLERANCE` | `0.5`/`0.6` | 疑似窃电 |
| `OFFLINE_MINUTES` | `30` | 设备离线 |
| `MAX_FUTURE_SKEW_MINUTES` | `5` | 允许的未来时钟偏差 |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_TOPIC` / `MQTT_USERNAME` / `MQTT_PASSWORD` | — | MQTT 接入 |

## 项目结构

```
app/
├── config.py            # 阈值与运行配置（环境变量可覆盖）
├── database.py          # SQLAlchemy 引擎/会话
├── models.py            # Device / MeterReading / Alert
├── schemas.py           # 请求/响应模型
├── constants.py         # 异常类型、状态机流转表
├── response.py          # 统一响应
├── exceptions.py        # 业务异常
├── services/
│   ├── ingest_service.py    # 幂等接收管线
│   ├── detection.py         # 检测引擎（去噪/高负荷/夜间/跳变/窃电）
│   └── alert_service.py     # 告警去重、状态机、离线扫描、自动恢复
├── routers/             # readings / alerts / devices / stats
├── main.py              # FastAPI 应用与全局异常处理
└── mqtt_worker.py       # MQTT 订阅接入（可选）
tests/test_api.py        # 19 个端到端用例
```
