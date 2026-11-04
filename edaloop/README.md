# edaloop

One evidence-gated design loop over open circuit tools: atopile, pcbparts, KiCad, KiCadRoutingTools, Freerouting, ngspice, circuitRF, openEMS, Palace, EMerge and the RF layout skills.

It adds what none of them has alone: a record of what was measured on exactly which bytes, one authority per question, and one answer to "what next?".

```text
requirements -> capture -> place -> route -> verify -> simulate -> evaluate -> release
 edaloop.yaml    atopile    KiCad    Free-    DRC ERC    openEMS    scorecard   fab zip
                 KiCad MCP  skills   routing  rf_path    Palace     + traced    + manifest
                                     copies   impedance  EMerge     limits
                                              rules      cascade
                        <---- next_action sends failures back to the right tool ----
```

## Use

```bash
uv sync
```

```bash
uv run edaloop doctor --fix
```

In a design folder:

```bash
edaloop init
```

```bash
edaloop next
```

| Command | What it does |
|---|---|
| `init` | Writes the project file, `.mcp.json`, hook and skill. Never overwrites. |
| `doctor` | Runs every external tool and reports what actually works. |
| `next` | The one next action. Agents loop on this. |
| `status` | The gate, with staleness resolved against the files now. |
| `run <check>` or `run all` | Runs a check and records evidence. `run all` skips fresh results. |
| `route`, `adopt` | Routes copies with each router, ranks them, and adopts one only if it is admissible. |
| `route --by-class` | Routes RF, then power, then signal, on copies. See [docs/routing-classes.md](docs/routing-classes.md). |
| `stage <name>` | Runs the sensors a routing stage names in `edaloop.yaml`. |
| `release` | Exports fabrication files only if the gate holds on these bytes. |
| `review`, `report` | Exports the schematic, renders, BOM and gate status for a person, with no Gerbers. Writes the HTML scorecard. |
| `describe <.kicad_sch or .kicad_pcb>` | Writes a board description from a schematic, or from a board's pads when there's no schematic. Finds the RF paths. |
| `bench`, `build <description>` | Checks a description's numbers, or makes a placed KiCad project from it. |
| `make <description>` | Runs the whole flow from a description to a reviewed board. `--quick` leaves out the three-solver EM check, so the result can't be released. |
| `compare <a> <b>` | Two boards side by side: size, RF length, vias, and whether they join the same pads. |
| `eval` | Runs the framework against known-good production boards. See [evaluation/README.md](evaluation/README.md). |
| `export` | Writes the board in KiCad 6.0 and EAGLE 7.7 formats, for tools that can't read KiCad 10, and reads each back. See [docs/exports.md](docs/exports.md). |
| `mechanical` | Writes an OpenSCAD test mount, clamp and case from the board, and renders them to STL. See [docs/mechanical.md](docs/mechanical.md). |
| `fritzing` | Writes a Fritzing part (.fzpz) for the board. It has been read back by edaloop, but not yet opened in Fritzing. |
| `ground`, `finish` | Places ground vias before routing, and fences, stitches and refills after. |

[docs/rf-layout-rules.md](docs/rf-layout-rules.md) lists what RF reviews keep finding, how layout builds to it, and how `rf_layout` measures it. [docs/board-description.md](docs/board-description.md) covers what a description holds and how a board is placed from it.

## The rules that make it hold together

1. **Evidence is tied to bytes.** Saving the board makes every claim about it stale.
2. **A tool crash is an error, never a pass.**
3. **Routers work on copies.** Adoption refuses any result that lost RF copper or ground vias.
4. **EM needs agreement.** With three solvers, an outlier is named and its error pattern suggests a fix. See `em_diagnose.py`.
5. **People own requirements and waivers.** A waiver needs a name and a reason, and stays visible on the scorecard.

See [docs/AUTHORITY.md](docs/AUTHORITY.md) for why each tool has the job it has. `../boardhand/` holds the guarded board-editing MCP server.

## Layout

`src/edaloop/`: `checks.py` (sensors and release gate), `controller.py` (next action), `em_diagnose.py`, `route.py`, `scaffold.py` (`init` and hook), `parts_api.py` (atopile parts from pcbparts), `spice.py`, `power.py`, `route_classes.py`, `thermal.py` (board fields through boardfield), `mcp_server.py`, `report.py`, `ledger.py`, `pcbfile.py`, `kicad_ops.py`, `toolchain.py`.

