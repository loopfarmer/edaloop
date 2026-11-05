"""Board current and temperature fields: from a .kicad_pcb to boardfield and back.

`power` screens each rail with a resistor network and IPC-2221. It can't see pours, current crowding or
neighbouring copper. This rasterises every layer of the board and solves the fields directly. The board is never written: zones are filled on a copy, since a stored fill can be stale.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import toolchain
from .pcbfile import Board, _child, _val

OPS = Path(__file__).with_name("kicad_ops.py")


def _edge_bbox(board: Board) -> list[float]:
    xs, ys = [], []
    for node in board.tree:
        if isinstance(node, list) and node and node[0] in ("gr_line", "gr_rect", "gr_arc", "gr_poly", "gr_circle"):
            if _val(node, "layer") != "Edge.Cuts":
                continue
            for key in ("start", "end", "mid", "center"):
                c = _child(node, key)
                if c:
                    xs.append(float(c[1]))
                    ys.append(float(c[2]))
            pts = _child(node, "pts")
            for xy in (x for x in (pts or []) if isinstance(x, list) and x[0] == "xy"):
                xs.append(float(xy[1]))
                ys.append(float(xy[2]))
    if not xs:
        for t in board.tracks:
            xs += [t.start[0], t.end[0]]
            ys += [t.start[1], t.end[1]]
        for p in board.all_pads():
            xs.append(p["x"])
            ys.append(p["y"])
    if not xs:
        raise ValueError("the board has no outline and no copper")
    return [min(xs), min(ys), max(xs), max(ys)]


def _stack(board: Board) -> tuple[list[dict], list[float]]:
    """Copper layers top to bottom, and the laminate between each pair, from the board's stackup."""
    setup = _child(board.tree, "setup")
    stackup = _child(setup, "stackup") if setup else None
    layers, gaps, gap = [], [], 0.0
    for item in (x for x in (stackup or []) if isinstance(x, list) and x[0] == "layer"):
        kind = str(_val(item, "type") or "")
        thick = float(_val(item, "thickness") or 0.0)
        if kind == "copper":
            if layers:
                gaps.append(gap)
            gap = 0.0
            layers.append({"name": item[1], "thickness_um": thick * 1000})
        elif layers and thick > 0:
            gap += thick
    if not layers:
        layers = [{"name": "F.Cu", "thickness_um": 35.0}, {"name": "B.Cu", "thickness_um": 35.0}]
        gaps = [1.5]
    return layers, gaps


def _drills(board: Board) -> dict[str, float]:
    out = {}
    for fp in board.tree:
        if not (isinstance(fp, list) and fp and fp[0] == "footprint"):
            continue
        props = [x for x in fp if isinstance(x, list) and x[0] == "property" and len(x) > 2 and x[1] == "Reference"]
        ref = props[0][2] if props else "?"
        for pad in (x for x in fp if isinstance(x, list) and x[0] == "pad"):
            drill = _child(pad, "drill")
            if drill:
                sizes = [v for v in drill[1:] if not isinstance(v, list) and v != "oval"]
                if sizes:
                    out[f"{ref}.{pad[1]}"] = float(sizes[0])
    return out


def _zones(board: Board, index: dict[str, int]) -> list[dict]:
    numbered = {x[1]: x[2] for x in board.tree if isinstance(x, list) and x and x[0] == "net" and len(x) > 2}
    polys = []
    for node in board.tree:
        if not (isinstance(node, list) and node and node[0] == "zone"):
            continue
        net = _val(node, "net_name") or numbered.get(_val(node, "net", ""), _val(node, "net", "")) or ""
        for fill in (x for x in node if isinstance(x, list) and x and x[0] == "filled_polygon"):
            layer = _val(fill, "layer")
            pts = _child(fill, "pts")
            if layer not in index or not pts:
                continue
            xy = [[float(p[1]), float(p[2])] for p in pts if isinstance(p, list) and p[0] == "xy"]
            if len(xy) >= 3:
                polys.append({"layer": index[layer], "net": net, "pts": xy})
    return polys


SINKS = ("U", "IC", "Q", "A")      # parts that use a rail's current, rather than pass it on


def _ref(pad_name: str) -> str:
    return pad_name.rsplit(".", 1)[0]


def _sink_of(load: str, pads: list[dict], rail_net: str) -> str:
    """The active part a load's current ends in, followed through series resistors and chokes. The load's own part if it reaches none."""
    on_net: dict[str, list[str]] = {}
    nets_of: dict[str, list[tuple[str, str]]] = {}
    for p in pads:
        if p["net"]:
            on_net.setdefault(p["net"], []).append(p["name"])
            nets_of.setdefault(_ref(p["name"]), []).append((p["name"], p["net"]))
    start = _ref(load)
    if start.startswith(SINKS):
        return start
    seen, frontier = {start}, [(start, load)]
    while frontier:
        following = []
        for ref, entry in frontier:
            for name, net in nets_of.get(ref, []):
                if name == entry or net == rail_net or net.upper() in ("GND", "AGND", "DGND", "PGND"):
                    continue
                for other in on_net.get(net, []):
                    who = _ref(other)
                    if who in seen:
                        continue
                    if who.startswith(SINKS):
                        return who
                    if who[0] in "RL" and len(nets_of.get(who, [])) == 2:
                        seen.add(who)
                        following.append((who, other))
        frontier = following
    return start


def part_heat(pads: list[dict], rails: list[dict], supply_v: float, overrides: dict | None = None) -> dict[str, dict]:
    """The DC power each part takes from the stated load currents, all of it counted as heat, which errs on the hot side.

    Currents add within a rail. The same current can be listed on two rails in series, such as a supply and the bias net it feeds, so a
    part takes the largest across rails rather than the sum. `overrides` gives a part's watts directly, and 0 leaves it out.
    """
    per_rail: list[dict[str, float]] = []
    for rail in rails:
        here: dict[str, float] = {}
        for load, amps in rail["loads"].items():
            sink = _sink_of(load, pads, rail["net"])
            here[sink] = here.get(sink, 0.0) + float(amps)
        per_rail.append(here)
    out = {}
    for ref in {r for here in per_rail for r in here}:
        amps = max(here.get(ref, 0.0) for here in per_rail)
        out[ref] = {"amps": round(amps, 6), "watts": round(amps * supply_v, 5), "source": "stated current"}
    for ref, watts in (overrides or {}).items():
        out[ref] = {"amps": None, "watts": float(watts), "source": "given"}
    return {r: v for r, v in out.items() if v["watts"] > 0}


def still_air_h(watts: float, area_m2: float, ambient_c: float = 25.0, emissivity: float = 0.9) -> dict:
    """Natural convection plus radiation for a board in still air, at the rise its own heat would drive. Both sides, in W/m2K.

    One fixed coefficient can't serve every board: convection is stronger on a small plate, and radiation grows with temperature. A figure
    of 10 put a 168 mm2 buck rated for 2 A continuous well past any silicon limit. Conduction out through connectors and wires isn't
    included, so a small board still reads somewhat hot.
    """
    side = area_m2 ** 0.5
    rise = 20.0
    h = 10.0
    for _ in range(40):
        kelvin, ambient = ambient_c + rise + 273.15, ambient_c + 273.15
        convection = 1.42 * (max(rise, 0.1) / side) ** 0.25         # laminar natural convection on a vertical plate
        radiation = emissivity * 5.67e-8 * (kelvin ** 2 + ambient ** 2) * (kelvin + ambient)
        h = convection + radiation
        rise = 0.5 * rise + 0.5 * watts / (2 * area_m2 * h)
    return {"h_w_m2k": round(h, 2), "convection": round(convection, 2), "radiation": round(radiation, 2), "average_rise_c": round(rise, 1)}


def heated_parts(pads: list[dict], heat: dict[str, dict]) -> list[dict]:
    """Each heated part as boardfield takes it: a box over its pads."""
    parts = []
    for ref, h in sorted(heat.items()):
        own = [p for p in pads if _ref(p["name"]) == ref]
        if not own:
            raise ValueError(f"thermal part {ref} isn't on the board")
        x1, x2 = min(p["x"] - p["hw"] for p in own), max(p["x"] + p["hw"] for p in own)
        y1, y2 = min(p["y"] - p["hh"] for p in own), max(p["y"] + p["hh"] for p in own)
        parts.append({"name": ref, "layer": min(own[0]["layers"]), "x": (x1 + x2) / 2, "y": (y1 + y2) / 2, "hw": (x2 - x1) / 2, "hh": (y2 - y1) / 2, "watts": h["watts"]})
    return parts


def export_problem(board_path: Path, rails: list[dict], parts: list[dict] | None = None, settings: dict | None = None) -> dict:
    """The boardfield problem for a board file, as it stands. Pass a filled copy to include pours."""
    board = Board(board_path)
    layers, gaps = _stack(board)
    index = {x["name"]: i for i, x in enumerate(layers)}
    tracks = [{"layer": index[t.layer], "net": t.net, "a": list(t.start), "b": list(t.end), "w": t.width}
              for t in board.tracks if t.kind != "via" and t.layer in index]
    vias = [{"net": t.net, "x": t.start[0], "y": t.start[1], "size": t.width, "drill": t.drill or 0.2}
            for t in board.tracks if t.kind == "via"]
    drills = _drills(board)
    pads = []
    for p in board.all_pads():
        on = list(range(len(layers))) if "*.Cu" in p["layers"] else [index[x] for x in p["layers"] if x in index]
        if on:
            pads.append({"name": p["name"], "net": p["net"], "layers": on, "x": p["x"], "y": p["y"],
                         "hw": p["hw"], "hh": p["hh"], "drill": drills.get(p["name"], 0.0)})
    return {"bbox": _edge_bbox(board), "layers": layers, "dielectrics_mm": gaps, "tracks": tracks, "vias": vias,
            "pads": pads, "polys": _zones(board, index), "rails": rails, "parts": parts or [],
            "settings": {"dx_mm": 0.1, **(settings or {})}}


def fill_copy(board_path: Path, out: Path) -> Path:
    """A copy with zones filled now, so that pours match the copper. The original isn't touched."""
    py = toolchain.paths()["kicad_python"]
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if board_path.with_suffix(ext).exists():
            shutil.copy(board_path.with_suffix(ext), out.with_suffix(ext))
    r = toolchain.run([py, OPS, "fill_zones", board_path, out], timeout=900)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"zone fill failed: {(r.stdout + r.stderr).strip()[-300:]}")
    return out


def run_field(problem: dict, work: Path, backend: str = "cpu", png: Path | None = None, timeout: int = 3600) -> dict:
    """Run boardfield. Returns its result. Raises if it can't run, so a crash can't read as a cool board."""
    binary = Path(toolchain.paths()["boardfield"])
    if not binary.exists():
        raise RuntimeError(f"boardfield isn't built at {binary}")
    work.mkdir(parents=True, exist_ok=True)
    src, out = work / "field_problem.json", work / "field_result.json"
    src.write_text(json.dumps(problem))
    out.unlink(missing_ok=True)
    cmd = [binary, src, "--backend", backend, "-o", out]
    if png:
        cmd += ["--png", png]
    r = toolchain.run(cmd, timeout=timeout)
    if r.returncode not in (0, 1) or not out.exists():
        raise RuntimeError(f"boardfield failed with exit {r.returncode}: {r.stderr.strip()[-300:]}")
    return json.loads(out.read_text())[-1]


def _ramp(t: float) -> str:
    """boardfield's dark-to-hot ramp, so the scale matches the picture."""
    r, g, b = min(t * 2.2, 1.0), min(max((t - 0.4) * 2.0, 0.0), 1.0), min(max((t - 0.75) * 4.0, 0.0), 1.0)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(20 + b * 235):02x}"


def heat_svg(png: Path, result: dict, bbox: list[float], board_path: Path, out: Path, title: str) -> Path:
    """The heat map with what makes it readable: the board edge, each part and its reference, the hotspot, and a scale in C.

    The colours are boardfield's own picture. The scale runs from the coolest cell to the hottest, as the picture does,
    so it reads only when boardfield reported that range.
    """
    import base64
    import html
    lo, hi = result.get("png_rise_range_c") or (None, None)
    ambient = float(result.get("hotspot", {}).get("temp_c", 0) - result.get("hotspot", {}).get("rise_c", 0))
    x1, y1, x2, y2 = bbox
    scale = 640 / max(x2 - x1, 1e-6)                     # px per mm
    w, h = (x2 - x1) * scale, (y2 - y1) * scale
    pad, bar = 40, 90
    width, height = w + 2 * pad + bar, h + 2 * pad + 30
    def at(x, y):
        return pad + (x - x1) * scale, pad + 30 + (y - y1) * scale
    image = base64.b64encode(Path(png).read_bytes()).decode()
    parts: dict[str, list] = {}
    for p in Board(board_path).all_pads():
        parts.setdefault(p["name"].split(".")[0], []).append(p)
    body = [f'<rect width="{width:.0f}" height="{height:.0f}" fill="#ffffff"/>',
            f'<text x="{pad}" y="24" font-size="15" font-weight="600">{html.escape(title)}</text>',
            f'<image x="{pad}" y="{pad + 30}" width="{w:.1f}" height="{h:.1f}" preserveAspectRatio="none" style="image-rendering:pixelated" '
            f'href="data:image/png;base64,{image}"/>',
            f'<rect x="{pad}" y="{pad + 30}" width="{w:.1f}" height="{h:.1f}" fill="none" stroke="#222" stroke-width="1.5"/>']
    for ref, pads in sorted(parts.items()):
        xs = [q["x"] - q["hw"] for q in pads] + [q["x"] + q["hw"] for q in pads]
        ys = [q["y"] - q["hh"] for q in pads] + [q["y"] + q["hh"] for q in pads]
        (ax, ay), (bx, by) = at(min(xs), min(ys)), at(max(xs), max(ys))
        body.append(f'<rect x="{ax:.1f}" y="{ay:.1f}" width="{bx - ax:.1f}" height="{by - ay:.1f}" fill="none" stroke="#fff" stroke-opacity="0.8" stroke-width="1"/>')
        body.append(f'<text x="{(ax + bx) / 2:.1f}" y="{ay - 3:.1f}" font-size="10" text-anchor="middle" fill="#fff" stroke="#000" stroke-width="2.5" paint-order="stroke">{html.escape(ref)}</text>')
    spot = result.get("hotspot") or {}
    if spot:
        sx, sy = at(spot["x"], spot["y"])
        body.append(f'<circle cx="{sx:.1f}" cy="{sy:.1f}" r="6" fill="none" stroke="#00e0ff" stroke-width="2"/>')
        body.append(f'<text x="{sx + 9:.1f}" y="{sy + 4:.1f}" font-size="11" fill="#00e0ff" stroke="#000" stroke-width="2.5" paint-order="stroke">'
                    f'{spot["temp_c"]:.1f} C, rise {spot["rise_c"]:.1f} C</text>')
    bx0, by0 = pad + w + 20, pad + 30
    stops = "".join(f'<stop offset="{k / 10:.1f}" stop-color="{_ramp(1 - k / 10)}"/>' for k in range(11))
    body.append(f'<defs><linearGradient id="ramp" x1="0" y1="0" x2="0" y2="1">{stops}</linearGradient></defs>')
    body.append(f'<rect x="{bx0}" y="{by0}" width="18" height="{h:.1f}" fill="url(#ramp)" stroke="#222"/>')
    if lo is not None:
        for k in range(5):
            t = k / 4
            y = by0 + h * (1 - t)
            rise = lo + (hi - lo) * t
            body.append(f'<text x="{bx0 + 24}" y="{y + 4:.1f}" font-size="11">{ambient + rise:.1f} C</text>')
            body.append(f'<text x="{bx0 + 24}" y="{y + 16:.1f}" font-size="9" fill="#666">rise {rise:.1f}</text>')
    else:
        body.append(f'<text x="{bx0 + 24}" y="{by0 + 12}" font-size="11">no scale: boardfield gave no range</text>')
    body.append(f'<text x="{pad}" y="{height - 10:.0f}" font-size="11" fill="#444">{x2 - x1:.1f} x {y2 - y1:.1f} mm, {ambient:.0f} C ambient. '
                f'Hottest layer at each point. A solver estimate, not a measurement</text>')
    out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" font-family="sans-serif">{"".join(body)}</svg>\n')
    return out
