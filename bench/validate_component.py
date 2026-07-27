"""Full validation of the esphome nrf24 / nrf24_bthome component against the
Ethernet hub at 192.168.2.70.

Two sniffer dongles play two different registered senders (AA:01:00:01 and
AA:01:00:02), a third sender id is deliberately not registered. Stimulus goes
out through each dongle's web API; the receiver's verdict is read from its log
over the network via `esphome logs`, so no serial port is involved and the two
senders can transmit independently.

Covers: per-sender attribution, the repeat dedup, packet-id aging at timeout,
the dynamic and the fixed pipe side by side, and the rejection paths -
unregistered sender, wrong service uuid, truncated objects, unknown object id,
zero padding.

Verdicts print per test so a failure names itself.

    python bench/validate_component.py
"""
import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

WEB_A = "http://127.0.0.1:8724"
WEB_B = "http://127.0.0.1:8725"
HUB_YAML = Path(r"C:\Repos\libs\esphome-rf24-remote\tests\wt32-eth01.yaml")
HUB_IP = "192.168.2.70"

ADDR_FIXED = "4354484D45"   # CTHME - fixed 32 bytes, no auto-ack
ADDR_DYN = "4254484D45"     # BTHME - dynamic length, auto-ack

SENDER_A = "AA010001"
SENDER_B = "AA010002"
SENDER_UNKNOWN = "DEADBEEF"

HDR = "D2FC44"              # BTHome service uuid (0xFCD2, little endian) + info


# ---- frame construction ------------------------------------------------------
def pad32(hexstr):
    """Fill to the 32-byte slot with 0xFF, the padding byte the transport strips."""
    return hexstr + "FF" * (32 - len(hexstr) // 2)


def click(sender, pid, header=HDR):
    """A click exactly as the RotRemote sends it: packet id, battery, voltage,
    button press."""
    return f"{sender}{header}00{pid:02X}01640C3C0D3A01"


def dimmer(sender, pid, index=1, right=True, steps=5):
    """index=1 is a plain rotation, index=2 the held rotation - the k-th 0x3C
    object addresses dimmer k, so index 2 needs a None entry in front."""
    ev = "02" if right else "01"
    body = "3C0000" if index == 2 else ""
    return f"{sender}{HDR}00{pid:02X}{body}3C{ev}{steps:02X}"


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


def tx(base, payload, address=ADDR_FIXED, repeat=1, gap=0, ack=False, pad=True):
    if pad:
        payload = pad32(payload)
    line = f"tx {address} {payload} {'ack' if ack else 'noack'}"
    if repeat > 1:
        line += f" x{repeat}"
    if gap:
        line += f" gap={gap}"
    return post(base, "/api/command", {"line": line, "wait": True})


# ---- the hub's view, read off its log over the network -----------------------
hub_lines = []
hub_lock = threading.Lock()
hub_proc = None


def hub_reader(proc):
    for raw in proc.stdout:
        text = raw.rstrip()
        with hub_lock:
            hub_lines.append(text)


def hub_start():
    global hub_proc
    hub_proc = subprocess.Popen(
        ["esphome", "logs", str(HUB_YAML), "--device", HUB_IP],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1)
    threading.Thread(target=hub_reader, args=(hub_proc,), daemon=True).start()
    # Wait for the handshake, otherwise the first test runs deaf.
    for _ in range(150):
        if hub_grep("handshake with"):
            return True
        time.sleep(0.2)
    return False


def hub_grep(pattern):
    rx = re.compile(pattern)
    with hub_lock:
        return [l for l in hub_lines if rx.search(l)]


def hub_clear():
    with hub_lock:
        hub_lines.clear()


# ---- verdicts ----------------------------------------------------------------
results = []


def verdict(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}", flush=True)


def settle(seconds=1.5):
    time.sleep(seconds)


# ---- run ---------------------------------------------------------------------
print(f"attaching to hub log at {HUB_IP} ...", flush=True)
if not hub_start():
    print("FATAL: no handshake with the hub - is it reachable?")
    sys.exit(2)
print("attached", flush=True)

for base, label in ((WEB_A, "A"), (WEB_B, "B")):
    st = configure(base)
    print(f"dongle {label}: fixed 32 bytes, no auto-ack, channel 90", flush=True)
print("", flush=True)

pid = 0x10


def next_pid():
    global pid
    pid = (pid + 1) & 0xFF
    if pid == 0:
        pid = 1
    return pid


# --- G1: the lab link itself --------------------------------------------------
hub_clear()
p = next_pid()
tx(WEB_A, click(SENDER_A, p), repeat=6, gap=15)
settle()
rx = hub_grep(r"RX p2 len=32")
verdict("G1 link dongle A -> hub, fixed pipe (6 copies)",
        len(rx) >= 1, f"{len(rx)}/6 copies reached the hub")

hub_clear()
p = next_pid()
tx(WEB_B, click(SENDER_B, p), repeat=6, gap=15)
settle()
rx = hub_grep(r"RX p2 len=32")
verdict("G1 link dongle B -> hub, fixed pipe (6 copies)",
        len(rx) >= 1, f"{len(rx)}/6 copies reached the hub")

# --- G2: sender attribution ---------------------------------------------------
hub_clear()
tx(WEB_A, click(SENDER_A, next_pid()))
settle(2.0)
tx(WEB_B, click(SENDER_B, next_pid()))
settle(2.0)
a = hub_grep(r"DEV=A button 1: press")
b = hub_grep(r"DEV=B button 1: press")
verdict("G2 two senders, each attributed to its own device",
        len(a) == 1 and len(b) == 1, f"A fired {len(a)}x, B fired {len(b)}x")

# --- G3: interleaved, no cross-talk ------------------------------------------
hub_clear()
for _ in range(3):
    tx(WEB_A, click(SENDER_A, next_pid()))
    time.sleep(0.4)
    tx(WEB_B, click(SENDER_B, next_pid()))
    time.sleep(0.4)
settle(2.0)
a = hub_grep(r"DEV=A button 1: press")
b = hub_grep(r"DEV=B button 1: press")
verdict("G3 interleaved senders keep their own dedup state",
        len(a) == 3 and len(b) == 3, f"A {len(a)}/3, B {len(b)}/3")

# --- G4: repeats of one event fire once --------------------------------------
hub_clear()
tx(WEB_A, click(SENDER_A, next_pid()), repeat=3, gap=8)
settle(2.0)
a = hub_grep(r"DEV=A button 1: press")
rx = hub_grep(r"RX p2 len=32")
verdict("G4 three copies of one event fire exactly one button event",
        len(a) == 1, f"{len(a)} events from {len(rx)} frames")

# --- G5: same packet id again is a repeat, a fresh one is an event ------------
hub_clear()
same = 0x42
tx(WEB_A, click(SENDER_A, same))
settle(1.0)
tx(WEB_A, click(SENDER_A, same))
settle(1.0)
a1 = len(hub_grep(r"DEV=A button 1: press"))
tx(WEB_A, click(SENDER_A, next_pid()))
settle(1.5)
a2 = len(hub_grep(r"DEV=A button 1: press"))
verdict("G5 repeated packet id suppressed, fresh packet id accepted",
        a1 == 1 and a2 == 2, f"after the repeat {a1} event(s), after a fresh id {a2}")

# --- G6: dimmer events, both instances and both directions -------------------
hub_clear()
tx(WEB_A, dimmer(SENDER_A, next_pid(), index=1, right=True, steps=5))
settle(1.0)
tx(WEB_A, dimmer(SENDER_A, next_pid(), index=1, right=False, steps=3))
settle(1.0)
tx(WEB_A, dimmer(SENDER_A, next_pid(), index=2, right=True, steps=2))
settle(1.5)
d1r = hub_grep(r"DEV=A dimmer 1: 5 steps")
d1l = hub_grep(r"DEV=A dimmer 1: -3 steps")
d2 = hub_grep(r"DEV=A dimmer 2: 2 steps")
verdict("G6 dimmer: instance index and sign of the step count",
        len(d1r) == 1 and len(d1l) == 1 and len(d2) == 1,
        f"rotate right {len(d1r)}, rotate left {len(d1l)}, held-rotate {len(d2)}")

# --- G7: sensor values land on the right device ------------------------------
hub_clear()
tx(WEB_A, f"{SENDER_A}{HDR}00{next_pid():02X}01500C2C01")  # battery 80%, 0.300 V
settle(1.5)
bat = hub_grep(r"'A Battery'.*80")
volt = hub_grep(r"'A Voltage'.*0\.3")
verdict("G7 battery and voltage published on the sending device",
        bool(bat) and bool(volt),
        f"battery {'ok' if bat else 'missing'}, voltage {'ok' if volt else 'missing'}")

# --- G8: unregistered sender is rejected -------------------------------------
hub_clear()
tx(WEB_A, click(SENDER_UNKNOWN, next_pid()))
settle(1.5)
rejected = hub_grep(r"unregistered sender DE:AD:BE:EF")
events = hub_grep(r"DEV=[AB] button")
verdict("G8 unregistered sender rejected, no event anywhere",
        bool(rejected) and not events,
        f"rejection logged: {bool(rejected)}, stray events: {len(events)}")

# --- G9: wrong service uuid is refused ---------------------------------------
hub_clear()
tx(WEB_A, click(SENDER_A, next_pid(), header="AAAA44"))
settle(1.5)
bad = hub_grep(r"AA:01:00:01: invalid BTHome service data")
events = hub_grep(r"DEV=A button")
verdict("G9 wrong service uuid refused as invalid service data",
        bool(bad) and not events,
        f"warning logged: {bool(bad)}, events fired: {len(events)}")

# --- G10: truncated object ---------------------------------------------------
# A payload whose objects do not add up may have been read with the wrong length,
# which turns the rest of it into something plausible rather than into an obvious
# error. Nothing from it is published and none of its events fire - not even the
# objects that parsed before the fault.
hub_clear()
# 0x0C (voltage) announces two bytes and only one follows before the padding.
truncated_pid = next_pid()
tx(WEB_A, f"{SENDER_A}{HDR}00{truncated_pid:02X}3A010C2C")
settle(1.5)
trunc = hub_grep(r"malformed BTHome payload, discarded \(truncated\)")
press = hub_grep(r"DEV=A button 1: press")
verdict("G10 truncated payload discarded whole, no event from it",
        bool(trunc) and not press,
        f"truncated warning: {bool(trunc)}, events fired: {len(press)}")

# A corrupted copy must not dedup away the intact repeats behind it: the same
# packet id, this time in a sound frame, still has to be accepted.
hub_clear()
tx(WEB_A, click(SENDER_A, truncated_pid))
settle(1.5)
press = hub_grep(r"DEV=A button 1: press")
verdict("G10b a discarded payload does not burn its packet id",
        len(press) == 1,
        f"intact frame with the same packet id fired {len(press)}x (expected 1)")

# --- G11: unknown object id --------------------------------------------------
hub_clear()
tx(WEB_A, f"{SENDER_A}{HDR}00{next_pid():02X}3A01AB01")
settle(1.5)
unknown = hub_grep(r"malformed BTHome payload, discarded \(unknown object id\)")
press = hub_grep(r"DEV=A button 1: press")
verdict("G11 unknown object id discarded whole, no crash",
        bool(unknown) and not press,
        f"unknown-id warning: {bool(unknown)}, events fired: {len(press)}")

# --- G11b: the packet id may sit behind the event it belongs to ---------------
# BTHome fixes no object order. Checking the id where it appears in the stream
# meant a repeat fired its button before the id could suppress it.
hub_clear()
swapped = f"{SENDER_A}{HDR}3A0100{next_pid():02X}"
tx(WEB_A, swapped)
settle(1.2)
tx(WEB_A, swapped)
settle(1.5)
press = hub_grep(r"DEV=A button 1: press")
verdict("G11b dedup does not depend on where the packet id sits",
        len(press) == 1,
        f"same frame sent twice fired {len(press)}x (expected 1)")

# --- G12: zero padding instead of 0xFF ---------------------------------------
# Why the padding byte is 0xFF: 0x00 is the object id "packet id", so zeros are
# data, not padding, and a zero-padded frame ends in a run of packet-id objects.
# How that plays out depends on whether the run is even or odd, so both are
# measured. Neither is harmless, which is the point.
def zeros(body):
    return body + "00" * (32 - len(body) // 2)


# Odd run: the last zero announces an object whose value byte is missing, so the
# payload is truncated and discarded whole.
hub_clear()
tx(WEB_A, zeros(f"{SENDER_A}{HDR}00{next_pid():02X}3A01"), pad=False)
settle(1.5)
odd_press = len(hub_grep(r"DEV=A button 1: press"))
odd_trunc = bool(hub_grep(r"malformed BTHome payload, discarded"))
verdict("G12 zero padding, odd run: discarded as truncated, event lost",
        odd_press == 0 and odd_trunc,
        f"fired {odd_press}x, discarded: {odd_trunc} - a real press would vanish")

# Even run: the zeros parse as packet-id objects, so the payload is accepted and
# the dedup state is left at 0. A later frame that legitimately carries packet
# id 0 is then swallowed.
hub_clear()
tx(WEB_A, zeros(f"{SENDER_A}{HDR}00{next_pid():02X}0C3C0D3A01"), pad=False)
settle(1.5)
even_press = len(hub_grep(r"DEV=A button 1: press"))
tx(WEB_A, click(SENDER_A, 0x00))
settle(1.5)
after = len(hub_grep(r"DEV=A button 1: press"))
verdict("G12b zero padding, even run: accepted but leaves the dedup state at 0",
        even_press == 1 and after == even_press,
        f"fired {even_press}x, a following packet id 0 was "
        f"{'swallowed' if after == even_press else 'accepted'}")

# Restore a sane dedup state for the tests that follow.
tx(WEB_A, click(SENDER_A, next_pid()))
settle(1.0)

# --- G13: the dynamic pipe still works (migration path) ----------------------
hub_clear()
configure(WEB_B, dpl=1, ack=1, pipe1=ADDR_DYN)
tx(WEB_B, click(SENDER_B, next_pid()), address=ADDR_DYN, ack=True, pad=False)
settle(2.0)
rx1 = hub_grep(r"RX p1 len=16")
b = hub_grep(r"DEV=B button 1: press")
verdict("G13 dynamic pipe with auto-ack accepts a 16-byte frame",
        bool(rx1) and len(b) == 1,
        f"frames on pipe 1: {len(rx1)}, events: {len(b)}")
configure(WEB_B)  # back to the fixed-length sender

# --- G14: packet-id aging at timeout ----------------------------------------
hub_clear()
stale = 0x77
tx(WEB_A, click(SENDER_A, stale))
settle(1.5)
first = len(hub_grep(r"DEV=A button 1: press"))
print("      waiting out the 15 s timeout ...", flush=True)
time.sleep(18)
offline = hub_grep(r"AA:01:00:01: offline")
tx(WEB_A, click(SENDER_A, stale))
settle(2.0)
second = len(hub_grep(r"DEV=A button 1: press"))
verdict("G14 after the timeout the same packet id counts as new again",
        first == 1 and bool(offline) and second == 2,
        f"before {first}, offline logged: {bool(offline)}, after {second}")

# --- G15: a clean burst stays clean ------------------------------------------
# Asserts rather than reports: after the tests that deliberately provoke
# warnings, sound traffic must produce none at all. An earlier version of this
# check only printed the counts and passed unconditionally, which made it
# incapable of failing and therefore worthless.
hub_clear()
for _ in range(5):
    tx(WEB_A, click(SENDER_A, next_pid()), repeat=3, gap=8)
    time.sleep(0.5)
    tx(WEB_B, click(SENDER_B, next_pid()), repeat=3, gap=8)
    time.sleep(0.5)
settle(2.0)
noise = (hub_grep(r"malformed BTHome") + hub_grep(r"bad length") +
         hub_grep(r"unregistered sender") + hub_grep(r"RX FIFO full"))
a = hub_grep(r"DEV=A button 1: press")
b = hub_grep(r"DEV=B button 1: press")
verdict("G15 30 sound frames, 10 events, no warnings of any kind",
        len(a) == 5 and len(b) == 5 and not noise,
        f"A {len(a)}/5, B {len(b)}/5, warnings: {len(noise)}"
        + (f" -> {noise[0][:90]}" if noise else ""))

# ---- teardown ---------------------------------------------------------------
if hub_proc:
    hub_proc.terminate()

print("\n--- summary ---")
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(0 if not failed else 1)
