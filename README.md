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
| `format <bin\|text>` | how received frames leave: readable lines (default) or [binary records](#format-bin-the-same-frames-in-half-the-time) |
| `baud <rate>` | raise the serial rate for this session; a reset restores 500000 — [and it buys nothing yet](#baud-and-why-it-buys-nothing-yet) |
| `tx <addr> <hex...> [ack\|noack] [x<n>] [gap=<ms>]` | transmit a payload (default `noack`), optionally `n` copies `gap` ms apart |
| `txseq <addr> <count> [ack\|noack]` | read the next `count` lines as payloads and transmit them in order — [see below](#sending-more-than-one-frame-txseq) |
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

### Sending more than one frame: `txseq`

`tx` costs a command and a reply per frame — about 7.8 ms, most of it serial
round trip. `txseq` pays that once for a whole run:

```
> txseq 4354484D45 128 noack
OK txseq ready count=128
> <64 hex characters>          <- 128 lines, no command word, no reply
...
OK txseq sent=128/128 ack=no
```

Between `ready` and the closing line the dongle reads **payloads, not
commands** — anything else typed there is transmitted, or ends the run with
`stopped=bad payload`. Half a second of silence ends it too, so an abandoned
run cannot swallow the commands that follow it. Unacknowledged runs report
progress every 32 frames (`OK txseq at=<n>`).

Nothing is added to the payloads. No sequence number, no length, no checksum:
only the caller knows what its receiver expects, and framing invented here
would describe a transfer this tool does not control.

#### How fast an acknowledged transfer goes

4096 bytes, verified byte-for-byte at the receiver each time:

| air rate | observer | ms/frame | kB/s |
|---|---|---|---|
| 250 kbps | `text` | 4.44 | 7.0 |
| 1 Mbps | `bin` | 1.88 | 16.7 |
| 2 Mbps | `bin` | **1.74** | **18.0** |

Three things had to be right, and each was measured rather than assumed.

**A window, not lockstep.** Waiting for every frame to be confirmed before
writing the next put a whole host round trip between frames - at 2 Mbps, 4.42
ms a frame of which 0.35 ms was air. Three payloads in flight closes that gap
and still cannot overflow the dongle's 256-byte input buffer, which holds three
69-byte payload lines but not four.

**The air rate is worth 1.3 ms, once.** 250 kbps to 1 Mbps saved 1.26 ms a
frame; 1 Mbps to 2 Mbps saved 0.04. Past the first step the air is no longer
what costs.

**The observer is not the transfer.** At 1 Mbps with a readable-line observer
the run reported 128/128 acknowledged while only 60 frames reached the host -
the chip accepted and acknowledged them, and the firmware then discarded them
at the flush. Reassembling that gives a corrupt file and no error anywhere. A
dongle watching a fast transfer needs [`format bin`](#format-bin-the-same-frames-in-half-the-time);
otherwise what fails is the measurement, and it fails silently.

That last point cuts both ways, and it is the good news about acknowledgement:
a receiver whose FIFO is full stops acknowledging, so the sender slows to what
the receiver can absorb. An acknowledged transfer paces itself. That is why the
250 kbps row above is byte-perfect even with a readable-line observer - the
sender was being held back to 4.44 ms a frame by the observer's own limit.

What bound next was the serial line to the sending dongle, and `txseq ... bin`
removed it. A payload arrives as a record - one length byte, the payload, one
checksum - instead of two hex characters per byte. No sync marker, because none
would help: the dongle knows it is owed a fixed number of records and each
states its own length, so a failed checksum ends the run rather than
transmitting a guess. The host asks for `bin` and falls back to hex when the
firmware answers `ERR unknown key`, which is the whole negotiation.

Halving the payload was worth less than it looks until the confirmations were
counted too. Measured at 2 Mbps, 512 frames:

| | ms/frame | kB/s |
|---|---|---|
| hex, no acknowledgement | 1.50 | 20.8 |
| **binary**, no acknowledgement | **0.79** | **39.5** |
| binary, acknowledged, confirmed every frame | 1.55 | 20.2 |
| binary, acknowledged, confirmed every third | **1.32** | **23.6** |

The payload halved exactly as arithmetic said. But a confirmation the host has
to wait for cost 0.76 ms on a 0.79 ms frame - it is a round trip, not sixteen
bytes. Confirming every third frame instead of every one recovers most of that,
and stays safe because a binary run's window is six records where a hex run's
is three: the same 256-byte buffer, smaller records.

Where that leaves a transfer, against 5.72 ms a frame and 5.5 kB/s when it was
first measured:

| | ms/frame | kB/s |
|---|---|---|
| 250 kbps | 2.74 | 11.4 |
| 2 Mbps | **1.32** | **23.6** |

#### Everything else that was tried

The remaining levers were measured and most of them did nothing. Written down
so they are not tried again.

**Window against confirmation interval**, 512 frames at 2 Mbps, ms per frame:

| window | conf=1 | conf=2 | conf=3 | conf=4 | conf=6 |
|---|---|---|---|---|---|
| 3 | 1.71 | 1.85 | 2.05 | – | – |
| 6 | 1.54 | 1.41 | 1.33 | 1.30 | 1.63 |
| 7 | 1.60 | 1.39 | 1.32 | **1.29** | 1.45 |

Both directions cost. Confirming every frame is a round trip per frame;
confirming too rarely leaves the host stalled with a full window. A window of
three cannot be rescued by any interval, which is why the hex path is slower
than the record path for a reason beyond its byte count. Seven and four are the
defaults now, and `conf=` on the command exposes the axis because the right
value is not derivable.

**1 MBaud is a net loss.** The sending line is what binds, so quadrupling it
should have helped. Measured: 24.4 kB/s at 500000 became **5.0 kB/s** at
1000000. At about one corrupted byte in a hundred lines a record fails its
checksum often enough that the run spends its time being resumed - five times
in 512 frames - and the capture becomes unreliable as well, because the
observing dongle is reading at the raised rate too. 500000 stays.

**Batching the writes did nothing.** Handing the dongle a windowful of records
in one `write` rather than one call per record measured 1.28 ms a frame either
way. The syscall was not the cost, and the change was reverted rather than kept
for tidiness.

**A bigger input buffer and a bigger window did nothing.** The obvious reading
of the gap below was that the host stalls waiting for a confirmation with the
window full, so the serial receive buffer went to 512 bytes and the window to
fourteen records. Measured across windows of 7, 10, 12 and 14: 1.30 to 1.33 ms
a frame, indistinguishable. The window was never the constraint, and the buffer
came back down rather than spend a quarter of the ATmega's RAM on nothing.

#### How close this is to the serial line, and what the rest is

At 500000 baud, 8N1, a byte costs 20 us. An acknowledged frame puts 34 bytes on
the wire outbound and about four inbound, so the line's own floor is **0.68 ms
a frame - 47 kB/s of payload**. Against that:

| | ms/frame | share of the line |
|---|---|---|
| unacknowledged | 0.79 | **86 %** |
| acknowledged | 1.29 | 51 % |

The unacknowledged path is essentially at the wire. The acknowledged one is at
half, and the missing half is not the host and not the buffer: it is that the
firmware does not overlap the air with the wire. It reads a record, transmits,
waits for the acknowledgement, and only then reads the next - while the host,
thanks to the window, wrote the next seven long ago.

That is measurable rather than argued. Going from 2 Mbps to 250 kbps costs
1.45 ms a frame; the arithmetic for one 329-bit packet and its acknowledgement
predicts 1.41 ms. Nothing hides behind anything - the extra air time shows up
in full, which is what "not overlapped" means.

Closing it means keeping the transmit FIFO fed under acknowledgement, the way
the unacknowledged path already does, and that has a price this tool should not
pay quietly: `TX_DS` is a flag rather than a counter, and on `MAX_RT` there is
no register saying how many packets are still queued behind the failed one. So
`sent` would stop being exact at the moment a frame fails - and `sent` is what
[resume](#picking-up-where-a-broken-run-stopped) trusts to continue without
sending anything twice. The failure would be duplicates rather than gaps, but
it would be a guarantee traded for about sixty per cent more speed.

So a transfer settles at **1.29 ms a frame and 24.3 kB/s** acknowledged, which
held from 4 kB to 64 kB (2048 frames in 2.6 seconds, every frame confirmed,
byte-for-byte identical). Unacknowledged and binary reaches 0.79 ms and
39.7 kB/s, which is the sending dongle's serial line and nothing else - but
at that rate the receiving dongle sees about a seventh of it.

#### Which air rate is actually used

**Re-measured on every change that could move it, committed with that change,
and always carrying the difference to the previous measurement.** A stale table
here is worse than none - which lever is worth pulling next has flipped several
times in this project purely because a number moved. And the delta is the half
that matters: several changes measured as *exactly zero*, which is the finding
that stops the same idea being tried a second time.

512 frames of 32 bytes, both dongles. A packet is 329 bits on air and its
acknowledgement 73 more; the serial line's own limit is a 34-byte record at
20 us a byte, so 0.68 ms a frame - **47 kB/s whatever the radio does**.

Read "air used" against the right denominator: acknowledged, the air's own floor
is not the packet but the whole exchange - 1316 us of packet, 292 of
acknowledgement and two 130 us turnarounds, so 1868 us at 250 kbps. There is no
such thing as "the full 250 kbps with acknowledgement"; 100 % of *that* is what
the column measures. And even unacknowledged only 256 of the 329 bits are
payload, so 78 % of the modulation rate is the ceiling on useful data before
anything else is counted.

**Transmit power is not a lever to reach for.** Measured on one channel, three
interleaved rounds: `low` 1.96 ms with 1-2 retransmissions, `high` 2.06 with
16-31, `max` **6.57 with 781-849**, `min` 2.59 with ~105. The dongles sit close
together and full power overloads the receiver's front end; `low` is both the
default and the optimum here.

Now at **fw 3.20.0**, three interleaved rounds per row, median. **Each rate on
its own best channel** — 250 kbps on 14, 1 and 2 Mbps on 88 — because the choice
turned out to be worth more than anything in the firmware and to differ by rate:
channel 14 is the quietest for 250 kbps and among the worst for 2 Mbps, the
faster rates occupying more of the band. Receiving side in `format bin`, so that
what arrived can be checked. The Δ is against fw 3.14.0 on the same channels.

| air rate | | measured | Δ ms | air used | wire used | seen by observer | Δ |
|---|---|---|---|---|---|---|---|
| 250 kbps | acknowledged | 1.95 ms, 16.4 kB/s | −0.04 | 96 % | 35 % | 512/512 | +0 |
| 250 kbps | not | 1.33 ms, 24.0 kB/s | +0.02 | 99 % | 51 % | 512/512 | +0 |
| 1 Mbps | acknowledged | 0.95 ms, 33.8 kB/s | +0.01 | 70 % | 72 % | 512/512 | +0 |
| 1 Mbps | not | 0.89 ms, 36.1 kB/s | +0.01 | 37 % | 77 % | 473/512 | −14 |
| 2 Mbps | acknowledged | 0.96 ms, 33.4 kB/s | +0.02 | 48 % | 71 % | 512/512 | +0 |
| 2 Mbps | not | 0.90 ms, 35.7 kB/s | +0.02 | 18 % | 76 % | 467/512 | −18 |

The sending times stand still, which is right and has been right three times
running: that path is bound by the serial line, so neither a shorter record nor
faster SPI shows up in it. What moves is the last column. Against fw 3.15.0 the
observer gained another 14 and 7 frames from CSN by direct port write, and the
acknowledged rows needed fewer retransmissions again (1 Mbps 71 → 64, 2 Mbps
133 → 125), because a receiver that keeps up goes on acknowledging.

Cumulative over this branch's work on the receive path: on the rows where the
observer is the constraint, **1 Mbps 426 → 487 of 512 and 2 Mbps 422 → 485**,
and padded sensor frames go from about 426 to 512 of 512 at both rates.

Against a **silent** receiver the same six rows read 1.31 / 1.99 / 0.88 / 0.90 /
0.87 / 0.89 ms with 512 of 512 seen everywhere except 250 kbps unacknowledged,
where about ten frames are lost on air rather than in the dongle. Acknowledgement
costs 4 % at 1 and 2 Mbps once the channel is clean — the 1.04 ms measured on a
mediocre channel was retransmissions and nothing else.

#### The last quarter of the wire, and three things that do not get it

A record needs 0.68 ms on the line and a run measures about 0.88. That quarter
resisted everything aimed at it:

| tried | result |
|---|---|
| window from 7 to 14 records | 0.88 → 0.87 ms |
| serial receive buffer 256 → 512 bytes | 0.90 → 0.89 ms |
| one `write` per window instead of per record | 0.88 → 0.87 ms |

All three are within the noise of the six rows above, and all three were
reverted rather than kept: a 512-byte buffer is a quarter of the ATmega's RAM,
and batching is code with nothing to show for itself. Confirming *less* often
is actively worse - at `conf=16` a run takes 4.2 ms a frame, because the host
then sits with a full window waiting.

What is left was guessed at as host-side scheduling - Python's reaction to a
confirmation, and the USB frame the CH340 is served in. Half of that guess was
wrong, and [measuring the same wire without the analyser on
it](#the-same-wire-without-the-analyser) is what showed it: Python costs nothing
measurable, and 0.09 ms of the 0.17 is spent before any of our code runs. The
sending path is therefore treated as finished at **75-80 % of the serial line**,
which is itself a fifth of what the radio can do.

**The confirmation repeat is a real fix even though it moved no number here.** A host whose
window is full cannot write another payload until one is confirmed, so a
confirmation damaged on the wire used to deadlock the run until the dongle's
500 ms quiet timer killed it - one bad byte on the return path costing an entire
transfer. The dongle now says the count again after 25 ms of silence, and
because that count is a running total rather than a tick, hearing it twice is
harmless. At 1 MBaud, where damaged bytes are common, an acknowledged 512-frame
run went from ending at 279/512 to completing byte-for-byte.

Which finally closes the serial rate, for the third time and now on its own
terms: 1 MBaud completes but is *slower* - 1.14 ms a frame against 1.01 - and
unacknowledged it still dies on a damaged payload record, which has no resume.
500000 stays.

**But the observer in those rows is itself the constraint**, and `format none`
is what shows it. A dongle on the receiving end of a transfer does not need to
write every frame out; writing costs it about 800 us a frame, and a receiver
that falls behind stops acknowledging, so the sender retransmits.

| air rate, acknowledged | receiver prints (`bin`) | receiver silent (`none`) |
|---|---|---|
| 250 kbps | 1.99 ms, 7 retransmissions | 1.99 ms, 7 |
| 1 Mbps | 1.00 ms, **78** | **0.87 ms**, **3** |
| 2 Mbps | 1.03 ms, **141** | **0.87 ms**, **0** |

So the retransmissions were never the air. They were the receiver's serial
port, arriving back at the sender as backpressure - which is the acknowledged
link doing exactly what it should, pacing itself to what the far end can take.
Against a receiver that is not narrating, an acknowledged transfer runs at
**0.87 ms a frame, 35.9 kB/s** - within a whisker of the unacknowledged 0.83,
and at 78 % of the serial line. Acknowledgement has stopped costing anything
worth measuring.

At 250 kbps it changes nothing, because there the air binds and no amount of
silence at the far end helps.

Read the two "used" columns together: above 250 kbps the air stops binding and
the serial line takes over. Both directions are now within a fifth of that
line, and the one measure that would widen it - a faster serial rate - has been
measured twice as a net loss, most recently at 1.10 ms a frame against 1.00.

#### The same thing without the UART

Every figure above measures the radio and the serial line together, because the
payload arrives over the serial line - and that turned out to be what binds.
`txtest` removes it: the payload comes from flash with a frame index stamped
into it, so nothing crosses the UART per frame. Point it at a receiver in
`format none` and there is no UART in the path at all.

2000 frames of 32 bytes, timed by the sending firmware's own clock:

| air rate | | us/frame | kB/s | air allows | received |
|---|---|---|---|---|---|
| 250 kbps | not acknowledged | 1284 | 24.3 | 1316 us | 1990/2000 |
| 250 kbps | acknowledged | 1981 | 15.8 | 1738 us | 2000/2000, 34 retransmissions |
| 1 Mbps | not acknowledged | 321 | **97.4** | 329 us | 1999/2000 |
| 1 Mbps | acknowledged | 678 | 46.1 | 532 us | 2000/2000, **0** |
| 2 Mbps | not acknowledged | **161** | **194.1** | 164 us | 1420/2000 |
| 2 Mbps | acknowledged | 473 | 66.1 | 331 us | 2000/2000, **0** |

Unacknowledged, the radio sits **on the air's own limit at every rate** - 161 us
against a calculated 164. The chip, the SPI bus and the drain loop were never
the constraint and this says so outright.

Set against the same transfers driven over serial:

| | through the UART | radio only | the UART costs |
|---|---|---|---|
| 2 Mbps, not acknowledged | 37.5 kB/s | 194.1 kB/s | **5.2x** |
| 2 Mbps, acknowledged | 35.9 kB/s | 66.1 kB/s | 1.8x |
| 1 Mbps, not acknowledged | 37.5 kB/s | 97.4 kB/s | 2.6x |

So the entire optimisation above - records instead of hex, a window, pipelining
- was work on the host link, and what lies behind it is five times larger. That
is the honest shape of this dongle: **a radio that can do 194 kB/s behind a
serial port that can do 47.**

The receiving side has a ceiling of its own, and the 2 Mbps unacknowledged row
is where it shows: 1420 of 2000, at a rate the sender held for the other five
rows. 1420 frames over 322 ms is 4400 a second, so a dongle can take a frame
off the radio and out of the FIFO in about 226 us when it prints nothing at all
- roughly 141 kB/s. Acknowledgement hides this completely, which is what the
zero retransmission counts mean: the link paced itself to the receiver without
losing anything.

#### Is 250 kbps actually exhausted, with and without acknowledgement

The table above says 250 kbps is where the air binds rather than the wire, which
makes it the one rate where the question "are we using all of it" has a clean
answer. Measured on channel 76 at address `42:54:48:4D:45` - away from the
BTHome sender that transmits on channel 90 continuously, because a third party
on the same channel and address would be measured along with everything else.

512 frames over the serial line, against `txtest`'s 1000 frames with no serial
line in the path at all:

| | radio only | over the UART at 500000 baud | the UART costs |
|---|---|---|---|
| not acknowledged | 1.287 ms | 1.31 ms | ~25 us, 2 % |
| acknowledged | 1.95-2.09 ms | 2.05 ms | nothing measurable |

**Unacknowledged, 250 kbps is exhausted.** 1.31 ms a frame against 1.316 ms of
calculated air time, and the serial line - which needs 0.68 ms for the same
frame - is not close to binding.

**Acknowledged, what is left is retransmissions rather than anything a program
can change.** The acknowledged cycle is 1316 us of packet, 292 us of
acknowledgement and two 130 us turnarounds, so 1868 us; a run measures about
2050. Fifteen `txtest` runs at five different retransmit delays put a number on
the difference: per-frame time tracks the retransmission count almost exactly,
at **2169 us per retransmission**, and extrapolating to none gives **1952 us** -
within 5 % of the arithmetic.

The auto-retransmit delay looked like a lever and is not. A first pass suggested
`retries=750,15` was worth 6 % over the default 1500 us; repeating it three
times per setting showed the retransmission count varying eight-fold *within* a
setting, and the apparent win disappeared:

| ard | us/frame, three runs | retransmissions |
|---|---|---|
| 500 us | 2107, 2104, 2263 | 85, 86, 164 |
| 750 us | 2178, 2342, 2238 | 109, 184, 134 |
| 1000 us | 2280, 2213, 2011 | 140, 113, 31 |
| 1250 us | 2068, 1995, 1956 | 49, 22, 8 |
| 1500 us | 2285, 2003, 2125 | 117, 23, 64 |

The spread within one setting is larger than the spread between settings, so
this measures the room, not the parameter. `retries` stays at 1500,15.

> One discrepancy is left open rather than smoothed over. Unacknowledged,
> `txtest`'s own clock says **1287 us** a frame where 329 bits at 250 kbps needs
> **1316 us** - 2 % *below* what the arithmetic says is possible, so one of the
> two is wrong. It is not the ATmega's clock: the serial bench above has the
> device and the host timing the same 32 kB transfer independently, and they
> agree to 0.3 %. Either the packet is shorter than 329 bits or the chip's
> "250 kbps" is not exactly 250 kbps. Two percent does not change any conclusion
> here, but it is not understood.

#### The same wire without the analyser

`txtest` takes the UART out to measure the radio. This does the opposite. A
second firmware, `bench/serialbench`, has no radio and no protocol in it at all:
it sources bytes, sinks bytes, echoes bytes, and acknowledges every k-th byte -
which is the shape `txseq ... bin conf=` has, minus everything else the analyser
is doing. Whatever that firmware cannot do, the analyser certainly cannot.

Driven from `bench/serialtest.py` at 500000 baud, 32 kB a run:

| | measured | of the line |
|---|---|---|
| dongle to host | 50.0 kB/s | **100 %**, not one wrong byte |
| host to dongle, 34-byte writes | 44.7 kB/s | 89 % |
| host to dongle, 4096-byte writes | 45.4 kB/s | 91 % |

The direction that carries a transfer is the one that falls short, and it falls
short by about the same amount whatever it is asked: block size barely moves it,
and a 64 kB driver buffer moves it not at all - the fourth null result for
buffer sizes in this file.

Which finally puts a number on the last quarter of the wire:

| | ms per 34-byte record |
|---|---|
| what the line needs | 0.68 |
| an empty firmware | 0.77 |
| the analyser | 0.85 |

Of the 0.17 ms between the analyser and arithmetic, **0.09 is gone before any of
our code runs**, and 0.08 is ours. The sending path is at 90 % of a firmware
that does nothing.

**It is not Python.** The same four measurements, written again in C++ against
the raw Win32 calls that pyserial wraps, agree to the third digit in every
row - 50.1 kB/s reading, 44.7 and 45.4 writing, 0.77 ms a record, 1.33 ms for
the round trip. C++ can also do the one thing pyserial cannot, keeping eight
writes outstanding so the driver never waits for the next buffer: 44.8 kB/s
against 44.7. The gap is below the Win32 API, not above it.

**A round trip costs 1.33 ms.** One byte out and one back, and the same at every
rate - 1.32 ms at 1 MBaud, 1.43 at 250 kbps - against 40 us of actual wire. That
is USB, and it is why the window matters and the confirmation interval does not:
a window of 7 records keeps 4.8 ms in flight and hides the round trip
completely, while a window of 1 pays it every record and takes 2.54 ms instead
of 0.77.

It also settles the serial rate for the fourth time, now with a cause instead of
a symptom:

| baud | dongle to host | host to dongle |
|---|---|---|
| 250000 | 100 %, clean | 100 %, clean |
| 500000 | 100 %, clean | 89-91 %, clean |
| 1000000 | full speed, **23000-31000 damaged bytes in each of five runs** | 70-77 %, clean |
| 2000000 | unusable | - |

At 1 MBaud the right *number* of bytes arrives - none missing, timing exact -
with the wrong contents, and only in the direction dongle to host. The other
direction is faultless, which means the two clocks agree: a rate mismatch would
break the working direction as well. C++ sees identical damage. And the lockstep
says the same thing from a third angle, by stalling - what comes back damaged is
the acknowledgements.

#### The Windows driver, cleared - and the trap in clearing it

The dongle was bound with usbipd and attached to WSL2, where Linux's `ch341`
driver takes over and WCH's Windows driver is out of the path entirely. The same
`bench/serialtest.py`, unchanged, against the same dongle.

Read naively, the result says Linux wins:

| | Windows | WSL2 / `ch341` |
|---|---|---|
| write, host's clock | 721 ms (91 %) | **675 ms (97 %)** |
| read, host's clock | 655 ms (100 %) | **617 ms (106 %)** |

**That 106 % is the tell.** Reading 32768 bytes off a 500000-baud line cannot take
less than 655 ms, so the second column is not a faster wire, it is a worse clock.
usbip returns from `write()` once the request is queued rather than once the
bytes have left, and it hands reads over in batches.

The dongle's own clock settles it, and it is the reason `serialbench` reports its
own timing at all:

| device says | Windows | WSL2 |
|---|---|---|
| write 32 kB at 500000 | 723-724 ms | **723 ms** |
| write 32 kB at 1 MBaud | 428 ms | **428 ms** |
| read 32 kB at 500000 | 655 ms | **655 ms** |

Identical, to the millisecond. The bytes cross the wire exactly as they did
before; only the host's idea of when it finished changed. And 1 MBaud corrupts
the same way under Linux - every read run damaged, 14700 to 30300 bytes wrong out
of 32768, with the write direction clean at the same 428 ms.

So **the Windows driver is not the cause**, of either finding. The missing 9-11 %
in the write direction and the 1 MBaud corruption are below both drivers: the
CH340 itself, or the USB scheduling underneath it. Two operating systems, three
host programs, one answer.

usbip's own cost is worth recording so nobody mistakes it for a result: a
one-byte round trip goes from 1.33 ms to 2.09 ms, and the lockstep figures under
WSL are noise for the same reason the throughput ones are.

What remains untried is a **CP210x on the same host** - a different bridge chip
with a different vendor's driver, where reaching 100 % would convict the CH340
and stopping at 91 % would convict the host's USB stack.

> Binding a device with usbipd makes Windows re-enumerate it, and it comes back
> with a **new COM number** each time - COM18 became COM35, then COM36. The
> number is set in the device's `PortName` under
> `HKLM\SYSTEM\CurrentControlSet\Enum\...\Device Parameters`, or through Device
> Manager under Port Settings, Advanced.

#### Where the RAM goes, and 324 bytes that were not paying rent

An ATmega328P has 2048 bytes. At fw 3.15.0 the firmware held 1496 of them, and
the question of what could be spent on new capability was really the question of
what the existing 1496 were for. From the ELF, not from guessing:

| | bytes | |
|---|---|---|
| `Serial` | 541 | two 256-byte ring buffers and the object |
| `RadioController` | 303 | of which **126 is the scan histogram**, 32 the repeat filter, 36 the pipe addresses |
| `CommandParser` | 154 | a 128-byte line buffer and its state |
| the register-name table | 198 | 57 bytes of pointers **and the nineteen names themselves** |

The serial buffers are a quarter of the chip on purpose - 256 out so
`Serial.print` does not block while the RX FIFO fills, 256 in because an
85-character `tx` line does not fit the Arduino default of 64 and a command
arriving during a transmit lost its tail.

The other two were paying no rent at all:

- **The scan's per-channel counters are now taken for the duration of a scan.**
  A scan retunes the radio, so it can never overlap with receiving; keeping 126
  bytes reserved permanently for something that runs for a few seconds was the
  single worst trade in the file. During a scan the total is exactly what it
  always was, and the resting state is 126 bytes better. It is the only
  allocation this firmware makes and the same pair makes and releases it, so
  there is nothing to fragment - and `scan live` gained the one way it can fail,
  which it reports.
- **The register-name table moved to flash.** It cost 198 bytes of RAM, not the
  57 the symbol table shows: string literals live in RAM on this architecture
  unless they are told otherwise, so the nineteen names were there too. For a
  diagnostic command that prints them on request.

**1496 → 1172 bytes**, and 876 free where 552 were. Nothing on the transmit or
receive path was touched, and a check run says so: 2 Mbps unacknowledged with the
receiver printing measures 0.88 ms a frame and 439 of 512, against 0.87 and 440
before; padded payloads stay at 512 of 512.

#### Where a transmitted frame's time actually goes

Three attempts to speed the sending path up measured as exactly zero - SPI from
4 to 8 MHz, CSN by port write, batching the writes - and each time the
explanation was "that path is bound by the serial line". True, but it was never
taken apart. `txseq` now reports the split, by the firmware's own clock: `us_air`
is the spin waiting for room in the transmit FIFO, `us_spi` is the payload going
out over the bus, and whatever a run takes beyond the two is the record arriving
over serial.

A 13452-byte JPEG, 421 acknowledged frames, per frame:

| air rate | total | waiting for the FIFO | SPI | the rest |
|---|---|---|---|---|
| 250 kbps | 2019 us | **1252 us** | 126 us | 641 us |
| 1 Mbps | 929 us | 107 us | 135 us | **687 us** |
| 2 Mbps | 948 us | 121 us | 140 us | **687 us** |

**A 34-byte record at 500000 baud needs 680 us.** The remainder above is 687 -
the serial line and essentially nothing else, which is the first direct
confirmation of a claim this file has been making for a while.

But read the rows again. At 1 and 2 Mbps the three parts **add up**: 687 + 107 +
135 = 929. They are sequential, not overlapped - the firmware waits for a whole
record, then spins for FIFO room, then writes it out, and only then starts
waiting for the next. The UART fills its ring buffer by interrupt throughout, so
the 242 us of radio work is time the wire could have been busy and was not.

That is worth about **a quarter of the sending path**, and it is the first
concrete lever found on it since the window. Not attempted here: it means writing
frame N to the radio while the bytes of frame N+1 are still arriving, which is a
change to how `feedSeqByte` and `sequenceWrite` are ordered rather than to any
protocol.

At 250 kbps none of this matters - the air alone wants 1252 us of the 2019, and
the wire is idle two thirds of the time.

#### 1 MBaud does work, in 32-byte blocks with a pause

Four times this file has closed the question of a faster serial rate, the last
time with a cause rather than a symptom: at 1 MBaud the dongle-to-host direction
delivers the right number of bytes with the wrong contents, under Windows and
under Linux alike. The fifth attempt came from a claim about the chip - that its
1 MBaud is only unreliable when the line runs *continuously*, and that letting it
breathe between short bursts makes it usable.

It is true. `bench/serialbench` gained a block mode for it, and 32768 bytes were
sent five times per setting:

| block | gap | kB/s | clean runs |
|---|---|---|---|
| 32 B | 0 us | 93.8 | 0/5 |
| 32 B | 25 us | 87.7 | 2/5 |
| 32 B | 40 us | 84.2 | 4/5 |
| 32 B | 50 us | 82.0 | 3/5 |
| **32 B** | **60 us** | **80.0** | **5/5** |
| 32 B | 75 us | 77.1 | 5/5 |
| 32 B | 100 us | 72.4 | 5/5 |
| 64 B | anything to 150 us | 78-95 | 0/5, once 1/5 |

**80.0 kB/s with nothing damaged, against 50.1 kB/s at 500000 baud - sixty
percent more on the direction that binds the receiver.**

Two details worth keeping. **The block size is not a tuning knob**: 64 bytes fail
at every gap tried, 32 succeed, and 32 bytes is the CH340's bulk endpoint. And
**the gap threshold is sharp but not where two runs said it was** - an earlier
pass called 50 us clean on the strength of two measurements, and five say 3/5.
Anything at or above 60 us was clean five times out of five.

The first attempt at this measured 23.4 kB/s at 500000 *and* at 1 MBaud -
identical, which is the signature of a limit that is not the wire. It was writing
a byte at a time with a flush between blocks, so the test measured its own
overhead. Building the block and handing it over in one write is what made the
question answerable.

It was then built into the receive path, at 100 us rather than the 60 that first
passed - the difference costs 7.6 kB/s and buys margin on a failure that is
silent. **Twice, and only the second way works.**

| receiver | frames of 512, 1 Mbps unacked | `us_out` |
|---|---|---|
| 500000 baud, continuous | 467-469 | 660 us |
| 1 MBaud, a block per record | 411-412 | 850 us |
| 1 MBaud, blocks aligned to 32 bytes | 451-459 | 735 us |

The first attempt paid the pause twice for a 35-byte record, because a record is
one block and a bit. Accumulating into a 32-byte buffer so the pause falls once
per block on the wire recovered most of that - and it is still behind.

**Reliability at 1 MBaud requires `Serial.flush()`, and flushing is what costs.**
At 500000 `Serial.write` returns as soon as the bytes are in the ring buffer and
the UART empties it by interrupt, so the drain loop does its 187 us of SPI work
*while* the line is busy. A flush ends that: the loop waits for every block, and
the overlap it gives up is worth more than the extra bandwidth. The bench
measured 72 kB/s at this setting because a bench has nothing else to do; a
receiver has 187 us of work per frame that used to hide behind the wire.

**The pause does not have to be waited for.** It only has to be there on the
wire. So the frames go into a queue of the firmware's own, and a pacer hands the
UART one block whenever the line has been idle for 100 us - called from the main
loop and from inside the drain loop, never blocking. The pause then falls in time
the dongle would have spent waiting for the next frame anyway.

| receiver | 1 Mbps air | 2 Mbps air |
|---|---|---|
| 500000 baud, continuous | 467-469 of 512 | 451-463 |
| **1 MBaud, paced** | **489** | **487-495** |

Twenty to thirty frames, on top of everything else this branch did to the same
column.

**One measurement in this sequence was a lie and worth recording as one.** The
first paced version reported 512 of 512 at *both* baud rates - too good, and it
was: `txPump()` had been placed inside the loop that reads serial bytes, which
cost the *sending* dongle 8 % (0.84 s to 0.93 for the 13 kB image). The receiver
was not keeping up better; less was arriving. Moving the call out of that loop
restored the sender to 0.40 s and left the receive gain standing on its own.

#### A quarter of the wire is idle, and it is not the confirmation

Put the two numbers next to each other. A record is 34 bytes, so 680 us of a
500000-baud line. A frame takes 922 us. **The wire is busy 74 % of the time** -
where the serial bench, an empty firmware driven with the same window and the
same confirmation interval, reaches 88 %.

The obvious suspect was the flow control. The host may run seven records ahead
and then waits, and `at=` is the message that lets it go again - and it was said
*after* the radio work, so the dongle delayed by its own 218 us the very thing
that unblocked the host. `at=` only claims that a record is whole and its
checksum passed, which is true the moment the checksum is checked, so saying it
first costs nothing and cannot overrun anything: the window is seven records and
the serial receive buffer holds seven and a half.

Measured over three transfers at each rate:

| | after the radio work | **before it** |
|---|---|---|
| 250 kbps | 823, 822 ms | 826, 820, 825 ms |
| 1 Mbps | 396, 395 ms | 396, 396, 395 ms |
| 2 Mbps | 387, 389 ms | 389, 391, 394 ms |

**Nothing at all**, and reverted. The host was not waiting on the confirmation,
so that explanation of the idle quarter is wrong and is written down here as
wrong rather than left standing as a plausible story.

What remains true is the measurement: the wire is idle a quarter of the time and
the reason is not known. The serial bench says a host driving this pattern over
this hardware leaves 12 % of the line unused on its own; where the other 14 %
goes has not been found, and five changes to this path have now failed to move
it.

#### The payload into the FIFO over our own SPI path

The last item the sending path accounts for: 138 us a frame to put 33 bytes into
the transmit FIFO, where the bus at 8 MHz is 33 of them. The rest is
`RF24::startFastWrite` - `digitalWrite` on CSN twice and the library's own
bookkeeping - and the receive path already showed what that costs. The same
measure, on the other path:

| | `us_spi` per frame | a 421-frame transfer |
|---|---|---|
| `RF24::startFastWrite` | 138 us | 0.39-0.41 s |
| **our own W_TX_PAYLOAD** | **109 us** | 0.39-0.40 s |

Stable to within a millisecond over 421 frames and across runs, so the 29 us is
real. The transfer time is not visibly better, because the radio environment
moved further than that between the runs - the retransmission count went from 46
to 56 at 2 Mbps in the same hour.

**Streaming the payload into the FIFO as it arrives was considered and not
built.** It would take the 109 us to nearly nothing, and it cannot be done
safely: a record's checksum is its *last* byte, so bytes clocked into the FIFO as
they arrive are already committed by the time the checksum says whether they were
intact - CE is high through a run, and a payload in the FIFO of a keyed radio is
on its way. Holding CE low to keep it revocable stalls the radio for the whole
660 us the record takes to arrive, which costs six times what it saves. The
guarantee the image test demonstrates - byte-identical, three rates, three runs -
is worth more than 109 us a frame.

#### Waiting for the radio while the record is still arriving

The split above says the sending path spends 687 us receiving a record, 107
waiting for room in the transmit FIFO and 135 writing it out - in that order, one
after the other. The middle one does not need a payload to hand, so it now
happens as soon as a record's *length* byte has arrived, with the other 33 bytes
still on the wire.

| | 250 kbps | 1 Mbps | 2 Mbps |
|---|---|---|---|
| before | 0.88 s, `us_air` 527 ms | 0.40 s, 45 ms | 0.41 s, 51 ms |
| **after** | **0.83 s, 508 ms** | 0.40 s, **30 ms** | 0.40 s, **39 ms** |

**It did what it was built to do and mostly did not matter.** The wait moved and
shrank by a third, and the total only followed at 250 kbps - six percent, where
the air is the constraint and the wire has time to spare. At 1 and 2 Mbps
nothing: the binding terms there are the 687 us of record and the 135 us of bus,
and the wait was never on the critical path. The prediction of "about a quarter
of the sending path" was wrong, and this is the third estimate in this file to
overshoot the same way.

What it did fix is a number. `OBSERVE_TX` holds the retransmission count of the
*current* transaction and is overwritten by the next, so reading it well after a
frame completed could report a later packet's count. Reaping each `TX_DS` as it
lands reads it while it still belongs to the frame that finished: the reported
count over a 421-frame transfer fell from 47 to 8 at 1 Mbps and from 97 to 46 at
2 Mbps, stable to within one across three runs. That is not fewer
retransmissions - it is the right ones being counted.

#### The frames as a stream

The record `format bin` sent was self-contained: a sync byte, a length, the pipe,
a timestamp, the payload, a checksum and a newline. Seven of those bytes were
frame rather than data, and six of the seven said the same thing as the record
before - same pipe, nearly the same millisecond, and a marker whose only job was
to say "this is not text".

They are now a stream. A **run** of frames opens by stating what they have in
common and closes when the drain loop has nothing left:

| | |
|---|---|
| `0x01` pipe len base(4) | a new epoch: which pipe, how long a payload is, and the millisecond to count from |
| `0x02` | the epoch before it still holds; frames follow directly |
| `0xFF` `
` | no more frames, whatever comes next is readable again |

and a frame between them costs three bytes:

| | |
|---|---|
| stored length (bits 5–0), `LONG` (bit 6) | how many payload bytes follow |
| the true length | only when `LONG` says the run's default does not apply |
| offset | milliseconds past the epoch's base, 0–255 |
| | payload, less any repeated tail, then the fill byte |
| CRC-8 | over the payload as it was, all of it |

**The offset is to the base, not to the frame before.** Every frame in a run is
therefore placed independently: one lost in between costs nothing and no error
accumulates. That is the difference between this and a delta chain, which was
considered and rejected for exactly that reason - a lost frame would have shifted
every timestamp after it, and repeat spacing is the one thing the stamp exists to
measure.

The stored length and the true length differ when a repeated tail was suppressed,
so the run's count is implied by the pair and needs no byte of its own. `LONG` is
for dynamic payloads, where the length changes frame to frame; with `dpl` off it
is never set.

A run ends at every pass of the drain loop, and anything not a frame - a reply, a
`WARN`, a `DBG` - closes the open run first. So a run is never interrupted, and
the host's rule is simple: outside a run, a byte below `0x03` opens one; inside
one, `0xFF` ends it.

Measured, 512 unacknowledged frames with the receiver printing every one:

| | api 6 record | api 7 stream |
|---|---|---|
| 1 Mbps, random payload | 454/512 | **466/512** |
| 2 Mbps, random payload | 447/512 | **463/512** |
| either rate, padded payload | 512/512 | 512/512 |
| `us_out`, the blocking part of the write | 692 us | 654 us |

**Less than the arithmetic promised, and the reason is worth writing down.** A
frame costs 3 bytes plus 1 to open the run and 2 to close it, so the fixed cost
per frame is 3 + 3/N for a run of N. Predicting a run of three - the depth of the
chip's RX FIFO - gives 4 bytes a frame and a comfortable margin. The measurement
says the gain is about a third of that, which means runs are mostly of one or
two: the drain loop is woken per frame and empties what is there rather than
waiting for company.

What it gives up is immediate resynchronisation. Only a run's first byte is a
marker, so a reader thrown off by a damaged byte waits for the next run instead
of the next frame. In this direction at 500000 baud no damaged byte has ever been
measured here; at 1 MBaud they are constant, and 1 MBaud does not work anyway.

#### CSN, and the difference between an Arduino call and a register

`digitalWrite` looks up the port and bit for a pin in three PROGMEM tables,
checks whether a timer owns the pin, and masks interrupts around the write. That
is about 4 us, and a frame comes out of the FIFO through four SPI transactions,
so eight of them. The pin stays configurable - only the translation stops
happening a hundred thousand times a second, because the port and bit are
resolved once in `hwset`.

| | `us_in` | seen at 2 Mbps, receiver printing |
|---|---|---|
| `digitalWrite` on CSN | 223 us | 439-441 of 512 |
| **direct port write** | **191 us** | **449-454 of 512** |
| plus `SPCR` written directly instead of `SPI.beginTransaction` | 191 us | 449-453 |

The third row is a null result and was reverted. Without a registered interrupt
`SPI.beginTransaction` compiles to the same two register writes on this
architecture, so hand-rolling them buys exactly nothing and gives up the one
thing the library call exists for.

Of the 191 us that remain, about 39 are the bus clocking 37 bytes at 8 MHz. Most
of the rest is `SPI.transfer` polling until each byte is done, and that is
inherent: the ATmega's SPI is not double-buffered, so every byte is written,
waited for and read. There is no third layer to peel.

#### How much of it is Arduino

The SPI clock was at 4 MHz where the chip allows 10 and the ATmega can drive 8.
Raising it is one line, and it is also the cleanest way to separate bus time
from library overhead - halving the clock rate halves the former and leaves the
latter alone.

`us_in`, the drain loop's own measure of everything up to the flush, fell from
**190 to 143 us** a frame. Of those 143, about 41 are the bus actually clocking
41 bytes. **The other hundred are Arduino**: `digitalWrite` on CSN costs some
4 us and there are two per transaction, plus `beginTransaction`,
`endTransaction`, and a polling loop per byte. Direct port writes would take
most of that back.

But it is not on the critical path of anything measured here. A transfer ran at
1.28 ms a frame before the change and 1.28 ms after - the sending side is bound
by the serial line and by air that does not overlap it, and 47 us against
1290 does not show. The receive path gained the 47 us against a per-frame cost
of some 830, most of which is serial output.

So the honest ranking of what is left, by measured headroom rather than by how
interesting it is:

| | worth | blocked on |
|---|---|---|
| overlap air with wire on the acknowledged path | 1.29 → ~0.85 ms | the exactness of `sent` |
| drop the per-payload flush on the receive path | 2.4 → 0.83 ms received | reproducing the duplicate fault |
| direct port writes instead of `digitalWrite` | ~60 us a frame | nothing, but it is not binding |
| a bigger payload | – | 32 bytes is the chip's maximum |
| a faster serial rate | negative | measured: 1 MBaud costs more than it saves |
| the write direction's missing 9-11 % | 0.85 → 0.77 ms | not reachable from here: [identical from Python and from C++](#the-same-wire-without-the-analyser) |

The clock stays at 8 MHz because it is free and correct, not because it helped.

#### Picking up where a broken run stopped

An acknowledged run reports how many frames the radio confirmed, and a
confirmed frame is one the receiver has. So a run that ends early continues
from exactly there, with nothing sent twice and nothing skipped, up to
`SEND_RETRIES` times. The reply then carries ` resumed=<n>`.

This is what would make a raised serial rate survivable, and measuring it is
how the rate was shown not to be worth raising. It earns its place anyway: a
record whose checksum fails is otherwise a whole transfer lost.

**Do not verify a transfer from the capture.** Sixteen kilobytes sent three
times at 250 kbps came back `sent=512/512 ack=yes failed=0` every time - the
radio confirmed every frame - while the observing dongle's own history held 512
frames twice and 510 once. Acknowledgement is what says the bytes arrived; the
sniffer's history is a separate and slightly lossy path, and reassembling it is
a check on the observer, not on the transfer.

**`ack` changes both the speed and the meaning.** Acknowledged, the dongle
confirms every frame and the host waits for that confirmation before writing
the next payload — it has to, because a frame being retried keeps the dongle
out of its serial port for longer than its input buffer can cover. That costs
speed and buys certainty:

| | per frame | of 4096 bytes, at a second dongle |
|---|---|---|
| `noack`, full rate | 1.6 ms | ~40 % |
| `ack` | 5.7 ms | 100 %, byte-for-byte |
| one `tx` per frame | 20 ms | 100 % |

Read that right: it was measured **dongle to dongle**, so the receiving side is
the analyser itself. The loss in the fast case is neither the radio nor the
host — it is the *receiving dongle*, which writes about 85 characters per frame
to its own serial port. At 500 kBaud that is roughly 1.7 ms, exactly what the
fast sender leaves it. Send faster than the receiver can talk and the surplus
is gone before anything can retry it, which is also why `fifofull` stays at
zero: the overflow was never in the radio's FIFO.

So the 40 % is a property of **this instrument**, not of the link. A receiver
that does not have to narrate every frame over a serial line — a real product,
an embedded node — may well take the full rate; that has not been measured
here. What generalises is the shape of it: `noack` throws a frame once and
never learns whether it landed, so the fast figure is whatever the slowest
stage downstream can absorb. `ack` sidesteps the question, both by pacing the
sender to a rate the receiver holds and by retrying what still does not land.

With `dpl=0` every frame is padded to `plsize`, so a transfer's last frame
carries filler and the receiver has to know the real length. With `dpl=1` the
lengths are exact and a file comes back out at its original size.

### The flush, and why it is now conditional

`rxmode 2` flushed the RX FIFO after **every** payload, which discards whatever
arrived while the firmware was busy and so limits a dongle to one frame per
drain pass - measured, half the reception rate. It exists for the duplicate
fault, and that fault has one stated condition, quoted from the section below:
a payload that fills the 32-byte slot comes out exactly once; one that does not
leaves the FIFO a payload out of step.

So the flush is now spent where that condition holds and not where it does not.
**A full 32-byte slot skips it**; anything shorter is flushed as before.

| | before | after |
|---|---|---|
| 512 frames of 32 bytes, `bin`, `rxmode 2` | 50 % | **99 %** |
| 60 events x 3 copies at `plsize` 8 and 16 | 180/180, no duplicates | 180/180, no duplicates |
| RotRemote, 20 clicks | 20 ids, no duplicates | 20 ids, no duplicates |

### The duplicate fault, re-measured — and not reproduced

The flush in `rxmode 2` costs half the reception rate (see
[`format bin`](#and-after-that-the-flush)), so whether it is still needed is
worth money. It was re-measured on **both dongles**, each as receiver with the
other sending, classifying every received payload as correct, a duplicate of
one already seen, one from an *earlier* run (the FIFO-RAM signature), or
foreign:

| varied | values |
|---|---|
| receiver | COM18, COM25 |
| `rxmode` | 0, 1, 2 |
| payloads | static 8/16/32, dynamic mixed 4..32 |
| timing | 20 ms apart, back to back (1.5 ms), 3 copies 5 ms apart |
| auto-ack | off, on |

**Every one of those returned zero duplicates and zero stale payloads.**

That is not permission to remove the flush. `rxmode 0` returned zero too — and
`rxmode 0` is the mode documented above as producing duplicates *and*
misalignment, so a bench on which it comes out clean is a bench that cannot
currently reproduce the fault at all. The negative result says the test is
blind, not that the fault is gone.

It has since been tried against the sender it was found with - a real
`RotRemote_BTHome`, triggered by toggling DTR on its port - and does not appear
there either: 25 clicks, 25 packet ids, no excess copies, in every `rxmode`
including the one documented as worst. That sender emits **static 32-byte**
payloads, so short and dynamic ones still have to come from a dongle, which is
how they were measured above.

Two false alarms are worth recording, because they were the same mistake twice.
A third device transmits on this channel and address, and counting frames
rather than identifying them attributed its traffic to the device under test -
once as "515 frames for 512 sent", once as "9 excess copies from the remote".
Both vanished on filtering by sender. **Any measurement of invented frames has
to identify payloads.**

The fault therefore stays unreproduced, which is why the flush was made
conditional rather than removed: a fault living in the chip's FIFO RAM, which
outlives a reset, is not disproved by a bench that cannot summon it - and the
payloads it was stated for are exactly the ones still protected.

**Beware of a third device.** Frames arriving on channel 90 at address
`43:54:48:4D:45` that nobody on the bench sent are not necessarily invented -
listening for 60 seconds with both dongles silent produced 36 of them, in
groups of three, decoding as ordinary BTHome sensor readings. An earlier
"515 frames for 512 sent" here was three of those, and was wrongly read as the
duplicate fault. Any measurement of invented frames has to identify payloads,
not count them.

Both boards measured the same throughout, within one or two frames of each
other on every run.

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

That switch is also the cheapest throughput lever there is, and it was measured
rather than assumed. A hundred events, each sent three times back to back:

| | frames through | events seen |
|---|---|---|
| `repeats 1`, `text` | 34 % | 98-99 % |
| `repeats 1`, `bin` | 50 % | 99-100 % |
| `repeats 0` | one per event, all of them | **100 %** |

Read that carefully, because it is not the result one expects. With `repeats 1`
two thirds of the frames never arrive - and almost every event is still seen,
because three copies of it were sent and only one has to survive. **Turning
repeats off does not recover events that were being missed.** The redundancy
was already covering the loss.

What it does is remove two thirds of the traffic at no cost to what is
observed, which is three times the headroom before anything starts being
dropped for real - a second sender, a denser burst, a slower decoder.

The cost is exact and worth stating: a run of identical payloads is
indistinguishable from a sender repeating one event, so **a transfer must run
with `repeats 1`**. In the measurement above `repeats 0` turned 300 frames into
101. If those had been a file, two thirds of it would be gone.

#### Why a sender repeats at all, when the chip can acknowledge

Worth separating, because they are easy to confuse: the repeats above are the
*sender's*, three separate packets carrying one event. The chip's auto-retransmit
is a different mechanism - it resends until acknowledged and then stops.

If both are available, acknowledgement wins outright. Measured over a
deliberately poor link (2 Mbps at `pa=min`, where a single packet lands 46 % of
the time):

| | events delivered | packets on air |
|---|---|---|
| one blind packet | 46/100 | 100 |
| three blind copies | 96/100 | 300 |
| one packet, acknowledged | **100/100** | **191** (91 retransmissions) |

Complete delivery for a third fewer packets than blind repetition, and on a good
link the same run cost **zero** retransmissions and 100 packets - a third of the
traffic for the same result. Blind repeats pay their price always;
acknowledgement pays only when the air makes it necessary.

So why do the senders this tool exists to watch repeat blindly? Because
acknowledgement needs **exactly one** receiver that owns the address and answers
for it. A sender broadcasting to several listeners cannot use it - two receivers
answering would collide - and this analyser is very often a third party
overhearing somebody else's traffic, which is a role that cannot acknowledge
anything.

That has a consequence for reading the table above: our receiving dongle *was*
the acknowledging party, which is why it saw 100/100. An analyser merely
listening in on an acknowledged link gets no benefit from those acknowledgements
at all - it would be back at the 46 %. Confirming that wants a third dongle, and
this bench has two, so it is reasoning rather than measurement.

The practical upshot: `repeats 0` is a filter for senders that repeat blindly. A
sender that acknowledges has no repeats to filter, and the switch does nothing.

No such lever exists for the padding. A `dpl=0` frame is padded to `plsize` on
the *sender's* side - the BTHome frames on this bench end in twelve `FF` bytes
because their sender pads them - so a receiver cannot decline to carry it.

> Why the firmware timestamps: host arrival times cannot resolve the gap between
> a sender's repeats. Measured on the host the same three repeats came out 0.5
> and 0.3 ms apart; on the dongle's clock they are **5 and 6 ms**. The host was
> timing how fast it drained three lines the OS had already buffered, not the
> air. Anything that reasons about repeat spacing or burst timing has to use
> `t=`.

### `format bin`: the same frames, in half the time

A readable frame line costs about **4.3 ms**, which caps a dongle at some 230
frames a second — measured as the closest spacing at which nothing is lost, by
sending firmware-timed bursts at a decreasing gap. Only about 1.7 ms of that is
the serial line at 500 kBaud; the rest is a 32-byte payload becoming 64 hex
characters, one `Serial.print` per byte.

`format bin` sends each frame as a record instead:

| offset | | |
|---|---|---|
| 0 | 1 byte | `0x01`, which begins a record |
| 1 | 1 byte | payload length before suppression, 1..32 |
| 2 | 1 byte | pipe in bits 2–0, suppressed run in bits 7–3 |
| 3 | 2 bytes | the low 16 bits of `millis()`, little-endian |
| 5 | *stored* bytes | the payload, less any repeated tail |
| 5+*stored* | 1 byte | the repeated byte — only when the run is not zero |
| … | 1 byte | CRC-8 over the payload **as it was**, all *len* bytes |
| … | 1 byte | `
` |

So 39 bytes for a payload with nothing to suppress and 28 for one padded the way
this bench's BTHome sender pads — against ninety-odd characters for the readable
line — assembled once and handed over in a single `Serial.write`. The checksum
covers **exactly** the bytes that `crc=` covers in the readable line, so `intact`
means the same thing either way, and a host that rebuilds the tail wrongly fails
the check rather than passing it.

Three things were paid for here, and the receiver's throughput is what asked for
each of them:

**The timestamp is two bytes, not four.** The host puts the high bits back from
its own clock: it knows roughly what the dongle's counter reads, so it takes the
value congruent to those 16 bits that lies nearest, and would have to be out by
more than half the 65.536 s wrap to pick wrong. `info` reports `ms=` so a host
that has just connected — or has heard nothing for hours — anchors on the
dongle's own answer instead of guessing. **This is deliberately not a delta.**
Every record still carries an absolute value, so frames lost in between cost
nothing; a delta would have shifted every timestamp after a lost frame, and
repeat spacing is the one thing this stamp exists to measure.

**Pipe and run count share a byte**, three bits and five.

**A repeated tail is sent as one byte and a count.** Senders that pad a static
payload out to 32 bytes are most of what this dongle ever sees. Measured over 54
real frames off the air, against two synthetic corpora for contrast:

| encoding | sniffed sensor frames | random bytes | English text |
|---|---|---|---|
| before | 41.0 B | 41.0 B | 41.0 B |
| **trailing run suppressed** | **30.0 B** | 41.0 B | 41.0 B |
| general run-length | 34.0 B | 41.1 B | 41.0 B |
| both | 32.0 B | 41.1 B | 41.0 B |

General run-length is *worse* than suppressing only the tail, which is why the
firmware does not do it: an escape byte costs two bytes every time its own value
appears in the data, and the short runs inside a BTHome payload do not repay it.
Suppressing only the tail needs no escape and cannot expand a record by more than
one byte.

`0x01` can introduce a record unambiguously because nothing else this firmware
prints lies outside printable ASCII — replies, warnings and the greeting stay
readable in binary mode, and only frames change shape. The length says where a
record ends, so a payload byte that happens to be `0x01` or a newline is data
like any other. A reader that mis-syncs takes a wrong length, fails the
checksum, and hunts for the next `0x01`.

Measured, same 320-frame bursts at a decreasing gap:

| spacing | `text` | `bin` |
|---|---|---|
| 1.3 ms | 43 % | 50 % |
| 2.3 ms | 62 % | 99 % |
| 3.3 ms | 82 % | **100 %** |
| 4.3 ms | **100 %** | 100 % |

So the ceiling moves from ~230 to ~400 frames a second — a real 1.7×, and less
than halving the bytes might suggest. What is left is no longer the protocol:
about 1.7 ms per frame goes on work that does not depend on the output shape at
all (the SPI read at 4 MHz, the per-payload `FLUSH_RX` that `rxmode 2`
performs, the repeat check, the LED writes).

#### What the shorter record bought, and where it ran out

512 frames sent unacknowledged with the receiver printing every one of them,
which is the case where the receive path is the constraint:

| air rate | payload | record | seen before | seen after |
|---|---|---|---|---|
| 1 Mbps | random, no tail | 41 → 39 B | 426/512 | 440/512 |
| 1 Mbps | 20 data + 12 × FF | 41 → 28 B | ~426/512 | **512/512** |
| 2 Mbps | random, no tail | 41 → 39 B | 422/512 | 440/512 |
| 2 Mbps | 20 data + 12 × FF | 41 → 28 B | ~422/512 | **512/512** |

The split is the whole point, and the arithmetic predicted it. A frame arrives
every 850 us; writing a record out costs 20 us a byte plus about 210 us of SPI
read and drain loop. At 41 bytes that is 1030 us and the dongle falls behind by
18 %. At 39 it is 950 — better, still behind. At 28 it is 770, and the loss does
not shrink, it **disappears**.

And the wall behind it: **the payload alone is 640 us**, so with the 210 us of
work around it a 32-byte frame needs 850 us to be narrated — exactly the arrival
interval, with nothing left for framing at all. Arbitrary payloads at 1 or
2 Mbps therefore cannot be printed frame-for-frame at 500000 baud, by this
firmware or any other. That case is `format none`, or acknowledged transfer,
which paces the sender to what the far end can take.

**A record is not a line.** It carries no terminator that means anything - the
length is what says where it ends - and a payload byte may be `0x0A` or `0x01`
like any other. In a serial console the frames are noise, and that is inherent:
this mode exists to stop spending nine tenths of a millisecond per frame making
them readable.

The trailing `
` is therefore not a terminator, and a reader must not treat it
as one. It buys one thing, for one byte in ninety: whatever the firmware prints
next starts on its own line. Without it a reply lands glued to the tail of a
record -

```
...Èð«NRF24ANALYSER fw=3.8.0 api=5 state=listening ...
```

- which is exactly the state somebody is in when they have opened a terminal to
find out why a binary session is misbehaving. Commands, replies, `WARN` lines
and the greeting are all still ordinary ASCII lines; only frames change shape.

#### And after that, the flush

With the readable line gone the ceiling moves again, and the next thing in the
way is not the protocol at all. `info` reports what a frame costs the firmware,
averaged since the last `listen`:

| | `us_in` (SPI, registers) | `us_out` (onto the wire) |
|---|---|---|
| `text` | 189 us | 3991 us |
| `bin` | 190 us | 642 us |

189 microseconds of SPI against four milliseconds of printing: the readable
line was never held up by the radio. But 832 us a frame would allow 1200 a
second, and `bin` was measured at 400. The difference is `rxmode 2`, which
**flushes the RX FIFO after every payload** — so whatever arrived while the
firmware was busy is discarded rather than queued, and the dongle can absorb
exactly one frame per pass. Against `rxmode 1`, which does not flush:

| spacing | `bin` + `rxmode 2` | `bin` + `rxmode 1` |
|---|---|---|
| 1.3 ms | 50 % | **100 %** |
| 2.3 ms | 100 % | 100 % |

1.3 ms is as fast as a 32-byte frame can be sent at 250 kbps, so without the
flush the receiver keeps up with everything the air can carry.

The flush is not gratuitous — it is the one measure that fixes the
[duplicate-frame fault](#duplicate-frames-and-the-rxmode-switch), and `rxmode 1`
is not a setting to run. But that fault was only ever observed for payloads
**shorter than the 32-byte slot**; one that fills the slot comes out exactly
once. Flushing only after a short payload would therefore cost nothing in
correctness and lift the full-slot case to the air rate. That is a change to
the one behaviour in this firmware arrived at purely empirically, so it is
written down here rather than made quietly.

#### `baud`, and why it buys nothing yet

`baud 250000|500000|1000000|2000000` raises the serial rate for a session; a
reset returns to 500000, so a host that does not know the command - or a
checkout that predates it - can always open the port. The reply goes out at the
old rate and is flushed before the switch, so the host knows exactly when to
follow, and the port is reconfigured rather than reopened (reopening pulls DTR,
which resets the dongle and loses its configuration).

Measured against the greeting, 100 exchanges each:

| rate | intact |
|---|---|
| 500000 | 100/100 |
| 1000000 | 99/100 |
| 2000000 | **0/100** |

2 MBaud does not work on this hardware at all. 1 MBaud does, at about one
corrupted byte in a hundred lines - and **it changes nothing end to end**: a
512-frame transfer arrived 51 % at 500000 and 50 % at 1000000. That is not a
disappointment, it is the earlier finding restated. Once `format bin` removed
the printing, the serial line stopped being what binds; the flush did. Doubling
a rate that is not the constraint buys exactly nothing, and here it costs 0.8 %
of frames to their checksum.

The command stays because it is the instrument that established this, and
because the moment the flush is addressed the line becomes the constraint again
- 0.66 ms of the remaining 0.83 ms per frame is wire time.

| 512 frames, sender at 1.5 ms/frame | received |
|---|---|
| `text`, `rxmode 2` | 35 % |
| `text`, `rxmode 1` | 35 % |
| `bin`, `rxmode 2` | 50 % |
| `bin`, `rxmode 1` | **100 %** |

Both halves have to go for the ceiling to move: the readable line is line-bound
whether or not it flushes, and the binary record is flush-bound until the flush
goes. (`rxmode 1` also returned 515 frames for 512 sent - the duplicate fault,
on full 32-byte slots, where it was not expected. Not a setting to run.)

**It is off after every reset, and it is not hidden**: `info` reports
`format=bin|text`. The default stays readable because that is what makes this
dongle debuggable with nothing but a terminal — turn it on when throughput
matters, and `nrf24_dongle.py` turns the records back into the very lines the
firmware would have printed, so nothing above the driver can tell the
difference.

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

**What never arrived is counted in Python**, and the browser only draws it. It
used to be worked out twice by two different methods — which, in a tool built to
measure frame loss, is the worst kind of bug: two believable numbers about the
same traffic. They are still two numbers, deliberately, because they answer
different questions and are shown in different places:

- the **`−n` on a row** is local and immediate: how far that frame's id was
  ahead of the furthest one seen from its sender. It can overstate, because ids
  arrive out of order and a straggler may fill the gap a moment later.
- the **total in the header** is the honest one and needs the whole set: the
  counter values that never appeared at all, over the smallest arc containing
  every id seen. That one takes the straggler back.

They are never added together, and both come from `Loss` in `nrf24web.py` — the
same class that answers `/api/capture`, so the table and an agent's capture
summary cannot disagree about the same traffic any more.

**Columns** switch off individually, from a menu next to the filters, and the
choice is remembered. They are hidden by a stylesheet rule rather than by
leaving the cells out: the row is addressed by position in several places —
which cell carries the pipe, which one the sender — and a table whose column
count depended on what was switched on would have every one of those doing
arithmetic about it. The last visible column cannot be switched off, because a
table with no columns is not a view of anything and finding the way back out
means guessing which invisible thing to click.

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
Selecting a row shows its **decoded fields** and its **hex dump**, one tab each
rather than side by side: the strip is a few lines tall, and halving its width
left neither half wide enough to read. Frames the decoder objected to are drawn
in red.

**Ctrl-click or right-click a second row and those same two tabs compare two
frames instead of showing one.** Decoded stacks each differing field's two
values one under the other and colours the part that moved rather than the whole
line - a decoder's value is a list, and marking all of "Battery 73; Temperature
24.59; Humidity 50.74" because one reading changed says only "this line
differs", which the reader already knows from it being listed at all — never side by side, which put them a field width
apart and left the eye to travel — and names the fields that match on a single
`identical` line, because the question a comparison is asked is what differs and
eight lines of "the same" is where that answer goes to hide. Raw lists the differing bytes as
`byte 8  0C → 0E`, four to a line, then both frames whole, one line each, so the
columns stand under one another and the differences sit in their context. Two
bytes written that way are three characters apart, which is why they are not
stacked the way the fields are: there the two values sat a field width apart and
the eye had to travel. Thirty-two bytes is ninety-six characters and fits the width this pane
has — the eight-byte blocks with a marker row under them predated the tabs, and
spread four differences over a dozen lines. Both tabs carry the same headline
saying how far apart the two are. Comparing two receptions is what this tool exists for — invented frames
showed up because two receivers disagreed — and it belongs in the same place and
the same reading order as everything else. It was a modal dialog for one
afternoon; a window is a second home for one half of the same question, and this
way the byte view and the decoded view are one tab apart rather than one window
apart. A length difference pads rather than stops the comparison, because that
is a finding too. The log and the free-text command line share
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
| `POST /api/send` | transmit a whole run through one [`txseq`](#sending-more-than-one-frame-txseq): `{"address", "payloads": [hex, …]}` or `{"address", "data": base64, "size": 1..32}`, plus `"ack"`. Answers `{"sent", "of", "bytes", "reply", "means"}` — `means` spells out what `sent` is worth, because without `ack` nothing confirms arrival. Progress arrives on the event stream as `{"type":"send"}`; `POST /api/send/cancel` stops a run |
| `POST /api/restart` | answer, then replace this process with one running the code on disk, handing it the open port. Resets the dongle |
| `POST /api/capture` | block for `seconds`, return the window's frames + stats |
| `POST /api/parser` | switch decoder, returns the history re-decoded |

### What a capture is, exactly

Anyone reassembling a transfer out of captured frames needs to know what the
capture is and is not:

- **`raw` is the whole payload**, every byte the radio handed over, in hex. The
  decoder's reading sits beside it and never replaces it — a frame no decoder
  understands still carries its bytes.
- **Frames are in arrival order**, as the dongle handed them out.
- **Nothing is deduplicated** while `repeats 1` is set (the default). `repeats
  0` suppresses identical back-to-back frames, which is a display convenience
  and destroys exactly the information a transfer needs — leave it at `1`.
- **`/api/frames` is capped at 5000** frames and drops the oldest beyond that.
  `/api/capture` returns its whole window uncapped: for a long transfer, use
  the capture window rather than reading back history.
- **A frame whose checksum fails is kept but not decoded.** It arrives with
  `flagged: true` and a `cells.data` saying it was corrupted between radio and
  host, and its `raw` is still there — so it is visible and countable, but
  never quietly reassembled into a file as if it were the bytes that were
  sent. Check `flagged` before concatenating.

What is *not* guaranteed is that everything transmitted was captured. Nothing
in a passive receiver can promise that; see the loss table under
[`txseq`](#sending-more-than-one-frame-txseq).

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
  platformio.ini              build environments (nano / nano_old / serialbench)
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
  bench/serialbench/          a dongle with no radio in it, to measure the wire
  bench/serialtest.py         drives it: read, write, round trip, lockstep
  bench/serialtest.cpp        the same measurements without Python in the way
```

`bench/serialbench` replaces the firmware on whichever dongle it is flashed to,
so a dongle is on loan for as long as it runs and has to be flashed back with
`pio run -e nano -t upload` afterwards. The C++ twin is built by hand and is not
in the tree:

```
g++ -O2 -std=c++17 -o bench/serialtest.exe bench/serialtest.cpp
```
