#!/usr/bin/env python3
"""nrf24web - browser front end for the nRF24 Analyser dongle.

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
import os
import queue
import re
import socket
import subprocess
import sys
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

# Version of this web app, shown in the corner so an answer can be told apart
# from the code on disk. It moves with every change that reaches a user, in the
# same shape as the firmware's: a feature raises the minor, a fix the patch.
#
# 1.1.0 is where it stopped standing still. Since 1.0.0: every status display
#       reads the dongle rather than the setup form; `listen` and `hwset`
#       acknowledge with the state they left behind; the project is called
#       nRF24 Analyser; the event stream is numbered and resumable; the scan
#       chart reads out the channel under the pointer; a dongle that stops
#       answering is reported instead of shown as listening; the stale-build
#       warning restarts into the new code when clicked; the frame list filters
#       by pipe and by sender, colours them apart once there are two to tell
#       apart, hides columns that are in the way, and stopped taking minutes to
#       redraw a full history.
APP_VERSION = "1.1.0"

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

# Identifies this run in the event ids handed to browsers. Event numbering
# starts over with the process, so the number alone cannot say whether a client
# is resuming from this run's stream or a previous one's.
_RUN = str(int(_STARTED_AT))

# The listening server, so a restart can hand its port to the successor.
_SERVER = None


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
# command sent from here already triggers one, and the MCP tools and any curl go
# through /api/command, so this is not the main path - it is the bound on how
# wrong the display can be when the assumption behind that sentence does not
# hold, and it is what notices a dongle that has stopped answering at all. Five
# seconds because that bound is also the detection delay below; the traffic is
# twenty short lines and a handful of register reads, which is nothing next to
# measuring against a radio that is not there.
INFO_HEARTBEAT = 5.0

# Everything else this process sends may change what the dongle is doing, so it
# is followed by an `info`. A tx does not, and a burst is dozens of them.
NO_REFRESH_AFTER = {"tx", "info"}

# How many heartbeats may go unanswered before the dongle counts as gone. The
# poll was already measuring this and throwing the result away: after a suspend
# and resume, one of these servers kept saying "listening" with a full
# configuration for seven hours while its port had been dead the whole time.
# Every poll in that span ran into a timeout and nobody drew a conclusion.
#
# Two, because the dongle answers `info` in milliseconds and waiting for a third
# would only be caution about nothing: half a minute of measuring against a dead
# radio is a real cost, and a wrong guess is not - the next answer clears the
# state by itself. The one thing that can legitimately delay a reply is a tx
# burst, whose gaps are firmware-side and can run a few seconds; that flags the
# radio for one poll and heals on the next.
DEAF_AFTER_POLLS = 2

# Not "listening" and not "not connected": the port is open and the process is
# fine, the thing on the other end has stopped answering. Naming it separately
# is the point - the two states it sits between are both reassuring.
DEAF_STATE = "no answer"


def column_spec(parser):
    """The decoder's table columns, as the browser wants them."""
    return [{"key": key, "label": label, "width": width,
             "packet": key == parser.packet_column,
             "source": key == parser.source_column}
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
        self._seq = 0

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
        """Numbers the event, hands it to every subscriber, returns the number.

        Numbered here because this is the one place every event passes through,
        so it is the only place that can promise the numbers run in the order
        the subscribers see them. That is what lets a browser whose connection
        dropped ask for the rest instead of for everything again - EventSource
        reconnects on its own, and a replayed history appended to a table that
        was never cleared shows every frame twice. In a tool whose purpose is
        counting retransmissions, that is not a cosmetic fault.
        """
        with self._lock:
            self._seq += 1
            event["id"] = self._seq
            targets = list(self._subscribers)
        for q in targets:
            q.put(event)
        return self._seq


class Session:
    """Owns the dongle, the decoded frame history and the active decoder."""

    def __init__(self, hub):
        self.hub = hub
        self.dongle = None
        self.parser = parsers.get("bthome")
        # Raw (event id, stamp, delta, device_ms, pipe, data, intact). The event
        # id is kept with the frame so a replay hands out the same numbers the
        # live stream did, which is what makes resuming from one possible.
        self.frames = deque(maxlen=MAX_FRAMES)
        # The event id at the last clear. A tab that was disconnected across one
        # would otherwise resume from before it and keep rows the server has
        # thrown away.
        self.cleared_at = 0
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
        self._unanswered = 0        # heartbeats the dongle has not answered
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
        self._unanswered = 0
        self.hub.publish(self.status_event())
        self.hub.publish(self.radio_event())

    def status_event(self):
        # The port comes off the open dongle, never from an argument or from
        # whatever a browser last selected: a tab that did not do the connecting
        # showed the port from its own dropdown, which was COM9 while the
        # capture underneath it came from COM18.
        return {"type": "status", "connected": self.dongle is not None,
                "port": self.dongle.port if self.dongle else None,
                "state": self.state_text, "greeting": self.greeting,
                # How long the dongle has been silent, once it counts as gone.
                # Computed here rather than from the snapshot's timestamp in the
                # browser: the two clocks need not agree, and this one is the
                # clock the silence was measured against.
                "silentFor": (round(time.time() - self.radio_at, 1)
                              if self.state_text == DEAF_STATE and self.radio_at
                              else None)}

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
            # The reader thread ends only when the port broke away underneath
            # it; a port that was closed properly took the whole session with
            # it. Nothing else will notice - writes to the dead handle can go on
            # succeeding - so this is where a vanished dongle is reported.
            if not owner.reading:
                self._port_lost("the serial port went away "
                                "(unplugged, or the host suspended)")
                return
            # A sweep retunes the radio across the band and reports as it goes;
            # asking it about itself in the middle of that interleaves with the
            # report and tells us only that it is scanning, which we know.
            if self.state_text == "scanning":
                continue
            # Counted before the question is asked, cleared when an answer
            # arrives. Silence is the only evidence available: the write can
            # succeed against a handle whose device is long gone.
            self._unanswered += 1
            if self._unanswered == DEAF_AFTER_POLLS:
                self.hub.publish({
                    "type": "line", "kind": "warn",
                    "text": f"WARN no answer to {DEAF_AFTER_POLLS} status polls "
                            f"({DEAF_AFTER_POLLS * INFO_HEARTBEAT:.0f}s) - the "
                            f"configuration shown is the last one it reported"})
            if self._unanswered >= DEAF_AFTER_POLLS:
                self.set_state(DEAF_STATE)
            try:
                self.refresh_info()
            except Exception:
                pass   # disconnected between the check and the write

    def _port_lost(self, why):
        self.hub.publish({"type": "line", "kind": "error",
                          "text": f"ERR {why} - disconnected"})
        self.disconnect()

    def _set_radio(self, info):
        self.radio = info
        self.radio_at = time.time()
        # It answered, so it is not deaf - whatever it was that answered, a poll
        # or a command's acknowledgement. The state below comes from the dongle
        # itself and replaces "no answer" without any special case for it.
        self._unanswered = 0
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
        """Every retained frame, decoded with the current parser.

        Each carries the event id it was published under, so a client can tell
        which of them it has already seen.
        """
        return [dict(self._decode(*frame[1:]), id=frame[0]) for frame in self.frames]

    def clear(self):
        """Discards the retained history - for everyone, not just the caller.

        Every tab is told, because the alternative is a second tab going on
        showing frames this process no longer has: the same one-truth rule the
        rest of the display follows.
        """
        self.frames.clear()
        self.last_stamp = None
        self.last_ms = None
        self.cleared_at = self.hub.publish({"type": "reset"})

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
            seq = self.hub.publish(self._decode(stamp, delta, device_ms, pipe,
                                                data, intact))
            self.frames.append((seq, stamp, delta, device_ms, pipe, data, intact))
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

        # From firmware 3.5.0 the acknowledgement of a command that changed the
        # radio carries the state it left behind. Taking it here means the
        # snapshot is right the moment the command is answered, without waiting
        # for the poll - and it is the firmware's account of what it did, not an
        # echo of what was asked, so a downgraded irq pin arrives with the OK
        # that reports success. Older firmware answers with a bare OK; then this
        # is None and the poll below does the work as before.
        ack = dongle.parse_ack(line)
        if ack is not None:
            self._set_radio(ack)

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
                        # Which build the dongle is running, and whether it
                        # speaks the command protocol this host does. Only the
                        # greeting carries it, so it is null until one arrives.
                        "firmware": {"fw": greeting.get("fields", {}).get("fw"),
                                     "api": greeting.get("fields", {}).get("api"),
                                     "expectedApi": dongle.EXPECTED_API,
                                     "apiOk": greeting.get("apiOk")}
                                    if greeting else None,
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
            elif self.path == "/api/restart":
                # Answer first, restart after: the reply is the last thing this
                # process will manage to say.
                self._json({"ok": True, "stale": _stale_sources(),
                            "port": (self.session.dongle.port
                                     if self.session.dongle else None)})
                threading.Thread(target=restart, daemon=True).start()
                return
            elif self.path == "/api/clear":
                self.session.clear()
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
        resume = self._resume_from()
        try:
            # A tab that cannot be continued has to be replaced: whatever is on
            # its screen came from a process that is gone, or from before a
            # clear. Sent before the replay, so the table is empty when it lands.
            if resume is None:
                self._sse({"type": "reset"})
            # Bring the tab up to date with what was already captured. Greeting
            # first, status second: the greeting carries the state as it was at
            # reset, so the current status has to land after it. All three are
            # snapshots of now, not history, so they go out either way - sending
            # the current truth twice costs nothing, omitting it costs the tab.
            if self.session.greeting is not None:
                # Stripped of its number: it is being replayed as a snapshot,
                # and the number it was first published under is far behind. A
                # tab reconnecting with nothing to catch up on would otherwise
                # adopt that old number as its resume point - EventSource takes
                # the last id it saw - and ask for the whole history again at
                # the next drop.
                self._sse({k: v for k, v in self.session.greeting.items()
                           if k != "id"})
            self._sse(self.session.status_event())
            # Third, because it outranks both: what the dongle itself last said.
            self._sse(self.session.radio_event())
            for frame in self.session.decoded_history():
                if resume is None or frame["id"] > resume:
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

    def _resume_from(self):
        """The last event id this client saw, if this process issued it.

        EventSource sends back the last `id:` it received, by itself, on every
        reconnect - which is exactly the field SSE has for this and the reason
        it beats a hand-rolled websocket here rather than merely being simpler.
        The id is scoped by a token for this process: after a restart the
        numbering begins again, and a client resuming from a number this run has
        not reached yet would be sent nothing at all and go on showing rows from
        a process that no longer exists. Unparseable, foreign, or from before a
        clear all mean the same thing - there is nothing to continue.
        """
        run, _, seq = self.headers.get("Last-Event-ID", "").partition("-")
        if run != _RUN or not seq.isdigit():
            return None
        resume = int(seq)
        return None if resume < self.session.cleared_at else resume

    def _sse(self, event):
        # Only events that went through the hub carry a number. The snapshots
        # replayed on connect deliberately do not: they describe now, so they
        # must not move a client's resume point backwards or forwards.
        if "id" in event:
            self.wfile.write(f"id: {_RUN}-{event['id']}\n".encode())
        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


def _already_serving(port):
    """True if something answers an HTTP request on the port already."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _reconnect_quietly(session, port):
    """Reopens the port a restart inherited. A failure is reported, not raised.

    The dongle may be gone by now - that is one of the reasons somebody
    restarts - and a traceback in the console would say less than the line the
    browser gets.
    """
    try:
        session.connect(port)
    except Exception as exc:
        session.hub.publish({"type": "line", "kind": "error",
                             "text": f"ERR could not reopen {port} after the restart: {exc}"})


def restart():
    """Replaces this process with one running the code that is on disk.

    Python keeps what it imported, so the only way to pick up an edit is to
    start again - which is why the corner says so when a source file has moved
    past the process. Doing it from in here saves the round trip through a
    terminal, but never on its own: a restart pulls DTR and resets the dongle,
    so a capture in progress and the radio configuration both go with it. That
    is a price for the person watching to agree to, not for a file watcher.

    The port that was open is handed to the successor, because putting it back
    is what makes the button worth pressing. The radio configuration is not: it
    did not survive the reset, and re-applying a remembered one would be this
    program deciding what the radio should be doing.
    """
    time.sleep(0.2)          # let the reply reach the browser before we go
    session = Handler.session
    port = session.dongle.port if session.dongle else None
    session.disconnect()     # the successor cannot open a port we still hold
    if _SERVER is not None:
        try:
            _SERVER.server_close()
        except OSError:
            pass

    argv = [sys.executable, *sys.argv]
    if "--restarted" not in argv:
        argv.append("--restarted")
    if port and "--reconnect" not in argv:
        argv += ["--reconnect", port]
    # Detached, so the successor outlives this process rather than dying with
    # the console it was started from.
    flags = {"creationflags": subprocess.DETACHED_PROCESS |
                              subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    subprocess.Popen(argv, cwd=str(HERE), close_fds=True, **flags)
    os._exit(0)


def main():
    ap = argparse.ArgumentParser(description="Browser front end for the nRF24 Analyser dongle.")
    ap.add_argument("--http", type=int, default=8724, help="http port (default 8724)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    # Both internal, both set by restart() on its successor. --reconnect is not
    # the --port flag this program deliberately does not have: it does not
    # choose a port, it restores the one that was already open.
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--reconnect", help=argparse.SUPPRESS)
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.http}/"

    # On Windows the server socket allows address reuse, so a second start would
    # bind the same port instead of failing - two servers, and the second one
    # cannot open the dongle the first is holding. Double-clicking start.cmd a
    # second time is exactly how that happens, so check first and just point the
    # browser at the instance already running. Not after a restart: the instance
    # that would answer is the one that just asked for this one.
    if not args.restarted and _already_serving(args.http):
        print(f"nRF24 Analyser is already running on {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    hub = Hub()
    session = Session(hub)
    Handler.hub = hub
    Handler.session = session

    # The predecessor closes its socket before spawning this one, but the two
    # overlap by however long that takes, so a restart waits rather than giving
    # up on the port it is meant to inherit.
    deadline = time.monotonic() + (3.0 if args.restarted else 0.0)
    while True:
        try:
            server = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)
            break
        except OSError as exc:
            if time.monotonic() >= deadline:
                print(f"cannot start on port {args.http}: {exc}")
                return
            time.sleep(0.1)

    global _SERVER
    _SERVER = server
    if args.reconnect:
        # After the socket is listening, so the browser finds the server up
        # while the dongle is still greeting.
        threading.Timer(0.1, lambda: _reconnect_quietly(session, args.reconnect)).start()
    print(f"nRF24 Analyser web ui on {url}   (Ctrl-C to stop)")
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
