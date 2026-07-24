# nrf24-sniffer

A debug / sniffer tool for the **BTHome-over-nRF24** radio protocol, in two parts:

1. **PlatformIO firmware** (`platformio.ini`, `src/`, `include/`) for the
   ATmega328P + CH340 + nRF24L01 USB dongle. Exposes a line-based serial protocol
   to configure the radio, dump received frames, scan for channel activity, and
   transmit arbitrary payloads.
2. **`nrf24term.py`** — a Python serial terminal (REPL) that streams live output,
   forwards typed commands, decodes BTHome v2 frames, applies config presets, and
   can log to a file.

It exists to validate and debug the wire format spoken by
[`RotRemote_BTHome`](../../active/RotRemote_BTHome) and received by the ESPHome
component: a 4-byte sender id followed by a BTHome v2 service-data payload, sent
on a shared broadcast address with NO_ACK.

## Hardware

USB dongle: **ATmega328P + CH340 + nRF24L01**. Pin map (`include/pins.h`):

| Signal        | ATmega pin |
|---------------|------------|
| Radio CE      | D9         |
| Radio CSN     | D10        |
| Radio IRQ     | D2         |
| LED (TX act.) | A1         |
| LED (RX act.) | D8         |
| SPI           | D11/D12/D13 (hardware SPI) |

LEDs are wired **active-low**. The RX LED blinks on every received frame, the TX
LED on every transmit. At boot the RX LED blinks **once** if the radio is
detected, **twice** if not.

## Firmware (PlatformIO)

The dongle is an ATmega328P at 16 MHz with a CH340 USB-serial bridge and a serial
bootloader, so it flashes over USB like an Arduino Nano. CH340 clones ship with
one of two bootloaders — the `platformio.ini` provides an environment for each:

| Environment | Board              | Bootloader / upload baud |
|-------------|--------------------|--------------------------|
| `nano`      | `nanoatmega328new` | new (115200) — default   |
| `nano_old`  | `nanoatmega328`    | old (57600) — fallback   |

The dongle port is set to **COM18** in `platformio.ini`; change `upload_port` /
`monitor_port` if it enumerates elsewhere (`pio device list`).

```bash
pio run -e nano                    # build
pio run -e nano -t upload          # flash (new bootloader)
pio run -e nano_old -t upload      # flash (old bootloader, if the above times out)
pio device monitor -e nano         # raw serial monitor @ 115200
```

The only library dependency is `nrf24/RF24` (TMRh20), pulled automatically by
PlatformIO. Build size on the ATmega328P: **flash ~8.6 KB (28%)**, **RAM ~478 B
global (23%)**.

## Serial protocol

**500000 baud**, one command per line (`\n` terminated, `\r` ignored). Every
command is answered with `OK ...` or `ERR ...`.

> Why 500000: the serial link, not the radio, is the bottleneck when a sender
> repeats each event a few ms apart. At 115200 one 17-byte frame line takes
> 5.6 ms, so a 3-repeat burst needed ~17 ms to print while arriving in ~10 ms —
> the 3-deep RX FIFO overflowed and frames were silently dropped. 500000 baud is
> an exact divisor at 16 MHz (0% error, cleaner than 115200), the compact hex
> output below and a 256-byte TX buffer bring one line down to ~1 ms.

| Command                       | Meaning                                                   |
|-------------------------------|-----------------------------------------------------------|
| `ch <0-125>`                  | RF channel                                                |
| `rate <250\|1000\|2000>`      | data rate in kbps                                         |
| `crc <0\|8\|16>`              | CRC length in bits (0 = disabled)                         |
| `aw <3\|4\|5>`                | address width in bytes                                    |
| `pipe <0-5> <XX:XX:...>`      | enable a reading pipe with the given address              |
| `pipe <0-5> off`              | disable a reading pipe                                    |
| `ack <0\|1>`                  | auto-ack off / on                                         |
| `dpl <0\|1>`                  | dynamic payloads off / on                                 |
| `plsize <1-32>`               | static payload size (used when `dpl 0`)                   |
| `pa <min\|low\|high\|max>`    | PA level                                                  |
| `repeats <0\|1>`              | `0` suppresses identical back-to-back frames (default `1`) |
| `listen`                      | enter RX mode                                             |
| `stop`                        | leave RX mode                                             |
| `info`                        | print current config + `radio.isChipConnected()`         |
| `scan [passes]`               | energy scan across all channels (default 64 passes)       |
| `tx <XX:..:XX> <hex...> [ack\|noack]` | transmit a payload (default `noack`)              |
| `help`                        | list commands                                             |

Addresses and payloads are hex bytes separated by `:` `,` or `.`; payload bytes
may also be space-separated. The **leftmost** hex byte is address byte 0 (matching
the sender's address array), so `pipe 1 42:54:48:4D:45` is ASCII `"BTHME"`.

### RX output

While listening, each frame is printed as one parseable line with the payload as
compact hex (no separators, to keep the line short):

```
RX p1 len=16 4D565202D2FC44004501350C8B093A01
```

`p<pipe>` is the pipe number, `len=<n>` the payload length, then `2*n` hex chars.

A sender that repeats each event emits several identical frames. `repeats 0`
prints only the first of a run (identical payload within 500 ms) — useful when
you care about events rather than the on-air repetition.

If the RX FIFO was already full when the firmware got to it, frames were dropped
by the chip and a warning line is emitted:

```
WARN fifo-full
```

That line is the discriminator when packets go missing: **with** it the host
could not keep up, **without** it the loss happened on the air.

### Scan output

`scan` reports channels where RF energy was detected (nRF24 RPD):

```
SCAN passes=64
SCAN ch=100 hits=7
...
OK scan done
```

Note: RPD is pure energy detection, independent of the configured address/CRC.

### Default configuration (at boot)

Matches the BTHome-over-nRF24 target protocol:

- channel **100**, **250 kbps**, **CRC16**, address width **5**
- pipe 1 = `42:54:48:4D:45` ("BTHME"), all other pipes off
- dynamic payloads **on**, auto-ack **off**, PA level **low**

RX mode is **off** at boot — send `listen` (or `:preset bthome`) to start.

> nRF24 hardware note: reading pipes 2–5 share address bytes 1–4 with pipe 1 and
> differ only in byte 0. Give pipe 1 the full address; for pipes 2–5 only byte 0
> is applied by the hardware.

## Python terminal

Only dependency is pyserial:

```bash
pip install -r requirements.txt
python nrf24term.py COM18            # plain terminal
python nrf24term.py COM18 --pretty   # decode RX frames as BTHome v2
```

A reader thread prints everything from the dongle; typed lines are forwarded
verbatim. Local commands (handled by the terminal) start with `:`:

| Local command    | Effect                                                    |
|------------------|-----------------------------------------------------------|
| `:pretty on\|off`| toggle BTHome pretty-printing                             |
| `:preset bthome` | apply the full BTHome config and `listen` in one step     |
| `:scan [passes]` | run a channel scan (renders each hit as an activity bar)  |
| `:log <file>`    | tee all dongle output to a file                           |
| `:log off`       | stop logging                                              |
| `:help`          | show local help                                           |
| `:quit` / `:exit`| close the terminal                                        |

Opening the port resets the ATmega (DTR), so the tool waits ~2 s and then sends
`info`. The default baud rate is 500000, matching the firmware; override with
`--baud` if you reflash it differently. The BTHome decoder accepts both the
compact hex format and the older space-separated one, so old logs stay readable.

### Pretty-print format

With `--pretty` (or `:pretty on`), a frame laid out as
`[4-byte sender id][BTHome v2 service data]` is decoded:

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

Service data must start with the BTHome UUID `D2 FC` followed by a device-info
byte. Decoded objects: packet id (`0x00`), battery % (`0x01`), voltage (`0x0C`,
uint16 LE × 0.001 V), button event (`0x3A`), dimmer event (`0x3C`), command
event (`0x3B`) and text / raw (`0x53` / `0x54`). The k-th `0x3A`/`0x3C` object
maps to button/dimmer *k*. Unknown object ids can't be length-decoded, so the
parser prints the remaining bytes as raw hex and stops.

**Not every object is fixed length** — the length can depend on the value:

| Object | Layout |
|--------|--------------------------------------------------------------|
| `0x3C` dimmer  | `3C 00` for `None` (**no** step byte), `3C <dir> <steps>` otherwise |
| `0x3B` command | `3B <argument count> <opcode> <arguments...>`                 |
| `0x53` / `0x54`| `<id> <length> <bytes...>`                                    |

The dimmer case matters for multi-dimmer packets: a rotation on dimmer 2 is sent
as `3C 00 3C 02 01` — instance 1 padded with `None`, then the real event.
Assuming a fixed 2-byte dimmer value swallows the second object id as a step
count and desynchronises the rest of the packet.

## Verifying against the real remote

1. Flash and connect: `pio run -e nano -t upload`, then
   `python nrf24term.py COM18 --pretty`. `info` should report `chip=connected`
   and the default config.
2. `:scan` — with 2.4 GHz WiFi around you should see hits clustered on some
   channels; this confirms the radio is alive.
3. `:preset bthome` (or `listen`). Trigger the `RotRemote_BTHome` remote (rotate
   / click) — live `RX p1 ...` lines should appear and, in pretty mode, decode to
   sender id + BTHome objects with the matching battery/voltage and event.
4. Emulate a sender toward a receiver or a second dongle, e.g.
   `tx 42:54:48:4D:45 4D 56 52 02 D2 FC 44 00 01 01 57 noack`.

### Measuring packet loss

The BTHome packet id is a free-running per-event counter, so gaps in it measure
loss directly. Log a session (`:log run.txt`), then count distinct packet ids and
their gaps. Interpretation:

- `WARN fifo-full` present → the host is the bottleneck; raise the baud rate
  further or use `repeats 0`.
- No warnings, but whole events (all repeats) missing → loss on the air. If
  complete events vanish far more often than independent per-packet loss would
  predict, the interference is **bursty**: all repeats fall inside one short
  outage. Spreading the sender's repeats further apart in time helps more than
  simply sending more of them.

## Layout

```
nrf24-sniffer/
  platformio.ini              build environments (nano / nano_old)
  include/pins.h              board pin map
  include/RadioController.h   radio wrapper + RadioConfig
  include/CommandParser.h     serial command parser
  src/RadioController.cpp     radio init, RX drain, tx, scan, info
  src/CommandParser.cpp       line protocol dispatch
  src/main.cpp                setup/loop wiring
  nrf24term.py                serial terminal + BTHome decoder
  requirements.txt            pyserial
  README.md
```
