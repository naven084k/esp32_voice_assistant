#include <Arduino.h>
#include <WiFi.h>
#include <WebServer.h>
#include <FS.h>
#include <SD_MMC.h>
#include "esp_camera.h"
// ^ WiFi/WebServer/FS/SD_MMC/esp_camera are all bundled with the esp32 Arduino core - no
//   Library Manager install needed. Board must be flashed with "PSRAM: OPI PSRAM" enabled
//   (Freenove ESP32-S3-WROOM CAM ships with PSRAM) since FRAMESIZE_UXGA framebuffers don't fit
//   in internal RAM.

// ====== EDIT THESE ======================================================
#define WIFI_SSID       "Naveens"
#define WIFI_PASSWORD   "7093030918"
// ========================================================================

#define TOUCH_PIN         1     // TTP223 touch module OUT pin - same wiring as voice_button.ino
// TTP223: VCC->3.3V, GND->GND, OUT->GPIO1. Assumes default (momentary) mode, so OUT reads HIGH
// only while a finger is touching the pad. Single tap = take one photo.
#define TOUCH_DEBOUNCE_MS 400   // capture + JPEG write takes a moment - longer than a voice tap's
                                // debounce so one touch can't fire two captures

// SD-card file browser (mirrors sd_card_info.ino / voice_button.ino's initSdFileServer()).
// ESP32-S3 has no fixed SD_MMC pin mapping, but these specific numbers are hard-wired on the
// Freenove ESP32-S3-WROOM CAM board itself (confirmed against Freenove's own official
// Sketch_07.3_Camera_SDcard example) - don't change them on this board.
#define SD_MMC_CLK      39
#define SD_MMC_CMD      38
#define SD_MMC_D0       40
#define SD_SERVER_PORT  8080
#define PHOTO_DIR       "/esp32"

// OV2640 header pinout for the Freenove ESP32-S3-WROOM CAM board. This board wires its camera
// header identically to Espressif's CAMERA_MODEL_ESP32S3_EYE - verified against Freenove's own
// camera_pins.h / Sketch_07.3_Camera_SDcard example. These are NOT generic ESP32-S3 defaults;
// a different board (AI-Thinker, ESP32-S3-CAM-LCD, etc.) uses different numbers.
#define PWDN_GPIO_NUM   -1
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM   15
#define SIOD_GPIO_NUM    4
#define SIOC_GPIO_NUM    5
#define Y9_GPIO_NUM     16
#define Y8_GPIO_NUM     17
#define Y7_GPIO_NUM     18
#define Y6_GPIO_NUM     12
#define Y5_GPIO_NUM     10
#define Y4_GPIO_NUM      8
#define Y3_GPIO_NUM      9
#define Y2_GPIO_NUM     11
#define VSYNC_GPIO_NUM   6
#define HREF_GPIO_NUM    7
#define PCLK_GPIO_NUM   13

WebServer fileServer(SD_SERVER_PORT);
bool sdReady     = false;
bool cameraReady = false;

bool     touchLastState = false;   // last-seen digitalRead(TOUCH_PIN), for rising-edge detection
uint32_t touchLastTapMs = 0;       // millis() of the last recognized tap, for debouncing

int nextPhotoIndex = 0;   // next filename to use under PHOTO_DIR, e.g. "7.jpg" - resumes from
                          // whatever's already on the card so re-flashing doesn't overwrite photos

const char* sdCardTypeName(uint8_t type) {
  switch (type) {
    case CARD_NONE:  return "No card detected";
    case CARD_MMC:   return "MMC";
    case CARD_SD:    return "SDSC";
    case CARD_SDHC:  return "SDHC/SDXC";
    default:         return "Unknown";
  }
}

// Scans PHOTO_DIR for the highest "<N>.jpg" already present so captures resume numbering
// instead of overwriting existing photos after a reboot/re-flash.
int findNextPhotoIndex() {
  int maxIndex = -1;
  File root = SD_MMC.open(PHOTO_DIR);
  if (!root || !root.isDirectory()) return 0;

  File entry = root.openNextFile();
  while (entry) {
    String name = entry.name();
    int slash = name.lastIndexOf('/');
    if (slash >= 0) name = name.substring(slash + 1);
    int dot = name.lastIndexOf('.');
    if (dot > 0) {
      int idx = name.substring(0, dot).toInt();
      if (idx > maxIndex) maxIndex = idx;
    }
    entry = root.openNextFile();
  }
  return maxIndex + 1;
}

bool initCamera() {
  camera_config_t config = {};
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location  = CAMERA_FB_IN_PSRAM;
  config.frame_size   = FRAMESIZE_UXGA;   // 1600x1200 still photo - needs PSRAM
  config.jpeg_quality = 10;               // lower number = higher quality/larger file
  config.fb_count     = 1;

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[CAM] init failed (0x%x) at UXGA - retrying at SVGA (no/less PSRAM?)\n", err);
    config.frame_size = FRAMESIZE_SVGA;   // 800x600 - fits without PSRAM
    err = esp_camera_init(&config);
    if (err != ESP_OK) {
      Serial.printf("[CAM] init failed again (0x%x) - giving up\n", err);
      return false;
    }
  }

  sensor_t* s = esp_camera_sensor_get();
  s->set_hmirror(s, 1);   // OV2640 mounts mirrored on this header - matches Freenove's own example
  s->set_vflip(s, 0);     // was 1 (Freenove's default) - flipped photos upside-down on this unit
  return true;
}

bool takePhoto() {
  if (!cameraReady) { Serial.println("[CAM] not ready - skipping capture"); return false; }
  if (!sdReady)     { Serial.println("[SD] not ready - skipping capture");  return false; }

  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) { Serial.println("[CAM] capture failed"); return false; }

  String path = String(PHOTO_DIR) + "/" + String(nextPhotoIndex) + ".jpg";
  File file = SD_MMC.open(path, FILE_WRITE);
  if (!file) {
    Serial.printf("[SD] failed to open %s for writing\n", path.c_str());
    esp_camera_fb_return(fb);
    return false;
  }
  file.write(fb->buf, fb->len);
  file.close();
  Serial.printf("[CAM] saved %s (%u bytes)\n", path.c_str(), (unsigned)fb->len);
  esp_camera_fb_return(fb);

  nextPhotoIndex++;
  return true;
}

// Rising-edge + debounce tap detection, same approach as voice_button.ino's touch handling.
void pollTouch() {
  bool state  = digitalRead(TOUCH_PIN) == HIGH;
  bool tapped = state && !touchLastState && (millis() - touchLastTapMs > TOUCH_DEBOUNCE_MS);
  touchLastState = state;
  if (tapped) {
    touchLastTapMs = millis();
    Serial.println("[TOUCH] tap detected - capturing photo");
    takePhoto();
  }
}

// Recursively builds an HTML list of every file under dirname, each linking to /download.
void listDir(const String& dirname, String& html) {
  File root = SD_MMC.open(dirname);
  if (!root || !root.isDirectory()) return;

  File entry = root.openNextFile();
  while (entry) {
    String name = entry.name();
    String fullPath = name.startsWith("/") ? name : dirname + (dirname.endsWith("/") ? "" : "/") + name;
    if (entry.isDirectory()) {
      listDir(fullPath, html);
    } else {
      html += "<li><a href=\"/download?path=" + fullPath + "\">" + fullPath + "</a> (" + String(entry.size()) + " bytes) "
              "<a href=\"/delete?path=" + fullPath + "\" onclick=\"return confirm('Delete " + fullPath + "?')\">[delete]</a></li>";
    }
    entry = root.openNextFile();
  }
}

void handleRoot() {
  if (!sdReady) {
    fileServer.send(503, "text/plain", "SD card not mounted");
    return;
  }
  String html = "<html><body><h3>ESP32-S3 Camera Photos</h3>";
  html += "<p>Camera: " + String(cameraReady ? "ready" : "NOT ready") + " | Tap the touch pad to capture a new photo.</p>";
  html += "<ul>";
  listDir(PHOTO_DIR, html);
  html += "</ul></body></html>";
  fileServer.send(200, "text/html", html);
}

void handleDownload() {
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
  fileServer.streamFile(file, "image/jpeg");
  file.close();
}

void handleDelete() {
  if (!fileServer.hasArg("path")) {
    fileServer.send(400, "text/plain", "Missing path parameter");
    return;
  }
  String path = fileServer.arg("path");
  if (!SD_MMC.exists(path)) {
    fileServer.send(404, "text/plain", "File not found: " + path);
    return;
  }
  bool ok = SD_MMC.remove(path);
  Serial.printf("[SD] delete %s: %s\n", path.c_str(), ok ? "ok" : "FAILED");
  fileServer.sendHeader("Location", "/");
  fileServer.send(303);   // redirect back to the file list either way
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== ESP32-S3 touch-triggered camera capture + SD web server ===");

  pinMode(TOUCH_PIN, INPUT);

  SD_MMC.setPins(SD_MMC_CLK, SD_MMC_CMD, SD_MMC_D0);
  if (!SD_MMC.begin("/sdcard", true)) {   // true = 1-bit mode, only D0 wired
    Serial.println("[SD] FAILED to mount SD_MMC card - check wiring/pins and formatting (FAT32/exFAT)");
  } else if (SD_MMC.cardType() == CARD_NONE) {
    Serial.println("[SD] no card attached");
  } else {
    sdReady = true;
    Serial.printf("[SD] mounted (%s), %.2f MB free\n", sdCardTypeName(SD_MMC.cardType()),
                  (SD_MMC.totalBytes() - SD_MMC.usedBytes()) / (1024.0 * 1024.0));
    if (!SD_MMC.exists(PHOTO_DIR) && SD_MMC.mkdir(PHOTO_DIR)) {
      Serial.printf("[SD] created %s folder\n", PHOTO_DIR);
    }
    nextPhotoIndex = findNextPhotoIndex();
  }

  cameraReady = initCamera();
  Serial.printf("[CAM] ready=%s\n", cameraReady ? "true" : "false");

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nWiFi connected. Tap the touch pad to take a photo; browse http://%s:%d/ to view/download them.\n",
                WiFi.localIP().toString().c_str(), SD_SERVER_PORT);

  fileServer.on("/", handleRoot);
  fileServer.on("/download", handleDownload);
  fileServer.on("/delete", handleDelete);
  fileServer.begin();
}

void loop() {
  fileServer.handleClient();   // web server keeps serving every iteration, regardless of touch state
  pollTouch();
}
