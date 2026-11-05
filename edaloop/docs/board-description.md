# Board descriptions

A description is one YAML file with a board's parts, nets, RF path and stackup. `testbenches/` holds examples. `edaloop describe` writes one from a schematic or a board, and marks with `REQUIRED` what the source can't say, such as a load's current. A build refuses until a person fills those in.

`assumed` lists defaults it used, such as the stackup when reading a schematic. `likely_rails` lists nets that look like supplies but have no currents yet. They aren't checked until you add them under `project.rails` with their source and load currents, and until then the power check fails rather than passing with nothing to check.

## How a board is placed

Placement comes from the nets and the footprints' courtyards. Nothing is placed by hand.

1. **RF rows.** Each RF path is one row. Every part is rotated so that its RF pads lie on the line, and placed as close to the last as the courtyards allow.
2. **Bias stacks.** A two-pin part with one pin on an RF net is a tap. Rails rise from it in a column, with the smallest capacitor nearest the choke.
3. **Channel parts.** On a board with several rows, a part that serves one row goes in and beside that row. Ownership comes from the nets: a part is a row's if the nets lead only to that row. A net or part reaching two rows is shared. When a rail has at least one capacitor per row, each row gets an equal share, and any left over go with the shared parts.
4. **One path.** On a board with one RF path, every other part with a net goes round it, nearest what it joins.
5. **Shared parts.** The rest goes in a shelf after the last row, such as headers and control logic.

## The schematic

A board with one RF path is drawn on one page. A board with several gets one page per path, holding the parts only that path uses, one page for what they share, and a root page linking them. Pages connect by net name. Every build exports the netlist and compares it with the description, so a drawing can't change a connection unnoticed.

## Heat

The temperature check solves twice. The first solve is the copper's own heat, judged against `power.max_rise_c`. The second adds each part's heat, found by following each load's stated current through series resistors and chokes to the part that uses it, at `thermal.supply_v`. All of that power counts as heat, and the result is the board's temperature under the part, so junctions run hotter.

The second solve works out convection and radiation for the board's size and heat, since a small board sheds heat faster per area than a large one, unless you set `thermal.part_h_w_m2k`. It leaves out conduction through connectors and wires, so small boards read somewhat hot.

Part rise only warns until you set `thermal.max_part_rise_c`, because the limit depends on your parts' ratings and your enclosure. Give a part's real dissipation with `thermal.part_watts: {U2: 0.18}`, or 0 to leave it out.

## Layout options

All of these go under `layout`, and all are optional except `footprints`.

| Key | Default | Meaning |
|---|---|---|
| `axis` | `x` | The direction the RF line runs. |
| `line` | | The RF line's position across the board. |
| `gap_mm` | `0.1` | Courtyard gap between parts on the line. |
| `row_gap_mm` | `1.3` | Gap between one row's parts and the next row's. |
| `columns` | `1` | Rows per column follow from this, for a long many-channel board. Connectors in inner columns aren't at a board edge, so this suits vertical connectors. |
| `column_gap_mm` | `3` | Gap between columns. |
| `channel_parts` | `beside` | `shelf` puts every off-row part in the shelf instead. |
| `rf_keepout_mm` | `1.2` | Half width of the band along the RF line that channel parts stay out of. |
| `tap_plane_void_mm` | off | Clears In1.Cu under the pads of parts hanging off the RF line, this far past each pad. This cuts your reference plane, so it's your call. Read `em_hops` on the result. |
| `schematic_pages` | one per path | `one` draws everything on one sheet. A board with one path always does. |
| `unlisted_pads` | refuse | `open` lets a footprint have pads the schematic doesn't. The rules check still reports them, because they may be pins a symbol dropped. Confirm them against the datasheet first. |

The RF layout rules' limits go under `project.rf_layout`, which the build copies to `rf_layout:` in `edaloop.yaml`, so that placement builds to the same numbers the `rf_layout` check measures. Placement reads `tap_stub_mm` from it. Unset, a shunt tap's RF pad goes on the line. Set, it sits beside the line, that far from the line's edge, on a stub. That measured worse on gain_block_demo: see [Taps beside the line](rf-layout-rules.md#taps-beside-the-line).

## Limits

- Placement handles straight RF rows. It doesn't fold a path, avoid an obstacle or fit a fixed outline with a cut-out.
- Ownership reads nets, not intent. A part with a net to only one row goes to that row, even if you wanted it elsewhere.
- Routing a long multi-channel board can leave nets open, and the flow reports them rather than hiding them. See the review for each board.
