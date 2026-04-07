#pragma once

#include <Arduino.h>
#include <M5Core2.h>

enum class UiMode {
  HUD = 0,
  VIEWER = 1,
  DETAIL = 2,
};

struct GameData {
  float x = 0.0f;
  float y = 0.0f;
  float z = 0.0f;
  String server = "Disconnected";
  int playersOnline = 0;
};

void drawHeader(const String &title);
void drawHudScreen(const GameData &data);
void drawViewerChrome();
void drawViewerStatus(bool connected, int screenshotBytes);
void drawViewerScreen(bool connected, int screenshotBytes);
void drawDetailScreen(const String &detailText);
UiMode handleTouch(UiMode currentMode);
