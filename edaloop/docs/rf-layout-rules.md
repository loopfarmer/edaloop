# RF layout rules

A clean DRC says nothing about any of this. These rules come from RF design reviews of production-intent receive boards, and each one moved a real board. edaloop builds to them and the `rf_layout` check measures them. Change a limit under `rf_layout:` in `edaloop.yaml`.

| Rule | Why | Built by | Measured as |
|---|---|---|---|
| Weld a via to each ground pad, at its outer end | Ground reaches the plane through the shortest path, and the via's hole stays out of the pad, so solder can't wick into it | `ground.pad_vias` | `ground_pattern` |
| Fill an RF connector's ground pads with vias, as many as fit | The cable's shield current returns to the plane through them, and one via is an inductance in series with the shield. General RF practice, not a maker's drawing: none of these connectors' drawings counts vias. `connector_ground_vias` (default `fill`, or a number) sets it, and `connector_ground_min_vias` (default 2) is the floor below which it's reported | `ground.pad_vias`, for parts `rf_rules.is_rf_connector` recognises by footprint, part number or value. Every site must clear the part's own rule areas: KiCad's U.FL forbids vias between its ground pads. A `per_pad` count in the description wins | `connector_ground` |
| Follow the maker's via pattern for each part | The part was characterised on that layout. Counts and positions come from its drawing | `rf_rules.LAND_PATTERNS`, matched by `mpn` | `ground_pattern` |
| Fence both sides of an RF line at 70 mil, or a twentieth of a guided wavelength if that's tighter | It holds the coplanar ground to the line. Fences go in after the route, where the copper leaves room | `ground.finish` lays the fence at pitch, then `fill_fence` puts a via in each gap and hole wherever one is legal, and runs again until nothing changes | `fence`: gaps over two pitches between ground connections, and fence-line points over one pitch from any. A GND pad within `fence_band_mm` (1.5 mm) of the trace's edge counts as a ground connection |
| Connect pours solidly, not with spokes | A thermal spoke is a slot in the return path, and a reflowed board doesn't need one | `kicad_ops` | Zone settings |
| Take unused inner pads off signal vias within 1 mm of an RF line | A through via's ring holds the plane back, and that antipad can reach under the trace | `ground.finish` | `plane` |
| Keep the plane whole across the trace's full width | A check along the centreline misses a bite out of one side | `route` keeps other nets off the reference layer | `plane`, sampled across the width |
| Keep supply and control on an inner layer | It leaves the top layer to RF. A pad keeps a short stub and a via | `route.layer_costs` makes the top layer dearer | `top_supply` |
| Keep other nets out of an RF part's pad gap | A trace between a choke's or a link's pads couples into the line | Reported. The layer cost makes it rare, and nothing forbids it yet | `pad_gap` |
| Keep other nets out of a limiter's pad field, on every layer | It splits the ground the part is referenced to | Reported, as above | `limiter` |
| Don't run other nets alongside an RF line | A long parallel run couples. Angle away from the line | Reported, as above | `parallel` |
| Put a choke at the RF junction, with its pad in the trace if it fits | A stub loads the line | Placer and `plan_tap` | `tap`, past `tap_max_mm` (default 3 mm) from the line's centre |
| Or, if you choose, put a shunt tap's RF pad beside the line: `tap_stub_mm` from the line's edge to the pad's edge, on a stub of the line's width. Off by default (`null`) | The line then keeps its own width past the tap, so `impedance` no longer sees a low section. It doesn't take the land's capacitance away, and measured worse here: see [Taps beside the line](#taps-beside-the-line). 0.1 mm is a common fabricator's minimum solder-mask web, the least that keeps the land's mask opening off the line: an assumption, so check your fabricator's. Either axis, either side: a rail's choke rises from the line and a part to ground hangs below it | Placer, `kicad_ops._derive_layout`, from the line's width and the pad's own size. `plan_tap` lays the stub, and `ground.pad_vias` plans around it | `taps_through_land` lists each two-pad shunt part whose land the net's track enters and leaves. With `tap_stub_mm` set, each one is a `tap_land` warning |
| Lead an RF line straight out of its pad, past the part's own pads and courtyard, before it bends | A line that leaves a corner pad across the part runs over the part's own ground pads, which shorts it, or squeezes past them with less than the clearance. See [RF pads on a part's corners](#rf-pads-on-a-parts-corners) | `route_classes.plan_hop`, when a hop's straight spine would cross a pad of either end's part on another net. `ground.pad_vias` plans its vias round the same runs. A row that overflows with a part turned to put corner pads on the line is placed again with the part square and the line jogging at it: `kicad_ops._square_rot` and `_row_lines` | DRC, and `rf_path`: a hop that has to bend reads as off axis and over its floor, as it is |
| Put the smallest bypass nearest the choke's cold end, then larger | At GHz only the small one is still a capacitor. Feed arrives at the largest first | Placer | `bypass_order`, `bypass_far` |
| Choose a capacitor from its measured S-parameters | A real 0402 has about 0.35 nH of its own, so above its resonance it's an inductor. An ideal-part reactance misleads | Your decision. The check estimates from typical inductance and asks you to look | `series_cap`, `bypass_z` |
| Don't tie filters at a plain node | A lossless node reflects what each filter rejects, so loss swings with phase | Your decision | `tee` |
| State the stackup and CPWG callout to the fabricator | A width only means 50 ohm on the stackup it was solved for | `review_pack` writes `fab_notes.txt` | Review |

## Taps beside the line

On every routed demo board, `impedance` reports a 28.9 ohm section on each bias choke's 0603 land, where the 0.36 mm line runs through it, and `em_hops` reads those hops at about 28.7 dB return loss against 48 dB for the rest. Putting the land beside the line was tried on gain_block_demo, with `tap_stub_mm: 0.1` and the full `edaloop make`. The line was the same, and each choke's land sat 0.72 mm off it on a 0.36 mm stub. These are EMerge readings of copper alone, with no parts, in band (0.9 to 1.5 GHz):

| | Pad on the line (default) | Pad beside the line, 0.1 mm stub |
|---|---|---|
| `em_hops` LNA1OUT, LNA2OUT, COAX | 28.7, 28.6, 28.7 dB | 25.0, 24.9, 25.0 dB |
| `em_hops` IN, LNA1IN, LNA2IN | 48.2, 47.9, 48.4 dB | 48.2, 48.3, 48.4 dB |
| Worst-case insertion loss, summed along the chain | 0.112 dB | 0.149 dB |
| `impedance` | warn: 28.9 ohm over 512 um on each choke's land | pass |
| `rf_path` | pass, 0.01% over its floor | fail, 7.46% over: each choke hop 10.8 to 11.1% |
| `drc` | pass | pass |
| Ground vias after `ground`, and on the finished board | 36, 105 | 36, 102 |

So the land beside the line is worse by about 3.7 dB. What loads the line is the land's area over In1.Cu, 0.21 mm below. On the line, 0.34 mm² of the land's 0.83 mm² is the line's own copper, so the land adds about 0.49 mm². Beside it, the whole land and the stub are added, about 0.87 mm². That's a rough area count, not a solve, but it points the same way as the solve. A 0.1 mm stub is far too short at 1.5 GHz to isolate the land, and `impedance` passes only because a cross-section of the line no longer crosses the land.

`rf_path` fails because its floor joins the net's pads by straight lines, which can run diagonally through the tap, and the routed line plus a square stub is longer. That's how the floor is defined, so a board with taps beside the line would need a person to accept that reading or the floor's definition to change.

To lower a choke's load, the copper under it has to change: a smaller land, or `layout.tap_plane_void_mm`, which clears the plane under it and so is your call. Neither was measured here.

## RF pads on a part's corners

Some parts put RF in and out on opposite corners: a TT1224 limiter (RLM-63-2W+) has pad 1 at one corner and pad 4 at the other, with ground pads 2, 3, 5 and 6 and the land pattern's plated ground holes between and around them. A SOT-343 amplifier does the same on a smaller scale.

**Placement.** Such a part is aimed pad to pad by default, so that both RF pads sit on the line: on gain_block_demo each SOT-343 turns 141.8 degrees, and that board is unchanged. On cband_element the TT1224 turns 49.6 degrees and spans 13.3 mm along the row, and the row overflowed its 61.7 mm board. When a row overflows, placement tries again with each part that was turned to an odd angle set square instead (`_square_rot`): of the four square rotations, those where the entry pad's line leaves it backwards along the row and the exit pad's forwards, neither crossing the part's own pads on another net, and of those the one with the least offset across the row between the two pads. The TT1224 then sits at 90 degrees, 10.16 mm along the row, with its pads 5.08 mm apart across it.

The line then jogs at the part (`_row_lines`). The parts after it go on the exit pad's line, so that every hop is straight and on its pads' axis, with no bend at all. The stretch of the row with the most parts, counting those stacked off its taps, keeps the line the description gives, and the rest move across to suit. The keep-out band that channel parts stay out of follows each stretch. A board that fits is never set square by this, and a description can set any part's rotation under `layout.rotation`, square or not; the line jogs wherever the exit pad sits off the entry pad's line. The headroom a bias stack is allowed is still worked out from the stated line, so a stack on a moved stretch is held only by the outline check.

**Routing.** `plan_hop` lays each hop as before, straight runs and 45-degree bends necked at a fine-pitch end. If that would cross a pad of either end's own part on another net, that end instead leaves its pad straight out (`pad_exit`): along the axis that clears the part's own pads by the line's half width and the board's clearance, the one nearest the way the line is going, and far enough to pass those pads by the clearance and the part's courtyard. Only then does it bend, at 45 degrees, toward the other end. The shortest such path that clears everything is laid. If none clears, the straight spine is drawn as it was, and the DRC sensor reports it, so a crossing is never hidden.

**Measured as.** `rf_path` isn't changed. A hop led out of a pad and bent reads as off axis, since its pads share neither x nor y, and as over its floor, since the floor is the straight distance between the pads. That's what it is: the bend is length, and the offset is placement that routing can't remove. A jog in placement keeps every hop on axis and at its floor, which is why placement jogs rather than bends.

This rule has no threshold of its own to set under `rf_layout:`. It uses the board's Default clearance, the RF line's width and the part's own courtyard and pads.

## What `em_hops` solves for each hop

`em_hops` cuts a window round each RF hop from the routed board and solves it as a 2-port in EMerge through circuitRF. It holds the hop's tracks, the tap pads on the line, the ground fill and ground pads on each signal layer, the ground vias in the window, and each signal layer's reference plane. The parts aren't in it, and neither are their own lands: the track is cut where it enters each end land, and a port is a lumped sheet there. Read the result as what the routing adds to the parts' datasheet figures.

| Hop | Reference planes | Marked assumed when |
|---|---|---|
| Wholly on F.Cu | `reference_layer`, or In1.Cu on a board with inner layers | Never. It's the plane the stackup is routed against |
| Wholly on B.Cu | `bottom_reference_layer`, or the mirror of the top's | The mirror is used |
| Through vias of its own | Each signal layer's own. F.Cu's and B.Cu's as above. An inner layer's are `inner_reference_layers[layer]`, or the inner layers either side of it | A mirror or the inner default is used, or an antipad isn't measured |

A hop through a via is solved as one model across its layers:

- **The via:** its barrel is a solid cylinder of its drill, from the top of its span to the bottom, so a through via used to reach an inner layer keeps its stub. The span comes from the stackup entry of exactly that span. A blind or buried via needs one, or the hop is refused.
- **Its rings:** drawn on every layer of its span, unless the board removes the unused ones. Then only the layers a track joins, and the two ends if it keeps them.
- **Its antipads:** each reference plane is drawn whole across the window, with a round hole at each signal via. The hole's radius is measured from the board's own ground fill on that layer, the distance from the via's centre to the fill. With no fill there it's the via's ring, or its hole where the ring is removed, plus that layer's pour clearance, or 0.2 mm if no pour states one. That one is marked assumed. A hole is drawn as a 32-gon with its corners on the circle, so its edge midpoints are 0.5% inside it.
- **Layers it passes:** a layer inside the via's span that's neither a signal layer nor a reference is drawn from its own ground fill, with the fill's own clearance round the via.
- **Ground vias:** every ground via in the window, at the fabricator's preferred 0.61 mm pad and 0.254 mm drill, on the stackup's via entry that joins all the hop's layers.
- **Ports:** each on its own end's layer, returning through the nearest reference plane with metal under it.

These are refused, each with its reason in the finding:

- A via whose layers have no plane in common, so that the return current changes plane there, with no ground via within `return_via_mm` of it. That defaults to the window's margin, `margin_mm`, 2.5 mm, so that the return path is inside the model.
- A ground fill that reaches a signal via's ring, which shorts it on the board as filled.
- A via with a padstack that differs by layer, a span the stackup has no entry for, or a signal layer that's another signal layer's reference plane. That includes every hop through a via on a two-layer board.
- A hop that changes layer with no via of its own, through a drilled pad, since that pad's barrel isn't drawn.

Each hop's assumptions are listed under `assumed` in its metrics, in its return loss finding, and in an `em_hops:<net>:assumed` warning. Each via's measured antipads, which layers kept a ring, and the distance to its nearest ground via are under `signal_vias`.

## What's measured, and what isn't

Readings that lean on a typical inductance are estimates, and they say so. The check can't see a choke's self-resonance, a part's stability or anything populated, so those stay with models and the bench.

A warning here isn't a fault. A fence gap at a part's own pads has no legal via site, and a short low-impedance section at a land may be fine. Report the reading, and let a person decide.

## Closing a via fence

A first pass that places vias at pitch and skips any site that fails a clearance leaves a count of skipped sites, and that count sounds worse than it is: a part's ground pad beside the line often covers the same stretch. So the fence is judged the way a reviewer reads it, on the board as built.

- **Gaps.** Along each side of each top-layer RF segment, the run between ground connections is measured. A ground connection is a GND via, or a GND pad whose edge lies within `fence_band_mm` of the trace's edge. A run longer than two pitches is a gap.
- **Holes.** The fence line, at the trace's edge plus `fence_line_mm` (0.58 mm), is walked in 0.2 mm steps on segments of any length. A point more than one pitch from any ground connection is a hole. This catches short hops between parts, which can't hold a gap. A point inside a part's courtyard is skipped.
- **Fill.** `edaloop finish` tries sites along each gap and out from the trace's edge, nearest the fence line first, 0.02 mm apart along the line, since a legal site between two runs can be 0.03 mm wide. It places a via wherever one clears every pad, track and via, and runs again, since one via can close two gaps. KiCad's DRC still removes any via it objects to.
- **What's left.** `finish` lists what it couldn't close, and `rf_layout` reports it. Moving copper is a person's call: often a control run beside the line can move 0.3 to 0.5 mm, or a run drops to an inner layer, and that frees a site.
