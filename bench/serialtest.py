#!/usr/bin/env python3
"""Measure what pyserial can do with the wire, against a dongle that does nothing else.

Every throughput figure in this project is bounded by the serial line, and the
analyser firmware cannot tell us how much of that bound is the line and how much
is the analyser: from the host's side both look the same. This runs the same
four shapes of traffic against `bench/serialbench` - a firmware with no radio in
it - so the two can be subtracted.

    python bench/serialtest.py --port COM18

The interesting one is `lockstep`, which reproduces what `txseq ... bin conf=`
does: fixed-size records, a window of unconfirmed ones, an acknowledgement every
few. If the bare wire needs the same milliseconds per record that the analyser
needs, there is nothing left to win in the analyser.

Nothing here imports nrf24_dongle on purpose. The point is to measure pyserial,
not our wrapper around it - but it does borrow the one thing that wrapper learned
the hard way: `read(n)` waits for n bytes or the timeout, so every read here asks
for one byte and then takes whatever else is already waiting. Getting that wrong
makes the host look slow and the link look lossy, which is what it did on the
first run of this file.
"""

from __future__ import annotations

import argparse
import statistics
import time

import serial

# What the analyser actually sends: one length byte, 32 payload bytes, one crc.
RECORD = 34
# And how it paces them - SEND_WINDOW_BIN and SEND_CONFIRM_BIN in nrf24web.py.
WINDOW = 7
CONFIRM = 4

BITS_PER_BYTE = 10   # 8N1: a start bit and a stop bit ride along with every byte


def wire_ms(nbytes: int, baud: int) -> float:
    return nbytes * BITS_PER_BYTE * 1000.0 / baud


# --------------------------------------------------------------------- the link


class Link:
    """A serial port plus the buffer that makes reading it not-slow."""

    def __init__(self, port: str, baud: int, timeout: float = 0.05):
        self.ser = serial.Serial(port, baud, timeout=timeout)
        self.buf = bytearray()

    def close(self) -> None:
        self.ser.close()

    def pump(self) -> int:
        """One blocking byte, then everything already buffered behind it."""
        chunk = self.ser.read(1)
        if chunk:
            waiting = self.ser.in_waiting
            if waiting:
                chunk += self.ser.read(waiting)
            self.buf += chunk
        return len(chunk)

    def clear(self) -> None:
        self.buf.clear()
        self.ser.reset_input_buffer()

    def line(self, timeout: float = 2.0) -> str:
        deadline = time.perf_counter() + timeout
        while True:
            if b"\n" in self.buf:
                line, _, rest = self.buf.partition(b"\n")
                self.buf = bytearray(rest)
                return line.decode("ascii", "replace").strip()
            if time.perf_counter() >= deadline:
                return ""
            self.pump()

    def exact(self, n: int, timeout: float = 10.0) -> bytes:
        """n raw bytes, or fewer if the deadline passes."""
        deadline = time.perf_counter() + timeout
        while len(self.buf) < n and time.perf_counter() < deadline:
            self.pump()
        out = bytes(self.buf[:n])
        self.buf = bytearray(self.buf[n:])
        return out

    def command(self, cmd: str, timeout: float = 2.0) -> str:
        self.clear()
        self.ser.write((cmd + "\n").encode())
        return self.line(timeout)


def integrity(got: bytes) -> dict:
    """Say *how* a stream is wrong, not just how much.

    The device sends an incrementing counter. A run of wrong bytes can mean two
    very different things: bytes were lost, after which every later byte is
    compared against the wrong position and looks wrong, or bytes arrived
    damaged. Reporting only a count makes a single lost byte look like the wire
    is on fire, so this separates the two.
    """
    first = next((i for i, b in enumerate(got) if b != (i & 0xFF)), -1)
    if first < 0:
        return {"bad": 0, "first": -1, "shift": 0, "damaged": 0}

    bad = sum(1 for i, b in enumerate(got) if b != (i & 0xFF))
    # If everything from the first mismatch on is consistently offset, the
    # stream lost (or gained) that many bytes at that point and is otherwise
    # intact - one event, not thousands.
    shift = (got[first] - first) & 0xFF
    damaged = sum(1 for i, b in enumerate(got[first:], start=first)
                  if b != ((i + shift) & 0xFF))
    return {"bad": bad, "first": first, "shift": shift, "damaged": damaged}


def parse_fields(line: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in line.split():
        if "=" in part:
            k, _, v = part.partition("=")
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------ the tests


def test_source(link: Link, baud: int, nbytes: int) -> dict:
    """Dongle to host: how fast can pyserial read?"""
    ack = link.command(f"s {nbytes}")
    if not ack.startswith("OK src"):
        raise RuntimeError(f"source not accepted: {ack!r}")

    t0 = time.perf_counter()
    got = link.exact(nbytes, timeout=max(5.0, wire_ms(nbytes, baud) / 250))
    t1 = time.perf_counter()

    fields = parse_fields(link.line(2.0))

    host_ms = (t1 - t0) * 1000.0
    return {
        "bytes": len(got),
        "host_ms": host_ms,
        "dev_ms": fields.get("us", 0) / 1000.0,
        "kBs": len(got) / host_ms if host_ms else 0.0,
        "wire_ms": wire_ms(nbytes, baud),
        "short": nbytes - len(got),
        **integrity(got),
    }


def test_sink(link: Link, baud: int, nbytes: int, chunk: int) -> dict:
    """Host to dongle: how fast can pyserial write, and can the AVR keep up?"""
    ack = link.command("r")
    if not ack.startswith("OK sink"):
        raise RuntimeError(f"sink not accepted: {ack!r}")

    data = bytes((i & 0xFF) for i in range(nbytes))
    t0 = time.perf_counter()
    for off in range(0, nbytes, chunk):
        link.ser.write(data[off : off + chunk])
    link.ser.flush()
    t1 = time.perf_counter()

    fields = parse_fields(link.line(3.0))

    host_ms = (t1 - t0) * 1000.0
    return {
        "bytes": nbytes,
        "chunk": chunk,
        "host_ms": host_ms,
        "dev_ms": fields.get("us", 0) / 1000.0,
        "kBs": nbytes / host_ms if host_ms else 0.0,
        "wire_ms": wire_ms(nbytes, baud),
        "bad": fields.get("bad", -1),
        "short": nbytes - fields.get("n", 0),
    }


def test_echo(link: Link, rounds: int) -> dict:
    """The shortest round trip this link has: one byte out, one byte back."""
    ack = link.command("e")
    if not ack.startswith("OK echo"):
        raise RuntimeError(f"echo not accepted: {ack!r}")

    old = link.ser.timeout
    link.ser.timeout = 1.0
    times = []
    for i in range(rounds):
        t0 = time.perf_counter()
        link.ser.write(bytes([i & 0xFF]))
        back = link.ser.read(1)
        t1 = time.perf_counter()
        if back:
            times.append((t1 - t0) * 1000.0)
    link.ser.timeout = old

    time.sleep(0.4)   # let it fall out of echo mode before the next test speaks
    link.clear()

    if not times:
        return {"rounds": 0}
    return {
        "rounds": len(times),
        "min_ms": min(times),
        "med_ms": statistics.median(times),
        "p90_ms": sorted(times)[int(len(times) * 0.9)],
        "max_ms": max(times),
    }


def test_lockstep(link: Link, baud: int, records: int,
                  window: int, confirm: int, record: int = RECORD) -> dict:
    """What txseq does, without the radio: records, a window, an ack every few."""
    ack = link.command(f"w {confirm * record}")
    if not ack.startswith("OK win"):
        raise RuntimeError(f"window not accepted: {ack!r}")

    payload = bytes(range(record))
    written = 0
    acked = 0            # records the device has confirmed
    waits = []

    stalled = False
    t0 = time.perf_counter()
    while written < records:
        while written < records and (written - acked) < window:
            link.ser.write(payload)
            written += 1

        if written < records:
            w0 = time.perf_counter()
            deadline = w0 + 3.0
            before = acked
            while (written - acked) >= window and time.perf_counter() < deadline:
                if not link.pump():
                    continue
                while b"\n" in link.buf:
                    line, _, rest = link.buf.partition(b"\n")
                    link.buf = bytearray(rest)
                    text = line.decode("ascii", "replace").strip()
                    if text.startswith("A"):
                        try:
                            acked = int(text[1:]) // record
                        except ValueError:
                            pass
            waits.append((time.perf_counter() - w0) * 1000.0)
            # A full window and no confirmation in three seconds means the link
            # is gone, not slow. Without this the loop writes nothing, waits
            # again, and never ends - which is how the first run at 1 MBaud hung.
            if acked == before:
                stalled = True
                break

    # The last records are on the wire; wait for the device to confirm them all
    # so the run is timed to its end rather than to the host's last write.
    deadline = time.perf_counter() + (0.0 if stalled else 3.0)
    while acked < (records // confirm) * confirm and time.perf_counter() < deadline:
        if not link.pump():
            continue
        while b"\n" in link.buf:
            line, _, rest = link.buf.partition(b"\n")
            link.buf = bytearray(rest)
            text = line.decode("ascii", "replace").strip()
            if text.startswith("A"):
                try:
                    acked = int(text[1:]) // record
                except ValueError:
                    pass
    t1 = time.perf_counter()

    time.sleep(0.4)
    link.clear()

    total_ms = (t1 - t0) * 1000.0
    return {
        "records": records,
        "record": record,
        "window": window,
        "confirm": confirm,
        "acked": acked,
        "written": written,
        "stalled": stalled,
        "total_ms": total_ms,
        "per_record_ms": total_ms / records,
        "wire_ms": wire_ms(record, baud),
        "kBs": (records * record) / total_ms if total_ms else 0.0,
        "med_wait_ms": statistics.median(waits) if waits else 0.0,
        "waits": len(waits),
    }


# ----------------------------------------------------------------------- main


def run_at(port: str, baud: int, args) -> None:
    """Open at 500000 (the boot rate), switch if asked, measure, switch back."""
    link = Link(port, 500000)
    try:
        time.sleep(2.0)   # opening pulls DTR and resets the board
        link.clear()

        hello = link.command("v")
        if not hello.startswith("SB"):
            raise RuntimeError(
                f"not the serial bench firmware (said {hello!r}) - flash it with "
                f"`pio run -e serialbench -t upload`")

        if baud != 500000:
            reply = link.command(f"b {baud}")
            if not reply.startswith("OK baud"):
                raise RuntimeError(f"baud not accepted: {reply!r}")
            link.ser.baudrate = baud
            time.sleep(0.1)
            link.clear()

        if args.buffer:
            try:
                link.ser.set_buffer_size(rx_size=args.buffer, tx_size=args.buffer)
            except Exception as exc:   # not every platform has it
                print(f"  (set_buffer_size unavailable: {exc})")

        pct = lambda kbs: kbs * 1000 * BITS_PER_BYTE / baud * 100
        print(f"\n=== {port} @ {baud} baud   {hello}"
              + (f"   buffer={args.buffer}" if args.buffer else ""))
        print(f"    a byte takes {1_000_000 * BITS_PER_BYTE / baud:.1f} us, "
              f"so the line is worth {baud / BITS_PER_BYTE / 1000:.1f} kB/s")

        for attempt in range(args.repeat):
            r = test_source(link, baud, args.bytes)
            print(f"\n  read   {r['bytes']} B in {r['host_ms']:.1f} ms = {r['kBs']:.1f} kB/s, "
                  f"{pct(r['kBs']):.0f}% of the line "
                  f"(wire {r['wire_ms']:.0f} ms, device says {r['dev_ms']:.0f} ms)")
            if r["bad"] or r["short"]:
                print(f"         !! first wrong byte at {r['first']}, "
                      f"{r['bad']} wrong in total, {r['short']} never arrived; "
                      f"the rest is offset by {r['shift']} with "
                      f"{r['damaged']} genuinely damaged bytes")

        for chunk in args.chunks:
            r = test_sink(link, baud, args.bytes, chunk)
            print(f"  write  {r['bytes']} B in {r['host_ms']:.1f} ms = {r['kBs']:.1f} kB/s, "
                  f"{pct(r['kBs']):.0f}% of the line, in {chunk} B chunks "
                  f"(device says {r['dev_ms']:.0f} ms)")
            if r["bad"] or r["short"]:
                print(f"         !! {r['bad']} wrong bytes, {r['short']} never arrived")

        e = test_echo(link, args.echo)
        if e.get("rounds"):
            print(f"\n  echo   one byte out and back: min {e['min_ms']:.2f} ms, "
                  f"median {e['med_ms']:.2f}, p90 {e['p90_ms']:.2f}, max {e['max_ms']:.2f} "
                  f"over {e['rounds']} rounds")
        else:
            print("\n  echo   no bytes came back")

        for window, confirm in args.lockstep:
            r = test_lockstep(link, baud, args.records, window, confirm)
            if r["stalled"]:
                print(f"  lock   window {window:2d}, confirm every {confirm:2d}: "
                      f"stalled after {r['written']} records, {r['acked']} confirmed")
                continue
            print(f"  lock   window {window:2d}, confirm every {confirm:2d}: "
                  f"{r['per_record_ms']:.2f} ms/record = {r['kBs']:.1f} kB/s, "
                  f"{pct(r['kBs']):.0f}% of the line "
                  f"({r['waits']} waits of {r['med_wait_ms']:.2f} ms; "
                  f"the record itself needs {r['wire_ms']:.2f} ms)")

        if baud != 500000:
            link.command("b 500000")
            link.ser.baudrate = 500000
            time.sleep(0.1)
    finally:
        link.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default="COM18")
    ap.add_argument("--baud", type=int, action="append", help="repeatable; default 500000")
    ap.add_argument("--bytes", type=int, default=32768, help="bytes per throughput run")
    ap.add_argument("--records", type=int, default=512,
                    help="records per lockstep run, matching a txseq run")
    ap.add_argument("--echo", type=int, default=200)
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat the read test, to see whether damage is steady")
    ap.add_argument("--buffer", type=int, default=0,
                    help="ask the driver for this receive/transmit buffer size")
    args = ap.parse_args()

    args.baud = args.baud or [500000]
    args.chunks = [RECORD, RECORD * WINDOW, 4096]
    args.lockstep = [(WINDOW, CONFIRM), (WINDOW, 1), (14, CONFIRM), (28, 8), (1, 1)]

    for baud in args.baud:
        # One rate failing is a result, not a reason to stop: whether the wire
        # carries 1 MBaud at all is half of what this program is for.
        try:
            run_at(args.port, baud, args)
        except Exception as exc:
            print(f"\n=== {args.port} @ {baud} baud   failed: {exc}")


if __name__ == "__main__":
    main()
