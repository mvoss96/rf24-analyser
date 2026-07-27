"""Shared plumbing for the hardware benches: put a sniffer dongle on the air,
send it frames, and read the receiver's verdict off its log over the network.

Extracted so the benches cannot drift apart in how they talk to the hardware -
a frame built one way in one bench and another way in the next makes their
results incomparable, which is the one thing a bench must not be.
"""
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def configure(base, dpl=0, plsize=32, ack=0, channel=90, pipe1=ADDR_FIXED):
    """Put a dongle on the air. dpl=0/plsize=32 makes it a fixed-length sender
    like a converted remote; dpl=1/ack=1 is what the dynamic pipe needs."""
    parts = [f"listen ch={channel}", "rate=250", "crc=16", "aw=5", "pa=low",
             f"ack={ack}", f"dpl={dpl}"]
    if not dpl:
        parts.append(f"plsize={plsize}")
    parts.append(f"pipe1={pipe1}")
    return post(base, "/api/command", {"line": " ".join(parts), "wait": True})


def tx(base, body, address=ADDR_FIXED, repeat=1, gap=0, ack=False, pad=True):
    if pad:
        body = pad32(body)
    line = f"tx {address} {body} {'ack' if ack else 'noack'}"
    if repeat > 1:
        line += f" x{repeat}"
    if gap:
        line += f" gap={gap}"
    return post(base, "/api/command", {"line": line, "wait": True})


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
