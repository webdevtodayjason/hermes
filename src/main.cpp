#include <Arduino.h>
#include <LovyanGFX.hpp>
#include <ArduinoJson.h>
#include <NimBLEDevice.h>
#include <FS.h>
#include <SD_MMC.h>
#include <Wire.h>
#include <WiFi.h>
#include <WebServer.h>
#include <HTTPClient.h>
#include <driver/i2s.h>
#include "generated_frames.h"

// Waveshare ESP32-S3-Touch-LCD-2.8 pins from official ESP-IDF demo.
static constexpr int LCD_MOSI = 45;
static constexpr int LCD_SCLK = 40;
static constexpr int LCD_CS   = 42;
static constexpr int LCD_DC   = 41;
static constexpr int LCD_RST  = 39;
static constexpr int LCD_BL   = 5;
static constexpr int BOOT_BTN = 0;
static constexpr int PWR_KEY_INPUT = 6;
static constexpr int PWR_HOLD = 7;
static constexpr int SD_CLK = 14;
static constexpr int SD_CMD = 17;
static constexpr int SD_D0 = 16;
static constexpr int SD_D3_EN = 21;
static constexpr int BAT_ADC_PIN = 8;
static constexpr float BAT_MEASUREMENT_OFFSET = 0.990476f;
static constexpr int I2S_DOUT = 47;
static constexpr int I2S_BCLK = 48;
static constexpr int I2S_LRC = 38;
static constexpr int SENSOR_SDA = 11;
static constexpr int SENSOR_SCL = 10;
static constexpr int TOUCH_SDA = 1;
static constexpr int TOUCH_SCL = 3;
static constexpr int TOUCH_INT = 4;
static constexpr int TOUCH_RST = 2;
static constexpr uint8_t CST328_ADDR = 0x1A;
static constexpr uint8_t PCF85063_ADDR = 0x51;
static constexpr uint8_t QMI8658_ADDR = 0x6B;

static constexpr int16_t W = 240;
static constexpr int16_t H = 320;
static constexpr int16_t CONSOLE_Y = 252;
static constexpr int16_t SWIPE_MIN_DX = 45;
static constexpr int16_t SWIPE_MAX_DY = 55;
// Temporary diagnostic: show palette candidates so we can lock the exact
// ST7789 color path from a real photo instead of guessing.
static constexpr bool COLOR_CALIBRATION_MODE = false;

class LGFX : public lgfx::LGFX_Device {
  lgfx::Panel_ST7789 _panel;
  lgfx::Bus_SPI _bus;
  lgfx::Light_PWM _light;
  lgfx::Touch_CST816S _touch;
public:
  LGFX() {
    auto b = _bus.config();
    b.spi_host = SPI3_HOST;
    b.spi_mode = 0;
    b.freq_write = 80000000;
    b.freq_read = 16000000;
    b.spi_3wire = false;
    b.use_lock = true;
    b.dma_channel = SPI_DMA_CH_AUTO;
    b.pin_sclk = LCD_SCLK;
    b.pin_mosi = LCD_MOSI;
    b.pin_miso = -1;
    b.pin_dc = LCD_DC;
    _bus.config(b);
    _panel.setBus(&_bus);

    auto p = _panel.config();
    p.pin_cs = LCD_CS;
    p.pin_rst = LCD_RST;
    p.pin_busy = -1;
    p.panel_width = W;
    p.panel_height = H;
    p.memory_width = W;
    p.memory_height = H;
    p.offset_x = 0;
    p.offset_y = 0;
    p.offset_rotation = 0;
    p.dummy_read_pixel = 8;
    p.dummy_read_bits = 1;
    p.readable = false;
    // Keep panel inversion OFF. The generated art is already black/green;
    // inverting the ST7789 turns it into the washed-out cyan-on-white image.
    p.invert = false;
    p.rgb_order = false; // BGR, matching Waveshare ESP-IDF demo
    p.dlen_16bit = false;
    p.bus_shared = false;
    _panel.config(p);

    auto l = _light.config();
    l.pin_bl = LCD_BL;
    l.invert = false;
    l.freq = 5000;
    l.pwm_channel = 7;
    _light.config(l);
    _panel.setLight(&_light);

    // Current V2 boards use a CST3530-class controller. LovyanGFX's CST816S
    // driver speaks the compatible 0x15 touch protocol used by community
    // working examples for this board family (SDA=48, SCL=47).
    auto t = _touch.config();
    t.x_min = 0;
    t.x_max = W - 1;
    t.y_min = 0;
    t.y_max = H - 1;
    t.i2c_port = 0;
    t.i2c_addr = 0x15;
    t.pin_sda = 48;
    t.pin_scl = 47;
    t.pin_int = -1;
    t.pin_rst = -1;
    t.bus_shared = false;
    t.offset_rotation = 0;
    t.freq = 400000;
    _touch.config(t);
    _panel.setTouch(&_touch);

    setPanel(&_panel);
  }
};

LGFX lcd;
LGFX *gfx = &lcd;
WebServer httpServer(80);

// Nordic UART Service UUIDs, same shape as Claude Desktop Buddy.
#define NUS_SERVICE_UUID "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_RX_UUID      "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
#define NUS_TX_UUID      "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

static NimBLECharacteristic *txChr = nullptr;
static bool bleConnected = false;
static bool sdReady = false;
static uint32_t sdMB = 0;
static String sdStatus = "SD:checking";
static bool audioReady = false;
static bool imuReady = false;
static bool imuCfgOk = false;   // config writes verified by read-back
static uint8_t imuStatus0 = 0;  // last STATUS0 (bit0 = accel data ready)
static uint8_t imuRev = 0;      // revision reg 0x01 (expect 0x7C/0x7B)
static bool rtcReady = false;
static bool wifiReady = false;
static String wifiStatus = "WiFi:off";
static String rtcClock = "RTC:--";
static float batVolts = 0.0f;
static float accelX = 0.0f, accelY = 0.0f, accelZ = 0.0f;
static float lastAccelMag = 1.0f;
static uint32_t lastSensorMs = 0;
static uint32_t lastShakeMs = 0;
// Physical-gesture state (IMU, 300ms poll). Thresholds are calibration
// knobs — g-units on this QMI8658 at ±4g/8192 LSB. Face-down is detected
// as attitude vs the LEARNED resting orientation (normalized dot product),
// so chip mounting sign and stand angle don't matter.
static bool quietMode = false;            // flipped-over = do-not-disturb
static float baseX = 0, baseY = 0, baseZ = 0;  // resting gravity vector
static bool baseSet = false;
static uint8_t basePolls = 0;
static uint8_t faceDownPolls = 0;         // consecutive dot<-0.35 samples
static uint8_t uprightPolls = 0;
static uint32_t lastMotionMs = 0;         // any mag-delta above noise floor
static uint32_t prevTapPulseMs = 0;       // first pulse of a double-tap pair
static uint32_t lastPickupMs = 0;
static bool approvalNagMuted = false;     // tap2 or routed-quiet approval
static uint32_t lastTelemetryMs = 0;
static uint8_t activeQmiAddr = QMI8658_ADDR;
static bool manualTouchReady = false;
static String touchStatus = "TP:checking";
static char forcedGroup[12] = "";
static uint32_t forcedStartMs = 0;
static uint32_t forcedUntilMs = 0;
static uint8_t localMood = 0;
static uint32_t nextBlinkMs = 7000;

// Pip-Boy landscape UI: the device sits rotated 90° CCW (USB to the side),
// content rotates 90° CW to compensate. Full-screen tabbed pages, swipe or
// tap the tab bar to move. Logical canvas: 320x240.
enum UiPage : uint8_t {
  PAGE_FACE  = 0,
  PAGE_MSGS  = 1,
  PAGE_OPS   = 2,
  PAGE_FLEET = 3,
  PAGE_CRON  = 4,
  PAGE_NET   = 5,
  PAGE_DEV   = 6,
  PAGE_COUNT = 7,
};
static const char* const PAGE_TABS[PAGE_COUNT] = {"FACE", "MSGS", "OPS", "FLEET", "CRON", "NET", "DEV"};

static constexpr int16_t LW = 320;        // logical landscape width
static constexpr int16_t LH = 240;        // logical landscape height
static constexpr int16_t TAB_H = 18;      // tab bar height
static constexpr int16_t FACE_ROW0 = 10;  // first portrait-art row shown on FACE

// Rotation index for gfx->setRotation(). 1 = content 90° CW (device rotated
// CCW); if the screen comes up upside down, set {"display":{"rotation":3}}
// over serial config — no reflash. 0 restores the legacy portrait UI's
// orientation (the UI itself stays landscape).
static uint8_t uiRot = 1;

static UiPage uiPage = PAGE_FACE;
static String serialLine;
static String bleLine;

static constexpr size_t MAX_LINE = 2048;

struct PendingAction {
  bool active = false;
  String id;
  String text;
  String detail;    // the raw command behind the approval, host-compacted
  String choices[3];
  uint8_t choiceCount = 0;
};

static void sendLine(const String &line);
static void applyJsonLine(const String &line);
static void triggerNamedMood(const char* reason, const char* mood, bool updateMsg = true, bool emit = true);
static void chirp(const char* kind);
static void goPage(int delta);
static void handleTap(uint16_t tx, uint16_t ty);
struct BuddyState {
  bool connected = false;
  int total = 0;
  int running = 0;
  int waiting = 0;
  uint32_t tokensToday = 0;
  uint32_t toolsToday = 0;
  String jobState = "idle";
  String jobLabel = "Status brief";
  PendingAction action;
  String msg = "Awaiting Hermes stream";
  String entries[5];
  int entryCount = 0;
  uint32_t lastSeenMs = 0;
  uint32_t lastTouchMs = 0;
  uint16_t touchX = 0;
  uint16_t touchY = 0;
  bool dirty = true;
} st;

// Toast: host-pushed banner that takes over the console band on ANY page for a
// few seconds (incoming replies + familiar_notify pings), so activity is never
// invisible just because the user is on the wrong page.
static String toastText;
static uint32_t toastUntilMs = 0;

// Host-rendered info pages (slot 0 = cron jobs, slot 1 = gateway vitals).
// The host formats the lines; firmware just displays them.
struct HostPage {
  String title;
  String l1;
  String l2;
  String l3;
  bool set = false;
};
static HostPage hostPages[3];   // 0 cron, 1 gateway vitals, 2 fleet
// Transient page (host slot 9, or local approval detail): drawn over any
// tab; the next tap closes it and returns to where you were.
static HostPage modal;
static String modalBody;        // long-text mode (approval detail)
static bool modalActive = false;
static UiPage modalReturn = PAGE_FACE;

// THE DECK: host-defined programmable buttons on the OPS tab (3x2 grid).
// confirm-flagged buttons arm on first tap ("SURE?") and fire on the second.
struct DeckButton {
  String label;
  uint8_t color = 0;      // 0 green, 1 amber, 2 red, 3 cyan
  bool confirm = false;
};
static DeckButton deck[6];
static uint8_t deckCount = 0;
static int8_t deckArmed = -1;          // button awaiting its confirm tap
static uint32_t deckArmedUntilMs = 0;  // arm window
static int8_t jobIndex = -1;           // which deck button's job is running

// Untethered: when USB goes silent and Wi-Fi is up, dial home over TCP
// (same newline-JSON protocol). Host address provisioned over USB into
// /hermes-buddy/config.json {"host":{"ip","port","token"}}.
static String hostIp;
static uint16_t hostPort = 8767;
static String hostToken;   // transport auth — first line on TCP dial-home
static WiFiClient tcpLink;
static String tcpLine;
static uint32_t lastTcpTryMs = 0;
static uint32_t lastSerialRxMs = 0;   // bytes on USB specifically (TCP must not count)
static bool backlightDim = false;

// MSGS scrollback: host keeps 40 lines; device pages a 5-line window.
static String msgsLines[5];
static uint8_t msgsCount = 0;
static uint16_t msgsOff = 0;
static uint16_t msgsTotal = 0;

static uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return gfx->color565(r, g, b); }
static uint16_t bswap565(uint16_t v) { return (uint16_t)((v << 8) | (v >> 8)); }
// Calibrated from on-device photos: Page 5/8 was correct, meaning
// LCD inversion ON with normal RGB565 values.
static const uint16_t BG     = 0x0000; // black
static const uint16_t PANEL  = 0x0040; // very dark green
static const uint16_t INK    = 0x7FEE; // pale terminal green
static const uint16_t DIM    = 0x0220; // dim phosphor
static const uint16_t VIOLET = 0x03E0; // medium green border
static const uint16_t CYAN   = 0x07F5; // cyan accent if needed
static const uint16_t GREEN  = 0x57EA; // bright phosphor green
static const uint16_t ORANGE = 0xFCA0; // orange warning
static const uint16_t RED    = 0xF965; // red alert
static const uint16_t GOLD   = 0xFEE0; // gold

static void drawWrapped(const String &text, int x, int y, int maxChars, int maxLines, uint16_t color, uint8_t size = 1) {
  gfx->setTextSize(size);
  gfx->setTextColor(color, BG);
  int line = 0;
  String rest = text;
  while (rest.length() && line < maxLines) {
    String chunk = rest.substring(0, min((int)rest.length(), maxChars));
    int cut = chunk.lastIndexOf(' ');
    if ((int)rest.length() > maxChars && cut > maxChars / 2) chunk = chunk.substring(0, cut);
    gfx->setCursor(x, y + line * (8 * size + 4));
    gfx->print(chunk);
    rest.remove(0, chunk.length());
    rest.trim();
    line++;
  }
}

static void drawHermesCore(int cx, int cy, uint16_t color, bool live) {
  // A tiny Hermes sigil: winged core + orbit + expressive eyes.
  gfx->drawCircle(cx, cy, 45, DIM);
  gfx->drawCircle(cx, cy, 36, color);
  gfx->drawRoundRect(cx - 28, cy - 24, 56, 48, 12, color);
  gfx->drawLine(cx - 44, cy - 10, cx - 70, cy - 28, color);
  gfx->drawLine(cx - 44, cy + 0,  cx - 76, cy - 4, color);
  gfx->drawLine(cx - 44, cy + 10, cx - 68, cy + 24, color);
  gfx->drawLine(cx + 44, cy - 10, cx + 70, cy - 28, color);
  gfx->drawLine(cx + 44, cy + 0,  cx + 76, cy - 4, color);
  gfx->drawLine(cx + 44, cy + 10, cx + 68, cy + 24, color);
  gfx->setTextSize(2);
  gfx->setTextColor(color, BG);
  gfx->setCursor(cx - 7, cy - 8);
  gfx->print("H");

  if (!live) {
    gfx->drawLine(cx - 21, cy - 10, cx - 12, cy - 10, DIM);
    gfx->drawLine(cx + 12, cy - 10, cx + 21, cy - 10, DIM);
    gfx->setTextSize(1);
    gfx->setCursor(cx - 17, cy + 28); gfx->print("sleep");
  } else if (st.waiting > 0) {
    gfx->fillCircle(cx - 18, cy - 10, 5, RED);
    gfx->fillCircle(cx + 18, cy - 10, 5, RED);
    gfx->drawLine(cx - 20, cy + 18, cx + 20, cy + 18, RED);
  } else if (st.running > 0) {
    gfx->fillCircle(cx - 18, cy - 10, 5, GOLD);
    gfx->fillCircle(cx + 18, cy - 10, 5, GOLD);
    gfx->drawCircle(cx, cy + 17, 10, GOLD);
  } else {
    gfx->fillCircle(cx - 18, cy - 10, 5, CYAN);
    gfx->fillCircle(cx + 18, cy - 10, 5, CYAN);
    gfx->drawLine(cx - 18, cy + 14, cx - 6, cy + 23, GREEN);
    gfx->drawLine(cx - 6, cy + 23, cx + 6, cy + 23, GREEN);
    gfx->drawLine(cx + 6, cy + 23, cx + 18, cy + 14, GREEN);
  }
}

static int frameForState(bool live) {
  if (!live) return 5;          // sleep
  if (st.waiting > 0) return 2; // alert
  if (st.running > 0) return 4; // thinking
  return 1;                     // idle
}

static void drawGeneratedFrame(int idx, int16_t yDst = 0, int rowStart = 0, int rowCount = HERMES_FRAME_H) {
  int count = sizeof(HERMES_FRAMES) / sizeof(HERMES_FRAMES[0]);
  if (idx < 0 || idx >= count) idx = 1;
  const uint8_t* data = HERMES_FRAMES[idx].data;
  uint16_t line[HERMES_FRAME_W];
  int rowEnd = min(rowStart + rowCount, (int)HERMES_FRAME_H);
  // LovyanGFX text/fill primitives send RGB565 correctly, but pushImage() uses
  // the raw 16-bit buffer byte order. Without swapBytes, green frame pixels show
  // up as red/orange on this ESP32-S3/ST7789 path.
  gfx->setSwapBytes(true);
  for (int y = rowStart; y < rowEnd; ++y) {
    for (int x = 0; x < HERMES_FRAME_W; x += 2) {
      uint8_t b = pgm_read_byte(data + y * (HERMES_FRAME_W / 2) + (x / 2));
      line[x] = HERMES_TERMINAL_PALETTE[(b >> 4) & 0x0F];
      line[x + 1] = HERMES_TERMINAL_PALETTE[b & 0x0F];
    }
    gfx->pushImage(0, yDst + (y - rowStart), HERMES_FRAME_W, 1, line);
  }
  gfx->setSwapBytes(false);
}

static const char* stateGroup(bool live) {
  if (forcedUntilMs && millis() < forcedUntilMs) return forcedGroup;
  if (forcedUntilMs && millis() >= forcedUntilMs) { forcedUntilMs = 0; forcedGroup[0] = 0; }
  if (!live) {
    // Don't make the physical familiar feel dead just because the desktop
    // bridge is quiet. Stay awake/idle for desk-pet interaction, then nap
    // only after a long untouched idle period.
    if (millis() > 180000 && millis() - st.lastTouchMs > 120000) return "sleep";
    return "idle";
  }
  if (st.waiting > 0) return "waiting";
  if (st.running > 0) return "thinking";
  return "idle";
}

static int stateFrameCount(const char* group) {
  if (!strcmp(group, "sleep")) return 3;
  if (!strcmp(group, "thinking")) return 4;
  if (!strcmp(group, "waiting")) return 2;
  if (!strcmp(group, "blink")) return 3;
  if (!strcmp(group, "wink")) return 2;
  if (!strcmp(group, "smile")) return 2;
  if (!strcmp(group, "happy")) return 3;
  return 1;
}

static uint32_t stateFrameMs(const char* group) {
  if (!strcmp(group, "sleep")) return 700;
  if (!strcmp(group, "thinking")) return 220;
  if (!strcmp(group, "waiting")) return 320;
  if (!strcmp(group, "blink")) return 160;
  if (!strcmp(group, "wink")) return 260;
  if (!strcmp(group, "smile")) return 450;
  if (!strcmp(group, "happy")) return 180;
  return 1000;
}

static int frameForSDGroup(const char* group, int count, uint32_t ms) {
  if (forcedUntilMs && !strcmp(group, forcedGroup)) {
    uint32_t e = millis() - forcedStartMs;
    // release frames sized to the (snappy) triggerNamedMood holds
    if (!strcmp(group, "wink")) return e < 550 ? 0 : 1;    // wink, un-wink
    if (!strcmp(group, "smile")) return e < 1100 ? 0 : 1;  // smile, neutral
    if (!strcmp(group, "happy")) return e < 1200 ? min(1, count - 1) : (count - 1);
    if (!strcmp(group, "blink")) return e < 120 ? 0 : (e < 260 ? 1 : min(2, count - 1));
  }
  return count > 1 ? ((millis() / ms) % count) : 0;
}

static bool drawSDRaw4(const char* group, int16_t yDst = 0, int rowStart = 0, int rowCount = HERMES_FRAME_H) {
  if (!sdReady) return false;
  int count = stateFrameCount(group);
  uint32_t ms = stateFrameMs(group);
  int frame = frameForSDGroup(group, count, ms);
  char path[96];
  snprintf(path, sizeof(path), "/hermes-buddy/frames/%s/%03d.raw4", group, frame);
  if (!SD_MMC.exists(path)) return false;
  File f = SD_MMC.open(path, FILE_READ);
  if (!f) return false;
  if (f.size() < (HERMES_FRAME_W * HERMES_FRAME_H / 2)) { f.close(); return false; }

  uint8_t packed[HERMES_FRAME_W / 2];
  uint16_t line[HERMES_FRAME_W];
  int rowEnd = min(rowStart + rowCount, (int)HERMES_FRAME_H);
  if (rowStart > 0) f.seek((uint32_t)rowStart * (HERMES_FRAME_W / 2));
  gfx->setSwapBytes(true);
  for (int y = rowStart; y < rowEnd; ++y) {
    int got = f.read(packed, sizeof(packed));
    if (got != (int)sizeof(packed)) { gfx->setSwapBytes(false); f.close(); return false; }
    for (int x = 0; x < HERMES_FRAME_W; x += 2) {
      uint8_t b = packed[x / 2];
      line[x] = HERMES_TERMINAL_PALETTE[(b >> 4) & 0x0F];
      line[x + 1] = HERMES_TERMINAL_PALETTE[b & 0x0F];
    }
    gfx->pushImage(0, yDst + (y - rowStart), HERMES_FRAME_W, 1, line);
  }
  gfx->setSwapBytes(false);
  f.close();
  return true;
}

static void triggerNamedMood(const char* reason, const char* mood, bool updateMsg, bool emit) {
  strncpy(forcedGroup, mood, sizeof(forcedGroup) - 1);
  forcedGroup[sizeof(forcedGroup) - 1] = 0;
  forcedStartMs = millis();
  // Emote beats, not poses: a wink lands and releases. (Jason: "a wink
  // should be like a wink" — was 2300/3300 and read as a 3-second leer.)
  uint32_t hold = 1000;
  if (!strcmp(mood, "wink")) hold = 700;
  else if (!strcmp(mood, "smile")) hold = 1300;
  else if (!strcmp(mood, "happy")) hold = 1500;
  else if (!strcmp(mood, "blink")) hold = 380;
  forcedUntilMs = forcedStartMs + hold;
  if (strcmp(reason, "auto") && strcmp(reason, "host")) st.lastTouchMs = millis();
  if (updateMsg) st.msg = String(reason) + ": " + mood;
  st.dirty = true;
  if (emit) sendLine(String("{\"event\":\"local_") + reason + "\",\"mood\":\"" + mood + "\"}");
}

static void triggerLocalMood(const char* reason) {
  const char* moods[] = {"wink", "smile", "happy"};
  triggerNamedMood(reason, moods[localMood++ % 3]);
}

static void drawCalibrationPage() {
  static uint8_t lastPage = 255;
  uint8_t page = (millis() / 4000) % 8;
  if (page == lastPage) return;
  lastPage = page;

  bool lcdInv = page >= 4;
  uint8_t mode = page % 4;
  const char* modeName = mode == 0 ? "NORMAL RGB565" :
                         mode == 1 ? "INVERTED RGB565" :
                         mode == 2 ? "BYTE-SWAP RGB565" :
                                     "SWAP+INVERT RGB565";
  gfx->invertDisplay(lcdInv);

  uint16_t desired[8] = {0x0000, 0x0020, 0x00E0, 0x03E0, 0x07E0, 0x57EA, 0xFFFF, 0xF800};
  auto mapColor = [&](uint16_t c) -> uint16_t {
    if (mode == 1) c = ~c;
    else if (mode == 2) c = bswap565(c);
    else if (mode == 3) c = bswap565(~c);
    return c;
  };

  gfx->fillScreen(mapColor(0x0000));
  gfx->setTextSize(1);
  gfx->setTextColor(mapColor(0x07E0), mapColor(0x0000));
  gfx->setCursor(10, 10);
  gfx->printf("PAGE %u/8", page + 1);
  gfx->setCursor(10, 26);
  gfx->printf("LCD INV: %s", lcdInv ? "ON" : "OFF");
  gfx->setCursor(10, 42);
  gfx->print(modeName);
  gfx->setCursor(10, 58);
  gfx->print("Find black bg + green bars");

  int y = 86;
  for (int i = 0; i < 8; ++i) {
    gfx->fillRect(18, y + i * 25, 118, 18, mapColor(desired[i]));
    gfx->drawRect(18, y + i * 25, 118, 18, mapColor(0xFFFF));
    gfx->setTextColor(mapColor(0xFFFF), mapColor(0x0000));
    gfx->setCursor(146, y + i * 25 + 5);
    gfx->printf("0x%04X", desired[i]);
  }
}

static bool cstWrite(uint16_t reg, const uint8_t* data, size_t len) {
  Wire1.beginTransmission(CST328_ADDR);
  Wire1.write((uint8_t)(reg >> 8));
  Wire1.write((uint8_t)(reg & 0xFF));
  for (size_t i = 0; i < len; ++i) Wire1.write(data[i]);
  return Wire1.endTransmission(true) == 0;
}

static bool cstRead(uint16_t reg, uint8_t* data, size_t len) {
  Wire1.beginTransmission(CST328_ADDR);
  Wire1.write((uint8_t)(reg >> 8));
  Wire1.write((uint8_t)(reg & 0xFF));
  if (Wire1.endTransmission(true) != 0) return false;
  size_t got = Wire1.requestFrom((int)CST328_ADDR, (int)len);
  if (got != len) return false;
  for (size_t i = 0; i < len; ++i) data[i] = Wire1.read();
  return true;
}

static void initManualTouch() {
  Wire1.begin(TOUCH_SDA, TOUCH_SCL, 400000);
  pinMode(TOUCH_INT, INPUT);
  pinMode(TOUCH_RST, OUTPUT);
  digitalWrite(TOUCH_RST, HIGH); delay(50);
  digitalWrite(TOUCH_RST, LOW); delay(5);
  digitalWrite(TOUCH_RST, HIGH); delay(60);

  uint8_t dummy = 0;
  cstWrite(0xD101, &dummy, 0); // debug/info mode per Waveshare demo
  uint8_t buf[24] = {0};
  bool ok = cstRead(0xD1FC, buf, 4) && cstRead(0xD1F4, buf, 24);
  uint16_t verification = ((uint16_t)buf[11] << 8) | buf[10];
  manualTouchReady = ok && verification == 0xCACA;
  if (manualTouchReady) {
    cstWrite(0xD109, &dummy, 0); // normal mode
    touchStatus = "TP:CST328";
    Serial.println("{\"touch\":\"ok\",\"driver\":\"cst328\"}");
  } else {
    touchStatus = String("TP:fail ") + String(verification, HEX);
    Serial.printf("{\"touch\":\"fail\",\"verify\":%u}\n", verification);
  }
}

static bool readManualTouch(uint16_t* x, uint16_t* y) {
  if (!manualTouchReady) return false;
  uint8_t n = 0;
  uint8_t clear = 0;
  if (!cstRead(0xD005, &n, 1)) return false;
  uint8_t cnt = n & 0x0F;
  if (cnt == 0 || cnt > 5) {
    cstWrite(0xD005, &clear, 1);
    return false;
  }
  uint8_t buf[28] = {0};
  if (!cstRead(0xD000, &buf[1], 27)) return false;
  cstWrite(0xD005, &clear, 1);
  uint16_t rawX = ((uint16_t)buf[2] << 4) + ((buf[4] & 0xF0) >> 4);
  uint16_t rawY = ((uint16_t)buf[3] << 4) + (buf[4] & 0x0F);
  // The CST328 reports native portrait coords (0..239, 0..319); map into the
  // rotated logical canvas so the rest of the UI thinks in landscape.
  switch (uiRot) {
    case 1: *x = rawY;          *y = (W - 1) - rawX; break;
    case 3: *x = (H - 1) - rawY; *y = rawX;          break;
    case 2: *x = (W - 1) - rawX; *y = (H - 1) - rawY; break;
    default: *x = rawX; *y = rawY; break;
  }
  return true;
}

static bool i2cRead8(uint8_t addr, uint8_t reg, uint8_t* value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(true) != 0) return false;
  if (Wire.requestFrom((int)addr, 1) != 1) return false;
  *value = Wire.read();
  return true;
}

static bool i2cReadBytes(uint8_t addr, uint8_t reg, uint8_t* data, size_t len) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(true) != 0) return false;
  if (Wire.requestFrom((int)addr, (int)len) != len) return false;
  for (size_t i = 0; i < len; ++i) data[i] = Wire.read();
  return true;
}

static bool i2cWrite8(uint8_t addr, uint8_t reg, uint8_t value) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission(true) == 0;
}

static void initAudio() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  cfg.sample_rate = 16000;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count = 4;
  cfg.dma_buf_len = 128;
  cfg.use_apll = false;
  cfg.tx_desc_auto_clear = true;
  if (i2s_driver_install(I2S_NUM_0, &cfg, 0, nullptr) == ESP_OK) {
    i2s_pin_config_t pins = {};
    pins.bck_io_num = I2S_BCLK;
    pins.ws_io_num = I2S_LRC;
    pins.data_out_num = I2S_DOUT;
    pins.data_in_num = I2S_PIN_NO_CHANGE;
    audioReady = (i2s_set_pin(I2S_NUM_0, &pins) == ESP_OK);
    if (!audioReady) i2s_driver_uninstall(I2S_NUM_0);
  }
  Serial.printf("{\"audio\":\"%s\",\"mode\":\"i2s-chirps\"}\n", audioReady ? "ok" : "off");
}

static void chirpTone(uint16_t hz, uint16_t ms) {
  if (!audioReady) return;
  const uint32_t rate = 16000;
  const uint32_t samples = (rate * ms) / 1000;
  int16_t buf[128];
  uint32_t halfPeriod = max<uint32_t>(1, rate / (hz * 2));
  for (uint32_t done = 0; done < samples;) {
    size_t n = min<uint32_t>(128, samples - done);
    for (size_t i = 0; i < n; ++i) {
      uint32_t phase = ((done + i) / halfPeriod) & 1;
      int16_t amp = (phase ? 1800 : -1800);
      uint32_t s = done + i;
      if (s < 24) amp = (int16_t)((int32_t)amp * s / 24);
      if (samples > s && samples - s < 24) amp = (int16_t)((int32_t)amp * (samples - s) / 24);
      buf[i] = amp;
    }
    size_t written = 0;
    i2s_write(I2S_NUM_0, buf, n * sizeof(int16_t), &written, pdMS_TO_TICKS(20));
    done += n;
  }
  i2s_zero_dma_buffer(I2S_NUM_0);
}

static volatile bool sayBusy = false;

static void chirp(const char* kind) {
  if (quietMode) return; // face-down = silent, no exceptions
  if (sayBusy) return;  // never chirp over speech
  if (!strcmp(kind, "boot")) { chirpTone(740, 35); chirpTone(988, 45); }
  else if (!strcmp(kind, "tap")) chirpTone(1200, 20);
  else if (!strcmp(kind, "alert")) { chirpTone(988, 35); chirpTone(740, 60); }
  else if (!strcmp(kind, "ack")) { chirpTone(880, 25); chirpTone(1320, 35); }
  else chirpTone(880, 25);
}

// ---- speech: stream raw s16le 16kHz mono PCM from a LAN URL into I2S ------
// The host renders TTS to exactly the I2S format, so playback is a straight
// HTTP read -> i2s_write pipe on its own FreeRTOS task (UI stays live).

static String sayPendingUrl;

static void sayTask(void*) {
  HTTPClient http;
  http.setConnectTimeout(3000);
  if (http.begin(sayPendingUrl)) {
    int code = http.GET();
    if (code == 200) {
      WiFiClient* stream = http.getStreamPtr();
      int total = http.getSize();
      int got = 0;
      uint8_t buf[1024];
      uint32_t lastData = millis();
      while (http.connected() && (total < 0 || got < total) && millis() - lastData < 3000) {
        size_t avail = stream->available();
        if (avail) {
          int n = stream->readBytes(buf, min(avail, sizeof(buf)));
          size_t written = 0;
          i2s_write(I2S_NUM_0, buf, n, &written, pdMS_TO_TICKS(500));
          got += n;
          lastData = millis();
        } else {
          vTaskDelay(pdMS_TO_TICKS(5));
        }
      }
      i2s_zero_dma_buffer(I2S_NUM_0);
    } else {
      Serial.printf("{\"say\":\"http-%d\"}\n", code);
    }
    http.end();
  }
  sayBusy = false;
  vTaskDelete(nullptr);
}

static void startSay(const char* url) {
  if (quietMode) return; // face-down mutes speech too
  if (!url || !url[0]) return;
  if (!wifiReady) { Serial.println("{\"say\":\"no-wifi\"}"); return; }
  if (!audioReady || sayBusy) return;
  sayPendingUrl = url;
  sayBusy = true;
  if (xTaskCreatePinnedToCore(sayTask, "say", 8192, nullptr, 1, nullptr, 0) != pdPASS) {
    sayBusy = false;
  }
}

static void initPowerDiagnostics() {
  pinMode(BAT_ADC_PIN, INPUT);
  analogSetPinAttenuation(BAT_ADC_PIN, ADC_11db);
  batVolts = analogReadMilliVolts(BAT_ADC_PIN) * 2.0f * BAT_MEASUREMENT_OFFSET / 1000.0f;
}

static void initMotionAndRtc() {
  // Wire is already bound to the hunt's winning pins by buildI2cScan()
  // QMI8658 bring-up: byte-faithful mirror of Waveshare's shipped demo for
  // THIS board (~/Projects/waveshare-esp32s3-touch-lcd-28 Gyro_QMI8658.cpp),
  // which is the only sequence proven to produce data here. Notably the demo
  // NEVER soft-resets (an interrupted reset wedges the chip until full
  // power-off) and enables CTRL7 BEFORE writing scale/ODR/LPF config.
  uint8_t who = 0;
  uint8_t rb[3] = {0xFF, 0xFF, 0xFF};
  imuReady = false;
  for (uint8_t addr : {(uint8_t)0x6B, (uint8_t)0x6A}) {
    if (i2cRead8(addr, 0x00, &who) && who == 0x05) {   // WHO_AM_I gate
      imuReady = true;
      activeQmiAddr = addr;
      break;
    }
  }
  if (imuReady) {
    uint8_t c1 = 0;
    i2cRead8(activeQmiAddr, 0x02, &c1);
    c1 = (c1 & 0xFE) | 0x40;                  // osc on + addr auto-increment
    i2cWrite8(activeQmiAddr, 0x02, c1);       // CTRL1
    const uint8_t seq[][2] = {
        {0x08, 0x43},   // CTRL7: hs-clock | gEN | aEN — demo enables FIRST
        {0x07, 0x00},   // CTRL6: AttitudeEngine MOD off
        {0x03, 0x10},   // CTRL2: accel ±4g @ 8000Hz (demo defaults)
        {0x04, 0x20},   // CTRL3: gyro ±64dps @ 8000Hz (demo defaults)
        {0x06, 0x71},   // CTRL5: gyro LPF mode3 + accel LPF mode0, both ON
    };
    imuCfgOk = true;
    for (auto &s : seq) {
      i2cWrite8(activeQmiAddr, s[0], s[1]);
      uint8_t r = 0xFF;
      if (!i2cRead8(activeQmiAddr, s[0], &r) || r != s[1]) imuCfgOk = false;
    }
    i2cRead8(activeQmiAddr, 0x02, &rb[0]);
    i2cRead8(activeQmiAddr, 0x03, &rb[1]);
    i2cRead8(activeQmiAddr, 0x08, &rb[2]);
    i2cRead8(activeQmiAddr, 0x01, &imuRev);
    delay(20);
  }
  uint8_t rtcprobe = 0;
  rtcReady = i2cRead8(PCF85063_ADDR, 0x04, &rtcprobe);
  Serial.printf("{\"imu\":\"%s\",\"imu_addr\":%u,\"who\":%u,\"rb\":[%u,%u,%u],"
                "\"imu_cfg\":\"%s\",\"rtc\":\"%s\"}\n",
                imuReady ? "ok" : "off", activeQmiAddr, who, rb[0], rb[1], rb[2],
                imuCfgOk ? "ok" : "FAIL", rtcReady ? "ok" : "off");
}

static uint8_t bcd2(uint8_t v) { return ((v >> 4) * 10) + (v & 0x0F); }

static void pollPeripheralSensors() {
  uint32_t now = millis();
  if (now - lastSensorMs < 300) return;
  lastSensorMs = now;
  batVolts = analogReadMilliVolts(BAT_ADC_PIN) * 2.0f * BAT_MEASUREMENT_OFFSET / 1000.0f;
  wifiReady = WiFi.status() == WL_CONNECTED;
  if (wifiReady) wifiStatus = String("WiFi:") + WiFi.localIP().toString();
  if (rtcReady) {
    uint8_t t[3] = {0};
    if (i2cReadBytes(PCF85063_ADDR, 0x04, t, 3)) {
      char buf[12];
      snprintf(buf, sizeof(buf), "%02u:%02u:%02u", bcd2(t[2] & 0x3F), bcd2(t[1] & 0x7F), bcd2(t[0] & 0x7F));
      rtcClock = String("RTC:") + buf;
    }
  }
  if (imuReady) {
    uint8_t st0 = 0;
    i2cRead8(activeQmiAddr, 0x2E, &st0);   // STATUS0 — diagnostic only; the
    imuStatus0 = st0;                      // demo reads outputs unconditionally
    uint8_t raw[6] = {0};
    if (i2cReadBytes(activeQmiAddr, 0x35, raw, 6)) {
      int16_t ax = (int16_t)((raw[1] << 8) | raw[0]);
      int16_t ay = (int16_t)((raw[3] << 8) | raw[2]);
      int16_t az = (int16_t)((raw[5] << 8) | raw[4]);
      accelX = ax / 8192.0f;   // CTRL2 0x10 = ±4g -> 8192 LSB/g
      accelY = ay / 8192.0f;
      accelZ = az / 8192.0f;
      float mag = sqrtf(accelX * accelX + accelY * accelY + accelZ * accelZ);
      float delta = fabsf(mag - lastAccelMag);
      uint32_t prevMotionMs = lastMotionMs;    // before this sample
      if (delta > 0.10f) lastMotionMs = now;   // noise floor — calibration knob

      // learn the resting attitude: ~3s of stillness (scale-agnostic — the
      // dot product below is normalized). Fallback: after 60s without a
      // still window, take the current sample — a rough baseline beats an
      // inert detector. ponytail: boot-captured only; re-seat -> reboot
      if (!baseSet) {
        if (delta < 0.10f) {
          if (++basePolls >= 10) {
            baseX = accelX; baseY = accelY; baseZ = accelZ;
            baseSet = true;
          }
        } else {
          basePolls = 0;
        }
        if (!baseSet && millis() > 60000 && mag > 0.15f) {
          baseX = accelX; baseY = accelY; baseZ = accelZ;
          baseSet = true;
        }
        if (baseSet)
          Serial.printf("{\"imu_base\":[%.2f,%.2f,%.2f]}\n", baseX, baseY, baseZ);
      }
      // face-down = flipped ~opposite the resting attitude; upright = back
      // near it. dot in [-1,1]; trigger <-0.35 (5 polls), release >+0.2 (3).
      float dot = 1.0f;
      if (baseSet) {
        float bm = sqrtf(baseX * baseX + baseY * baseY + baseZ * baseZ);
        if (bm > 0.15f && mag > 0.15f)   // scale-agnostic; just avoid noise/0
          dot = (accelX * baseX + accelY * baseY + accelZ * baseZ) / (bm * mag);
      }
      if (dot < -0.35f) { uprightPolls = 0; if (faceDownPolls < 250) faceDownPolls++; }
      else if (dot > 0.2f) { faceDownPolls = 0; if (uprightPolls < 250) uprightPolls++; }
      if (!quietMode && faceDownPolls == 5) {
        quietMode = true;
        gfx->setBrightness(10);   // screen is against the desk anyway
        sendLine("{\"cmd\":\"gesture\",\"gesture\":\"facedown\",\"quiet\":true}");
      } else if (quietMode && uprightPolls == 3) {
        quietMode = false;
        gfx->setBrightness(backlightDim ? 30 : 185);
        sendLine("{\"cmd\":\"gesture\",\"gesture\":\"upright\",\"quiet\":false}");
        chirp("ack");   // audible confirmation that sound is back
        st.dirty = true;
      }

      // shake (unchanged thresholds)
      if (delta > 0.65f && now - lastShakeMs > 1800) {
        lastShakeMs = now;
        sendLine("{\"cmd\":\"gesture\",\"gesture\":\"shake\"}");
        triggerNamedMood("shake", "happy");
        chirp("ack");
      } else if (!quietMode && delta > 0.22f && delta <= 0.65f && now - lastShakeMs > 1800) {
        // firm desk-knock pulse at 300ms sampling. Two pulses inside
        // 150–1200ms = double-tap: ack/dismiss. // ponytail: polled detection;
        // QMI8658 hardware tap-engine + INT if this proves flaky in practice
        if (prevTapPulseMs && now - prevTapPulseMs >= 150 && now - prevTapPulseMs <= 1200) {
          prevTapPulseMs = 0;
          toastUntilMs = 0;             // dismiss active banner
          approvalNagMuted = true;      // stop the 60s approval re-chirp
          sendLine("{\"cmd\":\"gesture\",\"gesture\":\"tap2\"}");
          triggerNamedMood("tap2", "blink");
          st.dirty = true;
        } else {
          prevTapPulseMs = now;
        }
      }
      if (prevTapPulseMs && now - prevTapPulseMs > 1200) prevTapPulseMs = 0;

      // pick-up: first real motion after >2 min of stillness
      if (delta > 0.18f && !quietMode && prevMotionMs &&
          now - prevMotionMs > 120000 && now - lastShakeMs > 1800) {
        lastPickupMs = now;
        sendLine("{\"cmd\":\"gesture\",\"gesture\":\"pickup\"}");
        triggerNamedMood("pickup", "happy");
      }
      lastAccelMag = mag;
    }
  }
}

static String i2cScanReport = "{}";   // both buses: [[addr,reg0],…] — built once
static int sensSda = SENSOR_SDA, sensScl = SENSOR_SCL;   // winner of the bus hunt

static String scanBus(TwoWire &w) {
  String out = "[";
  int found = 0;
  for (uint8_t a = 0x08; a <= 0x77 && found < 8; a++) {
    w.beginTransmission(a);
    if (w.endTransmission(true) != 0) continue;
    uint8_t v = 0xFF;
    w.beginTransmission(a);
    w.write((uint8_t)0x00);
    if (w.endTransmission(true) == 0 && w.requestFrom((int)a, 1) == 1) v = w.read();
    if (found) out += ",";
    out += "[" + String(a) + "," + String(v) + "]";
    found++;
  }
  return out + "]";
}

static void buildI2cScan() {
  // Bus hunt: the sensor bus scanned EMPTY at (sda=11,scl=10) while the same
  // scanner finds the CST328 on Wire1 — so try both pin orientations, report
  // every ACKing address + its reg-0x00 byte, and bind Wire to whichever
  // orientation actually has silicon. QMI8658 identifies as reg0==5.
  const int cand[][2] = {{11, 10}, {10, 11}};
  String rep = "";
  bool found = false;
  for (int i = 0; i < 2; i++) {
    Wire.end();
    Wire.begin(cand[i][0], cand[i][1]);
    delay(10);
    String hits = scanBus(Wire);
    if (i) rep += ",";
    rep += String("{\"sda\":") + cand[i][0] + ",\"scl\":" + cand[i][1] +
           ",\"hits\":" + hits + "}";
    if (!found && hits.length() > 2) {   // "[]" is empty; anything longer has hits
      found = true;
      sensSda = cand[i][0];
      sensScl = cand[i][1];
    }
  }
  Wire.end();
  Wire.begin(sensSda, sensScl);   // bind the winner (or default if both empty)
  delay(10);
  i2cScanReport = String("{\"sens\":[") + rep + "],\"pins\":[" + sensSda + "," +
                  sensScl + "],\"tp\":" + scanBus(Wire1) + "}";
}

static String batteryLine() {
  return String("BAT:") + String(batVolts, 2) + "V";
}

static String motionLine() {
  if (!imuReady) return "IMU:off";
  return String("IMU:") + String(accelX, 1) + "," + String(accelY, 1) + "," + String(accelZ, 1);
}

static void initSDCard() {
  pinMode(SD_D3_EN, OUTPUT);
  digitalWrite(SD_D3_EN, HIGH);
  delay(10);

  if (!SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0, -1, -1, -1)) {
    sdReady = false;
    sdStatus = "SD:pins-fail";
    Serial.println("{\"sd\":\"pins-fail\"}");
    return;
  }

  // 1-bit SDMMC matches the Waveshare Arduino demo. Do not auto-format:
  // a 128GB card may ship exFAT, and formatting it from firmware would be rude.
  if (!SD_MMC.begin("/sdcard", true, false)) {
    sdReady = false;
    sdStatus = "SD:mount-fail";
    Serial.println("{\"sd\":\"mount-fail\",\"hint\":\"format FAT32 if exFAT\"}");
    return;
  }

  uint8_t cardType = SD_MMC.cardType();
  if (cardType == CARD_NONE) {
    sdReady = false;
    sdStatus = "SD:none";
    Serial.println("{\"sd\":\"none\"}");
    return;
  }

  sdReady = true;
  sdMB = (uint32_t)(SD_MMC.totalBytes() / (1024ULL * 1024ULL));
  sdStatus = String("SD:") + sdMB + "MB";
  SD_MMC.mkdir("/hermes-buddy");
  SD_MMC.mkdir("/hermes-buddy/frames");
  SD_MMC.mkdir("/hermes-buddy/sounds");
  SD_MMC.mkdir("/hermes-buddy/logs");
  if (!SD_MMC.exists("/hermes-buddy/README.txt")) {
    File f = SD_MMC.open("/hermes-buddy/README.txt", FILE_WRITE);
    if (f) {
      f.println("Hermes Buddy SD card detected.");
      f.println("Put asset packs under /hermes-buddy/frames and sounds under /hermes-buddy/sounds.");
      f.close();
    }
  }
  Serial.printf("{\"sd\":\"ok\",\"mb\":%lu}\n", (unsigned long)sdMB);
}

static String deviceStatusJson() {
  String s = "{";
  s += "\"type\":\"device\"";
  s += ",\"battery_v\":" + String(batVolts, 2);
  s += ",\"rtc\":\"" + rtcClock + "\"";
  s += ",\"imu\":" + String(imuReady ? "true" : "false");
  s += ",\"audio\":" + String(audioReady ? "true" : "false");
  s += ",\"wifi\":\"" + wifiStatus + "\"";
  s += ",\"sd\":\"" + sdStatus + "\"";
  s += ",\"touch\":\"" + touchStatus + "\"";
  s += ",\"state\":\"" + st.jobState + "\"";
  s += "}";
  return s;
}

static void setupHttpServer() {
  httpServer.on("/", []() {
    httpServer.send(200, "text/plain", "Hermes Familiar online\n/status JSON\n/action?name=start|pause|cancel\n/chirp\n");
  });
  httpServer.on("/status", []() { httpServer.send(200, "application/json", deviceStatusJson()); });
  httpServer.on("/chirp", []() { chirp("ack"); httpServer.send(200, "application/json", "{\"ok\":true}"); });
  httpServer.on("/action", []() {
    String name = httpServer.arg("name");
    if (name != "start" && name != "pause" && name != "cancel") name = "start";
    sendLine(String("{\"cmd\":\"action\",\"action\":\"") + name + "\"}");
    httpServer.send(200, "application/json", String("{\"sent\":\"") + name + "\"}");
  });
  httpServer.begin();
}

static void applyRotation(uint8_t r) {
  uiRot = r & 3;
  gfx->setRotation(uiRot);
  st.dirty = true;
}

static void loadDisplayConfigFromSD() {
  if (!sdReady || !SD_MMC.exists("/hermes-buddy/config.json")) return;
  File f = SD_MMC.open("/hermes-buddy/config.json", FILE_READ);
  if (!f) return;
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) return;
  if (!doc["display"]["rotation"].isNull()) applyRotation((uint8_t)(doc["display"]["rotation"] | 1));
  hostIp = String((const char*)(doc["host"]["ip"] | ""));
  hostPort = doc["host"]["port"] | 8767;
  hostToken = String((const char*)(doc["host"]["token"] | ""));
}

static void initWiFiFromSD() {
  if (!sdReady || !SD_MMC.exists("/hermes-buddy/config.json")) {
    wifiStatus = "WiFi:no-config";
    Serial.println("{\"wifi\":\"no-config\"}");
    return;
  }
  File f = SD_MMC.open("/hermes-buddy/config.json", FILE_READ);
  if (!f) { wifiStatus = "WiFi:config-open-fail"; return; }
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) { wifiStatus = "WiFi:config-json-fail"; Serial.println("{\"wifi\":\"config-json-fail\"}"); return; }
  const char* ssid = doc["wifi"]["ssid"] | "";
  const char* pass = doc["wifi"]["password"] | "";
  if (!ssid || !ssid[0]) { wifiStatus = "WiFi:no-ssid"; Serial.println("{\"wifi\":\"no-ssid\"}"); return; }
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid, pass);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 6000) delay(100);
  if (WiFi.status() == WL_CONNECTED) {
    wifiReady = true;
    wifiStatus = String("WiFi:") + WiFi.localIP().toString();
    setupHttpServer();
    Serial.printf("{\"wifi\":\"ok\",\"ip\":\"%s\"}\n", WiFi.localIP().toString().c_str());
  } else {
    wifiReady = false;
    wifiStatus = "WiFi:fail";
    Serial.println("{\"wifi\":\"fail\"}");
  }
}

static void drawTabBar(uint16_t mood) {
  gfx->fillRect(0, 0, LW, TAB_H, BG);
  gfx->setTextSize(1);
  const int16_t tabW = LW / PAGE_COUNT;   // 53px
  for (int i = 0; i < PAGE_COUNT; ++i) {
    bool active = (i == (int)uiPage);
    bool alert = (i == PAGE_OPS && st.waiting > 0 && ((millis() / 400) & 1));
    uint16_t c = alert ? RED : (active ? mood : DIM);
    gfx->setTextColor(c, BG);
    int16_t x = i * tabW + (tabW - (int16_t)strlen(PAGE_TABS[i]) * 6) / 2;
    gfx->setCursor(x, 5);
    gfx->print(PAGE_TABS[i]);
    if (active) gfx->drawFastHLine(i * tabW + 6, TAB_H - 2, tabW - 12, mood);
  }
  gfx->drawFastHLine(0, TAB_H - 1, LW, DIM);
}

static void drawToastStrip() {
  const int16_t y0 = LH - 50;
  gfx->fillRect(0, y0, LW, 50, BG);
  gfx->drawFastHLine(0, y0, LW, GOLD);
  gfx->setTextSize(1);
  gfx->setTextColor(GOLD, BG);
  gfx->setCursor(8, y0 + 6);
  gfx->print("> HERMES:");
  drawWrapped(toastText, 8, y0 + 20, 50, 2, INK);
}

// Floating "working" pill: shown on ANY tab while a deck job runs, so a
// 40s hermes chat doesn't look frozen. Vanishes when jobIndex clears (the
// result lands in the same state frame that sets jobIndex back to -1).
static void drawWorkingPill() {
  const char* name = (jobIndex >= 0 && jobIndex < deckCount)
                       ? deck[jobIndex].label.c_str() : "WORKING";
  uint8_t dots = (millis() / 350) % 4;        // animated 0..3 dots
  char buf[24];
  snprintf(buf, sizeof(buf), "%s%s", name,
           dots == 0 ? "" : (dots == 1 ? "." : (dots == 2 ? ".." : "...")));
  int16_t w = 22 + (int16_t)strlen(name) * 6 + 20;   // spinner + label + dots room
  int16_t x = (LW - w) / 2, y = TAB_H + 6;
  gfx->fillRoundRect(x, y, w, 20, 6, PANEL);
  gfx->drawRoundRect(x, y, w, 20, 6, GOLD);
  // tiny spinner: a rotating bar char cycle
  static const char* spin[4] = {"-", "\\", "|", "/"};
  gfx->setTextSize(1);
  gfx->setTextColor(GOLD, PANEL);
  gfx->setCursor(x + 7, y + 6);
  gfx->print(spin[(millis() / 120) % 4]);
  gfx->setTextColor(INK, PANEL);
  gfx->setCursor(x + 18, y + 6);
  gfx->print(buf);
}

static void redraw() {
  st.dirty = false;
  bool live = st.connected && (millis() - st.lastSeenMs < 30000);
  const char* group = stateGroup(live);
  // Pending approval pulses RED<->ORANGE (the 220ms tick keeps repainting).
  uint16_t mood = (!strcmp(group, "sleep")) ? DIM
                : (st.waiting > 0 ? (((millis() / 400) & 1) ? RED : ORANGE)
                : (st.running > 0 ? ORANGE : GREEN));
  const char* label = st.waiting > 0 ? "WAIT" : (st.running > 0 ? "THINK" : (!strcmp(group, "sleep") ? "SLEEP" : "AWAKE"));
  const int16_t CY = TAB_H;              // content top
  const int16_t CH = LH - TAB_H;         // content height (222)

  drawTabBar(mood);

  if (modalActive) {
    gfx->fillRect(0, CY, LW, CH, BG);
    gfx->setTextColor(INK, BG);
    gfx->setCursor(8, CY + 6);
    gfx->print("> " + modal.title);
    gfx->drawFastHLine(8, CY + 18, LW - 16, DIM);
    gfx->setTextColor(GREEN, BG);
    if (modalBody.length()) {
      drawWrapped(modalBody, 8, CY + 28, 50, 9, GREEN);
    } else {
      drawWrapped(modal.l1, 8, CY + 28, 50, 2, GREEN);
      drawWrapped(modal.l2, 8, CY + 58, 50, 2, GREEN);
      drawWrapped(modal.l3, 8, CY + 88, 50, 2, GREEN);
    }
    gfx->setTextColor(DIM, BG);
    gfx->setCursor(8, CY + 152);
    gfx->print("tap to go back");
  } else if (uiPage == PAGE_FACE) {
    // Portrait art (240 wide) fills the left; 80px Pip-Boy vitals column right.
    if (!drawSDRaw4(group, CY, FACE_ROW0, CH))
      drawGeneratedFrame(frameForState(live), CY, FACE_ROW0, CH);
    const int16_t vx = HERMES_FRAME_W + 6;
    gfx->fillRect(HERMES_FRAME_W, CY, LW - HERMES_FRAME_W, CH, BG);
    gfx->drawFastVLine(HERMES_FRAME_W + 2, CY + 4, CH - 8, DIM);
    gfx->setTextSize(1);
    gfx->setTextColor(mood, BG);
    gfx->setCursor(vx, CY + 8);   gfx->print(label);
    gfx->setTextColor(INK, BG);
    gfx->setCursor(vx, CY + 28);  gfx->printf("S %d", st.total);
    gfx->setCursor(vx, CY + 42);  gfx->printf("R %d", st.running);
    gfx->setCursor(vx, CY + 56);  gfx->printf("W %d", st.waiting);
    gfx->setTextColor(DIM, BG);
    gfx->setCursor(vx, CY + 78);  gfx->print("TOK");
    gfx->setTextColor(INK, BG);
    gfx->setCursor(vx, CY + 90);
    if (st.tokensToday >= 1000) gfx->printf("%luk", (unsigned long)(st.tokensToday / 1000));
    else gfx->printf("%lu", (unsigned long)st.tokensToday);
    gfx->setTextColor(DIM, BG);
    gfx->setCursor(vx, CY + 110); gfx->print("JOB");
    gfx->setTextColor(INK, BG);
    String js = st.jobState; if (js.length() > 12) js = js.substring(0, 12);
    gfx->setCursor(vx, CY + 122); gfx->print(js);
    gfx->setTextColor(live ? GREEN : DIM, BG);
    gfx->setCursor(vx, CY + 150); gfx->print(live ? "USB ok" : "USB --");
    gfx->setTextColor(wifiReady ? GREEN : DIM, BG);
    gfx->setCursor(vx, CY + 164); gfx->print(wifiReady ? "NET ok" : "NET --");
    gfx->setTextColor(bleConnected ? GREEN : DIM, BG);
    gfx->setCursor(vx, CY + 178); gfx->print(bleConnected ? "BLE ok" : "BLE ad");
  } else {
    gfx->fillRect(0, CY, LW, CH, BG);
    gfx->setTextSize(1);
  }

  if (uiPage == PAGE_MSGS) {
    bool scrolled = (msgsOff > 0 && msgsCount > 0);
    gfx->setTextColor(DIM, BG);
    gfx->setCursor(8, CY + 6);
    if (scrolled) {
      gfx->printf("> HISTORY [%u-%u/%u] swipe down = newer", msgsOff + 1,
                  msgsOff + msgsCount, msgsTotal);
    } else {
      gfx->print("> RECENT TRAFFIC (swipe up = history)");
    }
    int16_t y = CY + 22;
    int n = scrolled ? msgsCount : st.entryCount;
    for (int i = 0; i < n; ++i) {
      const String &line = scrolled ? msgsLines[i] : st.entries[i];
      drawWrapped(line, 8, y, 50, 2, (!scrolled && i == 0) ? INK : GREEN);
      y += (line.length() > 50 ? 26 : 14);
      gfx->drawFastHLine(8, y - 4, LW - 16, PANEL);
      if (y > LH - 24) break;
    }
    if (n == 0) {
      gfx->setTextColor(DIM, BG);
      gfx->setCursor(8, CY + 30);
      gfx->print("no messages yet");
    }
  } else if (uiPage == PAGE_OPS) {
    if (st.action.active || st.waiting > 0) {
      gfx->setTextColor(mood, BG);
      gfx->setCursor(8, CY + 6);
      gfx->print("> APPROVAL REQUIRED");
      drawWrapped(st.action.active ? st.action.text : st.msg, 8, CY + 22, 50, 2, INK);
      if (st.action.detail.length()) drawWrapped(st.action.detail, 8, CY + 50, 50, 2, DIM);
      const int16_t by = CY + 80, bh = 100;
      gfx->drawRoundRect(12, by, 140, bh, 8, GREEN);
      gfx->setTextSize(2);
      gfx->setTextColor(GREEN, BG);
      gfx->setCursor(12 + 40, by + bh / 2 - 8); gfx->print("ALLOW");
      gfx->drawRoundRect(168, by, 140, bh, 8, RED);
      gfx->setTextColor(RED, BG);
      gfx->setCursor(168 + 46, by + bh / 2 - 8); gfx->print("DENY");
      gfx->setTextSize(1);
    } else {
      gfx->setTextColor(INK, BG);
      gfx->setCursor(8, CY + 6);
      gfx->print("> THE DECK");
      gfx->setTextColor(DIM, BG);
      gfx->setCursor(8, CY + 22);
      String j = st.jobState + ": " + st.jobLabel;
      if (j.length() > 50) j = j.substring(0, 50);
      gfx->print(j);
      if (deckCount == 0) {
        gfx->setTextColor(DIM, BG);
        gfx->setCursor(8, CY + 60);
        gfx->print("no buttons from host yet");
        gfx->setCursor(8, CY + 76);
        gfx->print("configure ~/.hermes/familiar_actions.json");
      } else {
        // 3x2 grid; running button fills solid, armed button asks SURE?
        const int16_t bw = 98, bh = 70, gx = 8, gy = 8;
        static const uint16_t deckCol[4] = {GREEN, GOLD, RED, CYAN};
        for (int i = 0; i < deckCount; ++i) {
          int16_t bx = 8 + (i % 3) * (bw + gx);
          int16_t by = CY + 38 + (i / 3) * (bh + gy);
          uint16_t col = deckCol[deck[i].color & 3];
          bool running = (jobIndex == i);
          bool armed = (deckArmed == i && millis() < deckArmedUntilMs);
          if (running) {
            gfx->fillRoundRect(bx, by, bw, bh, 8, col);
            gfx->setTextColor(BG, col);
          } else {
            gfx->drawRoundRect(bx, by, bw, bh, 8, armed ? GOLD : col);
            gfx->setTextColor(armed ? GOLD : col, BG);
          }
          const char* txt = armed ? "SURE?" : deck[i].label.c_str();
          int16_t tx2 = bx + (bw - (int16_t)strlen(txt) * 6) / 2;
          gfx->setCursor(tx2 > bx ? tx2 : bx + 2, by + bh / 2 - 8);
          gfx->print(txt);
          gfx->setTextColor(running ? BG : DIM, running ? col : BG);
          gfx->setCursor(bx + (bw - 6 * 4) / 2, by + bh / 2 + 6);
          gfx->print(running ? "STOP" : (deck[i].confirm && !armed ? "2TAP" : "    "));
        }
      }
    }
  } else if (uiPage == PAGE_CRON || uiPage == PAGE_NET || uiPage == PAGE_FLEET) {
    int slot = (uiPage == PAGE_CRON) ? 0 : (uiPage == PAGE_NET ? 1 : 2);
    HostPage &hp = hostPages[slot];
    static const char* defTitle[3] = {"CRON JOBS", "GATEWAY", "FLEET"};
    gfx->setTextColor(INK, BG);
    gfx->setCursor(8, CY + 6);
    gfx->print("> ");
    gfx->print(hp.set ? hp.title : String(defTitle[slot]));
    gfx->drawFastHLine(8, CY + 18, LW - 16, DIM);
    gfx->setTextColor(GREEN, BG);
    drawWrapped(hp.set ? hp.l1 : String("no data from host yet"), 8, CY + 28, 50, 2, GREEN);
    drawWrapped(hp.l2, 8, CY + 58, 50, 2, GREEN);
    drawWrapped(hp.l3, 8, CY + 88, 50, 2, GREEN);
  } else if (uiPage == PAGE_DEV) {
    gfx->setTextColor(INK, BG);
    gfx->setCursor(8, CY + 6);   gfx->print("> DEVICE");
    gfx->drawFastHLine(8, CY + 18, LW - 16, DIM);
    gfx->setTextColor(GREEN, BG);
    gfx->setCursor(8, CY + 28);  gfx->print(sdStatus + "  " + touchStatus);
    gfx->setCursor(8, CY + 44);  gfx->print(batteryLine() + "  " + rtcClock);
    gfx->setCursor(8, CY + 60);  gfx->print(wifiStatus);
    gfx->setCursor(8, CY + 76);  gfx->print(motionLine());
    gfx->setCursor(8, CY + 92);  gfx->printf("rot:%u  heap:%u", uiRot, (unsigned)ESP.getFreeHeap());
    gfx->setTextColor(DIM, BG);
    gfx->setCursor(8, CY + 116); gfx->print("swipe L/R or tap tabs to navigate");
  }

  // Toast strip on any page (never over a pending approval).
  if (toastUntilMs && millis() < toastUntilMs && !(st.action.active || st.waiting > 0)) {
    drawToastStrip();
  }
  // Floating "working" pill while a deck job runs (unless the OPS grid already
  // shows it as a solid STOP button, or an approval is up).
  if (jobIndex >= 0 && uiPage != PAGE_OPS && st.waiting == 0) {
    drawWorkingPill();
  }
}

static void sendLine(const String &line) {
  Serial.println(line);
  if (tcpLink.connected()) {
    tcpLink.print(line);
    tcpLink.print("\n");
  }
  if (bleConnected && txChr) {
    txChr->setValue((uint8_t*)line.c_str(), line.length());
    txChr->notify();
    txChr->setValue((uint8_t*)"\n", 1);
    txChr->notify();
  }
}

static void resetBuddyState() {
  st = BuddyState();
  uiPage = PAGE_FACE;
  forcedGroup[0] = 0;
  forcedStartMs = 0;
  forcedUntilMs = 0;
  serialLine = "";
  bleLine = "";
  st.dirty = true;
}

static void sendPermissionDecision(const char* decision) {
  String line = "{\"cmd\":\"permission\",\"decision\":\"";
  line += decision;
  line += "\"";
  if (st.action.active && st.action.id.length()) {
    line += ",\"id\":\"";
    line += st.action.id;
    line += "\"";
  }
  line += "}";
  sendLine(line);
  triggerNamedMood("action", strcmp(decision, "deny") == 0 ? "blink" : "happy");
}

static void goPage(int delta) {
  int next = ((int)uiPage + delta) % (int)PAGE_COUNT;
  if (next < 0) next += PAGE_COUNT;
  uiPage = (UiPage)next;
  toastUntilMs = 0;   // navigating dismisses a banner
  msgsOff = 0;        // and resets message history to the live tail
  st.msg = String("page ") + next;
  st.dirty = true;
}

static void handleTap(uint16_t tx, uint16_t ty) {
  sendLine(String("{\"cmd\":\"touch\",\"x\":") + tx + ",\"y\":" + ty + "}");

  if (toastUntilMs) { toastUntilMs = 0; st.dirty = true; }  // any tap dismisses a banner
  // (modal close happens in the touch-release handler — swipes included)

  // Tab bar: direct page select.
  if (ty < TAB_H) {
    int tab = tx / (LW / PAGE_COUNT);
    if (tab >= 0 && tab < PAGE_COUNT) {
      uiPage = (UiPage)tab;
      st.msg = String("tab: ") + PAGE_TABS[tab];
    }
    st.dirty = true;
    return;
  }

  if (uiPage == PAGE_OPS && ty > TAB_H + 30) {
    if (st.action.active || st.waiting > 0) {
      if (ty < TAB_H + 80) {
        // tap on the approval TEXT: show the full command, don't resolve.
        // (This also fixes taps on the text accidentally counting as
        // ALLOW/DENY, which is what the x<160 split used to do here.)
        modal.title = "APPROVAL DETAIL";
        modalBody = st.action.detail.length() ? st.action.detail : st.action.text;
        modalReturn = PAGE_OPS;
        modalActive = true;
        st.dirty = true;
        return;
      }
      const char* decision = tx < 160 ? "once" : "deny";
      sendPermissionDecision(decision);
      st.msg = String("decision: ") + decision;
      st.dirty = true;
      return;
    }
    // deck grid hit-test (3x2, matches redraw geometry)
    const int16_t bw = 98, bh = 70, gx = 8, gy = 8, y0 = TAB_H + 38;
    int col = (tx - 8) / (bw + gx);
    int row = (ty - y0) / (bh + gy);
    if (col >= 0 && col < 3 && row >= 0 && row < 2 &&
        (tx - 8) % (bw + gx) < bw && ty >= y0 && (ty - y0) % (bh + gy) < bh) {
      int i = row * 3 + col;
      if (i < deckCount) {
        if (deck[i].confirm && jobIndex != i && deckArmed != i) {
          deckArmed = i;                        // first tap arms
          deckArmedUntilMs = millis() + 2500;
          st.msg = deck[i].label + ": tap again";
          chirp("tap");
        } else {
          deckArmed = -1;                       // fire (or stop the running one)
          sendLine(String("{\"cmd\":\"deck\",\"i\":") + i + "}");
          st.msg = String("deck: ") + deck[i].label;
        }
      }
    }
    st.dirty = true;
    return;
  }

  if (st.waiting > 0) {
    uiPage = PAGE_OPS;
    st.msg = "tap allow or deny";
    triggerNamedMood("touch", "blink");
  } else if (uiPage == PAGE_FACE && tx < HERMES_FRAME_W) {
    triggerLocalMood("touch");
  } else if (uiPage == PAGE_FACE) {
    // vitals column: ask the host for the full stats picture
    sendLine("{\"cmd\":\"stats\"}");
    st.msg = "stats…";
  } else if (uiPage == PAGE_NET) {
    // link line: who's connected where, and where sound routes
    sendLine("{\"cmd\":\"net\"}");
    st.msg = "surfaces…";
  }
  st.dirty = true;
}

static void appendLineChar(String& buf, char c) {
  if (c == '\n') {
    applyJsonLine(buf);
    buf = "";
  } else if (c != '\r') {
    if (buf.length() < MAX_LINE) {
      buf += c;
    } else {
      buf = "";
      st.msg = "input line too long";
      st.dirty = true;
    }
  }
}

static void applyJsonLine(const String &line) {
  StaticJsonDocument<2048> doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    st.msg = "JSON parse error";
    st.dirty = true;
    return;
  }

  if (doc["cmd"] == "ping") {
    sendLine("{\"ack\":\"ping\",\"ok\":true}");
    // A ping means a fresh host connection (the plugin probes on connect and
    // the probe eats our boot hello). Re-announce so the host re-pushes the
    // deck + host config immediately instead of waiting for the 60s tick.
    sendLine("{\"hello\":\"hermes-buddy\",\"transport\":\"reconnect\"}");
    sendLine(String("{\"cmd\":\"diag\",\"scan\":") + i2cScanReport + "}");
    return;
  }
  if (doc["cmd"] == "clear") {
    resetBuddyState();
    return;
  }

  if (doc["type"] == "ack") {
    if (doc["msg"].is<const char*>()) st.msg = doc["msg"].as<const char*>();
    st.connected = true;
    st.lastSeenMs = millis();
    st.dirty = true;
    return;
  }
  if (doc["type"] == "event") {
    st.connected = true;
    st.lastSeenMs = millis();
    if (doc["msg"].is<const char*>()) st.msg = doc["msg"].as<const char*>();
    if (doc["event"] == "message") {
      triggerNamedMood("host", "blink", false, false);
      if (doc["msg"].is<const char*>()) {
        toastText = doc["msg"].as<const char*>();
        toastUntilMs = millis() + 4000;
      }
    }
    st.dirty = true;
    return;
  }
  if (doc["type"] == "notify") {
    // Deliberate ping from the agent (familiar_notify tool): banner + chirp.
    st.connected = true;
    st.lastSeenMs = millis();
    const char* m = doc["msg"] | "";
    st.msg = m;
    toastText = m;
    toastUntilMs = millis() + 1000UL * (uint32_t)(doc["secs"] | 8);
    const char* snd = doc["sound"] | "alert";
    if (strcmp(snd, "none") != 0) chirp(snd);
    const char* say = doc["say"] | "";
    if (say[0]) startSay(say);
    triggerNamedMood("host", "happy", false, false);
    st.dirty = true;
    return;
  }
  if (doc["type"] == "say") {
    st.connected = true;
    st.lastSeenMs = millis();
    const char* say = doc["url"] | "";
    if (say[0]) startSay(say);
    return;
  }
  if (doc["type"] == "deck") {
    deckCount = 0;
    if (doc["buttons"].is<JsonArray>()) {
      for (JsonVariant b : doc["buttons"].as<JsonArray>()) {
        if (deckCount >= 6) break;
        DeckButton &d = deck[deckCount];
        d.label = String((const char*)(b["label"] | "BTN"));
        const char* c = b["color"] | "green";
        d.color = !strcmp(c, "amber") ? 1 : (!strcmp(c, "red") ? 2 : (!strcmp(c, "cyan") ? 3 : 0));
        d.confirm = b["confirm"] | false;
        deckCount++;
      }
    }
    deckArmed = -1;
    st.connected = true;
    st.lastSeenMs = millis();
    st.dirty = true;
    return;
  }
  if (doc["type"] == "msgs") {
    msgsCount = 0;
    msgsOff = doc["off"] | 0;
    msgsTotal = doc["total"] | 0;
    if (doc["lines"].is<JsonArray>()) {
      for (JsonVariant v : doc["lines"].as<JsonArray>()) {
        if (msgsCount >= 5) break;
        msgsLines[msgsCount++] = v.as<String>();
      }
    }
    st.connected = true;
    st.lastSeenMs = millis();
    st.dirty = true;
    return;
  }
  if (doc["type"] == "page") {
    int slot = doc["slot"] | 0;
    if (slot == 9) {   // transient page: show now, tap returns
      modal.title = String((const char*)(doc["title"] | "HOST"));
      modal.l1 = String((const char*)(doc["lines"][0] | ""));
      modal.l2 = String((const char*)(doc["lines"][1] | ""));
      modal.l3 = String((const char*)(doc["lines"][2] | ""));
      modalBody = "";
      if (!modalActive) modalReturn = uiPage;
      modalActive = true;
      st.connected = true;
      st.lastSeenMs = millis();
      st.dirty = true;
      return;
    }
    if (slot < 0 || slot > 2) slot = 0;
    hostPages[slot].title = String((const char*)(doc["title"] | "HOST"));
    hostPages[slot].l1 = String((const char*)(doc["lines"][0] | ""));
    hostPages[slot].l2 = String((const char*)(doc["lines"][1] | ""));
    hostPages[slot].l3 = String((const char*)(doc["lines"][2] | ""));
    hostPages[slot].set = true;
    st.connected = true;
    st.lastSeenMs = millis();
    st.dirty = true;
    return;
  }
  if (doc["type"] == "config") {
    // Serial provisioning: merge received sections into /hermes-buddy/config.json
    // (today: {"wifi":{"ssid","password"}}), then (re)connect Wi-Fi live.
    bool ok = false;
    String why = "no-sd";
    if (sdReady) {
      StaticJsonDocument<512> cfg;
      if (SD_MMC.exists("/hermes-buddy/config.json")) {
        File rf = SD_MMC.open("/hermes-buddy/config.json", FILE_READ);
        if (rf) { deserializeJson(cfg, rf); rf.close(); }
      }
      if (!doc["wifi"].isNull()) cfg["wifi"] = doc["wifi"];
      if (!doc["display"].isNull()) cfg["display"] = doc["display"];
      if (!doc["host"].isNull()) cfg["host"] = doc["host"];
      File wf = SD_MMC.open("/hermes-buddy/config.json", FILE_WRITE);
      if (wf) {
        serializeJson(cfg, wf);
        wf.close();
        ok = true;
        why = "saved";
      } else {
        why = "write-fail";
      }
    }
    sendLine(String("{\"ack\":\"config\",\"ok\":") + (ok ? "true" : "false") + ",\"detail\":\"" + why + "\"}");
    if (!doc["display"]["rotation"].isNull()) applyRotation((uint8_t)(doc["display"]["rotation"] | 1));
    if (!doc["host"]["ip"].isNull()) {
      hostIp = String((const char*)doc["host"]["ip"]);
      hostPort = doc["host"]["port"] | 8767;
      if (!doc["host"]["token"].isNull())
        hostToken = String((const char*)doc["host"]["token"]);
    }
    if (ok && !doc["wifi"].isNull()) initWiFiFromSD();
    return;
  }
  if (doc["type"] == "permission") {
    st.connected = true;
    st.lastSeenMs = millis();
    st.waiting = 1;
    // host says Jason is on another surface — show it, but don't nag aloud
    approvalNagMuted = doc["quiet"] | false;
    st.action.active = true;
    st.action.id = doc["id"] | "";
    st.action.text = doc["text"] | "Hermes needs approval";
    st.action.detail = String((const char*)(doc["detail"] | ""));
    st.action.choiceCount = 0;
    if (doc["choices"].is<JsonArray>()) {
      for (JsonVariant v : doc["choices"].as<JsonArray>()) {
        if (st.action.choiceCount >= 3) break;
        st.action.choices[st.action.choiceCount++] = v.as<String>();
      }
    }
    if (st.action.choiceCount == 0) {
      st.action.choices[st.action.choiceCount++] = "once";
      st.action.choices[st.action.choiceCount++] = "deny";
    }
    uiPage = PAGE_OPS;
    st.msg = st.action.text;
    triggerNamedMood("host", "blink", false, false);
    st.dirty = true;
    return;
  }

  st.connected = true;
  st.lastSeenMs = millis();
  st.total = doc["total"] | st.total;
  st.running = doc["running"] | st.running;
  st.waiting = doc["waiting"] | st.waiting;
  if (st.waiting == 0) st.action.active = false;
  st.tokensToday = doc["tokens_today"] | st.tokensToday;
  st.toolsToday = doc["tools_today"] | st.toolsToday;
  if (doc["job_state"].is<const char*>()) st.jobState = doc["job_state"].as<const char*>();
  if (doc["job_label"].is<const char*>()) st.jobLabel = doc["job_label"].as<const char*>();
  jobIndex = (int8_t)(doc["job_index"] | (int)jobIndex);
  if (doc["msg"].is<const char*>()) st.msg = doc["msg"].as<const char*>();
  st.entryCount = 0;
  if (doc["entries"].is<JsonArray>()) {
    for (JsonVariant v : doc["entries"].as<JsonArray>()) {
      if (st.entryCount >= 5) break;
      st.entries[st.entryCount++] = v.as<String>();
    }
  }
  st.dirty = true;
}

class ServerCallbacks final : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo&) override { bleConnected = true; st.dirty = true; }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    bleConnected = false;
    st.dirty = true;
    NimBLEDevice::startAdvertising();
  }
};

class RxCallbacks final : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *chr, NimBLEConnInfo&) override {
    std::string v = chr->getValue();
    for (char c : v) appendLineChar(bleLine, c);
  }
};

static void setupBle() {
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_BT);
  char name[24];
  snprintf(name, sizeof(name), "Hermes-%02X%02X", mac[4], mac[5]);
  NimBLEDevice::init(name);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  NimBLEService *svc = server->createService(NUS_SERVICE_UUID);
  txChr = svc->createCharacteristic(NUS_TX_UUID, NIMBLE_PROPERTY::NOTIFY);
  NimBLECharacteristic *rxChr = svc->createCharacteristic(NUS_RX_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  rxChr->setCallbacks(new RxCallbacks());
  svc->start();
  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(NUS_SERVICE_UUID);
  // A 128-bit service UUID plus the local name can exceed the 31-byte legacy
  // advertising payload. Keep the name in the GAP device name from init() and
  // advertise only the NUS UUID to avoid NimBLEAdvertisementData overflow.
  adv->start();
}

void setup() {
  // Battery mode needs the firmware to latch the board's power-hold circuit.
  // Do this before Serial delay/display init or the board turns itself off as
  // soon as the physical power button is released.
  pinMode(PWR_KEY_INPUT, INPUT);
  pinMode(PWR_HOLD, OUTPUT);
  digitalWrite(PWR_HOLD, HIGH);

  // Default USB-CDC RX buffer is 256 bytes — too small for a burst of page
  // frames (the 360-byte deck frame overflows it while the screen repaints,
  // truncating the frame into a JSON parse error). Bigger buffer = whole
  // frames survive even during a slow redraw.
  Serial.setRxBufferSize(2048);
  Serial.begin(115200);
  delay(100);
  pinMode(BOOT_BTN, INPUT_PULLUP);
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, HIGH);
  if (!gfx->begin()) {
    Serial.println("display init failed");
  }
  gfx->setRotation(uiRot);   // landscape default; SD config may override below
  // Calibration photo PAGE 5/8: LCD inversion ON + normal RGB565 gives
  // black background and green terminal bars on this ST7789 panel.
  gfx->invertDisplay(true);
  // Slightly dim the IPS backlight so blacks stop glowing gray while the
  // phosphor palette stays saturated. Range is 0..255.
  gfx->setBrightness(185);
  initManualTouch();
  initPowerDiagnostics();
  buildI2cScan();   // bus hunt binds Wire to the pins that actually have silicon
  initMotionAndRtc();
  initAudio();
  initSDCard();
  loadDisplayConfigFromSD();
  initWiFiFromSD();
  setupBle();
  randomSeed((uint32_t)esp_random());
  nextBlinkMs = millis() + random(3500, 9000);
  st.dirty = true;
  sendLine("{\"hello\":\"hermes-buddy\",\"transport\":\"serial+ble-nus\"}");
  chirp("boot");
}

void loop() {
  if (COLOR_CALIBRATION_MODE) {
    drawCalibrationPage();
    delay(20);
    return;
  }

  while (Serial.available()) {
    lastSerialRxMs = millis();
    appendLineChar(serialLine, (char)Serial.read());
  }

  static bool lastBtn = true;
  bool btn = digitalRead(BOOT_BTN);
  if (lastBtn && !btn) {
    if (uiPage == PAGE_OPS) sendLine("{\"cmd\":\"action\",\"action\":\"start\"}");
    else if (st.waiting > 0) sendPermissionDecision("once");
    triggerLocalMood("button");
    chirp("tap");
  }
  lastBtn = btn;

  static bool wasTouched = false;
  static uint16_t touchStartX = 0, touchStartY = 0;
  static uint16_t touchLastX = 0, touchLastY = 0;
  static uint32_t touchStartMs = 0;
  uint16_t tx = 0, ty = 0;
  bool touched = readManualTouch(&tx, &ty);
  if (!touched) touched = gfx->getTouch(&tx, &ty);
  if (touched) {
    st.touchX = tx;
    st.touchY = ty;
    st.lastTouchMs = millis();
    st.dirty = true;
    touchLastX = tx;
    touchLastY = ty;
  }
  if (touched && !wasTouched) {
    touchStartX = tx;
    touchStartY = ty;
    touchLastX = tx;
    touchLastY = ty;
    touchStartMs = millis();
  }
  if (!touched && wasTouched) {
    chirp("tap");
    int dx = (int)touchLastX - (int)touchStartX;
    int dy = (int)touchLastY - (int)touchStartY;
    bool swipe = abs(dx) >= SWIPE_MIN_DX && abs(dy) <= SWIPE_MAX_DY;
    bool vswipe = abs(dy) >= SWIPE_MIN_DX && abs(dx) <= SWIPE_MAX_DY;
    if (modalActive) {
      // any gesture closes a transient page (swipes must not flip the
      // hidden page underneath it)
      modalActive = false;
      modalBody = "";
      uiPage = modalReturn;
    } else if (swipe) {
      // Finger moving left (negative dx) reveals the next page, like a phone carousel.
      goPage(dx < 0 ? 1 : -1);
      st.msg = dx < 0 ? "swipe: next" : "swipe: prev";
      sendLine(String("{\"cmd\":\"swipe\",\"dx\":") + dx + ",\"dy\":" + dy + "}");
    } else if (vswipe && uiPage == PAGE_MSGS) {
      // finger up (dy<0) digs into history; down returns toward live
      int next = (dy < 0) ? (int)msgsOff + 5 : (int)msgsOff - 5;
      if (next <= 0) {
        msgsOff = 0;   // back to the live tail, no host round-trip
        st.dirty = true;
      } else {
        sendLine(String("{\"cmd\":\"msgs\",\"off\":") + next + "}");
      }
    } else if (millis() - touchStartMs < 1200) {
      handleTap(touchStartX, touchStartY);
    }
    st.dirty = true;
  }
  wasTouched = touched;

  pollPeripheralSensors();
  if (wifiReady) httpServer.handleClient();

  // Untethered leg: USB host silent + Wi-Fi up + home known -> dial home.
  // Liveness keys off SERIAL bytes only — frames arriving over TCP must not
  // convince us USB is back.
  bool usbAlive = lastSerialRxMs != 0 && (millis() - lastSerialRxMs < 30000);
  if (wifiReady && hostIp.length()) {
    if (!tcpLink.connected() && !usbAlive && millis() - lastTcpTryMs > 10000) {
      lastTcpTryMs = millis();
      if (tcpLink.connect(hostIp.c_str(), hostPort)) {
        tcpLine = "";
        // authed transport: token must be the first line on the socket
        if (hostToken.length())
          sendLine(String("{\"type\":\"auth\",\"token\":\"") + hostToken + "\"}");
        sendLine("{\"hello\":\"hermes-buddy\",\"transport\":\"tcp\"}");
      }
    }
    while (tcpLink.connected() && tcpLink.available()) {
      appendLineChar(tcpLine, (char)tcpLink.read());
    }
  }
  if (tcpLink.connected() && usbAlive) tcpLink.stop();  // USB is back — one voice

  // Battery discipline: dim after 10 min without touch or host activity.
  bool idleDark = (millis() - st.lastTouchMs > 600000) &&
                  (millis() - st.lastSeenMs > 600000 || !st.connected) &&
                  st.running == 0 && st.waiting == 0;
  if (idleDark != backlightDim) {
    backlightDim = idleDark;
    gfx->setBrightness(idleDark ? 30 : 185);
  }

  if (st.connected && millis() - st.lastSeenMs > 30000) {
    st.connected = false;
    st.msg = "Bridge silent - waiting";
    st.dirty = true;
  }

  bool liveNow = st.connected && (millis() - st.lastSeenMs < 30000);
  const char* currentGroup = stateGroup(liveNow);
  if (!forcedUntilMs && !strcmp(currentGroup, "idle") && millis() >= nextBlinkMs) {
    triggerNamedMood("auto", "blink");
    nextBlinkMs = millis() + random(3500, 10000);
  }

  static uint8_t lastWaiting = 0;
  if (st.waiting > 0 && lastWaiting == 0 && !approvalNagMuted) chirp("alert");
  lastWaiting = st.waiting > 0 ? 1 : 0;

  // Gentle re-chirp every 60s while an approval sits unanswered —
  // unless tap2 acked it or the host routed it quiet.
  static uint32_t lastWaitChirp = 0;
  if (st.waiting > 0) {
    if (millis() - lastWaitChirp > 60000 && !approvalNagMuted) {
      lastWaitChirp = millis();
      chirp("alert");
    }
  } else {
    lastWaitChirp = millis();
    approvalNagMuted = false;   // next approval starts loud by default
  }

  // Telemetry heartbeat: battery, quiet, usb-alive, accel snapshot — host
  // routes/warns on it, and acc gives remote eyes for gesture calibration.
  if (millis() - lastTelemetryMs > 60000) {
    lastTelemetryMs = millis();
    sendLine(String("{\"cmd\":\"telemetry\",\"bat\":") + String(batVolts, 2) +
             ",\"quiet\":" + (quietMode ? "true" : "false") +
             ",\"usb\":" + (usbAlive ? "true" : "false") +
             ",\"acc\":[" + String(accelX, 2) + "," + String(accelY, 2) + "," +
             String(accelZ, 2) + "]" +
             ",\"base\":" + (baseSet ? "true" : "false") +
             ",\"imu_cfg\":" + (imuCfgOk ? "true" : "false") +
             ",\"imu\":" + (imuReady ? "true" : "false") +
             ",\"st0\":" + String(imuStatus0) + ",\"rev\":" + String(imuRev) +
             ",\"scan\":" + i2cScanReport + "}");
  }

  if (toastUntilMs && millis() >= toastUntilMs) {
    toastUntilMs = 0;
    st.dirty = true;
  }
  if (deckArmed >= 0 && millis() >= deckArmedUntilMs) {
    deckArmed = -1;   // arm window expired unanswered
    st.dirty = true;
  }

  static uint32_t lastPulse = 0;
  if (millis() - lastPulse > 220) {
    lastPulse = millis();
    // Keep SD-card state animations moving. Without SD assets, this simply
    // refreshes the sleeping/offline frame once in a while as before.
    if (sdReady || !st.connected || st.running > 0 || st.waiting > 0) st.dirty = true;
  }
  if (st.dirty) redraw();
  delay(10);
}
