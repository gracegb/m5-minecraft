"""
CraftCompanion Bridge — No more M5 instead web-only, publishes to Pub/Sub
Polls Minecraft via RCON, captures screenshots, uploads to GCS
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from mcrcon import MCRcon
from google.cloud import pubsub_v1, storage
from google.oauth2 import service_account

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bridge")

# ── Config ────────────────────────────────────────────────────────────────────
def _load_env_file() -> None:
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

RCON_HOST           = os.environ.get("RCON_HOST", "localhost")
RCON_PORT           = int(os.environ.get("RCON_PORT", 25575))
RCON_PASSWORD       = os.environ.get("RCON_PASSWORD", "craftcompanion")
RCON_RETRY_COOLDOWN = float(os.environ.get("RCON_RETRY_COOLDOWN_SECONDS", 8.0))

GCP_PROJECT         = os.environ.get("GCP_PROJECT", "craftcompanion-492604")
PUBSUB_TOPIC        = os.environ.get("PUBSUB_TOPIC", "craft-events")
SA_KEY_FILE         = os.environ.get("SA_KEY_FILE", "./bridge-sa-key.json")
GCS_BUCKET          = os.environ.get("GCS_BUCKET", "craftcompanion-screenshots")
MINECRAFT_SCREENSHOTS_DIR = Path(
    os.environ.get(
        "MINECRAFT_SCREENSHOTS_DIR",
        str(Path.home() / "Library/Application Support/minecraft/screenshots"),
    )
).expanduser()

POLL_INTERVAL       = float(os.environ.get("PLAYER_POLL_INTERVAL_SECONDS", 6.0))

# ── GCP clients ───────────────────────────────────────────────────────────────
credentials    = service_account.Credentials.from_service_account_file(SA_KEY_FILE)
publisher      = pubsub_v1.PublisherClient(credentials=credentials)
storage_client = storage.Client(credentials=credentials, project=GCP_PROJECT)
topic_path     = publisher.topic_path(GCP_PROJECT, PUBSUB_TOPIC)

# ── Session state ─────────────────────────────────────────────────────────────
session = {
    "started_at": None,
    "screenshots_sent": 0,
    "coords_visited": [],
}
_rcon_login_failed_until = 0.0


# ── RCON helpers ──────────────────────────────────────────────────────────────

def rcon_command(cmd: str) -> str:
    global _rcon_login_failed_until
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
            log.warning(f"RCON login failed. Backing off {RCON_RETRY_COOLDOWN:.1f}s.")
        else:
            log.warning(f"RCON error: {e}")
        return ""


def parse_player_list_response(player_resp: str) -> tuple[int, list[str]]:
    if not player_resp:
        return 0, []
    cleaned = re.sub(r"\xa7.", "", player_resp).strip()
    match = re.search(
        r"There are\s+(\d+)\s+of a max of\s+\d+\s+players online:?\s*(.*)",
        cleaned, flags=re.IGNORECASE,
    )
    if match:
        player_count = int(match.group(1))
        names_part = (match.group(2) or "").strip()
        return player_count, [n.strip() for n in names_part.split(",") if n.strip()]
    alt = re.search(r"\((\d+)\s*/\s*\d+\)\s*:\s*(.*)", cleaned)
    if alt:
        player_count = int(alt.group(1))
        names_part = (alt.group(2) or "").strip()
        return player_count, [n.strip() for n in names_part.split(",") if n.strip()]
    log.warning(f"Unrecognized RCON list response: {cleaned[:160]}")
    return 0, []


def get_inventory(player_name: str) -> dict:
    resp = rcon_command(f"data get entity {player_name} Inventory")
    if not resp:
        return {}
    try:
        log.info(f"Raw inventory for {player_name}: {resp[:200]}")
        raw = resp.split("[", 1)[1].rsplit("]", 1)[0]
        inventory = {}
        items = re.findall(r'\{[^}]+\}', raw)
        log.info(f"Found {len(items)} item blocks for {player_name}")
        for item in items:
            id_match    = re.search(r'id:\s*"([^"]+)"', item)
            count_match = re.search(r'Count:\s*(\d+)b?', item)
            if id_match:
                name  = id_match.group(1).replace("minecraft:", "")
                count = int(count_match.group(1)) if count_match else 1
                inventory[name] = inventory.get(name, 0) + count
        return inventory
    except Exception as e:
        log.warning(f"Inventory parse error for {player_name}: {e}")
        return {}


def get_game_data() -> dict:
    player_resp = rcon_command("list")
    player_count, player_names = parse_player_list_response(player_resp)

    coords = {}
    for name in player_names:
        pos_resp = rcon_command(f"data get entity {name} Pos")
        try:
            raw = pos_resp.split("[")[1].split("]")[0]
            xyz = [float(v.replace("d", "").strip()) for v in raw.split(",")]
            coords[name] = {
                "x": round(xyz[0], 1),
                "y": round(xyz[1], 1),
                "z": round(xyz[2], 1),
            }
            session["coords_visited"].append(coords[name])
        except Exception:
            coords[name] = None

    inventory = {}
    for name in player_names:
        inv = get_inventory(name)
        for item, count in inv.items():
            inventory[item] = inventory.get(item, 0) + count

    return {
        "player_count": player_count,
        "players": player_names,
        "coords": coords,
        "inventory": inventory,
        "server": f"{RCON_HOST}:{RCON_PORT}",
        "ts": int(time.time()),
    }


# ── Screenshot helpers ────────────────────────────────────────────────────────

def get_latest_minecraft_screenshot() -> tuple[Path, tuple[float, str]] | None:
    """
    Return the most recent in-game screenshot file and a marker tuple
    (mtime, filename) for monotonic change detection.
    """
    if not MINECRAFT_SCREENSHOTS_DIR.exists():
        return None
    try:
        candidates = []
        for p in MINECRAFT_SCREENSHOTS_DIR.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                continue
            st = p.stat()
            candidates.append((st.st_mtime, p.name, p))
        if not candidates:
            return None
        latest_mtime, latest_name, latest_path = max(candidates, key=lambda x: (x[0], x[1]))
        return latest_path, (latest_mtime, latest_name)
    except Exception as e:
        log.warning(f"Failed scanning Minecraft screenshots dir: {e}")
        return None

def upload_minecraft_screenshot_file(path: Path, session_id: str) -> str | None:
    """Upload a player-triggered in-game screenshot file from disk to GCS."""
    try:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        ext = path.suffix.lower().lstrip(".") or "png"
        bucket = storage_client.bucket(GCS_BUCKET)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        blob_name = f"screenshots/{session_id}/{timestamp}_{path.stem}.{ext}"
        blob = bucket.blob(blob_name)
        blob.upload_from_filename(str(path), content_type=content_type)

        url = f"https://storage.googleapis.com/{GCS_BUCKET}/{blob_name}"
        log.info(f"In-game screenshot uploaded from {path.name}: {url}")
        return url
    except Exception as e:
        log.warning(f"In-game screenshot upload failed ({path}): {e}")
        return None


# ── Pub/Sub publisher ─────────────────────────────────────────────────────────

def publish_event(event_type: str, data: dict):
    payload = json.dumps({
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session": {
            "started_at": session["started_at"],
            "coords_visited": session["coords_visited"][-50:],
            "screenshots_sent": session["screenshots_sent"],
        },
        "data": data,
    }).encode("utf-8")
    future = publisher.publish(topic_path, payload, event_type=event_type)
    log.info(f"Published [{event_type}] message_id={future.result()}")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def poll_loop():
    log.info("Bridge started — polling Minecraft via RCON")
    session["started_at"] = datetime.now(timezone.utc).isoformat()
    session_id = session["started_at"].replace(":", "_").replace(".", "_")
    publish_event("session.started", {})

    latest_existing = get_latest_minecraft_screenshot()
    last_uploaded_screenshot_marker = latest_existing[1] if latest_existing else None

    try:
        while True:
            data = get_game_data()
            log.info(f"Players: {data['players']} | Coords: {data['coords']}")

            if data["player_count"] > 0:
                latest = get_latest_minecraft_screenshot()
                if latest:
                    screenshot_path, marker = latest
                    has_new_in_game_screenshot = (
                        last_uploaded_screenshot_marker is None or marker > last_uploaded_screenshot_marker
                    )
                    if has_new_in_game_screenshot:
                        url = upload_minecraft_screenshot_file(screenshot_path, session_id)
                    else:
                        url = None
                    if url:
                        data["screenshot_url"] = url
                        session["screenshots_sent"] += 1
                        last_uploaded_screenshot_marker = marker
                publish_event("coords.updated", data)

            await asyncio.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Shutting down bridge...")
        publish_event("session.ended", {
            "coords_visited": session["coords_visited"],
        })


if __name__ == "__main__":
    asyncio.run(poll_loop())
