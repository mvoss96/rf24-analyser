#!/usr/bin/env python3
"""nrf24term - serial terminal / REPL for the nrf24-sniffer dongle.

A reader thread prints everything the dongle emits (command replies, live RX
lines, scan results); the main thread forwards typed lines to the dongle. Local
commands start with ':' and are handled here rather than sent on.

Comfort features:
  * --pretty / :pretty on|off   decode RX frames as BTHome v2
  * :preset bthome              apply the full BTHome-over-nRF24 radio config
  * :log <file> / :log off      tee everything from the dongle to a file
  * :scan [passes]              convenience wrapper for the dongle scan command
  * scan results render as a small activity bar

BTHome decoding is done by bthome-ble, the reference parser Home Assistant uses,
so what this tool shows is what a real BTHome receiver would see - including
nothing, when a frame does not follow the spec.

Dependencies: pyserial, bthome-ble (see requirements.txt).

    python nrf24term.py COM18
    python nrf24term.py COM18 --pretty
    python nrf24term.py /dev/ttyUSB0 --baud 115200
"""

import argparse
import logging
import sys
import threading
import time

try:
    import serial  # pyserial
except ImportError:
    sys.stderr.write("pyserial is required: pip install -r requirements.txt\n")
    sys.exit(1)


# --- BTHome v2 decoding -----------------------------------------------------
#
# Object parsing is delegated entirely to bthome-ble, the reference parser that
# Home Assistant uses. Nothing about BTHome measurements is reimplemented here:
# a hand-maintained length table drifts from the spec (this tool already shipped
# one that mis-decoded dimmer events), and leaning on the reference turns the
# sniffer into a conformance check - a frame bthome-ble cannot read is a frame
# no standard BTHome receiver can read either.
#
# Only the nRF24 envelope is ours: [4-byte sender id][D2 FC][device info],
# followed by the BTHome object bytes that are handed to the library.

try:
    from bthome_ble.parser import BTHomeBluetoothDeviceData, BTHomeVersion
except ImportError:
    sys.stderr.write(
        "bthome-ble is required for decoding: pip install -r requirements.txt\n"
    )
    sys.exit(1)

BTHOME_UUID = (0xD2, 0xFC)  # service UUID 0xFCD2, little-endian on the wire


def _ascii(b):
    return chr(b) if 0x20 <= b <= 0x7E else "."


class _LogCollector(logging.Handler):
    """Captures what bthome-ble logs while parsing one frame.

    The library explains why it rejected a payload (objects out of order, bad
    length) only through logging - and that reasoning is exactly what a protocol
    debugger needs to see.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


def _parse_objects(payload):
    """Run the reference parser over the BTHome object bytes.

    A fresh parser per frame is deliberate: the library deduplicates by packet
    id, which would hide the retransmissions a sniffer exists to show, and it
    keeps one sender's state out of the next sender's frame.
    """
    parser = BTHomeBluetoothDeviceData()
    parser.bthome_version = BTHomeVersion.V2

    collector = _LogCollector()
    logger = logging.getLogger("bthome_ble")
    previous_level = logger.level
    logger.addHandler(collector)
    logger.setLevel(logging.DEBUG)
    try:
        parser._parse_payload(bytes(payload), 0.0)
    except Exception as exc:  # private API; guard against upstream changes
        collector.records.append((logging.ERROR, f"{type(exc).__name__}: {exc}"))
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)

    return (
        getattr(parser, "_sensor_values_updates", {}) or {},
        getattr(parser, "_events_updates", {}) or {},
        getattr(parser, "_sensor_descriptions_updates", {}) or {},
        collector.records,
    )


def decode_frame(data):
    """Decode a raw frame (list of ints) into a list of text lines."""
    if len(data) < 4:
        return ["  (frame too short for a 4-byte sender id)"]

    sender = data[0:4]
    sender_hex = ":".join(f"{b:02X}" for b in sender)
    sender_ascii = "".join(_ascii(b) for b in sender)
    lines = [f'  sender    : {sender_hex}  "{sender_ascii}"']

    sd = data[4:]
    if len(sd) < 3 or (sd[0], sd[1]) != BTHOME_UUID:
        lines.append("  (no BTHome service data: expected D2 FC UUID)")
        if sd:
            lines.append("  raw       : " + " ".join(f"{b:02X}" for b in sd))
        return lines

    info = sd[2]
    flags = []
    if info & 0x01:
        flags.append("encrypted")
    if info & 0x04:
        flags.append("trigger-based")
    version = (info >> 5) & 0x07
    lines.append(f"  bthome    : v{version}" + (" " + ", ".join(flags) if flags else ""))

    payload = sd[3:]
    sensors, events, units, records = _parse_objects(payload)

    for key, value in sensors.items():
        desc = units.get(key)
        unit = ""
        if desc is not None and desc.native_unit_of_measurement:
            unit = f" {desc.native_unit_of_measurement}"
        lines.append(f"  {value.name:<10}: {value.native_value}{unit}")

    for value in events.values():
        props = value.event_properties or {}
        detail = ""
        if props:
            detail = " (" + ", ".join(f"{k}={v}" for k, v in props.items()) + ")"
        lines.append(f"  {value.name:<10}: {value.event_type}{detail}")

    if not sensors and not events:
        # Nothing came out of the reference parser, so this frame would be
        # dropped by any spec-conformant receiver.
        lines.append("  !! REJECTED by the reference parser (bthome-ble)")
        lines.append("  objects   : " + " ".join(f"{b:02X}" for b in payload))
        shown = records
    else:
        # Anything the library warned about is worth surfacing even on success.
        shown = [r for r in records if r[0] >= logging.WARNING]
    for _level, message in shown:
        lines.append(f"  reason    : {message}")

    return lines


def try_pretty(line):
    """If line is an 'RX ...' frame, return decoded text; else None."""
    if not line.startswith("RX "):
        return None
    parts = line.split()
    hex_tokens = []
    pipe = "?"
    length = "?"
    for tok in parts[1:]:
        if tok.startswith("p"):
            pipe = tok[1:]
        elif tok.startswith("len="):
            length = tok[4:]
        else:
            hex_tokens.append(tok)
    # The firmware emits compact hex ("4D565202..."); older logs used
    # space-separated bytes. Joining handles both.
    blob = "".join(hex_tokens)
    if not blob or len(blob) % 2:
        return None
    try:
        data = [int(blob[i:i + 2], 16) for i in range(0, len(blob), 2)]
    except ValueError:
        return None
    header = f"-- RX pipe {pipe}  ({length} bytes)"
    return "\n".join([header] + decode_frame(data))


def render_scan(line):
    """Turn 'SCAN ch=<n> hits=<h>' into a labelled bar; else return None."""
    if not line.startswith("SCAN ch="):
        return None
    try:
        _, ch_tok, hits_tok = line.split()
        ch = int(ch_tok.split("=")[1])
        hits = int(hits_tok.split("=")[1])
    except (ValueError, IndexError):
        return None
    freq = 2400 + ch  # MHz
    bar = "#" * min(hits, 40)
    return f"  ch {ch:3d} ({freq} MHz) {hits:3d} {bar}"


# --- Terminal ---------------------------------------------------------------

def _emit(state, text):
    sys.stdout.write(text + "\n")
    lf = state.get("logfile")
    if lf:
        lf.write(text + "\n")
        lf.flush()


def reader_loop(ser, state):
    buf = b""
    while not state["stop"]:
        try:
            chunk = ser.read(256)
        except serial.SerialException:
            _emit(state, "[serial disconnected]")
            state["stop"] = True
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("ascii", errors="replace").rstrip("\r")

            if line.startswith("NRF24SNIFFER"):
                _emit(state, line)
                api = None
                for field in line.split():
                    if field.startswith("api="):
                        api = field[4:]
                if api is not None and api != str(EXPECTED_API):
                    _emit(state, f"[warning] firmware speaks api={api}, "
                                 f"this tool expects api={EXPECTED_API}")
                continue

            scan = render_scan(line)
            if scan is not None:
                _emit(state, scan)
                continue
            if state["pretty"]:
                pretty = try_pretty(line)
                if pretty is not None:
                    _emit(state, pretty)
                    continue
            _emit(state, line)
        sys.stdout.flush()


# Command-protocol version this tool speaks; compared against the firmware's
# greeting so a mismatch is reported instead of producing cryptic errors.
EXPECTED_API = 2

# The firmware has no built-in pin map and no default radio settings - the host
# owns both. This is the wiring of the ATmega328P + CH340 + nRF24L01 dongle...
PRESET_HW = "hwset ce=9 csn=10 irq=2 led_rx=8 led_tx=A1"

# ...and the radio configuration of the BTHome-over-nRF24 protocol.
PRESET_BTHOME = [
    PRESET_HW,
    "listen ch=100 rate=250 crc=16 aw=5 pa=low ack=0 dpl=1 pipe1=42:54:48:4D:45",
]

LOCAL_HELP = """\
Local commands (handled by nrf24term, not sent to the dongle):
  :pretty on|off    toggle BTHome pretty-printing of RX frames
  :preset bthome    send hwset for this dongle + the BTHome listen line
  :scan [passes]    run a channel activity scan (default 64 passes)
  :log <file>       tee everything from the dongle to a file
  :log off          stop logging
  :help             show this help
  :quit / :exit     close the terminal

Everything else is sent verbatim to the dongle. Dongle commands:
  hwset ce=<pin> csn=<pin> [irq=<pin|none>] [led_rx=<pin|none>] [led_tx=<pin|none>]
  listen ch= rate= crc= aw= pa= ack= dpl= [plsize=] pipeN=<addr>
  listen | stop | info | scan [passes] | repeats <0|1>
  tx <addr> <hex...> [ack|noack]

The firmware has no defaults: hwset defines the wiring, listen the radio
parameters. Both are mandatory before anything is received.
"""


def main():
    ap = argparse.ArgumentParser(description="Serial terminal for the nrf24-sniffer dongle.")
    ap.add_argument("port", help="serial port, e.g. COM18 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=500000, help="baud rate (default 500000)")
    ap.add_argument("--pretty", action="store_true", help="decode RX frames as BTHome v2")
    args = ap.parse_args()

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0.1)
    except serial.SerialException as e:
        sys.stderr.write(f"could not open {args.port}: {e}\n")
        sys.exit(1)

    # Opening the port toggles DTR and resets the ATmega; give it a moment.
    time.sleep(2.0)
    ser.reset_input_buffer()

    state = {"stop": False, "pretty": args.pretty, "logfile": None}
    t = threading.Thread(target=reader_loop, args=(ser, state), daemon=True)
    t.start()

    sys.stdout.write(f"connected to {args.port} @ {args.baud} "
                     f"(pretty={'on' if args.pretty else 'off'}). "
                     f"Type :help for local commands, Ctrl-C to quit.\n")
    ser.write(b"info\n")

    def send(line):
        ser.write((line + "\n").encode("ascii", errors="replace"))

    try:
        while not state["stop"]:
            try:
                line = input()
            except EOFError:
                break

            s = line.strip()
            if s in (":quit", ":exit"):
                break
            if s == ":help":
                sys.stdout.write(LOCAL_HELP)
                continue
            if s == ":pretty on":
                state["pretty"] = True
                sys.stdout.write("[pretty on]\n")
                continue
            if s == ":pretty off":
                state["pretty"] = False
                sys.stdout.write("[pretty off]\n")
                continue
            if s == ":preset bthome":
                for cmd in PRESET_BTHOME:
                    send(cmd)
                    time.sleep(0.05)
                sys.stdout.write("[preset bthome applied]\n")
                continue
            if s.startswith(":scan"):
                parts = s.split()
                passes = parts[1] if len(parts) > 1 else ""
                send(("scan " + passes).strip())
                continue
            if s.startswith(":log"):
                parts = s.split(maxsplit=1)
                if len(parts) == 2 and parts[1] != "off":
                    if state["logfile"]:
                        state["logfile"].close()
                    state["logfile"] = open(parts[1], "a", encoding="utf-8")
                    sys.stdout.write(f"[logging to {parts[1]}]\n")
                else:
                    if state["logfile"]:
                        state["logfile"].close()
                        state["logfile"] = None
                    sys.stdout.write("[logging off]\n")
                continue

            send(line)
    except KeyboardInterrupt:
        pass
    finally:
        state["stop"] = True
        time.sleep(0.2)
        if state["logfile"]:
            state["logfile"].close()
        ser.close()
        sys.stdout.write("\nbye\n")


if __name__ == "__main__":
    main()
