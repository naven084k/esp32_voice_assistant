// ESP32-S3 touch-controlled record/playback
//
// Behavior:
// - Touch input HIGH  -> record from INMP441
// - Touch input LOW   -> stop recording and play back the last recording once
//
// Wiring used by this sketch:
// INMP441
//   BCLK -> GPIO40
//   WS   -> GPIO39
//   SD   -> GPIO41
//
// MAX98357A
//   BCLK -> GPIO48
//   LRC  -> GPIO21
//   DIN  -> GPIO47
//
// Touch module
//   OUT  -> GPIO14
//
// Notes:
// - This sketch expects PSRAM for a useful recording length.
// - Most touch modules drive OUT HIGH when touched. If yours is inverted,
//   change TOUCH_ACTIVE_STATE below.

#include <Arduino.h>
#include <driver/i2s.h>
#include <esp_heap_caps.h>
#include <math.h>

static constexpr gpio_num_t MIC_BCLK_PIN = GPIO_NUM_40;
static constexpr gpio_num_t MIC_WS_PIN   = GPIO_NUM_39;
static constexpr gpio_num_t MIC_SD_PIN   = GPIO_NUM_41;

static constexpr gpio_num_t SPK_BCLK_PIN = GPIO_NUM_48;
static constexpr gpio_num_t SPK_LRC_PIN  = GPIO_NUM_21;
static constexpr gpio_num_t SPK_DIN_PIN  = GPIO_NUM_47;

static constexpr int TOUCH_PIN = 14;
static constexpr int TOUCH_ACTIVE_STATE = HIGH;

static constexpr i2s_port_t I2S_AUDIO_PORT = I2S_NUM_0;

static constexpr uint32_t MIC_SAMPLE_RATE = 16000;
static constexpr uint32_t SPEAKER_SAMPLE_RATE = MIC_SAMPLE_RATE;
static constexpr size_t RECORD_SECONDS = 10;
static constexpr size_t MAX_SAMPLES = MIC_SAMPLE_RATE * RECORD_SECONDS;
static constexpr float PLAYBACK_VOLUME = 0.35f;
static constexpr float MIC_PLAYBACK_GAIN = 2.0f;
static constexpr float MIC_HIGHPASS_ALPHA = 0.995f;
static constexpr int16_t MIC_NOISE_GATE_THRESHOLD = 1400;
static constexpr uint16_t MIN_PLAYBACK_AVG_ABS = 300;
static constexpr bool DUPLICATE_TO_STEREO_OUTPUT = true;
static constexpr uint8_t PLAYBACK_REPEAT_COUNT = 10;
static constexpr bool PLAY_TEST_TONE_INSTEAD_OF_RECORDING = false;
static constexpr bool AUTO_PLAY_TEST_TONE_ON_BOOT = false;
static constexpr float TEST_TONE_FREQUENCY_HZ = 440.0f;
static constexpr uint16_t TEST_TONE_DURATION_MS = 3000;

static constexpr size_t MIC_DMA_BUFFER_SAMPLES = 256;
static constexpr size_t SPK_DMA_BUFFER_SAMPLES = 256;
static constexpr TickType_t I2S_TIMEOUT_TICKS = pdMS_TO_TICKS(20);

int16_t *recordBuffer = nullptr;
size_t recordedSamples = 0;
int16_t recordedMinSample = 32767;
int16_t recordedMaxSample = -32768;
uint64_t recordedAbsSum = 0;
size_t recordedClippedSamples = 0;
int64_t recordedSum = 0;
int16_t recordedMeanSample = 0;
int32_t micPrevInputSample = 0;
float micPrevHighpassSample = 0.0f;

bool isRecording = false;
bool isPlaying = false;
bool lastTouchState = false;
bool printedMicRawPreview = false;
size_t captureChunkCount = 0;
enum class AudioPortMode { None, Mic, Speaker };
AudioPortMode activeAudioPortMode = AudioPortMode::None;

void installMicI2S() {
  if (activeAudioPortMode != AudioPortMode::None) {
    i2s_driver_uninstall(I2S_AUDIO_PORT);
    activeAudioPortMode = AudioPortMode::None;
  }

  const i2s_config_t micConfig = {
    .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = static_cast<int>(MIC_SAMPLE_RATE),
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = MIC_DMA_BUFFER_SAMPLES,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };

  const i2s_pin_config_t micPins = {
    .bck_io_num = MIC_BCLK_PIN,
    .ws_io_num = MIC_WS_PIN,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = MIC_SD_PIN
  };

  esp_err_t err = i2s_driver_install(I2S_AUDIO_PORT, &micConfig, 0, nullptr);
  if (err != ESP_OK) {
    Serial.printf("Mic i2s_driver_install failed on port %d: %d\n", I2S_AUDIO_PORT, err);
    return;
  }

  err = i2s_set_pin(I2S_AUDIO_PORT, &micPins);
  if (err != ESP_OK) {
    Serial.printf("Mic i2s_set_pin failed on port %d: %d\n", I2S_AUDIO_PORT, err);
    return;
  }

  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
  activeAudioPortMode = AudioPortMode::Mic;
}

void installSpeakerI2S() {
  if (activeAudioPortMode != AudioPortMode::None) {
    i2s_driver_uninstall(I2S_AUDIO_PORT);
    activeAudioPortMode = AudioPortMode::None;
  }

  const i2s_config_t spkConfig = {
    .mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = static_cast<int>(SPEAKER_SAMPLE_RATE),
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = SPK_DMA_BUFFER_SAMPLES,
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
    Serial.printf("Speaker i2s_driver_install failed on port %d: %d\n", I2S_AUDIO_PORT, err);
    return;
  }

  err = i2s_set_pin(I2S_AUDIO_PORT, &spkPins);
  if (err != ESP_OK) {
    Serial.printf("Speaker i2s_set_pin failed on port %d: %d\n", I2S_AUDIO_PORT, err);
    return;
  }

  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
  activeAudioPortMode = AudioPortMode::Speaker;
}

void startRecording() {
  recordedSamples = 0;
  recordedMinSample = 32767;
  recordedMaxSample = -32768;
  recordedAbsSum = 0;
  recordedClippedSamples = 0;
  recordedSum = 0;
  recordedMeanSample = 0;
  micPrevInputSample = 0;
  micPrevHighpassSample = 0.0f;
  printedMicRawPreview = false;
  captureChunkCount = 0;
  isRecording = true;
  isPlaying = false;
  installMicI2S();
  Serial.println("Recording...");
}

void stopRecording() {
  isRecording = false;
  if (recordedSamples > 0) {
    recordedMeanSample = static_cast<int16_t>(recordedSum / static_cast<int64_t>(recordedSamples));
  }
  Serial.printf(
    "Recording stopped. Samples: %u, peak_min=%d, peak_max=%d, avg_abs=%u, clipped=%u, mean=%d\n",
    static_cast<unsigned>(recordedSamples),
    recordedMinSample,
    recordedMaxSample,
    recordedSamples == 0 ? 0U : static_cast<unsigned>(recordedAbsSum / recordedSamples),
    static_cast<unsigned>(recordedClippedSamples),
    recordedMeanSample
  );
}

void startPlayback() {
  if (!PLAY_TEST_TONE_INSTEAD_OF_RECORDING && recordedSamples == 0) {
    Serial.println("Nothing recorded.");
    return;
  }

  if (!PLAY_TEST_TONE_INSTEAD_OF_RECORDING) {
    const uint16_t avgAbs = recordedSamples == 0
      ? 0
      : static_cast<uint16_t>(recordedAbsSum / recordedSamples);
    if (avgAbs < MIN_PLAYBACK_AVG_ABS) {
      Serial.printf(
        "Recording too quiet to play back cleanly. avg_abs=%u threshold=%u\n",
        avgAbs,
        MIN_PLAYBACK_AVG_ABS
      );
      return;
    }
  }

  isPlaying = true;
  installSpeakerI2S();

  if (PLAY_TEST_TONE_INSTEAD_OF_RECORDING) {
    Serial.printf(
      "Playback test tone... freq=%.1fHz duration_ms=%u volume=%.2f repeats=%u\n",
      TEST_TONE_FREQUENCY_HZ,
      TEST_TONE_DURATION_MS,
      PLAYBACK_VOLUME,
      PLAYBACK_REPEAT_COUNT
    );
    return;
  }

  const unsigned playbackMs = static_cast<unsigned>((recordedSamples * 1000UL) / MIC_SAMPLE_RATE);
  Serial.printf(
    "Playback... samples=%u duration_ms=%u volume=%.2f repeats=%u\n",
    static_cast<unsigned>(recordedSamples),
    playbackMs,
    PLAYBACK_VOLUME,
    PLAYBACK_REPEAT_COUNT
  );
}

void playTestToneBlocking() {
  int16_t samples[128];
  float phase = 0.0f;
  const float phaseStep = 2.0f * PI * TEST_TONE_FREQUENCY_HZ / SPEAKER_SAMPLE_RATE;
  const size_t totalSamples = (static_cast<size_t>(SPEAKER_SAMPLE_RATE) * TEST_TONE_DURATION_MS) / 1000;

  for (uint8_t pass = 0; pass < PLAYBACK_REPEAT_COUNT; ++pass) {
    size_t generatedSamples = 0;
    Serial.printf("Test tone pass %u/%u\n", pass + 1, PLAYBACK_REPEAT_COUNT);

    while (generatedSamples < totalSamples) {
      const size_t stereoFramesToWrite = min(static_cast<size_t>(64), totalSamples - generatedSamples);
      size_t bytesWritten = 0;

      for (size_t i = 0; i < stereoFramesToWrite; ++i) {
        const int16_t sample = static_cast<int16_t>(sinf(phase) * 32767.0f * PLAYBACK_VOLUME);
        samples[i * 2] = sample;
        samples[i * 2 + 1] = sample;
        phase += phaseStep;
        if (phase >= 2.0f * PI) {
          phase -= 2.0f * PI;
        }
      }

      esp_err_t err = i2s_write(I2S_AUDIO_PORT, samples, stereoFramesToWrite * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
      if (err != ESP_OK) {
        Serial.printf("i2s_write error during test tone: %d\n", err);
        generatedSamples = totalSamples;
        break;
      }

      generatedSamples += bytesWritten / (2 * sizeof(int16_t));
    }
  }

  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
  isPlaying = false;
  Serial.println("Test tone playback finished.");
}

void playRecordingBlocking() {
  if (PLAY_TEST_TONE_INSTEAD_OF_RECORDING) {
    playTestToneBlocking();
    return;
  }

  int16_t playbackChunk[SPK_DMA_BUFFER_SAMPLES];
  int16_t stereoChunk[SPK_DMA_BUFFER_SAMPLES * 2];

  for (uint8_t pass = 0; pass < PLAYBACK_REPEAT_COUNT; ++pass) {
    size_t offset = 0;
    Serial.printf("Playback pass %u/%u\n", pass + 1, PLAYBACK_REPEAT_COUNT);

    while (offset < recordedSamples) {
      const size_t samplesToWrite = min(static_cast<size_t>(SPK_DMA_BUFFER_SAMPLES), recordedSamples - offset);
      size_t bytesWritten = 0;

      for (size_t i = 0; i < samplesToWrite; ++i) {
        int32_t centered = static_cast<int32_t>(recordBuffer[offset + i]) - recordedMeanSample;
        int32_t scaled = static_cast<int32_t>(centered * PLAYBACK_VOLUME * MIC_PLAYBACK_GAIN);
        if (scaled > 32767) scaled = 32767;
        if (scaled < -32768) scaled = -32768;
        playbackChunk[i] = static_cast<int16_t>(scaled);
      }

      const void *writeBuffer = playbackChunk;
      size_t writeSize = samplesToWrite * sizeof(int16_t);

      if (DUPLICATE_TO_STEREO_OUTPUT) {
        for (size_t i = 0; i < samplesToWrite; ++i) {
          stereoChunk[i * 2] = playbackChunk[i];
          stereoChunk[i * 2 + 1] = playbackChunk[i];
        }
        writeBuffer = stereoChunk;
        writeSize = samplesToWrite * 2 * sizeof(int16_t);
      }

      esp_err_t err = i2s_write(
        I2S_AUDIO_PORT,
        writeBuffer,
        writeSize,
        &bytesWritten,
        portMAX_DELAY
      );

      if (err != ESP_OK) {
        Serial.printf("i2s_write error: %d\n", err);
        offset = recordedSamples;
        break;
      }

      const size_t sampleFramesWritten = DUPLICATE_TO_STEREO_OUTPUT
        ? (bytesWritten / (2 * sizeof(int16_t)))
        : (bytesWritten / sizeof(int16_t));
      offset += sampleFramesWritten;
    }
  }

  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
  isPlaying = false;
  Serial.println("Playback finished.");
}

void captureAudioChunk() {
  static int32_t micRaw[MIC_DMA_BUFFER_SAMPLES];
  size_t bytesRead = 0;

  esp_err_t err = i2s_read(
    I2S_AUDIO_PORT,
    micRaw,
    sizeof(micRaw),
    &bytesRead,
    I2S_TIMEOUT_TICKS
  );

  if (err != ESP_OK || bytesRead == 0) {
    return;
  }

  const size_t samplesRead = bytesRead / sizeof(int32_t);
  captureChunkCount++;

  if (!printedMicRawPreview || captureChunkCount == 10) {
    const size_t previewCount = min(static_cast<size_t>(8), samplesRead);
    Serial.printf("micRaw preview chunk %u:", static_cast<unsigned>(captureChunkCount));
    for (size_t i = 0; i < previewCount; ++i) {
      Serial.printf(" [%u]=0x%08X(%ld)", static_cast<unsigned>(i), static_cast<uint32_t>(micRaw[i]), static_cast<long>(micRaw[i]));
    }
    Serial.println();
    if (!printedMicRawPreview) {
      printedMicRawPreview = true;
    }
  }

  for (size_t i = 0; i < samplesRead; ++i) {
    if (recordedSamples >= MAX_SAMPLES) {
      Serial.println("Buffer full. Stopping recording.");
      stopRecording();
      startPlayback();
      return;
    }

    // Convert 32-bit INMP441 samples to 16-bit, then high-pass and gate low-level noise.
    const int32_t rawSample = micRaw[i] >> 16;
    const float highpassed = MIC_HIGHPASS_ALPHA * (micPrevHighpassSample + static_cast<float>(rawSample - micPrevInputSample));
    micPrevInputSample = rawSample;
    micPrevHighpassSample = highpassed;

    int32_t sample = static_cast<int32_t>(highpassed);
    if (abs(sample) < MIC_NOISE_GATE_THRESHOLD) {
      sample = 0;
    }

    if (sample > 32767) {
      sample = 32767;
      recordedClippedSamples++;
    }
    if (sample < -32768) {
      sample = -32768;
      recordedClippedSamples++;
    }

    const int16_t sample16 = static_cast<int16_t>(sample);
    if (sample16 < recordedMinSample) recordedMinSample = sample16;
    if (sample16 > recordedMaxSample) recordedMaxSample = sample16;
    recordedAbsSum += static_cast<uint64_t>(abs(sample16));
    recordedSum += sample16;
    recordBuffer[recordedSamples++] = sample16;
  }

}

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(TOUCH_PIN, INPUT);

  if (!PLAY_TEST_TONE_INSTEAD_OF_RECORDING) {
    if (!psramFound()) {
      Serial.println("PSRAM not found. This sketch needs PSRAM for practical recording.");
      while (true) {
        delay(1000);
      }
    }

    recordBuffer = static_cast<int16_t *>(
      heap_caps_malloc(MAX_SAMPLES * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
    );

    if (recordBuffer == nullptr) {
      Serial.println("Failed to allocate recording buffer.");
      while (true) {
        delay(1000);
      }
    }

    installMicI2S();
  } else {
    Serial.println("Test tone mode active: mic and PSRAM setup skipped.");
  }

  lastTouchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  Serial.println();
  Serial.println("Ready.");
  if (PLAY_TEST_TONE_INSTEAD_OF_RECORDING) {
    Serial.println("Touch ON  = play test tone");
    Serial.println("Touch OFF = idle");
  } else {
    Serial.println("Touch ON  = record");
    Serial.println("Touch OFF = playback last recording");
  }
  Serial.printf("Playback volume = %.2f\n", PLAYBACK_VOLUME);
  Serial.printf("Mic playback gain = %.2f\n", MIC_PLAYBACK_GAIN);
  Serial.printf("Playback repeat count = %u\n", PLAYBACK_REPEAT_COUNT);
  Serial.printf("Play test tone instead of recording = %s\n", PLAY_TEST_TONE_INSTEAD_OF_RECORDING ? "ON" : "OFF");
  Serial.printf("Microphone sample rate = %u\n", MIC_SAMPLE_RATE);
  Serial.printf("Speaker sample rate = %u\n", SPEAKER_SAMPLE_RATE);
  Serial.printf("Stereo duplicate output = %s\n", DUPLICATE_TO_STEREO_OUTPUT ? "ON" : "OFF");
  Serial.printf("Shared I2S port = %d\n", I2S_AUDIO_PORT);
  Serial.printf(
    "Active I2S mode at boot = %s\n",
    activeAudioPortMode == AudioPortMode::Mic ? "Mic" : (activeAudioPortMode == AudioPortMode::Speaker ? "Speaker" : "None")
  );
  Serial.println("Note: GPIO48 may light the onboard white/RGB LED on some ESP32-S3 boards.");

  if (PLAY_TEST_TONE_INSTEAD_OF_RECORDING && AUTO_PLAY_TEST_TONE_ON_BOOT) {
    Serial.println("Auto-playing test tone now.");
    playTestToneBlocking();
  }
}

void loop() {
  const bool touchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  if (touchState && !lastTouchState) {
    if (PLAY_TEST_TONE_INSTEAD_OF_RECORDING) {
      startPlayback();
    } else {
      startRecording();
    }
  }

  if (!touchState && lastTouchState && isRecording) {
    stopRecording();
    startPlayback();
  }

  lastTouchState = touchState;

  if (isRecording) {
    captureAudioChunk();
  } else if (isPlaying) {
    playRecordingBlocking();
  } else {
    delay(5);
  }
}
