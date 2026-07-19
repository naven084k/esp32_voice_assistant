#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <ESP_I2S.h>
#include <WebSocketsClient.h>
// ^ Library: "WebSockets" by Markus Sattler (Links2004/arduinoWebSockets) — install via
//   Arduino IDE Library Manager, search "WebSockets", then verify <WebSocketsClient.h> resolves.

// ====== EDIT THESE ======================================================
#define WIFI_SSID       "test"
#define WIFI_PASSWORD   "test12"

#define VPS_HOST        "test123"     // cloudflared hostname, e.g. "abc-def-ghi.trycloudflare.com" — no "https://" prefix, no trailing slash
#define VPS_PORT        443                        // cloudflared serves over standard HTTPS/wss port, not the backend's local --port
#define VPS_WS_PATH     "/api/ws/voice"            // wss:// via cloudflared — still no auth (see backend README)
// ========================================================================

#define I2S_MIC_BCLK    4     // SCK
#define I2S_MIC_LRCL    5     // WS
#define I2S_MIC_DOUT    6     // SD
// INMP441: VDD->3.3V, GND->GND, L/R->GND

#define TOUCH_PIN       1     // TTP223 touch module OUT pin
// TTP223: VCC->3.3V, GND->GND, OUT->GPIO1. Assumes default (momentary) mode -
// solder jumper NOT bridged - so OUT reads HIGH only while a finger is touching
// the pad and LOW once released. If your module is bridged for latching/toggle
// output instead, remove the jumper so OUT behaves momentarily.
#define TOUCH_DEBOUNCE_MS 60    // ignore further taps within this long after one is recognized
#define DOUBLE_TAP_WINDOW_MS 400   // two taps this close together (but each still past the debounce
                                   // window above) count as one double-tap gesture

#define SAMPLE_RATE_HZ   16000
#define MIC_SHIFT           13     // PROVEN loud value from your bench test
#define RECORD_SECONDS      15     // safety cap; recording normally stops early via a second tap
#define RECORD_BUFFER_BYTES (SAMPLE_RATE_HZ * 2 * RECORD_SECONDS)

#define I2S_SPK_BCLK    15    // MAX98357 BCLK
#define I2S_SPK_LRC     16    // MAX98357 LRC (WS)
#define I2S_SPK_DOUT    17    // MAX98357 DIN
// MAX98357: VIN->5V(or 3.3V), GND->GND, SD->3.3V (enables mono L+R mix output)

#define TTS_SAMPLE_RATE_HZ 24000   // must match services/tts.py SAMPLE_RATE on the backend

I2SClass I2S_mic;
I2SClass I2S_speaker;
uint8_t* recBuffer = nullptr;
size_t   recBytes  = 0;

int  g_volumePercent = 150;   // TTS playback volume: 0=mute, 100=unity gain, up to 150=boosted (may clip)
bool wsConnected     = false;
bool waitingForReply = false;

// True from the moment a barge-in abort happens until the interrupted turn's own
// "audio_end" arrives on the wire. The backend keeps streaming the old reply's
// remaining bytes for a while after we've stopped listening for them locally (see
// pushTtsBytes below) - this flag lets us silently discard exactly that leftover,
// without touching state that now belongs to the new turn we've already started.
bool ignoreIncomingAudio = false;

bool     touchLastState = false;   // last-seen digitalRead(TOUCH_PIN), for rising-edge detection
uint32_t touchLastTapMs = 0;       // millis() of the last recognized tap, for debouncing

WebSocketsClient webSocket;

// Ring buffer for incoming TTS audio. WStype_BIN just memcpy's into this (microsecond-scale)
// instead of blocking on I2S playback directly - playback is drained a small slice at a time
// from loop(), so webSocket.loop() always gets called frequently enough to keep the TLS/TCP
// connection serviced. Without this, a multi-second reply blocks webSocketEvent() for so long
// that the far end (Cloudflare edge / backend) sees an unresponsive peer and drops the connection.
#define TTS_RING_BYTES  1048576  // ~21.8s of buffered 24kHz/16-bit mono audio (PSRAM, not tight SRAM)
uint8_t* ttsRing = nullptr;
size_t   ttsHead = 0;   // next write index (producer: pushTtsBytes)
size_t   ttsTail = 0;   // next read index  (consumer: drainTtsRing)
size_t   ttsFill = 0;   // bytes currently buffered
bool     audioEndReceived = false;   // true once the "audio_end" text message has arrived

void writeWavHeader(uint8_t* h, uint32_t pcmBytes, uint32_t sr) {
  uint32_t fileSize = pcmBytes + 36;
  uint32_t byteRate = sr * 2;
  memcpy(h, "RIFF", 4);
  h[4]=fileSize; h[5]=fileSize>>8; h[6]=fileSize>>16; h[7]=fileSize>>24;
  memcpy(h+8,  "WAVE", 4);
  memcpy(h+12, "fmt ", 4);
  h[16]=16; h[17]=0; h[18]=0; h[19]=0;
  h[20]=1;  h[21]=0;
  h[22]=1;  h[23]=0;
  h[24]=sr; h[25]=sr>>8; h[26]=sr>>16; h[27]=sr>>24;
  h[28]=byteRate; h[29]=byteRate>>8; h[30]=byteRate>>16; h[31]=byteRate>>24;
  h[32]=2;  h[33]=0;
  h[34]=16; h[35]=0;
  memcpy(h+36, "data", 4);
  h[40]=pcmBytes; h[41]=pcmBytes>>8; h[42]=pcmBytes>>16; h[43]=pcmBytes>>24;
}

// Edge-triggered: returns true exactly once per physical tap (LOW->HIGH transition on
// TOUCH_PIN), gated by a debounce window so contact bounce/noise can't register as
// multiple taps. Safe to call from both loop() (to detect the start tap) and
// recordUntilStop() (to detect the stop tap) since they never run in the same instant -
// recordUntilStop() blocks loop() entirely until it returns.
bool touchTapped() {
  bool state = digitalRead(TOUCH_PIN) == HIGH;
  bool tapped = state && !touchLastState && (millis() - touchLastTapMs > TOUCH_DEBOUNCE_MS);
  touchLastState = state;
  if (tapped) touchLastTapMs = millis();
  return tapped;
}

// Call once per loop() with that iteration's touchTapped() result. Returns true only on the
// second tap of a double-tap (two taps each individually past TOUCH_DEBOUNCE_MS apart, but
// both within DOUBLE_TAP_WINDOW_MS of each other). A single tap that's never followed by a
// second one within the window is silently absorbed - callers only ever see complete pairs.
bool isDoubleTap(bool tapped) {
  static uint32_t firstTapMs = 0;
  static bool     armed = false;
  if (!tapped) return false;
  uint32_t now = millis();
  if (armed && (now - firstTapMs) <= DOUBLE_TAP_WINDOW_MS) {
    armed = false;
    return true;
  }
  armed = true;
  firstTapMs = now;
  return false;
}

void recordUntilStop() {
  recBytes = 0;
  int32_t raw[256];
  int16_t* out = (int16_t*)recBuffer;
  size_t cnt = 0;
  long long sum = 0;
  uint32_t startMs = millis();
  bool stopRequested = false;

  while (recBytes < RECORD_BUFFER_BYTES &&
         (millis() - startMs) < RECORD_SECONDS * 1000UL &&
         !stopRequested) {
    size_t bytesRead = I2S_mic.readBytes((char*)raw, sizeof(raw));
    if (bytesRead > 0) {
      size_t n = bytesRead / 4;
      for (size_t i = 0; i < n; i++) {
        if (recBytes + 2 > RECORD_BUFFER_BYTES) break;
        int32_t s = raw[i] >> MIC_SHIFT;
        if (s > 32767) s = 32767; else if (s < -32768) s = -32768;
        out[cnt++] = (int16_t)s;
        sum += s;
        recBytes += 2;
      }
    }
    if (touchTapped()) stopRequested = true;
  }
  if (cnt == 0) { Serial.println("no samples"); return; }

  int16_t dc = (int16_t)(sum / (long long)cnt);
  int16_t peak = 0; double sumSq = 0;
  for (size_t i = 0; i < cnt; i++) {
    int32_t v = out[i] - dc;
    if (v > 32767) v = 32767; else if (v < -32768) v = -32768;
    out[i] = (int16_t)v;
    int16_t a = v < 0 ? -v : v;
    if (a > peak) peak = a;
    sumSq += (double)v * v;
  }
  int rms = (int)sqrt(sumSq / cnt);
  Serial.printf("<<< REC STOP  %.2fs  bytes=%u  peak=%d  RMS=%d  DC=%d\n",
                (millis() - startMs)/1000.0f, (unsigned)recBytes, (int)peak, rms, (int)dc);
}

// Streams recBuffer to the backend over the already-open WebSocket, then sends the
// "end" message that tells the backend to run STT -> LLM -> TTS on what it received.
void sendAudioToBackend() {
  if (!wsConnected) { Serial.println("[WS] not connected to backend, can't send audio"); return; }

  Serial.printf("[WS] sending %u bytes of audio...\n", (unsigned)recBytes);
  uint32_t t0 = millis();
  const size_t CHUNK = 4096;
  for (size_t off = 0; off < recBytes; off += CHUNK) {
    size_t len = min(CHUNK, recBytes - off);
    webSocket.sendBIN(recBuffer + off, len);
    yield();  // let the WiFi/TCP stack breathe and avoid tripping the watchdog on long sends
  }
  webSocket.sendTXT("{\"type\":\"end\"}");
  waitingForReply = true;
  audioEndReceived = false;
  Serial.printf("[WS] sent (%.1fs), waiting for reply...\n", (millis() - t0) / 1000.0f);
}

// Non-blocking producer: called from webSocketEvent(WStype_BIN). Just copies bytes into the
// ring buffer - no I2S calls here, so this never stalls the WebSocket's own loop() processing.
void pushTtsBytes(const uint8_t* data, size_t len) {
  if (len > TTS_RING_BYTES) len = TTS_RING_BYTES;   // clamp; shouldn't happen given the backend's chunk size
  if (ttsFill + len > TTS_RING_BYTES) {
    // Backpressure: network delivering faster than I2S can drain. Drop rather than block -
    // blocking here would recreate the exact stall this ring buffer exists to avoid.
    Serial.printf("[TTS] ring buffer full (fill=%u) - dropping %u bytes of audio\n",
                  (unsigned)ttsFill, (unsigned)len);
    return;
  }
  size_t firstPart = TTS_RING_BYTES - ttsHead;
  if (firstPart > len) firstPart = len;
  memcpy(ttsRing + ttsHead, data, firstPart);
  if (firstPart < len) memcpy(ttsRing, data + firstPart, len - firstPart);
  ttsHead = (ttsHead + len) % TTS_RING_BYTES;
  ttsFill += len;
}

// Paced consumer: called once per loop() iteration. Always writes exactly one ~21ms slice to
// I2S_speaker - real TTS audio (16-bit, 24kHz, mono, headerless — matches services/tts.py's
// contract) if any is queued, otherwise silence to keep the amp's clock locked (see below).
// I2S_speaker.write() blocks for ~21ms per slice either way, which naturally paces this
// function's caller (loop()) at real-time without starving webSocket.loop() for long stretches.
void drainTtsRing() {
  const size_t sliceSamples = 512;
  const size_t sliceBytes   = sliceSamples * 2;
  uint8_t stereoBuf[sliceSamples * 4];  // MAX98357 needs a full L+R frame per sample

  if (ttsFill == 0) {
    if (audioEndReceived && waitingForReply) {
      waitingForReply = false;
      audioEndReceived = false;
      Serial.println("[WS] reply complete\n==================================================\n");
    }
    // Keep BCLK/WS toggling continuously even with nothing queued. I2S DAC/amp combos like
    // the MAX98357 need a steady clock to stay locked; going fully silent (no write() calls)
    // lets it drift out of lock, so the first real samples of the next reply come out
    // garbled while it re-syncs. Feeding zeros is cheap insurance against that every reply.
    memset(stereoBuf, 0, sizeof(stereoBuf));
    I2S_speaker.write(stereoBuf, sizeof(stereoBuf));
    return;
  }

  size_t n = min(sliceBytes, ttsFill) & ~size_t(1);   // keep 16-bit sample alignment
  if (n == 0) return;

  uint8_t raw[sliceBytes];
  size_t firstPart = TTS_RING_BYTES - ttsTail;
  if (firstPart > n) firstPart = n;
  memcpy(raw, ttsRing + ttsTail, firstPart);
  if (firstPart < n) memcpy(raw + firstPart, ttsRing, n - firstPart);

  size_t samples = n / 2;
  for (size_t s = 0; s < samples; s++) {
    int16_t sample = (int16_t)(raw[s * 2] | (raw[s * 2 + 1] << 8));
    int32_t scaled = ((int32_t)sample * g_volumePercent) / 100;
    if (scaled > 32767) scaled = 32767; else if (scaled < -32768) scaled = -32768;
    uint8_t b0 = (uint8_t)(scaled & 0xFF), b1 = (uint8_t)((scaled >> 8) & 0xFF);
    size_t o = s * 4;
    stereoBuf[o]     = b0;  stereoBuf[o + 1] = b1;   // left
    stereoBuf[o + 2] = b0;  stereoBuf[o + 3] = b1;   // right (duplicated)
  }
  I2S_speaker.write(stereoBuf, samples * 4);

  ttsTail = (ttsTail + n) % TTS_RING_BYTES;
  ttsFill -= n;
}

void webSocketEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      Serial.println("[WS] connected to backend");
      break;

    case WStype_DISCONNECTED:
      wsConnected = false;
      waitingForReply = false;
      audioEndReceived = false;
      ignoreIncomingAudio = false;
      ttsHead = ttsTail = ttsFill = 0;   // discard any partial audio from the interrupted turn
      Serial.println("[WS] disconnected — will auto-reconnect");
      break;

    case WStype_TEXT: {
      JsonDocument doc;
      if (deserializeJson(doc, (const char*)payload, length)) {
        Serial.println("[WS] bad JSON from server");
        break;
      }
      String t = doc["type"] | "";
      if (t == "transcript") {
        Serial.printf("YOU SAID:\n  \"%s\"\n", (const char*)(doc["text"] | ""));
      } else if (t == "reply") {
        Serial.printf("ASSISTANT:\n  \"%s\"\n", (const char*)(doc["text"] | ""));
      } else if (t == "audio_end") {
        if (ignoreIncomingAudio) {
          // This is the boundary marker for the turn we barged in on - everything
          // from here on belongs to the new turn we already started.
          ignoreIncomingAudio = false;
          Serial.println("[WS] (discarded remainder of interrupted reply)");
        } else {
          // Bytes may still be buffered/playing in the ring - drainTtsRing() clears
          // waitingForReply once playback actually finishes, not just once received.
          audioEndReceived = true;
          Serial.println("[WS] reply fully received, finishing playback...");
        }
      } else if (t == "error") {
        if (ignoreIncomingAudio) {
          ignoreIncomingAudio = false;
          Serial.printf("[WS] (interrupted turn errored after barge-in: %s)\n", (const char*)(doc["detail"] | ""));
        } else {
          waitingForReply = false;
          audioEndReceived = false;
          ttsHead = ttsTail = ttsFill = 0;   // discard any partial audio for the failed turn
          Serial.printf("[WS] error: %s\n", (const char*)(doc["detail"] | ""));
        }
      }
      break;
    }

    case WStype_BIN:
      if (!ignoreIncomingAudio) pushTtsBytes(payload, length);
      break;

    case WStype_ERROR:
      Serial.println("[WS] transport error");
      break;

    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\n=== SPEAK_ROBOT touch-triggered | MIC_SHIFT=%d ===\n", MIC_SHIFT);

  pinMode(TOUCH_PIN, INPUT);

  if (!psramFound()) Serial.println("WARNING: PSRAM not found - set Tools->PSRAM=OPI PSRAM");
  recBuffer = (uint8_t*) ps_malloc(RECORD_BUFFER_BYTES);
  if (!recBuffer) { Serial.println("FATAL: ps_malloc failed"); while (true) delay(1000); }

  ttsRing = (uint8_t*) ps_malloc(TTS_RING_BYTES);
  if (!ttsRing) { Serial.println("FATAL: ps_malloc (tts ring) failed"); while (true) delay(1000); }

  I2S_mic.setPins(I2S_MIC_BCLK, I2S_MIC_LRCL, -1, I2S_MIC_DOUT);
  if (!I2S_mic.begin(I2S_MODE_STD, SAMPLE_RATE_HZ,
                     I2S_DATA_BIT_WIDTH_32BIT,
                     I2S_SLOT_MODE_MONO,
                     I2S_STD_SLOT_LEFT)) {     // if words never come, try I2S_STD_SLOT_RIGHT
    Serial.println("FATAL: I2S mic init failed");
    while (true) delay(1000);
  }

  I2S_speaker.setPins(I2S_SPK_BCLK, I2S_SPK_LRC, I2S_SPK_DOUT, -1);
  // Full stereo frame (L+R, duplicated in software) - the MAX98357 needs standard
  // two-slot BCLK/WS timing; true single-slot "mono" mode desyncs it and sounds garbled.
  if (!I2S_speaker.begin(I2S_MODE_STD, TTS_SAMPLE_RATE_HZ,
                          I2S_DATA_BIT_WIDTH_16BIT,
                          I2S_SLOT_MODE_STEREO)) {
    Serial.println("FATAL: I2S speaker init failed");
    while (true) delay(1000);
  }

  Serial.printf("Connecting to WiFi \"%s\"", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  WiFi.setSleep(false);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) { Serial.print("."); delay(300); }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) { Serial.println("FATAL: WiFi failed"); while (true) delay(1000); }
  Serial.printf("WiFi OK  IP=%s  RSSI=%d dBm\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());

  Serial.printf("Connecting to backend wss://%s:%d%s ...\n", VPS_HOST, VPS_PORT, VPS_WS_PATH);
  // beginSSL with no fingerprint/CA arg = certificate validation is skipped (insecure TLS).
  // Fine for testing behind cloudflared; the tunnel's cert is real, we just aren't pinning it.
  webSocket.beginSSL(VPS_HOST, VPS_PORT, VPS_WS_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(5000);
}

void loop() {
  webSocket.loop();   // must run every cycle to process the socket and keep it alive
  drainTtsRing();     // play back one small (~21ms) slice of buffered TTS audio, if any is queued

  // Volume is still adjustable over serial for bench tuning - doesn't conflict with the touch trigger.
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.startsWith("v") || line.startsWith("V")) {
      int v = line.substring(1).toInt();
      if (v < 0) v = 0;
      if (v > 150) v = 150;
      g_volumePercent = v;
      Serial.printf("Volume set to %d%%\n", g_volumePercent);
    }
  }

  bool tapped = touchTapped();

  if (waitingForReply) {
    // A reply is playing/pending: only a double-tap stops it, and it stops audio only -
    // it does NOT start a new recording. That keeps a single stray tap during playback
    // from being misread as "stop and immediately start listening."
    if (isDoubleTap(tapped)) {
      Serial.println("[WS] double-tap - stopping playback");
      ttsHead = ttsTail = ttsFill = 0;
      ignoreIncomingAudio = true;   // discard the interrupted turn's remaining bytes (see flag comment above)
      waitingForReply = false;
      audioEndReceived = false;
    }
    return;   // drainTtsRing() above already paces this loop at ~21ms/iteration via its I2S write
  }

  if (!tapped) {
    return;   // drainTtsRing() above already paces this loop at ~21ms/iteration via its I2S write
  }

  if (!wsConnected) {
    Serial.println("Not connected to backend yet - try again shortly.");
    return;
  }

  Serial.println(">>> RECORDING... tap the touch pad again to stop <<<");
  recordUntilStop();
  Serial.println("==================================================");
  sendAudioToBackend();
}
