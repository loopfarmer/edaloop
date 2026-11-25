"""A board description from an existing schematic: the way in for a design that wasn't written as a description.

The RF path is found, not asked for: the longest route between two connectors that a signal can take. Chokes, shunts and rails
aren't part of it. What a netlist can't say is left out rather than guessed. A load's current, for one, is marked so that a build
refuses until a person supplies it.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from . import toolchain

GROUND = re.compile(r"^(GND|AGND|DGND|PGND|VSS|0|GROUND)$", re.I)
RAIL = re.compile(r"^(VDD|VCC|VBUS|V\+|\+?\d+V\d*|VIN|VBAT|V[0-9].*)$", re.I)
RF_PORT = re.compile(r"\bSMA\b|SMA_|U\.?FL|MMCX|\bBNC\b|IPEX|ANTENNA|\bANT\b|\d\.\dGHZ", re.I)   # not SMB: that's also a diode package
CONNECTOR_REF = re.compile(r"^J\d")     # JP is a header or a jumper
ACTIVE = ("U", "IC", "X", "Q")
NEEDED = "REQUIRED"
SEARCH_LIMIT = 200_000
MAX_RF_NET_PINS = 6


class DescribeError(Exception):
    pass


def read_netlist(path: Path) -> tuple[dict, dict]:
    """Parts and nets from a KiCad XML netlist, with sheet paths taken off the names."""
    root = ET.parse(path).getroot()
    parts = {}
    for comp in root.find("components"):
        fields = {f.get("name"): (f.text or "") for f in (comp.find("fields") if comp.find("fields") is not None else [])}
        entry = {"value": comp.findtext("value") or "", "footprint": comp.findtext("footprint") or ""}
        mpn = fields.get("MPN") or fields.get("Partnumber") or fields.get("Manufacturer Part Number")
        if mpn:
            entry["mpn"] = mpn
        props = {p.get("name") for p in comp.findall("property")}
        if "dnp" in props:
            entry["dnp"] = True
        parts[comp.get("ref")] = entry
    nets = {}
    for net in root.find("nets"):
        members = [f"{n.get('ref')}.{n.get('pin')}" for n in net]
        nets[net.get("name").lstrip("/")] = members
    return parts, nets


def read_board(path: Path, shared: dict | None = None) -> tuple[dict, dict]:
    """The same from a routed or compiled board, for a design with no schematic to read, such as an atopile build.

    A numbered pad on no net gets the name KiCad gives an unconnected pin, since on a board that's all an empty net can mean.
    A reference that more than one footprint carries, such as a stitching via drawn as a footprint, isn't one part, so
    those footprints are left out, and counted by reference in `shared`.
    """
    import collections

    from .pcbfile import Board, _child

    tree = Board(path).tree
    numbered = {x[1]: x[2] for x in tree if isinstance(x, list) and x and x[0] == "net" and len(x) > 2}
    footprints = [x for x in tree if isinstance(x, list) and x and x[0] == "footprint"]

    def reference(fp: list) -> str | None:
        return next((x[2] for x in fp if isinstance(x, list) and x[0] == "property" and len(x) > 2 and x[1] == "Reference"), None)

    count = collections.Counter(reference(fp) for fp in footprints)
    parts, nets = {}, {}
    for fp in footprints:
        props = {x[1]: x[2] for x in fp if isinstance(x, list) and x[0] == "property" and len(x) > 2}
        ref = props.get("Reference")
        if not ref or ref.startswith(("REF", "#")):
            continue
        if count[ref] > 1:
            if shared is not None:
                shared[ref] = count[ref]
            continue
        entry = {"value": props.get("Value", ""), "footprint": fp[1]}
        mpn = props.get("MPN") or props.get("Partnumber") or props.get("Manufacturer Part Number")
        if mpn:
            entry["mpn"] = mpn
        attr = _child(fp, "attr")
        if attr and "dnp" in attr[1:]:
            entry["dnp"] = True
        parts[ref] = entry
        seen = set()
        for pad in (x for x in fp if isinstance(x, list) and x[0] == "pad"):
            number = str(pad[1])
            layers = _child(pad, "layers")
            if not number or number in seen or not any(str(lyr).endswith(".Cu") for lyr in (layers[1:] if layers else [])):
                continue
            seen.add(number)
            n = _child(pad, "net")
            name = numbered.get(n[-1], n[-1]) if n else ""
            nets.setdefault(name or f"unconnected-({ref}-Pad{number})", []).append(f"{ref}.{number}")
    return parts, nets


def likely_rails(nets: dict, chain: list, known: list[str]) -> list[str]:
    """Nets that look like supplies: named like one, or with a capacitor to ground on them. Only a list to start from, as a netlist can't give currents."""
    rf = {row[0] for row in chain}
    caps_to_ground = set()
    for net, members in nets.items():
        if GROUND.match(net):
            caps_to_ground |= {m.split(".")[0] for m in members if m.startswith("C")}
    out = []
    for net, members in nets.items():
        if net in rf or net in known or GROUND.match(net) or len(members) < 2:
            continue
        if RAIL.match(net) or any(m.split(".")[0] in caps_to_ground for m in members):
            out.append(net)
    return sorted(out)


def board_facts(path: Path, chain: list) -> dict:
    """What a board can say that a schematic can't: its stackup and its RF track width. Leaves out what it doesn't hold."""
    from .pcbfile import Board, _child, _val
    from .thermal import _stack

    board = Board(path)
    facts: dict = {}
    setup = _child(board.tree, "setup")
    stackup = _child(setup, "stackup") if setup else None
    if stackup:
        layers, gaps = _stack(board)
        rows = [x for x in stackup if isinstance(x, list) and x and x[0] == "layer" and str(_val(x, "type") or "") not in ("copper", "") and _val(x, "epsilon_r")]
        if len(layers) >= 2 and rows:
            facts["stackup"] = {"copper_um": [round(x["thickness_um"], 2) for x in layers], "dielectric_mm": [round(g, 4) for g in gaps],
                                "epsilon_r": float(_val(rows[0], "epsilon_r")), "loss_tangent": float(_val(rows[0], "loss_tangent") or 0.02)}
    nets = {row[0] for row in chain}
    lengths: dict[float, float] = {}
    for t in board.tracks:
        if t.kind == "segment" and t.net in nets:
            lengths[t.width] = lengths.get(t.width, 0.0) + abs(t.end[0] - t.start[0]) + abs(t.end[1] - t.start[1])
    if lengths:
        facts["rf_line_width_mm"] = max(lengths, key=lengths.get)
    return facts


def _blocks_rf(ref: str, part: dict, pins: dict[str, str]) -> bool:
    """A part a signal doesn't pass through: a choke, a real resistor, or anything with a side to ground that's its only other side."""
    named = [n for n in pins.values() if not GROUND.match(n)]
    if len(named) < 2:
        return True
    if ref.startswith("L"):
        return True
    if ref[0] == "R":
        value = (str(part.get("value", "")).split() or [""])[0].upper()
        return value not in ("0R", "0", "0R0", "0OHM")
    return False


class _Spent(Exception):
    """The chain search ran out of steps."""


def find_chains(parts: dict, nets: dict, pairs: list | None = None, notes: list | None = None) -> list[list[list[str]]]:
    """Each RF path on the board, longest first, with no part shared between two of them. Each is rows of net, entry pin and exit pin.

    The search tries every route between connectors, up to SEARCH_LIMIT steps in all. A board with many connectors and
    small signal nets has more routes than that, and a path cut short would wander through logic, so the search refuses
    and names the connector pairs it can join. `pairs` says which connectors each path runs between, as a person knows
    them: a path then ends only at its start's partner, each pair has a SEARCH_LIMIT of its own, and a pair whose routes
    still can't all be tried gets its shortest route, recorded in `notes`."""
    pins: dict[str, dict[str, str]] = {r: {} for r in parts}
    for net, members in nets.items():
        for member in members:
            ref, _, pin = member.partition(".")
            if ref in pins:
                pins[ref][pin] = net
    # A part that says it's an RF connector or antenna, whatever its reference. On a schematic with no such word, any J.
    ports = sorted((r for r, p in parts.items() if RF_PORT.search(f"{p.get('value', '')} {p.get('footprint', '')}")), key=lambda r: (len(r), r))
    connectors = ports or sorted((r for r in parts if CONNECTOR_REF.match(r)), key=lambda r: (len(r), r))
    partner: dict[str, set] = {}
    for a, b in pairs or []:
        partner.setdefault(str(a), set()).add(str(b))
        partner.setdefault(str(b), set()).add(str(a))
    unknown = sorted(r for r in partner if r not in parts)
    if unknown:
        raise DescribeError(f"the chains hint names {', '.join(unknown)}, which aren't parts on this board")
    if partner:
        connectors = sorted(set(connectors) | set(partner), key=lambda r: (len(r), r))
    # A control or power bus joins many parts, and a signal doesn't travel along one.
    usable = {n for n, m in nets.items() if not GROUND.match(n) and not RAIL.match(n) and not n.startswith("unconnected-") and len(m) <= MAX_RF_NET_PINS}
    # One footprint can hold two RF ports, as an in-line part with a plug at one end and a jack at the other does. Each of its signal pins on a
    # net of its own is then a port, and a path can run from one to another. Only a part that says it's an RF port counts: a header's pins aren't.
    multi_port = {r for r in ports if len({n for n in pins[r].values() if n in usable}) >= 2}
    # One RF port and a radio: the path ends at the radio, and a series inductor on the way is matching rather than a choke.
    radio_end = len(connectors) == 1 and bool(ports) and not multi_port
    if len(connectors) < 2 and not radio_end and not multi_port:
        raise DescribeError(f"an RF path needs two connectors, and this has {connectors or 'none'}")
    chains: list = []
    used: set[str] = set()
    steps = [0]
    direct: set = set()     # the (start, end) of each path that is a shortest route, not the longest

    def shortest(a: str, b: str) -> list:
        """The route from a to b through the fewest parts, as chain rows, or [] if there is none."""
        prev: dict = {a: None}
        queue = [a]
        for ref in queue:
            if ref == b:
                break
            for pin, net in pins[ref].items():
                if net not in usable:
                    continue
                for member in nets[net]:
                    other, _, _ = member.partition(".")
                    if other in prev or other in used or other not in parts or parts[other].get("dnp"):
                        continue
                    if other != b and (other in connectors or _blocks_rf(other, parts[other], pins[other])):
                        continue
                    prev[other] = (ref, pin, net, member)
                    queue.append(other)
        if b not in prev:
            return []
        rows, ref = [], b
        while prev[ref]:
            src, pin, net, member = prev[ref]
            rows.append([net, f"{src}.{pin}", member])
            ref = src
        return rows[::-1]

    def spent() -> DescribeError:
        found = [(a, b, len(r)) for i, a in enumerate(connectors) for b in connectors[i + 1:] if (r := shortest(a, b))]
        joined = "; ".join(f"{a}-{b} ({n} hops)" for a, b, n in sorted(found, key=lambda x: x[2])[:12]) or "none"
        return DescribeError(f"more routes between the {len(connectors)} connectors than can be searched. Name the connector pairs each "
                             "RF path runs between with --chains-hint, a YAML list such as [[J1, J2]], or write the chains by hand. "
                             f"Pairs with a route, fewest hops first: {joined}")

    def longest_from(start: str) -> list:
        best: list = []
        if partner:
            steps[0] = 0        # each named pair has a budget of its own

        def walk(ref: str, pin_in: str | None, seen: set, path: list) -> None:
            nonlocal best
            steps[0] += 1
            if steps[0] > SEARCH_LIMIT:
                raise _Spent
            for pin, net in pins[ref].items():
                if pin == pin_in or net not in usable:
                    continue
                for member in nets[net]:
                    other, _, other_pin = member.partition(".")
                    if other == ref:
                        continue
                    step = [net, f"{ref}.{pin}", member]
                    if other == start and start in multi_port and path and net != path[0][0]:
                        # Back at the footprint it started from, on another of its ports.
                        if len(path) + 1 > len(best):
                            best = [*path, step]
                        continue
                    if other in seen or other in used or other not in parts or parts[other].get("dnp"):
                        continue
                    if (other in connectors and (not partner or other in partner.get(start, ()))) or (radio_end and other.startswith(ACTIVE) and len(pins[other]) > 2):
                        if len(path) + 1 > len(best):
                            best = [*path, step]
                    elif other in connectors:
                        continue        # a connector the hint doesn't pair with this one: a path doesn't run through it
                    elif radio_end and other[0] == "L" and len([n for n in pins[other].values() if not GROUND.match(n) and not RAIL.match(n)]) == 2:
                        walk(other, other_pin, seen | {other}, [*path, step])
                    elif not _blocks_rf(other, parts[other], pins[other]):
                        walk(other, other_pin, seen | {other}, [*path, step])
        try:
            walk(start, None, {start}, [])
        except _Spent:
            if not partner:
                raise spent() from None
            # Every route couldn't be tried, so take the most direct one to a partner, and say so.
            routes = [r for b in sorted(partner.get(start, ())) if b not in used for r in [shortest(start, b)] if r]
            best = min(routes, key=len) if routes else []
            if best:
                direct.add((start, best[-1][2].partition(".")[0]))
        return best
    while True:
        found = [longest_from(c) for c in connectors if c not in used and (not partner or c in partner)]
        found = [f for f in found if f]
        if not found:
            break
        best = max(found, key=len)
        chains.append(best)
        ends = (best[0][1].partition(".")[0], best[-1][2].partition(".")[0])
        if ends in direct and notes is not None:
            notes.append(f"chains: {ends[0]} to {ends[1]} is the most direct route, as there were too many routes to try every one")
        for _, a, b in best:
            used.update((a.partition(".")[0], b.partition(".")[0]))
    if not chains:
        raise DescribeError("no path between connectors through signal parts" + (" between the hinted pairs" if partner else ""))
    if notes is not None:
        joined = {frozenset((c[0][1].partition(".")[0], c[-1][2].partition(".")[0])) for c in chains}
        for a, b in pairs or []:
            if frozenset((str(a), str(b))) not in joined:
                notes.append(f"chains: no path for the hinted pair {a}-{b} that keeps clear of the parts on the other paths")
    return chains


def load_pairs(path: Path) -> list[list[str]]:
    """A chains hint: a YAML list of connector pairs, each RF path's two ends, such as [[J1, J2], [J3, J4]]."""
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise DescribeError(f"couldn't read the chains hint {path}: {e}") from None
    if isinstance(data, dict):
        data = data.get("pairs")
    if not isinstance(data, list) or not data or not all(isinstance(x, list) and len(x) == 2 and x[0] != x[1] for x in data):
        raise DescribeError(f"the chains hint {path} must be a list of connector pairs, such as [[J1, J2], [J3, J4]]")
    return [[str(a), str(b)] for a, b in data]


def describe(netlist: Path, name: str, band_mhz: list[float], source: str = "", pairs: list | None = None) -> dict:
    shared: dict[str, int] = {}
    parts, nets = read_board(netlist, shared) if Path(netlist).suffix == ".kicad_pcb" else read_netlist(netlist)
    # The carrier nets KiCad makes for an unconnected pin mean the pin is open.
    opens = []
    kept = {}
    for net, members in nets.items():
        if net.startswith("unconnected-") or net.startswith("Net-(") and len(members) == 1:
            opens += members
        else:
            kept[net] = members
    notes: list[str] = []
    channels = find_chains(parts, kept, pairs, notes)
    chain = [row for channel in channels for row in channel]
    footprints = {r: p.pop("footprint") for r, p in parts.items()}
    missing = [r for r, f in footprints.items() if not f]
    if missing:
        raise DescribeError(f"no footprint on {missing}. Assign them in the schematic first")
    # A feed choke on the last RF net, and any rail it reaches, are what the description can say about power.
    rails = []
    seen_rails = set()
    for channel in channels:
        last_net = channel[-1][0]
        for member in kept[last_net]:
            ref, _, pin = member.partition(".")
            if ref.startswith("L"):
                for net, members in kept.items():
                    # A series inductor in the path has an RF net on its far side, and that isn't a rail it feeds from.
                    if net != last_net and net not in seen_rails and net not in {row[0] for row in chain} and any(m.startswith(ref + ".") for m in members):
                        seen_rails.add(net)
                        feed = next(m for m in members if m.startswith(ref + "."))
                        loads = {m: NEEDED for m in members if not m.startswith(ref + ".") and not m.startswith("C")}
                        rails.append({"net": net, "source": feed, "loads": loads, "max_drop_mv": 50})
    models = {}
    for ref in parts:
        if ref.startswith("J") or ref.startswith("FL") or parts[ref].get("dnp"):
            models[ref] = {"kind": "open", "reason": "no DC path modelled: connector, RF filter or not fitted"}
    facts = board_facts(Path(netlist), chain) if Path(netlist).suffix == ".kicad_pcb" else {}
    assumed = [k for k in ("stackup", "rf_line_width_mm") if k not in facts] + notes
    if shared:
        assumed.append("parts: left out footprints that share a reference, so aren't one part: " +
                       ", ".join(f"{r} x{n}" for r, n in sorted(shared.items(), key=lambda x: -x[1])))
    description = {
        "name": name,
        "summary": f"Described from {source or Path(netlist).name}. {len(channels)} RF path(s), the first: " +
                   " > ".join([channels[0][0][1].split(".")[0], *[c[2].split(".")[0] for c in channels[0]]]),
        "band_mhz": band_mhz, "supply_v": 5.0,
        "stackup": facts.get("stackup") or {"copper_um": [35, 15.2, 15.2, 35], "dielectric_mm": [0.2104, 1.065, 0.2104], "epsilon_r": 4.3, "loss_tangent": 0.02},
        "rf_line_width_mm": facts.get("rf_line_width_mm", 0.36),
        "assumed": assumed,
        "likely_rails": likely_rails(kept, chain, [x["net"] for x in rails]),
        "parts": parts, "nets": kept, "open_pins": sorted(opens), "chain": chain,
        "spice": {"models": models, "bench": {"name": "bias", "stimulus": [f"Vcable {chain[-1][0]} 0 DC 5"],
                                               "measure": [".control", "op", "let i_total_ma = -i(vcable)*1000", "print i_total_ma", "quit", ".endc"]},
                  "expect": {}},
        "layout": {"axis": "x", "line": 30.0, "gap_mm": 0.5, "pour_clearance_mm": 0.3, "footprints": footprints,
                   "ground": {"fence_pitch_mm": 4.0, "stitch_pitch_mm": 4.0}},
        "project": {"eps_eff": 3.25, "rails": rails},
    }
    return description


IMPORTED = "imported"


def export_board_footprints(board: Path, folder: Path) -> Path:
    """Copy every footprint a board uses into `folder`/imported.pretty, at rest and with a courtyard, and return the
    library. A board imported from EAGLE names footprints with no `library:` prefix, so these copies are the only
    library its parts can be placed from. Needs KiCad's Python."""
    lib = Path(folder) / f"{IMPORTED}.pretty"
    r = toolchain.run([toolchain.paths()["kicad_python"], Path(__file__).with_name("kicad_ops.py"), "export_footprints", board, lib], timeout=300)
    if r.returncode != 0 or not lib.is_dir():
        raise DescribeError(f"couldn't copy the board's footprints: {(r.stdout + r.stderr).strip()[-300:]}")
    return lib


def imported_names(footprints: dict) -> dict:
    """Each part's footprint, renamed into the imported library: `imported:<name>`, with any old library dropped."""
    return {ref: f"{IMPORTED}:" + str(fp).split(":")[-1] for ref, fp in footprints.items()}


def board_footprints(board: Path, description: dict, out: Path, replace: bool = True) -> dict | None:
    """When the board names footprints the placer can't load, copy them from the board beside the description and
    point the description at the copies. None when every footprint already resolves, and the description is unchanged.
    With `replace` false, a folder already there is left alone and the footprints aren't copied."""
    from . import boardgen
    unresolved = boardgen.unresolved_footprints(description, [])
    if not unresolved:
        return None
    folder = Path(out).parent / f"{Path(out).stem}_footprints"
    if not replace and (folder.exists() or folder.is_symlink()):
        return {"unresolved": len(unresolved), "copied": False,
                "problem": f"{len(unresolved)} parts name footprints the placer can't load, and {folder.name} is already there, "
                           "so they weren't copied into it. Name another output file"}
    made = not folder.exists()
    try:
        lib = export_board_footprints(board, folder)
    except (DescribeError, toolchain.ToolMissing) as e:
        if made:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)
        return {"unresolved": len(unresolved), "copied": False,
                "problem": f"{len(unresolved)} parts name footprints the placer can't load, and copying them from the board failed: {e}. "
                           "Name library footprints in layout.footprints before building"}
    renamed = imported_names(description["layout"]["footprints"])
    absent = sorted({fp for fp in renamed.values() if not (lib / (fp.split(":", 1)[1] + ".kicad_mod")).is_file()})
    if absent:
        raise DescribeError(f"the board's footprint copies are missing {', '.join(absent[:10])}")
    description["layout"]["footprints"] = renamed
    rel = folder.name
    return {"unresolved": len(unresolved), "copied": True, "library": str(lib), "footprints": len(set(renamed.values())),
            "build": f"edaloop build {Path(out).name} -o board --footprints {rel}"}


def describe_schematic(schematic: Path, name: str, band_mhz: list[float], out: Path, work: Path, replace_footprints: bool = True,
                       pairs: list | None = None) -> dict:
    """Write the description from a schematic, through KiCad's netlist, or straight from a board's pads."""
    from .project import FILE
    if Path(out).name == FILE or Path(out).resolve().name == FILE:
        # edaloop.yaml holds a person's requirements and waivers. A description is a different file.
        raise DescribeError(f"won't write a description over {FILE}: it holds a person's requirements and waivers. Name another file")
    if Path(schematic).suffix == ".kicad_pcb":
        netlist = Path(schematic)
    else:
        netlist = work / "describe_netlist.xml"
        work.mkdir(parents=True, exist_ok=True)
        r = toolchain.run([toolchain.paths()["kicad_cli"], "sch", "export", "netlist", "--format", "kicadxml", "-o", netlist, schematic], timeout=120)
        if r.returncode != 0 or not netlist.exists():
            raise DescribeError(f"couldn't export a netlist: {r.stderr.strip()[-300:]}")
    description = describe(netlist, name, band_mhz, Path(schematic).name, pairs)
    copied = board_footprints(Path(schematic), description, Path(out), replace_footprints) if Path(schematic).suffix == ".kicad_pcb" else None
    head = "# Written by `edaloop describe`. Check the RF path, and supply anything marked REQUIRED.\n"
    if copied and copied["copied"]:
        head += (f"# Its footprints are copies from {Path(schematic).name}, in {Path(copied['library']).parent.name}/. "
                 f"Build with: {copied['build']}\n")
    out.write_text(head + yaml.safe_dump(description, sort_keys=False, width=120))
    needed = [f"project.rails.{x['net']}.loads.{k}" for x in description["project"]["rails"] for k, v in x["loads"].items() if v == NEEDED]
    return {"written": str(out), "chain": description["summary"], "channels": sum(1 for row in description["chain"] if row[2].startswith("J")), "required": needed, "assumed": description["assumed"], "likely_rails": description["likely_rails"], "parts": len(description["parts"]), "nets": len(description["nets"]),
            **({"board_footprints": copied} if copied else {})}
