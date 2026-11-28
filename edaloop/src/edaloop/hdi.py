"""HDI and mixed-signal audits and constraint-driven diff-pair helpers: microvia/buried-via spans,
differential-pair length/skew/via-balance, same-layer zone priority collisions, and coupled-pair routing.
"""
from __future__ import annotations

import math
import re

from shapely.geometry import Polygon

from .pcbfile import Board, Track

_PAIR_SUFFIXES = (("_P", "_N"), ("_DP", "_DN"), ("+", "-"))


def _layer_index_map(board: Board) -> dict[str, int]:
    layers = board.copper_layers()
    if layers:
        return {name: i for i, name in enumerate(layers)}
    # Fallback when the board only lists a setup stackup or standard 2-layer names.
    seen = [k[0] for k in board.dielectric_gaps_mm()]
    ordered = list(dict.fromkeys(seen)) or ["F.Cu", "B.Cu"]
    return {name: i for i, name in enumerate(ordered)}


def _span_gap_mm(a: str, b: str, idx: dict[str, int], gaps: dict[tuple[str, str], float]) -> float | None:
    if (a, b) in gaps:
        return gaps[(a, b)]
    if a not in idx or b not in idx:
        return None
    ia, ib = sorted((idx[a], idx[b]))
    rev = {v: k for k, v in idx.items()}
    total = 0.0
    for k in range(ia, ib):
        pair = (rev[k], rev[k + 1])
        if pair not in gaps:
            return None
        total += gaps[pair]
    return total


def _via_stacks(micros: list[Track], tol_mm: float = 0.02) -> dict[tuple[float, float], list[Track]]:
    """Cluster microvias whose centres fall within `tol_mm` of each other."""
    cell = max(tol_mm, 1e-4)
    buckets: dict[tuple[int, int], list[Track]] = {}
    for v in micros:
        bx, by = int(round(v.start[0] / cell)), int(round(v.start[1] / cell))
        buckets.setdefault((bx, by), []).append(v)
    visited: set[int] = set()
    stacks: dict[tuple[float, float], list[Track]] = {}
    for i, v in enumerate(micros):
        if i in visited:
            continue
        cluster = [v]
        visited.add(i)
        queue = [v]
        while queue:
            cur = queue.pop()
            cx, cy = int(round(cur.start[0] / cell)), int(round(cur.start[1] / cell))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for other in buckets.get((cx + dx, cy + dy), []):
                        oid = id(other)
                        if any(id(x) == oid for x in cluster):
                            continue
                        if math.dist(cur.start, other.start) <= tol_mm:
                            cluster.append(other)
                            queue.append(other)
        for j, m in enumerate(micros):
            if any(id(x) == id(m) for x in cluster):
                visited.add(j)
        stacks[(round(v.start[0], 3), round(v.start[1], 3))] = cluster
    return stacks


def audit_vias(board: Board, cfg: dict | None = None) -> tuple[dict, list[tuple]]:
    """Audit through, blind/buried, and microvias for span legality, aspect ratio, and stack depth."""
    c = dict(cfg or {})
    max_aspect = float(c.get("max_microvia_aspect", 1.0))
    max_stacked = int(c.get("max_stacked_microvias", 2))
    stacked_sev = str(c.get("stacked_severity", "error"))
    min_drill = c.get("min_blind_drill_mm")
    idx = _layer_index_map(board)
    gaps = board.dielectric_gaps_mm()
    vias = [t for t in board.tracks if t.kind == "via"]
    through = [v for v in vias if v.via_type == "through"]
    blind = [v for v in vias if v.via_type == "blind"]
    micro = [v for v in vias if v.via_type == "micro"]
    findings: list[tuple] = []
    worst_aspect = 0.0

    for v in micro:
        x, y = round(v.start[0], 3), round(v.start[1], 3)
        if len(v.layers) >= 2 and v.layers[0] in idx and v.layers[1] in idx:
            span = abs(idx[v.layers[0]] - idx[v.layers[1]])
            if span != 1:
                findings.append(("hdi:via:micro_span",
                                 f"microvia on {v.net} at ({x}, {y}) spans {v.layers[0]}..{v.layers[1]} ({span} layers; must be 1)",
                                 "error"))
            gap = _span_gap_mm(v.layers[0], v.layers[1], idx, gaps)
            if gap is not None and v.drill > 0:
                aspect = gap / v.drill
                worst_aspect = max(worst_aspect, aspect)
                if aspect > max_aspect + 1e-6:
                    findings.append(("hdi:via:micro_aspect",
                                     f"microvia on {v.net} at ({x}, {y}) aspect ratio {aspect:.2f} exceeds {max_aspect:.2f} "
                                     f"(dielectric {gap:.3f} mm / drill {v.drill:.3f} mm)",
                                     "error"))

    for v in blind:
        x, y = round(v.start[0], 3), round(v.start[1], 3)
        if len(v.layers) >= 2 and v.layers[0] == v.layers[1]:
            findings.append(("hdi:via:invalid_span",
                             f"blind/buried via on {v.net} at ({x}, {y}) starts and ends on {v.layers[0]}",
                             "error"))
        if min_drill is not None and v.drill < float(min_drill) - 1e-6:
            findings.append(("hdi:via:drill_min",
                             f"blind/buried via on {v.net} at ({x}, {y}) drill {v.drill:.3f} mm below min {float(min_drill):.3f} mm",
                             "error"))

    stacks = _via_stacks(micro)
    stacked_max = 0
    for (sx, sy), group in stacks.items():
        spans = {tuple(sorted((idx[v.layers[0]], idx[v.layers[1]]))) for v in group
                 if len(v.layers) >= 2 and v.layers[0] in idx and v.layers[1] in idx}
        depth = len(spans) if spans else len(group)
        stacked_max = max(stacked_max, depth)
        if depth > max_stacked:
            findings.append(("hdi:via:stacked_depth",
                             f"{depth} stacked microvias at ({sx}, {sy}) exceeds max_stacked_microvias={max_stacked}",
                             stacked_sev))

    metrics = {
        "through": len(through),
        "blind_buried": len(blind),
        "micro": len(micro),
        "stacked_max": stacked_max,
        "max_microvia_aspect": round(worst_aspect, 3),
    }
    return metrics, findings


def _discover_pairs(nets: set[str]) -> list[dict]:
    """Auto-discover differential pairs named `*_P`/`*_N`, `*_DP`/`*_DN`, or `*+`/`*-`."""
    found = []
    used = set()
    for pos_suf, neg_suf in _PAIR_SUFFIXES:
        for net in sorted(nets):
            if net in used or not net.endswith(pos_suf):
                continue
            base = net[:-len(pos_suf)]
            partner = base + neg_suf
            if partner in nets and partner not in used:
                used.update((net, partner))
                found.append({"name": base or net, "pos": net, "neg": partner})
    return found


def audit_diff_pairs(board: Board, cfg: dict | None = None) -> tuple[dict, list[tuple]]:
    """Measure routed length, intra-pair skew, via-count balance, and inter-pair group skew."""
    c = dict(cfg or {})
    explicit = list(c.get("diff_pairs") or [])
    auto = bool(c.get("auto_discover", not explicit))
    include_barrel = bool(c.get("include_via_barrel", False))
    default_max_skew = float(c.get("max_skew_mm", 0.5))
    default_max_via_diff = c.get("max_via_diff")
    skew_sev = str(c.get("diff_pair_severity", "error"))

    idx = _layer_index_map(board)
    gaps = board.dielectric_gaps_mm()
    net_len: dict[str, float] = {}
    net_vias: dict[str, int] = {}
    for t in board.tracks:
        if not t.net:
            continue
        if t.kind == "via":
            net_vias[t.net] = net_vias.get(t.net, 0) + 1
            if include_barrel and len(t.layers) >= 2:
                gap = _span_gap_mm(t.layers[0], t.layers[1], idx, gaps)
                if gap is not None:
                    net_len[t.net] = net_len.get(t.net, 0.0) + gap
        else:
            net_len[t.net] = net_len.get(t.net, 0.0) + math.dist(t.start, t.end)

    pairs = list(explicit)
    if auto:
        known = {(p["pos"], p["neg"]) for p in pairs}
        routed_nets = {t.net for t in board.tracks if t.kind != "via" and t.net}
        for row in _discover_pairs(routed_nets):
            if (row["pos"], row["neg"]) not in known:
                pairs.append(row)

    findings: list[tuple] = []
    rows = {}
    by_group: dict[str, list[float]] = {}
    max_skew_seen = 0.0
    for row in pairs:
        pos, neg = str(row["pos"]), str(row["neg"])
        name = str(row.get("name") or f"{pos}/{neg}")
        l_pos = round(net_len.get(pos, 0.0), 4)
        l_neg = round(net_len.get(neg, 0.0), 4)
        v_pos = net_vias.get(pos, 0)
        v_neg = net_vias.get(neg, 0)
        skew = round(abs(l_pos - l_neg), 4)
        via_diff = abs(v_pos - v_neg)
        max_skew_seen = max(max_skew_seen, skew)
        rows[name] = {
            "pos": pos, "neg": neg, "pos_mm": l_pos, "neg_mm": l_neg,
            "skew_mm": skew, "pos_vias": v_pos, "neg_vias": v_neg, "via_diff": via_diff,
        }
        if l_pos == 0.0 or l_neg == 0.0:
            findings.append((f"hdi:diff_pair:{name}:unrouted",
                             f"diff pair {name} ({pos}/{neg}) has zero routed length ({l_pos}/{l_neg} mm)", skew_sev))
            continue
        limit = float(row.get("max_skew_mm", default_max_skew))
        if skew > limit + 1e-6:
            findings.append((f"hdi:diff_pair:{name}:skew",
                             f"diff pair {name} skew {skew:.3f} mm ({pos}={l_pos:.2f}, {neg}={l_neg:.2f}) exceeds {limit:.3f} mm",
                             skew_sev))
        vlimit = row.get("max_via_diff", default_max_via_diff)
        if vlimit is not None and via_diff > int(vlimit):
            findings.append((f"hdi:diff_pair:{name}:via_mismatch",
                             f"diff pair {name} via mismatch {via_diff} ({pos}={v_pos}, {neg}={v_neg}) exceeds {int(vlimit)}",
                             skew_sev))
        if row.get("min_length_mm") is not None and min(l_pos, l_neg) < float(row["min_length_mm"]) - 1e-6:
            findings.append((f"hdi:diff_pair:{name}:min_length",
                             f"diff pair {name} min length {min(l_pos, l_neg):.2f} mm below {float(row['min_length_mm']):.2f} mm",
                             skew_sev))
        if row.get("max_length_mm") is not None and max(l_pos, l_neg) > float(row["max_length_mm"]) + 1e-6:
            findings.append((f"hdi:diff_pair:{name}:max_length",
                             f"diff pair {name} max length {max(l_pos, l_neg):.2f} mm exceeds {float(row['max_length_mm']):.2f} mm",
                             skew_sev))
        if row.get("group"):
            by_group.setdefault(str(row["group"]), []).append((l_pos + l_neg) / 2.0)

    group_limits = c.get("group_skew_mm") or {}
    default_group_limit = c.get("max_group_skew_mm")
    group_metrics = {}
    for grp, lengths in sorted(by_group.items()):
        spread = round(max(lengths) - min(lengths), 4) if len(lengths) >= 2 else 0.0
        group_metrics[grp] = {"pairs": len(lengths), "skew_mm": spread}
        glim = group_limits.get(grp, default_group_limit)
        if glim is not None and spread > float(glim) + 1e-6:
            findings.append((f"hdi:diff_pair:group:{grp}",
                             f"diff-pair group {grp} inter-pair skew {spread:.3f} mm exceeds {float(glim):.3f} mm",
                             skew_sev))

    return {"pairs": rows, "groups": group_metrics, "pair_count": len(rows), "max_skew_mm": round(max_skew_seen, 4)}, findings


def audit_zone_priorities(board: Board, cfg: dict | None = None) -> tuple[dict, list[tuple]]:
    """Detect same-layer overlapping copper zones belonging to different nets with identical priority."""
    c = dict(cfg or {})
    sev = str(c.get("zone_priority_severity", "error"))
    zones = [z for z in board.zones() if not z["is_keepout"] and z["net"] and len(z["outline"]) >= 3]
    by_layer: dict[str, list[tuple[dict, Polygon]]] = {}
    for z in zones:
        poly = Polygon(z["outline"])
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 1e-9:
            continue
        for lyr in z["layers"]:
            by_layer.setdefault(lyr, []).append((z, poly))

    collisions = []
    findings: list[tuple] = []
    for lyr, items in sorted(by_layer.items()):
        for i, (za, pa) in enumerate(items):
            for zb, pb in items[i + 1:]:
                if za["net"] == zb["net"] or za["priority"] != zb["priority"]:
                    continue
                if not pa.intersects(pb):
                    continue
                area = pa.intersection(pb).area
                if area <= 1e-6:
                    continue
                label_a = f"{za['net']} ({za['name'] or 'unnamed'})"
                label_b = f"{zb['net']} ({zb['name'] or 'unnamed'})"
                collisions.append({"layer": lyr, "net_a": za["net"], "net_b": zb["net"], "priority": za["priority"], "area_mm2": round(area, 4)})
                tag = re.sub(r"[^A-Za-z0-9_]", "_", f"{lyr}_{za['net']}_{zb['net']}")
                findings.append((f"hdi:zone_priority:{tag}",
                                 f"{lyr}: copper zones {label_a} and {label_b} overlap ({area:.2f} mm2) with equal priority {za['priority']}",
                                 sev))
    return {"copper_zones": len(zones), "collisions": collisions, "collision_count": len(collisions)}, findings


def audit(board: Board, cfg: dict | None = None) -> tuple[dict, list[tuple]]:
    """Run all HDI / mixed-signal audits (vias, diff pairs, zone priorities)."""
    c = dict(cfg or {})
    v_metrics, v_find = audit_vias(board, c)
    d_metrics, d_find = audit_diff_pairs(board, c)
    z_metrics, z_find = audit_zone_priorities(board, c)
    return {"vias": v_metrics, "diff_pairs": d_metrics, "zones": z_metrics}, [*v_find, *d_find, *z_find]


def _octilinear_join(a: tuple[float, float], b: tuple[float, float]) -> list[tuple[float, float]]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    adx, ady = abs(dx), abs(dy)
    if adx < 1e-6 or ady < 1e-6 or abs(adx - ady) < 1e-6:
        return [a, b]
    d = min(adx, ady)
    mid = (round(a[0] + math.copysign(d, dx), 4), round(a[1] + math.copysign(d, dy), 4))
    return [a, mid, b]


def plan_coupled_pair(
    pos_net: str,
    neg_net: str,
    start_pos: tuple[float, float],
    start_neg: tuple[float, float],
    end_pos: tuple[float, float],
    end_neg: tuple[float, float],
    width_mm: float,
    gap_mm: float,
    layer: str = "F.Cu",
    via_layers: tuple[str, str] | None = None,
    via_size: float = 0.25,
    via_drill: float = 0.1,
) -> dict:
    """Plan coupled parallel octilinear segments (and optional microvia launches) for a differential pair."""
    ms = ((start_pos[0] + start_neg[0]) / 2.0, (start_pos[1] + start_neg[1]) / 2.0)
    me = ((end_pos[0] + end_neg[0]) / 2.0, (end_pos[1] + end_neg[1]) / 2.0)
    dx, dy = me[0] - ms[0], me[1] - ms[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        raise ValueError("start and end midpoints coincide")
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    # Preserve the polarity side chosen at the launch pads.
    side = 1.0 if ((start_pos[0] - ms[0]) * nx + (start_pos[1] - ms[1]) * ny) >= 0 else -1.0
    half = (width_mm + gap_mm) / 2.0
    p_off, n_off = (side * half * nx, side * half * ny), (-side * half * nx, -side * half * ny)
    trim = min(length * 0.2, max(half * 2.0, 0.25))
    cs = (ms[0] + ux * trim, ms[1] + uy * trim)
    ce = (me[0] - ux * trim, me[1] - uy * trim)
    trunk_pts_pos = [(round(cs[0] + p_off[0], 4), round(cs[1] + p_off[1], 4)),
                     (round(ce[0] + p_off[0], 4), round(ce[1] + p_off[1], 4))]
    trunk_pts_neg = [(round(cs[0] + n_off[0], 4), round(cs[1] + n_off[1], 4)),
                     (round(ce[0] + n_off[0], 4), round(ce[1] + n_off[1], 4))]
    pos_pts = _octilinear_join(start_pos, trunk_pts_pos[0]) + trunk_pts_pos[1:] + _octilinear_join(trunk_pts_pos[-1], end_pos)[1:]
    neg_pts = _octilinear_join(start_neg, trunk_pts_neg[0]) + trunk_pts_neg[1:] + _octilinear_join(trunk_pts_neg[-1], end_neg)[1:]

    def to_segs(net: str, pts: list[tuple[float, float]]) -> list[dict]:
        return [
            {"net": net, "start": [round(a[0], 4), round(a[1], 4)], "end": [round(b[0], 4), round(b[1], 4)],
             "width": round(width_mm, 4), "layer": layer}
            for a, b in zip(pts, pts[1:]) if math.dist(a, b) > 1e-6
        ]

    segments = to_segs(pos_net, pos_pts) + to_segs(neg_net, neg_pts)
    vias = []
    if via_layers is not None:
        vtype = "micro" if via_layers[0] != via_layers[1] else "through"
        for net, pt in ((pos_net, start_pos), (neg_net, start_neg), (pos_net, end_pos), (neg_net, end_neg)):
            vias.append({"x": round(pt[0], 4), "y": round(pt[1], 4), "size": via_size, "drill": via_drill,
                         "net": net, "type": vtype, "layers": list(via_layers)})
    pos_len = sum(math.dist(a, b) for a, b in zip(pos_pts, pos_pts[1:]))
    neg_len = sum(math.dist(a, b) for a, b in zip(neg_pts, neg_pts[1:]))
    return {"segments": segments, "vias": vias, "pos_length_mm": round(pos_len, 4), "neg_length_mm": round(neg_len, 4)}


def tune_pair_skew(
    segment: dict,
    delta_mm: float,
    min_gap_mm: float | None = None,
    max_amp_mm: float = 1.5,
    side: int = 1,
) -> dict:
    """Replace a straight segment with a trombone meander adding `delta_mm` of routed length."""
    if delta_mm <= 0:
        raise ValueError("delta_mm must be positive")
    (ax, ay), (bx, by) = tuple(segment["start"]), tuple(segment["end"])
    width = float(segment["width"])
    gap = float(min_gap_mm) if min_gap_mm is not None else 3.0 * width
    length = math.hypot(bx - ax, by - ay)
    pitch = width + gap
    ux, uy = (bx - ax) / length, (by - ay) / length
    nx, ny = -uy * (1 if side >= 0 else -1), ux * (1 if side >= 0 else -1)
    # Try N=1,2,... rectangular U-bumps: each bump adds 2*amp while taking `pitch` along the segment.
    margin = max(pitch, 0.2)
    usable = length - 2 * margin
    if usable < pitch:
        raise ValueError(f"segment length {length:.3f} mm too short for trombone pitch {pitch:.3f} mm")
    max_bumps = max(1, int(usable // (2 * pitch)))
    chosen = None
    for bumps in range(1, max_bumps + 1):
        amp = delta_mm / (2.0 * bumps)
        if amp <= max_amp_mm + 1e-9:
            chosen = (bumps, amp)
            break
    if chosen is None:
        raise ValueError(f"delta_mm={delta_mm:.3f} exceeds max_amp_mm={max_amp_mm:.3f} over usable span {usable:.3f} mm")
    bumps, amp = chosen
    pts: list[tuple[float, float]] = [(ax, ay)]
    step = usable / bumps
    s = margin
    for _ in range(bumps):
        w_bump = min(pitch, step * 0.5)
        s0 = s + (step - w_bump) / 2.0
        s1 = s0 + w_bump
        pts.extend([
            (ax + ux * s0, ay + uy * s0),
            (ax + ux * s0 + nx * amp, ay + uy * s0 + ny * amp),
            (ax + ux * s1 + nx * amp, ay + uy * s1 + ny * amp),
            (ax + ux * s1, ay + uy * s1),
        ])
        s += step
    pts.append((bx, by))
    segs = [
        {"net": segment["net"], "start": [round(p[0], 5), round(p[1], 5)], "end": [round(q[0], 5), round(q[1], 5)],
         "width": width, "layer": segment["layer"]}
        for p, q in zip(pts, pts[1:]) if math.dist(p, q) > 1e-6
    ]
    new_len = sum(math.dist(s["start"], s["end"]) for s in segs)
    return {"segments": segs, "added_mm": round(new_len - length, 5), "bumps": bumps, "amplitude_mm": round(amp, 5)}
