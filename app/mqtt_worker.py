"""MQTT 数据接入 worker（可选）。

订阅智能电表 MQTT 主题，将消息转入与 HTTP 上报完全相同的幂等接收与检测管线。

运行方式：
    MQTT_ENABLED=true MQTT_HOST=broker.local python -m app.mqtt_worker

消息格式（JSON，与 HTTP 单条上报一致）：
    topic: smartmeter/<meter_no>/reading
    {"meter_no":"M1001","room_no":"301","building":"A栋","reading":1234.5,
     "power":2.1,"reported_at":"2026-09-10T08:00:00","device_status":"normal"}
"""
import json
import logging
import os
import sys

from .database import SessionLocal
from .schemas import ReadingIn
from .services import ingest_service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mqtt_worker")

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None


def handle_payload(payload: bytes) -> None:
    try:
        item = ReadingIn.model_validate(json.loads(payload.decode("utf-8")))
    except Exception as e:
        log.warning("非法消息已丢弃: %s", e)
        return
    db = SessionLocal()
    try:
        result = ingest_service.ingest_readings(db, [item])
        log.info("meter=%s accepted=%s duplicates=%s alerts=%s",
                 item.meter_no, result["accepted"], result["duplicates"],
                 len(result["alerts_triggered"]))
    except Exception:
        db.rollback()
        log.exception("消息处理失败 meter=%s", item.meter_no)
    finally:
        db.close()


def main() -> int:
    if mqtt is None:
        log.error("未安装 paho-mqtt，请执行 pip install paho-mqtt")
        return 1

    host = os.getenv("MQTT_HOST", "127.0.0.1")
    port = int(os.getenv("MQTT_PORT", "1883"))
    topic = os.getenv("MQTT_TOPIC", "smartmeter/+/reading")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    username = os.getenv("MQTT_USERNAME")
    if username:
        client.username_pw_set(username, os.getenv("MQTT_PASSWORD", ""))

    def on_connect(c, userdata, flags, reason_code, properties=None):
        log.info("MQTT 已连接 %s:%s rc=%s，订阅 %s", host, port, reason_code, topic)
        c.subscribe(topic)

    def on_message(c, userdata, msg):
        handle_payload(msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, port, keepalive=60)
    client.loop_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
