#include "RadioController.h"
#include "Protocol.h"

// RX-ready interrupt flag. The IRQ pin is masked to fire on received data only
// (see reconfigure()), so any edge means "a frame is waiting".
static volatile bool s_rxFlag = false;
static void onRadioIrq() { s_rxFlag = true; }

static inline uint8_t crc8(const uint8_t *d, uint8_t n) { return nrf24_crc8(d, n); }

// --- Register-level RX -------------------------------------------------------
//
// These dongles hand out every payload shorter than 32 bytes twice: the second
// copy carries an earlier payload, and when the two differ in length it comes
// out read with the wrong width. Measured, not guessed - and not the library
// either, which an earlier version of this comment claimed:
//
//   * Two dongles hearing the same single click report *different* stale
//     payloads for it, so it is made up locally, not on the air.
//   * It survives reading the full 32-byte slot instead of the reported width,
//     never asking for the width at all, asking for it after the read, setting
//     RX_PW_Pn to the true length, and a datasheet-legal dynamic-payload
//     configuration (auto-ack on the open pipe). None of those is the cause.
//   * A payload that fills the 32-byte slot comes out exactly once. One that
//     does not leaves the FIFO a payload out of step, and the next arrival is
//     then announced twice.
//   * Stale bytes live in the chip's FIFO RAM and outlive an ATmega reset, a
//     firmware upload and any number of FLUSH_RX calls - flushing resets
//     pointers, it does not clear RAM. A frame from ten minutes ago can
//     therefore surface at any time.
//
// Talking to the registers directly is what makes the one measure that does
// work - a flush after every single payload - expressible at all.
namespace {
constexpr uint8_t CMD_R_REGISTER = 0x00;
constexpr uint8_t CMD_R_RX_PL_WID = 0x60;
constexpr uint8_t CMD_R_RX_PAYLOAD = 0x61;
constexpr uint8_t CMD_FLUSH_RX = 0xE2;
constexpr uint8_t REG_STATUS = 0x07;
constexpr uint8_t REG_FIFO_STATUS = 0x17;
constexpr uint8_t FIFO_RX_EMPTY = 0x01;
constexpr uint8_t STATUS_RX_DR = 0x40;

// The dongle's SPI is shared with nothing else, so one setting object is fine.
//
// 8 MHz is the ATmega's ceiling as a master (F_CPU/2) and well inside the
// nRF24L01+'s 10. It halves the time a payload spends on the bus: the drain
// loop's own measurement, `us_in` in `info`, fell from 190 to 143 us a frame,
// and 360 frames of mixed lengths came through with nothing corrupted,
// duplicated or invented.
//
// Worth knowing what that did and did not buy. It changed a transfer's speed
// by nothing at all - 1.28 ms a frame before and after - because the sending
// path is bound by the serial line and the air, not by SPI. And of the 143 us
// that remain, only about 41 are the bus clocking 41 bytes. The other hundred
// are Arduino: a digitalWrite on CSN costs some 4 us and there are two per
// transaction, plus beginTransaction, endTransaction and a polling loop per
// byte. Direct port writes would take most of it back, and would still not be
// on the critical path of anything measured here.
const SPISettings NRF_SPI(8000000, MSBFIRST, SPI_MODE0);
}  // namespace

uint8_t RadioController::regRead(uint8_t reg) {
  SPI.beginTransaction(NRF_SPI);
  digitalWrite(hw_.csn, LOW);
  SPI.transfer(CMD_R_REGISTER | reg);
  const uint8_t value = SPI.transfer(0xFF);
  digitalWrite(hw_.csn, HIGH);
  SPI.endTransaction();
  return value;
}

// Multi-byte read, for the address registers. The bytes come back in the order
// they were written - RF24 writes buf[0] first and the chip returns it first -
// so a readback prints identically to the address that was configured, without
// anyone having to reason about which end is the LSByte.
void RadioController::regReadBuf(uint8_t reg, uint8_t *buf, uint8_t len) {
  SPI.beginTransaction(NRF_SPI);
  digitalWrite(hw_.csn, LOW);
  SPI.transfer(CMD_R_REGISTER | reg);
  for (uint8_t i = 0; i < len; i++) buf[i] = SPI.transfer(0xFF);
  digitalWrite(hw_.csn, HIGH);
  SPI.endTransaction();
}

void RadioController::regWrite(uint8_t reg, uint8_t value) {
  SPI.beginTransaction(NRF_SPI);
  digitalWrite(hw_.csn, LOW);
  SPI.transfer(0x20 | reg);
  SPI.transfer(value);
  digitalWrite(hw_.csn, HIGH);
  SPI.endTransaction();
}

void RadioController::spiCommand(uint8_t cmd) {
  SPI.beginTransaction(NRF_SPI);
  digitalWrite(hw_.csn, LOW);
  SPI.transfer(cmd);
  digitalWrite(hw_.csn, HIGH);
  SPI.endTransaction();
}

uint8_t RadioController::payloadWidth() {
  SPI.beginTransaction(NRF_SPI);
  digitalWrite(hw_.csn, LOW);
  SPI.transfer(CMD_R_RX_PL_WID);
  const uint8_t width = SPI.transfer(0xFF);
  digitalWrite(hw_.csn, HIGH);
  SPI.endTransaction();
  return width;
}

void RadioController::readPayload(uint8_t *out, uint8_t len) {
  SPI.beginTransaction(NRF_SPI);
  digitalWrite(hw_.csn, LOW);
  SPI.transfer(CMD_R_RX_PAYLOAD);
  for (uint8_t i = 0; i < len; i++) {
    out[i] = SPI.transfer(0xFF);
  }
  digitalWrite(hw_.csn, HIGH);
  SPI.endTransaction();
}

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
  if (!chip) { hwState_ = HW_FAILED; return false; }

  hwReady_ = true;
  hwState_ = HW_CONNECTED;
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
  // How long to wait for an acknowledgement and how often to try again. Set
  // here rather than left at whatever begin() wrote, so that what `info`
  // reports is what the chip was told - and so a run can widen it when the
  // link is marginal instead of guessing why frames were given up on.
  radio_.setRetries((uint8_t)(cfg_.ardUs / 250 - 1), cfg_.arc);

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

void RadioController::invalidateHw() {
  hwReady_ = false;
  configured_ = false;
  listening_ = false;
  hwState_ = HW_FAILED;
}

const __FlashStringHelper *RadioController::hwStateName() const {
  switch (hwState_) {
    case HW_CONNECTED: return F("connected");
    case HW_FAILED:    return F("failed");
    default:           return F("none");
  }
}

bool RadioController::selfTestCe() {
  if (!hwReady_) return false;

  // Transient settings, deliberately NOT a default configuration: the radio is
  // left unconfigured afterwards, so nothing can be operated on these values.
  // One byte at minimum power on one channel is the smallest emission that
  // still proves CE keys the transmitter.
  static const uint8_t testAddr[5] = {'T', 'E', 'S', 'T', 0x00};
  const uint8_t payload = 0x00;

  radio_.stopListening();
  radio_.setAddressWidth(5);
  radio_.setChannel(CE_TEST_CHANNEL);
  radio_.setPALevel(RF24_PA_MIN);
  radio_.setAutoAck(false);
  radio_.enableDynamicAck();
  radio_.openWritingPipe(testAddr);

  radio_.startFastWrite(&payload, 1, true); // NO_ACK
  bool ok = radio_.txStandBy(TX_TIMEOUT_MS);
  if (!ok) radio_.flush_tx();

  configured_ = false; // the test settings are not a usable configuration
  return ok;
}

const __FlashStringHelper *RadioController::stateName() const {
  if (!hwReady_) return F("nohw");
  if (scanning_) return F("scanning");
  if (!configured_) return F("unconfigured");
  return listening_ ? F("listening") : F("idle");
}

void RadioController::startListening() {
  resetTiming();
  // The counters describe one capture, so they start over with it - otherwise
  // "12 overflows" says nothing about the run you are looking at.
  rxCount_ = 0;
  fifoFull_ = 0;
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

// Where a received frame's time goes: everything up to and including the flush
// (SPI, registers, the length question) against everything after it (the line
// or the record on the wire). Kept as sums so reading them costs nothing per
// frame, and reset by `listen` so a measurement starts where it was asked for.
void RadioController::accrue(uint32_t usEnter, uint32_t usRead) {
  usIn_ += usRead - usEnter;
  usOut_ += micros() - usRead;
  usFrames_++;
}

void RadioController::drainRx() {
  // A full RX FIFO means the host could not keep up; further frames arriving
  // now are dropped by the chip. Surfacing this separates "lost on air" from
  // "lost because we were busy printing". The count goes with the warning so a
  // host that missed earlier lines can still see how often it has happened.
  if (radio_.rxFifoFull()) {
    if (fifoFull_ < 0xFFFF) fifoFull_++;
    Serial.print(F("WARN fifo-full n="));
    Serial.println(fifoFull_);
  }

  for (uint8_t guard = 0; guard < 8; guard++) {
    const uint8_t fifoPre = regRead(REG_FIFO_STATUS);
    if (fifoPre & FIFO_RX_EMPTY) {
      break;
    }
    rxPass_++;
    const uint32_t usEnter = micros();
    // RX_P_NO in STATUS names the pipe of the payload at the FIFO top.
    const uint8_t statusPre = regRead(REG_STATUS);
    const uint8_t pipe = (statusPre >> 1) & 0x07;
    // Where the length comes from is the experiment: before the payload read
    // (every mode that ships), after it, or not asked for at all.
    uint8_t len;
    if (!cfg_.dpl)                    len = cfg_.plSize;
    else if (rxMode_ == RX_NOWID)     len = 32;
    else if (rxMode_ == RX_WIDLATE)   len = 0;   // filled in after the read
    else                              len = payloadWidth();
    if (rxMode_ != RX_WIDLATE && (len == 0 || len > 32)) {
      // Say so. This used to discard silently, which in a tool whose whole
      // purpose is to show what arrives is the worst possible failure mode.
      Serial.print(F("WARN bad payload length "));
      Serial.print(len);
      Serial.print(F(" on p"));
      Serial.println(pipe);
      spiCommand(CMD_FLUSH_RX); // corrupt dynamic length: discard to unstick the FIFO
      break;
    }
    // Clock the whole slot, then reset the FIFO. The flush is the part that
    // matters: it is the only thing measured to put the FIFO back in step, and
    // with it a mix of 8, 16 and 32-byte payloads comes out exactly as sent.
    // Reading the whole slot without flushing still duplicates - so does every
    // other read strategy tried (see the note above). Taking one payload per
    // pass and flushing costs the copies queued behind it, but a sender repeats
    // every event several times anyway, and a lost copy is worth far more than
    // an invented frame.
    uint8_t buf[32];
    readPayload(buf, rxMode_ == RX_WIDTH ? len : 32);
    if (rxMode_ == RX_WIDLATE) {
      len = cfg_.dpl ? payloadWidth() : cfg_.plSize;
      if (len == 0 || len > 32) {
        Serial.print(F("WARN bad payload length "));
        Serial.print(len);
        Serial.print(F(" on p"));
        Serial.println(pipe);
        spiCommand(CMD_FLUSH_RX);
        break;
      }
    }
    // Snapshots, not prints: printing here would put four milliseconds of serial
    // between the read and the flush, which is the interval under suspicion.
    const uint8_t fifoMid = rxDbg_ ? regRead(REG_FIFO_STATUS) : 0;
    // Not in RX_NOWID: the whole point of that mode is that R_RX_PL_WID is never
    // issued, and a trace that issues it anyway measures the wrong firmware.
    const uint8_t widthMid =
        (rxDbg_ && cfg_.dpl && rxMode_ != RX_NOWID) ? payloadWidth() : 0;
    regWrite(REG_STATUS, STATUS_RX_DR);
    // The flush costs half the reception rate - it discards whatever arrived
    // while the firmware was busy, so only one frame is taken per pass. It
    // exists for the duplicate fault, and that fault has one stated condition:
    // "a payload that fills the 32-byte slot comes out exactly once. One that
    // does not leaves the FIFO a payload out of step."
    //
    // So it is spent where it is needed and not where it is not. A full slot
    // skips it and the FIFO keeps its queue; anything shorter is flushed as
    // before.
    //
    // The fault did not reproduce here at all - not against a real RotRemote at
    // 32 bytes, not against a dongle sending 8, 12, 16, 20, 24 and 32 both
    // statically and dynamically, sparse, back to back and as three copies five
    // milliseconds apart, acknowledged and not, in every rxmode including the
    // one documented as worst. That is not why this is conditional rather than
    // gone: a fault that lives in the chip's FIFO RAM and outlives a reset is
    // not disproved by a bench that cannot summon it, and the payloads it was
    // stated for are exactly the ones still protected here.
    if (rxMode_ == RX_FLUSH && len < 32) spiCommand(CMD_FLUSH_RX);
    const uint8_t fifoPost = rxDbg_ ? regRead(REG_FIFO_STATUS) : 0;
    // Stamped where the frame leaves the FIFO, which is the earliest moment the
    // firmware knows of it. Host arrival times cannot resolve the few
    // milliseconds between a sender's repeats: they carry the serial transfer
    // and the host's scheduling on top.
    uint32_t stamp = millis();
    const uint32_t usRead = micros();

    if (rxDbg_) {
      // One line per pass, whether or not the frame itself is printed: the
      // question a trace answers is what the FIFO did, and a frame suppressed
      // by the repeat filter went through the same FIFO as any other.
      Serial.print(F("DBG n="));
      Serial.print(rxPass_);
      Serial.print(F(" mode="));
      Serial.print(rxMode_);
      Serial.print(F(" p"));
      Serial.print(pipe);
      Serial.print(F(" w="));
      Serial.print(len);
      Serial.print(F(" w2="));
      Serial.print(widthMid);
      Serial.print(F(" st="));
      Serial.print(statusPre, HEX);
      Serial.print(F(" fifo="));
      Serial.print(fifoPre, HEX);
      Serial.print('/');
      Serial.print(fifoMid, HEX);
      Serial.print('/');
      Serial.println(fifoPost, HEX);
    }

    if (isRepeat(buf, len) && !showRepeats_) continue;

    if (rxCount_ < 0xFFFFFFFF) rxCount_++;
    led(hw_.ledRx, true);

    if (outMode_ == OUT_NONE) {
      led(hw_.ledRx, false);
      accrue(usEnter, usRead);
      continue;
    }

    if (outMode_ == OUT_BIN) {
      // Sync, length, pipe, timestamp, payload, checksum - assembled here and
      // handed over in one Serial.write, so the per-byte cost is a copy into
      // the ring buffer instead of a number being formatted. 0x01 can start a
      // record unambiguously because nothing else this firmware prints is
      // outside printable ASCII, and the length says where the record ends, so
      // a payload byte that happens to be 0x01 or a newline decodes as data.
      //
      // The checksum covers exactly the bytes the readable line's `crc=` does -
      // the payload as it left the FIFO, nothing else. That keeps the two
      // shapes saying the same thing about the same bytes, so a host can turn
      // one into the other and everything above it goes on meaning what it
      // meant. The header rides unprotected, which costs nothing in practice: a
      // reader that mis-syncs takes a wrong length, and a wrong length fails
      // this checksum, so it hunts for the next sync byte instead of believing
      // a shifted frame.
      uint8_t rec[7 + 32 + 2];
      rec[0] = RX_BIN_SYNC;
      rec[1] = len;
      rec[2] = pipe;
      rec[3] = (uint8_t)(stamp);
      rec[4] = (uint8_t)(stamp >> 8);
      rec[5] = (uint8_t)(stamp >> 16);
      rec[6] = (uint8_t)(stamp >> 24);
      for (uint8_t i = 0; i < len; i++) rec[7 + i] = buf[i];
      rec[7 + len] = crc8(buf, len);
      // A newline after the record. It does not make this line-based - a
      // payload byte can be 0x0A and a reader must go by the length - but it
      // guarantees the next readable line starts on a fresh one. Without it a
      // reply printed while frames are arriving comes out glued to the tail of
      // a record, which is precisely the moment somebody has opened a terminal
      // to find out what is wrong. One byte of ninety.
      rec[8 + len] = '\n';
      Serial.write(rec, (size_t)(9 + len));
      led(hw_.ledRx, false);
      accrue(usEnter, usRead);
      continue;
    }

    // Compact hex (no separators) keeps the line short - the serial link is
    // the bottleneck during fast bursts.
    Serial.print(F("RX t="));
    Serial.print(stamp);
    Serial.print(F(" p"));
    Serial.print(pipe);
    Serial.print(F(" len="));
    Serial.print(len);
    // Everything after the radio's own CRC is unprotected: the SPI read, this
    // buffer, and the serial line. Measured with two dongles listening side by
    // side, one reported a quarter of its frames differing from what the sender
    // had logged - single flipped bits anywhere in the payload, sometimes two
    // lines run together - while the other reported 4%. A wrong byte in the
    // packet id turns a retransmission into what looks like a second event, so
    // without this the host cannot tell a measurement from an artefact. Taken
    // over the bytes as they left the FIFO, so it covers the whole path.
    Serial.print(F(" crc="));
    const uint8_t crc = crc8(buf, len);
    if (crc < 0x10) Serial.print('0');
    Serial.print(crc, HEX);
    Serial.print(' ');
    for (uint8_t i = 0; i < len; i++) {
      if (buf[i] < 0x10) Serial.print('0');
      Serial.print(buf[i], HEX);
    }
    Serial.println();
    led(hw_.ledRx, false);
    accrue(usEnter, usRead);
  }
  s_rxFlag = false;
}

void RadioController::poll() {
  if (scanning_) {
    // One sweep per poll, so commands - `scan off` above all - are still read
    // between them. A sweep is about 25 ms of blocked radio.
    if (scanDone_ == 0) {
      Serial.print(F("SCAN passes="));
      Serial.println(scanTarget_);
    }
    scanSweep();
    if (scanDone_ >= scanTarget_) scanReport();
    return;
  }
  if (listening_ && (s_rxFlag || radio_.available())) {
    drainRx();
  }
}

RadioController::TxResult RadioController::transmit(const uint8_t *addr,
                                                    const uint8_t *data,
                                                    uint8_t len, bool noack,
                                                    uint8_t count,
                                                    uint16_t gapMs) {
  radio_.stopListening();
  radio_.openWritingPipe(addr);
  led(hw_.ledTx, true);
  TxResult result;
  result.attempted = count;
  // Asking for an acknowledgement only means one when the chip is set up to
  // expect it. EN_AA is the chip's own answer to that, not the configuration's.
  result.acking = !noack && regRead(0x01) != 0;
  uint8_t sent = 0;
  for (uint8_t i = 0; i < count; i++) {
    // Bounded, deliberately not RF24::write(): if the CE pin is not actually
    // wired to the chip the transmission never starts, TX_DS/MAX_RT never arrive
    // and write() spins forever. With a timeout this becomes a reported failure -
    // and sent=0 doubles as the only practical check that CE is correct, since
    // isChipConnected() exercises SPI only and never touches CE.
    radio_.startFastWrite(data, len, noack); // noack => NO_ACK flag
    if (radio_.txStandBy(TX_TIMEOUT_MS)) {
      sent++;
    } else {
      result.failed++;
      radio_.flush_tx();
    }
    // ARC_CNT counts the retransmissions of the packet just handled; it resets
    // with each new one, so it has to be read here and summed.
    result.retries += regRead(0x08) & 0x0F;   // OBSERVE_TX
    if (gapMs != 0 && i + 1 < count) delay(gapMs);
  }
  result.sent = sent;
  led(hw_.ledTx, false);
  // openWritingPipe clobbers pipe 0; restore reading pipes and listen state.
  // Done once after the whole burst - reconfiguring between copies would add
  // milliseconds of SPI traffic exactly where the burst is meant to be tight.
  reconfigure();
  return result;
}

void RadioController::beginSequence(const uint8_t *addr, bool noack) {
  radio_.stopListening();
  radio_.openWritingPipe(addr);
  led(hw_.ledTx, true);
  seq_ = TxResult();
  seqNoack_ = noack;
  seq_.asked = !noack;
  seq_.acking = !noack && regRead(0x01) != 0;   // EN_AA, the chip's own answer
}

bool RadioController::sequenceWrite(const uint8_t *data, uint8_t len) {
  seq_.attempted++;
  if (!seq_.acking) {
    // Nothing to wait for, so only the FIFO can hold us up. Three deep: by the
    // time the third is written the first is usually gone.
    const uint32_t start = millis();
    while (regRead(REG_STATUS) & 0x01) {          // TX_FULL
      if (millis() - start > TX_TIMEOUT_MS) {     // CE not wired, or no clock
        seq_.failed++;
        return false;
      }
    }
    radio_.startFastWrite(data, len, true);
    seq_.sent++;
    return true;
  }
  // Acknowledged, and still pipelining: keep the FIFO fed and collect the
  // acknowledgements as they land, so the air runs while the next record is
  // still arriving over serial. Waiting for each frame instead cost half the
  // wire - 1.28 ms a frame at 2 Mbps where the line's own floor is 0.68.
  if (seq_.pipelining) {
    const uint32_t start = millis();
    while (true) {
      const uint8_t status = regRead(REG_STATUS);
      if (status & 0x10) {                        // MAX_RT: a frame gave up
        seq_.retries += regRead(0x08) & 0x0F;
        seq_.failed++;
        regWrite(REG_STATUS, 0x10);
        radio_.flush_tx();
        // Stop pipelining for the rest of the run. From here the count can no
        // longer be exact: TX_DS is a flag rather than a counter, and nothing
        // says how many packets were queued behind the one that failed. It can
        // only be short, never long, so the host resumes a little early and
        // sends up to a FIFO's worth twice - never skips any.
        seq_.pipelining = false;
        seq_.gaveUp = true;
        return false;                             // the run ends; host resumes
      }
      if (status & 0x20) {                        // TX_DS: one acknowledged
        seq_.sent++;
        seq_.retries += regRead(0x08) & 0x0F;     // OBSERVE_TX, that packet
        regWrite(REG_STATUS, 0x20);
      }
      if (!(status & 0x01)) break;                // TX_FULL clear: room for one
      if (millis() - start > TX_TIMEOUT_MS) {     // CE not wired, or no clock
        seq_.failed++;
        return false;
      }
    }
    radio_.startFastWrite(data, len, false);
    return true;
  }

  // After a failure the count has to mean something again, so each frame is
  // confirmed before the next goes in.
  radio_.startFastWrite(data, len, false);
  if (radio_.txStandBy(TX_TIMEOUT_MS)) {
    seq_.sent++;
    seq_.retries += regRead(0x08) & 0x0F;         // OBSERVE_TX, this packet
    return true;
  }
  seq_.failed++;
  seq_.retries += regRead(0x08) & 0x0F;
  seq_.gaveUp = true;
  radio_.flush_tx();
  return false;                                    // the run ends here
}

RadioController::TxResult RadioController::endSequence() {
  // Whatever is still in the FIFO has not been on the air yet. Waiting for it
  // is the difference between "handed over" and "transmitted".
  if (!seq_.acking) {
    radio_.txStandBy(TX_TIMEOUT_MS);
  } else if (seq_.pipelining) {
    // Drain what is still queued, then say the exact thing. Counting TX_DS
    // events can only undercount - two frames finishing between two looks are
    // one flag - so the tally is not what gets reported. If nothing gave up,
    // every frame that was written was acknowledged, and `sent` is `attempted`
    // exactly. That is the whole reason the pipeline stops at the first
    // failure: up to that point this identity holds.
    radio_.txStandBy(TX_TIMEOUT_MS);
    if (!seq_.gaveUp) seq_.sent = seq_.attempted;
  }
  led(hw_.ledTx, false);
  reconfigure();
  return seq_;
}

// The sweep runs at 2 Mbps whatever the radio is configured for. The RPD fires
// on carriers above about -64 dBm *inside the receiver bandwidth*, and that
// bandwidth follows the data rate: measured here, a band busy enough to light up
// 14-29 channels at 2 Mbps and 9-22 at 1 Mbps lit up exactly zero at 250 kbps.
// A scan configured for 250 kbps therefore reports an empty band no matter what
// is on the air, which is worse than useless. This measures the band; it does
// not claim to measure what a 250 kbps receiver will suffer from.
void RadioController::scanBegin() {
  radio_.stopListening();
  listening_ = false;
  radio_.setDataRate(RF24_2MBPS);
}

void RadioController::scanEnd() {
  if (configured_) {
    radio_.setDataRate(rateEnum(cfg_.rateKbps));
    radio_.setChannel(cfg_.channel);
  }
}

void RadioController::scanSweep() {
  for (uint8_t ch = 0; ch < CHANNELS; ch++) {
    radio_.setChannel(ch);
    radio_.startListening();
    delayMicroseconds(130);
    radio_.stopListening();
    if (radio_.testRPD() && scanCounts_[ch] < 255) scanCounts_[ch]++;
  }
  scanDone_++;
}

void RadioController::scanReport() {
  for (uint8_t ch = 0; ch < CHANNELS; ch++) {
    if (scanCounts_[ch]) {
      Serial.print(F("SCAN ch="));
      Serial.print(ch);
      Serial.print(F(" hits="));
      Serial.println(scanCounts_[ch]);
    }
    scanCounts_[ch] = 0;
  }
  Serial.println(F("SCAN end"));
  scanDone_ = 0;
}

void RadioController::scan(uint16_t passes) {
  bool wasListening = listening_;
  scanBegin();

  // Announced before the sweeps, not after: a sweep is a stretch of time with
  // nothing on the wire, and a host that hears about it only afterwards cannot
  // tell that from a command that was ignored.
  Serial.print(F("SCAN passes="));
  Serial.println(passes);
  for (uint16_t pass = 0; pass < passes; pass++) scanSweep();
  scanReport();

  scanEnd();
  if (wasListening) {
    radio_.startListening();
    listening_ = true;
  }
  Serial.println(F("OK scan done"));
}

void RadioController::startScan(uint16_t passesPerReport) {
  scanResume_ = listening_;
  scanBegin();
  for (uint8_t ch = 0; ch < CHANNELS; ch++) scanCounts_[ch] = 0;
  scanDone_ = 0;
  scanTarget_ = passesPerReport;
  scanning_ = true;
}

void RadioController::stopScan() {
  if (!scanning_) return;
  scanning_ = false;
  scanEnd();
  if (scanResume_) {
    radio_.startListening();
    listening_ = true;
  }
  scanResume_ = false;
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
  // A0 is a const in the Arduino core, not a macro - #ifdef would never match.
  if (pin >= A0 && pin < A0 + NUM_ANALOG_INPUTS) {
    Serial.print('A');
    Serial.print((int)(pin - A0));
    return;
  }
  Serial.print(pin);
}

void RadioController::printWiring() const {
  Serial.print(F("ce="));      printPin(hw_.ce);
  Serial.print(F(" csn="));    printPin(hw_.csn);
  Serial.print(F(" irq="));    printPin(hw_.irq);
  Serial.print(F(" led_rx=")); printPin(hw_.ledRx);
  Serial.print(F(" led_tx=")); printPin(hw_.ledTx);
}

// The registers worth comparing between two chips, by name, because "07=0E"
// tells you nothing you can act on. Addresses are left out: they are multi-byte
// reads and `info` already prints what the pipes were told to listen on.
void RadioController::printRegs() {
  static const struct { uint8_t reg; const char *name; } kRegs[] = {
      {0x00, "CONFIG"},     {0x01, "EN_AA"},      {0x02, "EN_RXADDR"},
      {0x03, "SETUP_AW"},   {0x04, "SETUP_RETR"}, {0x05, "RF_CH"},
      {0x06, "RF_SETUP"},   {0x07, "STATUS"},     {0x08, "OBSERVE_TX"},
      {0x09, "RPD"},        {0x11, "RX_PW_P0"},   {0x12, "RX_PW_P1"},
      {0x13, "RX_PW_P2"},   {0x14, "RX_PW_P3"},   {0x15, "RX_PW_P4"},
      {0x16, "RX_PW_P5"},   {0x17, "FIFO_STATUS"},{0x1C, "DYNPD"},
      {0x1D, "FEATURE"},
  };
  Serial.println(F("regs:"));
  for (uint8_t i = 0; i < sizeof(kRegs) / sizeof(kRegs[0]); i++) {
    const uint8_t value = regRead(kRegs[i].reg);
    Serial.print(F("  "));
    Serial.print(kRegs[i].name);
    Serial.print('=');
    if (value < 0x10) Serial.print('0');
    Serial.println(value, HEX);
  }
  Serial.println(F("OK"));
}

// The configuration the chip is holding, when that question has an answer.
//
// Only while listening: outside that window the registers describe the
// library's plumbing rather than the configuration (see ConfigSource), and a
// pipe 0 nobody asked for is exactly the kind of plausible-looking wrong value
// that costs an afternoon. `out` starts as the firmware's own copy, so a
// SRC_FIRMWARE answer is complete rather than empty.
RadioController::ConfigSource RadioController::readConfig(RadioConfig &out) {
  out = cfg_;
  if (!hwReady_ || !configured_ || !listening_) return SRC_FIRMWARE;

  const uint8_t config = regRead(0x00);          // CONFIG
  out.crcBits = !(config & 0x08) ? 0 : ((config & 0x04) ? 16 : 8);
  out.autoAck = regRead(0x01) != 0;              // EN_AA, all pipes together
  const uint8_t enRx = regRead(0x02);            // EN_RXADDR
  out.addrWidth = (uint8_t)((regRead(0x03) & 0x03) + 2);   // SETUP_AW
  const uint8_t retr = regRead(0x04);                     // SETUP_RETR
  out.ardUs = (uint16_t)(((retr >> 4) & 0x0F) + 1) * 250;
  out.arc = retr & 0x0F;
  out.channel = regRead(0x05) & 0x7F;            // RF_CH

  const uint8_t rf = regRead(0x06);              // RF_SETUP
  out.rateKbps = (rf & 0x20) ? 250 : ((rf & 0x08) ? 2000 : 1000);
  out.paLevel = (uint8_t)((rf >> 1) & 0x03);

  out.dpl = (regRead(0x1D) & 0x04) && regRead(0x1C) != 0;  // FEATURE, DYNPD
  out.plSize = regRead(0x12) & 0x3F;             // RX_PW_P1

  for (uint8_t p = 0; p < 6; p++) {
    out.pipeEn[p] = (enRx >> p) & 1;
    // Pipes 2-5 own one byte in their register; the rest of their address is
    // pipe 1's, which is read back here too.
    if (out.pipeEn[p]) regReadBuf((uint8_t)(0x0A + p), out.pipeAddr[p],
                                  p < 2 ? out.addrWidth : 1);
  }
  return SRC_CHIP;
}

void RadioController::printConfig(const RadioConfig &c, ConfigSource src, bool block) {
  // The only difference between the two layouts. Same tokens, same order, so a
  // host parses the acknowledgement and the info block with one function.
  auto sep = [block]() { block ? Serial.print(F("\n  ")) : Serial.print(' '); };

  sep(); Serial.print(F("channel=")); Serial.print(c.channel);
  sep(); Serial.print(F("rate="));    Serial.print(c.rateKbps); Serial.print(F("kbps"));
  sep(); Serial.print(F("crc="));     Serial.print(c.crcBits);
  sep(); Serial.print(F("aw="));      Serial.print(c.addrWidth);
  // Printed by name so the output matches what `listen pa=` accepts.
  sep(); Serial.print(F("pa="));
  switch (c.paLevel) {
    case 0:  Serial.print(F("min"));  break;
    case 1:  Serial.print(F("low"));  break;
    case 2:  Serial.print(F("high")); break;
    default: Serial.print(F("max"));  break;
  }
  sep(); Serial.print(F("ack="));    Serial.print(c.autoAck ? 1 : 0);
  sep(); Serial.print(F("dpl="));    Serial.print(c.dpl ? 1 : 0);
  sep(); Serial.print(F("plsize=")); Serial.print(c.plSize);
  sep(); Serial.print(F("retries=")); Serial.print(c.ardUs);
  Serial.print(','); Serial.print(c.arc);
  for (uint8_t p = 0; p < 6; p++) {
    if (!c.pipeEn[p]) continue;
    sep();
    Serial.print(F("pipe"));
    Serial.print(p);
    Serial.print('=');
    // Pipes 2-5 are configured with one byte but listen on a full address, the
    // rest of it borrowed from pipe 1. Print what they actually listen on.
    if (p >= 2) {
      uint8_t effective[MAX_ADDR_WIDTH];
      memcpy(effective, c.pipeAddr[1], c.addrWidth);
      effective[0] = c.pipeAddr[p][0];
      printAddr(effective, c.addrWidth);
    } else {
      printAddr(c.pipeAddr[p], c.addrWidth);
    }
  }
  // Whether the above was measured or merely intended. A reader who cannot tell
  // the two apart has to assume the weaker of them for both.
  sep(); Serial.print(F("src=")); Serial.print(src == SRC_CHIP ? F("chip") : F("firmware"));
}

void RadioController::printAck() {
  Serial.print(F(" state="));
  Serial.print(stateName());
  if (hwReady_) {
    Serial.print(' ');
    printWiring();
  }
  if (configured_) {
    RadioConfig c;
    const ConfigSource src = readConfig(c);
    printConfig(c, src, false);
  }
  Serial.println();
}

void RadioController::printInfo(long baud) {
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
  // Which shape frames are leaving in. Asked for at runtime and gone after a
  // reset, so it would otherwise be the one thing a dongle does that nothing
  // it says accounts for - and the one most likely to be mistaken for a broken
  // link, because in binary the frames stop looking like anything.
  Serial.print(F("  baud="));    Serial.println(baud);
  Serial.print(F("  format="));
  Serial.println(outMode_ == OUT_BIN ? F("bin")
                 : outMode_ == OUT_NONE ? F("none") : F("text"));
  // Averages, in microseconds, over the frames since the last `listen`. This is
  // the one number that says whether a dongle is keeping up and where its time
  // goes - guessing at it from component costs was off by a factor of five.
  Serial.print(F("  us_in="));
  Serial.print(usFrames_ ? (uint16_t)(usIn_ / usFrames_) : 0);
  Serial.print(F(" us_out="));
  Serial.print(usFrames_ ? (uint16_t)(usOut_ / usFrames_) : 0);
  Serial.print(F(" us_n="));
  Serial.println(usFrames_);
  Serial.print(F("  rxmode="));  Serial.println(rxMode_);
  // No newline yet: in block layout printConfig() opens each field with one, so
  // the last line printed here is the one it continues from.
  Serial.print(F("  rxdbg="));   Serial.print(rxDbg_ ? 1 : 0);

  if (configured_) {
    RadioConfig c;
    const ConfigSource src = readConfig(c);
    printConfig(c, src, true);
    Serial.print(F("\n  rx="));       Serial.print(rxCount_);
    Serial.print(F("\n  fifofull=")); Serial.print(fifoFull_);
  }
  Serial.println();
  Serial.println(F("OK"));
}
