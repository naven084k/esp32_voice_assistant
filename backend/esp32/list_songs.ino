// Standalone sketch: recursively lists every song file under SONGS_ROOT on the SD card
// mounted via SD_MMC (same pinout/wiring as voice_button.ino's initSdFileServer()).
// Prints an indented tree plus a flat count to Serial - no WiFi/audio pipeline needed.

#include <FS.h>
#include <SD_MMC.h>

// ESP32-S3 has no fixed SD_MMC pin mapping - keep these in sync with voice_button.ino
// if you're using the same board/wiring.
#define SD_MMC_CLK   39
#define SD_MMC_CMD   38
#define SD_MMC_D0    40   // 1-bit mode - only D0 wired

#define SONGS_ROOT   "/Naveen/songs"

bool isSongFile(const String& name) {
  String lower = name;
  lower.toLowerCase();
  return lower.endsWith(".mp3") || lower.endsWith(".wav");
}

const char* sdCardTypeName(uint8_t type) {
  switch (type) {
    case CARD_NONE:  return "No card detected";
    case CARD_MMC:   return "MMC";
    case CARD_SD:    return "SDSC";
    case CARD_SDHC:  return "SDHC/SDXC";
    default:         return "Unknown";
  }
}

// Recurses into every subfolder of dirname, printing each song file's full path
// (indented to show nesting) and tallying totals via the by-reference counters.
void listSongsRecursive(const String& dirname, int depth, int& songCount, int& folderCount) {
  File dir = SD_MMC.open(dirname);
  if (!dir || !dir.isDirectory()) {
    Serial.printf("[songs] cannot open directory: %s\n", dirname.c_str());
    return;
  }

  String indent;
  for (int i = 0; i < depth; i++) indent += "  ";

  File entry = dir.openNextFile();
  while (entry) {
    String name = entry.name();
    String leaf = name.startsWith("/") ? name.substring(name.lastIndexOf('/') + 1) : name;
    String fullPath = name.startsWith("/") ? name : dirname + (dirname.endsWith("/") ? "" : "/") + name;

    if (entry.isDirectory()) {
      folderCount++;
      Serial.printf("%s[%s]\n", indent.c_str(), leaf.c_str());
      listSongsRecursive(fullPath, depth + 1, songCount, folderCount);
    } else if (isSongFile(fullPath)) {
      songCount++;
      Serial.printf("%s%s  (%s, %u bytes)\n", indent.c_str(), leaf.c_str(), fullPath.c_str(), entry.size());
    }
    entry = dir.openNextFile();
  }
}

void setup() {
  Serial.begin(115200);
  delay(1000);   // let the serial monitor attach before the first print

  SD_MMC.setPins(SD_MMC_CLK, SD_MMC_CMD, SD_MMC_D0);
  if (!SD_MMC.begin("/sdcard", true)) {   // true = 1-bit mode, see SD_MMC_D0 comment above
    Serial.println("[SD] FAILED to mount SD_MMC card - check wiring/pins and formatting (FAT32/exFAT)");
    return;
  }
  if (SD_MMC.cardType() == CARD_NONE) {
    Serial.println("[SD] no card attached");
    return;
  }
  Serial.printf("[SD] mounted (%s), %.2f MB free\n", sdCardTypeName(SD_MMC.cardType()),
                (SD_MMC.totalBytes() - SD_MMC.usedBytes()) / (1024.0 * 1024.0));

  if (!SD_MMC.exists(SONGS_ROOT)) {
    Serial.printf("[songs] %s does not exist on the card\n", SONGS_ROOT);
    return;
  }

  Serial.printf("\n[songs] listing %s (recursive)\n\n", SONGS_ROOT);
  int songCount = 0, folderCount = 0;
  listSongsRecursive(SONGS_ROOT, 0, songCount, folderCount);
  Serial.printf("\n[songs] done - %d song file(s) in %d folder(s)\n", songCount, folderCount);
}

void loop() {
  // one-shot sketch - all work happens in setup()
}
