#!/usr/bin/env python3
"""Laptop-side bridge for CraftCompanion.

This script polls Minecraft state, captures JPEG screenshots, and moves data
between the game, BLE link, and Google Cloud endpoints.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

import requests

try:
    from PIL import ImageGrab
except ImportError:  # pragma: no cover - runtime dependency
    ImageGrab = None

try:
    import pyautogui
except ImportError:  # pragma: no cover - runtime dependency
    pyautogui = None

try:
    from mcipc.rcon.je import Client as RconClient
except ImportError:  # pragma: no cover - runtime dependency
    RconClient = None


SERVICE_UUID = UUID("5f110001-6fd8-4f9c-9f79-0dff5a66b001")
GAME_DATA_CHAR = UUID("5f110002-6fd8-4f9c-9f79-0dff5a66b001")
SCREENSHOT_CHUNK_CHAR = UUID("5f110003-6fd8-4f9c-9f79-0dff5a66b001")
KEYPRESS_CHAR = UUID("5f110004-6fd8-4f9c-9f79-0dff5a66b001")


@dataclass
class BridgeConfig:
    rcon_host: str = os.getenv("RCON_HOST", "127.0.0.1")
    rcon_port: int = int(os.getenv("RCON_PORT", "25575"))
    rcon_password: str = os.getenv("RCON_PASSWORD", "")
    minecraft_window_title: str = os.getenv("MC_WINDOW_TITLE", "Minecraft")

    bridge_name: str = os.getenv("BLE_BRIDGE_NAME", "CraftCompanionBridge")
    game_poll_seconds: float = float(os.getenv("GAME_POLL_SECONDS", "2.0"))

    screenshot_width: int = int(os.getenv("SCREENSHOT_WIDTH", "320"))
    screenshot_height: int = int(os.getenv("SCREENSHOT_HEIGHT", "240"))
    jpeg_quality: int = int(os.getenv("JPEG_QUALITY", "40"))
    chunk_bytes: int = int(os.getenv("BLE_CHUNK_BYTES", "500"))

    cloud_log_url: str = os.getenv("CLOUD_LOG_URL", "")
    cloud_get_url: str = os.getenv("CLOUD_GET_URL", "")
    cloud_timeout_seconds: float = float(os.getenv("CLOUD_TIMEOUT_SECONDS", "5"))


@dataclass
class SessionState:
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    screenshots_sent: int = 0
    coords_visited: List[Dict[str, float]] = field(default_factory=list)

    def add_coords(self, coords: Dict[str, float]) -> None:
        if not coords:
            return
        if self.coords_visited and self.coords_visited[-1] == coords:
            return
        self.coords_visited.append(coords)


class MinecraftDataSource:
    def __init__(self, config: BridgeConfig):
        self._cfg = config
        self._rcon: Optional[Any] = None

    def _ensure_client(self) -> None:
        if self._rcon is not None:
            return
        if RconClient is None:
            raise RuntimeError("mcipc is required for RCON access")
        self._rcon = RconClient(self._cfg.rcon_host, self._cfg.rcon_password, port=self._cfg.rcon_port)

    def read_game_data(self) -> Dict[str, Any]:
        """Return serializable game state payload expected by the M5 HUD."""
        try:
            self._ensure_client()
            with self._rcon as client:
                data = {
                    "coords": self._coords_from_query(client),
                    "server": self._server_name(client),
                    "players_online": self._player_count(client),
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
                return data
        except Exception as exc:  # pragma: no cover - hardware/runtime path
            return {
                "coords": {"x": 0.0, "y": 0.0, "z": 0.0},
                "server": "Unavailable",
                "players_online": 0,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "error": str(exc),
            }

    @staticmethod
    def _coords_from_query(client: Any) -> Dict[str, float]:
        # Works with a standard data-get command response.
        resp = client.run("data get entity @p Pos")
        # Response shape differs across server versions; parse defensively.
        tokens = [t.strip("d[],") for t in str(resp).replace("[", " ").replace("]", " ").split()]
        floats = []
        for token in tokens:
            try:
                floats.append(float(token))
            except ValueError:
                continue
        if len(floats) >= 3:
            return {"x": round(floats[0], 2), "y": round(floats[1], 2), "z": round(floats[2], 2)}
        return {"x": 0.0, "y": 0.0, "z": 0.0}

    @staticmethod
    def _server_name(client: Any) -> str:
        motd = client.run("gamerule sendCommandFeedback")
        return str(motd or "Minecraft Server")

    @staticmethod
    def _player_count(client: Any) -> int:
        list_resp = str(client.run("list"))
        # Typical response: "There are 1 of a max 20 players online: Steve"
        for token in list_resp.split():
            if token.isdigit():
                return int(token)
        return 0


class ScreenshotSource:
    def __init__(self, config: BridgeConfig):
        self._cfg = config

    def capture_jpeg(self) -> bytes:
        if ImageGrab is None:
            raise RuntimeError("Pillow is required for screenshot capture")

        img = ImageGrab.grab()
        img = img.resize((self._cfg.screenshot_width, self._cfg.screenshot_height))

        buff = io.BytesIO()
        img.save(buff, format="JPEG", optimize=True, quality=self._cfg.jpeg_quality)
        return buff.getvalue()

    def chunk(self, jpeg: bytes) -> List[bytes]:
        chunks: List[bytes] = []
        payload_size = max(8, self._cfg.chunk_bytes - 4)
        total = (len(jpeg) + payload_size - 1) // payload_size

        for idx in range(total):
            start = idx * payload_size
            end = min(len(jpeg), start + payload_size)
            # Header format: [chunk_idx_hi, chunk_idx_lo, total_hi, total_lo]
            header = bytes([(idx >> 8) & 0xFF, idx & 0xFF, (total >> 8) & 0xFF, total & 0xFF])
            chunks.append(header + jpeg[start:end])
        return chunks


class CloudClient:
    def __init__(self, config: BridgeConfig):
        self._cfg = config

    def log_session(self, event: str, state: SessionState) -> None:
        if not self._cfg.cloud_log_url:
            return
        payload = {
            "event": event,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "session": {
                "started_at": state.started_at.isoformat(),
                "screenshots_sent": state.screenshots_sent,
                "coords_visited": state.coords_visited,
            },
        }
        requests.post(self._cfg.cloud_log_url, json=payload, timeout=self._cfg.cloud_timeout_seconds)

    def get_last_session(self) -> Optional[Dict[str, Any]]:
        if not self._cfg.cloud_get_url:
            return None
        resp = requests.get(self._cfg.cloud_get_url, timeout=self._cfg.cloud_timeout_seconds)
        resp.raise_for_status()
        return resp.json()


class BLEPeripheral:
    """BLE transport abstraction.

    The concrete implementation depends on host OS Bluetooth stack. This
    baseline keeps the rest of the bridge logic complete and testable while
    making the transport easy to swap.
    """

    def __init__(self, config: BridgeConfig):
        self._cfg = config
        self._keypress_queue: asyncio.Queue[str] = asyncio.Queue()

    async def start(self) -> None:
        # Placeholder for platform-specific GATT server startup.
        print(f"[BLE] start peripheral '{self._cfg.bridge_name}' service={SERVICE_UUID}")

    async def stop(self) -> None:
        print("[BLE] stop peripheral")

    async def notify_game_data(self, payload: Dict[str, Any]) -> None:
        print(f"[BLE] GAME_DATA notify: {json.dumps(payload)}")

    async def notify_screenshot_chunks(self, chunks: List[bytes]) -> None:
        for chunk in chunks:
            _ = chunk
            await asyncio.sleep(0.005)
        print(f"[BLE] SCREENSHOT notify chunks={len(chunks)}")

    async def wait_for_keypress(self, timeout: float = 0.1) -> Optional[str]:
        try:
            return await asyncio.wait_for(self._keypress_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def inject_test_keypress(self, key: str) -> None:
        await self._keypress_queue.put(key)


class BridgeApp:
    def __init__(self, config: BridgeConfig):
        self._cfg = config
        self._session = SessionState()
        self._data = MinecraftDataSource(config)
        self._shots = ScreenshotSource(config)
        self._cloud = CloudClient(config)
        self._ble = BLEPeripheral(config)
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        await self._ble.start()
        self._cloud.log_session("connect", self._session)

        try:
            await asyncio.gather(self._game_loop(), self._command_loop())
        finally:
            self._cloud.log_session("disconnect", self._session)
            await self._ble.stop()

    async def _game_loop(self) -> None:
        while not self._shutdown.is_set():
            payload = self._data.read_game_data()
            self._session.add_coords(payload.get("coords", {}))
            await self._ble.notify_game_data(payload)
            await asyncio.sleep(self._cfg.game_poll_seconds)

    async def _command_loop(self) -> None:
        while not self._shutdown.is_set():
            cmd = await self._ble.wait_for_keypress(timeout=0.2)
            if not cmd:
                continue

            cmd = cmd.upper().strip()
            if cmd in {"LEFT", "RIGHT", "JUMP"}:
                self._send_keypress(cmd)
                continue

            if cmd == "REFRESH":
                await self._send_screenshot()
                continue

            if cmd == "QUIT":
                self._shutdown.set()
                continue

            if cmd == "FETCH_LOG":
                session = self._cloud.get_last_session()
                print(f"[CLOUD] last session={session}")

    def _send_keypress(self, cmd: str) -> None:
        if pyautogui is None:
            print(f"[INPUT] pyautogui missing; dropped {cmd}")
            return
        mapping = {"LEFT": "left", "RIGHT": "right", "JUMP": "space"}
        key = mapping[cmd]
        pyautogui.press(key)
        print(f"[INPUT] sent {key}")

    async def _send_screenshot(self) -> None:
        try:
            jpeg = self._shots.capture_jpeg()
            chunks = self._shots.chunk(jpeg)
            await self._ble.notify_screenshot_chunks(chunks)
            self._session.screenshots_sent += 1
        except Exception as exc:  # pragma: no cover - runtime dependency path
            print(f"[SHOT] failed: {exc}")


def main() -> None:
    cfg = BridgeConfig()
    app = BridgeApp(cfg)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("Bridge stopped by user")


if __name__ == "__main__":
    main()
