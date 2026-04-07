# m5-minecraft

# CraftCompanion

A portable M5Core2 companion device for Minecraft. Displays live game data and compressed screenshots on the M5 touchscreen, bridged through a laptop Python script that connects to Minecraft via RCON and routes data to Google Cloud.

---

## Architecture Overview

```
Minecraft (PC)
      │
      │ RCON / screenshot capture
      ▼
Python Bridge (laptop)
      │  ├─ Game data (coords, server info, player count)
      │  ├─ JPEG screenshot chunks
      │  └─ Keypress injection (pyautogui)
      │
      │ BLE (custom GATT)
      ▼
M5Core2 Device
      │
      └─ LCD touchscreen UI (HUD mode / Viewer mode)

Python Bridge ──── HTTPS ────► Google Cloud Function
                                      │
                               Session log (Firestore)
                                      │
                              fetched back to M5 Detail Panel
```

---

## Modes

### HUD Mode
Live dashboard pulled from the Python bridge every ~2 seconds.
- Player coordinates (X / Y / Z)
- Active server name + online player count
- Tap **Log** to open the Detail Panel (last session summary from GCP)

### Viewer Mode
Slow-refresh screenshot viewer with basic remote control.
- Python bridge captures the Minecraft window, compresses to JPEG (~10–20 KB), and splits into ~500-byte BLE MTU chunks
- M5 reassembles chunks and decodes JPEG to the LCD using `TJpgDec`
- On-screen buttons: **← Left**, **Right →**, **Jump** — each sends a keypress command back to the bridge via BLE
- **Shake to refresh** — IMU shake gesture (MPU6886 over I2C) triggers a new screenshot request without touching the screen

---

## Pipeline Detail

### Python Bridge (`bridge.py`)
| Responsibility | Library |
|---|---|
| BLE peripheral (GATT server) | `bleak` |
| Minecraft data via RCON | `mcrcon` |
| Screenshot capture + resize | `Pillow` |
| Keypress injection | `pyautogui` |
| GCP Cloud Function calls | `requests` |

**BLE GATT characteristics:**
- `GAME_DATA_CHAR` — JSON payload: coords, server name, player count (notify)
- `SCREENSHOT_CHUNK_CHAR` — binary: chunk index + JPEG bytes (notify)
- `KEYPRESS_CHAR` — string command from M5: `LEFT`, `RIGHT`, `JUMP` (write)

### M5Core2 Firmware (PlatformIO / Arduino)
- BLE Central role — scans and connects to bridge by name
- Parses `GAME_DATA_CHAR` notifications → updates HUD screen
- Reassembles `SCREENSHOT_CHUNK_CHAR` packets into a buffer → `TJpgDec` decode → `pushImage()` to LCD
- Reads MPU6886 (I2C) → shake detection → writes `KEYPRESS_CHAR` with `REFRESH`
- Touch zones mapped per screen: mode-select buttons, back, d-pad, refresh

### Google Cloud
- **Cloud Function** (`logSession`): called by bridge on connect/disconnect; writes session record to Firestore (timestamp, coords visited, screenshot count)
- M5 Detail Panel fetches last session JSON from a second Cloud Function endpoint and renders it as a scrollable text panel

---

## Novel Features

**1. IMU shake gesture (MPU6886 over I2C)**
Reads raw accelerometer data, applies a magnitude threshold + debounce timer to detect a deliberate shake, and fires a screenshot refresh. Demonstrates hardware not covered in class with real signal-processing logic on the M5.

**2. Chunked BLE JPEG transfer + on-device decode**
Screenshots too large for a single BLE packet are split on the bridge, sequenced with a chunk index header, reassembled in a heap buffer on the M5, and decoded directly to the LCD framebuffer using `TJpgDec`. Custom acknowledgment logic handles dropped chunks.

---

## Phase 2–4 Coverage

| Phase | Implementation |
|---|---|
| Phase 2 — I2C | MPU6886 accelerometer for shake gesture |
| Phase 3 — BLE | Custom GATT profile; chunked binary transfer; keypress commands |
| Phase 4 — GCP | Cloud Function + Firestore session logging; Detail Panel fetch |

---

## Hardware Notes
- M5Core2 BLE and WiFi cannot run simultaneously — WiFi is not used on the M5; all cloud calls go through the laptop bridge
- SD card not required; screenshots are streamed over BLE and rendered directly, never persisted
- Recommended screenshot resolution before compression: 320×240 (matches LCD), target JPEG quality 30–50% for reliable BLE transfer time (~3–6 seconds per frame)

---

## File Structure
```
/
├── bridge/
│   └── bridge.py          # Laptop-side Python bridge
├── m5/
│   ├── src/
│   │   └── main.cpp       # M5Core2 firmware
│   ├── include/
│   │   └── screens.h      # Screen/UI definitions
│   └── platformio.ini
└── cloud/
    └── main.py            # GCP Cloud Function (logSession + getSession)
```

---

## GitHub Push Policy

Push these:
- `README.md`
- `bridge/bridge.py`, `bridge/requirements.txt`, `bridge/.env.example`
- `cloud/main.py`, `cloud/requirements.txt`
- `m5/src/main.cpp`, `m5/include/screens.h`, `m5/platformio.ini`
- `.gitignore`

Do **not** push these (machine-local, generated, or sensitive):
- Minecraft runtime/state: `world/`, `world_nether/`, `world_the_end/`, `logs/`, `cache/`, `crash-reports/`, `libraries/`, `versions/`
- Local server config/state: `server.properties`, `ops.json`, `whitelist.json`, `banned-ips.json`, `banned-players.json`, `usercache.json`
- Build/editor artifacts: `.vscode/`, `m5/.vscode/`, `m5/.pio/`, `__pycache__/`

---

## Quick Setup (Fresh Clone)

### 1) Clone and install dependencies
```bash
git clone <your-repo-url>
cd m5-minecraft

python3 -m venv .venv
source .venv/bin/activate
pip install -r bridge/requirements.txt
```

### 2) Configure bridge environment
```bash
cp bridge/.env.example bridge/.env
```

Edit `bridge/.env` and set:
- `RCON_PASSWORD` to your local Minecraft server's RCON password
- Optional `CLOUD_LOG_URL` and `CLOUD_GET_URL` if using your own GCP project

Run bridge with env loaded:
```bash
set -a
source bridge/.env
set +a
python bridge/bridge.py
```

### 3) Configure Minecraft server locally (not in git)
In your local `server.properties`:
- `enable-rcon=true`
- `rcon.port=25575`
- `rcon.password=<same value as bridge/.env>`

### 4) Build/flash M5 firmware
```bash
cd m5
pio run -t upload
pio device monitor
```

### 5) Cloud function (optional if using defaults)
Deploy `cloud/main.py` and point `CLOUD_LOG_URL` / `CLOUD_GET_URL` at your endpoints.
