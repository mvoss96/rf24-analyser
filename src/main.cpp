/*
 * nrf24-sniffer firmware
 *
 * RF24-based debug / sniffer for the BTHome-over-nRF24 radio protocol, running
 * on an ATmega328P + CH340 + nRF24L01 USB dongle. A line-based ASCII protocol
 * over serial (500000 baud) configures the radio, dumps received frames, scans
 * for channel activity, and transmits arbitrary payloads. See README.md for the
 * command reference.
 *
 * Received frames are printed as:  RX p<pipe> len=<n> <hex payload>
 * Every command is answered with "OK ..." or "ERR ...".
 */

#include <Arduino.h>
#include "RadioController.h"
#include "CommandParser.h"

static RadioController g_radio;
static CommandParser g_parser(g_radio);

void setup() {
  // 500000 baud: an exact divisor at 16 MHz (0% error, cleaner than 115200) and
  // fast enough that printing a burst of frames cannot overrun the RX FIFO.
  Serial.begin(500000);
  bool chip = g_radio.begin();
  Serial.println(F("NRF24SNIFFER ready"));
  Serial.print(F("chip="));
  Serial.println(chip ? F("connected") : F("NOT connected"));
}

void loop() {
  while (Serial.available()) {
    g_parser.feed((char)Serial.read());
  }
  g_radio.poll();
}
