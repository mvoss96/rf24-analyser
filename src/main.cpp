/*
 * nrf24-sniffer firmware
 *
 * RF24-based debug / sniffer for raw nRF24L01 traffic, running on an ATmega328P
 * USB dongle. A line-based ASCII protocol over serial (500000 baud) defines the
 * board wiring, configures the radio, dumps received frames, scans for channel
 * activity, and transmits arbitrary payloads. See README.md for the reference.
 *
 * Nothing is assumed: neither the wiring nor the radio parameters have built-in
 * defaults. The host must send `hwset` and then `listen` before the radio does
 * anything, so the dongle can never be quietly listening on the wrong settings.
 *
 *   nohw --hwset--> unconfigured --listen k=v...--> listening <--stop--> idle
 *
 * Received frames are printed as:  RX p<pipe> len=<n> <hex payload>
 * Every command is answered with "OK ..." or "ERR ...".
 */

#include <Arduino.h>
#include "RadioController.h"
#include "CommandParser.h"
#include "HwStore.h"

// Firmware version, and the command-protocol version the host can check.
#define FW_VERSION "2.1.0"
#define API_VERSION 2

static RadioController g_radio;
static CommandParser g_parser(g_radio);

void setup() {
  // 500000 baud: an exact divisor at 16 MHz (0% error, cleaner than 115200) and
  // fast enough that printing a burst of frames cannot overrun the RX FIFO.
  Serial.begin(500000);

  // A stored wiring is restored, but never silently: the greeting states where
  // the pins came from, and a chip that does not answer on them drops back to
  // nohw rather than pretending to be ready.
  const __FlashStringHelper *hwSource = F("none");
  HwConfig stored;
  const bool haveStored = HwStore::load(stored);
  if (haveStored) {
    hwSource = g_radio.setHardware(stored) ? F("eeprom") : F("eeprom-failed");
  }

  Serial.print(F("NRF24SNIFFER fw=" FW_VERSION " api="));
  Serial.print(API_VERSION);
  Serial.print(F(" state="));
  Serial.print(g_radio.stateName());
  Serial.print(F(" hw="));
  Serial.print(hwSource);
  // Spell the wiring out rather than just its provenance: a stored-but-wrong
  // pin is otherwise invisible, and a wrong CE cannot be detected electrically
  // (isChipConnected() exercises SPI only). Seeing the pins is the check.
  if (haveStored) {
    Serial.print(' ');
    g_radio.printWiring();
  }
  Serial.println();
}

void loop() {
  while (Serial.available()) {
    g_parser.feed((char)Serial.read());
  }
  g_radio.poll();
}
