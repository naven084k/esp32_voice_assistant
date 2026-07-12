// ESP32-S3 touch-controlled "hello world" speaker test
//
// Purpose:
// - Produce a voice-like phrase without mic or PSRAM
// - Use this to judge hiss / distortion on the speaker path
// - Touch ON  -> play synthetic "hello world"
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

static constexpr gpio_num_t SPK_BCLK_PIN = GPIO_NUM_48;
static constexpr gpio_num_t SPK_LRC_PIN  = GPIO_NUM_21;
static constexpr gpio_num_t SPK_DIN_PIN  = GPIO_NUM_47;
static constexpr int TOUCH_PIN = 14;
static constexpr int TOUCH_ACTIVE_STATE = HIGH;

static constexpr i2s_port_t I2S_AUDIO_PORT = I2S_NUM_0;
static constexpr uint32_t SAMPLE_RATE = 44100;
static constexpr size_t CHUNK_FRAMES = 128;
static constexpr float MASTER_VOLUME = 0.14f;

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

void writeFrames(const int16_t *frames, size_t frameCount) {
  size_t bytesWritten = 0;
  i2s_write(I2S_AUDIO_PORT, frames, frameCount * 2 * sizeof(int16_t), &bytesWritten, portMAX_DELAY);
}

void renderTone(float freq, uint32_t durationMs, float volume, float phaseOffset = 0.0f) {
  int16_t buffer[CHUNK_FRAMES * 2];
  const float phaseStep = 2.0f * PI * freq / SAMPLE_RATE;
  float phase = phaseOffset;
  const size_t totalFrames = (static_cast<size_t>(SAMPLE_RATE) * durationMs) / 1000;
  size_t rendered = 0;

  while (rendered < totalFrames) {
    const size_t frames = min(CHUNK_FRAMES, totalFrames - rendered);
    for (size_t i = 0; i < frames; ++i) {
      const int16_t s = static_cast<int16_t>(sinf(phase) * 32767.0f * volume);
      buffer[i * 2] = s;
      buffer[i * 2 + 1] = s;
      phase += phaseStep;
      if (phase >= 2.0f * PI) phase -= 2.0f * PI;
    }
    writeFrames(buffer, frames);
    rendered += frames;
  }
}

void renderNoise(uint32_t durationMs, float volume) {
  int16_t buffer[CHUNK_FRAMES * 2];
  uint32_t lfsr = 0xACE1u;
  const size_t totalFrames = (static_cast<size_t>(SAMPLE_RATE) * durationMs) / 1000;
  size_t rendered = 0;

  while (rendered < totalFrames) {
    const size_t frames = min(CHUNK_FRAMES, totalFrames - rendered);
    for (size_t i = 0; i < frames; ++i) {
      lfsr = (lfsr >> 1) ^ (-(int32_t)(lfsr & 1u) & 0xB400u);
      const float noise = ((int32_t)(lfsr & 0xFFFF) - 32768) / 32768.0f;
      const int16_t s = static_cast<int16_t>(noise * 32767.0f * volume);
      buffer[i * 2] = s;
      buffer[i * 2 + 1] = s;
    }
    writeFrames(buffer, frames);
    rendered += frames;
  }
}

void renderVoiced(uint32_t durationMs, float f0, float f1, float f2, float f3, float volume, float noiseMix = 0.0f) {
  int16_t buffer[CHUNK_FRAMES * 2];
  float p0 = 0.0f, p1 = 0.0f, p2 = 0.0f, p3 = 0.0f;
  const float s0 = 2.0f * PI * f0 / SAMPLE_RATE;
  const float s1 = 2.0f * PI * f1 / SAMPLE_RATE;
  const float s2 = 2.0f * PI * f2 / SAMPLE_RATE;
  const float s3 = 2.0f * PI * f3 / SAMPLE_RATE;
  uint32_t lfsr = 0xBEEF1234u;
  const size_t totalFrames = (static_cast<size_t>(SAMPLE_RATE) * durationMs) / 1000;
  size_t rendered = 0;

  while (rendered < totalFrames) {
    const size_t frames = min(CHUNK_FRAMES, totalFrames - rendered);
    for (size_t i = 0; i < frames; ++i) {
      const float t = (totalFrames == 0) ? 0.0f : static_cast<float>(rendered + i) / static_cast<float>(totalFrames);
      const float env = t < 0.08f ? (t / 0.08f) : (t > 0.92f ? ((1.0f - t) / 0.08f) : 1.0f);

      lfsr = (lfsr >> 1) ^ (-(int32_t)(lfsr & 1u) & 0xB400u);
      const float noise = ((int32_t)(lfsr & 0xFFFF) - 32768) / 32768.0f;

      float sample = 0.65f * sinf(p0) + 0.25f * sinf(p1) + 0.15f * sinf(p2) + 0.10f * sinf(p3);
      sample = sample + noise * noiseMix;
      sample *= env * volume;

      buffer[i * 2] = static_cast<int16_t>(constrain(sample * 32767.0f, -32768.0f, 32767.0f));
      buffer[i * 2 + 1] = buffer[i * 2];

      p0 += s0; if (p0 >= 2.0f * PI) p0 -= 2.0f * PI;
      p1 += s1; if (p1 >= 2.0f * PI) p1 -= 2.0f * PI;
      p2 += s2; if (p2 >= 2.0f * PI) p2 -= 2.0f * PI;
      p3 += s3; if (p3 >= 2.0f * PI) p3 -= 2.0f * PI;
    }
    writeFrames(buffer, frames);
    rendered += frames;
  }
}

void sayHelloWorld() {
  Serial.println("Playing synthetic hello world...");

  renderNoise(70, MASTER_VOLUME * 0.55f);                       // H
  renderVoiced(180, 130.0f, 400.0f, 2000.0f, 3000.0f, MASTER_VOLUME); // EH
  renderVoiced(170, 120.0f, 350.0f, 2400.0f, 3200.0f, MASTER_VOLUME); // L
  renderVoiced(240, 110.0f, 500.0f, 1000.0f, 2600.0f, MASTER_VOLUME); // OH
  delay(40);
  renderVoiced(120, 120.0f, 300.0f, 700.0f, 2400.0f, MASTER_VOLUME);  // W
  renderVoiced(220, 115.0f, 500.0f, 1400.0f, 2500.0f, MASTER_VOLUME); // ER
  renderVoiced(160, 120.0f, 350.0f, 2400.0f, 3200.0f, MASTER_VOLUME); // L
  renderNoise(50, MASTER_VOLUME * 0.40f);                       // D burst

  Serial.println("Phrase finished.");
}

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(TOUCH_PIN, INPUT);
  installSpeakerI2S();

  lastTouchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  Serial.println();
  Serial.println("Hello-world speaker test ready.");
  Serial.println("Touch ON  = play phrase once");
  Serial.println("Touch OFF = idle");
  Serial.printf("Sample rate = %u Hz\n", SAMPLE_RATE);
  Serial.printf("Master volume = %.2f\n", MASTER_VOLUME);
}

void loop() {
  const bool touchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  if (touchState && !lastTouchState) {
    isPlaying = true;
    sayHelloWorld();
    isPlaying = false;
  }

  lastTouchState = touchState;

  if (!isPlaying) {
    delay(5);
  }
}
