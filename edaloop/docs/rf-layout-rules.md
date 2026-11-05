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

## What's measured, and what isn't

Readings that lean on a typical inductance are estimates, and they say so. The check can't see a choke's self-resonance, a part's stability or anything populated, so those stay with models and the bench.

A warning here isn't a fault. A fence gap at a part's own pads has no legal via site, and a short low-impedance section at a land may be fine. Report the reading, and let a person decide.
