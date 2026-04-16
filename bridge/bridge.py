"""
CraftCompanion Bridge — macOS
Connects M5Core2 (BLE) <-> Minecraft (RCON) <-> GCP (Cloud Functions)
"""

import asyncio
import io
import json
import logging
import os
import re
import struct
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import mss
import requests
from bleak import BleakClient, BleakScanner
from mcrcon import MCRcon
from PIL import Image

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bridge")

# ── Configuration ─────────────────────────────────────────────────────────────
def _load_env_file() -> None:
    """Load bridge/.env (or .env) without requiring python-dotenv."""
    env_candidates = [
        Path(__file__).resolve().parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in env_candidates:
        if not env_path.exists():
            continue
        try:
            for raw in env_path.read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:
            pass
        break


_load_env_file()

RCON_HOST = os.environ.get("RCON_HOST", "localhost")
RCON_PORT = int(os.environ.get("RCON_PORT", 25575))
# RCON_PASSWORD = os.environ.get("RCON_PASSWORD") #JUST HARDCODE IT FOR NOW
RCON_RETRY_COOLDOWN = float(os.environ.get("RCON_RETRY_COOLDOWN_SECONDS", 8.0))

CLOUD_LOG_URL = os.environ.get(
    "CLOUD_LOG_URL",
    "https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=log",
)
CLOUD_GET_URL = os.environ.get(
    "CLOUD_GET_URL",
    "https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=get",
)
CLOUD_TIMEOUT = int(os.environ.get("CLOUD_TIMEOUT_SECONDS", 5))
AUTO_FOCUS_WINDOW = os.environ.get("AUTO_FOCUS_WINDOW", "1") == "1"
LOOK_GAIN = float(os.environ.get("LOOK_GAIN", "4.0"))

# BLE device name advertised by the M5Core2
M5_DEVICE_NAME = "CraftCompanion"

# GATT characteristic UUIDs — must match M5 firmware exactly
GAME_DATA_CHAR_UUID   = "12345678-1234-1234-1234-123456789001"
SCREENSHOT_CHAR_UUID  = "12345678-1234-1234-1234-123456789002"
KEYPRESS_CHAR_UUID    = "12345678-1234-1234-1234-123456789003"

# Screenshot settings
SCREENSHOT_WIDTH  = 320
SCREENSHOT_HEIGHT = 240
JPEG_QUALITY      = 35          # lower = smaller BLE payload, faster transfer
CHUNK_SIZE        = int(os.environ.get("SCREENSHOT_CHUNK_SIZE", 180))
CHUNK_DELAY       = float(os.environ.get("SCREENSHOT_CHUNK_DELAY_SECONDS", 0.005))

# How often to poll/send game data (seconds)
PLAYER_POLL_INTERVAL = float(os.environ.get("PLAYER_POLL_INTERVAL_SECONDS", 6.0))
DETAIL_INTERVAL = 10.0
SCREENSHOT_INTERVAL = float(os.environ.get("SCREENSHOT_INTERVAL_SECONDS", 1.0))
SCREENSHOT_INTERVAL = max(0.3, min(SCREENSHOT_INTERVAL, 2.0))
RECONNECT_DELAY = float(os.environ.get("BLE_RECONNECT_DELAY_SECONDS", 2.0))

# ── Session state ─────────────────────────────────────────────────────────────
session = {
    "started_at": None,
    "screenshots_sent": 0,
    "coords_visited": [],
}
_rcon_password_warned = False
_rcon_login_failed_until = 0.0


# ── RCON helpers ──────────────────────────────────────────────────────────────

def rcon_command(cmd: str) -> str:
    """Run a single RCON command and return the response string."""
    global _rcon_password_warned, _rcon_login_failed_until
    if not RCON_PASSWORD:
        if not _rcon_password_warned:
            log.warning("RCON_PASSWORD is not set. Set it via environment variable.")
            _rcon_password_warned = True
        return ""
    now = time.monotonic()
    if now < _rcon_login_failed_until:
        return ""
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, RCON_PORT) as rc:
            return rc.command(cmd)
    except Exception as e:
        msg = str(e)
        if "Login failed" in msg:
            _rcon_login_failed_until = time.monotonic() + RCON_RETRY_COOLDOWN
            log.warning(
                "RCON login failed. Check server.properties rcon.password and bridge "
                f"RCON_PASSWORD. Backing off for {RCON_RETRY_COOLDOWN:.1f}s."
            )
        else:
            log.warning(f"RCON error: {e}")
        return ""


def parse_player_list_response(player_resp: str) -> tuple[int, list[str]]:
    """Parse the output from the Minecraft `list` command."""
    if not player_resp:
        return 0, []

    cleaned = re.sub(r"\xa7.", "", player_resp)
    cleaned = cleaned.strip()
    player_count = 0
    player_names: list[str] = []

    # Common Java server format:
    # "There are 1 of a max of 20 players online: GraceB"
    match = re.search(
        r"There are\s+(\d+)\s+of a max of\s+\d+\s+players online:?\s*(.*)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if match:
        player_count = int(match.group(1))
        names_part = (match.group(2) or "").strip()
        player_names = [n.strip() for n in names_part.split(",") if n.strip()]
        return player_count, player_names

    # Alternate format variants sometimes returned by proxies/plugins:
    # "Online players (1/20): GraceB"
    alt = re.search(r"\((\d+)\s*/\s*\d+\)\s*:\s*(.*)", cleaned)
    if alt:
        player_count = int(alt.group(1))
        names_part = (alt.group(2) or "").strip()
        player_names = [n.strip() for n in names_part.split(",") if n.strip()]
        return player_count, player_names

    log.warning(f"Unrecognized RCON list response: {cleaned[:160]}")
    return player_count, player_names


def get_player_overview() -> dict:
    """Fetch only player roster data for low-bandwidth BLE updates."""
    player_resp = rcon_command("list")
    player_count, player_names = parse_player_list_response(player_resp)
    return {
        "player_count": player_count,
        "players": player_names,
        "coords": {},
        "server": f"{RCON_HOST}:{RCON_PORT}",
        "ts": int(time.time()),
    }


def get_game_data() -> dict:
    """
    Pull current game state from Minecraft via RCON.
    Returns a dict ready to JSON-encode and send over BLE.
    """
    # Player list
    player_resp = rcon_command("list")
    player_count, player_names = parse_player_list_response(player_resp)

    # Coordinates for each online player
    coords = {}
    for name in player_names:
        pos_resp = rcon_command(f"data get entity {name} Pos")
        # e.g. "GraceB has the following entity data: [-12.4d, 64.0d, 88.1d]"
        try:
            raw = pos_resp.split("[")[1].split("]")[0]
            xyz = [float(v.replace("d", "").strip()) for v in raw.split(",")]
            coords[name] = {"x": round(xyz[0], 1), "y": round(xyz[1], 1), "z": round(xyz[2], 1)}
            # Track coords for session log
            session["coords_visited"].append(coords[name])
        except Exception:
            coords[name] = None

    return {
        "player_count": player_count,
        "players": player_names,
        "coords": coords,
        "server": f"{RCON_HOST}:{RCON_PORT}",
        "ts": int(time.time()),
    }


# ── Screenshot helpers ────────────────────────────────────────────────────────

def get_minecraft_window_bounds() -> dict | None:
    """Get Minecraft window bounds on macOS using AppleScript."""
    script = """
    tell application "System Events"
        tell process "java"
            set w to first window
            set pos to position of w
            set sz to size of w
            return (item 1 of pos) & "," & (item 2 of pos) & "," & (item 1 of sz) & "," & (item 2 of sz)
        end tell
    end tell
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=3,
        )
        parts = result.stdout.strip().split(",")
        if len(parts) == 4:
            x, y, w, h = [int(float(p.strip())) for p in parts]
            return {"left": x, "top": y, "width": w, "height": h}
    except Exception as e:
        log.warning(f"Window bounds failed: {e}")
    return None

def capture_screenshot() -> bytes:
    """
    Capture Minecraft window when available, otherwise primary monitor.
    Returns raw JPEG bytes.
    """
    with mss.mss() as sct:
        bounds = get_minecraft_window_bounds()
        monitor = bounds if bounds else sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    img = img.resize((SCREENSHOT_WIDTH, SCREENSHOT_HEIGHT), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def build_chunks(jpeg_bytes: bytes) -> tuple[int, list[bytes]]:
    """
    Split JPEG bytes into BLE-sized chunks.
    Packet format:
      [frame_id: 2 bytes big-endian] [chunk_index: 2 bytes] [total_chunks: 2 bytes] [payload]
    A final empty payload with chunk_index == total_chunks signals end-of-image.
    """
    frame_id = bridge_state.get("next_frame_id", 0) & 0xFFFF
    bridge_state["next_frame_id"] = (frame_id + 1) & 0xFFFF
    total = (len(jpeg_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = []
    for i in range(total):
        payload = jpeg_bytes[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        header = struct.pack(">HHH", frame_id, i, total)
        chunks.append(header + payload)
    # EOF sentinel: index == total, empty payload
    chunks.append(struct.pack(">HHH", frame_id, total, total))
    return frame_id, chunks


# ── GCP helpers ───────────────────────────────────────────────────────────────

def cloud_log(event: str):
    """Fire-and-forget session log to GCP."""
    try:
        payload = {
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session": {
                "started_at": session["started_at"],
                "screenshots_sent": session["screenshots_sent"],
                "coords_visited": session["coords_visited"][-50:],  # last 50 coords
            },
        }
        r = requests.post(CLOUD_LOG_URL, json=payload, timeout=CLOUD_TIMEOUT)
        log.info(f"GCP log [{event}]: {r.status_code}")
    except Exception as e:
        log.warning(f"GCP log failed: {e}")


def cloud_get_session() -> dict | None:
    """Fetch latest session from GCP. Returns dict or None."""
    try:
        r = requests.get(CLOUD_GET_URL, timeout=CLOUD_TIMEOUT)
        data = r.json()
        return data.get("session")
    except Exception as e:
        log.warning(f"GCP get failed: {e}")
        return None


def build_detail_text(session_doc: dict | None) -> str:
    """Format latest cloud session into a compact display block for M5 detail panel."""
    if not session_doc:
        return "No cloud session available."

    event = session_doc.get("event", "unknown")
    ts = session_doc.get("timestamp", "n/a")
    s = session_doc.get("session", {}) if isinstance(session_doc.get("session"), dict) else {}
    started = s.get("started_at", "n/a")
    shots = s.get("screenshots_sent", 0)
    coords = s.get("coords_visited", []) if isinstance(s.get("coords_visited"), list) else []

    lines = [
        f"Event: {event}",
        f"Updated: {ts}",
        f"Started: {started}",
        f"Shots: {shots}",
    ]

    if coords:
        lines.append("Last Coords:")
        for item in coords[-3:]:
            if isinstance(item, dict):
                x = item.get("x", 0.0)
                y = item.get("y", 0.0)
                z = item.get("z", 0.0)
                lines.append(f"({x}, {y}, {z})")

    text = "\n".join(lines)
    # Keep payload modest so BLE writes stay reliable.
    return text[:320]


# ── BLE keypress handler ──────────────────────────────────────────────────────

def handle_keypress(command: str):
    """
    Receives a keypress command string from the M5 and injects it into
    the active Minecraft window using pyautogui.
    Commands: movement (LEFT/RIGHT/FORWARD/BACK/STOP), button actions,
    and hold keys via DOWN:key / UP:key.
    """
    import pyautogui
    pyautogui.PAUSE = 0

    key_map = {
        "LEFT":  "a",
        "RIGHT": "d",
        "FORWARD": "w",
        "BACK": "s",
        "JUMP":  "space",
    }

    cmd = command.strip().upper()
    log.info(f"Keypress received: {cmd}")

    if AUTO_FOCUS_WINDOW and cmd != "REFRESH":
        focus_minecraft_window()

    if cmd == "REFRESH":
        # Signal the screenshot loop — handled via the shared flag below
        bridge_state["screenshot_requested"] = True
        return
    if cmd == "DATA_REFRESH":
        # Signal HUD/data refresh request from the M5.
        bridge_state["game_data_requested"] = True
        bridge_state["screenshot_pause_until"] = time.monotonic() + 2.0
        return

    if cmd.startswith("LOOK:"):
        try:
            payload = cmd.split(":", 1)[1]
            sx, sy = payload.split(",", 1)
            raw_x = int(sx)
            raw_y = int(sy)
            dx = int(raw_x * LOOK_GAIN)
            dy = int(raw_y * LOOK_GAIN)
            if dx == 0 and raw_x != 0:
                dx = 1 if raw_x > 0 else -1
            if dy == 0 and raw_y != 0:
                dy = 1 if raw_y > 0 else -1
            if dx != 0 or dy != 0:
                pyautogui.moveRel(dx, dy, duration=0)
        except Exception:
            pass
        return

    if cmd.startswith("DOWN:"):
        key = cmd.split(":", 1)[1].strip().lower()
        if key:
            held = bridge_state["held_keys"]
            if key not in held:
                pyautogui.keyDown(key)
                held.add(key)
        return

    if cmd.startswith("UP:"):
        key = cmd.split(":", 1)[1].strip().lower()
        if key:
            held = bridge_state["held_keys"]
            if key in held:
                pyautogui.keyUp(key)
                held.remove(key)
        return

    movement_key = key_map.get(cmd)
    if movement_key and cmd in {"LEFT", "RIGHT", "FORWARD", "BACK"}:
        current = bridge_state.get("movement_key")
        if current != movement_key:
            if current:
                pyautogui.keyUp(current)
            pyautogui.keyDown(movement_key)
            bridge_state["movement_key"] = movement_key
        return

    if cmd == "STOP":
        current = bridge_state.get("movement_key")
        if current:
            pyautogui.keyUp(current)
            bridge_state["movement_key"] = None
        return

    if cmd in {"ATTACK", "PUNCH"}:
        pyautogui.click(button="left")
        return

    if cmd == "PLACE":
        pyautogui.click(button="right")
        return

    if cmd == "INVENTORY":
        pyautogui.press("e")
        return

    key = key_map.get(cmd)
    if key:
        pyautogui.press(key)
        return

    if len(cmd) == 1 and cmd.isalpha():
        pyautogui.press(cmd.lower())


# ── Shared bridge state (used across async tasks) ─────────────────────────────
bridge_state = {
    "screenshot_requested": False,
    "screenshot_in_flight": False,
    "game_data_requested": True,
    "client": None,
    "movement_key": None,
    "last_focus_at": 0.0,
    "held_keys": set(),
    "ble_write_lock": None,
    "last_player_signature": None,
    "next_frame_id": 0,
    "screenshot_pause_until": 0.0,
}


def focus_minecraft_window() -> None:
    """Best-effort focus of the Minecraft Java window on macOS."""
    now = time.monotonic()
    if now - bridge_state.get("last_focus_at", 0.0) < 0.8:
        return
    bridge_state["last_focus_at"] = now

    script = """
    tell application "System Events"
        if exists process "java" then
            tell process "java"
                set frontmost to true
            end tell
        end if
    end tell
    """
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=2)
    except Exception:
        pass


# ── BLE notification callback ─────────────────────────────────────────────────

def on_keypress_notify(sender, data: bytearray):
    """Called when M5 writes to KEYPRESS_CHAR."""
    try:
        command = data.decode("utf-8").strip()
        handle_keypress(command)
    except Exception as e:
        log.warning(f"Keypress decode error: {e}")


# ── Async tasks ───────────────────────────────────────────────────────────────

async def ble_write(client: BleakClient, uuid: str, payload: bytes, response: bool):
    """Serialize BLE writes to avoid overlapping GATT operations."""
    lock = bridge_state.get("ble_write_lock")
    if lock is None:
        await client.write_gatt_char(uuid, payload, response=response)
        return
    async with lock:
        await client.write_gatt_char(uuid, payload, response=response)


async def game_data_loop(client: BleakClient):
    """Poll roster and push to M5 only when player list/count changes."""
    log.info("Game data loop started (change-driven)")
    bridge_state["last_player_signature"] = None
    while True:
        if not client.is_connected:
            log.info("Game data loop stopped: BLE disconnected")
            break
        try:
            refresh_requested = bool(bridge_state.get("game_data_requested"))
            if bridge_state.get("screenshot_in_flight") and not refresh_requested:
                await asyncio.sleep(PLAYER_POLL_INTERVAL)
                continue
            if refresh_requested:
                bridge_state["game_data_requested"] = False
                for _ in range(20):
                    if not bridge_state.get("screenshot_in_flight"):
                        break
                    await asyncio.sleep(0.05)
                data = get_game_data()
                signature = (data["player_count"], tuple(data["players"]))
                bridge_state["last_player_signature"] = signature
                payload = json.dumps(data).encode("utf-8")
                await ble_write(client, GAME_DATA_CHAR_UUID, payload, response=True)
                log.info(
                    f"Data refresh sent: {data['player_count']} players, "
                    f"coords_keys={len(data.get('coords', {}))}"
                )
                await asyncio.to_thread(cloud_log, "hud_refresh")
            else:
                data = get_player_overview()
                signature = (data["player_count"], tuple(data["players"]))
                if signature != bridge_state.get("last_player_signature"):
                    bridge_state["last_player_signature"] = signature
                    full = get_game_data()
                    payload = json.dumps(full).encode("utf-8")
                    await ble_write(client, GAME_DATA_CHAR_UUID, payload, response=True)
                    log.info(
                        f"Roster change sent: {full['player_count']} players, "
                        f"coords_keys={len(full.get('coords', {}))}"
                    )
        except Exception as e:
            log.warning(f"Game data loop error: {e}")
        await asyncio.sleep(PLAYER_POLL_INTERVAL)


async def detail_loop(client: BleakClient):
    """Push latest cloud session summary text for the M5 detail panel."""
    log.info("Detail loop started")
    while True:
        if not client.is_connected:
            log.info("Detail loop stopped: BLE disconnected")
            break
        try:
            if bridge_state.get("screenshot_in_flight"):
                await asyncio.sleep(DETAIL_INTERVAL)
                continue
            latest = await asyncio.to_thread(cloud_get_session)
            detail_text = build_detail_text(latest)
            payload = json.dumps({"detail": detail_text}).encode("utf-8")
            await ble_write(client, GAME_DATA_CHAR_UUID, payload, response=False)
            log.info("Detail payload sent")
        except Exception as e:
            log.warning(f"Detail loop error: {e}")
        await asyncio.sleep(DETAIL_INTERVAL)


async def screenshot_loop(client: BleakClient):
    """
    Send screenshots continuously at SCREENSHOT_INTERVAL seconds.
    Also honors manual REFRESH requests from the M5.
    """
    log.info("Screenshot loop started")
    log.info(f"Screenshot cadence configured: every {SCREENSHOT_INTERVAL:.2f}s")
    bridge_state["screenshot_requested"] = True  # send one immediately on connect
    next_due_at = time.monotonic()

    while True:
        if not client.is_connected:
            log.info("Screenshot loop stopped: BLE disconnected")
            break

        now = time.monotonic()
        auto_due = now >= next_due_at and now >= bridge_state.get("screenshot_pause_until", 0.0)

        if bridge_state["screenshot_requested"] or auto_due:
            bridge_state["screenshot_requested"] = False
            try:
                log.info("Capturing screenshot...")
                frame_start = time.monotonic()
                jpeg = await asyncio.to_thread(capture_screenshot)
                frame_id, chunks = build_chunks(jpeg)
                log.info(
                    f"Sending frame={frame_id} chunks={len(chunks)} jpeg_bytes={len(jpeg)}"
                )
                bridge_state["screenshot_in_flight"] = True

                for chunk in chunks:
                    if not client.is_connected:
                        raise ConnectionError("disconnected")
                    await ble_write(client, SCREENSHOT_CHAR_UUID, chunk, response=True)
                    # Small delay between chunks to avoid overwhelming the M5
                    await asyncio.sleep(CHUNK_DELAY)

                session["screenshots_sent"] += 1
                now_done = time.monotonic()
                next_due_at = now_done + SCREENSHOT_INTERVAL
                frame_ms = int((now_done - frame_start) * 1000)
                log.info(f"Screenshot sent frame={frame_id} in {frame_ms}ms")

            except Exception as e:
                msg = str(e).lower()
                if (not client.is_connected) or ("disconnect" in msg) or ("not connected" in msg):
                    log.info("Screenshot loop stopped: BLE disconnected")
                    break
                log.error(f"Screenshot error: {e}")
                # Keep loop alive after transient GATT errors.
                await asyncio.sleep(0.2)
            finally:
                bridge_state["screenshot_in_flight"] = False

        # Keep the loop responsive to manual REFRESH while still honoring cadence.
        await asyncio.sleep(0.05)


async def run_bridge_once():
    """Run one BLE connection session until disconnect/error."""
    log.info(f"Scanning for '{M5_DEVICE_NAME}'...")

    device = await BleakScanner.find_device_by_name(M5_DEVICE_NAME, timeout=30.0)
    if device is None:
        log.error(f"Could not find BLE device named '{M5_DEVICE_NAME}'. Is the M5 running?")
        return

    log.info(f"Found device: {device.address}")

    async with BleakClient(device) as client:
        bridge_state["client"] = client
        bridge_state["ble_write_lock"] = asyncio.Lock()
        log.info("Connected to M5Core2")

        # Start session
        session["started_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(cloud_log, "connect")

        # Subscribe to keypress notifications from M5
        await client.start_notify(KEYPRESS_CHAR_UUID, on_keypress_notify)
        log.info("Subscribed to keypress notifications")

        try:
            await asyncio.gather(
                game_data_loop(client),
                detail_loop(client),
                screenshot_loop(client),
            )
        except Exception as e:
            log.error(f"Bridge loop error: {e}")
        finally:
            try:
                await client.stop_notify(KEYPRESS_CHAR_UUID)
            except Exception:
                pass
            try:
                import pyautogui
                current = bridge_state.get("movement_key")
                if current:
                    pyautogui.keyUp(current)
                    bridge_state["movement_key"] = None
                held = bridge_state.get("held_keys", set())
                for key in list(held):
                    pyautogui.keyUp(key)
                    held.remove(key)
            except Exception:
                pass
            await asyncio.to_thread(cloud_log, "disconnect")
            bridge_state["ble_write_lock"] = None
            log.info("Disconnected — session logged to GCP")


async def run_bridge():
    """Main entry point — reconnect automatically after disconnects."""
    while True:
        try:
            await run_bridge_once()
        except Exception as e:
            log.error(f"Bridge crashed: {e}")
        log.info(f"Retrying BLE scan in {RECONNECT_DELAY:.1f}s...")
        await asyncio.sleep(RECONNECT_DELAY)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run_bridge())
