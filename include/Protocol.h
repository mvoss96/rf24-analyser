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
#define FW_VERSION "3.4.0"
#define API_VERSION 4
