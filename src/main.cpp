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
 * Received frames are printed as:  RX t=<ms> p<pipe> len=<n> <hex payload>
 * The timestamp is the firmware's own millis() taken as the frame leaves the
 * RX FIFO; host arrival times cannot resolve the milliseconds between a
 * sender's repeats. Every command is answered with "OK ..." or "ERR ...".
 */

#include <Arduino.h>
#include "RadioController.h"
#include "CommandParser.h"
#include "HwStore.h"
#include "Protocol.h"

static RadioController g_radio;
static CommandParser g_parser(g_radio);

void setup() {
  // 500000 baud: an exact divisor at 16 MHz (0% error, cleaner than 115200) and
  // fast enough that printing a burst of frames cannot overrun the RX FIFO.
  Serial.begin(500000);

  // A stored wiring is restored, but never silently: the greeting states where
  // the pins came from, and a chip that does not answer on them drops back to
  // nohw rather than pretending to be ready.
  // At boot a wiring can only have come from EEPROM, so its provenance carries
  // no information - what matters is whether it works. "connected" therefore
  // means both checks passed: the chip answers over SPI and CE actually keys
  // it. Why a wiring failed goes into a WARN line rather than the greeting.
  HwConfig stored;
  if (HwStore::load(stored)) {
    if (!g_radio.setHardware(stored)) {
      Serial.println(F("WARN stored wiring: chip does not answer over spi"));
    } else if (!g_radio.selfTestCe()) {
      Serial.println(F("WARN stored wiring: ce pin does not key the radio"));
      g_radio.invalidateHw();
    }
  }

  // The greeting is exactly what `status` prints, so a host that missed the
  // greeting learns the same things by asking.
  g_parser.printStatus();
}

void loop() {
  while (Serial.available()) {
    g_parser.feed((char)Serial.read());
  }
  g_radio.poll();
}
