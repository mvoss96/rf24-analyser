#include "RadioController.h"

// RX-ready interrupt flag. The IRQ pin is masked to fire on received data only
// (see reconfigure()), so any edge means "a frame is waiting".
static volatile bool s_rxFlag = false;
static void onRadioIrq() { s_rxFlag = true; }

RadioController::RadioController() {}

void RadioController::led(uint8_t pin, bool on) {
  if (pin != NO_PIN) digitalWrite(pin, on ? LED_ON : LED_OFF);
}

bool RadioController::setHardware(const HwConfig &hw) {
  // Tear down whatever the previous wiring installed.
  if (hw_.irq != NO_PIN) detachInterrupt(digitalPinToInterrupt(hw_.irq));
  listening_ = false;
  configured_ = false;
  hwReady_ = false;

  hw_ = hw;

  // An IRQ pin that cannot raise an interrupt is not fatal - polling works,
  // it just reacts a little later. Say so rather than pretending.
  if (hw_.irq != NO_PIN && digitalPinToInterrupt(hw_.irq) == NOT_AN_INTERRUPT) {
    Serial.print(F("WARN irq pin "));
    Serial.print(hw_.irq);
    Serial.println(F(" is not interrupt-capable, falling back to polling"));
    hw_.irq = NO_PIN;
  }

  if (hw_.ledRx != NO_PIN) pinMode(hw_.ledRx, OUTPUT);
  if (hw_.ledTx != NO_PIN) pinMode(hw_.ledTx, OUTPUT);
  led(hw_.ledRx, false);
  led(hw_.ledTx, false);

  bool chip = radio_.begin(hw_.ce, hw_.csn);
  if (!chip) return false;

  hwReady_ = true;
  if (hw_.irq != NO_PIN) {
    attachInterrupt(digitalPinToInterrupt(hw_.irq), onRadioIrq, FALLING);
  }

  // Single blink confirms the radio answered on the given pins.
  led(hw_.ledRx, true);
  delay(120);
  led(hw_.ledRx, false);
  return true;
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

void RadioController::applyConfig(const RadioConfig &cfg) {
  cfg_ = cfg;
  configured_ = true;
  reconfigure();
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

const __FlashStringHelper *RadioController::stateName() const {
  if (!hwReady_) return F("nohw");
  if (!configured_) return F("unconfigured");
  return listening_ ? F("listening") : F("idle");
}

void RadioController::startListening() {
  radio_.startListening();
  listening_ = true;
}

void RadioController::stopListening() {
  radio_.stopListening();
  listening_ = false;
}

bool RadioController::isRepeat(const uint8_t *buf, uint8_t len) {
  uint32_t now = millis();
  bool same = (len == lastLen_) && (memcmp(buf, lastFrame_, len) == 0);
  bool recent = (uint32_t)(now - lastMs_) < REPEAT_WINDOW_MS;
  memcpy(lastFrame_, buf, len);
  lastLen_ = len;
  lastMs_ = now;
  return same && recent;
}

void RadioController::drainRx() {
  // A full RX FIFO means the host could not keep up; further frames arriving
  // now are dropped by the chip. Surfacing this separates "lost on air" from
  // "lost because we were busy printing".
  if (radio_.rxFifoFull()) Serial.println(F("WARN fifo-full"));

  uint8_t pipe = 0;
  while (radio_.available(&pipe)) {
    uint8_t len = cfg_.dpl ? radio_.getDynamicPayloadSize() : cfg_.plSize;
    if (len == 0 || len > 32) {
      radio_.flush_rx(); // corrupt dynamic length: discard to unstick the FIFO
      break;
    }
    uint8_t buf[32];
    radio_.read(buf, len);

    if (isRepeat(buf, len) && !showRepeats_) continue;

    led(hw_.ledRx, true);
    // Compact hex (no separators) keeps the line short - the serial link is
    // the bottleneck during fast bursts.
    Serial.print(F("RX p"));
    Serial.print(pipe);
    Serial.print(F(" len="));
    Serial.print(len);
    Serial.print(' ');
    for (uint8_t i = 0; i < len; i++) {
      if (buf[i] < 0x10) Serial.print('0');
      Serial.print(buf[i], HEX);
    }
    Serial.println();
    led(hw_.ledRx, false);
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
  led(hw_.ledTx, true);
  // Bounded, deliberately not RF24::write(): if the CE pin is not actually
  // wired to the chip the transmission never starts, TX_DS/MAX_RT never arrive
  // and write() spins forever. With a timeout this becomes a reported failure -
  // and sent=0 doubles as the only practical check that CE is correct, since
  // isChipConnected() exercises SPI only and never touches CE.
  radio_.startFastWrite(data, len, noack); // noack => NO_ACK flag
  bool sent = radio_.txStandBy(TX_TIMEOUT_MS);
  if (!sent) radio_.flush_tx();
  led(hw_.ledTx, false);
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

static void printAddr(const uint8_t *a, uint8_t width) {
  for (uint8_t i = 0; i < width; i++) {
    if (i) Serial.print(':');
    if (a[i] < 0x10) Serial.print('0');
    Serial.print(a[i], HEX);
  }
}

// Prints analog pins as A0..A7 so the output can be pasted back into `hwset`.
static void printPin(uint8_t pin) {
  if (pin == NO_PIN) { Serial.print(F("none")); return; }
#ifdef A0
  if (pin >= A0 && pin <= A0 + 7) {
    Serial.print('A');
    Serial.print((int)(pin - A0));
    return;
  }
#endif
  Serial.print(pin);
}

void RadioController::printWiring() const {
  Serial.print(F("ce="));      printPin(hw_.ce);
  Serial.print(F(" csn="));    printPin(hw_.csn);
  Serial.print(F(" irq="));    printPin(hw_.irq);
  Serial.print(F(" led_rx=")); printPin(hw_.ledRx);
  Serial.print(F(" led_tx=")); printPin(hw_.ledTx);
}

void RadioController::printInfo() {
  Serial.println(F("info:"));
  Serial.print(F("  state="));   Serial.println(stateName());

  if (!hwReady_) {
    Serial.println(F("OK"));
    return;
  }

  Serial.print(F("  chip="));    Serial.println(radio_.isChipConnected() ? F("connected") : F("NOT connected"));
  Serial.print(F("  "));         printWiring();
  Serial.println();
  Serial.print(F("  repeats=")); Serial.println(showRepeats_ ? 1 : 0);

  if (!configured_) {
    Serial.println(F("OK"));
    return;
  }

  Serial.print(F("  channel="));  Serial.println(cfg_.channel);
  Serial.print(F("  rate="));     Serial.print(cfg_.rateKbps); Serial.println(F("kbps"));
  Serial.print(F("  crc="));      Serial.println(cfg_.crcBits);
  Serial.print(F("  aw="));       Serial.println(cfg_.addrWidth);
  // Printed by name so `info` output matches what `listen pa=` accepts.
  Serial.print(F("  pa="));
  switch (cfg_.paLevel) {
    case 0:  Serial.println(F("min"));  break;
    case 1:  Serial.println(F("low"));  break;
    case 2:  Serial.println(F("high")); break;
    default: Serial.println(F("max"));  break;
  }
  Serial.print(F("  ack="));      Serial.println(cfg_.autoAck ? 1 : 0);
  Serial.print(F("  dpl="));      Serial.println(cfg_.dpl ? 1 : 0);
  Serial.print(F("  plsize="));   Serial.println(cfg_.plSize);
  for (uint8_t p = 0; p < 6; p++) {
    if (!cfg_.pipeEn[p]) continue;
    Serial.print(F("  pipe"));
    Serial.print(p);
    Serial.print('=');
    printAddr(cfg_.pipeAddr[p], cfg_.addrWidth);
    Serial.println();
  }
  Serial.println(F("OK"));
}
