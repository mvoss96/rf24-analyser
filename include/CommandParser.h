#pragma once
#include <Arduino.h>
#include "RadioController.h"

// Buffers serial input into lines and dispatches the line-based ASCII command
// protocol. Every command is answered with "OK ..." or "ERR ...".
class CommandParser {
public:
  explicit CommandParser(RadioController &radio) : radio_(radio) {}

  // Feed one received serial character. Dispatches on CR, LF or CRLF.
  void feed(char c);

  // The identity-and-state line: printed once at boot as the greeting, and on
  // demand by `status`. A host that attaches to a dongle already running - or
  // to one whose adapter did not pull DTR - can ask instead of waiting.
  void printStatus();

private:
  static constexpr uint8_t BUF_SIZE = 128;

  RadioController &radio_;
  char buf_[BUF_SIZE];
  uint8_t len_ = 0;

  void dispatch(char *line);
  void handleHwset(char *args);
  void handleListen(char *args);
  void handleTx(char *args);
};
