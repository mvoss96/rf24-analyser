# nRF24 Analyser

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
`nrf24/RF24` (TMRh20) 1.6.1, pulled automatically. Build size at 3.6.0:
**flash 17.9 KB (58%)**, **RAM 1198 B (58%)**.

## Serial protocol

**500000 baud**, one command per line. **CR, LF or CRLF** are all accepted, so any
terminal works unconfigured. Every command is answered with `OK ...` or `ERR ...`.

> Why 500000: the serial link, not the radio, is the bottleneck when a sender
> repeats each event a few ms apart. At 115200 one frame line took 5.6 ms, so a
> 3-repeat burst needed ~17 ms to print while arriving in ~10 ms — the 3-deep RX
> FIFO overflowed and frames were dropped silently. 500000 is an exact divisor at
> 16 MHz (0% error), and with compact hex output plus a 256-byte TX buffer one
> line costs ~1 ms.

### Greeting and `status`

```
NRF24ANALYSER fw=3.6.0 api=5 state=unconfigured hw=connected ce=9 csn=10 irq=2 led_rx=8 led_tx=A1 t=133 rx=0 fifofull=0
```

Printed once at boot, and identically by **`status`** at any time. The greeting
alone was not enough: it arrives only on reset, so a host attaching to a dongle
that is already running — or through an adapter that does not pull DTR — had no
way to learn what it was talking to except to wait and give up.

`fw` is the firmware version, `api` the command-protocol version — the host can
check it and refuse to talk to an incompatible build. When a wiring is loaded the
pins are spelled out, because a stored-but-wrong pin is otherwise invisible.
`t` is the firmware's uptime in ms, `rx` and `fifofull` the counters described
under [RX output](#rx-output).

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
NRF24ANALYSER fw=3.6.0 api=5 state=nohw hw=failed ce=8 csn=10 irq=2 led_rx=8 led_tx=A1
```

### Commands

| Command | Meaning |
|---|---|
| `hwset ce=<pin> csn=<pin> [irq=<pin\|none>] [led_rx=<pin\|none>] [led_tx=<pin\|none>]` | define the wiring, bring the radio up, store it — [answers with the wiring it adopted](#acknowledgements-say-what-they-left-behind) |
| `hwclear` | forget the stored wiring (effective on reset) |
| `listen <k=v>...` | apply a complete radio config and start receiving — [answers with the configuration read back off the chip](#acknowledgements-say-what-they-left-behind) |
| `listen` | resume with the retained config, and say what that was |
| `stop` | stop receiving, keep the config |
| `status` | the greeting line again, at any time |
| `info` | state, wiring and configuration, with `src=` saying whether it was read off the chip |
| `scan [passes]` | one energy scan across all channels (default 64) |
| `scan live [passes]` | keep scanning, one report per N sweeps (default 8) |
| `scan off` | stop a live scan and resume whatever was running |
| `repeats <0\|1>` | `0` suppresses identical back-to-back frames |
| `tx <addr> <hex...> [ack\|noack] [x<n>] [gap=<ms>]` | transmit a payload (default `noack`), optionally `n` copies `gap` ms apart |
| `rxmode <0..4>` | how a payload is taken out of the RX FIFO — diagnosis only, see below |
| `rxdbg <0\|1>` | one `DBG` line per drain pass with the FIFO registers |
| `regs` | dump the chip's registers by name |
| `reg <addr> [value]` | read or write one register, bypassing the configuration |
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

### Duplicate frames, and the `rxmode` switch

These dongles hand out every payload **shorter than 32 bytes twice**, the second
copy carrying an earlier payload. `rxmode` exists to measure that rather than
argue about it; `2` is the default and the only setting anyone should run.

| Mode | Read strategy | Result against a real sender |
|---|---|---|
| `0` | read the width `R_RX_PL_WID` reports | duplicates, plus misalignment: `len=3` frames, payloads shifted or stitched together |
| `1` | read the whole 32-byte slot | one stale frame per burst |
| `2` | read the whole slot, then `FLUSH_RX` | **clean**, verified over a mix of 8, 16 and 32-byte payloads |
| `3` | never ask for the width, report 32 bytes | one stale frame per burst |
| `4` | ask for the width after reading the payload | one stale frame per burst |

> **Not settled — read this first.** Two things to hold on to.
>
> **Dongle-to-dongle links die on channels 99-101, and only there.** Twelve
> frames, 15 ms apart, per channel:
>
> | ch | 96 | 97 | 98 | **99** | **100** | **101** | 102 | 103 | 104 |
> |---|---|---|---|---|---|---|---|---|---|
> | intact | 12/12 | 12/12 | 12/12 | **1/12** | **0/12** | **0/12** | 11/12 | 12/12 | 12/12 |
>
> It is not the channel as such: the RotRemote reaches the same dongle on channel
> 100 perfectly well, and a dongle transmitting on channel 100 reaches an ESP32
> receiver perfectly well. It is the two dongles together - both directions,
> either address - and maximum transmit power does not recover it (channel 99
> does recover that way, channel 100 does not). The carrier detector, sampled per
> channel at 250 kbps, reports nothing above its -64 dBm threshold anywhere, so
> whatever this is, it is weak. Cause unknown. Consequence: **measurements between
> two dongles must not be taken on 99-101**.
>
> Everything below was measured on channel 100, i.e. across that broken link.
> Repeated on **channel 90**, with
> nothing but two dongles on the air, the default configuration produced **no
> stale frames at all**: 18 payloads, 18 correct. Same firmware,
> same read strategy, same 16-byte payloads. So the effect needs something else
> on the channel, and the "made up locally" conclusion below does not survive
> that. A device answering on the same address is the obvious suspect: a receiver
> whose `EN_AA` is set answers frames flagged NO_ACK on these chips (an
> acknowledgement was captured as a 1-byte frame), and an acknowledgement
> carrying a payload would arrive as exactly this - a complete, unshifted older
> payload, queued right behind the real frame, which is also why flushing hides
> it. Deciding it needs the lamps powered down, which has not been done.

**And the reason a sniffer suffers from it at all**: dynamic payload length is
gated on auto-acknowledge per pipe, which a sniffer cannot have - answering the
traffic it is supposed to observe is the one thing it must not do. Demonstrated on
an ESP32 receiving the same frames, minutes apart on one device: with auto-ack
enabled on its pipe, every click produced only its own copies; with `EN_AA=0` the
same device started reporting stale payloads immediately, including a test payload
from minutes earlier. So this dongle's configuration is permanently the one the
chip mishandles, and the flush is how a passive receiver lives with it.

What the measurements showed:

- Two dongles hearing the same single click sometimes report *different* stale
  payloads for it. That was read as proof the frame was invented locally; with
  two lamps able to answer, two different answers explain it just as well.
  Three senders (a RotRemote, the other dongle, and `tx` bursts) all provoke it.
- It is **not the read width**, not `R_RX_PL_WID` (modes 3 and 4), not `RX_PW_Pn`,
  and not the dynamic-payload configuration — enabling auto-ack on the open pipe,
  which is what the datasheet asks for, changes nothing.
- A payload that **fills** the 32-byte slot comes out exactly once. A shorter one
  leaves the FIFO a payload out of step, and the next arrival is announced twice.
- Stale bytes live in the **chip's FIFO RAM** and outlive an ATmega reset, a
  firmware upload and any number of `FLUSH_RX` calls — flushing resets pointers,
  it does not clear RAM. A frame from ten minutes ago can surface at any time, so
  a duplicate hunt has to start from a known state or it measures history.
- The flush after **every single payload** is the only measure that works, and it
  is close to free. Measured over four `x8 gap=0` bursts each: mode `2` delivers
  4 of 8 copies every time, mode `1` without any flush delivers 4–5 (and pays for
  it in phantoms). So a back-to-back burst loses copies **in the chip**, not in
  the flush — about one copy in eight is down to flushing. From `gap=5` ms upward
  mode `2` delivers every copy.
- Collecting the whole FIFO before flushing once, instead of flushing per
  payload, was built and measured: **no gain at all**, 4 of 8 like mode `2`. The
  copies do not queue up, because they arrive while the previous frame is still
  being printed, and by then the flush has already happened. Not worth a second
  code path, so it is not in the firmware — recorded here so it is not
  re-invented.

Reproducing it takes no user and no remote: point one dongle at another, set the
listener to `rxmode 1`, and send single 16-byte payloads a second apart. Every
one of them is reported twice, the second time as the payload before it.

An ESP32 with its own driver, listening to the same frames, reports each of them
exactly once — which is what makes this a property of these dongles rather than
of the traffic.

#### Where it probably comes from

The modules may be **Si24R1** rather than genuine nRF24L01+ — a clone that is
routinely sold under the Nordic part number. The one software test on offer for
this (writing bit 0 of `RF_SETUP`, which genuine silicon is said to ignore;
[nRF24/RF24#603](https://github.com/nRF24/RF24/issues/603)) calls **every** module
here a clone, including the ESP32's — and that one never duplicates a frame. So
the test does not discriminate, and the chip identity is still open; only the
marking or a current measurement will settle it. Its known defect fits, though: it
"got the ACK bit inverted (following an error in the datasheet), so it's
incompatible with the real nRF24L01+ (and good clones) in ESB mode"
([MySensors forum](https://forum.mysensors.org/topic/1153/we-are-mostly-using-fake-nrf24l01-s-but-worse-fakes-are-emerging)),
and the bit in question sits in the packet control field right next to the
payload-length field that dynamic payloads depend on. Libraries carry
accommodations specifically for it — CircuitPython's `allow_ask_no_ack` exists
"only for the Si24R1 chinese clone". This has **not** been confirmed against the
chip marking here; it is the best available explanation, not a verified fact.

Everything the chip's configuration space offers was tried against it, with the
listener on `rxmode 1` so any stale frame shows. None of it is a fix:

| Change | Result |
|---|---|
| auto-ack on the open pipe (`EN_AA`), `DYNPD` narrowed to it | stale frame unchanged |
| `EN_ACK_PAY` added, i.e. Nordic's own recipe for dynamic payloads without ack (`DYNPD` + `EN_DPL` + `EN_ACK_PAY` + `EN_DYN_ACK`, [DevZone](https://devzone.nordicsemi.com/f/nordic-q-a/1575/nrf24l01-dynamic-payload-configuration-without-ack)) | stale frame unchanged |
| `EN_DPL` alone, without `EN_DYN_ACK` | stale frame unchanged |
| `RX_PW_Pn` set to the true payload length | stale frame unchanged |
| sender transmits **without** the NO_ACK bit | stale frames gone — but half the frames go missing, and the rest arrive with flipped bytes |
| same, plus auto-ack on the receiving pipe | stale frames gone — link acknowledges, yet payload bytes still arrive corrupted |
| receiver without dynamic payloads (`dpl=0 plsize=16`) | nothing is received at all: dynamic payloads have to match on both sides |

The two variants that do stop the stale frames break reception instead, and both
need the *sender* changed — which is not on offer when the sender is somebody's
remote control. That leaves the flush.

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

**`scan`** — needs a wiring but deliberately **not** a radio configuration:
which channels are busy is what you want to know *before* choosing one. The
sweep retunes the radio across the band, so receiving is impossible while it
runs; `scan live` resumes it afterwards if it was running. One sweep is about
25 ms and the firmware does one per loop, so commands — `scan off` above all —
are still answered in between.

The sweep runs at **2 Mbps regardless of the configured data rate**, and puts
the configured rate back afterwards. The RPD fires on carriers above about
−64 dBm *within the receiver bandwidth*, and that bandwidth follows the data
rate. Measured on one band on one evening:

| Configured rate | Channels with hits per sweep |
|---|---|
| 250 kbps | 0, 0, 0, 0, 0 |
| 1 Mbps | 12, 22, 9, 16, 19 |
| 2 Mbps | 29, 20, 26, 14, 24 |

A scan inheriting a 250 kbps configuration reports an empty band whatever is on
the air — which is how this was found: the live scan looked broken. So the scan
measures **the band**; it does not claim to measure what a 250 kbps receiver
will actually suffer from, which would be a narrower and more forgiving picture.

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

**Pipes 0 and 1** take a full address of `aw` bytes. **Pipes 2–5 take exactly one
byte**, because that is all the hardware gives them: they share address bytes
1–4 with pipe 1 and differ only in byte 0. Demanding a full address there would
have the host inventing four bytes the chip then ignores.

```
> listen ... pipe1=42:54:48:4D:45 pipe2=4D:54:48:4D:45
ERR pipe 2 takes 1 byte - pipes 2-5 share the rest with pipe 1
> listen ... pipe1=42:54:48:4D:45 pipe2=4D
OK listening
```

`info` prints what those pipes actually listen on, joined with pipe 1's bytes:
`pipe2=4D:54:48:4D:45`.

**`tx`** — `x<n>` (1–16) transmits the same payload `n` times back to back,
`gap=<ms>` (0–250) milliseconds apart; with `gap=0` the copies are separated
only by the air time. That emulates a real broadcast sender's event repeats and
is the only way to get genuinely milliseconds-apart frames — driving single
`tx` commands over serial puts a round trip between every copy. The radio is
reconfigured for RX once after the whole burst, not between copies. A burst
replies `OK tx sent=<k>/<n>`; the single-frame reply keeps its historic
`sent=<0|1>` shape.

### Acknowledgements say what they left behind

`listen` and `hwset` do not answer with a bare `OK`. They complete it with the
resulting state, as `key=value` tokens in the same grammar `info` uses — so a
host parses one thing, and what the firmware *did* is in the line reporting that
it succeeded:

```
> hwset ce=9 csn=10 irq=5 led_rx=8 led_tx=A1
WARN irq pin 5 is not interrupt-capable, falling back to polling
OK hw connected saved state=unconfigured ce=9 csn=10 irq=none led_rx=8 led_tx=A1
```

Two things a caller would otherwise have to know by heart are simply stated
there: the irq pin it asked for was not taken, and `hwset` discarded the radio
configuration. A bare `OK` made both invisible, and a host that reconstructed
them was keeping a second copy of the firmware's rules — the copy that is right
until someone changes the firmware.

`listen` answers the same way, including the bare `listen` that resumes with the
retained configuration, which is exactly where the caller does not know what it
resumed with:

```
> listen
OK listening state=listening ce=9 csn=10 irq=2 led_rx=8 led_tx=A1 channel=90 rate=250kbps crc=16 aw=5 pa=low ack=0 dpl=0 plsize=32 pipe1=43:54:48:4D:45 src=chip
```

**`src=`** says where those values came from. `chip` means they were read back
out of the chip's registers, which is the only account that survives a value the
chip did not take. `firmware` means they are what the firmware holds, and it is
what you get whenever the radio is not listening — because there the registers
describe the library's plumbing rather than the configuration. Measured, on an
idle dongle configured for pipe 1 only:

```
listening  EN_RXADDR=0x02        (pipe 1)
idle       EN_RXADDR=0x03        (pipe 0 too), RX_ADDR_P0 = the last TX address
```

`RF24::stopListening()` writes the TX address into `RX_ADDR_P0` and force-enables
pipe 0, and a scan retunes channel and data rate across the band. Read
unconditionally, `info` would report a pipe 0 nobody configured — a
plausible-looking wrong value, which is worse than an obviously missing one.

This is additive: the tokens come after the `OK`, so a host that checks for `OK`
and stops reading sees the old behaviour. That is why `api` stayed at 4 and a
dongle still on older firmware works with a newer host — it answers `OK
listening`, and the host asks `info` as it always did.

### Example session

```
NRF24ANALYSER fw=3.6.0 api=5 state=nohw hw=none t=91 rx=0 fifofull=0
> hwset ce=9 csn=10 irq=2 led_rx=8 led_tx=A1
OK hw connected saved state=unconfigured ce=9 csn=10 irq=2 led_rx=8 led_tx=A1
> listen ch=100 rate=250 crc=16 aw=5 pa=low ack=0 dpl=1 pipe1=42:54:48:4D:45
OK listening state=listening ce=9 csn=10 irq=2 led_rx=8 led_tx=A1 channel=100 rate=250kbps crc=16 aw=5 pa=low ack=0 dpl=1 plsize=32 pipe1=42:54:48:4D:45 src=chip
RX t=43230 p1 len=16 4D565202D2FC44004501350C8B093A01
```

### RX output

```
RX t=43230 p1 len=16 4D565202D2FC44004501350C8B093A01
```

`t=<ms>` is the firmware's own `millis()`, taken as the frame leaves the RX
FIFO; `p<pipe>` is the pipe number, `len=<n>` the payload length, then `2*n` hex
chars. A sender that repeats each event emits several identical frames;
`repeats 0` prints only the first of a run (identical payload within 500 ms).

> Why the firmware timestamps: host arrival times cannot resolve the gap between
> a sender's repeats. Measured on the host the same three repeats came out 0.5
> and 0.3 ms apart; on the dongle's clock they are **5 and 6 ms**. The host was
> timing how fast it drained three lines the OS had already buffered, not the
> air. Anything that reasons about repeat spacing or burst timing has to use
> `t=`.

If the RX FIFO was already full, frames were dropped by the chip:

```
WARN fifo-full n=3
```

That line is the discriminator when packets go missing: **with** it the host could
not keep up, **without** it the loss happened on the air. `n` is the running count
for the current capture, so a host that missed earlier lines still sees the total;
`status` reports it as `fifofull=` alongside `rx=`, the number of frames printed.
Both reset on every `listen`, because they describe one capture.

The chip has no lost-frame counter, so `fifofull` is evidence, not a tally: a
full FIFO means at least one frame was at risk, not that exactly one was lost.

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
-- RX pipe 1  (15 bytes)  t=43230ms
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
  use `repeats 0`. `status` gives the running total as `fifofull=` without having
  to find the warnings in the log.
- No warnings, but whole events missing → loss on the air. If complete events
  vanish far more often than independent per-packet loss would predict, the
  interference is **bursty** and all repeats fall inside one outage. Spreading the
  sender's repeats further apart in time helps more than sending more of them.

Timing arguments must use the `t=` stamps, not host arrival times — the two
disagree by an order of magnitude on exactly the interval that matters here (see
[RX output](#rx-output)). "The repeats arrive together, so one outage swallows
all three" is a claim about the air, and only the dongle's clock measures it.

## Web UI

```bash
python nrf24web.py
```

Opens a browser at `http://127.0.0.1:8724/`. Pick the port there — the UI
preselects the one that worked last time, so there is no --port flag duplicating
the selector. Python owns the serial port and does the decoding; the browser is
presentation only.

That split is deliberate. `bthome-ble` is the reference parser and it is a Python
library, so letting the browser talk to the dongle directly (WebSerial) would
mean reimplementing the BTHome object layer in JavaScript — the exact kind of
second implementation that once made this tool silently swallow dimmer events.
It also keeps the UI working in any browser, not just Chrome.

**Standard library only** — `http.server` for the pages and JSON endpoints,
Server-Sent Events for the live stream. No web framework, no websocket package.

Settings live in a **dialog**, with a one-line summary of the active
configuration left in the toolbar. Settings are read and changed in bursts,
while frames arrive continuously, so an in-flow panel spent its life either
collapsed or shoving the frame table down the window every time it opened.

**Every status display reads the dongle, never the form.** The summary line, the
state pill, the open port, the tuned bar in the scan chart and the setup fields
themselves all come from the dongle's own `info`, which the server parses into a
snapshot, publishes as a `radio` event and answers `/api/state` with. It asks
after every command it sends and every five seconds besides — and takes the
snapshot straight out of the
[acknowledgements](#acknowledgements-say-what-they-left-behind) of `listen` and
`hwset`, which carry it in the same grammar.

That poll is also how a dongle that has **stopped answering** is noticed. Two
unanswered polls and the state becomes `no answer`: the pill turns red and the
summary line puts what it is showing into the past tense — `no answer for 14s —
last reported: ch90 …`. It is not a hypothetical. After a suspend and resume,
one of these servers went on reporting `listening` with a full configuration for
seven hours while its port had been dead the whole time; every poll in that span
ran into a timeout and nothing concluded anything from it. The other server
segfaulted outright in pyserial, which was the kinder of the two failures — a
dead process is honest. A vanished port is now reported as a disconnection
rather than left open, and the state clears itself the moment the dongle answers
again.

None of this is a detail of taste: the summary line used to be assembled from the setup fields, so a page
that had not configured anything itself described its own form — one freshly
loaded tab claimed `ch100 … p1=42:54:48:4D:45` while the dongle underneath it
was listening on channel 90 with `43:…`, and the frames on screen came from a
port the selector was not showing either. An input holds what someone typed,
which is a wish; drawn as a fact it makes the tool lie until somebody notices.

The setup dialog is therefore an **editor of the dongle's configuration**: it
opens filled with what the radio reports, `Apply` writes it back (`stop`,
`hwset`, `listen` — the firmware refuses `hwset` while listening — each awaiting
its reply, so a failure stops the sequence and leaves the dialog open with the
values still in it), and closing without applying puts the fields back. `Start`
is then only about reception: it resumes with the configuration the dongle
already has (a bare `listen`), and falls back to configuring from the fields
only for a dongle that has none, fresh off a reset.

Top right stands **which build is answering, at both ends of the serial link**:
the dongle's `fw 3.6.0 · api 5` from its greeting, and this server's own
version. Each turns into a warning on its own terms — an `api` the UI does not
speak, and a source file whose mtime has moved past the running process, which
Python will not reload. That second one is not hypothetical: a padding fix sat
on disk for hours while the UI, running the older import, kept flagging correct
frames as malformed.

While it is warning, **the version is a button**: clicking it restarts the
server into the code on disk. The successor inherits the port that was open and
reopens it, so the click is the whole procedure rather than the first step of
one. It is deliberately not automatic and deliberately not clickable at any
other time — a restart pulls DTR, which resets the dongle, so the radio
configuration and every captured frame go with it. That is a price for the
person watching to agree to, not for a file watcher to decide. The setup fields
keep what the dongle last reported, so `Start` puts the radio back where it was
rather than on the page's defaults.

The list **filters by pipe, and by sender where the decoder names one**. Both
pickers offer only values this capture has actually shown — six pipes where one
is in use is a list of five wrong answers — and the sender picker stays away
entirely under a decoder that has no notion of one, like the raw view. It is a
filter on the view and not on the capture: everything received is kept, so
widening it again brings the frames back, and the count says `3 of 128 frames`
rather than `3`, because a filtered count that looks like a total is a quiet lie
about how much traffic there was. When nothing matches, the table says that
instead of looking like a dead radio.

Pipes and senders are also **coloured apart**, each on the cell that names it,
and on **different channels**: the sender as the colour of its own text, the
pipe as a tint behind its number. One palette for both made blue mean "pipe 1"
and "this sender" in the same row, which reads as a relation between two things
that have nothing to do with each other. The two lists are also ordered to start
far apart, because both are handed out in order of appearance and the common
case — one or two of each — is the one that must not collide. Which column holds
the sender, the decoder declares, the same way it declares which one holds the
packet number, so the table never has to guess from the contents. A three-pixel stripe down the left edge came first and was
removed: it was correct and unreadable, and it marked the whole row without
saying which of the row's two colourable things it meant. Two things worth
telling apart need two places to say it, not one place and a stripe. Only once
there are two of them, though: a colour every row shares
says nothing and still costs the eye something. Six colours are defined and a
seventh value goes uncoloured rather than repeating one, because a repeated
colour asserts a sameness that is not there. A frame the decoder objected to
stays red throughout — that it is broken outranks whose it was.

> Following the tail is one scroll per batch of rows, not one per row. Reading
> `scrollHeight` forces the browser to lay the table out on the spot, so doing
> it per row made every redraw quadratic: 800 frames took 3.9 seconds instead of
> 11 ms, and a full 5000-frame history would have taken minutes. It cost that on
> every filter change and on every tab that opened against a server with history
> to replay.

Frames arrive with **millisecond timestamps and a Δ column** — the
three repeats of one event sit ~4 ms apart, which per-second resolution hides.
Selecting a row shows **decoded fields and the hex dump side by side**; frames the
decoder objected to are drawn in red. The log and the free-text command line share
their own tab. A tab opened later is brought up to date: the server replays the
greeting, the current state and the retained frames.

In the **scan chart** each of the 126 bars is a couple of pixels wide, so
pointing at one reads out which channel it is — `ch 30 · 2430 MHz · 6/64` — in
the header beside the summary, immediately and without covering the chart. The
native tooltip says the same, but only after a second's hesitation and only
until the pointer moves, which is no use for sweeping along the band looking for
the busy one.

### The stream is resumable, and that is not decoration

Commands go up over HTTP and answers come down over Server-Sent Events. The
split is not just what the standard library makes easy — it is what the traffic
is: commands are rare and want a status code and a reply body, frames arrive in
bursts a few milliseconds apart and want no one to have to ask.

But `EventSource` reconnects on its own, and a server that replays its history
into a table nobody cleared shows every frame twice. In a tool whose purpose is
counting retransmissions, that is not cosmetic. So every event that passes
through the hub is numbered, `id: <run>-<seq>`, and the browser hands the last
one back in `Last-Event-ID` at the next connection — the field SSE has for
exactly this, and the reason it beats a hand-rolled websocket here rather than
merely being simpler than one.

```
reconnect with Last-Event-ID: 1785158946-13
   │
   ├─ same run, nothing missing   ─►  three snapshots, no frames, nothing moves
   ├─ same run, frames since 13   ─►  only those frames
   └─ other run, or before a clear ─► `reset` first, then the whole history
```

`<run>` identifies the process. Numbering starts over when it does, so a client
resuming from a number this run has not reached would be sent nothing at all and
go on showing rows from a process that no longer exists — the same class of
fault as a stale display, one layer down. Anything that cannot be continued gets
a `reset` event, and the browser empties its table before the replay lands.
Clearing the history publishes the same event, so a second tab does not go on
showing frames the server has thrown away.

The snapshots replayed on connect — greeting, status, `radio` — deliberately
carry no number. They describe now rather than a point in the stream, and a
replayed greeting keeping the number it was first published under would drag a
client's resume point backwards.

| Endpoint | Purpose |
|---|---|
| `GET /api/events` | SSE stream of frames, log lines, greeting, status and `radio` (the dongle's own configuration, whenever it changes). **Resumable** — see below |
| `GET /api/ports`, `/api/parsers` | what is available |
| `GET /api/state` | one synchronous snapshot: connected, open port, state, decoder, `radio` (the parsed `info` block) with its `radioAge` in seconds, wiring, and `firmware` (the dongle's `fw`/`api` from the greeting, against the `api` this host speaks) |
| `POST /api/connect`, `/api/disconnect`, `/api/command` | control; `command` with `"wait": true` blocks for and returns the firmware's OK/ERR reply |
| `POST /api/burst` | transmit a frame sequence (`{"address", "frames": [{"payload", "repeat", "gap_ms", …}]}`), one awaited reply per entry |
| `POST /api/restart` | answer, then replace this process with one running the code on disk, handing it the open port. Resets the dongle |
| `POST /api/capture` | block for `seconds`, return the window's frames + stats |
| `POST /api/parser` | switch decoder, returns the history re-decoded |

## Letting another agent drive the dongle (MCP)

The serial port takes one owner at a time, so a second process cannot open the
dongle while the web UI holds it. `nrf24_mcp.py` sidesteps that: it is an **MCP
server that proxies to the running web UI over HTTP** and touches nothing
itself. A person keeps watching in the browser while an agent captures,
configures and transmits through the same dongle.

The consuming session's handout is [`MCP_FOR_AGENTS.md`](MCP_FOR_AGENTS.md) — a
self-contained page with the registration, the tools, and the traps that
otherwise produce wrong conclusions (an idle device that only broadcasts hourly,
NO_ACK duplicate frames, the shared dongle). Point the other agent at that file.

Start the web UI (`start.cmd`), then register the MCP server in the *consuming*
session — the copy in [`mcp.example.json`](mcp.example.json), or:

```bash
claude mcp add-json nrf24 "{\"command\":\"C:/Repos/tools/nrf24-analyser/.venv/Scripts/python.exe\",\"args\":[\"C:/Repos/tools/nrf24-analyser/nrf24_mcp.py\"]}"
```

| Tool | What it does |
|---|---|
| `nrf24_state` | connected? listening? on what wiring? |
| `nrf24_configure(channel, pipe1, …)` | tune the radio and start listening |
| `nrf24_capture(seconds)` | collect the window's frames, decoded, plus a per-sender summary (counts, packet-id range, skipped counter values) |
| `nrf24_transmit(address, payload, ack)` | send one frame — a stimulus to provoke a response |
| `nrf24_stop` | stop receiving |

A typical validation loop: `nrf24_configure` for the channel under test, flash
the firmware, `nrf24_capture(20)`, read the summary, decide pass/fail. The tools
return frames and statistics, not a verdict — the criteria live with the caller.
Reconfiguring or transmitting affects the browser view too; that is the price of
one shared dongle, and it is deliberate.

### Adding a decoder

Decoders live in [`nrf24_parsers.py`](nrf24_parsers.py). Subclass `Parser`,
declare the table columns it contributes, and apply `@register`:

```python
@register
class MyParser(Parser):
    name = "myproto"
    label = "My protocol"
    columns = (("id", "Msg#", 56), ("who", "Sender", 116), ("data", "Payload", None))

    def cells(self, data): ...      # {key: text} for those columns
    def detail(self, data): ...     # the field list for the detail pane
    def packet_id(self, data): ...  # optional: the sender's own counter
    def identity(self, data): ...   # optional: which frames are one event
```

Only Time, Δ, Pipe and Len are fixed — they describe the reception, not the
protocol. What a frame *says* is the decoder's business, and a single "Decoded"
column turned every protocol into prose, which cannot be scanned or compared
down a column. Widths are pixels, `None` takes the rest of the row.

Neither the web UI nor the terminal needs to change — the dropdown is built from
the registry.

`nrf24smart` is the proof of that: the legacy protocol this tool replaces, added
as a second decoder that shares nothing with BTHome, with no edit anywhere else.
It was reconstructed from `archive/smart-home-nrf` (`RFcomm/*.h` and
`nrf24Smart/message.py`) and is checked against those classes.

### Watching a fixed-payload sender

A sender on a fixed payload size fills the unused tail of every frame, and the
BTHome senders here fill it with `0xFF` — an object id BTHome does not define, so
it cannot be mistaken for data. The decoder drops that tail and says how much it
dropped, rather than hiding it:

```
  Button    : press
  padding   : 16 bytes of FF
```

Without that, every padded frame would trip the "objects and payload length
disagree" flag, which would make the flag useless for the frames that really are
malformed. It is reported rather than swallowed because the same bytes on a
*dynamic* sender would mean something quite different — that the frame was read
too long.

To receive such a sender at all, the radio has to be configured for it: `dpl=0
plsize=32`. Both ends must agree — a receiver on a fixed size hears **nothing**
from a sender using dynamic length, and the other way round.

**Limitation**: `dpl` and `plsize` are per-radio here, while the chip has them
per pipe (`DYNPD`, `RX_PW_Pn`). A receiver can therefore serve dynamic and fixed
senders at once — the ESPHome receiver in this ecosystem does — but this sniffer
watches one or the other, not both in one session.

### NRF24Smart, and what the frame does not say

Three packet shapes share one `id, uuid[4], msg_type` header:

```
device  id uuid[4] type fw power interval msgnum  data[n] sum[2]   (12+n)
host    id uuid[4] type                           data[n] sum[2]    (8+n)
remote  id uuid[4] type target[4] layer value     sum[2]            (14)
```

The checksum cannot tell them apart. It covers "everything but the last two
bytes" in all three, so a frame that validates as one validates as all of them.
The original receiver does not need to care: it keys on `msg_type == 6` for
remote packets and reads everything else as a device packet, because it only
ever receives from devices — host packets are the ones it sends.

A sniffer sees both directions and has no such context, so the decoder infers
one: `set` and `reset` only travel host→device, `boot`/`status`/`ok`/`error`
only device→host. `init` travels both ways and is reported as undetermined
rather than guessed.

## Layout

```
nRF24-Analyser/
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
  nrf24_parsers.py            decoder registry: raw, bthome, nrf24smart
  nrf24_mcp.py                MCP server: lets another agent drive the dongle
  requirements.txt            pyserial, bthome-ble
```
