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
#
# api=7 turns those records into a stream. What each one repeated - a sync byte,
# the pipe, the timestamp, a newline - is stated once for a run of frames, and
# each frame carries an offset from that. There is nothing left in common between
# the two layouts, so a host on either side of this reads the other as noise.
EXPECTED_API = 7

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


# Frames under `format bin` arrive in runs. A run opens with either a new epoch -
# pipe, the payload length to assume, and four bytes of millis - or with a byte
# saying the last epoch still holds, and closes with RX_RUN_END and a newline.
#
# Between those, each frame costs three bytes: how many payload bytes it carries,
# how many milliseconds past the epoch's base it arrived, and a checksum. The
# offset is to the base and not to the frame before it, so every frame is placed
# independently and one lost in between costs nothing.
RX_RUN_NEW = 0x01
RX_RUN_MORE = 0x02
RX_RUN_END = 0xFF
RX_RUN_NEW_LEN = 7          # marker, pipe, default length, four bytes of base

RX_LEN_MASK = 0x3F
RX_LEN_LONG = 0x40

# Named because it is easy to lose in an editor and impossible to see in a diff.
LF = b"\n"


def rx_frame_size(head, default_len):
    """How long the frame record starting here is, or None if more bytes are needed.

    Returns (size, true_len, stored) so the caller does not decode twice.
    Raises ValueError if these bytes cannot begin a frame at all.
    """
    if not head:
        return None
    b0 = head[0]
    if b0 == RX_RUN_END:
        raise ValueError("end of run")
    stored = b0 & RX_LEN_MASK
    at = 1
    if b0 & RX_LEN_LONG:
        if len(head) < 2:
            return None
        true_len = head[1]
        at = 2
    else:
        true_len = default_len
    if not 1 <= true_len <= 32 or stored > true_len or b0 & 0x80:
        raise ValueError(f"not a frame record: {b0:#04x}")
    # the offset byte, the payload, a fill byte if a tail was suppressed, the crc
    size = at + 1 + stored + (1 if stored < true_len else 0) + 1
    return (size, true_len, stored) if len(head) >= size else None


def rx_frame_payload(record, true_len, stored):
    """The payload as it left the FIFO, with any suppressed tail put back."""
    at = 2 if record[0] & RX_LEN_LONG else 1
    at += 1                                     # past the offset byte
    body = record[at:at + stored]
    run = true_len - stored
    return body if not run else body + bytes([record[at + stored]]) * run


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
        # The newline that closes a run can arrive in the next read, so whether
        # one is still owed outlives a single pass over the buffer.
        self._pending_lf = False
        # The open run of binary frames, and the epoch it counts from. The epoch
        # outlives the run: a later one can refer back to it with a single byte.
        self._run_open = False
        self._run_pipe = 0
        self._run_len = 32
        self._run_base = None

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
        """Pulls whole lines and whole frame runs out, leaves the remainder.

        Two shapes share this stream. Replies, warnings and - unless `format bin`
        was asked for - frames arrive as newline-terminated ASCII. Under
        `format bin` frames arrive in runs, opened by a byte outside printable
        ASCII so that a run can never be mistaken for a line.

        Each frame is turned into the very line the firmware would have printed
        for it. That is the whole trick: the binary shape is a transport saving
        on the wire and nothing above this method can tell which one arrived.
        """
        while buffer:
            if self._run_open:
                buffer, done = self._drain_run(buffer)
                if not done:
                    return buffer
                continue

            # The newline that closed the last run terminates nothing - the end
            # marker before it already did - so swallow it rather than reporting
            # a blank line. It may arrive in the read after the marker.
            if self._pending_lf:
                self._pending_lf = False
                if buffer[:1] == LF:
                    buffer = buffer[1:]
                    continue

            start = min((i for i in (buffer.find(bytes([RX_RUN_NEW])),
                                     buffer.find(bytes([RX_RUN_MORE])))
                         if i >= 0), default=-1)
            end = buffer.find(LF)

            if start < 0 or (0 <= end < start):
                # Nothing binary before the next line end: ordinary line.
                if end < 0:
                    return buffer
                raw, buffer = buffer[:end], buffer[end + 1:]
                self.lines.put(raw.decode("ascii", errors="replace").rstrip())
                continue

            if start:
                # ASCII ahead of the run with no newline of its own. The firmware
                # does not emit that, so it is a fragment of something already
                # broken; hand it up rather than dropping it silently.
                raw, buffer = buffer[:start], buffer[start:]
                text = raw.decode("ascii", errors="replace").strip()
                if text:
                    self.lines.put(text)
                continue

            if buffer[0] == RX_RUN_NEW:
                if len(buffer) < RX_RUN_NEW_LEN:
                    return buffer                  # the epoch is still in flight
                self._run_pipe = buffer[1]
                self._run_len = buffer[2]
                # The epoch carries the whole of millis(), so there is nothing to
                # reconstruct - the byte a frame spends is spent on the offset.
                self._run_base = int.from_bytes(buffer[3:7], "little")
                buffer = buffer[RX_RUN_NEW_LEN:]
            else:
                if self._run_base is None:
                    # A run that leans on an epoch this host never heard - the
                    # dongle was already talking when we connected. Nothing can
                    # be placed in time, so skip to the end of it.
                    buffer = buffer[1:]
                    continue
                buffer = buffer[1:]
            self._run_open = True

        return buffer

    def _drain_run(self, buffer):
        """Frames until the run ends. Returns (remainder, run_finished)."""
        while buffer:
            if buffer[0] == RX_RUN_END:
                buffer = buffer[1:]
                self._run_open = False
                self._pending_lf = True
                return buffer, True
            try:
                sized = rx_frame_size(buffer, self._run_len)
            except ValueError as exc:
                # Not a frame and not the end: the run is not where we think it
                # is. Say so and go back to hunting for a run marker.
                self.lines.put(f"[binary stream lost: {exc}]")
                self._run_open = False
                return buffer[1:], True
            if sized is None:
                return buffer, False               # frame still in flight
            size, true_len, stored = sized
            record, buffer = buffer[:size], buffer[size:]
            self.lines.put(self._rx_line(record, true_len, stored))
        return buffer, False

    def _rx_line(self, record, true_len, stored):
        """Writes a frame out as the line the firmware would print for it.

        The saving is on the wire - three bytes of frame against a `t=`, a pipe,
        a length and a checksum spelled out in decimal and hex - so it belongs on
        the wire. Above this, one shape: everything that reads frames goes on
        reading `RX ...` lines and never learns which way they arrived.
        """
        at = 2 if record[0] & RX_LEN_LONG else 1
        stamp = self._run_base + record[at]
        payload = rx_frame_payload(record, true_len, stored)
        return (f"RX t={stamp} p{self._run_pipe} len={true_len} "
                f"crc={record[-1]:02X} {payload.hex().upper()}")

