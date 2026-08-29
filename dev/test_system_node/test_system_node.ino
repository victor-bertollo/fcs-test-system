
#include <Adafruit_MCP23X17.h>
#include <Wire.h>

#define SDA_PIN 4
#define SCL_PIN 5
#define INPUT_ADDR 0x20
#define OUTPUT_ADDR 0x21
#define PINS_CNT 16
#define CLK_PIN 10

Adafruit_MCP23X17 input_mcp;
Adafruit_MCP23X17 output_mcp;
uint32_t input_bytes = 0x0;
uint8_t input_cnt = 0;
uint16_t output_bytes = 0x0;

void setup() {
  Wire.begin(SDA_PIN, SCL_PIN);
  Serial.begin(115200);
  while (!Serial);
  delay(1000);

  if (!input_mcp.begin_I2C(INPUT_ADDR)) {
    Serial.println("Error while beginning input GPIOs.");
    while (1);
  }

  if (!output_mcp.begin_I2C(OUTPUT_ADDR)) {
    Serial.println("Error while beginning output GPIOs.");
    while (1);
  }

  for (uint8_t i = 0; i < PINS_CNT; i++) {
    output_mcp.pinMode(i, INPUT);
  }

  pinMode(CLK_PIN, OUTPUT);
  digitalWrite(CLK_PIN, LOW);
}

void loop() {
  if (Serial.available() > 3) {
    for (uint8_t i = 0; i < 4; i++) {
      input_cnt++;
      input_bytes <<= 8;
      input_bytes |= Serial.read();
    }
  }

  for (uint8_t i = 0; i < PINS_CNT; i++) {
    uint8_t x_i = (input_bytes >> (i * 2 + 1)) & 1;
    uint8_t y_i = (input_bytes >> (i * 2)) & 1;

    if (x_i == 1) {
      input_mcp.pinMode(i, INPUT);
    } else {
      input_mcp.pinMode(i, OUTPUT);
      input_mcp.digitalWrite(i, y_i);
    }
  }

  input_bytes = 0x0;
  input_cnt = 0;
  output_bytes = 0x0;
  digitalWrite(CLK_PIN, HIGH);
  digitalWrite(CLK_PIN, LOW);

  for (int8_t i = 0; i < PINS_CNT; i++) {
    output_bytes <<= 1;
    output_bytes |= output_mcp.digitalRead(i) & 1;
  }

  Serial.write((output_bytes >> 8) & 0xFF);
  Serial.write(output_bytes & 0xFF);
  Serial.flush();
}
