#!/usr/bin/env python3
"""nrf24_mcp - an MCP server that lets another agent drive the sniffer dongle.

It is a thin proxy over the running nrf24web.py: every tool is an HTTP call to
that server, which is the sole owner of the serial port. That indirection is the
whole point - a person can keep watching in the browser while an agent captures,
configures and transmits through the same dongle, because nothing here touches
the port directly. Start nrf24web.py first (start.cmd); this server is useless
without it and says so.

Tools:
  nrf24_state       what the dongle is doing right now
  nrf24_configure   tune the radio and start listening
  nrf24_capture     collect and summarise frames over a time window
  nrf24_history     read back frames already captured
  nrf24_transmit    send one frame, optionally repeated ms apart (x<n>/gap)
  nrf24_burst       send a sequence of frames, one firmware reply each
  nrf24_command     any raw firmware command (status, info, scan, ...)
  nrf24_clear       discard the retained history
  nrf24_reset       reset the dongle, emptying its FIFO
  nrf24_stop        stop receiving

Register it in the consuming session's MCP config, e.g.:

    {"mcpServers": {"nrf24": {
        "command": "C:/Repos/tools/nrf24-sniffer/.venv/Scripts/python.exe",
        "args": ["C:/Repos/tools/nrf24-sniffer/nrf24_mcp.py"]}}}

Point it elsewhere with NRF24_WEB_URL (default http://127.0.0.1:8724).
"""

import json
import os
import re
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("NRF24_WEB_URL", "http://127.0.0.1:8724").rstrip("/")
mcp = FastMCP("nrf24-sniffer")


class DongleError(RuntimeError):
    """A message an agent can act on, not a stack trace."""


def _request(method, path, body=None, timeout=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.URLError as exc:
        # The single failure that matters: the web UI is not running.
        raise DongleError(
            f"cannot reach the sniffer web UI at {BASE} ({exc.reason}). "
            f"Start it first with start.cmd (or python nrf24web.py)."
        ) from None


def _command(line, timeout=15):
    """Send one firmware command line and return the firmware's reply.

    wait=true makes the web server hold the request until the firmware answers
    with OK or ERR, so an error like "ERR bad payload byte" lands here as an
    exception instead of scrolling past unseen in the browser log. It also
    means the state the firmware reports afterwards is settled - reading
    /api/state right after this cannot race the reply anymore.
    """
    result = _request("POST", "/api/command", {"line": line, "wait": True},
                      timeout=timeout)
    if result.get("ok") is False:
        raise DongleError(result.get("error") or result.get("reply", "command rejected"))
    return result


# -- tools ------------------------------------------------------------------


@mcp.tool()
def nrf24_state() -> dict:
    """Report what the dongle is doing: connected, on which port,
    listening/idle/scanning, the active decoder, and `radio` - the dongle's own
    answer about its channel, rate, crc, address width, pa level and pipe
    addresses, with `radioAge` saying how many seconds ago it said so. Call this
    first to check the web UI is up and the dongle is listening before capturing,
    and again after configuring: `radio` is what the radio reports, not what was
    asked of it, so it is the one place a silently rejected setting shows up."""
    return _request("GET", "/api/state", timeout=5)


@mcp.tool()
def nrf24_configure(
    channel: int,
    pipe1: str,
    rate: int = 250,
    crc: int = 16,
    aw: int = 5,
    pa: str = "low",
    ack: int = 0,
    dpl: int = 1,
    plsize: int = 32,
) -> dict:
    """Tune the radio and start listening. Only `channel` and `pipe1` (the
    address to listen on, e.g. "42:54:48:4D:45") are required; the defaults match
    a BTHome-over-nRF24 sender (250 kbps, CRC16, 5-byte address, dynamic
    payloads). Reconfiguring affects anyone watching in the browser too.

    Returns the dongle state afterwards; check that state == "listening"."""
    if not _request("GET", "/api/state", timeout=5).get("connected"):
        raise DongleError("the dongle is not connected in the web UI - connect "
                          "it there first (the port is chosen in the browser).")
    parts = [f"listen ch={channel}", f"rate={rate}", f"crc={crc}", f"aw={aw}",
             f"pa={pa}", f"ack={ack}", f"dpl={dpl}"]
    if not dpl:
        parts.append(f"plsize={plsize}")
    parts.append(f"pipe1={pipe1}")
    _command(" ".join(parts))
    return _request("GET", "/api/state", timeout=5)


@mcp.tool()
def nrf24_capture(seconds: float = 10) -> dict:
    """Listen for `seconds` and return every frame received in that window,
    decoded by the active decoder, plus a per-sender summary.

    The radio must already be listening (call nrf24_configure first). The
    summary gives, per sender: frame and event counts, the packet-id range, and
    how many counter values were skipped (missing). `missing_uncertain` is true
    when the gap is large enough that a 256-wrap of the byte counter cannot be
    ruled out. An empty result means nothing transmitted in the window, not that
    the capture failed - check `listening` is true."""
    return _request("POST", "/api/capture", {"seconds": seconds},
                    timeout=float(seconds) + 10)


@mcp.tool()
def nrf24_transmit(address: str, payload: str, ack: bool = False,
                   repeat: int = 1, gap_ms: int = 0) -> dict:
    """Transmit one frame - a stimulus to provoke a response you then capture.

    `address` and `payload` are hex, compact ("4254484D45") or separated
    ("42:54:48:4D:45"); the address length must match the configured aw. With
    ack=False (the default) the frame carries the NO_ACK flag, matching a
    broadcast sender. Requires a configured radio (nrf24_configure first).

    repeat (1..16) sends that many copies back to back from the firmware,
    gap_ms (0..250) milliseconds apart - genuinely milliseconds, like a real
    sender's event repeats, with no serial round trip in between. The reply is
    the firmware's own: `sent` counts the copies the radio confirmed, and a
    malformed payload raises instead of pretending it was sent."""
    if not 1 <= repeat <= 16:
        raise DongleError("repeat must be 1..16")
    if not 0 <= gap_ms <= 250:
        raise DongleError("gap_ms must be 0..250")
    line = f"tx {address} {payload} {'ack' if ack else 'noack'}"
    if repeat > 1:
        line += f" x{repeat}"
    if gap_ms:
        line += f" gap={gap_ms}"
    result = _command(line)
    reply = result.get("reply", "")
    match = re.search(r"sent=(\d+)(?:/(\d+))?", reply)
    sent = int(match.group(1)) if match else 0
    return {"sent": sent, "of": repeat, "reply": reply}


@mcp.tool()
def nrf24_burst(frames: list, address: str = "", ack: bool = False) -> dict:
    """Transmit a sequence of frames in one go, e.g. to exercise a receiver's
    FIFO or dedup logic without a serial round trip per MCP call.

    Each entry in `frames` is either a bare payload hex string or an object
    {"payload": hex, "repeat": 1..16, "gap_ms": 0..250, "address": override,
    "pause_ms": host-side pause after the entry}. `repeat` copies of one entry
    go out milliseconds apart (firmware-side); between entries sits the serial
    round trip (~5-10 ms). `address` is the default for entries without their
    own. Returns per-entry results with the firmware's reply and sent count;
    an ERR on one entry does not stop the rest."""
    body = {"address": address, "frames": frames, "ack": ack}
    total = sum((int(f.get("repeat", 1)) if isinstance(f, dict) else 1) *
                (int(f.get("gap_ms", 0)) if isinstance(f, dict) else 0) +
                (int(f.get("pause_ms", 0)) if isinstance(f, dict) else 0)
                for f in frames) if isinstance(frames, list) else 0
    result = _request("POST", "/api/burst", body,
                      timeout=30 + len(frames or []) * 5 + total / 1000.0)
    if result.get("ok") is False and "error" in result:
        raise DongleError(result["error"])
    return result


@mcp.tool()
def nrf24_stop() -> dict:
    """Stop receiving. Leaves the configuration in place; nrf24_configure or the
    browser's Start resumes it."""
    _command("stop")
    return _request("GET", "/api/state", timeout=5)


@mcp.tool()
def nrf24_command(line: str) -> dict:
    """Send one raw firmware command line and return the firmware's own reply.

    The escape hatch: everything the dongle can do is reachable here without a
    dedicated tool. The ones worth knowing:

      status            counters - `rx=` frames received, `fifofull=` overflows.
                        The answer to "is the radio hearing anything at all",
                        which an empty capture cannot tell you.
      info              the active configuration, pipe by pipe
      scan [passes]     energy per channel - which channels are busy. Needs no
                        radio configuration, so it works before you pick one.
      repeats 0|1       0 suppresses identical back-to-back frames
      help              the firmware's own command list

    Replies are "OK ..." or "ERR ..."; an ERR raises. Anything the firmware
    prints asynchronously (received frames, warnings) is not the reply - read
    those with nrf24_capture or nrf24_history."""
    return {"reply": _command(line).get("reply", "")}


@mcp.tool()
def nrf24_history(limit: int = 50) -> dict:
    """Return frames the sniffer already captured, newest last.

    nrf24_capture only sees the window it was asked for, so a frame that
    arrived a minute ago was out of reach. This reads the retained history
    instead - which is what lets you compare a frame against earlier ones and
    notice that it is byte for byte a repeat of something older.

    Each frame carries `raw` (compact hex of the whole payload), the decoded
    `cells`, `source` and `packetId`. Compare `raw` when the decoded view looks
    ambiguous: two frames can decode to the same measurements and still differ
    in the packet id, which is exactly the case worth catching."""
    frames = _request("GET", f"/api/frames?limit={max(0, int(limit))}", timeout=15)
    return frames


@mcp.tool()
def nrf24_clear() -> dict:
    """Discard the retained frame history, giving a measurement a clean zero.

    Affects the browser view too - a person watching loses the same rows."""
    _request("POST", "/api/clear", {}, timeout=5)
    return {"cleared": True}


@mcp.tool()
def nrf24_reset() -> dict:
    """Reopen the serial port, which pulls DTR and resets the dongle.

    Empties the radio's RX FIFO and the firmware's state. Use it to tell "this
    frame came off the air just now" from "this frame was still sitting in the
    sniffer": after a reset the dongle cannot know anything that happened
    before it.

    The radio configuration does NOT survive - call nrf24_configure again
    before capturing, or the dongle sits there unconfigured and silent."""
    result = _request("POST", "/api/reconnect", {}, timeout=20)
    return {"reset": True, "port": result.get("port"),
            "note": "radio is unconfigured now - call nrf24_configure before capturing"}


if __name__ == "__main__":
    mcp.run()
