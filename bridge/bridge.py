"""
CraftCompanion Bridge — macOS
Connects M5Core2 (BLE) <-> Minecraft (RCON) <-> GCP (Cloud Functions)
"""

import asyncio
import io
import json
import logging
import os
import struct
import subprocess
import time
from datetime import datetime, timezone

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
RCON_HOST = os.environ.get("RCON_HOST", "localhost")
RCON_PORT = int(os.environ.get("RCON_PORT", 25575))
RCON_PASSWORD = os.environ.get("RCON_PASSWORD")

CLOUD_LOG_URL = os.environ.get(
    "CLOUD_LOG_URL",
    "https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=log",
)
CLOUD_GET_URL = os.environ.get(
    "CLOUD_GET_URL",
    "https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=get",
)
CLOUD_TIMEOUT = int(os.environ.get("CLOUD_TIMEOUT_SECONDS", 5))

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
CHUNK_SIZE        = 490         # bytes per BLE packet (safe under 512 MTU)

# How often to push game data to M5 (seconds)
GAME_DATA_INTERVAL = 2.0
DETAIL_INTERVAL = 10.0
SCREENSHOT_INTERVAL = float(os.environ.get("SCREENSHOT_INTERVAL_SECONDS", 1.0))

# ── Session state ─────────────────────────────────────────────────────────────
session = {
    "started_at": None,
    "screenshots_sent": 0,
    "coords_visited": [],
}
_rcon_password_warned = False


# ── RCON helpers ──────────────────────────────────────────────────────────────

def rcon_command(cmd: str) -> str:
    """Run a single RCON command and return the response string."""
    global _rcon_password_warned
    if not RCON_PASSWORD:
        if not _rcon_password_warned:
            log.warning("RCON_PASSWORD is not set. Set it via environment variable.")
            _rcon_password_warned = True
        return ""
    try:
        with MCRcon(RCON_HOST, RCON_PASSWORD, RCON_PORT) as rc:
            return rc.command(cmd)
    except Exception as e:
        log.warning(f"RCON error: {e}")
        return ""


def get_game_data() -> dict:
    """
    Pull current game state from Minecraft via RCON.
    Returns a dict ready to JSON-encode and send over BLE.
    """
    # Player list
    player_resp = rcon_command("list")
    # e.g. "There are 1 of a max of 20 players online: GraceB"
    player_count = 0
    player_names = []
    if "players online:" in player_resp:
        try:
            player_count = int(player_resp.split("There are ")[1].split(" of")[0])
            names_part = player_resp.split("players online:")[-1].strip()
            player_names = [n.strip() for n in names_part.split(",") if n.strip()]
        except Exception:
            pass

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


def build_chunks(jpeg_bytes: bytes) -> list[bytes]:
    """
    Split JPEG bytes into BLE-sized chunks.
    Packet format: [chunk_index: 2 bytes big-endian] [total_chunks: 2 bytes] [payload]
    A final empty payload with chunk_index == total_chunks signals end-of-image.
    """
    total = (len(jpeg_bytes) + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks = []
    for i in range(total):
        payload = jpeg_bytes[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
        header = struct.pack(">HH", i, total)
        chunks.append(header + payload)
    # EOF sentinel: index == total, empty payload
    chunks.append(struct.pack(">HH", total, total))
    return chunks


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
    Commands: LEFT, RIGHT, FORWARD, BACK, STOP, JUMP, ATTACK, PLACE, REFRESH
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

    if cmd == "REFRESH":
        # Signal the screenshot loop — handled via the shared flag below
        bridge_state["screenshot_requested"] = True
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

    if cmd == "ATTACK":
        pyautogui.click(button="left")
        return

    if cmd == "PLACE":
        pyautogui.click(button="right")
        return

    key = key_map.get(cmd)
    if key:
        pyautogui.press(key)


# ── Shared bridge state (used across async tasks) ─────────────────────────────
bridge_state = {
    "screenshot_requested": False,
    "client": None,
    "movement_key": None,
}


# ── BLE notification callback ─────────────────────────────────────────────────

def on_keypress_notify(sender, data: bytearray):
    """Called when M5 writes to KEYPRESS_CHAR."""
    try:
        command = data.decode("utf-8").strip()
        handle_keypress(command)
    except Exception as e:
        log.warning(f"Keypress decode error: {e}")


# ── Async tasks ───────────────────────────────────────────────────────────────

async def game_data_loop(client: BleakClient):
    """Push game data to M5 every GAME_DATA_INTERVAL seconds."""
    log.info("Game data loop started")
    while client.is_connected:
        try:
            data = get_game_data()
            payload = json.dumps(data).encode("utf-8")
            await client.write_gatt_char(GAME_DATA_CHAR_UUID, payload, response=False)
            log.info(f"Game data sent: {data['player_count']} players")
        except Exception as e:
            log.warning(f"Game data loop error: {e}")
        await asyncio.sleep(GAME_DATA_INTERVAL)


async def detail_loop(client: BleakClient):
    """Push latest cloud session summary text for the M5 detail panel."""
    log.info("Detail loop started")
    while client.is_connected:
        try:
            latest = cloud_get_session()
            detail_text = build_detail_text(latest)
            payload = json.dumps({"detail": detail_text}).encode("utf-8")
            await client.write_gatt_char(GAME_DATA_CHAR_UUID, payload, response=False)
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
    bridge_state["screenshot_requested"] = True  # send one immediately on connect
    last_sent_at = 0.0

    while client.is_connected:
        now = time.monotonic()
        auto_due = (now - last_sent_at) >= SCREENSHOT_INTERVAL

        if bridge_state["screenshot_requested"] or auto_due:
            bridge_state["screenshot_requested"] = False
            try:
                log.info("Capturing screenshot...")
                jpeg = capture_screenshot()
                chunks = build_chunks(jpeg)
                log.info(f"Sending {len(chunks)} chunks ({len(jpeg)} bytes JPEG)")

                for i, chunk in enumerate(chunks):
                    if not client.is_connected:
                        raise ConnectionError("disconnected")
                    await client.write_gatt_char(
                        SCREENSHOT_CHAR_UUID, chunk, response=True
                    )
                    # Small delay between chunks to avoid overwhelming the M5
                    await asyncio.sleep(0.02)

                session["screenshots_sent"] += 1
                last_sent_at = time.monotonic()
                log.info("Screenshot sent")
                cloud_log("screenshot")

            except Exception as e:
                msg = str(e).lower()
                if "disconnect" in msg or "not connected" in msg:
                    log.info("Screenshot loop stopped: BLE disconnected")
                    break
                log.error(f"Screenshot error: {e}")

        await asyncio.sleep(0.1)


async def run_bridge():
    """Main entry point — scan, connect, start all loops."""
    log.info(f"Scanning for '{M5_DEVICE_NAME}'...")

    device = await BleakScanner.find_device_by_name(M5_DEVICE_NAME, timeout=30.0)
    if device is None:
        log.error(f"Could not find BLE device named '{M5_DEVICE_NAME}'. Is the M5 running?")
        return

    log.info(f"Found device: {device.address}")

    async with BleakClient(device) as client:
        bridge_state["client"] = client
        log.info("Connected to M5Core2")

        # Start session
        session["started_at"] = datetime.now(timezone.utc).isoformat()
        cloud_log("connect")

        # Subscribe to keypress notifications from M5
        await client.start_notify(KEYPRESS_CHAR_UUID, on_keypress_notify)
        log.info("Subscribed to keypress notifications")

        # Run game data and screenshot loops concurrently
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
            except Exception:
                pass
            cloud_log("disconnect")
            log.info("Disconnected — session logged to GCP")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(run_bridge())
