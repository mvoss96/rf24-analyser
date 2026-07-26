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
import sys
import threading
import time

try:
    import serial  # pyserial
except ImportError:
    sys.stderr.write("pyserial is required: pip install -r requirements.txt\n")
    sys.exit(1)


# --- Decoding ---------------------------------------------------------------
#
# Frame decoding and the serial protocol live in shared modules so the terminal
# and the GUI (nrf24gui.py) can never drift apart.

import nrf24_dongle as dongle
import nrf24_parsers as parsers

EXPECTED_API = dongle.EXPECTED_API


def decode_frame(data, parser_name="bthome"):
    """Decode a raw frame (list of ints) into a list of text lines."""
    parser = parsers.get(parser_name) or parsers.get("raw")
    reason = parser.available()
    if reason:
        return [f"  (decoder {parser.label} unavailable: {reason})"]
    return parser.detail(data)


def try_pretty(line):
    """If line is an 'RX ...' frame, return decoded text; else None.

    Parsing goes through the shared client rather than a second copy here - the
    copy that used to live in this file silently stopped decoding anything the
    day the firmware added a timestamp to the RX line.
    """
    received = dongle.parse_rx(line)
    if received is None:
        return None
    stamp, pipe, data, intact = received
    header = f"-- RX pipe {pipe}  ({len(data)} bytes)"
    if stamp is not None:
        header += f"  t={stamp}ms"
    if intact is False:
        # Decoding it would dress up corruption as a measurement.
        return "\n".join([header + "  !! CHECKSUM MISMATCH",
                          "   corrupted between radio and host - not what the radio received",
                          *parsers.hexdump(data)])
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
