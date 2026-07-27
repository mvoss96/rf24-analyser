#!/usr/bin/env python3
"""nrf24web - browser front end for the nrf24-sniffer dongle.

Python owns the serial port and does the decoding; the browser is presentation
only. That split is deliberate: bthome-ble is the reference BTHome parser and it
is a Python library, so moving frame decoding into the browser would mean a
second implementation of the object layer - exactly the drift that once made
this tool silently swallow dimmer events.

Standard library only: http.server for the pages and JSON endpoints,
Server-Sent Events for the live stream. No web framework, no websocket package.

    python nrf24web.py [--http 8724] [--no-browser]

The serial port is chosen in the UI, which remembers the last one that worked -
there is deliberately no --port flag duplicating that.
"""

import argparse
import json
import queue
import re
import socket
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import nrf24_dongle as dongle
import nrf24_parsers as parsers

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
MAX_FRAMES = 5000

APP_VERSION = "1.0.0"

# Python imports a module once and keeps it: editing nrf24_parsers.py while the
# server runs changes nothing until the process is restarted. That cost a real
# debugging session - the browser flagged correct frames as malformed for hours
# because the padding fix was on disk and not in memory. So the freshness of the
# running code is measured rather than trusted: the mtime of every source file
# this app serves from is taken at import and compared on request.
_SOURCE_FILES = ("nrf24web.py", "nrf24_parsers.py", "nrf24_dongle.py",
                 "web/index.html", "web/app.js", "web/app.css")


def _source_stamp():
    """(path, mtime) for each source file, skipping any that is missing."""
    stamp = {}
    for name in _SOURCE_FILES:
        path = HERE / name
        try:
            stamp[name] = path.stat().st_mtime
        except OSError:
            continue
    return stamp


_STARTED_AT = time.time()
_STAMP_AT_START = _source_stamp()


def _stale_sources():
    """Source files that changed on disk since this process loaded them."""
    now = _source_stamp()
    return sorted(name for name, mtime in now.items()
                  if _STAMP_AT_START.get(name) != mtime)
# Opening the port pulls DTR and resets the dongle, so the greeting takes about
# two seconds. After that we ask instead of waiting - not every adapter resets
# the board, and a dongle that was already running never greets at all. Only if
# the question goes unanswered too is the silence worth reporting.
GREETING_ASK = 2.0
GREETING_TIMEOUT = 4.5

# How often to ask the dongle what it is doing while it is connected. Every
# command this process sends already triggers one, so the heartbeat only exists
# for the configurations that never came through here at all - the MCP tools,
# bench/*.py, a curl on /api/command from another shell. Without it the display
# would keep showing the last thing this process happened to witness, which is
# exactly how a page ended up claiming ch100 while the dongle sat on 90.
INFO_HEARTBEAT = 10.0

# Everything else this process sends may change what the dongle is doing, so it
# is followed by an `info`. A tx does not, and a burst is dozens of them.
NO_REFRESH_AFTER = {"tx", "info"}


def column_spec(parser):
    """The decoder's table columns, as the browser wants them."""
    return [{"key": key, "label": label, "width": width,
             "packet": key == parser.packet_column}
            for key, label, width in parser.columns]


def _capture_stats(frames):
    """Per-sender summary of a captured window, for an autonomous consumer.

    Loss is computed over the set of distinct packet ids per sender, not frame
    by frame. A sender repeats each event several times and the repeats arrive
    interleaved - ...66, 67, 66, 68, 67... - so a frame-by-frame difference reads
    every step back to an earlier repeat as a huge gap. What is missing is which
    counter values never appeared at all: the span from the first id to the last
    (one byte, so one wrap is handled) minus the count of distinct ids seen.

    A wrap of the byte counter cannot be told from a genuine loss of 256 from
    these numbers, so `missing_uncertain` flags a span that used most of the
    cycle - the same caution the live table shows as "at least".
    """
    senders = {}
    events = set()
    for frame in frames:
        events.add(frame.get("identity"))
        source = frame.get("source")
        if source is None:
            continue
        s = senders.setdefault(source, {"frames": 0, "events": set(), "ids": [], "id_set": set()})
        s["frames"] += 1
        s["events"].add(frame.get("identity"))
        packet_id = frame.get("packetId")
        if packet_id is not None:
            s["ids"].append(packet_id)
            s["id_set"].add(packet_id)

    total_missing = 0
    out_senders = {}
    for source, s in senders.items():
        summary = {"frames": s["frames"], "events": len(s["events"])}
        if s["ids"]:
            span, missing = _id_span(s["id_set"])
            total_missing += missing
            summary.update(first_id=s["ids"][0], last_id=s["ids"][-1],
                           distinct_ids=len(s["id_set"]), missing=missing,
                           missing_uncertain=span >= 128)
        out_senders[source] = summary

    return {
        "frames": len(frames),
        "events": len(events - {None}),
        "senders": out_senders,
        "missing": total_missing,
    }


def _id_span(id_set):
    """(span, missing) for a set of byte packet ids on the 256 ring.

    The smallest arc that contains every id is the whole ring minus its largest
    empty gap; that arc is wrap-agnostic, so it needs neither a known start nor
    an assumption that ids arrive in order. `missing` is the arc's length minus
    the ids actually seen - the counter values that never appeared.
    """
    ids = sorted(id_set)
    n = len(ids)
    if n == 1:
        return 1, 0
    gaps = [ids[i + 1] - ids[i] for i in range(n - 1)]
    gaps.append(256 - ids[-1] + ids[0])   # the wrap-around gap
    span = 257 - max(gaps)                 # ids from one arc end to the other
    return span, span - n


class Hub:
    """Fan-out of events to every connected browser tab."""

    def __init__(self):
        self._subscribers = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event):
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            q.put(event)


class Session:
    """Owns the dongle, the decoded frame history and the active decoder."""

    def __init__(self, hub):
        self.hub = hub
        self.dongle = None
        self.parser = parsers.get("bthome")
        self.frames = deque(maxlen=MAX_FRAMES)  # raw (stamp, pipe, data)
        self.last_stamp = None
        self.last_ms = None
        # Remembered so a tab opened later still learns the current state -
        # the greeting only arrives once, at reset.
        self.greeting = None
        self.state_text = "not connected"
        self.was_listening = False
        self.scan = None
        # What the dongle last said about itself, from an `info` block. This is
        # the only thing the browser is allowed to render as radio state: an
        # input field holds what someone typed, which is a wish, and a wish that
        # is drawn as a fact makes the tool lie for as long as nobody notices.
        self.radio = None
        self.radio_at = None
        self._info_block = None     # lines collected since the `info:` header
        self._info_quiet = False    # swallow this block instead of logging it
        self._info_pending = 0      # blocks asked for by this class, not a user
        self._pump = None
        self._beat = None
        self._stop = threading.Event()
        # Serialises command()s so each reply is matched to its own command.
        # Only API-side commands hold it; a human typing in the browser terminal
        # can still interleave - the dongle is shared, that is documented.
        self._cmd_lock = threading.Lock()

    # -- connection --

    def connect(self, port):
        self.disconnect()
        self.dongle = dongle.Dongle(port)
        self.dongle.open()
        self._stop.clear()
        self._pump = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump.start()
        self._beat = threading.Thread(target=self._heartbeat_loop,
                                      args=(self.dongle,), daemon=True)
        self._beat.start()
        # Not "connected" yet: the port is open, but nothing has proved there is
        # a sniffer on the other end. The greeting is what does that.
        self.state_text = "connecting…"
        self.hub.publish(self.status_event())
        self._unless_greeted(GREETING_ASK, self._ask_status)
        self._unless_greeted(GREETING_TIMEOUT, self._greeting_overdue)

    def _unless_greeted(self, delay, action):
        """Runs `action` later, unless the greeting arrived or the port changed."""
        expected = self.dongle

        def run():
            if self.dongle is expected and self.greeting is None:
                action()

        timer = threading.Timer(delay, run)
        timer.daemon = True
        timer.start()

    def _ask_status(self):
        try:
            self.send("status")
        except Exception:
            pass  # disconnected in the meantime; the overdue timer will notice

    def _greeting_overdue(self):
        self.state_text = "no greeting"
        self.hub.publish({
            "type": "line", "kind": "warn",
            "text": f"WARN no answer to status after {GREETING_TIMEOUT:.1f}s - "
                    f"wrong port, or the dongle is not running this firmware?"})
        self.hub.publish(self.status_event())

    def disconnect(self):
        self._stop.set()
        if self.dongle is not None:
            self.dongle.close()
            self.dongle = None
        self.last_stamp = None
        self.last_ms = None
        self.greeting = None
        self.was_listening = False
        self.state_text = "not connected"
        self.radio = None
        self.radio_at = None
        self._info_block = None
        self._info_pending = 0
        self.hub.publish(self.status_event())
        self.hub.publish(self.radio_event())

    def status_event(self):
        # The port comes off the open dongle, never from an argument or from
        # whatever a browser last selected: a tab that did not do the connecting
        # showed the port from its own dropdown, which was COM9 while the
        # capture underneath it came from COM18.
        return {"type": "status", "connected": self.dongle is not None,
                "port": self.dongle.port if self.dongle else None,
                "state": self.state_text, "greeting": self.greeting}

    def radio_event(self):
        return {"type": "radio", "radio": self.radio, "at": self.radio_at}

    def set_state(self, text):
        """Records the state and tells every tab.

        The browser used to infer this from the text of log lines, which meant
        every new state had to be taught to two places - and the one that was
        forgotten was the browser, silently.
        """
        if text == self.state_text:
            return
        self.state_text = text
        self.hub.publish(self.status_event())

    def send(self, line, quiet=False):
        if self.dongle is None:
            raise RuntimeError("not connected")
        self.dongle.send(line)
        if quiet:
            return
        self.hub.publish({"type": "line", "text": f"> {line}", "kind": "sent"})
        # Whatever that was, it may have changed what the dongle is doing, and
        # only the dongle knows what it did with it. Asking straight afterwards
        # is free: the firmware handles lines in order, so the answer describes
        # the state the command left behind, not the one before it.
        head = line.split()[0] if line.split() else ""
        if head not in NO_REFRESH_AFTER:
            self.refresh_info()

    def refresh_info(self):
        """Asks the dongle what it is doing; the answer is not printed.

        Swallowing the block is what makes polling possible at all - twenty
        lines every ten seconds would bury the terminal panel. The snapshot it
        produces is published instead, and that is what the display reads.
        """
        self._info_pending += 1
        try:
            self.send("info", quiet=True)
        except Exception:
            self._info_pending = max(0, self._info_pending - 1)
            raise

    def _heartbeat_loop(self, owner):
        while not self._stop.wait(INFO_HEARTBEAT):
            # Reconnecting clears the stop flag again within microseconds, which
            # this thread may sleep straight through - so it checks whose dongle
            # the session holds now rather than trusting the flag alone.
            if self.dongle is not owner:
                return
            # A sweep retunes the radio across the band and reports as it goes;
            # asking it about itself in the middle of that interleaves with the
            # report and tells us only that it is scanning, which we know.
            if self.state_text == "scanning":
                continue
            try:
                self.refresh_info()
            except Exception:
                pass   # disconnected between the check and the write

    def _set_radio(self, info):
        self.radio = info
        self.radio_at = time.time()
        state = info.get("state")
        # The dongle's own word for what it is doing outranks the inference from
        # OK lines: that one only knows about the commands this process saw.
        if state in ("listening", "idle"):
            self.was_listening = state == "listening"
        if state:
            self.set_state(state)
        self.hub.publish(self.radio_event())

    def _consume_info(self, line):
        """Feeds one line to the `info:` collector; True if it must not be shown.

        Every block is collected, whoever asked for it - the heartbeat, an agent
        on /api/command, a human typing `info` in the terminal - so the snapshot
        is never more than one poll behind. Only the blocks this class asked for
        are swallowed; a typed `info` still prints its answer, as it must.
        """
        if line == dongle.INFO_HEADER:
            self._info_block = []
            self._info_quiet = self._info_pending > 0
            self._info_pending = max(0, self._info_pending - 1)
            return self._info_quiet
        if line.startswith("  "):
            self._info_block.append(line)
            return self._info_quiet
        if line.startswith("OK") or line.startswith("ERR"):
            if line.startswith("OK"):
                self._set_radio(dongle.parse_info(self._info_block))
            quiet, self._info_quiet = self._info_quiet, False
            self._info_block = None
            return quiet
        # A WARN can land in the middle of the block. It is not part of it and
        # not ours to swallow, and it does not end the block either.
        return False

    def command(self, line, timeout=5.0):
        """Sends one command and returns the firmware's OK/ERR reply line.

        The fire-and-forget send() leaves the reply in the event stream, where
        only a browser can see it - an HTTP consumer (the MCP server above all)
        was left believing every command succeeded. Waiting here is what makes
        "ERR bad payload byte" reach the caller that caused it. The lock keeps
        concurrent API commands from claiming each other's replies; RX frames
        and WARN lines pass through unclaimed either way.
        """
        with self._cmd_lock:
            q = self.hub.subscribe()
            try:
                self.send(line)
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            f"no OK/ERR reply to {line.split()[0]!r} within {timeout:.1f}s")
                    try:
                        event = q.get(timeout=remaining)
                    except queue.Empty:
                        continue
                    # `status` answers with the greeting line, not OK/ERR - it
                    # is a reply all the same (and the one that carries fw=).
                    if event.get("type") == "greeting":
                        return event.get("text", "")
                    if event.get("type") != "line" or event.get("kind") == "sent":
                        continue
                    text = event.get("text", "")
                    if text.startswith("OK") or text.startswith("ERR"):
                        return text
            finally:
                self.hub.unsubscribe(q)

    def burst(self, address, frames, ack=False):
        """Transmits a sequence of frames, waiting for each firmware reply.

        `frames` entries are {"payload": hex, "repeat": 1..16, "gap_ms": 0..250,
        "address": optional override, "pause_ms": host-side pause afterwards} -
        or a bare payload string. Repeats of one payload are the firmware's
        x<n>, i.e. genuinely milliseconds apart; between entries sits at least
        the serial round trip (~5-10 ms). Returns one result per entry; an ERR
        does not stop the rest - entries are independent stimuli and the caller
        sees per-entry ok flags.
        """
        if not isinstance(frames, list) or not frames:
            raise ValueError("frames must be a non-empty list")
        if len(frames) > 64:
            raise ValueError("at most 64 frames per burst")

        results = []
        for entry in frames:
            if isinstance(entry, str):
                entry = {"payload": entry}
            payload = str(entry.get("payload", "")).strip()
            if not payload:
                raise ValueError("frame without payload")
            repeat = int(entry.get("repeat", 1))
            gap_ms = int(entry.get("gap_ms", 0))
            if not 1 <= repeat <= 16:
                raise ValueError("repeat must be 1..16")
            if not 0 <= gap_ms <= 250:
                raise ValueError("gap_ms must be 0..250")
            addr = str(entry.get("address") or address or "").strip()
            if not addr:
                raise ValueError("frame without address (no burst-level default)")

            line = f"tx {addr} {payload} {'ack' if ack else 'noack'}"
            if repeat > 1:
                line += f" x{repeat}"
            if gap_ms:
                line += f" gap={gap_ms}"
            reply = self.command(line, timeout=5.0 + repeat * (gap_ms + 50) / 1000.0)

            m = re.search(r"sent=(\d+)(?:/(\d+))?", reply)
            sent = int(m.group(1)) if m else 0
            results.append({"payload": payload, "address": addr, "repeat": repeat,
                            "gap_ms": gap_ms, "reply": reply,
                            "sent": sent, "ok": reply.startswith("OK") and sent == repeat})

            pause_ms = int(entry.get("pause_ms", 0))
            if pause_ms > 0:
                time.sleep(min(pause_ms, 10000) / 1000.0)
        return results

    # -- decoding --

    def set_parser(self, name):
        parser = parsers.get(name)
        if parser is None:
            raise ValueError(f"unknown decoder {name!r}")
        reason = parser.available()
        if reason:
            raise RuntimeError(reason)
        self.parser = parser
        # One decoder is shared by every tab, so every tab has to hear about it.
        self.hub.publish({"type": "parser", "name": parser.name,
                          "columns": column_spec(parser)})

    def decoded_history(self):
        """Every retained frame, decoded with the current parser."""
        return [self._decode(*frame) for frame in self.frames]

    def capture(self, seconds):
        """Collect the frames decoded over the next `seconds` and summarise them.

        Subscribes to the same event stream the browser tabs use, so it captures
        exactly what the running configuration receives - it does not touch the
        dongle or the port, which is what lets a second consumer read along while
        someone watches in the browser. The radio must already be listening; how
        it is configured is the caller's business (that is what nrf24_configure
        and tx are for).
        """
        seconds = max(0.1, min(float(seconds), 600.0))
        q = self.hub.subscribe()
        collected = []
        deadline = time.monotonic() + seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    event = q.get(timeout=remaining)
                except queue.Empty:
                    break
                if event.get("type") == "frame":
                    collected.append(event)
        finally:
            self.hub.unsubscribe(q)

        return {
            "seconds": seconds,
            "decoder": self.parser.name,
            "listening": self.state_text == "listening",
            "frames": collected,
            "stats": _capture_stats(collected),
        }

    def _decode(self, stamp, delta, device_ms, pipe, data, intact=None):
        # A frame the checksum rejects was altered between the RX FIFO and here,
        # so nothing decoded from it describes the air. Say that instead of
        # decoding it: a plausible-looking row is worse than an obviously broken
        # one, and a flipped bit in a packet id reads as a whole extra event.
        if intact is False:
            return {
                "type": "frame", "time": stamp, "delta": delta, "deviceMs": device_ms,
                "pipe": pipe, "len": len(data),
                "cells": {"data": "!! corrupted between radio and host (checksum)"},
                "source": None, "packetId": None,
                "detail": ["  !! CHECKSUM MISMATCH - the payload below is not what the radio received",
                           "     (corruption on the SPI read or the serial line, not on the air)"],
                "hex": parsers.hexdump(data),
                "raw": bytes(data).hex().upper(),
                "flagged": True,
                "identity": None,
            }
        try:
            cells = {key: str(value) for key, value in self.parser.cells(data).items()}
            detail = self.parser.detail(data)
            identity = self.parser.identity(data)
            source, packet_id = self.parser.source(data), self.parser.packet_id(data)
        except Exception as exc:
            cells, detail = {"data": f"!! decoder error: {exc}"}, [str(exc)]
            identity, source, packet_id = bytes(data).hex(), None, None
        flagged = any("!!" in value for value in cells.values())
        return {
            "type": "frame",
            "time": stamp,
            "delta": delta,
            "deviceMs": device_ms,
            "pipe": pipe,
            "len": len(data),
            "cells": cells,
            # Neutral metadata the table reasons about without knowing the
            # protocol: who sent it, and the sender's own count for it.
            "source": source,
            "packetId": packet_id,
            "detail": detail,
            "hex": parsers.hexdump(data),
            # The payload as one compact hex string. The hexdump above is laid
            # out for a human to read; a consumer comparing two frames byte for
            # byte should not have to unpick its columns first.
            "raw": bytes(data).hex().upper(),
            "flagged": flagged,
            # Retransmissions of one event share this; the UI folds them into a
            # single row. Decoder-specific, so it is recomputed on every switch.
            "identity": identity,
        }

    # -- serial pump --

    def _pump_loop(self):
        while not self._stop.is_set():
            try:
                line = self.dongle.lines.get(timeout=0.2)
            except queue.Empty:
                continue
            except Exception:
                break
            self._handle(line)

    def _handle(self, line):
        received = dongle.parse_rx(line)
        if received is not None:
            device_ms, pipe, data, intact = received
            now = time.time()
            stamp = (time.strftime("%H:%M:%S", time.localtime(now))
                     + f".{int(now % 1 * 1000):03d}")
            # Wall clock for "when", the dongle's own clock for "how far apart".
            # The gap between a sender's repeats is a few milliseconds, which is
            # the same order as the serial transfer and this thread's scheduling
            # - measuring it here would mostly measure the host.
            if device_ms is not None:
                delta = None if self.last_ms is None else round(device_ms - self.last_ms, 1)
                self.last_ms = device_ms
            else:
                delta = None if self.last_stamp is None else round((now - self.last_stamp) * 1000, 1)
            self.last_stamp = now
            self.frames.append((stamp, delta, device_ms, pipe, data, intact))
            self.hub.publish(self._decode(stamp, delta, device_ms, pipe, data, intact))
            return

        # `info` answers with an indented block closed by OK, and it is the one
        # thing in this stream that describes the radio rather than reporting an
        # event. RX lines were handled above, so a frame arriving mid-block
        # cannot break it.
        if line == dongle.INFO_HEADER or self._info_block is not None:
            if self._consume_info(line):
                return

        greeting = dongle.parse_greeting(line)
        if greeting is not None:
            event = {"type": "greeting", "fields": greeting, "text": line,
                     "apiOk": greeting.get("api") == str(dongle.EXPECTED_API),
                     "expectedApi": dongle.EXPECTED_API}
            self.greeting = event
            self.state_text = greeting.get("state", "connected")
            self.was_listening = self.state_text == "listening"
            self.hub.publish(event)
            # The greeting proves there is a sniffer there and carries the
            # wiring, but not the radio configuration - and after a reset it
            # would not be the current one anyway. Ask, so the first thing the
            # display shows is the dongle and not a page default.
            self.refresh_info()
            return

        # A scan answers with one line per channel that had a hit, so a quiet
        # band produces nothing between "passes=" and "done". Collected into one
        # event, an empty result can say it is empty instead of looking broken.
        if line.startswith("SCAN passes="):
            self.scan = {"passes": int(line.split("=", 1)[1]), "hits": {}}
            self.hub.publish({"type": "scan", "state": "running"})
            return
        if line.startswith("SCAN ch="):
            hit = dongle.parse_scan(line)
            if hit is not None and self.scan is not None:
                self.scan["hits"][hit[0]] = hit[1]
            return
        if line.startswith("SCAN end"):
            if self.scan is not None:
                self.hub.publish({"type": "scan", "state": "done", **self.scan})
                self.scan = None
            return
        if line.startswith("OK scan live"):
            self.set_state("scanning")
        elif line.startswith("OK scan stopped"):
            # Stopping cuts the report window short, so the block already
            # announced will never be completed. Counting it would show a
            # fraction of a sweep against a full sweep's denominator.
            self.scan = None
            self.set_state("listening" if self.was_listening else "idle")
        elif line.startswith("OK listening"):
            self.was_listening = True
            self.set_state("listening")
        elif line.startswith("OK stopped"):
            self.was_listening = False
            self.set_state("idle")

        kind = "info"
        if line.startswith("ERR"):
            kind = "error"
        elif line.startswith("WARN"):
            kind = "warn"
        elif line.startswith("OK"):
            kind = "ok"
        self.hub.publish({"type": "line", "text": line, "kind": kind})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub = None
    session = None

    def log_message(self, *_args):
        pass  # the console belongs to the user, not to request logging

    def handle(self):
        # Closing a tab aborts its event stream mid-read, which is normal here
        # and not worth a traceback in the user's console every time.
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass

    # -- helpers --

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _file(self, name, content_type):
        path = WEB_DIR / name
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- routes --

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif self.path == "/app.css":
            self._file("app.css", "text/css; charset=utf-8")
        elif self.path == "/app.js":
            self._file("app.js", "application/javascript; charset=utf-8")
        elif self.path == "/api/ports":
            self._json([{"device": d, "description": desc}
                        for d, desc in dongle.available_ports()])
        elif self.path == "/api/parsers":
            # `active` matters: the decoder is server state, shared by every tab.
            # A page that picked its own default instead of reading this showed a
            # decoder name that had nothing to do with the rows underneath it.
            active = self.session.parser
            self._json([{"name": p.name, "label": p.label,
                         "description": p.description,
                         "unavailable": p.available(),
                         "active": p is active,
                         # The table header is the decoder's to define; only the
                         # radio-level columns are fixed.
                         "columns": column_spec(p)}
                        for p in parsers.all_parsers()])
        elif self.path.startswith("/api/frames"):
            # The retained history, decoded - what the browser shows, for a
            # consumer that has no browser. capture() only ever sees the window
            # it was called for, so a frame already on screen was unreachable
            # to an agent until now; reading back over past frames is how you
            # tell a repeat from a new event after the fact.
            query = parse_qs(urlparse(self.path).query)
            frames = self.session.decoded_history()
            try:
                limit = int(query.get("limit", ["0"])[0])
            except ValueError:
                limit = 0
            if limit > 0:
                frames = frames[-limit:]
            self._json({"decoder": self.session.parser.name,
                        "state": self.session.state_text,
                        "retained": len(self.session.frames),
                        "frames": frames})
        elif self.path == "/api/events":
            self._events()
        elif self.path == "/api/state":
            # A synchronous snapshot for a non-browser consumer: the browser
            # learns state from the SSE stream, but an agent wants one answer to
            # one question ("is it listening, on what wiring?").
            session = self.session
            greeting = session.greeting or {}
            stale = _stale_sources()
            radio = session.radio or {}
            self._json({"connected": session.dongle is not None,
                        "port": session.dongle.port if session.dongle else None,
                        "state": session.state_text,
                        "decoder": session.parser.name,
                        # What the dongle last said about itself, and how long
                        # ago it said it: a caller that has to trust this needs
                        # to know whether it is a second or an hour old.
                        "radio": session.radio,
                        "radioAge": (None if session.radio_at is None
                                     else round(time.time() - session.radio_at, 1)),
                        # The wiring the dongle reports, falling back to the one
                        # it greeted with before the first info arrived.
                        "wiring": radio.get("wiring") or
                                  ({k: greeting.get("fields", {}).get(k)
                                    for k in ("ce", "csn", "irq", "led_rx", "led_tx")}
                                   if greeting else None),
                        # The running build, so an answer from this server can be
                        # told apart from an answer from the code on disk.
                        "app": {"version": APP_VERSION,
                                "started": _STARTED_AT,
                                "uptime": round(time.time() - _STARTED_AT),
                                "stale": stale}})
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            payload = self._body()
            if self.path == "/api/connect":
                self.session.connect(payload["port"])
            elif self.path == "/api/disconnect":
                self.session.disconnect()
            elif self.path == "/api/reconnect":
                # Reopening the port pulls DTR, which resets the dongle: the
                # cheapest way to prove a frame did not come out of the
                # sniffer's own RX FIFO is to empty it and look again. The
                # radio configuration does not survive - `listen` again after.
                port = payload.get("port") or (self.session.dongle and self.session.dongle.port)
                if not port:
                    raise RuntimeError("not connected and no port given")
                self.session.connect(port)
                self._json({"ok": True, "port": port})
                return
            elif self.path == "/api/command":
                # wait=true turns the fire-and-forget send into a round trip:
                # the response carries the firmware's own OK/ERR line, so a
                # non-browser consumer finally sees its errors.
                if payload.get("wait"):
                    reply = self.session.command(payload["line"])
                    self._json({"ok": not reply.startswith("ERR"), "reply": reply})
                    return
                self.session.send(payload["line"])
            elif self.path == "/api/burst":
                results = self.session.burst(payload.get("address"),
                                             payload.get("frames"),
                                             bool(payload.get("ack", False)))
                self._json({"ok": all(r["ok"] for r in results), "results": results})
                return
            elif self.path == "/api/capture":
                # Blocks for the window, which ThreadingHTTPServer serves on its
                # own thread, so the browser and its SSE stream keep running.
                self._json(self.session.capture(payload.get("seconds", 10)))
                return
            elif self.path == "/api/parser":
                self.session.set_parser(payload["name"])
                self._json({"ok": True, "columns": column_spec(self.session.parser),
                            "frames": self.session.decoded_history()})
                return
            elif self.path == "/api/clear":
                self.session.frames.clear()
                self.session.last_stamp = None
                self.session.last_ms = None
            else:
                self.send_error(404)
                return
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, status=400)
            return
        self._json({"ok": True})

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = self.hub.subscribe()
        try:
            # Bring a fresh tab up to date with what was already captured.
            # Greeting first, status second: the greeting carries the state as
            # it was at reset, so the current status has to land after it.
            if self.session.greeting is not None:
                self._sse(self.session.greeting)
            self._sse(self.session.status_event())
            # Third, because it outranks both: what the dongle itself last said.
            self._sse(self.session.radio_event())
            for frame in self.session.decoded_history():
                self._sse(frame)
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")   # keep proxies and idle sockets alive
                    self.wfile.flush()
                    continue
                self._sse(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.hub.unsubscribe(q)

    def _sse(self, event):
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


def _already_serving(port):
    """True if something answers an HTTP request on the port already."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="Browser front end for the nrf24-sniffer dongle.")
    ap.add_argument("--http", type=int, default=8724, help="http port (default 8724)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.http}/"

    # On Windows the server socket allows address reuse, so a second start would
    # bind the same port instead of failing - two servers, and the second one
    # cannot open the dongle the first is holding. Double-clicking start.cmd a
    # second time is exactly how that happens, so check first and just point the
    # browser at the instance already running.
    if _already_serving(args.http):
        print(f"nrf24-sniffer is already running on {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    hub = Hub()
    session = Session(hub)
    Handler.hub = hub
    Handler.session = session

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)
    except OSError as exc:
        print(f"cannot start on port {args.http}: {exc}")
        return
    print(f"nrf24-sniffer web ui on {url}   (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.disconnect()
        server.server_close()


if __name__ == "__main__":
    main()
