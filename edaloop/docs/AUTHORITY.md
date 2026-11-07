# Who answers what

Each question has one authority. Each entry records the evidence behind the choice, from runs on the author's boards on 2026-11-03.

| Question | Authority | Why, and what was rejected |
|---|---|---|
| Board geometry | The `.kicad_pcb` | circuitRF 1.6.5's KiCad export still starts with a BOM and KiCad 10.0.5 will not load it. An earlier patched run on 1.0.2 also lost pad nets. Flow is one way: KiCad into circuitRF. |
| DRC and ERC | `kicad-cli` | circuitRF's import carries no design rules, so its DRC has nothing to enforce. |
| Design rules in force | The `.kicad_pro` | A board copied without one ran on KiCad's defaults and reported 125 false violations. `project_files` now fails in that case. |
| RF path quality | `rf_path`, using `scripts/rf_path.py` | DRC has no view of jogs, detours or vias in a cascade. |
| RF layout: fence, plane across a trace's width, pad gaps, supply on top, bias order | `rf_layout`, with the rules in `rf-layout-rules.md` | DRC passes all of these. Each one moved a real board in review. |
| Line impedance on real copper | circuitRF `impedance` | Quick and specific. The dips it reports are priced as return loss at the top of band. |
| Field-solver truth | At least two different solvers: EMerge and Palace by default. openEMS reads high on narrow coplanar lines, so it isn't a default | One Palace Draft run read 36.2 ohm on a 49 ohm line with no warning. |
| Autorouting | KiCadRoutingTools, pinned to the project's rules | With rules pinned it beat Freerouting on 4 of 7 boards and tied on 2, and both timed out on a full reroute of an 8-layer, 788-net board. Unpinned, it rewrites the project's rules so its copper passes. PCBWorld and OrthoRoute's own README steer ordinary boards away from OrthoRoute. |
| Design rules during routing | The original `.kicad_pro`, restored after each router call | KiCadRoutingTools lowered one test board's minimum via from 0.56 to 0.25 mm in the file beside its output. |
| Parts | The local pcbparts catalogue | atopile's hosted API no longer resolves. |
| DC, transient, noise | ngspice on KiCad's netlist | circuitRF has none of these. A part with no model fails the bench. |
| Heating and drop | `power`, first-order | DRC passed 1.5 A through 0.2 mm, a 49 C rise. |
| Heating and drop with pours | `thermal_field` (`boardfield`) | The IPC-2221 screen gave 49 C on a trace beside copper where the field solve gave 12 C. Checked against closed forms, not yet against a real board. |
| RF width | circuitRF in a loop | The chain file said 0.30 mm, which reads 55.4 ohm. A closed-form tool said 0.396 mm. The loop found the board's 0.36 mm. |
| Router safety | `compare_copper` and the ground-via count | Freerouting scored 0 DRC errors on a board that had lost 53 stitching vias during the strip step. |
| Requirements and waivers | A person, in `edaloop.yaml` | No MCP tool can change them. |

## Tool boundaries

- **Sensors** report and change nothing: every edaloop check.
- **Actuators** change the design: KiCad MCP, `.ato` source, routers, skills.

The KiCad MCP has about 124 tools here and circuitRF's MCP has 15. An agent starts from `next_action`, which names the one check or fix in play, so it does not have to choose among all of them.
