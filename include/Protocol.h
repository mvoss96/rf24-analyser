#pragma once

// Firmware version, and the command-protocol version the host can check.
//
// api=3 changed two things a host cannot ignore: RX lines carry a firmware
// timestamp, and pipes 2-5 take one byte instead of a full address.
//
// 3.2.0 extended tx with optional burst arguments (x<n>, gap=<ms>). Purely
// additive: a host that never sends them sees the exact old behaviour, so the
// api version stayed at 3.
//
// api=4 adds `crc=<XX>` to every RX line, covering the payload from the moment
// it leaves the RX FIFO. Not additive: a host that does not know the field
// would try to read it as payload hex and drop the frame, so the version has
// to move with it.
// 3.4.0 adds rxmode/rxdbg/regs for the RX-FIFO diagnosis. Additive, and the
// default rxmode is what 3.3.0 did unconditionally, so api stayed at 4.
//
// 3.5.0 makes `listen` and `hwset` acknowledge with the state they left behind
// instead of a bare OK, in the same key=value grammar `info` uses - so a host
// parses one thing, and a setting the firmware quietly changed (an irq pin that
// cannot interrupt, a chip that did not take a value) is visible in the very
// line that reports success. Additive: the tokens come after the OK, and a host
// that checks for OK and stops reading sees the old behaviour, so api stayed at
// 4 and a dongle on older firmware still works with a newer host.
// api=5 renames the greeting from NRF24SNIFFER to NRF24ANALYSER, the project
// having been renamed. It is the one break this field cannot announce: a host
// that does not know the new identity never recognises the line, so it never
// reads the api= in it either. The version moves anyway, because it is the
// record of what is and is not interchangeable - but what a host actually sees
// against older firmware is no greeting at all, and the answer is to reflash.
// 3.7.0 makes the command path honest under load. The serial receive buffer
// is 256 bytes, because loop() does not read the port while transmit() runs and
// an 85-character `tx` line does not fit the default 64 - a command arriving
// during a transmit lost its tail and was answered as a bad payload. An
// overlong line now says so instead of having its tail dispatched as a command
// of its own. And `tx` distinguishes an acknowledgement from the absence of
// anyone to give one: ack=off says the radio was never going to wait, where it
// used to say yes. Additive, so api stayed at 5.
// 3.8.0 adds `format bin`, a second shape for received frames. The readable
// line costs about 4 ms a frame, of which only 1.7 ms is the serial line -
// the rest is formatting 32 bytes into 64 hex characters, roughly a thousand
// clock cycles apiece. A binary record removes both. Additive and off by
// default, so api stays at 5: a host that never asks sees exactly what it saw
// before, and a reset returns a dongle to the readable form.
// 3.8.1 stops the transmit counters wrapping. attempted/sent/failed were bytes
// while `txseq` takes up to sixty thousand frames, so a 300-frame run reported
// `sent=44` and a 512-frame run `sent=0` - after transmitting all of them. A
// count that lies about a completed transfer reads exactly like a truncated
// one. `txseq` also tells `ack=off` from `ack=no` now, as `tx` already did.
#define FW_VERSION "3.12.0"
#define API_VERSION 5

// The rate a dongle always boots at. `baud` can raise it for a session, but a
// reset must come back here: a host that does not know the command - or a
// checkout that predates it - has to be able to open the port and be understood.
#define BOOT_BAUD 500000L

// CRC-8/ATM (polynomial 0x07). Small and table-less; it only has to catch
// corruption on the way between host and dongle, not to be cryptographically
// anything. Shared because both directions now use it: the frame records going
// out, and the payload records coming in.
inline uint8_t nrf24_crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0;
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

// Starts a binary frame record. Outside printable ASCII, which nothing else
// this firmware prints ever is, so a reader can tell the two apart mid-stream.
#define RX_BIN_SYNC 0x01
