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
# two seconds. Past that the silence means something, and saying so beats a pill
# that reads "connected" next to a port that never answers.
GREETING_TIMEOUT = 4.0


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
        # Remembered so a tab opened later still learns the current state -
        # the greeting only arrives once, at reset.
        self.greeting = None
        self.state_text = "not connected"
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
        watchdog = threading.Timer(GREETING_TIMEOUT, self._greeting_overdue, [self.dongle])
        watchdog.daemon = True
        watchdog.start()

    def _greeting_overdue(self, expected):
        if self.dongle is not expected or self.greeting is not None:
            return  # answered, or this connection is already history
        self.state_text = "no greeting"
        self.hub.publish({
            "type": "line", "kind": "warn",
            "text": f"WARN no greeting after {GREETING_TIMEOUT:.0f}s - "
                    f"wrong port, or the dongle is not running this firmware?"})
        self.hub.publish(self.status_event())

    def disconnect(self):
        self._stop.set()
        if self.dongle is not None:
            self.dongle.close()
            self.dongle = None
        self.last_stamp = None
        self.greeting = None
        self.state_text = "not connected"
        self.hub.publish(self.status_event())

    def status_event(self, port=None):
        return {"type": "status", "connected": self.dongle is not None,
                "port": port, "state": self.state_text, "greeting": self.greeting}

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

    def decoded_history(self):
        """Every retained frame, decoded with the current parser."""
        return [self._decode(stamp, delta, pipe, data)
                for stamp, delta, pipe, data in self.frames]

    def _decode(self, stamp, delta, pipe, data):
        try:
            summary = self.parser.summary(data)
            detail = self.parser.detail(data)
        except Exception as exc:
            summary, detail = f"(decoder error: {exc})", [str(exc)]
        flagged = "!!" in summary or "rejected" in summary.lower()
        return {
            "type": "frame",
            "time": stamp,
            "delta": delta,
            "pipe": pipe,
            "len": len(data),
            "summary": summary,
            "detail": detail,
            "hex": parsers.hexdump(data),
            "flagged": flagged,
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
            now = time.time()
            stamp = (time.strftime("%H:%M:%S", time.localtime(now))
                     + f".{int(now % 1 * 1000):03d}")
            delta = None if self.last_stamp is None else round((now - self.last_stamp) * 1000, 1)
            self.last_stamp = now
            pipe, data = received
            self.frames.append((stamp, delta, pipe, data))
            self.hub.publish(self._decode(stamp, delta, pipe, data))
            return

        greeting = dongle.parse_greeting(line)
        if greeting is not None:
            event = {"type": "greeting", "fields": greeting, "text": line,
                     "apiOk": greeting.get("api") == str(dongle.EXPECTED_API),
                     "expectedApi": dongle.EXPECTED_API}
            self.greeting = event
            self.state_text = greeting.get("state", "connected")
            self.hub.publish(event)
            return

        if line.startswith("OK listening"):
            self.state_text = "listening"
        elif line.startswith("OK stopped"):
            self.state_text = "idle"

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
            self._json([{"name": p.name, "label": p.label,
                         "description": p.description,
                         "unavailable": p.available()}
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
                self._json({"ok": True, "frames": self.session.decoded_history()})
                return
            elif self.path == "/api/clear":
                self.session.frames.clear()
                self.session.last_stamp = None
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
