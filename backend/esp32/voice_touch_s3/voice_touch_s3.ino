/**
 * ARIA Voice Agent — ESP32-S3 (N16R8) client
 *
 * Hardware:
 *   - INMP441 I2S microphone   (mic input,  16 kHz)
 *   - MAX98357A I2S amplifier  (audio out,  24 kHz)
 *   - SSD1306 OLED, 128x64, I2C (status / transcript / reply)
 *   - Capacitive touch pad     (hold-to-talk trigger, GPIO13 / T13)
 *
 * Wiring:
 *   OLED SDA     → GPIO 8
 *   OLED SCL     → GPIO 9
 *   Touch pad    → GPIO 13 (any wire/foil pad — touch and hold to talk)
 *   INMP441 WS   → GPIO 4
 *   INMP441 SCK  → GPIO 5
 *   INMP441 SD   → GPIO 6
 *   MAX98357 WS  → GPIO 15
 *   MAX98357 SCK → GPIO 16
 *   MAX98357 SD  → GPIO 17
 *
 * Pins were chosen to avoid GPIO 26-37 (reserved for the N16R8's octal
 * flash/PSRAM), GPIO 19/20 (native USB), and the boot-strapping pins
 * (0, 3, 45, 46).
 *
 * Libraries (install via Arduino Library Manager):
 *   - arduinoWebSockets  by Links2004        (WebSockets)
 *   - ArduinoJson        by Benoit Blanchon  (JSON parsing)
 *   - WiFiManager        by tzapu            (captive-portal WiFi setup)
 *   - Adafruit SSD1306 + Adafruit GFX Library (OLED)
 *
 * Board package: esp32 by Espressif Systems — select an "ESP32S3 Dev Module"
 * board with PSRAM: OPI PSRAM enabled, Flash: 16MB.
 *
 * Protocol (matches backend/routers/voice.py WS /api/ws/voice):
 *   Touch held   → stream raw PCM 16-bit 16 kHz mono to server
 *   Touch release → send {"type":"end"}, wait for TTS PCM response at 24 kHz
 *   Server text frames {"type":"transcript"|"reply"|"audio_end"|"error"}
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ─── Configuration ─────────────────────────────────────────────────────────

const char* SERVER_HOST    = "192.168.1.100";  // IP of the machine running uvicorn
const uint16_t SERVER_PORT = 8000;
const char* WS_PATH        = "/api/ws/voice";
const char* TTS_VOICE      = "en-IN-Neural2-B";

// ─── Pin definitions ───────────────────────────────────────────────────────

#define TOUCH_PIN    13   // T13 — touch pad, hold to talk

#define OLED_SDA     8
#define OLED_SCL     9
#define OLED_WIDTH   128
#define OLED_HEIGHT  64
#define OLED_ADDR    0x3C

// INMP441 mic (I2S port 0)
#define MIC_I2S_NUM  I2S_NUM_0
#define MIC_WS       4
#define MIC_SCK      5
#define MIC_SD       6

// MAX98357A speaker (I2S port 1)
#define SPK_I2S_NUM  I2S_NUM_1
#define SPK_WS       15
#define SPK_SCK      16
#define SPK_SD       17

// ─── Audio constants ───────────────────────────────────────────────────────

#define MIC_SAMPLE_RATE   16000   // Whisper expects 16 kHz
#define SPK_SAMPLE_RATE   24000   // Google Cloud TTS output rate
#define MIC_CHUNK_BYTES   512     // bytes per mic read (~16ms at 16kHz int16)

// ─── Touch calibration ─────────────────────────────────────────────────────

uint32_t touchBaseline   = 0;
uint32_t touchThreshold  = 0;
#define TOUCH_SAMPLES        16
#define TOUCH_MARGIN_PERCENT 30   // trigger when reading rises 30% above baseline

// ─── Globals ────────────────────────────────────────────────────────────────

WebSocketsClient ws;
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);

bool recording = false;
bool playing   = false;
bool connected = false;

// ─── OLED helpers ───────────────────────────────────────────────────────────

void showLines(const String& line1, const String& line2 = "", const String& line3 = "") {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println(line1);
  if (line2.length()) display.println(line2);
  if (line3.length()) display.println(line3);
  display.display();
}

// Word-wraps text to ~21 chars/line (text size 1 on a 128px-wide OLED) and
// prints as many lines as fit on screen.
void showWrapped(const char* heading, const String& text) {
  const uint8_t charsPerLine = 21;
  const uint8_t maxLines     = 7; // heading takes the first line

  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.println(heading);

  String remaining = text;
  uint8_t linesUsed = 0;
  while (remaining.length() > 0 && linesUsed < maxLines) {
    String chunk;
    if (remaining.length() <= charsPerLine) {
      chunk = remaining;
      remaining = "";
    } else {
      int breakAt = remaining.lastIndexOf(' ', charsPerLine);
      if (breakAt <= 0) breakAt = charsPerLine;
      chunk = remaining.substring(0, breakAt);
      remaining = remaining.substring(breakAt + 1);
    }
    display.println(chunk);
    linesUsed++;
  }
  display.display();
}

// ─── I2S initialisation ─────────────────────────────────────────────────────

void initMic() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate          = MIC_SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = 64,
    .use_apll             = false,
    .tx_desc_auto_clear   = false,
    .fixed_mclk           = 0
  };
  i2s_pin_config_t pins = {
    .bck_io_num   = MIC_SCK,
    .ws_io_num    = MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num  = MIC_SD
  };
  i2s_driver_install(MIC_I2S_NUM, &cfg, 0, NULL);
  i2s_set_pin(MIC_I2S_NUM, &pins);
}

void initSpeaker() {
  i2s_config_t cfg = {
    .mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate          = SPK_SAMPLE_RATE,
    .bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count        = 8,
    .dma_buf_len          = 128,
    .use_apll             = false,
    .tx_desc_auto_clear   = true,
    .fixed_mclk           = 0
  };
  i2s_pin_config_t pins = {
    .bck_io_num   = SPK_SCK,
    .ws_io_num    = SPK_WS,
    .data_out_num = SPK_SD,
    .data_in_num  = I2S_PIN_NO_CHANGE
  };
  i2s_driver_install(SPK_I2S_NUM, &cfg, 0, NULL);
  i2s_set_pin(SPK_I2S_NUM, &pins);
}

// ─── Touch handling ─────────────────────────────────────────────────────────

void calibrateTouch() {
  uint32_t sum = 0;
  for (int i = 0; i < TOUCH_SAMPLES; i++) {
    sum += touchRead(TOUCH_PIN);
    delay(10);
  }
  touchBaseline  = sum / TOUCH_SAMPLES;
  touchThreshold = touchBaseline + (touchBaseline * TOUCH_MARGIN_PERCENT / 100);
  Serial.printf("[TOUCH] baseline=%u threshold=%u\n", touchBaseline, touchThreshold);
}

// ESP32-S3 touch readings rise (not fall, unlike classic ESP32) when touched.
bool isTouched() {
  return touchRead(TOUCH_PIN) > touchThreshold;
}

// ─── WebSocket event handler ────────────────────────────────────────────────

void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {

    case WStype_CONNECTED:
      connected = true;
      Serial.println("[WS] Connected");
      ws.sendTXT("{\"type\":\"config\",\"voice\":\"" + String(TTS_VOICE) + "\"}");
      showLines("ARIA ready", "Touch pad to talk");
      break;

    case WStype_DISCONNECTED:
      connected = false;
      recording = false;
      playing   = false;
      Serial.println("[WS] Disconnected — reconnecting...");
      showLines("Reconnecting...");
      break;

    case WStype_BIN:
      // Raw PCM int16 24 kHz mono — write directly to speaker I2S
      if (length > 0) {
        if (!playing) {
          playing = true;
          Serial.println("[SPK] Playing response...");
          showLines("Speaking...");
        }
        size_t written = 0;
        i2s_write(SPK_I2S_NUM, payload, length, &written, portMAX_DELAY);
      }
      break;

    case WStype_TEXT: {
      StaticJsonDocument<512> doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) break;

      const char* msgType = doc["type"];
      if (!msgType) break;

      if (strcmp(msgType, "transcript") == 0) {
        const char* text = doc["text"].as<const char*>();
        Serial.print("[STT] ");
        Serial.println(text);
        showWrapped("You said:", String(text));
      } else if (strcmp(msgType, "reply") == 0) {
        const char* text = doc["text"].as<const char*>();
        Serial.print("[LLM] ");
        Serial.println(text);
        showWrapped("ARIA:", String(text));
      } else if (strcmp(msgType, "audio_end") == 0) {
        playing = false;
        Serial.println("[SPK] Done.");
        vTaskDelay(pdMS_TO_TICKS(200)); // drain I2S DMA buffer
        showLines("ARIA ready", "Touch pad to talk");
      } else if (strcmp(msgType, "error") == 0) {
        const char* detail = doc["detail"].as<const char*>();
        Serial.print("[ERR] ");
        Serial.println(detail);
        playing = false;
        showWrapped("Error:", String(detail));
      }
      break;
    }

    default:
      break;
  }
}

// ─── Setup ──────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);

  Wire.begin(OLED_SDA, OLED_SCL);
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR)) {
    Serial.println("[OLED] init failed");
  }
  showLines("ARIA Voice Agent", "Starting...");

  initMic();
  initSpeaker();

  // Calibrate the touch pad baseline before anything else touches it
  showLines("Calibrating touch...", "Don't touch pad");
  calibrateTouch();

  // Connect WiFi via captive-portal manager — works with any WiFi network,
  // no hardcoded SSID/password. On first boot (or if saved creds fail) it
  // opens an "ARIA-Setup" AP; connect to it with a phone to configure WiFi.
  showLines("WiFi setup", "Connect to AP:", "ARIA-Setup");
  WiFiManager wm;
  wm.setConfigPortalTimeout(180);
  if (!wm.autoConnect("ARIA-Setup")) {
    Serial.println("[WiFi] Config portal timed out, restarting");
    showLines("WiFi setup failed", "Restarting...");
    delay(2000);
    ESP.restart();
  }
  Serial.print("[WiFi] Connected, IP: ");
  Serial.println(WiFi.localIP());
  showLines("WiFi connected", WiFi.localIP().toString());
  delay(1000);

  // Connect WebSocket to the backend
  showLines("Connecting to", String(SERVER_HOST) + ":" + String(SERVER_PORT));
  ws.begin(SERVER_HOST, SERVER_PORT, WS_PATH);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(3000);

  Serial.println("[ARIA] Ready. Touch pad to speak.");
}

// ─── Main loop ──────────────────────────────────────────────────────────────

void loop() {
  ws.loop();

  bool touched = isTouched();

  // Touch just started — begin recording
  if (touched && !recording && connected && !playing) {
    recording = true;
    Serial.println("[MIC] Recording...");
    showLines("Listening...");
    i2s_start(MIC_I2S_NUM);
  }

  // Touch released — stop recording, signal server
  if (!touched && recording) {
    recording = false;
    i2s_stop(MIC_I2S_NUM);
    ws.sendTXT("{\"type\":\"end\"}");
    Serial.println("[MIC] Sent. Waiting for response...");
    showLines("Thinking...");
  }

  // While recording: read mic chunk and stream to server
  if (recording && connected) {
    static uint8_t micBuf[MIC_CHUNK_BYTES];
    size_t bytesRead = 0;
    esp_err_t ret = i2s_read(MIC_I2S_NUM, micBuf, MIC_CHUNK_BYTES, &bytesRead, pdMS_TO_TICKS(10));
    if (ret == ESP_OK && bytesRead > 0) {
      ws.sendBIN(micBuf, bytesRead);
    }
  }
}
