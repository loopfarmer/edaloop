# Feature test: gain_block_demo

This is a run of every edaloop step that the available tools allow on the example board, with what each one produced. It's here to show what a run looks like, and to record what this exercise found and fixed. Readings here are solver estimates on an unrouted board, not measurements.

## Setup

KiCad 10.0.8, `pcbnew` and ngspice 44.2 ran from KiCad's Docker image (`kicad/kicad:10.0.8`), through small wrapper scripts named in `~/.config/edaloop/toolchain.yaml`. `boardfield` was built from this repository. circuitRF and KiCadRoutingTools were not built, so nothing was routed. See [Requirements](../../README.md#requirements).

## What ran

| Step | Result | Notes |
|---|---|---|
| `build` | Placed 18 parts on a 40 × 20 mm board | |
| `ground` | 20 ground vias, none short of target | Before connector filling. See loopfarmer/edaloop#11 |
| `project_files` | pass | |
| `erc` | pass | Was 18 warnings before the fix below |
| `drc` | fail, 19 unconnected | Expected: nothing is routed |
| `rf_layout`, `rf_reference` | fail, RF nets unrouted | Were warn and pass before the fix below |
| `spice` | pass | Bias bench: 116 mA in total, 5.00 V at both LNAs |
| `cascade` | pass | 32.8 to 36.8 dB gain and at most 0.97 dB noise figure over 900 to 1500 MHz, from the stage table alone. With no copper, no line loss is included, and the evidence says so |
| `power` | fail, VDD doesn't reach R1 and R2 | Expected: unrouted |
| `thermal_field` | fail, VDD unrouted. 32.4 C rise under U1 from part heat | The rise is an upper estimate (0.58 W, still air, no conduction out through connectors). No `max_part_rise_c` is set, so nothing judges it. That limit is a person's decision |

The heat graph from `thermal_field`, with part heat:

![Heat graph of gain_block_demo with part heat: 48.0 to 57.4 C, hottest under U1](figures/gain_block_demo_heat.svg)

## What it found, and what changed

- **RF checks passed with no RF copper.** `rf_layout` reported a warning and `rf_reference` a pass on a placed but unrouted board, because there was nothing to measure. Both now fail with `unrouted`, naming the RF nets that have no copper.
- **ERC warned about every footprint library.** `build` placed parts from KiCad's standard libraries but left the project's `fp-lib-table` empty. It now lists them through `${KICAD10_FOOTPRINT_DIR}`, and ERC is clean.
- **The heat map had no scale.** `boardfield`'s PNG was 200 × 100 pixels, coloured from 0 C, with no axes or labels. It now colours from the coolest cell to the hottest and reports that range (`png_rise_range_c`). `thermal_field` also writes an SVG with the board edge, each part, the hotspot, and a scale in C with the rise beside it.

## Not run, and why

- **Routing** (`route`, `make`): it needs circuitRF to solve the RF width, and KiCadRoutingTools for power and signal. Building both was blocked in this environment, because it runs downloaded code.
- **S-parameters** (`impedance`, `em_fast`, `em_hops`, `em_crosscheck`): these need circuitRF and its EM solvers, and a routed board.
- **RF coupling between lines:** `rf_layout` measures parallel runs and pad-gap intrusions, but only on routed copper. There's no coupling simulation yet without the EM solvers.
- **`rf_path`, `rf_preserved` and `rules`:** these need routed copper, or a fabricator rule pack.
