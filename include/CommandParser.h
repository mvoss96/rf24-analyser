#pragma once
#include <Arduino.h>
#include "RadioController.h"

// Buffers serial input into lines and dispatches the line-based ASCII command
// protocol. Every command is answered with "OK ..." or "ERR ...".
class CommandParser {
public:
  explicit CommandParser(RadioController &radio) : radio_(radio) {}

  // Feed one received serial character. Dispatches on newline.
  void feed(char c);

private:
  static constexpr uint8_t BUF_SIZE = 96;

  RadioController &radio_;
  char buf_[BUF_SIZE];
  uint8_t len_ = 0;

  void dispatch(char *line);
  void handleTx(char *args);
  void handlePipe(char *args);
};
