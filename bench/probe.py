"""Drive one or more dongles directly on their serial ports, with the RX-FIFO
trace on, and provoke the real RotRemote between measurements.

The web UI cannot be used for this: the per-pass DBG lines are not frames, so
they only reach a browser's log pane. Here every line is timestamped and kept.

Two ports in one process is the point of the multi-port form: a phantom frame
that only one of two receivers reports cannot have been on the air, and the
comparison only holds if both heard the very same click.

  probe.py --port COM18 --mode 2 --clicks 5
  probe.py --port COM18 --port COM25 --mode 0 --clicks 5
  probe.py --port COM18 --mode 0 --after "reg 01 02"
"""
import argparse, threading, time
import serial

REMOTE_PORT = "COM9"


ADDR = "42:54:48:4D:45"
PA = "low"


def listen_line(channel, addr=None):
    # A channel argument is not cosmetic: the ESP32 in the room enables auto-ack
    # on its RX pipes, and on these chips that means it answers frames flagged
    # NO_ACK. Its acknowledgements collide with the traffic under measurement, so
    # a lab run has to move off the channel it listens on.
    #
    # A per-port address matters for the same reason: the answers to a frame go
    # out on the address of whoever answers, so hearing them means listening
    # there, not where the frame was sent. Two dongles, two addresses, one click.
    return (f"listen ch={channel} rate=250 crc=16 aw=5 pa={PA} ack=0 dpl=1 "
            f"pipe1={addr or ADDR}")

ap = argparse.ArgumentParser()
ap.add_argument("--port", action="append", default=[])
ap.add_argument("--mode", type=int, default=None, help="rxmode 0|1|2")
ap.add_argument("--clicks", type=int, default=5)
ap.add_argument("--dbg", type=int, default=1)
ap.add_argument("--no-listen", action="store_true")
ap.add_argument("--settle", type=float, default=2.5, help="seconds after a click")
ap.add_argument("--channel", type=int, default=100)
ap.add_argument("--addr", default="42:54:48:4D:45")
ap.add_argument("--pa", default="low")
ap.add_argument("--log", default=None)
ap.add_argument("--after", action="append", default=[],
                help="extra firmware command after listen, repeatable "
                     "(e.g. --after 'reg 01 02')")
ap.add_argument("--watch", action="append", default=[],
                help="PORT[:BAUD] to log only, never command - a third-party "
                     "receiver (the ESP32) as an independent witness")
ap.add_argument("--tx-port", default=None,
                help="dongle to transmit the stimulus from, instead of clicking "
                     "the remote; it is configured like the listeners")
ap.add_argument("--tx-cmd", action="append", default=[],
                help="one firmware tx line per round, repeatable")
ap.add_argument("--dwell", type=float, default=0,
                help="seconds to just listen and log, instead of provoking "
                     "anything - for asking what is already on a channel")
args = ap.parse_args()
ADDR = args.addr
PA = args.pa
ports = args.port or ["COM18"]

t0 = time.time()
logfile = open(args.log, "w", encoding="utf-8") if args.log else None
emit_lock = threading.Lock()


def emit(text):
    with emit_lock:
        out = f"{time.time() - t0:8.3f}  {text}"
        print(out, flush=True)
        if logfile:
            logfile.write(out + "\n")
            logfile.flush()


def reader(ser, tag):
    buf = b""
    while not stop.is_set():
        try:
            chunk = ser.read(256)
        except Exception:
            return
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("ascii", errors="replace").strip()
            if text:
                emit(f"{tag} < {text}")


def send(ser, tag, line, wait=0.4):
    emit(f"{tag} > {line}")
    ser.write((line + "\n").encode())
    ser.flush()
    time.sleep(wait)


def click(n):
    """One DTR pulse on the remote's debug port = one button press."""
    s = serial.Serial()
    s.port = REMOTE_PORT
    s.baudrate = 115200
    s.timeout = 0.2
    s.dtr = True
    s.open()
    time.sleep(0.05)
    s.dtr = False
    out = b""
    end = time.time() + 2.0
    while time.time() < end:
        chunk = s.read(256)
        if chunk:
            out += chunk
    s.close()
    text = out.decode("ascii", errors="replace").strip().replace("\r\n", " | ")
    emit(f"# click {n}: {text}")


stop = threading.Event()
sers = {}
watchers = []
for spec in args.watch:
    port, _, baud = spec.partition(":")
    w = serial.Serial(port, int(baud or 115200), timeout=0.2)
    watchers.append(w)
    threading.Thread(target=reader, args=(w, port.rjust(5)), daemon=True).start()
port_addr = {}
for spec in ports:
    # "COM25@99:6C:CA:80:01" listens on that address instead of the default one.
    port, _, addr = spec.partition("@")
    tag = port.rjust(5)
    port_addr[tag] = addr or None
    sers[tag] = serial.Serial(port, 500000, timeout=0.2)  # firmware speaks 500k
    threading.Thread(target=reader, args=(sers[tag], tag), daemon=True).start()
# The adapters pulled DTR on open, so the dongles are booting: let the greeting land.
time.sleep(2.0)

try:
    for tag, ser in sers.items():
        send(ser, tag, "status")
        if not args.no_listen:
            send(ser, tag, listen_line(args.channel, port_addr.get(tag)), wait=1.0)
        if args.mode is not None:
            send(ser, tag, f"rxmode {args.mode}")
        send(ser, tag, f"rxdbg {args.dbg}")
        for extra in args.after:
            send(ser, tag, extra)
        send(ser, tag, "regs", wait=0.8)
    emit("# --- provoking ---")
    if args.dwell > 0:
        time.sleep(args.dwell)
    elif args.tx_cmd:
        # A sending dongle instead of the remote: the only way to control what a
        # burst contains, which is what separates "identical copies" from
        # "different payloads" as the trigger.
        tx = serial.Serial(args.tx_port, 500000, timeout=0.2)
        threading.Thread(target=reader, args=(tx, "  TX"), daemon=True).start()
        time.sleep(2.0)
        send(tx, "  TX", listen_line(args.channel), wait=1.0)
        for i, line in enumerate(args.tx_cmd):
            emit(f"# round {i + 1}")
            send(tx, "  TX", line, wait=1.0)
            time.sleep(args.settle)
        tx.close()
    else:
        for i in range(args.clicks):
            click(i + 1)
            time.sleep(args.settle)
    emit("# --- done ---")
    for tag, ser in sers.items():
        send(ser, tag, "status")
finally:
    stop.set()
    time.sleep(0.3)
    for ser in list(sers.values()) + watchers:
        ser.close()
    if logfile:
        logfile.close()
