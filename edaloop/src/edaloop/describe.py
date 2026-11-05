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


def read_board(path: Path) -> tuple[dict, dict]:
    """The same from a routed or compiled board, for a design with no schematic to read, such as an atopile build.

    A numbered pad on no net gets the name KiCad gives an unconnected pin, since on a board that's all an empty net can mean.
    """
    from .pcbfile import Board, _child

    tree = Board(path).tree
    numbered = {x[1]: x[2] for x in tree if isinstance(x, list) and x and x[0] == "net" and len(x) > 2}
    parts, nets = {}, {}
    for fp in (x for x in tree if isinstance(x, list) and x and x[0] == "footprint"):
        props = {x[1]: x[2] for x in fp if isinstance(x, list) and x[0] == "property" and len(x) > 2}
        ref = props.get("Reference")
        if not ref or ref.startswith(("REF", "#")):
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


def find_chains(parts: dict, nets: dict) -> list[list[list[str]]]:
    """Each RF path on the board, longest first, with no part shared between two of them. Each is rows of net, entry pin and exit pin."""
    pins: dict[str, dict[str, str]] = {r: {} for r in parts}
    for net, members in nets.items():
        for member in members:
            ref, _, pin = member.partition(".")
            if ref in pins:
                pins[ref][pin] = net
    # A part that says it's an RF connector or antenna, whatever its reference. On a schematic with no such word, any J.
    ports = sorted((r for r, p in parts.items() if RF_PORT.search(f"{p.get('value', '')} {p.get('footprint', '')}")), key=lambda r: (len(r), r))
    connectors = ports or sorted((r for r in parts if CONNECTOR_REF.match(r)), key=lambda r: (len(r), r))
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

    def longest_from(start: str) -> list:
        best: list = []

        def walk(ref: str, pin_in: str | None, seen: set, path: list) -> None:
            nonlocal best
            steps[0] += 1
            if steps[0] > SEARCH_LIMIT:
                raise DescribeError(f"more routes between the {len(connectors)} connectors than can be searched. A board with many channels or a "
                                    "combining node needs its chains written by hand, for now")
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
                    if other in connectors or (radio_end and other.startswith(ACTIVE) and len(pins[other]) > 2):
                        if len(path) + 1 > len(best):
                            best = [*path, step]
                    elif radio_end and other[0] == "L" and len([n for n in pins[other].values() if not GROUND.match(n) and not RAIL.match(n)]) == 2:
                        walk(other, other_pin, seen | {other}, [*path, step])
                    elif not _blocks_rf(other, parts[other], pins[other]):
                        walk(other, other_pin, seen | {other}, [*path, step])
        walk(start, None, {start}, [])
        return best
    while True:
        found = [longest_from(c) for c in connectors if c not in used]
        found = [f for f in found if f]
        if not found:
            break
        best = max(found, key=len)
        chains.append(best)
        for _, a, b in best:
            used.update((a.partition(".")[0], b.partition(".")[0]))
    if not chains:
        raise DescribeError("no path between connectors through signal parts")
    return chains


def describe(netlist: Path, name: str, band_mhz: list[float], source: str = "") -> dict:
    parts, nets = read_board(netlist) if Path(netlist).suffix == ".kicad_pcb" else read_netlist(netlist)
    # The carrier nets KiCad makes for an unconnected pin mean the pin is open.
    opens = []
    kept = {}
    for net, members in nets.items():
        if net.startswith("unconnected-") or net.startswith("Net-(") and len(members) == 1:
            opens += members
        else:
            kept[net] = members
    channels = find_chains(parts, kept)
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
    assumed = [k for k in ("stackup", "rf_line_width_mm") if k not in facts]
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


def describe_schematic(schematic: Path, name: str, band_mhz: list[float], out: Path, work: Path) -> dict:
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
    description = describe(netlist, name, band_mhz, Path(schematic).name)
    out.write_text("# Written by `edaloop describe`. Check the RF path, and supply anything marked REQUIRED.\n" +
                   yaml.safe_dump(description, sort_keys=False, width=120))
    needed = [f"project.rails.{x['net']}.loads.{k}" for x in description["project"]["rails"] for k, v in x["loads"].items() if v == NEEDED]
    return {"written": str(out), "chain": description["summary"], "channels": sum(1 for row in description["chain"] if row[2].startswith("J")), "required": needed, "assumed": description["assumed"], "likely_rails": description["likely_rails"], "parts": len(description["parts"]), "nets": len(description["nets"])}
