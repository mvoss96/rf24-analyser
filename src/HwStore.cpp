#include <EEPROM.h>
#include "HwStore.h"

namespace {

constexpr int ADDR = 0;
// Distinct magic so a record written by another project on this chip is never
// mistaken for a wiring record.
constexpr uint8_t MAGIC0 = 0x4E; // 'N'
constexpr uint8_t MAGIC1 = 0x53; // 'S'
constexpr uint8_t LAYOUT = 1;

// Guards against a half-written or foreign record.
uint8_t checksum(const HwConfig &hw) {
  return (uint8_t)(LAYOUT + hw.ce + hw.csn * 3 + hw.irq * 5 + hw.ledRx * 7 + hw.ledTx * 11);
}

} // namespace

bool HwStore::load(HwConfig &hw) {
  if (EEPROM.read(ADDR) != MAGIC0) return false;
  if (EEPROM.read(ADDR + 1) != MAGIC1) return false;
  if (EEPROM.read(ADDR + 2) != LAYOUT) return false;

  HwConfig tmp;
  tmp.ce    = EEPROM.read(ADDR + 3);
  tmp.csn   = EEPROM.read(ADDR + 4);
  tmp.irq   = EEPROM.read(ADDR + 5);
  tmp.ledRx = EEPROM.read(ADDR + 6);
  tmp.ledTx = EEPROM.read(ADDR + 7);

  if (EEPROM.read(ADDR + 8) != checksum(tmp)) return false;
  if (tmp.ce == NO_PIN || tmp.csn == NO_PIN) return false; // both are mandatory

  hw = tmp;
  return true;
}

void HwStore::save(const HwConfig &hw) {
  // update() writes only bytes that actually differ, so re-saving an unchanged
  // wiring costs no EEPROM endurance.
  EEPROM.update(ADDR,     MAGIC0);
  EEPROM.update(ADDR + 1, MAGIC1);
  EEPROM.update(ADDR + 2, LAYOUT);
  EEPROM.update(ADDR + 3, hw.ce);
  EEPROM.update(ADDR + 4, hw.csn);
  EEPROM.update(ADDR + 5, hw.irq);
  EEPROM.update(ADDR + 6, hw.ledRx);
  EEPROM.update(ADDR + 7, hw.ledTx);
  EEPROM.update(ADDR + 8, checksum(hw));
}

void HwStore::clear() {
  EEPROM.update(ADDR, 0xFF);
  EEPROM.update(ADDR + 1, 0xFF);
}
