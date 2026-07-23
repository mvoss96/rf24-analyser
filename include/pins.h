#pragma once
#include <Arduino.h>

// Pin map of the ATmega328P + CH340 + nRF24L01 USB dongle.
namespace Pins {
constexpr uint8_t RADIO_CE  = 9;
constexpr uint8_t RADIO_CSN = 10;
constexpr uint8_t RADIO_IRQ = 2;
constexpr uint8_t LED_TX    = A1; // lit while transmitting
constexpr uint8_t LED_RX    = 8;  // lit while receiving
// SPI is on the hardware pins D11 (MOSI) / D12 (MISO) / D13 (SCK).

// LEDs are wired active-low on this board.
constexpr uint8_t LED_ON  = LOW;
constexpr uint8_t LED_OFF = HIGH;
} // namespace Pins
