#include <Arduino.h>
#include <ArduinoJson.h>
#include <BLE2902.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <M5Core2.h>
#include <TJpg_Decoder.h>
#include <Wire.h>

#include <cmath>
#include <vector>

#include "screens.h"

static const char *DEVICE_NAME = "CraftCompanion";
static BLEUUID SERVICE_UUID("12345678-1234-1234-1234-123456789000");
static BLEUUID GAME_DATA_CHAR_UUID("12345678-1234-1234-1234-123456789001");
static BLEUUID SCREENSHOT_CHAR_UUID("12345678-1234-1234-1234-123456789002");
static BLEUUID KEYPRESS_CHAR_UUID("12345678-1234-1234-1234-123456789003");

static const uint32_t SHAKE_DEBOUNCE_MS = 1200;
static const float SHAKE_THRESHOLD = 2.20f;
static const size_t MAX_JPEG_BYTES = 50000;
static const int DETAIL_TEXT_TOP = 30;
static const int DETAIL_TEXT_BOTTOM = 198;

UiMode currentMode = UiMode::HUD;
GameData gameData;
String detailText = "Detail panel is cloud-backed via laptop bridge.";

BLEServer *bleServer = nullptr;
BLECharacteristic *gameDataChar = nullptr;
BLECharacteristic *screenshotChar = nullptr;
BLECharacteristic *keypressChar = nullptr;

bool bridgeConnected = false;
unsigned long lastHudDrawMs = 0;
unsigned long lastShakeMs = 0;
unsigned long lastTouchMs = 0;
float accelX = 0.0f;
float accelY = 0.0f;
float accelZ = 0.0f;

std::vector<uint8_t> jpegBuffer;
uint16_t expectedChunks = 0;
uint16_t chunksReceived = 0;

bool tjpgOutput(int16_t x, int16_t y, uint16_t w, uint16_t h, uint16_t *bitmap) {
  M5.Lcd.pushImage(x, y, w, h, bitmap);
  return true;
}

static void drawHudIfVisible() {
  if (currentMode == UiMode::HUD) {
    drawHudScreen(gameData);
  }
}

static void sendKeypressCommand(const char *cmd) {
  if (!keypressChar || !bridgeConnected) {
    return;
  }
  keypressChar->setValue((uint8_t *)cmd, strlen(cmd));
  keypressChar->notify();
}

static void decodeAndRenderJpeg() {
  if (jpegBuffer.empty()) {
    return;
  }
  if (currentMode != UiMode::VIEWER) {
    currentMode = UiMode::VIEWER;
  }
  drawViewerChrome();
  M5.Lcd.fillRect(0, 24, 320, 182, BLACK);
  TJpgDec.drawJpg(0, 24, jpegBuffer.data(), jpegBuffer.size());
  drawViewerStatus(bridgeConnected, jpegBuffer.size());
}

static void parseAndStoreGameData(const std::string &payload) {
  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    return;
  }

  if (!doc["detail"].isNull()) {
    detailText = String(doc["detail"].as<const char *>());
    if (currentMode == UiMode::DETAIL) {
      drawDetailScreen(detailText);
    }
  }

  if (!doc["server"].isNull()) {
    gameData.server = doc["server"] | "Disconnected";
  }
  if (!doc["player_count"].isNull()) {
    gameData.playersOnline = doc["player_count"] | 0;
  }

  JsonObject coordsObj = doc["coords"].as<JsonObject>();
  if (!coordsObj.isNull() && coordsObj.size() > 0) {
    JsonPair first = *coordsObj.begin();
    JsonObject xyz = first.value().as<JsonObject>();
    if (!xyz.isNull()) {
      gameData.x = xyz["x"] | 0.0f;
      gameData.y = xyz["y"] | 0.0f;
      gameData.z = xyz["z"] | 0.0f;
    }
  }

  drawHudIfVisible();
}

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *) override {
    bridgeConnected = true;
    if (currentMode == UiMode::VIEWER) {
      drawViewerStatus(true, jpegBuffer.size());
    } else if (currentMode == UiMode::HUD) {
      drawHudScreen(gameData);
    } else if (currentMode == UiMode::DETAIL) {
      drawDetailScreen(detailText);
    }
  }

  void onDisconnect(BLEServer *server) override {
    bridgeConnected = false;
    BLEDevice::startAdvertising();
    if (currentMode == UiMode::VIEWER) {
      drawViewerStatus(false, jpegBuffer.size());
    } else if (currentMode == UiMode::HUD) {
      drawHudScreen(gameData);
    } else if (currentMode == UiMode::DETAIL) {
      drawDetailScreen(detailText);
    }
  }
};

class GameDataCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    std::string value = characteristic->getValue();
    if (value.empty()) {
      return;
    }
    parseAndStoreGameData(value);
  }
};

class ScreenshotCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    std::string value = characteristic->getValue();
    if (value.size() < 4) {
      return;
    }

    const uint8_t *data = reinterpret_cast<const uint8_t *>(value.data());
    uint16_t idx = (static_cast<uint16_t>(data[0]) << 8) | data[1];
    uint16_t total = (static_cast<uint16_t>(data[2]) << 8) | data[3];
    size_t payloadLen = value.size() - 4;

    if (idx == 0) {
      jpegBuffer.clear();
      jpegBuffer.reserve(MAX_JPEG_BYTES);
      expectedChunks = total;
      chunksReceived = 0;
    }

    if (idx == total && payloadLen == 0) {
      if (expectedChunks > 0 && chunksReceived >= expectedChunks) {
        decodeAndRenderJpeg();
      }
      return;
    }

    if ((jpegBuffer.size() + payloadLen) > MAX_JPEG_BYTES) {
      return;
    }

    jpegBuffer.insert(jpegBuffer.end(), data + 4, data + value.size());
    chunksReceived++;
  }
};

void drawHeader(const String &title) {
  M5.Lcd.fillRect(0, 0, 320, 24, DARKGREY);
  M5.Lcd.setTextColor(WHITE, DARKGREY);
  M5.Lcd.setCursor(8, 6);
  M5.Lcd.print(title);
}

void drawHudScreen(const GameData &data) {
  M5.Lcd.fillScreen(BLACK);
  drawHeader("CraftCompanion HUD");
  M5.Lcd.setTextColor(GREEN, BLACK);
  M5.Lcd.setCursor(12, 38);
  M5.Lcd.printf("X: %.2f", data.x);
  M5.Lcd.setCursor(12, 68);
  M5.Lcd.printf("Y: %.2f", data.y);
  M5.Lcd.setCursor(12, 98);
  M5.Lcd.printf("Z: %.2f", data.z);
  M5.Lcd.setCursor(12, 138);
  M5.Lcd.printf("Server: %s", data.server.c_str());
  M5.Lcd.setCursor(12, 168);
  M5.Lcd.printf("Players: %d", data.playersOnline);

  M5.Lcd.fillRoundRect(8, 202, 148, 34, 6, DARKCYAN);
  M5.Lcd.fillRoundRect(164, 202, 148, 34, 6, DARKCYAN);
  M5.Lcd.setTextColor(WHITE, DARKCYAN);
  M5.Lcd.setCursor(24, 212);
  M5.Lcd.print("VIEWER");
  M5.Lcd.setCursor(188, 212);
  M5.Lcd.print("DETAIL");
}

void drawViewerScreen(bool connected, int screenshotBytes) {
  drawViewerChrome();
  drawViewerStatus(connected, screenshotBytes);
}

void drawViewerChrome() {
  M5.Lcd.fillScreen(NAVY);
  drawHeader("Viewer");
  M5.Lcd.drawRect(0, 24, 320, 180, WHITE);

  M5.Lcd.fillRoundRect(10, 206, 90, 28, 4, DARKGREEN);
  M5.Lcd.fillRoundRect(115, 206, 90, 28, 4, DARKGREEN);
  M5.Lcd.fillRoundRect(220, 206, 90, 28, 4, DARKGREEN);
  M5.Lcd.setTextColor(WHITE, DARKGREEN);
  M5.Lcd.setCursor(30, 214);
  M5.Lcd.print("LEFT");
  M5.Lcd.setCursor(136, 214);
  M5.Lcd.print("RIGHT");
  M5.Lcd.setCursor(245, 214);
  M5.Lcd.print("JUMP");
}

void drawViewerStatus(bool connected, int screenshotBytes) {
  M5.Lcd.fillRect(150, 0, 170, 24, DARKGREY);
  M5.Lcd.setTextColor(WHITE, DARKGREY);
  M5.Lcd.setCursor(154, 6);
  M5.Lcd.printf("%s %dB", connected ? "ON" : "WAIT", screenshotBytes);
}

static void drawWrappedDetailText(const String &text) {
  const int x = 6;
  int y = DETAIL_TEXT_TOP;
  const int maxWidth = 308;
  const int lineHeight = 16;
  const int maxLines = (DETAIL_TEXT_BOTTOM - DETAIL_TEXT_TOP) / lineHeight;
  const int maxCols = maxWidth / 12;

  String line = "";
  int linesPrinted = 0;

  auto printLine = [&](const String &out) {
    if (linesPrinted >= maxLines) {
      return;
    }
    M5.Lcd.setCursor(x, y);
    M5.Lcd.print(out);
    y += lineHeight;
    linesPrinted++;
  };

  for (size_t i = 0; i < text.length(); i++) {
    char c = text.charAt(i);
    if (c == '\n') {
      printLine(line);
      line = "";
      if (linesPrinted >= maxLines) {
        break;
      }
      continue;
    }

    line += c;
    if (line.length() >= static_cast<size_t>(maxCols)) {
      int split = line.lastIndexOf(' ');
      if (split <= 0) {
        printLine(line);
        line = "";
      } else {
        printLine(line.substring(0, split));
        line = line.substring(split + 1);
      }
      if (linesPrinted >= maxLines) {
        break;
      }
    }
  }

  if (linesPrinted < maxLines && !line.isEmpty()) {
    printLine(line);
  }
}

void drawDetailScreen(const String &text) {
  M5.Lcd.fillScreen(BLACK);
  drawHeader("Session Log");
  M5.Lcd.setTextColor(WHITE, BLACK);
  drawWrappedDetailText(text);

  M5.Lcd.fillRoundRect(8, 202, 304, 34, 6, DARKCYAN);
  M5.Lcd.setTextColor(WHITE, DARKCYAN);
  M5.Lcd.setCursor(102, 212);
  M5.Lcd.print("BACK TO HUD");
}

UiMode handleTouch(UiMode mode) {
  if (!M5.Touch.ispressed()) {
    return mode;
  }

  unsigned long now = millis();
  if (now - lastTouchMs < 400) {
    return mode;
  }
  lastTouchMs = now;

  TouchPoint_t p = M5.Touch.getPressPoint();

  if (mode == UiMode::HUD) {
    if (p.y > 202 && p.x < 160) {
      return UiMode::VIEWER;
    }
    if (p.y > 202 && p.x >= 160) {
      return UiMode::DETAIL;
    }
  }

  if (mode == UiMode::VIEWER) {
    if (p.y > 206) {
      if (p.x < 100) {
        sendKeypressCommand("LEFT");
      } else if (p.x < 210) {
        sendKeypressCommand("RIGHT");
      } else {
        sendKeypressCommand("JUMP");
      }
    } else if (p.y < 24) {
      return UiMode::HUD;
    }
  }

  if (mode == UiMode::DETAIL) {
    if (p.y > 202) {
      return UiMode::HUD;
    }
  }

  return mode;
}

static void updateShake() {
  float mag = sqrtf(accelX * accelX + accelY * accelY + accelZ * accelZ);
  unsigned long now = millis();

  if (mag > SHAKE_THRESHOLD && (now - lastShakeMs) > SHAKE_DEBOUNCE_MS) {
    lastShakeMs = now;
    sendKeypressCommand("REFRESH");
  }
}

static void setupBlePeripheral() {
  BLEDevice::init(DEVICE_NAME);
  bleServer = BLEDevice::createServer();
  bleServer->setCallbacks(new ServerCallbacks());

  BLEService *service = bleServer->createService(SERVICE_UUID);

  gameDataChar = service->createCharacteristic(
      GAME_DATA_CHAR_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  gameDataChar->setCallbacks(new GameDataCallbacks());

  screenshotChar = service->createCharacteristic(
      SCREENSHOT_CHAR_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  screenshotChar->setCallbacks(new ScreenshotCallbacks());

  keypressChar = service->createCharacteristic(
      KEYPRESS_CHAR_UUID,
      BLECharacteristic::PROPERTY_NOTIFY | BLECharacteristic::PROPERTY_READ);
  keypressChar->addDescriptor(new BLE2902());
  keypressChar->setValue("IDLE");

  service->start();

  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE_UUID);
  advertising->setScanResponse(true);
  advertising->start();
}

void setup() {
  M5.begin();
  Wire.begin();
  M5.IMU.Init();
  M5.Lcd.setTextSize(2);

  TJpgDec.setSwapBytes(true);
  TJpgDec.setJpgScale(1);
  TJpgDec.setCallback(tjpgOutput);

  setupBlePeripheral();
  drawHudScreen(gameData);
}

void loop() {
  M5.update();
  M5.IMU.getAccelData(&accelX, &accelY, &accelZ);
  updateShake();

  UiMode nextMode = handleTouch(currentMode);
  if (nextMode != currentMode) {
    currentMode = nextMode;
    if (currentMode == UiMode::HUD) {
      drawHudScreen(gameData);
    } else if (currentMode == UiMode::VIEWER) {
      drawViewerChrome();
      if (!jpegBuffer.empty()) {
        TJpgDec.drawJpg(0, 24, jpegBuffer.data(), jpegBuffer.size());
      }
      drawViewerStatus(bridgeConnected, jpegBuffer.size());
    } else {
      drawDetailScreen(detailText);
    }
  }

  unsigned long now = millis();
  if (currentMode == UiMode::HUD && (now - lastHudDrawMs) > 2000) {
    lastHudDrawMs = now;
    drawHudScreen(gameData);
  }

  delay(30);
}
