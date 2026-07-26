#pragma once
#include <Arduino.h>
#include <RF24.h>

// Maximum nRF24 address width in bytes.
constexpr uint8_t MAX_ADDR_WIDTH = 5;

// Sentinel for "this pin is not used".
constexpr uint8_t NO_PIN = 255;

// LEDs are assumed to be wired active-low (the usual arrangement on these
// dongles). Everything else about the board comes from the host via `hwset`.
constexpr uint8_t LED_ON = LOW;
constexpr uint8_t LED_OFF = HIGH;

// Board wiring, supplied at runtime by the `hwset` command. Nothing about the
// board is compiled in, so one firmware image serves any nRF24 wiring.
struct HwConfig {
  uint8_t ce = NO_PIN;    // mandatory
  uint8_t csn = NO_PIN;   // mandatory
  uint8_t irq = NO_PIN;   // optional; NO_PIN falls back to polling
  uint8_t ledRx = NO_PIN; // optional
  uint8_t ledTx = NO_PIN; // optional
};

// Full radio configuration.
//
// There are deliberately NO defaults: the host must supply every parameter via
// the `listen` command before the radio does anything. A sniffer that silently
// comes up on some built-in channel invites the worst kind of error - concluding
// "nothing is being transmitted" when in truth the wrong question was asked.
// The values below are placeholders and are only meaningful once
// RadioController::configured() is true.
struct RadioConfig {
  uint8_t  channel   = 0;
  uint16_t rateKbps  = 0;     // 250 | 1000 | 2000
  uint8_t  crcBits   = 0;     // 0 | 8 | 16
  uint8_t  addrWidth = 0;     // 3 | 4 | 5
  bool     autoAck   = false;
  bool     dpl       = false; // dynamic payloads
  uint8_t  plSize    = 32;    // static payload size when dpl == false
  uint8_t  paLevel   = 0;     // 0=min 1=low 2=high 3=max
  bool     pipeEn[6] = {false, false, false, false, false, false};
  uint8_t  pipeAddr[6][MAX_ADDR_WIDTH] = {{0}, {0}, {0}, {0}, {0}, {0}};
};

// Wraps the RF24 radio and applies a RadioConfig to it. Owns RX draining,
// transmit, channel scanning and the `info` dump.
class RadioController {
public:
  RadioController();

  // Adopts a board wiring and brings the radio up on it. Emits a WARN and
  // falls back to polling if the requested IRQ pin cannot raise interrupts.
  // Returns true if the nRF24 chip responds. Discards any radio configuration.
  bool setHardware(const HwConfig &hw);

  // Adopts a complete configuration and applies it to the chip.
  void applyConfig(const RadioConfig &cfg);

  const RadioConfig &config() const { return cfg_; }
  const HwConfig &hw() const { return hw_; }
  bool hwReady() const { return hwReady_; }
  bool configured() const { return configured_; }
  bool listening() const { return listening_; }
  bool chipConnected() { return radio_.isChipConnected(); }

  // "none" until a wiring is adopted, then "connected" or "failed". Kept here
  // so `status` can report the same thing the greeting did, at any time.
  const __FlashStringHelper *hwStateName() const;
  void markHwFailed() { hwState_ = HW_FAILED; }

  // Frames printed since the last startListening(), and the number of times the
  // RX FIFO was found full. The chip has no lost-frame counter, so a full FIFO
  // is the only evidence available - it means at least one frame was at risk,
  // not that exactly one was lost.
  uint32_t rxCount() const { return rxCount_; }
  uint16_t fifoFullCount() const { return fifoFull_; }

  // "nohw" | "unconfigured" | "idle" | "listening"
  const __FlashStringHelper *stateName() const;

  // Prints the wiring as "ce=9 csn=10 irq=2 led_rx=8 led_tx=A1" (no newline).
  void printWiring() const;

  // Transmits one minimal packet to prove the CE pin actually keys the radio.
  // isChipConnected() cannot show this: it exercises SPI only and never touches
  // CE, so a wrong CE pin otherwise passes setHardware() and then receives
  // nothing. Uses a transient configuration and leaves the radio unconfigured.
  bool selfTestCe();

  // Drops back to the nohw state (used when a self-test fails).
  void invalidateHw();

  // Output filter: when false, identical back-to-back frames are printed once.
  void setShowRepeats(bool on) { showRepeats_ = on; }
  bool showRepeats() const { return showRepeats_; }

  void startListening();
  void stopListening();

  // Drains any pending RX frames to serial. Cheap to call every loop.
  void poll();

  // Transmits `count` copies of one payload to `addr`, `gapMs` apart. noack=true
  // sends with the NO_ACK flag (per-packet), matching a broadcast sender - which
  // repeats each event a few ms apart, exactly what count/gap emulate. The radio
  // stays in TX between the copies, so gap=0 spaces them only by the air time.
  // Returns how many copies the radio reported as sent.
  uint8_t transmit(const uint8_t *addr, const uint8_t *data, uint8_t len,
                   bool noack, uint8_t count = 1, uint16_t gapMs = 0);

  // Energy scan across all 126 channels, `passes` sweeps. Prints hits.
  void scan(uint16_t passes);

  // Continuous scanning: one report per `passesPerReport` sweeps, emitted from
  // poll() so commands keep being answered in between. Receiving is impossible
  // while it runs - the radio is being retuned across the band - and resumes on
  // stopScan() if it was running before.
  void startScan(uint16_t passesPerReport);
  void stopScan();
  bool scanning() const { return scanning_; }

  // Prints the current state and configuration.
  void printInfo();

private:
  // A frame repeated within this window counts as a retransmit of the previous
  // one (a sender typically repeats each event a few ms apart).
  static constexpr uint16_t REPEAT_WINDOW_MS = 500;

  // Upper bound for one transmit attempt; see transmit().
  static constexpr uint32_t TX_TIMEOUT_MS = 50;

  // Channel used by selfTestCe(). Kept well inside the 2.4 GHz ISM band -
  // the nRF24 tunes up to channel 125 (2525 MHz), which is outside it.
  static constexpr uint8_t CE_TEST_CHANNEL = 2; // 2402 MHz

  enum HwState : uint8_t { HW_NONE, HW_CONNECTED, HW_FAILED };

  RF24 radio_; // pinless constructor: pins are supplied at begin() time
  RadioConfig cfg_;
  HwConfig hw_;
  bool hwReady_ = false;
  bool configured_ = false;
  bool listening_ = false;
  bool showRepeats_ = true;
  HwState hwState_ = HW_NONE;
  uint32_t rxCount_ = 0;
  uint16_t fifoFull_ = 0;

  static constexpr uint8_t CHANNELS = 126;
  bool scanning_ = false;
  bool scanResume_ = false;      // was the radio listening when the scan began
  uint16_t scanTarget_ = 0;      // sweeps per report
  uint16_t scanDone_ = 0;        // sweeps since the last report
  uint8_t scanCounts_[CHANNELS] = {0};

  void scanBegin();              // stop receiving and widen the receiver
  void scanEnd();                // put the configured rate and channel back
  void scanSweep();              // one pass over every channel
  void scanReport();             // print and clear the accumulated counts

  void led(uint8_t pin, bool on);

  // Last frame seen, for the repeat filter.
  uint8_t lastFrame_[32] = {0};
  uint8_t lastLen_ = 0;
  uint32_t lastMs_ = 0;

  void reconfigure();
  void drainRx();
  // Records the frame and reports whether it repeats the previous one.
  bool isRepeat(const uint8_t *buf, uint8_t len);
};
