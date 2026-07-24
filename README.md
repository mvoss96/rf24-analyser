# nrf24-sniffer

A debug / sniffer tool for raw nRF24L01 traffic, built to validate and debug the
**BTHome-over-nRF24** protocol spoken by
[`RotRemote_BTHome`](../../active/RotRemote_BTHome). Two parts:

1. **PlatformIO firmware** (`platformio.ini`, `src/`, `include/`) for an
   ATmega328P + CH340 + nRF24L01 USB dongle.
2. **[`nrf24web.py`](#web-ui)** — a browser UI: every setting, a live frame table
   with a detail pane, and a switchable decoder.
3. **`nrf24term.py`** — a serial terminal / REPL for the same protocol.

Both front ends share the serial client (`nrf24_dongle.py`) and the decoder
registry (`nrf24_parsers.py`).

## Design: nothing is assumed

The firmware has **no built-in defaults** — neither for the board wiring nor for
the radio parameters. The host must send `hwset` and then `listen` before
anything happens:

```
nohw ──hwset──► unconfigured ──listen k=v...──► listening ◄──stop──► idle
```

(A dongle with a stored wiring boots straight into `unconfigured` — see
[Greeting](#greeting). The radio parameters are never remembered.)

Two reasons. First, a sniffer that quietly comes up on some compiled-in channel
invites the worst kind of error: concluding *"nothing is being transmitted"* when
in truth the wrong question was asked. Every radio parameter in a session log is
therefore one the host explicitly chose. Second, keeping the board pin map out of
the firmware means one image works with any nRF24 wiring — the protocol- and
board-specific knowledge lives on the host, where it belongs.

## Firmware (PlatformIO)

The dongle is an ATmega328P at 16 MHz with a CH340 bridge and a serial
bootloader, so it flashes over USB like an Arduino Nano. CH340 clones ship with
one of two bootloaders — one environment each:

| Environment | Board              | Bootloader / upload baud |
|-------------|--------------------|--------------------------|
| `nano`      | `nanoatmega328new` | new (115200) — default   |
| `nano_old`  | `nanoatmega328`    | old (57600) — fallback   |

Port is set to **COM18** in `platformio.ini` (`pio device list` to find yours).

```bash
pio run -e nano -t upload
```

If that ends in an avrdude sync timeout, use `-e nano_old`. Only dependency is
`nrf24/RF24` (TMRh20), pulled automatically. Build size: **flash ~12.8 KB (42%)**,
**RAM ~795 B (39%)**.

## Serial protocol

**500000 baud**, one command per line. **CR, LF or CRLF** are all accepted, so any
terminal works unconfigured. Every command is answered with `OK ...` or `ERR ...`.

> Why 500000: the serial link, not the radio, is the bottleneck when a sender
> repeats each event a few ms apart. At 115200 one frame line took 5.6 ms, so a
> 3-repeat burst needed ~17 ms to print while arriving in ~10 ms — the 3-deep RX
> FIFO overflowed and frames were dropped silently. 500000 is an exact divisor at
> 16 MHz (0% error), and with compact hex output plus a 256-byte TX buffer one
> line costs ~1 ms.

### Greeting

```
NRF24SNIFFER fw=2.3.0 api=2 state=unconfigured hw=connected ce=9 csn=10 irq=2 led_rx=8 led_tx=A1
```

`fw` is the firmware version, `api` the command-protocol version — the host can
check it and refuse to talk to an incompatible build. When a wiring is loaded the
pins are spelled out, because a stored-but-wrong pin is otherwise invisible.

| `hw=` | Meaning |
|---|---|
| `none` | nothing stored; `hwset` required |
| `connected` | wiring restored, chip answers over SPI **and** [CE keys it](#the-ce-self-test) |
| `failed` | a wiring is stored but did not come up |

Provenance is not reported because it carries no information: at boot a wiring
can only have come from EEPROM. Likewise `connected` covers both checks rather
than reporting them separately. When a wiring fails, the reason is stated in a
`WARN` line ahead of the greeting:

```
WARN stored wiring: ce pin does not key the radio
NRF24SNIFFER fw=2.3.0 api=2 state=nohw hw=failed ce=8 csn=10 irq=2 led_rx=8 led_tx=A1
```

### Commands

| Command | Meaning |
|---|---|
| `hwset ce=<pin> csn=<pin> [irq=<pin\|none>] [led_rx=<pin\|none>] [led_tx=<pin\|none>]` | define the wiring, bring the radio up, store it |
| `hwclear` | forget the stored wiring (effective on reset) |
| `listen <k=v>...` | apply a complete radio config and start receiving |
| `listen` | resume with the retained config |
| `stop` | stop receiving, keep the config |
| `info` | state, wiring and configuration |
| `scan [passes]` | energy scan across all channels (default 64) |
| `repeats <0\|1>` | `0` suppresses identical back-to-back frames |
| `tx <addr> <hex...> [ack\|noack]` | transmit a payload (default `noack`) |
| `help` | usage summary |

**`hwset`** — `ce` and `csn` are mandatory, the rest default to `none`. Pins are
plain numbers or `A0`–`A7`. Re-issuing it is allowed while not listening; it
discards the radio configuration.

Assigning one pin two roles is rejected (`ERR pin 8 assigned twice`) — that is
always a wiring mistake, and an LED write on the CE line is a miserable thing to
debug.

Every successful `hwset` is **stored in EEPROM** and restored on the next boot,
so a dongle keeps working without the host restating its wiring. This is the one
piece of remembered state, and it never becomes hidden state: the greeting spells
the pins out on every connect. `hwclear` returns the dongle to a virgin state.

### The CE self-test

`chip=connected` comes from `isChipConnected()`, which exercises **SPI only** —
CSN, MOSI, MISO, SCK. **CE is never involved**: it only switches the radio
between standby and active TX/RX. A wrong CE pin would therefore pass unnoticed
and then silently receive nothing.

The firmware guards against that automatically. Both at boot (when a wiring is
restored from EEPROM) and on every `hwset`, it transmits one packet and checks
that the radio actually keyed up:

```
> hwset ce=7 csn=10 irq=2 led_rx=8 led_tx=A1
ERR ce pin does not key the radio (spi ok) - check the ce wiring
```

A failed test is fatal: the wiring is **not** stored and the state drops back to
`nohw`, so a dongle can never sit in a configured-looking but dead state.

The test emits **one byte at minimum power (−18 dBm) on channel 2** (2402 MHz,
well inside the ISM band — the nRF24 tunes up to channel 125 / 2525 MHz, which is
outside it). These are transient settings, not defaults: the radio is left
`unconfigured` and cannot be operated on them.

Transmits are bounded by a 50 ms timeout for the same reason — `RF24::write()`
spins forever waiting for a `TX_DS`/`MAX_RT` that never arrives when CE is dead,
which used to hang the firmware until reset. `tx ... sent=0` remains a manual
check of the same property.

On the ATmega328P only D2/D3 can raise interrupts — any other `irq` pin is
accepted but degrades to polling with a warning:

```
WARN irq pin 7 is not interrupt-capable, falling back to polling
```

**`listen`** — mandatory keys are `ch`, `rate`, `crc`, `aw`, `pa`, `ack`, `dpl`
and at least one `pipeN`; `plsize` is required only when `dpl=0`. Missing keys are
named back:

```
> listen ch=100 rate=250
ERR missing: crc aw pa ack dpl pipeN
```

Addresses and payloads accept both the compact form the RX output uses
(`4254484D45`) and separated forms (`42:54:48:4D:45`), so a captured payload can
be pasted straight into a `tx`. The **leftmost** byte is address byte 0, matching
the sender's address array — `pipe1=42:54:48:4D:45` is ASCII `"BTHME"`.

### Example session

```
NRF24SNIFFER fw=2.3.0 api=2 state=nohw hw=none
> hwset ce=9 csn=10 irq=2 led_rx=8 led_tx=A1
OK hw connected saved
> listen ch=100 rate=250 crc=16 aw=5 pa=low ack=0 dpl=1 pipe1=42:54:48:4D:45
OK listening
RX p1 len=16 4D565202D2FC44004501350C8B093A01
```

### RX output

```
RX p1 len=16 4D565202D2FC44004501350C8B093A01
```

`p<pipe>` is the pipe number, `len=<n>` the payload length, then `2*n` hex chars.
A sender that repeats each event emits several identical frames; `repeats 0`
prints only the first of a run (identical payload within 500 ms).

If the RX FIFO was already full, frames were dropped by the chip:

```
WARN fifo-full
```

That line is the discriminator when packets go missing: **with** it the host could
not keep up, **without** it the loss happened on the air.

> nRF24 hardware note: reading pipes 2–5 share address bytes 1–4 with pipe 1 and
> differ only in byte 0.

## Operating it from a plain terminal

No Python needed. 500000 baud, 8N1, any line ending. The firmware does **not echo**
input, so enable local echo in your terminal.

```bash
pio device monitor -e nano
```

`monitor_echo` and `monitor_eol` are set in `platformio.ini`, so this just works.
In PuTTY choose *Serial*, speed `500000`, and set *Terminal → Local echo* to
*Force on*. Connecting toggles DTR and resets the ATmega, so you always start from
`state=nohw`.

## Python terminal

```bash
pip install -r requirements.txt
```

```bash
python nrf24term.py COM18 --pretty
```

Dependencies are `pyserial` and `bthome-ble`. The latter is not lightweight — it
pulls in bleak, cryptography and platform Bluetooth bindings, around 28 packages —
because it is the parser Home Assistant itself uses. That is the price of decoding
frames exactly as a real receiver would rather than approximating it; a virtualenv
is recommended.

Local commands start with `:`:

| Local command | Effect |
|---|---|
| `:pretty on\|off` | toggle BTHome pretty-printing |
| `:preset bthome` | send `hwset` for this dongle + the BTHome `listen` line |
| `:scan [passes]` | channel scan, rendered as activity bars |
| `:log <file>` / `:log off` | tee dongle output to a file |
| `:help`, `:quit` | help / exit |

Everything else is forwarded verbatim. The tool checks the greeting's `api=`
field against the version it speaks and warns on a mismatch.

### Pretty-print format

```
-- RX pipe 1  (15 bytes)
  sender    : 4D:56:52:02  "MVR."
  bthome    : v2 trigger-based
  packet id : 5
  battery   : 87 %
  voltage   : 2.960 V
  button 1  : press
  dimmer 1  : rotate right (3 steps)
```

Frames are `[4-byte sender id][BTHome v2 service data]`; the service data starts
with the UUID `D2 FC` and a device-info byte. Only that envelope is decoded here.

**Object parsing is delegated entirely to [`bthome-ble`](https://pypi.org/project/bthome-ble/)**,
the reference parser Home Assistant uses. Nothing about BTHome measurements is
reimplemented, for two reasons: a hand-maintained length table drifts from the
spec (this tool already shipped one that mis-decoded dimmer events), and using
the reference implementation makes the sniffer a **conformance check** — a frame
`bthome-ble` cannot read is a frame no standard receiver can read either.

A fresh parser instance is used per frame. The library deduplicates by packet id,
which would otherwise hide exactly the retransmissions a sniffer exists to show.

When the library rejects a payload outright, that is reported instead of a
prettified guess, along with the library's own explanation:

```
  !! REJECTED by the reference parser (bthome-ble)
  objects   : 00 7B 01 34 3C 00 3C 02 01
  reason    : BTHome device is not sending object ids in numerical order ...
```

Warnings are surfaced even when parsing partly succeeds — which is what the
current `bthome-cpp` dimmer deviation looks like:

> **Known deviation.** The BTHome spec encodes a dimmer `None` as `3C 00 00`
> (steps byte present), and `bthome-ble` assumes a fixed 2-byte value.
> `bthome-cpp` omits the steps byte, emitting `3C 00`. The sensors preceding the
> dimmer still decode, but **the dimmer event itself is silently lost** and the
> parser warns about object ids being out of order. Tracked for a fix in
> `bthome-cpp`.

## Measuring packet loss

The BTHome packet id is a free-running per-event counter, so gaps in it measure
loss directly. Log a session (`:log run.txt`) and count distinct ids and gaps:

- `WARN fifo-full` present → the host is the bottleneck; raise the baud rate or
  use `repeats 0`.
- No warnings, but whole events missing → loss on the air. If complete events
  vanish far more often than independent per-packet loss would predict, the
  interference is **bursty** and all repeats fall inside one outage. Spreading the
  sender's repeats further apart in time helps more than sending more of them.

## Web UI

```bash
python nrf24web.py --port COM18
```

Opens a browser at `http://127.0.0.1:8724/`. Python owns the serial port and does
the decoding; the browser is presentation only.

That split is deliberate. `bthome-ble` is the reference parser and it is a Python
library, so letting the browser talk to the dongle directly (WebSerial) would
mean reimplementing the BTHome object layer in JavaScript — the exact kind of
second implementation that once made this tool silently swallow dimmer events.
It also keeps the UI working in any browser, not just Chrome.

**Standard library only** — `http.server` for the pages and JSON endpoints,
Server-Sent Events for the live stream. No web framework, no websocket package.

The setup strip collapses to a one-line summary of the active configuration, so
after `Start` (which sends `hwset` and `listen` in one go) the frame table gets
the window. Frames arrive with **millisecond timestamps and a Δ column** — the
three repeats of one event sit ~4 ms apart, which per-second resolution hides.
Selecting a row shows **decoded fields and the hex dump side by side**; frames the
decoder objected to are drawn in red. The log and the free-text command line share
their own tab. A tab opened later is brought up to date: the server replays the
greeting, the current state and the retained frames.

| Endpoint | Purpose |
|---|---|
| `GET /api/events` | SSE stream of frames, log lines, greeting and status |
| `GET /api/ports`, `/api/parsers` | what is available |
| `POST /api/connect`, `/api/disconnect`, `/api/command` | control |
| `POST /api/parser` | switch decoder, returns the history re-decoded |

### Adding a decoder

Decoders live in [`nrf24_parsers.py`](nrf24_parsers.py). Subclass `Parser`,
implement `summary()` (one table row) and `detail()` (the field list), and apply
`@register`:

```python
@register
class MyParser(Parser):
    name = "myproto"
    label = "My protocol"

    def summary(self, data): ...
    def detail(self, data): ...
```

Neither the web UI nor the terminal needs to change — the dropdown is built from
the registry. The legacy nRF24 protocols in
`libs/esphome-rf24-remote/PROTOCOL.md` are the obvious next candidates.

## Layout

```
nrf24-sniffer/
  platformio.ini              build environments (nano / nano_old)
  include/RadioController.h   radio wrapper, HwConfig + RadioConfig
  include/CommandParser.h     serial command parser
  include/HwStore.h           EEPROM persistence for the wiring
  src/RadioController.cpp     hardware setup, RX drain, tx, scan, info
  src/CommandParser.cpp       line protocol dispatch
  src/HwStore.cpp             magic + checksum guarded EEPROM record
  src/main.cpp                greeting, wiring restore, super-loop
  nrf24term.py                serial terminal (REPL)
  nrf24web.py                 web UI backend (stdlib http.server + SSE)
  web/                        index.html, app.css, app.js
  nrf24_dongle.py             serial protocol client, shared by both
  nrf24_parsers.py            decoder registry: raw, bthome, ...
  requirements.txt            pyserial, bthome-ble
```
