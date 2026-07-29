#pragma once
#include <Arduino.h>
#include "RadioController.h"
#include "Protocol.h"

// Buffers serial input into lines and dispatches the line-based ASCII command
// protocol. Every command is answered with "OK ..." or "ERR ...".
class CommandParser {
public:
  explicit CommandParser(RadioController &radio) : radio_(radio) {}

  // Feed one received serial character. Dispatches on CR, LF or CRLF.
  void feed(char c);

  // Called every loop. Notices a `txseq` whose payload lines stopped arriving
  // and ends it, rather than leaving the dongle waiting for a run that the
  // host has abandoned - in which case every later command would be eaten as
  // if it were a payload.
  void poll();

  // The identity-and-state line: printed once at boot as the greeting, and on
  // demand by `status`. A host that attaches to a dongle already running - or
  // to one whose adapter did not pull DTR - can ask instead of waiting.
  void printStatus();

  // What the serial port is running at right now, so `info` can say so.
  long baud() const { return baud_; }

private:
  static constexpr uint8_t BUF_SIZE = 128;

  RadioController &radio_;
  char buf_[BUF_SIZE];
  uint8_t len_ = 0;
  bool overlong_ = false;   // this line outgrew the buffer; say so at its end

  // A `txseq` in progress: how many payload lines are still expected, how many
  // have been taken, and when the last one arrived.
  static constexpr uint16_t SEQ_IDLE = 0;
  static constexpr uint16_t SEQ_QUIET_MS = 500;   // silence that ends a run
  // Silence that gets the confirmation said again. A host with a full window
  // cannot write another payload until it is confirmed, so a confirmation lost
  // on the wire deadlocks the run until SEQ_QUIET_MS kills it - one damaged
  // byte on the return path costing the whole transfer. Saying the count again
  // costs sixteen bytes and turns that into a hiccup. It carries the running
  // total rather than a tick, so a repeat is harmless and a host that heard the
  // first one simply sees the same number twice.
  static constexpr uint16_t SEQ_NUDGE_MS = 25;
  static constexpr uint16_t SEQ_PROGRESS_EVERY = 32;
  // An acknowledged run is confirmed often, because the host may not write
  // faster than the dongle consumes. Once per frame when the payloads are hex
  // lines, because only three of those fit in the input buffer. Once per three
  // when they are records, because six fit - and a confirmation the host has
  // to wait for costs more than it looks: measured, going from none to one per
  // frame put 0.76 ms on a 0.79 ms frame.
  static constexpr uint16_t SEQ_PROGRESS_ACK_BIN = 3;
  uint16_t seqLeft_ = SEQ_IDLE;
  // Acknowledged runs confirm every frame before writing the next, so the
  // firmware is not reading the port for up to twenty milliseconds at a time.
  // It therefore answers every payload, and the host waits for that answer -
  // without the brake the buffer overruns and the run dies on a payload that
  // was never malformed, which is exactly how it failed the first time.
  bool seqAcking_ = false;
  // A run whose payloads arrive as binary records rather than hex lines. Two
  // characters per byte was half the traffic on a link that had become the
  // constraint; a record is a length, the payload, and a checksum over it. No
  // sync marker is needed and none would help: the parser knows it is owed
  // exactly `seqLeft_` records, and each says its own length.
  bool seqBin_ = false;
  uint16_t seqConf_ = 0;    // confirm every n-th frame; 0 = the default for the mode
  uint8_t binLen_ = 0;      // 0 = the next byte is a length
  uint8_t binGot_ = 0;
  long baud_ = BOOT_BAUD;   // what the port is running at, for `info` to report
  uint16_t seqTaken_ = 0;
  // When the run began, so its total can be reported against the two parts of
  // it the radio accounts for.
  uint32_t seqStartMs_ = 0;
  uint32_t seqLastMs_ = 0;
  uint32_t seqNudgeMs_ = 0;

  void dispatch(char *line);
  void handleHwset(char *args);
  void handleListen(char *args);
  void handleTx(char *args);
  void handleTxSeq(char *args);
  void handleTxTest(char *args);
  void feedSeqPayload(char *line);
  void feedSeqByte(uint8_t b);
  void sayAt(uint16_t n);
  uint16_t confEvery() const;
  void endSeq(const __FlashStringHelper *why);
};
