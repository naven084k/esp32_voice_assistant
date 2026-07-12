// ESP32-S3 touch-controlled prerecorded hello-world test
//
// Purpose:
// - Play a fixed PCM clip from flash
// - No microphone, no PSRAM, no runtime synthesis
// - Touch ON  -> play "hello world"
// - Touch OFF -> idle
//
// Wiring:
// MAX98357A
//   BCLK -> GPIO48
//   LRC  -> GPIO21
//   DIN  -> GPIO47
//   VIN  -> 5V
//   GND  -> GND
//   SD   -> 5V
//
// Touch sensor
//   OUT  -> GPIO14

#include <Arduino.h>
#include <driver/i2s.h>
#include <math.h>
#include "hello_world_pcm.h"

static constexpr gpio_num_t SPK_BCLK_PIN = GPIO_NUM_48;
static constexpr gpio_num_t SPK_LRC_PIN  = GPIO_NUM_21;
static constexpr gpio_num_t SPK_DIN_PIN  = GPIO_NUM_47;
static constexpr int TOUCH_PIN = 14;
static constexpr int TOUCH_ACTIVE_STATE = HIGH;

static constexpr i2s_port_t I2S_AUDIO_PORT = I2S_NUM_0;
static constexpr uint32_t SAMPLE_RATE = 8000;
static constexpr size_t FRAME_CHUNK = 256;
static constexpr float PLAYBACK_VOLUME = 0.45f;

static constexpr size_t WAV_HEADER_BYTES = 44;

bool lastTouchState = false;
bool isPlaying = false;

void installSpeakerI2S() {
  const i2s_config_t spkConfig = {
    .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = static_cast<int>(SAMPLE_RATE),
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 64,
    .use_apll = false,
    .tx_desc_auto_clear = true,
    .fixed_mclk = 0
  };

  const i2s_pin_config_t spkPins = {
    .bck_io_num = SPK_BCLK_PIN,
    .ws_io_num = SPK_LRC_PIN,
    .data_out_num = SPK_DIN_PIN,
    .data_in_num = I2S_PIN_NO_CHANGE
  };

  esp_err_t err = i2s_driver_install(I2S_AUDIO_PORT, &spkConfig, 0, nullptr);
  if (err != ESP_OK) {
    Serial.printf("i2s_driver_install failed: %d\n", err);
    while (true) delay(1000);
  }

  err = i2s_set_pin(I2S_AUDIO_PORT, &spkPins);
  if (err != ESP_OK) {
    Serial.printf("i2s_set_pin failed: %d\n", err);
    while (true) delay(1000);
  }

  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
}

static inline int16_t readSample16(const uint8_t *data, size_t byteIndex) {
  const uint16_t lo = data[byteIndex];
  const uint16_t hi = data[byteIndex + 1];
  return static_cast<int16_t>(lo | (hi << 8));
}

void playPrerecordedClip() {
  const uint8_t *wav = _home_naveen_code_esp32_hello_world_pcm_wav;
  const size_t wavLen = sizeof(_home_naveen_code_esp32_hello_world_pcm_wav);
  const size_t sampleBytes = wavLen - WAV_HEADER_BYTES;
  const size_t totalFrames = sampleBytes / sizeof(int16_t);

  Serial.printf("Playing prerecorded clip: %u frames at %u Hz\n", static_cast<unsigned>(totalFrames), SAMPLE_RATE);

  int16_t stereo[FRAME_CHUNK * 2];
  size_t frameIndex = 0;

  while (frameIndex < totalFrames) {
    const size_t frames = min(FRAME_CHUNK, totalFrames - frameIndex);

    for (size_t i = 0; i < frames; ++i) {
      const size_t sampleIndex = WAV_HEADER_BYTES + (frameIndex + i) * 2;
      const int16_t mono = readSample16(wav, sampleIndex);
      const int32_t scaled = static_cast<int32_t>(mono * PLAYBACK_VOLUME);
      const int16_t out = static_cast<int16_t>(constrain(scaled, -32768, 32767));
      stereo[i * 2] = out;
      stereo[i * 2 + 1] = out;
    }

    size_t bytesWritten = 0;
    i2s_write(I2S_AUDIO_PORT, stereo, frames * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
    frameIndex += bytesWritten / (2 * sizeof(int16_t));
  }

  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
  Serial.println("Clip finished.");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(TOUCH_PIN, INPUT);
  installSpeakerI2S();

  lastTouchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  Serial.println();
  Serial.println("Prerecorded hello-world test ready.");
  Serial.println("Touch ON  = play clip once");
  Serial.println("Touch OFF = idle");
  Serial.printf("Playback sample rate = %u Hz\n", SAMPLE_RATE);
  Serial.printf("Playback volume = %.2f\n", PLAYBACK_VOLUME);
}

void loop() {
  const bool touchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  if (touchState && !lastTouchState) {
    isPlaying = true;
    playPrerecordedClip();
    isPlaying = false;
  }

  lastTouchState = touchState;

  if (!isPlaying) {
    delay(5);
  }
}
