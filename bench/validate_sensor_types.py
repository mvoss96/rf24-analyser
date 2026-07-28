"""Every mapped BTHome measurement type, over the air, end to end.

The host test (tests/test_sensor_types.py in the component repo) proves that the
pinned bthome-cpp decodes each vector to the expected physical value. It cannot
prove that the value then reaches the configured entity, nor that the entity
carries the unit and the number of decimals the table promises - that only shows
in the receiver's own log.

So this transmits the same vectors from a sniffer dongle as sender AA:01:00:04
and holds the receiver to two lines per type: the decode line the BTHome layer
writes at VERBOSE, and the sensor's publish line, which prints the value with
the entity's accuracy and its unit.

Wants tests/wt32-eth01.yaml flashed on the hub: it registers AA:01:00:04 with
one entity per type, named after the YAML key.

    python bench/validate_sensor_types.py
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labkit as lab  # noqa: E402

COMPONENT_TESTS = Path(r"C:\Repos\libs\esphome-rf24-remote\tests")
sys.path.insert(0, str(COMPONENT_TESTS))
from sensor_type_vectors import (  # noqa: E402
    BINARY_VECTORS,
    TEXT_VECTORS,
    all_vectors,
    encoded,
)

HUB_YAML = COMPONENT_TESTS / "wt32-eth01.yaml"
HUB_IP = "192.168.2.70"

PROBE = "AA010004"          # the device carrying an entity for every type
PROBE_TEXT = "AA:01:00:04"
DONGLE_A = "AA010001"       # registered, but only battery and voltage on it

_pid = 0x20


def next_pid():
    """A fresh packet id per frame: the receiver drops repeats of one id, which
    is the point of the id and would otherwise look like a lost measurement."""
    global _pid
    _pid = (_pid + 1) & 0xFF
    if _pid == 0:
        _pid = 1
    return _pid


def send(objects, sender=PROBE):
    """One stimulus, sent the way a real sender sends: three copies of the same
    frame under one packet id, which the receiver dedups into a single event.

    Sent once, a vector fails whenever the channel drops that one frame - which
    showed up here as failures that moved from run to run and named a different
    innocent type each time. Repeating costs nothing at the receiver and takes
    the measurement out of the noise.
    """
    lab.hub_clear()
    lab.tx(lab.WEB_A, lab.payload(sender, next_pid(), objects), repeat=3, gap=8)
    lab.settle(1.2)


def decoded(object_id, instance=1):
    """The value the BTHome layer read out of the frame, before any mapping."""
    hits = lab.hub_grep(rf"{PROBE_TEXT}: sensor 0x{object_id:02X}#{instance}: (-?[\d.]+)")
    if not hits:
        return None
    return float(hits[-1].rsplit(": ", 1)[1])


def published(key):
    """What the entity sent on: the value as the entity formats it, and its unit.
    Returns (text, unit) so the number of decimals stays visible - it is part of
    what the table promises."""
    rx = re.compile(rf"'P {re.escape(key)}' >> (-?[\d.]+) *(\S*)")
    for line in reversed(lab.hub_dump()):
        m = rx.search(line)
        if m:
            return m.group(1), m.group(2)
    return None, None


def decoded_binary(object_id, instance=1):
    hits = lab.hub_grep(rf"{PROBE_TEXT}: binary 0x{object_id:02X}#{instance}: (on|off)")
    return hits[-1].rsplit(": ", 1)[1] if hits else None


def published_binary(key):
    rx = re.compile(rf"'B {re.escape(key)}' >> (ON|OFF)")
    for line in reversed(lab.hub_dump()):
        m = rx.search(line)
        if m:
            return m.group(1)
    return None


def published_text(name):
    rx = re.compile(rf"'{re.escape(name)}' >> '(.*)'")
    for line in reversed(lab.hub_dump()):
        m = rx.search(line)
        if m:
            return m.group(1)
    return None


def decimals(text):
    return len(text.split(".")[1]) if "." in text else 0


def close(a, b):
    return abs(a - b) <= max(1e-3, abs(b) * 1e-5)


# ---- run ---------------------------------------------------------------------
print(f"attaching to hub log at {HUB_IP} ...", flush=True)
if not lab.hub_start(HUB_YAML, HUB_IP):
    print("FATAL: no handshake with the hub - is it reachable?")
    sys.exit(2)
lab.configure(lab.WEB_A)
print("attached; dongle A on channel 90, fixed 32 bytes, no auto-ack\n", flush=True)

vectors = all_vectors()

# --- T1..Tn: one frame per vector ---------------------------------------------
# Three things have to hold at once, and each fails differently: the decoder
# reads the wrong number (wrong width or sign in the library), the value lands
# nowhere (wrong object id in the table), or it lands with the wrong unit or
# precision (wrong metadata in the table). The verdict names which.
for key, oid, raw, value_bytes, value, unit, dec in vectors:
    send(encoded(oid, value_bytes))
    got = decoded(oid)
    text, got_unit = published(key)
    want_unit = unit or ""

    faults = []
    if got is None:
        faults.append("not decoded")
    elif not close(got, value):
        faults.append(f"decoded {got}, expected {value}")
    if text is None:
        faults.append("never published")
    else:
        if not close(float(text), value):
            faults.append(f"published {text}, expected {value}")
        if decimals(text) != dec:
            faults.append(f"{decimals(text)} decimals, expected {dec}")
        if got_unit != want_unit:
            faults.append(f"unit {got_unit!r}, expected {want_unit!r}")

    lab.verdict(f"T {key} (0x{oid:02X}, raw {raw})",
                not faults,
                "; ".join(faults) or f"decoded {got}, published {text} {want_unit}".rstrip())

# --- B1..Bn: one frame per binary object --------------------------------------
# Nothing to scale here, so what a vector proves is that the id reaches the
# entity the table names and that both states arrive - a sensor stuck at its
# initial state passes a one-sided check without ever having worked.
#
# Each vector is preceded by the opposite state, for the same reason the text
# vectors are primed: a binary sensor publishes only on a change, so a second
# run of this bench would find the entity already holding the value and read the
# silence as a fault. Measured, not assumed - the binary block passed once and
# failed on the next run before this was added.
for key, oid, value_bytes, state in BINARY_VECTORS:
    send(encoded(oid, "00" if state else "01"))
    send(encoded(oid, value_bytes))
    want = "on" if state else "off"
    got = decoded_binary(oid)
    shown = published_binary(key)
    faults = []
    if got != want:
        faults.append(f"decoded {got or 'nothing'}, expected {want}")
    if shown != want.upper():
        faults.append(f"published {shown or 'nothing'}, expected {want.upper()}")
    lab.verdict(f"B {key} (0x{oid:02X}, {want})",
                not faults, "; ".join(faults) or f"decoded {got}, published {shown}")

# --- X1..Xn: text and raw -----------------------------------------------------
# A text sensor publishes only on change, so each vector is preceded by a
# priming value: without it a second run of this bench would find the entity
# already holding the value and read the silence as a fault.
PRIME = {0x53: "0178", 0x54: "0100"}  # "x", and a single zero byte

for key, oid, value_bytes, shown in TEXT_VECTORS:
    send(encoded(oid, PRIME[oid]))
    send(encoded(oid, value_bytes))
    kind = "text" if oid == 0x53 else "raw"
    seen = lab.hub_grep(rf"{PROBE_TEXT}: {kind} 0x{oid:02X}#1: \d+ bytes")
    got = published_text(f"P {key}")
    faults = []
    if not seen:
        faults.append("not decoded")
    if got != shown:
        faults.append(f"published {got!r}, expected {shown!r}")
    lab.verdict(f"X {key} (0x{oid:02X}, {shown})",
                not faults, "; ".join(faults) or f"published {got!r}")

# --- X+1: the same text again does not republish ------------------------------
# The property the priming above relies on, asserted rather than assumed.
NAMED = encoded(0x53, "0A6C61622D73656E736F72")  # "lab-sensor"
send(encoded(0x53, "0178"))  # leave the entity holding something else
lab.hub_clear()
lab.tx(lab.WEB_A, lab.payload(PROBE, next_pid(), NAMED))
lab.settle(1.2)
before = len(lab.hub_grep(r"'P text' >> "))
lab.tx(lab.WEB_A, lab.payload(PROBE, next_pid(), NAMED))
lab.settle(1.2)
after = len(lab.hub_grep(r"'P text' >> "))
lab.verdict("X a changed text publishes once, the same string again does not",
            before == 1 and after == 1,
            f"{before} publish(es) for the new string, {after - before} for the repeat")

# --- X+1b: the device name and the firmware version ---------------------------
# Both are built by hand from the frame - the name out of the first text object,
# the version out of two different widths - and both only ever reached Home
# Assistant, never a test.
send(encoded(0x53, "096C61622D70726F6265"))  # "lab-probe", nine characters
name = published_text("P device_name")
lab.verdict("X the first text object also names the device",
            name == "lab-probe", f"device_name {name!r}")

for label, objects, want in (("0xF2, three parts", encoded(0xF2, "030201"), "1.2.3"),
                             ("0xF1, four parts", encoded(0xF1, "04030201"), "1.2.3.4")):
    send(objects)
    got = published_text("P firmware_version")
    lab.verdict(f"X firmware version {label}", got == want, f"published {got!r}, expected {want!r}")

# --- X+2: a second text object is instance 2 ----------------------------------
send(encoded(0x53, "0161") + encoded(0x53, "0162"))  # prime both: "a", "b"
send(encoded(0x53, "0178") + encoded(0x53, "0179"))
first = published_text("P text")
second = published_text("P text 2")
lab.verdict("X index: the second text object feeds the index-2 sensor",
            first == "x" and second == "y",
            f"index 1 got {first!r}, index 2 got {second!r} (expected 'x' and 'y')")

# --- T+1: several types in one frame ------------------------------------------
# A real node sends more than one measurement per broadcast, and the objects are
# read in one pass over a shared buffer - a per-object bug that a single-object
# frame hides shows up here.
combo = (encoded(0x02, "2909") + encoded(0x03, "0113") + encoded(0x0C, "800D")
         + encoded(0x01, "55") + encoded(0x14, "B315"))
send(combo)
want = {"temperature": 23.45, "humidity": 48.65, "voltage": 3.456,
        "battery": 85.0, "moisture": 55.55}
missed = [k for k, v in want.items()
          if published(k)[0] is None or not close(float(published(k)[0]), v)]
lab.verdict("T combined frame: five types in one payload all publish",
            not missed, f"missing or wrong: {missed}" if missed else "all five landed")

# --- T+2: instances -----------------------------------------------------------
send(encoded(0x02, "2909") + encoded(0x02, "2EFB"))
first, _ = published("temperature")
second, _ = published("temperature 2")
ok = (first is not None and second is not None
      and close(float(first), 23.45) and close(float(second), -12.34))
lab.verdict("T index: the second object of an id feeds the index-2 sensor",
            ok, f"index 1 got {first}, index 2 got {second} (expected 23.45 and -12.34)")

# --- T+2b: a measurement and a binary object in one payload -------------------
# They share the instance counter, so a bug there would show as one of the two
# landing on the wrong instance and never publishing.
send(encoded(0x02, "2909") + encoded(0x21, "01") + encoded(0x03, "0113"))
temp, _ = published("temperature")
motion = published_binary("motion")
hum, _ = published("humidity")
lab.verdict("T measurements and a binary object in one payload",
            temp is not None and motion == "ON" and hum is not None,
            f"temperature {temp}, motion {motion}, humidity {hum}")

# --- T+2c: a frame filled to capacity ----------------------------------------
# Eleven single-byte objects: a 32-byte slot leaves 23 bytes after the sender
# id, the BTHome header and the packet id, and the smallest object is two bytes.
# (A payload without a packet id fits twelve, which is what the receiver's
# instance array is sized for - the host test covers that case.) The last id is
# sent twice: an array sized for fewer ids never records the ones past its end
# and answers instance 1 for both, which distinct ids alone would not reveal,
# because 1 is the right answer for those either way.
FULL_IDS = [0x01, 0x09, 0x0F, 0x21, 0x2E, 0x2F, 0x46, 0x57, 0x58, 0x64, 0x64]
send("".join(encoded(oid, "01") for oid in FULL_IDS))
seen = [oid for oid in set(FULL_IDS)
        if lab.hub_grep(rf"{PROBE_TEXT}: (sensor|binary) 0x{oid:02X}#1: ")]
second = lab.hub_grep(rf"{PROBE_TEXT}: sensor 0x64#2: ")
noise = lab.hub_grep(r"malformed BTHome payload")
lab.verdict("T a frame filled to capacity counts every object",
            len(seen) == len(set(FULL_IDS)) and bool(second) and not noise,
            f"{len(seen)}/{len(set(FULL_IDS))} ids at instance 1, "
            f"the repeated id reached instance 2: {bool(second)}, warnings: {len(noise)}")

# --- T+3: an object nobody mapped ---------------------------------------------
# 0xF0 is a device type id: the library knows it, no platform maps it to an
# entity. It has to be read and passed over, not treated as a fault - an
# unmapped object is a sender doing something this receiver was not configured
# for, and the rest of the frame is still good.
send(encoded(0xF0, "3412") + encoded(0x02, "2909"))
noise = lab.hub_grep(r"malformed BTHome payload")
temp, _ = published("temperature")
lab.verdict("T an unmapped but known object is skipped, not faulted",
            not noise and temp is not None and close(float(temp), 23.45),
            f"warnings: {len(noise)}, the mapped object beside it published {temp}")

# --- T+4: a type not configured on the sending device -------------------------
# Dongle A is registered but carries only battery and voltage. A temperature
# from it must be decoded and dropped, and above all must not appear on the
# probe's temperature entity: sensors belong to a device, not to the hub.
lab.hub_clear()
lab.tx(lab.WEB_A, lab.payload(DONGLE_A, next_pid(), encoded(0x02, "2EFB")))
lab.settle(1.2)
seen = lab.hub_grep(r"AA:01:00:01: sensor 0x02#1: -12\.34")
leaked, _ = published("temperature")
lab.verdict("T a type without an entity on that device is dropped, not rerouted",
            bool(seen) and leaked is None,
            f"decoded on the sender: {bool(seen)}, "
            f"leaked onto the probe entity: {leaked is not None}")

# --- T+5: a full frame of measurements, repeated ------------------------------
# The repeats a NO_ACK sender puts out must not republish: a chart of the value
# should show one point per broadcast, not three.
lab.hub_clear()
lab.tx(lab.WEB_A, lab.payload(PROBE, next_pid(), encoded(0x02, "2909")), repeat=3, gap=8)
lab.settle(2.0)
publishes = lab.hub_grep(r"'P temperature' >> ")
lab.verdict("T three copies of one frame publish the value once",
            len(publishes) == 1, f"{len(publishes)} publish(es) from 3 copies")

time.sleep(0.2)
lab.exit_with_summary()
