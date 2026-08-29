#include <Wire.h>

const int SDA_PIN = 4;
const int SCL_PIN = 5;

const byte MCP_ADDRS[2] = {0x20, 0x21};

// MCP23017 registers, BANK = 0
const byte IODIRA = 0x00;
const byte IODIRB = 0x01;
const byte GPPUA  = 0x0C;
const byte GPPUB  = 0x0D;
const byte GPIOA  = 0x12;
const byte GPIOB  = 0x13;

const int MCP_COUNT = 2;
const int PINS_PER_MCP = 16;
const int CONFIG_SIZE = MCP_COUNT * PINS_PER_MCP;  // 32 bytes

const int SETTLE_US = 1000;
const int CLOCK_HIGH_MS = 2;
const int CLOCK_LOW_MS = 2;

// IMPORTANT:
// i = INPUT of the tested board, therefore MCP must DRIVE this line (MCP OUTPUT)
// o = OUTPUT of the tested board, therefore MCP must READ this line (MCP INPUT)
// g = MCP OUTPUT LOW
// v = MCP OUTPUT HIGH
// c = MCP clock output, pulsed LOW -> HIGH -> LOW for every test vector
// z = MCP INPUT / Hi-Z, ignored

struct PinRef {
  byte mcp;  // 0 -> 0x20, 1 -> 0x21
  byte bit;  // 0..15
};

char configFrame[CONFIG_SIZE];
int configPos = 0;
bool configured = false;

PinRef inputPins[CONFIG_SIZE];
PinRef outputPins[CONFIG_SIZE];

int inputCount = 0;
int outputCount = 0;

char inputValues[CONFIG_SIZE];
int inputPos = 0;

// Current MCP output latches.
// Needed so that changing i-pins does not disturb g/v pins.
uint16_t gpioLatch[MCP_COUNT] = {0x0000, 0x0000};
uint16_t clockMask[MCP_COUNT] = {0x0000, 0x0000};

void writeReg(byte dev, byte reg, byte value) {
  Wire.beginTransmission(dev);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission();
}

bool readReg(byte dev, byte reg, byte &value) {
  Wire.beginTransmission(dev);
  Wire.write(reg);
  byte status = Wire.endTransmission(false);

  if (status != 0) {
    return false;
  }

  byte received = Wire.requestFrom(dev, (byte)1);

  if (received != 1 || !Wire.available()) {
    return false;
  }

  value = Wire.read();
  return true;
}

void writeGPIO16(byte dev, uint16_t value) {
  writeReg(dev, GPIOA, value & 0xFF);
  writeReg(dev, GPIOB, (value >> 8) & 0xFF);
}

void writeIODIR16(byte dev, uint16_t value) {
  writeReg(dev, IODIRA, value & 0xFF);
  writeReg(dev, IODIRB, (value >> 8) & 0xFF);
}

void writeGPPU16(byte dev, uint16_t value) {
  writeReg(dev, GPPUA, value & 0xFF);
  writeReg(dev, GPPUB, (value >> 8) & 0xFF);
}

void setAllHiZ() {
  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    // No pull-ups.
    writeGPPU16(MCP_ADDRS[mcp], 0x0000);

    // All pins INPUT / Hi-Z.
    writeIODIR16(MCP_ADDRS[mcp], 0xFFFF);

    // Preload output latch LOW for safety.
    gpioLatch[mcp] = 0x0000;
    writeGPIO16(MCP_ADDRS[mcp], gpioLatch[mcp]);
  }
}

bool isConfigByte(char c) {
  return c == 'i' ||
         c == 'o' ||
         c == 'c' ||
         c == 'g' ||
         c == 'v' ||
         c == 'z';
}

void resetProtocol() {
  configPos = 0;
  inputPos = 0;
  inputCount = 0;
  outputCount = 0;
  configured = false;

  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    clockMask[mcp] = 0x0000;
  }

  for (int i = 0; i < CONFIG_SIZE; ++i) {
    configFrame[i] = 'z';
    inputValues[i] = '0';
  }

  setAllHiZ();
}

void applyConfiguration() {
  uint16_t iodir[MCP_COUNT] = {0xFFFF, 0xFFFF};

  inputCount = 0;
  outputCount = 0;
  inputPos = 0;

  // Start from a known state.
  gpioLatch[0] = 0x0000;
  gpioLatch[1] = 0x0000;
  clockMask[0] = 0x0000;
  clockMask[1] = 0x0000;

  for (int pos = 0; pos < CONFIG_SIZE; ++pos) {
    int mcp = pos / PINS_PER_MCP;
    int bitIndex = pos % PINS_PER_MCP;

    uint16_t bit = (uint16_t)1 << bitIndex;
    char mode = configFrame[pos];

    if (mode == 'i') {
      // Input of tested board:
      // remember it as a value that will arrive from the computer,
      // and make MCP drive it.
      inputPins[inputCount].mcp = mcp;
      inputPins[inputCount].bit = bitIndex;
      ++inputCount;

      iodir[mcp] &= ~bit;  // MCP OUTPUT
      // Initial value LOW.
      gpioLatch[mcp] &= ~bit;

    } else if (mode == 'o') {
      // Output of tested board:
      // MCP must only observe it.
      outputPins[outputCount].mcp = mcp;
      outputPins[outputCount].bit = bitIndex;
      ++outputCount;

      iodir[mcp] |= bit;   // MCP INPUT / Hi-Z

    } else if (mode == 'c') {
      // Clock. It stays LOW between test vectors and is not included
      // in the input values received from the computer.
      iodir[mcp] &= ~bit;  // MCP OUTPUT
      gpioLatch[mcp] &= ~bit;
      clockMask[mcp] |= bit;

    } else if (mode == 'g') {
      // Ground.
      iodir[mcp] &= ~bit;  // MCP OUTPUT
      gpioLatch[mcp] &= ~bit;

    } else if (mode == 'v') {
      // VDD / HIGH.
      iodir[mcp] &= ~bit;  // MCP OUTPUT
      gpioLatch[mcp] |= bit;

    } else if (mode == 'z') {
      // Disconnected / ignored: INPUT / Hi-Z.
      iodir[mcp] |= bit;   // MCP INPUT / Hi-Z
    }
  }

  // Disable pull-ups everywhere.
  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    writeGPPU16(MCP_ADDRS[mcp], 0x0000);
  }

  // First preload the output latches.
  // Then enable the required pins as outputs.
  // This reduces unwanted glitches.
  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    writeGPIO16(MCP_ADDRS[mcp], gpioLatch[mcp]);
  }

  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    writeIODIR16(MCP_ADDRS[mcp], iodir[mcp]);
  }

  configured = true;
}

void pulseClocks() {
  bool hasClock = false;

  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    if (clockMask[mcp] != 0) {
      hasClock = true;
    }

    writeGPIO16(
      MCP_ADDRS[mcp],
      gpioLatch[mcp] | clockMask[mcp]
    );
  }

  if (!hasClock) {
    return;
  }

  delay(CLOCK_HIGH_MS);

  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    writeGPIO16(MCP_ADDRS[mcp], gpioLatch[mcp]);
  }

  delay(CLOCK_LOW_MS);
}

void applyInputValues() {
  for (int i = 0; i < inputCount; ++i) {
    byte mcp = inputPins[i].mcp;
    byte bitIndex = inputPins[i].bit;
    uint16_t bit = (uint16_t)1 << bitIndex;

    if (inputValues[i] == '1') {
      gpioLatch[mcp] |= bit;
    } else {
      gpioLatch[mcp] &= ~bit;
    }
  }

  // Apply both MCP latches.
  // g/v stay unchanged because their bits are stored in gpioLatch.
  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    writeGPIO16(MCP_ADDRS[mcp], gpioLatch[mcp]);
  }
}

void readAndSendOutputs() {
  byte values[MCP_COUNT][2] = {{0, 0}, {0, 0}};
  bool bankNeeded[MCP_COUNT][2] = {{false, false}, {false, false}};
  bool bankOk[MCP_COUNT][2] = {{true, true}, {true, true}};

  // Read only MCP banks that contain configured output pins.
  for (int i = 0; i < outputCount; ++i) {
    byte mcp = outputPins[i].mcp;
    byte bank = outputPins[i].bit < 8 ? 0 : 1;
    bankNeeded[mcp][bank] = true;
  }

  for (int mcp = 0; mcp < MCP_COUNT; ++mcp) {
    if (bankNeeded[mcp][0]) {
      bankOk[mcp][0] = readReg(MCP_ADDRS[mcp], GPIOA, values[mcp][0]);
    }
    if (bankNeeded[mcp][1]) {
      bankOk[mcp][1] = readReg(MCP_ADDRS[mcp], GPIOB, values[mcp][1]);
    }
  }

  // Send exactly t bytes, where t = outputCount.
  // Order is the same as in the 32-byte config:
  // 0x20 pin1..16, then 0x21 pin1..16.
  // Only pins marked 'o' are included.
  for (int i = 0; i < outputCount; ++i) {
    byte mcp = outputPins[i].mcp;
    byte bitIndex = outputPins[i].bit;
    byte bank = bitIndex < 8 ? 0 : 1;
    byte bankBit = bitIndex % 8;

    if (!bankOk[mcp][bank]) {
      // Keep the response length fixed, but do not turn an I2C failure
      // into a valid logical zero.
      Serial.write('E');
      continue;
    }

    char out = ((values[mcp][bank] >> bankBit) & 1) ? '1' : '0';
    Serial.write(out);
  }
}

void processTestVector() {
  applyInputValues();

  delayMicroseconds(SETTLE_US);

  pulseClocks();

  readAndSendOutputs();

  // Ready for the next test vector with the same configuration.
  inputPos = 0;
}

void setup() {
  Serial.begin(115200);
  delay(1500);

  Wire.begin(SDA_PIN, SCL_PIN);
  Wire.setClock(100000);

  resetProtocol();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();

    // Optional protocol reset.
    // After 'r', ESP expects a new 32-byte configuration.
    if (c == 'r') {
      resetProtocol();
      continue;
    }

    // Ignore whitespace so manual testing is easier.
    if (c == '\n' || c == '\r' || c == ' ' || c == '\t') {
      continue;
    }

    if (!configured) {
      // Phase 1:
      // receive exactly 32 configuration bytes.
      if (!isConfigByte(c)) {
        continue;
      }

      configFrame[configPos] = c;
      ++configPos;

      if (configPos == CONFIG_SIZE) {
        applyConfiguration();
        configPos = 0;
      }

      continue;
    }

    // Phase 2:
    // receive exactly k input-value bytes ('0' or '1'),
    // where k is the number of 'i' pins in the configuration.
    if (c != '0' && c != '1') {
      continue;
    }

    if (inputCount == 0) {
      // No dynamic inputs in this configuration.
      // A value byte acts as a trigger to sample outputs.
      processTestVector();
      continue;
    }

    inputValues[inputPos] = c;
    ++inputPos;

    if (inputPos == inputCount) {
      processTestVector();
    }
  }
}
