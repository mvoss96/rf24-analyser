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

Only dependency: pyserial.

    python nrf24term.py COM18
    python nrf24term.py COM18 --pretty
    python nrf24term.py /dev/ttyUSB0 --baud 115200
"""

import argparse
import sys
import threading
import time

try:
    import serial  # pyserial
except ImportError:
    sys.stderr.write("pyserial is required: pip install -r requirements.txt\n")
    sys.exit(1)


# --- BTHome v2 decoding -----------------------------------------------------

BTHOME_UUID = (0xD2, 0xFC)  # service UUID 0xFCD2, little-endian on the wire

BUTTON_EVENTS = {
    0x00: "none",
    0x01: "press",
    0x02: "double press",
    0x03: "triple press",
    0x04: "long press",
    0x05: "long double press",
    0x06: "long triple press",
    0x80: "hold press",
}

DIMMER_EVENTS = {
    0x00: "none",
    0x01: "rotate left",
    0x02: "rotate right",
}

COMMAND_EVENTS = {
    0x00: "off",
    0x01: "on",
    0x02: "toggle",
    0x03: "step up",
    0x04: "step down",
}

# Fixed-length BTHome objects -> number of value bytes (excluding the id byte).
FIXED_LEN = {
    0x00: 1,  # packet id
    0x01: 1,  # battery %
    0x0C: 2,  # voltage, uint16 LE, factor 0.001 V
    0x3A: 1,  # button event
}


def object_value_len(oid, rest):
    """Number of value bytes for object `oid`; `rest` are the bytes after the id.

    Several BTHome objects are variable length, so the count depends on the
    payload itself:
      0x3C dimmer  - None (0x00) carries no step byte, rotate events do
      0x3B command - [argument count][opcode][arguments...]
      0x53 / 0x54  - text / raw, prefixed with an explicit length byte
    Returns None when the length cannot be inferred (unknown object).
    """
    if oid == 0x3C:
        if not rest:
            return None
        return 1 if rest[0] == 0x00 else 2
    if oid == 0x3B:
        if not rest:
            return None
        return 2 + rest[0]
    if oid in (0x53, 0x54):
        if not rest:
            return None
        return 1 + rest[0]
    return FIXED_LEN.get(oid)


def _ascii(b):
    return chr(b) if 0x20 <= b <= 0x7E else "."


def decode_frame(data):
    """Decode a raw frame (list of ints) into a list of text lines."""
    lines = []
    if len(data) < 4:
        return ["  (frame too short for a 4-byte sender id)"]

    sender = data[0:4]
    sender_hex = ":".join(f"{b:02X}" for b in sender)
    sender_ascii = "".join(_ascii(b) for b in sender)
    lines.append(f"  sender    : {sender_hex}  \"{sender_ascii}\"")

    sd = data[4:]
    if len(sd) < 3 or (sd[0], sd[1]) != BTHOME_UUID:
        lines.append("  (no BTHome service data: expected D2 FC UUID)")
        if sd:
            lines.append("  raw       : " + " ".join(f"{b:02X}" for b in sd))
        return lines

    info = sd[2]
    version = (info >> 5) & 0x07
    flags = []
    if info & 0x01:
        flags.append("encrypted")
    if info & 0x04:
        flags.append("trigger-based")
    lines.append(f"  bthome    : v{version}" + (" " + ", ".join(flags) if flags else ""))

    obj = sd[3:]
    i = 0
    button_n = 0
    dimmer_n = 0
    while i < len(obj):
        oid = obj[i]
        vlen = object_value_len(oid, obj[i + 1:])
        if vlen is None:
            lines.append(
                f"  0x{oid:02X}      : unknown object, raw = "
                + " ".join(f"{b:02X}" for b in obj[i:])
            )
            break
        val = obj[i + 1 : i + 1 + vlen]
        if len(val) < vlen:
            lines.append(f"  0x{oid:02X}      : truncated value")
            break

        if oid == 0x00:
            lines.append(f"  packet id : {val[0]}")
        elif oid == 0x01:
            lines.append(f"  battery   : {val[0]} %")
        elif oid == 0x0C:
            mv = val[0] | (val[1] << 8)
            lines.append(f"  voltage   : {mv / 1000:.3f} V")
        elif oid == 0x3A:
            button_n += 1
            name = BUTTON_EVENTS.get(val[0], f"0x{val[0]:02X}")
            lines.append(f"  button {button_n}  : {name}")
        elif oid == 0x3C:
            dimmer_n += 1
            name = DIMMER_EVENTS.get(val[0], f"0x{val[0]:02X}")
            # None is a placeholder addressing a later dimmer and has no steps.
            steps = f" ({val[1]} steps)" if len(val) > 1 else ""
            lines.append(f"  dimmer {dimmer_n}  : {name}{steps}")
        elif oid == 0x3B:
            opcode = COMMAND_EVENTS.get(val[1], f"0x{val[1]:02X}")
            args = " ".join(f"{b:02X}" for b in val[2:])
            lines.append(f"  command   : {opcode}" + (f" [{args}]" if args else ""))
        elif oid in (0x53, 0x54):
            payload = bytes(val[1:])
            if oid == 0x53:
                shown = payload.decode("utf-8", errors="replace")
                lines.append(f"  text      : \"{shown}\"")
            else:
                lines.append("  raw       : " + " ".join(f"{b:02X}" for b in payload))

        i += 1 + vlen

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
