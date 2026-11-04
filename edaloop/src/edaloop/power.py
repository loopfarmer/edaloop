"""Power nets as their own concern: voltage drop and heating on the copper actually laid.

DRC passes a 0.2 mm trace carrying an amp, since no rule knows the current. This
builds each rail's tracks and vias into a resistor network, drives it with the
stated load currents, and reports the drop at each load and the temperature rise
of each segment.

The rise uses IPC-2221's curve fit, which is a first-order, usually conservative
estimate for a single trace. It's a screen for routing, not a thermal simulation:
it ignores neighbouring heat sources and pours. A pour on the rail counts as
carrying current only if a track reaches it, so a rail fed mainly by a pour reads
pessimistic, and the result says so.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

COPPER_RHO_OHM_MM = 1.72e-5
COPPER_ALPHA = 0.00393
IPC_K_OUTER, IPC_K_INNER = 0.048, 0.024
PLATING_UM = 25.0
WELD_MM = 0.002


def ipc2221_rise_c(current_a: float, width_mm: float, thickness_um: float, outer: bool) -> float:
    """IPC-2221 temperature rise: I = k * dT^0.44 * A^0.725, with A in square mils."""
    if current_a <= 0:
        return 0.0
    area_mil2 = (width_mm / 0.0254) * (thickness_um / 25.4)
    k = IPC_K_OUTER if outer else IPC_K_INNER
    return (current_a / (k * area_mil2 ** 0.725)) ** (1 / 0.44)


def ipc2221_min_width_mm(current_a: float, rise_c: float, thickness_um: float, outer: bool) -> float:
    """The narrowest track that stays within a temperature rise."""
    k = IPC_K_OUTER if outer else IPC_K_INNER
    area_mil2 = (current_a / (k * rise_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (thickness_um / 25.4) * 0.0254


def via_resistance_ohm(drill_mm: float, height_mm: float) -> float:
    plating = PLATING_UM / 1000
    area = math.pi * ((drill_mm / 2 + plating) ** 2 - (drill_mm / 2) ** 2)
    return COPPER_RHO_OHM_MM * height_mm / area


@dataclass
class RailResult:
    net: str
    connected: bool
    unreached: list
    drops_mv: dict
    worst_drop_mv: float
    worst_rise_c: float
    worst_segment: dict | None
    min_width_mm: float
    total_a: float
    segments: int
    vias: int


def solve_rail(net: str, tracks: list, pads: dict, source: str, loads: dict, *, thickness_um: dict,
               board_height_mm: float, outer_layers=("F.Cu", "B.Cu"), pad_layer_map: dict | None = None) -> RailResult:
    """Solve one rail. pads: "REF.PIN" -> (x, y, half_w, half_h). loads: "REF.PIN" -> amps."""
    nodes: list[tuple] = []

    def node(x, y, layer):
        for i, (nx, ny, nl) in enumerate(nodes):
            if nl == layer and abs(nx - x) < WELD_MM and abs(ny - y) < WELD_MM:
                return i
        nodes.append((x, y, layer))
        return len(nodes) - 1

    edges = []                 # (a, b, resistance, segment or None)
    segs = [t for t in tracks if t.kind != "via"]
    vias = [t for t in tracks if t.kind == "via"]
    layers = sorted({t.layer for t in segs})
    # A segment is split wherever another item lands on it, so tee junctions conduct.
    breaks: dict[int, list] = {i: [] for i in range(len(segs))}
    # A via's pad joins any track it touches, so a via landing beside a track still conducts.
    points = [(p[0], p[1], t.layer, 0.0) for t in segs for p in (t.start, t.end)]
    points += [(v.start[0], v.start[1], lyr, v.width / 2) for v in vias for lyr in layers]
    for i, s in enumerate(segs):
        ax, ay = s.start
        dx, dy = s.end[0] - ax, s.end[1] - ay
        length2 = dx * dx + dy * dy or 1e-12
        for px, py, lyr, radius in points:
            if lyr != s.layer:
                continue
            u = ((px - ax) * dx + (py - ay) * dy) / length2
            if 1e-6 < u < 1 - 1e-6 and math.hypot(ax + u * dx - px, ay + u * dy - py) < s.width / 2 + radius:
                breaks[i].append(u)
    for i, s in enumerate(segs):
        ax, ay = s.start
        dx, dy = s.end[0] - ax, s.end[1] - ay
        cuts = [0.0, *sorted(set(breaks[i])), 1.0]
        previous = node(ax, ay, s.layer)
        for u0, u1 in zip(cuts, cuts[1:]):
            here = node(ax + u1 * dx, ay + u1 * dy, s.layer)
            length = math.hypot(dx, dy) * (u1 - u0)
            t_um = thickness_um.get(s.layer, 35.0)
            area = s.width * t_um / 1000
            edges.append((previous, here, max(COPPER_RHO_OHM_MM * length / area, 1e-9), s))
            previous = here
    node_halfwidth: dict[int, float] = {}
    for a, b, _, seg in edges:
        if seg is not None:
            node_halfwidth[a] = max(node_halfwidth.get(a, 0.0), seg.width / 2)
            node_halfwidth[b] = max(node_halfwidth.get(b, 0.0), seg.width / 2)
    for v in vias:
        ends = [node(v.start[0], v.start[1], lyr) for lyr in layers]
        for i, (nx, ny, nl) in enumerate(list(nodes)):
            if nl in layers and i not in ends:
                reach = v.width / 2 + node_halfwidth.get(i, 0.0)
                if math.hypot(nx - v.start[0], ny - v.start[1]) <= reach:
                    edges.append((ends[layers.index(nl)], i, 1e-9, None))
        r = via_resistance_ohm(v.drill or 0.2, board_height_mm)
        for a, b in zip(ends, ends[1:]):
            edges.append((a, b, max(r / max(len(ends) - 1, 1), 1e-9), None))
    # Copper touching a pad is joined through the pad, which is treated as a perfect conductor.
    def pad_layers(name):
        return (pad_layer_map or {}).get(name, ("F.Cu", "B.Cu"))

    pad_nodes: dict[str, int] = {}
    for name, (x, y, hw, hh) in pads.items():
        pad_nodes[name] = len(nodes)
        nodes.append((x, y, f"pad:{name}"))
        for i, (nx, ny, nl) in enumerate(nodes[:-1]):
            if not str(nl).startswith("pad:") and nl in pad_layers(name) and abs(nx - x) <= hw + WELD_MM and abs(ny - y) <= hh + WELD_MM:
                edges.append((pad_nodes[name], i, 1e-9, None))
    n = len(nodes)
    if source not in pad_nodes:
        raise ValueError(f"{net}: source {source} is not a pad on this net")
    for name in loads:
        if name not in pad_nodes:
            raise ValueError(f"{net}: load {name} is not a pad on this net")
    # Reachability from the source decides what can be solved.
    adjacency: dict[int, list] = {i: [] for i in range(n)}
    for a, b, _, _ in edges:
        adjacency[a].append(b)
        adjacency[b].append(a)
    seen, stack = {pad_nodes[source]}, [pad_nodes[source]]
    while stack:
        for nb in adjacency[stack.pop()]:
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    unreached = [name for name in loads if pad_nodes[name] not in seen]
    index = {node_id: k for k, node_id in enumerate(sorted(seen - {pad_nodes[source]}))}
    G = np.zeros((len(index), len(index)))
    I = np.zeros(len(index))
    for a, b, r, _ in edges:
        if a not in seen or b not in seen:
            continue
        g = 1.0 / r
        if a in index:
            G[index[a], index[a]] += g
        if b in index:
            G[index[b], index[b]] += g
        if a in index and b in index:
            G[index[a], index[b]] -= g
            G[index[b], index[a]] -= g
    for name, amps in loads.items():
        if pad_nodes[name] in index:
            I[index[pad_nodes[name]]] += amps
    volts = np.linalg.solve(G, I) if len(index) else np.zeros(0)     # drop below the source

    def v(i):
        return float(volts[index[i]]) if i in index else 0.0
    worst_rise, worst_seg = 0.0, None
    for a, b, r, seg in edges:
        if seg is None or a not in seen or b not in seen:
            continue
        current = abs(v(a) - v(b)) / r
        t_um = thickness_um.get(seg.layer, 35.0)
        rise = ipc2221_rise_c(current, seg.width, t_um, seg.layer in outer_layers)
        if rise > worst_rise:
            worst_rise = rise
            worst_seg = {"layer": seg.layer, "width_mm": seg.width, "current_a": round(current, 3),
                         "start": seg.start, "end": seg.end}
    drops = {name: round(v(pad_nodes[name]) * 1000, 3) for name in loads if pad_nodes[name] in seen}
    return RailResult(net, not unreached, unreached, drops, max(drops.values(), default=0.0), round(worst_rise, 2),
                      worst_seg, min((s.width for s in segs), default=0.0), round(sum(loads.values()), 4),
                      len(segs), len(vias))
