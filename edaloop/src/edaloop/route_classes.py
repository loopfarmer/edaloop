"""Route by class, in order, with a sensor after each class.

Nets differ in what makes them good, so one cost function can't serve them all:

    RF      length and straightness, at a width from the impedance target. Placed first and locked.
    power   width from the current, so it can't heat. Routed second, while there's still room.
    signal  whatever connects. Routed last, around everything else.

DRC runs after each class. An RF error first retries with a narrower escape, then stops the run, since
later classes can only make it worse. The log always says how far the run got and what the last DRC found.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import time
from pathlib import Path

from . import power, toolchain
from .pcbfile import Board

OPS = Path(__file__).with_name("kicad_ops.py")
KRT_PYTHON = Path.home() / ".cache/edaloop/krt-venv/bin/python"
MIN_POWER_WIDTH_MM = 0.2
SIGNAL_ATTEMPTS = 4
RETRY_WITHOUT_GAIN_S = 1200.0


def _spine(a: tuple, b: tuple) -> list[tuple]:
    """Points from a to b using straight runs and 45-degree bends only."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) < 1e-3 or abs(dy) < 1e-3 or abs(abs(dx) - abs(dy)) < 1e-3:
        return [a, b]
    diagonal = min(abs(dx), abs(dy))
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    if abs(dx) > abs(dy):
        run = (abs(dx) - diagonal) / 2
        return [a, (a[0] + sx * run, a[1]), (a[0] + sx * (run + diagonal), a[1] + sy * diagonal), b]
    run = (abs(dy) - diagonal) / 2
    return [a, (a[0], a[1] + sy * run), (a[0] + sx * diagonal, a[1] + sy * (run + diagonal)), b]


def _nearest_on(points: list[tuple], p: tuple) -> tuple:
    """The closest point on a polyline, so that a tap lands on the spine as a short stub."""
    best, best_d = points[0], float("inf")
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy or 1e-12
        u = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
        q = (a[0] + u * dx, a[1] + u * dy)
        d = math.dist(p, q)
        if d < best_d:
            best, best_d = q, d
    return best


def _point_rect(px: float, py: float, r: dict) -> float:
    dx = max(abs(px - r["x"]) - r["hw"], 0.0)
    dy = max(abs(py - r["y"]) - r["hh"], 0.0)
    return math.hypot(dx, dy)


def _point_seg(px: float, py: float, a: tuple, b: tuple) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    u = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / ((dx * dx + dy * dy) or 1e-12)))
    return math.hypot(a[0] + u * dx - px, a[1] + u * dy - py)


def _seg_rect(a: tuple, b: tuple, r: dict) -> float:
    """Distance between a segment's centreline and a pad's box. Zero if they cross."""
    x0, x1, y0, y1 = r["x"] - r["hw"], r["x"] + r["hw"], r["y"] - r["hh"], r["y"] + r["hh"]
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    crosses = True
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                crosses = False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    if crosses and t0 <= t1:
        return 0.0
    ends = min(_point_rect(*a, r), _point_rect(*b, r))
    corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    return min(ends, *(_point_seg(c[0], c[1], a, b) for c in corners))


def _seg_seg(a: tuple, b: tuple, c: tuple, d: tuple) -> float:
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    if cross(a, b, c) * cross(a, b, d) < 0 and cross(c, d, a) * cross(c, d, b) < 0:
        return 0.0
    return min(_point_seg(*a, c, d), _point_seg(*b, c, d), _point_seg(*c, a, b), _point_seg(*d, a, b))


class Obstacles:
    """Copper a new RF track must clear on one layer: other nets' pads, tracks and vias.

    An IC's pins also get a corridor beyond their ends. Nothing is drawn there yet, but the pin still has
    to escape later, and a stub across the front of a pin row leaves those pins with no room for a via.
    """

    def __init__(self, board: Board, layer: str, clearance: float, corridor_mm: float = 1.2, ic_pins: int = 8):
        self.clearance = clearance + 0.005      # the router's rounding shouldn't land exactly on the rule
        self.pads = [p for p in board.all_pads() if layer in p["layers"] or "*.Cu" in p["layers"]]
        self.tracks = [t for t in board.tracks if t.kind == "via" or t.layer == layer]
        self.lines: list[dict] = []
        per_part: dict[tuple, int] = {}
        for p in self.pads:
            key = (p["fx"], p["fy"])
            per_part[key] = per_part.get(key, 0) + 1
        self.corridors = []
        for p in self.pads:
            is_centre = abs(p["x"] - p["fx"]) < 1e-6 and abs(p["y"] - p["fy"]) < 1e-6
            if per_part[(p["fx"], p["fy"])] >= ic_pins and not is_centre:
                d = _outward(p)
                self.corridors.append({"net": p["net"], "x": p["x"] + d[0] * (p["hw"] + corridor_mm / 2),
                                       "y": p["y"] + d[1] * (p["hh"] + corridor_mm / 2),
                                       "hw": corridor_mm / 2 if d[0] else p["hw"], "hh": corridor_mm / 2 if d[1] else p["hh"]})

    def add(self, seg: dict) -> None:
        self.lines.append(seg)

    def clear(self, net: str, a: tuple, b: tuple, width: float, corridors: bool = False) -> bool:
        margin = self.clearance + width / 2
        for p in self.pads:
            if p["net"] != net and _seg_rect(a, b, p) < margin:
                return False
        if corridors:
            for p in self.corridors:
                if p["net"] != net and _seg_rect(a, b, p) < margin:
                    return False
        for t in self.tracks:
            if t.net != net and _seg_seg(a, b, t.start, t.end) < margin + t.width / 2:
                return False
        for line in self.lines:
            if line["net"] != net and _seg_seg(a, b, line["start"], line["end"]) < margin + line["width"] / 2:
                return False
        return True


def _outward(pad: dict) -> tuple:
    """The way a pad's lead leaves its part: along the axis it extends furthest from the part's centre."""
    dx, dy = pad["x"] - pad["fx"], pad["y"] - pad["fy"]
    if abs(dx) >= abs(dy):
        return (math.copysign(1, dx) if abs(dx) > 1e-6 else 1.0, 0.0)
    return (0.0, math.copysign(1, dy))


def _turn_allowed(p: tuple, q: tuple, r: tuple) -> bool:
    """False for a corner of 90 degrees or sharper, which the RF rules forbid."""
    v1, v2 = (q[0] - p[0], q[1] - p[1]), (r[0] - q[0], r[1] - q[1])
    n1, n2 = math.hypot(*v1), math.hypot(*v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return True
    return (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2) > 0.5     # the direction changes by under 60 degrees


def _is_octilinear(p: tuple, q: tuple) -> bool:
    dx, dy = abs(q[0] - p[0]), abs(q[1] - p[1])
    return dx < 1e-3 or dy < 1e-3 or abs(dx - dy) < 1e-3


def necked_run(net: str, p: tuple, q: tuple, width: float, necks: list[float], obstacles: Obstacles, layer: str,
               neck_start: bool = True, neck_end: bool = True) -> list[dict]:
    """A straight run at full width, necked only as far as it must be at a fine-pitch end.

    Neighbouring pins can leave less room than a 50 ohm line needs, right where it lands. The neck is the
    shortest and widest that clears, so that the rest of the line keeps its impedance.
    """
    def seg(a, b, w):
        return {"net": net, "start": a, "end": b, "width": w, "layer": layer}
    if obstacles.clear(net, p, q, width):
        return [seg(p, q, width)]
    length = math.dist(p, q)
    ux, uy = (q[0] - p[0]) / length, (q[1] - p[1]) / length
    best = None
    for la_steps in range(0, 31) if neck_start else (0,):
        for lb_steps in range(0, 31) if neck_end else (0,):
            la, lb = 0.1 * la_steps, 0.1 * lb_steps
            if la + lb >= length or (la == 0 and lb == 0):
                continue
            a = (p[0] + ux * la, p[1] + uy * la)
            b = (q[0] - ux * lb, q[1] - uy * lb)
            if not obstacles.clear(net, a, b, width):
                continue
            wa = next((w for w in necks if obstacles.clear(net, p, a, w)), None) if la else width
            wb = next((w for w in necks if obstacles.clear(net, b, q, w)), None) if lb else width
            if wa is None or wb is None:
                continue
            cost = la + lb
            if best is None or cost < best[0]:
                best = (cost, a, b, wa, wb, la, lb)
    if best is None:
        return [seg(p, q, width)]       # nothing clears: draw it, and let the DRC sensor report it
    _, a, b, wa, wb, la, lb = best
    runs = []
    if la:
        runs.append(seg(p, a, wa))
    runs.append(seg(a, b, width))
    if lb:
        runs.append(seg(b, q, wb))
    return runs


def _spine_joins(p: tuple, spine: list[tuple]) -> list[tuple]:
    """Where octilinear rays from a point meet the spine, plus its nearest point."""
    joins = [_nearest_on(spine, p)]
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        for a, b in zip(spine, spine[1:]):
            sx, sy = b[0] - a[0], b[1] - a[1]
            det = dx * (-sy) - dy * (-sx)
            if abs(det) < 1e-9:
                continue
            t = ((a[0] - p[0]) * (-sy) - (a[1] - p[1]) * (-sx)) / det
            u = (dx * (a[1] - p[1]) - dy * (a[0] - p[0])) / det
            if t > 1e-6 and 0.0 <= u <= 1.0:
                joins.append((p[0] + dx * t, p[1] + dy * t))
    return joins


def plan_tap(pad: dict, spine: list[tuple], net: str, width: float, necks: list[float], obstacles: Obstacles) -> list[dict] | None:
    """The shortest clear stub from a pad to its spine: out of the pad, an optional 45-degree bend, then straight.

    The first segment may be narrower than the line, because neighbouring pins cap the width until it's
    past their ends. Returns None if nothing clears.
    """
    start = (pad["x"], pad["y"])
    d = _outward(pad)
    edge = pad["hw"] if d[0] else pad["hh"]
    # Leave neighbouring pins their escape room if at all possible. Only a tap with no other way crosses it.
    for respect in (True, False):
        best, best_score = None, float("inf")
        # Past the pad's end, plus the track's own half width and the clearance to anything beyond the pin row.
        for e_steps in range(0, 41):
            e = 0.0 if e_steps == 0 else edge + 0.1 * e_steps
            p1 = (start[0] + d[0] * e, start[1] + d[1] * e)
            for sign in (0, 1, -1):
                for c_steps in range(0, 31) if sign else (0,):
                    c = 0.1 * c_steps
                    side = (-d[1] * sign, d[0] * sign)
                    p2 = (p1[0] + (d[0] + side[0]) * c, p1[1] + (d[1] + side[1]) * c)
                    for q in _spine_joins(p2, spine):
                        if not _is_octilinear(p2, q):
                            continue
                        points = [start, p1, p2, q]
                        points = [x for i, x in enumerate(points) if i == 0 or math.dist(x, points[i - 1]) > 1e-3]
                        if len(points) < 2 or not all(_is_octilinear(a, b) for a, b in zip(points, points[1:])):
                            continue
                        if not all(_turn_allowed(*points[i:i + 3]) for i in range(len(points) - 2)):
                            continue
                        length = sum(math.dist(a, b) for a, b in zip(points, points[1:]))
                        score = length + 0.05 * (len(points) - 2)
                        if score >= best_score:
                            continue
                        neck = next((w for w in [width, *necks] if obstacles.clear(net, points[0], points[1], w, respect)), None)
                        if neck is None or not all(obstacles.clear(net, a, b, width, respect) for a, b in zip(points[1:], points[2:])):
                            continue
                        best_score = score
                        best = [{"net": net, "start": a, "end": b, "width": neck if i == 0 else width, "layer": "F.Cu"}
                                for i, (a, b) in enumerate(zip(points, points[1:]))]
        if best is not None:
            return best
    return None


def own_pads(pads: list[dict], pad: dict) -> list[dict]:
    """The other pads of a pad's own part that are on another net: what a line out of it must not cross."""
    return [q for q in pads if q is not pad and q["net"] != pad["net"] and (q["fx"], q["fy"]) == (pad["fx"], pad["fy"])
            and q.get("name") != pad.get("name")]


def _box_along(q: dict, origin: tuple, d: tuple) -> tuple[float, float, float, float]:
    """A box's extent along a direction from a point, then across it: (lowest along, highest along, lowest across, highest across)."""
    corners = [(q["x"] + sx * q["hw"] - origin[0], q["y"] + sy * q["hh"] - origin[1]) for sx in (-1, 1) for sy in (-1, 1)]
    along = [c[0] * d[0] + c[1] * d[1] for c in corners]
    across = [-c[0] * d[1] + c[1] * d[0] for c in corners]
    return min(along), max(along), min(across), max(across)


def pad_exit(pad: dict, own: list[dict], courtyard: tuple | None, width: float, clearance: float, toward: tuple) -> tuple | None:
    """Which way, and how far, a line runs straight out of an RF pad before it may turn: (direction, length from the pad's centre).

    The direction is the axis along which a line of this width, and its clearance, leaves the pad without crossing any of the part's own
    pads on another net. Of those, the one nearest the way the line is going. The length takes it past all of those pads by the
    clearance, and past the part's courtyard, so that a bend can't cut back across the part. None if no axis leaves the pad clear.
    """
    half = width / 2 + clearance
    start = (pad["x"], pad["y"])
    norm = math.hypot(*toward) or 1.0
    best = None
    for d in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
        boxes = [_box_along(q, start, d) for q in own]
        if any(hi > 0.0 and lo_x < half and hi_x > -half for _lo, hi, lo_x, hi_x in boxes):
            continue
        reach = [_box_along(pad, start, d)[1]] + [hi + half for _lo, hi, _a, _b in boxes]
        if courtyard:
            box = {"x": (courtyard[0] + courtyard[2]) / 2, "y": (courtyard[1] + courtyard[3]) / 2,
                   "hw": (courtyard[2] - courtyard[0]) / 2, "hh": (courtyard[3] - courtyard[1]) / 2}
            reach.append(_box_along(box, start, d)[1])
        score = (d[0] * toward[0] + d[1] * toward[1]) / norm
        if best is None or score > best[0] + 1e-9:
            best = (score, d, round(max(reach), 4))
    return (best[1], best[2]) if best else None


def _octilinear_path(points: list[tuple]) -> list[tuple] | None:
    """The points without repeats, if every run is octilinear and no corner is sharper than 45 degrees."""
    points = [x for i, x in enumerate(points) if i == 0 or math.dist(x, points[i - 1]) > 1e-3]
    if len(points) < 2 or not all(_is_octilinear(a, b) for a, b in zip(points, points[1:])):
        return None
    if not all(_turn_allowed(*points[i:i + 3]) for i in range(len(points) - 2)):
        return None
    return points


def escape_run(net: str, a: dict, b: dict, width: float, necks: list[float], obstacles: Obstacles, exits: dict,
               layer: str = "F.Cu") -> list[dict] | None:
    """A hop that leaves each end along its pad's exit, past its own part, before it bends: the shortest that clears.

    `exits` maps "a" and "b" to `pad_exit`'s answer, or None for an end that may be met straight. Each straight run out of a pad may be
    necked like any run at a pad. Returns None if no path of straight runs and 45-degree bends clears."""
    pa, pb = (a["x"], a["y"]), (b["x"], b["y"])

    def lengths(exit_):
        return [0.0] if exit_ is None else [exit_[1] + 0.1 * k for k in range(31)]
    best, best_score = None, float("inf")
    for ea in lengths(exits.get("a")):
        a1 = pa if exits.get("a") is None else (pa[0] + exits["a"][0][0] * ea, pa[1] + exits["a"][0][1] * ea)
        for eb in lengths(exits.get("b")):
            b1 = pb if exits.get("b") is None else (pb[0] + exits["b"][0][0] * eb, pb[1] + exits["b"][0][1] * eb)
            for middle in _spines(a1, b1):
                points = _octilinear_path([pa, *middle, pb])
                if points is None:
                    continue
                score = sum(math.dist(p, q) for p, q in zip(points, points[1:])) + 0.05 * (len(points) - 2)
                if score >= best_score:
                    continue
                segs = []
                last = len(points) - 2
                for i, (p, q) in enumerate(zip(points, points[1:])):
                    w = width if 0 < i < last or len(points) == 2 else next((n for n in [width, *necks] if obstacles.clear(net, p, q, n)), None)
                    if w is None or not obstacles.clear(net, p, q, w):
                        break
                    segs.append({"net": net, "start": p, "end": q, "width": w, "layer": layer})
                else:
                    best, best_score = segs, score
    return best


def _spines(a: tuple, b: tuple) -> list[list[tuple]]:
    """Ways from a to b of one straight run and one 45-degree run: `_spine`'s, and the diagonal first or last."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    out = [_spine(a, b)]
    diagonal = min(abs(dx), abs(dy))
    if diagonal > 1e-3 and abs(abs(dx) - abs(dy)) > 1e-3:
        sx, sy = math.copysign(1, dx), math.copysign(1, dy)
        corner = (a[0] + sx * diagonal, a[1] + sy * diagonal)
        out.append([a, corner, b])
        out.append([a, (b[0] - sx * diagonal, b[1] - sy * diagonal), b])
    return out


def plan_hop(net: str, a: dict, b: dict, width: float, necks: list[float], obstacles: Obstacles, layer: str = "F.Cu",
             pads: list[dict] | None = None, courtyards: dict | None = None, clearance: float = 0.2) -> tuple[list[dict], bool]:
    """The copper for one hop of an RF chain, and whether it had to leave a pad along its exit first.

    The hop is a spine of straight runs and 45-degree bends, necked where it must be at a fine-pitch end. If that would cross a pad of
    either end's own part on another net, each such end instead leaves its pad straight out, past its part, before it bends: see
    `escape_run`. If nothing clears that way either, the spine is drawn as it is, and the DRC sensor reports it."""
    spine = _spine((a["x"], a["y"]), (b["x"], b["y"]))
    last = len(spine) - 2
    plain = [seg for i, (p, q) in enumerate(zip(spine, spine[1:]))
             for seg in necked_run(net, p, q, width, necks, obstacles, layer, neck_start=i == 0, neck_end=i == last)]
    if pads is None:
        return plain, False
    exits, crossing = {}, False
    for key, pad, other in (("a", a, b), ("b", b, a)):
        own = own_pads(pads, pad)
        if any(_seg_rect(s["start"], s["end"], q) < obstacles.clearance + s["width"] / 2 for s in plain for q in own):
            crossing = True
            exits[key] = pad_exit(pad, own, (courtyards or {}).get((pad["fx"], pad["fy"])), width, clearance,
                                  (other["x"] - pad["x"], other["y"] - pad["y"]))
        else:
            exits[key] = None
    if not crossing:
        return plain, False
    run = escape_run(net, a, b, width, necks, obstacles, exits, layer)
    return (run, True) if run else (plain, False)


def rf_segments(board: Board, chains: list, width: float, net_width: dict | None = None, layer: str = "F.Cu",
                clearance: float = 0.2, necks: list[float] | None = None) -> tuple[list[dict], list[str]]:
    """Spines first, then a planned stub from every other pad. Returns segments and the pads no stub could reach."""
    necks = necks if necks is not None else [0.3, 0.25, 0.2]
    obstacles = Obstacles(board, layer, clearance)
    all_pads = board.all_pads()
    courtyards = board.courtyards()
    pads = {p["name"]: p for p in all_pads}
    out, spines, unreached = [], {}, []
    for net, entry, exit_ in chains:
        a_name, b_name = f"{entry[0]}.{entry[1]}", f"{exit_[0]}.{exit_[1]}"
        if a_name not in pads or b_name not in pads or pads[a_name]["net"] != net or pads[b_name]["net"] != net:
            raise ValueError(f"{net}: pad {a_name} or {b_name} isn't on the net")
        w = (net_width or {}).get(net, width)
        run, escaped = plan_hop(net, pads[a_name], pads[b_name], w, necks, obstacles, layer, all_pads, courtyards, clearance)
        # Taps join the copper actually laid, which is the spine unless the hop had to leave a pad first.
        spine = ([tuple(run[0]["start"]), *(tuple(s["end"]) for s in run)] if escaped
                 else _spine((pads[a_name]["x"], pads[a_name]["y"]), (pads[b_name]["x"], pads[b_name]["y"])))
        spines[net] = (spine, w, a_name, b_name)
        for seg in run:
            out.append(seg)
            obstacles.add(seg)
    for net, (spine, w, a_name, b_name) in spines.items():
        for name, pad in pads.items():
            if pad["net"] != net or name in (a_name, b_name):
                continue
            if layer not in pad["layers"] and "*.Cu" not in pad["layers"]:
                continue
            # The spine's centreline already lands on this pad, so copper joins them.
            if _point_rect(*_nearest_on(spine, (pad["x"], pad["y"])), pad) < 1e-3:
                continue
            stub = plan_tap(pad, spine, net, w, necks, obstacles)
            if stub is None:
                unreached.append(name)
                continue
            for seg in stub:
                out.append(seg)
                obstacles.add(seg)
    return out, unreached


def board_clearance(project_file: Path) -> float:
    """The Default net class's clearance, which is what the RF stubs must keep."""
    try:
        classes = json.loads(project_file.read_text())["net_settings"]["classes"]
        return next(float(c["clearance"]) for c in classes if c.get("name") == "Default")
    except (OSError, KeyError, StopIteration, ValueError):
        return 0.2


def drc_summary(board_path: Path, nets_of_interest: set | None = None) -> dict:
    """Errors, opens, and which errors touch the named nets."""
    out = board_path.with_suffix(".drc.json")
    # Refill first: the board's stored fill predates the copper just added, so it would read as false shorts.
    r = toolchain.run([toolchain.paths()["kicad_cli"], "pcb", "drc", "--format", "json", "--severity-error",
                       "--refill-zones", "-o", out, board_path], timeout=900)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"DRC failed with exit {r.returncode}: {r.stderr.strip()[-200:]}")
    d = json.loads(out.read_text())
    errors = [v for v in d["violations"] if v["severity"] == "error"]

    def touches(v):
        return any(f"[{n}]" in i["description"] for n in (nets_of_interest or ()) for i in v.get("items", []))
    hits = [v for v in errors if touches(v)]
    return {"errors": len(errors), "opens": len(d["unconnected_items"]), "errors_on_nets": len(hits),
            "examples": [f"{v['type']}: {v['items'][0]['description'][:60]}" for v in (hits or errors)[:4]]}


def measure_z0(board_path: Path, net: str, target: float, work: Path) -> float:
    """circuitRF's cross-section Z0 for a net's copper, away from its pads."""
    tp = toolchain.paths()
    env = toolchain.circuitrf_env()
    cells = work / "z_cells"
    shutil.rmtree(cells, ignore_errors=True)
    toolchain.run([tp["circuitrf"], "convert", board_path, "-o", cells, "--to", "clay"], env=env, timeout=300)
    clay = toolchain.glob_one(str(cells / board_path.stem / "layout" / "*.clay"))
    if not clay:
        raise RuntimeError("circuitRF could not import the board")
    r = toolchain.run([tp["circuitrf"], "impedance", clay, "--target", target, "--net", net], env=env, timeout=300)
    m = re.search(r"Z0 ([\d.]+)(?:–([\d.]+))? Ω(?: \(avg ([\d.]+)\))?", r.stdout)
    if not m:
        raise RuntimeError(f"circuitRF reported no trace on {net}: {r.stdout.strip()[-200:]}")
    return float(m.group(3) or m.group(1))


def _without_parts(text: str) -> str:
    """Board text with footprints, tracks and vias gone. pcbnew's bindings break after removing a footprint."""
    out, i, depth, in_string = [], 0, 0, False
    drop_from = None
    n = len(text)
    while i < n:
        c = text[i]
        if in_string:
            if c == "\\":
                i += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "(":
            if depth == 1 and text[i + 1:].split(None, 1)[0].rstrip(")") in ("footprint", "segment", "via", "arc"):
                drop_from = i
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 1 and drop_from is not None:
                out.append(i + 1)
                out.append(drop_from)
                drop_from = None
        i += 1
    # Cuts come as (end, start) pairs in order.
    pieces, last = [], 0
    for end, start in zip(out[0::2], out[1::2]):
        pieces.append(text[last:start])
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def solve_rf_width(base: Path, chains: list, start_mm: float, target: float, work: Path, tol_ohm: float = 0.5,
                   limit: int = 6) -> tuple[float, list[dict]]:
    """Find the RF width with the field solver in the loop, from the board's own stackup and pours.

    Probes on the shortest two-pad net, so that taps don't pull the reading. Secant steps on Z0 against width.
    """
    board = Board(base)
    plain = [c for c in chains if len(board.net_pads(c[0])) == 2] or chains
    probe = min(plain, key=lambda c: 0 if c else 1)
    history: list[dict] = []
    probe_board = work / "z_probe.kicad_pcb"

    def z_at(w: float) -> float:
        spec = work / "z_probe.json"
        spec.write_text(json.dumps(rf_segments(board, [probe], w)[0]))
        _op("add_locked", base, spec, probe_board)
        try:
            z, where = measure_z0(probe_board, probe[0], target, work), "board"
        except RuntimeError:
            # A hop packed between two parts is too short to read. A coupon has the same stackup and pours.
            stripped = work / "z_stripped.kicad_pcb"
            stripped.write_text(_without_parts(base.read_text()))
            _op("z_coupon", stripped, probe[0], w, probe_board)
            z, where = measure_z0(probe_board, probe[0], target, work), "coupon"
        history.append({"width_mm": round(w, 4), "z0_ohm": z, "read_on": where})
        return z
    w0, z0 = start_mm, z_at(start_mm)
    if abs(z0 - target) <= tol_ohm:
        return w0, history
    w1 = w0 * (z0 / target) if z0 > target else w0 * (z0 / target)
    for _ in range(limit - 1):
        w1 = round(max(w1, 0.1), 3)
        z1 = z_at(w1)
        if abs(z1 - target) <= tol_ohm:
            return w1, history
        slope = (z1 - z0) / (w1 - w0) if w1 != w0 else -1.0
        w0, z0 = w1, z1
        w1 = w0 + (target - z0) / (slope or -1.0)
    raise RuntimeError(f"RF width did not converge: {history}")


def _load_chains(path: Path) -> tuple[list, float, dict]:
    from .scripts.kicad_pcb import load_chains
    return load_chains(path)


def _op(*args, timeout=600) -> dict:
    r = toolchain.run([toolchain.paths()["kicad_python"], OPS, *args], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {(r.stdout + r.stderr).strip()[-400:]}")
    for line in reversed(r.stdout.strip().splitlines()):
        if line.startswith("{"):
            return json.loads(line)
    return {}


def project_rules(project_file: Path) -> dict:
    """The rules a router must keep to, from the project itself: the Default class and the board minimums."""
    try:
        data = json.loads(project_file.read_text())
        rules = data["board"]["design_settings"]["rules"]
        default = next(c for c in data["net_settings"]["classes"] if c.get("name") == "Default")
    except (OSError, KeyError, StopIteration, ValueError):
        return {}
    return {"clearance": default.get("clearance"), "track_width": default.get("track_width"),
            "via_size": default.get("via_diameter"), "via_drill": default.get("via_drill"),
            "min_track_width": rules.get("min_track_width"), "min_via_diameter": rules.get("min_via_diameter"),
            "min_hole": rules.get("min_through_hole_diameter"), "min_annular": rules.get("min_via_annular_width")}


def krt_rule_args(rules: dict) -> list[str]:
    """Pin the router to the project's rules. Left alone, it lowers them to make its copper pass."""
    args = ["--no-fix-drc-settings", "--escalation", "board"]
    for flag, key in (("--clearance", "clearance"), ("--track-width", "track_width"),
                      ("--via-size", "via_size"), ("--via-drill", "via_drill")):
        if rules.get(key):
            args += [flag, str(rules[key])]
    return args


def _krt(board_in: Path, board_out: Path, nets: list[str], extra: list[str] | None = None, timeout=1800,
         project: Path | None = None) -> dict:
    """Run the router, with rules pinned when a project is given. The project file is always put back."""
    krt = Path(toolchain.paths()["krt"])
    js = board_out.with_suffix(".krt.json")
    rules = project_rules(project) if project else {}
    pinned = krt_rule_args(rules) if project else []
    r = toolchain.run([KRT_PYTHON, krt / "py_router/route.py", board_in, board_out, "--nets", *nets,
                       "--json-out", js, *pinned, *(extra or [])], cwd=krt, env={"PYTHONDONTWRITEBYTECODE": "1"}, timeout=timeout)
    data = json.loads(js.read_text()) if js.exists() else {}
    changed = None
    if project:
        written = board_out.with_suffix(".kicad_pro")
        changed = written.exists() and project_rules(written) != rules
        shutil.copy(project, written)
    return {"rc": r.returncode, "failed": data.get("failed"), "successful": data.get("successful"),
            "min_clearance_used": data.get("min_clearance_used"), "rules_rewritten_by_router": changed,
            "tail": (r.stdout + r.stderr).strip()[-300:] if r.returncode else ""}


def open_nets(board_path: Path) -> list[str]:
    """Nets DRC still reports open. A net routed first can fail without appearing in the last pass's summary."""
    data = json.loads(board_path.with_suffix(".drc.json").read_text())
    names = {m for u in data["unconnected_items"] for i in u["items"] for m in re.findall(r"\[(.+?)\]", i["description"])}
    return sorted(names)


def failed_nets(summary: Path) -> list[str]:
    """Nets the router reports as unfinished, from its JSON summary."""
    if not summary.exists():
        return []
    data = json.loads(summary.read_text())
    names = [x if isinstance(x, str) else x.get("net_name") for x in data.get("failed_single", [])]
    names += [x.get("net_name") for x in data.get("failed_multipoint", [])]
    names += [x if isinstance(x, str) else x.get("net_name") for x in data.get("open_single", [])]
    return sorted({n for n in names if n})


def power_widths(rails: list[dict], thickness_um: float, max_rise_c: float, outer: bool = True) -> dict[str, float]:
    """Track width per rail, from its total current. `outer` is False when the router may lay power on an inner layer:
    IPC-2221 lets an inner track shed only about half the heat, so it must be wider. The sensor checks what was drawn."""
    widths = {}
    for rail in rails:
        amps = sum(float(v) for v in rail["loads"].values())
        need = power.ipc2221_min_width_mm(amps, float(rail.get("max_rise_c", max_rise_c)), thickness_um, outer=outer)
        widths[rail["net"]] = round(max(need, MIN_POWER_WIDTH_MM), 3)
    return widths


ESCAPE_WIDTH_MM = 0.1524     # the fabricator's narrowest, used only where the board's normal width doesn't fit between pads


def escape_stubs(board: Board, failed: list[str], rf_nets: set, clearance: float, layer: str = "F.Cu") -> list[dict]:
    """Straight stubs out of fine-pitch pins beside an RF pin, on any part with a pin the router couldn't reach.

    Beside an RF pin there's the line on one side and the next pin's via on the other, and a normal track doesn't fit between them.
    Every pin near the RF pin gets one, not just the failed one, since it's usually its neighbour's via that blocks it. Each stub stops
    where it can, and the router carries on from its end.
    """
    pads = [p for p in board.all_pads() if layer in p["layers"]]
    parts: dict[tuple, list[dict]] = {}
    for p in pads:
        parts.setdefault((p["fx"], p["fy"]), []).append(p)
    obstacles = Obstacles(board, layer, clearance)
    existing = {(t.start, t.net) for t in board.tracks if t.kind != "via"}
    out = []
    for group in parts.values():
        if len(group) < 8 or not any(p["net"] in failed for p in group):
            continue
        sides: dict[tuple, list[dict]] = {}
        for p in group:
            if abs(p["x"] - p["fx"]) > 1e-6 or abs(p["y"] - p["fy"]) > 1e-6:
                sides.setdefault(_outward(p), []).append(p)
        for d, side in sides.items():
            along = (lambda q: q["y"]) if d[0] else (lambda q: q["x"])
            ordered = sorted(along(q) for q in side)
            pitch = min((b - a for a, b in zip(ordered, ordered[1:]) if b - a > 1e-6), default=0.0)
            rf_pins = [q for q in side if q["net"] in rf_nets]
            if not rf_pins or pitch == 0.0 or pitch > 0.65:
                continue
            for q in side:
                if q["net"] in rf_nets or q["net"] in ("", "GND") or q["net"].startswith("unconnected-"):
                    continue
                if min(abs(along(q) - along(r)) for r in rf_pins) > 2.2 * pitch + 1e-6:
                    continue
                edge = q["hw"] if d[0] else q["hh"]
                a = (q["x"], q["y"])
                # Longer the nearer the RF pin, so that each stub ends past its outer neighbour's and has room to turn or drop a via.
                nearest = min(abs(along(q) - along(r)) for r in rf_pins)
                for reach in ((1.5, 1.0, 0.7, 0.4) if nearest <= 1.2 * pitch else (0.7, 0.4)):
                    b = (round(q["x"] + d[0] * (edge + reach), 4), round(q["y"] + d[1] * (edge + reach), 4))
                    if (a, q["net"]) not in existing and obstacles.clear(q["net"], a, b, ESCAPE_WIDTH_MM):
                        stub = {"net": q["net"], "start": list(a), "end": list(b), "width": ESCAPE_WIDTH_MM, "layer": layer}
                        obstacles.add({"net": q["net"], "start": a, "end": b, "width": ESCAPE_WIDTH_MM})
                        out.append(stub)
                        break
    return out


def dangling_stubs(drc: dict, stubs: list[dict]) -> list[dict]:
    """The stubs KiCad reports with a free end. The net reached that pin some other way, so each is a dead end beside an RF line."""
    ends = []
    for v in drc.get("violations", []):
        if v.get("type") != "track_dangling":
            continue
        for item in v.get("items", []):
            named = re.search(r"\[([^\]]+)\]", item.get("description", ""))
            if named:
                ends.append((named[1], item["pos"]["x"], item["pos"]["y"]))
    return [s for s in stubs if any(net == s["net"] and (math.dist((x, y), s["start"]) < 0.05 or math.dist((x, y), s["end"]) < 0.05) for net, x, y in ends)]


def stub_cut(board: Board, stub: dict, touch_mm: float = 0.05) -> list[float] | None:
    """Where a stub's copper stops being used: the join furthest along it. None if nothing joins it past its pad, so all of it can go."""
    (ax, ay), (bx, by) = stub["start"], stub["end"]
    length = math.hypot(bx - ax, by - ay) or 1.0
    ux, uy = (bx - ax) / length, (by - ay) / length
    furthest = None
    for t in board.tracks:
        if t.net != stub["net"]:
            continue
        if t.kind != "via" and {tuple(t.start), tuple(t.end)} == {(ax, ay), (bx, by)}:
            continue
        for x, y in (t.start,) if t.kind == "via" else (t.start, t.end):
            along = (x - ax) * ux + (y - ay) * uy
            if 0.0 < along <= length + touch_mm and abs((x - ax) * uy - (y - ay) * ux) <= touch_mm:
                furthest = max(furthest or 0.0, min(along, length))
    if furthest is None:
        return None
    return [round(ax + ux * furthest, 4), round(ay + uy * furthest, 4)]


def dangling_tails(drc: dict, board: Board, rf_nets: set) -> list[dict]:
    """Ordinary tracks KiCad reports with a free end, such as a router's short leftover. RF copper is never touched here."""
    out = []
    for v in drc.get("violations", []):
        if v.get("type") != "track_dangling":
            continue
        for item in v.get("items", []):
            named = re.search(r"\[([^\]]+)\]", item.get("description", ""))
            size = re.search(r"length ([\d.]+) mm", item.get("description", ""))
            if not named or not size or named[1] in rf_nets:
                continue
            at = (item["pos"]["x"], item["pos"]["y"])
            for t in board.tracks:
                if t.kind != "via" and t.net == named[1] and abs(math.dist(t.start, t.end) - float(size[1])) < 0.001 and min(math.dist(at, t.start), math.dist(at, t.end)) < 0.01:
                    out.append({"net": t.net, "start": list(t.start), "end": list(t.end)})
                    break
    return out


def keep_retrying(improved: bool, elapsed_s: float) -> bool:
    """Whether another signal attempt is worth its time. Small boards always try again, as an attempt costs seconds and order is luck.

    On a large board each retry routes the failed nets one by one, which takes most of an hour, so one that gained nothing ends it.
    """
    return improved or elapsed_s < RETRY_WITHOUT_GAIN_S


def _layer_args(layers: list, costs: dict | None) -> list[str]:
    """The router's layers, each with a cost when any is given."""
    if not layers:
        return []
    return ["--layers", *layers, *(["--layer-costs", *[str(float(costs.get(x, 1.0))) for x in layers]] if costs else [])]


def route(project_board: Path, chains_file: Path, rails: list[dict], work: Path, keep_nets: list[str],
          max_rise_c: float = 10.0, rf_target_ohm: float = 50.0, reference_layers: tuple = ("In1.Cu",),
          power_layers: tuple | None = None, layer_costs: dict | None = None) -> dict:
    """Run RF, then power, then signal, on copies in `work`. The input board isn't changed.

    `layer_costs` makes a layer dearer without forbidding it. A pad still gets its short stub on the top layer, and the run goes inside."""
    work.mkdir(parents=True, exist_ok=True)
    stem = "board"
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if project_board.with_suffix(ext).exists():
            shutil.copy(project_board.with_suffix(ext), work / f"{stem}{ext}")
    for table in ("fp-lib-table", "sym-lib-table"):
        if (project_board.parent / table).exists():
            shutil.copy(project_board.parent / table, work / table)
    log: dict = {"steps": []}
    stripped = work / f"{stem}.kicad_pcb"
    _op("export_dsn", project_board, stripped, work / "unused.dsn", ",".join(keep_nets), "all")
    current = stripped

    # RF
    chains, width, net_width = _load_chains(chains_file)
    board = Board(current)
    width, history = solve_rf_width(current, chains, width, rf_target_ohm, work)
    log["rf_width_loop"] = history
    clearance = board_clearance(project_board.with_suffix(".kicad_pro"))
    rf_names = {c[0] for c in chains}
    rf_out = work / "1_rf.kicad_pcb"
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if (work / f"{stem}{ext}").exists():
            shutil.copy(work / f"{stem}{ext}", rf_out.with_suffix(ext))
    # Sensor: DRC right after the RF pass. Narrower escapes are the one thing to try before giving up.
    for necks in ([0.3, 0.25, 0.2], [0.25, 0.2], [0.2]):
        segments, unreached = rf_segments(board, chains, width, net_width, clearance=clearance, necks=necks)
        spec = work / "rf_segments.json"
        spec.write_text(json.dumps(segments))
        _op("add_locked", current, spec, rf_out)
        sensor = drc_summary(rf_out, rf_names)
        if sensor["errors_on_nets"] == 0:
            break
    log["steps"].append({"class": "rf", "nets": len(chains), "segments": len(segments), "necks": necks,
                         "unreached": unreached, "drc": sensor})
    if sensor["errors_on_nets"]:
        log["stopped_at"] = "rf"
        return log
    current = rf_out

    # Nothing else may cut the plane the RF lines are referenced to.
    allowed = [lyr for lyr in Board(current).copper_thickness_um() if lyr not in reference_layers]
    layer_args = _layer_args(allowed, layer_costs)
    power_layer_args = _layer_args(list(power_layers), layer_costs) if power_layers else layer_args

    # Power
    thickness = Board(current).copper_thickness_um()
    inner = min(thickness.values(), default=35.0)
    # Sized for the worst layer power may take: inner, unless only the outer layers are open to it.
    power_may_use = list(power_layers) if power_layers else allowed
    widths = power_widths(rails, inner, max_rise_c, outer=all(lyr in ("F.Cu", "B.Cu") for lyr in power_may_use))
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if (work / f"{stem}{ext}").exists():
            shutil.copy(work / f"{stem}{ext}", work / f"2_power{ext}")
    power_out = work / "2_power.kicad_pcb"
    if widths:
        result = _krt(current, power_out, list(widths), ["--power-nets", *widths, "--power-nets-widths",
                                                         *[str(w) for w in widths.values()],
                                                         "--no-power-tap-neckdown", *power_layer_args], project=work / f"{stem}.kicad_pro")
        log["steps"].append({"class": "power", "widths_mm": widths, **result})
        if not power_out.exists():
            log["stopped_at"] = "power"
            return log
        sensor = drc_summary(power_out, set(widths) | rf_names)
        log["steps"][-1]["drc"] = sensor
        if sensor["errors_on_nets"]:
            log["stopped_at"] = "power"
            return log
        current = power_out

    # Signal
    done = rf_names | set(widths) | set(keep_nets)
    remaining = sorted(n for n in Board(current).net_names() - done if not n.startswith("unconnected-"))
    # A tap the RF pass couldn't plan goes to the router, on its own net, and the log says so.
    pads_by_name = {p["name"]: p["net"] for p in Board(current).all_pads()}
    handed_over = sorted({pads_by_name[n] for n in unreached})
    log["rf_taps_handed_to_router"] = unreached
    if handed_over:
        # Still RF, so it routes at the line's width, before the signals can take the room.
        handed = work / "1b_rf_taps.kicad_pcb"
        for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
            if (work / f"{stem}{ext}").exists():
                shutil.copy(work / f"{stem}{ext}", handed.with_suffix(ext))
        result = _krt(current, handed, handed_over, [*layer_args, "--track-width", str(width)], project=work / f"{stem}.kicad_pro")
        log["steps"].append({"class": "rf_taps", "nets": handed_over, **result})
        if not handed.exists():
            log["stopped_at"] = "rf_taps"
            return log
        current = handed
    final = work / "3_signal.kicad_pcb"
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if (work / f"{stem}{ext}").exists():
            shutil.copy(work / f"{stem}{ext}", final.with_suffix(ext))
    if remaining:
        project = work / f"{stem}.kicad_pro"
        first: list[str] = []
        best, best_file = None, work / "3_signal_best.kicad_pcb"
        began = time.time()
        for attempt in range(1, SIGNAL_ATTEMPTS + 1):
            before = current
            # A net that failed goes first next time, one at a time in the order they failed, since a
            # batch lets the router choose the order again. Everything else then routes around them.
            placed: list[str] = []
            for i, net in enumerate(first):
                early = work / f"3_signal_first_{attempt}_{i}.kicad_pcb"
                _krt(before, early, [net], layer_args, project=project)
                if early.exists():
                    before = early
                    # KiCad decides whether it's connected. The router's own report can disagree.
                    drc_summary(early, {net})
                    if net not in open_nets(early):
                        placed.append(net)
            result = _krt(before, final, [n for n in remaining if n not in placed], layer_args, project=project)
            result.update(attempt=attempt, routed_first=list(first))
            log["steps"].append({"class": "signal", "nets": len(remaining), **result})
            if not final.exists():
                log["stopped_at"] = "signal"
                return log
            sensor = drc_summary(final, rf_names)
            log["steps"][-1]["drc"] = sensor
            # Keep the best attempt, not the last: a retry can be worse than the one before it.
            score = (sensor["opens"], sensor["errors"])
            improved = best is None or score < best[0]
            if improved:
                best = (score, attempt)
                shutil.copy(final, best_file)
            if sensor["opens"] == 0:
                break
            failed = failed_nets(final.with_suffix(".krt.json")) or open_nets(final)
            if not failed or attempt == SIGNAL_ATTEMPTS:
                break
            if not keep_retrying(improved, time.time() - began):
                log["stopped_retrying"] = f"attempt {attempt} gained nothing after {round(time.time() - began)} s"
                break
            first = [*first, *[n for n in failed if n not in first]]
            final.unlink()
            stubs = escape_stubs(Board(current), failed, rf_names, board_clearance(project))
            if stubs:
                # Into the base of every later attempt, so the router starts from where the stubs end.
                spec = work / f"escape_stubs_{attempt}.json"
                spec.write_text(json.dumps(stubs))
                stubbed = work / f"3_escapes_{attempt}.kicad_pcb"
                _op("add_locked", current, spec, stubbed)
                shutil.copy(project, stubbed.with_suffix(".kicad_pro"))
                current = stubbed
                log.setdefault("escape_stubs", []).extend(s["net"] for s in stubs)
        shutil.copy(best_file, final)
        log["best_signal_attempt"] = best[1]
        made = [s for f in sorted(work.glob("escape_stubs_*.json")) for s in json.loads(f.read_text())]
        trimmed = []
        if made:
            # Dangling track is only a warning, so the summary above doesn't see it.
            seen = work / "3_signal_warnings.json"
            toolchain.run([toolchain.paths()["kicad_cli"], "pcb", "drc", "--format", "json", "--severity-warning", "--refill-zones", "-o", seen, final], timeout=900)
            trimmed = dangling_stubs(json.loads(seen.read_text()), made) if seen.exists() else []
        if trimmed:
            spec = work / "unused_stubs.json"
            spec.write_text(json.dumps(trimmed))
            trial = work / "3_signal_trimmed.kicad_pcb"
            shutil.copy(project, trial.with_suffix(".kicad_pro"))
            _op("remove_tracks", final, spec, trial)
            cut = Board(final)
            kept_part = [{**s, "end": stub_cut(cut, s)} for s in trimmed if stub_cut(cut, s)]
            if kept_part:
                (work / "stub_stays.json").write_text(json.dumps(kept_part))
                _op("add_locked", trial, work / "stub_stays.json", trial)
            # KiCad has the last word on connectivity: a trim that opens anything is thrown away.
            kept = drc_summary(trial, rf_names)["opens"] <= best[0][0]
            if kept:
                shutil.copy(trial, final)
            log["escape_stubs_unused"] = {"nets": [s["net"] for s in trimmed], "cut_back": len(kept_part), "applied": kept}
        # Removing a tail can leave the one behind it free, so this goes round a few times.
        for _ in range(3):
            seen = work / "3_signal_warnings.json"
            toolchain.run([toolchain.paths()["kicad_cli"], "pcb", "drc", "--format", "json", "--severity-warning", "--refill-zones", "-o", seen, final], timeout=900)
            tails = dangling_tails(json.loads(seen.read_text()), Board(final), rf_names) if seen.exists() else []
            if not tails:
                break
            spec = work / "dangling_tails.json"
            spec.write_text(json.dumps(tails))
            trial = work / "3_signal_tidied.kicad_pcb"
            shutil.copy(project, trial.with_suffix(".kicad_pro"))
            _op("remove_tracks", final, spec, trial)
            if drc_summary(trial, rf_names)["opens"] > best[0][0]:
                break
            shutil.copy(trial, final)
            log["dangling_tails_removed"] = log.get("dangling_tails_removed", 0) + len(tails)
    else:
        shutil.copy(current, final)
    log["final"] = str(final)
    log["final_drc"] = drc_summary(final, rf_names)
    log["rules_kept"] = project_rules(final.with_suffix(".kicad_pro")) == project_rules(work / f"{stem}.kicad_pro")
    return log
