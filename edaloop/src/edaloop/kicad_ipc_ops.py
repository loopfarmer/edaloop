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
import shutil
import sys
from contextlib import contextmanager

try:
    from kipy import KiCad
    from kipy.board_types import Net, RuleAreaSettings, Track, Via, Zone
    from kipy.common_types import PolygonWithHoles
    from kipy.geometry import PolyLine, PolyLineNode, Vector2
    from kipy.proto.board import board_commands_pb2, board_jobs_pb2
    from kipy.proto.board.board_types_pb2 import BoardLayer, ViaType, ZoneType
    from kipy.proto.common.commands import editor_commands_pb2
    from kipy.proto.common.types import base_types_pb2, jobs_pb2
    from kipy.util import pack_any
    from kipy.util.units import from_mm, to_mm
except ImportError as missing:      # refused in main(), with how to install it
    KIPY_MISSING = missing
else:
    KIPY_MISSING = None

# A zone fill or a Specctra export on a big board takes minutes; the default 2 s would call a slow request a failure.
REQUEST_TIMEOUT_MS = 900_000

UNPORTED = {
    "build_board": "it poses each footprint many times while it solves a row, reading pads and courtyards after each pose",
    "place_silk": "it measures each reference's text box at every place it tries",
    "strip_inner_pads": "it reads the board's unconnected count after each via it changes, and the IPC API has no ratsnest",
    "export_footprints": "it writes footprints to a library with pcbnew's own writer",
}


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


def board_facts(src, out):
    """True pad shapes, angles and courtyards, which the file reader only approximates at odd rotations."""
    with board_at(src) as board:
        pads, yards, keepouts = [], [], _keepouts(board.get_zones(), "")
        for fp in board.get_footprints():
            ref = fp.reference_field.text.value
            back = fp.layer == BoardLayer.BL_B_Cu
            yard_layer = BoardLayer.BL_B_CrtYd if back else BoardLayer.BL_F_CrtYd
            shapes = [s for s in fp.definition.shapes if s.layer == yard_layer]
            if shapes:
                # KiCad's box round a shape takes in half its line width each side; pcbnew's courtyard is the line itself.
                boxes = [(board.get_item_bounding_box(s), to_mm(s.attributes.stroke.width) / 2) for s in shapes]
                x1, y1 = min(to_mm(b.pos.x) + w for b, w in boxes), min(to_mm(b.pos.y) + w for b, w in boxes)
                x2, y2 = max(to_mm(b.pos.x + b.size.x) - w for b, w in boxes), max(to_mm(b.pos.y + b.size.y) - w for b, w in boxes)
            else:
                x1 = y1 = x2 = y2 = 0.0      # as pcbnew's empty courtyard
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
       "z_coupon": z_coupon, "add_voids": add_voids, "remove_tracks": remove_tracks}


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
