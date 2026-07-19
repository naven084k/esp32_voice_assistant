#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <ESP_I2S.h>

// ====== EDIT THESE ======================================================
#define WIFI_SSID       "test"
#define WIFI_PASSWORD   "test"
#define OPENAI_API_KEY  "test"
// ========================================================================

#define I2S_MIC_BCLK    4     // SCK
#define I2S_MIC_LRCL    5     // WS
#define I2S_MIC_DOUT    6     // SD
// INMP441: VDD->3.3V, GND->GND, L/R->GND

#define SAMPLE_RATE_HZ   16000
#define MIC_SHIFT           13     // PROVEN loud value from your bench test
#define RECORD_SECONDS      15     // safety cap; recording normally stops early via "2"
#define RECORD_BUFFER_BYTES (SAMPLE_RATE_HZ * 2 * RECORD_SECONDS)

#define STT_MODEL    "whisper-1"
#define STT_LANGUAGE "en"     // test in English first; "hi" later for Hindi

#define I2S_SPK_BCLK    15    // MAX98357 BCLK
#define I2S_SPK_LRC     16    // MAX98357 LRC (WS)
#define I2S_SPK_DOUT    17    // MAX98357 DIN
// MAX98357: VIN->5V(or 3.3V), GND->GND, SD->3.3V (enables mono L+R mix output)

#define TTS_MODEL          "tts-1"
#define TTS_VOICE          "alloy"
#define TTS_SAMPLE_RATE_HZ 24000     // OpenAI response_format=pcm is fixed 24kHz/16-bit/mono

I2SClass I2S_mic;
I2SClass I2S_speaker;
uint8_t* recBuffer = nullptr;
size_t   recBytes  = 0;

int g_volumePercent = 150;   // TTS playback volume: 0=mute, 100=unity gain, up to 150=boosted (may clip)

// Growable PSRAM sink for HTTPClient::writeToStream(). Unlike reading getStreamPtr()
// directly, writeToStream() transparently de-chunks Transfer-Encoding: chunked bodies
// (which is what OpenAI's TTS endpoint sends, since it has no Content-Length) - reading
// the raw stream ourselves let chunk-size/CRLF framing bytes leak into the PCM as noise.
class PsramAudioStream : public Stream {
public:
  uint8_t* buf = nullptr;
  size_t   len = 0;
  size_t   capacity = 0;

  bool reserve(size_t initialCapacity) {
    buf = (uint8_t*)ps_malloc(initialCapacity);
    if (!buf) return false;
    capacity = initialCapacity;
    len = 0;
    return true;
  }
  size_t write(uint8_t b) override { return write(&b, 1); }
  size_t write(const uint8_t *data, size_t size) override {
    if (len + size > capacity) {
      size_t newCap = capacity ? capacity : 4096;
      while (newCap < len + size) newCap *= 2;
      uint8_t* grown = (uint8_t*)ps_realloc(buf, newCap);
      if (!grown) return 0;
      buf = grown;
      capacity = newCap;
    }
    memcpy(buf + len, data, size);
    len += size;
    return size;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
};

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

bool checkStopCommand() {
  if (!Serial.available()) return false;
  String line = Serial.readStringUntil('\n');
  line.trim();
  return line == "2";
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
    if (checkStopCommand()) stopRequested = true;
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

String transcribeAudio() {
  Serial.println("[STT] uploading to Whisper...");
  uint32_t t0 = millis();

  const String boundary = "----ESP32S3Boundary7d3a";
  String head;
  head  = "--" + boundary + "\r\n";
  head += "Content-Disposition: form-data; name=\"model\"\r\n\r\n";
  head += String(STT_MODEL) + "\r\n";
  head += "--" + boundary + "\r\n";
  head += "Content-Disposition: form-data; name=\"temperature\"\r\n\r\n0\r\n";
  if (strlen(STT_LANGUAGE) > 0) {
    head += "--" + boundary + "\r\n";
    head += "Content-Disposition: form-data; name=\"language\"\r\n\r\n";
    head += String(STT_LANGUAGE) + "\r\n";
  }
  head += "--" + boundary + "\r\n";
  head += "Content-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n";
  head += "Content-Type: audio/wav\r\n\r\n";
  String tail = "\r\n--" + boundary + "--\r\n";

  uint8_t wav[44];
  writeWavHeader(wav, recBytes, SAMPLE_RATE_HZ);

  size_t bodyLen = head.length() + 44 + recBytes + tail.length();
  uint8_t* body = (uint8_t*)ps_malloc(bodyLen);
  if (!body) { Serial.println("[STT] body alloc failed"); return ""; }
  size_t p = 0;
  memcpy(body + p, head.c_str(), head.length()); p += head.length();
  memcpy(body + p, wav, 44);                     p += 44;
  memcpy(body + p, recBuffer, recBytes);         p += recBytes;
  memcpy(body + p, tail.c_str(), tail.length()); p += tail.length();

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient https;
  https.setReuse(false);
  https.setTimeout(30000);
  if (!https.begin(client, "https://api.openai.com/v1/audio/transcriptions")) {
    Serial.println("[STT] begin failed"); free(body); return "";
  }
  https.addHeader("Authorization", String("Bearer ") + OPENAI_API_KEY);
  https.addHeader("Content-Type", "multipart/form-data; boundary=" + boundary);

  int code = https.POST(body, bodyLen);
  free(body);
  String resp = (code > 0) ? https.getString() : "";
  https.end();

  Serial.printf("[STT] HTTP %d  (%.1fs)\n", code, (millis() - t0) / 1000.0f);
  Serial.print("[STT] raw: "); Serial.println(resp);

  if (code != 200) return "";
  JsonDocument doc;
  if (deserializeJson(doc, resp)) return "";
  if (doc["error"].is<JsonObject>()) {
    Serial.print("[STT] API error: ");
    Serial.println(doc["error"]["message"].as<const char*>());
    return "";
  }
  return doc["text"] | "";
}

void speakText(const String &text) {
  Serial.println("[TTS] requesting speech...");
  uint32_t t0 = millis();

  JsonDocument reqDoc;
  reqDoc["model"] = TTS_MODEL;
  reqDoc["voice"] = TTS_VOICE;
  reqDoc["input"] = text;
  reqDoc["response_format"] = "pcm";
  String reqBody;
  serializeJson(reqDoc, reqBody);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient https;
  https.setReuse(false);
  https.setTimeout(30000);
  if (!https.begin(client, "https://api.openai.com/v1/audio/speech")) {
    Serial.println("[TTS] begin failed");
    return;
  }
  https.addHeader("Authorization", String("Bearer ") + OPENAI_API_KEY);
  https.addHeader("Content-Type", "application/json");

  int code = https.POST(reqBody);
  if (code != 200) {
    String resp = https.getString();
    Serial.printf("[TTS] HTTP %d\n", code);
    JsonDocument doc;
    if (!deserializeJson(doc, resp) && doc["error"].is<JsonObject>()) {
      Serial.print("[TTS] API error: ");
      Serial.println(doc["error"]["message"].as<const char*>());
    }
    https.end();
    return;
  }

  // TTS generation can pause for a couple seconds between chunks; the WiFiClientSecure's
  // default read timeout (~5s) is too short for that and trips writeToStream() into
  // HTTPC_ERROR_READ_TIMEOUT (-11). Stretch it out now that we're actually connected.
  https.getStreamPtr()->setTimeout(20000);

  // --- Download the whole PCM payload into PSRAM first (via writeToStream(), which
  // correctly de-chunks the response), so network jitter/TLS stalls and chunk framing
  // can never corrupt the I2S DMA feed during playback. ---
  PsramAudioStream pcmStream;
  if (!pcmStream.reserve((size_t)(TTS_SAMPLE_RATE_HZ * 2 * 10))) { // guess 10s, grows if needed
    Serial.println("[TTS] pcm alloc failed");
    https.end();
    return;
  }
  int written = https.writeToStream(&pcmStream);
  https.end();
  if (written < 0) {
    Serial.printf("[TTS] writeToStream error %d\n", written);
    free(pcmStream.buf);
    return;
  }
  uint8_t* pcm    = pcmStream.buf;
  size_t   pcmLen = pcmStream.len;
  Serial.printf("[TTS] downloaded %u bytes  (%.1fs)\n", (unsigned)pcmLen, (millis() - t0) / 1000.0f);

  // --- Play from the PSRAM buffer in one steady pass; I2S_speaker.write() paces
  // itself off the DMA, so playback is now decoupled from the network entirely. ---
  size_t evenLen = pcmLen & ~size_t(1);   // keep 16-bit PCM samples aligned
  const size_t chunkSamples = 512;
  uint8_t stereoBuf[chunkSamples * 4];    // MAX98357 needs a full L+R frame per sample
  uint32_t playT0 = millis();
  for (size_t i = 0; i < evenLen; i += chunkSamples * 2) {
    size_t bytesThisChunk = evenLen - i;
    if (bytesThisChunk > chunkSamples * 2) bytesThisChunk = chunkSamples * 2;
    size_t samples = bytesThisChunk / 2;
    for (size_t s = 0; s < samples; s++) {
      int16_t sample = (int16_t)(pcm[i + s * 2] | (pcm[i + s * 2 + 1] << 8));
      int32_t scaled = ((int32_t)sample * g_volumePercent) / 100;
      if (scaled > 32767) scaled = 32767; else if (scaled < -32768) scaled = -32768;
      uint8_t b0 = (uint8_t)(scaled & 0xFF), b1 = (uint8_t)((scaled >> 8) & 0xFF);
      size_t o = s * 4;
      stereoBuf[o]     = b0;  stereoBuf[o + 1] = b1;   // left
      stereoBuf[o + 2] = b0;  stereoBuf[o + 3] = b1;   // right (duplicated)
    }
    I2S_speaker.write(stereoBuf, samples * 4);
  }
  free(pcm);
  Serial.printf("[TTS] played %u bytes  (%.1fs)\n", (unsigned)evenLen, (millis() - playT0) / 1000.0f);
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.printf("\n=== SPEAK_ROBOT serial-triggered | MIC_SHIFT=%d ===\n", MIC_SHIFT);

  if (!psramFound()) Serial.println("WARNING: PSRAM not found - set Tools->PSRAM=OPI PSRAM");
  recBuffer = (uint8_t*) ps_malloc(RECORD_BUFFER_BYTES);
  if (!recBuffer) { Serial.println("FATAL: ps_malloc failed"); while (true) delay(1000); }

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
}

void loop() {
  Serial.printf("Type 1 + Enter to start recording (2 + Enter stops it early). "
                "Type v<0-150> + Enter to set volume (now %d%%)...\n", g_volumePercent);
  while (!Serial.available()) {
    delay(20);
  }
  String line = Serial.readStringUntil('\n');
  line.trim();

  if (line.startsWith("v") || line.startsWith("V")) {
    int v = line.substring(1).toInt();
    if (v < 0) v = 0;
    if (v > 150) v = 150;
    g_volumePercent = v;
    Serial.printf("Volume set to %d%%\n", g_volumePercent);
    return;
  }

  if (line != "1") {
    Serial.println("Ignored - type 1 to start recording, or v<0-150> to set volume.");
    return;
  }

  Serial.println(">>> RECORDING... type 2 + Enter to stop <<<");
  recordUntilStop();

  String text = transcribeAudio();
  Serial.println("==================================================");
  if (text.length()) {
    Serial.printf("YOU SAID:\n  \"%s\"\n", text.c_str());
    speakText(text);
  } else {
    Serial.println("Transcription failed (see log above).");
  }
  Serial.println("==================================================\n");
}