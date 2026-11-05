# RF layout rules

A clean DRC says nothing about any of this. These rules come from RF design reviews of production-intent receive boards, and each one moved a real board. edaloop builds to them and the `rf_layout` check measures them. Change a limit under `rf_layout:` in `edaloop.yaml`.

| Rule | Why | Built by | Measured as |
|---|---|---|---|
| Weld a via to each ground pad, at its outer end | Ground reaches the plane through the shortest path, and the via's hole stays out of the pad, so solder can't wick into it | `ground.pad_vias` | `ground_pattern` |
| Fill an RF connector's ground pads with vias, as many as fit | The cable's shield current returns to the plane through them, and one via is an inductance in series with the shield. General RF practice, not a maker's drawing: none of these connectors' drawings counts vias. `connector_ground_vias` (default `fill`, or a number) sets it, and `connector_ground_min_vias` (default 2) is the floor below which it's reported | `ground.pad_vias`, for parts `rf_rules.is_rf_connector` recognises by footprint, part number or value. Every site must clear the part's own rule areas: KiCad's U.FL forbids vias between its ground pads. A `per_pad` count in the description wins | `connector_ground` |
| Follow the maker's via pattern for each part | The part was characterised on that layout. Counts and positions come from its drawing | `rf_rules.LAND_PATTERNS`, matched by `mpn` | `ground_pattern` |
| Fence both sides of an RF line at 70 mil, or a twentieth of a guided wavelength if that's tighter | It holds the coplanar ground to the line. Fences go in after the route, where the copper leaves room | `ground.finish` | `fence` |
| Connect pours solidly, not with spokes | A thermal spoke is a slot in the return path, and a reflowed board doesn't need one | `kicad_ops` | Zone settings |
| Take unused inner pads off signal vias within 1 mm of an RF line | A through via's ring holds the plane back, and that antipad can reach under the trace | `ground.finish` | `plane` |
| Keep the plane whole across the trace's full width | A check along the centreline misses a bite out of one side | `route` keeps other nets off the reference layer | `plane`, sampled across the width |
| Keep supply and control on an inner layer | It leaves the top layer to RF. A pad keeps a short stub and a via | `route.layer_costs` makes the top layer dearer | `top_supply` |
| Keep other nets out of an RF part's pad gap | A trace between a choke's or a link's pads couples into the line | Reported. The layer cost makes it rare, and nothing forbids it yet | `pad_gap` |
| Keep other nets out of a limiter's pad field, on every layer | It splits the ground the part is referenced to | Reported, as above | `limiter` |
| Don't run other nets alongside an RF line | A long parallel run couples. Angle away from the line | Reported, as above | `parallel` |
| Put a choke at the RF junction, with its pad in the trace if it fits | A stub loads the line | Placer and `plan_tap` | `tap` |
| Put the smallest bypass nearest the choke's cold end, then larger | At GHz only the small one is still a capacitor. Feed arrives at the largest first | Placer | `bypass_order`, `bypass_far` |
| Choose a capacitor from its measured S-parameters | A real 0402 has about 0.35 nH of its own, so above its resonance it's an inductor. An ideal-part reactance misleads | Your decision. The check estimates from typical inductance and asks you to look | `series_cap`, `bypass_z` |
| Don't tie filters at a plain node | A lossless node reflects what each filter rejects, so loss swings with phase | Your decision | `tee` |
| State the stackup and CPWG callout to the fabricator | A width only means 50 ohm on the stackup it was solved for | `review_pack` writes `fab_notes.txt` | Review |

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
