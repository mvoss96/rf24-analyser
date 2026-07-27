"""Press the RotRemote's button by pulling DTR on its debug port.

Despite the file name this does not reset the remote - RTS does not either.
Pulling DTR is read as a button press, so the device transmits a click frame
with a fresh packet id: a reproducible stimulus from the real hardware, without
anyone having to touch it. The remote's own log of what it sent is printed, so
a receiver's view can be compared against the sender's."""
import sys, time, serial
port = sys.argv[1] if len(sys.argv) > 1 else "COM9"
s = serial.Serial()
s.port = port; s.baudrate = 115200; s.timeout = 0.2
s.dtr = True
s.open()
time.sleep(0.05)
s.dtr = False
out = b""
end = time.time() + 4
while time.time() < end:
    chunk = s.read(256)
    if chunk:
        out += chunk
s.close()
print(out.decode("ascii", errors="replace").strip())
