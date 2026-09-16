#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模擬一台 ESP32 智慧鎖，用來在實體硬體到場之前驗證整條軟體鏈路。

為什麼需要這支：
實體測試最貴的成本是「不知道問題出在韌體、WiFi、broker 還是後端」。這支把
軟體側（App → API → bridge → broker → 裝置 → 狀態回報 → 資料庫）單獨跑通，
真硬體接上來之後，剩下要煩惱的就只有 WiFi、TLS 與 servo。

它確實抓到過東西：`control_commands.status` 永遠停在 PUBLISHED（韌體的
state 不帶 command_id），導致 App 每次控制都會跳 30 秒逾時。那個 bug 沒有
實體裝置也重現得出來。

行為對照 Arduino/MqttSmartLock/MqttSmartLock.ino：

  ✓ topic 命名          home/device/<mac>/{config,cmd,state,ota,event}
  ✓ 註冊封包            {"model": ..., "mac": ...} → home/register
  ✓ 連線後順序          subscribe → register → 回報目前狀態（applyLockState）
  ✓ 一次開機只註冊一次   斷線重連不重送（韌體的 registered 旗標）
  ✓ 狀態封包            {"locked": bool}
  ✓ 事件封包            {"type": "doorbell" | "tamper_detected"}
  ✓ 自動上鎖            解鎖後 auto_lock_sec 秒自動上鎖並再發一次 state
  ✓ config 覆蓋         用 server 回傳的 auto_lock_sec 覆蓋預設值

**沒有**模擬的部分（這些只能用實體裝置驗證）：

  ✗ TLS 8883 + 內嵌 CA + NTP 對時（這支走明文 1883）
  ✗ OTA：韌體下載、SHA-256、Ed25519 驗簽、分區切換
  ✗ 實體世界：servo 卡住、WiFi 掉線、觸控誤觸發、電源不穩

# ponytail: 指令解析用 JSON，韌體是字串比對（先找 "unlock" 再找 "lock"）。
# bridge 送出的一律是格式良好的 {"action": "..."}，兩者結果相同；真要測
# 韌體那套比對的邊界情況（例如 action 值裡同時含兩個字串），得用實機。

用法：
    python fake_device.py <MAC> [broker] [port]

    # 另開一個終端機，模擬按門鈴 / 觸發防拆
    python fake_device.py <MAC> --event doorbell
    python fake_device.py <MAC> --event tamper_detected

MAC 要跟 App 裡「配對裝置」填的識別碼完全一致，否則 bridge 查不到
devices 表的資料列，狀態回報會被丟掉。
"""
import argparse
import json
import sys
import threading
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

DEFAULT_MODEL = "SMART-LOCK-V1"
# 韌體的預設值，會被 server 回傳的 retained config 覆蓋。
DEFAULT_AUTO_LOCK_SEC = 5


class FakeLock:
    def __init__(self, mac: str, broker: str, port: int, model: str):
        self.mac = mac
        self.broker = broker
        self.port = port
        self.model = model

        self.t_config = f"home/device/{mac}/config"
        self.t_cmd = f"home/device/{mac}/cmd"
        self.t_state = f"home/device/{mac}/state"
        self.t_event = f"home/device/{mac}/event"
        self.t_ota = f"home/device/{mac}/ota"

        # 韌體: bool locked = true（models.yaml 的 default_state: locked）
        self.locked = True
        self.auto_lock_sec = DEFAULT_AUTO_LOCK_SEC
        self.unlock_at = None
        # 韌體的 registered 旗標：一次開機只註冊一次，斷線重連不重送
        self.registered = False

        self.client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"esp32-lock-{mac}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        print(f"[fake {self.mac}] {msg}", flush=True)

    # ---------- 狀態控制（對應韌體的 applyLockState）----------
    def apply_lock_state(self, new_locked: bool) -> None:
        with self._lock:
            self.locked = new_locked
            self.unlock_at = time.monotonic() if not new_locked else None
        payload = json.dumps({"locked": new_locked})
        self.client.publish(self.t_state, payload)
        self.log(f"[STATE] {payload}  ({'上鎖' if new_locked else '解鎖'})")

    def publish_event(self, event_type: str) -> None:
        payload = json.dumps({"type": event_type})
        self.client.publish(self.t_event, payload)
        self.log(f"[EVENT] {payload}")

    # ---------- MQTT ----------
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        self.log(f"[MQTT] 連線 {self.broker}:{self.port} ... {rc}")
        client.subscribe(self.t_config)
        client.subscribe(self.t_cmd)
        client.subscribe(self.t_ota)

        if not self.registered:
            reg = json.dumps({"model": self.model, "mac": self.mac})
            client.publish("home/register", reg)
            self.log(f"[REGISTER] {reg}")
            self.registered = True
        else:
            self.log("[REGISTER] 已註冊過，略過")

        # 韌體連上之後會回報一次目前狀態
        self.apply_lock_state(self.locked)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:
            self.log(f"[WARN] 非 JSON: {msg.payload!r}")
            return

        if msg.topic == self.t_config:
            sec = payload.get("auto_lock_sec")
            if isinstance(sec, int) and sec > 0:
                self.auto_lock_sec = sec
            self.log(f"[CONFIG] auto_lock_sec = {self.auto_lock_sec}（註冊成功）")
            return

        if msg.topic == self.t_ota:
            self.log(f"[OTA] 收到更新指令 {payload} —— 這支不模擬 OTA，忽略")
            return

        if msg.topic == self.t_cmd:
            action = str(payload.get("action", "")).lower()
            self.log(f"[CMD] {payload}")
            if action == "unlock":
                time.sleep(0.5)  # 模擬 servo 轉動
                self.apply_lock_state(False)
            elif action == "lock":
                time.sleep(0.5)
                self.apply_lock_state(True)
            elif action == "doorbell_ack":
                self.log("[CMD] 門鈴已確認")
            else:
                self.log(f"[CMD] 未知指令: {action}")

    # ---------- 主迴圈（對應韌體的 loop）----------
    def run(self) -> None:
        self.client.connect(self.broker, self.port, keepalive=30)
        self.client.loop_start()
        self.log("啟動，Ctrl+C 結束")
        try:
            while True:
                with self._lock:
                    should_auto_lock = (
                        not self.locked
                        and self.unlock_at is not None
                        and time.monotonic() - self.unlock_at >= self.auto_lock_sec
                    )
                if should_auto_lock:
                    self.log(f"[AUTO] 超過 {self.auto_lock_sec} 秒，自動上鎖")
                    self.apply_lock_state(True)
                time.sleep(0.1)
        except KeyboardInterrupt:
            self.log("結束")
        finally:
            self.client.loop_stop()
            self.client.disconnect()


def send_one_event(mac: str, broker: str, port: int, event_type: str) -> None:
    """一次性送出事件，用來測 doorbell / tamper 的稽核日誌路徑。"""
    client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id=f"esp32-evt-{mac}")
    client.connect(broker, port, keepalive=10)
    client.loop_start()
    payload = json.dumps({"type": event_type})
    info = client.publish(f"home/device/{mac}/event", payload)
    info.wait_for_publish(timeout=5)
    print(f"[fake {mac}] [EVENT] {payload} 已送出", flush=True)
    client.loop_stop()
    client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="模擬 ESP32 智慧鎖")
    parser.add_argument("mac", help="裝置 MAC，要與 App 配對時填的識別碼一致")
    parser.add_argument("broker", nargs="?", default="localhost")
    parser.add_argument("port", nargs="?", type=int, default=1883)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--event",
        choices=["doorbell", "tamper_detected"],
        help="只送一次事件就結束，不進入常駐模式",
    )
    args = parser.parse_args()

    if args.event:
        send_one_event(args.mac, args.broker, args.port, args.event)
        return

    FakeLock(args.mac, args.broker, args.port, args.model).run()


if __name__ == "__main__":
    sys.exit(main())
