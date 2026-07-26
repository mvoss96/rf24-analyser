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
  nrf24_transmit    send one frame (a stimulus to provoke a response)
  nrf24_stop        stop receiving

Register it in the consuming session's MCP config, e.g.:

    {"mcpServers": {"nrf24": {
        "command": "C:/Repos/tools/nrf24-sniffer/.venv/Scripts/python.exe",
        "args": ["C:/Repos/tools/nrf24-sniffer/nrf24_mcp.py"]}}}

Point it elsewhere with NRF24_WEB_URL (default http://127.0.0.1:8724).
"""

import json
import os
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
    """Send one firmware command line and confirm the server accepted it."""
    result = _request("POST", "/api/command", {"line": line}, timeout=timeout)
    if result.get("ok") is False:
        raise DongleError(result.get("error", "command rejected"))
    return result


# -- tools ------------------------------------------------------------------


@mcp.tool()
def nrf24_state() -> dict:
    """Report what the dongle is doing: connected, listening/idle/scanning, the
    active decoder, and the wiring it came up on. Call this first to check the
    web UI is up and the dongle is listening before capturing."""
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
def nrf24_transmit(address: str, payload: str, ack: bool = False) -> dict:
    """Transmit one frame - a stimulus to provoke a response you then capture.

    `address` and `payload` are hex, compact ("4254484D45") or separated
    ("42:54:48:4D:45"); the address length must match the configured aw. With
    ack=False (the default) the frame carries the NO_ACK flag, matching a
    broadcast sender. Requires a configured radio (nrf24_configure first)."""
    _command(f"tx {address} {payload} {'ack' if ack else 'noack'}")
    return {"sent": True, "note": "the firmware's tx reply is in the web UI log"}


@mcp.tool()
def nrf24_stop() -> dict:
    """Stop receiving. Leaves the configuration in place; nrf24_configure or the
    browser's Start resumes it."""
    _command("stop")
    return _request("GET", "/api/state", timeout=5)


if __name__ == "__main__":
    mcp.run()
