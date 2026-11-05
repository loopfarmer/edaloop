"""Ground vias for a generated board: welded to ground pads before routing, fence and stitching after.

Positions come from true pad shapes and angles, read through pcbnew. KiCad's DRC has the last word: a via it
objects to is removed and the objection doesn't reach the board. A pad left without its via is reported.
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import yaml

from . import rf_rules, route_classes, toolchain
from .pcbfile import Board

OPS = Path(__file__).with_name("kicad_ops.py")
VIA, DRILL = 0.61, 0.254     # the fabricator's preferred drill, with its 0.178 mm annular ring
RADIUS = VIA / 2
CLEAR = 0.2                 # copper to copper, the net class
STUB = 0.2                  # a ground stub off a fine-pitch pin
WELD = RADIUS - DRILL / 2 - 0.02   # how far a via's ring overlaps its pad's edge. The hole stays just outside, so the pad's solder can't wick into it
HOLE_PITCH = 0.6            # via to via, centres
PLACEMENT_FAULTS = {"clearance", "hole_clearance", "hole_to_hole", "shorting_items", "items_not_allowed",
                    "copper_edge_clearance", "tracks_crossing", "track_dangling"}


def _op(*args) -> dict:
    r = toolchain.run([toolchain.paths()["kicad_python"], OPS, *args], timeout=600)
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode != 0 or not lines:
        raise RuntimeError(f"{args[0]} failed: {(r.stdout + r.stderr).strip()[-300:]}")
    return json.loads(lines[-1])


def _facts(board: Path, work: Path) -> dict:
    out = work / "board_facts.json"
    _op("board_facts", board, out)
    return json.loads(out.read_text())


def _to_pad(pad: dict, x: float, y: float) -> tuple[float, float]:
    """A board point in the pad's own axes. KiCad's angles run clockwise on screen, with y down."""
    t = math.radians(pad["rot"])
    dx, dy = x - pad["x"], y - pad["y"]
    return dx * math.cos(t) - dy * math.sin(t), dx * math.sin(t) + dy * math.cos(t)


def _from_pad(pad: dict, u: float, v: float) -> tuple[float, float]:
    t = math.radians(pad["rot"])
    return pad["x"] + u * math.cos(t) + v * math.sin(t), pad["y"] - u * math.sin(t) + v * math.cos(t)


def pad_gap(pad: dict, x: float, y: float) -> float:
    u, v = _to_pad(pad, x, y)
    du, dv = max(abs(u) - pad["w"] / 2, 0.0), max(abs(v) - pad["h"] / 2, 0.0)
    return math.hypot(du, dv)


def _seg_gap(p: tuple, a: tuple, b: tuple) -> float:
    ax, ay, bx, by = *a, *b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / max(dx * dx + dy * dy, 1e-12)))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


class Field:
    """What a new ground via must stay clear of."""

    def __init__(self, facts: dict, tracks: list, vias: list[tuple], lines: list[tuple]):
        self.foreign = [p for p in facts["pads"] if p["net"] != "GND"]
        self.tracks = tracks        # (a, b, half width)
        self.vias = list(vias)
        self.lines = lines          # planned copper that isn't laid yet: (a, b, half width)
        self.x1, self.y1, self.x2, self.y2 = facts["outline"]
        self.keepouts = [k["pts"] for k in facts.get("keepouts") or [] if len(k["pts"]) >= 3]
        # Every drilled pad is a hole, whatever its net. A footprint's own ground holes (a land pattern's vias, a filter's
        # plated ground holes) aren't foreign copper, but a new via's drill must still keep its distance from them.
        self.holes = [(p["x"], p["y"], p["drill"]) for p in facts["pads"] if (p.get("drill") or 0) > 0]

    def clear(self, x: float, y: float) -> bool:
        reach = RADIUS + CLEAR
        margin = RADIUS + 0.55
        if not (self.x1 + margin <= x <= self.x2 - margin and self.y1 + margin <= y <= self.y2 - margin):
            return False
        # A pad's own clearance, where its land pattern sets one, replaces the net class's.
        if any(pad_gap(p, x, y) < RADIUS + max(CLEAR, p.get("clearance") or 0.0) for p in self.foreign):
            return False
        # KiCad flags a via that so much as overlaps a rule area forbidding vias, so the whole ring must stay out.
        if any(_in_polygon(x, y, pts) or _polygon_gap(x, y, pts) < RADIUS for pts in self.keepouts):
            return False
        if any(_seg_gap((x, y), a, b) < reach + hw for a, b, hw in self.tracks + self.lines):
            return False
        # The same edge-to-edge gap as two of these vias at their pitch.
        if any(math.hypot(x - hx, y - hy) < (DRILL + d) / 2 + HOLE_PITCH - DRILL for hx, hy, d in self.holes):
            return False
        return all(math.hypot(x - vx, y - vy) >= HOLE_PITCH for vx, vy in self.vias)


def _in_polygon(x: float, y: float, pts: list) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _polygon_gap(x: float, y: float, pts: list) -> float:
    return min(_seg_gap((x, y), a, b) for a, b in zip(pts, pts[1:] + pts[:1]))


def _stub_clear(field: Field, a: tuple, b: tuple) -> bool:
    """A stub's sampled points clear foreign pads and the planned runs."""
    reach = STUB / 2 + CLEAR
    for k in range(9):
        q = (a[0] + (b[0] - a[0]) * k / 8, a[1] + (b[1] - a[1]) * k / 8)
        if any(pad_gap(x, *q) < STUB / 2 + max(CLEAR, x.get("clearance") or 0.0) for x in field.foreign):
            return False
        if any(_seg_gap(q, s, e) < reach + hw for s, e, hw in field.lines):
            return False
    return True


def _planned_lines(bench: dict, facts: dict) -> list[tuple]:
    """The straight RF and bias runs the router will lay, and each tap's stub onto them."""
    at = {f"{p['ref']}.{p['pad']}": (p["x"], p["y"]) for p in facts["pads"]}
    hw = bench["rf_line_width_mm"] / 2
    lines = []
    for net, entry, leave in [*bench["chain"], *(bench.get("extra_chains") or [])]:
        a, b = at[entry], at[leave]
        lines.append((a, b, hw))
        for pin in bench["nets"][net]:
            if pin not in (entry, leave):
                lines.append((at[pin], a, hw) if math.dist(at[pin], a) < math.dist(at[pin], b) else (at[pin], b, hw))
    return lines


def _candidates(pad: dict, bias_from: tuple) -> list[tuple]:
    """Points on each edge of a pad, welded to it, the side facing away from the RF line first."""
    out = []
    half_w, half_h = pad["w"] / 2, pad["h"] / 2
    off = RADIUS - WELD
    along_v = [0.0] + [s * d for d in (0.2, 0.4, 0.6, 0.8) if d < half_h for s in (1, -1)]
    along_u = [0.0] + [s * d for d in (0.2, 0.4, 0.6, 0.8) if d < half_w for s in (1, -1)]
    for v in along_v:
        out += [_from_pad(pad, half_w + off, v), _from_pad(pad, -half_w - off, v)]
    for u in along_u:
        out += [_from_pad(pad, u, half_h + off), _from_pad(pad, u, -half_h - off)]
    return sorted(out, key=lambda p: -min(_seg_gap(p, a, b) for a, b, _ in bias_from) if bias_from else 0)


def _load(p) -> tuple[dict, dict]:
    bench = yaml.safe_load((p.root / "board_description.yaml").read_text())
    return bench, bench["layout"].get("ground") or {}


def _write_vias(board: Path, points: list[tuple], locked: bool, work: Path) -> None:
    spec = work / "new_vias.json"
    spec.write_text(json.dumps([{"x": round(x, 4), "y": round(y, 4), "size": VIA, "drill": DRILL, "net": "GND", "locked": locked}
                                for x, y in points]))
    _op("add_vias", board, spec, board)


def maker_patterns(bench: dict, facts: dict, cfg: dict, rf_layout: dict | None = None) -> dict:
    """The ground vias each part's maker draws, from `rf_rules.LAND_PATTERNS`, and an RF connector's, which fill its
    ground pads. What the description says about a part wins."""
    parts = bench.get("parts") or {}
    footprints = (bench.get("layout") or {}).get("footprints") or {}
    per_pad = cfg.get("per_pad") or {}
    connector = {**rf_rules.DEFAULTS, **(rf_layout or {})}["connector_ground_vias"]
    by_ref: dict[str, dict] = {}
    for pad in facts["pads"]:
        by_ref.setdefault(pad["ref"], {})[pad["pad"]] = pad
    paddle, counts, under = dict(cfg.get("paddle") or {}), {}, []
    for ref, pads in by_ref.items():
        if ref not in per_pad and rf_rules.is_rf_connector({**(parts.get(ref) or {}), "footprint": footprints.get(ref, "")}):
            for number, pad in pads.items():
                if pad["net"] == "GND" and f"{ref}.{number}" not in per_pad:
                    counts[f"{ref}.{number}"] = connector
        rule = rf_rules.land_pattern((parts.get(ref) or {}).get("mpn"))
        if not rule:
            continue
        for number, grid in rule.get("paddle", {}).items():
            if number in pads and pads[number]["net"] == "GND":
                paddle.setdefault(f"{ref}.{number}", list(grid))
        for number, pad in pads.items():
            want = rule.get("pins", {}).get(number, rule.get("per_ground_pin"))
            if want and pad["net"] == "GND":
                counts[f"{ref}.{number}"] = want
        # A row under a limiter's body sits between an RF pad and a ground pad, so only `mid` needs both pads to be ground.
        pairs = [*[pair for pair in rule.get("mid", []) if pair[0] in pads and pair[1] in pads and pads[pair[0]]["net"] == "GND" == pads[pair[1]]["net"]],
                 *[pair for row in rule.get("under", []) for pair in row]]
        for a, b in pairs:
            if a in pads and b in pads:
                under.append((f"{ref} under the body", ((pads[a]["x"] + pads[b]["x"]) / 2, (pads[a]["y"] + pads[b]["y"]) / 2)))
    return {"paddle": paddle, "counts": {**counts, **per_pad}, "under": under}


def pad_vias(p) -> dict:
    """Vias at each ground pad, before routing, placed so that the RF and bias runs stay free."""
    bench, cfg = _load(p)
    work = p.root / ".edaloop"
    work.mkdir(exist_ok=True)
    facts = _facts(p.board, work)
    lines = _planned_lines(bench, facts)
    field = Field(facts, [], [], lines)
    placed, bare, stubs = [], [], []
    rf_layout = {**rf_rules.DEFAULTS, **p.section("rf_layout")}
    patterned = maker_patterns(bench, facts, cfg, rf_layout)
    # Datasheet patterns first: vias inside a part's paddle, on a grid.
    for name, (cols, rows) in patterned["paddle"].items():
        pad = next(x for x in facts["pads"] if f"{x['ref']}.{x['pad']}" == name)
        for i in range(cols):
            for j in range(rows):
                u = (i - (cols - 1) / 2) * max((pad["w"] - VIA) / max(cols - 1, 1), HOLE_PITCH if cols > 1 else 0)
                v = (j - (rows - 1) / 2) * max((pad["h"] - VIA) / max(rows - 1, 1), HOLE_PITCH if rows > 1 else 0)
                x, y = _from_pad(pad, u, v)
                placed.append((x, y))
                field.vias.append((x, y))
    # Vias the maker draws under the body, tented. A site that's taken slides a little before it's given up.
    for name, (x0, y0) in patterned["under"]:
        for dx, dy in ((0, 0), (0.15, 0), (-0.15, 0), (0, 0.15), (0, -0.15), (0.3, 0), (-0.3, 0), (0, 0.3), (0, -0.3)):
            if field.clear(x0 + dx, y0 + dy):
                placed.append((x0 + dx, y0 + dy))
                field.vias.append((x0 + dx, y0 + dy))
                break
        else:
            bare.append({"pad": name, "wanted": 1, "placed": 0})
    paddles = set(patterned["paddle"])
    counts = patterned["counts"]
    for pad in facts["pads"]:
        name = f"{pad['ref']}.{pad['pad']}"
        if pad["net"] != "GND" or name in paddles or not pad["front"]:
            continue
        asked = counts.get(name, counts.get(pad["ref"], 1))
        # "fill" takes every welded site that clears. Short of the floor, it's reported like any other count.
        fill = str(asked).lower() == "fill"
        want = math.inf if fill else int(asked)
        need = int(rf_layout["connector_ground_min_vias"]) if fill else want
        got = 0
        for x, y in _candidates(pad, [(a, b, h) for a, b, h in lines]):
            if got >= want:
                break
            if field.clear(x, y):
                placed.append((x, y))
                field.vias.append((x, y))
                got += 1
        if got < need:
            # A fine-pitch pin has no room for a welded via. Take a short stub off its end to the nearest via that fits.
            long_u = pad["w"] >= pad["h"]
            half = (pad["w"] if long_u else pad["h"]) / 2
            for reach, sideways in sorted(((r, s) for r in (0.35, 0.45, 0.55, 0.7, 0.85) for s in (0.0, 0.1, -0.1, 0.2, -0.2, 0.3, -0.3, 0.4, -0.4)),
                                          key=lambda x: math.hypot(*x)):
                for sign in (1, -1):
                    u, v = (sign * (half + reach), sideways) if long_u else (sideways, sign * (half + reach))
                    a = _from_pad(pad, sign * half if long_u else 0.0, 0.0 if long_u else sign * half)
                    x, y = _from_pad(pad, u, v)
                    if got < need and field.clear(x, y) and _stub_clear(field, a, (x, y)):
                        placed.append((x, y))
                        field.vias.append((x, y))
                        stubs.append({"net": "GND", "start": [round(a[0], 4), round(a[1], 4)],
                                      "end": [round(x, 4), round(y, 4)], "width": STUB, "layer": "F.Cu"})
                        got += 1
        if got < need:
            bare.append({"pad": name, "wanted": "fill" if fill else want, "placed": got})
    _write_vias(p.board, placed, True, work)
    if stubs:
        spec = work / "ground_stubs.json"
        spec.write_text(json.dumps(stubs))
        _op("add_locked", p.board, spec, p.board)
    return {"vias": len(placed), "stubs": len(stubs), "short_of_target": bare}


def _prune(board: Path, work: Path) -> dict:
    """KiCad's DRC decides. Any GND via it objects to goes, and the check repeats."""
    removed = 0
    for _ in range(6):
        summary = route_classes.drc_summary(board)
        data = json.loads(board.with_suffix(".drc.json").read_text())
        bad = []
        for v in data["violations"]:
            if v["severity"] != "error":
                continue
            # Objections to where a via sits. A size or rule objection means the project is wrong, and
            # removing vias would pass DRC by deleting the ground. That has to stay visible as an error.
            if v["type"] not in PLACEMENT_FAULTS:
                continue
            for item in v.get("items", []):
                if item["description"].startswith("Via") and "[GND]" in item["description"] and "pos" in item:
                    bad.append((item["pos"]["x"], item["pos"]["y"]))
                    break
        if not bad:
            return {"removed": removed, **summary}
        spec = work / "bad_vias.json"
        spec.write_text(json.dumps(bad))
        removed += _op("remove_vias", board, spec, board)["removed"]
    return {"removed": removed, **route_classes.drc_summary(board)}


def tap_voids(pads: list[dict], chain: list, grow_mm: float, line_width_mm: float) -> list[dict]:
    """Where to clear the plane under the pads of parts hanging off the RF line.

    A choke's pad is wider than the line, so over a near plane it's a shunt capacitor. Clearing the plane beneath it trades that for the
    plane below, which is much further away. A part's own lands on the path are left, since its datasheet was measured on them.
    """
    out = []
    for row in chain:
        net, entry, leave = row[0], str(row[1]), str(row[2])
        for pad in pads:
            name = f"{pad['ref']}.{pad['pad']}"
            if pad["net"] != net or name in (entry, leave) or min(pad["w"], pad["h"]) <= line_width_mm * 1.3:
                continue
            w, h = pad["w"] / 2 + grow_mm, pad["h"] / 2 + grow_mm
            out.append({"pad": name, "pts": [list(_from_pad(pad, u, v)) for u, v in ((w, h), (-w, h), (-w, -h), (w, -h))]})
    return out


def finish(p) -> dict:
    """Bring the by-class route back, then add the RF fence and stitching around the real copper."""
    bench, cfg = _load(p)
    work = p.root / ".edaloop"
    routed = work / "route_classes" / "3_signal.kicad_pcb"
    if not routed.exists():
        raise RuntimeError("no by-class route to finish. Run `edaloop route --by-class` first")
    shutil.copy(routed, p.board)
    board = Board(p.board)
    facts = _facts(p.board, work)
    tracks = [((t.start[0], t.start[1]), (t.end[0], t.end[1]), t.width / 2) for t in board.tracks if t.kind != "via" and t.net != "GND"]
    foreign_vias = [t for t in board.tracks if t.kind == "via" and t.net != "GND"]
    tracks += [((t.start[0], t.start[1]), (t.start[0], t.start[1]), t.width / 2) for t in foreign_vias]
    ground = [(t.start[0], t.start[1]) for t in board.tracks if t.kind == "via" and t.net == "GND"]
    field = Field(facts, tracks, ground, [])
    yards = facts["courtyards"]

    def under_part(x: float, y: float) -> bool:
        return any(c["x1"] - RADIUS < x < c["x2"] + RADIUS and c["y1"] - RADIUS < y < c["y2"] + RADIUS for c in yards)

    placed = []
    high_ghz = bench["band_mhz"][1] / 1000 if bench.get("band_mhz") else None
    pitch = float(cfg.get("fence_pitch_mm") or rf_rules.fence_pitch(high_ghz, float((bench.get("project") or {}).get("eps_eff", 3.25))))
    gap = float(bench["layout"].get("pour_clearance_mm", 0.3))
    rf = {c[0] for c in bench["chain"]}
    for t in board.tracks:
        if t.kind == "via" or t.net not in rf:
            continue
        (ax, ay), (bx, by) = t.start, t.end
        length = math.hypot(bx - ax, by - ay)
        if length < 0.5:
            continue
        ux, uy = (bx - ax) / length, (by - ay) / length
        offset = t.width / 2 + gap + RADIUS + 0.05
        count = max(1, math.ceil(length / pitch))
        step = length / count
        for k in range(count):
            for side in (1, -1):
                # A site that's taken slides along the line, or out a little from the pour's edge, before it's given up.
                for slide, out in ((0, 0), (0.25, 0), (-0.25, 0), (0, 0.15), (0.25, 0.15), (-0.25, 0.15), (0.4, 0), (-0.4, 0), (0, 0.3)):
                    s = (k + 0.5 + slide) * step
                    x, y = ax + ux * s - side * uy * (offset + out), ay + uy * s + side * ux * (offset + out)
                    # A tented via beside the line can sit under a part's body, as long as it clears the pads.
                    if 0 <= s <= length and field.clear(x, y):
                        placed.append((x, y))
                        field.vias.append((x, y))
                        break
    fence = len(placed)
    grid = float(cfg.get("stitch_pitch_mm", 4.0))
    x1, y1, x2, y2 = facts["outline"]
    y = y1 + 1.2
    while y < y2 - 1.0:
        x = x1 + 1.2
        while x < x2 - 1.0:
            if not under_part(x, y) and field.clear(x, y):
                placed.append((x, y))
                field.vias.append((x, y))
            x += grid
        y += grid
    _write_vias(p.board, placed, False, work)
    pruned = _prune(p.board, work)
    voids = []
    if bench["layout"].get("tap_plane_void_mm") is not None:
        voids = tap_voids(facts["pads"], bench["chain"], float(bench["layout"]["tap_plane_void_mm"]), float(bench["rf_line_width_mm"]))
        spec = work / "plane_voids.json"
        spec.write_text(json.dumps({"layer": "In1.Cu", "voids": voids}))
        _op("add_voids", p.board, spec, p.board)
    stripped = _op("strip_inner_pads", p.board, ",".join(sorted(rf)), 1.0, p.board)["changed"]
    silk = _op("place_silk", p.board, p.board)
    filled = work / "filled.kicad_pcb"
    _op("fill_zones", p.board, filled)
    shutil.copy(filled, p.board)
    return {"fence_vias": fence, "stitch_vias": len(placed) - fence, "ground_vias_now": sum(
        1 for t in Board(p.board).tracks if t.kind == "via" and t.net == "GND"), "vias_with_inner_pads_stripped": stripped, "silk_hidden": silk["hidden"], "plane_voids": [v["pad"] for v in voids], **pruned}
