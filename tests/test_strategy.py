"""分层检测策略管理、命中追溯与只读试跑的端到端用例。"""
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


BASE = "/api/v1/detection-strategies"


def make_reading(meter="M1001", room="301", building="A栋", reading=100.0,
                 power=1.0, ts="2026-09-09T10:00:00", status="normal"):
    return {"meter_no": meter, "room_no": room, "building": building,
            "reading": reading, "power": power, "reported_at": ts,
            "device_status": status}


def post_readings(client, readings):
    resp = client.post("/api/v1/readings", json={"readings": readings})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    return body["data"]


def create_strategy(client, **payload):
    resp = client.post(BASE, json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 0
    return body["data"]


def publish(client, sid, by="tester"):
    resp = client.post(f"{BASE}/{sid}/publish", json={"published_by": by})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------- 草稿 CRUD 与参数校验 ----------

def test_strategy_crud_and_validation(client):
    # 非法层级
    resp = client.post(BASE, json={"name": "x", "scope": "city"})
    assert resp.status_code == 400 and resp.json()["code"] == 40000
    # 楼栋级缺 building
    resp = client.post(BASE, json={"name": "楼栋策略", "scope": "building"})
    assert resp.status_code == 400 and resp.json()["code"] == 40000
    # 未知异常类型
    resp = client.post(BASE, json={"name": "g", "scope": "global",
                                   "rules": {"not_a_type": {"x": 1}}})
    assert resp.status_code == 400 and resp.json()["code"] == 40000
    # 非法取值范围（drop_tolerance 必须 0-1）
    resp = client.post(BASE, json={
        "name": "g", "scope": "global",
        "rules": {"suspected_theft": {"drop_tolerance": 2.0}}})
    assert resp.status_code == 400 and resp.json()["code"] == 40000
    # 连续次数必须 >=1
    resp = client.post(BASE, json={
        "name": "g", "scope": "global",
        "rules": {"sustained_high_load": {"consecutive": 0}}})
    assert resp.status_code == 400 and resp.json()["code"] == 40000

    s = create_strategy(client, name="全局默认", scope="global")
    assert s["is_published"] is False
    assert s["has_unpublished_changes"] is True
    # 未提供的规则全部以系统默认补齐
    assert s["rules"]["sustained_high_load"]["threshold_kw"] == 5.0
    assert s["rules"]["device_offline"]["offline_minutes"] == 30

    # 同一作用域重复创建被拒绝
    resp = client.post(BASE, json={"name": "全局默认2", "scope": "global"})
    assert resp.status_code == 400 and resp.json()["code"] == 40000

    # 编辑草稿
    resp = client.put(f"{BASE}/{s['id']}",
                      json={"rules": {"sustained_high_load": {
                          "threshold_kw": 3.5, "consecutive": 2}}})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["rules"]["sustained_high_load"]["threshold_kw"] == 3.5
    # 部分覆盖不影响其他异常默认值
    assert data["rules"]["power_jump"]["threshold_kw"] == 3.0

    # 查询接口
    listed = client.get(BASE, params={"scope": "global"}).json()["data"]
    assert listed["total"] == 1 and listed["items"][0]["id"] == s["id"]
    assert client.get(BASE, params={"scope": "meter"}).json()["data"]["total"] == 0

    # 404
    assert client.get(f"{BASE}/9999").status_code == 404
    assert client.get(f"{BASE}/9999").json()["code"] == 40401


# ---------- 版本发布与不可变 ----------

def test_publish_creates_immutable_incrementing_versions(client):
    s = create_strategy(client, name="全局", scope="global",
                        rules={"power_jump": {"threshold_kw": 4.0}})
    v1 = publish(client, s["id"])
    assert v1["version"] == 1 and v1["immutable"] is True
    assert v1["rules"]["power_jump"]["threshold_kw"] == 4.0

    # 发布后修改草稿不改变已发布版本
    client.put(f"{BASE}/{s['id']}",
               json={"rules": {"power_jump": {"threshold_kw": 1.5}}})
    detail = client.get(f"{BASE}/{s['id']}").json()["data"]
    assert detail["has_unpublished_changes"] is True

    versions = client.get(f"{BASE}/{s['id']}/versions").json()["data"]["items"]
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["rules"]["power_jump"]["threshold_kw"] == 4.0

    # 再次发布产生 v2，v1 保持原样
    v2 = publish(client, s["id"], by="ops")
    assert v2["version"] == 2 and v2["published_by"] == "ops"
    versions = client.get(f"{BASE}/{s['id']}/versions").json()["data"]["items"]
    assert [v["version"] for v in versions] == [2, 1]
    v1_db = next(v for v in versions if v["version"] == 1)
    assert v1_db["rules"]["power_jump"]["threshold_kw"] == 4.0


def test_unpublished_draft_does_not_affect_detection(client):
    """草稿发布前不生效：默认阈值 5kW 仍需 3 次，4.5kW 不触发。"""
    create_strategy(client, name="全局草稿", scope="global",
                    rules={"sustained_high_load": {"threshold_kw": 4.0,
                                                   "consecutive": 2}})
    data = post_readings(client, [
        make_reading(reading=100 + 0.1 * i, power=4.5,
                     ts=f"2026-09-09T10:{m:02d}:00")
        for i, m in enumerate((0, 5, 10))])
    assert data["alerts_triggered"] == []


# ---------- 四级优先级 ----------

def test_meter_level_overrides_global(client):
    g = create_strategy(client, name="全局", scope="global",
                        rules={"sustained_high_load": {"threshold_kw": 5.0,
                                                       "consecutive": 3}})
    publish(client, g["id"])
    m = create_strategy(client, name="电表专项", scope="meter", meter_no="M1001",
                        rules={"sustained_high_load": {"threshold_kw": 4.0,
                                                       "consecutive": 2}})
    publish(client, m["id"])

    data = post_readings(client, [
        make_reading(meter="M1001", reading=100 + 0.1 * i, power=4.5,
                     ts=f"2026-09-09T10:{mm:02d}:00")
        for i, mm in enumerate((0, 5))])
    assert [a["anomaly_type"] for a in data["alerts_triggered"]] == \
           ["sustained_high_load"]

    # 同房间的另一块表不命中电表级策略 -> 全局 5kW/3 次不触发
    data2 = post_readings(client, [
        make_reading(meter="M2002", reading=100 + 0.1 * i, power=4.5,
                     ts=f"2026-09-09T10:{mm:02d}:00")
        for i, mm in enumerate((0, 5))])
    assert data2["alerts_triggered"] == []

    # 选择链应显示 meter 命中
    chain = client.get(f"{BASE}/effective", params={"meter_no": "M1001"}).json()["data"]
    assert chain["selected_scope"] == "meter"
    assert chain["selected_version"] == 1
    meter_entry = next(c for c in chain["chain"] if c["scope"] == "meter")
    assert meter_entry["configured"] and meter_entry["in_effective_period"] is True


def test_priority_order_meter_room_building_global(client):
    # 楼栋 2kW/2、房间 1kW/2、电表 6kW/2：最终以电表为准，4.5kW 不触发
    b = create_strategy(client, name="楼栋", scope="building", building="A栋",
                        rules={"sustained_high_load": {"threshold_kw": 2.0,
                                                       "consecutive": 2}})
    publish(client, b["id"])
    r = create_strategy(client, name="房间", scope="room", building="A栋",
                        room_no="301",
                        rules={"sustained_high_load": {"threshold_kw": 1.0,
                                                       "consecutive": 2}})
    publish(client, r["id"])
    m = create_strategy(client, name="电表", scope="meter", meter_no="M1001",
                        rules={"sustained_high_load": {"threshold_kw": 6.0,
                                                       "consecutive": 2}})
    publish(client, m["id"])

    data = post_readings(client, [
        make_reading(power=4.5, ts=f"2026-09-09T10:{mm:02d}:00")
        for mm in (0, 5)])
    assert data["alerts_triggered"] == []

    # 停用电表策略 -> 回退到房间级 1kW
    client.post(f"{BASE}/{m['id']}/enabled", json={"enabled": False})
    data = post_readings(client, [
        make_reading(reading=102 + 0.1 * i, power=4.5,
                     ts=f"2026-09-09T11:{mm:02d}:00")
        for i, mm in enumerate((0, 5))])
    assert any(a["anomaly_type"] == "sustained_high_load"
               for a in data["alerts_triggered"])


def test_fallback_through_levels_when_outside_period(client):
    """电表级策略仅在周六生效；工作日读数应回退到全局策略。"""
    g = create_strategy(client, name="全局", scope="global",
                        rules={"sustained_high_load": {"threshold_kw": 5.0,
                                                       "consecutive": 3}})
    publish(client, g["id"])
    # 2026-09-09 是周三（isoweekday=3）
    m = create_strategy(
        client, name="电表周末", scope="meter", meter_no="M1001",
        rules={"sustained_high_load": {"threshold_kw": 1.0, "consecutive": 2}},
        effective_periods=[{"days_of_week": [6, 7], "start": "00:00", "end": "23:59"}])
    publish(client, m["id"])

    data = post_readings(client, [
        make_reading(reading=100 + 0.1 * i, power=2.0,
                     ts=f"2026-09-09T10:{mm:02d}:00")
        for i, mm in enumerate((0, 5))])
    assert data["alerts_triggered"] == []

    chain = client.get(f"{BASE}/effective",
                       params={"meter_no": "M1001",
                               "at": "2026-09-09T10:00:00"}).json()["data"]
    meter_entry = next(c for c in chain["chain"] if c["scope"] == "meter")
    assert meter_entry["in_effective_period"] is False
    assert chain["selected_scope"] == "global"
    assert chain["selected_is_default"] is False


def test_disable_anomaly_type_in_policy(client):
    s = create_strategy(client, name="全局", scope="global",
                        rules={"power_jump": {"enabled": False}})
    publish(client, s["id"])
    data = post_readings(client, [
        make_reading(reading=300.0, power=1.0, ts="2026-09-09T10:00:00"),
        make_reading(reading=300.1, power=5.0, ts="2026-09-09T10:05:00"),
        make_reading(reading=300.5, power=5.2, ts="2026-09-09T10:10:00"),
    ])
    assert not any(a["anomaly_type"] == "power_jump"
                   for a in data["alerts_triggered"])


def test_enable_disable_strategy_takes_effect(client):
    s = create_strategy(client, name="全局宽松", scope="global",
                        rules={"sustained_high_load": {"threshold_kw": 4.0,
                                                       "consecutive": 2}})
    publish(client, s["id"])
    client.post(f"{BASE}/{s['id']}/enabled", json={"enabled": False})
    data = post_readings(client, [
        make_reading(reading=100 + 0.1 * i, power=4.5,
                     ts=f"2026-09-09T10:{mm:02d}:00")
        for i, mm in enumerate((0, 5))])
    assert data["alerts_triggered"] == []
    client.post(f"{BASE}/{s['id']}/enabled", json={"enabled": True})
    data = post_readings(client, [
        make_reading(reading=101 + 0.1 * i, power=4.5,
                     ts=f"2026-09-09T11:{mm:02d}:00")
        for i, mm in enumerate((0, 5))])
    assert any(a["anomaly_type"] == "sustained_high_load"
               for a in data["alerts_triggered"])


# ---------- 命中追溯快照 ----------

def test_alert_persists_hit_policy_snapshot(client):
    m = create_strategy(client, name="电表专项", scope="meter", meter_no="M1001",
                        rules={"power_jump": {"threshold_kw": 2.5}})
    publish(client, m["id"])
    post_readings(client, [
        make_reading(reading=300.0, power=1.0, ts="2026-09-09T10:00:00"),
        make_reading(reading=300.1, power=5.0, ts="2026-09-09T10:05:00"),
        make_reading(reading=300.5, power=5.2, ts="2026-09-09T10:10:00"),
    ])
    items = client.get("/api/v1/alerts",
                       params={"anomaly_type": "power_jump"}).json()["data"]["items"]
    assert len(items) == 1
    alert = items[0]
    assert alert["strategy_scope"] == "meter"
    snap = alert["strategy_snapshot"]
    assert snap["rule"]["threshold_kw"] == 2.5
    assert snap["version"] == 1
    assert snap["strategy_name"] == "电表专项"
    assert snap["scope_target"] == {"meter_no": "M1001"}

    # 策略变更并重新发布 -> 历史告警快照不变
    client.put(f"{BASE}/{m['id']}",
               json={"rules": {"power_jump": {"threshold_kw": 8.0}}})
    publish(client, m["id"])
    detail = client.get(f"/api/v1/alerts/{alert['id']}").json()["data"]
    assert detail["strategy_snapshot"]["rule"]["threshold_kw"] == 2.5
    assert detail["strategy_version_id"] == snap["strategy_version_id"]


def test_default_policy_alert_has_snapshot(client):
    """未配置任何策略时，告警仍记录完整快照（标记为系统默认）。"""
    post_readings(client, [
        make_reading(reading=100 + 0.5 * i, power=6.0,
                     ts=f"2026-09-09T10:{mm:02d}:00")
        for i, mm in enumerate((0, 5, 10))])
    alert = client.get("/api/v1/alerts").json()["data"]["items"][0]
    snap = alert["strategy_snapshot"]
    assert snap is not None
    assert snap["is_default"] is True
    assert snap["rule"]["threshold_kw"] == 5.0
    assert snap["rule"]["consecutive"] == 3
    assert alert["strategy_scope"] == "global"


# ---------- 离线策略走分层 ----------

def test_offline_scan_uses_policy_threshold(client):
    from datetime import datetime, timedelta, timezone
    s = create_strategy(client, name="全局离线", scope="global",
                        rules={"device_offline": {"offline_minutes": 10}})
    publish(client, s["id"])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    post_readings(client, [make_reading(reading=700.0, power=1.0,
                                        ts=(now - timedelta(minutes=20)).isoformat())])
    resp = client.post("/api/v1/alerts/scan-offline")
    assert resp.json()["data"]["count"] == 1
    alert = resp.json()["data"]["new_alerts"][0]
    assert alert["strategy_snapshot"]["rule"]["offline_minutes"] == 10

    # 停用离线检测 -> 扫描不再产生新告警（恢复后扫描）
    client.post(f"/api/v1/alerts/{alert['id']}/status",
                json={"status": "false_positive"})
    client.put(f"{BASE}/{s['id']}",
               json={"rules": {"device_offline": {"enabled": False}}})
    publish(client, s["id"])
    assert client.post("/api/v1/alerts/scan-offline").json()["data"]["count"] == 0


# ---------- 只读试跑 ----------

def test_trial_run_readonly_and_reuses_pipeline(client):
    # 历史数据：低基线上的单点尖峰（1.0 -> 10.0 -> 1.0，应被去噪）+ 缓慢爬升后
    # 连续高负荷；读数严格按功率积分推进，不产生疑似窃电；最大相邻变化 2kW，
    # 不触发功率跳变。
    powers = [1.0, 10.0, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0]
    readings, total = [], 0.0
    for i, power in enumerate(powers):
        if i > 0:
            total += (powers[i - 1] + power) / 2 * (5 / 60)
        readings.append(make_reading(reading=400.0 + total, power=power,
                                     ts=f"2026-09-09T10:{i*5:02d}:00"))
    post_readings(client, readings)
    before_alerts = client.get("/api/v1/alerts").json()["data"]["total"]
    before_readings = client.get("/api/v1/readings",
                                 params={"meter_no": "M1001"}).json()["data"]["total"]

    resp = client.post(f"{BASE}/trial-run", json={
        "meter_no": "M1001",
        "start": "2026-09-09T10:00:00", "end": "2026-09-09T10:35:00"})
    assert resp.status_code == 200, resp.text
    result = resp.json()["data"]
    assert result["readings_in_range"] == 8
    assert result["outliers_marked"] == 1
    types = [a["anomaly_type"] for a in result["predicted_anomalies"]]
    assert types == ["sustained_high_load"]
    hit = result["predicted_anomalies"][0]
    assert hit["hit_strategy"]["is_default"] is True
    assert "阈值" in hit["evidence"]

    # 只读：正式告警数量、读数数量与设备状态均不变
    assert client.get("/api/v1/alerts").json()["data"]["total"] == before_alerts
    assert client.get("/api/v1/readings",
                      params={"meter_no": "M1001"}).json()["data"]["total"] \
           == before_readings
    dev = client.get("/api/v1/devices").json()["data"]["items"][0]
    assert dev["status"] == "normal"


def test_trial_run_uses_published_policy_and_range_filter(client):
    post_readings(client, [
        make_reading(reading=100.0, power=4.5, ts="2026-09-09T10:00:00"),
        make_reading(reading=100.1, power=4.5, ts="2026-09-09T10:05:00"),
        make_reading(reading=100.2, power=4.5, ts="2026-09-09T10:10:00"),
    ])
    m = create_strategy(client, name="电表", scope="meter", meter_no="M1001",
                        rules={"sustained_high_load": {"threshold_kw": 4.0,
                                                       "consecutive": 2}})
    publish(client, m["id"])

    result = client.post(f"{BASE}/trial-run", json={
        "meter_no": "M1001",
        "start": "2026-09-09T10:09:00", "end": "2026-09-09T10:11:00"}).json()["data"]
    # 命中时刻 10:05 落在范围外 -> 范围过滤后不返回，但回放仍使用全部历史做上下文
    assert result["readings_replayed"] == 3
    assert all(a["anomaly_type"] != "sustained_high_load"
               for a in result["predicted_anomalies"])

    full = client.post(f"{BASE}/trial-run", json={
        "meter_no": "M1001",
        "start": "2026-09-09T10:00:00", "end": "2026-09-09T10:10:00"}).json()["data"]
    hit = next(a for a in full["predicted_anomalies"]
               if a["anomaly_type"] == "sustained_high_load")
    assert hit["hit_strategy"]["scope"] == "meter"
    assert hit["hit_strategy"]["rule"]["threshold_kw"] == 4.0


def test_trial_run_validates_inputs(client):
    # 电表不存在
    resp = client.post(f"{BASE}/trial-run", json={
        "meter_no": "NOPE", "start": "2026-09-09T10:00:00",
        "end": "2026-09-09T10:10:00"})
    assert resp.status_code == 404 and resp.json()["code"] == 40401

    post_readings(client, [make_reading()])
    # 范围内无读数
    resp = client.post(f"{BASE}/trial-run", json={
        "meter_no": "M1001", "start": "2020-01-01T00:00:00",
        "end": "2020-01-02T00:00:00"})
    assert resp.status_code == 404 and resp.json()["code"] == 40401
    # start > end
    resp = client.post(f"{BASE}/trial-run", json={
        "meter_no": "M1001", "start": "2026-09-09T11:00:00",
        "end": "2026-09-09T10:00:00"})
    assert resp.status_code == 400 and resp.json()["code"] == 40000


def test_unified_response_shape_for_strategy_apis(client):
    for resp in (client.post(BASE, json={"name": "g", "scope": "global"}),
                 client.get(BASE),
                 client.post(f"{BASE}/trial-run",
                             json={"meter_no": "X", "start": "bad",
                                   "end": "2026-01-01T00:00:00"})):
        body = resp.json()
        assert set(body.keys()) == {"code", "message", "data"}
