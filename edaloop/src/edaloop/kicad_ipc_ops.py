"""The board edits of kicad_ops.py, through KiCad 11's IPC API instead of the pcbnew module KiCad 11 no longer ships.

  python kicad_ipc_ops.py <op> <args...>

Same op names, arguments and one JSON line on stdout as kicad_ops.py, so a caller can't tell which ran; `toolchain.kicad_op`
picks. Each op starts a headless `kicad-cli api-server` on its source board, makes its change in one commit, saves the result
(with its project, as pcbnew's SaveBoard does) and stops the server. A server that won't start or a request that fails exits
non-zero, so it can't read as an edit that happened.

Needs kicad-python 0.10 or later, and shapely for plane_coverage. $EDALOOP_KICAD_CLI names the kicad-cli to serve from.

The ops that need pcbnew's geometry on footprints KiCad hasn't placed yet, or its ratsnest, aren't here yet: see UNPORTED.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
from contextlib import contextmanager

from scripts.kicad_pcb import set_properties, to_local

try:
    from kipy import KiCad
    from kipy.board_types import Net, RuleAreaSettings, Track, Via, Zone
    from kipy.common_types import PolygonWithHoles
    from kipy.geometry import PolyLine, PolyLineNode, Vector2
    from kipy.proto.board import board_commands_pb2, board_jobs_pb2
    from kipy.proto.board.board_commands_pb2 import BoardFlipDirection
    from kipy.proto.board.board_types_pb2 import BoardLayer, UnconnectedLayerRemoval, ViaType, ZoneType
    from kipy.proto.common.commands import base_commands_pb2, editor_commands_pb2
    from kipy.proto.common.types import KIID, base_types_pb2, jobs_pb2
    from kipy.util import pack_any
    from kipy.util.units import from_mm, to_mm
except ImportError as missing:      # refused in main(), with how to install it
    KIPY_MISSING = missing
else:
    KIPY_MISSING = None

# A zone fill or a Specctra export on a big board takes minutes; the default 2 s would call a slow request a failure.
REQUEST_TIMEOUT_MS = 900_000

# Ops kicad_ops.py has that this file doesn't yet, each with why: main() refuses them by name. None now (#84).
UNPORTED = {}


@contextmanager
def board_at(src):
    """A headless KiCad with `src` open, and the board. The server is stopped however the op ends."""
    kicad = KiCad(headless=True, kicad_cli_path=os.environ.get("EDALOOP_KICAD_CLI") or None,
                  file_path=os.path.abspath(src), timeout_ms=REQUEST_TIMEOUT_MS)
    try:
        yield kicad.get_board()
    finally:
        kicad.close()


@contextmanager
def commit(board, what):
    """One commit for the op's changes. Dropped, and the op failed, if anything in it raises."""
    c = board.begin_commit()
    try:
        yield
    except BaseException:
        board.drop_commit(c)
        raise
    board.push_commit(c, "edaloop " + what)


def create(board, items):
    """Add items to the board, failing unless KiCad made every one.

    Sent directly rather than through kicad-python's Board.create_items, which on its main branch (6904820) unpacks each
    item KiCad returns twice and raises after the items are made."""
    cmd = editor_commands_pb2.CreateItems()
    cmd.header.document.CopyFrom(board.document)
    cmd.items.extend(pack_any(item.proto) for item in items)
    r = board.client.send(cmd, editor_commands_pb2.CreateItemsResponse)
    bad = [x.status.error_message or editor_commands_pb2.ItemStatusCode.Name(x.status.code)
           for x in r.created_items if x.status.code != editor_commands_pb2.ItemStatusCode.ISC_OK]
    if r.status != base_types_pb2.ItemRequestStatus.IRS_OK or bad or len(r.created_items) != len(cmd.items):
        raise SystemExit("KiCad made %d of %d items: %s" % (len(r.created_items) - len(bad), len(cmd.items),
                                                             "; ".join(bad) or base_types_pb2.ItemRequestStatus.Name(r.status)))


def save(board, src, dst):
    """Write the board to `dst` with a project beside it, as pcbnew's SaveBoard does: the source's project where it has one.

    The board and the project are written apart because the 10.99 nightly (c508d01f46) refuses SaveCopyOfDocument with
    the project included when `dst`'s project is already there, though it overwrites the board alone."""
    src, dst = os.path.abspath(src), os.path.abspath(dst)
    if dst == src:
        board.save()
        return
    project, theirs = os.path.splitext(dst)[0] + ".kicad_pro", os.path.splitext(src)[0] + ".kicad_pro"
    board.save_as(dst, overwrite=True, include_project=not os.path.exists(theirs) and not os.path.exists(project))
    if os.path.exists(theirs):
        shutil.copyfile(theirs, project)


def layer(name):
    """KiCad's canonical layer name ("F.Cu", "In2.Cu") as the API's layer id."""
    try:
        return BoardLayer.Value("BL_" + name.replace(".", "_"))
    except ValueError:
        raise SystemExit("no such layer: %s" % name) from None


def layer_name(value):
    return BoardLayer.Name(value)[3:].replace("_", ".", 1)


def xy(x, y):
    return Vector2.from_xy(from_mm(x), from_mm(y))


def mm(v):
    return to_mm(v.x), to_mm(v.y)


def nets_by_name(board):
    return {n.name: n for n in board.get_nets()}


def net(board, nets, name):
    if name not in nets:
        raise SystemExit("no net %s on the board" % name)
    return nets[name]


def copper(board):
    """Tracks, arcs and vias: what pcbnew's GetTracks() returns."""
    return list(board.get_tracks()) + list(board.get_vias())


def edge_box(board):
    """The board outline's bounding box in mm (left, top, right, bottom), from the shapes on Edge.Cuts."""
    edges = [s for s in board.get_shapes() if s.layer == BoardLayer.BL_Edge_Cuts]
    if not edges:
        raise SystemExit("the board has no outline on Edge.Cuts")
    boxes = [board.get_item_bounding_box(s) for s in edges]
    return (min(to_mm(b.pos.x) for b in boxes), min(to_mm(b.pos.y) for b in boxes),
            max(to_mm(b.pos.x + b.size.x) for b in boxes), max(to_mm(b.pos.y + b.size.y) for b in boxes))


def polygon(points):
    line = PolyLine()
    for x, y in points:
        line.append(PolyLineNode.from_xy(from_mm(x), from_mm(y)))
    line.closed = True
    poly = PolygonWithHoles()
    poly.outline = line
    return poly


def refill(board):
    board.refill_zones(block=True, max_poll_seconds=REQUEST_TIMEOUT_MS / 1000)


def run_job(board, command, output):
    command.job_settings.document.CopyFrom(board.document)
    command.job_settings.output_path = os.path.abspath(output)
    result = board.client.send(command, jobs_pb2.RunJobResponse)
    if result.status != jobs_pb2.JobStatus.JS_SUCCESS:
        raise SystemExit("%s failed: %s" % (type(command).__name__, result.message or jobs_pb2.JobStatus.Name(result.status)))
    return result


# ---------------------------------------------------------------- the ops, as in kicad_ops.py

def export_dsn(src, dst, dsn, keep_nets="GND", scope="unlocked"):
    """Remove copper, then export. Scope "unlocked" keeps locked items; "all" removes them too. Ground stays."""
    keep = set(keep_nets.split(","))
    with board_at(src) as board:
        items = copper(board)
        gone = [t for t in items if not ((t.locked and scope != "all") or t.net.name in keep)]
        if gone:
            with commit(board, "export_dsn"):
                board.remove_items(gone)
        save(board, src, dst)
    # Exported from the board as saved, in a session of its own: the export job reads the document KiCad loaded, not the
    # copper just removed from it.
    with board_at(dst) as saved:
        run_job(saved, board_jobs_pb2.RunBoardJobExportSpecctraDsn(), dsn)
    print(json.dumps({"removed": len(gone), "kept": len(items) - len(gone)}))


def add_locked(src, spec, dst):
    """Add locked tracks from a JSON list of net, start, end, width and layer."""
    wanted = json.load(open(spec))
    with board_at(src) as board:
        nets = nets_by_name(board)
        tracks = []
        for item in wanted:
            t = Track()
            t.start, t.end = xy(*item["start"]), xy(*item["end"])
            t.width = from_mm(item["width"])
            t.layer = layer(item["layer"])
            t.net = net(board, nets, item["net"])
            t.locked = True
            tracks.append(t)
        if tracks:
            with commit(board, "add_locked"):
                create(board, tracks)
        save(board, src, dst)
    print(json.dumps({"added": len(tracks)}))


def fill_zones(src, dst):
    with board_at(src) as board:
        refill(board)
        save(board, src, dst)
        zones = len(board.get_zones())
    print(json.dumps({"zones": zones}))


def import_ses(src, ses, dst):
    with board_at(src) as board:
        cmd = board_commands_pb2.ImportSpecctraSession()
        cmd.board.CopyFrom(board.document)
        cmd.session_path = os.path.abspath(ses)
        result = board.client.send(cmd, board_commands_pb2.ImportSpecctraSessionResponse)
        refill(board)
        save(board, src, dst)
        tracks = len(copper(board))
    # pcbnew's importer says only whether it read the file; this one also counts what it couldn't place, which is worth seeing.
    print(json.dumps({"tracks": tracks, "skipped": result.skipped_count}))


def z_coupon(src, net_name, width, dst):
    """One long straight line down the middle of a board already stripped of parts and tracks."""
    with board_at(src) as board:
        x1, y1, x2, y2 = edge_box(board)
        if x2 - x1 >= y2 - y1:
            a, b = (x1 + (x2 - x1) * 0.15, (y1 + y2) / 2), (x2 - (x2 - x1) * 0.15, (y1 + y2) / 2)
        else:
            a, b = ((x1 + x2) / 2, y1 + (y2 - y1) * 0.15), ((x1 + x2) / 2, y2 - (y2 - y1) * 0.15)
        t = Track()
        t.start, t.end = xy(*a), xy(*b)
        t.width = from_mm(float(width))
        t.layer = BoardLayer.BL_F_Cu
        # pcbnew adds the net when the board has none of that name; a track given a new name does the same here.
        t.net = nets_by_name(board).get(net_name) or Net(name=net_name)
        with commit(board, "z_coupon"):
            create(board, [t])
        refill(board)
        save(board, src, dst)
    print(json.dumps({"length_mm": round(math.hypot(a[0] - b[0], a[1] - b[1]), 2)}))


def add_vias(src, spec, dst):
    items = json.load(open(spec))
    with board_at(src) as board:
        nets = nets_by_name(board)
        vias = []
        for item in items:
            v = Via()
            v.type = ViaType.VT_THROUGH
            v.position = xy(item["x"], item["y"])
            v.diameter = from_mm(item["size"])
            v.drill_diameter = from_mm(item["drill"])
            v.net = net(board, nets, item["net"])
            v.locked = bool(item.get("locked"))
            vias.append(v)
        if vias:
            with commit(board, "add_vias"):
                create(board, vias)
        save(board, src, dst)
    print(json.dumps({"added": len(items)}))


def _at(point, x, y):
    px, py = mm(point)
    return abs(px - x) < 0.01 and abs(py - y) < 0.01


def remove_tracks(src, spec, dst):
    """Remove the tracks with these ends, to 0.01 mm. Nothing else is touched."""
    wanted = json.load(open(spec))
    with board_at(src) as board:
        gone = [t for t in board.get_tracks()
                if any(_at(t.start, *w["start"]) and _at(t.end, *w["end"]) and t.net.name == w["net"] for w in wanted)]
        if gone:
            with commit(board, "remove_tracks"):
                board.remove_items(gone)
        save(board, src, dst)
    print(json.dumps({"removed": len(gone)}))


def remove_vias(src, spec, dst):
    """Remove the vias at these points, to 0.01 mm. Other copper isn't touched."""
    points = json.load(open(spec))
    with board_at(src) as board:
        gone = [v for v in board.get_vias() if any(_at(v.position, x, y) for x, y in points)]
        if gone:
            with commit(board, "remove_vias"):
                board.remove_items(gone)
        save(board, src, dst)
    print(json.dumps({"removed": len(gone)}))


def add_voids(src, spec, dst):
    """Rule areas that keep pours off a layer. Tracks and vias are still allowed through, so only the plane changes."""
    data = json.load(open(spec))
    with board_at(src) as board:
        zones = []
        for item in data["voids"]:
            z = Zone()
            z.type = ZoneType.ZT_RULE_AREA
            z.layers = [layer(data["layer"])]
            z.outline = polygon(item["pts"])
            rules = RuleAreaSettings()
            rules.keepout_copper = True
            rules.keepout_tracks = rules.keepout_vias = rules.keepout_pads = rules.keepout_footprints = False
            z.rule_area_settings = rules
            zones.append(z)
        if zones:
            with commit(board, "add_voids"):
                create(board, zones)
        save(board, src, dst)
    print(json.dumps({"voids": len(data["voids"])}))


def update(board, items):
    """Change items on the board, failing unless KiCad changed every one. Direct, for the reason `create` gives."""
    cmd = editor_commands_pb2.UpdateItems()
    cmd.header.document.CopyFrom(board.document)
    cmd.items.extend(pack_any(item.proto) for item in items)
    r = board.client.send(cmd, editor_commands_pb2.UpdateItemsResponse)
    bad = [x.status.error_message or editor_commands_pb2.ItemStatusCode.Name(x.status.code)
           for x in r.updated_items if x.status.code != editor_commands_pb2.ItemStatusCode.ISC_OK]
    if r.status != base_types_pb2.ItemRequestStatus.IRS_OK or bad or len(r.updated_items) != len(cmd.items):
        raise SystemExit("KiCad changed %d of %d items: %s" % (len(r.updated_items) - len(bad), len(cmd.items),
                                                               "; ".join(bad) or base_types_pb2.ItemRequestStatus.Name(r.status)))


def unconnected(board):
    """The ratsnest's count, which the IPC API doesn't give: each net's pads in copper-joined groups, less one a net.

    pcbnew's GetUnconnectedCount(False) counts the ratsnest's edges, and a net's ratsnest joins its groups in a tree,
    so the two agree wherever every group holds a pad. Copper joined to no pad isn't in either."""
    by_net = {}
    for pad in board.get_pads():
        if pad.net.name:
            by_net.setdefault(pad.net.name, []).append(pad.id.value)
    count = 0
    for pads in by_net.values():
        left = set(pads)
        while left:
            seed = next(iter(left))
            reached = {item.id.value for item in board.get_connected_items(KIID(value=seed))}
            left -= reached | {seed}
            count += 1
        count -= 1
    return count


def _distance(x, y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    s = 0.0 if length == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length))
    return math.hypot(x - x1 - s * dx, y - y1 - s * dy)


def ends_to_centre(via_xy, via_width, ends, tol=0.001):
    """As kicad_ops.ends_to_centre: the track ends to move onto a via's centre before its unused inner pads come off."""
    vx, vy = via_xy
    return [key for key, x, y, width in ends if tol < math.hypot(x - vx, y - vy) <= (via_width + width) / 2]


def strip_inner_pads(src, nets_csv, radius, dst):
    """Take the unused inner-layer pads off signal vias beside an RF line, as kicad_ops.strip_inner_pads does.

    A track end on an inner layer that reaches the ring off its centre is moved onto the centre first, so KiCad sees it
    joined. If the strip still opens a net, the pads go back on the vias that opened it, and they're listed."""
    nets, radius = set(nets_csv.split(",")), float(radius)
    with board_at(src) as board:
        tracks = [t for t in board.get_tracks() if isinstance(t, Track)]
        lines = [(*mm(t.start), *mm(t.end)) for t in tracks if t.net.name in nets]
        before = unconnected(board)
        inner = [t for t in tracks if not t.locked and t.layer not in (BoardLayer.BL_F_Cu, BoardLayer.BL_B_Cu)]
        changed, moved, ends_moved = [], [], 0
        for via in board.get_vias():
            stack = via.padstack
            if via.net.name == "GND" or stack.unconnected_layer_removal != UnconnectedLayerRemoval.ULR_KEEP:
                continue
            x, y = mm(via.position)
            if not any(_distance(x, y, *line) < radius for line in lines):
                continue
            ends = [((t, end), *mm(getattr(t, end)), to_mm(t.width)) for t in inner if t.net.name == via.net.name
                    for end in ("start", "end")]
            for t, end in ends_to_centre((x, y), to_mm(via.diameter), ends):
                setattr(t, end, Vector2.from_xy(via.position.x, via.position.y))
                moved.append(t)
                ends_moved += 1
            stack.unconnected_layer_removal = UnconnectedLayerRemoval.ULR_REMOVE_EXCEPT_START_AND_END
            changed.append(via)
        kept = []
        if changed:
            with commit(board, "strip_inner_pads"):
                update(board, list({id(t): t for t in moved}.values()) + changed)
            if unconnected(board) > before:
                # One via at a time, the strips that opened something are given back.
                for via in changed:
                    now = unconnected(board)
                    via.padstack.unconnected_layer_removal = UnconnectedLayerRemoval.ULR_KEEP
                    with commit(board, "strip_inner_pads: give back"):
                        update(board, [via])
                    if unconnected(board) < now:
                        x, y = mm(via.position)
                        kept.append({"net": via.net.name, "at": [round(x, 3), round(y, 3)]})
                    else:
                        via.padstack.unconnected_layer_removal = UnconnectedLayerRemoval.ULR_REMOVE_EXCEPT_START_AND_END
                        with commit(board, "strip_inner_pads: strip again"):
                            update(board, [via])
        after = unconnected(board)
        save(board, src, dst)
    print(json.dumps({"changed": len(changed) - len(kept), "track_ends_centred": ends_moved, "kept_pads": kept,
                      "unconnected_before": before, "unconnected_after": after}))


# ---------------------------------------------------------------- reading what only KiCad knows

def _local_clearance(pad, fp):
    """A pad's own clearance override in mm, or its footprint's, or None when the net class rules."""
    own = pad.copper_clearance_override
    if own is not None:
        return round(to_mm(own), 4)
    theirs = getattr(fp, "copper_clearance_override", None) if fp is not None else None
    return round(to_mm(theirs), 4) if theirs is not None else None


def _keepouts(zones, ref, fp=None):
    """Rule areas that forbid vias. A footprint's own come back in its frame, not the board's as its pads do, so they're
    turned and moved by its pose here, as KiCad's RotatePoint does. A flipped footprint's local coordinates are already
    flipped (see scripts/kicad_pcb.py), so one transform serves both sides."""
    out = []
    if fp is not None:
        ox, oy = mm(fp.position)
        a = math.radians(fp.orientation.degrees)
        c, s = math.cos(a), math.sin(a)
    for zone in zones:
        rules = zone.rule_area_settings if zone.is_rule_area() else None
        if not (rules and rules.keepout_vias):
            continue
        pts = [[to_mm(n.point.x), to_mm(n.point.y)] for n in zone.outline.outline.nodes]
        if fp is not None:
            pts = [[ox + x * c + y * s, oy - x * s + y * c] for x, y in pts]
        out.append({"ref": ref, "name": zone.name, "pts": pts})
    return out


def _front_courtyard(board, fp):
    """A footprint's F.CrtYd outline as a box in nm (left, top, right, bottom), as pcbnew's _courtyard(fp, 0).BBox()
    gives it: (0, 0, 0, 0) when there's none, which is also what a flipped footprint, its courtyard on B.CrtYd, gets."""
    shapes = [s for s in fp.definition.shapes if s.layer == BoardLayer.BL_F_CrtYd]
    if not shapes:
        return 0, 0, 0, 0
    # KiCad's box round a shape takes in half its line width each side; pcbnew's courtyard is the line itself.
    boxes = [(board.get_item_bounding_box(s), s.attributes.stroke.width // 2) for s in shapes]
    return (min(b.pos.x + w for b, w in boxes), min(b.pos.y + w for b, w in boxes),
            max(b.pos.x + b.size.x - w for b, w in boxes), max(b.pos.y + b.size.y - w for b, w in boxes))


def _box(b):
    return b.pos.x, b.pos.y, b.pos.x + b.size.x, b.pos.y + b.size.y


def _box2(b):
    """A Box2 message from GetTextExtents as (left, top, right, bottom), nm."""
    return b.position.x_nm, b.position.y_nm, b.position.x_nm + b.size.x_nm, b.position.y_nm + b.size.y_nm


def place_silk(src, dst):
    """Put each reference where it clears pads, other references and the edge, as kicad_ops.place_silk does. One that fits
    nowhere is hidden, and reported.

    KiCad measures: each reference's box once for each angle (the box moves with the text), and every pad and silk
    line. The chosen places are then written into the file, not sent back through UpdateItems, which on the 10.99
    nightly (c508d01f46) saves a turned footprint's texts at the wrong angle (KiCad #26164). KiCad then reads the file back, and any
    reference that isn't where it was put fails the op."""
    margin, clear = from_mm(0.35), from_mm(0.12)
    size, thickness = 0.8, 0.12
    with board_at(src) as board:
        ex1, ey1, ex2, ey2 = (from_mm(v) for v in edge_box(board))
        fps = sorted(board.get_footprints(), key=lambda f: f.reference_field.text.value)
        pads = [p for fp in fps for p in fp.definition.pads]
        obstacles = [_box(b) for b in board.get_item_bounding_box(pads)] if pads else []
        silk = [s for fp in fps for s in fp.definition.shapes if s.layer == BoardLayer.BL_F_SilkS]
        obstacles += [_box(b) for b in board.get_item_bounding_box(silk)] if silk else []

        def hits(box, others):
            x1, y1, x2, y2 = box
            return any(x1 - clear < o[2] and x2 + clear > o[0] and y1 - clear < o[3] and y2 + clear > o[1] for o in others)
        placed, hidden, wanted = [], [], {}
        for fp in fps:
            ref = fp.reference_field.text.value
            text = fp.reference_field.text
            attrs = text.attributes
            attrs.size = Vector2.from_xy(from_mm(size), from_mm(size))
            attrs.stroke_width = from_mm(thickness)
            attrs.keep_upright = True
            yx1, yy1, yx2, yy2 = _front_courtyard(board, fp)
            cx, cy = (yx1 + yx2) // 2, (yy1 + yy2) // 2
            at, choice = text.position, None
            for angle in (0, 90):
                b = _box2(_text_box(board, text, angle))
                hw, hh = (b[2] - b[0]) // 2, (b[3] - b[1]) // 2
                for extra in (0.1, 0.4, 0.8, 1.2, 1.8, 2.6):
                    e = from_mm(extra)
                    for x, y in ((cx, yy1 - hh - e), (cx, yy2 + hh + e), (yx1 - hw - e, cy), (yx2 + hw + e, cy)):
                        dx, dy = int(x) - at.x, int(y) - at.y
                        box = (b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy)
                        inside = box[0] > ex1 + margin and box[2] < ex2 - margin and box[1] > ey1 + margin and box[3] < ey2 - margin
                        if inside and not hits(box, obstacles) and not hits(box, placed):
                            placed.append(box)
                            choice = (int(x), int(y), angle)
                            break
                    if choice:
                        break
                if choice:
                    break
            fx, fy = mm(fp.position)
            change = {"font": (size, size, thickness), "hide": choice is None}
            if choice:
                change["at"] = (*to_local(fx, fy, fp.orientation.degrees, to_mm(choice[0]), to_mm(choice[1])), choice[2])
            else:
                hidden.append(ref)
            wanted[ref] = {"Reference": change, "Value": {"hide": True}}
    text = set_properties(open(src).read(), wanted)
    _write_beside(src, dst, text)
    # KiCad reads back what was written: every reference where it was put, at its angle, shown or hidden as chosen.
    with board_at(dst) as board:
        wrong = []
        for fp in board.get_footprints():
            ref = fp.reference_field.text.value
            change = wanted.get(ref, {}).get("Reference")
            if change is None:
                continue
            field = fp.reference_field
            if field.visible == change["hide"] or fp.value_field.visible:
                wrong.append("%s shown or hidden wrongly" % ref)
            if "at" in change:
                lx, ly, angle = change["at"]
                gx, gy = mm(field.text.position)
                fx, fy = mm(fp.position)
                ex, ey = to_local(fx, fy, fp.orientation.degrees, gx, gy)
                if math.hypot(ex - lx, ey - ly) > 1e-5 or abs((field.text.attributes.angle - angle + 180) % 360 - 180) > 1e-6:
                    wrong.append("%s at (%.4f, %.4f) %.1f deg, not (%.4f, %.4f) %.1f deg" % (ref, ex, ey, field.text.attributes.angle, lx, ly, angle))
        if wrong:
            raise SystemExit("place_silk: KiCad reads the references back elsewhere: " + "; ".join(wrong[:6]))
    print(json.dumps({"placed": len(placed), "hidden": hidden}))


def _placer():
    """kicad_ops' placement, run on fpmodel's footprints: fpmodel stands in for the pcbnew module it imports."""
    import fpmodel
    sys.modules.setdefault("pcbnew", fpmodel)
    if sys.modules["pcbnew"] is not fpmodel:
        raise SystemExit("a real pcbnew is loaded in this process, so placement can't run on fpmodel")
    import kicad_ops
    return fpmodel, kicad_ops


def build_board(seed, spec_path, dst):
    """Parts, nets, solved positions, outline and ground pours on the seed's stackup, as kicad_ops.build_board makes them.

    Placement runs on fpmodel's footprints, read from the libraries with no KiCad; the board is then written as text,
    so no footprint goes through UpdateItems. KiCad opens it, fills the pours and saves it, then reads every footprint
    and pad back: one that isn't where placement put it, or on its net, fails the op."""
    import uuid
    fpmodel, placer = _placer()
    spec = json.load(open(spec_path))
    layout = spec["layout"]
    fps = {}
    for ref, part in spec["parts"].items():
        fp = fpmodel.load(layout["footprints"][ref], spec["lib_dirs"])
        fp.SetReference(ref)
        # As schematic.shown_value: KiCad reads a symbol's empty Value as its reference, so the footprint says the same, or parity fails.
        fp.SetValue(str(part.get("value") or "").strip() or ref)
        fps[ref] = fp
    assigned, unlisted = placer._pad_nets(fps, spec)
    placed, squared, (left, right, top, bottom), (w, h) = placer.solve_placement(fps, spec)
    seed_text = open(seed).read().rstrip()
    if not seed_text.endswith(")"):
        raise SystemExit("%s doesn't end as a board does" % seed)
    copper = re.findall(r'\(\d+\s+"([^"]+\.Cu)"\s+(?:signal|power|mixed|jumper)', seed_text[:seed_text.index("(setup")])
    order = sorted(copper, key=lambda n: (n != "F.Cu", n == "B.Cu", int(re.sub(r"\D", "", n) or 0)))
    parts = []
    for ref, fp in fps.items():
        part = spec["parts"][ref]
        on_sheet = (spec.get("sheet_of") or {}).get(ref)
        parts.append(fpmodel.board_footprint(
            fp, assigned[ref], lambda: str(uuid.uuid4()), "/" + spec["uuids"][ref], on_sheet["name"] if on_sheet else "/",
            on_sheet["file"] if on_sheet else os.path.basename(dst).replace(".kicad_pcb", ".kicad_sch"),
            str(part.get("mpn", "")), bool(part.get("dnp"))))
    inset = 0.6
    pours = [fpmodel.pour_text("GND", layer, inset, inset, w - inset, h - inset, layout.get("pour_clearance_mm", 0.3), str(uuid.uuid4()))
             for layer in order]
    body = "\n".join(parts + [fpmodel.outline_text(w, h, str(uuid.uuid4()))] + pours)
    with open(dst, "w") as f:
        f.write(seed_text[:-1].rstrip() + "\n" + body + "\n)\n")
    with board_at(dst) as board:
        refill(board)
        save(board, dst, dst)
        wrong = []
        for got in board.get_footprints():
            ref = got.reference_field.text.value
            fp = fps.get(ref)
            if fp is None:
                wrong.append("%s isn't a part" % ref)
                continue
            gx, gy = mm(got.position)
            if math.hypot(gx - fp.x / 1e6, gy - fp.y / 1e6) > 1e-5 or abs((got.orientation.degrees - fp.rot + 180) % 360 - 180) > 1e-6:
                wrong.append("%s at (%.4f, %.4f) %.2f, not (%.4f, %.4f) %.2f" % (ref, gx, gy, got.orientation.degrees, fp.x / 1e6, fp.y / 1e6, fp.rot))
            mine = {}
            for pad in fp.Pads():
                mine.setdefault(pad.GetNumber(), []).append(pad.GetPosition())
            for pad in got.definition.pads:
                px, py = mm(pad.position)
                near = min((math.hypot(px - q.x / 1e6, py - q.y / 1e6) for q in mine.get(pad.number, [])), default=None)
                if near is None or near > 1e-5:
                    wrong.append("%s.%s at (%.4f, %.4f), %s from where placement put it" % (ref, pad.number, px, py,
                                                                                         "far" if near is None else "%.5f mm" % near))
                if pad.net.name != assigned[ref].get(pad.number, ""):
                    wrong.append("%s.%s on %r, not %r" % (ref, pad.number, pad.net.name, assigned[ref].get(pad.number, "")))
        missing = sorted(set(fps) - {f.reference_field.text.value for f in board.get_footprints()})
        wrong += ["%s isn't on the board" % r for r in missing]
        if wrong:
            raise SystemExit("build_board: KiCad reads the board back differently from what was placed: " + "; ".join(wrong[:8]))
    print(json.dumps({"parts": len(placed), "unlisted_pads": unlisted, "set_square": squared, "folds": spec.get("folds") or [],
                      "packed": bool(spec.get("packed")), "taps_beside": spec.get("taps_beside") or {},
                      "span_mm": [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)], "outline_mm": [w, h],
                      "positions": {r: [round(f.x / 1e6, 3), round(f.y / 1e6, 3), f.GetOrientationDegrees()] for r, f in fps.items()}}))


CLUSTER_MM = 2.0      # pads closer than this belong to one courtyard box, as in kicad_ops


def _boxes_touch(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def _clusters(boxes):
    """Pads in groups, each within CLUSTER_MM of another in its group, as kicad_ops._pad_clusters: indices in pad order."""
    gap = from_mm(CLUSTER_MM) // 2
    grown = [(b[0] - gap, b[1] - gap, b[2] + gap, b[3] + gap) for b in boxes]
    owner = list(range(len(boxes)))

    def find(i):
        while owner[i] != i:
            owner[i] = owner[owner[i]]
            i = owner[i]
        return i
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _boxes_touch(grown[i], grown[j]):
                owner[find(i)] = find(j)
    groups = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    return sorted(groups.values())


def _merge(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _inside(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def export_footprints(src, pretty):
    """Each footprint a board uses, as a library, at rest, with a courtyard, as kicad_ops.export_footprints writes them.

    The IPC API has no footprint-library writer. KiCad flips the back-side parts onto the front in a scratch session
    (FlipItems, so no footprint goes through UpdateItems), saves a scratch copy, and each footprint is cut from that
    copy: unturned, off the board, its nets gone. A footprint with no courtyard gets one round its groups of pads, as
    kicad_ops does: the groups found on the board as placed, so a part with another part's pads between its own keeps
    one box for each group."""
    import tempfile

    import fpmodel
    from scripts.kicad_pcb import spans
    os.makedirs(pretty, exist_ok=True)
    source = open(src).read()
    root, _ = spans(source)
    first = {}                      # the first footprint of each name in the file, by uuid, as pcbnew's Footprints() gives them
    for node in (c for c in root[3] if c[0] == "footprint"):
        name = re.match(r'\(footprint\s+"((?:[^"\\]|\\.)*)"', source[node[1]:node[2]]).group(1).split(":", 1)[-1]
        uid = re.search(r'\n\s*\(uuid\s+"([^"]+)"\)', source[node[1]:node[2]])
        if name not in first and uid:
            first[name] = uid.group(1)
    with tempfile.TemporaryDirectory(prefix="edaloop-fp-") as scratch:
        flipped = os.path.join(scratch, "flipped.kicad_pcb")
        with board_at(src) as board:
            fps = board.get_footprints()
            by_id = {f.id.value: f for f in fps}
            wanted = [by_id[u] for u in first.values() if u in by_id]
            # Clusters and the parts between them, on the board as placed.
            placed = {}
            for fp in wanted:
                boxes = [_box(b) for b in board.get_item_bounding_box(list(fp.definition.pads))] if fp.definition.pads else []
                placed[fp.id.value] = boxes
            pads_by_side = {}
            for fp in fps:
                pads_by_side.setdefault(fp.layer == BoardLayer.BL_B_Cu, []).append((fp.id.value, [p.position for p in fp.definition.pads]))
            sides = {f.id.value: f.layer == BoardLayer.BL_B_Cu for f in wanted}       # before the flip below
            back = [f for f in wanted if f.layer == BoardLayer.BL_B_Cu]
            if back:
                with commit(board, "export_footprints: flip to the front"):
                    board.flip_items(back, BoardFlipDirection.BFD_TOP_BOTTOM)
            board.save_as(flipped, overwrite=True, include_project=False)
        text = open(flipped).read()
        version = re.search(r"\(version\s+(\d+)\)", text).group(1)
        froot, _ = spans(text)
        nodes = {}
        for node in (c for c in froot[3] if c[0] == "footprint"):
            uid = re.search(r'\n\s*\(uuid\s+"([^"]+)"\)', text[node[1]:node[2]])
            if uid:
                nodes[uid.group(1)] = node
    made, added, split = 0, 0, []
    for name, uid in first.items():
        node = nodes.get(uid)
        if node is None:
            raise SystemExit("export_footprints: KiCad's copy of the board has no footprint %s (%s)" % (name, uid))
        lib = _at_rest(text, node, name, version)
        fp = fpmodel.FOOTPRINT("", name, lib)
        if fp.courtyard.is_empty and fp.pads:
            boxes = placed.get(uid, [])
            groups = _clusters(boxes) if boxes else [list(range(len(fp.pads)))]
            if len(groups) > 1:
                whole, own = _merge(boxes), [_merge([boxes[i] for i in g]) for g in groups]
                between = any(_inside(whole, q.x, q.y) and not any(_inside(o, q.x, q.y) for o in own)
                              for other, pts in pads_by_side.get(sides[uid], []) if other != uid for q in pts)
                if not between:
                    groups = [list(range(len(fp.pads)))]
            rest = [fp.pads[i].GetBoundingBox() for i in range(len(fp.pads))]
            m = from_mm(0.25)
            rects = []
            for g in groups:
                b = _merge([(rest[i].GetLeft(), rest[i].GetTop(), rest[i].GetRight(), rest[i].GetBottom()) for i in g])
                rects.append('\t(fp_rect (start %s %s) (end %s %s) (stroke (width 0.05) (type default)) (fill no) (layer "F.CrtYd"))'
                             % (fpmodel._mm(b[0] - m), fpmodel._mm(b[1] - m), fpmodel._mm(b[2] + m), fpmodel._mm(b[3] + m)))
            lib = lib.rstrip()[:-1].rstrip() + "\n" + "\n".join(rects) + "\n)\n"
            added += 1
            if len(groups) > 1:
                split.append(name)
        with open(os.path.join(pretty, name + ".kicad_mod"), "w") as f:
            f.write(lib)
        made += 1
    print(json.dumps({"footprints": made, "courtyards_added": added, "courtyards_split": split}))


def _at_rest(text, node, name, version):
    """One footprint from a board as a library footprint: unturned and off the board, with no nets, path or sheet."""
    import fpmodel
    from scripts.kicad_pcb import footprint_pose, to_local
    fx, fy, rot = footprint_pose(_plain(text, node))
    body = text[node[1]:node[2]]
    base = node[1]
    edits = []
    head = re.match(r'\(footprint\s+("(?:[^"\\]|\\.)*")', body)
    edits.append((base + head.start(1), base + head.end(1), fpmodel._quote(name) + ' (version %s) (generator "edaloop")' % version))
    for c in node[3]:
        if c[0] in ("at", "transform", "path", "sheetname", "sheetfile", "uuid"):
            edits.append((c[1], c[2], ""))
        elif c[0] in ("property", "fp_text"):
            at = next((k for k in c[3] if k[0] == "at"), None)
            if at is not None:
                edits.append((at[1], at[2], fpmodel._turned_at(text, at, -rot)))
        elif c[0] == "pad":
            at = next(k for k in c[3] if k[0] == "at")
            edits.append((at[1], at[2], fpmodel._turned_at(text, at, -rot)))
            for k in c[3]:
                if k[0] == "net":
                    edits.append((k[1], k[2], ""))
        elif c[0] == "zone":
            for k in c[3]:
                if k[0] == "filled_polygon":
                    edits.append((k[1], k[2], ""))
            for xy in fpmodel._all(c, "xy"):
                x, y = (float(v) for v in text[xy[1] + 1:xy[2] - 1].split()[1:3])
                lx, ly = to_local(fx, fy, rot, x, y)
                edits.append((xy[1], xy[2], "(xy %s %s)" % (fpmodel._mm(lx * 1e6), fpmodel._mm(ly * 1e6))))
    out = text
    for a, b, new in sorted(edits, reverse=True):
        out = out[:a] + new + out[b:]
    end = node[2] + sum(len(new) - (b - a) for a, b, new in edits)
    return out[node[1]:end].replace("\n\t", "\n") + "\n"


def _plain(text, node):
    """A span node as the plain lists footprint_pose reads: its tag and its children's tags and atoms."""
    from scripts.kicad_pcb import sexp
    return sexp(text[node[1]:node[2]])[0]


def _write_beside(src, dst, text):
    """Write a board's text to `dst` with the source's project beside it, as `save` does."""
    with open(dst, "w") as f:
        f.write(text)
    theirs, project = os.path.splitext(src)[0] + ".kicad_pro", os.path.splitext(dst)[0] + ".kicad_pro"
    if os.path.exists(theirs) and os.path.abspath(theirs) != os.path.abspath(project):
        shutil.copyfile(theirs, project)


def _text_box(board, text, angle):
    """The box KiCad draws a text in, at its own position and attributes and at `angle` on the board, in nm."""
    cmd = base_commands_pb2.GetTextExtents()
    cmd.text.CopyFrom(text.as_text().proto)
    cmd.text.attributes.angle.value_degrees = angle
    return board.client.send(cmd, base_types_pb2.Box2)


def board_facts(src, out):
    """True pad shapes, angles and courtyards, which the file reader only approximates at odd rotations."""
    with board_at(src) as board:
        pads, yards, keepouts = [], [], _keepouts(board.get_zones(), "")
        for fp in board.get_footprints():
            ref = fp.reference_field.text.value
            back = fp.layer == BoardLayer.BL_B_Cu
            x1, y1, x2, y2 = (to_mm(v) for v in _front_courtyard(board, fp))
            yards.append({"ref": ref, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "rot": fp.orientation.degrees,
                          "back": back, "value": fp.value_field.text.value})
            for pad in fp.definition.pads:
                p = pad.position
                stack = pad.padstack
                front_layer = stack.copper_layer(BoardLayer.BL_F_Cu)
                size = (front_layer or (stack.copper_layers[0] if len(stack.copper_layers) else None))
                w, h = (to_mm(size.size.x), to_mm(size.size.y)) if size is not None else (0.0, 0.0)
                on = set(stack.layers)
                drill = to_mm(stack.drill.diameter.x) if stack.drill and stack.drill.diameter.x else 0.0
                pads.append({"ref": ref, "pad": pad.number, "net": pad.net.name, "x": to_mm(p.x), "y": to_mm(p.y), "w": w, "h": h,
                             "rot": stack.angle.degrees, "back": BoardLayer.BL_B_Cu in on or drill > 0, "drill": drill,
                             "front": BoardLayer.BL_F_Cu in on or drill > 0, "clearance": _local_clearance(pad, fp)})
            keepouts += _keepouts([i for i in fp.definition.items if isinstance(i, Zone)], ref, fp)
        outline = list(edge_box(board))
    json.dump({"pads": pads, "courtyards": yards, "keepouts": keepouts, "outline": outline}, open(out, "w"))
    print(json.dumps({"pads": len(pads)}))


def plane_coverage(src, nets_csv, layer_name, out):
    """How much of each RF trace's width has ground beneath it, from the stored fill. Sampled along and across each F.Cu segment."""
    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union
    from shapely.prepared import prep

    nets = set(nets_csv.split(","))
    with board_at(src) as board:
        where = layer(layer_name)
        parts = []
        for zone in board.get_zones():
            if zone.is_rule_area() or zone.net is None or zone.net.name != "GND" or where not in zone.layers:
                continue
            for poly in zone.filled_polygons.get(where, []):
                shell = [(to_mm(n.point.x), to_mm(n.point.y)) for n in poly.outline.nodes]
                holes = [[(to_mm(n.point.x), to_mm(n.point.y)) for n in h.nodes] for h in poly.holes]
                if len(shell) >= 3:
                    # KiCad's fill is fractured, its holes joined to the outline by zero-width slits, which shapely calls
                    # invalid and won't test a point against. buffer(0) closes the slits and keeps the area.
                    parts.append(Polygon(shell, [h for h in holes if len(h) >= 3]).buffer(0))
        ground = prep(unary_union(parts)) if parts else None
        tracks = [t for t in board.get_tracks() if isinstance(t, Track) and t.layer == BoardLayer.BL_F_Cu and t.net.name in nets]
    found = {}
    for t in tracks:
        (x1, y1), (x2, y2) = mm(t.start), mm(t.end)
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        ux, uy = (x2 - x1) / length, (y2 - y1) / length
        width = to_mm(t.width)
        row = found.setdefault(t.net.name, {"coverage": 1.0, "at": [round(x1, 3), round(y1, 3)]})
        steps = max(1, int(length / 0.05))
        for i in range(steps + 1):
            x, y = x1 + ux * length * i / steps, y1 + uy * length * i / steps
            hit = 0 if ground is None else sum(
                ground.contains(Point(x - uy * (-width / 2 + width * k / 12), y + ux * (-width / 2 + width * k / 12))) for k in range(13))
            if hit / 13 < row["coverage"]:
                row.update(coverage=hit / 13, at=[round(x, 3), round(y, 3)])
    json.dump(found, open(out, "w"))
    print(json.dumps({"nets": len(found)}))


OPS = {"export_dsn": export_dsn, "import_ses": import_ses, "add_locked": add_locked, "fill_zones": fill_zones,
       "board_facts": board_facts, "add_vias": add_vias, "remove_vias": remove_vias, "plane_coverage": plane_coverage,
       "z_coupon": z_coupon, "add_voids": add_voids, "remove_tracks": remove_tracks, "strip_inner_pads": strip_inner_pads, "place_silk": place_silk,
       "build_board": build_board, "export_footprints": export_footprints}


def main(argv):
    name = argv[1]
    if name in UNPORTED:
        raise SystemExit("%s isn't on KiCad 11's IPC API yet: %s. Use KiCad 10 for it (set kicad_backend: swig and kicad_python "
                         "in ~/.config/edaloop/toolchain.yaml)" % (name, UNPORTED[name]))
    if name not in OPS:
        raise SystemExit("no such op: %s" % name)
    if KIPY_MISSING is not None:
        raise SystemExit("board edits on KiCad 11 need kicad-python 0.10 or later in %s (%s). Install it, or set ipc_python "
                         "in ~/.config/edaloop/toolchain.yaml to a Python that has it" % (sys.executable, KIPY_MISSING))
    OPS[name](*argv[2:])


if __name__ == "__main__":
    main(sys.argv)
