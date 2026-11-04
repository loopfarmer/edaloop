#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EAGLE 7.7 XML exports of a routed board, for tools that can't read KiCad's own files, such as Altium's Import Wizard.

    KPY export_eagle.py BOARD.kicad_pcb NAME OUT_DIR KICAD_CLI [SCHEMATIC.kicad_sch]

WHY 7.7.  Altium's EAGLE importer states its input as XML files from EAGLE 6.4 through 9.4, and Autodesk's current files are stamped above that.
7.7.0 sits inside the range, and its DTD ships with edaloop in data/ (CadSoft, CC BY-ND 3.0, unmodified), so every file is validated against the
grammar of exactly the version it claims to be. Nothing from 8.x or 9.x is written.

THE TRANSFORM.  KiCad stores y down and EAGLE stores y up, and both show the board from the top, so a plain y negation keeps every on-screen angle.
KiCad flips a footprint about its X axis. EAGLE defines a package top-side and mirrors it with an M on the element's rotation:
    top-side part     rot = R(theta)
    bottom-side part  rot = MR(180 - theta)
with package-local coordinates (u, v) = (x_rel, -y_rel) on top and (x_rel, y_rel) on the bottom. EAGLE's MR(phi) turns clockwise as seen from the top.
A check written from the same derivation as the code can't find the derivation's error, so KiCad's own importer reads each board back. Position
agreement isn't identity: a symmetric ball grid lands on itself when turned 180 degrees with every pad on the wrong net, so the round trip compares
nets pad by pad. One flipped angle can't separate MR(180 - theta) from MR(theta), so bottom-side parts at 0 and 180 degrees are what test it.

WHAT IS CARRIED, AND WHAT ISN'T.
  copper      Every pad, track, via and pour outline, on four layers mapped 1 / 2 / 15 / 16 with layerSetup (1+2*15+16). Pours are outlines, and the
              importer refills them. Only four-layer boards are handled.
  keepouts    Rule areas go to restrict layers 41/42/43 and parts to 39/40. EAGLE has no inner-layer keepout, so inner layers get pour cutouts.
  outline     Edge.Cuts to layer 20, including internal slots and holes, which EAGLE reads as cutouts.
  silk, fab   21/22 and 51/52. Solder-mask graphics go to 29/30.
  paste       Subdivided paste, stored by KiCad as paste-only pads, becomes tCream rectangles, and the copper pad is written cream="no".
  courtyard   Not carried. EAGLE has no courtyard layer, and tKeepout means something stronger in Altium's importer.
  schematic   A connectivity carrier, not a drawing: one boxed symbol per package, with every pin on a labelled stub. It lets an importer link the
              schematic and board with named nets.

Exits non-zero if any check fails.
"""
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

import pcbnew

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pcbfile import parse_sexpr  # noqa: E402

VERSION = "7.7.0"
DTD = os.path.join(HERE, "data", "eagle-7.7.0.dtd")
CLI = "kicad-cli"
TOL = 0.001          # mm.  KiCad is nm; EAGLE is written to 0.1 um.
GRID = 2.54          # schematic grid; pins land on it or EAGLE will not join

CU = {pcbnew.F_Cu: 1, pcbnew.In1_Cu: 2, pcbnew.In2_Cu: 15, pcbnew.B_Cu: 16}
GFX = {pcbnew.F_SilkS: 21, pcbnew.B_SilkS: 22, pcbnew.F_Fab: 51,
       pcbnew.B_Fab: 52, pcbnew.F_Mask: 29, pcbnew.B_Mask: 30,
       pcbnew.F_Paste: 31, pcbnew.B_Paste: 32}
SWAP = {21: 22, 22: 21, 51: 52, 52: 51, 29: 30, 30: 29, 31: 32, 32: 31,
        39: 40, 40: 39}

LAYERS = ([(1, "Top", 4)] + [(n, "Route%d" % n, 1) for n in range(2, 16)] +
          [(16, "Bottom", 1), (17, "Pads", 2), (18, "Vias", 2),
           (19, "Unrouted", 6), (20, "Dimension", 15), (21, "tPlace", 7),
           (22, "bPlace", 7), (23, "tOrigins", 15), (24, "bOrigins", 15),
           (25, "tNames", 7), (26, "bNames", 7), (27, "tValues", 7),
           (28, "bValues", 7), (29, "tStop", 7), (30, "bStop", 7),
           (31, "tCream", 7), (32, "bCream", 7), (35, "tGlue", 7),
           (36, "bGlue", 7), (39, "tKeepout", 4), (40, "bKeepout", 1),
           (41, "tRestrict", 4), (42, "bRestrict", 1), (43, "vRestrict", 2),
           (44, "Drills", 7), (45, "Holes", 7), (46, "Milling", 3),
           (47, "Measures", 7), (48, "Document", 7), (49, "Reference", 7),
           (51, "tDocu", 7), (52, "bDocu", 7), (91, "Nets", 2),
           (92, "Busses", 1), (93, "Pins", 2), (94, "Symbols", 4),
           (95, "Names", 7), (96, "Values", 7), (97, "Info", 7),
           (98, "Guide", 6)])
ACTIVE_CU = {1, 2, 15, 16}


# ------------------------------------------------------------- numbers
def f(v):
    """mm -> EAGLE string, 0.1 um resolution, no trailing zeros."""
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "", "-") else s


def mm(v):
    return pcbnew.ToMM(v)


def rotk(x, y, deg):
    """KiCad's rotation: y down, positive angles counter-clockwise on screen."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def angdiff(a, b):
    d = (a - b) % 360.0
    return min(d, 360.0 - d)


class Place(object):
    """One footprint instance: KiCad absolute <-> EAGLE package-local."""

    def __init__(self, fp):
        self.flip = fp.IsFlipped()
        self.theta = fp.GetOrientationDegrees()
        p = fp.GetPosition()
        self.px, self.py = mm(p.x), mm(p.y)

    def local(self, X, Y):
        lx, ly = rotk(X - self.px, Y - self.py, -self.theta)
        return (lx, ly) if self.flip else (lx, -ly)

    def angle(self, a_rel):
        return (-a_rel if self.flip else a_rel) % 360.0

    def rot(self):
        if self.flip:
            return "MR" + f((180.0 - self.theta) % 360.0)
        return "R" + f(self.theta % 360.0)

    def eagle_abs(self, u, v):
        """Where EAGLE puts a package point for the rotation written above.

        MR(phi) mirrors x and turns CLOCKWISE by phi as seen from the top
        -- counter-clockwise seen from the bottom, where the part is.  That
        reading was established by KiCad's own importer on a real board,
        not by this function: the first version of it modelled the
        opposite sense, agreed with the first version of rot() to 0.0000
        um, and was wrong by half a turn.  See the module docstring."""
        if self.flip:
            ang, x, y = -math.radians((180.0 - self.theta) % 360.0), -u, v
        else:
            ang, x, y = math.radians(self.theta % 360.0), u, v
        c, s = math.cos(ang), math.sin(ang)
        return self.px + x * c - y * s, -self.py + x * s + y * c


# ------------------------------------------------------------- packages
def pad_items(fp, pl, report):
    """-> [item], {pad uuid: eagle pad name}, [(uuid, number, u, v)]"""
    rows = []
    for p in fp.Pads():
        r = p.GetFPRelativePosition()
        u, v = (mm(r.x), mm(r.y)) if pl.flip else (mm(r.x), -mm(r.y))
        rows.append((p.GetNumber(), round(u, 3), round(v, 3), u, v, p))
    rows.sort(key=lambda t: (t[0], t[1], t[2]))
    items, names, seen = [], {}, Counter()
    side_cu = pcbnew.B_Cu if pl.flip else pcbnew.F_Cu
    side_mask = pcbnew.B_Mask if pl.flip else pcbnew.F_Mask
    side_paste = pcbnew.B_Paste if pl.flip else pcbnew.F_Paste
    for num, _, _, u, v, p in rows:
        attr = p.GetAttribute()
        rot = pl.angle(p.GetFPRelativeOrientation().AsDegrees())
        sz = p.GetSize()
        sx, sy = mm(sz.x), mm(sz.y)
        uid = p.m_Uuid.AsString()
        copper = any(p.IsOnLayer(l) for l in CU)
        if attr == pcbnew.PAD_ATTRIB_NPTH:
            items.append(("hole", u, v, mm(p.GetDrillSize().x)))
            continue
        if not copper:
            # a paste-only (or mask-only) aperture -- see the docstring
            for lay, lid in ((31, side_paste), (29, side_mask)):
                if p.IsOnLayer(lid):
                    items.append(("rect", lay, u - sx / 2, v - sy / 2,
                                  u + sx / 2, v + sy / 2, rot))
            report["aperture pads -> cream/stop rectangles"] += 1
            continue
        base = num or "~"
        name = base if seen[base] == 0 else "%s@%d" % (base, seen[base])
        seen[base] += 1
        names[uid] = name
        sh = p.GetShape()
        if attr == pcbnew.PAD_ATTRIB_PTH:
            drill = mm(p.GetDrillSize().x)
            if abs(sx - sy) < TOL:
                shape = "square" if sh == pcbnew.PAD_SHAPE_RECTANGLE else "round"
                dia, prot = sx, rot
            else:
                # EAGLE 7 HAS ONE ELONGATED THROUGH-HOLE PAD, "long": an
                # obround along its own x, whose LENGTH is not a pad
                # attribute at all but the board-wide psElongationLong.
                # So the long axis goes on x (turn it if KiCad's is y), the
                # width is the short side, and build() sets the elongation
                # from the board's own long pads.  A rounded-rectangle or
                # rectangular through-hole pad cannot be expressed and
                # becomes an obround of the same extents -- recorded, not
                # hidden: see build() and the round trip.
                shape, dia = "long", min(sx, sy)
                prot = rot if sx >= sy else (rot + 90.0) % 360.0
            report.setdefault("_tht", []).append(
                (fp.GetReference(), num, sx, sy, corner(p), shape, dia))
            items.append(("pad", name, u, v, drill, dia, shape, prot))
            continue
        if not p.IsOnLayer(side_cu):
            report["SMD pad on the far side of its footprint"] += 1
        if sh == pcbnew.PAD_SHAPE_ROUNDRECT:
            rnd = int(round(p.GetFrontRoundRectRadiusRatio() * 200))
        elif sh in (pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_OVAL):
            rnd = 100
        elif sh == pcbnew.PAD_SHAPE_RECTANGLE:
            rnd = 0
        else:
            raise NotImplementedError("pad shape %d on %s" % (sh, fp.GetReference()))
        items.append(("smd", name, u, v, sx, sy, rot, max(0, min(100, rnd)),
                      p.IsOnLayer(side_mask), p.IsOnLayer(side_paste)))
    return items, names


def gfx_items(fp, pl, report):
    items = []
    for g in fp.GraphicalItems():
        if g.GetClass() != "PCB_SHAPE":
            continue
        lay = GFX.get(g.GetLayer())
        if lay is None:
            report["footprint graphics not carried (%s)" % g.GetLayerName()] += 1
            continue
        if pl.flip:
            lay = SWAP[lay]
        w = mm(g.GetWidth())
        st = g.GetShape()
        L = lambda pt: pl.local(mm(pt.x), mm(pt.y))
        if st == pcbnew.SHAPE_T_SEGMENT:
            a, b = L(g.GetStart()), L(g.GetEnd())
            items.append(("wire", lay, a[0], a[1], b[0], b[1], w))
        elif st == pcbnew.SHAPE_T_RECTANGLE:
            s, e = g.GetStart(), g.GetEnd()
            pts = [L(pcbnew.VECTOR2I(x, y)) for x, y in
                   ((s.x, s.y), (e.x, s.y), (e.x, e.y), (s.x, e.y))]
            if g.IsAnyFill():
                items.append(("poly", lay, w, pts))
            else:
                for i in range(4):
                    a, b = pts[i], pts[(i + 1) % 4]
                    items.append(("wire", lay, a[0], a[1], b[0], b[1], w))
        elif st == pcbnew.SHAPE_T_CIRCLE:
            c = L(g.GetCenter())
            items.append(("circle", lay, c[0], c[1], mm(g.GetRadius()),
                          0.0 if g.IsAnyFill() else w))
        elif st == pcbnew.SHAPE_T_POLY:
            o = g.GetPolyShape().Outline(0)
            pts = [L(o.CPoint(i)) for i in range(o.PointCount())]
            if g.IsAnyFill():
                items.append(("poly", lay, w, pts))
            else:
                for i in range(len(pts)):
                    a, b = pts[i], pts[(i + 1) % len(pts)]
                    items.append(("wire", lay, a[0], a[1], b[0], b[1], w))
        else:
            # none on any live board; say so rather than draw it wrong
            report["footprint graphic shape %s NOT carried" % g.GetShapeStr()] += 1
    return items


ANGLE_AT = {"smd": 6, "pad": 7, "rect": 6}


def _period(it):
    """The rotational symmetry of a pad-like item, in degrees."""
    if it[0] == "smd":
        return 90.0 if abs(it[4] - it[5]) < TOL else 180.0
    if it[0] == "pad":
        return 90.0 if it[6] in ("round", "square", "octagon") else 180.0
    return 90.0 if abs((it[4] - it[2]) - (it[5] - it[3])) < TOL else 180.0


def _eq(x, y):
    if x[0] != y[0] or len(x) != len(y):
        return False
    for j in range(1, len(x)):
        p, q = x[j], y[j]
        if isinstance(p, list):
            if len(p) != len(q) or sorted((round(m[0], 3), round(m[1], 3)) for m in p) != \
                    sorted((round(n[0], 3), round(n[1], 3)) for n in q):
                return False
        elif j == ANGLE_AT.get(x[0]):
            per = _period(x)
            d = (p - q) % per
            if min(d, per - d) > 0.05:
                return False
        elif isinstance(p, float):
            if abs(p - q) > TOL:
                return False
        elif p != q:
            return False
    return True


def same(a, b):
    """Two packages are the same copper and the same drawing.

    TWO THINGS THE FIRST VERSION GOT WRONG, found by asking why one
    board came out at 24 packages for 12 footprints.  (1) A 0402 at 180 degrees
    reports its pads at 180 and one at 0 reports them at 0; a rectangle
    turned half a revolution is the same rectangle, so pad angles compare
    modulo the pad's own symmetry.  (2) Mirroring a part swaps which of
    its two silk lines is listed first, so graphics compare as a SET.
    Pads keep their names; everything else is order-free."""
    if len(a) != len(b):
        return False
    pa = {i[1]: i for i in a if i[0] in ("smd", "pad")}
    pb = {i[1]: i for i in b if i[0] in ("smd", "pad")}
    if set(pa) != set(pb) or any(not _eq(pa[k], pb[k]) for k in pa):
        return False
    rest = [i for i in b if i[0] not in ("smd", "pad")]
    used = [False] * len(rest)
    for i in (i for i in a if i[0] not in ("smd", "pad")):
        for k, j in enumerate(rest):
            if not used[k] and _eq(i, j):
                used[k] = True
                break
        else:
            return False
    return True


def package_xml(name, items):
    e = ET.Element("package", name=name)
    for it in items:
        k = it[0]
        if k == "smd":
            _, n, u, v, dx, dy, rot, rnd, stop, cream = it
            ET.SubElement(e, "smd", name=n, x=f(u), y=f(v), dx=f(dx), dy=f(dy),
                          layer="1", roundness=str(rnd), rot="R" + f(rot),
                          stop="yes" if stop else "no",
                          cream="yes" if cream else "no")
        elif k == "pad":
            _, n, u, v, drill, dia, shape, rot = it
            ET.SubElement(e, "pad", name=n, x=f(u), y=f(v), drill=f(drill),
                          diameter=f(dia), shape=shape, rot="R" + f(rot))
        elif k == "hole":
            ET.SubElement(e, "hole", x=f(it[1]), y=f(it[2]), drill=f(it[3]))
        elif k == "rect":
            _, lay, x1, y1, x2, y2, rot = it
            ET.SubElement(e, "rectangle", x1=f(x1), y1=f(y1), x2=f(x2),
                          y2=f(y2), layer=str(lay), rot="R" + f(rot))
        elif k == "wire":
            _, lay, x1, y1, x2, y2, w = it
            ET.SubElement(e, "wire", x1=f(x1), y1=f(y1), x2=f(x2), y2=f(y2),
                          width=f(w), layer=str(lay))
        elif k == "circle":
            _, lay, x, y, r, w = it
            ET.SubElement(e, "circle", x=f(x), y=f(y), radius=f(r), width=f(w),
                          layer=str(lay))
        elif k == "poly":
            _, lay, w, pts = it
            pe = ET.SubElement(e, "polygon", width=f(w), layer=str(lay))
            for x, y in pts:
                ET.SubElement(pe, "vertex", x=f(x), y=f(y))
    t = ET.SubElement(e, "text", x="0", y="0", size="1", layer="25")
    t.text = ">NAME"
    t = ET.SubElement(e, "text", x="0", y="-1.5", size="1", layer="27")
    t.text = ">VALUE"
    return e


def safe(name):
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", name) or "PKG"


# ------------------------------------------------------------- shared xml
def layers_xml(parent):
    ls = ET.SubElement(parent, "layers")
    for num, name, color in LAYERS:
        act = "no" if 2 <= num <= 15 and num not in ACTIVE_CU else "yes"
        ET.SubElement(ls, "layer", number=str(num), name=name, color=str(color),
                      fill="1", visible="yes" if act == "yes" else "no",
                      active=act)


def write(root, path):
    ET.indent(root, space=" ")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="utf-8"?>\n')
        fh.write('<!DOCTYPE eagle SYSTEM "eagle.dtd">\n')
        fh.write(ET.tostring(root, encoding="unicode"))
        fh.write("\n")


ASSUMED_CLEARANCE_MM = 0.2


def clearance_mm(board, report):
    """The default net class's clearance, which the EAGLE design rules carry.

    If no KiCad version's route to it works, 0.2 mm is written and the export
    fails, so that an assumed rule never reaches a fabricator unmarked.
    """
    ds = board.GetDesignSettings()
    routes = (lambda: ds.m_NetSettings.GetDefaultNetclass().GetClearance(),
              lambda: ds.m_NetSettings.m_DefaultNetClass.GetClearance(),
              lambda: ds.GetDefault().GetClearance())
    for route in routes:
        try:
            return mm(route())
        except Exception:                               # noqa: BLE001 - try the next KiCad version's API
            continue
    report["ASSUMED design-rule clearance mm (board's unreadable)"] = ASSUMED_CLEARANCE_MM
    return ASSUMED_CLEARANCE_MM


# ------------------------------------------------------------- board
def build(src, bd, report):
    b = pcbnew.LoadBoard(src)
    if b.GetCopperLayerCount() != 4:
        raise SystemExit("%s: %d copper layers, this exporter maps four"
                         % (bd, b.GetCopperLayerCount()))
    lib = bd

    packages = {}          # name -> items
    variants = defaultdict(list)
    placed = []            # (fp, pl, pkgname, padnames)
    worst = 0.0
    for fp in b.GetFootprints():
        pl = Place(fp)
        pitems, names = pad_items(fp, pl, report)
        items = pitems + gfx_items(fp, pl, report)
        base = safe(fp.GetFPIDAsString().split(":")[-1])
        chosen = None
        for cand in variants[base]:
            if same(packages[cand], items):
                chosen = cand
                break
        if chosen is None:
            chosen = base if not variants[base] else "%s__v%d" % (base, len(variants[base]) + 1)
            variants[base].append(chosen)
            packages[chosen] = items
        # rename this instance's pads onto the package's names, by number
        # and nearest position -- never by list order
        pk = [it for it in packages[chosen] if it[0] in ("smd", "pad")]
        final = {}
        for p in fp.Pads():
            uid = p.m_Uuid.AsString()
            if uid not in names:
                continue
            r = p.GetFPRelativePosition()
            u, v = (mm(r.x), mm(r.y)) if pl.flip else (mm(r.x), -mm(r.y))
            num = p.GetNumber() or "~"
            best = min((it for it in pk if it[1].split("@")[0] == num),
                       key=lambda it: (it[2] - u) ** 2 + (it[3] - v) ** 2)
            final[uid] = best[1]
            # THE SELF-CHECK: EAGLE's placement of the PACKAGE's pad
            # against pcbnew's absolute position of this instance's pad
            ex, ey = pl.eagle_abs(best[2], best[3])
            pos = p.GetPosition()
            worst = max(worst, math.hypot(ex - mm(pos.x), ey + mm(pos.y)))
        placed.append((fp, pl, chosen, final))
    # THE BOARD-WIDE ELONGATION, AND WHAT EAGLE CANNOT SAY.  Every long
    # through-hole pad on a board shares psElongationLong, a whole
    # percent; take it from the board's own long pads.  Then list every
    # through-hole pad whose written geometry differs from its source --
    # extents or corner radius -- so the round trip can hold the importer
    # to what was WRITTEN for exactly those pads and to the source for
    # every other one.
    tht = report.get("_tht", [])
    longs = Counter(int(round(100.0 * (max(t[2], t[3]) / min(t[2], t[3]) - 1.0)))
                    for t in tht if t[5] == "long")
    elong = longs.most_common(1)[0][0] if longs else 100
    approx = {}
    for ref, num, sx, sy, sc, shape, dia in tht:
        if shape == "long":
            wsz, wc = (dia, dia * (1.0 + elong / 100.0)), dia / 2.0
        elif shape == "round":
            wsz, wc = (dia, dia), dia / 2.0
        else:
            wsz, wc = (dia, dia), 0.0
        ssz = tuple(sorted((sx, sy)))
        if abs(ssz[0] - wsz[0]) > TOL or abs(ssz[1] - wsz[1]) > TOL or abs(sc - wc) > TOL:
            approx[(ref, num)] = (ssz, sc, tuple(sorted(wsz)), wc)
    report["_approx"] = approx
    report["_elong"] = elong
    report["packages"] = len(packages)
    report["package variants"] = sum(len(v) - 1 for v in variants.values())

    root = ET.Element("eagle", version=VERSION)
    dr = ET.SubElement(root, "drawing")
    layers_xml(dr)
    board = ET.SubElement(dr, "board")

    # -- plain: outline, silk text, keepouts
    plain = ET.SubElement(board, "plain")
    for d in b.GetDrawings():
        lname = d.GetLayerName()
        if d.GetClass() == "PCB_SHAPE" and d.GetLayer() == pcbnew.Edge_Cuts:
            w = f(mm(d.GetWidth()))
            st = d.GetShape()
            if st == pcbnew.SHAPE_T_RECTANGLE:
                s, e = d.GetStart(), d.GetEnd()
                pts = [(mm(s.x), -mm(s.y)), (mm(e.x), -mm(s.y)),
                       (mm(e.x), -mm(e.y)), (mm(s.x), -mm(e.y))]
                for i in range(4):
                    a, c = pts[i], pts[(i + 1) % 4]
                    ET.SubElement(plain, "wire", x1=f(a[0]), y1=f(a[1]),
                                  x2=f(c[0]), y2=f(c[1]), width=w, layer="20")
            elif st == pcbnew.SHAPE_T_CIRCLE:
                c = d.GetCenter()
                ET.SubElement(plain, "circle", x=f(mm(c.x)), y=f(-mm(c.y)),
                              radius=f(mm(d.GetRadius())), width=w, layer="20")
            elif st == pcbnew.SHAPE_T_SEGMENT:
                s, e = d.GetStart(), d.GetEnd()
                ET.SubElement(plain, "wire", x1=f(mm(s.x)), y1=f(-mm(s.y)),
                              x2=f(mm(e.x)), y2=f(-mm(e.y)), width=w, layer="20")
            else:
                raise NotImplementedError("Edge.Cuts shape %s" % d.GetShapeStr())
        elif d.GetClass() == "PCB_TEXT" and d.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            p = d.GetPosition()
            h = mm(d.GetTextHeight())
            back = d.GetLayer() == pcbnew.B_SilkS
            t = ET.SubElement(plain, "text", x=f(mm(p.x)), y=f(-mm(p.y)),
                              size=f(h), layer="22" if back else "21",
                              font="vector",
                              ratio=str(max(1, min(31, int(round(100 * mm(d.GetTextThickness()) / h))))),
                              rot=("MR" if back else "R") + f(d.GetTextAngleDegrees() % 360),
                              align="center")
            t.text = d.GetText()
        else:
            report["board drawing not carried (%s %s)" % (lname, d.GetClass())] += 1
    for z in b.Zones():
        if not z.GetIsRuleArea():
            continue
        o = z.Outline().Outline(0)
        pts = [(mm(o.CPoint(i).x), -mm(o.CPoint(i).y)) for i in range(o.PointCount())]
        lays = list(z.GetLayerSet().Seq())
        targets = []
        if z.GetDoNotAllowTracks() or z.GetDoNotAllowZoneFills():
            if pcbnew.F_Cu in lays:
                targets.append(("41", None))
            if pcbnew.B_Cu in lays:
                targets.append(("42", None))
            for inner in (pcbnew.In1_Cu, pcbnew.In2_Cu):
                if inner in lays:
                    targets.append((str(CU[inner]), "cutout"))
        if z.GetDoNotAllowVias():
            targets.append(("43", None))
        if z.GetDoNotAllowFootprints():
            if pcbnew.F_Cu in lays:
                targets.append(("39", None))
            if pcbnew.B_Cu in lays:
                targets.append(("40", None))
        for lay, pour in targets:
            report["_keepouts_written" if lay in ("39", "40") else "_rules_written"] += 1
            kw = dict(width="0", layer=lay)
            if pour:
                kw["pour"] = pour
            pe = ET.SubElement(plain, "polygon", **kw)
            for x, y in pts:
                ET.SubElement(pe, "vertex", x=f(x), y=f(y))
        report["rule areas -> restrict/keepout/cutout polygons"] += 1

    # -- the library
    libs = ET.SubElement(board, "libraries")
    le = ET.SubElement(libs, "library", name=lib)
    pks = ET.SubElement(le, "packages")
    for name in sorted(packages):
        pks.append(package_xml(name, packages[name]))
    ET.SubElement(board, "attributes")
    ET.SubElement(board, "variantdefs")
    classes = ET.SubElement(board, "classes")
    ET.SubElement(classes, "class", number="0", name="default", width="0", drill="0")
    clr = clearance_mm(b, report)
    rules = ET.SubElement(board, "designrules", name=lib)
    for k, v in (("layerSetup", "(1+2*15+16)"),
                 ("mdWireWire", "%smm" % f(clr)), ("mdWirePad", "%smm" % f(clr)),
                 ("mdWireVia", "%smm" % f(clr)), ("mdPadPad", "%smm" % f(clr)),
                 ("mdPadVia", "%smm" % f(clr)), ("mdViaVia", "%smm" % f(clr)),
                 ("mdSmdPad", "%smm" % f(clr)), ("mdSmdVia", "%smm" % f(clr)),
                 ("mdSmdSmd", "%smm" % f(clr)), ("mdCopperDimension", "0.5mm"),
                 ("msWidth", "0.15mm"), ("msDrill", "0.2mm"),
                 # restring 0: every pad and via carries its own diameter,
                 # and a rule-derived annulus must not enlarge it
                 ("rvPadTop", "0"), ("rvPadInner", "0"), ("rvPadBottom", "0"),
                 ("rlMinPadTop", "0mm"), ("rlMaxPadTop", "20mm"),
                 ("rlMinPadInner", "0mm"), ("rlMaxPadInner", "20mm"),
                 ("rlMinPadBottom", "0mm"), ("rlMaxPadBottom", "20mm"),
                 ("rvViaOuter", "0"), ("rvViaInner", "0"),
                 ("rlMinViaOuter", "0mm"), ("rlMaxViaOuter", "20mm"),
                 ("rlMinViaInner", "0mm"), ("rlMaxViaInner", "20mm"),
                 ("psTop", "-1"), ("psBottom", "-1"), ("psFirst", "-1"),
                 ("psElongationLong", str(report["_elong"])),
                 ("psElongationOffset", str(report["_elong"])),
                 ("mvStopFrame", "0"), ("mvCreamFrame", "0"),
                 ("mlMinStopFrame", "0mm"), ("mlMaxStopFrame", "0mm"),
                 ("mlMinCreamFrame", "0mm"), ("mlMaxCreamFrame", "0mm"),
                 ("srRoundness", "0"), ("srMinRoundness", "0mm"),
                 ("srMaxRoundness", "20mm")):
        ET.SubElement(rules, "param", name=k, value=v)

    # -- elements
    els = ET.SubElement(board, "elements")
    for fp, pl, pk, _ in sorted(placed, key=lambda t: t[0].GetReference()):
        p = fp.GetPosition()
        ET.SubElement(els, "element", name=fp.GetReference(), library=lib,
                      package=pk, value=fp.GetValue(), x=f(mm(p.x)),
                      y=f(-mm(p.y)), rot=pl.rot())

    # -- signals
    sig = defaultdict(lambda: {"c": [], "w": [], "v": [], "z": []})
    for fp, pl, pk, names in placed:
        for p in fp.Pads():
            uid = p.m_Uuid.AsString()
            if uid in names and p.GetNetname():
                sig[p.GetNetname()]["c"].append((fp.GetReference(), names[uid]))
    for t in b.GetTracks():
        net = t.GetNetname()
        if t.GetClass() == "PCB_VIA":
            try:
                dia = mm(t.GetWidth(pcbnew.F_Cu))
            except TypeError:
                dia = mm(t.GetWidth())
            if not net:
                report["vias with no net (dropped)"] += 1
                continue
            sig[net]["v"].append((mm(t.GetPosition().x), -mm(t.GetPosition().y),
                                  mm(t.GetDrillValue()), dia))
        elif t.GetClass() == "PCB_TRACK":
            s, e = t.GetStart(), t.GetEnd()
            w = (mm(s.x), -mm(s.y), mm(e.x), -mm(e.y), mm(t.GetWidth()), CU[t.GetLayer()])
            if net:
                sig[net]["w"].append(w)
            else:
                report["tracks with no net (to plain)"] += 1
                ET.SubElement(plain, "wire", x1=f(w[0]), y1=f(w[1]), x2=f(w[2]),
                              y2=f(w[3]), width=f(w[4]), layer=str(w[5]))
        else:
            raise NotImplementedError("track class %s" % t.GetClass())
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        o = z.Outline().Outline(0)
        pts = [(mm(o.CPoint(i).x), -mm(o.CPoint(i).y)) for i in range(o.PointCount())]
        th = "no" if z.GetPadConnection() == pcbnew.ZONE_CONNECTION_FULL else "yes"
        clz = z.GetLocalClearance()
        for lay in z.GetLayerSet().Seq():
            if lay in CU:
                sig[z.GetNetname()]["z"].append((CU[lay], pts, mm(z.GetMinThickness()),
                                                 mm(clz) if clz else clr, th))
    sigs = ET.SubElement(board, "signals")
    for net in sorted(sig):
        d = sig[net]
        se = ET.SubElement(sigs, "signal", name=net)
        for ref, pad in sorted(d["c"]):
            ET.SubElement(se, "contactref", element=ref, pad=pad)
        for lay, pts, w, iso, th in d["z"]:
            pe = ET.SubElement(se, "polygon", width=f(w), layer=str(lay),
                               isolate=f(iso), thermals=th, rank="1")
            for x, y in pts:
                ET.SubElement(pe, "vertex", x=f(x), y=f(y))
        for x1, y1, x2, y2, w, lay in d["w"]:
            ET.SubElement(se, "wire", x1=f(x1), y1=f(y1), x2=f(x2), y2=f(y2),
                          width=f(w), layer=str(lay))
        for x, y, drill, dia in d["v"]:
            ET.SubElement(se, "via", x=f(x), y=f(y), extent="1-16",
                          drill=f(drill), diameter=f(dia))
    report["self-check worst pad error um"] = worst * 1000.0
    return b, root, packages, placed, sig


# ------------------------------------------------------------- schematic
def natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def schematic(bd, packages, placed, sig):
    lib = bd
    root = ET.Element("eagle", version=VERSION)
    dr = ET.SubElement(root, "drawing")
    layers_xml(dr)
    sch = ET.SubElement(dr, "schematic")
    libs = ET.SubElement(sch, "libraries")
    le = ET.SubElement(libs, "library", name=lib)
    pks = ET.SubElement(le, "packages")
    syms = ET.SubElement(le, "symbols")
    dsets = ET.SubElement(le, "devicesets")
    geom = {}                     # package -> {pin: (x, y, side)}, W, H
    for name in sorted(packages):
        pks.append(package_xml(name, packages[name]))
        pads = [it[1] for it in packages[name] if it[0] in ("smd", "pad")]
        pins = sorted({p.split("@")[0] for p in pads}, key=natural)
        half = (len(pins) + 1) // 2
        left, right = pins[:half], pins[half:]
        maxlen = max([len(p) for p in pins] + [1])
        W = max(4, int(math.ceil((maxlen * 1.0 + 2) / GRID))) * 2 * GRID
        rows = max(len(left), len(right), 1)
        H = (rows + 1) * GRID
        top = H / 2.0 - GRID / 2.0
        top = round(top / GRID) * GRID
        sy = ET.SubElement(syms, "symbol", name="S_" + name)
        box = [(-W / 2, top + GRID), (W / 2, top + GRID),
               (W / 2, top - rows * GRID), (-W / 2, top - rows * GRID)]
        for i in range(4):
            a, c = box[i], box[(i + 1) % 4]
            ET.SubElement(sy, "wire", x1=f(a[0]), y1=f(a[1]), x2=f(c[0]),
                          y2=f(c[1]), width="0.254", layer="94")
        where = {}
        for i, pn in enumerate(left):
            x, y = -W / 2 - GRID, top - i * GRID
            ET.SubElement(sy, "pin", name=pn, x=f(x), y=f(y), length="short",
                          direction="pas", rot="R0")
            where[pn] = (x, y, -1)
        for i, pn in enumerate(right):
            x, y = W / 2 + GRID, top - i * GRID
            ET.SubElement(sy, "pin", name=pn, x=f(x), y=f(y), length="short",
                          direction="pas", rot="R180")
            where[pn] = (x, y, +1)
        t = ET.SubElement(sy, "text", x=f(-W / 2), y=f(top + 2 * GRID),
                          size="1.778", layer="95")
        t.text = ">NAME"
        t = ET.SubElement(sy, "text", x=f(-W / 2), y=f(top - (rows + 1.5) * GRID),
                          size="1.778", layer="96")
        t.text = ">VALUE"
        ds = ET.SubElement(dsets, "deviceset", name="D_" + name, uservalue="yes")
        gs = ET.SubElement(ds, "gates")
        ET.SubElement(gs, "gate", name="G$1", symbol="S_" + name, x="0", y="0")
        dvs = ET.SubElement(ds, "devices")
        dv = ET.SubElement(dvs, "device", name="", package=name)
        cs = ET.SubElement(dv, "connects")
        for pn in pins:
            ET.SubElement(cs, "connect", gate="G$1", pin=pn,
                          pad=" ".join(sorted((p for p in pads if p.split("@")[0] == pn),
                                              key=natural)))
        tech = ET.SubElement(dv, "technologies")
        ET.SubElement(tech, "technology", name="")
        geom[name] = (where, W + 2 * GRID, H + 2 * GRID)

    ET.SubElement(sch, "attributes")
    ET.SubElement(sch, "variantdefs")
    cl = ET.SubElement(sch, "classes")
    ET.SubElement(cl, "class", number="0", name="default", width="0", drill="0")
    parts = ET.SubElement(sch, "parts")
    order = sorted(placed, key=lambda t: natural(t[0].GetReference()))
    for fp, pl, pk, _ in order:
        ET.SubElement(parts, "part", name=fp.GetReference(), library=lib,
                      deviceset="D_" + pk, device="", value=fp.GetValue())

    # placement: sheets of 40, six columns, every coordinate on the grid
    pinpos = {}                   # (ref, pin) -> (sheet, x, y, side)
    sheets_el = ET.SubElement(sch, "sheets")
    PER, COLS = 40, 6
    sheet_nodes = []
    for si in range(0, len(order), PER):
        chunk = order[si:si + PER]
        colw = max(geom[pk][1] for _, _, pk, _ in chunk) + 30.48
        roww = max(geom[pk][2] for _, _, pk, _ in chunk) + 10.16
        colw = math.ceil(colw / GRID) * GRID
        roww = math.ceil(roww / GRID) * GRID
        sh = ET.SubElement(sheets_el, "sheet")
        ET.SubElement(sh, "plain")
        inst = ET.SubElement(sh, "instances")
        for k, (fp, pl, pk, _) in enumerate(chunk):
            x = (k % COLS) * colw
            y = -(k // COLS) * roww
            ET.SubElement(inst, "instance", part=fp.GetReference(), gate="G$1",
                          x=f(x), y=f(y))
            for pn, (px, py, side) in geom[pk][0].items():
                pinpos[(fp.GetReference(), pn)] = (len(sheet_nodes), x + px, y + py, side)
        ET.SubElement(sh, "busses")
        sheet_nodes.append((sh, ET.SubElement(sh, "nets")))

    per_sheet = [defaultdict(list) for _ in sheet_nodes]
    for net in sorted(sig):
        pins = sorted({(ref, pad.split("@")[0]) for ref, pad in sig[net]["c"]})
        for ref, pn in pins:
            s, x, y, side = pinpos[(ref, pn)]
            per_sheet[s][net].append((ref, pn, x, y, side))
    for s, (sh, nets_el) in enumerate(sheet_nodes):
        for net in sorted(per_sheet[s]):
            ne = ET.SubElement(nets_el, "net", name=net)
            for ref, pn, x, y, side in per_sheet[s][net]:
                seg = ET.SubElement(ne, "segment")
                ET.SubElement(seg, "pinref", part=ref, gate="G$1", pin=pn)
                x2 = x + side * GRID
                ET.SubElement(seg, "wire", x1=f(x), y1=f(y), x2=f(x2), y2=f(y),
                              width="0.1524", layer="91")
                ET.SubElement(seg, "label", x=f(x2), y=f(y), size="1.27",
                              layer="95", rot="R0" if side > 0 else "R180")
    return root


# ------------------------------------------------------------- checks
def dtd_ok(path):
    # xmllint reads --dtdvalid as a URL, so a space in its path fails to load. A relative path doesn't have the problem.
    p = subprocess.run(["xmllint", "--noout", "--dtdvalid", os.path.basename(DTD), os.path.abspath(path)],
                       capture_output=True, text=True, cwd=os.path.dirname(DTD))
    # xmllint echoes the DOCTYPE line under a warning that it could not
    # fetch "eagle.dtd" by the SYSTEM name; --dtdvalid supplies the real
    # one, so only lines that report an ERROR are errors.
    errs = [l for l in p.stderr.splitlines()
            if "error" in l.lower() and "failed to load external entity" not in l]
    return p.returncode == 0, errs[:4]


def corner(p):
    """A pad's corner radius in mm, whatever KiCad calls its shape."""
    sz = p.GetSize()
    m = min(mm(sz.x), mm(sz.y))
    sh = p.GetShape()
    if sh in (pcbnew.PAD_SHAPE_CIRCLE, pcbnew.PAD_SHAPE_OVAL):
        return m / 2.0
    if sh == pcbnew.PAD_SHAPE_ROUNDRECT:
        return p.GetFrontRoundRectRadiusRatio() * m
    return 0.0


def roundtrip(orig, brd, report):
    """KiCad's own EAGLE importer reads the file back; compare to source."""
    imp = pcbnew.LoadBoard(brd, pcbnew.PCB_IO_MGR.EAGLE)
    if imp is None:
        return {"FAIL": "KiCad's EAGLE importer returned nothing"}
    out = Counter()
    ofp = {fp.GetReference(): fp for fp in orig.GetFootprints()}
    ifp = {fp.GetReference(): fp for fp in imp.GetFootprints()}
    out["footprints missing"] = len(set(ofp) - set(ifp))
    out["footprints extra"] = len(set(ifp) - set(ofp))
    worst = 0.0
    for ref, fo in ofp.items():
        fi = ifp.get(ref)
        if fi is None:
            continue
        op = [p for p in fo.Pads() if any(p.IsOnLayer(l) for l in CU)
              and p.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH]
        ip = [p for p in fi.Pads() if any(p.IsOnLayer(l) for l in CU)
              and p.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH]
        if len(op) != len(ip):
            out["footprints with a different copper pad count"] += 1
        for p in op:
            pos = p.GetPosition()
            q = min(ip, key=lambda r: (r.GetPosition().x - pos.x) ** 2 +
                    (r.GetPosition().y - pos.y) ** 2) if ip else None
            if q is None:
                continue
            d = math.hypot(mm(q.GetPosition().x - pos.x), mm(q.GetPosition().y - pos.y))
            worst = max(worst, d)
            if d > TOL:
                out["pads displaced > 1 um"] += 1
            if q.GetNetname() != p.GetNetname():
                out["pads on a different net"] += 1
            if q.IsOnLayer(pcbnew.F_Cu) != p.IsOnLayer(pcbnew.F_Cu):
                out["pads on the wrong side"] += 1
            os_, is_ = p.GetSize(), q.GetSize()
            c = sorted((mm(is_.x), mm(is_.y)))
            key = (ref, p.GetNumber())
            if key in report["_approx"]:
                # held to what EAGLE was TOLD, not to what it cannot say
                _, _, a, want_corner = report["_approx"][key]
                a = list(a)
                out["pads EAGLE cannot express (compared as written)"] += 1
            else:
                a = sorted((mm(os_.x), mm(os_.y)))
                want_corner = corner(p)
            # EAGLE stores a long pad's length as a whole-percent
            # elongation, so a long pad may come back a few microns off
            if abs(a[0] - c[0]) > TOL or abs(a[1] - c[1]) > max(TOL, 0.006 * a[0]):
                out["pads resized"] += 1
            # CORNER RADIUS, NOT SHAPE NAME.  A circle written as an SMD
            # with roundness 100 comes back as a roundrect of ratio 0.5,
            # which is the same copper; comparing the enum called 160
            # connector balls "changed" when not one of them was.
            # EAGLE roundness is a whole percent, so allow one percent.
            if abs(want_corner - corner(q)) > max(TOL, 0.006 * a[0]):
                out["pads whose corner radius changed"] += 1
            # ORIENTATION OF THE LONG AXIS, modulo the pad's symmetry.  An
            # angle alone cannot see a pad whose width and length traded
            # places, which is exactly the error a "long" pad invites.
            def axis(pp):
                s = pp.GetSize()
                return (pp.GetOrientationDegrees() +
                        (90.0 if mm(s.y) > mm(s.x) + TOL else 0.0)) % 180.0
            if abs(c[0] - c[1]) < TOL:
                if corner(q) < c[0] / 2.0 - TOL:
                    d = (p.GetOrientationDegrees() - q.GetOrientationDegrees()) % 90.0
                    if min(d, 90.0 - d) > 0.05:
                        out["pads rotated"] += 1
            else:
                d = (axis(p) - axis(q)) % 180.0
                if min(d, 180.0 - d) > 0.05:
                    out["pads rotated"] += 1
    out["worst pad position error um"] = round(worst * 1000.0, 3)

    def tracks(bb):
        c = Counter()
        ln = Counter()
        for t in bb.GetTracks():
            if t.GetClass() == "PCB_TRACK":
                c[bb.GetLayerName(t.GetLayer()) if False else t.GetLayer()] += 1
                ln[t.GetNetname()] += t.GetLength()
        return c, ln
    oc, ol = tracks(orig)
    ic, il = tracks(imp)
    out["track segments: source"] = sum(oc.values())
    out["track segments: imported"] = sum(ic.values())
    out["nets whose routed length differs > 1 um"] = sum(
        1 for n in set(ol) | set(il) if abs(mm(ol.get(n, 0) - il.get(n, 0))) > TOL)
    ov = {(round(mm(v.GetPosition().x), 3), round(mm(v.GetPosition().y), 3))
          for v in orig.GetTracks() if v.GetClass() == "PCB_VIA"}
    iv = {(round(mm(v.GetPosition().x), 3), round(mm(v.GetPosition().y), 3))
          for v in imp.GetTracks() if v.GetClass() == "PCB_VIA"}
    out["vias: source"] = len(ov)
    out["vias missing after import"] = len(ov - iv)
    # THREE KINDS OF POLYGON, COUNTED APART.  Lumped together the first
    # run read 4 pours against 20 imported and looked like sixteen stray
    # copper zones; they were the part-keepout polygons, which KiCad reads
    # as courtyard zones.  Each kind is held to what was written.
    def zcount(bb, rule, copper):
        n = 0
        for z in bb.Zones():
            if z.GetIsRuleArea() != rule:
                continue
            for l in z.GetLayerSet().Seq():
                if (l in CU) == copper:
                    n += 1
        return n
    oz, iz = zcount(orig, False, True), zcount(imp, False, True)
    out["copper pours: source / imported"] = "%d / %d" % (oz, iz)
    out["_pour_mismatch"] = int(oz != iz)
    ir = sum(1 for z in imp.Zones() if z.GetIsRuleArea())
    out["rule areas: written / imported"] = "%d / %d" % (report["_rules_written"], ir)
    out["_rules_mismatch"] = int(ir != report["_rules_written"])
    ik = zcount(imp, False, False)
    out["part keepouts: written / imported"] = "%d / %d" % (report["_keepouts_written"], ik)
    out["_keepout_mismatch"] = int(ik != report["_keepouts_written"])

    # SILKSCREEN, because nothing above looks at a graphic.  A bottom-side
    # part whose outline came back mirrored the wrong way would pass every
    # pad test on a symmetric land pattern and still print its pin-1 mark
    # on the wrong corner -- a shift the size of the part, not of a stroke.
    # COMPARED AS PRINTED INK, NOT AS GEOMETRY, and the first version of
    # this check is why.  It compared raw bounding boxes and failed 88
    # footprints across six boards whose every silk LINE came back to the
    # micron.  Each differed only in its filled pin-1 triangle.  KiCad
    # draws that as three vertices and a 0.12 mm stroke; this file stores
    # it the same way; KiCad's EAGLE importer reads it back as the outline
    # GROWN by the stroke with MITRED corners and no stroke.  Measured on
    # a SOT-23-6: the base edge moves
    # out 0.060 mm, as it should, but the
    # apex reaches 54.592 against the stroke's 54.549 -- 43 um -- and the
    # base corners 58 um, because a round cap stops short of a mitre.  The
    # same marker with a different corner.  So both sides go through
    # KiCad's own TransformShapeToPolygon, stroke included, and the
    # tolerance is 0.10 mm: above that corner, far below a transform
    # error, which moves a marker by the size of its part.
    def silk(fp):
        ps = pcbnew.SHAPE_POLY_SET()
        found = False
        for g in fp.GraphicalItems():
            if g.GetClass() == "PCB_SHAPE" and g.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS):
                g.TransformShapeToPolygon(ps, g.GetLayer(), 0, pcbnew.FromMM(0.001),
                                          pcbnew.ERROR_INSIDE)
                found = True
        if not found:
            return None
        bb = ps.BBox()
        return (mm(bb.GetLeft()), mm(bb.GetTop()), mm(bb.GetRight()), mm(bb.GetBottom()))
    moved, worst_silk = 0, 0.0
    for ref, fo in ofp.items():
        if ref not in ifp:
            continue
        s, t = silk(fo), silk(ifp[ref])
        if (s is None) != (t is None):
            moved += 1
            continue
        if s:
            d = max(abs(m - n) for m, n in zip(s, t))
            worst_silk = max(worst_silk, d)
            if d > 0.10:
                moved += 1
    out["footprints whose silkscreen moved > 0.10 mm"] = moved
    out["worst silkscreen extent difference um"] = round(worst_silk * 1000.0, 1)
    ob, ib = orig.GetBoardEdgesBoundingBox(), imp.GetBoardEdgesBoundingBox()
    out["outline bbox error um"] = round(1000.0 * max(
        abs(mm(ob.GetLeft() - ib.GetLeft())), abs(mm(ob.GetRight() - ib.GetRight())),
        abs(mm(ob.GetTop() - ib.GetTop())), abs(mm(ob.GetBottom() - ib.GetBottom()))), 3)
    return out


def _netlist(path):
    """{net: [(ref, pin), ...]} from a KiCad netlist. Net names are bare, because the leading slash only says where the label sat."""
    def kid(node, name):
        return next((x for x in node if isinstance(x, list) and x and x[0] == name), [])
    root = parse_sexpr(open(path).read())
    refs = {kid(c, "ref")[1] for c in kid(root, "components")[1:]}
    nets = {}
    for net in kid(root, "nets")[1:]:
        nets[kid(net, "name")[1].lstrip("/")] = [(kid(n, "ref")[1], kid(n, "pin")[1]) for n in net if isinstance(n, list) and n and n[0] == "node"]
    return refs, nets


def sch_netlist_check(sch, sig, reference=None):
    """The schematic, read back from its own XML.

    THIS IS NOT AN INDEPENDENT READER, and it says so rather than passing
    for one.  kicad-cli 10.0.5 has no EAGLE schematic import -- its sch
    subcommands are erc, export and upgrade -- and `sch export netlist`
    refuses even a KNOWN-GOOD EAGLE 7.7.0 schematic (drtrigon/eagle,
    DACRackUnit/power-supply.sch) with "Failed to load schematic".  The
    same importer's BOARD side read that project's .brd without complaint,
    which is why the board gets a true round trip and this does not.

    So it parses the written file and resolves connectivity the way EAGLE
    does -- net, segment, pinref, part, deviceset, device connect, pad --
    instead of trusting the pin names it wrote, and it checks the one
    piece of geometry EAGLE joins on: every stub must START on its pin.
    What it compares against is the independent half: the netlist KiCad
    exports from the project's own schematic.  The DTD check is the other
    independent half.
    """
    root = ET.parse(sch).getroot()
    lib = root.find("drawing/schematic/libraries/library")
    syms = {s.get("name"): {p.get("name"): (float(p.get("x")), float(p.get("y")))
                            for p in s.findall("pin")}
            for s in lib.find("symbols")}
    dsets = {}
    for ds in lib.find("devicesets"):
        dev = ds.find("devices/device")
        dsets[ds.get("name")] = (ds.find("gates/gate").get("symbol"),
                                 {c.get("pin"): c.get("pad").split()
                                  for c in dev.find("connects")})
    parts = {p.get("name"): p.get("deviceset")
             for p in root.find("drawing/schematic/parts")}
    got = defaultdict(set)
    off_pin = segs = 0
    for sh in root.find("drawing/schematic/sheets"):
        inst = {i.get("part"): (float(i.get("x")), float(i.get("y")))
                for i in sh.find("instances")}
        for net in sh.find("nets"):
            for seg in net.findall("segment"):
                segs += 1
                pr, w = seg.find("pinref"), seg.find("wire")
                ref, pin = pr.get("part"), pr.get("pin")
                sym, conn = dsets[parts[ref]]
                px, py = syms[sym][pin]
                ix, iy = inst[ref]
                if abs(float(w.get("x1")) - (ix + px)) > 1e-4 or \
                        abs(float(w.get("y1")) - (iy + py)) > 1e-4:
                    off_pin += 1
                for pad in conn[pin]:
                    got[net.get("name")].add((ref, pad.split("@")[0]))
    want = {n: {(r, pad.split("@")[0]) for r, pad in d["c"]}
            for n, d in sig.items() if d["c"]}
    out = Counter()
    out["nets: board / schematic"] = "%d / %d" % (len(want), len(got))
    out["stubs checked / not starting on their pin"] = "%d / %d" % (segs, off_pin)
    out["board nets missing or different in schematic"] = sum(
        1 for n, m in want.items() if got.get(n) != m)
    if reference:
        refs, rn = _netlist(reference)
        out["schematic nets not reproduced"] = sum(
            1 for n, m in rn.items()
            if len(m) > 1 and {x for x in got.get(n, set()) if x[0] in refs} != set(m))
        out["parts not in the schematic"] = ", ".join(sorted(set(parts) - refs)) or "none"
    out["_off_pin"] = off_pin
    return out


# ------------------------------------------------------------- main
def export(src, bd, dst, schematic_path=None):
    """Write and check NAME.brd and NAME.sch. Returns the list of failed checks."""
    report = Counter()
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst)
    orig, broot, packages, placed, sig = build(src, bd, report)
    brd = os.path.join(dst, "%s.brd" % bd)
    sch = os.path.join(dst, "%s.sch" % bd)
    write(broot, brd)
    write(schematic(bd, packages, placed, sig), sch)
    print("== %s" % bd)
    for k in sorted(report):
        if k.startswith("_"):
            continue
        v = report[k]
        print("   %-48s %s" % (k, ("%.4f" % v) if isinstance(v, float) else v))
    print("   %-48s %d%%" % ("psElongationLong (from the board's long pads)", report["_elong"]))
    for (ref, num), (ssz, sc, wsz, wc) in sorted(report["_approx"].items()):
        print("   EAGLE CANNOT EXPRESS %s pad %s: %.2f x %.2f r%.2f written as %.2f x %.2f r%.2f"
              % (ref, num, ssz[0], ssz[1], sc, wsz[0], wsz[1], wc))
    fails = []
    if report["self-check worst pad error um"] > 1.0:
        fails.append("self-check")
    if "ASSUMED design-rule clearance mm (board's unreadable)" in report:
        fails.append("clearance")
    for path in (brd, sch):
        ok, errs = dtd_ok(path)
        print("   DTD %-44s %s" % (os.path.basename(path), "valid" if ok else "INVALID"))
        for e in errs:
            print("       %s" % e)
        if not ok:
            fails.append("DTD " + os.path.basename(path))
    rt = roundtrip(orig, brd, report)
    print("   -- KiCad EAGLE importer, board read back")
    for k in sorted(rt):
        if not k.startswith("_"):
            print("   %-48s %s" % (k, rt[k]))
    for k in ("_pour_mismatch", "_rules_mismatch", "_keepout_mismatch",
              "footprints whose silkscreen moved > 0.10 mm"):
        if rt.get(k):
            fails.append(k.lstrip("_"))
    for k in ("FAIL", "footprints missing", "footprints extra", "pads displaced > 1 um",
              "pads on a different net", "pads on the wrong side", "vias missing after import",
              "nets whose routed length differs > 1 um", "pads resized", "pads rotated",
              "pads whose corner radius changed",
              "footprints with a different copper pad count"):
        if rt.get(k):
            fails.append(k)
    reference = None
    if schematic_path:
        reference = os.path.join(tempfile.mkdtemp(), "reference.net")
        r = subprocess.run([CLI, "sch", "export", "netlist", "--output", reference, schematic_path], capture_output=True)
        if r.returncode or not os.path.exists(reference):
            fails.append("kicad-cli couldn't export the project's netlist")
            reference = None
    sc = sch_netlist_check(sch, sig, reference)
    print("   -- schematic parsed back (no independent EAGLE .sch reader exists here)")
    for k in sorted(sc):
        if not k.startswith("_"):
            print("   %-48s %s" % (k, sc[k]))
    for k in ("_off_pin", "board nets missing or different in schematic", "schematic nets not reproduced"):
        if sc.get(k):
            fails.append(k.lstrip("_"))
    print("   %s" % ("OK" if not fails else "FAILED: " + "; ".join(fails)))
    return fails


if __name__ == "__main__":
    CLI = sys.argv[4]
    _fails = export(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[5] if len(sys.argv) > 5 else None)
    sys.stdout.flush()
    os._exit(1 if _fails else 0)    # pcbnew's bindings can segfault during interpreter shutdown, after the work is saved
