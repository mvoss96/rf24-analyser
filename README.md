# nrf24-sniffer

A debug / sniffer tool for the **BTHome-over-nRF24** radio protocol, built from
two parts:

1. **`firmware/Nrf24Sniffer`** — Arduino firmware for the ATmega328P + CH340 +
   nRF24L01 USB dongle. Exposes a line-based serial protocol to configure the
   radio, dump received frames, and transmit arbitrary payloads.
2. **`nrf24term.py`** — a small Python serial terminal that streams live RX
   lines, forwards typed commands, and can pretty-print BTHome v2 frames.

It exists to validate and debug the wire format spoken by
[`RotRemote_BTHome`](../../active/RotRemote_BTHome) and received by the ESPHome
component: a 4-byte sender id followed by a BTHome v2 service-data payload, sent
on a shared broadcast address with NO_ACK.

## Why a new firmware

The dongle originally ran the `nrf24USB` firmware (in
`archive/smart-home-nrf/nrf24USB`), which is built on **NRFLite**. NRFLite uses
1-byte radio ids and derives the real 5-byte pipe addresses internally, so it
cannot listen to raw RF24 traffic that uses explicit 5-byte addresses. This
firmware is a clean rewrite on the **RF24 library (TMRh20)**, keeping only the
board's pin map and startup-blink idea.

## Hardware

USB dongle: **ATmega328P + CH340 + nRF24L01**. Pin map is inherited from the old
`nrf24USB` firmware (`include/config.h`):

| Signal        | ATmega pin |
|---------------|------------|
| Radio CE      | D9         |
| Radio CSN     | D10        |
| Radio IRQ     | D2         |
| LED (TX act.) | A1         |
| LED (RX act.) | D8         |
| SPI           | D11/D12/D13 (hardware SPI) |

LEDs are wired **active-low**. The RX LED blinks on every received frame, the TX
LED on every transmit.

## Board / toolchain

The old `platformio.ini` targets `board = ATmega328P` (`platform = atmelavr`,
Arduino framework) with a CH340 USB-serial bridge — i.e. a **16 MHz ATmega328P
with a standard serial bootloader**, flashed over USB. The firmware here is a
native Arduino sketch (no PlatformIO); build it with `arduino-cli`.

The default FQBN below is `arduino:avr:nano`. This only fixes MCU + 16 MHz clock
for the compile; the exact bootloader baud only matters for **upload**, and CH340
dongles come in two flavours — pick the one that uploads:

| Board variant                         | FQBN                                  | Upload baud |
|---------------------------------------|---------------------------------------|-------------|
| Nano, new bootloader (Optiboot)       | `arduino:avr:nano`                    | 115200      |
| Nano, old bootloader (common on CH340)| `arduino:avr:nano:cpu=atmega328old`   | 57600       |
| Uno-style                             | `arduino:avr:uno`                     | 115200      |

All three produce an identical binary; they differ only in the upload protocol.

### Compile

```bash
arduino-cli compile --fqbn arduino:avr:nano firmware/Nrf24Sniffer
```

Requires the `RF24` library (TMRh20) and the `arduino:avr` core:

```bash
arduino-cli core install arduino:avr
arduino-cli lib install RF24
```

### Upload

Replace `COM5` with the dongle's port (`arduino-cli board list` to find it):

```bash
arduino-cli upload --fqbn arduino:avr:nano -p COM5 firmware/Nrf24Sniffer
```

If that ends in an avrdude sync timeout, the dongle has the old bootloader — use:

```bash
arduino-cli upload --fqbn arduino:avr:nano:cpu=atmega328old -p COM5 firmware/Nrf24Sniffer
```

### Build size

For `arduino:avr:nano` (ATmega328P): **flash 7,860 bytes (25%)**, **RAM 471
bytes global (22%)**.

## Serial protocol

115200 baud, one command per line (`\n` terminated, `\r` ignored). Every command
is answered with `OK ...` or `ERR ...`.

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
| `listen`                      | enter RX mode                                             |
| `stop`                        | leave RX mode                                             |
| `info`                        | print current config + `radio.isChipConnected()`         |
| `tx <XX:..:XX> <hex...> [ack\|noack]` | transmit a payload (default `noack`)              |
| `help`                        | list commands                                             |

Addresses and payloads are hex bytes separated by `:` `,` or `.`; payload bytes
may also be space-separated. The **leftmost** hex byte is address byte 0 (matching
the sender's address array), so `pipe 1 42:54:48:4D:45` is ASCII `"BTHME"`.

### RX output

While listening, each received frame is printed as one parseable line:

```
RX p1 len=15 4D 56 52 02 D2 FC 44 00 05 01 57 0C 90 0B ...
```

`p<pipe>` is the pipe number, `len=<n>` the payload length, followed by `n` hex
bytes.

### Default configuration (at boot)

Matches the BTHome-over-nRF24 target protocol:

- channel **100**, **250 kbps**, **CRC16**, address width **5**
- pipe 1 = `42:54:48:4D:45` ("BTHME"), all other pipes off
- dynamic payloads **on**, auto-ack **off**, PA level **low**

RX mode is **off** at boot — send `listen` to start receiving.

> nRF24 hardware note: reading pipes 2–5 share address bytes 1–4 with pipe 1 and
> differ only in byte 0. Give pipe 1 the full address; for pipes 2–5 only byte 0
> is applied by the hardware.

## Python terminal

Only dependency is pyserial:

```bash
pip install -r requirements.txt
python nrf24term.py COM5            # plain terminal
python nrf24term.py COM5 --pretty   # decode RX frames as BTHome v2
```

A reader thread prints everything from the dongle; typed lines are forwarded
verbatim. Local commands (handled by the terminal, not the dongle) start with
`:` — `:pretty on|off`, `:help`, `:quit`. Opening the port resets the ATmega
(DTR), so the tool waits ~2 s and then sends `info`.

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
uint16 LE × 0.001 V), button event (`0x3A`), dimmer event (`0x3C`, direction +
steps). The k-th `0x3A`/`0x3C` object maps to button/dimmer *k*. Unknown object
ids can't be length-decoded, so the parser prints the remaining bytes as raw hex
and stops.

## Smoke test (with hardware)

No hardware was attached during development; verify on a real dongle like this:

1. Flash the firmware (see **Upload**). The RX LED blinks **once** at boot if the
   radio is detected, **twice** if not.
2. `python nrf24term.py COM5 --pretty`. It sends `info` automatically; confirm
   the reply shows `chip=connected` and the default config (channel 100, 250 kbps,
   CRC16, pipe1 = 42:54:48:4D:45).
3. Type `listen`. Trigger the `RotRemote_BTHome` remote (rotate / click). Live
   `RX p1 ...` lines should appear and, in pretty mode, decode to sender id +
   BTHome objects with the matching battery/voltage and button/dimmer event.
4. Loopback / emulation: with a second dongle (or against a real receiver), send
   e.g. `tx 42:54:48:4D:45 4D 56 52 02 D2 FC 44 00 01 01 57 noack` and confirm
   the receiver / other sniffer sees it.
5. Sanity-check `info` after changing settings (`ch 101`, `rate 1000`, …) — each
   should answer `OK` and be reflected in the next `info`.

## Layout

```
nrf24-sniffer/
  firmware/Nrf24Sniffer/Nrf24Sniffer.ino   RF24-based dongle firmware
  nrf24term.py                             serial terminal + BTHome decoder
  requirements.txt                         pyserial
  README.md
```
