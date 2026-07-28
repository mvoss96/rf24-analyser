// A serial bench with nothing in it.
//
// No radio, no SPI, no protocol - this firmware exists to answer one question
// the analyser firmware cannot answer about itself: how much of the wire can a
// host running pyserial actually use? Every number the analyser produces is
// bounded by this one, so measuring the two against each other says whether the
// next millisecond is to be found in our code or somewhere below it.
//
// It offers exactly four measurements:
//
//   s <n>    source  - write n bytes as fast as the UART takes them
//   r        sink    - read bytes until quiet, and time them
//   w <k>    window  - the same, but say "A <total>" every k bytes
//   e        echo    - send every byte straight back
//
// The sink modes check the bytes as they arrive: the host sends an incrementing
// counter, so a lost or doubled byte is visible rather than merely suspected. A
// throughput figure from a link that quietly drops bytes is not a measurement.
//
// Every mode returns to the command prompt after 300 ms of silence, so a run
// ends by stopping rather than by resetting the board.
//
// Build and flash:  pio run -e serialbench -t upload

#include <Arduino.h>

static const char VERSION[] = "1.0.0";

// Long enough that no gap inside a run can be mistaken for the end of one, and
// short enough that a measurement is not mostly waiting.
static const uint32_t QUIET_US = 300000UL;

// If a mode is entered and nothing ever arrives, fall back to the prompt rather
// than sitting there needing a reset.
static const uint32_t DEAD_US = 5000000UL;

static long baud_ = 500000L;

static char line_[32];
static uint8_t lineLen_ = 0;

// ---------------------------------------------------------------- reporting

static void report(const char *tag, uint32_t n, uint32_t us, uint32_t bad) {
  Serial.print(tag);
  Serial.print(F(" n="));
  Serial.print(n);
  Serial.print(F(" us="));
  Serial.print(us);
  Serial.print(F(" bad="));
  Serial.println(bad);
}

// ------------------------------------------------------------------- source

// Write n bytes and time the writing. The count is announced first and the
// result last, so the host can read exactly n bytes in between and time them
// from its own side; the two clocks disagreeing is itself a finding.
static void source(uint32_t n) {
  Serial.print(F("OK src n="));
  Serial.println(n);
  Serial.flush();   // the announcement must not be part of what is timed

  const uint32_t t0 = micros();
  uint8_t v = 0;
  for (uint32_t i = 0; i < n; i++) Serial.write(v++);
  Serial.flush();   // and neither may the tail still sitting in the buffer
  const uint32_t us = micros() - t0;

  report("SRC", n, us, 0);
}

// --------------------------------------------------------------------- sink

// Read until quiet. With window > 0, say how many bytes have arrived every
// `window` bytes - which is what the analyser's txseq does with `conf=`, minus
// everything else the analyser is doing.
static void sink(uint16_t window) {
  uint32_t n = 0, bad = 0;
  uint32_t t0 = 0, tLast = micros();
  uint8_t expect = 0;
  uint16_t since = 0;

  for (;;) {
    if (Serial.available()) {
      const uint8_t b = (uint8_t)Serial.read();
      if (b != expect) bad++;
      // Resynchronise on what actually arrived: one lost byte then costs one
      // mismatch instead of making every byte after it look wrong.
      expect = b + 1;
      n++;

      // micros() costs about as much as receiving a byte does at 2 Mbps, so it
      // is called every 64th byte instead of every byte. Over a run of tens of
      // thousands that is a rounding error; every byte would be a tax on the
      // very thing being measured.
      if (n == 1) { tLast = micros(); t0 = tLast; }
      else if ((n & 0x3F) == 0) tLast = micros();

      if (window && ++since >= window) {
        since = 0;
        Serial.print('A');
        Serial.println(n);
      }
    } else {
      const uint32_t idle = micros() - tLast;
      if (n && idle > QUIET_US) break;
      if (!n && idle > DEAD_US) break;
    }
  }

  report("SINK", n, tLast - t0, bad);
}

// --------------------------------------------------------------------- echo

// One byte back for every byte in, which is the shortest round trip this link
// can have. Anything the analyser pays above this is its own.
static void echo() {
  uint32_t n = 0;
  uint32_t tLast = micros();

  for (;;) {
    if (Serial.available()) {
      Serial.write((uint8_t)Serial.read());
      n++;
      if (n == 1 || (n & 0x3F) == 0) tLast = micros();
    } else {
      const uint32_t idle = micros() - tLast;
      if (n && idle > QUIET_US) break;
      if (!n && idle > DEAD_US) break;
    }
  }

  report("ECHO", n, 0, 0);
}

// ----------------------------------------------------------------- commands

static void info() {
  Serial.print(F("SB "));
  Serial.print(VERSION);
  Serial.print(F(" baud="));
  Serial.print(baud_);
  Serial.print(F(" rxbuf="));
  Serial.print((int)SERIAL_RX_BUFFER_SIZE);
  Serial.print(F(" txbuf="));
  Serial.println((int)SERIAL_TX_BUFFER_SIZE);
}

static void setBaud(long v) {
  Serial.print(F("OK baud="));
  Serial.println(v);
  Serial.flush();   // said at the old rate, or it is not heard at all
  Serial.end();
  Serial.begin(v);
  baud_ = v;
}

static void handle(char *s) {
  while (*s == ' ') s++;
  const char c = *s++;
  const long arg = strtol(s, nullptr, 10);

  switch (c) {
    case 'v': info(); break;
    case 's': source(arg > 0 ? (uint32_t)arg : 1000UL); break;
    case 'r': Serial.println(F("OK sink")); sink(0); break;
    case 'w': Serial.print(F("OK win k=")); Serial.println(arg);
              sink(arg > 0 ? (uint16_t)arg : 1); break;
    case 'e': Serial.println(F("OK echo")); echo(); break;
    case 'b': if (arg > 0) setBaud(arg); else Serial.println(F("ERR baud")); break;
    case '\0': break;
    default: Serial.println(F("ERR cmd")); break;
  }
}

void setup() {
  Serial.begin(baud_);
  info();
}

void loop() {
  while (Serial.available()) {
    const char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen_) {
        line_[lineLen_] = '\0';
        lineLen_ = 0;
        handle(line_);
      }
    } else if (lineLen_ < sizeof(line_) - 1) {
      line_[lineLen_++] = c;
    }
  }
}
