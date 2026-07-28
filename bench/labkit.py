"""Shared plumbing for the hardware benches: put a sniffer dongle on the air,
send it frames, and read the receiver's verdict off its log over the network.

Extracted so the benches cannot drift apart in how they talk to the hardware -
a frame built one way in one bench and another way in the next makes their
results incomparable, which is the one thing a bench must not be.
"""
import atexit
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request

WEB_A = "http://127.0.0.1:8724"
WEB_B = "http://127.0.0.1:8725"

ADDR_FIXED = "4354484D45"   # CTHME - fixed 32 bytes, no auto-ack
ADDR_DYN = "4254484D45"     # BTHME - dynamic length, auto-ack

HDR = "D2FC44"              # BTHome service uuid (0xFCD2, little endian) + info


# ---- frame construction ------------------------------------------------------
def pad32(hexstr):
    """Fill to the 32-byte slot with 0xFF, the byte a fixed-length sender pads
    with. Note that it is not stripped on arrival: a value byte may be 0xFF."""
    return hexstr + "FF" * (32 - len(hexstr) // 2)


def payload(sender, pid, objects, header=HDR):
    """A BTHome frame as it goes on the air: sender id, service header, the
    packet id that identifies repeats, then the objects."""
    return f"{sender}{header}00{pid:02X}{objects}"


# ---- dongle web API ----------------------------------------------------------
def post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except (urllib.error.URLError, ConnectionError) as err:
        # The dongle service is restarted from time to time while it is being
        # worked on. One retry, said out loud - a bench that hides a lost
        # connection would report the receiver at fault for the silence.
        print(f"      note: {base} did not answer ({err}); retrying once", flush=True)
        time.sleep(3)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)


# What each dongle was last told to be, so it can be told again after the
# service restarts and comes back with no configuration.
_setup = {}


def configure(base, dpl=0, plsize=32, ack=0, channel=90, pipe1=ADDR_FIXED):
    """Put a dongle on the air. dpl=0/plsize=32 makes it a fixed-length sender
    like a converted remote; dpl=1/ack=1 is what the dynamic pipe needs."""
    _setup[base] = dict(dpl=dpl, plsize=plsize, ack=ack, channel=channel, pipe1=pipe1)
    parts = [f"listen ch={channel}", "rate=250", "crc=16", "aw=5", "pa=low",
             f"ack={ack}", f"dpl={dpl}"]
    if not dpl:
        parts.append(f"plsize={plsize}")
    parts.append(f"pipe1={pipe1}")
    return post(base, "/api/command", {"line": " ".join(parts), "wait": True})


def tx(base, body, address=ADDR_FIXED, repeat=1, gap=0, ack=False, pad=True):
    if pad:
        body = pad32(body)
    # Checked here rather than left to the dongle. The radio's slot is 32 bytes,
    # so an over-long frame is a fault in the test that built it, and saying so
    # locally names the length instead of leaving "ERR bad payload byte" to be
    # interpreted. It also separates that case from the transport glitch below.
    if len(body) % 2 or len(body) // 2 > 32:
        raise ValueError(f"payload is {len(body) / 2} bytes, the slot holds 32:\n  {body}")
    line = f"tx {address} {body} {'ack' if ack else 'noack'}"
    if repeat > 1:
        line += f" x{repeat}"
    if gap:
        line += f" gap={gap}"
    reply = post(base, "/api/command", {"line": line, "wait": True})
    # A dongle that came back from a service restart has forgotten what it was.
    # Told again once, out loud, and the transmit repeated - the alternative is
    # a run that blames the receiver for hearing nothing.
    if not reply.get("ok", False) and "unconfigured" in str(reply.get("reply", "")):
        print(f"      note: {base} lost its configuration; setting it up again", flush=True)
        configure(base, **_setup.get(base, {}))
        reply = post(base, "/api/command", {"line": line, "wait": True})
    # The dongle refuses a well-formed 32-byte frame now and then - measured, ten
    # identical repeats of a line it had just rejected all went out. The payload
    # was checked above, so this is the serial path rather than the frame; one
    # retry, out loud. A refusal that survives it is a real result and stops the
    # run, because a bench that ignores a refused stimulus reads the receiver's
    # silence as a fault in the receiver.
    if not reply.get("ok", False):
        print(f"      note: {base} refused a checked payload "
              f"({reply.get('reply')!r}); retrying once", flush=True)
        time.sleep(0.3)
        reply = post(base, "/api/command", {"line": line, "wait": True})
    if not reply.get("ok", False):
        raise RuntimeError(f"dongle refused the transmit: {reply.get('reply')!r}\n  {line}")
    return reply


# ---- the hub's view, read off its log over the network -----------------------
_lines = []
_lock = threading.Lock()
_proc = None


def _reader(proc):
    for raw in proc.stdout:
        with _lock:
            _lines.append(raw.rstrip())


def hub_start(yaml_path, ip):
    """Attach to the receiver's log and wait for the handshake - starting a test
    before it lands means running deaf and blaming the component for it."""
    global _proc
    # The reader holds one of the receiver's few API connection slots for as long
    # as it lives. A bench that dies on an exception used to leave it behind, and
    # enough of those and the next run cannot get a connection at all - which
    # reads as "the hub is unreachable" and sends you looking at the hardware.
    atexit.register(hub_stop)
    _proc = subprocess.Popen(
        ["esphome", "logs", str(yaml_path), "--device", ip],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=_reader, args=(_proc,), daemon=True).start()
    for _ in range(150):
        if hub_grep("handshake with"):
            return True
        time.sleep(0.2)
    return False


def hub_stop():
    if _proc:
        _proc.terminate()


def hub_grep(pattern):
    rx = re.compile(pattern)
    with _lock:
        return [line for line in _lines if rx.search(line)]


def hub_clear():
    with _lock:
        _lines.clear()


def hub_dump():
    with _lock:
        return list(_lines)


# ---- verdicts ----------------------------------------------------------------
results = []


def verdict(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}", flush=True)


def settle(seconds=1.5):
    time.sleep(seconds)


def summary():
    print("\n--- summary ---")
    for name, ok, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 0 if not failed else 1


def exit_with_summary():
    hub_stop()
    sys.exit(summary())
