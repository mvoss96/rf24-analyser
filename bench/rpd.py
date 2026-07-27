"""Carrier detect per channel, at the data rate that actually matters.

The firmware's `scan` sweeps at 2 Mbps, because the RPD only reports carriers
inside the receiver bandwidth and that bandwidth follows the data rate - a 2 Mbps
sweep sees a wide band, a 250 kbps sweep sees almost nothing of it. That makes the
sweep useless for the opposite question: is there a carrier sitting exactly on
*this* channel, in the bandwidth a 250 kbps receiver suffers from?

RPD latches while in RX and only clears on leaving it, so each sample means stop,
listen, wait, read - which is why this is a script and not a sweep.
"""
import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM18"
CHANNELS = [int(c) for c in (sys.argv[2].split(",") if len(sys.argv) > 2
                             else ["96", "98", "99", "100", "101", "102", "104", "110"])]
SAMPLES = int(sys.argv[3]) if len(sys.argv) > 3 else 12
DWELL = 0.05  # seconds in RX before asking

s = serial.Serial(PORT, 500000, timeout=0.3)
time.sleep(2.0)  # the open pulled DTR: let it boot


def cmd(line, want, timeout=2.0):
    s.reset_input_buffer()
    s.write((line + "\n").encode())
    s.flush()
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        buf += s.read(128)
        for text in buf.decode("ascii", errors="replace").splitlines():
            if want in text:
                return text.strip()
    return ""


print(f"{'ch':>4}  {'carrier':>8}   MHz")
for ch in CHANNELS:
    hits = 0
    for _ in range(SAMPLES):
        cmd("stop", "OK")
        cmd(f"listen ch={ch} rate=250 crc=16 aw=5 pa=low ack=0 dpl=1 "
            "pipe1=42:54:48:4D:45", "OK listening")
        time.sleep(DWELL)
        reply = cmd("reg 09", "OK reg 9=")
        if reply.endswith("=1"):
            hits += 1
    print(f"{ch:>4}  {hits:>3}/{SAMPLES}    {2400 + ch}", flush=True)
s.close()
