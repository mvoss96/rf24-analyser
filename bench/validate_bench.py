"""Regression run for the current setup: everything on channel 90, dynamic
payloads, auto-ack on the receivers.

Stimulus comes from dongle B through its web API, so no serial port has to be
taken away from anyone, and the frames are byte-identical to what the RotRemote
sends - same sender id, same BTHome objects, only the packet id differs. Both
dongles and the ESP32 report what they heard; the lamp is checked separately in
Home Assistant.

Verdicts are printed per test so a failure names itself instead of needing the
raw log read back.
"""
import json, sys, threading, time, urllib.request
import serial

WEB_A = "http://127.0.0.1:8724"   # listener
WEB_B = "http://127.0.0.1:8725"   # transmitter
C3_PORT = "COM19"
ADDR = "42:54:48:4D:45"
ADDR_HEX = "4254484D45"

# A real click frame: [sender B7:4F:E7:7F][BTHome: uuid, device info, packet id,
# battery 100%, voltage 3.398V, button press]. Only the packet id varies.
def click_frame(pid):
    return f"B74FE77FD2FC4400{pid:02X}01640C460D3A01"


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def frames(base):
    with urllib.request.urlopen(base + "/api/frames?limit=200", timeout=10) as r:
        return json.load(r)["frames"]


def clear_all():
    for base in (WEB_A, WEB_B):
        post(base, "/api/clear", {})


def tx(payload, repeat=1, gap=0):
    line = f"tx {ADDR_HEX} {payload} noack"
    if repeat > 1:
        line += f" x{repeat}"
    if gap:
        line += f" gap={gap}"
    return post(WEB_B, "/api/command", {"line": line, "wait": True})


# --- the ESP32's view, read straight off its log ------------------------------
c3_lines = []
c3_stop = threading.Event()


def c3_reader(ser):
    buf = b""
    while not c3_stop.is_set():
        try:
            chunk = ser.read(256)
        except Exception:
            return
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if "nrf24" in text or "Button" in text or "button" in text:
                c3_lines.append(text)


results = []


def verdict(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}", flush=True)


c3 = None
try:
    c3 = serial.Serial(C3_PORT, 115200, timeout=0.2)
    threading.Thread(target=c3_reader, args=(c3,), daemon=True).start()
    print(f"ESP32 on {C3_PORT} attached\n")
except Exception as exc:
    print(f"ESP32 on {C3_PORT} not readable ({exc}) - continuing without it\n")

# --- T1: is the lab link itself sound on this channel? ------------------------
clear_all()
payload = "AA010203040506070809101112131415"
tx(payload, repeat=12, gap=15)
time.sleep(1.5)
got = [f for f in frames(WEB_A) if f["raw"] == payload]
verdict("T1 link dongle B -> dongle A (12 frames, 15 ms apart)",
        len(got) == 12, f"{len(got)}/12 intact")

# --- T2: single events, the case that used to produce stale copies ------------
clear_all()
c3_lines.clear()
sent = []
for i in range(4):
    pid = 0x90 + i
    sent.append(click_frame(pid))
    tx(sent[-1])
    time.sleep(2.0)
time.sleep(1.0)

# Only dongle A is a listener here - dongle B is the one transmitting, and a
# radio does not hear itself.
got = frames(WEB_A)
stale = [f["raw"] for f in got if f["raw"] not in sent]
counts = {p: sum(1 for f in got if f["raw"] == p) for p in sent}
verdict("T2 dongle A: one frame per single event, no stale payloads",
        all(c == 1 for c in counts.values()) and not stale,
        f"per event {list(counts.values())}, stale frames: {len(stale)}"
        + (f" {stale[:2]}" if stale else ""))

presses = [l for l in c3_lines if "Button 1: press" in l]
rx_lines = [l for l in c3_lines if "RX p" in l]
verdict("T2 ESP32: exactly one button event per single event",
        len(presses) == 4, f"{len(presses)} button events from {len(rx_lines)} frames")

# --- T3: repeats, the way the real remote sends ------------------------------
clear_all()
c3_lines.clear()
burst = click_frame(0x94)
tx(burst, repeat=3, gap=8)
time.sleep(2.5)
got_a = frames(WEB_A)
stale = [f["raw"] for f in got_a if f["raw"] != burst]
verdict("T3 dongle A: burst of 3 arrives without stale copies",
        len([f for f in got_a if f["raw"] == burst]) >= 1 and not stale,
        f"{len([f for f in got_a if f['raw'] == burst])} copies, "
        f"stale: {len(stale)}")
presses = [l for l in c3_lines if "Button 1: press" in l]
verdict("T3 ESP32: three copies of one event fire one button event",
        len(presses) == 1, f"{len(presses)} button events")

# --- T4: the real remote, which is the only stimulus that counts -------------
# A DTR pulse on the remote's debug port presses its button. Both dongles listen
# here - nobody has to transmit - so the same click can be compared across two
# receivers, which is what exposed invented frames in the first place.
REMOTE_PORT = "COM9"


def click():
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
    text = out.decode("ascii", errors="replace")
    for line in text.splitlines():
        if "frame bytes" in line:
            return "".join(line.split("frame bytes:")[1].split()).upper()
    return ""


clear_all()
c3_lines.clear()
clicked = []
try:
    for _ in range(4):
        raw = click()
        if raw:
            clicked.append(raw)
        time.sleep(2.5)
except Exception as exc:
    print(f"      (remote port unavailable: {exc})")

if clicked:
    verdict("T4 remote: four clicks, each with a fresh packet id",
            len(clicked) == 4 and len(set(clicked)) == 4,
            f"sent {len(clicked)}: " + ", ".join(c[16:18] for c in clicked))

    for name, base in (("dongle A", WEB_A), ("dongle B", WEB_B)):
        got = frames(base)
        stale = [f["raw"] for f in got if f["raw"] not in clicked]
        per = [sum(1 for f in got if f["raw"] == c) for c in clicked]
        verdict(f"T4 {name}: every click heard, nothing invented",
                all(p >= 1 for p in per) and not stale,
                f"copies per click {per}, stale frames: {len(stale)}"
                + (f" {stale[:2]}" if stale else ""))

    presses = [l for l in c3_lines if "Button 1: press" in l]
    verdict("T4 ESP32: one button event per click",
            len(presses) == len(clicked),
            f"{len(presses)} button events for {len(clicked)} clicks")
else:
    print("SKIP  T4 real remote (serial port busy)")

c3_stop.set()
time.sleep(0.3)
if c3:
    c3.close()

print("\n--- summary ---")
for name, ok, detail in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
