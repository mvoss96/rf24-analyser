#pragma once

// Firmware version, and the command-protocol version the host can check.
//
// api=3 changed two things a host cannot ignore: RX lines carry a firmware
// timestamp, and pipes 2-5 take one byte instead of a full address.
//
// 3.2.0 extended tx with optional burst arguments (x<n>, gap=<ms>). Purely
// additive: a host that never sends them sees the exact old behaviour, so the
// api version stays at 3.
#define FW_VERSION "3.2.0"
#define API_VERSION 3
