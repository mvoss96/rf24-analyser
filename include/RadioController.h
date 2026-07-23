#pragma once
#include <Arduino.h>
#include <RF24.h>
#include "pins.h"

// Maximum nRF24 address width in bytes.
constexpr uint8_t MAX_ADDR_WIDTH = 5;

// Full radio configuration. Defaults match the BTHome-over-nRF24 target
// protocol: channel 100, 250 kbps, CRC16, 5-byte address, pipe 1 = "BTHME",
// dynamic payloads on, auto-ack off, PA level low.
struct RadioConfig {
  uint8_t  channel   = 100;
  uint16_t rateKbps  = 250;  // 250 | 1000 | 2000
  uint8_t  crcBits   = 16;   // 0 | 8 | 16
  uint8_t  addrWidth = 5;    // 3 | 4 | 5
  bool     autoAck   = false;
  bool     dpl       = true; // dynamic payloads
  uint8_t  plSize    = 32;   // static payload size when dpl == false
  uint8_t  paLevel   = 1;    // 0=min 1=low 2=high 3=max
  bool     pipeEn[6] = {false, true, false, false, false, false};
  uint8_t  pipeAddr[6][MAX_ADDR_WIDTH] = {
    {0},
    {0x42, 0x54, 0x48, 0x4D, 0x45}, // "BTHME"
    {0}, {0}, {0}, {0},
  };
};

// Wraps the RF24 radio and applies a RadioConfig to it. Owns RX draining,
// transmit, channel scanning and the `info` dump.
class RadioController {
public:
  RadioController();

  // Initialises the radio and applies the current config. Returns true if the
  // nRF24 chip responds.
  bool begin();

  RadioConfig &config() { return cfg_; }
  bool listening() const { return listening_; }
  bool chipConnected() { return radio_.isChipConnected(); }

  // Applies the full config to the radio, preserving listen state.
  void reconfigure();

  void startListening();
  void stopListening();

  // Drains any pending RX frames to serial. Cheap to call every loop.
  void poll();

  // Transmits one payload to `addr`. noack=true sends with the NO_ACK flag
  // (per-packet), matching the broadcast sender. Returns radio.write() result.
  bool transmit(const uint8_t *addr, const uint8_t *data, uint8_t len, bool noack);

  // Energy scan across all 126 channels, `passes` sweeps. Prints hits.
  void scan(uint16_t passes);

  // Prints the current configuration and chip status.
  void printInfo();

private:
  RF24 radio_;
  RadioConfig cfg_;
  bool listening_ = false;
  void drainRx();
};
