#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <FS.h>
#include <SD_MMC.h>
// ^ WebServer/DNSServer/Preferences/FS/SD_MMC are all bundled with the esp32 Arduino core - no
//   Library Manager install needed. WebServer/DNSServer/Preferences back the WiFi-setup captive
//   portal (see runWifiSetupPortal()); FS/SD_MMC back the SD-card file browser (see initSdFileServer()).
#include <ArduinoJson.h>
#include <ESP_I2S.h>
#include <WebSocketsClient.h>
// ^ Library: "WebSockets" by Markus Sattler (Links2004/arduinoWebSockets) — install via
//   Arduino IDE Library Manager, search "WebSockets", then verify <WebSocketsClient.h> resolves.
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
// ^ Libraries: "Adafruit SSD1306" + "Adafruit GFX Library" (Adafruit BusIO comes along
//   as a dependency) — status OLED, see the "OLED status display" section below.
#include <HTTPClient.h>   // chunked download of songs from URLs (download_song device action)
#include <time.h>   // NTP-synced clock for the idle screen's occasional time display
#include <math.h>   // sinf/lroundf for the idle face's breathing animation

#include <AudioFileSourceFS.h>
#include <AudioGeneratorMP3.h>
#include <AudioOutput.h>
// ^ Library: "ESP8266Audio" by Earle F. Philhower, III — install via Arduino IDE Library
//   Manager, search "ESP8266Audio", then verify these headers resolve. Decodes local SD-card
//   MP3s (see the "Music playback" section below) - playback is fed through our own I2S_speaker
//   (via a custom AudioOutput subclass) rather than the library's own AudioOutputI2S, so it
//   shares the exact same I2S peripheral/pins TTS already uses instead of a second driver
//   instance fighting over them.

// ====== EDIT THESE ======================================================
// WiFi credentials are no longer hardcoded here - they're entered once via a captive-portal
// setup page (see runWifiSetupPortal()) and persisted in flash (Preferences/NVS), so the same
// binary can be pointed at any network without a re-flash. See WIFI_SETUP_* below.
#define WIFI_SETUP_AP_SSID        "ESP32-Setup"   // shown during setup - join this from any phone
#define WIFI_SETUP_AP_PASSWORD    "test@123"      // default setup-hotspot password (WPA2 needs >=8 chars)
#define WIFI_RESET_HOLD_MS        5000            // hold the touch pad this long - at power-on, or anytime
                                                   // while running - to wipe saved WiFi creds and re-enter setup mode
#define DNS_PORT 53

// Fallback backend host, only used the very first time the device boots with nothing saved
// yet - cloudflared quick-tunnel hostnames rotate, so the real value normally comes from NVS
// (see loadVpsHost()) and is entered/updated through the same setup portal as WiFi, without a
// re-flash. Edit this default too if you'd like a fresh flash to start pointed somewhere sane.
#define VPS_HOST_DEFAULT "earnings-grade-heat-acknowledge.trycloudflare.com"     // cloudflared host, e.g. "abc-def-ghi.trycloudflare.com" — no "https://" prefix, no trailing slash
#define VPS_PORT        443                        // cloudflared serves over standard HTTPS/wss port, not the backend's local --port
#define VPS_WS_PATH     "/api/ws/voice"            // wss:// via cloudflared — still no auth (see backend README)

#define OLED_SDA        8     // status display (SSD1306 128x64 I2C)
#define OLED_SCL        9
#define OLED_I2C_ADDR   0x3C  // try 0x3D if display.begin() fails

// SD-card file browser (see initSdFileServer() setup + handleBrowse()/handleSdDownload()).
// ESP32-S3 has no fixed SD_MMC pin mapping - these must match your board's actual SD slot wiring.
#define SD_MMC_CLK      39
#define SD_MMC_CMD      38
#define SD_MMC_D0       40    // 1-bit mode - only D0 wired; pass false to SD_MMC.begin() for 4-bit if D1-D3 are too
#define SD_SERVER_PORT  8080  // separate port from the WiFi-setup portal's WebServer (port 80, AP-mode only)

#define NTP_SERVER      "pool.ntp.org"
#define GMT_OFFSET_SEC  19800 // IST (Hyderabad) = UTC+5:30 = 5*3600 + 30*60
#define DST_OFFSET_SEC  0     // India does not observe daylight saving
// ========================================================================

#define I2S_MIC_BCLK    4     // SCK
#define I2S_MIC_LRCL    5     // WS
#define I2S_MIC_DOUT    6     // SD
// INMP441: VDD->3.3V, GND->GND, L/R->GND

#define TOUCH_PIN       1     // TTP223 touch module OUT pin
// TTP223: VCC->3.3V, GND->GND, OUT->GPIO1. Assumes default (momentary) mode -
// solder jumper NOT bridged - so OUT reads HIGH only while a finger is touching
// the pad and LOW once released. If your module is bridged for latching/toggle
// output instead, remove the jumper so OUT behaves momentarily. Single-tap is
// the whole interaction model: tap once to start listening, tap again to stop and send.
#define TOUCH_DEBOUNCE_MS 60    // ignore state changes within this long of the last recognized one

#define SAMPLE_RATE_HZ   16000
#define MIC_SHIFT           13     // PROVEN loud value from your bench test
#define RECORD_SECONDS      15     // safety cap; recording normally stops early on the second tap
#define RECORD_BUFFER_BYTES (SAMPLE_RATE_HZ * 2 * RECORD_SECONDS)

#define I2S_SPK_BCLK    15    // MAX98357 BCLK
#define I2S_SPK_LRC     16    // MAX98357 LRC (WS)
#define I2S_SPK_DOUT    17    // MAX98357 DIN
// MAX98357: VIN->5V(or 3.3V), GND->GND, SD->3.3V (enables mono L+R mix output)

#define TTS_SAMPLE_RATE_HZ 24000   // must match services/tts.py SAMPLE_RATE on the backend

// How often to feed the speaker a silent I2S slice while recordUntilStop()/sendAudioToBackend()
// are otherwise blocking loop() (and therefore drainTtsRing()) for seconds at a time. Without
// this, the MAX98357 loses its BCLK/WS clock lock during that gap and the first ~1-2s of the
// *next* reply comes out as noise/static while it re-syncs. Tunable: shorten if noise persists,
// lengthen only if it turns out to cost recording quality (unlikely - see feedSilentSlice() call sites).
#define SPEAKER_KEEPALIVE_MS 200

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

// Music playback state (see the "Music playback" section below, and its webSocketEvent()
// "play_song"/"stop_song" handlers). A queued song is deferred rather than started the instant
// its WS message arrives, since the backend sends it right after "audio_end" - but this device's
// own TTS ring buffer may still have seconds of that turn's "Playing X now" confirmation audio
// left to play out. loop() starts g_pendingSong* once waitingForReply naturally clears.
bool     musicPlaying     = false;
// Set/cleared only by the /music/pause web control (see handleMusicPauseToggle()). While true,
// musicLoop() early-returns every iteration - no decode step, no I2S write - freezing playback
// exactly where it was; false resumes it on the very next loop() iteration. Always reset to
// false on a fresh playSongFile() so it never carries over from one track to the next.
bool     musicPaused      = false;
bool     g_songPending    = false;
String   g_pendingSongPath;
String   g_pendingSongTitle;
String   g_currentTrackName;
String   g_currentSongPath;   // set by playSongFile() - lets an interruption remember what to resume

// Set when a touch interrupts a playing song to listen (see loop()'s musicPlaying tap handler).
// Cleared the moment real speech is heard (a non-empty "transcript" WS message - see
// webSocketEvent()), so a genuine question/request is never fought with the old song resuming.
// If it's still true once the turn concludes in an "error" (STT heard nothing at all), that means
// the interruption didn't produce a question, so webSocketEvent()'s "error" handler resumes
// g_interruptedSong* instead of showing an error for what wasn't really a failure.
bool     g_songWasInterrupted = false;
String   g_interruptedSongPath;
String   g_interruptedSongTitle;

// Download-then-play state: when the backend sends a "download_song" action the ESP32 downloads
// the file from the URL to the SD card, then plays it locally. Like g_songPending, the actual
// download is deferred to loop() so it doesn't block the WS event handler mid-callback.
bool     g_downloadPending = false;
String   g_downloadUrl;
String   g_downloadPath;      // SD card path, e.g. "/Naveen/songs/<album>/slug.mp3"
String   g_downloadTitle;
String   g_downloadId;        // echoed back in download_ack so the backend knows which of its
                               // pending downloads this was - empty for a download_song action
                               // that didn't include one (sendDownloadAck() then no-ops)

// Up-next queue for playlist-style requests (backend's "play_song_queue" action, e.g. "play some
// Arijit Singh songs") - the first song is started the normal g_pendingSong* way, the rest sit
// here until musicLoop() sees the current track finish on its own and calls dequeueNextSong().
// A fresh play_song/play_song_queue/download_song request replaces whatever's still queued here
// (see their webSocketEvent() handlers); an explicit stop_song clears it too. Deliberately NOT
// cleared when a touch interrupts a song to ask a question, or when that interruption resumes the
// song - both keep the same playlist session going.
#define SONG_QUEUE_MAX 20
String   g_songQueuePath[SONG_QUEUE_MAX];
String   g_songQueueTitle[SONG_QUEUE_MAX];
int      g_songQueueCount = 0;   // number of valid entries at the front of the arrays above

// Playback history for the /music page's "Prev" control - every song advanceToNextSong() leaves
// behind (queue auto-advance or the /music/next control) gets pushed here so it can be gone back
// to. Plain append/pop-from-the-end stack (no shifting needed, unlike the queue's FIFO arrays).
// Cleared alongside the queue on a fresh play_song/play_song_queue/download_song/stop_song.
#define SONG_HISTORY_MAX 20
String   g_songHistoryPath[SONG_HISTORY_MAX];
String   g_songHistoryTitle[SONG_HISTORY_MAX];
int      g_songHistoryCount = 0;

bool     touchLastState = false;   // last-seen digitalRead(TOUCH_PIN), for rising-edge detection
uint32_t touchLastTapMs = 0;       // millis() of the last recognized tap, for debouncing

WebSocketsClient webSocket;

// WiFi/backend setup portal (see runWifiSetupPortal()). g_wifiSsid/g_wifiPass/g_vpsHost hold
// whatever config is currently in use - loaded from NVS at boot - so loop()'s runtime
// WiFi-drop retry can reuse them without re-reading flash each time.
WebServer setupServer(80);
DNSServer dnsServer;
String    g_wifiSsid, g_wifiPass;
String    g_vpsHost;

// SD-card file browser (see runSdFileServer() below) - separate WebServer instance/port from
// setupServer since this one runs during normal STA-mode operation, not just the AP-mode portal.
WebServer fileServer(SD_SERVER_PORT);
bool      sdReady = false;

// Set by handleFileUpload() at UPLOAD_FILE_START and read again at UPLOAD_FILE_WRITE/END/ABORTED -
// WebServer only ever runs one upload at a time on a given instance, so a single pair of globals
// (rather than something keyed per-request) is enough here.
File      g_uploadFile;
String    g_uploadPath;

// Ring buffer for incoming TTS audio. WStype_BIN just memcpy's into this (microsecond-scale)
// instead of blocking on I2S playback directly - playback is drained a small slice at a time
// from loop(), so webSocket.loop() always gets called frequently enough to keep the TLS/TCP
// connection serviced. Without this, a multi-second reply blocks webSocketEvent() for so long
// that the far end (Cloudflare edge / backend) sees an unresponsive peer and drops the connection.
#define TTS_RING_BYTES  1048576  // ~21.8s of buffered 24kHz/16-bit mono audio (PSRAM, not tight SRAM)
// Before playing the very first slice of a new reply, wait until this much audio is buffered.
// Absorbs bursty first-chunk delivery (TCP/TLS may fragment the backend's 8KB WS frame across
// multiple onEvent(WStype_BIN) callbacks) so drainTtsRing() doesn't underrun on slice #1 and
// leave the amp starved. ~85ms of audio at 24kHz/16-bit mono = half a backend chunk.
#define TTS_PREBUFFER_BYTES 4096
// Linear fade-in applied to the first N samples of each reply to avoid an amplitude pop from
// silence -> full-volume PCM (compounded by g_volumePercent default of 150%). ~21ms at 24kHz.
#define TTS_FADEIN_SAMPLES  512
uint8_t* ttsRing = nullptr;
size_t   ttsHead = 0;   // next write index (producer: pushTtsBytes)
size_t   ttsTail = 0;   // next read index  (consumer: drainTtsRing)
size_t   ttsFill = 0;   // bytes currently buffered
bool     audioEndReceived = false;   // true once the "audio_end" text message has arrived
bool     speakingShown    = false;   // set once STATE_SPEAKING has been shown for the current reply
// Set inside drainTtsRing() the instant real playback starts, consumed in loop() right after
// drainTtsRing() returns. Defers the ~20-25ms blocking SSD1306 I2C flush (updateDisplay ->
// drawFrame -> display.display()) off the I2S critical path - if we flushed inline before the
// first I2S write, the MAX98357 would lose BCLK/WS clock lock and glitch the first ~1-2s of
// playback (the exact failure mode feedSilentSlice()'s keep-alive design exists to prevent).
bool     pendingSpeakingDisplay = false;
bool     ttsPrebufferPrimed = false;   // false until ttsFill first crosses TTS_PREBUFFER_BYTES this reply
uint32_t fadeInSamplesRemaining = 0;   // counts down from TTS_FADEIN_SAMPLES on the first real slice

// ====================== OLED status display (SSD1306 128x64 I2C) ======================
// Icon-only state screen: blinking face rotating with a clock (idle), animated waveform
// (recording), hourglass (processing), speaker (speaking), warning triangle (error) —
// plus a top-bar WiFi icon, a boot "eyes opening" animation, and an optional one-line
// truncated subtitle (e.g. the transcript). No paragraphs of text.

Adafruit_SSD1306 display(128, 64, &Wire, -1);

enum AssistantState { STATE_IDLE, STATE_RECORDING, STATE_PROCESSING, STATE_SPEAKING, STATE_MUSIC, STATE_ERROR };
enum ErrorKind { ERR_WIFI, ERR_MIC, ERR_API, ERR_GENERIC };

AssistantState g_state         = STATE_IDLE;
ErrorKind      g_errorKind     = ERR_GENERIC;
bool           g_wifiConnected = false;
char           g_subtitle[22]  = "";
uint32_t       g_animFrame     = 0;
uint32_t       g_lastAnimMs    = 0;
#define ANIM_INTERVAL_MS 300

bool ntpSynced = false;   // set once in setup() after a successful NTP fetch; gates the idle clock screen

// ---- idle screen: rotates between the face and (if NTP is synced) a small clock ----
bool     g_showClock      = false;
uint32_t g_idleRotateAtMs = 0;
#define IDLE_FACE_MS   12000   // how long the face stays up per idle rotation
#define IDLE_CLOCK_MS  4000    // how long the clock stays up before flipping back to the face

// ---- mood: a brief blink-then-settle animation, played on top of the idle face for
// personality on a successful interaction. Purely cosmetic - tickDisplay() only redraws
// for it a few times over well under a second, so it can't add latency to the audio path.
uint32_t g_moodStartMs = 0;
uint32_t g_moodUntilMs = 0;   // nonzero while a mood animation is playing

void playMood(uint32_t durationMs) {
  g_moodStartMs = millis();
  g_moodUntilMs = g_moodStartMs + durationMs;
}

// ---- idle "breathing + curious glance": makes the resting face read as alive/waiting
// instead of a static stare. Breathing is a continuous slow size pulse; glances are a
// periodic brief pupil nudge left/right/up, both purely cosmetic (driven off millis()).
#define BREATH_PERIOD_MS   2000
#define GLANCE_DURATION_MS 600
bool     g_glancing      = false;
int8_t   g_glanceDir     = 0;    // 0=left, 1=right, 2=up
uint32_t g_glanceUntilMs = 0;
uint32_t g_nextGlanceMs  = 0;    // scheduled time of the next glance

static const int16_t ICON_CX = 64, ICON_CY = 29, LABEL_Y = 46, SUBTITLE_Y = 56;

void drawWifiIcon(int16_t x, int16_t y) {
  int16_t cx = x + 6, cy = y + 8;
  display.drawCircleHelper(cx, cy, 7, 0x3, SSD1306_WHITE);
  display.drawCircleHelper(cx, cy, 4, 0x3, SSD1306_WHITE);
  display.fillCircle(cx, cy, 1, SSD1306_WHITE);
  if (!g_wifiConnected) display.drawLine(x, y, x + 12, y + 11, SSD1306_WHITE);
}

// Two round eyes plus a permanent smile arc, centered - a proper smiley face. Eyes are
// normally open; briefly close then settle into a curved "happy" shape while a mood
// animation (see playMood) is active.
void drawFaceIcon(int16_t cx, int16_t cy) {
  int8_t eyeShape = 0;   // 0 = open, 1 = closed (blink), 2 = happy/curious (curved)
  if (g_moodUntilMs != 0) {
    uint32_t elapsed = millis() - g_moodStartMs;
    eyeShape = (elapsed < 300 && (elapsed / 100) % 2 == 0) ? 1 : 2;
  }

  // Breathing: eye/smile radius pulses +/-1px on a slow sine cycle.
  float breath = sinf((millis() % BREATH_PERIOD_MS) / (float)BREATH_PERIOD_MS * 2 * PI);
  int16_t eyeR   = 4 + (int16_t)lroundf(breath);   // 3..5
  int16_t smileR = 8 + (int16_t)lroundf(breath);   // 7..9

  // Curious glance: while eyes are open (not blinking/mood), briefly offset the pupil
  // dot from center to suggest a quick look left/right/up.
  int16_t pupilDX = 0, pupilDY = 0;
  if (eyeShape == 0 && g_glancing) {
    switch (g_glanceDir) {
      case 0: pupilDX = -2; break;   // left
      case 1: pupilDX =  2; break;   // right
      default: pupilDY = -2; break;  // up
    }
  }

  int16_t eyeDX = 9, eyeY = cy - 2;
  for (int i = -1; i <= 1; i += 2) {
    int16_t ex = cx + i * eyeDX;
    if (eyeShape == 1) {
      display.drawFastHLine(ex - 4, eyeY, 8, SSD1306_WHITE);          // closed: flat line
    } else if (eyeShape == 2) {
      display.drawCircleHelper(ex, eyeY + 4, 5, 0x3, SSD1306_WHITE);  // happy: upward curve
    } else {
      display.fillCircle(ex, eyeY, eyeR, SSD1306_WHITE);                       // open: round eye
      display.fillCircle(ex + pupilDX, eyeY + pupilDY, 1, SSD1306_BLACK);      // pupil (glance offset)
    }
  }
  display.drawCircleHelper(cx, cy + 4, smileR, 0xC, SSD1306_WHITE);   // smile: bottom arc of a circle
}

// Classic digital-clock readout (24h HH:MM in a bezeled box, colon blinking once a
// second) in place of the face, in the localtime configured in setup() via configTime()
// (IST - see GMT_OFFSET_SEC). Falls back to the face if NTP never synced (no time
// source) so the idle rotation never shows a blank/stale clock.
void drawClockIcon(int16_t cx, int16_t cy) {
  struct tm timeinfo;
  if (!ntpSynced || !getLocalTime(&timeinfo, 0)) {
    drawFaceIcon(cx, cy);
    drawLabel("TIME");
    return;
  }
  bool colonOn = (timeinfo.tm_sec % 2) == 0;
  char buf[6];
  snprintf(buf, sizeof(buf), "%02d%c%02d", timeinfo.tm_hour, colonOn ? ':' : ' ', timeinfo.tm_min);

  display.setTextSize(3);
  int16_t w = strlen(buf) * 18, h = 24;
  int16_t x = cx - w / 2, y = cy - h / 2;
  display.setCursor(x, y);
  display.print(buf);

  char dateBuf[16];
  strftime(dateBuf, sizeof(dateBuf), "%d %b %Y", &timeinfo);
  drawLabel(dateBuf);   // reuses the same centered subtitle row every other state's label uses
}

void drawWaveformIcon(int16_t cx, int16_t cy) {
  static const uint8_t heights[8][5] = {
    { 6, 10, 16, 10,  6}, {10, 16,  8, 16, 10}, {16,  8, 12,  8, 16}, { 8, 14, 18, 14,  8},
    { 6, 12, 20, 12,  6}, {12, 18, 10, 18, 12}, {18, 10, 14, 10, 18}, {10,  6, 10,  6, 10},
  };
  const uint8_t* h = heights[g_animFrame % 8];
  int16_t barW = 4, gap = 3;
  int16_t startX = cx - (5 * barW + 4 * gap) / 2;
  for (int i = 0; i < 5; i++) {
    int16_t x = startX + i * (barW + gap);
    display.fillRect(x, cy - h[i] / 2, barW, h[i], SSD1306_WHITE);
  }
}

// Music-playing icon: a 7-bar equalizer with each bar bouncing to its own out-of-phase pattern
// (unlike drawWaveformIcon's single 5-bar shape) so it reads as a livelier "now playing" meter.
// Driven by g_animFrame, same as every other state's icon animation.
void drawMusicBarsIcon(int16_t cx, int16_t cy) {
  static const uint8_t heights[8][7] = {
    { 5, 14,  8, 20, 10, 16,  6}, {10,  6, 16, 12, 20,  8, 14},
    {16, 10,  6,  8, 14, 18,  9}, { 8, 18, 12,  5, 16, 10, 20},
    { 6,  9, 20, 14,  8, 12, 16}, {14, 16,  9, 18,  6, 20,  8},
    {20,  8, 14,  6, 12,  9, 18}, { 9, 12,  8, 16, 18,  6, 10},
  };
  static const uint8_t flat[7] = {6, 6, 6, 6, 6, 6, 6};   // paused: bars hold level, don't bounce
  const uint8_t* h = musicPaused ? flat : heights[g_animFrame % 8];
  int16_t barW = 5, gap = 3, n = 7;
  int16_t startX = cx - (n * barW + (n - 1) * gap) / 2;
  for (int i = 0; i < n; i++) {
    int16_t x = startX + i * (barW + gap);
    display.fillRect(x, cy - h[i] / 2, barW, h[i], SSD1306_WHITE);
  }
}

void drawHourglassIcon(int16_t cx, int16_t cy) {
  int16_t w = 14, h = 20;
  int16_t left = cx - w / 2, right = cx + w / 2, top = cy - h / 2, bottom = cy + h / 2;
  display.drawTriangle(left, top, right, top, cx, cy, SSD1306_WHITE);
  display.drawTriangle(left, bottom, right, bottom, cx, cy, SSD1306_WHITE);
  display.drawFastHLine(left - 1, top, w + 2, SSD1306_WHITE);
  display.drawFastHLine(left - 1, bottom, w + 2, SSD1306_WHITE);
  int16_t sandY = cy + 2 + (g_animFrame % 6);
  if (sandY < bottom - 1) display.fillRect(cx - 1, sandY, 2, 2, SSD1306_WHITE);
}

void drawSpeakerIcon(int16_t cx, int16_t cy) {
  int16_t bx = cx - 12, by = cy - 5;
  display.fillRect(bx, by, 5, 10, SSD1306_WHITE);
  display.fillTriangle(bx + 5, by, bx + 5, by + 10, bx + 14, cy - 9, SSD1306_WHITE);
  display.fillTriangle(bx + 5, by + 10, bx + 14, cy - 9, bx + 14, cy + 9, SSD1306_WHITE);
  display.drawCircleHelper(bx + 14, cy, 5, 0x6, SSD1306_WHITE);
  if ((g_animFrame % 4) < 2) display.drawCircleHelper(bx + 14, cy, 9, 0x6, SSD1306_WHITE);
}

// Small standalone smiley - a top-corner accent shown only while STATE_SPEAKING, echoing
// the idle face's smile so a reply landing reads as "happy to have helped" at a glance.
void drawEmojiSmiley(int16_t cx, int16_t cy, int16_t r) {
  display.drawCircle(cx, cy, r, SSD1306_WHITE);
  display.fillCircle(cx - r / 2, cy - r / 3, 1, SSD1306_WHITE);
  display.fillCircle(cx + r / 2, cy - r / 3, 1, SSD1306_WHITE);
  display.drawCircleHelper(cx, cy, r - 3, 0xC, SSD1306_WHITE);
}

const char* errorLabel() {
  switch (g_errorKind) {
    case ERR_WIFI: return "WIFI LOST";
    case ERR_MIC:  return "MIC ERROR";
    case ERR_API:  return "API ERROR";
    default:       return "ERROR";
  }
}

void drawErrorIcon(int16_t cx, int16_t cy) {
  int16_t r = 11;
  display.drawTriangle(cx, cy - r, cx - r, cy + r - 2, cx + r, cy + r - 2, SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(cx - 1, cy - 2);
  display.print("!");
}

void drawLabel(const char* text) {
  int16_t w = strlen(text) * 6;
  display.setTextSize(1);
  display.setCursor((128 - w) / 2, LABEL_Y);
  display.print(text);
}

// Marquee-scrolls `text` across the subtitle row when it's too wide to fit; centers it and
// prints it plainly otherwise. Used for the current song title on STATE_MUSIC, which (unlike
// g_subtitle) isn't truncated to 21 chars. Scroll position is driven by g_animFrame, so it
// advances at the same cadence as that state's icon animation (see tickDisplay()).
void drawScrollingText(const String& text, int16_t y) {
  display.setTextSize(1);
  int16_t textW = text.length() * 6;
  if (textW <= 128) {
    display.setCursor((128 - textW) / 2, y);
    display.print(text);
    return;
  }
  String padded = text + "   ";   // gap before the loop repeats
  int16_t fullW = padded.length() * 6;
  int16_t offset = (g_animFrame * 4) % fullW;   // scroll speed: 4px per animation tick
  int16_t x = -offset;
  display.setCursor(x, y);
  display.print(padded);
  display.setCursor(x + fullW, y);
  display.print(padded);   // second copy so the wraparound looks continuous
}

void drawFrame() {
  display.clearDisplay();
  display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
  drawWifiIcon(128 - 14, 0);
  if (g_wifiConnected) {
    // Top-left of the top bar (~114px of clear space to the left of the WiFi icon at x=114) -
    // fits any IPv4 at textSize 1. Useful when Serial isn't attached and the user needs the
    // SD file-browser URL at http://<this>:8080/.
    display.setTextSize(1);
    display.setCursor(0, 2);
    display.print(WiFi.localIP());
  }

  switch (g_state) {
    case STATE_IDLE:
      if (g_showClock) { drawClockIcon(ICON_CX, ICON_CY); }   // draws its own date label (or "TIME" if not yet NTP-synced)
      else              { drawFaceIcon(ICON_CX, ICON_CY);  drawLabel("TOUCH TO ASK"); }
      break;
    case STATE_RECORDING:  drawWaveformIcon(ICON_CX, ICON_CY);  drawLabel("LISTENING");  break;
    case STATE_PROCESSING: drawHourglassIcon(ICON_CX, ICON_CY); drawLabel("THINKING");   break;
    case STATE_SPEAKING:
      drawSpeakerIcon(ICON_CX, ICON_CY);
      drawLabel("SPEAKING");
      drawEmojiSmiley(10, 6, 6);   // top-left accent, mirroring the WiFi icon's top-right corner
      break;
    case STATE_MUSIC: {
      drawMusicBarsIcon(ICON_CX, ICON_CY);
      char label[16];
      if (musicPaused) {
        snprintf(label, sizeof(label), "PAUSED");
      } else if (g_songQueueCount > 0) {
        snprintf(label, sizeof(label), "PLAYING +%d", g_songQueueCount);   // songs still queued up next
      } else {
        snprintf(label, sizeof(label), "PLAYING");
      }
      drawLabel(label);
      break;
    }
    case STATE_ERROR:      drawErrorIcon(ICON_CX, ICON_CY);     drawLabel(errorLabel()); break;
  }

  if (g_state == STATE_MUSIC) {
    drawScrollingText(g_currentTrackName, SUBTITLE_Y);   // full title, not the 21-char g_subtitle
  } else if (g_subtitle[0] != '\0') {
    display.setTextSize(1);
    display.setCursor(0, SUBTITLE_Y);
    display.print(g_subtitle);
  }
  display.display();
}

void displayInit() {
  Wire.begin(OLED_SDA, OLED_SCL);
  Wire.setClock(400000);   // Fast Mode - cuts each full-frame push from ~80-100ms to ~20-25ms
  if (!display.begin(SSD1306_SWITCHCAPVCC, OLED_I2C_ADDR)) {
    Serial.println("WARNING: SSD1306 init failed (check wiring/I2C address) - continuing without display");
    return;
  }
  display.setTextColor(SSD1306_WHITE);
  display.setTextWrap(false);
  drawFrame();
}

// Eyes easing open, one step per call - meant to be called once per iteration of the
// WiFi-connect wait loop in setup() so the display animates instead of sitting blank,
// without adding any extra delay of its own (it reuses that loop's existing wait).
void drawBootFrame(uint8_t step) {
  static const uint8_t openness[] = {1, 2, 4, 6, 8, 8};   // eyelid height in px: closed -> open
  uint8_t h = openness[step % 6];
  display.clearDisplay();
  display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
  for (int i = -1; i <= 1; i += 2) {
    int16_t ex = ICON_CX + i * 9;
    display.fillRect(ex - 4, ICON_CY - h / 2, 8, h, SSD1306_WHITE);
  }
  drawLabel("WAKING UP");
  display.display();
}

// Shown once the setup portal's AP+webserver are up (see runWifiSetupPortal()): the network
// name to join plus its password (the IP is still logged to Serial, but the password is the
// one thing that isn't otherwise guessable), reusing the existing drawLabel()/drawWifiIcon()
// helpers and the same subtitle-row layout drawFrame() uses rather than inventing a new state.
void drawSetupScreen() {
  display.clearDisplay();
  display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
  drawWifiIcon(128 - 14, 0);          // g_wifiConnected is false here - shows the "disconnected" slash
  drawLabel("JOIN " WIFI_SETUP_AP_SSID);
  display.setTextSize(1);
  display.setCursor(0, SUBTITLE_Y);
  display.print("Pass: " WIFI_SETUP_AP_PASSWORD);
  display.display();
}

// One-off "cheers" celebration, shown for a couple seconds the first time setup() connects
// after a fresh save from the portal (see consumeFreshProvisionFlag()), before falling through
// to the normal idle screen. Reuses drawEmojiSmiley() at a bigger radius plus a few sparkle
// rays for a "party" look, rather than inventing a whole new icon language.
void playCheersAnimation() {
  display.clearDisplay();
  display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
  drawWifiIcon(128 - 14, 0);
  drawEmojiSmiley(ICON_CX, ICON_CY, 13);
  static const int8_t rayDX[] = {-1,  1, -1, 1, 0,  0};
  static const int8_t rayDY[] = {-1, -1,  1, 1, -1, 1};
  for (uint8_t i = 0; i < 6; i++) {
    int16_t x0 = ICON_CX + rayDX[i] * 19, y0 = ICON_CY + rayDY[i] * 13;
    display.drawFastHLine(x0 - 2, y0, 5, SSD1306_WHITE);
    display.drawFastVLine(x0, y0 - 2, 5, SSD1306_WHITE);
  }
  drawLabel("CONNECTED!");
  display.display();
  delay(1800);
}

// Quick "eyes snapping open" flourish played the instant a touch begins, before switching
// to the full STATE_RECORDING waveform screen - purely cosmetic, kept short (~150ms total)
// so it doesn't add noticeable latency to the mic capture that starts right after.
void playWakeAnimation() {
  static const uint8_t openness[] = {2, 5, 8};
  for (uint8_t i = 0; i < 3; i++) {
    display.clearDisplay();
    display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
    drawWifiIcon(128 - 14, 0);
    for (int s = -1; s <= 1; s += 2) {
      int16_t ex = ICON_CX + s * 9;
      display.fillRect(ex - 4, ICON_CY - openness[i] / 2, 8, openness[i], SSD1306_WHITE);
    }
    drawLabel("LISTENING");
    display.display();
    delay(50);
  }
}

// Call whenever the assistant's state changes. subtitle is optional (e.g. first few words
// of a transcript) and is truncated to fit one line - pass "" (or omit) for none.
void updateDisplay(AssistantState newState, const char* subtitle = "") {
  g_state = newState;
  g_animFrame = 0;
  g_lastAnimMs = millis();
  if (newState == STATE_IDLE) {
    g_showClock = false;                         // always (re)enter idle on the face, not mid-clock
    g_idleRotateAtMs = millis() + IDLE_FACE_MS;
    g_glancing = false;
    g_nextGlanceMs = millis() + 3000 + random(3000);   // first glance 3-6s after going idle
  }
  strncpy(g_subtitle, subtitle, sizeof(g_subtitle) - 1);
  g_subtitle[sizeof(g_subtitle) - 1] = '\0';
  drawFrame();
}

void setDisplayError(ErrorKind kind) {
  g_errorKind = kind;
  updateDisplay(STATE_ERROR);
}

void setWifiConnected(bool connected) {
  if (connected == g_wifiConnected) return;
  g_wifiConnected = connected;
  drawFrame();
}

// Call once per loop() iteration. Cheap: error is fully static (no-op). Idle redraws every
// ANIM_INTERVAL_MS to drive the breathing pulse (and, less often, a mood animation, curious
// glance, or face<->clock flip) - same cadence as the other states' icon animations, so this
// stays safe to run alongside I2S.
void tickDisplay() {
  uint32_t now = millis();
  if (g_state == STATE_ERROR) return;
  // Skip animation-driven display.display() calls during TTS's active phase - a ~20-25ms SSD1306
  // I2C flush every 300ms would starve I2S and glitch the amp throughout the reply. The static
  // icon painted by the state-transition updateDisplay() is enough.
  //
  // STATE_MUSIC is deliberately NOT skipped here (unlike PROCESSING/SPEAKING) so the equalizer
  // bars and song-title scroll animate during playback: drainMusicRing() only ever writes one
  // ~11.6ms slice per loop() iteration, and MUSIC_RING_BYTES (~0.37s) has enough headroom to
  // absorb an occasional 20-25ms flush every 300ms without underrunning, since the decoder/SD
  // read side normally fills the ring faster than realtime (see MUSIC_RING_BYTES's comment).
  // Verify on real hardware if songs start crackling after this change - if so, revert to
  // skipping like the other two states and drop the animation instead.
  if (g_state == STATE_PROCESSING || g_state == STATE_SPEAKING) return;

  if (g_state == STATE_IDLE) {
    if (g_moodUntilMs != 0 && now >= g_moodUntilMs) {
      g_moodUntilMs = 0;
    } else if (g_moodUntilMs == 0 && !g_showClock) {
      if (g_glancing && now >= g_glanceUntilMs) {
        g_glancing = false;
        g_nextGlanceMs = now + 5000 + random(3000);   // next glance in 5-8s
      } else if (!g_glancing && now >= g_nextGlanceMs) {
        g_glancing = true;
        g_glanceDir = random(3);
        g_glanceUntilMs = now + GLANCE_DURATION_MS;
      }
    }
    if (g_moodUntilMs == 0 && ntpSynced && now >= g_idleRotateAtMs) {
      g_showClock = !g_showClock;
      g_idleRotateAtMs = now + (g_showClock ? IDLE_CLOCK_MS : IDLE_FACE_MS);
    }
    if (now - g_lastAnimMs >= ANIM_INTERVAL_MS) {
      g_lastAnimMs = now;
      g_animFrame++;
      drawFrame();
    }
    return;
  }

  if (now - g_lastAnimMs < ANIM_INTERVAL_MS) return;
  g_lastAnimMs = now;
  g_animFrame++;
  drawFrame();
}
// ================== end OLED status display ==================

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

// Called once, early in setup(): holding the touch pad continuously through this window wipes
// saved WiFi creds and forces the setup portal, even if a previously-saved network would still
// connect fine (e.g. the device has been moved to a new location). A normal boot (pad untouched)
// bails out on the very first check below, so this costs no perceptible delay on the common path.
bool checkBootLongPressForSetup() {
  uint32_t start = millis();
  drawLabel("HOLD TO SETUP");
  display.display();
  while (millis() - start < WIFI_RESET_HOLD_MS) {
    if (digitalRead(TOUCH_PIN) != HIGH) return false;   // released early (or never touched) - not a request
    delay(20);
  }
  return true;   // held continuously through the whole window
}

// Same gesture as checkBootLongPressForSetup(), but polled from loop() the instant a tap starts
// (rather than once at boot) so a long hold works anytime the device is running too - e.g. to
// recover from a bad WiFi reset without needing a power-cycle. A normal quick tap releases well
// before WIFI_RESET_HOLD_MS elapses, so this adds negligible delay to the common tap-to-record path.
bool checkRuntimeLongHoldForWifiReset() {
  uint32_t start = millis();
  while (millis() - start < WIFI_RESET_HOLD_MS) {
    if (digitalRead(TOUCH_PIN) != HIGH) return false;   // released early - just a normal tap
    delay(20);
  }
  return true;   // held continuously through the whole window
}

// Records until a second tap is detected (edge-triggered via touchTapped(), same as the tap
// that started this recording) or RECORD_SECONDS elapses as a safety cap. Reusing touchTapped()
// here is safe per its own comment: it never runs concurrently with loop()'s call to it, since
// this function blocks loop() entirely until it returns.
void recordUntilStop() {
  recBytes = 0;
  int32_t raw[256];
  int16_t* out = (int16_t*)recBuffer;
  size_t cnt = 0;
  long long sum = 0;
  uint32_t startMs = millis();
  uint32_t lastSpeakerFeedMs = startMs;   // see feedSilentSlice() - keeps the amp's clock locked
                                          // through however long this recording turns out to last

  while (recBytes < RECORD_BUFFER_BYTES && (millis() - startMs) < RECORD_SECONDS * 1000UL) {
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
    if (millis() - lastSpeakerFeedMs >= SPEAKER_KEEPALIVE_MS) {
      feedSilentSlice();
      lastSpeakerFeedMs = millis();
    }
    if (touchTapped()) break;   // second tap = stop recording and send
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
  uint32_t lastSpeakerFeedMs = t0;   // see feedSilentSlice() - keeps the amp's clock locked
                                     // through however long this upload takes over WiFi
  const size_t CHUNK = 4096;
  for (size_t off = 0; off < recBytes; off += CHUNK) {
    size_t len = min(CHUNK, recBytes - off);
    webSocket.sendBIN(recBuffer + off, len);
    yield();  // let the WiFi/TCP stack breathe and avoid tripping the watchdog on long sends
    if (millis() - lastSpeakerFeedMs >= SPEAKER_KEEPALIVE_MS) {
      feedSilentSlice();
      lastSpeakerFeedMs = millis();
    }
  }
  webSocket.sendTXT("{\"type\":\"end\"}");
  waitingForReply = true;
  audioEndReceived = false;
  speakingShown = false;
  ttsPrebufferPrimed = false;
  fadeInSamplesRemaining = 0;
  // display already flipped to STATE_PROCESSING by the caller the instant the second tap landed
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

// Keep BCLK/WS toggling continuously even with nothing queued. I2S DAC/amp combos like the
// MAX98357 need a steady clock to stay locked; going fully silent (no write() calls) lets it
// drift out of lock, so the first real samples of the next reply come out garbled while it
// re-syncs. Feeding zeros is cheap insurance against that - called from drainTtsRing() every
// idle loop() iteration, and periodically from recordUntilStop()/sendAudioToBackend() too,
// since those block loop() (and therefore drainTtsRing()) for seconds at a time otherwise.
void feedSilentSlice() {
  const size_t sliceSamples = 512;
  uint8_t stereoBuf[sliceSamples * 4];  // MAX98357 needs a full L+R frame per sample
  memset(stereoBuf, 0, sizeof(stereoBuf));
  I2S_speaker.write(stereoBuf, sizeof(stereoBuf));
}

// Paced consumer: called once per loop() iteration. Always writes exactly one ~21ms slice to
// I2S_speaker - real TTS audio (16-bit, 24kHz, mono, headerless — matches services/tts.py's
// contract) if any is queued, otherwise silence to keep the amp's clock locked (see above).
// I2S_speaker.write() blocks for ~21ms per slice either way, which naturally paces this
// function's caller (loop()) at real-time without starving webSocket.loop() for long stretches.
void drainTtsRing() {
  const size_t sliceSamples = 512;
  const size_t sliceBytes   = sliceSamples * 2;

  if (ttsFill == 0) {
    if (audioEndReceived && waitingForReply) {
      waitingForReply = false;
      audioEndReceived = false;
      speakingShown = false;
      ttsPrebufferPrimed = false;
      fadeInSamplesRemaining = 0;
      updateDisplay(STATE_IDLE);
      playMood(900);   // quick happy/curious blink - a successful reply just finished
      Serial.println("[WS] reply complete\n==================================================\n");
    }
    feedSilentSlice();
    return;
  }

  // Prebuffer gate: on the first slice of a new reply, wait until we have enough queued that a
  // slow/fragmented first WS frame can't underrun us mid-slice. Very short replies (that finish
  // before hitting the threshold) still play - audioEndReceived releases the gate immediately.
  if (!ttsPrebufferPrimed) {
    if (ttsFill < TTS_PREBUFFER_BYTES && !audioEndReceived) {
      feedSilentSlice();
      return;
    }
    ttsPrebufferPrimed = true;
    fadeInSamplesRemaining = TTS_FADEIN_SAMPLES;
  }

  size_t n = min(sliceBytes, ttsFill) & ~size_t(1);   // keep 16-bit sample alignment
  if (n == 0) return;

  if (!speakingShown) {
    speakingShown = true;
    pendingSpeakingDisplay = true;   // loop() flushes the display AFTER our first I2S write returns
  }

  uint8_t raw[sliceBytes];
  uint8_t stereoBuf[sliceSamples * 4];  // MAX98357 needs a full L+R frame per sample
  size_t firstPart = TTS_RING_BYTES - ttsTail;
  if (firstPart > n) firstPart = n;
  memcpy(raw, ttsRing + ttsTail, firstPart);
  if (firstPart < n) memcpy(raw + firstPart, ttsRing, n - firstPart);

  size_t samples = n / 2;
  for (size_t s = 0; s < samples; s++) {
    int16_t sample = (int16_t)(raw[s * 2] | (raw[s * 2 + 1] << 8));
    int32_t scaled = ((int32_t)sample * g_volumePercent) / 100;
    if (fadeInSamplesRemaining > 0) {
      // Linear ramp: attenuation goes 0 -> full over the first TTS_FADEIN_SAMPLES samples.
      uint32_t num = TTS_FADEIN_SAMPLES - fadeInSamplesRemaining;
      scaled = (scaled * (int32_t)num) / TTS_FADEIN_SAMPLES;
      fadeInSamplesRemaining--;
    }
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
      updateDisplay(STATE_IDLE);
      Serial.println("[WS] connected to backend");
      break;

    case WStype_DISCONNECTED:
      wsConnected = false;
      waitingForReply = false;
      audioEndReceived = false;
      ignoreIncomingAudio = false;
      speakingShown = false;
      ttsPrebufferPrimed = false;
      fadeInSamplesRemaining = 0;
      pendingSpeakingDisplay = false;
      ttsHead = ttsTail = ttsFill = 0;   // discard any partial audio from the interrupted turn
      setDisplayError(ERR_WIFI);
      // WiFi.status() here tells you whether this is a real WiFi drop or just the backend/
      // tunnel going away while WiFi itself is still fine - both show the same "WIFI LOST"
      // icon, but only one of them is actually WiFi.
      Serial.printf("[WS] disconnected (WiFi status: %s) - will auto-reconnect, or tap to retry now\n",
                    wifiStatusStr(WiFi.status()));
      break;

    case WStype_TEXT: {
      JsonDocument doc;
      if (deserializeJson(doc, (const char*)payload, length)) {
        Serial.println("[WS] bad JSON from server");
        break;
      }
      String t = doc["type"] | "";
      if (t == "transcript") {
        const char* heard = (const char*)(doc["text"] | "");
        Serial.printf("YOU SAID:\n  \"%s\"\n", heard);
        updateDisplay(STATE_PROCESSING, heard);   // STT done, LLM/TTS still pending
        if (heard[0] != '\0') {
          // Real speech was heard, so a normal reply is coming - don't auto-resume whatever song
          // this listen may have interrupted (see g_songWasInterrupted's comment above).
          g_songWasInterrupted = false;
        }
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
          speakingShown = false;
          ttsPrebufferPrimed = false;
          fadeInSamplesRemaining = 0;
          pendingSpeakingDisplay = false;
          ttsHead = ttsTail = ttsFill = 0;   // discard any partial audio for the failed turn
          if (g_songWasInterrupted) {
            // Nothing came of interrupting the song to listen (no speech heard this turn) - pick
            // back up where we left off rather than showing an error for what wasn't a real failure.
            g_songWasInterrupted = false;
            Serial.println("[WS] no question asked - resuming interrupted song");
            g_pendingSongPath  = g_interruptedSongPath;
            g_pendingSongTitle = g_interruptedSongTitle;
            g_songPending = true;
          } else {
            setDisplayError(ERR_API);
            Serial.printf("[WS] error: %s\n", (const char*)(doc["detail"] | ""));
          }
        }
      } else if (t == "play_song") {
        String path  = (const char*)(doc["path"]  | "");
        String title = (const char*)(doc["title"] | "");
        if (sdReady && path.length() && isMp3(path)) {
          g_songQueueCount = 0;     // fresh single-song request replaces any playlist still queued
          g_songHistoryCount = 0;   // ...and its "prev" history too
          // Deferred to loop() rather than started here - see g_songPending's comment above.
          g_pendingSongPath  = path;
          g_pendingSongTitle = title;
          g_songPending = true;
          Serial.printf("[WS] play_song queued: %s (%s)\n", title.c_str(), path.c_str());
        } else {
          Serial.printf("[WS] play_song ignored (sdReady=%s, path=\"%s\")\n",
                        sdReady ? "true" : "false", path.c_str());
        }
      } else if (t == "play_song_queue") {
        // Playlist-style request (backend's play_song_queue tool, e.g. "play some Arijit Singh
        // songs"): first entry starts the normal deferred way, the rest fill g_songQueue* for
        // musicLoop() to advance through automatically as each track finishes on its own.
        JsonArray songs = doc["songs"].as<JsonArray>();
        g_songQueueCount = 0;     // fresh playlist request replaces any playlist still queued
        g_songHistoryCount = 0;   // ...and its "prev" history too
        bool first = true;
        for (JsonVariant sv : songs) {
          String path  = sv["path"]  | "";
          String title = sv["title"] | "";
          if (!sdReady || !path.length() || !isMp3(path)) continue;
          if (first) {
            g_pendingSongPath  = path;
            g_pendingSongTitle = title;
            g_songPending = true;
            first = false;
          } else if (g_songQueueCount < SONG_QUEUE_MAX) {
            g_songQueuePath[g_songQueueCount]  = path;
            g_songQueueTitle[g_songQueueCount] = title;
            g_songQueueCount++;
          }
        }
        Serial.printf("[WS] play_song_queue: %d song(s) queued (sdReady=%s)\n",
                      (first ? 0 : 1) + g_songQueueCount, sdReady ? "true" : "false");
      } else if (t == "download_song") {
        String url        = (const char*)(doc["url"]         | "");
        String path        = (const char*)(doc["path"]        | "");
        String title       = (const char*)(doc["title"]       | "");
        String downloadId  = (const char*)(doc["download_id"] | "");
        if (sdReady && url.length() && path.length()) {
          g_songQueueCount   = 0;   // fresh single-song request replaces any playlist still queued
          g_songHistoryCount = 0;   // ...and its "prev" history too
          g_downloadUrl      = url;
          g_downloadPath     = path;
          g_downloadTitle    = title;
          g_downloadId       = downloadId;
          g_downloadPending  = true;
          Serial.printf("[WS] download_song queued: %s -> %s\n", title.c_str(), path.c_str());
        } else {
          Serial.printf("[WS] download_song ignored (sdReady=%s, url=%d, path=%d)\n",
                        sdReady ? "true" : "false", url.length(), path.length());
          sendDownloadAck(downloadId, false);   // otherwise the backend waits for an ack that
                                                 // will never come until _sweep_stale()'s timeout
        }
      } else if (t == "stop_song") {
        g_songPending = false;
        g_downloadPending = false;
        g_songQueueCount = 0;     // explicit stop cancels the rest of the playlist too
        g_songHistoryCount = 0;   // ...and its "prev" history
        stopMusic();
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

// ---- WiFi credential + backend host storage (NVS via Preferences) + captive-portal setup ----
// Lets the same binary join any network and point at any backend without a re-flash: both are
// entered once through a phone-facing setup page and persisted here, instead of being #defined
// at compile time. See runWifiSetupPortal() below and its call sites in setup()/loop().

bool loadSavedWifiCreds(String &ssid, String &pass) {
  Preferences prefs;
  prefs.begin("wifi", true);   // read-only; fine even if the namespace doesn't exist yet
  ssid = prefs.getString("ssid", "");
  pass = prefs.getString("pass", "");
  prefs.end();
  return ssid.length() > 0;
}

void saveWifiCreds(const String &ssid, const String &pass) {
  Preferences prefs;
  prefs.begin("wifi", false);
  prefs.putString("ssid", ssid);
  prefs.putString("pass", pass);
  prefs.end();
}

// Clears only the saved WiFi creds (vpshost is left untouched) so the next boot has nothing to
// connect with and falls straight into the setup portal - used by the "resetwifi" serial command.
void clearWifiCreds() {
  Preferences prefs;
  prefs.begin("wifi", false);
  prefs.remove("ssid");
  prefs.remove("pass");
  prefs.end();
}

// Falls back to VPS_HOST_DEFAULT until a host has ever been saved via the setup portal.
String loadVpsHost() {
  Preferences prefs;
  prefs.begin("wifi", true);
  String host = prefs.getString("vpshost", VPS_HOST_DEFAULT);
  prefs.end();
  return host;
}

void saveVpsHost(const String &host) {
  Preferences prefs;
  prefs.begin("wifi", false);
  prefs.putString("vpshost", host);
  prefs.end();
}

// Marks "just provisioned" (WiFi and/or backend host) so setup() shows a one-off "cheers"
// celebration the next time it connects successfully, instead of the plain idle screen.
void markFreshProvision() {
  Preferences prefs;
  prefs.begin("wifi", false);
  prefs.putBool("fresh", true);
  prefs.end();
}

// True exactly once, the first time setup() connects successfully after a fresh save from the
// portal (across the ESP.restart() that follows a save) - see markFreshProvision() above.
bool consumeFreshProvisionFlag() {
  Preferences prefs;
  prefs.begin("wifi", false);
  bool fresh = prefs.getBool("fresh", false);
  if (fresh) prefs.putBool("fresh", false);
  prefs.end();
  return fresh;
}

// Built fresh per request (rather than a static PROGMEM template) so it can show the
// currently-saved backend host as a reference point - this endpoint is only ever hit a
// handful of times during setup, so the extra String work is a non-issue.
void handleSetupRoot() {
  String html =
    "<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>ESP32 Setup</title></head>"
    "<body style=\"font-family:sans-serif;text-align:center;padding:24px\">"
    "<h2>Connect ESP32 to WiFi</h2>"
    "<form action=\"/save\" method=\"POST\">"
    "<input name=\"ssid\" placeholder=\"WiFi Name (SSID)\" style=\"width:80%;padding:8px;margin:6px\"><br>"
    "<input name=\"pass\" type=\"password\" placeholder=\"WiFi Password\" style=\"width:80%;padding:8px;margin:6px\"><br>"
    "<hr style=\"margin:20px 0\">"
    "<p style=\"color:#666;font-size:0.9em;margin-bottom:4px\">Backend server - only if it changed"
    " (current: " + g_vpsHost + ")</p>"
    "<input name=\"vpshost\" placeholder=\"e.g. abc-def.trycloudflare.com\" style=\"width:80%;padding:8px;margin:6px\"><br>"
    "<input type=\"submit\" value=\"Save & Connect\" style=\"padding:10px 24px;margin-top:10px\">"
    "</form></body></html>";
  setupServer.send(200, "text/html", html);
}

void handleSetupSave() {
  String ssid    = setupServer.hasArg("ssid")    ? setupServer.arg("ssid")    : "";
  String pass    = setupServer.hasArg("pass")    ? setupServer.arg("pass")    : "";
  String vpshost = setupServer.hasArg("vpshost") ? setupServer.arg("vpshost") : "";
  vpshost.trim();

  // Both fields are optional so this same form/reboot cycle also covers "just update the
  // backend host, WiFi is fine" (reached via the boot long-press) - but at least one of the
  // two has to actually be provided, or there's nothing to do.
  if (ssid.length() == 0 && vpshost.length() == 0) {
    setupServer.send(400, "text/plain", "Enter a WiFi SSID and/or a backend host to save");
    return;
  }

  String summary;
  if (ssid.length() > 0) {
    saveWifiCreds(ssid, pass);   // pass may be empty, e.g. for an open target network
    summary += "WiFi: \"" + ssid + "\". ";
  }
  if (vpshost.length() > 0) {
    saveVpsHost(vpshost);
    summary += "Backend host: \"" + vpshost + "\".";
  }
  markFreshProvision();

  setupServer.send(200, "text/html",
    "<html><body style='font-family:sans-serif;text-align:center;padding:24px'>"
    "<h3>Saved!</h3><p>" + summary + "</p><p>Rebooting...</p>"
    "<p>You can reconnect this phone to your normal WiFi now.</p></body></html>");
  delay(1500);      // let the HTTP response actually flush to the phone before the AP disappears
  ESP.restart();
}

// Any URL the OS's captive-portal probe hits (Android's /generate_204, iOS's
// /hotspot-detect.html, Windows's /connecttest.txt, etc.) that isn't "/" or "/save" lands here.
// Redirecting instead of returning each probe's expected clean response is exactly what makes
// the OS conclude "this network has a sign-in page" and pop the captive-portal sheet.
void handleSetupNotFound() {
  setupServer.sendHeader("Location", String("http://") + WiFi.softAPIP().toString() + "/", true);
  setupServer.send(302, "text/plain", "");
}

// Blocking captive-portal setup mode. Never returns under normal operation -
// handleSetupSave() saves the new credentials and calls ESP.restart(), so the only way "out"
// is a reboot into the normal setup()/loop() flow with the new creds now present in NVS.
void runWifiSetupPortal() {
  WiFi.mode(WIFI_AP);
  WiFi.softAP(WIFI_SETUP_AP_SSID, WIFI_SETUP_AP_PASSWORD);
  IPAddress apIP = WiFi.softAPIP();   // defaults to 192.168.4.1

  dnsServer.start(DNS_PORT, "*", apIP);   // redirect every DNS lookup to us -> captive-portal popup

  setupServer.on("/", HTTP_GET, handleSetupRoot);
  setupServer.on("/save", HTTP_POST, handleSetupSave);
  setupServer.onNotFound(handleSetupNotFound);
  setupServer.begin();

  drawSetupScreen();
  Serial.printf("[WiFi] setup portal up - join \"%s\" (password: %s) and open http://%s/\n",
                WIFI_SETUP_AP_SSID, WIFI_SETUP_AP_PASSWORD, apIP.toString().c_str());

  while (true) {   // dedicated blocking loop - nothing else needs to run concurrently
    dnsServer.processNextRequest();
    setupServer.handleClient();
    delay(2);
  }
}

const char* wifiStatusStr(wl_status_t s) {
  switch (s) {
    case WL_IDLE_STATUS:     return "IDLE_STATUS";
    case WL_NO_SSID_AVAIL:   return "NO_SSID_AVAIL (SSID not found - wrong name, out of range, or a 5GHz-only network? ESP32 is 2.4GHz-only)";
    case WL_SCAN_COMPLETED:  return "SCAN_COMPLETED";
    case WL_CONNECT_FAILED:  return "CONNECT_FAILED (likely wrong password)";
    case WL_CONNECTION_LOST: return "CONNECTION_LOST";
    case WL_DISCONNECTED:    return "DISCONNECTED";
    case WL_CONNECTED:       return "CONNECTED";
    default:                 return "UNKNOWN";
  }
}

// Blocking WiFi (re)connect attempt, up to timeoutMs. Logs every status change it passes
// through (not just a dot per 300ms) so a real failure reason - bad password, SSID not
// found/out of range, etc. - shows up in Serial instead of a bare timeout. Reused for both
// the initial boot connect and touch-triggered retries (see setup() and loop()).
bool connectWifi(const char* ssid, const char* pass, uint32_t timeoutMs) {
  Serial.printf("[WiFi] connecting to \"%s\"...\n", ssid);
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);   // clear any stale association before a fresh attempt
  delay(100);
  WiFi.setSleep(false);
  WiFi.begin(ssid, pass);

  uint32_t t0 = millis();
  uint8_t bootStep = 0;
  wl_status_t lastStatus = (wl_status_t)255;   // sentinel so the first status always logs
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) {
    wl_status_t s = WiFi.status();
    if (s != lastStatus) {
      Serial.printf("[WiFi] status: %s\n", wifiStatusStr(s));
      lastStatus = s;
    }
    drawBootFrame(bootStep++);   // eyes opening, in place of a blank screen - same 300ms wait either way
    delay(300);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[WiFi] FAILED after %lums - last status: %s\n",
                  (unsigned long)(millis() - t0), wifiStatusStr(WiFi.status()));
    return false;
  }

  Serial.printf("[WiFi] connected. IP=%s  RSSI=%d dBm\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

// ---- SD-card file browser (tiles + download + upload + delete) ----------------------------
// Runs on fileServer (port SD_SERVER_PORT) once WiFi is up, alongside the voice pipeline.
// Reuses the same list+download design already proven in sd_card_info.ino, just wired to the
// shared g_wifiSsid/WebServer pattern instead of that sketch's own hardcoded WiFi/setup() loop.
const char* sdCardTypeName(uint8_t type) {
  switch (type) {
    case CARD_NONE:  return "No card detected";
    case CARD_MMC:   return "MMC";
    case CARD_SD:    return "SDSC";
    case CARD_SDHC:  return "SDHC/SDXC";
    default:         return "Unknown";
  }
}

// Shared CSS for every page this webserver serves (Home/Browse/Music/Volume) - kept as one
// constant so the nav bar and tile grid look the same everywhere rather than each handler
// rolling its own inline styles. Pure ASCII (icons below use numeric HTML character references
// rather than literal UTF-8 in the source, same reasoning as the &laquo;/&raquo; already used in
// handleMusicPage - it renders identically in the browser without depending on this file's own
// encoding).
const char* PAGE_CSS =
  "body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:#111;color:#eee;margin:0}"
  "nav{display:flex;gap:18px;background:#1b1b1b;padding:12px 16px;position:sticky;top:0}"
  "nav a{color:#8ab4f8;text-decoration:none;font-size:14px;font-weight:600}"
  "nav a.active{color:#fff;border-bottom:2px solid #8ab4f8}"
  "main{padding:16px}"
  "h3{margin:4px 0 14px}"
  "a{color:#8ab4f8}"
  ".grid{display:flex;flex-wrap:wrap;gap:12px}"
  ".tile{background:#1e1e1e;border:1px solid #333;border-radius:12px;padding:14px 10px;width:120px;"
  "text-align:center;position:relative}"
  ".tile a.open{display:block;color:#eee;text-decoration:none}"
  ".tile .icon{font-size:30px;line-height:1}"
  ".tile .name{font-size:12px;margin-top:8px;word-break:break-word;max-height:34px;overflow:hidden}"
  ".tile .size{font-size:10px;color:#888;margin-top:2px}"
  ".tile form{position:absolute;top:4px;right:4px;margin:0}"
  ".del{background:#3a1414;color:#f88;border:1px solid #622;border-radius:6px;font-size:12px;"
  "width:20px;height:20px;line-height:1;cursor:pointer}"
  ".ctrl a,.ctrl button{background:#1e1e1e;border:1px solid #333;color:#8ab4f8;border-radius:8px;"
  "padding:8px 14px;text-decoration:none;font-size:13px;margin:0 8px 8px 0;display:inline-block;"
  "cursor:pointer}"
  ".upload{margin:16px 0}"
  "input[type=range]{width:100%}"
  ".sub{color:#999;font-size:13px}";

// Wraps `body` with the shared <head>/CSS and nav bar - `active` is "home"/"browse"/"music"/
// "volume" so the matching nav link gets the .active underline. `extraHead` is optional raw HTML
// appended inside <head> (e.g. handleMusicPage()'s auto-refresh <meta>).
String htmlPage(const String& title, const String& active, const String& body, const String& extraHead = "") {
  String html = "<html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">";
  html += "<title>" + title + "</title><style>" + String(PAGE_CSS) + "</style>" + extraHead + "</head><body>";
  html += "<nav>"
          "<a href=\"/\" class=\"" + String(active == "home" ? "active" : "") + "\">Home</a>"
          "<a href=\"/browse\" class=\"" + String(active == "browse" ? "active" : "") + "\">Browse</a>"
          "<a href=\"/music\" class=\"" + String(active == "music" ? "active" : "") + "\">Music</a>"
          "<a href=\"/volume\" class=\"" + String(active == "volume" ? "active" : "") + "\">Volume</a>"
          "</nav><main>" + body + "</main></body></html>";
  return html;
}

// &#128193; folder, &#127925; musical note, &#128196; generic file - numeric refs so this stays
// plain ASCII in the source regardless of how the phone/browser renders the glyph.
String tileIcon(bool isDir, const String& leaf) {
  if (isDir) return "&#128193;";
  String lower = leaf; lower.toLowerCase();
  if (lower.endsWith(".mp3") || lower.endsWith(".wav")) return "&#127925;";
  return "&#128196;";
}

// Renders one tile per immediate child of dirname (no recursion - folders link back into
// /browse?path= so the user drills in one level at a time on click). Each tile is either an
// <a> to open it (download for a file, navigate for a folder) plus a small "&times;" button that
// POSTs to /delete for that exact path - confirm() guards it client-side since there's no undo.
void listSdDirTiles(const String& dirname, String& html) {
  File root = SD_MMC.open(dirname);
  if (!root || !root.isDirectory()) return;

  File entry = root.openNextFile();
  while (entry) {
    String name = entry.name();
    String leaf = name.startsWith("/") ? name.substring(name.lastIndexOf('/') + 1) : name;
    String fullPath = name.startsWith("/") ? name : dirname + (dirname.endsWith("/") ? "" : "/") + name;
    bool isDir = entry.isDirectory();
    String sizeKb = isDir ? "" : (String(entry.size() / 1024) + " KB");
    entry.close();   // metadata already captured above - close before any recursive/child work

    html += "<div class=\"tile\">";
    html += "<a class=\"open\" href=\"" + String(isDir ? "/browse?path=" : "/download?path=") + fullPath + "\">";
    html += "<div class=\"icon\">" + tileIcon(isDir, leaf) + "</div>";
    html += "<div class=\"name\">" + leaf + (isDir ? "/" : "") + "</div>";
    if (sizeKb.length()) html += "<div class=\"size\">" + sizeKb + "</div>";
    html += "</a>";
    html += "<form method=\"POST\" action=\"/delete\" "
            "onsubmit=\"return confirm('Delete " + leaf + (isDir ? " and everything inside it" : "") +
            "? This cannot be undone.');\">"
            "<input type=\"hidden\" name=\"path\" value=\"" + fullPath + "\">"
            "<input type=\"hidden\" name=\"dir\" value=\"" + dirname + "\">"
            "<button class=\"del\" type=\"submit\">&times;</button></form>";
    html += "</div>";

    entry = root.openNextFile();
  }
  root.close();
}

void handleHome() {
  String body = "<h3>ARIA</h3>";
  if (!sdReady) {
    body += "<p class=\"sub\">SD card not mounted.</p>";
  } else {
    float freeMB  = (SD_MMC.totalBytes() - SD_MMC.usedBytes()) / (1024.0 * 1024.0);
    float totalMB = SD_MMC.totalBytes() / (1024.0 * 1024.0);
    body += "<p class=\"sub\">" + String(freeMB, 0) + " MB free of " + String(totalMB, 0) + " MB &middot; volume " +
            String(g_volumePercent) + "%</p>";
  }
  if (musicPlaying) {
    body += "<p class=\"sub\">Now playing: " + g_currentTrackName + (musicPaused ? " (paused)" : "") + "</p>";
  }
  body += "<div class=\"grid\">"
          "<div class=\"tile\"><a class=\"open\" href=\"/browse\"><div class=\"icon\">&#128193;</div>"
          "<div class=\"name\">Browse</div></a></div>"
          "<div class=\"tile\"><a class=\"open\" href=\"/music\"><div class=\"icon\">&#127925;</div>"
          "<div class=\"name\">Music</div></a></div>"
          "<div class=\"tile\"><a class=\"open\" href=\"/volume\"><div class=\"icon\">&#128266;</div>"
          "<div class=\"name\">Volume</div></a></div>"
          "</div>";
  fileServer.send(200, "text/html", htmlPage("ARIA", "home", body));
}

void handleBrowse() {
  if (!sdReady) {
    fileServer.send(200, "text/html", htmlPage("Browse", "browse", "<p>SD card not mounted.</p>"));
    return;
  }
  String path = fileServer.hasArg("path") ? fileServer.arg("path") : "/";
  String body = "<h3>" + path + "</h3>";
  if (path != "/") {
    int lastSlash = path.lastIndexOf('/');
    String parent = (lastSlash <= 0) ? "/" : path.substring(0, lastSlash);
    body += "<p class=\"ctrl\"><a href=\"/browse?path=" + parent + "\">.. Up</a></p>";
  }
  // Uploads land in whatever folder is currently being browsed - handleFileUpload() reads the
  // same ?path= query param off this form's action URL.
  body += "<form class=\"upload\" method=\"POST\" action=\"/upload?path=" + path +
          "\" enctype=\"multipart/form-data\">"
          "<input type=\"file\" name=\"file\"> <input type=\"submit\" value=\"Upload\"></form>";
  body += "<div class=\"grid\">";
  listSdDirTiles(path, body);
  body += "</div>";
  fileServer.send(200, "text/html", htmlPage("Browse", "browse", body));
}

// Blocks loop() (and therefore webSocket.loop()/drainTtsRing()) for as long as the transfer
// takes, same tradeoff this codebase already accepts for recordUntilStop()/sendAudioToBackend()/
// runWifiSetupPortal() - fine for occasional file grabs, but a large file will pause the voice
// assistant (WS keepalive, TTS playback) until the download finishes.
void handleSdDownload() {
  if (!fileServer.hasArg("path")) {
    fileServer.send(400, "text/plain", "Missing path parameter");
    return;
  }
  String path = fileServer.arg("path");
  File file = SD_MMC.open(path, FILE_READ);
  if (!file || file.isDirectory()) {
    fileServer.send(404, "text/plain", "File not found: " + path);
    return;
  }
  String filename = path.substring(path.lastIndexOf('/') + 1);
  fileServer.sendHeader("Content-Disposition", "attachment; filename=\"" + filename + "\"");
  fileServer.streamFile(file, "application/octet-stream");
  file.close();
}

// Streaming upload callback for POST /upload (see handleBrowse()'s <form>) - the WebServer
// library calls this repeatedly as multipart body chunks arrive, then calls the plain handler
// registered alongside it (handleUploadDone below) once the whole request has been read. Mirrors
// handleSdDownload()'s blocks-loop()-for-the-duration tradeoff, just in the other direction.
void handleFileUpload() {
  HTTPUpload& upload = fileServer.upload();
  if (upload.status == UPLOAD_FILE_START) {
    if (!sdReady) return;
    String dir = fileServer.hasArg("path") ? fileServer.arg("path") : "/";
    if (!dir.endsWith("/")) dir += "/";
    g_uploadPath = dir + upload.filename;
    Serial.printf("[SD] upload starting: %s\n", g_uploadPath.c_str());
    g_uploadFile = SD_MMC.open(g_uploadPath, FILE_WRITE);
    if (!g_uploadFile) Serial.printf("[SD] failed to open %s for writing\n", g_uploadPath.c_str());
  } else if (upload.status == UPLOAD_FILE_WRITE) {
    if (g_uploadFile) g_uploadFile.write(upload.buf, upload.currentSize);
  } else if (upload.status == UPLOAD_FILE_END) {
    if (g_uploadFile) {
      g_uploadFile.close();
      Serial.printf("[SD] upload complete: %s (%u bytes)\n", g_uploadPath.c_str(), upload.totalSize);
    }
  } else if (upload.status == UPLOAD_FILE_ABORTED) {
    if (g_uploadFile) g_uploadFile.close();
    if (sdReady && g_uploadPath.length()) SD_MMC.remove(g_uploadPath);   // don't leave a partial file behind
    Serial.println("[SD] upload aborted");
  }
}

// Called once handleFileUpload() above has finished consuming the whole request body - redirects
// back to the folder just uploaded into so the new file shows up in the listing.
void handleUploadDone() {
  String dir = fileServer.hasArg("path") ? fileServer.arg("path") : "/";
  fileServer.sendHeader("Location", "/browse?path=" + dir);
  fileServer.send(303);
}

// Deletes `path` outright if it's a file; if it's a directory, recurses into every child first
// (depth-first) so the directory itself is empty by the time SD_MMC.rmdir() runs - the SD_MMC/FS
// layer only removes empty directories. Each child is fully closed before recursing into it,
// since reusing an open File handle for the parent across a recursive SD_MMC.open() of a child on
// this FS layer isn't guaranteed safe. Returns false (and stops) on the first failure, so a
// partially-deleted directory can be retried rather than silently reporting success.
bool deleteRecursive(const String& path) {
  File f = SD_MMC.open(path);
  if (!f) return false;
  if (!f.isDirectory()) {
    f.close();
    return SD_MMC.remove(path);
  }
  File entry = f.openNextFile();
  while (entry) {
    String name = entry.name();
    String childPath = name.startsWith("/") ? name : path + (path.endsWith("/") ? "" : "/") + name;
    entry.close();
    if (!deleteRecursive(childPath)) { f.close(); return false; }
    entry = f.openNextFile();
  }
  f.close();
  return SD_MMC.rmdir(path);
}

// POST-only (see listSdDirTiles()'s <form>, guarded client-side by a confirm() prompt since this
// is irreversible) - deletes a single file or a whole directory tree, then redirects back to
// whichever folder the tile was shown in.
void handleSdDelete() {
  if (!sdReady || !fileServer.hasArg("path")) {
    fileServer.send(400, "text/plain", "Missing path parameter");
    return;
  }
  String path = fileServer.arg("path");
  String returnDir = fileServer.hasArg("dir") ? fileServer.arg("dir") : "/";
  if (path.length() == 0 || path == "/") {
    fileServer.send(400, "text/plain", "Refusing to delete the SD card root");
    return;
  }
  bool ok = deleteRecursive(path);
  Serial.printf("[SD] delete %s -> %s\n", path.c_str(), ok ? "ok" : "FAILED (not empty or in use?)");
  fileServer.sendHeader("Location", "/browse?path=" + returnDir);
  fileServer.send(303);
}

// ---- /music page: now-playing + up-next queue, with prev/pause/next/stop controls -----------
// Same fileServer/port as the SD browser above, just a different route. Controls are plain GET
// links (like handleSdDownload()'s file links) rather than a JS-driven player - each one performs
// its action then 303-redirects back to /music so a page reload always reflects the new state.
// The music-changing functions themselves (advanceToNextSong(), dequeueNextSong(), etc.) live
// down in the "Music playback" section further below; forward-calling them here is fine, same as
// webSocketEvent() forward-calling playSongFile()/downloadAndPlaySong() earlier in the file.
void handleMusicPage() {
  String body;
  body += "<h3>Now Playing</h3><div class=\"grid\">";
  body += "<div class=\"tile\"><div class=\"icon\">&#127925;</div><div class=\"name\">";
  body += musicPlaying ? (g_currentTrackName + (musicPaused ? " (paused)" : "")) : "<i>Nothing playing</i>";
  body += "</div></div></div>";
  body += "<p class=\"ctrl\">"
          "<a href=\"/music/prev\">&laquo; Prev</a>"
          "<a href=\"/music/pause\">" + String(musicPaused ? "Resume" : "Pause") + "</a>"
          "<a href=\"/music/next\">Next &raquo;</a>"
          "<a href=\"/music/stop\">Stop</a>"
          "</p>";
  body += "<h3>Up Next</h3>";
  if (g_songQueueCount == 0) {
    body += "<p class=\"sub\"><i>Queue empty</i></p>";
  } else {
    body += "<div class=\"grid\">";
    for (int i = 0; i < g_songQueueCount; i++) {
      body += "<div class=\"tile\"><div class=\"icon\">&#127925;</div><div class=\"name\">" +
              g_songQueueTitle[i] + "</div></div>";
    }
    body += "</div>";
  }
  fileServer.send(200, "text/html",
                   htmlPage("Music", "music", body, "<meta http-equiv=\"refresh\" content=\"5\">"));
}

void handleMusicNext() {
  if (musicPlaying) advanceToNextSong();
  else if (g_songQueueCount > 0) dequeueNextSong();   // nothing was playing but songs are waiting
  fileServer.sendHeader("Location", "/music");
  fileServer.send(303);
}

void handleMusicPrev() {
  if (g_songHistoryCount > 0) {
    // Put whatever's currently playing back at the front of the queue so "Next" can return to
    // it, then start the most recent history entry the same deferred way play_song does.
    if (musicPlaying) {
      unshiftQueue(g_currentSongPath, g_currentTrackName);
      stopMusic();
    }
    String path, title;
    popHistory(path, title);
    g_pendingSongPath  = path;
    g_pendingSongTitle = title;
    g_songPending = true;
  }
  fileServer.sendHeader("Location", "/music");
  fileServer.send(303);
}

void handleMusicPauseToggle() {
  if (musicPlaying) musicPaused = !musicPaused;
  fileServer.sendHeader("Location", "/music");
  fileServer.send(303);
}

void handleMusicStop() {
  g_songPending = false;
  g_downloadPending = false;
  g_songQueueCount = 0;
  g_songHistoryCount = 0;
  stopMusic();
  fileServer.sendHeader("Location", "/music");
  fileServer.send(303);
}

// Single shared setter for g_volumePercent (0-150, see its declaration up top) - used by both the
// serial "v<n>" bench command and the /volume web control so the clamping/logging stays in sync
// between the two.
void setVolume(int v) {
  if (v < 0) v = 0;
  if (v > 150) v = 150;
  g_volumePercent = v;
  Serial.printf("Volume set to %d%%\n", g_volumePercent);
}

void handleVolumePage() {
  String body = "<h3>Volume</h3>";
  body += "<p class=\"sub\">" + String(g_volumePercent) + "% (100% = unity gain, above may clip)</p>";
  body += "<form method=\"GET\" action=\"/volume/set\">"
          "<input type=\"range\" name=\"value\" min=\"0\" max=\"150\" value=\"" + String(g_volumePercent) +
          "\" onchange=\"this.form.submit()\">"
          "</form>";
  body += "<p class=\"ctrl\">"
          "<a href=\"/volume/set?value=0\">Mute</a>"
          "<a href=\"/volume/set?value=100\">100%</a>"
          "<a href=\"/volume/set?value=150\">150%</a>"
          "</p>";
  fileServer.send(200, "text/html", htmlPage("Volume", "volume", body));
}

void handleVolumeSet() {
  if (fileServer.hasArg("value")) setVolume(fileServer.arg("value").toInt());
  fileServer.sendHeader("Location", "/volume");
  fileServer.send(303);
}

// Shown for a few seconds right after the file browser comes up, since Serial (where this URL
// is also logged) isn't always plugged in/open - reuses the same top-bar+label+subtitle layout
// drawFrame()'s other one-off screens (e.g. playCheersAnimation()) use, rather than a new one.
void showSdServerScreen(const String& ip) {
  display.clearDisplay();
  display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
  drawWifiIcon(128 - 14, 0);
  drawLabel(sdReady ? "SD SERVER" : "NO SD CARD");
  display.setTextSize(1);
  display.setCursor(0, SUBTITLE_Y);
  display.print(ip + ":" + String(SD_SERVER_PORT));
  display.display();
  delay(3000);
}

// Called once from setup() after WiFi is up. Mounting failure (no card / bad wiring) is
// non-fatal - sdReady stays false and handleBrowse() reports it over HTTP instead of blocking
// the rest of the voice assistant from starting.
void initSdFileServer() {
  SD_MMC.setPins(SD_MMC_CLK, SD_MMC_CMD, SD_MMC_D0);
  if (!SD_MMC.begin("/sdcard", true)) {   // true = 1-bit mode, see SD_MMC_D0 comment above
    Serial.println("[SD] FAILED to mount SD_MMC card - check wiring/pins and formatting (FAT32/exFAT)");
  } else if (SD_MMC.cardType() == CARD_NONE) {
    Serial.println("[SD] no card attached");
  } else {
    sdReady = true;
    Serial.printf("[SD] mounted (%s), %.2f MB free\n", sdCardTypeName(SD_MMC.cardType()),
                  (SD_MMC.totalBytes() - SD_MMC.usedBytes()) / (1024.0 * 1024.0));
    // Ensure the /esp32 working folder exists - mkdir() is a no-op (returns false) if the dir
    // is already there, so this is safe to run on every boot.
    if (SD_MMC.mkdir("/esp32")) Serial.println("[SD] created /esp32 folder");
    else                         Serial.println("[SD] /esp32 folder already present");
  }

  fileServer.on("/", HTTP_GET, handleHome);
  fileServer.on("/browse", HTTP_GET, handleBrowse);
  fileServer.on("/download", HTTP_GET, handleSdDownload);
  fileServer.on("/upload", HTTP_POST, handleUploadDone, handleFileUpload);
  fileServer.on("/delete", HTTP_POST, handleSdDelete);
  fileServer.on("/music", HTTP_GET, handleMusicPage);
  fileServer.on("/music/next", HTTP_GET, handleMusicNext);
  fileServer.on("/music/prev", HTTP_GET, handleMusicPrev);
  fileServer.on("/music/pause", HTTP_GET, handleMusicPauseToggle);
  fileServer.on("/music/stop", HTTP_GET, handleMusicStop);
  fileServer.on("/volume", HTTP_GET, handleVolumePage);
  fileServer.on("/volume/set", HTTP_GET, handleVolumeSet);
  fileServer.begin();
  String ip = WiFi.localIP().toString();
  Serial.printf("[SD] file browser up at http://%s:%d/ (sdReady=%s)\n",
                ip.c_str(), SD_SERVER_PORT, sdReady ? "true" : "false");
  showSdServerScreen(ip);
}

// ---- Music playback (MP3 files on the SD card, voice-triggered via the "play_song"/"stop_song"
// WS messages handled in webSocketEvent() above) --------------------------------------------
// Reuses the exact I2S_speaker instance drainTtsRing() drives for TTS, reconfigured to the
// song's own sample rate while playing - see loop()'s musicPlaying/waitingForReply gating, which
// keeps this and TTS playback from ever running at the same time, so there's no contention over
// the shared peripheral.
//
// ConsumeSample() must never block: AudioGeneratorMP3::loop() can push an entire decoded MP3
// frame's samples through it synchronously in one call, so a version that wrote straight to I2S
// per small batch (an earlier iteration of this code) could rack up several back-to-back blocking
// I2S writes inside a single musicLoop() call - starving loop() of the chance to notice a touch
// tap or service webSocket.loop() for that whole stretch (this is exactly the touch-doesn't-stop-
// the-song bug it caused). So instead ConsumeSample() only does a fast, non-blocking memcpy into
// musicRing, mirroring pushTtsBytes()/ttsRing; the actual paced I2S_speaker.write() happens once
// per musicLoop() iteration in drainMusicRing() below, bounded to one ~11.6ms slice regardless of
// how much the decoder just produced - same bound drainTtsRing() already guarantees for TTS.
#define MUSIC_RING_BYTES 65536   // 64KiB: ~0.37s of buffered 44.1kHz/16-bit stereo audio (PSRAM) -
                                 // headroom for the decoder/SD-read side to burst ahead of playback
uint8_t* musicRing = nullptr;
size_t   musicHead = 0;   // next write index (producer: I2SMusicOutput::ConsumeSample)
size_t   musicTail = 0;   // next read index  (consumer: drainMusicRing)
size_t   musicFill = 0;   // bytes currently buffered
bool     musicDecoderDone = false;   // true once mp3Decoder->loop() returns false (EOF/error)

class I2SMusicOutput : public AudioOutput {
 public:
  bool begin() override { return true; }
  bool stop() override { return true; }
  bool SetRate(int hz) override {
    AudioOutput::SetRate(hz);
    I2S_speaker.end();
    I2S_speaker.setPins(I2S_SPK_BCLK, I2S_SPK_LRC, I2S_SPK_DOUT, -1);
    I2S_speaker.begin(I2S_MODE_STD, hz, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
    return true;
  }
  bool ConsumeSample(int16_t sample[2]) override {
    if (!musicRing || musicFill + 4 > MUSIC_RING_BYTES) return false;   // ring full - decoder retries this sample
    // Same g_volumePercent scaling drainTtsRing() applies to TTS - without this, music plays at
    // unity gain while TTS plays boosted (default 150%), so a song sounds quieter by comparison.
    int16_t scaled[2];
    for (int ch = 0; ch < 2; ch++) {
      int32_t s = ((int32_t)sample[ch] * g_volumePercent) / 100;
      if (s > 32767) s = 32767; else if (s < -32768) s = -32768;
      scaled[ch] = (int16_t)s;
    }
    size_t firstPart = MUSIC_RING_BYTES - musicHead;
    if (firstPart >= 4) {
      memcpy(musicRing + musicHead, scaled, 4);
    } else {
      memcpy(musicRing + musicHead, scaled, firstPart);
      memcpy(musicRing, (uint8_t*)scaled + firstPart, 4 - firstPart);
    }
    musicHead = (musicHead + 4) % MUSIC_RING_BYTES;
    musicFill += 4;
    return true;
  }
};

AudioFileSourceFS*  mp3Source  = nullptr;
AudioGeneratorMP3*  mp3Decoder = nullptr;
I2SMusicOutput*     mp3Output  = nullptr;

bool isMp3(const String& name) {
  String lower = name;
  lower.toLowerCase();
  return lower.endsWith(".mp3");
}

// Back to TTS's fixed sample rate once a song finishes/stops - mirrors I2SMusicOutput::SetRate()
// above, just restoring the other side of the same speaker instance.
void restoreSpeakerForTts() {
  I2S_speaker.end();
  I2S_speaker.setPins(I2S_SPK_BCLK, I2S_SPK_LRC, I2S_SPK_DOUT, -1);
  I2S_speaker.begin(I2S_MODE_STD, TTS_SAMPLE_RATE_HZ, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
}

void stopMusic() {
  if (!musicPlaying) return;
  if (mp3Decoder) { mp3Decoder->stop(); delete mp3Decoder; mp3Decoder = nullptr; }
  if (mp3Source)  { mp3Source->close(); delete mp3Source;  mp3Source  = nullptr; }
  musicHead = musicTail = musicFill = 0;   // discard any buffered-but-unplayed audio
  musicDecoderDone = false;
  musicPlaying = false;
  restoreSpeakerForTts();
  updateDisplay(STATE_IDLE);
  Serial.println("[music] stopped");
}

// Redraws the DOWNLOADING screen with a progress bar in place of the icon - called once up front
// and then periodically (throttled in the caller to avoid saturating I2C) while
// downloadAndPlaySong() blocks loop() for the network transfer. percent < 0 means the server
// didn't send a Content-Length, so the size (and therefore progress) is unknown.
void drawDownloadProgress(int percent) {
  display.clearDisplay();
  display.drawFastHLine(0, 11, 128, SSD1306_WHITE);
  drawWifiIcon(128 - 14, 0);

  const int16_t barW = 100, barH = 10, barX = (128 - barW) / 2, barY = ICON_CY - barH / 2;
  display.drawRect(barX, barY, barW, barH, SSD1306_WHITE);
  if (percent >= 0) {
    int16_t fillW = (barW - 2) * constrain(percent, 0, 100) / 100;
    if (fillW > 0) display.fillRect(barX + 1, barY + 1, fillW, barH - 2, SSD1306_WHITE);
  }

  drawLabel("DOWNLOADING");

  char pct[6];
  if (percent >= 0) snprintf(pct, sizeof(pct), "%d%%", constrain(percent, 0, 100));
  else strcpy(pct, "...");
  display.setTextSize(1);
  int16_t w = strlen(pct) * 6;
  display.setCursor((128 - w) / 2, SUBTITLE_Y);
  display.print(pct);

  display.display();
}

// Tells the backend whether its download_song action ended up with the file actually saved to
// SD card - see services/yt_song.py's confirm_download() on the backend side, which only updates
// the song index (and cleans up its own temp copy) once this arrives. downloadId is empty for a
// download_song action that never included one (e.g. a stale build on the other end), in which
// case this is a deliberate no-op rather than sending a malformed ack the backend can't match up.
void sendDownloadAck(const String& downloadId, bool success) {
  if (downloadId.length() == 0) return;
  String msg = "{\"type\":\"download_ack\",\"download_id\":\"" + downloadId + "\",\"success\":" +
               (success ? "true" : "false") + "}";
  webSocket.sendTXT(msg);
}

// Downloads an MP3 from `url` to `sdPath` on the SD card in 4KB chunks, then hands off to
// playSongFile() for playback. Creates the parent directory (e.g. the album folder) if it
// doesn't exist yet. Blocks loop() for the duration of the download (same tradeoff as
// handleSdDownload / recordUntilStop).
void downloadAndPlaySong(const String& url, const String& sdPath, const String& title, const String& downloadId) {
  if (!sdReady) { Serial.println("[dl] SD card not ready"); sendDownloadAck(downloadId, false); return; }

  // Ensure the parent directory exists (e.g. /Naveen/songs/<album>/) - a single mkdir() is
  // enough since SONGS_ROOT ("/Naveen/songs") itself is already guaranteed to exist.
  int lastSlash = sdPath.lastIndexOf('/');
  if (lastSlash > 0) {
    String dir = sdPath.substring(0, lastSlash);
    SD_MMC.mkdir(dir);
  }

  // Skip download if the file already exists on the SD card
  if (SD_MMC.exists(sdPath)) {
    Serial.printf("[dl] already on SD card: %s - playing directly\n", sdPath.c_str());
    sendDownloadAck(downloadId, true);
    playSongFile(sdPath, title);
    return;
  }

  Serial.printf("[dl] downloading %s -> %s\n", url.c_str(), sdPath.c_str());
  updateDisplay(STATE_PROCESSING);

  HTTPClient http;
  http.begin(url);
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  http.setTimeout(30000);
  int httpCode = http.GET();

  if (httpCode != HTTP_CODE_OK) {
    Serial.printf("[dl] HTTP %d for %s\n", httpCode, url.c_str());
    http.end();
    setDisplayError(ERR_API);
    sendDownloadAck(downloadId, false);
    return;
  }

  int contentLength = http.getSize();
  WiFiClient* stream = http.getStreamPtr();
  File outFile = SD_MMC.open(sdPath, FILE_WRITE);
  if (!outFile) {
    Serial.printf("[dl] failed to open %s for writing\n", sdPath.c_str());
    http.end();
    setDisplayError(ERR_API);
    sendDownloadAck(downloadId, false);
    return;
  }

  drawDownloadProgress(contentLength > 0 ? 0 : -1);

  const size_t BUF_SIZE = 4096;
  uint8_t buf[BUF_SIZE];
  int totalWritten = 0;
  uint32_t lastProgressMs = millis();
  while (http.connected() && (contentLength < 0 || totalWritten < contentLength)) {
    size_t avail = stream->available();
    if (avail == 0) { delay(1); continue; }
    size_t toRead = (avail < BUF_SIZE) ? avail : BUF_SIZE;
    int bytesRead = stream->read(buf, toRead);
    if (bytesRead <= 0) break;
    outFile.write(buf, bytesRead);
    totalWritten += bytesRead;

    // Throttled to ~1 redraw / 150ms - an SSD1306 full-frame flush costs ~20-25ms over I2C, and
    // a 4KB chunk over a fast local link can arrive far more often than that.
    uint32_t now = millis();
    if (contentLength > 0 && now - lastProgressMs >= 150) {
      lastProgressMs = now;
      drawDownloadProgress((int)((totalWritten * 100LL) / contentLength));
    }
  }
  outFile.close();
  http.end();

  if (contentLength > 0) drawDownloadProgress(100);   // guarantee the final frame lands even if throttled out above

  Serial.printf("[dl] saved %d bytes to %s\n", totalWritten, sdPath.c_str());

  if (totalWritten > 0 && isMp3(sdPath)) {
    sendDownloadAck(downloadId, true);
    playSongFile(sdPath, title);
  } else {
    Serial.println("[dl] download appears empty or not an mp3");
    SD_MMC.remove(sdPath);
    setDisplayError(ERR_API);
    sendDownloadAck(downloadId, false);
  }
}

void playSongFile(const String& path, const String& title) {
  if (!sdReady) { Serial.println("[music] SD card not ready - can't play"); return; }
  if (!musicRing) { Serial.println("[music] ps_malloc failed at boot - music playback unavailable"); return; }
  if (musicPlaying) stopMusic();

  mp3Source = new AudioFileSourceFS(SD_MMC, path.c_str());
  if (!mp3Output) mp3Output = new I2SMusicOutput();
  mp3Decoder = new AudioGeneratorMP3();
  musicHead = musicTail = musicFill = 0;
  musicDecoderDone = false;
  if (!mp3Decoder->begin(mp3Source, mp3Output)) {
    Serial.printf("[music] failed to start %s\n", path.c_str());
    delete mp3Decoder; mp3Decoder = nullptr;
    delete mp3Source;  mp3Source  = nullptr;
    setDisplayError(ERR_GENERIC);   // otherwise the screen is left stuck on whatever it showed
                                    // before this call (e.g. downloadAndPlaySong()'s 100% bar)
    return;
  }
  g_currentTrackName = title.length() ? title : path.substring(path.lastIndexOf('/') + 1);
  g_currentSongPath  = path;
  musicPlaying = true;
  musicPaused  = false;   // a fresh track always starts playing, regardless of the last one's pause state
  Serial.printf("[music] playing %s (%s)\n", g_currentTrackName.c_str(), path.c_str());
  updateDisplay(STATE_MUSIC, g_currentTrackName.c_str());
}

// Paced consumer: called once per musicLoop() iteration. Writes exactly one ~11.6ms slice to
// I2S_speaker if any is queued - same bounded-per-call contract as drainTtsRing(), regardless of
// how much mp3Decoder->loop() just decoded into musicRing this iteration.
void drainMusicRing() {
  if (!musicRing || musicFill == 0) return;
  const size_t sliceBytes = 512 * 4;   // 512 frames, 4 bytes/frame (int16 L + int16 R)
  size_t n = min(sliceBytes, musicFill) & ~size_t(3);   // keep 4-byte frame alignment
  if (n == 0) return;

  uint8_t raw[sliceBytes];
  size_t firstPart = MUSIC_RING_BYTES - musicTail;
  if (firstPart > n) firstPart = n;
  memcpy(raw, musicRing + musicTail, firstPart);
  if (firstPart < n) memcpy(raw + firstPart, musicRing, n - firstPart);
  I2S_speaker.write(raw, n);

  musicTail = (musicTail + n) % MUSIC_RING_BYTES;
  musicFill -= n;
}

// Pops the next queued song (see g_songQueue*'s declaration up top) into the g_pendingSong*
// deferred-start slot used elsewhere (webSocketEvent()'s "play_song" handler, the interrupted-
// song resume path), shifting the rest of the queue down one slot. Only called (from
// advanceToNextSong() below) once g_songQueueCount is already known to be > 0.
void dequeueNextSong() {
  g_pendingSongPath  = g_songQueuePath[0];
  g_pendingSongTitle = g_songQueueTitle[0];
  g_songPending = true;
  for (int i = 1; i < g_songQueueCount; i++) {
    g_songQueuePath[i - 1]  = g_songQueuePath[i];
    g_songQueueTitle[i - 1] = g_songQueueTitle[i];
  }
  g_songQueueCount--;
}

// Inserts a song at the FRONT of the up-next queue (unlike a normal enqueue, which appends) -
// used by handleMusicPrev() to put the song being left back where "Next" would return to it.
// Silently drops the request if the queue is already full rather than growing past SONG_QUEUE_MAX.
void unshiftQueue(const String& path, const String& title) {
  if (g_songQueueCount >= SONG_QUEUE_MAX) return;
  for (int i = g_songQueueCount; i > 0; i--) {
    g_songQueuePath[i]  = g_songQueuePath[i - 1];
    g_songQueueTitle[i] = g_songQueueTitle[i - 1];
  }
  g_songQueuePath[0]  = path;
  g_songQueueTitle[0] = title;
  g_songQueueCount++;
}

// Appends to the /music page's "Prev" history (see g_songHistory*'s declaration up top). Drops
// the oldest entry to make room past SONG_HISTORY_MAX rather than growing unbounded - a playlist
// longer than that just can't be rewound all the way back to its start.
void pushHistory(const String& path, const String& title) {
  if (!path.length()) return;   // nothing has played yet - don't record an empty placeholder
  if (g_songHistoryCount >= SONG_HISTORY_MAX) {
    for (int i = 1; i < SONG_HISTORY_MAX; i++) {
      g_songHistoryPath[i - 1]  = g_songHistoryPath[i];
      g_songHistoryTitle[i - 1] = g_songHistoryTitle[i];
    }
    g_songHistoryCount = SONG_HISTORY_MAX - 1;
  }
  g_songHistoryPath[g_songHistoryCount]  = path;
  g_songHistoryTitle[g_songHistoryCount] = title;
  g_songHistoryCount++;
}

// Pops the most recently played song off the history stack. Caller must check
// g_songHistoryCount > 0 first.
void popHistory(String& path, String& title) {
  g_songHistoryCount--;
  path  = g_songHistoryPath[g_songHistoryCount];
  title = g_songHistoryTitle[g_songHistoryCount];
}

// Shared by musicLoop() (current track finished on its own) and the /music/next web control:
// records the song being left in history, stops it, and starts whatever's at the front of the
// queue if anything is - otherwise playback just stops. Caller must check musicPlaying first.
void advanceToNextSong() {
  pushHistory(g_currentSongPath, g_currentTrackName);
  // stopMusic() briefly shows STATE_IDLE (one ~20-25ms display flush) even when a queued song is
  // about to start right back up on the next loop() iteration - a minor cosmetic blip traded for
  // reusing the same well-tested stop/start path rather than a separate track-to-track one.
  stopMusic();
  if (g_songQueueCount > 0) dequeueNextSong();
}

// Called once per loop() iteration while musicPlaying, mirroring drainTtsRing()'s role for TTS.
// The decode step just fills musicRing (no blocking I2S write in it - see ConsumeSample()'s
// comment above), so this always drains a slice too; the track only advances once decoding has
// finished *and* the ring has fully drained, so the last ~0.37s of buffered audio isn't cut off
// early (mirrors drainTtsRing()'s ttsFill==0 && audioEndReceived gate).
void musicLoop() {
  if (musicPaused) return;   // frozen by the /music/pause control - no decode step, no I2S write
  if (mp3Decoder && mp3Decoder->isRunning() && !musicDecoderDone) {
    if (!mp3Decoder->loop()) {
      mp3Decoder->stop();   // track finished (or hit a decode error)
      musicDecoderDone = true;
    }
  } else {
    musicDecoderDone = true;
  }
  drainMusicRing();
  if (musicDecoderDone && musicFill == 0) {
    Serial.println("[music] finished");
    advanceToNextSong();
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

  musicRing = (uint8_t*) ps_malloc(MUSIC_RING_BYTES);
  if (!musicRing) Serial.println("WARNING: ps_malloc (music ring) failed - SD song playback disabled");

  displayInit();   // early, so mic/WiFi init failures below can still show an error icon
  g_vpsHost = loadVpsHost();   // before any possible runWifiSetupPortal() call below, which shows it

  if (checkBootLongPressForSetup()) {
    Serial.println("[WiFi] long-press detected at boot - clearing saved WiFi creds and entering setup portal");
    clearWifiCreds();
    runWifiSetupPortal();   // never returns - saving credentials triggers ESP.restart()
  }

  I2S_mic.setPins(I2S_MIC_BCLK, I2S_MIC_LRCL, -1, I2S_MIC_DOUT);
  if (!I2S_mic.begin(I2S_MODE_STD, SAMPLE_RATE_HZ,
                     I2S_DATA_BIT_WIDTH_32BIT,
                     I2S_SLOT_MODE_MONO,
                     I2S_STD_SLOT_LEFT)) {     // if words never come, try I2S_STD_SLOT_RIGHT
    Serial.println("FATAL: I2S mic init failed");
    setDisplayError(ERR_MIC);
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

  bool haveCreds = loadSavedWifiCreds(g_wifiSsid, g_wifiPass);
  bool connected = haveCreds && connectWifi(g_wifiSsid.c_str(), g_wifiPass.c_str(), 20000);
  if (!connected) {
    // No saved network (first-ever boot) or it failed to connect (moved/router changed) -
    // either way, drop straight into the setup portal instead of hanging or looping on
    // credentials that clearly aren't working. This never returns; a successful save reboots
    // into this same setup() with the new creds now in NVS.
    Serial.println(haveCreds
      ? "[WiFi] saved credentials failed to connect - entering setup portal"
      : "[WiFi] no saved WiFi credentials - entering setup portal");
    runWifiSetupPortal();
  }
  setWifiConnected(true);
  if (consumeFreshProvisionFlag()) {
    Serial.println("[WiFi] first connect after setup - celebrating on screen");
    playCheersAnimation();   // then falls straight through to the normal idle screen below
  }
  updateDisplay(STATE_IDLE);

  initSdFileServer();   // mounts SD_MMC (non-fatal if absent) and starts the file-browser WebServer

  configTime(GMT_OFFSET_SEC, DST_OFFSET_SEC, NTP_SERVER);
  struct tm timeinfo;
  ntpSynced = getLocalTime(&timeinfo, 5000);   // one-time wait here in setup(), never in loop()
  if (ntpSynced) Serial.printf("NTP synced: %02d:%02d\n", timeinfo.tm_hour, timeinfo.tm_min);
  else           Serial.println("WARNING: NTP sync failed - idle clock screen will stay off");

  Serial.printf("Connecting to backend wss://%s:%d%s ...\n", g_vpsHost.c_str(), VPS_PORT, VPS_WS_PATH);
  // beginSSL with no fingerprint/CA arg = certificate validation is skipped (insecure TLS).
  // Fine for testing behind cloudflared; the tunnel's cert is real, we just aren't pinning it.
  webSocket.beginSSL(g_vpsHost.c_str(), VPS_PORT, VPS_WS_PATH);
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(5000);
}

void loop() {
  webSocket.loop();   // must run every cycle to process the socket and keep it alive
  fileServer.handleClient();   // SD file browser - returns immediately if no client is connected
  if (musicPlaying) {
    musicLoop();      // pump the MP3 decoder / feed I2S_speaker one batch at a time
  } else {
    drainTtsRing();   // play back one small (~21ms) slice of buffered TTS audio, if any is queued
    if (pendingSpeakingDisplay) {
      // Deferred so the ~20-25ms SSD1306 I2C flush inside updateDisplay() doesn't starve I2S at
      // the exact moment we transition silence -> real audio; drainTtsRing() has already written
      // one slice, so the amp stays clock-locked across this flush.
      pendingSpeakingDisplay = false;
      updateDisplay(STATE_SPEAKING);
    }
  }
  tickDisplay();      // advance any in-progress icon animation (no-op most iterations)

  static uint32_t lastWifiCheckMs = 0;
  if (millis() - lastWifiCheckMs > 2000) {
    lastWifiCheckMs = millis();
    setWifiConnected(WiFi.status() == WL_CONNECTED);
  }

  // Volume is still adjustable over serial for bench tuning - doesn't conflict with the touch trigger.
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.startsWith("v") || line.startsWith("V")) {
      setVolume(line.substring(1).toInt());
    } else if (line.equalsIgnoreCase("resetwifi")) {
      Serial.println("[WiFi] resetwifi requested - clearing saved credentials and rebooting into setup portal");
      clearWifiCreds();
      delay(200);
      ESP.restart();
    }
  }

  bool tapped = touchTapped();

  // Long hold (any state, anytime) wins over every other touch gesture below - reset WiFi and
  // drop into the setup hotspot. Checked immediately off the rising edge so a normal quick tap
  // (which releases well within WIFI_RESET_HOLD_MS) falls through to the rest of loop() untouched.
  if (tapped && checkRuntimeLongHoldForWifiReset()) {
    Serial.println("[WiFi] long-hold detected - clearing saved WiFi creds and entering setup portal");
    clearWifiCreds();
    runWifiSetupPortal();   // never returns - saving credentials triggers ESP.restart()
  }

  if (g_state == STATE_ERROR && g_errorKind == ERR_WIFI && tapped) {
    Serial.println("[WiFi] retry requested via touch");
    if (connectWifi(g_wifiSsid.c_str(), g_wifiPass.c_str(), 15000)) {
      setWifiConnected(true);
      updateDisplay(STATE_IDLE);
      // Reconnect the socket immediately rather than waiting on its own 5s retry timer -
      // WiFi was just down, so any prior connection state is stale anyway.
      webSocket.disconnect();
      webSocket.beginSSL(g_vpsHost.c_str(), VPS_PORT, VPS_WS_PATH);
    } else {
      setDisplayError(ERR_WIFI);
    }
    return;
  }

  if (waitingForReply) {
    // A touch here means "interrupt and ask again" - stop whatever's playing/pending and
    // fall straight into a fresh listen below, using the same touch-to-talk gesture as idle.
    if (!tapped) {
      return;   // drainTtsRing() above already paces this loop at ~21ms/iteration via its I2S write
    }
    Serial.println("[touch] barge-in - stopping playback to listen again");
    ttsHead = ttsTail = ttsFill = 0;
    ignoreIncomingAudio = true;   // discard the interrupted turn's remaining bytes (see flag comment above)
    waitingForReply = false;
    audioEndReceived = false;
    speakingShown = false;
    ttsPrebufferPrimed = false;
    fadeInSamplesRemaining = 0;
    pendingSpeakingDisplay = false;
  }

  if (musicPlaying) {
    // Same one-tap-does-both convention as the barge-in block above: a touch stops the song and
    // falls straight through to start a fresh recording, no second tap needed.
    if (!tapped) {
      return;   // musicLoop() above already paces this iteration via its blocking I2S write
    }
    Serial.println("[touch] stopping song to listen");
    g_interruptedSongPath  = g_currentSongPath;
    g_interruptedSongTitle = g_currentTrackName;
    g_songWasInterrupted   = true;
    stopMusic();
  }

  // A queued play_song (see webSocketEvent()'s "play_song" handler) starts as soon as this
  // device is free to play it - not gated on `tapped`, since starting playback isn't a touch
  // gesture. By this point in loop(), waitingForReply/musicPlaying already reflect anything
  // drainTtsRing()/the barge-in block above just changed this same iteration.
  if (g_songPending && !waitingForReply && !musicPlaying) {
    g_songPending = false;
    playSongFile(g_pendingSongPath, g_pendingSongTitle);
    return;   // don't also fall through into a tap-triggered recording this same iteration
  }

  if (g_downloadPending && !waitingForReply && !musicPlaying) {
    g_downloadPending = false;
    downloadAndPlaySong(g_downloadUrl, g_downloadPath, g_downloadTitle, g_downloadId);
    return;
  }

  if (!tapped) {
    return;   // drainTtsRing() above already paces this loop at ~21ms/iteration via its I2S write
  }

  if (!wsConnected) {
    Serial.println("Not connected to backend yet - try again shortly.");
    return;
  }

  Serial.println(">>> tap - waking up & listening (tap again to stop) <<<");
  playWakeAnimation();
  updateDisplay(STATE_RECORDING);
  recordUntilStop();
  Serial.println("<<< second tap - thinking >>>");
  updateDisplay(STATE_PROCESSING);
  sendAudioToBackend();
}