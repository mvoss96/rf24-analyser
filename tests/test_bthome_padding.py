"""Where the BTHome parser decides a sender's data ends.

A sender on a fixed payload size fills the slot to the configured length with
0xFF, so a receiver has to find the boundary itself. Doing it by trimming
trailing 0xFF looks right and is not: BTHome is little endian, so a signed
16-bit measurement between -0.01 and -2.56 ends in 0xFF, and the trim eats it.
A temperature of -1.00 C is `02 9C FF` - it came out of this tool as a
malformed frame, for exactly the readings around freezing one watches a sensor
for.

So the boundary is walked forward, object by object, and 0xFF counts as padding
only where an object id is expected. These cases hold that walk to it.

    python tests/test_bthome_padding.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nrf24_parsers import BTHomeParser  # noqa: E402

SENDER = "AA010004"
HDR = "D2FC44"  # service uuid 0xFCD2 little endian, then the device-info byte

results = []


def verdict(name, ok, detail):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}", flush=True)


def frame(objects_hex, slot=32):
    """A frame as it comes off a fixed-size pipe: filled up with 0xFF."""
    body = bytes.fromhex(SENDER + HDR + objects_hex)
    return body + b"\xFF" * max(0, slot - len(body))


def split(objects_hex, slot=32):
    data = frame(objects_hex, slot)
    sender, info, objects = BTHomeParser._split(data)
    del sender, info
    return objects.hex().upper(), BTHomeParser._trailing(data).hex().upper()


# --- the measurements the old trim destroyed ----------------------------------
for label, objects, expected in (
    ("-1.00 C", "00110" + "29CFF", "00110" + "29CFF"),   # packet id 1, temp 02 9C FF
    ("-0.01 C", "0011" + "02FFFF", "0011" + "02FFFF"),
    ("-0.1 rotation", "0011" + "3FFFFF", "0011" + "3FFFFF"),
):
    objects_out, trailing = split(objects)
    verdict(f"P a measurement ending in 0xFF survives the padding boundary ({label})",
            objects_out == expected and set(trailing) <= {"F"},
            f"objects {objects_out}, trailing {len(trailing) // 2} bytes")

# --- an object behind one that ends in 0xFF -----------------------------------
objects_out, trailing = split("001102" + "9CFF" + "03" + "8813")
verdict("P an object after an 0xFF-ending value is still read",
        objects_out == "0011029CFF038813",
        f"objects {objects_out}")

# --- a frame that needs no padding --------------------------------------------
full = "0011" + "01" * 0 + "029CFF" + "038813" + "0C800D" + "01640918" + "3A013C0205"
objects_out, trailing = split(full, slot=4 + 3 + len(full) // 2)
verdict("P a frame that fills the slot exactly has no trailing bytes",
        objects_out == full.upper() and trailing == "",
        f"{len(objects_out) // 2} object bytes, trailing {trailing!r}")

# --- text and raw carrying 0xFF -----------------------------------------------
# Their bytes are announced by a length, so a walk that respects it gets past
# them whatever they contain - including the padding byte itself.
objects_out, trailing = split("0011" + "5403FFFFFF" + "0164")
verdict("P a raw object made of 0xFF bytes is not mistaken for padding",
        objects_out.startswith("00115403FFFFFF"),
        f"objects {objects_out}")

objects_out, trailing = split("0011" + "5303FF00FF" + "0164")
verdict("P a text object carrying 0xFF and a zero is read whole",
        objects_out == "00115303FF00FF0164",
        f"objects {objects_out}")

# --- the command event, whose length depends on its own payload ---------------
# [id][argument count][opcode][arguments...]
objects_out, trailing = split("0011" + "3B027F0102" + "0164")
verdict("P a command event with two arguments is measured by its argument count",
        objects_out == "00113B027F01020164",
        f"objects {objects_out}")

# --- every object id the reference parser knows -------------------------------
# One synthetic object per id, to catch a width this walk gets wrong. Variable
# ones are given a length of two.
from bthome_ble.const import MEAS_TYPES  # noqa: E402

wrong = []
for object_id, meas in sorted(MEAS_TYPES.items()):
    if meas.data_format in ("string", "raw"):
        body = f"{object_id:02X}02AABB"
    elif meas.data_format == "command":
        body = f"{object_id:02X}027F0102"
    else:
        body = f"{object_id:02X}" + "01" * meas.data_length
    objects = bytes.fromhex(body)
    if BTHomeParser._data_end(objects) != len(objects):
        wrong.append(f"0x{object_id:02X} ({meas.data_format})")
verdict("P every object id the reference parser knows is walked over exactly",
        not wrong, "; ".join(wrong) or f"{len(MEAS_TYPES)} ids consumed whole")

# --- trailing bytes that are not padding --------------------------------------
# The case the old trim could not express: bytes after the last object that are
# not 0xFF mean the frame was read too long, and saying "padding" would be a
# lie.
data = bytes.fromhex(SENDER + HDR + "0011029CFF" + "AABBCC")
trailing = BTHomeParser._trailing(data)
verdict("P bytes after the last object that are not 0xFF are reported as such",
        trailing == b"\xAA\xBB\xCC",
        f"trailing {trailing.hex().upper()!r}")

# --- an object announcing more than is there ----------------------------------
data = bytes.fromhex(SENDER + HDR + "0011" + "5320")  # text announcing 32 bytes
sender, info, objects = BTHomeParser._split(data)
del sender, info
verdict("P an object announcing more bytes than the frame holds stops the walk",
        objects.hex().upper() == "0011",
        f"objects {objects.hex().upper()}, "
        f"trailing {BTHomeParser._trailing(data).hex().upper()}")

print("\n--- summary ---")
for name, ok, _ in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
failed = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(0 if not failed else 1)
