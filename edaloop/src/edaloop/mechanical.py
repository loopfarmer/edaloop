"""A test mount and a case for a board, as OpenSCAD models generated from the board itself.

Everything comes from the file: outline, mounting holes, where parts sit, which connectors reach an edge, and which pads a probe can touch
from below. Part heights are the exception, as a board file doesn't hold them. They're rough figures by part type unless the project gives
them under `mechanical.part_height_mm`, and the result lists which it assumed.
"""
from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path

from . import ground, toolchain

MOUNTING_DRILL_MM = 2.0
TYPE_HEIGHT = {"ic": 1.2, "conn": 3.5, "R": 0.6, "C": 1.0, "L": 1.5, "other": 2.0}
PIN_BELOW_MM = 2.5           # a through-hole pin past the board's underside, trimmed
EDGE_REACH_MM = 2.5          # a connector this near an edge opens through that wall
PROBE_DIAMETER_MM = 1.7      # a common pogo pin's barrel, with room to slide


def part_kind(ref: str) -> str:
    letters = re.sub(r"[^A-Z]", "", ref.upper())
    if letters[:2] == "JP" or letters[:1] == "J" or letters[:1] == "X" or letters[:4] == "CONN":
        return "conn"
    if letters[:1] in ("U", "Q") or letters[:2] in ("IC", "FL"):
        return "ic"
    return letters[0] if letters[:1] in ("R", "C", "L") else "other"


def part_boxes(facts: dict, heights: dict | None = None) -> list[dict]:
    """One box per part, turned with its footprint and sized from its pads."""
    yards = {y["ref"]: y for y in facts["courtyards"]}
    by: dict[str, list[dict]] = {}
    for pad in facts["pads"]:
        by.setdefault(pad["ref"], []).append(pad)
    out = []
    for ref, pads in by.items():
        # A part that's only a mounting hole or a fiducial has no body to clear.
        if all(not p["net"] for p in pads) and (any(p.get("drill", 0) >= MOUNTING_DRILL_MM for p in pads) or len(pads) == 1):
            continue
        yard = yards.get(ref, {})
        t = math.radians(float(yard.get("rot", 0.0)))
        c, s = math.cos(t), math.sin(t)
        cx0, cy0 = sum(p["x"] for p in pads) / len(pads), sum(p["y"] for p in pads) / len(pads)
        us, vs = [], []
        for p in pads:
            dx, dy = p["x"] - cx0, p["y"] - cy0
            a = math.radians(float(p.get("rot", 0.0)) - float(yard.get("rot", 0.0)))
            hw = abs(p["w"] * math.cos(a)) / 2 + abs(p["h"] * math.sin(a)) / 2
            hh = abs(p["w"] * math.sin(a)) / 2 + abs(p["h"] * math.cos(a)) / 2
            u, v = dx * c - dy * s, dx * s + dy * c
            us += [u - hw, u + hw]
            vs += [v - hh, v + hh]
        u0, u1, v0, v1 = min(us), max(us), min(vs), max(vs)
        kind = part_kind(ref)
        given = (heights or {}).get(ref)
        out.append({"ref": ref, "kind": kind, "x": cx0 + (u0 + u1) / 2 * c + (v0 + v1) / 2 * s, "y": cy0 - (u0 + u1) / 2 * s + (v0 + v1) / 2 * c,
                    "w": max(u1 - u0, 0.2), "h": max(v1 - v0, 0.2), "rot": float(yard.get("rot", 0.0)), "back": bool(yard.get("back")),
                    "tall": float(given if given is not None else TYPE_HEIGHT[kind]), "height_given": given is not None, "value": yard.get("value", "")})
    return out


def holes(facts: dict) -> list[dict]:
    return [{"ref": p["ref"], "x": p["x"], "y": p["y"], "d": p["drill"]} for p in facts["pads"] if p.get("drill", 0) >= MOUNTING_DRILL_MM and p["net"] in ("", "GND")]


def _extent(part: dict) -> tuple[float, float]:
    t = math.radians(part["rot"])
    return (abs(part["w"] * math.cos(t)) + abs(part["h"] * math.sin(t))) / 2, (abs(part["w"] * math.sin(t)) + abs(part["h"] * math.cos(t))) / 2


def edge_openings(parts: list[dict], outline: list[float], reach: float = EDGE_REACH_MM) -> list[dict]:
    """Connectors that reach an edge, and the wall each needs an opening in. In board coordinates."""
    x1, y1, x2, y2 = outline
    out = []
    for part in parts:
        if part["kind"] != "conn" or part["back"]:
            continue
        ex, ey = _extent(part)
        gaps = {"left": part["x"] - ex - x1, "right": x2 - part["x"] - ex, "top": part["y"] - ey - y1, "bottom": y2 - part["y"] - ey}
        side, gap = min(gaps.items(), key=lambda kv: kv[1])
        if gap <= reach:
            along = part["y"] if side in ("left", "right") else part["x"]
            width = 2 * (ey if side in ("left", "right") else ex) + 1.0
            out.append({"ref": part["ref"], "side": side, "at": along, "width": width, "tall": part["tall"] + 0.5})
    return out


def lid_openings(parts: list[dict], edges: list[dict]) -> list[dict]:
    """Connectors inside the board that a cable reaches from above."""
    at_edge = {e["ref"] for e in edges}
    return [{"ref": p["ref"], "x": p["x"], "y": p["y"], "d": max(_extent(p)) * 2 + 1.5} for p in parts if p["kind"] == "conn" and not p["back"] and p["ref"] not in at_edge]


def probes(facts: dict, rails: list[dict]) -> tuple[list[dict], list[str]]:
    """A pogo pin for each rail's ends and for ground: from below where a pad is drilled or underneath, and otherwise down through the clamp."""
    wanted = list(dict.fromkeys(name for rail in rails for name in (rail["source"], *rail["loads"])))
    named = {f"{p['ref']}.{p['pad']}": p for p in facts["pads"]}
    found, missing = [], []
    for name in wanted:
        pad = named.get(name)
        if not pad:
            missing.append(name)
            continue
        found.append({"pad": name, "net": pad["net"], "x": pad["x"], "y": pad["y"], "from": "below" if pad.get("back") or pad.get("drill", 0) > 0 else "above"})
    # A ground return, so a probe card has something to measure against.
    grounds = [p for p in facts["pads"] if p["net"] == "GND" and p.get("drill", 0) < MOUNTING_DRILL_MM]
    ground_pad = next((p for p in grounds if p.get("back") or p.get("drill", 0) > 0), grounds[0] if grounds else None)
    if ground_pad and found:
        found.append({"pad": f"{ground_pad['ref']}.{ground_pad['pad']}", "net": "GND", "x": ground_pad["x"], "y": ground_pad["y"], "from": "below" if ground_pad.get("back") or ground_pad.get("drill", 0) > 0 else "above"})
    return found, missing


def press_points(parts: list[dict], outline: list[float], inset: float = 2.0, radius: float = 1.25) -> list[list[float]]:
    """Where a lid or clamp can press on a board with no mounting holes: as near each corner as the top side has clear space."""
    x1, y1, x2, y2 = outline
    tops = [(p, *_extent(p)) for p in parts if not p["back"]]
    out = []
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        best = None
        for step in range(0, 25):
            for a in range(0, step + 1):
                x = (x1 + inset + a) if sx > 0 else (x2 - inset - a)
                y = (y1 + inset + step - a) if sy > 0 else (y2 - inset - step + a)
                if all(abs(p["x"] - x) > ex + radius + 0.3 or abs(p["y"] - y) > ey + radius + 0.3 for p, ex, ey in tops):
                    best = [x, y]
                    break
            if best:
                break
        if best:
            out.append(best)
    return out


def closed_solid(stl: Path) -> tuple[bool, int]:
    """Whether a mesh is watertight, read from the file: every edge is shared by exactly two faces. Returns the answer and the facet count."""
    data = stl.read_bytes()
    faces: list[tuple] = []
    if data[:5] == b"solid" and b"facet" in data[:400]:
        vertices = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", data)
        for i in range(0, len(vertices) - 2, 3):
            faces.append((vertices[i], vertices[i + 1], vertices[i + 2]))
    else:
        count = struct.unpack("<I", data[80:84])[0]
        for i in range(count):
            off = 84 + i * 50 + 12
            faces.append(tuple(data[off + k * 12:off + k * 12 + 12] for k in range(3)))
    edges: dict[tuple, int] = {}
    for a, b, c in faces:
        for u, v in ((a, b), (b, c), (c, a)):
            if u == v:
                continue
            key = (u, v) if u < v else (v, u)
            edges[key] = edges.get(key, 0) + 1
    return bool(faces) and all(n % 2 == 0 for n in edges.values()), len(faces)


def check_fit(parts: list[dict], mounts: list[dict], outline: list[float], post_margin: float = 1.5) -> list[str]:
    """What won't fit, in terms a person can act on."""
    problems = []
    x1, y1, x2, y2 = outline
    for hole in mounts:
        radius = hole["d"] / 2 + post_margin
        if not (x1 + radius / 2 <= hole["x"] <= x2 - radius / 2 and y1 + radius / 2 <= hole["y"] <= y2 - radius / 2):
            problems.append(f"{hole['ref']}: its mounting post runs off the board")
    for part in parts:
        if part["back"]:
            ex, ey = _extent(part)
            for hole in mounts:
                if abs(part["x"] - hole["x"]) < ex + hole["d"] / 2 + post_margin and abs(part["y"] - hole["y"]) < ey + hole["d"] / 2 + post_margin:
                    problems.append(f"{part['ref']} on the underside is under the post at {hole['ref']}")
    return problems


def scad_text(name: str, outline: list[float], thickness: float, parts: list[dict], mounts: list[dict], edges: list[dict], lids: list[dict], pins: list[dict], has_through_pins: bool) -> str:
    x1, y1, x2, y2 = outline
    w, h = x2 - x1, y2 - y1

    def px(v):
        return round(v - x1, 3)

    def py(v):
        return round(y2 - v, 3)     # board y runs down, and OpenSCAD's runs up
    above = max((p["tall"] for p in parts if not p["back"]), default=1.0)
    below = max([p["tall"] for p in parts if p["back"]] + ([PIN_BELOW_MM] if has_through_pins else []) + [1.0])
    tops = [[px(p["x"]), py(p["y"]), round(p["w"], 3), round(p["h"], 3), p["rot"], p["tall"], p["ref"]] for p in parts if not p["back"]]
    backs = [[px(p["x"]), py(p["y"]), round(p["w"], 3), round(p["h"], 3), p["rot"], p["tall"], p["ref"]] for p in parts if p["back"]]
    hole_rows = [[px(m["x"]), py(m["y"]), round(m["d"], 3)] for m in mounts]
    # In OpenSCAD's frame a board-top edge is the far wall.
    walls = {"left": ("left", lambda v: py(v)), "right": ("right", lambda v: py(v)), "top": ("far", lambda v: px(v)), "bottom": ("near", lambda v: px(v))}
    edge_rows = [[walls[e["side"]][0], walls[e["side"]][1](e["at"]), round(e["width"], 3), round(e["tall"], 3), e["ref"]] for e in edges]
    lid_rows = [[px(o["x"]), py(o["y"]), round(o["d"], 3), o["ref"]] for o in lids]
    pin_rows = [[px(p["x"]), py(p["y"]), p["pad"], p["net"]] for p in pins if p["from"] == "below"]
    pin_top_rows = [[px(p["x"]), py(p["y"]), p["pad"], p["net"]] for p in pins if p["from"] == "above"]
    press_rows = [[px(q[0]), py(q[1])] for q in press_points(parts, outline)] if not mounts else []
    data = json.dumps
    return f"""// Test mount and case for {name}. Generated by edaloop from the board file. Regenerate rather than edit the lists below.
// Render one with: openscad -D 'part="case_bottom"' -o case_bottom.stl {name}_mechanical.scad
part = "assembly"; // [assembly, test_mount, test_clamp, case_bottom, case_top]

/* [Fit] */
fit = 0.25;        // gap round the board
wall = 2;          // wall thickness
floor_t = 2;       // floor and lid thickness
ledge = 1.2;       // how far a board edge rests on its support
ear = 8;           // corner ear that holds a closing screw
ear_screw = 2.6;   // pilot for an M3 thread-forming screw
ear_clear = 3.4;   // clearance for that screw

/* [From the board, in mm] */
board = [{round(w, 3)}, {round(h, 3)}];
board_t = {thickness:g};
above = {above:g};      // tallest part on top
below = {below:g};      // tallest part or pin below
holes = {data(hole_rows)};
top_parts = {data(tops)};
back_parts = {data(backs)};
edge_cutouts = {data(edge_rows)};
lid_cutouts = {data(lid_rows)};
probes = {data(pin_rows)};       // from below, through the mount's floor
probes_top = {data(pin_top_rows)};   // from above, through the clamp
press = {data(press_rows)};       // where the clamp and lid press on a board with no holes

$fn = 40;
eps = 0.01;
top_gap = above + 1;      // room over the tallest part
outer = [board[0] + 2 * (fit + wall), board[1] + 2 * (fit + wall)];
board_z = floor_t + below;
case_h = board_z + board_t + top_gap;

// Board coordinates in the outer shell's frame.
module on_board() {{ translate([wall + fit, wall + fit, 0]) children(); }}

// Corner ears, outside the walls, for the screws that close a case or a clamp.
function ear_points() = [[-ear / 2, -ear / 2 + 4], [outer[0] + ear / 2, -ear / 2 + 4], [-ear / 2, outer[1] + ear / 2 - 4], [outer[0] + ear / 2, outer[1] + ear / 2 - 4]];
module ears(height) {{ for (e = ear_points()) translate([e[0] - ear / 2, e[1] - ear / 2, 0]) cube([ear + eps, ear, height]); }}
module ear_holes(d, z0, height) {{ for (e = ear_points()) translate([e[0], e[1], z0]) cylinder(d = d, h = height); }}

module board_model() {{
  color("seagreen") translate([0, 0, 0]) cube([board[0], board[1], board_t]);
  for (p = top_parts) translate([p[0], p[1], board_t]) rotate(p[4]) color("dimgray") translate([-p[2] / 2, -p[3] / 2, 0]) cube([p[2], p[3], p[5]]);
  for (p = back_parts) translate([p[0], p[1], -p[5]]) rotate(p[4]) color("dimgray") translate([-p[2] / 2, -p[3] / 2, 0]) cube([p[2], p[3], p[5]]);
}}

module tray(height) {{
  difference() {{
    cube([outer[0], outer[1], height]);
    translate([wall, wall, floor_t]) cube([board[0] + 2 * fit, board[1] + 2 * fit, height]);
  }}
}}

// A rim to rest the board's edge on, wherever no post carries it.
module rest() {{
  on_board() difference() {{
    translate([-fit, -fit, 0]) cube([board[0] + 2 * fit, board[1] + 2 * fit, board_z]);
    translate([ledge, ledge, -eps]) cube([board[0] - 2 * ledge, board[1] - 2 * ledge, board_z + 2 * eps]);
  }}
}}

module edge_openings(z0) {{
  for (e = edge_cutouts) {{
    if (e[0] == "left") translate([-eps, wall + fit + e[1] - e[2] / 2, z0]) cube([wall + 2 * eps + fit, e[2], case_h]);
    if (e[0] == "right") translate([outer[0] - wall - fit - eps, wall + fit + e[1] - e[2] / 2, z0]) cube([wall + 2 * eps + fit, e[2], case_h]);
    if (e[0] == "near") translate([wall + fit + e[1] - e[2] / 2, -eps, z0]) cube([e[2], wall + 2 * eps + fit, case_h]);
    if (e[0] == "far") translate([wall + fit + e[1] - e[2] / 2, outer[1] - wall - fit - eps, z0]) cube([e[2], wall + 2 * eps + fit, case_h]);
  }}
}}

// Posts up through each mounting hole. A locating pin on the mount, and a pilot hole in the case.
module posts(pin) {{
  on_board() for (m = holes) translate([m[0], m[1], 0]) {{
    cylinder(d = m[2] + 3, h = board_z);
    if (pin) cylinder(d = m[2] - 0.3, h = board_z + board_t + 1);
  }}
}}
module pilots() {{
  on_board() for (m = holes) translate([m[0], m[1], floor_t]) cylinder(d = max(m[2] - 0.8, 1), h = board_z);
}}

module test_mount() {{
  difference() {{
    union() {{
      tray(board_z + board_t);
      rest();
      posts(true);
      ears(board_z + board_t);
    }}
    ear_holes(ear_screw, floor_t, board_z + board_t);
    // Pogo pins from below, through the floor.
    on_board() for (p = probes) translate([p[0], p[1], -eps]) cylinder(d = {PROBE_DIAMETER_MM}, h = floor_t + 2 * eps);
    on_board() for (p = probes) translate([p[0] + 1.2, p[1] - 0.6, floor_t - 0.3]) linear_extrude(0.31) text(p[3], size = 1.2);
    edge_openings(board_z);
  }}
}}

// Holds the board down on the mount, clear of its parts, and carries pogo pins for the pads on top.
module test_clamp() {{
  gap = top_gap;
  difference() {{
    union() {{
      cube([outer[0], outer[1], floor_t]);
      ears(floor_t + gap);
      // A skirt on the walls, down to the mount.
      translate([0, 0, floor_t - eps]) difference() {{
        cube([outer[0], outer[1], gap]);
        translate([wall, wall, -eps]) cube([board[0] + 2 * fit, board[1] + 2 * fit, gap + 2 * eps]);
      }}
      for (q = press) on_board() translate([board[0] - q[0], q[1], floor_t - eps]) cylinder(d = 2.5, h = gap);
      for (m = holes) on_board() translate([board[0] - m[0], m[1], floor_t - eps]) difference() {{ cylinder(d = m[2] + 3, h = gap); translate([0, 0, gap - 1.5]) cylinder(d = m[2] + 0.3, h = 2); }}
    }}
    ear_holes(ear_clear, -eps, floor_t + gap + 2 * eps);
    on_board() for (p = probes_top) translate([board[0] - p[0], p[1], -eps]) cylinder(d = {PROBE_DIAMETER_MM}, h = floor_t + 2 * eps);
    on_board() for (p = probes_top) translate([board[0] - p[0] - 1.2, p[1] + 1.2, -eps]) mirror([1, 0, 0]) linear_extrude(0.31) text(p[3], size = 1.2);
    on_board() for (o = lid_cutouts) translate([board[0] - o[0], o[1], -eps]) cylinder(d = o[2], h = floor_t + 2 * eps);
    // The clamp is turned over to go on, so an edge connector's opening is on its mirror side.
    translate([outer[0], 0, 0]) mirror([1, 0, 0]) edge_openings(floor_t);
  }}
}}

module case_bottom() {{
  difference() {{
    union() {{
      tray(case_h - floor_t);
      rest();
      posts(false);
      ears(case_h - floor_t);
    }}
    pilots();
    ear_holes(ear_screw, floor_t, case_h);
    edge_openings(board_z);
  }}
}}

module case_top() {{
  difference() {{
    union() {{
      cube([outer[0], outer[1], floor_t]);
      ears(floor_t);
      // A lip inside the walls, to locate the lid.
      difference() {{
        translate([wall + fit / 2, wall + fit / 2, floor_t]) cube([board[0] + fit, board[1] + fit, 2]);
        translate([wall + fit / 2 + 1.2, wall + fit / 2 + 1.2, floor_t - eps]) cube([board[0] + fit - 2.4, board[1] + fit - 2.4, 2 + 2 * eps]);
      }}
      // Pegs that hold down a board with no mounting holes.
      for (q = press) on_board() translate([board[0] - q[0], q[1], floor_t - eps]) cylinder(d = 2.5, h = top_gap - 0.1);
    }}
    ear_holes(ear_clear, -eps, floor_t + 2 * eps);
    on_board() for (m = holes) translate([board[0] - m[0], m[1], -eps]) cylinder(d = m[2] + 0.3, h = floor_t + 2 * eps);
    on_board() for (o = lid_cutouts) translate([board[0] - o[0], o[1], -eps]) cylinder(d = o[2], h = floor_t + 2 * eps);
  }}
}}

module assembly() {{
  case_bottom();
  on_board() translate([0, 0, board_z]) board_model();
  // The lid is modelled flat side down for printing, and turned over to sit on the case. Lifted here so the inside shows.
  translate([outer[0], 0, case_h + 15]) rotate([0, 180, 0]) case_top();
}}

if (part == "test_mount") test_mount();
if (part == "test_clamp") test_clamp();
if (part == "case_bottom") case_bottom();
if (part == "case_top") case_top();
if (part == "assembly") assembly();
"""


def build(p, output: Path | None = None, render: bool = True) -> dict:
    out = Path(output) if output else p.root / "mechanical"
    out.mkdir(parents=True, exist_ok=True)
    cfg = p.section("mechanical")
    facts = ground._facts(p.board, p.work)
    from .pcbfile import Board
    board = Board(p.board)
    from .thermal import _stack
    layers, gaps = _stack(board)
    thickness = round(sum(gaps) + sum(x["thickness_um"] for x in layers) / 1000, 3)
    parts = part_boxes(facts, cfg.get("part_height_mm"))
    mounts = holes(facts)
    outline = facts["outline"]
    edges = edge_openings(parts, outline, float(cfg.get("edge_reach_mm", EDGE_REACH_MM)))
    lids = lid_openings(parts, edges)
    pins, not_found = probes(facts, p.section("power").get("rails") or [])
    through = any(0 < x.get("drill", 0) < MOUNTING_DRILL_MM for x in facts["pads"])
    text = scad_text(p.name, outline, thickness, parts, mounts, edges, lids, pins, through)
    scad = out / f"{p.name}_mechanical.scad"
    scad.write_text(text)
    assumed = sorted(x["ref"] for x in parts if not x["height_given"])
    problems = check_fit(parts, mounts, outline)
    result = {"scad": str(scad), "board_mm": [round(outline[2] - outline[0], 2), round(outline[3] - outline[1], 2)], "thickness_mm": thickness, "mounting_holes": len(mounts),
              "edge_openings": [e["ref"] for e in edges], "lid_openings": [o["ref"] for o in lids], "probes_below": [x["pad"] for x in pins if x["from"] == "below"], "probes_above": [x["pad"] for x in pins if x["from"] == "above"], "probes_not_found": not_found,
              "heights_assumed": len(assumed), "assumed_refs": assumed[:20], "problems": problems, "rendered": {}}
    if not mounts:
        result["note"] = "no mounting holes, so the board rests on a rim, and the clamp and lid hold it down with pegs near its corners"
    tool = toolchain.paths().get("openscad", "")
    if render and tool and Path(tool).exists():
        for part in ("test_mount", "test_clamp", "case_bottom", "case_top"):
            stl = out / f"{p.name}_{part}.stl"
            r = toolchain.run([tool, "-D", f'part="{part}"', "-o", stl, scad], timeout=900)
            log = r.stdout + r.stderr
            made = r.returncode == 0 and stl.exists() and stl.stat().st_size > 0
            closed, facets = closed_solid(stl) if made else (False, 0)
            result["rendered"][part] = {"stl": str(stl), "made": made, "closed_solid": closed, "facets": facets, "warnings": [ln[:160] for ln in log.splitlines() if "WARNING" in ln or "ERROR" in ln][:4]}
            if not result["rendered"][part]["made"]:
                problems.append(f"OpenSCAD couldn't render {part}: {log.strip()[-200:]}")
            elif not closed:
                problems.append(f"{part} isn't a closed solid, so it may not slice")
    elif render:
        result["note_render"] = "OpenSCAD isn't installed where edaloop looks, so only the .scad was written"
    return result
