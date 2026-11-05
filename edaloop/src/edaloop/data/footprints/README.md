# Footprints for the shipped testbenches

`lband_element` and `cband_element` name their RF parts' land patterns `RFLib_FP:*` and `CBandLib_FP:*`. These libraries stand in for them so the boards build with no network. `edaloop build` and `make` look here after any `--footprints` folder, and use a library here only if no earlier folder has one of the same name.

None of these were checked against Mini-Circuits' own case-style drawings, which couldn't be fetched when they were added. Each footprint's `descr` names the KiCad footprint it was copied from and what changed:

| Footprint | From KiCad | Change or assumption |
|---|---|---|
| `RFLib_FP:LFCN_FV1206` | `Filter:Filter_Mini-Circuits_FV1206` | The two plated ground holes drill 0.254 mm (was 0.3), so the 0.61 mm pad keeps a 0.178 mm ring. The stock footprint fails a 0.1778 mm annular-ring rule. Copper unchanged |
| `RFLib_FP:RLM_TT1224` | `RF_Mini-Circuits:Mini-Circuits_TT1224_LandPatternPL-258_ClockwisePinNumbering` | Unchanged |
| `CBandLib_FP:HFCN_FV1206-1` | `Filter:Filter_Mini-Circuits_FV1206-1` | Unchanged |
| `CBandLib_FP:MC1631-1` | `Package_DFN_QFN:DFN-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm` | **Assumed** package: KiCad has no MC1631-1. Chosen because the description puts the RF pins on 3 and 6, on opposite sides, with pad 9 as the paddle |
| `CBandLib_FP:DQ1225` | `Package_DFN_QFN:QFN-12-1EP_3x3mm_P0.5mm_EP1.65x1.65mm` | **Assumed** package: KiCad has no DQ1225. Chosen because the RF pins 2 and 8 are the middle pins of opposite sides, with pad 13 as the paddle |

Replace the assumed ones from Mini-Circuits' drawings before building a real board from them.

These files are derived from the KiCad footprint library, licensed CC-BY-SA 4.0 with KiCad's exception for designs that use them. They stay under that licence, not this repository's MIT licence.
