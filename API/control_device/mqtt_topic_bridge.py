#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MQTT topic bridge between the API's assumed convention and the convention
actually used by mqtt-server/ + the real ESP32 firmware (Arduino/MqttSmartLock).

This file is new and does NOT modify control_device.py or
mqtt_status_worker.py. It exists because:

- control_device.py (CONTROL_MODE=mqtt) publishes commands to
  home/{family_id}/device/{device_id}/cmd with an uppercase action
  (e.g. "UNLOCK"). The real ESP32 firmware only listens on
  home/device/<mac>/cmd and only understands a lowercase
  {"action": "lock" | "unlock"}.
- The real ESP32 firmware reports lock state on home/device/<mac>/state as
  {"locked": bool}, and doorbell/tamper events on home/device/<mac>/event.
  device_status_update.handle_status() (existing, unmodified) expects
  {"family_id", "device_id", "status", "physical_state", ...}.

This bridge subscribes to both worlds' topics and translates between them,
calling the existing device_status_update.handle_status() directly instead
of re-publishing to home/{family_id}/device/{device_id}/status.

It also runs a background sweep (UC5.1) that auto-reverts devices out of
maintenance mode once their maintenance_expires_at has passed, since this is
the one long-lived API process available to do that without a dedicated
scheduler — see sweep_expired_maintenance().

Run as its own long-lived process (see docker-compose service api-mqtt-bridge).
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymysql
import paho.mqtt.client as mqtt

sys.path.insert(0, str(Path(__file__).parent))
import device_status_update  # existing, unmodified API module
import mqtt_tls

ACTION_MAP = {"LOCK": "lock", "UNLOCK": "unlock"}
MAINTENANCE_SWEEP_INTERVAL_SECONDS = int(os.getenv("MAINTENANCE_SWEEP_INTERVAL_SECONDS", "60"))

# 指令與狀態回報的關聯設定，見 lookup_pending_command()。
# 窗口要比 App 的輪詢上限（30 秒）寬，讓裝置慢一點回報也還關聯得到，
# 但不能長到把昨天的殘留指令也算進來。
CORRELATION_WINDOW_SEC = int(os.getenv("COMMAND_CORRELATION_WINDOW_SEC", "120"))
EXPECTED_STATE_BY_ACTION = {"LOCK": "LOCKED", "UNLOCK": "UNLOCKED"}
# 對付「先 publish 後 commit」的競態，見 lookup_pending_command()。
# 最壞情況多花 3 × 0.3 = 0.9 秒，只發生在真的沒有待處理指令時（例如現場手動
# 操作門鎖），不影響一般控制流程。
CORRELATION_RETRIES = int(os.getenv("COMMAND_CORRELATION_RETRIES", "3"))
CORRELATION_RETRY_DELAY_SEC = float(os.getenv("COMMAND_CORRELATION_RETRY_DELAY_SEC", "0.3"))


def get_db_connection():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", os.getenv("MYSQL_USER", "vboxuser")),
        password=os.getenv("DB_PASSWORD", os.getenv("DB_PASS", os.getenv("MYSQL_PASSWORD", ""))),
        database=os.getenv("DB_NAME", os.getenv("MYSQL_DATABASE", "devicemanagement")),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def lookup_family_id(device_id: str) -> Optional[int]:
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT family_id FROM devices WHERE device_id=%s", (device_id,))
                row = cur.fetchone()
                return int(row["family_id"]) if row and row.get("family_id") is not None else None
        finally:
            conn.close()
    except Exception as exc:
        print(f"[bridge] DB lookup failed for {device_id}: {exc}", file=sys.stderr)
        return None


def write_event_audit(device_id: str, family_id: Optional[int], event_type: str) -> None:
    """doorbell / tamper_detected has no equivalent in the API's UC set yet,
    so record it directly in audit_logs using the same hash-chain style the
    rest of API/ already uses."""
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_hash FROM audit_logs ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                prev_hash = row["current_hash"] if row and row.get("current_hash") else "0" * 64
                timestamp = int(time.time())
                command_id = f"EVT_{timestamp}_{device_id}"
                raw = json.dumps({
                    "command_id": command_id, "device_id": device_id, "family_id": family_id,
                    "action": "DEVICE_EVENT", "event_type": event_type, "timestamp": timestamp,
                    "prev_hash": prev_hash,
                }, sort_keys=True)
                current_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                cur.execute(
                    """
                    INSERT INTO audit_logs
                      (command_id, actor_id, actor_type, device_id, family_id, action,
                       parameters, status, decision, reason, prev_hash, current_hash, hash, timestamp)
                    VALUES (%s, %s, 'DEVICE', %s, %s, 'DEVICE_EVENT', CAST(%s AS JSON),
                            'SUCCEEDED', 'ALLOW', %s, %s, %s, %s, %s)
                    """,
                    (command_id, device_id, device_id, family_id,
                     json.dumps({"event_type": event_type}), event_type, prev_hash,
                     current_hash, current_hash, timestamp),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[bridge] failed to write event audit for {device_id}: {exc}", file=sys.stderr)


def sweep_expired_maintenance() -> None:
    """UC5.1: force any device whose maintenance_expires_at has passed back into
    normal mode, and record the auto-revert in audit_logs. Runs on a timer from
    main() for as long as this process is alive."""
    try:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT device_id, family_id FROM devices "
                    "WHERE maintenance_mode = 1 AND maintenance_expires_at IS NOT NULL "
                    "AND maintenance_expires_at <= NOW()"
                )
                expired = cur.fetchall()
                for row in expired:
                    device_id = row["device_id"]
                    family_id = row.get("family_id")
                    cur.execute(
                        """
                        UPDATE devices
                        SET maintenance_mode = 0, maintenance_expires_at = NULL, maintenance_reason = NULL,
                            last_action = 'MAINTENANCE_MODE_AUTO_EXPIRED'
                        WHERE device_id = %s
                        """,
                        (device_id,),
                    )
                    cur.execute("SELECT current_hash FROM audit_logs ORDER BY id DESC LIMIT 1")
                    prev_row = cur.fetchone()
                    prev_hash = prev_row["current_hash"] if prev_row and prev_row.get("current_hash") else "0" * 64
                    timestamp = int(time.time())
                    command_id = f"MTN_AUTO_{timestamp}_{device_id}"
                    raw = json.dumps({
                        "command_id": command_id, "device_id": device_id, "family_id": family_id,
                        "action": "MAINTENANCE_MODE_AUTO_EXPIRED", "timestamp": timestamp,
                        "prev_hash": prev_hash,
                    }, sort_keys=True)
                    current_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                    cur.execute(
                        """
                        INSERT INTO audit_logs
                          (command_id, actor_id, actor_type, device_id, family_id, action,
                           parameters, status, decision, reason, prev_hash, current_hash, hash, timestamp)
                        VALUES (%s, %s, 'SYSTEM', %s, %s, 'MAINTENANCE_MODE_AUTO_EXPIRED', CAST(%s AS JSON),
                                'Restored', 'ALLOW', %s, %s, %s, %s, %s)
                        """,
                        (command_id, "mqtt_topic_bridge", device_id, family_id,
                         json.dumps({}), "MAINTENANCE_WINDOW_EXPIRED", prev_hash,
                         current_hash, current_hash, timestamp),
                    )
                    print(f"[bridge] maintenance window expired for {device_id}, auto-restored")
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"[bridge] maintenance sweep failed: {exc}", file=sys.stderr)


def maintenance_sweep_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        sweep_expired_maintenance()
        stop_event.wait(MAINTENANCE_SWEEP_INTERVAL_SECONDS)


def topic_parts(topic: str) -> List[str]:
    return topic.split("/")


def handle_api_cmd(client: mqtt.Client, topic: str, payload: Dict[str, Any]) -> None:
    # home/{family_id}/device/{device_id}/cmd  (published by control_device.py)
    p = topic_parts(topic)
    if len(p) != 5 or p[0] != "home" or p[2] != "device" or p[4] != "cmd":
        return
    device_id = p[3]
    action = str(payload.get("action", "")).strip().upper()
    mapped = ACTION_MAP.get(action)
    if not mapped:
        print(f"[bridge] {device_id}: action '{action}' not supported by firmware, dropped")
        return
    real_topic = f"home/device/{device_id}/cmd"
    client.publish(real_topic, json.dumps({"action": mapped}))
    print(f"[bridge] {topic} action={action} -> {real_topic} action={mapped}")


def lookup_pending_command(device_id: str, expected_state: str) -> Optional[str]:
    """找出這筆狀態回報對應的指令 command_id。

    韌體的 home/device/<mac>/state 只有 {"locked": bool}，**沒有 command_id**
    （見 Arduino/MqttSmartLock 的 publishState）。而
    device_status_update.handle_status() 是 `if command_id:` 才會去更新
    control_commands。兩者相接的結果是：指令送出後永遠停在 PUBLISHED、
    completed_at 永遠是 NULL —— 即使裝置確實動作了。

    對 App 來說這等於「每一次控制都失敗」：輪詢看不到狀態離開 PUBLISHED，
    最後一定跳逾時訊息。實測（模擬 ESP32）確認過這個行為。

    這裡做關聯：取這台裝置最近一筆還沒完成、且期望結果與回報狀態相符的指令。

    - 只看 PUBLISHED / ACCEPTED，已完成的不再回頭改
    - 限制 CORRELATION_WINDOW_SEC 之內，避免把很久以前的殘留指令
      誤標成完成（例如裝置離線兩天後才上線回報）
    - 比對期望狀態：UNLOCK 對應 UNLOCKED、LOCK 對應 LOCKED。使用者在裝置端
      手動轉動門鎖時，實體狀態會與待處理指令相反，那筆指令不該被標記成完成

    重試的原因是一個競態：control_device.py 用 autocommit=False，**先 publish
    到 MQTT、後 commit**（publish 在 _publish_command，commit 在 main 尾端）。
    裝置回應得比 commit 快時，這裡就查不到那筆指令 —— 實測模擬 ESP32
    （0.5 秒回應）100% 撞到，真實 ESP32 轉動 servo 約 1 秒也在風險範圍。

    # ponytail: 短重試而非改 control_device 的交易邊界 —— 指令列與稽核鏈寫入
    # 在同一個交易裡，把 commit 提前到 publish 之前才是根本解，但那會拆開
    # 兩者的原子性，要連同稽核鏈的寫入順序一起重新設計。真的要做時，正確的
    # 順序是「先 commit 指令列（狀態 PENDING）→ publish → 再更新為 PUBLISHED」。
    """
    for attempt in range(CORRELATION_RETRIES):
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT command_id, action
                        FROM control_commands
                        WHERE device_id = %s
                          AND status IN ('PUBLISHED', 'ACCEPTED')
                          AND created_at >= (NOW() - INTERVAL %s SECOND)
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (device_id, CORRELATION_WINDOW_SEC),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception as exc:
            print(f"[bridge] pending command lookup failed for {device_id}: {exc}", file=sys.stderr)
            return None

        if row:
            action = str(row.get("action") or "").upper()
            if EXPECTED_STATE_BY_ACTION.get(action, expected_state) != expected_state:
                # 待處理的是 LOCK 但裝置回報 UNLOCKED（或反之）：
                # 多半是有人在現場手動操作，不是這筆指令的結果。
                return None
            return str(row["command_id"])

        if attempt < CORRELATION_RETRIES - 1:
            time.sleep(CORRELATION_RETRY_DELAY_SEC)

    # 查不到對應指令是正常情況：使用者在現場手動轉動門鎖、或裝置開機回報初始
    # 狀態時，本來就沒有指令在等。狀態照樣會寫進 device_telemetry。
    return None


def handle_device_state(topic: str, payload: Dict[str, Any]) -> None:
    # home/device/<mac>/state  (published by the real ESP32 firmware)
    p = topic_parts(topic)
    if len(p) != 4 or p[1] != "device" or p[3] != "state":
        return
    device_id = p[2]
    family_id = lookup_family_id(device_id)
    if family_id is None:
        print(f"[bridge] {device_id}: not paired (no row in devices table), skipping state update")
        return
    locked = payload.get("locked")
    physical_state = "LOCKED" if locked else "UNLOCKED"
    status_payload = {
        "family_id": family_id,
        "device_id": device_id,
        "status": "SUCCEEDED",
        "physical_state": physical_state,
    }
    # 韌體不帶 command_id，這裡補上去，否則 control_commands 永遠不會離開
    # PUBLISHED，App 的輪詢一定逾時。
    command_id = lookup_pending_command(device_id, physical_state)
    if command_id:
        status_payload["command_id"] = command_id
        print(f"[bridge] {device_id}: state {physical_state} -> 關聯指令 {command_id}")
    try:
        result = device_status_update.handle_status(status_payload)
        print(f"[bridge] {topic} -> device_status_update: {result}")
    except Exception as exc:
        print(f"[bridge] device_status_update failed for {device_id}: {exc}", file=sys.stderr)


def handle_device_event(topic: str, payload: Dict[str, Any]) -> None:
    # home/device/<mac>/event  (doorbell / tamper_detected)
    p = topic_parts(topic)
    if len(p) != 4 or p[1] != "device" or p[3] != "event":
        return
    device_id = p[2]
    family_id = lookup_family_id(device_id)
    event_type = payload.get("type", "unknown")
    write_event_audit(device_id, family_id, event_type)
    print(f"[bridge] {topic} event={event_type} -> audit_logs (family_id={family_id})")


def main() -> None:
    host = os.getenv("MQTT_HOST", "localhost")
    port = mqtt_tls.broker_port()

    client = mqtt.Client(client_id="api-mqtt-topic-bridge")
    mqtt_tls.apply_tls(client)

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print(f"[bridge] connected to {host}:{port}")
            client.subscribe("home/+/device/+/cmd", qos=1)
            client.subscribe("home/device/+/state", qos=1)
            client.subscribe("home/device/+/event", qos=1)
        else:
            print(f"[bridge] MQTT connect failed rc={rc}", file=sys.stderr)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            print(f"[bridge] bad payload on {msg.topic}: {exc}", file=sys.stderr)
            return

        p = topic_parts(msg.topic)
        try:
            if len(p) == 5 and p[2] == "device" and p[4] == "cmd":
                handle_api_cmd(client, msg.topic, payload)
            elif len(p) == 4 and p[1] == "device" and p[3] == "state":
                handle_device_state(msg.topic, payload)
            elif len(p) == 4 and p[1] == "device" and p[3] == "event":
                handle_device_event(msg.topic, payload)
        except Exception as exc:
            print(f"[bridge] error handling {msg.topic}: {exc}", file=sys.stderr)

    client.on_connect = on_connect
    client.on_message = on_message

    running = True
    maintenance_stop = threading.Event()
    maintenance_thread = threading.Thread(
        target=maintenance_sweep_loop, args=(maintenance_stop,), daemon=True
    )

    def stop(_signum, _frame):
        nonlocal running
        running = False
        maintenance_stop.set()
        client.disconnect()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    client.connect(host, port, keepalive=30)
    client.loop_start()
    maintenance_thread.start()
    print(f"[bridge] maintenance-mode sweep running every {MAINTENANCE_SWEEP_INTERVAL_SECONDS}s")
    try:
        while running:
            signal.pause()
    except AttributeError:
        while running:
            time.sleep(1)
    finally:
        maintenance_stop.set()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
