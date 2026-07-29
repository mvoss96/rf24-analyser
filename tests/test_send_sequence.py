"""What the host does when a `txseq` run stops before its count.

A run stops early whenever the radio gives up on a frame, and it stops with
payloads still in flight: the host writes a window ahead of the dongle's
confirmations, so up to seven records are already on the wire when the dongle
decides to end the run. The dongle was in command mode by the time they
arrived and answered each of them - `ERR unknown cmd`, or `ERR line too long`
where two records ran together past the line buffer.

Nobody read those as the answer to the payloads that caused them. The host read
the first one as the answer to the header it sent next, refused a transfer that
the dongle would have accepted, and reported HTTP 400 to whoever asked for it.
Six of thirty-two acknowledged 512-frame transfers failed that way in a sweep,
every one of them behind a run whose retransmission count was three times the
usual - because a slow frame is one the host gets further ahead of.

The dongle drops what is behind an abandoned run now, and says when it has
finished. These cases hold both sides to that, and to the older firmware that
does not: a transfer that can be finished has to be finished either way.

    python tests/test_send_sequence.py
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nrf24web  # noqa: E402
import nrf24_dongle as dongle  # noqa: E402

results = []


def verdict(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}", flush=True)


class FakeDongle:
    """The firmware's side of `txseq`, answering as the real one does.

    Replies are published straight from the write that provoked them, which is
    what the real dongle does too on this timescale - the host is blocked
    reading the port a few microseconds later either way. Only the drain is
    timed, because the whole point of it is that it ends on silence.
    """

    DRAIN_S = 0.05      # SEQ_DRAIN_MS in the firmware

    def __init__(self, hub, give_up_at=None, drains=True):
        self.hub = hub
        self.port = "FAKE"
        self.give_up_at = give_up_at    # end the run once this many are taken
        self.drains = drains            # firmware 3.16.0 or older
        self.mode = "cmd"
        self.left = self.taken = self.conf = 0
        self.draining = False
        self._timer = None
        self._lock = threading.RLock()
        # The regression this file exists for: bytes of payload that reached a
        # dongle which was reading commands. It should never be anything but 0.
        self.payload_in_cmd_mode = 0
        self.garbage_errs = 0
        # And the other half of it: payloads written in a shape the run was not
        # opened with, because the host lost track of which shape that was.
        self.binary = False
        self.wrong_shape = 0
        # Answers to an earlier run's leftovers, still coming out of the port
        # as the host writes its first header. This is what the dongle was
        # doing at the moment the sweep's failures happened.
        self.stale = 0

    # -- what the host calls --

    def send(self, line):
        self._feed((line + "\n").encode())

    def send_raw(self, data):
        self._feed(data)

    def set_baud(self, rate):
        pass

    # -- the state machine --

    def _feed(self, data):
        with self._lock:
            while self.stale:
                self.stale -= 1
                self._say("ERR unknown cmd (try help)")
            if self.draining:
                self._restart_drain()
                return
            if self.mode == "seq":
                self._take(data)
                return
            self._command(data)

    def _command(self, data):
        text = data.decode("latin-1").strip()
        if not text.startswith("txseq"):
            # A payload record read as a command: the failure being tested.
            self.payload_in_cmd_mode += len(data)
            self.garbage_errs += 1
            self._say("ERR unknown cmd (try help)")
            return
        parts = text.split()
        self.left = int(parts[2])
        self.taken = 0
        self.conf = 4
        binary = "bin" in parts
        for tok in parts:
            if tok.startswith("conf="):
                self.conf = int(tok[5:])
        if not binary:
            self._say("ERR only bin is modelled here")
            return
        self.mode = "seq"
        self.binary = True
        self._say(f"OK txseq ready count={self.left} bin")

    def _take(self, data):
        # A record is length, payload, checksum, and the host writes exactly
        # one per call - so the framing does not have to be reassembled here,
        # but a hex line arriving instead of a record is worth noticing. So is
        # a second header: both mean the host thinks this run is something
        # other than what it is.
        if self.binary and data.endswith(b"\n"):
            self.wrong_shape += 1
        self.taken += 1
        self.left -= 1
        if self.give_up_at is not None and self.taken >= self.give_up_at:
            self._end("gave up")
            return
        if self.left == 0:
            self._end(None)
            return
        if self.taken % self.conf == 0:
            self._say(f"OK txseq at={self.taken}")

    def _end(self, why):
        sent = self.taken
        self.mode = "cmd"
        self.give_up_at = None      # the next run is not this one
        line = f"OK txseq sent={sent}/{sent + self.left} ack=yes failed=0 retries=0"
        if why:
            line += f" stopped={why}"
        self._say(line)
        if why and self.drains:
            self._restart_drain()

    def _restart_drain(self):
        self.draining = True
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.DRAIN_S, self._drained)
        self._timer.daemon = True
        self._timer.start()

    def _drained(self):
        with self._lock:
            self.draining = False
            self._say("OK txseq idle dropped=0")

    def _say(self, text):
        self.hub.publish({"type": "line", "text": text, "kind": "rx"})


def transfer(frames=64, give_up_at=None, drains=True, ack=True, stale=0):
    """One /api/send worth of work against a scripted dongle."""
    hub = nrf24web.Hub()
    session = nrf24web.Session(hub)
    session.dongle = FakeDongle(hub, give_up_at=give_up_at, drains=drains)
    session.dongle.stale = stale
    payloads = [f"{i:04X}" + "AA" * 14 for i in range(frames)]
    started = time.monotonic()
    try:
        reply = session.send_sequence("11223344", payloads, ack=ack)
        error = None
    except Exception as exc:               # what /api/send turns into a 400
        reply, error = None, f"{type(exc).__name__}: {exc}"
    return session.dongle, reply, error, time.monotonic() - started


# --- a run that never breaks --------------------------------------------------
fake, reply, error, _ = transfer(frames=64)
verdict("P a clean run is one attempt and reports every frame",
        error is None and reply is not None and "sent=64/64" in reply
        and fake.payload_in_cmd_mode == 0,
        f"reply {reply!r}, error {error!r}")

# --- a run that gives up with a window still in flight ------------------------
# The case from the sweep. The host has written up to SEND_WINDOW_BIN records
# past the frame the dongle stopped on; none of them may be read as a command,
# and the transfer has to finish rather than fail.
fake, reply, error, took = transfer(frames=64, give_up_at=20)
verdict("P a run that gives up mid-window is resumed, not failed",
        error is None and reply is not None and "sent=64/64" in reply,
        f"reply {reply!r}, error {error!r}, {took:.2f}s")

verdict("P no payload reaches a dongle that is reading commands",
        fake.payload_in_cmd_mode == 0 and fake.garbage_errs == 0,
        f"{fake.payload_in_cmd_mode} bytes in command mode, "
        f"{fake.garbage_errs} ERR lines provoked")

# --- the same against firmware that does not drain ----------------------------
# 3.15.0 and older answer every leftover record. The host cannot stop them
# arriving, but it can decline to read them as the answer to its own header -
# which is the whole difference between a resumed transfer and a 400.
fake, reply, error, took = transfer(frames=64, give_up_at=20, drains=False)
verdict("P leftover ERR lines from older firmware do not fail the transfer",
        error is None and reply is not None and "sent=64/64" in reply,
        f"reply {reply!r}, error {error!r}, "
        f"{fake.garbage_errs} leftover ERR lines, {took:.2f}s")

# --- one stale ERR, which is the worst number of them -------------------------
# Two stale lines answered both headers and the transfer failed loudly. One
# answered the first and left the dongle's real `OK txseq ready ... bin` to
# answer the second - so the host read a run it had opened in records as one it
# had opened in hex lines, and wrote hex lines into it. That is not a 400. It
# is a transfer that reports success having sent something else entirely.
fake, reply, error, _ = transfer(frames=64, stale=1)
verdict("P a single stale ERR does not change what shape the payloads take",
        error is None and reply is not None and "sent=64/64" in reply
        and fake.wrong_shape == 0,
        f"reply {reply!r}, error {error!r}, "
        f"{fake.wrong_shape} payloads written in the wrong shape")

# --- a header the dongle really will not take ---------------------------------
# Retrying is not a way of ignoring a refusal: a dongle that refuses every time
# still has to reach the caller, and with its own words rather than a timeout.


class AlwaysRefuses(FakeDongle):
    def _command(self, data):
        self.garbage_errs += 1
        self._say("ERR unconfigured - run listen first")


hub = nrf24web.Hub()
session = nrf24web.Session(hub)
session.dongle = AlwaysRefuses(hub)
try:
    session.send_sequence("11223344", ["AABB"] * 8, ack=True)
    outcome = "no error"
except Exception as exc:
    outcome = str(exc)
verdict("P a header refused every time is reported, with the dongle's reason",
        "unconfigured" in outcome
        and session.dongle.payload_in_cmd_mode == 0,
        f"raised {outcome!r} after "
        f"{session.dongle.garbage_errs} refusals, "
        f"{session.dongle.payload_in_cmd_mode} payload bytes written")

verdict("P a refused header is asked again before it is given up on",
        session.dongle.garbage_errs == nrf24web.SEND_HEADER_RETRIES + 1,
        f"{session.dongle.garbage_errs} headers sent for "
        f"{nrf24web.SEND_HEADER_RETRIES} retries")

print("\n--- summary ---")
for name, ok, _ in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(0 if not failed else 1)
