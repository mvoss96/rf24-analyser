#include "CommandParser.h"
#include "HwStore.h"
#include "Protocol.h"

// --- Small parsing helpers -------------------------------------------------

// Returns 0..15 for a hex digit, or 0xFF if not a hex digit.
static uint8_t hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return 0xFF;
}

// Parses hex bytes into out[start..], returning the new count or -1 on error.
// Accepts both the compact form the RX output uses ("4D565202") and separated
// forms ("4D:56:52:02"), so a captured payload can be pasted straight back into
// a tx command. Uses no tokenizer state at all, so it is safe to call from
// inside any parsing loop.
static int parseHexList(const char *s, uint8_t *out, uint8_t start, uint8_t maxLen) {
  int count = start;
  uint8_t hi = 0xFF; // pending high nibble, 0xFF = none
  for (const char *p = s; *p; ++p) {
    if (*p == ':' || *p == ',' || *p == '.') {
      if (hi != 0xFF) return -1; // separator in the middle of a byte
      continue;
    }
    uint8_t n = hexNibble(*p);
    if (n == 0xFF) return -1;
    if (hi == 0xFF) {
      hi = n;
    } else {
      if (count >= maxLen) return -1;
      out[count++] = (uint8_t)((hi << 4) | n);
      hi = 0xFF;
    }
  }
  if (hi != 0xFF) return -1; // trailing half byte
  return count;
}

// Parses a pin: a plain number, "A0".."A7", or "none". Returns NO_PIN for
// "none" and -1 if the value is not a pin at all.
static int parsePin(const char *v) {
  if (strcmp(v, "none") == 0) return NO_PIN;
  if ((v[0] == 'A' || v[0] == 'a') && v[1] >= '0' && v[1] <= '7' && v[2] == '\0') {
    return A0 + (v[1] - '0');
  }
  if (v[0] == '\0') return -1;
  for (const char *p = v; *p; ++p) {
    if (*p < '0' || *p > '9') return -1;
  }
  int n = atoi(v);
  if (n < 0 || n >= NO_PIN) return -1;
  return n;
}

// Splits "key=value" in place. Returns the value, or nullptr if there is no '='.
static char *splitKeyValue(char *tok) {
  char *eq = strchr(tok, '=');
  if (eq == nullptr) return nullptr;
  *eq = '\0';
  return eq + 1;
}

static void ok() { Serial.println(F("OK")); }
static void err(const __FlashStringHelper *m) { Serial.print(F("ERR ")); Serial.println(m); }

// Mandatory `listen` keys, tracked as a bitmask.
enum : uint8_t {
  K_CH   = 1 << 0,
  K_RATE = 1 << 1,
  K_CRC  = 1 << 2,
  K_AW   = 1 << 3,
  K_PA   = 1 << 4,
  K_ACK  = 1 << 5,
  K_DPL  = 1 << 6,
  K_PIPE = 1 << 7,
};
static constexpr uint8_t K_ALL = K_CH | K_RATE | K_CRC | K_AW | K_PA | K_ACK | K_DPL | K_PIPE;

static void reportMissing(uint8_t have) {
  Serial.print(F("ERR missing:"));
  if (!(have & K_CH))   Serial.print(F(" ch"));
  if (!(have & K_RATE)) Serial.print(F(" rate"));
  if (!(have & K_CRC))  Serial.print(F(" crc"));
  if (!(have & K_AW))   Serial.print(F(" aw"));
  if (!(have & K_PA))   Serial.print(F(" pa"));
  if (!(have & K_ACK))  Serial.print(F(" ack"));
  if (!(have & K_DPL))  Serial.print(F(" dpl"));
  if (!(have & K_PIPE)) Serial.print(F(" pipeN"));
  Serial.println();
}

// --- Line assembly ---------------------------------------------------------

void CommandParser::feed(char c) {
  // Accept CR, LF or CRLF as the line terminator, so any terminal works
  // unconfigured (PuTTY sends CR on Enter, miniterm CRLF). The trailing empty
  // line a CRLF produces is dispatched too, but dispatch() ignores it.
  if (c == '\n' || c == '\r') {
    buf_[len_] = '\0';
    dispatch(buf_);
    len_ = 0;
  } else if (len_ < BUF_SIZE - 1) {
    buf_[len_++] = c;
  } else {
    len_ = 0; // overlong line: reset to stay in sync
  }
}

// --- Command handlers ------------------------------------------------------

void CommandParser::printStatus() {
  Serial.print(F("NRF24SNIFFER fw=" FW_VERSION " api="));
  Serial.print(API_VERSION);
  Serial.print(F(" state="));
  Serial.print(radio_.stateName());
  Serial.print(F(" hw="));
  Serial.print(radio_.hwStateName());
  // Spell the wiring out rather than just its provenance: a stored-but-wrong
  // pin is otherwise invisible, and a wrong CE cannot be detected electrically
  // (isChipConnected() exercises SPI only). Seeing the pins is the check.
  if (radio_.hw().ce != NO_PIN || radio_.hw().csn != NO_PIN) {
    Serial.print(' ');
    radio_.printWiring();
  }
  Serial.print(F(" t="));
  Serial.print(millis());
  Serial.print(F(" rx="));
  Serial.print(radio_.rxCount());
  Serial.print(F(" fifofull="));
  Serial.print(radio_.fifoFullCount());
  Serial.println();
}

void CommandParser::handleHwset(char *args) {
  if (radio_.listening()) { err(F("stop first")); return; }
  if (args == nullptr) {
    err(F("usage: hwset ce=<pin> csn=<pin> [irq=<pin|none>] [led_rx=<pin|none>] [led_tx=<pin|none>]"));
    return;
  }

  HwConfig hw;
  bool haveCe = false, haveCsn = false;
  char *save = nullptr;
  for (char *tok = strtok_r(args, " \t", &save); tok != nullptr;
       tok = strtok_r(nullptr, " \t", &save)) {
    char *v = splitKeyValue(tok);
    if (v == nullptr) { err(F("expected key=value")); return; }
    int p = parsePin(v);
    if (p < 0) { err(F("bad pin value")); return; }

    if (strcmp(tok, "ce") == 0) {
      if (p == NO_PIN) { err(F("ce cannot be none")); return; }
      hw.ce = (uint8_t)p; haveCe = true;
    } else if (strcmp(tok, "csn") == 0) {
      if (p == NO_PIN) { err(F("csn cannot be none")); return; }
      hw.csn = (uint8_t)p; haveCsn = true;
    } else if (strcmp(tok, "irq") == 0) {
      hw.irq = (uint8_t)p;
    } else if (strcmp(tok, "led_rx") == 0) {
      hw.ledRx = (uint8_t)p;
    } else if (strcmp(tok, "led_tx") == 0) {
      hw.ledTx = (uint8_t)p;
    } else {
      err(F("unknown key")); return;
    }
  }
  if (!haveCe || !haveCsn) {
    Serial.print(F("ERR missing:"));
    if (!haveCe) Serial.print(F(" ce"));
    if (!haveCsn) Serial.print(F(" csn"));
    Serial.println();
    return;
  }

  // Two roles on one pin is always a wiring mistake - they would drive each
  // other (an LED write on the CE line, say). Catch it before touching the chip.
  {
    const uint8_t assigned[5] = {hw.ce, hw.csn, hw.irq, hw.ledRx, hw.ledTx};
    for (uint8_t i = 0; i < 5; i++) {
      if (assigned[i] == NO_PIN) continue;
      for (uint8_t j = i + 1; j < 5; j++) {
        if (assigned[i] == assigned[j]) {
          Serial.print(F("ERR pin "));
          Serial.print(assigned[i]);
          Serial.println(F(" assigned twice"));
          return;
        }
      }
    }
  }

  // setHardware() emits its own WARN if the IRQ pin cannot interrupt.
  if (!radio_.setHardware(hw)) {
    err(F("chip not responding - check ce/csn wiring and power"));
    return;
  }
  // SPI works, but that says nothing about CE - prove it before accepting.
  if (!radio_.selfTestCe()) {
    radio_.invalidateHw();
    err(F("ce pin does not key the radio (spi ok) - check the ce wiring"));
    return;
  }
  // Persist what is actually in use (setHardware may have downgraded irq), so
  // the dongle comes back on the same wiring after a reset.
  HwStore::save(radio_.hw());
  Serial.println(F("OK hw connected saved"));
}

void CommandParser::handleListen(char *args) {
  if (!radio_.hwReady()) { err(F("no hardware - run hwset first")); return; }

  if (args == nullptr) {
    if (!radio_.configured()) { reportMissing(0); return; }
    radio_.startListening();
    Serial.println(F("OK listening"));
    return;
  }

  RadioConfig c;
  uint8_t have = 0;
  bool havePlsize = false;
  uint8_t pipeLen[6] = {0};

  char *save = nullptr;
  for (char *tok = strtok_r(args, " \t", &save); tok != nullptr;
       tok = strtok_r(nullptr, " \t", &save)) {
    char *v = splitKeyValue(tok);
    if (v == nullptr) { err(F("expected key=value")); return; }

    if (strcmp(tok, "ch") == 0) {
      int x = atoi(v); if (x < 0 || x > 125) { err(F("ch 0..125")); return; }
      c.channel = (uint8_t)x; have |= K_CH;
    } else if (strcmp(tok, "rate") == 0) {
      int x = atoi(v); if (x != 250 && x != 1000 && x != 2000) { err(F("rate 250|1000|2000")); return; }
      c.rateKbps = (uint16_t)x; have |= K_RATE;
    } else if (strcmp(tok, "crc") == 0) {
      int x = atoi(v); if (x != 0 && x != 8 && x != 16) { err(F("crc 0|8|16")); return; }
      c.crcBits = (uint8_t)x; have |= K_CRC;
    } else if (strcmp(tok, "aw") == 0) {
      int x = atoi(v); if (x < 3 || x > 5) { err(F("aw 3|4|5")); return; }
      c.addrWidth = (uint8_t)x; have |= K_AW;
    } else if (strcmp(tok, "ack") == 0) {
      int x = atoi(v); if (x != 0 && x != 1) { err(F("ack 0|1")); return; }
      c.autoAck = (x == 1); have |= K_ACK;
    } else if (strcmp(tok, "dpl") == 0) {
      int x = atoi(v); if (x != 0 && x != 1) { err(F("dpl 0|1")); return; }
      c.dpl = (x == 1); have |= K_DPL;
    } else if (strcmp(tok, "plsize") == 0) {
      int x = atoi(v); if (x < 1 || x > 32) { err(F("plsize 1..32")); return; }
      c.plSize = (uint8_t)x; havePlsize = true;
    } else if (strcmp(tok, "pa") == 0) {
      if (strcmp(v, "min") == 0)       c.paLevel = 0;
      else if (strcmp(v, "low") == 0)  c.paLevel = 1;
      else if (strcmp(v, "high") == 0) c.paLevel = 2;
      else if (strcmp(v, "max") == 0)  c.paLevel = 3;
      else { err(F("pa min|low|high|max")); return; }
      have |= K_PA;
    } else if (strncmp(tok, "pipe", 4) == 0 && tok[4] >= '0' && tok[4] <= '5' && tok[5] == '\0') {
      uint8_t p = tok[4] - '0';
      int n = parseHexList(v, c.pipeAddr[p], 0, MAX_ADDR_WIDTH);
      if (n < 1) { err(F("bad pipe address")); return; }
      pipeLen[p] = (uint8_t)n;
      c.pipeEn[p] = true;
      have |= K_PIPE;
    } else {
      err(F("unknown key")); return;
    }
  }

  if (have != K_ALL) { reportMissing(have); return; }
  if (!c.dpl && !havePlsize) { err(F("missing: plsize (required when dpl=0)")); return; }
  // Pipes 2-5 have exactly one byte of their own; the radio takes the rest of
  // their address from pipe 1. Demanding a full address there would have the
  // host inventing bytes that the chip then ignores.
  for (uint8_t p = 0; p < 6; p++) {
    if (!c.pipeEn[p]) continue;
    uint8_t need = (p >= 2) ? 1 : c.addrWidth;
    if (pipeLen[p] != need) {
      Serial.print(F("ERR pipe "));
      Serial.print(p);
      Serial.print(F(" takes "));
      Serial.print(need);
      if (p >= 2) Serial.println(F(" byte - pipes 2-5 share the rest with pipe 1"));
      else        Serial.println(F(" bytes, one per aw"));
      return;
    }
    if (p >= 2 && !c.pipeEn[1]) {
      Serial.print(F("ERR pipe "));
      Serial.print(p);
      Serial.println(F(" needs pipe1 - the rest of its address comes from there"));
      return;
    }
  }

  radio_.applyConfig(c);
  radio_.startListening();
  Serial.println(F("OK listening"));
}

void CommandParser::handleTx(char *args) {
  if (!radio_.configured()) { err(F("unconfigured - run listen first")); return; }
  if (args == nullptr) { err(F("usage: tx <addr> <hex...> [ack|noack]")); return; }

  const RadioConfig &cfg = radio_.config();
  char *save = nullptr;

  char *addrTok = strtok_r(args, " \t", &save);
  if (addrTok == nullptr) { err(F("missing addr")); return; }
  uint8_t addr[MAX_ADDR_WIDTH];
  int an = parseHexList(addrTok, addr, 0, MAX_ADDR_WIDTH);
  if (an != cfg.addrWidth) { err(F("addr length must match aw")); return; }

  uint8_t payload[32];
  uint8_t plen = 0;
  bool noack = true; // default: emulate a broadcast sender
  for (char *tok = strtok_r(nullptr, " \t", &save); tok != nullptr;
       tok = strtok_r(nullptr, " \t", &save)) {
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

void CommandParser::dispatch(char *line) {
  char *save = nullptr;
  char *cmd = strtok_r(line, " \t", &save);
  if (cmd == nullptr) return; // empty line

  char *rest = strtok_r(nullptr, "", &save); // remainder, or nullptr

  if (strcmp(cmd, "hwset") == 0) {
    handleHwset(rest);
  } else if (strcmp(cmd, "hwclear") == 0) {
    HwStore::clear();
    Serial.println(F("OK hw cleared (takes effect on reset)"));
  } else if (strcmp(cmd, "listen") == 0) {
    handleListen(rest);
  } else if (strcmp(cmd, "stop") == 0) {
    if (!radio_.hwReady()) { err(F("no hardware - run hwset first")); return; }
    radio_.stopListening();
    Serial.println(F("OK stopped"));
  } else if (strcmp(cmd, "status") == 0) {
    printStatus();
  } else if (strcmp(cmd, "info") == 0) {
    radio_.printInfo();
  } else if (strcmp(cmd, "scan") == 0) {
    if (!radio_.configured()) { err(F("unconfigured - run listen first")); return; }
    int v = rest ? atoi(rest) : 0;
    radio_.scan((v <= 0) ? 64 : (uint16_t)v);
  } else if (strcmp(cmd, "tx") == 0) {
    handleTx(rest);
  } else if (strcmp(cmd, "repeats") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("repeats 0|1")); return; }
    radio_.setShowRepeats(v == 1);
    ok();
  } else if (strcmp(cmd, "help") == 0) {
    Serial.println(F("hwset ce=<pin> csn=<pin> [irq=<pin|none>] [led_rx=<pin|none>] [led_tx=<pin|none>]"));
    Serial.println(F("listen ch= rate= crc= aw= pa= ack= dpl= [plsize=] pipeN=<addr>"));
    Serial.println(F("hwclear | listen | stop | info | scan [passes] | repeats <0|1>"));
    Serial.println(F("tx <addr> <hex...> [ack|noack]"));
    ok();
  } else {
    err(F("unknown cmd (try help)"));
  }
}
