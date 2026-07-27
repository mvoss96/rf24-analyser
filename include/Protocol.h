#pragma once

// Firmware version, and the command-protocol version the host can check.
//
// api=3 changed two things a host cannot ignore: RX lines carry a firmware
// timestamp, and pipes 2-5 take one byte instead of a full address.
//
// 3.2.0 extended tx with optional burst arguments (x<n>, gap=<ms>). Purely
// additive: a host that never sends them sees the exact old behaviour, so the
// api version stayed at 3.
//
// api=4 adds `crc=<XX>` to every RX line, covering the payload from the moment
// it leaves the RX FIFO. Not additive: a host that does not know the field
// would try to read it as payload hex and drop the frame, so the version has
// to move with it.
// 3.4.0 adds rxmode/rxdbg/regs for the RX-FIFO diagnosis. Additive, and the
// default rxmode is what 3.3.0 did unconditionally, so api stayed at 4.
//
// 3.5.0 makes `listen` and `hwset` acknowledge with the state they left behind
// instead of a bare OK, in the same key=value grammar `info` uses - so a host
// parses one thing, and a setting the firmware quietly changed (an irq pin that
// cannot interrupt, a chip that did not take a value) is visible in the very
// line that reports success. Additive: the tokens come after the OK, and a host
// that checks for OK and stops reading sees the old behaviour, so api stayed at
// 4 and a dongle on older firmware still works with a newer host.
// api=5 renames the greeting from NRF24SNIFFER to NRF24ANALYSER, the project
// having been renamed. It is the one break this field cannot announce: a host
// that does not know the new identity never recognises the line, so it never
// reads the api= in it either. The version moves anyway, because it is the
// record of what is and is not interchangeable - but what a host actually sees
// against older firmware is no greeting at all, and the answer is to reflash.
#define FW_VERSION "3.6.0"
#define API_VERSION 5
