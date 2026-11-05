"""What RF layout reviews keep finding, as rules a board can be built to and measured against.

DRC passes all of it. Each rule below moved a real board in review: a supply run through a choke's pad gap, a via's antipad under an RF
line, a limiter whose ground is split by a bias trace. `audit` reads a board and reports. `ground.py` and the router build to the same
numbers. Every threshold can be changed under `rf_layout:` in edaloop.yaml, and a reading is a measurement, not a verdict on the part.
"""
from __future__ import annotations

import math
import re

from shapely.geometry import LineString, MultiPoint

FENCE_PITCH_MM = 1.778      # 70 mil centre to centre, which is under a twentieth of a guided wavelength through C-band
C_M_S = 299_792_458.0

# Ground vias from each maker's suggested layout. `pins` is vias at a pin's outer end, `paddle` a grid in an exposed pad, `mid` one via under
# the body between two pads, and `under` a row under the body, midway between pairs of pads. Only pads that are really on GND get a via.
LAND_PATTERNS = [
    (r"^PSA4-5043\+?", {"pins": {"2": 2, "4": 2}, "mid": [("2", "4")], "drawing": "Mini-Circuits MMM1362"}),
    (r"^PMA2-63LN\+?", {"pins": {"2": 1, "4": 2, "5": 2, "7": 1, "8": 2}, "paddle": {"9": (1, 2)}, "drawing": "Mini-Circuits MC1631-1"}),
    (r"^PMA3-83LN\+?", {"pins": {"1": 1, "3": 1, "4": 1, "5": 1, "6": 1, "7": 1, "9": 1, "10": 1}, "paddle": {"13": (2, 3)}, "drawing": "Mini-Circuits PL-456"}),
    (r"^RLM-(33|63)", {"under": [(("1", "6"), ("3", "4"))], "per_ground_pin": 2, "drawing": "Mini-Circuits PL-258"}),
    (r"^HFCN-3100\+?", {"mid": [("2", "5")], "drawing": "Mini-Circuits PL-158"}),
]
# An RF connector, by its footprint, part number or value. Its ground pads are where the cable's shield meets the
# board, so they get as many vias as fit. General RF practice, not a maker's drawing: no connector here has one
# that counts vias, which is why this isn't a LAND_PATTERNS entry.
RF_CONNECTOR = re.compile(r"Connector_Coaxial|\b[UWH]\.?FL\b|\bIPEX\b|\bMHF|\bSMA\b|\bSMB\b|\bSMP\b|\bMMCX\b|\bMCX\b|\bBNC\b", re.I)
LIMITER = re.compile(r"^(RLM|VLM)-|limiter", re.I)
FILTER = re.compile(r"^FL\d|(high|low|band)pass|^(HFCN|LFCN|BFCN)-", re.I)
SERIES_L_NH = {"0201": 0.25, "0402": 0.35, "0603": 0.45, "0805": 0.6}      # a chip capacitor's own inductance, typical
VIA_L_NH = 0.25             # a bypass's via to the plane

DEFAULTS = {"fence_factor": 1.25, "fence_reach_mm": 1.3, "coverage_warn": 1.0, "coverage_error": 0.9, "parallel_gap_mm": 1.0, "parallel_max_mm": 3.0,
            "top_run_max_mm": 5.0, "tap_max_mm": 3.0, "bypass_max_mm": 3.0, "series_rl_min_db": 20.0, "bypass_max_ohm": 25.0,
            # An RF connector's ground pads: "fill" places as many vias as fit, or give a number. Fewer than the minimum is reported.
            "connector_ground_vias": "fill", "connector_ground_min_vias": 2}


def is_rf_connector(part: dict | None) -> bool:
    """True for a coax connector, read from its footprint, part number or value."""
    part = part or {}
    return any(RF_CONNECTOR.search(str(part.get(k) or "")) for k in ("footprint", "mpn", "value"))


def land_pattern(mpn: str | None) -> dict | None:
    for pattern, rule in LAND_PATTERNS:
        if mpn and re.search(pattern, mpn, re.I):
            return rule
    return None


def fence_pitch(high_ghz: float | None, eps_eff: float = 3.25) -> float:
    """70 mil, or a twentieth of a guided wavelength at the top of the band if that's tighter."""
    if not high_ghz:
        return FENCE_PITCH_MM
    return min(FENCE_PITCH_MM, C_M_S / (high_ghz * 1e9 * math.sqrt(eps_eff)) / 20 * 1000)


def capacitance_f(value: str | None) -> float | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([pnu])\s*F?\b", str(value or ""), re.I) or re.match(r"^\s*(\d+)([pnu])(\d+)\b", str(value or ""), re.I)
    if not m:
        return None
    number = float(f"{m[1]}.{m[3]}") if m.re.groups == 3 else float(m[1])
    return number * {"p": 1e-12, "n": 1e-9, "u": 1e-6}[m[2].lower()]


def series_cap(c_f: float, esl_nh: float, f_hz: float) -> float:
    """A chip capacitor's reactance with its own inductance, in ohm. Below its resonance it's a capacitor, and above it an inductor."""
    w = 2 * math.pi * f_hz
    return w * esl_nh * 1e-9 - 1 / (w * c_f)


def series_rl_db(x_ohm: float, z0: float = 50.0) -> float:
    return -20 * math.log10(max(abs(x_ohm) / math.hypot(2 * z0, x_ohm), 1e-6))


def srf_ghz(c_f: float, esl_nh: float) -> float:
    return 1 / (2 * math.pi * math.sqrt(esl_nh * 1e-9 * c_f)) / 1e9


def _seg_distance(p: tuple, a: tuple, b: tuple) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _crosses(a: tuple, b: tuple, c: tuple, d: tuple) -> bool:
    return LineString([a, b]).intersects(LineString([c, d]))


def _pad_edge_distance(pad: dict, x: float, y: float) -> float:
    t = math.radians(-pad.get("rot", 0.0))
    dx, dy = x - pad["x"], y - pad["y"]
    u, v = dx * math.cos(t) + dy * math.sin(t), -dx * math.sin(t) + dy * math.cos(t)
    return math.hypot(max(abs(u) - pad["w"] / 2, 0.0), max(abs(v) - pad["h"] / 2, 0.0))


def parallel_runs(tracks: list, rf_nets: set, gap_mm: float, angle_deg: float = 10.0) -> dict:
    """How far each other net runs alongside each RF net on one layer, edge to edge within `gap_mm`."""
    total: dict[tuple, float] = {}
    rf = [t for t in tracks if t.net in rf_nets and t.kind != "via"]
    for other in (t for t in tracks if t.kind != "via" and t.net not in rf_nets and t.net != "GND"):
        ox, oy = other.end[0] - other.start[0], other.end[1] - other.start[1]
        length = math.hypot(ox, oy)
        if length < 0.1:
            continue
        for line in (t for t in rf if t.layer == other.layer):
            lx, ly = line.end[0] - line.start[0], line.end[1] - line.start[1]
            run = math.hypot(lx, ly)
            if run < 0.1 or abs(ox * ly - oy * lx) / (length * run) > math.sin(math.radians(angle_deg)):
                continue
            ux, uy = lx / run, ly / run
            s = sorted(((p[0] - line.start[0]) * ux + (p[1] - line.start[1]) * uy) for p in (other.start, other.end))
            overlap = min(s[1], run) - max(s[0], 0.0)
            mid = ((other.start[0] + other.end[0]) / 2, (other.start[1] + other.end[1]) / 2)
            edge = _seg_distance(mid, line.start, line.end) - (other.width + line.width) / 2
            if overlap > 0 and edge <= gap_mm:
                key = (other.net, line.net)
                total[key] = total.get(key, 0.0) + overlap
    return total


def longest_top_run(tracks: list, net: str, layer: str = "F.Cu") -> tuple[float, float]:
    """A net's total length on a layer, and its longest connected stretch."""
    segs = [t for t in tracks if t.net == net and t.kind != "via" and t.layer == layer]
    total = sum(math.dist(t.start, t.end) for t in segs)
    group = list(range(len(segs)))

    def find(i: int) -> int:
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    for i, a in enumerate(segs):
        for j in range(i):
            b = segs[j]
            if min(math.dist(x, y) for x in (a.start, a.end) for y in (b.start, b.end)) < 0.01:
                group[find(i)] = find(j)
    sums: dict[int, float] = {}
    for i, t in enumerate(segs):
        sums[find(i)] = sums.get(find(i), 0.0) + math.dist(t.start, t.end)
    return round(total, 2), round(max(sums.values(), default=0.0), 2)


def fence_stretches(tracks: list, vias: list, nets: set, reach_mm: float, min_len: float = 0.5) -> dict[str, float]:
    """Each RF net's longest run along a track, on either side, between ground vias. A via's reach is measured from the track's centre."""
    out: dict[str, float] = {}
    for t in tracks:
        if t.kind == "via" or t.net not in nets:
            continue
        lx, ly = t.end[0] - t.start[0], t.end[1] - t.start[1]
        run = math.hypot(lx, ly)
        if run < min_len:
            continue
        ux, uy = lx / run, ly / run
        for side in (1, -1):
            stops = [0.0, run]
            for vx, vy in vias:
                along = (vx - t.start[0]) * ux + (vy - t.start[1]) * uy
                across = -(vx - t.start[0]) * uy + (vy - t.start[1]) * ux
                if 0 <= along <= run and 0 < side * across <= reach_mm:
                    stops.append(along)
            stops.sort()
            out[t.net] = max(out.get(t.net, 0.0), max(b - a for a, b in zip(stops, stops[1:])))
    return {k: round(v, 2) for k, v in out.items()}


def audit(tracks: list, facts: dict, chains: list, rails: list, parts: dict, band_ghz: tuple, reference: str, coverage: dict, copper_layers: int, cfg: dict | None = None) -> tuple[dict, list[tuple]]:
    """Measure a board against the rules. Returns metrics, and findings as (id, message, severity)."""
    c = {**DEFAULTS, **(cfg or {})}
    pads = facts["pads"]
    by_name = {f"{p['ref']}.{p['pad']}": p for p in pads}
    by_ref: dict[str, list] = {}
    for p in pads:
        by_ref.setdefault(p["ref"], []).append(p)
    yard = {y["ref"]: y for y in facts.get("courtyards", [])}
    rf_nets = {row[0] for row in chains}
    # A plated ground hole reaches the plane whether the router drew it or the footprint brought it: KiCad's TT1224 PL-258
    # land pattern carries its maker's vias as pads. An unplated hole has no net, so it isn't counted.
    ground_vias = [t.start for t in tracks if t.kind == "via" and t.net == "GND"] + \
        [(p["x"], p["y"]) for p in pads if p["net"] == "GND" and (p.get("drill") or 0) > 0]
    findings: list[tuple] = []
    metrics: dict = {}

    def value_of(ref: str) -> str:
        return str((parts.get(ref) or {}).get("value") or yard.get(ref, {}).get("value") or "")

    def identity(ref: str) -> str:
        return str((parts.get(ref) or {}).get("mpn") or value_of(ref))

    # Via fence
    pitch = float(c.get("fence_pitch_mm") or fence_pitch(band_ghz[1], float(c.get("eps_eff", 3.25))))
    stretches = fence_stretches(tracks, ground_vias, rf_nets, float(c["fence_reach_mm"]))
    long_ones = {k: v for k, v in stretches.items() if v > pitch * float(c["fence_factor"])}
    metrics["fence"] = {"pitch_mm": round(pitch, 3), "longest_mm": max(stretches.values(), default=0.0), "nets_over_pitch": len(long_ones), "nets": len(stretches)}
    for net, length in long_ones.items():
        findings.append((f"rf_layout:fence:{net}", f"{net}: {length} mm of line without a ground via on one side, against a {pitch:.2f} mm fence. The stretch can be at a part's own pads, where a via can't go", "warning"))

    # The plane under the line
    if coverage:
        worst = min(v["coverage"] for v in coverage.values())
        metrics["plane_coverage_min"] = round(worst, 4)
        for net, row in coverage.items():
            if row["coverage"] < float(c["coverage_warn"]):
                severity = "error" if row["coverage"] < float(c["coverage_error"]) else "warning"
                findings.append((f"rf_layout:plane:{net}", f"{net}: {reference} covers {row['coverage'] * 100:.1f}% of the trace's width near ({row['at'][0]}, {row['at'][1]}). Look for a via's antipad or a split there", severity))

    # Other nets through an RF part's pad gap
    through = []
    tapped = sorted({p["ref"] for p in pads if p["net"] in rf_nets and len(by_ref[p["ref"]]) == 2})
    for ref in tapped:
        a, b = by_ref[ref]
        if not (a["front"] and b["front"]):
            continue
        own = {a["net"], b["net"], "GND"}
        for t in tracks:
            if t.kind != "via" and t.layer == "F.Cu" and t.net not in own and _crosses(t.start, t.end, (a["x"], a["y"]), (b["x"], b["y"])):
                through.append(ref)
                findings.append((f"rf_layout:pad_gap:{ref}", f"{t.net} runs between the pads of {ref} on F.Cu. Keep other nets out of an RF part's pad gap: move it to an inner layer", "warning"))
                break
    metrics["pad_gap_runs"] = len(through)

    # A limiter's ground stays whole, on every layer
    split = []
    for ref, own_pads in by_ref.items():
        if not LIMITER.search(identity(ref)) or len(own_pads) < 3:
            continue
        field = MultiPoint([(p["x"], p["y"]) for p in own_pads]).convex_hull.buffer(0.2)
        own = {p["net"] for p in own_pads} | {"GND"}
        for t in tracks:
            if t.kind != "via" and t.net not in own and field.intersects(LineString([t.start, t.end])):
                split.append(ref)
                findings.append((f"rf_layout:limiter:{ref}", f"{t.net} on {t.layer} runs under the pad field of limiter {ref}, which splits the ground it's referenced to", "warning"))
                break
    metrics["limiters_split"] = len(split)

    # Coupling alongside the line
    runs = parallel_runs(tracks, rf_nets, float(c["parallel_gap_mm"]))
    metrics["parallel_longest_mm"] = round(max(runs.values(), default=0.0), 2)
    for (other, line), length in runs.items():
        if length > float(c["parallel_max_mm"]):
            findings.append((f"rf_layout:parallel:{other}:{line}", f"{other} runs {length:.1f} mm beside {line} within {c['parallel_gap_mm']} mm, on one layer. Angle it away or move it to an inner layer", "warning"))

    # Supply on an inner layer
    top = {}
    if copper_layers > 2:
        for rail in rails:
            net = rail["net"]
            if net in rf_nets:
                continue
            total, longest = longest_top_run(tracks, net)
            top[net] = {"total_mm": total, "longest_mm": longest}
            if longest > float(c["top_run_max_mm"]):
                findings.append((f"rf_layout:top_supply:{net}", f"{net} has a {longest} mm run on F.Cu. Supply belongs on an inner layer under whole ground, with a via beside each pad", "warning"))
    metrics["supply_on_top"] = top

    # Taps and bias
    taps, bypasses = {}, {}
    for row in chains:
        net, entry, leave = row[0], str(row[1]), str(row[2])
        if entry not in by_name or leave not in by_name:
            continue
        a, b = by_name[entry], by_name[leave]
        for p in pads:
            name = f"{p['ref']}.{p['pad']}"
            if p["net"] != net or name in (entry, leave) or p["net"] == "GND":
                continue
            stub = _seg_distance((p["x"], p["y"]), (a["x"], a["y"]), (b["x"], b["y"]))
            taps[name] = round(stub, 2)
            if stub > float(c["tap_max_mm"]):
                findings.append((f"rf_layout:tap:{name}", f"{name} sits {stub:.1f} mm off {net}, a stub that loads the line. Put the part at the junction, with its pad in the trace if it fits", "warning"))
            if p["ref"].startswith("L") and len(by_ref[p["ref"]]) == 2:
                cold = next(q for q in by_ref[p["ref"]] if q is not p)
                caps = []
                if cold["net"] == "GND":        # a shunt inductor to ground, part of a match, has no cold end to bypass
                    continue
                for q in pads:
                    if q["net"] == cold["net"] and q["ref"].startswith("C") and len(by_ref[q["ref"]]) == 2 and any(r["net"] == "GND" for r in by_ref[q["ref"]]):
                        farads = capacitance_f(value_of(q["ref"]))
                        if farads:
                            caps.append((farads, math.dist((q["x"], q["y"]), (cold["x"], cold["y"])), q["ref"]))
                if caps:
                    caps.sort()
                    near = min(caps, key=lambda x: x[1])
                    smallest = caps[0]
                    esl = _esl(parts, smallest[2])
                    worst = max(abs(series_cap(smallest[0], esl + VIA_L_NH, f * 1e9)) for f in band_ghz)
                    bypasses[p["ref"]] = {"smallest": smallest[2], "distance_mm": round(smallest[1], 2), "nearest": near[2], "worst_ohm": round(worst, 1)}
                    if smallest[2] != near[2]:
                        findings.append((f"rf_layout:bypass_order:{p['ref']}", f"{p['ref']}'s nearest bypass is {near[2]}, but its smallest is {smallest[2]}. Smallest goes nearest the cold end, then larger", "warning"))
                    if smallest[1] > float(c["bypass_max_mm"]):
                        findings.append((f"rf_layout:bypass_far:{p['ref']}", f"{smallest[2]} is {smallest[1]:.1f} mm from {p['ref']}'s cold pad", "warning"))
                    if worst > float(c["bypass_max_ohm"]):
                        findings.append((f"rf_layout:bypass_z:{p['ref']}", f"{smallest[2]} ({value_of(smallest[2])}) estimates {worst:.0f} ohm to ground at a band edge with its via. Check its measured S-parameters", "warning"))
    metrics["taps_mm"], metrics["bypass"] = taps, bypasses

    # Series capacitors, from a chip's typical inductance. Its measured S-parameters decide.
    blocks = {}
    for ref, own_pads in by_ref.items():
        if not ref.startswith("C") or len(own_pads) != 2 or not all(p["net"] in rf_nets for p in own_pads):
            continue
        farads = capacitance_f(value_of(ref))
        if not farads:
            continue
        esl = _esl(parts, ref)
        rl = min(series_rl_db(series_cap(farads, esl, f * 1e9)) for f in band_ghz)
        blocks[ref] = {"value": value_of(ref), "srf_ghz": round(srf_ghz(farads, esl), 2), "worst_rl_db": round(rl, 1)}
        if rl < float(c["series_rl_min_db"]):
            findings.append((f"rf_layout:series_cap:{ref}", f"{ref} ({value_of(ref)}) estimates {rl:.1f} dB return loss at a band edge, resonating at {srf_ghz(farads, esl):.2f} GHz. Check its measured S-parameters", "warning"))
        elif srf_ghz(farads, esl) < band_ghz[0] * 0.7:
            findings.append((f"rf_layout:series_cap:{ref}", f"{ref} ({value_of(ref)}) resonates at {srf_ghz(farads, esl):.2f} GHz, so it's an inductor across the band, and some parts have a parasitic resonance there. Check its measured S-parameters", "warning"))
    metrics["series_caps"] = blocks

    # Filters tied at a node
    for net in rf_nets:
        filters = {p["ref"] for p in pads if p["net"] == net and FILTER.search(p["ref"]) or (p["net"] == net and FILTER.search(identity(p["ref"])))}
        if len(filters) >= 3:
            findings.append((f"rf_layout:tee:{net}", f"{net} ties {len(filters)} filters together. A plain node reflects what each one rejects, so loss swings with phase. Use a multiplexer", "warning"))

    # Maker's ground patterns
    short = []
    radius_reach = 0.9
    for ref, own_pads in by_ref.items():
        rule = land_pattern(identity(ref))
        if not rule:
            continue
        named = {p["pad"]: p for p in own_pads}
        grounds = [p for p in own_pads if p["net"] == "GND"]
        for p in grounds:
            if p["pad"] in rule.get("paddle", {}):
                nx, ny = rule["paddle"][p["pad"]]
                got = sum(1 for v in ground_vias if _pad_edge_distance(p, *v) == 0)
                if got < nx * ny:
                    short.append(f"{ref}.{p['pad']}: {got} of {nx * ny} paddle vias")
                continue
            want = rule.get("pins", {}).get(p["pad"], rule.get("per_ground_pin", 0))
            got = sum(1 for v in ground_vias if _pad_edge_distance(p, *v) <= radius_reach)
            if got < want:
                short.append(f"{ref}.{p['pad']}: {got} of {want} vias")
        for x, y in rule.get("mid", []):
            if x in named and y in named and named[x]["net"] == "GND" == named[y]["net"]:
                at = ((named[x]["x"] + named[y]["x"]) / 2, (named[x]["y"] + named[y]["y"]) / 2)
                if not any(math.dist(at, v) <= 0.6 for v in ground_vias):
                    short.append(f"{ref}: no via under the body between pads {x} and {y}")
        for pairs in rule.get("under", []):
            for x, y in pairs:
                if x in named and y in named:
                    at = ((named[x]["x"] + named[y]["x"]) / 2, (named[x]["y"] + named[y]["y"]) / 2)
                    if not any(math.dist(at, v) <= 0.6 for v in ground_vias):
                        short.append(f"{ref}: no via in the row under the body, between pads {x} and {y}")
    metrics["ground_pattern_short"] = short

    # RF connectors: the shield's return reaches the plane through every via on the ground pads.
    floor = int(c["connector_ground_min_vias"])
    thin = []
    for ref, own_pads in by_ref.items():
        if not is_rf_connector({**(parts.get(ref) or {}), "value": value_of(ref)}):
            continue
        for p in own_pads:
            if p["net"] == "GND":
                got = sum(1 for v in ground_vias if _pad_edge_distance(p, *v) <= radius_reach)
                if got < floor:
                    thin.append(f"{ref}.{p['pad']}: {got}")
    metrics["connector_ground_thin"] = thin
    if thin:
        findings.append(("rf_layout:connector_ground", f"RF connector ground pads with fewer than {floor} vias: " + "; ".join(thin[:8])
                         + (f"; and {len(thin) - 8} more" if len(thin) > 8 else "") + ". The cable's return current reaches the plane through them", "warning"))
    if short:
        findings.append(("rf_layout:ground_pattern", "Short of the maker's ground-via pattern: " + "; ".join(short[:8]) + (f"; and {len(short) - 8} more" if len(short) > 8 else ""), "warning"))
    return metrics, findings


def _esl(parts: dict, ref: str) -> float:
    footprint = str((parts.get(ref) or {}).get("footprint") or "")
    for size, nh in SERIES_L_NH.items():
        if size in footprint:
            return nh
    return SERIES_L_NH["0402"]
