"""A board read from its .kicad_pcb, answering the part of pcbnew's API export_eagle.py asks, with no KiCad.

KiCad 11 ships no pcbnew module. export_eagle.py only reads a board: its footprints and their pads and drawings, its
own drawings, tracks, vias and zones, and its default clearance. This module reads them from the file, in pcbnew's
units (nm) and frames: a pad's position on the board through the pad transform of scripts/kicad_pcb.py, and relative
to its footprint as the file stores it. export_eagle.py imports it as `pcbnew` where the real one isn't there.

LoadBoard(path, PCB_IO_MGR.EAGLE), the round trip, has kicad-cli import the EAGLE file ($EDALOOP_KICAD_CLI, else
kicad-cli on PATH) and reads what it writes.

What it can't read as pcbnew would is refused rather than guessed: a footprint pose it can't apply (see
footprint_pose), a pad shape it doesn't know.

Runs under Python 3.9, as export_eagle.py may.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile

from scripts.kicad_pcb import footprint_pose, kid, kids, sexp

# ------------------------------------------------------------- pcbnew's names
_LAYER_NAMES = ["F.Cu"] + ["In%d.Cu" % i for i in range(1, 31)] + ["B.Cu", "F.Adhes", "B.Adhes", "F.Paste", "B.Paste",
                                                                    "F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "Dwgs.User",
                                                                    "Cmts.User", "Eco1.User", "Eco2.User", "Edge.Cuts",
                                                                    "Margin", "F.CrtYd", "B.CrtYd", "F.Fab", "B.Fab"]
_LAYER = {name: i for i, name in enumerate(_LAYER_NAMES)}
_COPPER = [n for n in _LAYER_NAMES if n.endswith(".Cu")]
for _name, _i in _LAYER.items():
    globals()[_name.replace(".", "_")] = _i

PAD_ATTRIB_PTH, PAD_ATTRIB_SMD, PAD_ATTRIB_CONN, PAD_ATTRIB_NPTH = range(4)
PAD_SHAPE_CIRCLE, PAD_SHAPE_RECTANGLE, PAD_SHAPE_OVAL, PAD_SHAPE_TRAPEZOID, PAD_SHAPE_ROUNDRECT, \
    PAD_SHAPE_CHAMFERED_RECT, PAD_SHAPE_CUSTOM = range(7)
SHAPE_T_SEGMENT, SHAPE_T_RECTANGLE, SHAPE_T_ARC, SHAPE_T_CIRCLE, SHAPE_T_POLY, SHAPE_T_BEZIER = range(6)
ZONE_CONNECTION_INHERITED, ZONE_CONNECTION_NONE, ZONE_CONNECTION_THERMAL, ZONE_CONNECTION_FULL, ZONE_CONNECTION_THT_THERMAL = range(5)
VIATYPE_THROUGH, VIATYPE_BLIND_BURIED, VIATYPE_MICROVIA = range(3)
VIATYPE_BLIND = VIATYPE_BURIED = VIATYPE_BLIND_BURIED
ERROR_INSIDE, ERROR_OUTSIDE = range(2)
DEFAULT_CLEARANCE_NM = 200000          # KiCad's Default net class, where a board has no project to say otherwise


class PCB_IO_MGR:
    KICAD_SEXP, EAGLE = "kicad", "eagle"


def ToMM(nm):
    return nm / 1e6


def FromMM(mm):
    return int(round(mm * 1e6))


class VECTOR2I:
    def __init__(self, x=0, y=0):
        self.x, self.y = int(round(x)), int(round(y))

    def __sub__(self, other):
        return VECTOR2I(self.x - other.x, self.y - other.y)


class BOX2I:
    def __init__(self, x1, y1, x2, y2):
        self.l, self.t, self.r, self.b = int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))

    def GetLeft(self):
        return self.l

    def GetTop(self):
        return self.t

    def GetRight(self):
        return self.r

    def GetBottom(self):
        return self.b


class _Angle:
    def __init__(self, degrees):
        self.degrees = degrees

    def AsDegrees(self):
        return self.degrees


class _Uuid:
    def __init__(self, value):
        self.value = value

    def AsString(self):
        return self.value


class _Chain:
    def __init__(self, pts):
        self.pts = pts

    def PointCount(self):
        return len(self.pts)

    def CPoint(self, i):
        return VECTOR2I(*self.pts[i])


class _Outline:
    def __init__(self, outlines):
        self.outlines = outlines

    def Outline(self, i):
        return _Chain(self.outlines[i])

    def OutlineCount(self):
        return len(self.outlines)


class _LayerSet:
    def __init__(self, ids):
        self.ids = ids

    def Seq(self):
        return list(self.ids)


class SHAPE_POLY_SET:
    """Points only: the round trip asks for its box, and a box needs no polygon arithmetic."""

    def __init__(self):
        self.pts = []

    def BBox(self):
        xs, ys = [p[0] for p in self.pts], [p[1] for p in self.pts]
        return BOX2I(min(xs), min(ys), max(xs), max(ys)) if self.pts else BOX2I(0, 0, 0, 0)


# ------------------------------------------------------------- reading
def _f(node, i=1):
    return float(node[i])


def _xy(node):
    return _f(node, 1) * 1e6, _f(node, 2) * 1e6


def _val(node, tag, default=None):
    k = kid(node, tag)
    return k[1] if k is not None and len(k) > 1 else default


def _layers(names):
    """Layer names, with KiCad's wildcards spread out, as ids."""
    out = []
    for n in names:
        n = str(n)
        if n in ("*.Cu", "F&B.Cu"):
            out += [_LAYER[c] for c in (_COPPER if n == "*.Cu" else ["F.Cu", "B.Cu"])]
        elif n.startswith("*."):
            out += [_LAYER[side + n[1:]] for side in ("F", "B") if side + n[1:] in _LAYER]
        elif n in _LAYER:
            out.append(_LAYER[n])
    return out


def _place(fx, fy, rot, x, y):
    t = math.radians(rot)
    return fx + x * math.cos(t) + y * math.sin(t), fy - x * math.sin(t) + y * math.cos(t)


def _norm(a):
    a = math.fmod(a, 360.0)
    if a <= -180.0:
        a += 360.0
    elif a > 180.0:
        a -= 360.0
    return a


def _net(node, numbered):
    n = kid(node, "net")
    if n is None:
        return str(_val(node, "net_name", "") or "")
    name = n[-1] if len(n) > 1 else ""
    return str(numbered.get(name, name))


class _Shape:
    """A drawing, a footprint's or the board's, in board coordinates."""

    def __init__(self, node, to_board=None, rot=0.0, names=None):
        self.node, self.tag = node, str(node[0])
        self.names = names or {}
        self.kind = self.tag.split("_", 1)[1]
        self.layer_name = str(_val(node, "layer", ""))
        self.to_board = to_board or (lambda x, y: (x, y))
        self.rot = rot
        stroke = kid(node, "stroke")
        width = kid(stroke, "width") if stroke is not None else kid(node, "width")
        self.width = _f(width) * 1e6 if width is not None else 0.0
        fill = kid(node, "fill")
        self.filled = fill is not None and len(fill) > 1 and str(fill[1]) in ("solid", "yes")

    def GetClass(self):
        return "PCB_TEXT" if self.kind == "text" else "PCB_SHAPE"

    def GetLayer(self):
        return _LAYER.get(self.layer_name, -1)

    def GetLayerName(self):
        return self.names.get(self.layer_name, self.layer_name)

    def GetWidth(self):
        return int(round(self.width))

    def IsAnyFill(self):
        return self.filled

    def _pt(self, tag):
        return VECTOR2I(*self.to_board(*_xy(kid(self.node, tag))))

    def _rect_corners(self):
        (x1, y1), (x2, y2) = _xy(kid(self.node, "start")), _xy(kid(self.node, "end"))
        return [self.to_board(x, y) for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2))]

    def GetShape(self):
        if self.kind == "line":
            return SHAPE_T_SEGMENT
        if self.kind == "rect":
            # A rectangle turned to an angle that isn't a quarter is a polygon to pcbnew.
            return SHAPE_T_RECTANGLE if abs(math.fmod(self.rot, 90.0)) < 1e-9 else SHAPE_T_POLY
        if self.kind == "circle":
            return SHAPE_T_CIRCLE
        if self.kind == "arc":
            return SHAPE_T_ARC
        if self.kind == "poly":
            return SHAPE_T_POLY
        if self.kind == "curve":
            return SHAPE_T_BEZIER
        return -1

    def GetShapeStr(self):
        return {"line": "Line", "rect": "Rect", "arc": "Arc", "circle": "Circle", "poly": "Polygon", "curve": "Bezier"}.get(self.kind, self.kind)

    def GetStart(self):
        return self._pt("start")     # a rectangle's too: pcbnew turns its two corners with the footprint, unsorted

    def GetEnd(self):
        return self._pt("end")

    def GetCenter(self):
        return self._pt("center")

    def GetRadius(self):
        c, e = _xy(kid(self.node, "center")), _xy(kid(self.node, "end"))
        return int(round(math.hypot(e[0] - c[0], e[1] - c[1])))

    def GetPolyShape(self):
        if self.kind == "rect":
            return _Outline([self._rect_corners()])
        pts = [self.to_board(*_xy(p)) for p in kids(kid(self.node, "pts"), "xy")]
        return _Outline([pts])

    # text
    def GetPosition(self):
        return self._pt("at")

    def _font(self, tag):
        effects = kid(self.node, "effects")
        font = kid(effects, "font") if effects is not None else None
        node = kid(font, tag) if font is not None else None
        return node

    def GetTextHeight(self):
        size = self._font("size")
        return FromMM(_f(size, 1)) if size is not None else FromMM(1.0)

    def GetTextThickness(self):
        t = self._font("thickness")
        return FromMM(_f(t)) if t is not None else FromMM(0.15)

    def GetTextAngleDegrees(self):
        at = kid(self.node, "at")
        return float(at[3]) if len(at) > 3 and at[3] not in ("unlocked",) else 0.0

    def GetText(self):
        return str(self.node[2]) if self.tag == "fp_text" else str(self.node[1])

    def TransformShapeToPolygon(self, ps, layer, clearance, max_error, error_loc):
        """The ink: the shape's points, grown by half its stroke. Only the box of it is ever asked for."""
        half = self.width / 2.0 + clearance
        if self.kind == "circle":
            c = self.GetCenter()
            r = self.GetRadius() + half
            ps.pts += [(c.x - r, c.y - r), (c.x + r, c.y + r)]
            return
        if self.kind in ("line", "arc"):
            pts = [(p.x, p.y) for p in (self.GetStart(), self.GetEnd())]
        else:
            pts = self.GetPolyShape().outlines[0]
        ps.pts += [(x + dx, y + dy) for x, y in pts for dx in (-half, half) for dy in (-half, half)]


class _Pad:
    def __init__(self, fp, node, index):
        self.fp, self.node = fp, node
        self.number = str(node[1])
        self.kind, self.shape = str(node[2]), str(node[3])
        at = kid(node, "at")
        self.local = _xy(at)
        self.angle = float(at[3]) if len(at) > 3 else 0.0          # on the board, as the file keeps a pad's
        size = kid(node, "size")
        self.size = (_f(size, 1) * 1e6, _f(size, 2) * 1e6)
        drill = kid(node, "drill")
        self.drill = (0.0, 0.0)
        if drill is not None:
            nums = [x for x in drill[1:] if not isinstance(x, list) and x != "oval"]
            if nums:
                d = [float(v) * 1e6 for v in nums]
                self.drill = (d[0], d[1] if len(d) > 1 else d[0])
        self.layer_ids = _layers((kid(node, "layers") or ["layers"])[1:])
        uid = _val(node, "uuid") or _val(node, "tstamp")
        self.uuid = str(uid) if uid else "%s-pad-%d" % (fp.GetReference(), index)
        if self.shape not in ("circle", "rect", "oval", "trapezoid", "roundrect", "chamfered_rect", "custom"):
            raise SystemExit("%s pad %s: a shape boardread doesn't read: %s" % (fp.GetReference(), self.number, self.shape))

    def GetNumber(self):
        return self.number

    def GetAttribute(self):
        return {"thru_hole": PAD_ATTRIB_PTH, "smd": PAD_ATTRIB_SMD, "connect": PAD_ATTRIB_CONN,
                "np_thru_hole": PAD_ATTRIB_NPTH}[self.kind]

    def GetShape(self):
        return {"circle": PAD_SHAPE_CIRCLE, "rect": PAD_SHAPE_RECTANGLE, "oval": PAD_SHAPE_OVAL, "trapezoid": PAD_SHAPE_TRAPEZOID,
                "roundrect": PAD_SHAPE_ROUNDRECT, "chamfered_rect": PAD_SHAPE_CHAMFERED_RECT, "custom": PAD_SHAPE_CUSTOM}[self.shape]

    def GetFrontRoundRectRadiusRatio(self):
        return float(_val(self.node, "roundrect_rratio", 0.25))

    def GetSize(self, layer=None):
        return VECTOR2I(*self.size)

    def GetDrillSize(self):
        return VECTOR2I(*self.drill)

    def GetFPRelativePosition(self):
        return VECTOR2I(*self.local)

    def GetFPRelativeOrientation(self):
        return _Angle(_norm(self.angle - self.fp.rot))

    def GetOrientationDegrees(self):
        return _norm(self.angle)

    def GetPosition(self):
        return VECTOR2I(*_place(self.fp.x, self.fp.y, self.fp.rot, *self.local))

    def IsOnLayer(self, layer):
        return layer in self.layer_ids

    def GetNetname(self):
        return _net(self.node, self.fp.board.numbered)

    @property
    def m_Uuid(self):
        return _Uuid(self.uuid)


class _Footprint:
    def __init__(self, board, node):
        self.board, self.node = board, node
        self.x, self.y, self.rot = footprint_pose(node)
        self.x, self.y = self.x * 1e6, self.y * 1e6
        self.side = str(_val(node, "layer", "F.Cu"))
        props = {str(p[1]): str(p[2]) for p in kids(node, "property") if len(p) > 2}
        texts = {str(t[1]): str(t[2]) for t in kids(node, "fp_text") if len(t) > 2}
        self.ref = props.get("Reference", texts.get("reference", ""))
        self.value = props.get("Value", texts.get("value", ""))
        self.pads = [_Pad(self, p, i) for i, p in enumerate(kids(node, "pad"))]
        to_board = lambda x, y: _place(self.x, self.y, self.rot, x, y)
        self.drawings = [_Shape(g, to_board, self.rot, board.layer_names) for g in node[1:] if isinstance(g, list) and g and
                         str(g[0]) in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_curve", "fp_text")]

    def GetReference(self):
        return self.ref

    def GetValue(self):
        return self.value

    def GetFPIDAsString(self):
        return str(self.node[1])

    def IsFlipped(self):
        return self.side == "B.Cu"

    def GetOrientationDegrees(self):
        return _norm(self.rot)

    def GetPosition(self):
        return VECTOR2I(self.x, self.y)

    def Pads(self):
        return list(self.pads)

    def GraphicalItems(self):
        return list(self.drawings)


class _Track:
    def __init__(self, board, node):
        self.board, self.node, self.tag = board, node, str(node[0])
        if self.tag == "via":
            vtype = str(node[1]) if len(node) > 1 and isinstance(node[1], str) else str(_val(node, "type", "through"))
            self.via_type = {"micro": VIATYPE_MICROVIA, "blind": VIATYPE_BLIND_BURIED}.get(vtype, VIATYPE_THROUGH)
            lnode = kid(node, "layers")
            self.layer_pair = _layers(lnode[1:3]) if lnode and len(lnode) >= 3 else [_LAYER.get("F.Cu", 0), _LAYER.get("B.Cu", 31)]
        else:
            self.via_type = VIATYPE_THROUGH
            self.layer_pair = []

    def GetViaType(self):
        return self.via_type

    def TopLayer(self):
        return self.layer_pair[0] if self.layer_pair else -1

    def BottomLayer(self):
        return self.layer_pair[-1] if self.layer_pair else -1

    def LayerPair(self):
        return tuple(self.layer_pair[:2]) if len(self.layer_pair) >= 2 else (-1, -1)

    def GetClass(self):
        return {"segment": "PCB_TRACK", "arc": "PCB_ARC", "via": "PCB_VIA"}[self.tag]

    def GetNetname(self):
        return _net(self.node, self.board.numbered)

    def GetLayer(self):
        return _LAYER.get(str(_val(self.node, "layer", "")), -1)

    def GetWidth(self, layer=None):
        return FromMM(float(_val(self.node, "size" if self.tag == "via" else "width", 0)))

    def GetStart(self):
        return VECTOR2I(*_xy(kid(self.node, "start")))

    def GetEnd(self):
        return VECTOR2I(*_xy(kid(self.node, "end")))

    def GetPosition(self):
        return VECTOR2I(*_xy(kid(self.node, "at")))

    def GetDrillValue(self):
        return FromMM(float(_val(self.node, "drill", 0)))

    def GetLength(self):
        if self.tag == "segment":
            s, e = self.GetStart(), self.GetEnd()
            return math.hypot(e.x - s.x, e.y - s.y)
        if self.tag == "arc":
            (ax, ay), (mx, my), (bx, by) = _xy(kid(self.node, "start")), _xy(kid(self.node, "mid")), _xy(kid(self.node, "end"))
            d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
            if abs(d) < 1e-9:
                return math.hypot(bx - ax, by - ay)
            ux = ((ax ** 2 + ay ** 2) * (my - by) + (mx ** 2 + my ** 2) * (by - ay) + (bx ** 2 + by ** 2) * (ay - my)) / d
            uy = ((ax ** 2 + ay ** 2) * (bx - mx) + (mx ** 2 + my ** 2) * (ax - bx) + (bx ** 2 + by ** 2) * (mx - ax)) / d
            r = math.hypot(ax - ux, ay - uy)
            a0, am, a1 = (math.atan2(y - uy, x - ux) for x, y in ((ax, ay), (mx, my), (bx, by)))
            sweep = (a1 - a0) % (2 * math.pi)
            if (am - a0) % (2 * math.pi) > sweep:
                sweep = 2 * math.pi - sweep
            return r * sweep
        return 0.0


class _Zone:
    def __init__(self, board, node):
        self.board, self.node = board, node
        names = (kid(node, "layers") or kid(node, "layer") or ["layer"])[1:]
        self.layer_ids = _layers(names)
        self.keepout = kid(node, "keepout")

    def GetIsRuleArea(self):
        return self.keepout is not None

    def _allowed(self, what):
        return str(_val(self.keepout, what, "allowed")) != "not_allowed" if self.keepout is not None else True

    def GetDoNotAllowTracks(self):
        return not self._allowed("tracks")

    def GetDoNotAllowVias(self):
        return not self._allowed("vias")

    def GetDoNotAllowZoneFills(self):
        return not self._allowed("copperpour")

    def GetDoNotAllowFootprints(self):
        return not self._allowed("footprints")

    def GetLayerSet(self):
        return _LayerSet(self.layer_ids)

    def GetNetname(self):
        return _net(self.node, self.board.numbered)

    def Outline(self):
        return _Outline([[_xy(p) for p in kids(kid(poly, "pts"), "xy")] for poly in kids(self.node, "polygon")])

    def GetPadConnection(self):
        c = kid(self.node, "connect_pads")
        if c is None:
            return ZONE_CONNECTION_THERMAL
        mode = next((str(x) for x in c[1:] if not isinstance(x, list)), "")
        return {"yes": ZONE_CONNECTION_FULL, "no": ZONE_CONNECTION_NONE, "thru_hole_only": ZONE_CONNECTION_THT_THERMAL}.get(mode, ZONE_CONNECTION_THERMAL)

    def GetLocalClearance(self):
        c = kid(self.node, "connect_pads")
        cl = kid(c, "clearance") if c is not None else None
        return FromMM(_f(cl)) if cl is not None else 0

    def GetMinThickness(self):
        return FromMM(float(_val(self.node, "min_thickness", 0.25)))

    def GetAssignedPriority(self):
        return int(float(_val(self.node, "priority", 0) or 0))


class _NetClass:
    def __init__(self, clearance):
        self.clearance = clearance

    def GetClearance(self):
        return self.clearance


class _DesignSettings:
    def __init__(self, clearance):
        self.default = _NetClass(clearance)

    def GetDefault(self):
        return self.default


class BOARD:
    def __init__(self, path):
        self.path = path
        self.root, _ = sexp(open(path, encoding="utf-8").read())
        self.numbered = {str(n[1]): str(n[2]) for n in kids(self.root, "net") if len(n) > 2}
        # A layer's user name where the board's layer table gives one, as pcbnew's GetLayerName answers ("F.Courtyard").
        table = kid(self.root, "layers")
        self.layer_names = {str(r[1]): str(r[3]) for r in (table[1:] if table else []) if isinstance(r, list) and len(r) > 3}
        self.footprints = [_Footprint(self, n) for n in kids(self.root, "footprint")]
        self.drawings = [_Shape(n, names=self.layer_names) for n in self.root[1:] if isinstance(n, list) and n and
                         str(n[0]) in ("gr_line", "gr_rect", "gr_circle", "gr_arc", "gr_poly", "gr_curve", "gr_text")]
        self.tracks = [_Track(self, n) for n in self.root[1:] if isinstance(n, list) and n and str(n[0]) in ("segment", "arc", "via")]
        self.zones = [_Zone(self, n) for n in kids(self.root, "zone")]

    def GetCopperLayerCount(self):
        table = kid(self.root, "layers")
        return sum(1 for row in (table[1:] if table else []) if isinstance(row, list) and len(row) > 1 and str(row[1]).endswith(".Cu"))

    def GetFootprints(self):
        return list(self.footprints)

    Footprints = GetFootprints

    def GetDrawings(self):
        return list(self.drawings)

    def GetTracks(self):
        return list(self.tracks)

    def Zones(self):
        return list(self.zones)

    def GetLayerName(self, layer):
        name = _LAYER_NAMES[layer] if 0 <= layer < len(_LAYER_NAMES) else ""
        return self.layer_names.get(name, name)

    def GetDesignSettings(self):
        """The Default net class's clearance from the project beside the board, as pcbnew loads it, or KiCad's default."""
        project = os.path.splitext(self.path)[0] + ".kicad_pro"
        clearance = DEFAULT_CLEARANCE_NM
        if os.path.exists(project):
            try:
                classes = json.load(open(project))["net_settings"]["classes"]
                default = next(c for c in classes if c.get("name") == "Default")
                clearance = FromMM(float(default["clearance"]))
            except (KeyError, StopIteration, ValueError, TypeError):
                pass
        return _DesignSettings(clearance)

    def GetBoardEdgesBoundingBox(self):
        ps = SHAPE_POLY_SET()
        for d in self.drawings:
            if d.layer_name == "Edge.Cuts":
                d.TransformShapeToPolygon(ps, Edge_Cuts, 0, 0, ERROR_INSIDE)       # noqa: F821 - set from _LAYER above
        return ps.BBox()


def LoadBoard(path, kind=None):
    """A board from its .kicad_pcb; with PCB_IO_MGR.EAGLE, an EAGLE .brd that kicad-cli imports first."""
    if kind == PCB_IO_MGR.EAGLE:
        cli = os.environ.get("EDALOOP_KICAD_CLI") or "kicad-cli"
        out = os.path.join(tempfile.mkdtemp(prefix="edaloop-eagle-"), "imported.kicad_pcb")
        r = subprocess.run([cli, "pcb", "import", "--format", "eagle", "--output", out, path], capture_output=True, text=True)
        if r.returncode or not os.path.exists(out):
            return None
        path = out
    return BOARD(path)
