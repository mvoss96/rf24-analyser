#include "RadioController.h"

// RX-ready interrupt flag. The dongle's IRQ pin is masked to fire on received
// data only (see reconfigure()), so any edge means "a frame is waiting".
static volatile bool s_rxFlag = false;
static void onRadioIrq() { s_rxFlag = true; }

RadioController::RadioController() : radio_(Pins::RADIO_CE, Pins::RADIO_CSN) {}

bool RadioController::begin() {
  pinMode(Pins::LED_TX, OUTPUT);
  pinMode(Pins::LED_RX, OUTPUT);
  digitalWrite(Pins::LED_TX, Pins::LED_OFF);
  digitalWrite(Pins::LED_RX, Pins::LED_OFF);

  bool chip = radio_.begin();
  reconfigure();

  if (chip) {
    attachInterrupt(digitalPinToInterrupt(Pins::RADIO_IRQ), onRadioIrq, FALLING);
  }

  // Startup blink: 1x = radio present, 2x = radio not detected.
  for (uint8_t i = 0; i < (chip ? 1 : 2); i++) {
    digitalWrite(Pins::LED_RX, Pins::LED_ON);
    delay(120);
    digitalWrite(Pins::LED_RX, Pins::LED_OFF);
    delay(120);
  }
  return chip;
}

static rf24_datarate_e rateEnum(uint16_t kbps) {
  switch (kbps) {
    case 1000: return RF24_1MBPS;
    case 2000: return RF24_2MBPS;
    default:   return RF24_250KBPS;
  }
}

static rf24_crclength_e crcEnum(uint8_t bits) {
  switch (bits) {
    case 0:  return RF24_CRC_DISABLED;
    case 8:  return RF24_CRC_8;
    default: return RF24_CRC_16;
  }
}

void RadioController::reconfigure() {
  bool wasListening = listening_;
  radio_.stopListening();

  radio_.setChannel(cfg_.channel);
  radio_.setDataRate(rateEnum(cfg_.rateKbps));
  radio_.setCRCLength(crcEnum(cfg_.crcBits));
  radio_.setAddressWidth(cfg_.addrWidth);
  radio_.setPALevel((rf24_pa_dbm_e)cfg_.paLevel);
  radio_.setAutoAck(cfg_.autoAck);

  if (cfg_.dpl) {
    radio_.enableDynamicPayloads();
  } else {
    radio_.disableDynamicPayloads();
    radio_.setPayloadSize(cfg_.plSize);
  }
  // Allow per-packet NO_ACK on transmit (tx ... noack).
  radio_.enableDynamicAck();

  for (uint8_t p = 0; p < 6; p++) {
    if (cfg_.pipeEn[p]) radio_.openReadingPipe(p, cfg_.pipeAddr[p]);
    else                radio_.closeReadingPipe(p);
  }

  // Deliver only RX-ready interrupts on the IRQ pin.
  radio_.maskIRQ(true, true, false);

  if (wasListening) {
    radio_.startListening();
    listening_ = true;
  }
}

void RadioController::startListening() {
  radio_.startListening();
  listening_ = true;
}

void RadioController::stopListening() {
  radio_.stopListening();
  listening_ = false;
}

void RadioController::drainRx() {
  uint8_t pipe = 0;
  while (radio_.available(&pipe)) {
    uint8_t len = cfg_.dpl ? radio_.getDynamicPayloadSize() : cfg_.plSize;
    if (len == 0 || len > 32) {
      radio_.flush_rx(); // corrupt dynamic length: discard to unstick the FIFO
      break;
    }
    uint8_t buf[32];
    radio_.read(buf, len);

    digitalWrite(Pins::LED_RX, Pins::LED_ON);
    Serial.print(F("RX p"));
    Serial.print(pipe);
    Serial.print(F(" len="));
    Serial.print(len);
    for (uint8_t i = 0; i < len; i++) {
      Serial.print(' ');
      if (buf[i] < 0x10) Serial.print('0');
      Serial.print(buf[i], HEX);
    }
    Serial.println();
    digitalWrite(Pins::LED_RX, Pins::LED_OFF);
  }
  s_rxFlag = false;
}

void RadioController::poll() {
  if (listening_ && (s_rxFlag || radio_.available())) {
    drainRx();
  }
}

bool RadioController::transmit(const uint8_t *addr, const uint8_t *data,
                              uint8_t len, bool noack) {
  radio_.stopListening();
  radio_.openWritingPipe(addr);
  digitalWrite(Pins::LED_TX, Pins::LED_ON);
  bool sent = radio_.write(data, len, noack); // 3rd arg true => NO_ACK
  digitalWrite(Pins::LED_TX, Pins::LED_OFF);
  // openWritingPipe clobbers pipe 0; restore reading pipes and listen state.
  reconfigure();
  return sent;
}

void RadioController::scan(uint16_t passes) {
  uint8_t counts[126] = {0};
  bool wasListening = listening_;
  radio_.stopListening();

  for (uint16_t pass = 0; pass < passes; pass++) {
    for (uint8_t ch = 0; ch <= 125; ch++) {
      radio_.setChannel(ch);
      radio_.startListening();
      delayMicroseconds(130);
      radio_.stopListening();
      if (radio_.testRPD() && counts[ch] < 255) counts[ch]++;
    }
  }

  Serial.print(F("SCAN passes="));
  Serial.println(passes);
  for (uint8_t ch = 0; ch <= 125; ch++) {
    if (counts[ch]) {
      Serial.print(F("SCAN ch="));
      Serial.print(ch);
      Serial.print(F(" hits="));
      Serial.println(counts[ch]);
    }
  }

  radio_.setChannel(cfg_.channel);
  if (wasListening) {
    radio_.startListening();
    listening_ = true;
  }
  Serial.println(F("OK scan done"));
}

static void printAddr(uint8_t *a, uint8_t width) {
  for (uint8_t i = 0; i < width; i++) {
    if (i) Serial.print(':');
    if (a[i] < 0x10) Serial.print('0');
    Serial.print(a[i], HEX);
  }
}

void RadioController::printInfo() {
  Serial.println(F("info:"));
  Serial.print(F("  chip="));      Serial.println(radio_.isChipConnected() ? F("connected") : F("NOT connected"));
  Serial.print(F("  listening=")); Serial.println(listening_ ? 1 : 0);
  Serial.print(F("  channel="));   Serial.println(cfg_.channel);
  Serial.print(F("  rate="));      Serial.print(cfg_.rateKbps); Serial.println(F("kbps"));
  Serial.print(F("  crc="));       Serial.println(cfg_.crcBits);
  Serial.print(F("  aw="));        Serial.println(cfg_.addrWidth);
  Serial.print(F("  pa="));        Serial.println(cfg_.paLevel); // 0=min 1=low 2=high 3=max
  Serial.print(F("  autoack="));   Serial.println(cfg_.autoAck ? 1 : 0);
  Serial.print(F("  dpl="));       Serial.println(cfg_.dpl ? 1 : 0);
  Serial.print(F("  plsize="));    Serial.println(cfg_.plSize);
  for (uint8_t p = 0; p < 6; p++) {
    Serial.print(F("  pipe"));
    Serial.print(p);
    Serial.print('=');
    if (cfg_.pipeEn[p]) printAddr(cfg_.pipeAddr[p], cfg_.addrWidth);
    else Serial.print(F("off"));
    Serial.println();
  }
  Serial.println(F("OK"));
}
