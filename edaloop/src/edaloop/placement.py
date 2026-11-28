"""The placement critic: what a placed board will cost to route, read before anything is routed.

Every number here is an estimate from the pads alone, made in seconds, so that a placement can be judged (or a placer can
search) before a router spends an hour finding out. Four readings:

  rf_hops     each RF hop's shortest path on its own layer around every other pad, against its floor. A router can't beat
              the shortest path on the layer without a via, and an RF line shouldn't take one, so a hop already over its
              limit here will be over it after routing. Found on a 0.1 mm grid, then pulled straight between corners.
  escapes     a part's dedicated pins (a pin whose net goes only to one two-pad part, such as a decoupling or ACG cap):
              the straight escape from each pin to its part, and whether it crosses the IC's other pins or another
              escape. Caps out of pin order are a crossing, and a crossing is a via or a pin that doesn't route.
  rf_crossings  every other net's spanning tree against the RF lines. Each crossing is a layer change on both sides of
              an RF line; one beside a limiter puts supply or control copper in the ground the limiter is referenced to.
  congestion  RUDY (Spindler and Johannes, 2007): each routed net's half-perimeter wire spread over its bounding box,
              summed per tile and divided by the tile's track supply. RUDY reads low, since half-perimeter is the least
              wire a net can use, so a tile over 1 here is over for certain.

`critique` returns readings, findings and one `cost`, a single number a placement search can minimise.
"""
from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field

import numpy as np

from .scripts.kicad_pcb import Board

LIMITER = re.compile(r"^(RLM|VLM)-|limiter", re.I)

DEFAULTS = {
    "rf_layer": "F.Cu",             # the layer the RF lines are laid on
    "clearance_mm": 0.2,            # copper to copper
    "grid_mm": 0.1,                 # the search grid for an RF hop's path
    "neck_mm": 0.6,                 # within this of its own pad a line may neck down to clear a fine-pitch neighbour
    "window_mm": 3.0,               # how far past a hop's own pads its path may wander, before the window is doubled once
    "escape_width_mm": 0.15,        # the track a dedicated pin escapes with
    "min_ic_pads": 6,               # a part with at least this many pads has dedicated pins worth checking
    "limiter_band_mm": 1.5,         # a crossing this near a limiter's pads is in its ground
    "tile_mm": 2.0,                 # RUDY tile
    "track_pitch_mm": 0.4,          # track plus clearance, for a tile's supply
    "max_utilisation": 1.0,         # RUDY demand over supply before a tile is reported
    "ground_nets": ["GND"],         # nets the pours connect; never routed as tracks
    "search_cap": None,             # optional max heap pops per A* search (None = uncapped)
    "cost_weights": {"rf_excess_mm": 1.0, "escape": 5.0, "rf_crossing": 0.2, "limiter_crossing": 5.0, "overflow": 2.0},
}


@dataclass
class Critique:
    metrics: dict = field(default_factory=dict)
    findings: list = field(default_factory=list)      # (id, message, severity)
    hops: list = field(default_factory=list)
    cost: float = 0.0


# ----------------------------------------------------------------------------------------------- geometry

def _rect_distance(xs, ys, pad) -> np.ndarray:
    """Distance from each point to a pad's rotated rectangle, 0 inside. The same test as kicad_pcb.pt_rect, over arrays."""
    a = math.radians(pad.ang)
    dx, dy = xs - pad.x, ys - pad.y
    lx = dx * math.cos(a) - dy * math.sin(a)
    ly = dx * math.sin(a) + dy * math.cos(a)
    return np.hypot(np.maximum(np.abs(lx) - pad.hx, 0.0), np.maximum(np.abs(ly) - pad.hy, 0.0))


def _reach(pad) -> float:
    return math.hypot(pad.hx, pad.hy)


def _seg_distance(px, py, x1, y1, x2, y2):
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    t = np.clip(((px - x1) * vx + (py - y1) * vy) / L2, 0.0, 1.0) if L2 > 1e-12 else 0.0
    return np.hypot(px - (x1 + t * vx), py - (y1 + t * vy))


def _cross(a, b, c, d) -> tuple | None:
    """The point where segments ab and cd cross, not counting a shared end."""
    r = (b[0] - a[0], b[1] - a[1])
    s = (d[0] - c[0], d[1] - c[1])
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-12:
        return None
    t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den
    u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (a[0] + t * r[0], a[1] + t * r[1])
    return None


def _mst_edges(points: list[tuple]) -> list[tuple]:
    if len(points) < 2:
        return []
    inside, out, edges = [0], list(range(1, len(points))), []
    while out:
        best = min(((math.dist(points[i], points[j]), i, j) for j in out for i in inside))
        edges.append((points[best[1]], points[best[2]]))
        inside.append(best[2])
        out.remove(best[2])
    return edges


# ----------------------------------------------------------------------------------------------- RF hops

class _Grid:
    """Free cells for one net's line on one layer, in a window."""

    def __init__(self, board, net, layer, half_w, window, cfg, own_pads, obstacles, laid=()):
        x0, y0, x1, y1 = window
        g = cfg["grid_mm"]
        self.g, self.x0, self.y0 = g, x0, y0
        self.nx, self.ny = int((x1 - x0) / g) + 1, int((y1 - y0) / g) + 1
        xs = x0 + g * np.arange(self.nx)
        ys = y0 + g * np.arange(self.ny)
        X, Y = np.meshgrid(xs, ys, indexing="ij")
        need_full = cfg["clearance_mm"] + half_w
        # Near its own pads a line may neck down to the pad's own width, as a router does to leave a fine-pitch pin.
        near_own = np.zeros_like(X, dtype=bool)
        for p in own_pads:
            near_own |= _rect_distance(X, Y, p) < cfg["neck_mm"]
        need = np.where(near_own, cfg["clearance_mm"], need_full)
        blocked = np.zeros_like(X, dtype=bool)
        for p in obstacles:
            if p.x + _reach(p) + need_full < x0 or p.x - _reach(p) - need_full > x1 or \
               p.y + _reach(p) + need_full < y0 or p.y - _reach(p) - need_full > y1:
                continue
            blocked |= _rect_distance(X, Y, p) < need
        for s in board.segs:
            if s["net"] == net or s["layer"] != layer or not s.get("locked"):
                continue
            blocked |= _seg_distance(X, Y, s["x1"], s["y1"], s["x2"], s["y2"]) < need + s["w"] / 2
        for v in board.vias:
            if v["net"] == net:
                continue
            blocked |= np.hypot(X - v["x"], Y - v["y"]) < need + v["d"] / 2
        # RF lines already estimated: another RF line can't run through them either.
        for (ax, ay), (bx, by), w in laid:
            if max(ax, bx) + need_full + w < x0 or min(ax, bx) - need_full - w > x1 or \
               max(ay, by) + need_full + w < y0 or min(ay, by) - need_full - w > y1:
                continue
            blocked |= _seg_distance(X, Y, ax, ay, bx, by) < need + w / 2
        # A line can always stand on its own pads.
        for p in own_pads:
            blocked &= ~(_rect_distance(X, Y, p) < 1e-9)
        self.free = ~blocked

    def cell(self, x, y):
        return (min(max(int(round((x - self.x0) / self.g)), 0), self.nx - 1),
                min(max(int(round((y - self.y0) / self.g)), 0), self.ny - 1))

    def xy(self, c):
        return (self.x0 + c[0] * self.g, self.y0 + c[1] * self.g)

    def line_free(self, a, b) -> bool:
        n = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        for k in range(n + 1):
            t = k / n if n else 0
            if not self.free[int(round(a[0] + t * (b[0] - a[0]))), int(round(a[1] + t * (b[1] - a[1])))]:
                return False
        return True

    def path(self, start, goals, search_cap: int | None = None) -> list | None:
        """Octile A* from a start cell to any goal cell, then pulled straight between corners that can see each other."""
        goals = set(goals)
        if not goals:
            return None
        gl = list(goals)

        def h(c):
            return min(max(abs(c[0] - q[0]), abs(c[1] - q[1])) + (math.sqrt(2) - 1) * min(abs(c[0] - q[0]), abs(c[1] - q[1])) for q in gl[:8])
        dist = {start: 0.0}
        prev = {}
        heap = [(h(start), 0.0, start)]
        steps = [(1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, math.sqrt(2)), (1, -1, math.sqrt(2)), (-1, 1, math.sqrt(2)), (-1, -1, math.sqrt(2))]
        end = None
        pops = 0
        while heap:
            pops += 1
            if search_cap and pops > search_cap:
                return None
            _, d, c = heapq.heappop(heap)
            if d > dist.get(c, math.inf):
                continue
            if c in goals:
                end = c
                break
            for dx, dy, w in steps:
                n = (c[0] + dx, c[1] + dy)
                if not (0 <= n[0] < self.nx and 0 <= n[1] < self.ny) or not self.free[n]:
                    continue
                nd = d + w
                if nd < dist.get(n, math.inf):
                    dist[n], prev[n] = nd, c
                    heapq.heappush(heap, (nd + h(n), nd, n))
        if end is None:
            return None
        cells = [end]
        while cells[-1] != start:
            cells.append(prev[cells[-1]])
        cells.reverse()
        pulled = [cells[0]]
        i = 0
        while i < len(cells) - 1:
            j = len(cells) - 1
            while j > i + 1 and not self.line_free(cells[i], cells[j]):
                j -= 1
            pulled.append(cells[j])
            i = j
        return [self.xy(c) for c in pulled]


def _net_paths(board, net, rows, width, cfg, layer_pads, laid=()) -> dict:
    """One RF net's shortest copper: its first row entry to exit, each later row from its entry to its exit or to the
    copper already laid (a combining node is one tree, not five lines), and each remaining pad by its shortest stub."""
    layer = cfg["rf_layer"]
    ends = [(board.index.get(a), board.index.get(z), a, z) for a, z in rows]
    missing = [f"{a[0]}.{a[1]} / {z[0]}.{z[1]}" for pa, pz, a, z in ends if not pa or not pz]
    if missing:
        return {"net": net, "missing": missing[0]}
    own = board.net_pads(net)
    a0, z0 = rows[0]
    floor = board.floor(net, a0, z0)
    xs, ys = [p.x for p in own], [p.y for p in own]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    obstacles = [p for p in layer_pads if p.net != net]
    margin = max(cfg["window_mm"], 0.25 * span)
    # A long net is searched on a coarser grid, so its window stays near a million cells.
    sub = {**cfg, "grid_mm": max(cfg["grid_mm"], (span + 2 * margin) / 1000)}
    lines = None
    for grow in (1, 2, 4):
        m = margin * grow
        grid = _Grid(board, net, layer, width / 2, (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m), sub, own, obstacles, laid)
        X, Y = np.meshgrid(grid.x0 + grid.g * np.arange(grid.nx), grid.y0 + grid.g * np.arange(grid.ny), indexing="ij")
        lines, tree = [], set()
        for pa, pz, _, _ in ends:
            goal_mask = (_rect_distance(X, Y, pz) < 1e-9) & grid.free
            goals = {tuple(int(v) for v in g) for g in zip(*np.nonzero(goal_mask))} or {grid.cell(pz.x, pz.y)}
            pts = grid.path(grid.cell(pa.x, pa.y), goals | tree, search_cap=cfg.get("search_cap"))
            if not pts:
                lines = None
                break
            joined_tree = grid.cell(*pts[-1]) not in goals
            pts = [(pa.x, pa.y)] + pts[1:-1] + ([pts[-1]] if joined_tree else [(pz.x, pz.y)])
            lines.append(pts)
            for i in range(len(pts) - 1):
                n = max(1, int(math.dist(pts[i], pts[i + 1]) / grid.g))
                for k in range(n + 1):
                    t = k / n
                    tree.add(grid.cell(pts[i][0] + t * (pts[i + 1][0] - pts[i][0]), pts[i][1] + t * (pts[i + 1][1] - pts[i][1])))
        if lines is not None:
            break
    if lines is None:
        return {"net": net, "floor": round(floor, 3), "blocked": True, "paths": []}
    length = sum(math.dist(pts[i], pts[i + 1]) for pts in lines for i in range(len(pts) - 1))
    on_line = {id(p) for pa, pz, _, _ in ends for p in (pa, pz)}
    segs = [(pts[i], pts[i + 1]) for pts in lines for i in range(len(pts) - 1)]
    for p in own:
        if id(p) not in on_line:
            length += min(float(_seg_distance(p.x, p.y, *a, *b)) for a, b in segs)
    return {"net": net, "floor": round(floor, 3), "estimate": round(length, 3), "rows": len(rows),
            "paths": [[(round(x, 3), round(y, 3)) for x, y in pts] for pts in lines],
            "bends": sum(len(pts) - 2 for pts in lines)}


# ----------------------------------------------------------------------------------------------- the critique

def critique(board: Board, chains: list, width: float = 0.3, net_width: dict | None = None, *, max_over_pct: float = 5.0,
             parts: dict | None = None, route_layers: int = 3, cfg: dict | None = None) -> Critique:
    """Read a placed board. `chains` as load_chains gives them: (net, (ref, pin), (ref, pin)). `parts` (REF -> {value})
    names the limiters; `route_layers` is how many layers carry signal tracks, for RUDY's supply."""
    cfg = {**DEFAULTS, **(cfg or {})}
    weights = {**DEFAULTS["cost_weights"], **(cfg.get("cost_weights") or {})}
    out = Critique()
    ground = set(cfg["ground_nets"])
    rf_nets = {c[0] for c in chains}
    layer_pads = [p for p in board.pads if p.on(cfg["rf_layer"])]

    # 1. RF hops: the shortest path each can take, against its floor.
    rows_by_net = {}
    for net, a, z in chains:
        if (a, z) not in rows_by_net.setdefault(net, []):
            rows_by_net[net].append((a, z))
    excess_total = 0.0
    laid = []
    # Shortest first: a short hop has the least room to move, so it claims its line before the long ones go round it.
    order = sorted(rows_by_net, key=lambda n: board.floor(n, *rows_by_net[n][0]) if all(r in board.index for r in rows_by_net[n][0]) else 0)
    for net in order:
        rows = rows_by_net[net]
        w = (net_width or {}).get(net, width)
        hop = _net_paths(board, net, rows, w, cfg, layer_pads, laid)
        for pts in hop.get("paths") or []:
            laid += [(pts[i], pts[i + 1], w) for i in range(len(pts) - 1)]
        # The parts a fix would move or turn: every part with a pad on the hop. Named here so that no reader has to map net to parts.
        hop["parts"] = sorted({ref for pair in rows for ref, _pin in pair})
        out.hops.append(hop)
        if hop.get("missing"):
            out.findings.append((f"placement:rf:{net}:missing", f"{net}: pad {hop['missing']} not on the board", "error"))
            continue
        if hop.get("blocked"):
            out.findings.append((f"placement:rf:{net}:blocked",
                                 f"{net}: no path on {cfg['rf_layer']} between its pads, around the other pads and the shorter RF lines laid first "
                                 f"(parts {', '.join(hop['parts'])})", "error"))
            excess_total += 10 * hop["floor"]
            continue
        over = 100 * (hop["estimate"] / hop["floor"] - 1) if hop["floor"] > 0 else 0.0
        hop["over_pct"] = round(over, 1)
        excess = max(0.0, hop["estimate"] - hop["floor"] * (1 + max_over_pct / 100))
        excess_total += excess
        if over > max_over_pct:
            why = "no pad is in the way, so the pads are off the straight line" if hop["bends"] == 0 else \
                f"other pads are in the way: it bends {hop['bends']} time(s)"
            out.findings.append((f"placement:rf:{net}:over_floor",
                                 f"{net}: shortest path on {cfg['rf_layer']} is about {hop['estimate']:.2f} mm, {over:.0f}% over its "
                                 f"floor of {hop['floor']:.2f} mm ({why}; parts {', '.join(hop['parts'])})", "error"))

    # The RF lines as laid out above: what the other nets have to cross.
    rf_lines = []
    for hop in out.hops:
        for pts in hop.get("paths") or []:
            rf_lines += [(hop["net"], pts[i], pts[i + 1]) for i in range(len(pts) - 1)]

    # 2. Dedicated pins: escape order.
    by_net = {}
    for p in board.pads:
        if p.net:
            by_net.setdefault(p.net, []).append(p)
    by_ref = {}
    for p in board.pads:
        by_ref.setdefault(p.ref, []).append(p)
    escape_problems = 0
    for ref, pads in sorted(by_ref.items()):
        if len(pads) < cfg["min_ic_pads"]:
            continue
        escapes = []
        for pin in pads:
            if not pin.net or pin.net in ground or pin.net in rf_nets:
                continue
            members = by_net.get(pin.net, [])
            if len(members) != 2:
                continue
            other = members[0] if members[1] is pin else members[1]
            if other.ref == ref or len(by_ref.get(other.ref, [])) != 2:
                continue
            escapes.append((pin, other))
        half = cfg["escape_width_mm"] / 2 + cfg["clearance_mm"]
        cx, cy = sum(q.x for q in pads) / len(pads), sum(q.y for q in pads) / len(pads)

        def stub(pin, cx=cx, cy=cy, half=half):
            """Where an escape leaves its pin: straight out along the pad's long side, away from the part, past the pad's end."""
            a = math.radians(pin.ang)
            ux, uy = (math.cos(a), -math.sin(a)) if pin.hx >= pin.hy else (math.sin(a), math.cos(a))
            if ux * (pin.x - cx) + uy * (pin.y - cy) < 0:
                ux, uy = -ux, -uy
            reach = max(pin.hx, pin.hy) + half
            return (pin.x + ux * reach, pin.y + uy * reach)

        legs = {}
        for pin, sat in escapes:
            # From the stub's end straight to the part, sampled against the IC's own other pins.
            s = stub(pin)
            legs[id(pin)] = (s, (sat.x, sat.y))
            n = max(2, int(math.dist(s, (sat.x, sat.y)) / 0.05))
            ts = np.linspace(0, 1, n + 1)
            px, py = s[0] + ts * (sat.x - s[0]), s[1] + ts * (sat.y - s[1])
            hit = [q for q in pads if q is not pin and q.net != pin.net and bool(np.any(_rect_distance(px, py, q) < half))]
            if hit:
                escape_problems += 1
                names = ", ".join((q.num or "the exposed pad") + (f" ({q.net})" if q.net and q.num else "") for q in hit[:4])
                out.findings.append((f"placement:escape:{ref}.{pin.num}",
                                     f"{ref} pin {pin.num} ({pin.net}): the straight way to {sat.ref} crosses {ref} pin(s) {names}; "
                                     f"put {sat.ref} beside pin {pin.num}", "warning"))
        for i, (p1, s1) in enumerate(escapes):
            for p2, s2 in escapes[i + 1:]:
                (a1, b1), (a2, b2) = legs[id(p1)], legs[id(p2)]
                if _cross(a1, b1, a2, b2) or _cross((p1.x, p1.y), a1, a2, b2) or _cross((p2.x, p2.y), a2, a1, b1):
                    escape_problems += 1
                    out.findings.append((f"placement:escape:{ref}.{p1.num}x{p2.num}",
                                         f"{ref}: the escapes of pin {p1.num} ({p1.net}) to {s1.ref} and pin {p2.num} ({p2.net}) to {s2.ref} "
                                         f"cross, so {s1.ref} and {s2.ref} are out of pin order", "warning"))

    # 3. Other nets across the RF lines.
    limiters = [r for r, v in (parts or {}).items() if LIMITER.search(str((v or {}).get("value", "")) + " " + str((v or {}).get("mpn", "")))]
    limiter_pads = [p for r in limiters for p in by_ref.get(r, [])]
    crossings, near_limiter = {}, {}
    for net, members in by_net.items():
        if net in ground or net in rf_nets or len(members) < 2:
            continue
        for a, b in _mst_edges([(p.x, p.y) for p in members]):
            for rf_net, c, d in rf_lines:
                at = _cross(a, b, c, d)
                if not at:
                    continue
                crossings[net] = crossings.get(net, 0) + 1
                close = [p.ref for p in limiter_pads if float(_rect_distance(np.array(at[0]), np.array(at[1]), p)) < cfg["limiter_band_mm"]]
                if close:
                    near_limiter.setdefault((net, close[0]), (rf_net, at))
    for (net, ref), (rf_net, at) in sorted(near_limiter.items()):
        out.findings.append((f"placement:limiter:{ref}:{net}",
                             f"{net} has to cross {rf_net} beside limiter {ref}, near ({at[0]:.1f}, {at[1]:.1f}): its copper will run "
                             f"in the limiter's ground. Move {net}'s parts so it crosses away from {ref}", "warning"))

    # 4. RUDY.
    xs = [p.x for p in board.pads]
    ys = [p.y for p in board.pads]
    t = cfg["tile_mm"]
    x0, y0 = min(xs) - t, min(ys) - t
    nx, ny = int((max(xs) - x0) / t) + 2, int((max(ys) - y0) / t) + 2
    demand = np.zeros((nx, ny))
    contrib = {}
    for net, members in by_net.items():
        if net in ground or net in rf_nets or len(members) < 2:
            continue
        bx0, bx1 = min(p.x for p in members), max(p.x for p in members)
        by0, by1 = min(p.y for p in members), max(p.y for p in members)
        w, h = max(bx1 - bx0, t / 2), max(by1 - by0, t / 2)
        density = ((bx1 - bx0) + (by1 - by0)) / (w * h)       # mm of wire per mm2 over its box
        if density <= 0:
            continue
        cx0, cx1 = (bx0 + bx1 - w) / 2, (bx0 + bx1 + w) / 2
        cy0, cy1 = (by0 + by1 - h) / 2, (by0 + by1 + h) / 2
        for i in range(max(0, int((cx0 - x0) / t)), min(nx, int((cx1 - x0) / t) + 1)):
            ox = max(0.0, min(cx1, x0 + (i + 1) * t) - max(cx0, x0 + i * t))
            for j in range(max(0, int((cy0 - y0) / t)), min(ny, int((cy1 - y0) / t) + 1)):
                oy = max(0.0, min(cy1, y0 + (j + 1) * t) - max(cy0, y0 + j * t))
                if ox * oy > 0:
                    demand[i, j] += density * ox * oy / (t * t)
                    contrib.setdefault((i, j), []).append((density * ox * oy / (t * t), net))
    supply = route_layers / cfg["track_pitch_mm"]
    util = demand / supply
    hot = np.argwhere(util > cfg["max_utilisation"])
    overflow = float(np.sum(np.maximum(util - cfg["max_utilisation"], 0.0)))
    # Report the worst tiles, merged so one hot spot is one finding.
    reported = []
    for i, j in sorted(map(tuple, hot), key=lambda c: -util[c]):
        if any(abs(i - a) <= 2 and abs(j - b) <= 2 for a, b in reported):
            continue
        reported.append((i, j))
        top = ", ".join(n for _, n in sorted(contrib[(i, j)], reverse=True)[:4])
        out.findings.append((f"placement:congestion:{i}:{j}",
                             f"routing demand near ({x0 + (i + 0.5) * t:.1f}, {y0 + (j + 0.5) * t:.1f}) is {util[i, j]:.1f}x the "
                             f"{route_layers}-layer supply (RUDY estimate; most from {top})", "warning"))
        if len(reported) >= 10:
            break

    ok = [h for h in out.hops if h.get("estimate") is not None]
    out.metrics = {
        "rf_hops": len(out.hops),
        "rf_hops_over": sum(f[0].endswith(":over_floor") for f in out.findings),
        "rf_hops_blocked": sum(f[0].endswith(":blocked") for f in out.findings),
        "rf_parts_by_net": {h["net"]: h["parts"] for h in out.hops if any(f[0] in (f"placement:rf:{h['net']}:over_floor",
                                                                                     f"placement:rf:{h['net']}:blocked") for f in out.findings)},
        "rf_floor_mm": round(sum(h["floor"] for h in ok), 2),
        "rf_estimate_mm": round(sum(h["estimate"] for h in ok), 2),
        "rf_excess_mm": round(excess_total, 2),
        "escape_problems": escape_problems,
        "rf_crossings": sum(crossings.values()),
        "rf_crossings_by_net": dict(sorted(crossings.items(), key=lambda kv: -kv[1])[:12]),
        "limiter_crossings": len(near_limiter),
        "peak_utilisation": round(float(util.max()), 2) if util.size else 0.0,
        "overflow": round(overflow, 2),
        "estimate": "geometry only: shortest paths on a grid, straight escapes, spanning trees and RUDY; no router ran",
    }
    out.cost = round(weights["rf_excess_mm"] * excess_total + weights["escape"] * escape_problems +
                     weights["rf_crossing"] * sum(crossings.values()) + weights["limiter_crossing"] * len(near_limiter) +
                     weights["overflow"] * overflow, 3)
    out.metrics["cost"] = out.cost
    return out
