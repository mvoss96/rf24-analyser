"""Serial client for the nrf24-sniffer dongle.

Speaks the line-based ASCII protocol described in README.md and is shared by the
terminal (nrf24term.py) and the GUI (nrf24gui.py) so there is exactly one
implementation of it.

A reader thread turns the byte stream into lines and puts them on a queue;
callers drain it however suits them (a print loop, a tkinter after() tick).
"""

import queue
import threading

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - reported by the entry points
    serial = None
    list_ports = None

# Command-protocol version this client speaks; compared against the firmware's
# greeting so a mismatch is reported instead of producing cryptic errors.
EXPECTED_API = 4

DEFAULT_BAUD = 500000


def available_ports():
    """[(device, description), ...] for every serial port on the system."""
    if list_ports is None:
        return []
    return [(p.device, p.description or "") for p in list_ports.comports()]


def parse_greeting(line):
    """Parses 'NRF24SNIFFER fw=.. api=.. state=.. hw=.. ce=..' into a dict.

    Returns None for any other line.
    """
    if not line.startswith("NRF24SNIFFER"):
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
                   "rxmode", "rxdbg", "rx", "fifofull")


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

    @property
    def is_open(self):
        return self._serial is not None

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
        if self._serial is None:
            raise RuntimeError("not connected")
        self._serial.write((line.rstrip("\r\n") + "\n").encode("ascii", errors="replace"))

    def _read_loop(self):
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = self._serial.read(512)
            except Exception as exc:
                self.lines.put(f"[serial error: {exc}]")
                break
            if not chunk:
                continue
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                self.lines.put(raw.decode("ascii", errors="replace").rstrip("\r"))
