"""The checks. Each reads the design, runs one authority, and reports evidence.

Which tool is the authority for which question is decided here and nowhere
else. See docs/AUTHORITY.md for why each choice was made.
"""
from __future__ import annotations

import collections
import fnmatch
import json
import math
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import asdict
from pathlib import Path

import numpy as np
import skrf
import yaml

from . import parts_api, power, spice, thermal, toolchain
from .em_diagnose import diagnose
from .ledger import ERROR, FAIL, PASS, WARN, Evidence, Finding, sha_bytes, sha_file
from .pcbfile import Board, compare_copper
from .project import Project, ProjectError

REGISTRY = {}


def check(name, stage, needs_board=True):
    def deco(fn):
        REGISTRY[name] = dict(fn=fn, stage=stage, needs_board=needs_board, doc=(fn.__doc__ or "").strip())
        return fn
    return deco


class Outcome:
    def __init__(self, inputs, metrics=None, findings=None, tools=None, artifacts=None, summary=""):
        self.inputs, self.metrics = inputs, metrics or {}
        self.findings, self.tools = findings or [], tools or {}
        self.artifacts, self.summary = artifacts or [], summary


# Recorded for an input that wasn't there when the check ran, so that creating it later makes the evidence stale.
ABSENT = "absent"


def _files(p: Project, *paths) -> dict[str, str]:
    out = {}
    for path in paths:
        if not path:
            continue
        path = Path(path)
        key = str(path.relative_to(p.root))
        if not path.exists():
            out[key] = ABSENT
            continue
        out[key] = sha_file(path)
        # A change on a channel's page is a change to the schematic, though the root's bytes stay the same.
        if str(path).endswith(".kicad_sch"):
            for child in re.findall(r'\(property "Sheetfile" "([^"]+)"', path.read_text(errors="replace")):
                out.update(_files(p, path.parent / child))
    return out


def _digest(obj) -> str:
    return sha_bytes(json.dumps(obj, sort_keys=True, default=str).encode())


class UnknownCheck(ProjectError):
    """A check name that isn't in the registry. Raised before anything runs or is recorded."""


def require_check(name: str) -> dict:
    """The registry entry for `name`, or a refusal that lists the checks there are."""
    spec = REGISTRY.get(name)
    if spec is None:
        raise UnknownCheck(f"no check named {name!r}. Available checks: {', '.join(sorted(REGISTRY))}")
    return spec


def run_check(p: Project, name: str) -> Evidence:
    # An unknown name is refused and deliberately not recorded. No check ran and no input was read, so
    # there are no bytes for evidence to stand on, and an entry would be evidence of nothing. It could
    # never read as a pass either way, but an error filed under a typo would add a ledger name that no
    # gate resolves. A gated name with no check is already reported by gate_state as "unknown", which
    # hands it to a person, so leaving the ledger untouched is the honest record.
    spec = require_check(name)
    start = time.time()
    try:
        o = spec["fn"](p)
    except Exception as e:  # noqa: BLE001 - a tool crash is evidence too, never a pass
        ev = Evidence(name, ERROR, f"{type(e).__name__}: {e}"[:400], {}, seconds=round(time.time() - start, 1))
        return p.ledger.append(ev)
    # Errors first, so that whoever reads the top of the list reads what's wrong rather than a note on the way to it.
    findings = sorted(p.apply_waivers(o.findings), key=lambda f: f.severity != "error")
    live = [f for f in findings if not f.waived_by]
    errors = [f for f in live if f.severity == "error"]
    warnings = [f for f in live if f.severity != "error"]
    status = FAIL if errors else WARN if warnings else PASS
    summary = o.summary or (f"{len(errors)} error(s), {len(warnings)} warning(s), "
                            f"{len(findings) - len(live)} waived")
    ev = Evidence(name, status, summary, o.inputs, o.metrics, [asdict(f) for f in findings],
                  o.tools, o.artifacts, round(time.time() - start, 1))
    return p.ledger.append(ev)


# ---------------------------------------------------------------- capture

@check("capture", "capture")
def capture(p: Project) -> Outcome:
    """Compile the atopile source into the KiCad board, when the design is written in ato."""
    cfg = p.section("capture")
    if cfg.get("kind") != "atopile":
        raise RuntimeError("capture needs capture.kind: atopile in edaloop.yaml")
    tp = toolchain.paths()
    build = cfg.get("build", "default")
    # Parts come from the local pcbparts catalogue, since atopile's hosted service is gone.
    with parts_api.Running() as parts:
        r = toolchain.run([tp["ato"], "build", "--build", build], cwd=p.root,
                          env={**toolchain.ato_env(), "ATO_SERVICES_COMPONENTS_URL": parts.url}, timeout=1800)
    log = p.work / "ato_build.log"
    log.write_text(r.stdout + r.stderr)
    findings = []
    if r.returncode != 0:
        tail = [ln.strip() for ln in (r.stdout + r.stderr).splitlines() if ln.strip()][-1:]
        findings.append(Finding("capture:build_failed", " ".join(tail)[:300]))
    sources = sorted(p.root.glob("*.ato"))
    return Outcome({**_files(p, *sources), **_files(p, p.root / "ato.yaml")},
                   {"source_files": len(sources)}, findings, {"atopile": "ato"},
                   [str(log.relative_to(p.root))], "built" if r.returncode == 0 else "")


# ---------------------------------------------------------------- verify

@check("project_files", "verify")
def project_files(p: Project) -> Outcome:
    """The board must travel with its .kicad_pro, or DRC silently applies KiCad's default rules."""
    findings = []
    if not p.project_file.exists():
        findings.append(Finding("project_files:no_kicad_pro",
                                f"{p.project_file.name} is missing, so DRC would run on default rules"))
    lib = p.board.parent / "fp-lib-table"
    if not lib.exists():
        findings.append(Finding("project_files:no_fp_lib_table",
                                "no project fp-lib-table: library checks may report false footprint issues",
                                "warning"))
    return Outcome(_files(p, p.board, p.project_file, lib), findings=findings)


@check("drc", "verify")
def drc(p: Project) -> Outcome:
    """KiCad DRC. kicad-cli is the only DRC authority: circuitRF's import carries no rules."""
    out = p.work / "drc.json"
    tp = toolchain.paths()
    cmd = [tp["kicad_cli"], "pcb", "drc", "--format", "json", "--severity-all", "-o", out, p.board]
    if p.schematic:
        cmd.insert(3, "--schematic-parity")
    # KiCad's DRC isn't always repeatable: an 8-layer board gave 1,622 or 1,623 on one file, and 1,139 or 1,180 with refill.
    repeats = int(p.section("drc").get("repeat", 1))
    # Refill by default: copper added since the last fill reads as false shorts against a stale one.
    if p.section("drc").get("refill_zones", True):
        cmd.insert(3, "--refill-zones")
    d, counts_seen = None, []
    for _ in range(max(repeats, 1)):
        out.unlink(missing_ok=True)
        r = toolchain.run(cmd, timeout=900)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"kicad-cli drc failed with exit {r.returncode}: {r.stderr.strip()[-300:]}")
        this = json.loads(out.read_text())
        counts_seen.append(len(this["violations"]))
        if d is None or len(this["violations"]) > len(d["violations"]):
            d = this                # the worst run is the evidence
    findings = []
    for v in d["violations"]:
        where = v["items"][0]["description"] if v.get("items") else ""
        sev = "error" if v["severity"] == "error" else "warning"
        findings.append(Finding(f"drc:{v['type']}", f"{v['description']} - {where}"[:240], sev))
    open_nets = collections.Counter()
    for v in d["unconnected_items"]:
        names = [m[1] for m in (re.search(r"\[([^\]]+)\]", i.get("description", "")) for i in v.get("items", [])) if m]
        name = names[0] if names else "?"
        open_nets[name] += 1
        where = " to ".join(i.get("description", "")[:60] for i in v.get("items", []))
        findings.append(Finding("drc:unconnected", f"{name}: {v['description']} - {where}"[:240]))
    for v in d["schematic_parity"]:
        findings.append(Finding(f"drc:parity:{v['type']}", v["description"][:240]))
    if len(set(counts_seen)) > 1:
        findings.append(Finding("drc:not_repeatable", f"identical input gave {counts_seen} violations; the worst is reported", "warning"))
    counts = collections.Counter(v["type"] for v in d["violations"])
    metrics = {"runs": counts_seen, "violations": len(d["violations"]), "unconnected": len(d["unconnected_items"]),
               "parity": len(d["schematic_parity"]), "by_type": dict(counts), "open_nets": dict(open_nets.most_common())}
    return Outcome({**_files(p, p.board, p.project_file, p.schematic), "drc_config": _config(p, "drc_config")}, metrics, findings,
                   {"kicad-cli": d.get("kicad_version", "")}, [str(out.relative_to(p.root))])


@check("rf_reference", "verify")
def rf_reference(p: Project) -> Outcome:
    """Nothing but ground crosses the plane under an RF line. DRC allows it, and the line's impedance doesn't survive it."""
    cfg = p.section("route")
    ref_layers = set(cfg.get("reference_layers") or ["In1.Cu"])
    ground = set(cfg.get("keep_nets") or ["GND"])
    margin = float(cfg.get("reference_margin_mm", 1.0))
    board = Board(p.board)
    nets = set(_chain_nets(p))
    rf = [t for t in board.tracks if t.net in nets and t.kind != "via"]
    intruders = [t for t in board.tracks if t.layer in ref_layers and t.kind != "via" and t.net not in ground]
    findings = _no_rf_copper("rf_reference", nets, rf)
    for t in intruders:
        for line in rf:
            if _segments_within(t, line, margin + t.width / 2 + line.width / 2):
                findings.append(Finding(f"rf_reference:{line.net}",
                                        f"{t.net} on {t.layer} runs under {line.net}, near ({t.start[0]:.2f}, {t.start[1]:.2f})"))
                break
    other = sum(math.dist(t.start, t.end) for t in intruders)
    return Outcome({**_files(p, p.board), **_chain_files(p), "route_config": _config(p, "route_config")},
                   {"crossings": len(findings), "non_ground_on_reference_mm": round(other, 2)}, findings)


@check("rf_layout", "verify", needs_board=True)
def rf_layout(p: Project) -> Outcome:
    """What RF reviews keep finding and DRC can't: the fence, the plane across a trace's width, pad gaps, limiter ground, supply on top, taps and bypass order."""
    from . import ground, rf_rules
    cfg = {**p.section("rf_layout")}
    cfg.setdefault("eps_eff", (p.section("rf").get("impedance") or {}).get("eps_eff", 3.25))
    src = p.root / p.section("rf")["chains"]
    chains = _chains(p)
    reference = (p.section("route").get("reference_layers") or ["In1.Cu"])[0]
    board = Board(p.board)
    filled = p.work / "rf_layout_filled.kicad_pcb"
    thermal.fill_copy(p.board, filled)
    facts = ground._facts(p.board, p.work)
    covered = p.work / "plane_coverage.json"
    ground._op("plane_coverage", filled, ",".join(c[0] for c in chains), reference, covered)
    parts = {}
    described = p.root / "board_description.yaml"
    if described.exists():
        bench = yaml.safe_load(described.read_text())
        footprints = (bench.get("layout") or {}).get("footprints") or {}
        parts = {r: {**(v or {}), "footprint": footprints.get(r, "")} for r, v in (bench.get("parts") or {}).items()}
        chains = bench.get("chain") or chains      # a bias stack is listed with the RF chains so it's laid straight, but it isn't an RF line
    hops = p.section("em_hops").get("band_ghz")
    band = tuple(hops) if hops else (float((p.section("rf").get("impedance") or {}).get("max_freq_ghz", 3.0)),) * 2
    metrics, found = rf_rules.audit(board.tracks, facts, chains, p.section("power").get("rails") or [], parts, band, reference,
                                    json.loads(covered.read_text()), len(board.copper_thickness_um()) or 2, cfg)
    rf_nets = {row[0] for row in chains}
    found = [(f.id, f.message, f.severity) for f in _no_rf_copper("rf_layout", rf_nets, [t for t in board.tracks if t.net in rf_nets and t.kind != "via"])] + list(found)
    return Outcome({**_files(p, p.board, src, described), "rf_layout_config": _config(p, "rf_layout_config"),
                    "rf_config": _config(p, "rf_config"), "power_config": _config(p, "power_config"),
                    "em_hops_config": _config(p, "em_hops_config"), "route_config": _config(p, "route_config")},
                   metrics, [Finding(*f) for f in found])


@check("erc", "verify")
def erc(p: Project) -> Outcome:
    """KiCad ERC over the whole schematic hierarchy."""
    if not p.schematic:
        raise RuntimeError("edaloop.yaml names no schematic")
    out = p.work / "erc.json"
    r = toolchain.run([toolchain.paths()["kicad_cli"], "sch", "erc", "--format", "json", "--severity-all",
                       "-o", out, p.schematic], timeout=600)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"kicad-cli erc failed: {r.stderr.strip()[-300:]}")
    d = json.loads(out.read_text())
    findings = []
    for sheet in d["sheets"]:
        for v in sheet["violations"]:
            sev = "error" if v["severity"] == "error" else "warning"
            findings.append(Finding(f"erc:{v['type']}", v["description"][:240], sev))
    sheets = list(p.schematic.parent.glob("*.kicad_sch"))
    return Outcome(_files(p, *sheets, p.project_file), {"violations": len(findings)}, findings,
                   {"kicad-cli": d.get("kicad_version", "")}, [str(out.relative_to(p.root))])


_RF_PATH_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1])
sys.dont_write_bytecode = True
import rf_path
chains, _, _ = rf_path.load_chains(sys.argv[3])
print(json.dumps(rf_path.measure(rf_path.Board(sys.argv[2]), chains), default=str))
"""


def _no_rf_copper(check: str, nets, rf_tracks) -> list[Finding]:
    """An RF check with no RF copper to read has measured nothing, and must say so rather than pass."""
    unrouted = sorted(set(nets) - {t.net for t in rf_tracks})
    if not unrouted:
        return []
    return [Finding(f"{check}:unrouted", f"no copper on {len(unrouted)} RF net(s), so nothing was measured there: "
                    + ", ".join(unrouted[:8]) + (" and more" if len(unrouted) > 8 else "") + ". Route the board first")]


def _segments_within(a, b, limit: float) -> bool:
    """True if two straight tracks pass within a distance, checked by sampling the shorter span."""
    def dist(px, py, s):
        dx, dy = s.end[0] - s.start[0], s.end[1] - s.start[1]
        u = max(0.0, min(1.0, ((px - s.start[0]) * dx + (py - s.start[1]) * dy) / ((dx * dx + dy * dy) or 1e-12)))
        return math.hypot(s.start[0] + u * dx - px, s.start[1] + u * dy - py)
    steps = max(2, int(math.dist(a.start, a.end) / max(limit, 0.05)) + 1)
    for i in range(steps + 1):
        u = i / steps
        if dist(a.start[0] + u * (a.end[0] - a.start[0]), a.start[1] + u * (a.end[1] - a.start[1]), b) <= limit:
            return True
    return False


def _chains(p: Project) -> list:
    from .scripts.kicad_pcb import load_chains
    return load_chains(p.root / p.section("rf")["chains"])[0]


def _chain_nets(p: Project) -> list[str]:
    return [c[0] for c in _chains(p)]


def _chain_files(p: Project) -> dict[str, str]:
    """The chain spec as an input. A check that reads the RF net list must go stale when the list changes."""
    chains = p.section("rf").get("chains")
    return _files(p, p.root / chains) if chains else {}


@check("rf_path", "verify")
def rf_path(p: Project) -> Outcome:
    """Each RF net measured against its spanning-tree floor. DRC cannot see any of this."""
    cfg = p.section("rf")
    chains = p.root / cfg["chains"]
    r = toolchain.run([sys.executable, "-c", _RF_PATH_DRIVER, Path(__file__).with_name("scripts"), p.board, chains], timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"rf_path failed: {r.stderr.strip()[-300:]}")
    rows = json.loads(r.stdout)
    max_over = float(cfg.get("max_over_pct", 5.0))
    findings = []
    floor = routed = vias = 0.0
    for row in rows:
        net = row["net"]
        if row.get("missing"):
            findings.append(Finding(f"rf_path:{net}:missing", f"{net}: pad {row['missing']} not found"))
            continue
        floor += row.get("floor") or 0
        routed += row.get("routed") or 0
        vias += row.get("vias") or 0
        if row.get("off_bad"):
            findings.append(Finding(f"rf_path:{net}:off_axis",
                                    f"{net}: pads off axis by {row.get('off')} mm - fix placement, not routing"))
        if row.get("vias"):
            findings.append(Finding(f"rf_path:{net}:vias", f"{net}: {row['vias']} via(s) in an RF net"))
        if row.get("floor") and row.get("routed") is not None:
            over = 100 * (row["routed"] / row["floor"] - 1)
            if row["routed"] == 0:
                findings.append(Finding(f"rf_path:{net}:unrouted", f"{net}: no copper measured"))
            elif over > max_over:
                findings.append(Finding(f"rf_path:{net}:over_floor",
                                        f"{net}: {over:.1f}% over its floor of {row['floor']:.2f} mm"))
    metrics = {"floor_mm": round(floor, 3), "routed_mm": round(routed, 3), "vias": int(vias),
               "nets": len(rows), "over_pct": round(100 * (routed / floor - 1), 2) if floor else None}
    return Outcome(_files(p, p.board, chains), metrics, findings, {"rf_path": "scripts/rf_path.py"})


def rf_baseline_path(p: Project) -> Path:
    """Where RF copper's baseline lives: beside edaloop.yaml, so that it is committed with the project.

    It is not configurable. Pointing the check at a new file would be a way to start a new baseline unseen.
    """
    return p.root / "rf_baseline.json"


@check("rf_preserved", "verify")
def rf_preserved(p: Project) -> Outcome:
    """RF copper and ground vias are held to the baseline: a fixed wire is only a cost to Freerouting, not a wall."""
    base, legacy = rf_baseline_path(p), p.root / ".edaloop" / "rf_baseline.json"
    key, legacy_key = str(base.relative_to(p.root)), str(legacy.relative_to(p.root))
    nets = _chain_nets(p)
    board = Board(p.board)
    now = board.copper_items(nets)
    cfg = p.section("route")
    ground_nets = list(cfg.get("keep_nets") or ["GND"])
    inputs = lambda: {**_files(p, p.board, base), **_chain_files(p), "route_config": _config(p, "route_config")}
    # The first baseline this project recorded is the one copper is held to. Deleting or rewriting the file
    # mustn't quietly move the reference to wherever a router left the copper. Older projects kept it in .edaloop/.
    recorded = lambda ev: next((ev.inputs[k] for k in (key, legacy_key) if ev.inputs.get(k, ABSENT) != ABSENT), None)
    first = next((ev for ev in p.ledger.all() if ev.check == "rf_preserved" and recorded(ev)), None)
    findings = []
    if not base.exists() and legacy.exists():
        # The baseline moves beside edaloop.yaml, where it is committed with the project and outlives .edaloop/.
        base.write_bytes(legacy.read_bytes())
    if not base.exists():
        base.write_text(json.dumps({"rf": now, "ground_vias": board.ground_vias(ground_nets)}))
        if first is None:
            return Outcome(inputs(), {"nets": len(nets), "ground_vias": board.ground_vias(ground_nets)},
                           summary=f"baseline recorded for {len(nets)} RF nets in {key}. Commit it with the project")
    if first is not None and sha_file(base) != recorded(first):
        findings.append(Finding("rf_preserved:baseline_changed",
                                f"{key} is not the baseline first recorded on {first.when}, so the copper is no longer held to it. "
                                "Restore that file, or have a person waive rf_preserved:baseline_changed with a name and a reason"))
    old = json.loads(base.read_text())
    delta = compare_copper(old["rf"], now)
    max_added = float(cfg.get("max_rf_added_mm", 0.01))
    findings += [Finding(f"rf_preserved:{n}:lost", f"{n}: {c} baseline RF item(s) gone or moved") for n, c in delta["lost"].items()]
    if delta["added_mm"] > max_added:
        findings.append(Finding("rf_preserved:added", f"{delta['added_mm']} mm of new copper on RF nets (limit {max_added})"))
    if delta["new_vias"]:
        findings.append(Finding("rf_preserved:vias", f"{delta['new_vias']} new via(s) on RF nets"))
    vias = board.ground_vias(ground_nets)
    if vias < old["ground_vias"]:
        findings.append(Finding("rf_preserved:ground_vias",
                                f"ground vias fell from {old['ground_vias']} to {vias}. DRC does not flag this"))
    return Outcome(inputs(), {"nets": len(nets), "lost": len(delta["lost"]),
                                              "added_mm": delta["added_mm"], "ground_vias": vias}, findings)


_IMP_T = re.compile(r"^\s*(T\d+)\s+(PASS|WARN\w*|FAIL)\s+Z0 ([\d.]+)(?:–([\d.]+))? Ω(?: \(avg ([\d.]+)\))?(?:.*?, ([\d.]+) µm,)?.*")
_IMP_DIP = re.compile(r"!\s*Z0 ([\d.]+) Ω over ([\d.]+) µm from \((-?[\d.]+), (-?[\d.]+)\) µm")


def section_rl_db(z_ohm: float, length_um: float, f_hz: float, eps_eff: float, z_ref: float) -> float:
    """Return loss of one short off-impedance section between matched lines, from its ABCD matrix."""
    theta = 2 * np.pi * f_hz * np.sqrt(eps_eff) / 299792458.0 * length_um * 1e-6
    a, d = np.cos(theta), np.cos(theta)
    b, c = 1j * z_ohm * np.sin(theta), 1j * np.sin(theta) / z_ohm
    s11 = abs((a + b / z_ref - c * z_ref - d) / (a + b / z_ref + c * z_ref + d))
    return float("inf") if s11 < 1e-12 else float(-20 * np.log10(s11))


def _unread_net_2d(p: Project, net: str, target: float, f_hz: float, eps: float) -> dict | None:
    """A net circuitRF can't read, in the 2D reference solver at its own width. None if there's no copper or no EM setup to read it by."""
    cfg = p.section("em")
    if not cfg.get("signal_layer") or not cfg.get("gap_um"):
        return None
    board = Board(p.board)
    segs = [t for t in board.tracks if t.net == net and t.kind == "segment" and t.layer == cfg["signal_layer"]]
    if not segs:
        return None
    longest = {}
    for s in segs:
        longest[s.width] = longest.get(s.width, 0.0) + math.hypot(s.end[0] - s.start[0], s.end[1] - s.start[1])
    width = max(longest, key=longest.get)
    z = _em_reference(board, {**cfg, "width_um": width * 1000})["z0_ohm"]
    length_um = sum(longest.values()) * 1000
    return {"net": net, "width_mm": width, "length_um": round(length_um, 1), "z0_ohm": z,
            "rl_db": round(section_rl_db(z, length_um, f_hz, eps, target), 1)}


def _pad_land(p: Project, net: str, x_um: float, y_um: float, within_mm: float = 1.0) -> str:
    """Which pad a low section sits on, when it's one. circuitRF reports y upward, and KiCad downward."""
    x, y = x_um / 1000, -y_um / 1000
    near = [(math.hypot(q["x"] - x, q["y"] - y), q["name"]) for q in Board(p.board).all_pads() if q["net"] == net]
    near = [n for n in near if n[0] <= within_mm]
    return f", on the land of {min(near)[1]}" if near else ""


@check("impedance", "verify")
def impedance(p: Project) -> Outcome:
    """circuitRF cross-section Z0 on the real copper; each local dip is priced as return loss at the top of band."""
    cfg = p.section("rf")
    imp = cfg.get("impedance") or {}
    target, tol = imp.get("target", 50), imp.get("tol", 10)
    f_hz = float(imp.get("max_freq_ghz", 3.0)) * 1e9
    min_rl = float(imp.get("min_section_rl_db", 20))
    eps = float(imp.get("eps_eff", 3.25))
    tp = toolchain.paths()
    env = toolchain.circuitrf_env()
    cells = p.work / "crf_cells"
    shutil.rmtree(cells, ignore_errors=True)
    r = toolchain.run([tp["circuitrf"], "convert", p.board, "-o", cells, "--to", "clay"], env=env, timeout=600)
    clay = toolchain.glob_one(str(cells / p.board.stem / "layout" / "*.clay"))
    if not clay:
        raise RuntimeError(f"circuitRF import failed: {r.stdout.strip()[-200:]} {r.stderr.strip()[-200:]}")
    findings, rows, worst_rl = [], [], None
    for net in imp.get("nets") or _chain_nets(p):
        # No --max-freq: circuitRF would then drop sections it judges electrically
        # short and the dips would vanish. They are all priced below instead.
        r = toolchain.run([tp["circuitrf"], "impedance", clay, "--target", target, "--tol", tol, "--net", net],
                          env=env, timeout=900)
        text = r.stdout
        for ln in text.splitlines():
            m = _IMP_T.match(ln)
            if m:
                lo = float(m[3])
                hi = float(m[4] or m[3])
                rows.append({"net": net, "status": m[2], "z_min": lo, "z_max": hi, "z_avg": float(m[5] or lo),
                             "length_um": float(m[6]) if m[6] else None})
            d = _IMP_DIP.search(ln)
            if d:
                z, length = float(d[1]), float(d[2])
                rl = section_rl_db(z, length, f_hz, eps, target)
                worst_rl = rl if worst_rl is None else min(worst_rl, rl)
                msg = (f"{net}: {z:.1f} ohm over {length:.0f} um at ({d[3]}, {d[4]}) um{_pad_land(p, net, float(d[3]), float(d[4]))}; "
                       f"return loss {rl:.1f} dB at {f_hz / 1e9:g} GHz")
                findings.append(Finding(f"impedance:{net}:section", msg, "warning" if rl >= min_rl else "error"))
    metrics = {"traces": len(rows), "z_avg_min": min((x["z_avg"] for x in rows), default=None),
               "z_avg_max": max((x["z_avg"] for x in rows), default=None), "worst_section_rl_db": worst_rl, "rows": rows}
    located = {f.id.split(":")[1] for f in findings}
    read = {x["net"] for x in rows}
    for net in imp.get("nets") or _chain_nets(p):
        if net not in read:
            # Too short to read, usually. Still not the same as measured and fine, so it stays visible.
            reading = _unread_net_2d(p, net, target, f_hz, eps)
            if reading:
                metrics.setdefault("read_in_2d", []).append(reading)
                findings.append(Finding(f"impedance:{net}:unread",
                                        f"{net}: circuitRF gave no reading. A 2D cross-section of its {reading['width_mm']} mm track, "
                                        f"{reading['length_um']:.0f} um long, reads {reading['z0_ohm']} ohm, return loss {reading['rl_db']:.1f} dB "
                                        f"at {f_hz / 1e9:g} GHz. That's the straight line only, with no pad or bend", "warning"))
            else:
                findings.append(Finding(f"impedance:{net}:unread", f"{net}: circuitRF gave no reading, so this net isn't checked here",
                                        "warning"))
    if not rows and not metrics.get("read_in_2d"):
        raise RuntimeError("circuitRF reported no traces on the chain nets, and there's no EM setup to read them in 2D")
    for row in rows:
        if row["status"] != "PASS" and row["net"] not in located:
            findings.append(Finding(f"impedance:{row['net']}:unlocated",
                                    f"{row['net']}: circuitRF says {row['status']} (min {row['z_min']} ohm) but gave no location",
                                    "warning"))
        if not target * (1 - tol / 100) <= row["z_avg"] <= target * (1 + tol / 100):
            # A neck at a fine-pitch pin is a short section, not the line's impedance, so it's priced like a dip.
            # It must be electrically short as well: a long line at the wrong impedance stays an error.
            length = row.get("length_um")
            lambda_g_um = 299792458.0 / (f_hz * eps ** 0.5) * 1e6
            rl = section_rl_db(row["z_avg"], length, f_hz, eps, target) if length else None
            short = bool(length) and length < lambda_g_um / 20 and rl >= min_rl
            detail = f"; {length:.0f} um long, return loss {rl:.1f} dB at {f_hz / 1e9:.2f} GHz" if length else ""
            findings.append(Finding(f"impedance:{row['net']}:average",
                                    f"{row['net']}: average Z0 {row['z_avg']:.1f} ohm is outside {target} ohm +/- {tol}%{detail}",
                                    "warning" if short else "error"))
    return Outcome(_files(p, p.board, p.project_file), metrics, findings, {"circuitRF": "impedance"})


SHORT_NECK_MM = 5.0


@check("power", "verify")
def power_rails(p: Project) -> Outcome:
    """Each rail's drop and heating on its real copper, from stated load currents. DRC sees none of this."""
    cfg = p.section("power")
    board = Board(p.board)
    thickness = board.copper_thickness_um()
    findings, rows = [], {}
    default_rise = float(cfg.get("max_rise_c", 10))
    if not cfg.get("rails"):
        # Nothing to check must not read as nothing wrong.
        findings.append(Finding("power:no_rails", "no rails are described, so no supply was checked. Add each with its source and load currents under power.rails"))
    for rail in cfg.get("rails") or []:
        net = rail["net"]
        tracks = [t for t in board.tracks if t.net == net]
        pads = board.net_pads(net)
        # Each pad's real layers: a through-hole pad joins every copper layer, and a top pad doesn't touch the bottom.
        layer_map = power.pad_layers([x for x in board.all_pads() if x["net"] == net], list(thickness))
        r = power.solve_rail(net, tracks, pads, rail["source"], {k: float(v) for k, v in rail["loads"].items()},
                             thickness_um=thickness, board_height_mm=board.board_height_mm(), pad_layer_map=layer_map)
        rows[net] = {k: getattr(r, k) for k in ("connected", "drops_mv", "worst_drop_mv", "worst_rise_c", "worst_segment",
                                                "min_width_mm", "total_a", "segments", "vias")}
        pours = board.zones_on_net(net)
        rows[net]["zones"] = pours
        if r.unreached:
            kind = "warning" if pours else "error"
            findings.append(Finding(f"power:{net}:unreached", f"{net}: tracks don't reach {r.unreached}"
                                    + (f" and {pours} pour(s) on the net aren't modelled" if pours else ""), kind))
        limit = float(rail.get("max_rise_c", default_rise))
        if r.worst_rise_c > limit:
            s = r.worst_segment
            outer = s["layer"] in ("F.Cu", "B.Cu")
            need = power.ipc2221_min_width_mm(s["current_a"], limit, thickness.get(s["layer"], 35.0), outer)
            # IPC-2221 assumes a long track. A short neck between pads sheds its heat into them, as known-good power boards show.
            span = math.dist(s["start"], s["end"])
            findings.append(Finding(f"power:{net}:heating",
                                    f"{net}: {s['width_mm']} mm on {s['layer']} carries {s['current_a']} A, rising "
                                    f"{r.worst_rise_c} C by IPC-2221 (limit {limit} C). Needs about {need:.2f} mm" + (f". It's only {span:.1f} mm long, so read thermal_field for this one" if span < SHORT_NECK_MM else ""),
                                    "warning" if span < SHORT_NECK_MM else "error"))
        max_drop = rail.get("max_drop_mv")
        if max_drop is not None and r.worst_drop_mv > float(max_drop):
            findings.append(Finding(f"power:{net}:drop", f"{net}: drop {r.worst_drop_mv} mV exceeds {max_drop} mV"))
    worst = max(rows.values(), key=lambda x: x["worst_rise_c"], default=None)
    metrics = {"rails": rows, "worst_rise_c": worst["worst_rise_c"] if worst else None,
               "worst_drop_mv": max((x["worst_drop_mv"] for x in rows.values()), default=None), "method": "resistor network, IPC-2221"}
    return Outcome({**_files(p, p.board), "power_config": _config(p, "power_config")}, metrics, findings)


@check("thermal_field", "verify")
def thermal_field(p: Project) -> Outcome:
    """Current and temperature solved on the real copper, pours included, for each rail in `power`. A failed solve can't pass."""
    cfg = p.section("thermal")
    rails = [{"net": r["net"], "source": r["source"], "loads": {k: float(v) for k, v in r["loads"].items()}}
             for r in p.section("power").get("rails", [])]
    if not rails:
        return Outcome({**_files(p, p.board), "power_config": _config(p, "power_config")}, {"rails": []},
                       [Finding("thermal_field:no_rails", "no rails are described, so there's no current to solve. Add them under power.rails")])
    filled = thermal.fill_copy(p.board, p.work / "thermal_filled.kicad_pcb")
    settings = {k: cfg[k] for k in ("dx_mm", "thermal_dx_mm", "sublayers", "h_w_m2k", "k_in_plane", "k_through", "ambient_c", "coupled") if k in cfg}
    problem = thermal.export_problem(filled, rails, cfg.get("parts"), settings)
    png = p.work / "thermal_field.png"
    o = thermal.run_field(problem, p.work / "thermal", cfg.get("backend", "cpu"), png)
    # The picture is a reading's companion, not the reading: a missing PNG leaves the measurement standing, without the plot.
    svgs = [thermal.heat_svg(png, o, problem["bbox"], filled, p.work / "thermal_field.svg", f"{p.name}: rail current only")] if png.exists() else []
    findings = []
    limit = float(cfg.get("max_rise_c", p.section("power").get("max_rise_c", 10)))
    hot = o["hotspot"]
    if hot["rise_c"] > limit:
        findings.append(Finding("thermal_field:heating", f"{hot['rise_c']:.1f} C rise on {hot['layer']} near "
                                f"({hot['x']:.1f}, {hot['y']:.1f}) mm, limit {limit} C"))
    if not o["converged"]:
        findings.append(Finding("thermal_field:not_converged", "the solve stopped before reaching its tolerance"))
    if abs(o["energy_error_pct"]) > 1.0:
        findings.append(Finding("thermal_field:energy", f"heat out differs from heat in by {o['energy_error_pct']:.2f}%"))
    drops = {}
    for r in o["rails"]:
        worst = max(r["drops_mv"].values(), default=0.0)
        drops[r["net"]] = round(worst, 3)
        if r["unreached"]:
            findings.append(Finding(f"thermal_field:{r['net']}:unreached", f"{r['net']}: copper doesn't reach {r['unreached']}"))
        if abs(r["balance_error_pct"]) > 1.0:
            findings.append(Finding(f"thermal_field:{r['net']}:balance", f"{r['net']}: power balance off by {r['balance_error_pct']:.2f}%"))
    for rail in p.section("power").get("rails", []):
        if rail.get("max_drop_mv") is not None and drops.get(rail["net"], 0) > float(rail["max_drop_mv"]):
            findings.append(Finding(f"thermal_field:{rail['net']}:drop", f"{rail['net']}: {drops[rail['net']]} mV drop exceeds {rail['max_drop_mv']} mV"))
    metrics = {"rise_c": round(hot["rise_c"], 3), "hotspot": hot, "drops_mv": drops, "backend": o["backend"], "device": o["device"],
               "grid": o["grid"], "seconds": round(o["timing"]["total_s"], 2), "energy_error_pct": round(o["energy_error_pct"], 4)}
    # The solve above is the copper's own heat, and the limit above is for traces. Parts get their own solve, and their own limit.
    supply = cfg.get("supply_v")
    if supply is None:
        findings.append(Finding("thermal_field:no_part_heat", "thermal.supply_v isn't set, so the parts' own heat isn't in this result, only the copper's", "warning"))
    else:
        heat = thermal.part_heat(problem["pads"], rails, float(supply), cfg.get("part_watts"))
        if heat:
            # A person's figure for the air wins. Otherwise it's worked out for this board, as the fixed figure above only suits the copper's own tiny heat.
            x1, y1, x2, y2 = problem["bbox"]
            air = thermal.still_air_h(sum(h["watts"] for h in heat.values()), (x2 - x1) * (y2 - y1) * 1e-6, float(cfg.get("ambient_c", 25.0)))
            part_h = float(cfg.get("part_h_w_m2k", air["h_w_m2k"]))
            heated = {**problem, "parts": [*(problem.get("parts") or []), *thermal.heated_parts(problem["pads"], heat)],
                      "settings": {**problem["settings"], "h_w_m2k": part_h}}
            warm = thermal.run_field(heated, p.work / "thermal_parts", cfg.get("backend", "cpu"), p.work / "thermal_parts.png")
            if (p.work / "thermal_parts.png").exists():
                svgs.append(thermal.heat_svg(p.work / "thermal_parts.png", warm, problem["bbox"], filled, p.work / "thermal_parts.svg",
                                             f"{p.name}: with part heat (upper estimate)"))
            top = warm["hotspot"]
            metrics.update(part_heat=heat, part_watts_total=round(sum(h["watts"] for h in heat.values()), 4), part_rise_c=round(top["rise_c"], 2), part_hotspot=top,
                           part_h_w_m2k=part_h, part_method="stated DC current times supply, all as heat, on the board under the part, in still air with no conduction out through connectors. Junction temperature is higher")
            if not warm["converged"]:
                findings.append(Finding("thermal_field:parts_not_converged", "the solve with part heat stopped before reaching its tolerance"))
            if abs(warm["energy_error_pct"]) > 1.0:
                findings.append(Finding("thermal_field:parts_energy", f"with part heat, heat out differs from heat in by {warm['energy_error_pct']:.2f}%"))
            where = f"{top['rise_c']:.1f} C rise under the parts, near ({top['x']:.1f}, {top['y']:.1f}) mm, from {metrics['part_watts_total']:.2f} W"
            part_limit = cfg.get("max_part_rise_c")
            if part_limit is None:
                findings.append(Finding("thermal_field:part_rise", f"{where}. No limit is set, so nothing is judging this. Set thermal.max_part_rise_c from your parts' ratings and enclosure", "warning"))
            elif top["rise_c"] > float(part_limit):
                findings.append(Finding("thermal_field:part_rise", f"{where}, limit {part_limit} C"))
    inputs = {**_files(p, p.board, p.project_file), "thermal_config": _config(p, "thermal_config")}
    return Outcome(inputs, metrics, findings, {"boardfield": o["device"]}, [str(x.relative_to(p.root)) for x in (png, *svgs) if x.exists()])


@check("em_fast", "route")
def em_fast(p: Project) -> Outcome:
    """A quick field-solver read for routing iterations: one solver, light mesh, refined where its error estimate asks.

    It's a trend sensor between edits, within about 2% here. em_crosscheck stays the release evidence.
    """
    cfg = p.section("em")
    fast = cfg.get("fast") or {"solver": "emerge", "quality": "Draft", "adaptive_mesh": 4}
    board = Board(p.board)
    inputs = {"em_fast_config": _config(p, "em_fast_config"), "stackup": board.stackup_digest()}
    ws = _em_workspace(p, cfg, board)
    label = "fast_" + _solver_label(fast)
    layout = "cells/COUPON/layout/COUPON.clay"
    s2p, seconds = _em_run(p, cfg, ws, label, layout, fast)
    zc, eff, il = _zc(s2p, float(cfg["length_um"]) / 1000, float(cfg.get("at_ghz", 3.0)) * 1e9)
    target, tol = float(cfg.get("target_ohm", 50)), float(cfg.get("tol_pct", 10))
    findings = []
    if abs(zc - target) > target * tol / 100:
        findings.append(Finding("em_fast:off_target", f"fast read {zc:.1f} ohm against {target} ohm. Confirm with em_crosscheck"))
    return Outcome(inputs, {"zc_ohm": round(zc, 2), "eps_eff": round(eff, 3), "seconds": seconds}, findings, {"solver": fast["solver"]})


@check("em_hops", "simulate", needs_board=True)
def em_hops(p: Project) -> Outcome:
    """Each RF hop as routed, solved as a 2-port with its own pours and vias: what the copper adds, without the parts."""
    from . import em_hops as hops
    from . import route_classes
    cfg = p.section("em_hops")
    board = Board(p.board)
    rf_cfg = p.section("rf")
    every = route_classes._load_chains(p.root / rf_cfg["chains"])[0]
    wanted = set(cfg.get("nets") or [c[0] for c in every])
    chains = [c for c in every if c[0] in wanted]
    solver = cfg.get("solver") or {"solver": "emerge", "quality": "Accurate"}
    label = _solver_label(solver)
    points = {"fmin_ghz": cfg["band_ghz"][0], "fmax_ghz": cfg["band_ghz"][1]}

    def run_solver(ws: Path, layout: str):
        return _em_run(p, points, ws, label, layout, solver)
    results = hops.run(p, cfg, board, chains, solver, label, run_solver)
    min_rl = float(cfg.get("min_rl_db", 20))
    warn_rl = float(cfg.get("warn_rl_db", 25))
    findings = []
    unsolved = {net: m for net, m in results.items() if "unsolved" in m}
    for net, m in unsolved.items():
        findings.append(Finding(f"em_hops:{net}:unsolved", f"{net}: the solver gave no result, so this hop is unchecked: {m['unsolved']}"))
    results = {net: m for net, m in results.items() if net not in unsolved}
    if not results:
        raise RuntimeError(f"no hop solved. First: {next(iter(unsolved.values()))['unsolved'] if unsolved else 'no RF hops found'}")
    for net, m in results.items():
        # All of the signal comes back and none gets through: the copper in the model isn't joined up, whatever the board does.
        if m["rl_db"] < 1.0 and m["il_max_db"] > 20.0:
            findings.append(Finding(f"em_hops:{net}:open", f"{net}: reads as an open circuit ({m['rl_db']} dB return loss, {m['il_max_db']} dB loss), so this reading is about the model and not the hop"))
        if m.get("port_narrowed"):
            findings.append(Finding(f"em_hops:{net}:port", f"{net}: the solver narrowed a port on a tilted track, so this reading can't be trusted"))
        if m["rl_db"] < min_rl:
            findings.append(Finding(f"em_hops:{net}:return_loss", f"{net}: return loss {m['rl_db']} dB in band, under {min_rl:g} dB"))
        elif m["rl_db"] < warn_rl:
            findings.append(Finding(f"em_hops:{net}:return_loss", f"{net}: return loss {m['rl_db']} dB in band", "warning"))
    rls = [m["rl_db"] for m in results.values()]
    channels = [g for g in ([n for n in group if n in results] for group in series_groups(every)) if g]
    metrics = {"hops": results, "channels": channels, "unsolved": sorted(unsolved), "unsolved_why": unsolved, "worst_rl_db": min(rls),
               "total_il_max_db": round(max(sum(results[n]["il_max_db"] for n in g) for g in channels), 3)}
    return Outcome({**_files(p, p.board), **_chain_files(p), "em_hops_config": _config(p, "em_hops_config"), "rf_config": _config(p, "rf_config")}, metrics, findings, {"solver": solver["solver"]})


def _rule_profile(p: Project) -> tuple[Path, dict]:
    cfg = p.section("rules")
    pack = Path(cfg.get("pack") or toolchain.paths()["rule_pack"]).expanduser()
    return pack, yaml.safe_load((pack / cfg.get("profile", "manufacture/summit_standard.yaml")).read_text())


@check("rules", "verify")
def rules(p: Project) -> Outcome:
    """Fabricator limits and RF routing rules from the rule pack, applied to the actual copper."""
    pack, std = _rule_profile(p)
    cap, eng = std["published_capabilities"], std["engineering_defaults"]
    board = Board(p.board)
    segs = [t for t in board.tracks if t.kind != "via"]
    vias = [t for t in board.tracks if t.kind == "via"]
    findings = []
    min_width = min((t.width for t in segs), default=None)
    if min_width is not None and min_width < eng["min_trace_mm"] - 1e-6:
        findings.append(Finding("rules:min_trace", f"narrowest track {min_width} mm is under {eng['min_trace_mm']} mm"))
    drills = [v.drill for v in vias]
    if drills:
        hard = cap["mechanical_drill"]["minimum_in"] * 25.4
        pref = cap["mechanical_drill"]["preferred_design_mm"]
        if min(drills) < hard - 1e-6:
            findings.append(Finding("rules:min_drill", f"via drill {min(drills)} mm is under the {hard:.3f} mm process limit"))
        elif min(drills) < pref - 1e-6:
            n = sum(d < pref - 1e-6 for d in drills)
            findings.append(Finding("rules:preferred_drill",
                                    f"{n} via(s) at {min(drills)} mm drill, under the preferred {pref} mm", "warning"))
        ring = min((v.width - v.drill) / 2 for v in vias)
        need = cap["annular_ring"]["class_2_min_in"] * 25.4
        if ring < need - 1e-6:
            findings.append(Finding("rules:annular_ring", f"annular ring {ring:.3f} mm is under {need:.3f} mm (class 2)"))
    else:
        ring = None
    if p.section("rf").get("chains"):
        # A bend only matters when the track is wide against a wavelength. Known-good boards bend wide lines at low frequency and work.
        f_ghz = float((p.section("rf").get("impedance") or {}).get("max_freq_ghz", 0) or 0)
        rf_nets = _chain_nets(p)
        widest = max((t.width for t in segs if t.net in set(rf_nets)), default=0.0)
        small = f_ghz > 0 and widest < 299.8 / (f_ghz * 4.5 ** 0.5) / 20
        for where in board.right_angles(rf_nets):
            findings.append(Finding("rules:rf_right_angle", f"90 degree or sharper bend on RF: {where}" + (f". The {widest} mm track is under a twentieth of a wavelength at {f_ghz:g} GHz, so this is minor" if small else ""),
                                    "warning" if small else "error"))
    floating = sorted(x["name"] for x in board.all_pads() if not x["net"] and not x["name"].endswith("."))
    if floating:
        findings.append(Finding("rules:pad_on_no_net",
                                f"{len(floating)} numbered pad(s) on no net, so their pins may be missing from the schematic. "
                                f"Check them against the datasheet: {', '.join(floating[:12])}{' and more' if len(floating) > 12 else ''}", "warning"))
    metrics = {"pads_on_no_net": floating, "min_track_mm": min_width, "min_drill_mm": min(drills) if drills else None,
               "min_annular_mm": round(ring, 4) if ring is not None else None, "profile": std["manufacturer"]["name"]}
    return Outcome({**_files(p, p.board), **_chain_files(p), "rule_profile": _config(p, "rule_profile"), "rf_config": _config(p, "rf_config")}, metrics, findings, {"rule pack": str(pack.name)})


# ---------------------------------------------------------------- simulate

def _zc(s2p: Path, length_mm: float, f_hz: float) -> tuple[float, float, float]:
    """Characteristic impedance, effective permittivity and insertion loss of a uniform line."""
    n = skrf.Network(str(s2p))
    i = int(np.argmin(abs(n.f - f_hz)))
    s11, s21 = n.s[i, 0, 0], n.s[i, 1, 0]
    z0 = np.real(n.z0[i, 0])
    zc = z0 * np.sqrt(((1 + s11) ** 2 - s21 ** 2) / ((1 - s11) ** 2 - s21 ** 2))
    zc = zc if zc.real > 0 else -zc
    gl = np.arccosh((1 - s11 ** 2 + s21 ** 2) / (2 * s21))
    beta = abs(gl.imag) / (length_mm * 1e-3)
    return (float(abs(zc)), float((beta * 299792458.0 / (2 * np.pi * n.f[i])) ** 2),
            float(-20 * np.log10(abs(s21))))


def _coupon(p: Project, cfg: dict, ws: Path) -> str:
    """Write a straight line coupon with the board's own stackup and cross-section."""
    tp = toolchain.paths()
    cells = p.work / "em_cells"
    shutil.rmtree(cells, ignore_errors=True)
    toolchain.run([tp["circuitrf"], "convert", p.board, "-o", cells, "--to", "clay"],
                  env=toolchain.circuitrf_env(), timeout=600)
    tech_src = toolchain.glob_one(str(cells / "**" / "*.ctech")) or toolchain.glob_one(str(cells / "*.ctech"))
    if not tech_src:
        import glob
        hits = glob.glob(str(cells / "**" / "*.ctech"), recursive=True)
        tech_src = hits[0] if hits else None
    if not tech_src:
        raise RuntimeError("the circuitRF import produced no technology file")
    tech = json.loads(Path(tech_src).read_text())
    signal, ground = cfg["signal_layer"], cfg["ground_layer"]
    key = {}
    for lyr in tech["Stackup"]["Layers"]:
        if lyr.get("Kind") == "Conductor" and lyr["Name"] in (signal, ground):
            key[lyr["Name"]] = lyr["DrawingLayers"][0]["Layer"]
        lyr["IsGroundReference"] = lyr["Name"] == ground     # the import never marks it
    if len(key) != 2:
        raise RuntimeError(f"layers {signal} and {ground} are not both in the stackup")
    (ws / "cells" / "COUPON" / "layout").mkdir(parents=True)
    (ws / "em").mkdir()
    (ws / ".cws").write_text(json.dumps({"FormatVersion": 2, "LibraryRefs": [], "KnownFiles": []}))
    (ws / "cells" / "tech.ctech").write_text(json.dumps(tech))
    (ws / "cells" / "COUPON" / ".ccell").write_text(json.dumps(
        {"FormatVersion": 1, "Parameters": [], "PrimaryLayout": "COUPON.clay", "IsTestBench": False,
         "Terminals": [], "NumPorts": 0}))
    w, gap, side, length = (float(cfg[k]) for k in ("width_um", "gap_um", "side_um", "length_um"))
    um = 1000
    h = w / 2
    lsig, lgnd = key[signal], key[ground]

    def rect(layer, x0, y0, x1, y1, net):
        pts = [x0, y0, x1, y0, x1, y1, x0, y1]
        return {"$type": "Poly", "Xy": [int(v * um) for v in pts],
                "Layer": {"Layer": layer, "Datatype": 0}, "Net": net}
    shapes = [
        {"$type": "Path", "Xy": [0, 0, 0, int(length * um)], "Width": int(w * um), "End": "Flush",
         "Layer": {"Layer": lsig, "Datatype": 0}, "Net": "SIG"},
        rect(lsig, -(h + gap + side), 0, -(h + gap), length, "GND"),
        rect(lsig, h + gap, 0, h + gap + side, length, "GND"),
        rect(lgnd, -(h + gap + side), 0, h + gap + side, length, "GND"),
        {"$type": "Label", "Layer": {"Layer": lsig, "Datatype": 0}, "X": 0, "Y": int(length * um), "Text": "1",
         "Height": 400000, "IsPort": True, "PortDirection": "R270"},
        {"$type": "Label", "Layer": {"Layer": lsig, "Datatype": 0}, "X": 0, "Y": 0, "Text": "2",
         "Height": 400000, "IsPort": True, "PortDirection": "R90"},
    ]
    (ws / "cells" / "COUPON" / "layout" / "COUPON.clay").write_text(json.dumps(
        {"FormatVersion": 1, "DbuPerMicron": 1000, "DisplayUnit": "Um", "SnapDbu": 0, "AngleMode": "AnyAngle",
         "TechRef": "../../tech.ctech", "Shapes": shapes, "Instances": []}))
    return "cells/COUPON/layout/COUPON.clay"


def _em_setup(name: str, layout: str, solver: dict, fmin: float, fmax: float) -> dict:
    kind = solver["solver"]
    cem = {"FormatVersion": 1, "Name": name, "LayoutRef": layout,
           "Frequency": {"StartExpr": str(fmin), "StopExpr": str(fmax), "StepExpr": "", "NumPoints": 21,
                         "Mode": "PointCount", "Kind": "Linear", "StartUnit": "GHz", "StopUnit": "GHz",
                         "StepUnit": "Hz"},
           "Solver3D": {"openems": "OpenEms", "palace": "Palace", "emerge": "Emerge"}[kind],
           "AirBox": {k: {"PaddingUm": 1500} for k in ("XMin", "XMax", "YMin", "YMax", "ZMin", "ZMax")}}
    if kind == "palace":
        cem["Palace"] = {"Quality": solver.get("quality", "Standard")}
    elif kind == "emerge":
        cem["EMerge"] = {"Quality": solver.get("quality", "Standard")}
        if solver.get("adaptive_mesh"):
            cem["EMerge"]["AdaptiveMesh"] = solver["adaptive_mesh"]
    else:
        cem["OpenEms"] = {"CellsPerWavelength": solver.get("cells_per_wavelength", 40), "MinCellUm": solver.get("min_cell_um", 8)}
    return cem


def _solver_label(s: dict) -> str:
    label = f"{s['solver']}_{s.get('quality', s.get('cells_per_wavelength', 40))}"
    return f"{label}_amr{s['adaptive_mesh']}" if s.get("adaptive_mesh") else label


_GEOMETRY_KEYS = ("signal_layer", "ground_layer", "width_um", "gap_um", "side_um", "length_um", "fmin_ghz", "fmax_ghz")


def _em_workspace(p: Project, cfg: dict, board: Board) -> Path:
    """The workspace is keyed on geometry alone, so adding or refining one solver reruns only that one."""
    geometry = {k: cfg.get(k) for k in _GEOMETRY_KEYS}
    ws = p.work / "em" / _digest([geometry, board.stackup_digest()])[:12]
    if not (ws / ".cws").exists():
        shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True)
        _coupon(p, cfg, ws)
    return ws


def _em_run(p: Project, cfg: dict, ws: Path, label: str, layout: str, s: dict) -> tuple[Path, float | None]:
    """One solver setup. A finished result is reused, since it depends only on the setup."""
    tp = toolchain.paths()
    cem = ws / "em" / f"{label}.cem"
    if not cem.exists():
        cem.write_text(json.dumps(_em_setup(label, layout, s, cfg.get("fmin_ghz", 1), cfg.get("fmax_ghz", 5)), indent=1))

    def result():
        return next(iter(sorted((ws / "results").glob(f"{label}.*.s2p"))), None) if (ws / "results").exists() else None
    s2p = result()
    if s2p is not None:
        return s2p, None
    t0 = time.time()
    r = toolchain.run([tp["circuitrf"], "em", cem], cwd=ws, env=toolchain.circuitrf_env(), timeout=int(cfg.get("timeout_s", 7200)))
    (p.work / f"em_{label}.log").write_text(r.stdout + r.stderr)
    # Beside the result as well, since several hops can run at once and the shared log only holds the last.
    (ws / f"{label}.log").write_text(r.stdout + r.stderr)
    s2p = result()
    if s2p is None:
        raise RuntimeError(f"{label}: solver produced no result: {(r.stdout + r.stderr).strip()[-800:]}")
    return s2p, round(time.time() - t0, 1)


REFERENCE_AGREES_PCT = 2.0


@check("em_crosscheck", "simulate")
def em_crosscheck(p: Project) -> Outcome:
    """Independent field solvers on a coupon of the board's own line, with disagreement diagnosed. One solver's number is not evidence."""
    cfg = p.section("em")
    solvers = cfg["solvers"]
    for s in solvers:
        if str(s.get("quality", "")).lower() == "draft" and not s.get("probe") and not s.get("adaptive_mesh"):
            raise RuntimeError("Palace Draft read 36.2 ohm on a 49 ohm line with no warning; it is refused here "
                               "unless marked probe: true, which keeps it out of any passing result")
    if len({s["solver"] for s in solvers}) < 2:
        raise RuntimeError("em_crosscheck needs at least two different solvers")
    board = Board(p.board)
    nets = _chain_nets(p)
    widths = sorted({t.width for t in board.tracks if t.net in nets and t.kind == "segment"})
    w_mm = float(cfg["width_um"]) / 1000
    findings = []
    if widths and not any(abs(x - w_mm) < 1e-6 for x in widths):
        findings.append(Finding("em_crosscheck:coupon_width",
                                f"coupon is {w_mm} mm but the RF nets are {widths} mm wide"))
    inputs = {"em_config": _config(p, "em_config"), "stackup": board.stackup_digest(), **_chain_files(p)}
    ws = _em_workspace(p, cfg, board)
    layout = "cells/COUPON/layout/COUPON.clay"
    length_mm = float(cfg["length_um"]) / 1000
    f_hz = float(cfg.get("at_ghz", 3.0)) * 1e9
    results, tools = {}, {}
    for s in solvers:
        label = _solver_label(s)
        s2p, seconds = _em_run(p, cfg, ws, label, layout, s)
        zc, eff, il = _zc(s2p, length_mm, f_hz)
        results[label] = {"solver": s["solver"], "zc_ohm": round(zc, 2), "eps_eff": round(eff, 3),
                          "il_db": round(il, 3), "seconds": seconds}
        tools[label] = s["solver"]
    zs = [v["zc_ohm"] for v in results.values()]
    spread = 100 * (max(zs) - min(zs)) / np.mean(zs)
    max_spread = float(cfg.get("max_spread_pct", 2.0))
    target = float(cfg.get("target_ohm", 50))
    tol = float(cfg.get("tol_pct", 10))
    d = diagnose(results, target, tol, max_spread)
    if d["verdict"] == "outlier":
        for a in d["actions"]:
            findings.append(Finding(f"em_crosscheck:{a['solver']}:outlier",
                                    f"{a['solver']} disagrees (Zc {a['zc_err_pct']:+.1f}%, eps_eff {a['eps_err_pct']:+.1f}%): "
                                    f"{a['cause']} ({a['confidence']}). Try: {a['try']}"))
    elif d["verdict"] == "no_majority":
        findings.append(Finding("em_crosscheck:no_majority", f"no agreeing majority: {zs} ohm. {d['actions'][0]['try']}"))
    elif d["verdict"] == "design_off_target":
        findings.append(Finding("em_crosscheck:off_target", f"{d['actions'][0]['why']}. {d['actions'][0]['try']}"))
    if d["loss_note"]:
        findings.append(Finding("em_crosscheck:loss", d["loss_note"], "warning"))
    reference = _em_reference(board, cfg)
    for row in results.values():
        row["vs_reference_pct"] = round(100 * (row["zc_ohm"] - reference["z0_ohm"]) / reference["z0_ohm"], 1)
    backed = [k for k in (d["agreeing"] or []) if abs(results[k]["vs_reference_pct"]) <= REFERENCE_AGREES_PCT]
    if d["agreeing"] and not backed:
        findings.append(Finding("em_crosscheck:reference", f"the agreeing solvers read {d['zc_trusted_ohm']} ohm, and the 2D reference reads "
                                f"{reference['z0_ohm']} ohm. Check the coupon against the board's stackup", "warning"))
    for finding in findings:
        if finding.id.endswith(":outlier"):
            finding.message = finding.message.rstrip(".") + f". The 2D reference reads {reference['z0_ohm']} ohm, which backs {', '.join(backed) or 'neither side'}."
    metrics = {"solvers": results, "spread_pct": round(float(spread), 2), "zc_ohm": d["zc_trusted_ohm"],
               "eps_eff": round(float(np.mean([results[k]["eps_eff"] for k in (d["agreeing"] or results)])), 3),
               "at_ghz": f_hz / 1e9, "diagnosis": d, "reference": {**reference, "backs": backed}}
    return Outcome(inputs, metrics, findings, tools)


def _em_reference(board: Board, cfg: dict) -> dict:
    """The coupon's cross-section, solved in 2D from the same stackup. See em_reference.py for what it leaves out."""
    from .em_reference import coplanar_z0
    from .thermal import _stack
    layers, gaps = _stack(board)
    names = [x["name"] for x in layers]
    i, j = sorted((names.index(cfg["signal_layer"]), names.index(cfg["ground_layer"])))
    height_um = 1000 * sum(gaps[i:j]) + sum(x["thickness_um"] for x in layers[i + 1:j])
    return coplanar_z0(float(cfg["width_um"]), float(cfg["gap_um"]), layers[names.index(cfg["signal_layer"])]["thickness_um"],
                       height_um, _dielectric_er(board), float(cfg.get("side_um", 3000)))


def _dielectric_er(board: Board) -> float:
    from .pcbfile import _child, _val
    stackup = _child(_child(board.tree, "setup"), "stackup")
    found = [float(_val(x, "epsilon_r")) for x in stackup if isinstance(x, list) and x and x[0] == "layer" and _val(x, "epsilon_r")]
    return found[0] if found else 4.5


def _interp_logf(table, f):
    pts = sorted((float(a), float(b)) for a, b in table)
    if f <= pts[0][0]:
        return pts[0][1]
    if f >= pts[-1][0]:
        return pts[-1][1]
    for (f0, v0), (f1, v1) in zip(pts, pts[1:]):
        if f0 <= f <= f1:
            return v0 + np.log(f / f0) / np.log(f1 / f0) * (v1 - v0)
    return pts[-1][1]


def friis(stages: list[dict], f_mhz: float) -> dict:
    """Noise figure, gain and output IP3 down a chain, per stage values interpolated in log f.

    OIP3 is None if any active stage doesn't give one: leaving a stage out would report a chain better than it is.
    """
    f_total, g_total, oip3_inv = 1.0, 1.0, 0.0
    oip3_known = True
    for st in stages:
        if "loss_db" in st:
            gain = -_interp_logf(st["loss_db"], f_mhz)
            nf = -gain
            oip3 = st.get("oip3_dbm", 60.0)
        else:
            gain = _interp_logf(st["gain_db"], f_mhz)
            nf = _interp_logf(st["nf_db"], f_mhz)
            oip3_known = oip3_known and "oip3_dbm" in st
            oip3 = _interp_logf(st["oip3_dbm"], f_mhz) if "oip3_dbm" in st else 60.0
        g = 10 ** (gain / 10)
        f_total += (10 ** (nf / 10) - 1) / g_total
        oip3_inv = oip3_inv / g + 1 / (10 ** (oip3 / 10))
        g_total *= g
    return {"gain_db": round(10 * np.log10(g_total), 2), "nf_db": round(10 * np.log10(f_total), 2),
            "oip3_dbm": round(float(-10 * np.log10(oip3_inv)), 2) if oip3_known else None}


def series_groups(chains: list) -> list[list[str]]:
    """Nets in series, one list per RF path. A signal only takes one path, so losses add within a list and not across them."""
    groups: list[list[str]] = []
    leave = None
    for net, entry, out in chains:
        if leave is None or entry[0] != leave:
            groups.append([])
        groups[-1].append(net)
        leave = out[0]
    return groups


def _hop_loss_table(hop_ev) -> list:
    """Loss of the solved hops against frequency in MHz, along the worst path at each frequency."""
    hops = hop_ev.metrics["hops"]
    groups = hop_ev.metrics.get("channels") or [list(hops)]
    freqs = sorted({float(f) for m in hops.values() for f in m["il_by_ghz"]})
    return [[f * 1000, round(max(sum(_nearest_loss(hops[n]["il_by_ghz"], f) for n in g) for g in groups), 4)] for f in freqs]


def _nearest_loss(by_ghz: dict, f: float) -> float:
    key = min(by_ghz, key=lambda k: abs(float(k) - f))
    return by_ghz[key]


@check("cascade", "simulate", needs_board=True)
def cascade(p: Project) -> Outcome:
    """Friis cascade over the band from the stage table, with the measured RF copper added as a stage."""
    cfg = p.section("cascade")
    stages = list(cfg.get("stages") or [])
    if not stages or not cfg.get("freq_mhz"):
        # No table, nothing cascaded. That's a gap to fill, never a clean chain.
        missing = " and ".join(x for x, ok in (("stages", stages), ("freq_mhz", cfg.get("freq_mhz"))) if not ok)
        return Outcome({"stages": _config(p, "stages")}, {}, [Finding("cascade:no_stages",
                       f"cascade has no {missing} in edaloop.yaml, so there is nothing to cascade. Add the stage table from the parts' datasheets")])
    rf = p.ledger.latest().get("rf_path")
    routed = None
    db_per_mm = cfg.get("line_loss_db_per_mm")
    inputs = {"stages": _config(p, "stages")}
    hop_ev = p.ledger.latest().get("em_hops")
    measured = None
    if db_per_mm and hop_ev and hop_ev.status in ("pass", "warn") and current_inputs(p, hop_ev):
        # Solved copper replaces the per-millimetre estimate. Pads and necks are in it, and the parts aren't.
        table = _hop_loss_table(hop_ev)
        stages.insert(0, {"name": "RF copper, solved", "loss_db": table})
        measured = table
        inputs["em_hops_table"] = _config(p, "em_hops_table")
    elif db_per_mm and rf and rf.metrics.get("routed_mm"):
        routed = rf.metrics["routed_mm"]
        stages.insert(0, {"name": "RF copper", "loss_db": [[1, routed * float(db_per_mm)]]})
        inputs.update({k: v for k, v in rf.inputs.items() if k.endswith(".kicad_pcb")})
    per_f = {str(f): friis(stages, float(f)) for f in cfg["freq_mhz"]}
    worst = {"gain_min_db": min(v["gain_db"] for v in per_f.values()),
             "gain_max_db": max(v["gain_db"] for v in per_f.values()),
             "nf_max_db": max(v["nf_db"] for v in per_f.values()),
             "oip3_min_dbm": None if any(v["oip3_dbm"] is None for v in per_f.values()) else min(v["oip3_dbm"] for v in per_f.values())}
    unknown = [st.get("name", "?") for st in stages if "loss_db" not in st and "oip3_dbm" not in st]
    findings = [Finding("cascade:oip3_unknown", f"chain OIP3 isn't computed: no OIP3 given for {', '.join(unknown)}", "warning")] if unknown else []
    return Outcome(inputs, {**worst, "by_freq": per_f, "copper_mm": routed, "copper_source": "solved hops" if measured else
                            ("per-mm estimate" if routed else "stage table only")}, findings)


@check("spice", "simulate")
def spice_benches(p: Project) -> Outcome:
    """DC, transient and noise benches on the schematic's own netlist. A part with no model fails the bench."""
    cfg = p.section("spice")
    if not p.schematic:
        raise RuntimeError("the spice check needs a schematic")
    parts = spice.read_netlist(p.schematic, p.work / "spice_netlist.xml")
    extra = p.root / cfg["registry"] if cfg.get("registry") else None
    registry = spice.load_registry(extra)
    models = {x.ref: spice.resolve(x, cfg.get("models") or {}, registry) for x in parts}
    findings = [Finding(f"spice:model_missing:{ref}", f"{ref} has no SPICE model and no stated exclusion")
                for ref, m in models.items() if m["kind"] == "missing"]
    metrics: dict = {"models": {ref: {"kind": m["kind"], "provenance": m.get("provenance"), "note": m.get("note", "")}
                                for ref, m in models.items()}}
    artifacts = []
    if not findings:
        for bench in cfg["benches"]:
            name = bench["name"]
            deck = spice.build_deck(f"{p.name} {name}", parts, models, bench)
            run = spice.run_deck(deck, p.work / "spice", name)
            metrics[name] = run["values"]
            artifacts.append(str(Path(run["log"]).relative_to(p.root)))
            for measure in run["failed"]:
                findings.append(Finding(f"spice:{name}:{measure}:failed", f"{name}: the measurement {measure} did not resolve"))
    files = [*p.schematic.parent.glob("*.kicad_sch")]
    # Worked out from the models already resolved, so the netlist isn't exported twice. _spice_digest repeats it.
    return Outcome({**_files(p, *files), "spice_config": _spice_models_digest(cfg, models.values())},
                   metrics, findings, {"ngspice": "47"}, artifacts)


# ---------------------------------------------------------------- evaluate

def _lookup(metrics: dict, dotted: str):
    v = metrics
    for part in dotted.split("."):
        if not isinstance(v, dict) or part not in v:
            return None
        v = v[part]
    return v


def _spice_digest(p: Project) -> str:
    cfg = p.section("spice")
    extra = p.root / cfg["registry"] if cfg.get("registry") else None
    parts = spice.read_netlist(p.schematic, p.work / "spice_netlist.xml")
    reg = spice.load_registry(extra)
    return _spice_models_digest(cfg, [spice.resolve(x, cfg.get("models") or {}, reg) for x in parts])


def _spice_models_digest(cfg: dict, models) -> str:
    files = sorted({m["file"] for m in models if m.get("file")})
    return _digest([cfg, [sha_file(Path(f)) for f in files]])


def _hop_table_digest(p: Project) -> str | None:
    hop = p.ledger.latest().get("em_hops")
    return _digest(_hop_loss_table(hop)) if hop else None


def _requirement_checks(p: Project) -> set[str]:
    return {req["metric"].split(".", 1)[0] for req in p.cfg.get("requirements") or []} - {"requirements"}


def _requirements_digest(p: Project) -> str:
    latest = p.ledger.latest()
    # The evidence's own content, not only its time: two runs can land in the same second.
    read = {name: [latest[name].when, latest[name].status, latest[name].metrics] for name in _requirement_checks(p) if name in latest}
    return _digest([p.cfg.get("requirements"), read])


# Every input that isn't a file, and how to work out its digest again now. A check records a key through
# _config, and current_inputs recomputes it from here, so the two can't drift apart. A key missing from
# this table makes evidence stale, and a key that can no longer be worked out does too.
CONFIG_DIGESTS = {
    "drc_config": lambda p: _digest(p.section("drc")),
    "rf_config": lambda p: _digest(p.section("rf")),
    "route_config": lambda p: _digest(p.section("route")),
    "rf_layout_config": lambda p: _digest(p.section("rf_layout")),
    "power_config": lambda p: _digest(p.section("power")),
    "thermal_config": lambda p: _digest([p.section("thermal"), p.section("power")]),
    "em_config": lambda p: _digest(p.section("em")),
    "em_fast_config": lambda p: _digest([{k: p.section("em").get(k) for k in _GEOMETRY_KEYS},
                                         p.section("em").get("fast") or {"solver": "emerge", "quality": "Draft", "adaptive_mesh": 4}]),
    "em_hops_config": lambda p: _digest([p.section("em_hops"), p.section("em_hops").get("solver") or {"solver": "emerge", "quality": "Accurate"}]),
    "em_hops_table": _hop_table_digest,
    "stackup": lambda p: Board(p.board).stackup_digest(),
    "stages": lambda p: _digest(p.section("cascade")),
    "rule_profile": lambda p: _digest(_rule_profile(p)[1]),
    "spice_config": _spice_digest,
    "requirements": _requirements_digest,
}


def _config(p: Project, key: str) -> str:
    return CONFIG_DIGESTS[key](p)


def current_inputs(p: Project, ev: Evidence) -> bool:
    """True while every input the evidence read still has the same content."""
    for key, digest in ev.inputs.items():
        if key in CONFIG_DIGESTS:
            try:
                if _config(p, key) != digest:
                    return False
            except Exception:  # noqa: BLE001 - an input that can't be read now can't vouch for the old result
                return False
            # A requirement's verdict is only as fresh as the evidence it was read from.
            if key == "requirements":
                latest = p.ledger.latest()
                if any(name in latest and not current_inputs(p, latest[name]) for name in _requirement_checks(p)):
                    return False
            continue
        path = p.root / key
        if path.exists():
            if sha_file(path) != digest:
                return False
        elif digest != ABSENT:
            return False
    return True


@check("requirements", "evaluate")
def requirements(p: Project) -> Outcome:
    """Every stated requirement traced to one metric on fresh evidence. Stale evidence fails the requirement."""
    latest = p.ledger.latest()
    findings, rows = [], []
    for req in p.cfg.get("requirements") or []:
        check_name, metric = req["metric"].split(".", 1)
        ev = latest.get(check_name)
        row = {"id": req["id"], "text": req.get("text", ""), "metric": req["metric"],
               "min": req.get("min"), "max": req.get("max"), "value": None, "state": "no evidence"}
        rows.append(row)
        if ev is None:
            findings.append(Finding(f"requirements:{req['id']}", f"{req['id']}: {check_name} has not run"))
            continue
        if not current_inputs(p, ev):
            row["state"] = "stale"
            findings.append(Finding(f"requirements:{req['id']}", f"{req['id']}: {check_name} evidence is stale"))
            continue
        value = _lookup(ev.metrics, metric)
        row["value"] = value
        if value is None:
            findings.append(Finding(f"requirements:{req['id']}", f"{req['id']}: {req['metric']} was not measured"))
            row["state"] = "not measured"
            continue
        low = req.get("min")
        high = req.get("max")
        ok = (low is None or value >= low) and (high is None or value <= high)
        row["state"] = "met" if ok else "missed"
        if not ok:
            findings.append(Finding(f"requirements:{req['id']}",
                                    f"{req['id']}: {req['metric']} = {value}, limit [{low}, {high}]"))
    inputs = {"requirements": _config(p, "requirements"), **_files(p, p.cfg_path)}
    return Outcome(inputs, {"rows": rows, "met": sum(r["state"] == "met" for r in rows), "total": len(rows)}, findings)


# ---------------------------------------------------------------- release

def withdrawn_waivers(p: Project, ev: Evidence) -> list[dict]:
    """Findings this evidence counted as waived, whose waiver is no longer in the project. The result can't stand without it."""
    waivers = p.waivers()
    return [f for f in ev.findings if f.get("waived_by") and not any(fnmatch.fnmatch(f["id"], w.match) for w in waivers)]


def gate_state(p: Project) -> list[dict]:
    """The state of every check the project's gate requires, with staleness resolved now."""
    latest = p.ledger.latest()
    if not p.gate():
        # Nothing gated is not the same as everything passing.
        return [{"check": "gate", "stage": "release", "state": "empty", "when": "",
                 "summary": "edaloop.yaml gates no checks, so no evidence stands behind a release. A person decides what the gate holds"}]
    rows = []
    for name in p.gate():
        ev = latest.get(name)
        if name not in REGISTRY:
            rows.append({"check": name, "stage": "?", "state": "unknown", "when": "",
                         "summary": f"{name} is gated in edaloop.yaml but no check has that name. See list_checks"})
            continue
        if ev is None:
            state = "missing"
        elif not current_inputs(p, ev) or withdrawn_waivers(p, ev):
            state = "stale"
        else:
            state = ev.status
        summary = ev.summary if ev else ""
        if ev and withdrawn_waivers(p, ev):
            summary = f"{summary}. It relied on a waiver that is no longer in edaloop.yaml"
        rows.append({"check": name, "stage": REGISTRY[name]["stage"], "state": state, "summary": summary, "when": ev.when if ev else ""})
    return rows


def fab_notes(p: Project) -> str:
    """The callout a fabricator needs to hold 50 ohm. A width only means something on the stackup it was solved for."""
    bench = {}
    described = p.root / "board_description.yaml"
    if described.exists():
        bench = yaml.safe_load(described.read_text())
    stack = bench.get("stackup") or {}
    copper = Board(p.board).copper_thickness_um()
    reference = (p.section("route").get("reference_layers") or ["In1.Cu"])[0]
    lines = [f"Fabrication notes for {p.name}", "", "Copper, um: " + ", ".join(f"{k} {v:g}" for k, v in copper.items())]
    if stack:
        lines.append("Dielectric, mm, top to bottom: " + ", ".join(f"{x:g}" for x in stack.get("dielectric_mm", [])))
        lines.append(f"Relative permittivity: {stack.get('epsilon_r')}, loss tangent {stack.get('loss_tangent')}")
    if bench.get("rf_line_width_mm"):
        gap = (bench.get("layout") or {}).get("pour_clearance_mm", 0.3)
        lines.append(f"Controlled impedance: 50 ohm CPWG on F.Cu, {bench['rf_line_width_mm']} mm trace, {gap} mm gap, referenced to {reference}.")
    lines += ["Confirm this stackup before fabrication, or give the fabricator's own, so that the width can be solved again.", ""]
    return "\n".join(lines)


def review_pack(p: Project) -> dict:
    """Everything a person needs to review the design, whatever the gate says. It holds no Gerbers, so it can't be sent to a fab by mistake."""
    cli = toolchain.paths()["kicad_cli"]
    out = p.root / "review"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    steps = [[cli, "pcb", "render", "--quality", "basic", "--width", "1800", "--height", "1800", "--side", "top", "-o", out / "board_top.png", p.board],
             [cli, "pcb", "render", "--quality", "basic", "--width", "1800", "--height", "1800", "--side", "bottom", "-o", out / "board_bottom.png", p.board],
             [cli, "pcb", "export", "pdf", "--layers", "F.Cu,F.SilkS,Edge.Cuts", "--mode-single", "-o", out / "board_front.pdf", p.board],
             [cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm", "-o", out / "positions.csv", p.board]]
    if p.schematic:
        steps += [[cli, "sch", "export", "pdf", "-o", out / "schematic.pdf", p.schematic],
                  [cli, "sch", "export", "bom", "-o", out / "bom.csv", p.schematic]]
    failed = []
    for cmd in steps:
        r = toolchain.run(cmd, timeout=600)
        if r.returncode != 0:
            failed.append(f"{cmd[1]} {cmd[2]}: {r.stderr.strip()[-200:]}")
    (out / "fab_notes.txt").write_text(fab_notes(p))
    rows = gate_state(p)
    blockers = [{"check": r["check"], "state": r["state"], "summary": r["summary"]} for r in rows if r["state"] not in ("pass", "warn")]
    warnings = [{"check": r["check"], "summary": r["summary"]} for r in rows if r["state"] == "warn"]
    (out / "status.json").write_text(json.dumps({"name": p.name, "board_sha256": sha_file(p.board), "ready_to_release": not blockers,
                                                 "blockers": blockers, "warnings": warnings, "export_failures": failed}, indent=1))
    return {"folder": str(out.relative_to(p.root)), "files": sorted(x.name for x in out.iterdir()), "ready_to_release": not blockers,
            "blockers": [b["check"] for b in blockers], "export_failures": failed}


def release(p: Project) -> dict:
    """Export fabrication files, only if every gated check holds on these exact bytes."""
    rows = gate_state(p)
    blockers = [r for r in rows if r["state"] not in ("pass", "warn")]
    if blockers:
        return {"released": False, "blockers": blockers}
    # The gate held on these bytes. Exports read the board again, so check that it is still them afterwards.
    board_sha = sha_file(p.board)
    cli = toolchain.paths()["kicad_cli"]
    out = p.root / "release"
    shutil.rmtree(out, ignore_errors=True)
    (out / "gerbers").mkdir(parents=True)
    steps = [
        [cli, "pcb", "export", "gerbers", "-o", f"{out}/gerbers/", p.board],
        [cli, "pcb", "export", "drill", "-o", f"{out}/gerbers/", p.board],
        [cli, "pcb", "export", "pos", "--format", "csv", "--units", "mm", "-o", out / "positions.csv", p.board],
    ]
    if p.schematic:
        steps.append([cli, "sch", "export", "bom", "-o", out / "bom.csv", p.schematic])
        steps.append([cli, "sch", "export", "pdf", "-o", out / "schematic.pdf", p.schematic])
    for cmd in steps:
        try:
            r = toolchain.run(cmd, timeout=600)
        except (OSError, subprocess.SubprocessError) as e:
            # An exporter that can't start is an error, and it mustn't leave a half-written release folder behind.
            shutil.rmtree(out, ignore_errors=True)
            return {"released": False, "blockers": [{"check": "export", "state": "error", "summary": f"{type(e).__name__}: {e}"[:300]}]}
        if r.returncode != 0:
            shutil.rmtree(out, ignore_errors=True)
            return {"released": False, "blockers": [{"check": "export", "state": "error", "summary": r.stderr.strip()[-300:]}]}
    if sha_file(p.board) != board_sha or any(r["state"] not in ("pass", "warn") for r in gate_state(p)):
        shutil.rmtree(out, ignore_errors=True)
        return {"released": False, "blockers": [{"check": "export", "state": "stale",
                                                 "summary": "the board or the evidence changed while the files were exported, so they may not match. Run release again"}]}
    latest = p.ledger.latest()
    baseline = rf_baseline_path(p)
    manifest = {"name": p.name, "board_sha256": board_sha,
                "rf_baseline_sha256": sha_file(baseline) if baseline.exists() else None,
                "evidence": {r["check"]: asdict(latest[r["check"]]) for r in rows},
                "waivers": [w.__dict__ for w in p.waivers()]}
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    archive = p.root / f"{p.name}_release.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(out))
    return {"released": True, "archive": str(archive.relative_to(p.root)), "board_sha256": manifest["board_sha256"],
            "warnings": [r["check"] for r in rows if r["state"] == "warn"]}
