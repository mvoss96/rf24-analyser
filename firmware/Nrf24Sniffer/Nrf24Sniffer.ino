/*
 * Nrf24Sniffer - RF24-based nRF24L01 debug / sniffer firmware
 *
 * Target: USB dongle with ATmega328P + CH340 + nRF24L01 (from the legacy
 * "nrf24USB" project). Pinout is inherited from that board; the radio stack is
 * rewritten on top of the RF24 library (TMRh20) so raw 5-byte-address traffic
 * can be observed and emulated - the old NRFLite firmware could not do this.
 *
 * A line-based ASCII protocol over serial (115200 baud) configures the radio,
 * dumps received frames, and transmits arbitrary payloads. See README.md for
 * the full command reference. Every command is answered with "OK ..." or
 * "ERR ...". Received frames are printed as:
 *
 *     RX p<pipe> len=<n> <b0> <b1> ... <bn-1>
 *
 * Default configuration matches the BTHome-over-nRF24 target protocol:
 * channel 100, 250 kbps, CRC16, 5-byte address, pipe 1 = "BTHME", dynamic
 * payloads on, auto-ack off, PA level LOW.
 */

#include <SPI.h>
#include <RF24.h>

// --- Pin map (inherited from the legacy nrf24USB board) --------------------
static const uint8_t PIN_RADIO_CE  = 9;
static const uint8_t PIN_RADIO_CSN = 10;
static const uint8_t PIN_RADIO_IRQ = 2;
static const uint8_t PIN_LED_TX    = A1; // activity on transmit
static const uint8_t PIN_LED_RX    = 8;  // activity on receive
// LEDs are wired active-low on this board (LOW = lit).
static const uint8_t LED_ON  = LOW;
static const uint8_t LED_OFF = HIGH;

static const unsigned long SERIAL_SPEED = 115200;
static const uint8_t MAX_ADDR_WIDTH = 5;
static const uint8_t LINE_BUF_SIZE  = 96;

RF24 radio(PIN_RADIO_CE, PIN_RADIO_CSN);

// --- Radio configuration state ---------------------------------------------
struct Config {
  uint8_t  channel   = 100;
  uint16_t rateKbps  = 250;   // 250 | 1000 | 2000
  uint8_t  crcBits   = 16;    // 0 | 8 | 16
  uint8_t  addrWidth = 5;     // 3 | 4 | 5
  bool     autoAck   = false;
  bool     dpl       = true;  // dynamic payloads
  uint8_t  plSize    = 32;    // static payload size when dpl == false
  uint8_t  paLevel   = 1;     // 0=min 1=low 2=high 3=max
  bool     pipeEn[6] = { false, true, false, false, false, false };
  uint8_t  pipeAddr[6][MAX_ADDR_WIDTH] = {
    { 0 },
    { 0x42, 0x54, 0x48, 0x4D, 0x45 }, // "BTHME"
    { 0 }, { 0 }, { 0 }, { 0 },
  };
};

static Config cfg;
static bool listening = false;

// RX interrupt flag (IRQ pin present on this board).
static volatile bool rxFlag = false;
static void onRadioIrq() { rxFlag = true; }

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

// Parses a run of hex bytes separated by ':' ',' or '.' from a mutable string,
// appending to out (up to maxLen). Returns count parsed, or -1 on any bad byte.
static int parseHexList(char *s, uint8_t *out, uint8_t start, uint8_t maxLen) {
  int count = start;
  char *tok = strtok(s, ":,.");
  while (tok != nullptr) {
    if (count >= maxLen) return -1;
    int b = parseByte(tok);
    if (b < 0) return -1;
    out[count++] = (uint8_t)b;
    tok = strtok(nullptr, ":,.");
  }
  return count;
}

static void printAddr(uint8_t *a, uint8_t width) {
  for (uint8_t i = 0; i < width; i++) {
    if (i) Serial.print(':');
    if (a[i] < 0x10) Serial.print('0');
    Serial.print(a[i], HEX);
  }
}

// --- Radio (re)configuration -----------------------------------------------

static rf24_datarate_e rateEnum() {
  switch (cfg.rateKbps) {
    case 1000: return RF24_1MBPS;
    case 2000: return RF24_2MBPS;
    default:   return RF24_250KBPS;
  }
}

static rf24_crclength_e crcEnum() {
  switch (cfg.crcBits) {
    case 0:  return RF24_CRC_DISABLED;
    case 8:  return RF24_CRC_8;
    default: return RF24_CRC_16;
  }
}

// Applies the full Config to the radio. Safe to call at any time; restores the
// previous listening state afterwards.
static void reconfigure() {
  bool wasListening = listening;
  radio.stopListening();

  radio.setChannel(cfg.channel);
  radio.setDataRate(rateEnum());
  radio.setCRCLength(crcEnum());
  radio.setAddressWidth(cfg.addrWidth);
  radio.setPALevel((rf24_pa_dbm_e)cfg.paLevel);
  radio.setAutoAck(cfg.autoAck);

  if (cfg.dpl) {
    radio.enableDynamicPayloads();
  } else {
    radio.disableDynamicPayloads();
    radio.setPayloadSize(cfg.plSize);
  }
  // Allow per-packet NO_ACK on transmit (tx ... noack).
  radio.enableDynamicAck();

  for (uint8_t p = 0; p < 6; p++) {
    if (cfg.pipeEn[p]) {
      radio.openReadingPipe(p, cfg.pipeAddr[p]);
    } else {
      radio.closeReadingPipe(p);
    }
  }

  // Deliver only RX-ready interrupts on the IRQ pin.
  radio.maskIRQ(true, true, false);

  if (wasListening) {
    radio.startListening();
    listening = true;
  }
}

// --- RX handling -----------------------------------------------------------

static void drainRx() {
  uint8_t pipe = 0;
  while (radio.available(&pipe)) {
    uint8_t len = cfg.dpl ? radio.getDynamicPayloadSize() : cfg.plSize;
    if (len == 0 || len > 32) {
      // Corrupt dynamic length: discard to avoid a stuck FIFO.
      radio.flush_rx();
      break;
    }
    uint8_t buf[32];
    radio.read(buf, len);

    digitalWrite(PIN_LED_RX, LED_ON);
    Serial.print(F("RX p"));
    Serial.print(pipe);
    Serial.print(F(" len="));
    Serial.print(len);
    for (uint8_t i = 0; i < len; i++) {
      Serial.print(' ');
      if (buf[i] < 0x10) Serial.print('0');
      Serial.print(buf[i], HEX);
    }
    Serial.println();
    digitalWrite(PIN_LED_RX, LED_OFF);
  }
  rxFlag = false;
}

// --- Command handlers ------------------------------------------------------

static void ok()            { Serial.println(F("OK")); }
static void okMsg(const __FlashStringHelper *m) { Serial.print(F("OK ")); Serial.println(m); }
static void err(const __FlashStringHelper *m)   { Serial.print(F("ERR ")); Serial.println(m); }

static void cmdInfo() {
  Serial.println(F("info:"));
  Serial.print(F("  chip="));      Serial.println(radio.isChipConnected() ? F("connected") : F("NOT connected"));
  Serial.print(F("  listening=")); Serial.println(listening ? 1 : 0);
  Serial.print(F("  channel="));   Serial.println(cfg.channel);
  Serial.print(F("  rate="));      Serial.print(cfg.rateKbps);   Serial.println(F("kbps"));
  Serial.print(F("  crc="));       Serial.println(cfg.crcBits);
  Serial.print(F("  aw="));        Serial.println(cfg.addrWidth);
  Serial.print(F("  pa="));        Serial.println(cfg.paLevel); // 0=min 1=low 2=high 3=max
  Serial.print(F("  autoack="));   Serial.println(cfg.autoAck ? 1 : 0);
  Serial.print(F("  dpl="));       Serial.println(cfg.dpl ? 1 : 0);
  Serial.print(F("  plsize="));    Serial.println(cfg.plSize);
  for (uint8_t p = 0; p < 6; p++) {
    Serial.print(F("  pipe"));
    Serial.print(p);
    Serial.print('=');
    if (cfg.pipeEn[p]) printAddr(cfg.pipeAddr[p], cfg.addrWidth);
    else Serial.print(F("off"));
    Serial.println();
  }
  ok();
}

static void cmdTx(char *args) {
  if (args == nullptr) { err(F("usage: tx <addr> <hex...> [ack|noack]")); return; }

  // First token is the destination address.
  char *addrTok = strtok(args, " \t");
  if (addrTok == nullptr) { err(F("missing addr")); return; }
  uint8_t addr[MAX_ADDR_WIDTH];
  int an = parseHexList(addrTok, addr, 0, cfg.addrWidth);
  if (an != cfg.addrWidth) { err(F("bad addr width")); return; }

  // Remaining tokens: payload hex bytes plus an optional ack/noack keyword.
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

  radio.stopListening();
  radio.openWritingPipe(addr);
  digitalWrite(PIN_LED_TX, LED_ON);
  bool sent = radio.write(payload, plen, noack); // 3rd arg true => NO_ACK
  digitalWrite(PIN_LED_TX, LED_OFF);

  // Restore reading pipes (openWritingPipe clobbers pipe 0) and listen state.
  reconfigure();

  Serial.print(F("OK tx sent="));
  Serial.print(sent ? 1 : 0);
  Serial.print(F(" ack="));
  Serial.println(noack ? F("no") : F("yes"));
}

// Dispatches one complete command line (mutable, NUL-terminated).
static void dispatch(char *line) {
  char *cmd = strtok(line, " \t");
  if (cmd == nullptr) return; // empty line

  char *rest = strtok(nullptr, ""); // remainder of the line, or nullptr

  if (strcmp(cmd, "info") == 0) {
    cmdInfo();
  } else if (strcmp(cmd, "listen") == 0) {
    radio.startListening();
    listening = true;
    okMsg(F("listening"));
  } else if (strcmp(cmd, "stop") == 0) {
    radio.stopListening();
    listening = false;
    okMsg(F("stopped"));
  } else if (strcmp(cmd, "ch") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 0 || v > 125) { err(F("ch 0..125")); return; }
    cfg.channel = (uint8_t)v; reconfigure(); ok();
  } else if (strcmp(cmd, "rate") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 250 && v != 1000 && v != 2000) { err(F("rate 250|1000|2000")); return; }
    cfg.rateKbps = (uint16_t)v; reconfigure(); ok();
  } else if (strcmp(cmd, "crc") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 8 && v != 16) { err(F("crc 0|8|16")); return; }
    cfg.crcBits = (uint8_t)v; reconfigure(); ok();
  } else if (strcmp(cmd, "aw") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 3 || v > 5) { err(F("aw 3|4|5")); return; }
    cfg.addrWidth = (uint8_t)v; reconfigure(); ok();
  } else if (strcmp(cmd, "ack") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("ack 0|1")); return; }
    cfg.autoAck = (v == 1); reconfigure(); ok();
  } else if (strcmp(cmd, "dpl") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v != 0 && v != 1) { err(F("dpl 0|1")); return; }
    cfg.dpl = (v == 1); reconfigure(); ok();
  } else if (strcmp(cmd, "plsize") == 0) {
    int v = rest ? atoi(rest) : -1;
    if (v < 1 || v > 32) { err(F("plsize 1..32")); return; }
    cfg.plSize = (uint8_t)v; reconfigure(); ok();
  } else if (strcmp(cmd, "pa") == 0) {
    if (rest == nullptr)             { err(F("pa min|low|high|max")); return; }
    else if (strcmp(rest, "min")  == 0) cfg.paLevel = 0;
    else if (strcmp(rest, "low")  == 0) cfg.paLevel = 1;
    else if (strcmp(rest, "high") == 0) cfg.paLevel = 2;
    else if (strcmp(rest, "max")  == 0) cfg.paLevel = 3;
    else { err(F("pa min|low|high|max")); return; }
    reconfigure(); ok();
  } else if (strcmp(cmd, "pipe") == 0) {
    char *nTok = rest ? strtok(rest, " \t") : nullptr;
    char *aTok = nTok ? strtok(nullptr, " \t") : nullptr;
    int p = nTok ? atoi(nTok) : -1;
    if (p < 0 || p > 5 || aTok == nullptr) { err(F("usage: pipe <0-5> <addr|off>")); return; }
    if (strcmp(aTok, "off") == 0) {
      cfg.pipeEn[p] = false; reconfigure(); ok(); return;
    }
    uint8_t tmp[MAX_ADDR_WIDTH];
    int n = parseHexList(aTok, tmp, 0, cfg.addrWidth);
    if (n != cfg.addrWidth) { err(F("addr must match aw")); return; }
    memcpy(cfg.pipeAddr[p], tmp, cfg.addrWidth);
    cfg.pipeEn[p] = true;
    reconfigure(); ok();
  } else if (strcmp(cmd, "tx") == 0) {
    cmdTx(rest);
  } else if (strcmp(cmd, "help") == 0) {
    Serial.println(F("cmds: ch rate crc aw pipe ack dpl plsize pa listen stop info tx help"));
    ok();
  } else {
    err(F("unknown cmd (try help)"));
  }
}

// --- Arduino entry points --------------------------------------------------

void setup() {
  pinMode(PIN_LED_TX, OUTPUT);
  pinMode(PIN_LED_RX, OUTPUT);
  digitalWrite(PIN_LED_TX, LED_OFF);
  digitalWrite(PIN_LED_RX, LED_OFF);

  Serial.begin(SERIAL_SPEED);

  bool chip = radio.begin();
  reconfigure();

  if (chip) {
    attachInterrupt(digitalPinToInterrupt(PIN_RADIO_IRQ), onRadioIrq, FALLING);
  }

  // Startup blink: 1x = radio present, 2x = radio not detected.
  for (uint8_t i = 0; i < (chip ? 1 : 2); i++) {
    digitalWrite(PIN_LED_RX, LED_ON);  delay(120);
    digitalWrite(PIN_LED_RX, LED_OFF); delay(120);
  }

  Serial.println(F("NRF24SNIFFER ready"));
  Serial.print(F("chip="));
  Serial.println(chip ? F("connected") : F("NOT connected"));
}

void loop() {
  // Serial command input, one line at a time.
  static char lineBuf[LINE_BUF_SIZE];
  static uint8_t lineLen = 0;

  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuf[lineLen] = '\0';
      dispatch(lineBuf);
      lineLen = 0;
    } else if (lineLen < LINE_BUF_SIZE - 1) {
      lineBuf[lineLen++] = c;
    } else {
      // Overlong line: reset to stay in sync.
      lineLen = 0;
    }
  }

  // Received frames: IRQ sets the flag, but poll as a fallback too.
  if (listening && (rxFlag || radio.available())) {
    drainRx();
  }
}
