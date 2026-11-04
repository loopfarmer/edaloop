"""Read-only facts out of a .kicad_pcb, with no dependency on pcbnew."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path


def parse_sexpr(text: str):
    """Parse KiCad's s-expression format into nested lists of strings."""
    stack, current, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
        elif c == "(":
            stack.append(current)
            current = []
            i += 1
        elif c == ")":
            parent = stack.pop()
            parent.append(current)
            current = parent
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            current.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            current.append(text[i:j])
            i = j
    return current[0]


def compare_copper(baseline: dict, now: dict) -> dict:
    """What happened to baseline copper. Lost copper is a real move; added copper is measured."""
    lost, added_mm = {}, 0.0
    for net, items in baseline.items():
        current = [tuple(x) for x in now.get(net, [])]
        old = {tuple(x) for x in items}
        gone = old - set(current)
        if gone:
            lost[net] = len(gone)
        for x in current:
            if x not in old and x[0] != "via":
                added_mm += math.dist(x[2:4], x[4:6])
    new_vias = sum(tuple(x) not in {tuple(y) for y in baseline[n]} and x[0] == "via"
                   for n in baseline for x in now.get(n, []))
    return {"lost": lost, "added_mm": round(added_mm, 4), "new_vias": new_vias}


def _child(node, name):
    return next((x for x in node if isinstance(x, list) and x and x[0] == name), None)


def _val(node, name, default=None):
    c = _child(node, name)
    return c[1] if c and len(c) > 1 else default


@dataclass(frozen=True)
class Track:
    kind: str                # segment | arc | via
    net: str
    layer: str
    start: tuple
    end: tuple
    width: float
    drill: float
    locked: bool


class Board:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.tree = parse_sexpr(self.path.read_text())
        numbered = {x[1]: x[2] for x in self.tree if isinstance(x, list) and x[0] == "net" and len(x) > 2}
        self.tracks: list[Track] = []
        for node in self.tree:
            if not isinstance(node, list) or node[0] not in ("segment", "arc", "via"):
                continue
            net = _val(node, "net", "")
            net = numbered.get(net, net)
            if node[0] == "via":
                at = tuple(float(v) for v in _child(node, "at")[1:3])
                layers = _child(node, "layers")
                self.tracks.append(Track("via", net, layers[1] if layers else "", at, at,
                                         float(_val(node, "size", 0)), float(_val(node, "drill", 0)),
                                         _val(node, "locked") == "yes"))
            else:
                self.tracks.append(Track(node[0], net, _val(node, "layer", ""),
                                         tuple(float(v) for v in _child(node, "start")[1:3]),
                                         tuple(float(v) for v in _child(node, "end")[1:3]),
                                         float(_val(node, "width", 0)), 0.0,
                                         _val(node, "locked") == "yes"))

    def net_names(self) -> set[str]:
        names = set()
        for node in self.tree:
            if isinstance(node, list) and node[0] == "footprint":
                for pad in (x for x in node if isinstance(x, list) and x[0] == "pad"):
                    net = _child(pad, "net")
                    if net:
                        names.add(net[-1])
        return names - {""}

    def all_pads(self) -> list[dict]:
        """Every copper pad: name, net, centre, half sizes in board axes, footprint centre, and layers.

        Rotation is applied conservatively to the pad's box: a pad turned 90 degrees swaps its
        sides, and at other angles it uses the larger side both ways.
        """
        numbered = {x[1]: x[2] for x in self.tree if isinstance(x, list) and x and x[0] == "net" and len(x) > 2}
        out = []
        for fp in self.tree:
            if not (isinstance(fp, list) and fp and fp[0] == "footprint"):
                continue
            at = _child(fp, "at")
            fx, fy = float(at[1]), float(at[2])
            frot = float(at[3]) if len(at) > 3 else 0.0
            props = [x for x in fp if isinstance(x, list) and x[0] == "property" and len(x) > 2 and x[1] == "Reference"]
            ref = props[0][2] if props else (_val(fp, "fp_text") or "?")
            th = math.radians(frot)
            for pad in (x for x in fp if isinstance(x, list) and x[0] == "pad"):
                layers = _child(pad, "layers")
                copper = [str(lyr) for lyr in (layers[1:] if layers else []) if str(lyr).endswith(".Cu")]
                if not copper:
                    continue
                n = _child(pad, "net")
                name = n[-1] if n else ""
                name = numbered.get(name, name)
                pat = _child(pad, "at")
                px, py = float(pat[1]), float(pat[2])
                prot = float(pat[3]) if len(pat) > 3 else frot   # a pad's angle in the file already includes the footprint's
                size = _child(pad, "size")
                w, h = float(size[1]), float(size[2])
                quarter = abs(prot) % 180
                if abs(quarter - 90) < 1:
                    w, h = h, w
                elif quarter > 1:
                    w = h = max(w, h)
                out.append({"name": f"{ref}.{pad[1]}", "net": name,
                            "x": round(fx + px * math.cos(th) + py * math.sin(th), 5),
                            "y": round(fy - px * math.sin(th) + py * math.cos(th), 5),
                            "hw": w / 2, "hh": h / 2, "fx": fx, "fy": fy, "layers": copper})
        return out

    def net_pads(self, net: str) -> dict[str, tuple]:
        """Pads on a net: "REF.PIN" -> (x, y, half_width, half_height) in board coordinates."""
        return {p["name"]: (p["x"], p["y"], p["hw"], p["hh"]) for p in self.all_pads() if p["net"] == net}

    def copper_thickness_um(self) -> dict[str, float]:
        """Copper thickness per layer from the board's stackup. Empty if the file has none."""
        setup = _child(self.tree, "setup")
        stackup = _child(setup, "stackup") if setup else None
        out = {}
        for layer in (x for x in (stackup or []) if isinstance(x, list) and x[0] == "layer"):
            if _val(layer, "type") == "copper" and _val(layer, "thickness"):
                out[layer[1]] = float(_val(layer, "thickness")) * 1000
        return out

    def board_height_mm(self) -> float:
        setup = _child(self.tree, "setup")
        stackup = _child(setup, "stackup") if setup else None
        total = sum(float(_val(x, "thickness")) for x in (stackup or [])
                    if isinstance(x, list) and x[0] == "layer" and _val(x, "thickness"))
        return total or 1.6

    def zones_on_net(self, net: str) -> int:
        numbered = {x[1]: x[2] for x in self.tree if isinstance(x, list) and x[0] == "net" and len(x) > 2}
        count = 0
        for node in self.tree:
            if isinstance(node, list) and node[0] == "zone":
                name = _val(node, "net_name") or numbered.get(_val(node, "net", ""), _val(node, "net", ""))
                count += name == net
        return count

    def stats(self) -> dict:
        segs = [t for t in self.tracks if t.kind != "via"]
        return {"segments": len(segs), "vias": sum(t.kind == "via" for t in self.tracks),
                "track_mm": round(sum(math.dist(t.start, t.end) for t in segs), 2),
                "locked": sum(t.locked for t in self.tracks),
                "nets": len(self.net_names())}

    def stackup_digest(self) -> str:
        setup = _child(self.tree, "setup")
        stackup = _child(setup, "stackup") if setup else None
        return hashlib.sha256(repr(stackup).encode()).hexdigest()

    def copper_items(self, nets) -> dict[str, list]:
        """Copper per net, rounded to 0.1 um because a pcbnew save perturbs coordinates by about 1 nm."""
        def item(t):
            pts = [round(v, 4) for v in (*t.start, *t.end)]
            return [t.kind, t.layer, *pts, round(t.width, 4), round(t.drill, 4)]
        return {n: sorted(item(t) for t in self.tracks if t.net == n) for n in nets}

    def ground_vias(self, nets=("GND",)) -> int:
        return sum(t.kind == "via" and t.net in nets for t in self.tracks)

    def right_angles(self, nets) -> list[str]:
        """Junctions on the named nets where two segments of a net meet at 90 degrees or sharper."""
        found = []
        for name in nets:
            segs = [t for t in self.tracks if t.net == name and t.kind == "segment"]
            for i, a in enumerate(segs):
                for b in segs[i + 1:]:
                    if a.layer != b.layer:
                        continue
                    for pa, qa in ((a.start, a.end), (a.end, a.start)):
                        for pb, qb in ((b.start, b.end), (b.end, b.start)):
                            if math.dist(pa, pb) > 1e-4:
                                continue
                            v1 = (qa[0] - pa[0], qa[1] - pa[1])
                            v2 = (qb[0] - pb[0], qb[1] - pb[1])
                            n1, n2 = math.hypot(*v1), math.hypot(*v2)
                            if n1 < 1e-6 or n2 < 1e-6:
                                continue
                            cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)
                            if cos >= -1e-6:
                                found.append(f"{name} at ({pa[0]:.2f}, {pa[1]:.2f})")
        return found
