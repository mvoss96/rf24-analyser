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

# Known BTHome object ids -> number of value bytes (excluding the id byte).
# Objects not listed here cannot be length-decoded, so parsing stops and the
# remainder is dumped as raw hex.
OBJECT_LEN = {
    0x00: 1,  # packet id
    0x01: 1,  # battery %
    0x0C: 2,  # voltage, uint16 LE, factor 0.001 V
    0x3A: 1,  # button event
    0x3C: 2,  # dimmer event: direction + steps
}


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
        vlen = OBJECT_LEN.get(oid)
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
            lines.append(f"  dimmer {dimmer_n}  : {name} ({val[1]} steps)")

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
    try:
        data = [int(t, 16) for t in hex_tokens]
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


# Radio config applied by ':preset bthome' (matches the firmware defaults, and
# starts listening).
PRESET_BTHOME = [
    "stop", "aw 5", "crc 16", "rate 250", "ch 100",
    "dpl 1", "ack 0", "pa low", "pipe 1 42:54:48:4D:45", "listen",
]

LOCAL_HELP = """\
Local commands (handled by nrf24term, not sent to the dongle):
  :pretty on|off    toggle BTHome pretty-printing of RX frames
  :preset bthome    apply the full BTHome-over-nRF24 config and listen
  :scan [passes]    run a channel activity scan (default 64 passes)
  :log <file>       tee everything from the dongle to a file
  :log off          stop logging
  :help             show this help
  :quit / :exit     close the terminal

Everything else is sent verbatim to the dongle. Dongle commands:
  ch <0-125>            rate <250|1000|2000>   crc <0|8|16>    aw <3|4|5>
  pipe <0-5> <addr|off> ack <0|1>              dpl <0|1>       plsize <1-32>
  pa <min|low|high|max> listen   stop   info   scan [passes]
  tx <addr> <hex...> [ack|noack]
"""


def main():
    ap = argparse.ArgumentParser(description="Serial terminal for the nrf24-sniffer dongle.")
    ap.add_argument("port", help="serial port, e.g. COM18 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200, help="baud rate (default 115200)")
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
