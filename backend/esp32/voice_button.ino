/**
 * ARIA Voice Button — ESP32 WebSocket client
 *
 * Hardware:
 *   - INMP441 I2S microphone  (mic input,  16 kHz)
 *   - MAX98357A I2S amplifier (audio out,  24 kHz)
 *   - Momentary push button   (GPIO 15, active LOW)
 *
 * Wiring:
 *   Button       → GPIO 15 + 10kΩ pullup to 3.3V
 *   INMP441 WS   → GPIO 25
 *   INMP441 SCK  → GPIO 26
 *   INMP441 SD   → GPIO 27
 *   MAX98357 WS  → GPIO 32
 *   MAX98357 SCK → GPIO 33
 *   MAX98357 SD  → GPIO 34
 *
 * Libraries (install via Arduino Library Manager):
 *   - arduinoWebSockets by Links2004   (WebSockets)
 *   - ArduinoJson                      (JSON parsing)
 *
 * Protocol:
 *   Button held  → stream raw PCM 16-bit 16 kHz mono to server
 *   Button up    → send {"type":"end"}, wait for TTS PCM response at 24 kHz
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>

// ─── Configuration ────────────────────────────────────────────────────────────

const char* SERVER_HOST   = "192.168.1.100";  // IP of the machine running uvicorn
const uint16_t SERVER_PORT = 8000;
const char* WS_PATH       = "/api/ws/voice";
const char* TTS_VOICE     = "en-IN-Neural2-B";

// ─── Pin definitions ──────────────────────────────────────────────────────────

#define BTN_PIN      15

// INMP441 mic (I2S port 0)
#define MIC_I2S_NUM  I2S_NUM_0
#define MIC_WS       25
#define MIC_SCK      26
#define MIC_SD       27

// MAX98357A speaker (I2S port 1)
#define SPK_I2S_NUM  I2S_NUM_1
#define SPK_WS       32
#define SPK_SCK      33
#define SPK_SD       34

// ─── Audio constants ──────────────────────────────────────────────────────────

#define MIC_SAMPLE_RATE   16000   // Whisper expects 16 kHz
#define SPK_SAMPLE_RATE   24000   // Google Cloud TTS output rate
#define MIC_CHUNK_BYTES   512     // bytes per mic read (~16ms at 16kHz int16)

// ─── Globals ─────────────────────────────────────────────────────────────────

WebSocketsClient ws;
bool recording    = false;
bool playing      = false;
bool connected    = false;

// ─── I2S initialisation ───────────────────────────────────────────────────────

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

// ─── WebSocket event handler ──────────────────────────────────────────────────

void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {

    case WStype_CONNECTED:
      connected = true;
      Serial.println("[WS] Connected");
      // Send voice config on connect
      ws.sendTXT("{\"type\":\"config\",\"voice\":\"" + String(TTS_VOICE) + "\"}");
      break;

    case WStype_DISCONNECTED:
      connected = false;
      recording = false;
      playing   = false;
      Serial.println("[WS] Disconnected — reconnecting...");
      break;

    case WStype_BIN:
      // Raw PCM int16 24 kHz mono — write directly to speaker I2S
      if (length > 0) {
        if (!playing) {
          playing = true;
          Serial.println("[SPK] Playing response...");
        }
        size_t written = 0;
        i2s_write(SPK_I2S_NUM, payload, length, &written, portMAX_DELAY);
      }
      break;

    case WStype_TEXT: {
      // JSON control frames
      StaticJsonDocument<256> doc;
      DeserializationError err = deserializeJson(doc, payload, length);
      if (err) break;

      const char* msgType = doc["type"];
      if (!msgType) break;

      if (strcmp(msgType, "transcript") == 0) {
        Serial.print("[STT] ");
        Serial.println(doc["text"].as<const char*>());
      } else if (strcmp(msgType, "reply") == 0) {
        Serial.print("[LLM] ");
        Serial.println(doc["text"].as<const char*>());
      } else if (strcmp(msgType, "audio_end") == 0) {
        playing = false;
        Serial.println("[SPK] Done.");
        // Drain the I2S DMA buffer so last chunk finishes playing
        vTaskDelay(pdMS_TO_TICKS(200));
      } else if (strcmp(msgType, "error") == 0) {
        Serial.print("[ERR] ");
        Serial.println(doc["detail"].as<const char*>());
        playing = false;
      }
      break;
    }

    default:
      break;
  }
}

// ─── Setup ────────────────────────────────────────────────────────────────────

void setup() {
  Serial.begin(115200);
  pinMode(BTN_PIN, INPUT_PULLUP);

  initMic();
  initSpeaker();

  // Connect WiFi via captive-portal manager
  WiFiManager wm;
  wm.setConfigPortalTimeout(180);
  if (!wm.autoConnect("ARIA-Setup")) {
    Serial.println("[WiFi] Config portal timed out, restarting");
    ESP.restart();
  }
  Serial.print("[WiFi] Connected, IP: ");
  Serial.println(WiFi.localIP());

  // Connect WebSocket
  ws.begin(SERVER_HOST, SERVER_PORT, WS_PATH);
  ws.onEvent(onWsEvent);
  ws.setReconnectInterval(3000);

  Serial.println("[ARIA] Ready. Hold button to speak.");
}

// ─── Main loop ────────────────────────────────────────────────────────────────

void loop() {
  ws.loop();

  bool btnPressed = (digitalRead(BTN_PIN) == LOW);

  // Button just pressed — start recording
  if (btnPressed && !recording && connected && !playing) {
    recording = true;
    Serial.println("[MIC] Recording...");
    i2s_start(MIC_I2S_NUM);
  }

  // Button just released — stop recording, signal server
  if (!btnPressed && recording) {
    recording = false;
    i2s_stop(MIC_I2S_NUM);
    ws.sendTXT("{\"type\":\"end\"}");
    Serial.println("[MIC] Sent. Waiting for response...");
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
