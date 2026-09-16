from __future__ import annotations

import base64
import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from memco.config import UNLIMITED, MemoryConfig, try_limit


class InprocBus:
    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self.up = True

    def connected(self) -> bool:
        return self.up

    def subscribe(self, topic: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers[topic] = handler

    def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        if not self.up:
            return False
        handler = self._handlers.get(topic)
        if handler:
            handler(payload)
        return True


class MqttBus:
    def __init__(self, host: str, port: int, client_id: str, cfg: MemoryConfig) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self.cfg = cfg
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._parts: dict[str, dict[int, bytes]] = {}
        self._ready = threading.Event()
        self._client = None
        self._qos = {
            cfg.topic("recall/req"): cfg.qos_recall,
            cfg.topic("recall/res"): cfg.qos_recall,
            cfg.topic("archive"): cfg.qos_archive,
            cfg.topic("snap"): cfg.qos_snapshot,
            cfg.topic("t0"): cfg.qos_recall,
        }

    def connected(self) -> bool:
        try:
            return bool(self._ready.is_set() and self._client and self._client.is_connected())
        except Exception:
            return False

    def subscribe(self, topic: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers[topic] = handler
        if self._client and self._ready.is_set():
            self._client.subscribe(topic, qos=self._qos.get(topic, self.cfg.qos_recall))

    def publish(self, topic: str, payload: dict[str, Any]) -> bool:
        if not self.connected() or self._client is None:
            return False
        import paho.mqtt.client as mqtt

        qos = self._qos.get(topic, self.cfg.qos_recall)
        body = json.dumps(payload, ensure_ascii=False)
        chunks = pack_parts(body, self.cfg.shard_bytes) if topic.endswith("/snap") else [body]
        timeout = None if self.cfg.net_timeout == UNLIMITED else float(self.cfg.net_timeout)
        for chunk in chunks:
            info = self._client.publish(topic, chunk, qos=qos)
            if int(getattr(info, "rc", 1)) != mqtt.MQTT_ERR_SUCCESS:
                return False
            if not _wait_pub(info, timeout):
                return False
        return True

    def connect(self) -> None:
        timeout = self.cfg.net_timeout
        limit = try_limit(self.cfg.net_retries)
        attempt = 0
        last: Exception | None = None
        while True:
            if limit is not None and attempt >= limit:
                break
            attempt += 1
            self._ready.clear()
            client = _mqtt_client(self._client_id)
            client.on_connect = lambda c, *a: self._on_connect(c, *a)
            client.on_message = self._on_message
            self._client = client
            try:
                client.connect(self._host, self._port, keepalive=30)
                client.loop_start()
                wait = None if timeout == UNLIMITED else float(timeout)
                if wait is None:
                    while not self._ready.is_set():
                        time.sleep(0.05)
                    return
                if self._ready.wait(timeout=wait):
                    return
                last = TimeoutError("mqtt not ready")
            except Exception as exc:
                last = exc
            try:
                client.on_connect = None
                client.on_message = None
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            time.sleep(min(1.5, 0.4 * attempt))
        raise TimeoutError(f"mqtt {self._host}:{self._port} failed") from last

    def _on_connect(self, client, *args) -> None:
        rc = args[-1] if args else 0
        rc = getattr(rc, "value", rc)
        try:
            if int(rc) != 0:
                return
        except Exception:
            return
        for topic in self._handlers:
            client.subscribe(topic, qos=self._qos.get(topic, self.cfg.qos_recall))
        self._ready.set()

    def _on_message(self, _client, _userdata, message) -> None:
        handler = self._handlers.get(message.topic)
        if not handler:
            return
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            return
        if isinstance(payload, dict) and payload.get("_part"):
            payload = take_part(payload, self._parts)
            if payload is None:
                return
        if isinstance(payload, dict):
            handler(payload)

    def close(self) -> None:
        if self._client is None:
            return
        self._client.loop_stop()
        self._client.disconnect()


def _mqtt_client(client_id: str):
    import paho.mqtt.client as mqtt

    kw: dict[str, Any] = {"client_id": client_id}
    if hasattr(mqtt, "CallbackAPIVersion"):
        kw["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION1
    return mqtt.Client(**kw)


def _wait_pub(info: Any, timeout: float | None) -> bool:
    wait = getattr(info, "wait_for_publish", None)
    if wait is None:
        return True
    try:
        if timeout is None:
            wait()
        else:
            wait(timeout=timeout)
    except Exception:
        return False
    published = getattr(info, "is_published", None)
    if published is None:
        return True
    try:
        return bool(published())
    except Exception:
        return True


def pack_parts(body: str, size: int) -> list[str]:
    raw = body.encode("utf-8")
    if size <= 0 or size == UNLIMITED or len(raw) <= size:
        return [body]
    mid = uuid.uuid4().hex
    chunks = [raw[i : i + size] for i in range(0, len(raw), size)]
    n = len(chunks)
    return [
        json.dumps({"_part": True, "id": mid, "i": i, "n": n, "b": base64.b64encode(chunk).decode("ascii")})
        for i, chunk in enumerate(chunks)
    ]


def take_part(part: dict[str, Any], buf: dict[str, dict[int, bytes]]) -> dict[str, Any] | None:
    mid = str(part.get("id") or "")
    try:
        i = int(part["i"])
        n = int(part["n"])
        chunk = base64.b64decode(part["b"])
    except Exception:
        return None
    if not mid or n <= 0 or i < 0 or i >= n:
        return None
    if mid not in buf and len(buf) >= 8:
        buf.pop(next(iter(buf)))
    got = buf.setdefault(mid, {})
    got[i] = chunk
    if len(got) < n:
        return None
    blob = b"".join(got[j] for j in range(n))
    buf.pop(mid, None)
    try:
        data = json.loads(blob.decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None
