#include <Wire.h>

const int SDA_PIN = 4;
const int SCL_PIN = 5;

const byte MCP_IO = 0x20;
const byte MCP_READER = 0x21;

// Если используешь второй MCP23017 только для чтения, оставь true.
// Если когда-нибудь захочешь работать одним MCP, можно будет поставить false.
const bool USE_SEPARATE_READER = true;

// MCP23017 registers, BANK = 0
const byte IODIRA = 0x00;
const byte IODIRB = 0x01;
const byte GPPUA  = 0x0C;
const byte GPPUB  = 0x0D;
const byte GPIOA  = 0x12;
const byte GPIOB  = 0x13;

const int PIN_COUNT = 16;

const int SETTLE_MICS = 2;
const int CLOCK_HIGH_MICS = 2;
const int CLOCK_LOW_MICS = 2;

char frame[PIN_COUNT];
int framePos = 0;

void writeReg(byte dev, byte reg, byte value) {
  Wire.beginTransmission(dev);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission();
}

byte readReg(byte dev, byte reg) {
  Wire.beginTransmission(dev);
  Wire.write(reg);
  Wire.endTransmission(false);

  Wire.requestFrom(dev, (byte)1);

  if (Wire.available()) {
    return Wire.read();
  }

  return 0;
}

void writeGPIO16(byte dev, uint16_t value) {
  writeReg(dev, GPIOA, value & 0xFF);
  writeReg(dev, GPIOB, (value >> 8) & 0xFF);
}

uint16_t readGPIO16(byte dev) {
  byte a = readReg(dev, GPIOA);
  byte b = readReg(dev, GPIOB);

  return ((uint16_t)b << 8) | a;
}

void writeIODIR16(byte dev, uint16_t iodir) {
  writeReg(dev, IODIRA, iodir & 0xFF);
  writeReg(dev, IODIRB, (iodir >> 8) & 0xFF);
}

void writeGPPU16(byte dev, uint16_t pullups) {
  writeReg(dev, GPPUA, pullups & 0xFF);
  writeReg(dev, GPPUB, (pullups >> 8) & 0xFF);
}

void setAllHiZ() {
  // IODIR bit = 1 -> input / Hi-Z
  writeIODIR16(MCP_IO, 0xFFFF);

  // pull-ups off
  writeGPPU16(MCP_IO, 0x0000);

  // preload output latch = 0
  writeGPIO16(MCP_IO, 0x0000);
}

void configureHardware() {
  setAllHiZ();

  if (USE_SEPARATE_READER) {
    // Reader MCP: all pins are inputs, no pull-ups
    writeIODIR16(MCP_READER, 0xFFFF);
    writeGPPU16(MCP_READER, 0x0000);
  }
}

bool isCommandByte(char c) {
  return c == 'z' ||
         c == '0' ||
         c == '1' ||
         c == 'v' ||
         c == 'g' ||
         c == 'c';
}

void printBitsPinOrder(uint16_t value) {
  // Печатаем в порядке pin 1 -> pin 16,
  // то есть bit 0 -> bit 15.
  for (int bit = 0; bit < 16; bit++) {
    Serial.print((value >> bit) & 1);
  }
}

uint16_t readLines() {
  if (USE_SEPARATE_READER) {
    return readGPIO16(MCP_READER);
  }

  return readGPIO16(MCP_IO);
}

void resetFrame() {
  framePos = 0;

  for (int i = 0; i < PIN_COUNT; i++) {
    frame[i] = 'z';
  }

  setAllHiZ();
}

void processFrame() {
  uint16_t outputMask = 0x0000;
  uint16_t outputValue = 0x0000;
  uint16_t clockMask = 0x0000;

  for (int pin = 0; pin < PIN_COUNT; pin++) {
    char cmd = frame[pin];
    uint16_t bit = (uint16_t)1 << pin;

    if (cmd == 'z') {
      // Hi-Z/input, no pull-up.
      // Ничего не добавляем в outputMask.
    } else if (cmd == '0') {
      // Обычный логический LOW.
      outputMask |= bit;
      // outputValue bit remains 0.
    } else if (cmd == '1' || cmd == 'v') {
      // HIGH / VDD-like output.
      outputMask |= bit;
      outputValue |= bit;
    } else if (cmd == 'g') {
      // ВАЖНО:
      // У MCP23017 нет INPUT_PULLDOWN.
      // Поэтому "g" реализован как OUTPUT LOW.
      // Электрически это то же самое, что '0',
      // но в протоколе можно различать смысл.
      outputMask |= bit;
      // outputValue bit remains 0.
    } else if (cmd == 'c') {
      // Clock pin:
      // сначала держим LOW,
      // после назначения остальных пинов дадим импульс HIGH -> LOW.
      outputMask |= bit;
      clockMask |= bit;
      // outputValue bit remains 0 initially.
    }
  }

  // IODIR: 1 = input/Hi-Z, 0 = output
  uint16_t iodir = ~outputMask;

  // Сначала записываем значения в output latch,
  // потом включаем нужные пины как OUTPUT.
  // Это уменьшает шанс случайных коротких глитчей.
  writeGPIO16(MCP_IO, outputValue);
  writeIODIR16(MCP_IO, iodir);

  delayMicroseconds(SETTLE_MICS);

  if (clockMask != 0) {
    // Rising edge: clock LOW -> HIGH.
    writeGPIO16(MCP_IO, outputValue | clockMask);
    delay(CLOCK_HIGH_MICS);

    // Falling back LOW.
    writeGPIO16(MCP_IO, outputValue);
    delay(CLOCK_LOW_MICS);
  }

  delayMicroseconds(SETTLE_MICS);

  uint16_t readValue = readLines();

  printBitsPinOrder(readValue);

  framePos = 0;
}

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(100);
  delay(1500);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);

  configureHardware();

  resetFrame();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();

    // Удобно игнорировать переносы строк и пробелы.
    if (c == '\n' || c == '\r' || c == ' ' || c == '\t') {
      continue;
    }

    if (c == 'r') {
      resetFrame();
      continue;
    }

    if (!isCommandByte(c)) {
      continue;
    }

    frame[framePos] = c;
    framePos++;

    if (framePos == PIN_COUNT) {
      processFrame();
    }
  }
}