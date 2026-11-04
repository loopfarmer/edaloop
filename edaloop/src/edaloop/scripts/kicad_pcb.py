#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A .kicad_pcb read as geometry: pads, segments, vias, and a clearance test.

THE PAD TRANSFORM LIVES HERE AND NOWHERE ELSE. Other RF scripts here
ask this module where a pad is, because a sign error in it is
invisible -- it lands pads up to 6 mm away, and the wrong answer looks
plausible enough to "disprove" a set of rotations that were right.

    ax = fx + px*cos(t) + py*sin(t)
    ay = fy - px*sin(t) + py*cos(t)

validated to 0.000 mm against pcbnew's own GetPosition() over all 1450
pads of a two-sided board -- which is the check that matters, because
KiCad's DRC only reports the pads it has something to say about and on
these boards those are all on the front. THE SAME TRANSFORM SERVES BOTH
SIDES: a flipped footprint's pads are stored already in the flipped
frame. This file used to negate the local Y for a back-side footprint,
which put 276 of 603 back pads up to 3.810 mm out of place. A pad's own
(at x y rot) third element adds to the footprint's rotation.
"""
import math
import re


# ==========================================================================
# s-expressions
# ==========================================================================
def sexp(s, i=0):
    """Minimal reader. Returns (node, next_index). Atoms are str."""
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    if s[i] == "(":
        out = []
        i += 1
        while True:
            while i < n and s[i] in " \t\r\n":
                i += 1
            if s[i] == ")":
                return out, i + 1
            v, i = sexp(s, i)
            out.append(v)
    if s[i] == '"':
        buf = []
        i += 1
        while s[i] != '"':
            if s[i] == "\\":
                i += 1
            buf.append(s[i])
            i += 1
        return "".join(buf), i + 1
    j = i
    while j < n and s[j] not in ' \t\r\n()"':
        j += 1
    return s[i:j], j


def kids(node, tag):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def kid(node, tag):
    g = kids(node, tag)
    return g[0] if g else None


# ==========================================================================
# geometry
# ==========================================================================
def pt_rect(px, py, cx, cy, hx, hy, ang):
    """Distance from a point to a ROTATED RECTANGLE, 0 inside.

    Pads are rectangles and must be tested as rectangles. A circumscribed
    circle makes a 1.95 x 0.60 mm SOIC pad look 1.02 mm wide in every
    direction, which is how a 3.17 mm hop comes back "blocked" while
    fourteen identical hops on the same board route without complaint.
    """
    dx, dy = px - cx, py - cy
    a = math.radians(ang)
    lx = dx * math.cos(a) - dy * math.sin(a)
    ly = dx * math.sin(a) + dy * math.cos(a)
    return math.hypot(max(abs(lx) - hx, 0.0), max(abs(ly) - hy, 0.0))


def pt_seg(px, py, x1, y1, x2, y2):
    vx, vy = x2 - x1, y2 - y1
    L2 = vx * vx + vy * vy
    if L2 < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * vx + (py - y1) * vy) / L2))
    return math.hypot(px - (x1 + t * vx), py - (y1 + t * vy))


def mst(points):
    """Euclidean minimum spanning tree length.

    The honest floor for a net with more than two pads. Measuring such a
    net against the pad-to-pad distance of two of them counts its
    legitimate branches as excess -- which is how a bias-T tap on every
    channel of a board first read as 3.54 mm of waste apiece.
    """
    if len(points) < 2:
        return 0.0
    inside, out = [points[0]], list(points[1:])
    total = 0.0
    while out:
        best, bi = None, 0
        for i, q in enumerate(out):
            d = min(math.hypot(q[0] - r[0], q[1] - r[1]) for r in inside)
            if best is None or d < best:
                best, bi = d, i
        total += best
        inside.append(out.pop(bi))
    return total


def axis_snap(x1, y1, x2, y2, tol=0.02):
    """Force a nearly-horizontal or nearly-vertical run to be exactly one.

    Pad centres carry floating-point noise -- a pad meant for a row at
    y=10.000 lands at y=9.999668 -- so a run that is straight to the eye
    is 0.0003 mm off axis in the file. KiCad does not care. Freerouting
    does: it reports "traces not 45 degree" and then never writes its
    session file. The offset is three orders of magnitude inside the pad,
    so snapping cannot miss it.
    """
    if abs(y2 - y1) < tol:
        y2 = y1
    if abs(x2 - x1) < tol:
        x2 = x1
    return (round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4))


# ==========================================================================
# the board
# ==========================================================================
class Pad(object):
    def __init__(self, ref, num, x, y, hx, hy, ang, net, layers, shape):
        self.ref, self.num, self.x, self.y = ref, num, x, y
        self.hx, self.hy, self.ang = hx, hy, ang
        self.net, self.layers, self.shape = net, layers, shape

    def on(self, layer):
        """Through-hole pads carry "*.Cu" and obstruct every layer, which
        is what they physically do."""
        return any(l == layer or l == "*.Cu" or
                   (l.startswith("*.") and layer.endswith(l[1:]))
                   for l in self.layers)

    def clear_of(self, px, py, need):
        return pt_rect(px, py, self.x, self.y, self.hx, self.hy,
                       self.ang) >= need

    def __repr__(self):
        return "%s.%s@(%.3f,%.3f)" % (self.ref, self.num, self.x, self.y)


class Board(object):
    """Read-only geometry plus rewrite/save. Never edited through a tool
    that keeps its own in-memory copy: such a copy goes stale the moment
    anything else writes the file, and in this workflow that is every
    kicad-cli run with --save-board."""

    def __init__(self, path, clearance=0.2, step=0.05):
        self.path = path
        self.clearance = clearance
        self.step = step
        self.text = open(path).read()
        root, _ = sexp(self.text)
        self.pads, self.segs, self.vias = [], [], []
        for fp in kids(root, "footprint"):
            at = kid(fp, "at")
            fx, fy = float(at[1]), float(at[2])
            frot = float(at[3]) if len(at) > 3 else 0.0
            ref = ""
            for pr in kids(fp, "property"):
                if len(pr) > 2 and pr[1] == "Reference":
                    ref = pr[2]
            t = math.radians(frot)
            for pd in kids(fp, "pad"):
                pat = kid(pd, "at")
                px, py = float(pat[1]), float(pat[2])
                prot = float(pat[3]) if len(pat) > 3 else 0.0
                # NO NEGATION FOR A BACK-SIDE FOOTPRINT -- KiCad stores a
                # flipped footprint's pads already in the flipped frame,
                # so one rotation serves both sides. Measured against
                # pcbnew's GetPosition() over 1450 pads: without this,
                # 276 of 603 back pads land up to 3.810 mm out; with it,
                # all 1357 uniquely-named pads agree to 0.000 mm.
                ax = fx + px * math.cos(t) + py * math.sin(t)
                ay = fy - px * math.sin(t) + py * math.cos(t)
                sz = kid(pd, "size")
                hx, hy = float(sz[1]) / 2.0, float(sz[2]) / 2.0
                nk = kid(pd, "net")
                lay = kid(pd, "layers") or []
                # A PAD'S ANGLE IN THE FILE IS ABSOLUTE: the footprint's
                # rotation is already in it.  Adding frot again swaps a
                # pad's width and height on a footprint at 90/270 and
                # mis-turns it at any other angle.  Checked against
                # pcbnew's pad polygons.
                self.pads.append(Pad(ref, pd[1], ax, ay, hx, hy,
                                     prot, nk[1] if nk else "",
                                     lay[1:], pd[2]))
                # .shape is the pad TYPE (smd, thru_hole); .form is its outline (circle, rect,
                # oval, roundrect).  A round pad tested as its bounding square reads a corner
                # 0.99 mm from a 0.75 mm-radius pin as copper.
                self.pads[-1].form = pd[3] if len(pd) > 3 else ""
        for sg in kids(root, "segment"):
            s, e = kid(sg, "start"), kid(sg, "end")
            self.segs.append(dict(
                x1=float(s[1]), y1=float(s[2]),
                x2=float(e[1]), y2=float(e[2]),
                net=(kid(sg, "net") or ["", ""])[1],
                layer=(kid(sg, "layer") or ["", ""])[1],
                locked=bool(kids(sg, "locked")),
                w=float((kid(sg, "width") or ["", 0.2])[1])))
        for v in kids(root, "via"):
            a = kid(v, "at")
            self.vias.append(dict(
                x=float(a[1]), y=float(a[2]),
                net=(kid(v, "net") or ["", ""])[1],
                d=float((kid(v, "size") or ["", 0.6])[1]),
                # KEEP THE LOCK AND THE DRILL.  D188.  save() deletes every via
                # block and rebuilds it from this dict, so a field that is
                # not read here is a field deleted from the board.  Any
                # tool that opens a board and saves it -- even one that
                # only ADDS vias somewhere else -- silently unlocked every
                # via already on it and reset every drill to 0.30.
                dr=float((kid(v, "drill") or ["", 0.30])[1]),
                locked=bool(kids(v, "locked"))))
        self.index = {(p.ref, p.num): p for p in self.pads}

    # ------------------------------------------------------- measurement
    def net_pads(self, net):
        return [p for p in self.pads if p.net == net]

    def net_len(self, net):
        return sum(math.hypot(s["x2"] - s["x1"], s["y2"] - s["y1"])
                   for s in self.segs if s["net"] == net)

    def net_vias(self, net):
        return sum(1 for v in self.vias if v["net"] == net)

    def net_bends(self, net):
        """direction changes on this net's polyline"""
        dirs = set()
        for s in self.segs:
            if s["net"] == net:
                dirs.add(round(math.degrees(math.atan2(
                    s["y2"] - s["y1"], s["x2"] - s["x1"])) % 180.0, 1))
        return max(0, len(dirs) - 1)

    def floor(self, net, a=None, z=None):
        """spanning-tree lower bound over the net's own pads"""
        P = self.net_pads(net)
        if len(P) > 2:
            return mst([(p.x, p.y) for p in P])
        if a and z and a in self.index and z in self.index:
            pa, pz = self.index[a], self.index[z]
            return math.hypot(pz.x - pa.x, pz.y - pa.y)
        return mst([(p.x, p.y) for p in P])

    # -------------------------------------------------------- clearance
    def blocked(self, x1, y1, x2, y2, net, layer, w):
        """First obstruction on a candidate run, or None.

        Same-layer obstacles only, except through-hole pads. Sampled
        along the run rather than solved, because the rectangle test is
        cheap and the sampling pitch is far finer than any pad.
        """
        half = w / 2.0
        L = math.hypot(x2 - x1, y2 - y1)
        n = max(2, int(L / self.step) + 1)
        for i in range(n + 1):
            t = i / float(n)
            px, py = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            for p in self.pads:
                if p.net == net or not p.on(layer):
                    continue
                if not p.clear_of(px, py, self.clearance + half):
                    return "pad %s.%s" % (p.ref, p.num)
            for s in self.segs:
                if s["net"] == net or s["layer"] != layer:
                    continue
                if pt_seg(px, py, s["x1"], s["y1"], s["x2"], s["y2"]) \
                        < self.clearance + half + s["w"] / 2.0:
                    return "trace %s" % s["net"]
            for v in self.vias:
                if v["net"] == net:
                    continue
                if math.hypot(px - v["x"], py - v["y"]) \
                        < self.clearance + half + v["d"] / 2.0:
                    return "via %s" % v["net"]
        return None

    # -------------------------------------------------------- rewriting
    def rewrite(self, net, runs, w, layer="F.Cu", locked=False):
        """Drop every segment and via on `net`, lay `runs` instead.

        locked=True makes KiCad write (locked yes), which the DSN export
        turns into (type fix) -- the form Freerouting routes AROUND.
        """
        self.segs = [s for s in self.segs if s["net"] != net]
        self.vias = [v for v in self.vias if v["net"] != net]
        for (x1, y1, x2, y2) in runs:
            self.segs.append(dict(x1=x1, y1=y1, x2=x2, y2=y2, net=net,
                                  layer=layer, w=w, locked=locked))

    def via(self, x, y, net, d=0.6):
        """A run that changes layer needs one of these at BOTH ends.
        Not on a fine-pitch pad: via-in-pad on a 0.5 mm QFN costs four
        hole_clearance and two clearance violations. Offset it and reach
        it with a surface stub."""
        self.vias.append(dict(x=x, y=y, net=net, d=d))

    def save(self, out=None):
        body = self.text
        body = re.sub(r'\n\t\(segment\n(?:.*?\n)*?\t\)', '', body)
        body = re.sub(r'\n\t\(via\n(?:.*?\n)*?\t\)', '', body)
        add = []
        for s in self.segs:
            add.append(
                '\t(segment\n\t\t(start %.4f %.4f)\n\t\t(end %.4f %.4f)\n'
                '\t\t(width %.2f)\n\t\t(layer "%s")\n%s\t\t(net "%s")\n\t)'
                % (s["x1"], s["y1"], s["x2"], s["y2"], s["w"], s["layer"],
                   "\t\t(locked yes)\n" if s.get("locked") else "",
                   s["net"]))
        for v in self.vias:
            add.append(
                '\t(via\n\t\t(at %.4f %.4f)\n\t\t(size %.2f)\n'
                '\t\t(drill %.2f)\n\t\t(layers "F.Cu" "B.Cu")\n'
                '%s\t\t(net "%s")\n\t)'
                % (v["x"], v["y"], v["d"], v.get("dr", 0.30),
                   "\t\t(locked yes)\n" if v.get("locked") else "",
                   v["net"]))
        i = body.rindex(")")
        body = body[:i] + "\n".join(add) + "\n" + body[i:]
        open(out or self.path, "w").write(body)
        return len(self.segs), len(self.vias)


# ==========================================================================
# the chain spec
# ==========================================================================
def load_chains(path):
    """Import a chain-spec module by file path.

    It must define CHAINS: a list of (net, (ref, pin), (ref, pin)) giving
    each RF net's ENTRY and EXIT pad. Everything else on the net is a TAP
    and gets a perpendicular stub off the spine. Written out rather than
    inferred, because a "furthest pair" heuristic picks the wrong spine
    for any multi-pad combining node.

    Optional: WIDTH (float, default 0.30) and NET_WIDTH ({net: float}).
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("chainspec", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return (list(m.CHAINS), getattr(m, "WIDTH", 0.30),
            dict(getattr(m, "NET_WIDTH", {})))
