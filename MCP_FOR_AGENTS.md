# nRF24 sniffer over MCP — for a consuming agent

You can drive an nRF24 sniffer dongle to validate your firmware against real
radio traffic. Access is through an MCP server that proxies to a running web UI;
that web UI owns the serial port, and you never touch it directly. This
indirection is deliberate — a person can keep watching in the browser while you
capture, configure and transmit through the same dongle.

## Prerequisite

The web UI must be running: `C:\Repos\tools\nrf24-sniffer\start.cmd`. If any tool
returns `cannot reach the sniffer web UI`, it is not running — ask the human to
start it. `nrf24_state()` is the cheapest way to check.

## Register the MCP server (once, in the consuming project)

```bash
claude mcp add-json nrf24 "{\"command\":\"C:/Repos/tools/nrf24-sniffer/.venv/Scripts/python.exe\",\"args\":[\"C:/Repos/tools/nrf24-sniffer/nrf24_mcp.py\"]}"
```

Or copy the `mcpServers` entry from
[`mcp.example.json`](mcp.example.json) into this project's `.mcp.json`.
Point it at a non-default host with the `NRF24_WEB_URL` env var
(default `http://127.0.0.1:8724`).

## Tools

| Tool | Purpose |
|---|---|
| `nrf24_state()` | connected? `listening`/`idle`/`scanning`? on what wiring? |
| `nrf24_configure(channel, pipe1, rate=250, crc=16, aw=5, pa="low", ack=0, dpl=1, plsize=32)` | tune the radio and start listening. Only `channel` and `pipe1` (e.g. `"42:54:48:4D:45"`) are required; the defaults match a BTHome-over-nRF24 sender |
| `nrf24_capture(seconds=10)` | listen for `seconds`, return every frame decoded plus a per-sender summary |
| `nrf24_transmit(address, payload, ack=False, repeat=1, gap_ms=0)` | send one frame — a stimulus to provoke a response. `repeat`/`gap_ms` send up to 16 copies genuinely milliseconds apart (firmware-side), like a real sender's event repeats |
| `nrf24_burst(frames, address="", ack=False)` | send a sequence of frames: each entry a payload hex string or `{"payload", "repeat", "gap_ms", "address", "pause_ms"}`. Per-entry firmware replies; ~5-10 ms serial round trip between entries |
| `nrf24_stop()` | stop receiving |

`nrf24_transmit` and `nrf24_burst` return the firmware's own reply: `sent`
counts the copies the radio confirmed on air, and a rejected command (bad hex,
unconfigured radio) raises instead of pretending success. The dongle cannot
receive its own transmissions — a capture never contains them; point a second
receiver at the channel to see them land.

`nrf24_capture` returns, per sender: `frames`, `events`, `first_id`/`last_id`,
`distinct_ids`, `missing` (skipped counter values), and `missing_uncertain`
(true when a 256-wrap of the byte counter cannot be ruled out). It returns
frames and statistics, **not** a verdict — the pass/fail criteria are yours.

## Three things that otherwise cause wrong conclusions

1. **An empty capture does not mean "broken".** The test device (a RotRemote)
   broadcasts its periodic status only when idle — every ~1 min on the current
   test firmware, hourly on production builds, and not at all on a dead
   battery. Waiting for it passively is pointless. Validate **stimulus-driven**:
   send `nrf24_transmit(...)` then capture briefly, or have a human interact
   with the device while you capture.

2. **Frames arrive duplicated and out of order.** The sender broadcasts with
   NO_ACK and repeats each event three times; NO_ACK disables the nRF24's
   hardware duplicate rejection, so every copy — the intentional repeats and any
   the receiver re-captures off a strong near-field signal — reaches the FIFO.
   You will see ~5-6 frames per event, and an earlier packet id can appear after
   a later one. **Deduplicate on `(sender, packet_id)`.** The capture summary
   already counts *events*, not raw frames, and computes `missing` over the set
   of ids (wrap-safe), so use those rather than counting frames yourself.

3. **The dongle is shared.** A human may be watching in the browser.
   `nrf24_configure` and `nrf24_transmit` change their view too. That is
   intended, but you are not alone on the device.

## Typical loop

```
nrf24_state()                                  # is the UI up, is it listening?
nrf24_configure(channel=100, pipe1="42:54:48:4D:45")
# … flash your firmware / trigger a stimulus …
nrf24_transmit("42:54:48:4D:45", "4D565202")   # optional: provoke a response
nrf24_capture(seconds=8)                        # capture, read the stats, judge for yourself
```

## What the frames mean (BTHome decoder, the default)

Each frame is `[4-byte sender id][BTHome v2 service data]`, decoded by the
reference parser `bthome-ble`. A capture frame's `cells` carries the sender, the
packet id, and the decoded measurements/events (e.g.
`Dimmer: rotate_right steps=1; Battery 57; Voltage 2.486`). A frame the reference
parser rejects is flagged with `!!` — that means no standard BTHome receiver
could read it either, which is itself a useful validation signal.
