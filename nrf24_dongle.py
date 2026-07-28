"""Serial client for the nRF24 Analyser dongle.

Speaks the line-based ASCII protocol described in README.md and is shared by the
terminal (nrf24term.py) and the GUI (nrf24gui.py) so there is exactly one
implementation of it.

A reader thread turns the byte stream into lines and puts them on a queue;
callers drain it however suits them (a print loop, a tkinter after() tick).
"""

import queue
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - reported by the entry points
    serial = None
    list_ports = None

# Command-protocol version this client speaks; compared against the firmware's
# greeting so a mismatch is reported instead of producing cryptic errors.
#
# api=5 is the rename of the greeting itself, from NRF24SNIFFER to
# NRF24ANALYSER. It is the one incompatibility this field cannot warn about: a
# host that does not know the new identity never recognises the line at all, so
# it never gets as far as reading api= out of it. What it sees instead is
# silence where the greeting should be, which the ui reports as "no greeting" -
# and the fix is to reflash the dongle.
#
# api=6 shrinks the binary frame record: a two-byte timestamp instead of four,
# the pipe sharing a byte with a suppressed-run count, and a repeated tail sent
# as one fill byte. A host reading the old layout would take the pipe byte for
# part of the timestamp and the payload for the rest, so this one the version
# does have to announce.
EXPECTED_API = 6

DEFAULT_BAUD = 500000


def available_ports():
    """[(device, description), ...] for every serial port on the system."""
    if list_ports is None:
        return []
    return [(p.device, p.description or "") for p in list_ports.comports()]


def parse_greeting(line):
    """Parses 'NRF24ANALYSER fw=.. api=.. state=.. hw=.. ce=..' into a dict.

    Returns None for any other line - including the NRF24SNIFFER greeting of
    firmware older than 3.6.0, which is deliberate: the identity is what the
    host recognises the device by, and accepting two of them would leave the
    project answering to a name it no longer has for as long as anyone remembers
    to keep both. A dongle that still greets with the old name is reported as
    not greeting at all, and wants reflashing.
    """
    if not line.startswith("NRF24ANALYSER"):
        return None
    fields = {}
    for token in line.split()[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            fields[key] = value
    return fields


INFO_HEADER = "info:"

# `info` answers with an indented block, not one line: the state, then - as far
# as the dongle has got - the wiring, then the radio configuration, then the
# counters, closed by OK. Fields it cannot know yet are simply absent (a dongle
# without wiring reports state and nothing else), so a caller has to ask what is
# there rather than index into a fixed shape.
INFO_INT_FIELDS = ("channel", "crc", "aw", "plsize", "ack", "dpl", "repeats",
                   "rxmode", "rxdbg", "rx", "fifofull", "baud", "us_in",
                   "us_out", "us_n", "ms")


def parse_info(lines):
    """Parses the body of an `info:` block into a snapshot of the radio.

    `lines` are the indented lines between `info:` and its `OK`. Everything in
    them is key=value, one or more per line, so the wiring line parses like any
    other. Numbers become numbers; `rate=250kbps` loses its unit; the pipes are
    collected separately because their count varies with the configuration.

    This is what the dongle says about itself, which is the only thing that can
    honestly be shown as its state - a form field says what someone typed.
    """
    fields = {}
    pipes = {}
    for line in lines:
        for token in line.split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key.startswith("pipe") and key[4:].isdigit():
                pipes[int(key[4:])] = value
            else:
                fields[key] = value

    info = {"pipes": pipes}
    for key, value in fields.items():
        if key in INFO_INT_FIELDS:
            try:
                info[key] = int(value)
            except ValueError:
                info[key] = value
        elif key == "rate":
            try:
                info[key] = int(value.removesuffix("kbps"))
            except ValueError:
                info[key] = value
        else:
            info[key] = value

    info["wiring"] = {key: fields[key] for key in
                      ("ce", "csn", "irq", "led_rx", "led_tx") if key in fields}
    # Two questions the block answers by omission: printInfo() stops after the
    # state when there is no wiring, and after the counters-free part when the
    # radio was never configured. Saying so explicitly keeps every consumer from
    # rediscovering that rule.
    info["hwReady"] = bool(info["wiring"])
    info["configured"] = "channel" in info
    return info


def parse_ack(line):
    """The state an `OK` line reports having left behind, or None.

    From firmware 3.5.0 the acknowledgements of `listen` and `hwset` carry the
    resulting state as key=value tokens in the same grammar as `info` - so this
    is parse_info() with a different line ending. Acknowledging the outcome
    rather than the fact of success is what makes a setting the firmware quietly
    changed visible: an irq pin downgraded to polling, a configuration discarded
    by hwset, a value the chip did not take.

    Older firmware answers with a bare `OK listening`, which returns None here,
    and the host falls back to asking. That is why the tokens were added after
    the OK rather than replacing it, and why the api version did not have to
    move: a dongle that has not been reflashed still works.
    """
    if not line.startswith("OK") or " state=" not in line:
        return None
    return parse_info([line])


def crc8(data):
    """CRC-8/ATM (polynomial 0x07) over a byte sequence - the firmware's."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


# A frame under `format bin`: sync, the length before suppression, the pipe and
# suppressed-run count sharing a byte, two bytes of millis, the payload with any
# repeated tail replaced by one fill byte, and the same checksum over the same
# bytes the readable line carries.
RX_BIN_SYNC = b"\x01"
RX_BIN_HEADER = 5

# The wrap of the two-byte timestamp, and the furthest the host's estimate of the
# dongle's clock may be out before it picks the wrong one.
STAMP_WRAP = 1 << 16
STAMP_HALF = STAMP_WRAP // 2


def rx_record_size(header):
    """How long the record starting with these bytes is, or None if it cannot be one.

    Needs the first three bytes. Returns the length up to and including the
    checksum; the newline after it is not part of the record.
    """
    if len(header) < 3:
        return None
    length, run = header[1], header[2] >> 3
    pipe = header[2] & 0x07
    if not 1 <= length <= 32 or pipe > 5 or run >= length:
        return None
    stored = length - run
    return RX_BIN_HEADER + stored + (1 if run else 0) + 1


def rx_payload(record):
    """The payload as it left the FIFO, with any suppressed tail put back."""
    length, run = record[1], record[2] >> 3
    stored = length - run
    body = record[RX_BIN_HEADER:RX_BIN_HEADER + stored]
    if not run:
        return body
    return body + bytes([record[RX_BIN_HEADER + stored]]) * run


def seq_record(payload):
    """One `txseq ... bin` payload: its length, itself, and a checksum.

    Half the bytes of the hex line it replaces, which is what the sending
    dongle's serial link had become bound by. There is no sync marker because
    none would help: the dongle knows it is owed a fixed number of records and
    each states its own length, so a reader that has lost its place cannot get
    it back by scanning - which is why the checksum ends the run rather than
    skipping a record.
    """
    if not 1 <= len(payload) <= 32:
        raise ValueError("payload must be 1..32 bytes")
    return bytes([len(payload)]) + payload + bytes([crc8(payload)])


def parse_rx(line):
    """Parses 'RX t=<ms> p<pipe> len=<n> crc=<XX> <hex>' into
    (stamp_ms, pipe, data, intact).

    Returns None for any other line. `stamp_ms` is the firmware's own millis()
    at the moment the frame left the RX FIFO - the only clock in the system that
    has not had the serial link and the host scheduler added to it. It is None
    for the pre-api-3 form, which carried no timestamp.

    `intact` is False when the payload does not match the checksum the firmware
    computed as the frame left the FIFO, and None when the firmware did not send
    one (pre-api-4). Everything between those two points is unprotected - the
    SPI read, the firmware's buffer, the serial line - and measurably lossy: two
    dongles listening to the same traffic disagreed with the sender's own log on
    24% and 4% of frames respectively. A frame that fails here is not evidence
    about the air; it says the sniffer mangled it. The caller decides what to do
    with that, but it must not be able to mistake one for the other.

    Accepts both the compact hex the firmware emits ("4D565202...") and the
    space-separated form older logs used.
    """
    if not line.startswith("RX "):
        return None
    stamp = None
    pipe = None
    claimed = None
    tokens = []
    for token in line.split()[1:]:
        if token.startswith("t=") and stamp is None:
            try:
                stamp = int(token[2:])
            except ValueError:
                return None
        elif token.startswith("crc=") and claimed is None:
            try:
                claimed = int(token[4:], 16)
            except ValueError:
                return None
        elif token.startswith("p") and pipe is None:
            try:
                pipe = int(token[1:])
            except ValueError:
                return None
        elif token.startswith("len="):
            continue
        else:
            tokens.append(token)
    blob = "".join(tokens)
    if not blob or len(blob) % 2:
        return None
    try:
        data = [int(blob[i:i + 2], 16) for i in range(0, len(blob), 2)]
    except ValueError:
        return None
    intact = None if claimed is None else crc8(data) == claimed
    return stamp, pipe, data, intact


def parse_scan(line):
    """Parses 'SCAN ch=<n> hits=<h>' into (channel, hits) or None."""
    if not line.startswith("SCAN ch="):
        return None
    try:
        _, ch_token, hits_token = line.split()
        return int(ch_token.split("=")[1]), int(hits_token.split("=")[1])
    except (ValueError, IndexError):
        return None


def build_hwset(ce, csn, irq="none", led_rx="none", led_tx="none"):
    return (f"hwset ce={ce} csn={csn} irq={irq or 'none'} "
            f"led_rx={led_rx or 'none'} led_tx={led_tx or 'none'}")


def build_listen(ch, rate, crc, aw, pa, ack, dpl, pipes, plsize=None):
    """Builds a full `listen` line. `pipes` maps pipe number -> address string.

    Pipes 0 and 1 take `aw` bytes; pipes 2-5 take exactly one, since the radio
    shares the rest of their address with pipe 1.
    """
    parts = [f"listen ch={ch}", f"rate={rate}", f"crc={crc}", f"aw={aw}",
             f"pa={pa}", f"ack={int(ack)}", f"dpl={int(dpl)}"]
    if not dpl and plsize:
        parts.append(f"plsize={plsize}")
    for number in sorted(pipes):
        address = (pipes[number] or "").strip()
        if address:
            parts.append(f"pipe{number}={address}")
    return " ".join(parts)


class Dongle:
    """Owns the serial port and a reader thread producing complete lines."""

    def __init__(self, port, baud=DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self.lines = queue.Queue()
        self._serial = None
        self._thread = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()   # one writer at a time - see send()
        # A binary record's trailing newline can arrive in the next read, so
        # whether one is still owed outlives a single pass over the buffer.
        self._pending_lf = False
        # Where the dongle's clock was, and when we heard that, so the two bytes
        # a record carries can be put back into a full millisecond count.
        self._stamp_full = None
        self._stamp_heard = 0.0

    @property
    def is_open(self):
        return self._serial is not None

    @property
    def reading(self):
        """True while the reader thread is alive.

        It ends on its own only when the port broke away underneath it - a port
        that was closed properly took the whole session with it. Nothing else
        notices that: writes to the dead handle can go on succeeding, so the
        caller would keep talking to a device that is not there.
        """
        return self._thread is not None and self._thread.is_alive()

    def open(self):
        if serial is None:
            raise RuntimeError("pyserial is required: pip install -r requirements.txt")
        self._serial = serial.Serial(self.port, self.baud, timeout=0.1)
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def send(self, line):
        """Writes one line. Whole, and never braided with another thread's.

        Four threads reach this: the HTTP handler answering a command, the
        heartbeat asking `info`, the timers that chase a missing greeting, and
        the pump thread when a greeting arrives. Without the lock two writes can
        interleave inside the driver, and what the firmware then reads is one
        line spliced into another - which it reports as whatever the splice
        happened to break: `ERR bad payload byte` for an intact payload, `ERR
        expected key=value` for a correct listen. Both were seen, both were
        sporadic, and both were blamed on the line that was sent rather than on
        the one that arrived.
        """
        if self._serial is None:
            raise RuntimeError("not connected")
        data = (line.rstrip("\r\n") + "\n").encode("ascii", errors="replace")
        with self._write_lock:
            self._serial.write(data)

    def send_raw(self, data):
        """Writes bytes with nothing added - no terminator, no encoding.

        Only for a `txseq ... bin` payload stream, where the dongle is counting
        bytes rather than looking for lines. Takes the same lock as send(), so
        a payload record still cannot be braided into a command.
        """
        if self._serial is None:
            raise RuntimeError("not connected")
        with self._write_lock:
            self._serial.write(data)

    def set_baud(self, baud):
        """Follows the dongle to a rate it has just switched to.

        Only ever called after `OK baud=` has been read, which the firmware
        sends and flushes before switching, so the change happens on an idle
        line. The port is reconfigured rather than reopened: reopening pulls
        DTR, which resets the dongle - and a dongle that reset is back at
        BOOT_BAUD with its radio configuration gone, which is the one outcome
        this must not produce.
        """
        if self._serial is None:
            raise RuntimeError("not connected")
        self._serial.baudrate = baud
        self.baud = baud

    def _read_loop(self):
        buffer = b""
        while not self._stop.is_set():
            try:
                # One byte, blocking, then whatever else is already buffered.
                #
                # read(512) does not mean "up to 512": pyserial waits for 512
                # bytes or the port timeout, whichever comes first. A reply is
                # twenty bytes, so every single one of them sat in the driver
                # for the whole 100 ms timeout before this loop saw it. That was
                # the cost of a command: measured at 129 ms for a `tx` that the
                # radio finishes in under two, and it was paid by every awaited
                # command in the program - each frame of a burst, each Apply in
                # the browser, each MCP call.
                chunk = self._serial.read(1)
                if chunk:
                    waiting = self._serial.in_waiting
                    if waiting:
                        chunk += self._serial.read(waiting)
            except Exception as exc:
                self.lines.put(f"[serial error: {exc}]")
                break
            if not chunk:
                continue
            buffer += chunk
            buffer = self._drain(buffer)

    def _drain(self, buffer):
        """Pulls whole lines and whole binary records out, leaves the remainder.

        Two shapes share this stream. Replies, warnings and - unless `format
        bin` was asked for - frames arrive as newline-terminated ASCII. A frame
        under `format bin` arrives as a record introduced by RX_BIN_SYNC, which
        is outside printable ASCII and so cannot begin a line.

        A record is turned into the very line the firmware would have printed
        for it. That is the whole trick: the binary shape is a transport saving
        on the wire and nothing above this method can tell which one arrived.
        """
        while buffer:
            # The firmware writes a newline after each record so that a reply
            # printed mid-stream starts on its own line in a terminal. It
            # terminates nothing - the length already said where the record
            # ended - so swallow it rather than reporting a blank line. It may
            # arrive in the read after the record it belongs to.
            if self._pending_lf:
                self._pending_lf = False
                if buffer[:1] == b"\n":
                    buffer = buffer[1:]
                    continue

            start = buffer.find(RX_BIN_SYNC)
            end = buffer.find(b"\n")

            if start < 0 or (0 <= end < start):
                # Nothing binary before the next line end: ordinary line.
                if end < 0:
                    return buffer
                raw, buffer = buffer[:end], buffer[end + 1:]
                self.lines.put(raw.decode("ascii", errors="replace").rstrip("\r"))
                continue

            if start:
                # ASCII ahead of the record with no newline of its own. The
                # firmware does not emit that, so it is a fragment of something
                # already broken; hand it up rather than dropping it silently.
                raw, buffer = buffer[:start], buffer[start:]
                text = raw.decode("ascii", errors="replace").strip()
                if text:
                    self.lines.put(text)
                continue

            if len(buffer) < 3:
                return buffer                      # header still in flight
            size = rx_record_size(buffer)
            if size is None:
                buffer = buffer[1:]                # not a header; resync
                continue
            if len(buffer) < size:
                return buffer                      # payload still in flight
            record, buffer = buffer[:size], buffer[size:]
            self._pending_lf = True     # swallowed at the top, this pass or next
            self.lines.put(self._rx_line(record))
        return buffer

    def stamp_anchor(self, device_ms):
        """Takes the dongle's own `ms=` as the truth about where its clock is.

        `info` reports it, so a host that has just connected does not have to
        assume the dongle booted a moment ago, and one that has heard nothing for
        hours cannot have drifted into the wrong wrap.
        """
        self._stamp_full = int(device_ms)
        self._stamp_heard = time.monotonic()

    def _unwrap_stamp(self, low):
        """Puts the high bits back on a two-byte timestamp.

        The record carries the low 16 bits of the dongle's millisecond clock,
        which wrap every 65.536 s. The host knows roughly what that clock reads -
        it knew a moment ago and its own clock has run since - so it picks the
        value congruent to those 16 bits that lies nearest the estimate. Being
        wrong needs the estimate to be out by more than 32.7 s.

        Each record carries an absolute value, so this cannot accumulate: frames
        lost in between cost nothing, and one bad reconstruction does not poison
        the next. That is the whole reason the wire does not carry a delta.
        """
        now = time.monotonic()
        if self._stamp_full is None:
            self._stamp_full, self._stamp_heard = low, now
            return low
        estimate = self._stamp_full + int((now - self._stamp_heard) * 1000)
        full = estimate + ((low - estimate) % STAMP_WRAP)
        if full - estimate > STAMP_HALF:
            full -= STAMP_WRAP
        self._stamp_full, self._stamp_heard = full, now
        return full

    def _rx_line(self, record):
        """Writes a binary frame record out as the line the firmware would print.

        The saving is on the wire - 28 to 39 bytes against about 95 characters,
        and no number formatted a byte at a time - so it belongs on the wire.
        Above this, one shape: everything that reads frames goes on reading
        `RX ...` lines and never learns which way they arrived.
        """
        payload = rx_payload(record)
        return (f"RX t={self._unwrap_stamp(record[3] | (record[4] << 8))} "
                f"p{record[2] & 0x07} len={record[1]} "
                f"crc={record[len(record) - 1]:02X} {payload.hex().upper()}")
