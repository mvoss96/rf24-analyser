#pragma once
#include <Arduino.h>
#include <RF24.h>
#include <SPI.h>

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
  // Auto-retransmit: how long the chip waits for an acknowledgement and how
  // often it tries again. Only in play when autoAck is on. The defaults are
  // what RF24::begin() writes, not the chip's own reset values (250 us / 3) -
  // worth saying, because a report of "no acknowledgement" reads differently
  // depending on how long the radio was willing to wait for one.
  uint16_t ardUs     = 1500;  // 250..4000, in steps of 250
  uint8_t  arc       = 15;    // 0..15; 0 disables retransmission entirely
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

  // How a payload is taken out of the RX FIFO. The shipping behaviour is
  // RX_FLUSH, which was arrived at empirically, so the alternatives have to be
  // comparable against the same traffic without reflashing: the fault depends on
  // FIFO history, and a reflash is one of the few things that disturbs it.
  //   RX_WIDTH:   read the width the chip reports, leave the FIFO alone
  //   RX_FULL:    read the whole 32-byte slot, leave the FIFO alone
  //   RX_FLUSH:   read the whole slot, then flush - what master does
  //   RX_NOWID:   never ask for the width at all, report the whole slot
  //   RX_WIDLATE: ask for the width only after the payload has been read
  //
  // The last two exist because R_RX_PL_WID is the one chip command the clean
  // configurations never issue: static payload sizes were always clean, and they
  // take the length from RX_PW_Pn instead. If asking for the width is itself
  // what makes the chip announce a second, stale arrival, then asking later - the
  // trace shows the width still readable afterwards - costs nothing at all.
  enum RxMode : uint8_t {
    RX_WIDTH = 0, RX_FULL = 1, RX_FLUSH = 2, RX_NOWID = 3, RX_WIDLATE = 4
  };
  void setRxMode(uint8_t mode) { rxMode_ = mode; }
  uint8_t rxMode() const { return rxMode_; }

  // How a received frame leaves: as the readable `RX t=... ` line, or as a
  // binary record. Measured, the readable line costs about 4 ms a frame and
  // caps reception at some 250 frames a second - and only 1.7 ms of that is
  // the serial line. The rest is printing 32 bytes as 64 hex characters one
  // Serial.print at a time, about a thousand clock cycles per byte. A binary
  // record removes both halves at once, so this is worth a switch rather than
  // a compile-time choice. Readable is the default and a reset returns to it:
  // whoever opens a terminal expecting to read along is not surprised, and
  // only a host that asked for throughput gets bytes it has to decode.
  void resetTiming() { usIn_ = usOut_ = usFrames_ = 0; }
  // Three shapes, not two. `none` drains the FIFO and counts, and prints
  // nothing at all - which is what a dongle on the receiving end of a transfer
  // actually needs. Writing every frame out costs about 800 us, and a receiver
  // that cannot keep up stops acknowledging, so the sender retransmits: 77
  // retransmissions in 512 frames, measured. Silence removes that.
  enum OutMode : uint8_t { OUT_TEXT = 0, OUT_BIN = 1, OUT_NONE = 2 };
  void setOutMode(uint8_t m) { outMode_ = m; }
  uint8_t outMode() const { return outMode_; }
  void setBinaryOut(bool on) { outMode_ = on ? OUT_BIN : OUT_TEXT; }
  bool binaryOut() const { return outMode_ == OUT_BIN; }

  // Per-pass FIFO trace. Off by default and deliberately so: it adds SPI reads
  // and a serial line to every drain pass, which is milliseconds in exactly the
  // window being measured.
  void setRxDbg(bool on) { rxDbg_ = on; }
  bool rxDbg() const { return rxDbg_; }

  // Dumps the chip's registers, so two dongles showing different behaviour on
  // the same traffic can be compared byte for byte.
  void printRegs();

  // Raw register access for the diagnosis. Reaches states applyConfig() cannot
  // express, which is the whole point - the suspicion is that the configuration
  // itself (dynamic payloads without auto-ack) is what the chip mishandles.
  uint8_t regPeek(uint8_t reg) { return regRead(reg); }
  void regPoke(uint8_t reg, uint8_t value) { regWrite(reg, value); }

  void startListening();
  void stopListening();

  // Drains any pending RX frames to serial. Cheap to call every loop.
  void poll();

  // What a transmission actually did, as opposed to what it was asked to do.
  //
  // `sent` used to be the whole answer and it could not tell "the receiver
  // acknowledged" from "the radio was never expecting one to". Asking for `ack`
  // while the configuration has auto-ack off reports success for every frame,
  // with nobody listening on the address - true, and read as the opposite.
  struct TxResult {
    // Sixteen bits, not eight: `tx` sends at most sixteen copies, but `txseq`
    // takes up to sixty thousand frames. As bytes these wrapped, and silently -
    // a 300-frame run reported `sent=44`, a 512-frame run `sent=0`, both after
    // transmitting every frame. A count that lies about a completed transfer is
    // worse than no count, because it reads exactly like a truncated one.
    uint16_t attempted = 0;  // frames handed to the radio
    uint16_t sent = 0;       // left the FIFO: acknowledged, or emitted if no ack
    uint16_t failed = 0;     // gave up after the configured retransmissions
    uint16_t retries = 0;    // summed ARC_CNT, the retransmissions it did make
    bool acking = false;     // was an acknowledgement actually going to be waited for
    bool asked = false;      // ...and was one asked for, which is a different question
    // Whether the run is still keeping the transmit FIFO fed. It stops at the
    // first frame that gives up, because from that moment the count can no
    // longer be exact - see sequenceWrite().
    bool pipelining = true;
    bool gaveUp = false;
    // Where a frame's time went while the radio had it. `airUs` is the spin
    // waiting for room in the transmit FIFO, which is the air draining it;
    // `spiUs` is the payload going out over the bus. Whatever a run takes beyond
    // these two is the record arriving over serial and the parser reading it -
    // the split that says which of the three to work on next.
    uint32_t airUs = 0;
    uint32_t spiUs = 0;
  };

  // Transmits `count` copies of one payload to `addr`, `gapMs` apart. noack=true
  // sends with the NO_ACK flag (per-packet), matching a broadcast sender - which
  // repeats each event a few ms apart, exactly what count/gap emulate. The radio
  // stays in TX between the copies, so gap=0 spaces them only by the air time.
  // Returns how many copies the radio reported as sent.
  TxResult transmit(const uint8_t *addr, const uint8_t *data, uint8_t len,
                    bool noack, uint8_t count = 1, uint16_t gapMs = 0);

  // A run of different payloads, sent back to back.
  //
  // The radio is set up once and the FIFO kept fed: three packets deep, so a
  // free slot is written the moment there is one rather than after waiting for
  // the previous packet to be confirmed. That is what keeps the air busy - one
  // command per frame spent about seven milliseconds on serial and setup for
  // every one millisecond of transmission.
  //
  // Without acknowledgements the writes are never waited on at all, and `sent`
  // means emitted. With them each frame is confirmed before the next is written
  // - slower, but the count then means what it says - and the first frame the
  // receiver does not acknowledge ends the run, because in a transfer the
  // frames after a lost one are worth nothing until it is resent.
  void beginSequence(const uint8_t *addr, bool noack);
  bool sequenceWrite(const uint8_t *data, uint8_t len);   // false: gave up here
  // Wait for room in the transmit FIFO and reap what has completed, without
  // having a payload to hand yet. Called while the record is still arriving over
  // serial, which is the whole point: that wait used to happen after the record
  // was complete, so it was added to the wire time instead of hidden behind it.
  // Idempotent - sequenceWrite still checks, and then finds nothing to wait for.
  bool sequenceReady();                                   // false: gave up here
  TxResult endSequence();

  // Energy scan across all 126 channels, `passes` sweeps. Prints hits.
  void scan(uint16_t passes);

  // Continuous scanning: one report per `passesPerReport` sweeps, emitted from
  // poll() so commands keep being answered in between. Receiving is impossible
  // while it runs - the radio is being retuned across the band - and resumes on
  // stopScan() if it was running before.
  // False if the per-channel counters could not be taken; nothing has changed
  // and the caller must not report a scan as running.
  bool startScan(uint16_t passesPerReport);
  void stopScan();
  bool scanning() const { return scanning_; }

  // Prints the current state and configuration.
  // `baud` is passed in rather than held here: the serial port is not the
  // radio's business, but a session that raised its rate needs that visible in
  // the one block that claims to describe the whole dongle.
  void printInfo(long baud);

  // Where a reported configuration came from.
  //
  // The registers hold what the chip was actually given, which is the only
  // account that survives a value the chip did not take - but they say that only
  // while it is listening. stopListening() writes the TX address into RX_ADDR_P0
  // and force-enables pipe 0 (RF24.cpp), and a scan retunes the channel and the
  // data rate across the band. Read outside that window they describe the
  // library's plumbing rather than the configuration, so there the honest answer
  // is what the firmware holds - said as such, in `src=`.
  enum ConfigSource : uint8_t { SRC_CHIP, SRC_FIRMWARE };

  // Fills `out` with the configuration and says where it came from.
  ConfigSource readConfig(RadioConfig &out);

  // Completes the OK line of a command that may have changed the radio: the
  // state, the wiring and the configuration it left behind, as key=value tokens
  // in the grammar `info` uses, terminated by a newline. Acknowledging the
  // outcome instead of the mere fact of success is the point - `hwset` may
  // downgrade an irq pin to polling, and it discards the radio configuration.
  void printAck();

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

  // CSN, resolved once into the port register and bit it actually is.
  //
  // `digitalWrite` walks three PROGMEM lookup tables, checks for a timer on the
  // pin and masks interrupts, which is about 4 us - and a frame comes out of the
  // FIFO through four SPI transactions, so eight of them. The pin stays
  // configurable; only the translation stops happening a hundred thousand times
  // a second. Nothing in an interrupt writes a port here, so the read-modify-
  // write below needs no guard of its own.
  volatile uint8_t *csnOut_ = nullptr;
  uint8_t csnBit_ = 0;

  inline void csnLow()  { *csnOut_ &= (uint8_t)~csnBit_; }
  inline void csnHigh() { *csnOut_ |= csnBit_; }

  RF24 radio_; // pinless constructor: pins are supplied at begin() time
  RadioConfig cfg_;
  HwConfig hw_;
  bool hwReady_ = false;
  bool configured_ = false;
  bool listening_ = false;
  bool showRepeats_ = true;
  uint8_t outMode_ = 0;   // OutMode
  uint32_t usIn_ = 0, usOut_ = 0, usFrames_ = 0;
  uint8_t rxMode_ = RX_FLUSH;
  bool rxDbg_ = false;
  uint32_t rxPass_ = 0;   // drain passes since boot, to line traces up
  HwState hwState_ = HW_NONE;
  uint32_t rxCount_ = 0;
  uint16_t fifoFull_ = 0;

  // Running totals of the sequence in progress; see beginSequence().
  TxResult seq_;
  bool seqNoack_ = true;

  static constexpr uint8_t CHANNELS = 126;
  bool scanning_ = false;
  bool scanResume_ = false;      // was the radio listening when the scan began
  uint16_t scanTarget_ = 0;      // sweeps per report
  uint16_t scanDone_ = 0;        // sweeps since the last report
  // One counter per channel, held only while a scan runs. As a member it was
  // 126 bytes of a 2 KB chip reserved permanently for something that happens
  // for a few seconds and never while receiving - and the two cannot overlap,
  // because a scan retunes the radio. Allocated in scanBegin and released in
  // scanEnd, so the peak is what it always was and the resting state is 126
  // bytes better. It is the only allocation this firmware makes, and it is
  // freed by the same pair that made it, so there is nothing to fragment.
  uint8_t *scanCounts_ = nullptr;

  bool scanBegin();              // stop receiving, widen the receiver, take the counters
  void scanEnd();                // put the configured rate and channel back
  void scanSweep();              // one pass over every channel
  void scanReport();             // print and clear the accumulated counts

  void led(uint8_t pin, bool on);
  void accrue(uint32_t usEnter, uint32_t usRead);

  // The open run of binary frames, if there is one. `streamOpen_` is true only
  // inside a pass of the drain loop; the rest holds the epoch a later run can
  // still refer to, so a quiet dongle re-opens with one byte instead of seven.
  bool     streamOpen_ = false;
  bool     streamEpoch_ = false;   // the fields below have ever been set
  uint8_t  streamPipe_ = 0;
  uint8_t  streamLen_ = 0;         // the true length the run announced
  uint32_t streamBase_ = 0;

  void streamFrame(const uint8_t *buf, uint8_t len, uint8_t pipe, uint32_t stamp);
  void streamEnd();

  // Last frame seen, for the repeat filter.
  uint8_t lastFrame_[32] = {0};
  uint8_t lastLen_ = 0;
  uint32_t lastMs_ = 0;

  void reconfigure();
  void drainRx();

  // The configuration as key=value tokens. `block` picks the layout - one field
  // per indented line for the `info` dump, all on the current line for an
  // acknowledgement - and nothing else differs, so one grammar reads both.
  void printConfig(const RadioConfig &c, ConfigSource src, bool block);

  // Chip access for the RX path, bypassing the library - see the comment on
  // these in RadioController.cpp for why the library's own read sequence is
  // not used here.
  uint8_t regRead(uint8_t reg);
  void regReadBuf(uint8_t reg, uint8_t *buf, uint8_t len);
  void regWrite(uint8_t reg, uint8_t value);
  void spiCommand(uint8_t cmd);
  uint8_t payloadWidth();
  void readPayload(uint8_t *out, uint8_t len);
  // Records the frame and reports whether it repeats the previous one.
  bool isRepeat(const uint8_t *buf, uint8_t len);
};
