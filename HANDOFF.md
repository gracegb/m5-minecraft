# CraftCompanion — Full Project Handoff
> M5Core2 Minecraft Companion Device
> Machine: 2020 M1 MacBook Pro / macOS
> Repo: `/Users/gracebergquist/Repos/m5-minecraft`

---

## What This Project Is

An M5Core2 touchscreen device that acts as a live companion for Minecraft:
- **HUD mode** — displays live player coordinates and server/player info pulled from Minecraft via RCON
- **Viewer mode** — receives compressed JPEG screenshots of the Minecraft window over BLE, renders them on the M5 LCD, and sends touch-based movement commands (LEFT / RIGHT / JUMP) back to the laptop to control the game
- **Detail panel** — shows the latest session summary fetched from Google Cloud Firestore
- **Shake to refresh** — IMU shake gesture on M5 triggers a new screenshot

The laptop runs a Python bridge that sits between all three systems (Minecraft, M5, GCP). The M5 has no WiFi in this flow.

---

## Architecture

```
Minecraft Java (localhost:25565)
        │
        │ RCON (localhost:25575)
        ▼
Python bridge.py  (laptop)
        │  ├─ game data JSON → BLE → M5 GAME_DATA_CHAR
        │  ├─ JPEG chunks   → BLE → M5 SCREENSHOT_CHAR
        │  └─ keypress inject (pyautogui) ← BLE ← M5 KEYPRESS_CHAR
        │
        └─ HTTPS → GCP Cloud Function → Firestore
                        ↑
              M5 Detail Panel fetches via bridge
```

---

## Repo Structure

```
m5-minecraft/
├── bridge/
│   ├── bridge.py          # Python BLE+RCON+GCP bridge (main script)
│   └── requirements.txt   # bleak, mcrcon, mss, Pillow, pyautogui, requests
├── cloud/
│   ├── main.py            # GCP Cloud Function (session_api)
│   └── requirements.txt   # firebase-admin, functions-framework
├── m5/
│   ├── src/
│   │   └── main.cpp       # M5Core2 firmware
│   ├── include/
│   │   └── screens.h      # UiMode enum, GameData struct, fn declarations
│   └── platformio.ini     # PlatformIO build config
├── server.properties      # Minecraft local server config (RCON enabled)
├── .gitignore
└── HANDOFF.md             # this file
```

---

## GCP / Firebase — What Is Already Set Up

**Project ID:** `craftcompanion-492604`
**Region:** `us-central1`

### What exists in GCP:
- Firestore database (Native mode, us-central1)
- Service account: `craftcompanion-fn@craftcompanion-492604.iam.gserviceaccount.com`
  - Role: `roles/datastore.user`
- Cloud Function deployed: `session-api` (gen2, python312)
  - Entry point: `session_api`
  - Unauthenticated invoker allowed (prototype only)

### Live endpoints:
```
POST https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=log
GET  https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=get
```

### Firestore collection: `craft_companion_sessions`
Document fields: `event`, `timestamp`, `session.started_at`,
`session.screenshots_sent`, `session.coords_visited`, `updated_at`

### Verify cloud is still working:
```bash
curl -X POST "https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=log" \
  -H "Content-Type: application/json" \
  -d '{"event":"test","timestamp":"2026-04-06T12:00:00Z","session":{"started_at":"2026-04-06T12:00:00Z","screenshots_sent":0,"coords_visited":[]}}'

curl "https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=get"
```

---

## BLE Contract (Bridge ↔ M5)

| | Value |
|---|---|
| Device name (M5 advertises) | `CraftCompanion` |
| Service UUID | `12345678-1234-1234-1234-123456789000` |
| GAME_DATA_CHAR | `...9001` — bridge writes JSON; M5 onWrite callback |
| SCREENSHOT_CHAR | `...9002` — bridge writes chunked JPEG; M5 onWrite callback |
| KEYPRESS_CHAR | `...9003` — M5 notifies bridge with command strings |

### Data formats:
**Game data JSON** (bridge → M5 every 2s):
```json
{"player_count": 1, "players": ["Dragon__Archer"], "coords": {"Dragon__Archer": {"x": -12.4, "y": 64.0, "z": 88.1}}, "server": "localhost:25575", "ts": 1234567890}
```

**Detail panel JSON** (bridge → M5 on connect + every ~10s):
```json
{"detail": "Last: 2026-04-06T12:00\nScreenshots: 3\nCoords logged: 12\nLast pos: -12,64,88"}
```
If the JSON has a `"detail"` key, M5 stores it for the Detail Panel and does not update HUD fields.

**Screenshot chunk packet** (bridge → M5):
```
[chunk_index: 2 bytes big-endian][total_chunks: 2 bytes][JPEG payload: up to 490 bytes]
EOF sentinel: chunk_index == total_chunks, empty payload
```

**Keypress commands** (M5 → bridge via BLE notify):
`LEFT` | `RIGHT` | `JUMP` | `REFRESH`

---

## Minecraft Local Server Setup

Server jar lives in the repo root (gitignored). Minecraft version: **1.21.4**.

`server.properties` key settings:
```
enable-rcon=true
rcon.password=craftcompanion
rcon.port=25575
online-mode=false
```

### Start the server:
```bash
cd /Users/gracebergquist/Repos/m5-minecraft
java -Xmx2G -Xms1G -jar server.jar nogui
```
Wait for: `Done! For help, type "help"` and `RCON running on 0.0.0.0:25575`

### Connect Minecraft client:
Open Minecraft launcher → Multiplayer → Add Server → `localhost:25565`
You must be in-game (joined the world) for RCON to return real coords.

### Test RCON manually:
```bash
source .venv/bin/activate
python3 -c "from mcrcon import MCRcon; r = MCRcon('localhost', 'craftcompanion', 25575); r.connect(); print(r.command('list')); r.disconnect()"
```

---

## Python Environment

**Python version:** 3.11.2 (via pyenv)
**Venv location:** `/Users/gracebergquist/Repos/m5-minecraft/.venv`

### Activate:
```bash
cd /Users/gracebergquist/Repos/m5-minecraft
source .venv/bin/activate
```

### Installed packages (key ones):
```
bleak==3.0.1
mcrcon==0.7.0
Pillow==12.2.0
mss==10.1.0
PyAutoGUI==0.9.54
requests==2.33.1
```

### macOS permission required:
Terminal needs **Screen Recording** permission for `mss` screenshot capture.
System Settings → Privacy & Security → Screen Recording → enable Terminal.

---

## Running the Full Stack

You need **4 things running simultaneously**:

### Terminal 1 — Minecraft server
```bash
cd /Users/gracebergquist/Repos/m5-minecraft
java -Xmx2G -Xms1G -jar server.jar nogui
```

### Terminal 2 — Python bridge
```bash
cd /Users/gracebergquist/Repos/m5-minecraft
source .venv/bin/activate
python bridge/bridge.py
```

### Minecraft app
Launch via Minecraft launcher → join `localhost:25565`

### M5Core2
Flash firmware via PlatformIO (see below), then power on.
It will show HUD screen and start advertising `CraftCompanion` over BLE.
Bridge will find it automatically within ~5 seconds of starting.

---

## M5Core2 Firmware

**Framework:** Arduino via PlatformIO
**Libraries:** M5Core2, ArduinoJson, TJpg_Decoder (bodmer)
**Display:** M5.Lcd (built-in)

### Flash:
```bash
cd /Users/gracebergquist/Repos/m5-minecraft/m5
pio run --target upload
```

### Screen states (UiMode enum in screens.h):
- `HUD` — default, shows coords + server info + VIEWER/DETAIL buttons
- `VIEWER` — shows screenshot + LEFT/RIGHT/JUMP buttons at bottom
- `DETAIL` — shows last GCP session summary + BACK TO HUD button

### Touch zones:
- HUD: bottom-left → VIEWER, bottom-right → DETAIL
- VIEWER: bottom-left → LEFT, bottom-center → RIGHT, bottom-right → JUMP; tap header → back to HUD
- DETAIL: bottom bar → back to HUD

### Shake gesture:
IMU threshold: `2.20f`, debounce: `1200ms`
Shake sends `REFRESH` keypress notify to bridge → triggers new screenshot

---

## Known Bugs Fixed in This Session

1. `BleakAdvertisedDevice` import error — removed unused import (bleak 3.x removed it)
2. Bridge crash on disconnect — `stop_notify` called after connection dropped;
   wrap in try/except in the `finally` block of `run_bridge`
3. LEFT button spamming — touch debounce needed in `handleTouch` in `main.cpp`;
   add `static unsigned long lastTouchMs = 0` and `if (now - lastTouchMs < 400) return mode`
4. Screenshots capturing full desktop instead of Minecraft window — add
   `get_minecraft_window_bounds()` using AppleScript (`osascript`) to target
   the `java` process window, fall back to monitor 1 if it fails

---

## Known Remaining Work

- [ ] Apply the 4 bug fixes above (fixes 2–4 not yet applied to code)
- [ ] Detail Panel: M5 firmware needs to render `detail` key from JSON (parse in
      `parseAndStoreGameData` — if `doc.containsKey("detail")` store to `detailText`
      and redraw if currently on DETAIL screen)
- [ ] `drawViewerScreen` overwrites image area on connection status change — split
      into `drawViewerChrome()` (header + buttons only) and full redraw; only call
      full redraw on first enter, not on status updates
- [ ] Add auth/protection to Cloud Function endpoints (remove `--allow-unauthenticated`)
- [ ] Deduplicate `coords_visited` before logging to control Firestore payload growth
- [ ] Add chunk retransmit/ACK logic for dropped screenshot chunks

---

## Environment Variables (optional overrides)

The bridge has these hardcoded as defaults but respects env vars:
```bash
export CLOUD_LOG_URL="https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=log"
export CLOUD_GET_URL="https://us-central1-craftcompanion-492604.cloudfunctions.net/session-api?action=get"
export CLOUD_TIMEOUT_SECONDS=5
```

---

## What Was Verified Working in This Session

- GCP Cloud Function deployed and responding (log + get both return 200)
- Firestore writing and reading documents correctly
- Minecraft local server running with RCON on localhost:25575
- RCON returning live player list and coords
- Bridge scanning for and connecting to M5 over BLE
- Game data JSON flowing from bridge → M5 (HUD updating with live coords)
- Screenshot capture → JPEG compress → chunk → BLE transfer → M5 reassemble → TJpgDec render
- Touch controls on M5 (LEFT/RIGHT/JUMP) injecting keypresses into Minecraft via pyautogui
- Shake gesture on M5 triggering REFRESH → new screenshot
- GCP session logging on connect, screenshot, disconnect (all 200 responses confirmed)
- Detail panel payload flowing from bridge → M5
