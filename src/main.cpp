/*
 * nrf24-sniffer firmware
 *
 * RF24-based debug / sniffer for the BTHome-over-nRF24 radio protocol, running
 * on an ATmega328P + CH340 + nRF24L01 USB dongle. A line-based ASCII protocol
 * over serial (115200 baud) configures the radio, dumps received frames, scans
 * for channel activity, and transmits arbitrary payloads. See README.md for the
 * command reference.
 *
 * Received frames are printed as:  RX p<pipe> len=<n> <b0> <b1> ...
 * Every command is answered with "OK ..." or "ERR ...".
 */

#include <Arduino.h>
#include "RadioController.h"
#include "CommandParser.h"

static RadioController g_radio;
static CommandParser g_parser(g_radio);

void setup() {
  Serial.begin(115200);
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
