# Board descriptions

A description is one YAML file with a board's parts, nets, RF path and stackup. `testbenches/` holds examples. `edaloop describe` writes one from a schematic or a board, and marks with `REQUIRED` what the source can't say, such as a load's current. A build refuses until a person fills those in.

`describe` takes a connector for an RF port when its value or footprint names one, such as SMA, U.FL or Type-N. With only one such port, another `J` of three pins or fewer, none on a rail, is tried as the path's other end, as an antenna's feed studs are, before the path is taken to end at a radio. `notes` says when it did. A part across two nets of the path, such as a GDT across a balanced pair, is never a hop, and only `L` and a number is an inductor, so `LIM1` is a signal part. A path never runs through logic: not along a net named for a clock, a bus, a strobe or an enable (`CLK`, `SPI`, `DATA`, `LE`, `EN`, `MUTE` and the like), not through an FPGA, processor, logic gate, clock chip or oscillator (`DIGITAL_PART`), and not through a `J` of more than three pins that doesn't say it's RF, such as a control header. When that leaves no two connectors joined, each path runs from its RF port to the furthest radio it reaches, and `notes` says so. A board with no RF port and no such path is refused.

`assumed` lists defaults it used, such as the stackup when reading a schematic, and anything it left out or guessed about the RF path. From a board, it also reads the stackup, the RF track width, and `layout.gap_mm`: the closest two neighbouring parts on the RF path sit on the board, courtyard to courtyard, rounded down to 0.05 mm. That's never looser than the 0.5 mm default, and a pair whose courtyards overlap, as one covering a two-SMA kit's whole footprint does, isn't counted. A footprint with no courtyard is measured by the box `describe` gives its copy, its pads and 0.25 mm. `assumed` says which pair set it. `likely_rails` lists nets that look like supplies but have no currents yet. They aren't checked until you add them under `project.rails` with their source and load currents, and until then the power check fails rather than passing with nothing to check.

### Boards with many connectors

`describe` finds each RF path by trying every route between connectors, and keeps the longest. A board with many connectors and small signal nets has more routes than it can try (`SEARCH_LIMIT` steps), and a search cut short wanders through logic. So it stops, and names the connector pairs that have a route, with the fewest hops between each:

```
edaloop: more routes between the 7 connectors than can be searched. Name the connector pairs each RF path runs between with --chains-hint, ...
Pairs with a route, fewest hops first: J1-J2 (6 hops); J1-J5 (8 hops); J2-J5 (8 hops)
```

Name the pairs from the design in a YAML file, and describe again:

```bash
echo '[[J2, J5]]' > pairs.yaml
```

```bash
edaloop describe board.kicad_pcb -o d.yaml --band-mhz 1 3800 --chains-hint pairs.yaml
```

Each path then ends only at its partner, and each pair gets a search budget of its own. A pair whose routes still can't all be tried gets its most direct route, and `assumed` says so. That route can leave out a part on a longer branch, such as a filter beside a bypass, so check it. A pair that can't be joined without parts already on another path is named in `assumed` too. The MCP tool takes the same pairs as `chains_hint`.

A reference that more than one footprint carries, such as stitching vias drawn as footprints all called `GND_VIA`, isn't one part. `describe` leaves those footprints out and lists them in `assumed`.

## How a board is placed

Placement comes from the nets and the footprints' courtyards. Nothing is placed by hand.

1. **RF rows.** Each RF path is one row. Every part is rotated so that its RF pads lie on the line, and placed as close to the last as the courtyards allow. A part whose input and output pads are on the same side, such as a switch with its RF on pins 6 and 8 of a row of five, can't send its output along the line without crossing its own pins. Its output leaves straight out from that side instead: the line moves across the row, past the part's courtyard and the gap, and the next part sits directly out from the pad. A row that comes back to the part it started at, through another port, as from one SMA of a two-SMA kit to the other, is placed between the two ports. The part's courtyard has to leave that gap open. A row too long for the gap is folded into a U between the ports, as lna915's released board snakes its amplifier between its two SMAs: along, across, along, back across, and along into the return port. The line turns only at a two-pad part on it, such as a coupling capacitor, which stands across the turn so that no hop bends, and each part turns with its bias stack. Of the folds that fit, the placer keeps the one with the fewest hops over the critic's limit, then the least excess, then the shortest line, by a quick stand-in for the critic. If none fits with the row's taps on the line, it tries once more with them beside it, where they take no length along the row, and the build names the taps it moved and writes them to `rf_layout.tap_stub_mm` in `edaloop.yaml`. A tap beside the line hangs off the next part's input pad, on the other side of the line if its own side is taken there, so that its stub adds nothing to the hop. A folded row has no one line for `layout.line` to set, so it moves along and across the board as far as it needs to keep off the start and top edges. If that fails too, the row is refused with the gap and the row's length. A row too long for a fixed outline along its axis, even with its odd-angled parts set square, and too long to fit turned, is folded to fit the outline the same way, as a serpentine: along, across, back, and on for as many legs as it needs, each leg within the outline's length and all of them within its width. It turns only at a two-pad part on the line, as above, never runs back past its first part, so a connector at the start stays at the board's edge, and moves each later leg across as far as it needs to clear the legs before it. Of the folds that fit, the placer keeps the one with the fewest hops over the critic's limit, then the least excess, then the fewest turns, then the shortest line, and tries it first with no part set square. The build names the parts each folded row turns at, under `folds` in its result. A row that would fit turned isn't folded: a straight row along the other axis is better, and the hint says to set it. A row too wide for the outline across, or parts packed past it, aren't folded either. A board that fits straight is placed as before. A footprint copied from a board that has no courtyard, as from EAGLE, gets a box round each group of pads only where the board places another part between the groups. Otherwise it gets one box round all its pads.
2. **Bias stacks.** A two-pin part with one pin on an RF net is a tap. Rails rise from it in a column, with the smallest capacitor nearest the choke. A part across a balanced pair, such as a GDT across an antenna's feed studs, is a tap too: its pad on the line, the rest of it toward the pair's other leg. The other leg is a net joining the same two parts as a hop of the path, and no rail.
3. **Channel parts.** On a board with several rows, a part that serves one row goes in and beside that row. Ownership comes from the nets: a part is a row's if the nets lead only to that row. A net or part reaching two rows is shared. When a rail has at least one capacitor per row, each row gets an equal share, and any left over go with the shared parts.
4. **One path.** On a board with one RF path, every other part with a net goes round it, nearest what it joins.
5. **Per-pin decoupling.** Before anything else goes round an IC on a row, each capacitor on a net that joins it to one pin of the IC, and nothing else, goes beside that pin: straight out from the IC's side, in the pins' order along it, a courtyard apart. A step attenuator's ACG caps are the usual case. Placed anywhere else, one pin's cap can sit across a neighbour's escape, and no route order joins both. A cap that isn't clear beside its pin is placed as in 3 and 4.
6. **Shared parts.** The rest goes in a shelf after the last row, such as headers and control logic.

## The schematic

A board with one RF path is drawn on one page. A board with several gets one page per path, holding the parts only that path uses, one page for what they share, and a root page linking them. Pages connect by net name. Every build exports the netlist and compares it with the description, so a drawing can't change a connection unnoticed.

## Heat

The temperature check solves twice. The first solve is the copper's own heat, judged against `power.max_rise_c`. The second adds each part's heat, found by following each load's stated current through series resistors and chokes to the part that uses it, at `thermal.supply_v`. All of that power counts as heat, and the result is the board's temperature under the part, so junctions run hotter.

A rail can give its own `volts`, and then its parts are charged at that voltage rather than the supply's. If the rail's `source` is a regulator, the regulator is charged what it drops, (V_in − V_out) × I_out, plus any current it takes beyond what it passes on: the output's power is already counted in the parts that use it. Current that reaches a connector rather than an active part, such as a bias-T feeding an active antenna, leaves the board and isn't counted. It's listed in `part_current_off_board`. Without `volts`, every load is charged the supply's voltage, which overstates a board with regulated rails.

```yaml
rails:
  - {net: VIN, source: J2.1, loads: {U1.1: 0.102, L5.1: 0.05}}
  - {net: V3V3, source: U1.2, volts: 3.3, loads: {U2.1: 0.1}}
```

The second solve works out convection and radiation for the board's size and heat, since a small board sheds heat faster per area than a large one, unless you set `thermal.part_h_w_m2k`. It leaves out conduction through connectors and wires, so small boards read somewhat hot.

Part rise only warns until you set `thermal.max_part_rise_c`, because the limit depends on your parts' ratings and your enclosure. Give a part's real dissipation with `thermal.part_watts: {U2: 0.18}`, or 0 to leave it out.

## Layout options

All of these go under `layout`, and all are optional except `footprints`.

`footprints` names each part's footprint as `library:name`, where the library is a `.pretty` folder. The build looks in the `--footprints` folders, then edaloop's stand-ins, then KiCad's own libraries, and checks every part before it places any. A part with no footprint, a name with no library (as a board imported from EAGLE has), a library it can't find or a footprint its library doesn't have stops the build, with each part named. `--json` lists them under `detail.parts`.

`edaloop describe` from a board catches this first. If any of the board's footprints wouldn't load, it copies every footprint the board uses into `<description>_footprints/imported.pretty`, beside the description, and names each part `imported:<name>`. The copies are the board's own, flattened to the top side at no rotation, and given a courtyard where the board had none. The description's first lines give the build command, with `--footprints <description>_footprints`. Copying needs KiCad's Python. Without it, the description keeps the board's names, and the result's `board_footprints.problem` says what's missing. A board whose footprints already load is left alone.

| Key | Default | Meaning |
|---|---|---|
| `axis` | `x` | The direction the RF line runs. |
| `line` | | The RF line's position across the board. |
| `gap_mm` | `0.1` | Courtyard gap between parts on the line. |
| `rotation` | none | A part's rotation in degrees, by reference, in place of the derived one. A part whose RF pads sit on different corners, like a TT1224 limiter, is derived turned to put both pads on the line, which makes it longer along the row. If the row then overflows the outline, such parts are set square and the line jogs at them: see [RF pads on a part's corners](rf-layout-rules.md#rf-pads-on-a-parts-corners). Set a rotation here to choose yourself. Whatever the rotation, where a part's exit pad sits off its entry pad's line, the line jogs there, so each hop stays on its pads' axis. |
| `place` | none | Moves named parts from where placement put them, as `{REF: [dx, dy]}` in mm, after everything is placed and before the outline is checked. For what the rules can't place. A part whose entry and exit RF pads sit on one side with other pins between them, like operacake's switch U2 (pins 6 and 8), gets the line laid along that side, so the hop out of the middle pad has to bend round the pins beyond it. There, `{U2: [2, 0], C9: [0, 2]}` drops the next part below the exit pad and clears the critic. A move that lands a part's courtyard on another part is refused, with both named. |
| `row_gap_mm` | `1.3` | Gap between one row's parts and the next row's. |
| `columns` | `1` | Rows per column follow from this, for a long many-channel board. Connectors in inner columns aren't at a board edge, so this suits vertical connectors. |
| `column_gap_mm` | `3` | Gap between columns. |
| `channel_parts` | `beside` | `shelf` puts every off-row part in the shelf instead. |
| `rf_keepout_mm` | `1.2` | Half width of the band along the RF line that channel parts stay out of. |
| `tap_plane_void_mm` | off | Clears In1.Cu under the pads of parts hanging off the RF line, this far past each pad. This cuts your reference plane, so it's your call. Read `em_hops` on the result. |
| `schematic_pages` | one per path | `one` draws everything on one sheet. A board with one path always does. |
| `unlisted_pads` | refuse | `open` lets a footprint have pads the schematic doesn't. The rules check still reports them, because they may be pins a symbol dropped. Confirm them against the datasheet first. |

The RF layout rules' limits go under `project.rf_layout`, which the build copies to `rf_layout:` in `edaloop.yaml`, so that placement builds to the same numbers the `rf_layout` check measures. Placement reads `tap_stub_mm` from it. Unset, a shunt tap's RF pad goes on the line. Set to a number, every tap sits beside the line, that far from the line's edge, on a stub. Set to `{REF: mm}`, the taps named do, and the rest stay on the line. A tap beside the line takes no length along it. That measured worse on gain_block_demo: see [Taps beside the line](rf-layout-rules.md#taps-beside-the-line).

## When the parts don't fit

A build that can't fit the parts inside the outline, 0.5 mm in from its edge, stops and says by how much and what to change. The data comes first:

```
edaloop --json build description.yaml -o out
```

exits 2 and prints `{"error": ..., "detail": {...}}`. `detail.error` is `outline_overflow`, and `detail` holds the parts' span, the outline, `over_mm` (how far past each edge), the row's length along `layout.axis` (`row_mm`) and its width across, the parts set square, `folded` (whether the placer tried to fold the row into the outline, and no fold fit), and `hint`. When the row is too long for the board along its axis but would fit turned, `fits_on_axis` names the other axis and the hint says to set it. Otherwise the hint names the side to move `layout.line` away from, or how much to shorten the row. The same hint ends the error line people see.

A build that fails leaves its output folder as it found it, so the next build can go into the same folder. A folder that already holds files is still refused.

## Limits

- Placement handles straight RF rows, and folds one between a loop-through part's ports or into a fixed outline too short for it. It doesn't fold a row to avoid an obstacle or fit an outline with a cut-out, and it doesn't fold the parts off the rows: on a board with hundreds of them, such as LibreVNA or LimeRFE, they pack below the rows in a strip far longer than the rows are, and that strip, not the rows, is what no fixed outline holds.
- Ownership reads nets, not intent. A part with a net to only one row goes to that row, even if you wanted it elsewhere.
- Routing a long multi-channel board can leave nets open, and the flow reports them rather than hiding them. See the review for each board.
