// ESP32-S3 speaker-only touch test
//
// Purpose:
// - Isolate MAX98357A + speaker behavior
// - No microphone, no recording, no PSRAM
// - Touch ON  -> play a sine tone
// - Touch OFF -> stop audio
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
static constexpr float TONE_FREQ_HZ = 440.0f;
static constexpr float VOLUME = 0.18f;
static constexpr size_t DMA_FRAMES = 128;

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

void stopAudio() {
  i2s_zero_dma_buffer(I2S_AUDIO_PORT);
  isPlaying = false;
}

void playToneStep() {
  static float phase = 0.0f;
  static int16_t buffer[DMA_FRAMES * 2];
  const float phaseStep = 2.0f * PI * TONE_FREQ_HZ / SAMPLE_RATE;

  for (size_t i = 0; i < DMA_FRAMES; ++i) {
    const int16_t sample = static_cast<int16_t>(sinf(phase) * 32767.0f * VOLUME);
    buffer[i * 2] = sample;
    buffer[i * 2 + 1] = sample;
    phase += phaseStep;
    if (phase >= 2.0f * PI) {
      phase -= 2.0f * PI;
    }
  }

  size_t bytesWritten = 0;
  i2s_write(I2S_AUDIO_PORT, buffer, sizeof(buffer), &bytesWritten, portMAX_DELAY);
}

void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(TOUCH_PIN, INPUT);
  installSpeakerI2S();

  lastTouchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  Serial.println();
  Serial.println("Speaker-only touch test ready.");
  Serial.println("Touch ON  = play 440 Hz tone");
  Serial.println("Touch OFF = stop");
  Serial.printf("Sample rate = %u Hz\n", SAMPLE_RATE);
  Serial.printf("Volume = %.2f\n", VOLUME);
  Serial.println("If you still hear noise with no tone, the issue is speaker/amp wiring, power, or amp gain.");
}

void loop() {
  const bool touchState = (digitalRead(TOUCH_PIN) == TOUCH_ACTIVE_STATE);

  if (touchState && !lastTouchState) {
    Serial.println("Touch ON -> tone active");
    isPlaying = true;
  }

  if (!touchState && lastTouchState) {
    Serial.println("Touch OFF -> tone stopped");
    stopAudio();
  }

  lastTouchState = touchState;

  if (isPlaying) {
    playToneStep();
  } else {
    delay(5);
  }
}
