# The placement critic

`edaloop critic` reads a placed board and estimates what it will cost to route. It also runs as the `placement` check in the `place` stage. It needs no router, no KiCad and no solver. It reads only the pads, the chain file and `board_description.yaml`, and takes seconds: 14 s on a finished 95-RF-net combiner placement, or about a minute on its first placement, where RF lines came back blocked and their search windows grew. So it can run after every placement move, or inside a placement search.

All of its readings are estimates, and each one reads low:

| Reading | What it does | Why it reads low |
|---|---|---|
| `rf_hops` | Finds each RF net's shortest path on its own layer around every other pad, on a 0.1 mm grid, then pulls it straight between corners. A net named on several chain rows, such as a combining node, is one tree: each later row joins the copper already laid. Nets are laid shortest first, and each laid line is an obstacle to the nets after it. | A router can't beat the shortest path on the layer without a via, and an RF line shouldn't take one. The critic doesn't see the fence or the signal routing that arrives later, so the real route comes out longer. On the combiner board it read the mixer RF lines at 62–74 % over their floor; the router laid them 102 % over. |
| `escapes` | A dedicated pin's net goes only to one two-pad part, such as a decoupling, ACG or pull-up part. Each escape is modelled as a stub straight out of its pin, then a straight run to the part. The critic reports an escape that crosses the IC's other pins, and pairs of escapes that cross each other, meaning the parts are out of pin order. | A crossing can sometimes be routed through a via. One with no room for a via is an unroutable pin. |
| `rf_crossings` | Lays each non-RF net's spanning tree over the RF lines and counts where they cross. A crossing within `limiter_band_mm` of a limiter's pads is reported. | Spanning trees are the shortest connections, so a real route crosses at least as often. |
| `congestion` | RUDY (Spindler and Johannes, 2007): each net's half-perimeter wire is spread over its bounding box and summed per tile, then compared with the tile's supply of `route_layers / track_pitch_mm`. | Half-perimeter is the least wire a net can use, so a tile over 1 is over for certain. |

`cost` weighs all four readings into one number for a placement search to minimise. The weights are set in `placement.cost_weights`.

Every limit can be changed under `placement:` in `edaloop.yaml`: `rf_layer`, `clearance_mm`, `grid_mm`, `neck_mm`, `window_mm`, `escape_width_mm`, `min_ic_pads`, `limiter_band_mm`, `tile_mm`, `track_pitch_mm`, `max_utilisation`, `ground_nets` and `cost_weights`. The RF limit is `rf.max_over_pct`, the same one `rf_path` uses.

Severity: an RF net over its limit, or with no path at all, is an error. A route can only be longer than the critic's estimate, so the routed net will fail `rf_path` too. Escapes, limiter crossings and congestion are warnings, because a via or a layer change can sometimes clear them.

What it can't see: fence and stitching vias that `finish` adds, return-path quality, coupling between parallel lines, and anything that depends on the router's choices. Those stay with `rf_layout`, `rf_path` and the EM checks after routing.
