#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Write a KiCad project again in KiCad 6.0 format.

KiCad writes forward only, so this rewrites its files into the older syntax. The version token is what importers such as Altium's Import Wizard
refuse on, and everything the token promises has to change with it, or the file is a lie that parses.

WHAT ACTUALLY DIFFERS between 20211123 and 20260306, in the subset
KiCad writes for these projects:

  quoting        6.0 wrote (generator eeschema) and (uuid 1542cbf-...)
                 as bare tokens. 8.0 onward quotes both.
  booleans       6.0 wrote (hide), (fields_autoplaced) and (pin_names
                 (offset 0.254) hide) as BARE FLAGS -- present means
                 true. 8.0 turned them into (hide yes) / (hide no), and
                 a 6.0 reader has no idea what to do with the "yes".
  property ids   6.0 numbered every property: (property "Reference"
                 "J1" (id 0) ...). 7.0 dropped the id.
  hide placement 6.0 keeps hide INSIDE (effects ...). 10.0 puts it
                 beside effects on a lib_symbol property and inside it
                 on an instance property -- both, in the same file.
  instance data  THE STRUCTURAL ONE. 6.0 collects reference/unit/value/
                 footprint into a (symbol_instances ...) block at the
                 END of each sheet file, keyed by a path that ENDS in
                 the symbol's own uuid and does NOT start with the root
                 sheet's. 7.0 moved it inside each symbol as (instances
                 (project NAME (path ...))) where the path is the SHEET
                 path and starts with the root uuid. Convert the one to
                 the other and a 6.0 reader finds its references; miss
                 it and every part imports unannotated.
  new tokens     exclude_from_sim (8.0), dnp (7.0), show_name and
                 do_not_autoplace (8.0), in_pos_files (9.0),
                 duplicate_pin_numbers_are_jumpers (9.0),
                 generator_version (8.0), embedded_fonts (9.0). All are
                 dropped; none carries information this design uses.

HOW IT IS CHECKED.  KiCad 10 still contains the 6.0 parser and picks it by the version token. So the converted file is read back with kicad-cli,
its netlist is exported, and that's compared net by net with the netlist of the original. If the instance block is wrong, references come back as "?"
and the comparison fails loudly. A separate list catches any token 6.0 never had, because KiCad reads those happily. The board is checked by comparing
what each file says about every object's geometry.

That's a test of the file, not of any other tool's importer.
"""
import os
import re
import shutil
import subprocess
import tempfile

CLI = "kicad-cli"

SCH_V6 = "20211123"

# tokens 7.0 and later added that 6.0 has never seen
DROP = ("generator_version", "embedded_fonts", "exclude_from_sim", "dnp",
        "show_name", "do_not_autoplace", "in_pos_files",
        "duplicate_pin_numbers_are_jumpers")
# 6.0 spells these as a flag rather than name + yes/no -- but NOT in the
# same shape, and the difference is not cosmetic. `hide` is a BARE TOKEN
# sitting inside (effects ...) or (pin_names ...); `fields_autoplaced` is
# an EMPTY LIST, (fields_autoplaced). Emit the second one as a bare token
# and the file will not load at all -- which is how this was found, by
# bisecting the transform against kicad-cli one step at a time.
FLAGS_BARE = ("hide",)
FLAGS_EMPTY = ("fields_autoplaced",)
# the four properties 6.0 numbers first, in its own order
FIXED_ID = {"Reference": 0, "Value": 1, "Footprint": 2, "Datasheet": 3,
            "Sheetname": 0, "Sheetfile": 1}


# ==========================================================================
# a reader that REMEMBERS WHICH ATOMS WERE QUOTED
# ==========================================================================
class Q(str):
    """an atom that was quoted in the source, and must be quoted again"""


class Bare(str):
    """an atom that must NOT be quoted, whatever it looks like"""


def read(s, i=0):
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    if s[i] == "(":
        out, i = [], i + 1
        while True:
            while i < n and s[i] in " \t\r\n":
                i += 1
            if s[i] == ")":
                return out, i + 1
            v, i = read(s, i)
            out.append(v)
    if s[i] == '"':
        buf, i = [], i + 1
        while s[i] != '"':
            if s[i] == "\\":
                i += 1
            buf.append(s[i])
            i += 1
        return Q("".join(buf)), i + 1
    j = i
    while j < n and s[j] not in ' \t\r\n()"':
        j += 1
    return Bare(s[i:j]), j


def write(node, depth=0):
    pad = "\t" * depth
    if not isinstance(node, list):
        return _atom(node)
    if not node:
        return "()"
    head = _atom(node[0])
    rest = node[1:]
    flat = all(not isinstance(c, list) for c in rest)
    if flat:
        return "%s(%s%s)" % (pad, head,
                             "".join(" " + _atom(c) for c in rest))
    out = ["%s(%s" % (pad, head)]
    for c in rest:
        out.append(write(c, depth + 1) if isinstance(c, list)
                   else "%s%s" % ("\t" * (depth + 1), _atom(c)))
    out.append("%s)" % pad)
    return "\n".join(out)


def _atom(a):
    if isinstance(a, Bare):
        return str(a)
    if isinstance(a, Q):
        return '"%s"' % str(a).replace("\\", "\\\\").replace('"', '\\"')
    return str(a)


def tag(node):
    return str(node[0]) if isinstance(node, list) and node else None


def kids(node, t):
    return [c for c in node if tag(c) == t]


def kid(node, t):
    g = kids(node, t)
    return g[0] if g else None


# ==========================================================================
# the transform
# ==========================================================================
def strip_flags(node):
    """(hide yes) -> hide, (hide no) -> gone, and the same for
    fields_autoplaced. Recursive, because they appear at four depths."""
    out = []
    for c in node:
        if isinstance(c, list):
            t = tag(c)
            if t in DROP:
                continue
            # 6.0's (power) is a bare flag. 9.0 gave it a scope --
            # (power global) / (power local) -- and 6.0 stops dead on the
            # argument: "Expecting ')'", line 1328.
            if t == "power":
                out.append([Bare("power")])
                continue
            if t in FLAGS_BARE or t in FLAGS_EMPTY:
                if len(c) > 1 and str(c[1]) == "no":
                    continue
                out.append(Bare(t) if t in FLAGS_BARE else [Bare(t)])
                continue
            out.append(strip_flags(c))
        else:
            out.append(c)
    return out


def fix_property(prop, seq):
    """6.0 numbers properties and keeps `hide` inside (effects ...)."""
    name = str(prop[1]) if len(prop) > 1 else ""
    pid = FIXED_ID.get(name, seq)
    body, hidden = [], False
    for c in prop[3:]:
        if c == "hide" or tag(c) == "hide":
            hidden = True                     # a lib_symbol property puts
            continue                          # it beside effects; 6.0 does
        body.append(c)                        # not
    if hidden:
        eff = None
        for c in body:
            if tag(c) == "effects":
                eff = c
        if eff is None:
            eff = [Bare("effects")]
            body.append(eff)
        if "hide" not in eff:
            eff.append(Bare("hide"))
    return [Bare("property"), prop[1], prop[2], [Bare("id"), Bare(str(pid))]] + body


def renumber(node):
    """walk any node, fixing every (property ...) it contains"""
    seq = 4
    out = []
    for c in node:
        if tag(c) == "property":
            name = str(c[1]) if len(c) > 1 else ""
            out.append(fix_property(c, FIXED_ID.get(name, seq)))
            if name not in FIXED_ID:
                seq += 1
        elif isinstance(c, list):
            out.append(renumber(c))
        else:
            out.append(c)
    return out


def unquote_uuids(node):
    """6.0 wrote uuids and the generator name as bare tokens."""
    out = []
    for c in node:
        if isinstance(c, list):
            t = tag(c)
            if t in ("uuid", "generator") and len(c) > 1:
                out.append([c[0], Bare(str(c[1]))])
                continue
            out.append(unquote_uuids(c))
        else:
            out.append(c)
    return out


def hide_power_pins(node):
    """A 6.0 power symbol connects through an INVISIBLE pin.

    This one is not syntax and no grammar check would have found it. In
    6.0, LIB_PIN::IsPowerConnection() is

        !IsVisible() && type == PT_POWER_IN && parent->IsPower()

    -- the pin has to be HIDDEN. And the NET NAME comes from the pin's
    own name, not from the symbol's value: 6.0's canonical power:GND is
    (pin power_in line (at 0 0 270) (length 0) hide (name "GND" ...)).
    10.0 takes power-ness from the symbol and the name from its value,
    so the cached power:GND that ships inside these files has a VISIBLE
    pin called "". Both halves are needed -- hiding it alone still gave
    "<NO NET>", and it broke 10.0 as well, which was the clue that the
    NAME was the missing half rather than the flag.

    Converted without this, the file loads, draws correctly, and every
    ground symbol on the sheet is decoration: KiCad 6 exports the whole
    ground net as one connected group named "<NO NET>" and 17 pads reach
    the board unnamed. It was found by exporting a netlist from Eeschema
    6.0.11 and reading it -- and confirmed by letting KiCad 6 re-save the
    schematic itself and getting the same "<NO NET>" out of ITS file, so
    the fault was in what the file MEANT, not in how it was written.
    """
    n = 0
    for sym in kids(kid(node, "lib_symbols") or [], "symbol"):
        if not any(tag(x) == "power" for x in sym):
            continue
        val = ""
        for pr in kids(sym, "property"):
            if len(pr) > 2 and str(pr[1]) == "Value":
                val = str(pr[2])
        for sub in kids(sym, "symbol"):
            for pin in kids(sub, "pin"):
                # power_in ONLY. PWR_FLAG's pin is power_out, is not
                # hidden and is named "pwr" in the 6.0 library -- it is
                # not a driver and must be left exactly as it is, or the
                # flag starts naming the nets it is attached to.
                if len(pin) < 2 or str(pin[1]) != "power_in":
                    continue
                if "hide" not in pin:
                    at = next((i for i, x in enumerate(pin)
                               if tag(x) == "length"), len(pin) - 1)
                    pin.insert(at + 1, Bare("hide"))
                nm = kid(pin, "name")
                if nm is not None and not str(nm[1]) and val:
                    nm[1] = Q(val)
                n += 1
    return n


def split_polylines(node):
    """6.0's schematic polyline is a TWO-POINT line.

    A schematic graphic line in 6.0 is a SCH_LINE and SCH_LINE has two
    ends; multi-point shapes are SCH_SHAPE and arrived in 7.0. A three
    point polyline parses as far as the third (xy ...) and then 6.0 says
    "Expecting ')'". The element board had none and loaded; the discone
    board has one drawn round a corner and did not.

    Split into consecutive two-point lines. Same ink, same stroke, and
    each piece gets its own uuid because two objects cannot share one.
    """
    import uuid as _uuid
    out, n = [], 0

    def walk(nd, inlib=False):
        res = []
        for c in nd:
            if not isinstance(c, list):
                res.append(c)
                continue
            # NOT INSIDE lib_symbols. A LIBRARY symbol's graphic is a
            # LIB_SHAPE and 6.0 has always allowed it many points -- the
            # ground triangle is six. Splitting those turned every power
            # symbol into five lines carrying a (uuid), which a library
            # graphic may not have, and the whole file stopped yielding
            # a netlist. Only the SHEET-level polyline is the two-point
            # SCH_LINE.
            if tag(c) == "lib_symbols":
                res.append(walk(c, True))
                continue
            if tag(c) == "polyline" and not inlib:
                pts = kid(c, "pts")
                if pts is not None and len(pts) > 3:
                    rest = [x for x in c[1:] if tag(x) not in ("pts", "uuid")]
                    for i in range(1, len(pts) - 1):
                        res.append([Bare("polyline"),
                                    [Bare("pts"), pts[i], pts[i + 1]]] +
                                   [x for x in rest] +
                                   [[Bare("uuid"), Bare(str(_uuid.uuid4()))]])
                    out.append(1)
                    continue
            res.append(walk(c, inlib))
        return res
    node = walk(node)
    return node, len(out)


def collect_instances(root, root_uuid):
    """Pull every symbol's (instances ...) out and build 6.0's
    (symbol_instances ...) block.

    THE PATH IS NOT THE SAME PATH. 10.0 stores the path to the SHEET the
    symbol sits on, starting with the root sheet's uuid. 6.0 stores the
    path to the SYMBOL, and does not include the root sheet. So the
    conversion is: drop the leading root uuid, append the symbol's own.
    Get this wrong in either direction and the file parses, imports, and
    every part comes in without a reference.
    """
    rows = []

    def walk(node):
        out = []
        for c in node:
            if not isinstance(c, list):
                out.append(c)
                continue
            if tag(c) == "symbol" and kid(c, "lib_id") is not None:
                inst = kid(c, "instances")
                u = kid(c, "uuid")
                if inst is not None and u is not None:
                    prj = kid(inst, "project")
                    pth = kid(prj, "path") if prj is not None else None
                    if pth is not None:
                        sheet = str(pth[1])
                        if sheet.startswith("/" + root_uuid):
                            sheet = sheet[len("/" + root_uuid):]
                        ref = kid(pth, "reference")
                        unit = kid(pth, "unit")
                        val = kid(c, "property")
                        vals = {str(p[1]): str(p[2]) for p in kids(c, "property")
                                if len(p) > 2}
                        rows.append([
                            Bare("path"), Q(sheet.rstrip("/") + "/" + str(u[1])),
                            [Bare("reference"), Q(str(ref[1]) if ref else "?")],
                            [Bare("unit"), Bare(str(unit[1]) if unit else "1")],
                            [Bare("value"), Q(vals.get("Value", ""))],
                            [Bare("footprint"), Q(vals.get("Footprint", ""))],
                        ])
                c = [x for x in c if tag(x) != "instances"]
            elif tag(c) == "sheet":
                c = [x for x in c if tag(x) != "instances"]
            out.append(walk(c))
        return out

    root = walk(root)
    if rows:
        root.append([Bare("symbol_instances")] + rows)
    return root


def convert_sch(path, out_path, root_uuid):
    node, _ = read(open(path).read())
    v = kid(node, "version")
    if v is not None:
        v[1] = Bare(SCH_V6)
    node = strip_flags(node)
    node = renumber(node)
    node = unquote_uuids(node)
    node, _n = split_polylines(node)
    hide_power_pins(node)
    node = collect_instances(node, root_uuid)
    # (generator "eeschema") -> (generator eeschema), already bare above
    open(out_path, "w").write(write(node) + "\n")


# ==========================================================================
# driver
# ==========================================================================
def root_uuid_of(path):
    node, _ = read(open(path).read())
    u = kid(node, "uuid")
    return str(u[1]) if u is not None else ""


def project_v6(pro_path, out_path):
    """The .kicad_pro is JSON and its meta.version is what a reader
    checks. 6.0 wrote 1."""
    import json
    d = json.load(open(pro_path))
    d.setdefault("meta", {})["version"] = 1
    json.dump(d, open(out_path, "w"), indent=2)


# EVERY TOKEN KiCad 6.0's SCHEMATIC GRAMMAR KNOWS, in the subset this
# project emits. The netlist round trip below proves the file still says
# the same thing; it does NOT prove the file is 6.0, because KiCad 10's
# parser is version-TOLERANT -- swap the version token on an unmodified
# 10.0 file and kicad-cli reads it happily. So the version claim needs
# its own check, and this is it: anything outside this list is a token
# 6.0 never had, and another tool's importer is entitled to choke on it.
V6_TOKENS = set("""
kicad_sch version generator uuid paper title_block title date rev company
lib_id
comment lib_symbols symbol pin_names pin_numbers offset hide in_bom
on_board power property id at effects font size thickness bold italic
justify color face rectangle start end stroke width type fill polyline
pts xy arc mid circle center radius pin name number length unit alternate
junction diameter wire bus bus_entry no_connect text label global_label
hierarchical_label shape fields_autoplaced sheet mirror sheet_instances
path page symbol_instances reference value footprint datasheet image
scale data extends autoplaced
""".split())


def conformance(path):
    """Report any token in the file that 6.0's grammar does not have.

    Walks the PARSE TREE, not the text. A regex over the raw file counts
    "(BNC" inside a page note as a token and reports eight of them; the
    first version of this did exactly that and made the check useless by
    burying two real findings in twenty false ones.
    """
    node, _ = read(open(path).read())
    bad = {}

    def walk(nd):
        t = tag(nd)
        if t is not None and t not in V6_TOKENS:
            bad[t] = bad.get(t, 0) + 1
        for c in nd:
            if isinstance(c, list):
                walk(c)
    walk(node)
    return bad


def structure(path):
    """The two things a downgrade silently gets wrong."""
    node, _ = read(open(path).read())
    problems = []
    n_sym = n_inst = 0

    def walk(nd):
        nonlocal n_sym
        for c in nd:
            if not isinstance(c, list):
                continue
            if tag(c) == "symbol" and kid(c, "lib_id") is not None:
                n_sym += 1
                if kid(c, "instances") is not None:
                    problems.append("a symbol still carries (instances)")
            for pr in kids(c, "property"):
                if kid(pr, "id") is None:
                    problems.append("property %r has no (id)" % str(pr[1]))
            walk(c)
    walk(node)
    si = kid(node, "symbol_instances")
    n_inst = len(kids(si, "path")) if si is not None else 0
    if n_sym and n_inst != n_sym:
        problems.append("%d symbol(s) but %d symbol_instances row(s)"
                        % (n_sym, n_inst))
    for r in (kids(si, "path") if si is not None else []):
        ref = kid(r, "reference")
        if ref is None or str(ref[1]) in ("", "?"):
            problems.append("an instance row has no reference")
    return sorted(set(problems)), n_sym


def verify(root, original):
    """Read the 6.0 file back with KiCad's own 6.0 parser and compare its netlist with the original's."""
    work = tempfile.mkdtemp()
    nets = []
    for path, name in ((root, "converted.net"), (original, "original.net")):
        out = os.path.join(work, name)
        r = subprocess.run([CLI, "sch", "export", "netlist", "--output", out, path], capture_output=True)
        if r.returncode or not os.path.exists(out):
            return "kicad-cli refused %s: %s" % (name, r.stderr.decode()[:200].strip())
        nets.append(_nets(open(out).read()))
    a, b = nets
    if a != b:
        return "netlist differs: %d nets vs %d, missing %s, extra %s" % (len(a), len(b), sorted(set(b) - set(a))[:4], sorted(set(a) - set(b))[:4])
    return None


def _nets(s):
    out = {}
    for m in re.finditer(r'\(name "([^"]+)"\)((?:(?!\(name ")[\s\S])*?)'
                         r'(?=\n\t\t\t\(name "|\Z)', s):
        nodes = re.findall(r'\(ref "([^"]+)"\)\s*\n\s*\(pin "([^"]+)"\)',
                           m.group(2))
        if nodes:
            out[m.group(1).lstrip("/")] = sorted(nodes)
    return out


def _table(kind, names, suffix):
    # No (version N) line. That's a 7.0 addition, and 6.0's table parser stops on it.
    rows = "".join('  (lib (name "%s")(type "KiCad")(uri "${KIPRJMOD}/%s%s")(options "")(descr ""))\n' % (n, n, suffix) for n in names)
    return "(%s\n%s)\n" % (kind, rows)


def export(project_dir, board_path, schematic_path, dst, cli=None):
    """Write the project in 6.0 syntax. Returns a dict of what was checked, with `problems` empty if all of it held."""
    global CLI
    CLI = cli or CLI
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    root = str(schematic_path)
    ru = root_uuid_of(root)
    sheets = 0
    for f in sorted(os.listdir(project_dir)):
        a, o = os.path.join(project_dir, f), os.path.join(dst, f)
        if f.endswith(".kicad_sch"):
            convert_sch(a, o, ru)
            sheets += 1
        elif f.endswith(".kicad_pro"):
            project_v6(a, o)
    out_pcb = os.path.join(dst, os.path.basename(str(board_path)))
    convert_pcb(str(board_path), out_pcb)
    # Self-contained, or it imports with unresolved symbols: local libraries come along, and the tables name them through ${KIPRJMOD}.
    symbols, footprints = [], []
    for f in sorted(os.listdir(project_dir)):
        a = os.path.join(project_dir, f)
        if f.endswith(".kicad_sym"):
            shutil.copy2(a, os.path.join(dst, f))
            symbols.append(f[: -len(".kicad_sym")])
        elif f.endswith(".pretty") and os.path.isdir(a):
            shutil.copytree(a, os.path.join(dst, f))
            footprints.append(f[: -len(".pretty")])
    with open(os.path.join(dst, "sym-lib-table"), "w") as fh:
        fh.write(_table("sym_lib_table", symbols, ".kicad_sym"))
    with open(os.path.join(dst, "fp-lib-table"), "w") as fh:
        fh.write(_table("fp_lib_table", footprints, ".pretty"))
    problems = []
    bad = verify(os.path.join(dst, os.path.basename(root)), root)
    if bad:
        problems.append(bad)
    # The board is checked object by object. kicad-cli reads a 6.0 board with the same tolerant parser as a 6.0 schematic, so "it loads" proves little.
    a, b = pcb_geometry(str(board_path)), pcb_geometry(out_pcb)
    lost = sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b))
    if lost:
        problems.append("%d board object(s) differ in geometry" % lost)
    if "(uuid" in open(out_pcb).read():
        problems.append("a (uuid) survived the rename to (tstamp)")
    r = subprocess.run([CLI, "pcb", "export", "ipcd356", "--output", os.devnull, out_pcb], capture_output=True)
    if r.returncode:
        problems.append("kicad-cli couldn't read the board back")
    tokens, symbols_seen = {}, 0
    for f in sorted(os.listdir(dst)):
        if not f.endswith(".kicad_sch"):
            continue
        for k, v in conformance(os.path.join(dst, f)).items():
            tokens[k] = tokens.get(k, 0) + v
        found, count = structure(os.path.join(dst, f))
        problems += found
        symbols_seen += count
    if tokens:
        problems.append("tokens outside the 6.0 grammar: " + ", ".join("%s x%d" % kv for kv in sorted(tokens.items())))
    return {"sheets": sheets, "symbols": symbols_seen, "board_objects": sum(a.values()), "problems": sorted(set(problems))}


# ==========================================================================
# the board file
# ==========================================================================
PCB_V6 = "20211014"
PCB_EMPTY = ("remove_unused_layers", "keep_end_layers", "zone_layer_connections")
GRAPHIC = ("fp_line", "fp_rect", "fp_poly", "fp_circle", "fp_arc",
           "gr_line", "gr_rect", "gr_poly", "gr_circle", "gr_arc",
           "gr_curve")
# 7.0 and later added these to the BOARD grammar. None carries anything
# this design depends on: they are plot options, mask-tenting detail,
# jumper hints and the embedded-font container.
PCB_DROP = ("generator_version", "embedded_fonts",
            "duplicate_pad_numbers_are_jumpers", "tenting", "covering",
            "capping", "plugging", "filling",
            "allow_soldermask_bridges_in_footprints", "legacy_teardrops",
            "dashed_line_dash_ratio", "dashed_line_gap_ratio",
            "hidednponfab", "crossoutdnponfab", "sketchdnponfab",
            "sketchpadsonfab", "plot_black_and_white", "pdf_metadata",
            "pdf_single_document", "pdf_front_fp_property_popups",
            "pdf_back_fp_property_popups", "plot_on_all_layers_selection",
            "dxfimperialunits", "dxfpolygonmode", "dxfusepcbnewfont",
            "svgprecision", "psa4output", "dielectric_constraints",
            "component_classes",
            # THE WHOLE PLOT BLOCK GOES. 6.0 spells its booleans
            # true/false and everything after it spells them yes/no, so
            # (disableapertmacros no) is a hard parse error in 6.0 --
            # "Expecting 'true|false'", line 117. Converting them one by
            # one would work and would be pointless: pcbplotparams is
            # the gerber-plot settings, it has nothing to do
            # with the design, and a reader without it uses its own
            # defaults. Dropping it removes a whole class of version
            # drift instead of tracking it.
            "pcbplotparams",
            # 7.0's rule-area placement rules; 6.0 zones have none
            "placement")


def pcb_v6(node):
    """Rename, drop and re-shape the board into 6.0's vocabulary.

    THE THREE THAT MATTER:
      uuid -> tstamp    6.0 called every object's identity tstamp, and
                        wrote it bare. 8.0 renamed it and quoted it. 394
                        of them in one board file.
      property -> fp_text   6.0 keeps a footprint's reference and value
                        as (fp_text reference "J1" ...). 8.0 moved them
                        into (property "Reference" "J1" ...) alongside
                        user fields. A 6.0 reader that meets (property)
                        inside a footprint has nowhere to put it, and the
                        board imports with no designators at all.
      tedit             6.0 requires it on every footprint. It is a
                        last-edit timestamp nothing reads; 0 is legal.
    """
    # 6.0 KEEPS A NET TABLE AND 10.0 HAS NONE. Every net reference in a
    # 10.0 board is (net "NAME"); 6.0 declares (net 0 "") ... (net N
    # "NAME") once at the top and then refers to nets BY NUMBER -- and
    # not in one shape either: a pad wants (net N "NAME"), a track or a
    # via wants (net N), and a zone wants (net N) with a separate
    # (net_name "NAME"). Without the table 6.0 stops at the first pad:
    # "need a number for 'net number'".
    names = []

    def gather(nd):
        for c in nd:
            if isinstance(c, list):
                if tag(c) == "net" and len(c) == 2 and isinstance(c[1], Q):
                    if str(c[1]) not in names:
                        names.append(str(c[1]))
                gather(c)
    gather(node)
    num = {"": 0}
    for i, nm in enumerate(sorted(n for n in names if n), start=1):
        num[nm] = i

    def walk(nd, parent=None):
        out = []
        for c in nd:
            if not isinstance(c, list):
                out.append(c)
                continue
            t = tag(c)
            if t in PCB_DROP:
                continue
            # the board has the same flag problem the schematic has:
            # 6.0 writes `hide` and `locked` as BARE tokens and has no
            # `unlocked` at all, because not locked is the default.
            if t in ("hide", "locked"):
                if len(c) > 1 and str(c[1]) == "no":
                    continue
                out.append(Bare(t))
                continue
            if t == "unlocked":
                continue
            # and the empty-list flavour, the same trap the schematic's
            # fields_autoplaced was: 6.0 spells these (remove_unused_layers)
            # with nothing in the brackets, so a `no` there is not a value
            # it can read -- "Expecting ')'".
            if t in PCB_EMPTY:
                if len(c) > 1 and str(c[1]) == "no":
                    continue
                out.append([Bare(t)])
                continue
            # 6.0's graphics carry (width W). 7.0 replaced that with
            # (stroke (width W) (type solid)) and 6.0's parser says so
            # in as many words: "Expecting 'layer, width, fill, tstamp,
            # locked, or status'".
            # a GRAPHIC's fill is none/solid in 6.0 and no/yes after it.
            # A ZONE's fill is (fill yes ...) in both, so this is scoped
            # to the graphics or it would break every pour on the board.
            if t == "net" and len(c) == 2 and isinstance(c[1], Q):
                nm = str(c[1])
                n = num.get(nm, 0)
                if parent == "pad":
                    out.append([Bare("net"), Bare(str(n)), Q(nm)])
                elif parent == "zone":
                    out.append([Bare("net"), Bare(str(n))])
                    out.append([Bare("net_name"), Q(nm)])
                else:
                    out.append([Bare("net"), Bare(str(n))])
                continue
            if t == "fill" and parent in GRAPHIC and len(c) == 2:
                out.append([Bare("fill"),
                            Bare({"no": "none", "yes": "solid"}.get(
                                str(c[1]), str(c[1])))])
                continue
            if t == "stroke" and parent in GRAPHIC:
                w = kid(c, "width")
                if w is not None:
                    out.append([Bare("width"), w[1]])
                continue
            if t == "uuid":
                out.append([Bare("tstamp"), Bare(str(c[1]))])
                continue
            if t == "property" and parent == "footprint":
                name = str(c[1])
                if name in ("Reference", "Value"):
                    body = [x for x in c[3:] if tag(x) != "uuid"]
                    ts = kid(c, "uuid")
                    row = [Bare("fp_text"), Bare(name.lower()), c[2]] + \
                        walk(body, "fp_text")
                    if ts is not None:
                        row.append([Bare("tstamp"), Bare(str(ts[1]))])
                    out.append(row)
                continue          # every other footprint property is a
                                  # user field 6.0 has no slot for
            out.append(walk(c, t))
        return out

    node = walk(node)
    v = kid(node, "version")
    if v is not None:
        v[1] = Bare(PCB_V6)
    g = kid(node, "generator")
    if g is not None:
        g[1] = Bare("pcbnew")
    # the table itself, ahead of the first footprint
    at = max([i for i, c in enumerate(node)
              if tag(c) in ("setup", "paper", "title_block", "general")]
             or [1]) + 1
    for nm in sorted(num, key=lambda k: num[k], reverse=True):
        node.insert(at, [Bare("net"), Bare(str(num[nm])), Q(nm)])
    for fp in kids(node, "footprint"):
        if kid(fp, "tedit") is None:
            at = next((i for i, x in enumerate(fp)
                       if tag(x) == "layer"), 1)
            fp.insert(at + 1, [Bare("tedit"), Bare("0")])
    return node


def convert_pcb(path, out_path):
    node, _ = read(open(path).read())
    open(out_path, "w").write(write(pcb_v6(node)) + "\n")


GEOM = ("segment", "via", "pad", "filled_polygon", "gr_rect", "fp_line",
        "fp_rect", "fp_poly", "polygon", "footprint", "zone")


def pcb_geometry(path):
    """Every copper and outline object, keyed by its own coordinates.

    NOT A GERBER COMPARISON, and the first attempt was one. Plotting both
    files gave four copper layers that differed -- and the difference was
    not in the files at all: KiCad RE-FILLS a zone when the version token
    says the fill came from an older algorithm, so it was comparing its
    own recomputed pour against the stored one. The stored filled_polygon
    blocks are identical, all 1031 points of them. Compare what the file
    SAYS and the question goes away.
    """
    node, _ = read(open(path).read())
    out = {}

    def num(x):
        try:
            return round(float(x), 4)
        except Exception:
            return str(x)

    def walk(nd):
        if tag(nd) in GEOM:
            key = [tag(nd)]
            for c in nd:
                # A STROKE AND A WIDTH ARE THE SAME NUMBER. 6.0 writes
                # (width 0.12) on a graphic and 7.0 writes (stroke (width
                # 0.12) (type solid)), so a comparison that keys on the
                # token reports every silkscreen line as a difference --
                # 214 of them on the element board -- and none of them
                # moved. Normalise before keying or the check measures
                # the conversion instead of the copper.
                if tag(c) == "stroke":
                    w = kid(c, "width")
                    if w is not None:
                        key.append(("width", num(w[1])))
                    continue
                if isinstance(c, list) and tag(c) in (
                        "at", "start", "end", "size", "drill", "width",
                        "layer", "layers", "pts"):
                    key.append(tuple([tag(c)] + [num(x) for x in c[1:]
                                                 if not isinstance(x, list)]))
                    if tag(c) == "pts":
                        key.append(tuple(tuple(num(v) for v in xy[1:])
                                         for xy in c[1:]))
            k = tuple(str(x) for x in key)
            out[k] = out.get(k, 0) + 1
        for c in nd:
            if isinstance(c, list):
                walk(c)
    walk(node)
    return out
