"""How edaloop does on boards that are known to work.

Each sample is a shipping product whose design files are public. A failure on one is therefore a finding about edaloop first: it either
misread the board, or its check is wrong for a design that demonstrably works. Results are `pass`, `gap` (edaloop disagrees with the known
board or can't handle it) or `info` (a reading with nothing known to hold it against).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from . import boardgen, checks, toolchain
from . import describe as described
from .pcbfile import Board, parse_sexpr

CACHE = Path.home() / ".cache" / "edaloop" / "evaluation"
MANIFEST = Path(__file__).resolve().parents[2] / "evaluation" / "samples.yaml"
RAW = "https://raw.githubusercontent.com/{owner}/{repo}/{commit}/{file}"
COPPER_LAYERS = {1, 2, 15, 16}


def load_samples(path: Path = MANIFEST) -> dict:
    data = yaml.safe_load(path.read_text())
    for sample in data["samples"].values():
        sample["fritzing_commit"] = data.get("fritzing_commit")
    return data["samples"]


def fritzing_score(sample: dict, board: Path) -> list[dict]:
    """Adafruit's own Fritzing part for this board, drawn separately from its netlist, as a second opinion on what edaloop read."""
    from . import fritzing, ground
    if not sample.get("fritzing"):
        return []
    part = fritzing.read_part(fritzing.fetch_part(sample["fritzing_commit"], sample["fritzing"]))
    _, nets = described.read_board(board)
    agree = fritzing.pin_agreement(part, nets)
    rows = [_row("fritzing.pins", agree["found"] >= 0.8 * agree["pins"], f"{agree['found']} of {agree['pins']} pins in Adafruit's part are nets on the board. Not found: {agree['missing'][:8]}")]
    facts = ground._facts(board, board.parent)
    x1, y1, x2, y2 = facts["outline"]
    if part["size_mm"]:
        mine, theirs = sorted([x2 - x1, y2 - y1]), sorted(part["size_mm"])
        close = all(abs(a - b) <= 1.0 for a, b in zip(mine, theirs))
        # A part's drawing can run past the board, as with an overhanging connector, so only a part smaller than the board disagrees with it.
        rows.append(_row("fritzing.size", True if close else None if all(b >= a - 1.0 for a, b in zip(mine, theirs)) else False, f"board {mine[0]:.1f} x {mine[1]:.1f} mm, part {theirs[0]:.1f} x {theirs[1]:.1f} mm"))
    pads = fritzing.pad_agreement(list(part["pads"].values()), [[p["x"], p["y"]] for p in facts["pads"]])
    if pads["compared"]:
        rows.append(_row("fritzing.pads", pads["share"] >= 0.8, f"{pads['share']:.0%} of the part's {pads['compared']} located pads have a board pad within 0.5 mm"))
    return rows


def fetch(sample: dict) -> Path:
    """The board file at its pinned commit, kept in a cache. Only ever parsed as data."""
    folder = CACHE / sample["repo"] / sample["commit"] / _cache_key(sample)
    out = folder / f"board{Path(sample['file']).suffix}"
    _download(sample, sample["file"], out)
    if sample.get("project"):
        _download(sample, sample["project"], out.with_suffix(".kicad_pro"))
        # A project's custom rules sit beside it under the same name, and KiCad's DRC reads them. Without them the board is judged by
        # its net classes alone, which a custom rule may relax on purpose. Most projects have none, and that's recorded so it isn't asked again.
        rules = str(Path(sample["project"]).with_suffix(".kicad_dru"))
        _download(sample, rules, out.with_suffix(".kicad_dru"), optional=True)
    return out


def _cache_key(sample: dict) -> str:
    """One repository can hold several boards, and an Eagle sample is the only one in its own."""
    return "" if sample["file"].endswith(".brd") else Path(sample["file"]).stem


def _download(sample: dict, file: str, out: Path, optional: bool = False) -> None:
    """Fetch one file at the pinned commit. An optional file that the commit doesn't have leaves a marker saying so, and no file."""
    absent = out.with_name(out.name + ".absent")
    if out.exists() or (optional and absent.exists()):
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    url = RAW.format(owner=sample.get("owner", "adafruit"), repo=sample["repo"], commit=sample["commit"], file=urllib.parse.quote(file))
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        if optional and e.code == 404:
            absent.write_text(f"{file} isn't in {sample['repo']} at {sample['commit']}\n")
            return
        raise
    temporary = out.with_name(out.name + ".tmp")
    temporary.write_bytes(data)
    temporary.rename(out)


def read_kicad(path: Path) -> dict:
    """What a released KiCad file says itself, before any upgrade, to hold the upgraded copy against. Older files say `module` and `fp_text`."""
    tree = parse_sexpr(path.read_text(errors="replace"))
    tracks = sum(1 for x in tree if isinstance(x, list) and x and x[0] in ("segment", "arc"))
    vias = sum(1 for x in tree if isinstance(x, list) and x and x[0] == "via")
    layers = next((x for x in tree if isinstance(x, list) and x and x[0] == "layers"), [])
    # A board can rename its copper layers, as `C1F` or `T`, so go by the layer's type.
    copper = sum(1 for x in layers[1:] if isinstance(x, list) and len(x) > 2 and str(x[2]) in ("signal", "power", "mixed", "jumper"))
    names = {x[1]: x[2] for x in tree if isinstance(x, list) and x and x[0] == "net" and len(x) > 2}
    groups: dict[str, set[str]] = {}
    elements = set()
    for part in (x for x in tree if isinstance(x, list) and x and x[0] in ("module", "footprint")):
        ref = next((str(x[2]) for x in part if isinstance(x, list) and len(x) > 2 and x[0] == "fp_text" and x[1] == "reference"), None)
        ref = ref or next((str(x[2]) for x in part if isinstance(x, list) and len(x) > 2 and x[0] == "property" and x[1] == "Reference"), "?")
        elements.add(ref)
        for pad in (x for x in part if isinstance(x, list) and x and x[0] == "pad"):
            net = next((x for x in pad if isinstance(x, list) and x and x[0] == "net"), None)
            if not net or len(net) < 2:
                continue
            name = str(net[2]) if len(net) > 2 else str(names.get(net[1], net[1]))
            if name and not name.startswith("unconnected-"):
                groups.setdefault(name, set()).add(f"{ref}.{pad[1]}")
    return {"groups": [frozenset(v) for v in groups.values() if len(v) >= 2], "tracks": tracks, "vias": vias, "copper_layers": copper, "rules": _kicad_rules(tree), "elements": elements}


def _kicad_rules(tree: list) -> dict:
    """The rules an older file carries itself. Newer files keep theirs in a project file, and `kicad-cli pcb upgrade` drops these without writing one."""
    def number(node: list, key: str) -> float | None:
        found = next((x for x in node if isinstance(x, list) and len(x) > 1 and x[0] == key), None)
        return float(found[1]) if found else None
    setup = next((x for x in tree if isinstance(x, list) and x and x[0] == "setup"), [])
    classes = [x for x in tree if isinstance(x, list) and x and x[0] == "net_class"]
    clearances = [v for v in [number(setup, "trace_clearance"), *(number(c, "clearance") for c in classes)] if v]
    widths = [v for v in [number(setup, "trace_min"), *(number(c, "trace_width") for c in classes)] if v]
    return {"clearance": min(clearances) if clearances else None, "width": min(widths) if widths else None, "edge": number(setup, "edge_clearance")}


# Faults in text, silk or mask aren't copper faults, and nor is a courtyard that overlaps another or holds a stitching via. Castellated and edge-launch pads meet the edge on purpose, and Eagle's keepouts import as rule areas that pads legitimately sit in.
_SEVERITIES = dict(text_height="ignore", text_thickness="ignore", silk_edge_clearance="ignore", unresolved_variable="ignore", malformed_courtyard="ignore", courtyards_overlap="warning", pth_inside_courtyard="warning",
                   copper_edge_clearance="warning", solder_mask_bridge="warning", items_not_allowed="warning")
# A footprint's library attributes and a fiducial's padstack aren't copper faults either, and KiCad only started checking them after these files were drawn.
_KICAD_SEVERITIES = {**_SEVERITIES, "footprint_type_mismatch": "warning", "padstack": "warning", "drill_out_of_range": "warning", "starved_thermal": "warning"}


def _lenient_project(name: str, clearance: float, width: float, edge: float, severities: dict) -> dict:
    """A project that judges a released board by its own stated clearance, with KiCad's newer general limits out of the way."""
    project = boardgen.project_file({"chain": [], "rf_line_width_mm": width}, name)
    project["board"]["design_settings"]["rules"].update(min_track_width=min(width, 0.1), min_clearance=0.0, min_via_annular_width=0.05, min_via_diameter=0.2,
                                                         min_through_hole_diameter=0.1, min_hole_clearance=0.1, min_hole_to_hole=0.1, min_text_height=0.0, min_text_thickness=0.0,
                                                         min_copper_edge_clearance=edge)
    project["board"]["design_settings"]["rule_severities"].update(severities)
    for c in project["net_settings"]["classes"]:
        c.update(clearance=clearance, track_width=width)
    return project


def copy_kicad_board(sample: dict, source: Path, facts: dict, out: Path, name: str) -> Path:
    """A working copy of a released KiCad board, in today's file format. kicad-cli reads old files and rewrites them. The cache stays as fetched."""
    out.mkdir(parents=True, exist_ok=True)
    board = out / f"{name}.kicad_pcb"
    shutil.copy(source, board)
    for ext in (".kicad_pro", ".kicad_dru"):
        if source.with_suffix(ext).exists():
            shutil.copy(source.with_suffix(ext), board.with_suffix(ext))
    r = toolchain.run([toolchain.paths()["kicad_cli"], "pcb", "upgrade", "--force", board], timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"kicad-cli couldn't upgrade it: {(r.stdout + r.stderr).strip()[-300:]}")
    if "(stackup" not in board.read_text(errors="replace"):
        # Older files don't state a stackup. `assumed` in samples.yaml supplies one, and readings that lean on it are reported, not judged.
        board.write_text(repair(board.read_text(errors="replace"), stack_for(sample, facts["copper_layers"])[0]))
    pro = board.with_suffix(".kicad_pro")
    if source.with_suffix(".kicad_pro").exists():
        project = json.loads(pro.read_text())
        project["board"]["design_settings"].setdefault("rule_severities", {}).update(_KICAD_SEVERITIES)
    else:
        # A shade under the file's own rule, as with an Eagle file: stored coordinates are rounded, so copper it passed can read a micron short.
        rules = facts["rules"]
        project = _lenient_project(name, (rules["clearance"] or 0.2) * 0.99, rules["width"] or 0.2, rules["edge"] or 0.0, _KICAD_SEVERITIES)
    pro.write_text(json.dumps(project, indent=2))
    return board


def read_eagle(path: Path) -> dict:
    """What the released file itself says, to hold the import against."""
    root = ET.parse(path).getroot()
    groups, tracks, vias, used = [], 0, 0, set()
    elements = {e.get("name") for e in root.iter("element")}
    for signal in root.iter("signal"):
        pins = {f"{c.get('element')}.{c.get('pad')}" for c in signal.findall("contactref")}
        if len(pins) >= 2:
            groups.append(frozenset(pins))
        for wire in signal.findall("wire"):
            if int(wire.get("layer", 0)) <= 16:
                tracks += 1
                used.add(int(wire.get("layer")))
        vias += len(signal.findall("via"))
    params = {p.get("name"): p.get("value") for p in root.iter("param")}
    return {"groups": groups, "tracks": tracks, "vias": vias, "copper_layers": len(used) or 2, "rules": params, "elements": elements}


def to_mm(value: str | None, default: float) -> float:
    m = re.match(r"([\d.]+)\s*(mil|mm|inch|mic)?", value or "")
    if not m:
        return default
    return float(m[1]) * {"mil": 0.0254, "mm": 1.0, "inch": 25.4, "mic": 0.001, None: 1.0}[m[2]]


def repair(text: str, stackup: dict | None) -> str:
    """Make an Eagle import loadable: items on a layer KiCad couldn't name, copper layers nothing uses, and a stackup."""
    text = text.replace('"UNDEFINED"', '"Cmts.User"')
    used = set(re.findall(r'\(layers?\s+"(In\d+\.Cu)"', text)) | set(re.findall(r'"(In\d+\.Cu)"\s*\)', text))
    def keep(m):
        return m[0] if m[1] in used else ""
    text = re.sub(r'[ \t]*\(\d+\s+"(In\d+\.Cu)"\s+signal\)\s*\n', keep, text)
    if stackup and "(stackup" not in text:
        text = re.sub(r"\(setup\b", "(setup (stackup " + boardgen.stackup_text({"stackup": stackup}) + ' (copper_finish "ENIG") (dielectric_constraints no))', text, count=1)
    return text


def stack_for(sample: dict, copper_layers: int) -> tuple[dict, list[str]]:
    """The stackup, and which of its values aren't stated by the board's own documents."""
    known, assumed = sample.get("known") or {}, sample.get("assumed") or {}
    default = {"copper_um": [35, 35], "dielectric_mm": [1.5], "epsilon_r": 4.5} if copper_layers == 2 else \
              {"copper_um": [35, 17.5, 17.5, 35], "dielectric_mm": [0.2, 1.1, 0.2], "epsilon_r": 4.5}
    stack, guesses = {}, []
    for key in ("copper_um", "dielectric_mm", "epsilon_r"):
        if key in known:
            stack[key] = known[key]
        else:
            stack[key] = assumed.get(key, default[key])
            guesses.append(key)
    stack["loss_tangent"] = 0.02
    return stack, guesses


def import_board(sample: dict, brd: Path, eagle: dict, out: Path, name: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "imported_raw.kicad_pcb"
    # kicad-cli leaves an empty rules file beside what it imports, so it reads a copy and the cache stays as fetched.
    source = out / "released.brd"
    shutil.copy(brd, source)
    r = toolchain.run([toolchain.paths()["kicad_cli"], "pcb", "import", "--format", "eagle", "-o", raw, source], timeout=300)
    if r.returncode != 0 or not raw.exists():
        raise RuntimeError(f"kicad-cli couldn't import it: {(r.stdout + r.stderr).strip()[-300:]}")
    stack, _ = stack_for(sample, eagle["copper_layers"])
    board = out / f"{name}.kicad_pcb"
    board.write_text(repair(raw.read_text(), stack))
    raw.unlink()
    (out / "imported_raw.kicad_prl").unlink(missing_ok=True)
    # The released file's own clearances, so that DRC judges the board by the rules it was made to.
    rules = eagle["rules"]
    # A shade under the file's own rule: its coordinates are rounded in mils, so copper it passed can read a micron short after import.
    clearance = min(to_mm(rules.get(k), 0.2) for k in ("mdWireWire", "mdWirePad", "mdWireVia", "mdPadPad", "mdPadVia", "mdSmdPad", "mdSmdVia", "mdSmdSmd")) * 0.99
    width = to_mm(rules.get("msWidth"), 0.2)
    project = _lenient_project(name, clearance, width, to_mm(rules.get("mdCopperDimension"), 0.25), _SEVERITIES)
    board.with_suffix(".kicad_pro").write_text(json.dumps(project, indent=2))
    return board


def import_score(board: Path, eagle: dict) -> list[dict]:
    b = Board(board)
    parts, nets = described.read_board(board)
    tracks = sum(1 for t in b.tracks if t.kind != "via")
    vias = sum(1 for t in b.tracks if t.kind == "via")
    # KiCad wants a number on every reference, so its import turns a part called VBAT into VBAT0. That's the same part.
    def released(member: str) -> str:
        ref, _, pad = member.rpartition(".")
        return f"{ref[:-1]}.{pad}" if ref not in eagle["elements"] and ref.endswith("0") and ref[:-1] in eagle["elements"] else member
    groups = [frozenset(released(m) for m in v) for k, v in nets.items() if not k.startswith("unconnected-") and len(v) >= 2]
    same = len(set(groups) & set(eagle["groups"]))
    out = [_row("import.copper", tracks == eagle["tracks"] and vias == eagle["vias"], f"{tracks} tracks and {vias} vias, the file has {eagle['tracks']} and {eagle['vias']}"),
           _row("import.nets", same == len(set(eagle["groups"])), f"{same} of {len(set(eagle['groups']))} nets join the same pads as the released file")]
    return out


def describe_score(sample: dict, board: Path, name: str) -> tuple[list[dict], dict | None]:
    known = sample.get("known") or {}
    want = known.get("rf_path")
    band = known.get("band_mhz", [1, 2])
    stated = "rf_path" in known      # null says the board has none. Left out, nothing has been verified to hold the result against.
    try:
        d = described.describe(board, name, band)
    except described.DescribeError as e:
        if not stated:
            return [_row("describe.rf_path", None, f"found no single path, and none is stated to compare with: {e}"[:300])], None
        if want is None:
            return [_row("describe.rf_path", True, f"no RF path, as expected ({e})")], None
        return [_row("describe.rf_path", False, f"found no path, expected {' > '.join(want)}: {e}")], None
    path = [d["chain"][0][1].split(".")[0], *[c[2].split(".")[0] for c in d["chain"]]]
    if not stated:
        return [_row("describe.rf_path", None, f"found {' > '.join(path)}, with no stated path to compare")], d
    if want is None:
        return [_row("describe.rf_path", False, f"found an RF path on a board with none: {' > '.join(path)}")], d
    return [_row("describe.rf_path", path == want, f"found {' > '.join(path)}, expected {' > '.join(want)}")], d


def _row(name: str, ok: bool | None, text: str) -> dict:
    return {"item": name, "result": "info" if ok is None else "pass" if ok else "gap", "text": text}


def project_for(sample: dict, out: Path, name: str, chain: list | None, width: float) -> None:
    known = sample.get("known") or {}
    reference = "In1.Cu" if len(Board(out / f"{name}.kicad_pcb").copper_thickness_um()) > 2 else "B.Cu"
    gap = 300
    cfg = {"name": name, "board": f"{name}.kicad_pcb", "route": {"keep_nets": ["GND"], "reference_layers": [reference]},
           "power": {"max_rise_c": 10, "rails": known.get("rails") or []}, "drc": {"refill_zones": True}, "rules": {},
           "thermal": {"dx_mm": 0.05, "thermal_dx_mm": 0.2, "sublayers": 3, "h_w_m2k": 10}}
    if known.get("supply_v"):
        cfg["thermal"]["supply_v"] = known["supply_v"]
    if known.get("part_watts"):
        cfg["thermal"]["part_watts"] = known["part_watts"]
    if chain:
        (out / "chains.yaml").write_text(boardgen.chains_file({"chain": chain, "rf_line_width_mm": width, "layout": {}}))
        low, high = known["band_mhz"]
        cfg["em"] = {"signal_layer": "F.Cu", "ground_layer": reference, "gap_um": gap, "side_um": 3000}
        cfg["rf"] = {"chains": "chains.yaml", "max_over_pct": 100, "impedance": {"target": 50, "tol": 10, "max_freq_ghz": high / 1000 * 1.05, "min_section_rl_db": 20}}
        cfg["em_hops"] = {"band_ghz": [round(low * 0.95 / 1000, 3), round(high * 1.05 / 1000, 3)], "reference_layer": reference, "min_rl_db": 15, "warn_rl_db": 20}
    (out / "edaloop.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))


def checks_score(sample: dict, out: Path, chain: list | None) -> list[dict]:
    from .project import Project
    p = Project(out)
    rows = []
    ev = checks.run_check(p, "drc")
    m = ev.metrics or {}
    if not m:
        rows.append(_row("known.drc", False, f"the check itself failed: {ev.summary}"))
    else:
        by = dict(sorted((m.get("by_type") or {}).items(), key=lambda kv: -kv[1])[:4])
        if m.get("refill_blocked") and m["unconnected"]:
            # The pours couldn't be refilled, so an open here may be an empty pour. That's the outline's finding, in known.drc, and not one about the copper.
            rows.append(_row("known.opens", None, f"{m['unconnected']} open connections, unjudged: the outline isn't closed, so KiCad couldn't refill the pours"))
        else:
            rows.append(_row("known.opens", m["unconnected"] == 0, f"{m['unconnected']} open connections on a board that works"))
        hard = [f for f in ev.findings if f.get("severity") == "error" and f.get("id") not in ("drc:unconnected", "drc:refill_blocked")]  # the note on blocked refills is about opens, read above
        # Copper drawn inside an Eagle footprint arrives with no net, so it reads as touching what it was made to touch.
        no_net = [f for f in hard if "[<no net>]" in f["message"] or "<no net>" in f["message"]]
        errs = len(hard) - len(no_net)
        rows.append(_row("known.drc", errs == 0, f"{errs} rule errors against the file's own clearances, and {len(no_net)} more from footprint copper the import leaves on no net. Warnings: {by}"))
    ev = checks.run_check(p, "rules")
    fab = [f["message"] for f in ev.findings if f.get("severity") == "error"]
    rows.append(_row("known.fabricator", None if ev.status == "error" else not fab, ev.summary if ev.status == "error" else ("; ".join(fab)[:200] or "within the fabricator limits")))
    if chain:
        ev = checks.run_check(p, "rf_path")
        rows.append(_row("rf.length", None if ev.status != "error" else False, f"{ev.metrics.get('routed_mm')} mm routed, {ev.metrics.get('vias')} vias, floor {ev.metrics.get('floor_mm')} mm" if ev.metrics else ev.summary))
        ev = checks.run_check(p, "impedance")
        rows.append(_row("rf.impedance", None if ev.status != "error" else False, (f"Z0 average {ev.metrics.get('z_avg_min')} to {ev.metrics.get('z_avg_max')} ohm on an assumed stackup" if ev.metrics.get("rows") else "too short for circuitRF. In 2D: " + ", ".join(f"{r['net']} {r['z0_ohm']} ohm" for r in ev.metrics.get("read_in_2d") or [])) if ev.metrics else ev.summary))
        ev = checks.run_check(p, "em_hops")
        hops = (ev.metrics or {}).get("hops") or {}
        skipped = {k: v["unsolved"][:160] for k, v in ((ev.metrics or {}).get("unsolved_why") or {}).items()}
        rows.append(_row("rf.hops", False if skipped or ev.status == "error" else None, (", ".join(f"{k}: {v['rl_db']} dB return loss, {v['il_max_db']} dB loss" for k, v in hops.items()) + (f". Couldn't model: {skipped}" if skipped else "")) if hops or skipped else ev.summary))
    if p.section("power").get("rails"):
        ev = checks.run_check(p, "power")
        fatal = [f["message"][:140] for f in ev.findings if f["severity"] == "error"]
        rows.append(_row("power.drop", not fatal and ev.status != "error", (f"worst drop {ev.metrics.get('worst_drop_mv')} mV at the stated load. " + (f"False alarm on a board that works: {fatal[:2]}" if fatal else "")) if ev.metrics else ev.summary))
        ev = checks.run_check(p, "thermal_field")
        m = ev.metrics or {}
        # Silicon stops at about 150 C. A rise past 125 C says a board rated for continuous use couldn't run, so the model is what's wrong.
        rise = m.get("part_rise_c")
        rows.append(_row("power.heat", rise is not None and rise < 125, f"copper +{m.get('rise_c')} C, with parts +{rise} C from {m.get('part_watts_total')} W at {m.get('part_h_w_m2k')} W/m2K" if m else ev.summary))
    return rows


def regenerate(sample: dict, board: Path, d: dict | None, eagle: dict, out: Path, name: str) -> list[dict]:
    """Rebuild the board from its netlist alone and hold the result against the released layout."""
    from . import compare as comparing
    if not d or not d.get("chain"):
        return [_row("regen.layout", False, "the placer only handles RF rows, and this board has none")]
    lib = out / "imported.pretty"
    r = toolchain.run([toolchain.paths()["kicad_python"], Path(__file__).with_name("kicad_ops.py"), "export_footprints", board, lib], timeout=300)
    if r.returncode != 0 or not lib.exists():
        return [_row("regen.layout", False, f"couldn't extract its footprints: {(r.stdout + r.stderr).strip()[-300:]}")]
    stack, _ = stack_for(sample, eagle["copper_layers"])
    bench = {**d, "stackup": stack, "layout": {**d["layout"], "unlisted_pads": "open", "footprints": {ref: "imported:" + str(fp).split(":")[-1] for ref, fp in d["layout"]["footprints"].items()}}}
    spec = out / "description.yaml"
    spec.write_text(yaml.safe_dump(bench, sort_keys=False))
    made = boardgen.make(spec, out / "generated", [out], True)
    if not made.get("made"):
        return [_row("regen.layout", False, f"stopped at {made.get('stopped_at')}: {str(made.get('refused') or made.get('steps', {}).get('route'))[:300]}")]
    new = out / "generated" / f"{name}.kicad_pcb"
    c = comparing.compare(new, board, [row[0] for row in d["chain"]])
    drc = made["steps"]["route"].get("final_drc") or {}
    a, b = c["first_board"], c["second_board"]
    return [_row("regen.connects", drc.get("opens") == 0 and drc.get("errors") == 0 and c["same_connections"],
                 f"{drc.get('opens')} opens, {drc.get('errors')} errors, and {'the same' if c['same_connections'] else 'different'} connections as the released board"),
            _row("regen.size", None, f"{a['size_mm']} mm, released {b['size_mm']} mm"),
            _row("regen.rf", None, f"RF {sum(a['rf_mm'].values()):.2f} mm with {a['rf_vias']} vias, released {sum(b['rf_mm'].values()):.2f} mm with {b['rf_vias']} vias"),
            _row("regen.copper", None, f"{a['copper_mm']} mm of copper and {a['vias']} vias, released {b['copper_mm']} mm and {b['vias']}")]


def evaluate_one(key: str, sample: dict, work: Path, level: str) -> dict:
    name = re.sub(r"\W", "_", key)
    out = work / key
    if out.exists():
        shutil.rmtree(out)
    result = {"sample": key, "title": sample["title"], "class": sample["class"], "product": sample["product"], "rows": []}
    try:
        source = fetch(sample)
        eagle = read_kicad(source) if source.suffix == ".kicad_pcb" else read_eagle(source)
        result["layers"] = eagle["copper_layers"]
        board = copy_kicad_board(sample, source, eagle, out, name) if source.suffix == ".kicad_pcb" else import_board(sample, source, eagle, out, name)
        result["rows"] += import_score(board, eagle)
        rows, d = describe_score(sample, board, name)
        result["rows"] += rows
        result["rows"] += fritzing_score(sample, board)
        if level == "checks":
            known = sample.get("known") or {}
            chain = d["chain"] if d and (known.get("rf_path") or known.get("try_rf")) else None
            project_for(sample, out, name, chain, d["rf_line_width_mm"] if d else 0.2)
            result["rows"] += checks_score(sample, out, chain)
        if level == "regenerate":
            result["rows"] += regenerate(sample, board, d, eagle, out, name)
    except Exception as e:  # noqa: BLE001 - a sample that breaks edaloop is exactly what this is for, and mustn't stop the others
        result["rows"].append(_row("crash", False, f"{type(e).__name__}: {e}"[:400]))
    return result


def scorecard(results: list[dict]) -> dict:
    tally = {"pass": 0, "gap": 0, "info": 0}
    for r in results:
        for row in r["rows"]:
            tally[row["result"]] += 1
    return {"tally": tally, "samples": results}


def run(names: list[str], work: Path, level: str = "describe") -> dict:
    samples = load_samples()
    wanted = names or list(samples)
    unknown = [n for n in wanted if n not in samples]
    if unknown:
        raise ValueError(f"no such sample: {unknown}. There are {list(samples)}")
    work.mkdir(parents=True, exist_ok=True)
    results = []
    for i, n in enumerate(wanted, 1):
        print(f"[{i}/{len(wanted)}] {n}: {level} level", file=sys.stderr, flush=True)
        results.append(evaluate_one(n, samples[n], work, level))
    card = scorecard(results)
    (work / "scorecard.json").write_text(json.dumps(card, indent=1, default=str))
    return card
