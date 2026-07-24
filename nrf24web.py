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
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import nrf24_dongle as dongle
import nrf24_parsers as parsers

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
MAX_FRAMES = 5000
# Opening the port pulls DTR and resets the dongle, so the greeting takes about
# two seconds. After that we ask instead of waiting - not every adapter resets
# the board, and a dongle that was already running never greets at all. Only if
# the question goes unanswered too is the silence worth reporting.
GREETING_ASK = 2.0
GREETING_TIMEOUT = 4.5


def column_spec(parser):
    """The decoder's table columns, as the browser wants them."""
    return [{"key": key, "label": label, "width": width,
             "packet": key == parser.packet_column}
            for key, label, width in parser.columns]


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
        self._pump = None
        self._stop = threading.Event()

    # -- connection --

    def connect(self, port):
        self.disconnect()
        self.dongle = dongle.Dongle(port)
        self.dongle.open()
        self._stop.clear()
        self._pump = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump.start()
        # Not "connected" yet: the port is open, but nothing has proved there is
        # a sniffer on the other end. The greeting is what does that.
        self.state_text = "connecting…"
        self.hub.publish(self.status_event(port))
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
        self.hub.publish(self.status_event())

    def status_event(self, port=None):
        return {"type": "status", "connected": self.dongle is not None,
                "port": port, "state": self.state_text, "greeting": self.greeting}

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

    def send(self, line):
        if self.dongle is None:
            raise RuntimeError("not connected")
        self.dongle.send(line)
        self.hub.publish({"type": "line", "text": f"> {line}", "kind": "sent"})

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

    def _decode(self, stamp, delta, device_ms, pipe, data):
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
            device_ms, pipe, data = received
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
            self.frames.append((stamp, delta, device_ms, pipe, data))
            self.hub.publish(self._decode(stamp, delta, device_ms, pipe, data))
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
        elif self.path == "/api/events":
            self._events()
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            payload = self._body()
            if self.path == "/api/connect":
                self.session.connect(payload["port"])
            elif self.path == "/api/disconnect":
                self.session.disconnect()
            elif self.path == "/api/command":
                self.session.send(payload["line"])
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


def main():
    ap = argparse.ArgumentParser(description="Browser front end for the nrf24-sniffer dongle.")
    ap.add_argument("--http", type=int, default=8724, help="http port (default 8724)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser")
    args = ap.parse_args()

    hub = Hub()
    session = Session(hub)
    Handler.hub = hub
    Handler.session = session

    server = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)
    url = f"http://127.0.0.1:{args.http}/"
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
