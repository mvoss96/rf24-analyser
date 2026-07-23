#include "CommandParser.h"

// --- Small parsing helpers -------------------------------------------------

// Returns 0..15 for a hex digit, or 0xFF if not a hex digit.
static uint8_t hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return 0xFF;
}

// Parses a two-char hex byte "XX". Returns -1 on error.
static int parseByte(const char *tok) {
  if (tok == nullptr || tok[0] == '\0' || tok[1] == '\0' || tok[2] != '\0') return -1;
  uint8_t hi = hexNibble(tok[0]);
  uint8_t lo = hexNibble(tok[1]);
  if (hi == 0xFF || lo == 0xFF) return -1;
  return (hi << 4) | lo;
}

// Parses hex bytes separated by ':' ',' or '.' from a mutable string, appending
// to out starting at `start` (up to maxLen). Returns new count, or -1 on error.
static int parseHexList(char *s, uint8_t *out, uint8_t start, uint8_t maxLen) {
  int count = start;
  for (char *tok = strtok(s, ":,."); tok != nullptr; tok = strtok(nullptr, ":,.")) {
    if (count >= maxLen) return -1;
    int b = parseByte(tok);
    if (b < 0) return -1;
    out[count++] = (uint8_t)b;
  }
  return count;
}

static void ok()  { Serial.println(F("OK")); }
static void err(const __FlashStringHelper *m) { Serial.print(F("ERR ")); Serial.println(m); }

// --- Command handlers ------------------------------------------------------

void CommandParser::feed(char c) {
  if (c == '\r') return;
  if (c == '\n') {
    buf_[len_] = '\0';
    dispatch(buf_);
    len_ = 0;
  } else if (len_ < BUF_SIZE - 1) {
    buf_[len_++] = c;
  } else {
    len_ = 0; // overlong line: reset to stay in sync
  }
}

void CommandParser::handleTx(char *args) {
  if (args == nullptr) { err(F("usage: tx <addr> <hex...> [ack|noack]")); return; }

  RadioConfig &cfg = radio_.config();

  char *addrTok = strtok(args, " \t");
  if (addrTok == nullptr) { err(F("missing addr")); return; }
  uint8_t addr[MAX_ADDR_WIDTH];
  int an = parseHexList(addrTok, addr, 0, cfg.addrWidth);
  if (an != cfg.addrWidth) { err(F("bad addr width")); return; }

  uint8_t payload[32];
  uint8_t plen = 0;
  bool noack = true; // default: emulate the broadcast sender
  for (char *tok = strtok(nullptr, " \t"); tok != nullptr; tok = strtok(nullptr, " \t")) {
    if (strcmp(tok, "ack") == 0)   { noack = false; continue; }
    if (strcmp(tok, "noack") == 0) { noack = true;  continue; }
    int n = parseHexList(tok, payload, plen, 32);
    if (n < 0) { err(F("bad payload byte")); return; }
    plen = (uint8_t)n;
  }
  if (plen == 0) { err(F("empty payload")); return; }

  bool sent = radio_.transmit(addr, payload, plen, noack);
  Serial.print(F("OK tx sent="));
  Serial.print(sent ? 1 : 0);
  Serial.print(F(" ack="));
  Serial.println(noack ? F("no") : F("yes"));
}

void CommandParser::handlePipe(char *args) {
  RadioConfig &cfg = radio_.config();
  char *nTok = args ? strtok(args, " \t") : nullptr;
  char *aTok = nTok ? strtok(nullptr, " \t") : nullptr;
  int p = nTok ? atoi(nTok) : -1;
  if (p < 0 || p > 5 || aTok == nullptr) { err(F("usage: pipe <0-5> <addr|off>")); return; }

  if (strcmp(aTok, "off") == 0) {
    cfg.pipeEn[p] = false;
    radio_.reconfigure();
    ok();
    return;
  }
  uint8_t tmp[MAX_ADDR_WIDTH];
  int n = parseHexList(aTok, tmp, 0, cfg.addrWidth);
  if (n != cfg.addrWidth) { err(F("addr must match aw")); return; }
  memcpy(cfg.pipeAddr[p], tmp, cfg.addrWidth);
  cfg.pipeEn[p] = true;
  radio_.reconfigure();
  ok();
}

void CommandParser::dispatch(char *line) {
  char *cmd = strtok(line, " \t");
  if (cmd == nullptr) return; // empty line

  char *rest = strtok(nullptr, ""); // remainder of the line, or nullptr
  RadioConfig &cfg = radio_.config();

  if (strcmp(cmd, "info") == 0) {
    radio_.printInfo();
  } else if (strcmp(cmd, "listen") == 0) {
    radio_.startListening();
    Serial.println(F("OK listening"));
  } else if (strcmp(cmd, "stop") == 0) {
    radio_.stopListening();
    Serial.println(F("OK stopped"));
  } else if (strcmp(cmd, "ch") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 0 || v > 125) { err(F("ch 0..125")); return; }
    cfg.channel = (uint8_t)v; radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "rate") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 250 && v != 1000 && v != 2000) { err(F("rate 250|1000|2000")); return; }
    cfg.rateKbps = (uint16_t)v; radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "crc") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 8 && v != 16) { err(F("crc 0|8|16")); return; }
    cfg.crcBits = (uint8_t)v; radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "aw") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 3 || v > 5) { err(F("aw 3|4|5")); return; }
    cfg.addrWidth = (uint8_t)v; radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "ack") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("ack 0|1")); return; }
    cfg.autoAck = (v == 1); radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "dpl") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("dpl 0|1")); return; }
    cfg.dpl = (v == 1); radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "plsize") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 1 || v > 32) { err(F("plsize 1..32")); return; }
    cfg.plSize = (uint8_t)v; radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "pa") == 0) {
    if (rest == nullptr)                cfg.paLevel = 255;
    else if (strcmp(rest, "min")  == 0) cfg.paLevel = 0;
    else if (strcmp(rest, "low")  == 0) cfg.paLevel = 1;
    else if (strcmp(rest, "high") == 0) cfg.paLevel = 2;
    else if (strcmp(rest, "max")  == 0) cfg.paLevel = 3;
    else                                cfg.paLevel = 255;
    if (cfg.paLevel == 255) { cfg.paLevel = 1; err(F("pa min|low|high|max")); return; }
    radio_.reconfigure(); ok();
  } else if (strcmp(cmd, "pipe") == 0) {
    handlePipe(rest);
  } else if (strcmp(cmd, "tx") == 0) {
    handleTx(rest);
  } else if (strcmp(cmd, "scan") == 0) {
    int v = rest ? atoi(rest) : 0;
    uint16_t passes = (v <= 0) ? 64 : (uint16_t)v;
    radio_.scan(passes);
  } else if (strcmp(cmd, "help") == 0) {
    Serial.println(F("cmds: ch rate crc aw pipe ack dpl plsize pa listen stop info tx scan help"));
    ok();
  } else {
    err(F("unknown cmd (try help)"));
  }
}
