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
// api=6 makes the binary frame record smaller, and it is the receiver's
// throughput that asked for it. A record was 9 bytes of frame around 32 bytes of
// payload; at 2 Mbps a frame arrives every 850 us and writing 41 bytes out takes
// 1030, so the dongle fell behind by 18 % and dropped that many. Three changes,
// none of them additive - a host reading the old layout would take the pipe byte
// for a timestamp - so the version moves:
//
//   - the timestamp is the low 16 bits of millis() instead of all 32. The host
//     puts the high bits back from its own clock, which would have to be wrong
//     by more than half of the 65.536 s wrap for that to fail. Each record still
//     carries an absolute value, so a lost frame costs nothing; a delta would
//     have shifted every timestamp after it, which is why this is not one.
//   - pipe and the suppressed-run count share a byte, three bits and five.
//   - a repeated tail is sent as one fill byte instead of itself. Senders that
//     pad a static payload are most of what this dongle ever sees: measured over
//     real BTHome traffic a record goes from 41 bytes to 28, which is the
//     difference between falling behind and keeping up. Arbitrary payloads have
//     no tail to suppress and pay nothing for the attempt.
// 3.16.0 frees 183 bytes of RAM without giving anything up. The scan's
// per-channel counters are taken for the duration of a scan instead of being a
// member - a scan retunes the radio, so it can never overlap with receiving, and
// the peak is unchanged while the resting state is 126 bytes better. The
// register-name table moves to flash, where it always belonged. Additive: only
// `scan live` gains a way to fail, and it says so. api stays at 6.
// api=7 makes the binary frames a stream rather than a sequence of self-contained
// records. What every record repeated - a sync byte, the pipe, two bytes of
// timestamp and a newline - is stated once for a run of them, and each frame
// carries a one-byte offset from that. Fixed cost per frame goes from 7 bytes to
// 3, and a run costs 7 to open and 2 to close.
//
// The timestamp is still not a delta between frames. It is an offset from the
// run's own base, so every frame in a run remains independently placed and a
// lost one shifts nothing - which is the property that made a delta to the
// previous frame unusable.
//
// What this gives up is immediate resynchronisation: only a run's first byte is
// a sync marker, so a reader thrown off by a damaged byte waits for the next run
// rather than the next frame. In this direction, at 500000 baud, no damaged byte
// has ever been measured; at 1 MBaud they are constant, and 1 MBaud does not
// work for other reasons.
// 3.21.0 stops a `txseq` that ended early from answering the payloads behind
// it. The host writes a window ahead of the confirmations, so when the dongle
// gives up on a frame there are still up to seven records on the way to it -
// and those were parsed as commands, one `ERR unknown cmd` or `ERR line too
// long` apiece. The host read the first of them as the answer to whatever it
// said next, which is how a run that merely needed picking up again became a
// failed transfer. They are dropped now until the port has been quiet for
// SEQ_DRAIN_MS, and `OK txseq idle dropped=<n>` says when that is over.
// Additive: it only ever follows a `stopped=` line, so a host that stops
// reading at one sees what it saw before, and api stays at 7.
#define FW_VERSION "3.21.0"
#define API_VERSION 7

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

// A run of binary frames. All three are outside printable ASCII, which nothing
// else this firmware prints ever is, so a reader can tell a run from a reply
// wherever one begins.
//
//   RX_RUN_NEW    pipe, default length, four bytes of millis - a new epoch
//   RX_RUN_MORE   the epoch before it still holds; frames follow directly
//   RX_RUN_END    no more frames; whatever comes next is readable again
//
// A run ends at every pass of the drain loop, so a reply can only ever be
// printed between runs and never inside one.
#define RX_RUN_NEW  0x01
#define RX_RUN_MORE 0x02
#define RX_RUN_END  0xFF

// In a frame record's first byte: how many payload bytes it carries, and
// whether the byte after it states what the payload really was.
//
// The two differ when a repeated tail has been suppressed, and the true length
// is usually the one the run announced - a sender's payload size rarely changes
// mid-run, and with dynamic payloads off it cannot. So the record carries what
// it stores, and says "the true length follows" only when it has to.
#define RX_LEN_MASK  0x3F
#define RX_LEN_LONG  0x40

// A tail shorter than this is left alone: suppressing n bytes costs one byte to
// say what they were, so two saves a single byte and is not worth the branch.
#define RX_RUN_MIN 3

// Five bits hold it, and one payload byte always has to remain for the fill
// value to be read from.
#define RX_RUN_MAX 31
