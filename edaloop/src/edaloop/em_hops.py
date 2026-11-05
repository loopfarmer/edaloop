"""Field-solve each RF hop as routed: its own copper, the pours and ground vias around it, and the reference plane.

The coupon check proves the line's impedance. It says nothing about a neck, a choke's pad on the line or the pad at
each end. This cuts a window around each hop from the routed board and solves it as a 2-port, so that those show up as
return loss and insertion loss. A hop is cached on its geometry: an edit elsewhere doesn't repeat its solve.

What this isn't: parts aren't modelled, so a choke is only its pad, and a port is a lumped sheet where the track leaves each
end land. Read the results as what the routing adds to the parts' datasheet figures, not as the assembled chain.
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import skrf
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from . import ground, toolchain

UM = 1000       # clay's database units per micron


MESH_FAULTS = ("volume is 0", "MeshError")
WINDOW_RETRIES_MM = (0.37, 0.71)   # not round numbers, so the new edge doesn't land on the same copper
DEFAULT_WORKERS = 3      # each solver uses several threads itself, so more than this mostly contends
KILLED = "killed by the system"     # what _em_run says when the solver died to SIGKILL, which is most often the kernel out of memory


def _layer(n: int) -> dict:
    return {"Layer": n, "Datatype": 0}


def _no_holes(poly) -> list:
    """Clay polygons have no holes, so a polygon with one is split through it until none are left."""
    if poly.is_empty:
        return []
    if poly.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [x for g in poly.geoms for x in _no_holes(g)]
    if poly.geom_type != "Polygon" or poly.area < 1e-6:
        return []
    if not poly.interiors:
        return [poly]
    cx = Polygon(poly.interiors[0]).centroid.x
    x1, y1, x2, y2 = poly.bounds
    return _no_holes(poly.intersection(box(x1 - 1, y1 - 1, cx, y2 + 1))) + _no_holes(poly.intersection(box(cx, y1 - 1, x2 + 1, y2 + 1)))


def pad_polygon(pad: dict) -> Polygon:
    """A pad's true outline in board millimetres."""
    w, h = pad["w"] / 2, pad["h"] / 2
    return Polygon([ground._from_pad(pad, u, v) for u, v in ((w, h), (-w, h), (-w, -h), (w, -h))])


def _snapped(points) -> list[tuple[float, float]]:
    """To 10 um. A rotated part's pad lands under a micron off the line, and a track that tilted makes a solver cut its port to half
    the width, on what it reads as a curved edge. That makes the answer depend on the mesh. Ten microns can't change a line's impedance."""
    return [(round(p[0], 2), round(p[1], 2)) for p in points]


def land_edge(pad: dict, into: tuple[float, float], tracks: list = ()) -> tuple[float, float]:
    """Where the track leaves a land, heading along the line, stepped just onto the track so that a solver reads it as on the metal.

    The line through the land's centre is used when the track is on it. A track can leave from off centre, as one drawn from a corner of a
    wide connector land does, and then the port goes where that track crosses the land's edge, since anywhere else it's on no metal."""
    land = pad_polygon(pad)
    reach = pad["w"] + pad["h"]
    ray = LineString([(pad["x"], pad["y"]), (pad["x"] + into[0] * reach, pad["y"] + into[1] * reach)])
    hit = ray.intersection(land.exterior)
    point = hit if hit.geom_type == "Point" else min(hit.geoms, key=lambda g: g.distance(Point(pad["x"], pad["y"])))
    step = 0.005
    at = (point.x + into[0] * step, point.y + into[1] * step)
    if tracks and not any(LineString([t.start, t.end]).distance(Point(at)) < t.width / 2 for t in tracks):
        crossing = _track_leaves(land, tracks, into)
        if crossing is not None:
            at = (crossing[0] + into[0] * step, crossing[1] + into[1] * step)
    return at


def _track_leaves(land: Polygon, tracks: list, into: tuple[float, float]) -> tuple[float, float] | None:
    """Where a track heading `into` crosses out of the land, or None if no track does."""
    for t in tracks:
        for near, far in ((t.start, t.end), (t.end, t.start)):
            span = math.dist(near, far)
            if span < 1e-9 or land.distance(Point(near)) >= 1e-3 or land.contains(Point(far)):
                continue
            if ((far[0] - near[0]) * into[0] + (far[1] - near[1]) * into[1]) / span < 0.9998:
                continue
            hit = LineString([near, far]).intersection(land.exterior)
            points = [hit] if hit.geom_type == "Point" else [g for g in getattr(hit, "geoms", []) if g.geom_type == "Point"]
            if points:
                p = max(points, key=lambda g: g.distance(Point(near)))
                return p.x, p.y
    return None


def leaving_direction(pad: dict, tracks: list, chord: tuple[float, float]) -> tuple[float, float]:
    """Which way the track leaves a land. On a hop that bends, the straight line to the far end misses the copper, and so would a port put on it."""
    land = pad_polygon(pad)
    leaving = []
    for t in tracks:
        for near, far in ((t.start, t.end), (t.end, t.start)):
            if land.distance(Point(near)) < 1e-3 and not land.contains(Point(far)):
                span = math.dist(near, far)
                leaving.append(((far[0] - near[0]) / span, (far[1] - near[1]) / span))
    if not leaving:
        return chord
    # A land with a tap on it has more than one track leaving. The hop runs along the one heading most towards its other end.
    d = max(leaving, key=lambda v: v[0] * chord[0] + v[1] * chord[1])
    # Keep the straight line when the track agrees with it, so that a straight hop's port doesn't move.
    return chord if d[0] * chord[0] + d[1] * chord[1] > 0.9998 else d


def direction_label(d: tuple[float, float]) -> str:
    """A port's direction points into the copper. Board y runs down and clay y runs up."""
    angle = math.degrees(math.atan2(-d[1], d[0])) % 360
    return f"R{int(round(angle / 90) * 90) % 360}"


def plane_voids(board, layer: str) -> list[Polygon]:
    """Rule areas keeping pours off a layer. The hop's reference plane has to have the same holes as the board's."""
    from .pcbfile import _child, _val
    out = []
    for zone in (x for x in board.tree if isinstance(x, list) and x and x[0] == "zone"):
        keepout = _child(zone, "keepout")
        if not keepout or _val(zone, "layer") != layer or _val(keepout, "copperpour") != "not_allowed":
            continue
        pts = _child(_child(zone, "polygon"), "pts")
        out.append(Polygon([(float(q[1]), float(q[2])) for q in pts if isinstance(q, list) and q[0] == "xy"]))
    return out


def cut_shapes(net: str, tracks: list, pads: list, pours: list[Polygon], vias: list[tuple], window, signal: int, reference: int,
               entry: dict, leave: dict, voids: list[Polygon] | None = None, below: int | None = None, vias_on: int | None = None) -> list[dict]:
    """Clay shapes for one hop. Coordinates in, board mm. Coordinates out, clay database units with y flipped. `vias_on` is the via drawing
    layer from `via_layer`, and is needed whenever there are vias."""
    if vias and vias_on is None:
        raise ValueError("ground vias need the stackup's via layer, or the solver reads them as inert")
    def xy(points) -> list[int]:
        return [int(round(v)) for p in points for v in (p[0] * 1000 * UM, -p[1] * 1000 * UM)]

    def polygon(p, layer, name) -> dict:
        return {"$type": "Poly", "Xy": xy(list(p.exterior.coords)[:-1]), "Layer": _layer(layer), "Net": name}
    shapes = []
    # A part's own lands are left out: its datasheet was measured on them, so counting them here counts them twice.
    # The track is cut where it enters each land, and tap pads stay, since the routing put them on the line.
    lands = unary_union([pad_polygon(entry), pad_polygon(leave)])
    for t in tracks:
        kept = LineString([t.start, t.end]).difference(lands)
        for piece in ([kept] if kept.geom_type == "LineString" else list(getattr(kept, "geoms", []))):
            if piece.length > 1e-3:
                shapes.append({"$type": "Path", "Xy": xy(_snapped(piece.coords)), "Width": int(round(t.width * 1000 * UM)), "End": "Flush",
                               "Layer": _layer(signal), "Net": net})
    for pad in pads:
        if pad["net"] == net and pad is not entry and pad is not leave:
            shapes.append(polygon(pad_polygon(pad), signal, net))
    # Ground copper on the signal layer: the fill, plus ground pads in the window, which the fill may only touch.
    grounds = [pad_polygon(x) for x in pads if x["net"] == "GND" and pad_polygon(x).intersects(window)]
    merged = unary_union([*[p.intersection(window) for p in pours], *[g.intersection(window) for g in grounds]])
    # The window can cut a patch off from its vias. Left in, it floats and resonates, which the real board doesn't do.
    for piece in _no_holes(merged):
        if any(piece.distance(Point(v)) < ground.RADIUS for v in vias):
            shapes.append(polygon(piece, signal, "GND"))
    holes = [v for v in voids or [] if v.intersects(window)]
    if holes:
        for piece in _no_holes(window.difference(unary_union(holes))):
            shapes.append(polygon(piece, reference, "GND"))
        # Through a void the pad sees the next plane down. Leaving it out would flatter the result.
        if below is not None:
            shapes.append(polygon(window, below, "GND"))
    else:
        shapes.append(polygon(window, reference, "GND"))
    for x, y in vias:
        shapes.append({"$type": "Via", "X": int(round(x * 1000 * UM)), "Y": int(round(-y * 1000 * UM)), "PadSize": int(ground.VIA * 1000 * UM),
                       "DrillSize": int(ground.DRILL * 1000 * UM), "LandingLayer": _layer(signal), "Layer": _layer(vias_on), "Net": "GND"})
    span = math.dist((entry["x"], entry["y"]), (leave["x"], leave["y"])) or 1.0
    d = ((leave["x"] - entry["x"]) / span, (leave["y"] - entry["y"]) / span)
    for text, pad, chord in (("1", entry, d), ("2", leave, (-d[0], -d[1]))):
        into = leaving_direction(pad, tracks, chord)
        at = _snapped([land_edge(pad, into, tracks)])[0]
        # Snapping can pull a port that sat a few microns past the edge back onto the land, which is cut out of the model. Step it on again.
        for _ in range(3):
            if pad_polygon(pad).distance(Point(at)) > 1e-3:
                break
            at = _snapped([(at[0] + into[0] * 0.01, at[1] + into[1] * 0.01)])[0]
        shapes.append({"$type": "Label", "Layer": _layer(signal), "X": int(round(at[0] * 1000 * UM)), "Y": int(round(-at[1] * 1000 * UM)),
                       "Text": text, "Height": 400000, "IsPort": True, "PortDirection": direction_label(into)})
    return shapes


_PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22"]


def sparams_svg(hops: dict[str, Path], low_hz: float, high_hz: float, out: Path, title: str) -> Path:
    """Each hop's |S11| and |S21| in dB against frequency, the band shaded. Solver results for the copper alone, without the parts."""
    import html
    nets = list(hops)
    data = {net: skrf.Network(str(path)) for net, path in hops.items()}
    f_lo = min(float(n.f.min()) for n in data.values()) / 1e9
    f_hi = max(float(n.f.max()) for n in data.values()) / 1e9
    w, h, left, top, gap = 640, 220, 60, 56, 60
    panels = [("|S11|, dB (return loss)", lambda n: 20 * np.log10(np.maximum(np.abs(n.s[:, 0, 0]), 1e-6))),
              ("|S21|, dB (insertion loss)", lambda n: 20 * np.log10(np.maximum(np.abs(n.s[:, 1, 0]), 1e-6)))]
    body = [f'<rect width="{w + left + 170}" height="{top + 2 * h + gap + 50}" fill="#fff"/>',
            f'<text x="{left}" y="24" font-size="15" font-weight="600">{html.escape(title)}</text>']
    for k, (label, fn) in enumerate(panels):
        y0 = top + k * (h + gap)
        curves = {net: (data[net].f / 1e9, fn(data[net])) for net in nets}
        lo = min(float(c[1].min()) for c in curves.values())
        hi = max(float(c[1].max()) for c in curves.values())
        # A little room round the curves, and at least 0.2 dB of span, so a near-lossless line isn't drawn as flat noise.
        pad = max(0.05 * (hi - lo), (0.2 - (hi - lo)) / 2, 0.0)
        lo, hi = lo - pad, hi + pad
        def x(f):
            return left + (f - f_lo) / max(f_hi - f_lo, 1e-9) * w

        def y(v, y0=y0, lo=lo, hi=hi):
            return y0 + (hi - v) / (hi - lo) * h
        body.append(f'<rect x="{x(low_hz / 1e9):.1f}" y="{y0}" width="{x(high_hz / 1e9) - x(low_hz / 1e9):.1f}" height="{h}" fill="#000" fill-opacity="0.05"/>')
        body.append(f'<rect x="{left}" y="{y0}" width="{w}" height="{h}" fill="none" stroke="#222"/>')
        body.append(f'<text x="{left}" y="{y0 - 6}" font-size="12">{label}</text>')
        for t in range(5):
            v = lo + (hi - lo) * t / 4
            places = 2 if hi - lo < 1 else 1 if hi - lo < 10 else 0
            body.append(f'<text x="{left - 6}" y="{y(v) + 4:.1f}" font-size="10" text-anchor="end">{round(v, places) + 0.0:.{places}f}</text>')
            body.append(f'<line x1="{left}" x2="{left + w}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#ddd"/>')
        for t in range(5):
            f = f_lo + (f_hi - f_lo) * t / 4
            body.append(f'<text x="{x(f):.1f}" y="{y0 + h + 14}" font-size="10" text-anchor="middle">{f:.2f}</text>')
        for i, (fs, vs) in enumerate(curves.values()):
            pts = " ".join(f"{x(float(a)):.1f},{y(float(b)):.1f}" for a, b in zip(fs, vs))
            body.append(f'<polyline points="{pts}" fill="none" stroke="{_PALETTE[i % len(_PALETTE)]}" stroke-width="1.6"/>')
    for i, net in enumerate(nets):
        body.append(f'<line x1="{left + w + 16}" x2="{left + w + 36}" y1="{top + 10 + 18 * i}" y2="{top + 10 + 18 * i}" stroke="{_PALETTE[i % len(_PALETTE)]}" stroke-width="2"/>')
        body.append(f'<text x="{left + w + 42}" y="{top + 14 + 18 * i}" font-size="11">{html.escape(net)}</text>')
    body.append(f'<text x="{left}" y="{top + 2 * h + gap + 34}" font-size="11" fill="#444">GHz. Shaded: the band. Each hop solved alone, copper without its parts: a solver result, not a measurement</text>')
    out.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w + left + 170}" height="{top + 2 * h + gap + 50}" font-family="sans-serif">{"".join(body)}</svg>\n')
    return out


def hop_metrics(s2p: Path, low_hz: float, high_hz: float) -> dict:
    n = skrf.Network(str(s2p))
    use = (n.f >= low_hz * 0.999) & (n.f <= high_hz * 1.001)
    if not use.any():
        raise RuntimeError(f"{s2p.name} has no points in the band")
    s11 = np.abs(n.s[use, 0, 0])
    s22 = np.abs(n.s[use, 1, 1])
    il = -20 * np.log10(np.abs(n.s[use, 1, 0]))
    worst = np.maximum(s11, s22)
    return {"rl_db": round(float(-20 * np.log10(max(worst.max(), 1e-6))), 1), "il_max_db": round(float(il.max()), 3),
            "il_by_ghz": {f"{f / 1e9:.3f}": round(float(v), 3) for f, v in zip(n.f[use], il)}}


def _pours_and_vias(p) -> tuple[dict[int, list[Polygon]], list[tuple], dict]:
    """Ground fills by clay layer, vias and technology as circuitRF imports them, which is the same reading every EM check uses."""
    tp = toolchain.paths()
    cells = p.work / "hop_cells"
    shutil.rmtree(cells, ignore_errors=True)
    toolchain.run([tp["circuitrf"], "convert", p.board, "-o", cells, "--to", "clay"], env=toolchain.circuitrf_env(), timeout=600)
    clay = cells / p.board.stem / "layout" / f"{p.board.stem}.clay"
    tech_file = cells / f"{p.board.stem}.ctech"
    if not clay.exists() or not tech_file.exists():
        raise RuntimeError("circuitRF couldn't import the board")
    data = json.loads(clay.read_text())
    tech = json.loads(tech_file.read_text())
    pours, vias = {}, []
    keys = {x["Name"]: x["DrawingLayers"][0]["Layer"] for x in tech["Stackup"]["Layers"] if x.get("Kind") == "Conductor"}
    for shape in data["Shapes"]:
        if shape["$type"] == "Poly" and shape.get("Net") == "GND":
            q = shape["Xy"]
            poly = Polygon([(q[i] / UM / 1000, -q[i + 1] / UM / 1000) for i in range(0, len(q), 2)]).buffer(0)
            pours.setdefault(shape["Layer"]["Layer"], []).append(poly)
        elif shape["$type"] == "Via" and shape.get("Net") == "GND":
            vias.append((shape["X"] / UM / 1000, -shape["Y"] / UM / 1000))
    return pours, vias, {"tech": tech, "keys": keys}


class OffLayer(Exception):
    """A hop this model can't represent."""


def via_layer(tech: dict, signal: str, reference: str) -> int:
    """The drawing layer of a via that joins the signal layer to its reference plane. circuitRF reads a via by its drawing layer, and one drawn
    on a layer no via entry claims is inert in EM extraction, so a guessed layer number would solve the hop with no ground vias at all."""
    conductors = [x["Name"] for x in tech["Stackup"]["Layers"] if x.get("Kind") == "Conductor"]
    want = {conductors.index(signal), conductors.index(reference)}
    for entry in (x for x in tech["Stackup"]["Layers"] if x.get("Kind") == "Via" and x.get("DrawingLayers")):
        span = [conductors.index(n) for n in (entry.get("SpanFromLayer"), entry.get("SpanToLayer")) if n in conductors]
        if len(span) == 2 and min(span) <= min(want) and max(want) <= max(span):
            return entry["DrawingLayers"][0]["Layer"]
    raise OffLayer(f"the stackup has no via joining {signal} to {reference}, so its ground vias can't be modelled")


def on_layer(pad: dict, layer: str) -> bool:
    """Whether a pad has copper on an outer layer. A drilled pad has it on both."""
    return bool(pad.get("drill")) or bool(pad.get("back") if layer == "B.Cu" else pad.get("front", True))


def hop_layers(net: str, tracks: list, ends: tuple[dict, dict], names: list[str], cfg: dict) -> tuple[str, str, str | None, bool]:
    """The hop's signal layer, its reference plane, the plane past that, and whether the reference is assumed rather than configured.

    The model is one outer signal layer over a plane. A hop on F.Cu is referenced to `reference_layer`, In1.Cu by default on a board with
    inner layers. A hop wholly on B.Cu is the same picture upside down: `bottom_reference_layer` if set, otherwise the mirror of the top's,
    which is an assumption about the stackup and is marked as one."""
    layers = {t.layer for t in tracks if t.kind != "via"}
    if any(t.kind == "via" for t in tracks) or len(layers) > 1:
        # One signal layer over a plane, so a hop through a via would be drawn as two pieces with an open between them.
        raise OffLayer(f"it changes layer ({', '.join(sorted(layers))}) through a via, and this model only has one signal layer")
    if not layers:
        layers = {"B.Cu"} if all(on_layer(x, "B.Cu") and not on_layer(x, "F.Cu") for x in ends) else {"F.Cu"}
    signal = next(iter(layers))
    if names[0] != "F.Cu" or names[-1] != "B.Cu" or signal not in ("F.Cu", "B.Cu"):
        raise OffLayer(f"it runs on {signal}, and this model only has an outer signal layer over a plane")
    top = cfg.get("reference_layer") or ("In1.Cu" if "In1.Cu" in names else "B.Cu")
    if signal == "F.Cu":
        i = names.index(top)
        return signal, top, names[i + 1] if i + 1 < len(names) else None, False
    reference = cfg.get("bottom_reference_layer") or names[len(names) - 1 - names.index(top)]
    if reference == signal:
        raise OffLayer(f"its reference plane would be {reference}, the layer it runs on")
    i = names.index(reference)
    return signal, reference, names[i - 1] if i > 0 else None, not cfg.get("bottom_reference_layer")


def solve_unique(jobs: dict[str, Path], solve, workers: int) -> dict:
    """Solve each distinct geometry once, several at a time. Each result is `solve`'s return, or the error text if it raised RuntimeError."""
    def one(ws):
        try:
            return solve(ws)
        except RuntimeError as e:
            return str(e)[:900]
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        out = dict(zip(jobs, pool.map(one, jobs.values())))
    # A solver the system killed most likely ran out of memory beside the others. Alone it may fit, so each gets one more go, one at a time.
    for key in [k for k, v in out.items() if isinstance(v, str) and KILLED in v]:
        again = one(jobs[key])
        out[key] = again if not isinstance(again, str) else f"{again[:820]} (also when run alone)"
    return out


def run(p, cfg: dict, board, chains: list, solver: dict, solver_label: str, run_solver) -> dict:
    """Solve each hop. Returns per-hop metrics. `run_solver` is the check's own workspace runner."""
    facts = ground._facts(p.board, p.work)
    pads = facts["pads"]
    named = {f"{x['ref']}.{x['pad']}": x for x in pads}
    pours, vias, tech = _pours_and_vias(p)
    margin = float(cfg.get("margin_mm", 2.5))
    low, high = float(cfg["band_ghz"][0]) * 1e9, float(cfg["band_ghz"][1]) * 1e9
    names = list(tech["keys"])
    keys = tech["keys"]

    def prepare(net, entry, leave, reach) -> tuple:
        ends = (named[f"{entry[0]}.{entry[1]}"], named[f"{leave[0]}.{leave[1]}"])
        own = [x for x in pads if x["net"] == net]
        signal, reference, below, assumed = hop_layers(net, [t for t in board.tracks if t.net == net], ends, names, cfg)
        tracks = [t for t in board.tracks if t.net == net and t.kind != "via"]
        outline = unary_union([pad_polygon(x) for x in own])
        x1, y1, x2, y2 = outline.bounds
        window = box(x1 - reach, y1 - reach, x2 + reach, y2 + reach)
        keep = [v for v in vias if window.contains(Point(v))]
        # Only pads with copper on the signal layer: a ground pad on the far side isn't ground beside this track.
        near = [x for x in pads if on_layer(x, signal) and pad_polygon(x).intersects(window)]
        shapes = cut_shapes(net, tracks, near, pours.get(keys[signal], []), keep, window, keys[signal], keys[reference], *ends,
                            plane_voids(board, reference), keys[below] if below else None, via_layer(tech["tech"], signal, reference))
        layers = {"signal_layer": signal, "reference_layer": reference, **({"reference_assumed": True} if assumed else {})}
        digest = hashlib.sha256(json.dumps([shapes, tech["tech"]["Stackup"], solver, cfg.get("freq_points"), cfg["band_ghz"]], sort_keys=True).encode()).hexdigest()[:14]
        ws = p.work / "hops" / digest
        if not (ws / ".cws").exists():
            shutil.rmtree(ws, ignore_errors=True)
            (ws / "cells" / "HOP" / "layout").mkdir(parents=True)
            (ws / "em").mkdir()
            t = json.loads(json.dumps(tech["tech"]))
            for layer in t["Stackup"]["Layers"]:
                layer["IsGroundReference"] = layer["Name"] == reference
            (ws / "cells" / "tech.ctech").write_text(json.dumps(t))
            (ws / "cells" / "HOP" / ".ccell").write_text(json.dumps({"FormatVersion": 1, "Parameters": [], "PrimaryLayout": "HOP.clay",
                                                                      "IsTestBench": False, "Terminals": [], "NumPorts": 0}))
            (ws / "cells" / "HOP" / "layout" / "HOP.clay").write_text(json.dumps(
                {"FormatVersion": 1, "DbuPerMicron": 1000, "DisplayUnit": "Um", "SnapDbu": 0, "AngleMode": "AnyAngle",
                 "TechRef": "../../tech.ctech", "Shapes": shapes, "Instances": []}))
            (ws / ".cws").write_text(json.dumps({"FormatVersion": 2, "LibraryRefs": [], "KnownFiles": []}))
        return digest, ws, (round(x2 - x1 + 2 * reach, 2), round(y2 - y1 + 2 * reach, 2)), len(keep), layers

    def solve_all(todo: list, reach: float) -> dict:
        rows, found = [], {}
        for row in todo:
            try:
                rows.append((row[0], *prepare(*row, reach)))
            except OffLayer as e:
                found[row[0]] = {"unsolved": str(e), "digest": ""}
        solved = solve_unique({r[1]: r[2] for r in rows}, lambda ws: run_solver(ws, "cells/HOP/layout/HOP.clay"), int(cfg.get("parallel", DEFAULT_WORKERS)))
        for net, digest, ws, size, via_count, layers in rows:
            result = solved[digest]
            if isinstance(result, str):
                # One unsolvable hop mustn't cost the readings of the others. The check still fails on it.
                found[net] = {"unsolved": result, "digest": digest}
                continue
            s2p, seconds = result
            kept_log = ws / f"{solver_label}.log"
            narrowed = kept_log.exists() and "curved edge" in kept_log.read_text()
            found[net] = {**hop_metrics(s2p, low, high), "seconds": seconds, "port_narrowed": narrowed, "window_mm": list(size), "vias": via_count, "digest": digest,
                          **layers, "s2p": str(s2p)}
        return found

    out = solve_all(chains, margin)
    # A window edge can leave a sliver of copper the mesher can't handle. It's a fault of the cut and not of the hop, so cut it elsewhere.
    for extra in WINDOW_RETRIES_MM:
        again = [row for row in chains if any(fault in out[row[0]].get("unsolved", "") for fault in MESH_FAULTS)]
        if not again:
            break
        for net, m in solve_all(again, margin + extra).items():
            out[net] = {**m, "window_retried": True}
    return out
