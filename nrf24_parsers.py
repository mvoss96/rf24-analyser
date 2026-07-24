"""Pluggable frame decoders for the nrf24-sniffer.

A parser turns the raw bytes of one received frame into a short summary (one
table row) and a detailed field list. Adding a protocol means adding a Parser
subclass and calling register() - neither the GUI nor the terminal needs to
change.

Currently registered:
  raw     - hex dump only, no interpretation
  bthome  - [4-byte sender id][BTHome v2 service data], decoded by bthome-ble
  nrf24smart - the legacy NRF24Smart protocol (see the class for the layout)
"""

import logging

# --- Parser interface -------------------------------------------------------


class Parser:
    """Decodes one frame. Subclasses override summary() and detail()."""

    name = ""       # identifier used in the UI and on the command line
    label = ""      # human-readable name
    description = ""

    def available(self):
        """Returns None if usable, otherwise a reason string."""
        return None

    def summary(self, data):
        """One short line describing the frame, for a table row."""
        raise NotImplementedError

    def detail(self, data):
        """List of text lines describing the frame in full."""
        raise NotImplementedError


_REGISTRY = {}


def register(cls):
    """Class decorator: registers one instance of the parser under its name."""
    _REGISTRY[cls.name] = cls()
    return cls


def get(name):
    return _REGISTRY.get(name)


def names():
    return list(_REGISTRY)


def all_parsers():
    return list(_REGISTRY.values())


def hexdump(data, per_line=16):
    """Classic offset / hex / ASCII dump."""
    lines = []
    for off in range(0, len(data), per_line):
        chunk = data[off:off + per_line]
        hexpart = " ".join(f"{b:02X}" for b in chunk)
        text = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in chunk)
        lines.append(f"  {off:04X}  {hexpart:<{per_line * 3 - 1}}  {text}")
    return lines


# --- raw --------------------------------------------------------------------


@register
class RawParser(Parser):
    name = "raw"
    label = "Raw hex"
    description = "No interpretation - hex dump only."

    def summary(self, data):
        head = " ".join(f"{b:02X}" for b in data[:12])
        return head + (" ..." if len(data) > 12 else "")

    def detail(self, data):
        return [f"  {len(data)} bytes"] + hexdump(data)


# --- NRF24Smart (the protocol this tool replaces) ----------------------------
#
# Reconstructed from the packet classes in archive/smart-home-nrf:
# RFcomm/ClientPacket.h, ServerPacket.h, RemotePacket.h, RFmessages.h.

SMART_MSG_TYPES = {
    0: "error", 1: "init", 2: "boot", 3: "set", 4: "reset",
    5: "status", 6: "remote", 7: "ok",
}
SMART_CHANGE_TYPES = {0: "invalid", 1: "set", 2: "toggle", 3: "increase", 4: "decrease"}
SMART_LAYERS = {0: "buttons"}
SMART_LAYERS.update({n: f"axis{n}" for n in range(1, 10)})
SMART_AXIS_DIRS = {0: "up", 1: "down"}
SMART_REMOTE = 6
SMART_SET = 3


SMART_FROM_HOST = (SMART_SET, 4)        # set, reset - only the host sends these
SMART_FROM_DEVICE = (0, 2, 5, 7)        # error, boot, status, ok


def _smart_checksum_ok(data):
    """The checksum covers every byte but the trailing two, MSB first.

    Identical in all three packet shapes, which is why it says nothing about
    which shape a frame is - see the parser docstring.
    """
    return ((data[-2] << 8) | data[-1]) == sum(data[:-2]) & 0xFFFF


@register
class NRF24SmartParser(Parser):
    """The NRF24Smart protocol, three packet shapes sharing one header.

    All three begin with `id, uuid[4], msg_type`, then diverge:

      device  id uuid[4] type fw power interval msgnum  data[n] sum[2]   (12+n)
      host    id uuid[4] type                           data[n] sum[2]   (8+n)
      remote  id uuid[4] type target[4] layer value     sum[2]           (14)

    The checksum does not tell them apart. It spans "everything but the last two
    bytes" in every shape, so all three validate the same bytes and a frame that
    checksums out as one checksums out as all of them. What the original
    receiver keys on is `msg_type`: type 6 is a remote packet, anything else is
    read as a device packet (CommunicationManager.py:215) - it can do that
    because it only ever receives from devices, and host packets are the ones it
    sends.

    A sniffer watches both directions and gets no such context, so direction is
    inferred from the message type: `set` and `reset` only ever travel host to
    device, `boot`/`status`/`ok`/`error` only the other way. `init` goes both
    ways - a device asks for an id and the host answers with one - and is
    reported as undetermined rather than guessed.

    Written mainly to keep the decoder interface honest: a second protocol that
    shares nothing with BTHome is the test of whether "add a parser, change
    nothing else" holds.
    """

    name = "nrf24smart"
    label = "NRF24Smart (legacy)"
    description = "The pre-BTHome protocol: device, host and remote packets."

    # -- identification --

    @staticmethod
    def _shape(data):
        """('remote'|'device'|'host'|None, note) for the frame's layout."""
        if data[5] == SMART_REMOTE:
            if len(data) != 14:
                return None, f"type is remote but the frame is {len(data)} bytes, not 14"
            return "remote", ""
        if data[5] in SMART_FROM_HOST:
            return ("host", "") if len(data) >= 8 else (None, "too short for a host packet")
        if data[5] in SMART_FROM_DEVICE:
            return ("device", "") if len(data) >= 12 else (None, "too short for a device packet")
        # init, or an unknown type: could be either direction
        if len(data) >= 12:
            return "device", "direction undetermined; read as a device packet"
        return "host", "direction undetermined; too short for a device packet"

    @staticmethod
    def _head(data):
        uuid = ":".join(f"{b:02X}" for b in data[1:5])
        kind = SMART_MSG_TYPES.get(data[5], f"type{data[5]}")
        return data[0], uuid, kind

    # -- Parser API --

    def summary(self, data):
        if len(data) < 8:
            return "(too short for NRF24Smart)"
        _id, uuid, kind = self._head(data)
        if not _smart_checksum_ok(data):
            return f"{uuid}  {kind}  !! checksum mismatch"

        shape, note = self._shape(data)
        if shape is None:
            return f"{uuid}  {kind}  !! {note}"

        # "remote remote" reads as a stutter: for that shape the type says nothing
        # the shape has not already said.
        parts = [uuid, shape if shape == kind else f"{shape} {kind}"]
        if shape == "remote":
            target = ":".join(f"{b:02X}" for b in data[6:10])
            layer = data[10]
            value = data[11] if layer == 0 else SMART_AXIS_DIRS.get(data[11], data[11])
            parts.append(f"-> {target} {SMART_LAYERS.get(layer, f'layer{layer}')}={value}")
        elif shape == "device":
            parts.append(f"fw{data[6]} power={data[7]} msg#{data[9]}")
        if note:
            parts.append(f"({note})")
        return "  ".join(parts)

    def detail(self, data):
        if len(data) < 8:
            return [f"  {len(data)} bytes - too short for any NRF24Smart packet"] + hexdump(data)

        _id, uuid, kind = self._head(data)
        lines = [
            f"  id        : {data[0]}",
            f"  uuid      : {uuid}",
            f"  msg type  : {kind} ({data[5]})",
        ]

        if not _smart_checksum_ok(data):
            expected = sum(data[:-2]) & 0xFFFF
            lines.append(f"  !! checksum {(data[-2] << 8) | data[-1]:04X}, "
                         f"computed {expected:04X} - corrupt, or not this protocol")
            return lines + hexdump(data)

        shape, note = self._shape(data)
        if shape is None:
            return lines + [f"  !! {note}"] + hexdump(data)
        lines.append(f"  shape     : {shape}" + (f"  ({note})" if note else ""))

        if shape == "remote":
            lines.append("  target    : " + ":".join(f"{b:02X}" for b in data[6:10]))
            layer = data[10]
            lines.append(f"  layer     : {SMART_LAYERS.get(layer, layer)} ({layer})")
            if layer == 0:
                lines.append(f"  button    : {data[11]}")
            else:
                lines.append(f"  direction : {SMART_AXIS_DIRS.get(data[11], data[11])} ({data[11]})")
        elif shape == "device":
            lines.append(f"  firmware  : {data[6]}")
            # POWER_TYPE doubles as the battery level on battery-powered devices,
            # so it cannot be rendered as one or the other with any confidence.
            lines.append(f"  power     : {data[7]}  (battery % or mains flag)")
            lines.append(f"  interval  : {data[8]} s")
            lines.append(f"  msg num   : {data[9]}")
            lines += self._payload_lines(data[5], data[10:-2])
        else:
            lines += self._payload_lines(data[5], data[6:-2])

        return lines

    @staticmethod
    def _payload_lines(msg_type, payload):
        if not payload:
            return ["  data      : (none)"]
        lines = ["  data      : " + " ".join(f"{b:02X}" for b in payload)]
        # A SET carries varIndex, changeType, valueSize, then the value.
        if msg_type == SMART_SET and len(payload) >= 3:
            change = SMART_CHANGE_TYPES.get(payload[1], payload[1])
            size = payload[2]
            value = payload[3:3 + size]
            lines.append(f"  set       : var {payload[0]} {change}"
                         + (" = " + " ".join(f"{b:02X}" for b in value) if value else ""))
            if len(payload) - 3 != size:
                lines.append(f"  !! valueSize {size} does not match the {len(payload) - 3}"
                             f" bytes present")
        return lines


# --- BTHome v2 --------------------------------------------------------------


BTHOME_UUID = (0xD2, 0xFC)  # service UUID 0xFCD2, little-endian on the wire


class _LogCollector(logging.Handler):
    """Captures what bthome-ble logs while parsing one frame.

    The library explains why it rejected a payload (objects out of order, bad
    length) only through logging - and that reasoning is exactly what a protocol
    debugger needs to see.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.levelno, record.getMessage()))


@register
class BTHomeParser(Parser):
    """[4-byte sender id][D2 FC][device info][BTHome objects].

    Object parsing is delegated entirely to bthome-ble, the reference parser
    Home Assistant uses. Nothing about BTHome measurements is reimplemented: a
    hand-maintained table drifts from the spec (this tool already shipped one
    that mis-decoded dimmer events), and leaning on the reference turns the
    sniffer into a conformance check - a frame bthome-ble cannot read is a frame
    no standard receiver can read either.
    """

    name = "bthome"
    label = "BTHome v2 (over nRF24)"
    description = "4-byte sender id + BTHome v2 service data, decoded by bthome-ble."

    def available(self):
        try:
            import bthome_ble.parser  # noqa: F401
        except ImportError:
            return "bthome-ble is not installed (pip install -r requirements.txt)"
        return None

    # -- internals --

    @staticmethod
    def _split(data):
        """Returns (sender, device_info, objects) or None if not a BTHome frame."""
        if len(data) < 7:
            return None
        if (data[4], data[5]) != BTHOME_UUID:
            return None
        return data[0:4], data[6], data[7:]

    @staticmethod
    def _parse_objects(payload):
        """Run the reference parser over the BTHome object bytes.

        A fresh parser per frame is deliberate: the library deduplicates by
        packet id, which would hide exactly the retransmissions a sniffer exists
        to show, and it keeps one sender's state out of the next sender's frame.
        """
        from bthome_ble.parser import BTHomeBluetoothDeviceData, BTHomeVersion

        parser = BTHomeBluetoothDeviceData()
        parser.bthome_version = BTHomeVersion.V2

        collector = _LogCollector()
        logger = logging.getLogger("bthome_ble")
        previous_level = logger.level
        logger.addHandler(collector)
        logger.setLevel(logging.DEBUG)
        try:
            parser._parse_payload(bytes(payload), 0.0)
        except Exception as exc:  # private API; guard against upstream changes
            collector.records.append((logging.ERROR, f"{type(exc).__name__}: {exc}"))
        finally:
            logger.removeHandler(collector)
            logger.setLevel(previous_level)

        return (
            getattr(parser, "_sensor_values_updates", {}) or {},
            getattr(parser, "_events_updates", {}) or {},
            getattr(parser, "_sensor_descriptions_updates", {}) or {},
            collector.records,
        )

    # -- Parser API --

    def summary(self, data):
        split = self._split(data)
        if split is None:
            return "(not a BTHome frame)"
        sender, _info, payload = split
        sender_hex = ":".join(f"{b:02X}" for b in sender)

        sensors, events, _units, records = self._parse_objects(payload)
        parts = []
        for value in events.values():
            props = value.event_properties or {}
            extra = " " + ", ".join(f"{k}={v}" for k, v in props.items()) if props else ""
            parts.append(f"{value.name}: {value.event_type}{extra}")
        if not parts:
            for value in sensors.values():
                parts.append(f"{value.name} {value.native_value}")
        if not parts:
            return f"{sender_hex}  !! rejected by bthome-ble"

        flagged = any(level >= logging.WARNING for level, _ in records)
        return f"{sender_hex}  " + "; ".join(parts) + ("  !!" if flagged else "")

    def detail(self, data):
        if len(data) < 4:
            return ["  (frame too short for a 4-byte sender id)"]

        sender = data[0:4]
        sender_hex = ":".join(f"{b:02X}" for b in sender)
        sender_ascii = "".join(chr(b) if 0x20 <= b <= 0x7E else "." for b in sender)
        lines = [f'  sender    : {sender_hex}  "{sender_ascii}"']

        split = self._split(data)
        if split is None:
            lines.append("  (no BTHome service data: expected D2 FC UUID)")
            lines += hexdump(data[4:])
            return lines

        _sender, info, payload = split
        flags = []
        if info & 0x01:
            flags.append("encrypted")
        if info & 0x04:
            flags.append("trigger-based")
        version = (info >> 5) & 0x07
        lines.append(f"  bthome    : v{version}" + (" " + ", ".join(flags) if flags else ""))

        sensors, events, units, records = self._parse_objects(payload)

        for key, value in sensors.items():
            desc = units.get(key)
            unit = ""
            if desc is not None and desc.native_unit_of_measurement:
                unit = f" {desc.native_unit_of_measurement}"
            lines.append(f"  {value.name:<10}: {value.native_value}{unit}")

        for value in events.values():
            props = value.event_properties or {}
            extra = ""
            if props:
                extra = " (" + ", ".join(f"{k}={v}" for k, v in props.items()) + ")"
            lines.append(f"  {value.name:<10}: {value.event_type}{extra}")

        if not sensors and not events:
            # Nothing came out of the reference parser, so this frame would be
            # dropped by any spec-conformant receiver.
            lines.append("  !! REJECTED by the reference parser (bthome-ble)")
            lines.append("  objects   : " + " ".join(f"{b:02X}" for b in payload))
            shown = records
        else:
            # Anything the library warned about is worth surfacing even on success.
            shown = [r for r in records if r[0] >= logging.WARNING]
        for _level, message in shown:
            lines.append(f"  reason    : {message}")

        return lines
