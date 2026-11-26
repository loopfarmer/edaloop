"""Footprints read from .kicad_mod files, answering the part of pcbnew's footprint API placement asks, with no KiCad.

KiCad 11 has no pcbnew module, and its IPC API can't pose a footprint that isn't on a board yet or hand back a courtyard
polygon. kicad_ops.solve_placement only asks a footprint where its pads and courtyard are at a pose, so this module
stands in for pcbnew there: kicad_ipc_ops installs it as `pcbnew` before importing kicad_ops, as the tests' stand-in
does. Units, angles and rotation follow pcbnew's: nanometres, degrees, y down, and the pad transform of
scripts/kicad_pcb.py, x' = fx + x cos t + y sin t, y' = fy - x sin t + y cos t.

What it can't read the same as pcbnew is refused, never approximated silently: a part placed on the back, or a pad
shape it doesn't know. Arcs are drawn as chords no more than ARC_ERROR_NM from the arc, as KiCad draws them.

Needs shapely.
"""
from __future__ import annotations

import math
import os
import re

from scripts.kicad_pcb import kid, kids, sexp
from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, box
from shapely.ops import polygonize, unary_union

ARC_ERROR_NM = 5000             # KiCad's courtyard maxError, 0.005 mm: arcs as chords, and how far a courtyard is shrunk
CHAIN_EPSILON_NM = 20000        # how far apart two ends of a courtyard's lines may be and still join, 0.02 mm

# pcbnew's names for the layers and constants placement uses. Distinct values are all that matter.
F_Cu, B_Cu, F_CrtYd, B_CrtYd, F_Fab, Edge_Cuts = range(6)
DEGREES_T = "degrees"
CORNER_STRATEGY_ROUND_ALL_CORNERS = "round"


def FromMM(mm):
    return int(round(mm * 1e6))


def ToMM(nm):
    return nm / 1e6


class VECTOR2I:
    def __init__(self, x=0, y=0):
        self.x, self.y = int(round(x)), int(round(y))

    def __repr__(self):
        return "VECTOR2I(%d, %d)" % (self.x, self.y)


class EDA_ANGLE:
    def __init__(self, value, unit=DEGREES_T):
        if unit != DEGREES_T:
            raise ValueError("only degrees")
        self.degrees = float(value)


class BOX2I:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = int(round(left)), int(round(top)), int(round(right)), int(round(bottom))

    def GetLeft(self):
        return self.left

    def GetTop(self):
        return self.top

    def GetRight(self):
        return self.right

    def GetBottom(self):
        return self.bottom

    def GetWidth(self):
        return self.right - self.left

    def GetHeight(self):
        return self.bottom - self.top

    def Contains(self, p):
        return self.left <= p.x <= self.right and self.top <= p.y <= self.bottom


def _bbox(geom):
    x1, y1, x2, y2 = geom.bounds
    return BOX2I(x1, y1, x2, y2)


def _kicad_segcount(radius, error, angle=360.0):
    """KiCad's GetArcToSegmentCount."""
    radius, error = max(1, int(radius)), max(1, int(error))
    increment = 180 / math.pi * math.acos(1.0 - error / radius) * 2
    increment = min(360.0 / 8, increment)
    return max(int(math.floor(abs(angle) / increment + 0.5)), 2)


def _segments(radius, error=ARC_ERROR_NM, angle=360.0):
    """Chords for an arc of `radius` nm over `angle` degrees, none further than `error` from it."""
    if radius <= error:
        return 4
    step = 2 * math.degrees(math.acos(1 - error / radius))
    return max(1, int(math.ceil(abs(angle) / step)))


class SHAPE_POLY_SET:
    """Polygons in nm, as pcbnew's: the parts placement uses."""

    def __init__(self, other=None):
        self.geom = other.geom if isinstance(other, SHAPE_POLY_SET) else (other if other is not None else Polygon())
        self._ring = None

    def OutlineCount(self):
        g = self.geom
        return 0 if g.is_empty else (len(g.geoms) if isinstance(g, MultiPolygon) else 1)

    def NewOutline(self):
        self._close()
        self._ring = []

    def Append(self, x, y):
        self._ring.append((x, y))

    def _close(self):
        if self._ring and len(self._ring) >= 3:
            self.geom = unary_union([self.geom, Polygon(self._ring)])
        self._ring = None

    def Inflate(self, amount, strategy, max_error):
        self._close()
        if amount:
            quarter = max(1, int(round(_kicad_segcount(abs(amount), max_error) / 4)))
            self.geom = self.geom.buffer(amount, quad_segs=quarter)

    def BooleanIntersection(self, other):
        self._close()
        other._close()
        self.geom = self.geom.intersection(other.geom)

    def Area(self):
        self._close()
        return self.geom.area

    def BBox(self):
        self._close()
        return _bbox(self.geom) if not self.geom.is_empty else BOX2I(0, 0, 0, 0)


def _num(node, i):
    return float(node[i])


def _xy(node):
    return _num(node, 1) * 1e6, _num(node, 2) * 1e6


def _arc_points(start, mid, end, error=ARC_ERROR_NM):
    """Points along the arc through start, mid and end, nm."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-9:
        return [start, end]
    ux = ((ax ** 2 + ay ** 2) * (by - cy) + (bx ** 2 + by ** 2) * (cy - ay) + (cx ** 2 + cy ** 2) * (ay - by)) / d
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) + (bx ** 2 + by ** 2) * (ax - cx) + (cx ** 2 + cy ** 2) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, a1, am = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, end, mid))

    def between(a, lo, hi):          # whether a is on the counter-clockwise sweep lo -> hi
        return (a - lo) % (2 * math.pi) <= (hi - lo) % (2 * math.pi)
    sweep = (a1 - a0) % (2 * math.pi)
    if not between(am, a0, a1):
        sweep -= 2 * math.pi
    n = _segments(r, error, math.degrees(sweep))
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _poly_points(pts):
    out = []
    for item in pts[1:]:
        if item[0] == "xy":
            out.append(_xy(item))
        elif item[0] == "arc":
            arc = _arc_points(_xy(kid(item, "start")), _xy(kid(item, "mid")), _xy(kid(item, "end")))
            out.extend(arc if not out or out[-1] != arc[0] else arc[1:])
    return out


def _shape_outline(node):
    """A graphic's area as a polygon, nm, for a closed shape, or its path as a line for an open one."""
    tag = node[0]
    if tag in ("fp_rect", "gr_rect"):
        (x1, y1), (x2, y2) = _xy(kid(node, "start")), _xy(kid(node, "end"))
        return box(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    if tag in ("fp_circle", "gr_circle"):
        c, e = _xy(kid(node, "center")), _xy(kid(node, "end"))
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return Polygon([(c[0] + r * math.cos(t), c[1] + r * math.sin(t))
                        for t in (2 * math.pi * i / _segments(r) for i in range(_segments(r)))])
    if tag in ("fp_poly", "gr_poly"):
        return Polygon(_poly_points(kid(node, "pts")))
    if tag in ("fp_line", "gr_line"):
        return LineString([_xy(kid(node, "start")), _xy(kid(node, "end"))])
    if tag in ("fp_arc", "gr_arc"):
        return LineString(_arc_points(_xy(kid(node, "start")), _xy(kid(node, "mid")), _xy(kid(node, "end"))))
    return None


def _width(node):
    stroke = kid(node, "stroke")
    w = kid(stroke, "width") if stroke else kid(node, "width")
    return float(w[1]) * 1e6 if w else 0.0


def _layer(node):
    layer = kid(node, "layer")
    return layer[1] if layer else ""


def _closed_outline(shapes):
    """The area a set of shapes closes, as KiCad builds a courtyard: closed shapes as they are, lines and arcs joined end
    to end. Lines that don't close leave it empty, as KiCad's courtyard is when it can't build one."""
    areas, lines = [], []
    for s in shapes:
        g = _shape_outline(s)
        if g is None:
            continue
        (lines if isinstance(g, LineString) else areas).append(g)
    if lines:
        # KiCad joins an end to the next start up to CHAIN_EPSILON_NM away; ends that close are snapped together here.
        ends = []
        for line in lines:
            for x, y in (line.coords[0], line.coords[-1]):
                if not any(math.hypot(x - ex, y - ey) <= CHAIN_EPSILON_NM for ex, ey in ends):
                    ends.append((x, y))

        def snap(pt):
            return next(((ex, ey) for ex, ey in ends if math.hypot(pt[0] - ex, pt[1] - ey) <= CHAIN_EPSILON_NM), pt)
        snapped = [LineString([snap(c) if i in (0, len(line.coords) - 1) else c for i, c in enumerate(line.coords)]) for line in lines]
        rings = list(polygonize(unary_union(snapped)))
        if not rings:
            return Polygon()
        used = unary_union([r.exterior for r in rings])
        if any(not used.buffer(2).contains(line) for line in snapped):
            return Polygon()
        areas += rings
    if not areas:
        return Polygon()
    # Touching courtyards, or ones exactly the clearance apart, are legal in KiCad: it shrinks each by its arc tolerance.
    return unary_union(areas).buffer(-ARC_ERROR_NM, join_style="mitre")


def _turn(geom, x, y, degrees):
    """A local shape in the board's frame at origin (x, y) and rotation `degrees`, by the pad transform."""
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    return affinity.affine_transform(geom, [c, s, -s, c, x, y])


def _place(x, y, degrees, px, py):
    t = math.radians(degrees)
    return x + px * math.cos(t) + py * math.sin(t), y - px * math.sin(t) + py * math.cos(t)


def _normal(degrees):
    """An angle as pcbnew keeps a footprint's: in (-180, 180]."""
    a = math.fmod(degrees, 360.0)
    if a <= -180.0:
        a += 360.0
    elif a > 180.0:
        a -= 360.0
    return 0.0 if a == 0 else a


class PAD:
    def __init__(self, fp, node):
        self.fp, self.node = fp, node
        self.number = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else ""
        self.kind = str(node[2])
        self.shape = str(node[3])
        at = kid(node, "at")
        self.local = (_num(at, 1) * 1e6, _num(at, 2) * 1e6)
        self.angle = float(at[3]) if len(at) > 3 else 0.0       # in the library: the footprint unturned
        size = kid(node, "size")
        self.w, self.h = _num(size, 1) * 1e6, _num(size, 2) * 1e6
        layers = kid(node, "layers")
        self.layers = [str(x) for x in (layers[1:] if layers else [])]
        offset = None
        drill = kid(node, "drill")
        if drill:
            offset = kid(drill, "offset")
        self.offset = _xy(offset) if offset else (0.0, 0.0)
        self.outline = self._outline()

    def _outline(self):
        """The pad's copper in its own frame, at its own origin, nm."""
        w, h, shape = self.w, self.h, self.shape
        ox, oy = self.offset
        if shape in ("rect", "trapezoid"):
            g = box(-w / 2, -h / 2, w / 2, h / 2)
            if shape == "trapezoid":
                delta = kid(self.node, "rect_delta")
                if delta and (float(delta[1]) or float(delta[2])):
                    raise SystemExit("%s.%s is a trapezoid pad, which fpmodel doesn't draw yet" % (self.fp.reference, self.number))
        elif shape == "roundrect":
            ratio = kid(self.node, "roundrect_rratio")
            r = min(w, h) * (float(ratio[1]) if ratio else 0.25)
            core = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r) if r > 0 else box(-w / 2, -h / 2, w / 2, h / 2)
            g = core.buffer(r, quad_segs=max(1, _segments(r, angle=90.0))) if r > 0 else core
        elif shape == "circle":
            g = Point(0, 0).buffer(w / 2, quad_segs=max(1, _segments(w / 2, angle=90.0)))
        elif shape == "oval":
            if w == h:
                g = Point(0, 0).buffer(w / 2, quad_segs=max(1, _segments(w / 2, angle=90.0)))
            elif w > h:
                g = LineString([(-(w - h) / 2, 0), ((w - h) / 2, 0)]).buffer(h / 2, quad_segs=max(1, _segments(h / 2, angle=90.0)))
            else:
                g = LineString([(0, -(h - w) / 2), (0, (h - w) / 2)]).buffer(w / 2, quad_segs=max(1, _segments(w / 2, angle=90.0)))
        elif shape == "custom":
            options = kid(self.node, "options")
            anchor = str((kid(options, "anchor") or ["", "rect"])[1]) if options else "rect"
            parts = [Point(0, 0).buffer(w / 2) if anchor == "circle" else box(-w / 2, -h / 2, w / 2, h / 2)]
            prims = kid(self.node, "primitives")
            for p in (prims[1:] if prims else []):
                g = _shape_outline(p)
                if g is None:
                    raise SystemExit("%s.%s has a custom pad primitive fpmodel doesn't draw: %s" % (self.fp.reference, self.number, p[0]))
                width = _width(p)
                filled = kid(p, "fill")
                if isinstance(g, LineString) or (filled is not None and str(filled[1]) in ("no", "none")):
                    g = (g.exterior if isinstance(g, Polygon) else g).buffer(width / 2)
                elif width:
                    g = g.buffer(width / 2)
                parts.append(g)
            g = unary_union(parts)
        else:
            raise SystemExit("%s.%s has a pad shape fpmodel doesn't draw: %s" % (self.fp.reference, self.number, shape))
        return affinity.translate(g, ox, oy)

    def GetNumber(self):
        return self.number

    def _xy(self):
        return _place(self.fp.x, self.fp.y, self.fp.rot, *self.local)

    def GetPosition(self):
        return VECTOR2I(*self._xy())

    def GetOrientationDegrees(self):
        return _normal(self.angle + self.fp.rot)

    def GetBoundingBox(self):
        """The pad's box on the board. Exact for the shapes KiCad builds from rectangles, round ends and round corners, as
        its effective shape gives it; a custom pad's comes from its drawn outline."""
        x, y = self._xy()
        a = self.angle + self.fp.rot
        w, h, ox, oy = self.w, self.h, self.offset[0], self.offset[1]
        if self.shape in ("rect", "trapezoid", "roundrect", "circle", "oval"):
            if self.shape == "circle" or (self.shape == "oval" and w == h):
                r, core = w / 2, [(ox, oy)]
            elif self.shape == "oval":
                r = min(w, h) / 2
                core = [(ox - (w - h) / 2, oy), (ox + (w - h) / 2, oy)] if w > h else [(ox, oy - (h - w) / 2), (ox, oy + (h - w) / 2)]
            else:
                ratio = kid(self.node, "roundrect_rratio") if self.shape == "roundrect" else None
                r = min(w, h) * (float(ratio[1]) if ratio else 0.25) if self.shape == "roundrect" else 0.0
                core = [(ox + sx * (w / 2 - r), oy + sy * (h / 2 - r)) for sx in (-1, 1) for sy in (-1, 1)]
            pts = [_place(x, y, a, cx, cy) for cx, cy in core]
            return BOX2I(min(p[0] for p in pts) - r, min(p[1] for p in pts) - r, max(p[0] for p in pts) + r, max(p[1] for p in pts) + r)
        return _bbox(_turn(self.outline, x, y, a))

    def IsOnLayer(self, layer):
        names = {F_Cu: "F.Cu", B_Cu: "B.Cu"}
        name = names.get(layer)
        return name is not None and any(lyr in (name, "*.Cu", "F&B.Cu") for lyr in self.layers)


class FOOTPRINT:
    """One footprint from a library, at a pose. Placement poses it; nothing here touches a board."""

    def __init__(self, lib, name, text, path=""):
        self.lib, self.name, self.text, self.path = lib, name, text, path
        self.tree, _ = sexp(text)
        if str(self.tree[0]) not in ("footprint", "module"):
            raise SystemExit("%s isn't a footprint" % path)
        layer = kid(self.tree, "layer")
        if layer and str(layer[1]) != "F.Cu":
            raise SystemExit("%s:%s sits on %s in its library; fpmodel places footprints on the front only" % (lib, name, layer[1]))
        self.reference, self.value = "", ""
        self.x = self.y = 0.0
        self.rot = 0.0
        self.pads = [PAD(self, p) for p in kids(self.tree, "pad")]
        graphics = [g for g in self.tree if isinstance(g, list) and g and str(g[0]).startswith("fp_") and g[0] != "fp_text"]
        self.courtyard = _closed_outline([g for g in graphics if _layer(g) == "F.CrtYd"])
        drawn = []
        for g in graphics:
            shape = _shape_outline(g)
            if shape is not None:
                drawn.append(shape.buffer(_width(g) / 2) if _width(g) else shape)
        self.drawing = unary_union(drawn + [_turn(p.outline, *p.local, p.angle) for p in self.pads])

    # identity
    def GetReference(self):
        return self.reference

    def SetReference(self, ref):
        self.reference = ref

    def GetValue(self):
        return self.value

    def SetValue(self, value):
        self.value = value

    # pose
    def GetPosition(self):
        return VECTOR2I(self.x, self.y)

    def SetPosition(self, p):
        self.x, self.y = float(p.x), float(p.y)

    def GetOrientationDegrees(self):
        return _normal(self.rot)

    def SetOrientationDegrees(self, degrees):
        self.rot = _normal(degrees)

    def Rotate(self, pivot, angle):
        """Turn the footprint about a point: its origin moves round the point, and its rotation grows by the angle."""
        self.x, self.y = _place(pivot.x, pivot.y, angle.degrees, self.x - pivot.x, self.y - pivot.y)
        self.rot = _normal(self.rot + angle.degrees)

    def IsFlipped(self):
        return False

    # geometry
    def Pads(self):
        return list(self.pads)

    def BuildCourtyardCaches(self):
        pass

    def GetCourtyard(self, layer):
        if layer != F_CrtYd or self.courtyard.is_empty:
            return SHAPE_POLY_SET()
        return SHAPE_POLY_SET(_turn(self.courtyard, self.x, self.y, self.rot))

    def GetBoundingBox(self, include_text=False):
        """Pads and drawing, without text, as pcbnew's GetBoundingBox(False)."""
        if self.drawing.is_empty:
            return BOX2I(self.x, self.y, self.x, self.y)
        return _bbox(_turn(self.drawing, self.x, self.y, self.rot))


def load(fpid, dirs):
    """A footprint by its library:name, from the first of `dirs` that has library.pretty with it."""
    if ":" not in fpid:
        raise SystemExit("footprint %s has no library: write it as library:name" % fpid)
    lib, name = fpid.split(":", 1)
    for root in dirs:
        path = os.path.join(root, lib + ".pretty", name + ".kicad_mod")
        if os.path.isfile(path):
            return FOOTPRINT(lib, name, open(path, encoding="utf-8").read(), path)
    raise SystemExit("footprint not found: %s" % fpid)


# ---------------------------------------------------------------- writing a placed footprint into a board

def _mm(nm):
    t = ("%.6f" % (nm / 1e6)).rstrip("0").rstrip(".")
    return "0" if t in ("-0", "") else t


def _deg(a):
    """An angle as KiCad writes a pad's or a text's on a board: [0, 360), none for 0."""
    a = math.fmod(a, 360.0)
    a = a + 360.0 if a < 0 else a
    a = round(a, 6)
    return "" if a in (0.0, 360.0) else " " + (("%.6f" % a).rstrip("0").rstrip("."))


def _quote(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _turned_at(text, node, rot):
    """An (at x y [a]) node rewritten with `rot` added to its angle, its position untouched."""
    body = text[node[1] + 1:node[2] - 1].split()
    x, y = body[1], body[2]
    a = float(body[3]) if len(body) > 3 and body[3] != "unlocked" else 0.0
    return "(at %s %s%s)" % (x, y, _deg(a + rot))


def board_footprint(fp, nets, uuid_of, path, sheetname, sheetfile, mpn, dnp):
    """The footprint's library text as KiCad writes it on a board: named library:name, at its pose, its pads on their
    nets and their angles and its texts' turned with it (KiCad keeps both on the board's axes), its own zones moved
    into the board's frame, and the fields pcbnew's build_board adds. Everything else is the library's, unchanged."""
    from scripts.kicad_pcb import spans
    text = fp.text
    root, _ = spans(text)
    rot = fp.rot
    edits = []
    head = text.index(_quote(fp.name), root[1])
    edits.append((head, head + len(_quote(fp.name)), _quote("%s:%s" % (fp.lib, fp.name))))
    props = []
    for node in root[3]:
        tag = node[0]
        if tag in ("version", "generator", "generator_version"):
            edits.append((node[1], node[2], ""))
        elif tag == "layer":
            edits.append((node[2], node[2], '\n\t\t(uuid "%s")\n\t\t(at %s %s%s)' % (uuid_of(), _mm(fp.x), _mm(fp.y), _deg(rot))))
        elif tag in ("property", "fp_text"):
            at = next((c for c in node[3] if c[0] == "at"), None)
            if at is not None:
                edits.append((at[1], at[2], _turned_at(text, at, rot)))
            if tag == "property":
                props.append(node)
        elif tag == "pad":
            at = next(c for c in node[3] if c[0] == "at")
            edits.append((at[1], at[2], _turned_at(text, at, rot)))
            m = re.match(r'\(pad\s+("(?:[^"\\]|\\.)*"|[^\s()"]+)', text[node[1]:node[2]])
            number = m.group(1)[1:-1] if m and m.group(1).startswith('"') else (m.group(1) if m else "")
            net = nets.get(number)
            layers = next((c for c in node[3] if c[0] == "layers"), None)
            if net and layers is not None:
                edits.append((layers[2], layers[2], "\n\t\t\t(net %s)" % _quote(net)))
        elif tag == "zone":
            for xy in _all(node, "xy"):
                x, y = (float(v) * 1e6 for v in text[xy[1] + 1:xy[2] - 1].split()[1:3])
                bx, by = _place(fp.x, fp.y, rot, x, y)
                edits.append((xy[1], xy[2], "(xy %s %s)" % (_mm(bx), _mm(by))))
        elif tag == "attr" and dnp:
            edits.append((node[2] - 1, node[2] - 1, " dnp"))
    # Reference and Value say this part's; the fields pcbnew's build_board adds follow the library's own.
    for node in props:
        m = re.match(r'\(property\s+("(?:[^"\\]|\\.)*")\s+("(?:[^"\\]|\\.)*")', text[node[1]:node[2]])
        name = m.group(1)[1:-1]
        if name in ("Reference", "Value"):
            start = node[1] + m.start(2)
            edits.append((start, start + len(m.group(2)), _quote(fp.reference if name == "Reference" else fp.value)))
    after = props[-1][2] if props else next(n for n in root[3] if n[0] == "layer")[2]
    added = ('\n\t\t(property "MPN" %s (at %s %s) (layer "F.Fab") (hide yes) (uuid "%s") (effects (font (size 1 1) (thickness 0.15))))'
             '\n\t\t(path %s)\n\t\t(sheetname %s)\n\t\t(sheetfile %s)') % (_quote(mpn), _mm(fp.x * 0), _mm(fp.y * 0), uuid_of(),
                                                                        _quote(path), _quote(sheetname), _quote(sheetfile))
    if dnp and not any(n[0] == "attr" for n in root[3]):
        added += "\n\t\t(attr dnp)"
    edits.append((after, after, added))
    for a, b, new in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        text = text[:a] + new + text[b:]
    return "\t" + text.strip().replace("\n", "\n\t")


def _all(node, tag):
    out = []
    for c in node[3]:
        if c[0] == tag:
            out.append(c)
        out += _all(c, tag)
    return out


def outline_text(w, h, uuid):
    return ('\t(gr_rect (start 0 0) (end %s %s) (stroke (width 0.1) (type default)) (fill no) (layer "Edge.Cuts") (uuid "%s"))'
            % (_mm(w * 1e6), _mm(h * 1e6), uuid))


def pour_text(net, layer, x1, y1, x2, y2, clearance, uuid):
    """A pour as pcbnew's _pour makes one: solid pad connection, the clearance given, 0.15 mm minimum width, islands always
    removed."""
    pts = " ".join("(xy %s %s)" % (_mm(x * 1e6), _mm(y * 1e6)) for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)))
    return ('\t(zone (net %s) (layer %s) (uuid "%s") (hatch edge 0.5) (connect_pads yes (clearance %s)) (min_thickness 0.15) '
            '(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0)) (polygon (pts %s)))'
            % (_quote(net), _quote(layer), uuid, _mm(clearance * 1e6), pts))
