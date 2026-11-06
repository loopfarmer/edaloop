"""Board operations that need pcbnew. Runs under KiCad's own Python 3.9.

One operation per process: pcbnew cannot load a second board in the same
process after the first save. Runs as a script, so of this package it imports only
channel_groups, which needs nothing but the standard library.

    kicad_ops.py export_dsn   <in.kicad_pcb> <out.kicad_pcb> <out.dsn> <keep,nets>
    kicad_ops.py import_ses   <in.kicad_pcb> <in.ses> <out.kicad_pcb>
    kicad_ops.py add_locked   <in.kicad_pcb> <tracks.json> <out.kicad_pcb>
    kicad_ops.py fill_zones   <in.kicad_pcb> <out.kicad_pcb>
    kicad_ops.py build_board  <seed.kicad_pcb> <spec.json> <out.kicad_pcb>
    kicad_ops.py board_facts  <in.kicad_pcb> <out.json>
    kicad_ops.py place_silk   <in.kicad_pcb> <out.kicad_pcb>
    kicad_ops.py z_coupon     <in.kicad_pcb> <net> <width_mm> <out.kicad_pcb>
    kicad_ops.py add_vias     <in.kicad_pcb> <vias.json> <out.kicad_pcb>
    kicad_ops.py remove_vias  <in.kicad_pcb> <points.json> <out.kicad_pcb>
    kicad_ops.py remove_tracks <in.kicad_pcb> <tracks.json> <out.kicad_pcb>
    kicad_ops.py export_footprints <in.kicad_pcb> <out.pretty>
    kicad_ops.py add_voids    <in.kicad_pcb> <voids.json> <out.kicad_pcb>
"""
import json
import math
import os
import sys

import channel_groups
import pcbnew


def export_dsn(src, dst, dsn, keep_nets="GND", scope="unlocked"):
    """Remove copper, then export. Scope "unlocked" keeps locked items; "all" removes them too. Ground stays."""
    keep = set(keep_nets.split(","))
    board = pcbnew.LoadBoard(src)
    removed = kept = 0
    for track in list(board.GetTracks()):
        if (track.IsLocked() and scope != "all") or track.GetNetname() in keep:
            kept += 1
        else:
            board.Remove(track)
            removed += 1
    pcbnew.SaveBoard(dst, board)
    if not pcbnew.ExportSpecctraDSN(board, dsn):
        raise SystemExit("ExportSpecctraDSN failed")
    print(json.dumps({"removed": removed, "kept": kept}))


def add_locked(src, spec, dst):
    """Add locked tracks from a JSON list of net, start, end, width and layer."""
    board = pcbnew.LoadBoard(src)
    nets = board.GetNetsByName()
    ids = {board.GetLayerName(i): i for i in range(pcbnew.PCB_LAYER_ID_COUNT)}
    added = 0
    for item in json.load(open(spec)):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(item["start"][0]), pcbnew.FromMM(item["start"][1])))
        track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(item["end"][0]), pcbnew.FromMM(item["end"][1])))
        track.SetWidth(pcbnew.FromMM(item["width"]))
        track.SetLayer(ids[item["layer"]])
        track.SetNet(nets[item["net"]])
        track.SetLocked(True)
        board.Add(track)
        added += 1
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"added": added}))


def fill_zones(src, dst):
    board = pcbnew.LoadBoard(src)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"zones": len(board.Zones())}))


def import_ses(src, ses, dst):
    board = pcbnew.LoadBoard(src)
    if not pcbnew.ImportSpecctraSES(board, ses):
        raise SystemExit("ImportSpecctraSES failed")
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"tracks": len(board.GetTracks())}))


# ---------------------------------------------------------------- placement

def _pose(fp, x, y, rot):
    fp.SetOrientationDegrees(rot)
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)))


def _pad_xy(fp, number):
    for pad in fp.Pads():
        if pad.GetNumber() == number:
            q = pad.GetPosition()
            return pcbnew.ToMM(q.x), pcbnew.ToMM(q.y)
    raise SystemExit("%s has no pad %s" % (fp.GetReference(), number))


def _at_pad(fp, rot, number, x, y):
    _pose(fp, 0, 0, rot)
    dx, dy = _pad_xy(fp, number)
    _pose(fp, x - dx, y - dy, rot)


def _courtyard(fp, gap):
    fp.BuildCourtyardCaches()
    s = pcbnew.SHAPE_POLY_SET(fp.GetCourtyard(pcbnew.F_CrtYd))
    s.Inflate(pcbnew.FromMM(gap / 2.0), pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS, pcbnew.FromMM(0.01))
    return s


def _hit(fp, others, gap):
    mine = _courtyard(fp, gap)
    for other in others:
        x = pcbnew.SHAPE_POLY_SET(mine)
        x.BooleanIntersection(_courtyard(other, gap))
        if x.Area() > 0:
            return other.GetReference()
    return None


def _load_footprint(fpid, dirs):
    lib, name = fpid.split(":", 1)
    for root in dirs:
        path = os.path.join(root, lib + ".pretty")
        if os.path.isdir(path):
            fp = pcbnew.FootprintLoad(path, name)
            if fp:
                fp.SetFPID(pcbnew.LIB_ID(lib, name))
                return fp
    raise SystemExit("footprint not found: %s" % fpid)


def _attach(fps, item):
    """Pose one part relative to a pad or part already posed."""
    fp = fps[item["ref"]]
    if item.get("pad"):
        ax, ay = _pad_xy(fps[item["to"].split(".")[0]], item["to"].split(".")[1])
        _at_pad(fp, item["rot"], str(item["pad"]), ax + item.get("dx", 0.0), ay + item.get("dy", 0.0))
    else:
        c = fps[item["to"]].GetPosition()
        _pose(fp, pcbnew.ToMM(c.x) + item.get("dx", 0.0), pcbnew.ToMM(c.y) + item.get("dy", 0.0), item["rot"])
    return fp


# ---------------------------------------------------------------- deriving a layout

SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}


def _farads(value):
    """A capacitor's value for ordering, smallest first. Anything unreadable sorts last."""
    import re
    text = str(value).strip().replace("\u00b5", "u").replace("\u03bc", "u")
    # 4u7 is 4.7 uF: the multiplier letter stands in for the decimal point.
    m = re.match(r"(\d+)([pnum])(\d+)", text)
    if m:
        return float(m.group(1) + "." + m.group(3)) * SI[m.group(2)]
    m = re.match(r"([\d.]+)\s*([pnum])?", text)
    return float(m.group(1)) * SI.get(m.group(2) or "", 1.0) if m else 1e9


def _rot_for(fp, a, b, direction):
    """The rotation that points the vector from pad a to pad b, or from the origin to pad b, along a direction on the board.

    A pad on a part with more than two faces the way its side of the package does, not the way it lies from the centre. On a QFN, the
    second pin sits well off the middle of its side, and pointing the centre-to-pad line along the board turned the part 33.8 degrees, so
    that the line ran in across the corner of the pad beside it."""
    _pose(fp, 0, 0, 0)
    bx, by = _pad_xy(fp, b)
    ax, ay = _pad_xy(fp, a) if a is not None else (0.0, 0.0)
    v = math.degrees(math.atan2(by - ay, bx - ax))
    if a is None and len(list(fp.Pads())) > 2:
        v = round(v / 90.0) * 90.0
    d = math.degrees(math.atan2(direction[1], direction[0]))
    return round((v - d) % 360, 1)


def _pad_span(fp, rot, a, b, direction):
    """How far apart two pads sit in a direction at this rotation."""
    _pose(fp, 0, 0, rot)
    ax, ay = _pad_xy(fp, a)
    bx, by = _pad_xy(fp, b)
    return abs((bx - ax) * direction[0] + (by - ay) * direction[1])


def _reach(fp, rot, pad, direction):
    """How far a part's courtyard extends from a pad, in a direction, at this rotation."""
    _pose(fp, 0, 0, rot)
    px, py = _pad_xy(fp, pad)
    box = _courtyard(fp, 0).BBox()
    corners = [(pcbnew.ToMM(x), pcbnew.ToMM(y)) for x in (box.GetLeft(), box.GetRight()) for y in (box.GetTop(), box.GetBottom())]
    return max((x - px) * direction[0] + (y - py) * direction[1] for x, y in corners)


def _pad_reach(fp, rot, pad, direction):
    """How far a pad's copper extends from its centre, in a direction, at this rotation."""
    _pose(fp, 0, 0, rot)
    px, py = _pad_xy(fp, pad)
    box = next(q for q in fp.Pads() if q.GetNumber() == pad).GetBoundingBox()
    corners = [(pcbnew.ToMM(x), pcbnew.ToMM(y)) for x in (box.GetLeft(), box.GetRight()) for y in (box.GetTop(), box.GetBottom())]
    return max((x - px) * direction[0] + (y - py) * direction[1] for x, y in corners)


def tap_offset(half_line, stub, pad_reach):
    """How far a tap's RF pad centre sits off the line's centre: the line's half width, the stub from its edge, then the pad's own reach
    back toward the line. None leaves the pad on the line."""
    if stub is None:
        return 0.0
    return half_line + float(stub) + pad_reach


EXIT_CLEARANCE_MM = 0.2     # copper to copper beside a pad's exit: the Default net class's clearance, as boardgen writes it
JOG_MIN_MM = 0.01           # an exit pad nearer the line than this is on it. A pad-to-pad turn rounded to 0.1 degree leaves less


def _is_square(rot):
    return abs((float(rot) + 45.0) % 90.0 - 45.0) < 0.05


def _exit_clear(fp, rot, number, direction, nets, half):
    """Whether a line `half` wide each side, clearance included, leaves a pad along a direction without crossing a pad of the same part
    on another net. A pad with no number is counted as another net's."""
    _pose(fp, 0, 0, rot)
    px, py = _pad_xy(fp, number)
    mine = nets.get(number)
    for pad in fp.Pads():
        n = pad.GetNumber()
        if n == number or (n and mine is not None and nets.get(n) == mine):
            continue
        box = pad.GetBoundingBox()
        corners = [(pcbnew.ToMM(x) - px, pcbnew.ToMM(y) - py) for x in (box.GetLeft(), box.GetRight()) for y in (box.GetTop(), box.GetBottom())]
        along = [cx * direction[0] + cy * direction[1] for cx, cy in corners]
        across = [-cx * direction[1] + cy * direction[0] for cx, cy in corners]
        if max(along) > 0.0 and min(across) < half and max(across) > -half:
            return False
    return True


def _square_rot(fp, entry, leave, travel, nets, half, fallback, choice=0):
    """A square rotation for a part whose RF pads a pad-to-pad turn would leave at an angle, such as a TT1224 limiter's opposite corners.

    Each RF pad must face its way along the row: the entry pad's line leaves it backwards along the row, and the exit pad's forwards,
    neither crossing the part's own pads on other nets. The square rotations that do are ranked by how near the exit pad sits to the
    entry pad's line, so that the line's jog across the row is least, then by how near they are to the pad-to-pad turn. `choice` picks
    one by rank, the last if there are fewer. None does: the fallback."""
    back = (-travel[0], -travel[1])
    ranked = []
    for rot in (0.0, 90.0, 180.0, 270.0):
        if not (_exit_clear(fp, rot, entry, back, nets, half) and _exit_clear(fp, rot, leave, travel, nets, half)):
            continue
        _pose(fp, 0, 0, rot)
        ex, ey = _pad_xy(fp, entry)
        lx, ly = _pad_xy(fp, leave)
        if (lx - ex) * travel[0] + (ly - ey) * travel[1] <= 0:
            continue
        jog = abs(-(lx - ex) * travel[1] + (ly - ey) * travel[0])
        turn = abs((rot - float(fallback) + 180.0) % 360.0 - 180.0)
        ranked.append(((round(jog, 3), turn), rot))
    ranked.sort()
    return ranked[min(choice, len(ranked) - 1)][1] if ranked else fallback


def _row_lines(fps, walk, attach, line, axis):
    """Where the line runs across the board at each part of a row.

    A part whose exit pad sits off its entry pad's line, a square-set TT1224 for one, moves the line across by that much for the parts
    after it, so that every hop stays straight and on its pads' axis. Where the line jogs like that, the stretch of the row with the most
    parts, counting those stacked off its taps, keeps the line the description gives, and the rest move to suit."""
    across = 1 if axis == "x" else 0
    offsets, stretch, here, counts = [], [], 0.0, [0]
    for item in walk:
        ref, rot, entry, leave = item[:4]
        offsets.append(here)
        stretch.append(len(counts) - 1)
        counts[-1] += 1 + len(attach.get(ref, []))
        if entry is not None and leave is not None and str(entry) != str(leave):
            _pose(fps[ref], 0, 0, rot)
            jog = _pad_xy(fps[ref], str(leave))[across] - _pad_xy(fps[ref], str(entry))[across]
            if abs(jog) > JOG_MIN_MM:
                here += jog
                counts.append(0)
    keep = max(range(len(counts)), key=lambda i: (counts[i], -i))
    base = next(o for o, s in zip(offsets, stretch) if s == keep)
    return [round(float(line) + o - base, 4) for o in offsets]


def _derive_layout(fps, spec, square=False, choice=0):
    """Rotations and bias positions from the netlist, so that a new board needs only its parts and nets.

    The row: each part's input and output pads on the line, facing along it. A two-pin part with one pin on an RF net is a tap, reaching
    up if its other end is a rail and down if that's ground. Its RF pad is on the line, unless `rf_layout.tap_stub_mm` is set: then it sits
    beside the line, that far from the line's edge, on a stub. Rails rise from each tap in one column: the
    smallest capacitor nearest the choke, then the bulk, then any link onward. Capacitors go in pairs, the first pair at the rail's
    feed, where the cable's RF arrives, and the next at the load.

    A part with more than two pads is aimed pad to pad, which turns it to an odd angle when its RF pads sit on different corners. With
    `square`, such a part is set square instead, at `_square_rot`'s `choice`, and the line jogs across the row at it: see `_row_lines`.
    """
    layout, pins = spec["layout"], spec["pins"]
    chain, nets, parts = spec["chain"], spec["nets"], spec["parts"]
    axis = layout["axis"]
    travel = (0.0, 1.0) if axis == "y" else (1.0, 0.0)
    up = (1.0, 0.0) if axis == "y" else (0.0, -1.0)
    down = (-up[0], -up[1])
    rules = spec.get("rf_layout") or {}
    stub = rules.get("tap_stub_mm")
    widths = spec.get("net_width") or {}
    chain_nets = {c[0] for c in chain}
    on_row = {x.split(".")[0] for c in chain for x in c[1:]}
    taps = {}
    for net, entry, leave in chain:
        for member in nets[net]:
            ref, pin = member.split(".")
            if member not in (entry, leave) and ref not in on_row and ref not in taps and len(pins[ref]) <= 2:
                taps[ref] = (net, pin)
    # Rail pairs: which anchor pad carries which capacitors.
    anchors = {}                                        # rail net -> anchor pins, feed first
    for net, _entry, _leave in reversed(chain):
        for ref, (tap_net, pin) in taps.items():
            if tap_net == net:
                far = next((p for p in pins[ref] if p != pin), None)
                rail = pins[ref].get(far)
                if rail and rail != "GND" and rail not in chain_nets:
                    anchors.setdefault(rail, []).append(ref + "." + far)
    done = {r for r in taps} | on_row
    stacks = {}                                         # anchor pin -> the parts rising from it, in order

    def rail_plan(rail):
        here = anchors.get(rail, [])
        members = [m.split(".") for m in nets[rail]]
        loose = [(r, p) for r, p in members if r not in done and r in pins and len(pins[r]) == 2]
        caps = sorted([x for x in loose if pins[x[0]].get(next((q for q in pins[x[0]] if q != x[1]), "")) == "GND" and x[0][0] == "C"],
                      key=lambda x: _farads(parts[x[0]].get("value")))
        others = [x for x in loose if x not in caps]
        links = [x for x in others if pins[x[0]].get(next((q for q in pins[x[0]] if q != x[1]), "")) != "GND"]
        return here, caps, others, links
    # Load ends first, so that a link up to the supply belongs to the rail it feeds, and the supply then knows its anchors.
    order = list(anchors)[::-1]
    seen = set()
    while order:
        rail = order.pop(0)
        if rail in seen:
            continue
        seen.add(rail)
        here, caps, others, links = rail_plan(rail)
        if not here:
            continue
        pairs = max(1, len(caps) // 2)
        for a in here:
            stacks.setdefault(a, [])
        # Further anchors reached through a link on another rail arrive here before their capacitors are shared out.
        for i, a in enumerate(here[:pairs]):
            # One small and one bulk at each anchor, the small one nearest the choke.
            stacks[a] += caps[i::pairs]
        last = here[min(pairs, len(here)) - 1]
        for ref, pin in others:
            stacks[last].append((ref, pin))
            done.add(ref)
            far = next((p for p in pins[ref] if p != pin), None)
            onward = pins[ref].get(far)
            if onward and onward not in ("GND",) and onward not in chain_nets:
                anchors.setdefault(onward, []).append(ref + "." + far)
                order.append(onward)
                seen.discard(onward)
        done.update(x[0] for x in caps)

    walk, attach = [], {}

    outline = spec["outline"]
    edge = (outline[0] - layout["line"]) if outline and up[0] else (layout["line"] if outline else 20.0)
    room = edge - 1.3

    def rise(group, anchor_pin, sign, climbed):
        ref, pin = anchor_pin.split(".")
        prev_fp, prev_pad = fps[ref], pin
        prev_rot = prev_fp.GetOrientationDegrees()
        for item in stacks.get(anchor_pin, []):
            r, p = item if isinstance(item, tuple) else item
            far = next(q for q in pins[r] if q != p)
            grounded = pins[r].get(far) == "GND"
            direction = (-travel[0], -travel[1]) if grounded else sign
            rot = _rot_for(fps[r], p, far, direction)
            gap = _reach(prev_fp, prev_rot, prev_pad, sign) + _reach(fps[r], rot, p, (-sign[0], -sign[1])) + 0.15
            height = climbed[0] + gap + _reach(fps[r], rot, far, sign) + _pad_span(fps[r], rot, p, far, sign)
            if not grounded and height > room:
                # No height left for a link, so its branch turns and runs along the board instead.
                sign = (-travel[0], -travel[1])
                rot = _rot_for(fps[r], p, far, sign)
                gap = _reach(prev_fp, prev_rot, prev_pad, sign) + _reach(fps[r], rot, p, (-sign[0], -sign[1])) + 0.15
            group.append({"ref": r, "rot": rot, "pad": p, "to": prev_fp.GetReference() + "." + prev_pad, "dx": sign[0] * gap, "dy": sign[1] * gap})
            climbed[0] += gap if sign in (up, down) else 0.0
            if not grounded:
                climbed[0] += _pad_span(fps[r], rot, p, far, sign) if sign in (up, down) else 0.0
                rise(group, r + "." + far, sign, climbed)
                prev_fp, prev_pad, prev_rot = fps[r], far, rot
            else:
                prev_fp, prev_pad, prev_rot = fps[r], p, rot

    rows = []
    for net, entry, leave in chain:
        ref = entry.split(".")[0]
        if not walk or walk[-1][3] is None:
            walk = []
            rows.append(walk)
            walk.append([ref, _rot_for(fps[ref], None, entry.split(".")[1], travel), None, entry.split(".")[1]])
        for ref, (tap_net, pin) in taps.items():
            if tap_net != net:
                continue
            far = next((p for p in pins[ref] if p != pin), None)
            grounded = far is None or pins[ref].get(far) in ("GND", None)
            side = down if grounded else up
            rot = _rot_for(fps[ref], pin, far, side) if far else 0.0
            half_line = float(widths.get(net, spec.get("rf_line_width_mm") or 0.0)) / 2
            off = tap_offset(half_line, stub, _pad_reach(fps[ref], rot, pin, (-side[0], -side[1]))) if far else 0.0
            walk.append([ref, rot, pin, pin, [round(side[0] * off, 4), round(side[1] * off, 4)]])
            if far and not grounded:
                fps[ref].SetOrientationDegrees(rot)
                group = []
                _pose(fps[ref], 0, 0, rot)
                px, py = _pad_xy(fps[ref], pin)
                fx, fy = _pad_xy(fps[ref], far)
                # The stack climbs from the line, so it starts the tap's offset higher.
                rise(group, ref + "." + far, up, [abs((fx - px) * up[0] + (fy - py) * up[1]) + off])
                attach[ref] = group
        ref, pin = leave.split(".")
        following = next((c for c in chain if c[1].split(".")[0] == ref), None)
        if following:
            out_pin = following[1].split(".")[1]
            rot = _rot_for(fps[ref], pin, out_pin, travel)
            if square and len(list(fps[ref].Pads())) > 2 and not _is_square(rot):
                half = max(float(widths.get(n, spec.get("rf_line_width_mm") or 0.0)) for n in (net, following[0])) / 2 + EXIT_CLEARANCE_MM
                rot = _square_rot(fps[ref], pin, out_pin, travel, pins.get(ref) or {}, half, rot, choice)
            walk.append([ref, rot, pin, out_pin])
        else:
            walk.append([ref, _rot_for(fps[ref], None, pin, (-travel[0], -travel[1])), pin, None])
    # A person's rotation for a part wins over the derived one. A part whose RF pads sit on opposite corners, like a TT1224
    # limiter, is otherwise turned to put both on the line, which can make it far longer along the row.
    turned = layout.get("rotation") or {}
    for walk_row in rows:
        for item in walk_row:
            if item[0] in turned:
                item[1] = float(turned[item[0]])
    return rows, attach


def _solve_row(fps, layout):
    """Walk the cascade along its line: each input pad goes on the line as close as the courtyards allow.

    `lines`, from `_row_lines`, gives the line at each part where it jogs. Left out, the line is straight."""
    axis = 1 if layout["axis"] == "y" else 0
    line, start = layout["line"], layout["start"]
    lines = layout.get("lines") or [line] * len(layout["walk"])
    gap, step = layout.get("gap_mm", 0.1), 0.05
    placed, inline, previous = [], [], None
    groups = layout.get("attach") or {}

    def point(s):
        return (line, s) if axis else (s, line)

    for row, line in zip(layout["walk"], lines):
        ref, rot, entry, leave = row[:4]
        # A tap's pad sits off the line, beside it, by this much.
        ox, oy = row[4] if len(row) > 4 else (0.0, 0.0)
        entry, leave = (str(entry) if entry is not None else None), (str(leave) if leave is not None else None)
        fp = fps[ref]
        if previous is None:
            _pose(fp, *point(start), rot)
            # Bring the pad that faces the cascade onto the line, keeping the origin where it was asked for.
            px, py = _pad_xy(fp, leave)
            if axis:
                _pose(fp, line - (px - line), start, rot)
            else:
                _pose(fp, start, line - (py - line), rot)
            group = []
        else:
            s = previous + 0.3
            while True:
                at = point(s)
                _at_pad(fp, rot, entry, at[0] + ox, at[1] + oy)
                group = [_attach(fps, item) for item in groups.get(ref, [])]
                clash = _hit(fp, inline, gap) or _hit(fp, [x for x in placed if x not in inline], 0.1)
                clash = clash or next((h for h in (_hit(g, placed + [fp] + [o for o in group if o is not g], 0.1)
                                                   for g in group) if h), None)
                if not clash:
                    break
                s += step
                if s > start + 500:
                    raise SystemExit("no room for %s on the line: it keeps hitting %s" % (ref, clash))
        placed += group + [fp]
        inline.append(fp)
        previous = _pad_xy(fp, leave)[axis] if leave else None
    return placed


def _extent(group, axis):
    boxes = [_courtyard(fp, 0).BBox() for fp in group]
    if axis == "y":
        return min(pcbnew.ToMM(b.GetLeft()) for b in boxes), max(pcbnew.ToMM(b.GetRight()) for b in boxes)
    return min(pcbnew.ToMM(b.GetTop()) for b in boxes), max(pcbnew.ToMM(b.GetBottom()) for b in boxes)


def _shift(group, dx, dy):
    for fp in group:
        q = fp.GetPosition()
        fp.SetPosition(pcbnew.VECTOR2I(q.x + pcbnew.FromMM(dx), q.y + pcbnew.FromMM(dy)))


def _box_mm(fp, gap=0.0):
    b = _courtyard(fp, 0).BBox()
    return (pcbnew.ToMM(b.GetLeft()) - gap, pcbnew.ToMM(b.GetTop()) - gap, pcbnew.ToMM(b.GetRight()) + gap, pcbnew.ToMM(b.GetBottom()) + gap)


def _line_bands(boxes, lines, band, axis, x0, y0, x1, y1):
    """The keep-out along the RF line, one box for each straight stretch of it. A stretch runs from the part where the line last jogged,
    so that the hop out of that part is covered, to its own last part. The first and last reach a millimetre past the row."""
    starts = [i for i in range(len(lines)) if i == 0 or abs(lines[i] - lines[i - 1]) > 1e-6]
    out = []
    for k, i in enumerate(starts):
        j = starts[k + 1] - 1 if k + 1 < len(starts) else len(lines) - 1
        first, last = boxes[max(i - 1, 0)], boxes[j]
        if axis == "x":
            lo, hi = (x0 - 1 if k == 0 else first[0]), (x1 + 1 if k == len(starts) - 1 else last[2])
            out.append((lo, lines[i] - band, hi, lines[i] + band))
        else:
            lo, hi = (y0 - 1 if k == 0 else first[1]), (y1 + 1 if k == len(starts) - 1 else last[3])
            out.append((lines[i] - band, lo, lines[i] + band, hi))
    return out


def _seat_channel_parts(fps, row, refs, spec, attach, walk, lines=None):
    """Put a channel's own support parts in and beside its row, keeping off the RF line and the bias stacks."""
    layout, pins = spec["layout"], spec["pins"]
    axis, line = layout["axis"], float(layout["line"])
    boxes = [_box_mm(fp) for fp in row]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    margin = 12.0
    # The RF line and everything between a stack and the line stay clear, for the line itself and for its via fence.
    band = float(layout.get("rf_keepout_mm", 1.2))
    if lines and len(set(lines)) > 1:
        inline = [_box_mm(fps[w[0]]) for w in walk]
        obstacles = _line_bands(inline, lines, band, axis, x0, y0, x1, y1)
    else:
        obstacles = [(x0 - 1, line - band, x1 + 1, line + band)] if axis == "x" else [(line - band, y0 - 1, line + band, y1 + 1)]
    for fp in row:
        crowded = len(list(fp.Pads())) > 4
        obstacles.append(_box_mm(fp, 1.0 if crowded else 0.4))
    for walk_row, at in zip(walk, lines or [line] * len(walk)):
        for item in attach.get(walk_row[0], []):
            b = _box_mm(fps[item["ref"]], 0.4)
            obstacles.append((b[0], min(b[1], at), b[2], max(b[3], at)) if axis == "x" else (min(b[0], at), b[1], max(b[2], at), b[3]))
    net_pads = {}

    def note(fp):
        by_pin = pins.get(fp.GetReference(), {})
        for pad in fp.Pads():
            net = by_pin.get(pad.GetNumber())
            if net and net not in channel_groups.GROUND:
                q = pad.GetPosition()
                net_pads.setdefault(net, []).append((pcbnew.ToMM(q.x), pcbnew.ToMM(q.y)))

    for fp in row:
        note(fp)
    region = (x0, y0 - margin, x1, y1 + margin) if axis == "x" else (x0 - margin, y0, x1 + margin, y1)
    low, high = (y0, y1) if axis == "x" else (x0, x1)
    out = []
    for ref in channel_groups.seating_order(refs, pins, {fp.GetReference() for fp in row}):
        fp = fps[ref]
        crowded = len(list(fp.Pads())) > 4
        shapes = {}
        for rot in (0.0, 90.0):
            _pose(fp, 0, 0, rot)
            by_pin = pins.get(ref, {})
            pads = []
            for pad in fp.Pads():
                net = by_pin.get(pad.GetNumber())
                if net:
                    q = pad.GetPosition()
                    pads.append((net, pcbnew.ToMM(q.x), pcbnew.ToMM(q.y)))
            shapes[rot] = {"box": _box_mm(fp, 0.8 if crowded else 0.3), "pads": pads}
        spot = channel_groups.seat(shapes, obstacles, net_pads, region, stack_low=low, stack_high=high, axis=axis)
        if spot is None:
            return out, ref
        _pose(fp, spot[0], spot[1], spot[2])
        obstacles.append(_box_mm(fp, 0.3))
        note(fp)
        out.append(fp)
    return out, None


def _place_rows(fps, layout, rows, attach, spec=None):
    """Each channel as its own row, solved alone and then stacked below the one before, clear of its bias stacks."""
    placed, floor = [], None
    gap = float(layout.get("row_gap_mm", 1.3))
    by_row = {}
    if spec and len(rows) > 1 and layout.get("channel_parts", "beside") == "beside":
        members = [{w[0] for w in walk} | {i["ref"] for w in walk for i in attach.get(w[0], [])} for walk in rows]
        owners = channel_groups.channel_owners(spec["pins"], members)
        owners.update(channel_groups.spread_bypass(spec["pins"], members, owners))
        for ref, i in owners.items():
            if ref in fps:
                by_row.setdefault(i, []).append(ref)
    elif spec and len(rows) == 1 and layout.get("channel_parts", "beside") == "beside":
        # One path: every other part with a net to place by goes round it, nearest what it joins, in place of a shelf sorted by size.
        members = {w[0] for w in rows[0]} | {i["ref"] for w in rows[0] for i in attach.get(w[0], [])}
        by_row[0] = [r for r in fps if r not in members and any(n not in channel_groups.GROUND and not n.startswith("unconnected-") for n in spec["pins"].get(r, {}).values())]
    columns = max(int(layout.get("columns", 1)), 1)
    per_column = -(-len(rows) // columns)
    column_gap = float(layout.get("column_gap_mm", 3.0))
    column_shift, reach, home, first_top = 0.0, None, 0.0, 0.0
    for index, walk in enumerate(rows):
        first = fps[walk[0][0]]
        _pose(first, 0, 0, walk[0][1])
        box = _courtyard(first, 0).BBox()
        low = pcbnew.ToMM(box.GetTop() if layout["axis"] == "y" else box.GetLeft())
        lines = _row_lines(fps, walk, attach, layout["line"], layout["axis"])
        row = _solve_row(fps, {**layout, "walk": walk, "attach": attach, "start": layout.get("start", 0.9 - low), "lines": lines})
        if by_row.get(index):
            # A part that finds no room goes to the shelf with the shared ones rather than failing the board.
            row += _seat_channel_parts(fps, row, by_row[index], spec, attach, walk, lines)[0]
        across, stack = ("y", "x") if layout["axis"] == "x" else ("x", "y")
        if index == 0:
            home, first_top = _extent(row, across)[0], _extent(row, stack)[0]
        elif index % per_column == 0:
            # A new column: along by the width of everything so far, and back to the top.
            floor, column_shift = None, reach + column_gap - home
        if column_shift:
            _shift(row, column_shift, 0) if layout["axis"] == "x" else _shift(row, 0, column_shift)
        top = _extent(row, stack)[0]
        step = (floor + gap - top) if floor is not None else (first_top - top)
        _shift(row, step, 0) if layout["axis"] == "y" else _shift(row, 0, step)
        floor = _extent(row, stack)[1]
        end = _extent(row, across)[1]
        reach = end if reach is None else max(reach, end)
        placed += row
    return placed


def _shelf(extras, placed, layout):
    """Parts on no row, such as regulators and headers, packed in lines below the rows for the router to reach. Largest first."""
    if not extras:
        return []
    def size(fp):
        b = _courtyard(fp, 0).BBox()
        return pcbnew.ToMM(b.GetWidth()), pcbnew.ToMM(b.GetHeight())
    boxes = [_courtyard(fp, 0).BBox() for fp in placed]
    left = min(pcbnew.ToMM(b.GetLeft()) for b in boxes)
    right = max(pcbnew.ToMM(b.GetRight()) for b in boxes)
    y = max(pcbnew.ToMM(b.GetBottom()) for b in boxes) + 1.5
    x = left
    span = max(right - left, 20.0)
    tallest = 0.0
    gap = 0.8
    for fp in sorted(extras, key=lambda f: -max(size(f))):
        _pose(fp, 0, 0, 0)
        w, h = size(fp)
        if x + w > left + span and x > left:
            x, y, tallest = left, y + tallest + gap, 0.0
        box = _courtyard(fp, 0).BBox()
        _pose(fp, x - pcbnew.ToMM(box.GetLeft()), y - pcbnew.ToMM(box.GetTop()), 0)
        x += w + gap
        tallest = max(tallest, h)
    return list(extras)


def _outline(board, x1, y1, x2, y2):
    shape = pcbnew.PCB_SHAPE(board)
    shape.SetShape(pcbnew.SHAPE_T_RECT)
    shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(x1), pcbnew.FromMM(y1)))
    shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(x2), pcbnew.FromMM(y2)))
    shape.SetLayer(pcbnew.Edge_Cuts)
    shape.SetWidth(pcbnew.FromMM(0.1))
    board.Add(shape)


def _pour(board, net, layer, x1, y1, x2, y2, clearance):
    zone = pcbnew.ZONE(board)
    zone.SetLayer(layer)
    zone.SetNet(net)
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)):
        outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
    zone.SetLocalClearance(pcbnew.FromMM(clearance))
    zone.SetMinThickness(pcbnew.FromMM(0.15))
    zone.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
    board.Add(zone)


def fits(span, outline, margin=0.5):
    """Whether parts spanning (left, right, top, bottom) sit inside an outline from the origin, with a margin. No outline always fits."""
    if outline is None:
        return True
    left, right, top, bottom = span
    return left >= margin and top >= margin and right <= outline[0] - margin and bottom <= outline[1] - margin


def build_board(seed, spec_path, dst):
    """Parts, nets, solved positions, outline and ground pours on the seed's stackup. No tracks."""
    spec = json.load(open(spec_path))
    layout = spec["layout"]
    board = pcbnew.LoadBoard(seed)
    nets = {}
    for pins in spec["pins"].values():
        for name in pins.values():
            if name not in nets:
                nets[name] = pcbnew.NETINFO_ITEM(board, name)
                board.Add(nets[name])
    fps, problems, unlisted = {}, [], []
    opens = set(spec["open_pins"])
    for ref, part in spec["parts"].items():
        fp = _load_footprint(layout["footprints"][ref], spec["lib_dirs"])
        fp.SetReference(ref)
        # As schematic.shown_value: KiCad reads a symbol's empty Value as its reference, so the footprint says the same, or parity fails.
        fp.SetValue(str(part.get("value") or "").strip() or ref)
        fp.SetPath(pcbnew.KIID_PATH("/" + spec["uuids"][ref]))
        on_sheet = (spec.get("sheet_of") or {}).get(ref)
        fp.SetSheetname(on_sheet["name"] if on_sheet else "/")
        fp.SetSheetfile(on_sheet["file"] if on_sheet else os.path.basename(dst).replace(".kicad_pcb", ".kicad_sch"))
        fp.SetDNP(bool(part.get("dnp")))
        field = pcbnew.PCB_FIELD(fp, pcbnew.FIELD_T_USER, "MPN")
        field.SetText(str(part.get("mpn", "")))
        field.SetLayer(pcbnew.F_Fab)
        field.SetVisible(False)
        fp.Add(field)
        board.Add(fp)
        fps[ref] = fp
        numbers = set()
        for pad in fp.Pads():
            number = pad.GetNumber()
            if not number:
                continue
            numbers.add(number)
            name = spec["pins"][ref].get(number)
            if name:
                pad.SetNet(nets[name])
            elif ref + "." + number in opens:
                # The name KiCad gives a no-connect pin, so that schematic parity holds.
                name = "unconnected-(%s-Pad%s)" % (ref, number)
                if name not in nets:
                    nets[name] = pcbnew.NETINFO_ITEM(board, name)
                    board.Add(nets[name])
                pad.SetNet(nets[name])
            elif layout.get("unlisted_pads") == "open":
                unlisted.append("%s.%s" % (ref, number))
            else:
                problems.append("%s.%s is on the footprint but on no net" % (ref, number))
        problems += ["%s.%s is on a net but not on the footprint" % (ref, n) for n in spec["pins"][ref] if n not in numbers]
    if problems:
        raise SystemExit("pin and pad counts disagree on %d pads, which may be pins the schematic dropped. Confirm against the datasheet, "
                         "then list them in open_pins, or set layout.unlisted_pads to open. First: %s" % (len(problems), "; ".join(problems[:10])))
    def place(square, choice=0):
        rows, attach = _derive_layout(fps, spec, square, choice)
        placed = _place_rows(fps, layout, rows, attach, spec)
        extras = [x for x in fps.values() if x not in placed]
        placed += _shelf(extras, placed, layout)
        boxes = [_courtyard(fp, 0).BBox() for fp in placed]
        return rows, placed, (min(pcbnew.ToMM(b.GetLeft()) for b in boxes), max(pcbnew.ToMM(b.GetRight()) for b in boxes),
                              min(pcbnew.ToMM(b.GetTop()) for b in boxes), max(pcbnew.ToMM(b.GetBottom()) for b in boxes))

    rows, placed, span = place(False)
    squared = []
    if not fits(span, spec["outline"]):
        # A part turned to an odd angle to put two corner pads on the line is longer along the row than it is square. Where the row
        # doesn't fit, such parts are set square and the line jogs at them. A board that fits is never changed by this.
        # Each such part tries its square rotations in `_square_rot`'s order, least jog first, until the row fits.
        turned = {w[0]: w[1] for row in rows for w in row}
        tried = []
        for choice in range(4):
            rows2, placed2, span2 = place(True, choice)
            rotations = {w[0]: w[1] for row in rows2 for w in row}
            if rotations == turned or rotations in tried:
                break
            tried.append(rotations)
            if fits(span2, spec["outline"]):
                break
        if tried and not fits(span2, spec["outline"]):
            rows2, placed2, span2 = place(True, 0)     # none fits: report the first, with the least jog
            rotations = tried[0]
        if tried:
            squared = sorted(r for r in rotations if rotations[r] != turned.get(r))
            placed, span = placed2, span2
        else:
            rows, placed, span = place(False)
    left, right, top, bottom = span
    if spec["outline"] is None:
        # No outline given: the smallest that holds the parts with room for the pour and stitching, with the parts moved to suit.
        side = 2.5
        dx, dy = side - left, side - top
        _shift(placed, dx, dy)
        left, right, top, bottom = left + dx, right + dx, top + dy, bottom + dy
        spec["outline"] = [round(right + side + 0.05, 1), round(bottom + side + 0.05, 1)]
    w, h = spec["outline"]
    if left < 0.5 or top < 0.5 or right > w - 0.5 or bottom > h - 0.5:
        raise SystemExit("parts span x %.2f..%.2f, y %.2f..%.2f, outside the %g x %g mm outline with a 0.5 mm margin%s"
                         % (left, right, top, bottom, w, h, ", with %s set square" % ", ".join(squared) if squared else ""))
    _outline(board, 0, 0, w, h)
    inset = 0.6
    ground = nets["GND"]
    for layer in board.GetEnabledLayers().CuStack():
        _pour(board, ground, layer, inset, inset, w - inset, h - inset, layout.get("pour_clearance_mm", 0.3))
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"parts": len(placed), "unlisted_pads": unlisted, "set_square": squared, "span_mm": [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)],
                      "outline_mm": [w, h], "positions": {r: [round(pcbnew.ToMM(f.GetPosition().x), 3),
                                                              round(pcbnew.ToMM(f.GetPosition().y), 3),
                                                              f.GetOrientationDegrees()] for r, f in fps.items()}}))


def _local_clearance(pad, fp):
    """A pad's own clearance override in mm, or its footprint's, or None when the net class rules. Land patterns set these:
    KiCad's TT1224 PL-258 asks for 0.94 mm round its RF pads."""
    for owner in (pad, fp):
        get = getattr(owner, "GetLocalClearance", None)
        value = get() if get else None
        if value:
            return pcbnew.ToMM(value)
    return None


def board_facts(src, out):
    """True pad shapes, angles and courtyards, which the file reader only approximates at odd rotations."""
    board = pcbnew.LoadBoard(src)
    pads, yards = [], []
    for fp in board.GetFootprints():
        box = _courtyard(fp, 0).BBox()
        yards.append({"ref": fp.GetReference(), "x1": pcbnew.ToMM(box.GetLeft()), "y1": pcbnew.ToMM(box.GetTop()),
                      "x2": pcbnew.ToMM(box.GetRight()), "y2": pcbnew.ToMM(box.GetBottom()), "rot": fp.GetOrientationDegrees(),
                      "back": bool(fp.IsFlipped()), "value": fp.GetValue()})
        for pad in fp.Pads():
            p, size = pad.GetPosition(), pad.GetSize(pcbnew.F_Cu)
            pads.append({"ref": fp.GetReference(), "pad": pad.GetNumber(), "net": pad.GetNetname(),
                         "x": pcbnew.ToMM(p.x), "y": pcbnew.ToMM(p.y), "w": pcbnew.ToMM(size.x), "h": pcbnew.ToMM(size.y),
                         "rot": pad.GetOrientationDegrees(), "back": pad.IsOnLayer(pcbnew.B_Cu), "drill": pcbnew.ToMM(pad.GetDrillSizeX()),
                         "front": pad.IsOnLayer(pcbnew.F_Cu), "clearance": _local_clearance(pad, fp)})
    # Rule areas that forbid vias, the board's own and each footprint's (KiCad's U.FL has one between its ground
    # pads). A via goes through every layer, so a keepout on any layer rules a site out.
    keepouts = []
    zones = list(board.Zones()) + [(fp.GetReference(), z) for fp in board.GetFootprints() for z in fp.Zones()]
    for item in zones:
        ref, zone = item if isinstance(item, tuple) else ("", item)
        if not (zone.GetIsRuleArea() and zone.GetDoNotAllowVias()):
            continue
        outline = zone.Outline()
        for i in range(outline.OutlineCount()):
            chain = outline.Outline(i)
            pts = [[pcbnew.ToMM(chain.CPoint(j).x), pcbnew.ToMM(chain.CPoint(j).y)] for j in range(chain.PointCount())]
            keepouts.append({"ref": ref, "name": zone.GetZoneName(), "pts": pts})
    box = board.GetBoardEdgesBoundingBox()
    json.dump({"pads": pads, "courtyards": yards, "keepouts": keepouts,
               "outline": [pcbnew.ToMM(box.GetLeft()), pcbnew.ToMM(box.GetTop()), pcbnew.ToMM(box.GetRight()), pcbnew.ToMM(box.GetBottom())]},
              open(out, "w"))
    print(json.dumps({"pads": len(pads)}))


def place_silk(src, dst):
    """Put each reference where it clears pads, other references and the edge. One that fits nowhere is hidden, and reported."""
    board = pcbnew.LoadBoard(src)
    edge = board.GetBoardEdgesBoundingBox()
    margin = pcbnew.FromMM(0.35)
    clear = pcbnew.FromMM(0.12)
    def rect(box):
        return box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()
    obstacles = []
    for fp in board.GetFootprints():
        obstacles += [rect(pad.GetBoundingBox()) for pad in fp.Pads()]
        obstacles += [rect(g.GetBoundingBox()) for g in fp.GraphicalItems() if g.GetLayer() == pcbnew.F_SilkS and g.GetClass() != "PCB_TEXT"]

    def hits(box, others):
        x1, y1, x2, y2 = box
        return any(x1 - clear < o[2] and x2 + clear > o[0] and y1 - clear < o[3] and y2 + clear > o[1] for o in others)
    placed, hidden = [], []
    for fp in sorted(board.GetFootprints(), key=lambda f: f.GetReference()):
        fp.Value().SetVisible(False)
        text = fp.Reference()
        text.SetVisible(True)
        text.SetTextHeight(pcbnew.FromMM(0.8))
        text.SetTextWidth(pcbnew.FromMM(0.8))
        text.SetTextThickness(pcbnew.FromMM(0.12))
        text.SetKeepUpright(True)
        yard = _courtyard(fp, 0).BBox()
        cx, cy = (yard.GetLeft() + yard.GetRight()) // 2, (yard.GetTop() + yard.GetBottom()) // 2
        done = False
        for angle in (0, 90):
            text.SetTextAngleDegrees(angle)
            box = text.GetBoundingBox()
            hw, hh = box.GetWidth() // 2, box.GetHeight() // 2
            for extra in (0.1, 0.4, 0.8, 1.2, 1.8, 2.6):
                e = pcbnew.FromMM(extra)
                for x, y in ((cx, yard.GetTop() - hh - e), (cx, yard.GetBottom() + hh + e), (yard.GetLeft() - hw - e, cy), (yard.GetRight() + hw + e, cy)):
                    text.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
                    box = rect(text.GetBoundingBox())
                    inside = (box[0] > edge.GetLeft() + margin and box[2] < edge.GetRight() - margin and
                              box[1] > edge.GetTop() + margin and box[3] < edge.GetBottom() - margin)
                    if inside and not hits(box, obstacles) and not hits(box, placed):
                        placed.append(box)
                        done = True
                        break
                if done:
                    break
            if done:
                break
        if not done:
            text.SetVisible(False)
            hidden.append(fp.GetReference())
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"placed": len(placed), "hidden": hidden}))


def z_coupon(src, net, width, dst):
    """One long straight line down the middle of a board already stripped of parts and tracks.

    Hops packed between parts are too short for a cross-section reading, so width is solved here instead.
    """
    board = pcbnew.LoadBoard(src)
    box = board.GetBoardEdgesBoundingBox()
    x1, y1, x2, y2 = (pcbnew.ToMM(v) for v in (box.GetLeft(), box.GetTop(), box.GetRight(), box.GetBottom()))
    if x2 - x1 >= y2 - y1:
        a, b = (x1 + (x2 - x1) * 0.15, (y1 + y2) / 2), (x2 - (x2 - x1) * 0.15, (y1 + y2) / 2)
    else:
        a, b = ((x1 + x2) / 2, y1 + (y2 - y1) * 0.15), ((x1 + x2) / 2, y2 - (y2 - y1) * 0.15)
    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(a[0]), pcbnew.FromMM(a[1])))
    track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(b[0]), pcbnew.FromMM(b[1])))
    track.SetWidth(pcbnew.FromMM(float(width)))
    track.SetLayer(pcbnew.F_Cu)
    if net not in board.GetNetsByName():
        board.Add(pcbnew.NETINFO_ITEM(board, net))
    track.SetNet(board.GetNetsByName()[net])
    board.Add(track)
    pcbnew.ZONE_FILLER(board).Fill(board.Zones())
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"length_mm": round(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5, 2)}))


def add_vias(src, spec, dst):
    board = pcbnew.LoadBoard(src)
    nets = board.GetNetsByName()
    items = json.load(open(spec))
    for item in items:
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(item["x"]), pcbnew.FromMM(item["y"])))
        via.SetWidth(pcbnew.FromMM(item["size"]))
        via.SetDrill(pcbnew.FromMM(item["drill"]))
        via.SetViaType(pcbnew.VIATYPE_THROUGH)
        via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        via.SetNet(nets[item["net"]])
        via.SetLocked(bool(item.get("locked")))
        board.Add(via)
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"added": len(items)}))


def export_footprints(src, pretty):
    """Each footprint a board uses, as a library, at rest, with a courtyard. A board from another tool often has few courtyards, and placement needs them."""
    board = pcbnew.LoadBoard(src)
    os.makedirs(pretty, exist_ok=True)
    made = {}
    added = 0
    for fp in board.Footprints():
        name = str(fp.GetFPID().GetLibItemName())
        if name in made:
            continue
        copy = pcbnew.FOOTPRINT(fp)
        if copy.IsFlipped():
            copy.Flip(copy.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        copy.SetOrientationDegrees(0)
        copy.SetPosition(pcbnew.VECTOR2I(0, 0))
        for pad in copy.Pads():
            pad.SetNetCode(0)
        copy.BuildCourtyardCaches()
        if copy.GetCourtyard(pcbnew.F_CrtYd).OutlineCount() == 0 and len(list(copy.Pads())):
            box = None
            for pad in copy.Pads():
                b = pad.GetBoundingBox()
                if box is None:
                    box = b
                else:
                    box.Merge(b)
            m = pcbnew.FromMM(0.25)
            rect = pcbnew.PCB_SHAPE(copy)
            rect.SetShape(pcbnew.SHAPE_T_RECT)
            rect.SetStart(pcbnew.VECTOR2I(box.GetLeft() - m, box.GetTop() - m))
            rect.SetEnd(pcbnew.VECTOR2I(box.GetRight() + m, box.GetBottom() + m))
            rect.SetLayer(pcbnew.F_CrtYd)
            rect.SetWidth(pcbnew.FromMM(0.05))
            copy.Add(rect)
            added += 1
        # Not pcbnew.FootprintSave: it picks its writer from what the folder already holds, and this one starts empty.
        pcbnew.PCB_IO_MGR.FindPlugin(pcbnew.PCB_IO_MGR.KICAD_SEXP).FootprintSave(pretty, copy)
        made[name] = True
    print(json.dumps({"footprints": len(made), "courtyards_added": added}))


def remove_tracks(src, spec, dst):
    """Remove the tracks with these ends, to 0.01 mm. Nothing else is touched."""
    board = pcbnew.LoadBoard(src)
    wanted = json.load(open(spec))
    removed = 0

    def at(point, xy):
        return abs(pcbnew.ToMM(point.x) - xy[0]) < 0.01 and abs(pcbnew.ToMM(point.y) - xy[1]) < 0.01
    for track in list(board.GetTracks()):
        if track.GetClass() == "PCB_VIA":
            continue
        if any(at(track.GetStart(), w["start"]) and at(track.GetEnd(), w["end"]) and track.GetNetname() == w["net"] for w in wanted):
            board.Remove(track)
            removed += 1
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"removed": removed}))


def add_voids(src, spec, dst):
    """Rule areas that keep pours off a layer. Tracks and vias are still allowed through, so only the plane changes."""
    board = pcbnew.LoadBoard(src)
    data = json.load(open(spec))
    layer = board.GetLayerID(data["layer"])
    for item in data["voids"]:
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer)
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowZoneFills(True)
        zone.SetDoNotAllowTracks(False)
        zone.SetDoNotAllowVias(False)
        zone.SetDoNotAllowPads(False)
        zone.SetDoNotAllowFootprints(False)
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in item["pts"]:
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        board.Add(zone)
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"voids": len(data["voids"])}))


def strip_inner_pads(src, nets_csv, radius, dst):
    """Take the unused inner-layer pads off signal vias beside an RF line. A through via's ring on the plane keeps the pour back, and that antipad can reach under the trace."""
    board = pcbnew.LoadBoard(src)
    nets = set(nets_csv.split(","))
    radius = float(radius)
    lines = []
    for t in board.GetTracks():
        if t.GetClass() == "PCB_TRACK" and t.GetNetname() in nets:
            a, b = t.GetStart(), t.GetEnd()
            lines.append(tuple(pcbnew.ToMM(v) for v in (a.x, a.y, b.x, b.y)))

    def distance(x, y, x1, y1, x2, y2):
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        s = 0.0 if length == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length))
        return math.hypot(x - x1 - s * dx, y - y1 - s * dy)

    changed = 0
    for via in board.GetTracks():
        if via.GetClass() != "PCB_VIA" or via.GetNetname() == "GND":
            continue
        p = via.GetPosition()
        if any(distance(pcbnew.ToMM(p.x), pcbnew.ToMM(p.y), *line) < radius for line in lines) and not via.GetRemoveUnconnected():
            via.SetRemoveUnconnected(True)
            via.SetKeepStartEnd(True)
            changed += 1
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"changed": changed}))


def plane_coverage(src, nets_csv, layer_name, out):
    """How much of each RF trace's width has ground beneath it, from the stored fill. Sampled along and across each F.Cu segment."""
    board = pcbnew.LoadBoard(src)
    nets = set(nets_csv.split(","))
    layer = board.GetLayerID(layer_name)
    ground = pcbnew.SHAPE_POLY_SET()
    for zone in board.Zones():
        if zone.GetNetname() == "GND" and zone.IsOnLayer(layer) and not zone.GetIsRuleArea():
            ground.Append(zone.GetFilledPolysList(layer))
    found = {}
    for t in board.GetTracks():
        if t.GetClass() != "PCB_TRACK" or t.GetLayer() != pcbnew.F_Cu or t.GetNetname() not in nets:
            continue
        a, b = t.GetStart(), t.GetEnd()
        x1, y1, x2, y2 = (pcbnew.ToMM(v) for v in (a.x, a.y, b.x, b.y))
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        width = pcbnew.ToMM(t.GetWidth())
        row = found.setdefault(t.GetNetname(), {"coverage": 1.0, "at": [round(x1, 3), round(y1, 3)]})
        steps = max(1, int(length / 0.05))
        for i in range(steps + 1):
            x, y = x1 + ux * length * i / steps, y1 + uy * length * i / steps
            hit = sum(ground.Contains(pcbnew.VECTOR2I(pcbnew.FromMM(x - uy * (-width / 2 + width * k / 12)), pcbnew.FromMM(y + ux * (-width / 2 + width * k / 12)))) for k in range(13))
            if hit / 13 < row["coverage"]:
                row.update(coverage=hit / 13, at=[round(x, 3), round(y, 3)])
    json.dump(found, open(out, "w"))
    print(json.dumps({"nets": len(found)}))


def remove_vias(src, spec, dst):
    """Remove the vias at these points, to 0.01 mm. Other copper isn't touched."""
    board = pcbnew.LoadBoard(src)
    removed = 0
    points = json.load(open(spec))
    for track in list(board.GetTracks()):
        if track.GetClass() != "PCB_VIA":
            continue
        p = track.GetPosition()
        if any(abs(pcbnew.ToMM(p.x) - x) < 0.01 and abs(pcbnew.ToMM(p.y) - y) < 0.01 for x, y in points):
            board.Remove(track)
            removed += 1
    pcbnew.SaveBoard(dst, board)
    print(json.dumps({"removed": removed}))


if __name__ == "__main__":
    op = {"export_dsn": export_dsn, "import_ses": import_ses, "add_locked": add_locked, "fill_zones": fill_zones,
          "build_board": build_board, "board_facts": board_facts, "add_vias": add_vias, "remove_vias": remove_vias, "strip_inner_pads": strip_inner_pads, "plane_coverage": plane_coverage,
          "z_coupon": z_coupon, "place_silk": place_silk, "add_voids": add_voids, "remove_tracks": remove_tracks, "export_footprints": export_footprints}[sys.argv[1]]
    op(*sys.argv[2:])
    # pcbnew's bindings can segfault during interpreter shutdown, after the work is saved and reported.
    sys.stdout.flush()
    os._exit(0)
