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
// nRF24L01+'s 10. What each layer of the Arduino stack costs, by the drain
// loop's own clock (`us_in` in `info`), per received frame:
//
//   4 MHz bus                                          190 us
//   8 MHz bus                                          143 us
//   ... the same under load, with a 39-byte record     223 us
//   CSN by direct port write instead of digitalWrite   191 us
//   SPCR written directly instead of beginTransaction  191 us  - nothing
//
// The last of those was tried and reverted. Without a registered interrupt
// `SPI.beginTransaction` compiles to the same two register writes on this
// architecture, so hand-rolling them buys exactly zero and gives up the one
// thing the library call is for.
//
// Of the 191 that remain, about 39 are the bus clocking 37 bytes. Most of the
// rest is `SPI.transfer`'s poll-until-done, which is inherent: the ATmega's SPI
// is not double-buffered, so every byte is written, waited for, and read.
//
// None of this shows on the sending path - a transfer runs at the same
// milliseconds either way, because that path is bound by the serial line - and
// all of it shows on the receiving one, where a frame arriving every 850 us has
// to be read out and written on before the next lands.
const SPISettings NRF_SPI(8000000, MSBFIRST, SPI_MODE0);
}  // namespace

uint8_t RadioController::regRead(uint8_t reg) {
  SPI.beginTransaction(NRF_SPI);
  csnLow();
  SPI.transfer(CMD_R_REGISTER | reg);
  const uint8_t value = SPI.transfer(0xFF);
  csnHigh();
  SPI.endTransaction();
  return value;
}

// Multi-byte read, for the address registers. The bytes come back in the order
// they were written - RF24 writes buf[0] first and the chip returns it first -
// so a readback prints identically to the address that was configured, without
// anyone having to reason about which end is the LSByte.
void RadioController::regReadBuf(uint8_t reg, uint8_t *buf, uint8_t len) {
  SPI.beginTransaction(NRF_SPI);
  csnLow();
  SPI.transfer(CMD_R_REGISTER | reg);
  for (uint8_t i = 0; i < len; i++) buf[i] = SPI.transfer(0xFF);
  csnHigh();
  SPI.endTransaction();
}

void RadioController::regWrite(uint8_t reg, uint8_t value) {
  SPI.beginTransaction(NRF_SPI);
  csnLow();
  SPI.transfer(0x20 | reg);
  SPI.transfer(value);
  csnHigh();
  SPI.endTransaction();
}

void RadioController::spiCommand(uint8_t cmd) {
  SPI.beginTransaction(NRF_SPI);
  csnLow();
  SPI.transfer(cmd);
  csnHigh();
  SPI.endTransaction();
}

uint8_t RadioController::payloadWidth() {
  SPI.beginTransaction(NRF_SPI);
  csnLow();
  SPI.transfer(CMD_R_RX_PL_WID);
  const uint8_t width = SPI.transfer(0xFF);
  csnHigh();
  SPI.endTransaction();
  return width;
}

void RadioController::readPayload(uint8_t *out, uint8_t len) {
  SPI.beginTransaction(NRF_SPI);
  csnLow();
  SPI.transfer(CMD_R_RX_PAYLOAD);
  for (uint8_t i = 0; i < len; i++) {
    out[i] = SPI.transfer(0xFF);
  }
  csnHigh();
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

  // Resolve CSN once. radio_.begin() below sets the pin's direction and drives
  // it, so this only has to survive until the first transaction of our own.
  csnOut_ = portOutputRegister(digitalPinToPort(hw_.csn));
  csnBit_ = digitalPinToBitMask(hw_.csn);

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
  // A capture starts with no epoch to refer back to, so the first run states one
  // rather than assuming a host that has just connected shares ours.
  streamEpoch_ = false;
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

// One frame into the open run of binary frames, opening or re-opening it first.
//
// Everything a run of frames has in common - which pipe they came in on, how
// long their payloads are, and which millisecond they are counted from - is
// stated once at the top of the run. A frame then costs three bytes: what it
// stores, an offset from the run's base, and a checksum.
//
// The offset is to the *base*, not to the frame before. Every frame is therefore
// placed independently: one lost in between costs nothing, and no error
// accumulates. A delta to the previous frame would have shifted every timestamp
// after a loss, which is precisely what the stamp exists to measure.
//
// Assembled here and handed over in one Serial.write, so the per-byte cost is a
// copy into the ring buffer rather than a number being formatted.
void RadioController::streamFrame(const uint8_t *buf, uint8_t len, uint8_t pipe,
                                  uint32_t stamp) {
  // A repeated tail goes as one byte. Senders that pad a static payload out to
  // 32 bytes are most of what this dongle ever sees - the BTHome sender on this
  // bench ends every frame with twelve bytes of FF - and those cost the same
  // serial time as twelve bytes of data. The scan runs backwards over at most 31
  // bytes, nothing against the 240 us they would take on the wire, and a payload
  // with no repeated tail loses nothing by being asked.
  uint8_t run = 0;
  if (len >= RX_RUN_MIN) {
    const uint8_t fill = buf[len - 1];
    run = 1;
    while (run < len - 1 && run < RX_RUN_MAX && buf[len - 1 - run] == fill) run++;
    if (run < RX_RUN_MIN) run = 0;
  }
  const uint8_t stored = (uint8_t)(len - run);

  // The epoch has to be re-stated when it no longer describes this frame. A
  // one-byte offset carries 255 ms, which at 2 Mbps is some 290 frames.
  const bool needEpoch = !streamEpoch_ || pipe != streamPipe_ ||
                         (uint32_t)(stamp - streamBase_) > 255;
  if (streamOpen_ && needEpoch) streamEnd();

  uint8_t rec[7 + 3 + 32 + 1];
  uint8_t n = 0;
  if (!streamOpen_) {
    if (needEpoch) {
      rec[n++] = RX_RUN_NEW;
      rec[n++] = pipe;
      rec[n++] = len;                       // what a frame is, unless it says otherwise
      rec[n++] = (uint8_t)(stamp);
      rec[n++] = (uint8_t)(stamp >> 8);
      rec[n++] = (uint8_t)(stamp >> 16);
      rec[n++] = (uint8_t)(stamp >> 24);
      streamPipe_ = pipe;
      streamLen_ = len;
      streamBase_ = stamp;
      streamEpoch_ = true;
    } else {
      // Same pipe, same minute: the run before it said all of that already.
      rec[n++] = RX_RUN_MORE;
    }
    streamOpen_ = true;
  }

  const bool longLen = (len != streamLen_);
  rec[n++] = (uint8_t)(stored | (longLen ? RX_LEN_LONG : 0));
  if (longLen) rec[n++] = len;
  rec[n++] = (uint8_t)(stamp - streamBase_);
  for (uint8_t i = 0; i < stored; i++) rec[n++] = buf[i];
  if (run) rec[n++] = buf[len - 1];
  // Over the payload as it was, not as it is being sent: the binary and the
  // readable shape have to keep saying the same thing about the same bytes, and
  // a host that rebuilds the tail wrongly should fail this check, not pass it.
  rec[n++] = crc8(buf, len);

  Serial.write(rec, (size_t)n);
}

// Close the run, so that whatever is printed next is readable again.
//
// RX_RUN_END cannot be mistaken for the first byte of another frame: that byte
// carries a stored length of at most 32 and one flag, so it never exceeds 0x60.
// The newline after it is for a person with a terminal open - it terminates
// nothing, and a reader must go by RX_RUN_END.
void RadioController::streamEnd() {
  if (!streamOpen_) return;
  static const uint8_t tail[2] = {RX_RUN_END, '\n'};
  Serial.write(tail, sizeof(tail));
  streamOpen_ = false;
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
      streamEnd();   // a readable line cannot be printed inside a run
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
        streamEnd();   // a readable line cannot be printed inside a run
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
      streamEnd();
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
      streamFrame(buf, len, pipe, stamp);
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
  // The run closes with the pass that filled it. Nothing outside this function
  // has to know about it: by the time a command is read or a reply printed,
  // there is no open run to interrupt.
  streamEnd();
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
bool RadioController::scanBegin() {
  if (scanCounts_ == nullptr) {
    scanCounts_ = (uint8_t *)calloc(CHANNELS, 1);
    if (scanCounts_ == nullptr) return false;
  }
  radio_.stopListening();
  listening_ = false;
  radio_.setDataRate(RF24_2MBPS);
  return true;
}

void RadioController::scanEnd() {
  free(scanCounts_);
  scanCounts_ = nullptr;
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
  if (!scanBegin()) { Serial.println(F("ERR no memory for a scan")); return; }

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

bool RadioController::startScan(uint16_t passesPerReport) {
  scanResume_ = listening_;
  if (!scanBegin()) return false;
  scanDone_ = 0;
  scanTarget_ = passesPerReport;
  scanning_ = true;
  return true;
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
  // Both tables in flash. As a plain array of pointers-to-string this cost 57
  // bytes of RAM permanently to print nineteen names on request, which on a 2 KB
  // chip is a poor trade for a diagnostic command.
  static const uint8_t kRegAddr[] PROGMEM = {
      0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09,
      0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x1C, 0x1D,
  };
  static const char kRegName[][12] PROGMEM = {
      "CONFIG",   "EN_AA",    "EN_RXADDR", "SETUP_AW", "SETUP_RETR",
      "RF_CH",    "RF_SETUP", "STATUS",    "OBSERVE_TX", "RPD",
      "RX_PW_P0", "RX_PW_P1", "RX_PW_P2",  "RX_PW_P3", "RX_PW_P4",
      "RX_PW_P5", "FIFO_STATUS", "DYNPD",  "FEATURE",
  };
  char name[12];

  Serial.println(F("regs:"));
  for (uint8_t i = 0; i < sizeof(kRegAddr); i++) {
    const uint8_t value = regRead(pgm_read_byte(&kRegAddr[i]));
    strcpy_P(name, kRegName[i]);
    Serial.print(F("  "));
    Serial.print(name);
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
  // The clock frame records are stamped against. They carry only its low 16
  // bits, so a host that has just connected - or has heard nothing for hours -
  // can anchor itself here instead of guessing which wrap it is in.
  Serial.print(F("  ms="));      Serial.println(millis());
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
