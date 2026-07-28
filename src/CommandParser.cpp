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
  // Before anything looks for a line terminator: a binary payload byte may be
  // any value at all, newline included, and assembling it into lines would
  // corrupt the very first payload that happened to contain one.
  if (seqLeft_ != SEQ_IDLE && seqBin_) { feedSeqByte((uint8_t)c); return; }

  // Accept CR, LF or CRLF as the line terminator, so any terminal works
  // unconfigured (PuTTY sends CR on Enter, miniterm CRLF). The trailing empty
  // line a CRLF produces is dispatched too, but dispatch() ignores it.
  if (c == '\n' || c == '\r') {
    // An overlong line is reported, not quietly repaired. Resetting the buffer
    // mid-line used to turn the tail into a line of its own, which was then
    // dispatched as a command nobody sent - and the answer described that
    // wreckage rather than the length that caused it.
    if (overlong_) {
      err(F("line too long"));
      overlong_ = false;
      if (seqLeft_ != SEQ_IDLE) endSeq(F("line too long"));
    } else {
      buf_[len_] = '\0';
      if (seqLeft_ != SEQ_IDLE) feedSeqPayload(buf_);
      else dispatch(buf_);
    }
    len_ = 0;
  } else if (len_ < BUF_SIZE - 1) {
    buf_[len_++] = c;
  } else {
    overlong_ = true;   // keep reading to the terminator, then say so
  }
}

void CommandParser::poll() {
  if (seqLeft_ == SEQ_IDLE) return;
  if (millis() - seqLastMs_ > SEQ_QUIET_MS) endSeq(F("truncated"));
}

// --- Sending a run of payloads ---------------------------------------------

void CommandParser::handleTxSeq(char *args) {
  if (!radio_.configured()) { err(F("unconfigured - run listen first")); return; }
  if (args == nullptr) { err(F("usage: txseq <addr> <count> [ack|noack]")); return; }

  const RadioConfig &cfg = radio_.config();
  char *save = nullptr;
  char *addrTok = strtok_r(args, " \t", &save);
  if (addrTok == nullptr) { err(F("missing addr")); return; }
  uint8_t addr[MAX_ADDR_WIDTH];
  if (parseHexList(addrTok, addr, 0, MAX_ADDR_WIDTH) != cfg.addrWidth) {
    err(F("addr length must match aw")); return;
  }
  char *countTok = strtok_r(nullptr, " \t", &save);
  if (countTok == nullptr) { err(F("missing count")); return; }
  long count = atol(countTok);
  if (count < 1 || count > 60000) { err(F("count 1..60000")); return; }

  bool noack = true;
  bool binary = false;
  uint16_t conf = 0;
  for (char *tok = strtok_r(nullptr, " \t", &save); tok != nullptr;
       tok = strtok_r(nullptr, " \t", &save)) {
    if (strcmp(tok, "ack") == 0)        noack = false;
    else if (strcmp(tok, "noack") == 0) noack = true;
    else if (strcmp(tok, "bin") == 0)   binary = true;
    else if (strncmp(tok, "conf=", 5) == 0) {
      // How often the dongle confirms, in frames. Exposed because the right
      // value is not derivable - it trades a host round trip against how many
      // payloads may be in flight, and both were measured rather than reasoned.
      long v = atol(tok + 5);
      if (v < 1 || v > 255) { err(F("conf 1..255")); return; }
      conf = (uint16_t)v;
    }
    else { err(F("unknown key")); return; }
  }

  seqLeft_ = (uint16_t)count;
  seqAcking_ = !noack;
  seqBin_ = binary;
  seqConf_ = conf;
  binLen_ = 0;
  seqTaken_ = 0;
  seqLastMs_ = millis();
  radio_.beginSequence(addr, noack);
  // Said before the payloads, so a host knows the dongle is listening for them
  // rather than for commands - and so a human who typed it by hand sees why
  // the next thing they type is not answered.
  Serial.print(F("OK txseq ready count="));
  Serial.print(count);
  // Echoed so a host can tell an accepted `bin` from a firmware that ignored
  // it - and older firmware answers `ERR unknown key`, which is the same
  // question asked the other way round.
  if (binary) Serial.print(F(" bin"));
  Serial.println();
}

// A payload that never crossed the serial line. Everything else in this
// firmware measures the radio and the UART together, because the bytes arrive
// over the UART - and the UART turned out to be the constraint in both
// directions. `txtest` takes the payload from flash instead and stamps a frame
// index into it, so what is left is the radio, the SPI bus and this loop.
//
// Pair it with `format none` at the far end and no UART is in the path at all.
static const uint8_t TEST_PATTERN[32] PROGMEM = {
  0x5A, 0xA5, 0x00, 0x00, 0x0F, 0xF0, 0x33, 0xCC, 0x01, 0x02, 0x04, 0x08,
  0x10, 0x20, 0x40, 0x80, 0xFE, 0xFD, 0xFB, 0xF7, 0xEF, 0xDF, 0xBF, 0x7F,
  0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88
};

void CommandParser::handleTxTest(char *args) {
  if (!radio_.configured()) { err(F("unconfigured - run listen first")); return; }
  if (args == nullptr) { err(F("usage: txtest <addr> <count> [ack|noack] [size=<n>]")); return; }

  const RadioConfig &cfg = radio_.config();
  char *save = nullptr;
  char *addrTok = strtok_r(args, " 	", &save);
  if (addrTok == nullptr) { err(F("missing addr")); return; }
  uint8_t addr[MAX_ADDR_WIDTH];
  if (parseHexList(addrTok, addr, 0, MAX_ADDR_WIDTH) != cfg.addrWidth) {
    err(F("addr length must match aw")); return;
  }
  char *countTok = strtok_r(nullptr, " 	", &save);
  if (countTok == nullptr) { err(F("missing count")); return; }
  long count = atol(countTok);
  if (count < 1 || count > 20000) { err(F("count 1..20000")); return; }

  bool noack = true;
  uint8_t size = 32;
  for (char *tok = strtok_r(nullptr, " 	", &save); tok != nullptr;
       tok = strtok_r(nullptr, " 	", &save)) {
    if (strcmp(tok, "ack") == 0)        noack = false;
    else if (strcmp(tok, "noack") == 0) noack = true;
    else if (strncmp(tok, "size=", 5) == 0) {
      long v = atol(tok + 5);
      if (v < 3 || v > 32) { err(F("size 3..32")); return; }
      size = (uint8_t)v;
    }
    else { err(F("unknown key")); return; }
  }

  uint8_t payload[32];
  for (uint8_t i = 0; i < size; i++) payload[i] = pgm_read_byte(&TEST_PATTERN[i]);

  radio_.beginSequence(addr, noack);
  const uint32_t started = micros();
  long done = 0;
  for (; done < count; done++) {
    // Two bytes of frame index, so the far end can tell the frames apart and
    // a duplicate or a gap is visible rather than assumed.
    payload[1] = (uint8_t)(done >> 8);
    payload[2] = (uint8_t)done;
    if (!radio_.sequenceWrite(payload, size)) break;
  }
  const RadioController::TxResult r = radio_.endSequence();
  const uint32_t took = micros() - started;

  // The firmware's own clock, because the host's includes the round trip that
  // this command exists to measure without.
  Serial.print(F("OK txtest sent="));
  Serial.print(r.sent);
  Serial.print('/');
  Serial.print(count);
  Serial.print(F(" size="));
  Serial.print(size);
  Serial.print(F(" ack="));
  if (!r.acking) Serial.print(r.asked ? F("off") : F("no"));
  else { Serial.print(F("yes failed=")); Serial.print(r.failed);
         Serial.print(F(" retries=")); Serial.print(r.retries); }
  Serial.print(F(" us="));
  Serial.print(took);
  Serial.print(F(" us_per="));
  Serial.println(done ? took / (uint32_t)done : 0);
}

void CommandParser::feedSeqPayload(char *line) {
  seqLastMs_ = millis();
  if (line[0] == '\0') return;              // a CRLF's second terminator
  uint8_t payload[32];
  int n = parseHexList(line, payload, 0, 32);
  if (n < 1) { endSeq(F("bad payload")); return; }

  const bool more = radio_.sequenceWrite(payload, (uint8_t)n);
  seqTaken_++;
  seqLeft_--;
  if (!more) { endSeq(F("gave up")); return; }
  if (seqLeft_ == SEQ_IDLE) { endSeq(nullptr); return; }
  // A progress line every so often: a run of three thousand frames is nearly a
  // minute of silence otherwise, and silence is what a hung dongle looks like.
  if (seqTaken_ % confEvery() == 0) {
    Serial.print(F("OK txseq at="));
    Serial.println(seqTaken_);
  }
}

uint16_t CommandParser::confEvery() const {
  if (seqConf_) return seqConf_;
  if (!seqAcking_) return SEQ_PROGRESS_EVERY;
  return seqBin_ ? SEQ_PROGRESS_ACK_BIN : 1;
}

void CommandParser::feedSeqByte(uint8_t b) {
  seqLastMs_ = millis();
  if (binLen_ == 0) {
    if (b < 1 || b > 32) { endSeq(F("bad length")); return; }
    binLen_ = b;
    binGot_ = 0;
    return;
  }
  if (binGot_ < binLen_) { buf_[binGot_++] = (char)b; return; }

  // The byte after the payload is its checksum. Nothing here can resynchronise
  // on its own - a wrong length would swallow the records behind it - so a
  // failed checksum ends the run rather than transmitting a guess.
  const uint8_t len = binLen_;
  binLen_ = 0;
  if (b != nrf24_crc8((const uint8_t *)buf_, len)) { endSeq(F("bad payload")); return; }

  const bool more = radio_.sequenceWrite((const uint8_t *)buf_, len);
  seqTaken_++;
  seqLeft_--;
  if (!more) { endSeq(F("gave up")); return; }
  if (seqLeft_ == SEQ_IDLE) { endSeq(nullptr); return; }
  if (seqTaken_ % confEvery() == 0) {
    Serial.print(F("OK txseq at="));
    Serial.println(seqTaken_);
  }
}

void CommandParser::endSeq(const __FlashStringHelper *why) {
  const uint16_t asked = seqTaken_ + seqLeft_;
  seqLeft_ = SEQ_IDLE;
  seqBin_ = false;
  binLen_ = 0;
  len_ = 0;               // whatever the byte stream left in the line buffer
  const RadioController::TxResult r = radio_.endSequence();
  Serial.print(F("OK txseq sent="));
  Serial.print(r.sent);
  Serial.print('/');
  Serial.print(asked);
  Serial.print(F(" ack="));
  if (!r.acking) {
    // The same distinction `tx` makes: `no` is a receiver that stayed silent,
    // `off` is a radio that was never going to wait for one. Reporting the
    // second as the first invites a hunt for a receiver that is fine.
    Serial.print(r.asked ? F("off") : F("no"));
  } else {
    Serial.print(F("yes failed="));
    Serial.print(r.failed);
    Serial.print(F(" retries="));
    Serial.print(r.retries);
  }
  if (why != nullptr) {
    Serial.print(F(" stopped="));
    Serial.print(why);
  }
  Serial.println();
}

// --- Command handlers ------------------------------------------------------

void CommandParser::printStatus() {
  Serial.print(F("NRF24ANALYSER fw=" FW_VERSION " api="));
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
  // Not a bare OK: setHardware() may have downgraded an irq pin that cannot
  // interrupt, and it discards the radio configuration. Both are in the answer
  // now, so a host does not have to know either rule to stay in step.
  Serial.print(F("OK hw connected saved"));
  radio_.printAck();
}

void CommandParser::handleListen(char *args) {
  if (!radio_.hwReady()) { err(F("no hardware - run hwset first")); return; }
  // Scanning retunes the radio across the band, so the two cannot coexist.
  // Asking to listen is unambiguous about which one is wanted.
  radio_.stopScan();

  if (args == nullptr) {
    if (!radio_.configured()) { reportMissing(0); return; }
    radio_.startListening();
    // Resuming is exactly where the caller does not know what it resumed with,
    // so this is the acknowledgement that has to say.
    Serial.print(F("OK listening"));
    radio_.printAck();
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
    } else if (strcmp(tok, "retries") == 0) {
      // retries=<ard_us>,<arc> - optional, so a listen line that never mentions
      // it keeps the values the radio already had.
      char *comma = strchr(v, ',');
      if (comma == nullptr) { err(F("retries=<ard_us>,<count>")); return; }
      *comma = '\0';
      int ard = atoi(v), arc = atoi(comma + 1);
      if (ard < 250 || ard > 4000 || ard % 250) { err(F("ard 250..4000 in steps of 250")); return; }
      if (arc < 0 || arc > 15) { err(F("retry count 0..15")); return; }
      c.ardUs = (uint16_t)ard; c.arc = (uint8_t)arc;
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
  // Read back off the chip, not echoed from the request: the answer is only
  // worth having if it can differ from what was asked for.
  Serial.print(F("OK listening"));
  radio_.printAck();
}

void CommandParser::handleTx(char *args) {
  if (!radio_.configured()) { err(F("unconfigured - run listen first")); return; }
  if (args == nullptr) { err(F("usage: tx <addr> <hex...> [ack|noack] [x<n>] [gap=<ms>]")); return; }

  const RadioConfig &cfg = radio_.config();
  char *save = nullptr;

  char *addrTok = strtok_r(args, " \t", &save);
  if (addrTok == nullptr) { err(F("missing addr")); return; }
  uint8_t addr[MAX_ADDR_WIDTH];
  int an = parseHexList(addrTok, addr, 0, MAX_ADDR_WIDTH);
  if (an != cfg.addrWidth) { err(F("addr length must match aw")); return; }

  uint8_t payload[32];
  uint8_t plen = 0;
  bool noack = true;  // default: emulate a broadcast sender
  uint8_t count = 1;  // x<n>: copies of the frame, back to back like a sender's repeats
  uint16_t gapMs = 0; // gap=<ms>: pause between the copies
  for (char *tok = strtok_r(nullptr, " \t", &save); tok != nullptr;
       tok = strtok_r(nullptr, " \t", &save)) {
    if (strcmp(tok, "ack") == 0)   { noack = false; continue; }
    if (strcmp(tok, "noack") == 0) { noack = true;  continue; }
    if (tok[0] == 'x' && tok[1] >= '0' && tok[1] <= '9') {
      int x = atoi(tok + 1);
      if (x < 1 || x > 16) { err(F("x 1..16")); return; }
      count = (uint8_t)x;
      continue;
    }
    if (strncmp(tok, "gap=", 4) == 0) {
      int x = atoi(tok + 4);
      if (x < 0 || x > 250) { err(F("gap 0..250 ms")); return; }
      gapMs = (uint16_t)x;
      continue;
    }
    int n = parseHexList(tok, payload, plen, 32);
    if (n < 0) { err(F("bad payload byte")); return; }
    plen = (uint8_t)n;
  }
  if (plen == 0) { err(F("empty payload")); return; }

  const RadioController::TxResult r =
      radio_.transmit(addr, payload, plen, noack, count, gapMs);
  Serial.print(F("OK tx sent="));
  Serial.print(r.sent);
  // The single-frame reply keeps its exact historic shape; only a burst gets
  // the /n suffix, so an old host parsing "sent=1" never sees anything new.
  if (count > 1) {
    Serial.print('/');
    Serial.print(count);
  }
  // Three states, not two. `yes` means the receiver acknowledged; `no` means
  // none was asked for; `off` means one was asked for and the radio was never
  // going to wait for it, because auto-ack is disabled in the configuration.
  // That last case used to report `yes` and read as proof of delivery to an
  // address nobody was listening on.
  Serial.print(F(" ack="));
  if (noack) {
    Serial.print(F("no"));
  } else if (!r.acking) {
    Serial.print(F("off"));
  } else {
    Serial.print(F("yes"));
    Serial.print(F(" failed="));
    Serial.print(r.failed);
    Serial.print(F(" retries="));
    Serial.print(r.retries);
  }
  Serial.println();
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
    radio_.stopScan();     // "stop" means stop, whichever mode is running
    radio_.stopListening();
    Serial.println(F("OK stopped"));
  } else if (strcmp(cmd, "status") == 0) {
    printStatus();
  } else if (strcmp(cmd, "info") == 0) {
    radio_.printInfo(baud_);
  } else if (strcmp(cmd, "scan") == 0) {
    // Deliberately does not require a radio configuration: which channels are
    // busy is exactly what you want to know *before* choosing one.
    if (!radio_.hwReady()) { err(F("no hardware - run hwset first")); return; }
    if (rest != nullptr && strcmp(rest, "off") == 0) {
      radio_.stopScan();
      Serial.println(F("OK scan stopped"));
    } else if (rest != nullptr && strncmp(rest, "live", 4) == 0) {
      int v = atoi(rest + 4);   // "live" or "live <passes per report>"
      radio_.startScan((v <= 0) ? 8 : (uint16_t)v);
      Serial.println(F("OK scan live"));
    } else {
      if (radio_.scanning()) { err(F("scan live is running - scan off first")); return; }
      int v = rest ? atoi(rest) : 0;
      radio_.scan((v <= 0) ? 64 : (uint16_t)v);
    }
  } else if (strcmp(cmd, "tx") == 0) {
    handleTx(rest);
  } else if (strcmp(cmd, "txseq") == 0) {
    handleTxSeq(rest);
  } else if (strcmp(cmd, "repeats") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("repeats 0|1")); return; }
    radio_.setShowRepeats(v == 1);
    ok();
  } else if (strcmp(cmd, "rxmode") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 0 || v > 4) {
      err(F("rxmode 0..4 (width|full|full+flush|no-width|width-after)"));
      return;
    }
    radio_.setRxMode((uint8_t)v);
    ok();
  } else if (strcmp(cmd, "baud") == 0) {
    // Raised for a session, never stored. The reply goes out at the old rate
    // and is flushed before the switch, so the host knows exactly when to
    // follow - and a reset, or simply unplugging it, returns the dongle to a
    // rate anything can open.
    long v = rest ? atol(rest) : 0;
    if (v != 250000L && v != 500000L && v != 1000000L && v != 2000000L) {
      err(F("baud 250000|500000|1000000|2000000")); return;
    }
    Serial.print(F("OK baud="));
    Serial.println(v);
    Serial.flush();
    Serial.end();
    Serial.begin(v);
    baud_ = v;
  } else if (strcmp(cmd, "txtest") == 0) {
    handleTxTest(rest);
  } else if (strcmp(cmd, "format") == 0) {
    // Answered in ASCII either way, including the one that switches to binary:
    // the reply belongs to the command stream, which stays readable.
    if (rest == nullptr) { err(F("format bin|text|none")); return; }
    if (strcmp(rest, "bin") == 0)       radio_.setOutMode(RadioController::OUT_BIN);
    else if (strcmp(rest, "text") == 0) radio_.setOutMode(RadioController::OUT_TEXT);
    else if (strcmp(rest, "none") == 0) radio_.setOutMode(RadioController::OUT_NONE);
    else { err(F("format bin|text|none")); return; }
    Serial.print(F("OK format="));
    Serial.println(radio_.outMode() == RadioController::OUT_BIN ? F("bin")
                   : radio_.outMode() == RadioController::OUT_NONE ? F("none")
                   : F("text"));
  } else if (strcmp(cmd, "rxdbg") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("rxdbg 0|1")); return; }
    radio_.setRxDbg(v == 1);
    ok();
  } else if (strcmp(cmd, "regs") == 0) {
    if (!radio_.hwReady()) { err(F("no hardware - run hwset first")); return; }
    radio_.printRegs();
  } else if (strcmp(cmd, "reg") == 0) {
    // Deliberately unguarded: the point of it is to put the chip into states the
    // configuration path cannot express - a listening receiver with auto-ack on
    // one pipe, say - and see which of them stop the duplicates. A `listen`
    // afterwards puts the configured values back.
    if (!radio_.hwReady()) { err(F("no hardware - run hwset first")); return; }
    char *addr = rest ? strtok_r(rest, " \t", &save) : nullptr;
    if (addr == nullptr) { err(F("usage: reg <addr-hex> [value-hex]")); return; }
    char *value = strtok_r(nullptr, " \t", &save);
    long a = strtol(addr, nullptr, 16);
    if (a < 0 || a > 0x1F) { err(F("addr must be 00..1F")); return; }
    if (value != nullptr) {
      long v = strtol(value, nullptr, 16);
      if (v < 0 || v > 0xFF) { err(F("value must be 00..FF")); return; }
      radio_.regPoke((uint8_t)a, (uint8_t)v);
    }
    Serial.print(F("OK reg "));
    Serial.print((uint8_t)a, HEX);
    Serial.print('=');
    Serial.println(radio_.regPeek((uint8_t)a), HEX);
  } else if (strcmp(cmd, "help") == 0) {
    Serial.println(F("hwset ce=<pin> csn=<pin> [irq=<pin|none>] [led_rx=<pin|none>] [led_tx=<pin|none>]"));
    Serial.println(F("listen ch= rate= crc= aw= pa= ack= dpl= [plsize=] pipeN=<addr>"));
    Serial.println(F("hwclear | listen | stop | info | scan [passes] | repeats <0|1>"));
    Serial.println(F("txseq <addr> <count> [ack|noack] [bin] then <count> payloads"));
    Serial.println(F("  bin: raw records len+payload+crc8 instead of hex lines"));
    Serial.println(F("tx <addr> <hex...> [ack|noack] [x<n>] [gap=<ms>]"));
    Serial.println(F("format bin|text|none  (bin: binary records; none: count only)"));
    Serial.println(F("txtest <addr> <count> [ack|noack] [size=<n>]  (payload from flash)"));
    Serial.println(F("baud 250000|500000|1000000|2000000  (for this session; reset restores)"));
    Serial.println(F("rxmode <0|1|2> | rxdbg <0|1> | regs | reg <addr> [val]  (diagnosis)"));
    ok();
  } else {
    err(F("unknown cmd (try help)"));
  }
}
