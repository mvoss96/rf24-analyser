#pragma once
#include "RadioController.h"

// Persists the board wiring in EEPROM. Pins are a physical property of the
// board, not a per-session choice, and unlike radio parameters a wrong pin
// fails loudly (the chip simply does not answer) - so remembering them cannot
// cause the silent-wrong-result problem that keeps radio settings explicit.
//
// The provenance is always reported in the greeting (hw=eeprom / eeprom-failed
// / none), so the convenience never turns into hidden state.
namespace HwStore {

// Loads a stored wiring. Returns false if nothing valid is stored.
bool load(HwConfig &hw);

// Stores the wiring (byte-wise update, so unchanged bytes are not rewritten).
void save(const HwConfig &hw);

// Invalidates the stored record.
void clear();

} // namespace HwStore
