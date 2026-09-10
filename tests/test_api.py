import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def make_reading(meter="M1001", room="301", building="A栋", reading=100.0,
                 power=1.0, ts="2026-09-10T10:00:00", status="normal"):
    return {"meter_no": meter, "room_no": room, "building": building,
            "reading": reading, "power": power, "reported_at": ts,
            "device_status": status}


def post(client, readings):
    resp = client.post("/api/v1/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    return body["data"]


def alerts_of(client, **params):
    resp = client.get("/api/v1/alerts", params=params)
    assert resp.status_code == 200
    return resp.json()["data"]


# ---------- 统一响应与参数校验 ----------

def test_unified_response_shape(client):
    data = post(client, [make_reading()])
    assert set(data.keys()) >= {"accepted", "duplicates", "alerts_triggered"}
    resp = client.get("/api/v1/alerts")
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "ok" and "data" in body


def test_validation_error_returns_unified_shape(client):
    resp = client.post("/api/v1/readings", json={"readings": [{"meter_no": "M1"}]})
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == 40000 and body["data"] is None
    assert "参数校验失败" in body["message"]


# ---------- 幂等接收 ----------

def test_ingest_idempotent(client):
    readings = [make_reading(reading=100 + i, ts=f"2026-09-10T10:0{i}:00") for i in range(3)]
    data = post(client, readings)
    assert data["accepted"] == 3 and data["duplicates"] == 0
    # 重复上报同一批（meter_no + reported_at 相同）不产生新数据
    data2 = post(client, readings)
    assert data2["accepted"] == 0 and data2["duplicates"] == 3
    resp = client.get("/api/v1/readings", params={"meter_no": "M1001"})
    assert resp.json()["data"]["total"] == 3


# ---------- 持续高负荷 ----------

def test_sustained_high_load(client):
    readings = [make_reading(reading=100 + 0.5 * i, power=6.0,
                             ts=f"2026-09-10T10:{i:02d}:00") for i in (0, 5, 10)]
    data = post(client, readings)
    types = [a["anomaly_type"] for a in data["alerts_triggered"]]
    assert "sustained_high_load" in types
    items = alerts_of(client, anomaly_type="sustained_high_load", room_no="301")["items"]
    assert len(items) == 1
    assert items[0]["status"] == "pending"


def test_below_threshold_no_high_load_alert(client):
    readings = [make_reading(reading=100 + 0.1 * i, power=4.0,
                             ts=f"2026-09-10T10:{i:02d}:00") for i in (0, 5, 10)]
    data = post(client, readings)
    assert data["alerts_triggered"] == []


# ---------- 夜间异常活跃 ----------

def test_night_active(client):
    readings = [make_reading(reading=200 + 0.2 * i, power=2.0,
                             ts=f"2026-09-10T01:{i:02d}:00") for i in (0, 5)]
    data = post(client, readings)
    types = [a["anomaly_type"] for a in data["alerts_triggered"]]
    assert "night_active" in types


def test_daytime_same_power_no_night_alert(client):
    readings = [make_reading(reading=200 + 0.2 * i, power=2.0,
                             ts=f"2026-09-10T14:{i:02d}:00") for i in (0, 5)]
    data = post(client, readings)
    assert data["alerts_triggered"] == []


# ---------- 功率跳变与离群值去噪 ----------

def test_power_jump(client):
    readings = [
        make_reading(reading=300.0, power=1.0, ts="2026-09-10T10:00:00"),
        make_reading(reading=300.1, power=5.0, ts="2026-09-10T10:05:00"),
        make_reading(reading=300.5, power=5.2, ts="2026-09-10T10:10:00"),
    ]
    data = post(client, readings)
    types = [a["anomaly_type"] for a in data["alerts_triggered"]]
    assert "power_jump" in types


def test_short_outlier_denoised_no_jump_alert(client):
    """单点尖峰 1.0 -> 9.0 -> 1.0 应被去噪，不触发功率跳变告警。"""
    readings = [
        make_reading(reading=400.0, power=1.0, ts="2026-09-10T10:00:00"),
        make_reading(reading=400.1, power=9.0, ts="2026-09-10T10:05:00"),
        make_reading(reading=400.2, power=1.0, ts="2026-09-10T10:10:00"),
    ]
    data = post(client, readings)
    assert data["alerts_triggered"] == []
    resp = client.get("/api/v1/readings", params={"meter_no": "M1001"})
    items = resp.json()["data"]["items"]
    outliers = [r for r in items if r["is_outlier"]]
    assert len(outliers) == 1 and outliers[0]["power"] == 9.0


# ---------- 疑似窃电 ----------

def test_theft_reading_rollback(client):
    readings = [
        make_reading(reading=500.0, power=1.0, ts="2026-09-10T10:00:00"),
        make_reading(reading=499.0, power=1.0, ts="2026-09-10T10:05:00"),
    ]
    data = post(client, readings)
    types = [a["anomaly_type"] for a in data["alerts_triggered"]]
    assert "suspected_theft" in types


def test_theft_energy_mismatch(client):
    """功率 6kW 持续 1 小时应走约 6 度电，表计只走 0.5 度 -> 疑似窃电。"""
    readings = [
        make_reading(reading=600.0, power=6.0, ts="2026-09-10T10:00:00"),
        make_reading(reading=600.5, power=6.0, ts="2026-09-10T11:00:00"),
    ]
    data = post(client, readings)
    types = [a["anomaly_type"] for a in data["alerts_triggered"]]
    assert "suspected_theft" in types


# ---------- 设备离线 ----------

def test_device_offline_and_auto_recover(client):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    old_ts = (now - timedelta(minutes=60)).isoformat()
    post(client, [make_reading(reading=700.0, power=1.0, ts=old_ts)])

    resp = client.post("/api/v1/alerts/scan-offline")
    assert resp.status_code == 200
    assert resp.json()["data"]["count"] == 1
    alert = resp.json()["data"]["new_alerts"][0]
    assert alert["anomaly_type"] == "device_offline"

    # 设备重新上报 -> 离线告警自动恢复
    post(client, [make_reading(reading=700.1, power=1.0, ts=now.isoformat())])
    detail = client.get(f"/api/v1/alerts/{alert['id']}").json()["data"]
    assert detail["status"] == "recovered"


# ---------- 告警状态机 ----------

def _create_high_load_alert(client):
    readings = [make_reading(reading=100 + 0.5 * i, power=6.0,
                             ts=f"2026-09-10T10:{i:02d}:00") for i in (0, 5, 10)]
    post(client, readings)
    return alerts_of(client, anomaly_type="sustained_high_load")["items"][0]


def test_alert_status_flow(client):
    alert = _create_high_load_alert(client)
    aid = alert["id"]

    resp = client.post(f"/api/v1/alerts/{aid}/status",
                       json={"status": "investigating", "note": "已派单核查"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "investigating"
    assert data["response_seconds"] is not None and data["response_seconds"] >= 0
    assert "已派单核查" in data["note"]

    resp = client.post(f"/api/v1/alerts/{aid}/status", json={"status": "rectified"})
    assert resp.json()["data"]["status"] == "rectified"
    assert resp.json()["data"]["resolved_at"] is not None


def test_invalid_transition_rejected(client):
    alert = _create_high_load_alert(client)
    aid = alert["id"]
    # pending 不能直接到 rectified
    resp = client.post(f"/api/v1/alerts/{aid}/status", json={"status": "rectified"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 40001
    # 终态不可再流转
    client.post(f"/api/v1/alerts/{aid}/status", json={"status": "investigating"})
    client.post(f"/api/v1/alerts/{aid}/status", json={"status": "false_positive"})
    resp = client.post(f"/api/v1/alerts/{aid}/status", json={"status": "investigating"})
    assert resp.status_code == 400


def test_unknown_status_value_rejected(client):
    alert = _create_high_load_alert(client)
    resp = client.post(f"/api/v1/alerts/{alert['id']}/status", json={"status": "closed"})
    assert resp.status_code == 400


# ---------- 查询接口 ----------

def test_alert_query_filters(client):
    # A栋301：高负荷；B栋101：夜间活跃
    post(client, [make_reading(meter="M1", room="301", building="A栋",
                               reading=100 + 0.5 * i, power=6.0,
                               ts=f"2026-09-10T10:{i:02d}:00") for i in (0, 5, 10)])
    post(client, [make_reading(meter="M2", room="101", building="B栋",
                               reading=200 + 0.2 * i, power=2.0,
                               ts=f"2026-09-10T01:{i:02d}:00") for i in (0, 5)])

    assert alerts_of(client, room_no="301")["total"] == 1
    assert alerts_of(client, building="B栋")["total"] == 1
    assert alerts_of(client, anomaly_type="night_active")["total"] == 1
    assert alerts_of(client, anomaly_type="power_jump")["total"] == 0
    # 时间范围过滤
    assert alerts_of(client, start="2026-09-10T09:00:00", end="2026-09-10T11:00:00")["total"] == 1
    assert alerts_of(client, start="2026-09-11T00:00:00")["total"] == 0


# ---------- 统计分析 ----------

def test_stats_top_rooms(client):
    # 301：持续高负荷 + 功率突增（2 条告警）；101：夜间异常活跃（1 条告警）
    post(client, [make_reading(meter="M1", room="301", building="A栋",
                               reading=r, power=p, ts=f"2026-09-10T10:{m:02d}:00")
                  for m, r, p in [(0, 100.0, 3.0), (5, 100.5, 5.0), (10, 101.0, 5.2),
                                  (15, 101.5, 5.2), (20, 102.0, 8.5), (25, 102.5, 8.5)]])
    post(client, [make_reading(meter="M2", room="101", building="A栋",
                               reading=200 + 0.2 * i, power=2.0,
                               ts=f"2026-09-10T01:{i:02d}:00") for i in (0, 5)])
    data = client.get("/api/v1/stats/top-rooms", params={"days": 7}).json()["data"]
    assert data["items"][0]["room_no"] == "301"
    assert data["items"][0]["alert_count"] == 2
    assert "sustained_high_load" in data["items"][0]["type_breakdown"]
    assert data["items"][1]["room_no"] == "101"
    assert data["items"][1]["alert_count"] == 1


def test_stats_response_time(client):
    alert = _create_high_load_alert(client)
    client.post(f"/api/v1/alerts/{alert['id']}/status", json={"status": "investigating"})
    data = client.get("/api/v1/stats/response-time").json()["data"]
    assert data["total_alerts"] == 1
    assert data["distribution"]["<5分钟"] == 1
    assert data["stats"]["responded_count"] == 1
    assert data["stats"]["avg_seconds"] >= 0


def test_stats_compliance(client):
    _create_high_load_alert(client)  # 301 不合规
    post(client, [make_reading(meter="M9", room="999", building="A栋",
                               reading=900.0, power=0.5, ts="2026-09-10T12:00:00")])
    data = client.get("/api/v1/stats/compliance", params={"days": 30}).json()["data"]
    assert data["total_rooms"] == 2
    assert data["compliant_rooms"] == 1
    assert data["compliance_rate"] == 0.5
    assert data["by_building"][0]["building"] == "A栋"
