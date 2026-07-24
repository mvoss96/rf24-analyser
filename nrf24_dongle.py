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
EXPECTED_API = 2

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


def parse_rx(line):
    """Parses 'RX p<pipe> len=<n> <hex>' into (pipe, data) or None.

    Accepts both the compact hex the firmware emits ("4D565202...") and the
    space-separated form older logs used.
    """
    if not line.startswith("RX "):
        return None
    pipe = None
    tokens = []
    for token in line.split()[1:]:
        if token.startswith("p") and pipe is None:
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
    return pipe, data


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
    """Builds a full `listen` line. `pipes` maps pipe number -> address string."""
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
