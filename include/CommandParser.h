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
  static constexpr uint16_t SEQ_PROGRESS_EVERY = 32;
  uint16_t seqLeft_ = SEQ_IDLE;
  // Acknowledged runs confirm every frame before writing the next, so the
  // firmware is not reading the port for up to twenty milliseconds at a time.
  // It therefore answers every payload, and the host waits for that answer -
  // without the brake the buffer overruns and the run dies on a payload that
  // was never malformed, which is exactly how it failed the first time.
  bool seqAcking_ = false;
  long baud_ = BOOT_BAUD;   // what the port is running at, for `info` to report
  uint16_t seqTaken_ = 0;
  uint32_t seqLastMs_ = 0;

  void dispatch(char *line);
  void handleHwset(char *args);
  void handleListen(char *args);
  void handleTx(char *args);
  void handleTxSeq(char *args);
  void feedSeqPayload(char *line);
  void endSeq(const __FlashStringHelper *why);
};
