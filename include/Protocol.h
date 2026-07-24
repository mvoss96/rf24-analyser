#pragma once

// Firmware version, and the command-protocol version the host can check.
//
// api=3 changed two things a host cannot ignore: RX lines carry a firmware
// timestamp, and pipes 2-5 take one byte instead of a full address.
#define FW_VERSION "3.1.1"
#define API_VERSION 3
