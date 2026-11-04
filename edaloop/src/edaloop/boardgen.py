"""A KiCad project from a written board description: schematic, placement, pours and edaloop.yaml.

The description in testbenches/ states the netlist and how the parts sit on the RF line. Positions are solved
from the parts' own courtyards, in kicad_ops.py, so a changed part re-solves the board instead of invalidating a
table. Routing is `edaloop route --by-class`, as for any other board. Generated files are never hand-edited:
change the description and build again.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import yaml

from . import schematic as drawing
from . import toolchain
from .project import ProjectError

OPS = Path(__file__).with_name("kicad_ops.py")
STANDARD_LIBS = ["/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints", "/usr/share/kicad/footprints"]
NS = uuid.UUID("6f2a6c1e-0b5e-4d0a-9f55-0d3b7d1f7a10")

RULES = {"min_clearance": 0.0, "min_connection": 0.0, "min_copper_edge_clearance": 0.5, "min_hole_clearance": 0.25,
         "min_hole_to_hole": 0.2, "min_through_hole": 0.2, "min_through_hole_diameter": 0.2, "min_microvia_diameter": 0.2, "min_microvia_drill": 0.1, "min_track_width": 0.1524,
         "min_via_annular_width": 0.1778, "min_via_diameter": 0.56, "min_silk_clearance": 0.0, "min_text_height": 0.8,
         "min_text_thickness": 0.08, "max_error": 0.005}
# Conditions of a generated board rather than faults in it. Each is visible here so that it can be argued with.
SEVERITIES = {"silk_overlap": "warning", "silk_over_copper": "warning", "silk_edge_clearance": "warning",
              "lib_footprint_issues": "ignore", "lib_footprint_mismatch": "ignore", "starved_thermal": "warning"}


class BuildError(ProjectError):
    pass


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


def _q(text) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def stackup_text(bench: dict) -> str:
    st = bench["stackup"]
    copper, gaps = st["copper_um"], st["dielectric_mm"]
    names = ["F.Cu", *[f"In{i}.Cu" for i in range(1, len(copper) - 1)], "B.Cu"]
    out = ['(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))']
    for i, nm in enumerate(names):
        out.append(f'(layer "{nm}" (type "copper") (thickness {copper[i] / 1000:g}))')
        if i < len(gaps):
            kind = "core" if i == len(gaps) // 2 else "prepreg"
            out.append(f'(layer "dielectric {i + 1}" (type "{kind}") (thickness {gaps[i]:g}) (material "FR4") '
                       f'(epsilon_r {st["epsilon_r"]:g}) (loss_tangent {st.get("loss_tangent", 0.02):g}))')
    out.append('(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))')
    return " ".join(out)


def seed_board(bench: dict) -> str:
    """An empty board with the stackup. pcbnew can't write a stackup, so this one step is text."""
    st = bench["stackup"]
    n = len(st["copper_um"])
    ids = [(0, "F.Cu"), *[(2 * i + 2, f"In{i}.Cu") for i in range(1, n - 1)], (2, "B.Cu")]
    copper = " ".join(f'({i} "{nm}" {"power" if nm.startswith("In") else "signal"})' for i, nm in ids)
    thick = sum(st["copper_um"]) / 1000 + sum(st["dielectric_mm"]) + 0.02
    return (f'(kicad_pcb (version 20260206) (generator "edaloop") (generator_version "10.0") '
            f'(general (thickness {thick:g}) (legacy_teardrops no)) (paper "A4") (layers {copper} '
            '(9 "F.Adhes" user "F.Adhesive") (11 "B.Adhes" user "B.Adhesive") (13 "F.Paste" user) (15 "B.Paste" user) '
            '(5 "F.SilkS" user "F.Silkscreen") (7 "B.SilkS" user "B.Silkscreen") (1 "F.Mask" user) (3 "B.Mask" user) '
            '(17 "Dwgs.User" user "User.Drawings") (19 "Cmts.User" user "User.Comments") (21 "Eco1.User" user "User.Eco1") '
            '(23 "Eco2.User" user "User.Eco2") (25 "Edge.Cuts" user) (27 "Margin" user) (31 "F.CrtYd" user "F.Courtyard") '
            '(29 "B.CrtYd" user "B.Courtyard") (35 "F.Fab" user) (33 "B.Fab" user)) '
            f'(setup (stackup {stackup_text(bench)} (copper_finish "ENIG") (dielectric_constraints no)) '
            '(pad_to_mask_clearance 0) (allow_soldermask_bridges_in_footprints no)) (net 0 "") (embedded_fonts no))\n')


def project_file(bench: dict, name: str) -> dict:
    chains = bench["chain"]
    extra = [c[0] for c in bench.get("extra_chains") or []]
    rf_nets = [c[0] for c in chains] + extra
    default = {"name": "Default", "clearance": 0.2, "track_width": 0.2, "via_diameter": 0.61, "via_drill": 0.254,
               "microvia_diameter": 0.3, "microvia_drill": 0.1, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
               "diff_pair_width": 0.2, "line_style": 0, "wire_width": 6, "bus_width": 12, "priority": 2147483647,
               "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)"}
    rf = {**default, "name": "RF", "track_width": bench["rf_line_width_mm"], "priority": 0}
    return {"board": {"design_settings": {"rules": RULES, "rule_severities": SEVERITIES,
                                          "defaults": {"zones": {"min_clearance": 0.3}}}},
            "meta": {"filename": f"{name}.kicad_pro", "version": 3},
            "net_settings": {"classes": [default, rf], "meta": {"version": 5},
                             "netclass_patterns": [{"netclass": "RF", "pattern": n} for n in rf_nets]},
            "sheets": [[_uid(name, "root"), "Root"]], "pcbnew": {}, "schematic": {}, "libraries": {}}


def chains_file(bench: dict) -> str:
    rows = []
    for net, entry, leave in [*bench["chain"], *(bench.get("extra_chains") or [])]:
        rows.append(f'    ({net!r}, {tuple(entry.split("."))!r}, {tuple(leave.split("."))!r}),')
    return ('# Generated from the board description. Edit that, not this.\n'
            f'WIDTH = {bench["rf_line_width_mm"]}\nNET_WIDTH = {(bench["layout"].get("net_width") or {})!r}\n'
            'CHAINS = [\n' + "\n".join(rows) + '\n]\n')


def project_yaml(bench: dict, name: str, chains: str) -> dict:
    cfg = bench["project"]
    reference = "In1.Cu" if len(bench["stackup"]["copper_um"]) > 2 else "B.Cu"
    pack = Path(toolchain.paths()["rule_pack"]).expanduser()
    has_rules = (pack / "manufacture/summit_standard.yaml").exists()
    gate = ["project_files", "erc", "drc", *(["rules"] if has_rules else []), "rf_path", "rf_preserved", "rf_reference", "rf_layout",
            "impedance", "power", "thermal_field", "em_hops", "cascade", "spice", "em_crosscheck", "requirements"]
    low, high = bench["band_mhz"]
    cascade = {"freq_mhz": [], "stages": []}
    if bench.get("stages"):
        cascade = {"freq_mhz": [low, 1000, high], "stages": bench["stages"]}
    elif not bench.get("design_table"):
        gate.remove("cascade")      # no stage values to work from, and a described schematic won't have them yet
    else:
        table = bench["design_table"]
        f = table["freq_mhz"]
        cascade = {"freq_mhz": [f], "stages": [
            {"name": r["stage"], "gain_db": [[f, r["gain_db"]]], "nf_db": [[f, r["nf_db"]]], **({"oip3_dbm": [[f, r["oip3_dbm"]]]} if "oip3_dbm" in r else {})}
            for r in table["rows"] if r["stage"] != "input cable"]}
    cascade["line_loss_db_per_mm"] = cfg.get("line_loss_db_per_mm", 0.004)
    bench_cfg = bench["spice"]
    return {"name": name, "board": f"{name}.kicad_pcb", "schematic": f"{name}.kicad_sch",
            "rf": {"chains": chains, "max_over_pct": 5,
                   "impedance": {"target": 50, "tol": 10, "max_freq_ghz": high / 1000 * 1.05, "min_section_rl_db": 20,
                                 "eps_eff": cfg.get("eps_eff", 3.25)}},
            "em": {"signal_layer": "F.Cu", "ground_layer": reference, "gap_um": 300, "side_um": 3000, "length_um": 10000,
                   "width_um": int(bench["rf_line_width_mm"] * 1000), "fmin_ghz": 1, "fmax_ghz": 5, "at_ghz": 3,
                   "target_ohm": 50, "tol_pct": 10, "max_spread_pct": 2,
                   "fast": {"solver": "emerge", "quality": "Draft", "adaptive_mesh": 4},
                   "solvers": [{"solver": "emerge", "quality": "Accurate"},
                               {"solver": "openems", "cells_per_wavelength": 40, "min_cell_um": 8},
                               {"solver": "palace", "quality": "Standard"}],
                   **(cfg.get("em") or {})},
            "power": {"max_rise_c": 10, "rails": cfg["rails"]},
            "thermal": {"dx_mm": 0.05, "thermal_dx_mm": 0.2, "sublayers": 3, "h_w_m2k": 10, "supply_v": bench.get("supply_v", 5.0), **(cfg.get("thermal") or {})},
            "route": {"keep_nets": ["GND"], "reference_layers": [reference],
                      # Supply and control run under whole ground, not across the RF layer, so a board with an inner layer to spare uses it.
                      **({"power_layers": ["F.Cu", "In2.Cu", "B.Cu"], "layer_costs": {"F.Cu": 6.0, "In2.Cu": 1.0, "B.Cu": 1.5}}
                         if len(bench["stackup"]["copper_um"]) > 2 else {"power_layers": ["F.Cu", "B.Cu"]})},
            "stages": {"place": ["rf_path"], "rf": ["rf_path", "rf_preserved", "rf_reference", "impedance", "em_fast"],
                       "power": ["power", "thermal_field", "drc"], "signal": ["drc", "rf_preserved"]},
            **({"rules": {"pack": str(pack), "profile": "manufacture/summit_standard.yaml"}} if has_rules else {}),
            "em_hops": {"band_ghz": [low / 1000, high / 1000], "nets": [c[0] for c in bench["chain"]],
                        "solver": {"solver": "emerge", "quality": "Accurate"}, "min_rl_db": 15, "warn_rl_db": 20},
            "cascade": cascade,
            "spice": {"models": bench_cfg["models"], "benches": [bench_cfg["bench"]]},
            "requirements": [{"id": "R1", "text": "No DRC violations", "metric": "drc.violations", "max": 0},
                             {"id": "R2", "text": "Board fully connected", "metric": "drc.unconnected", "max": 0},
                             {"id": "R3", "text": "No vias in the RF path", "metric": "rf_path.vias", "max": 0},
                             {"id": "R4", "text": "RF path within 5% of its floor", "metric": "rf_path.over_pct", "max": 5},
                             {"id": "R5", "text": "Line within 10% of 50 ohm by field solver", "metric": "em_crosscheck.zc_ohm",
                              "min": 45, "max": 55},
                             {"id": "R6", "text": "Field solvers agree to 2%", "metric": "em_crosscheck.spread_pct", "max": 2},
                             {"id": "R7", "text": "Each RF hop's copper returns at least 15 dB in band", "metric": "em_hops.worst_rl_db",
                              "min": 15}],
            "gate": gate, "waivers": []}


def make(bench_path: Path, out: Path, footprint_dirs: list[Path], quick: bool = False) -> dict:
    """Description to reviewed layout: build, ground, route, finish, the checks, and a review folder. Stops at the first step that fails.

    With `quick`, the three-solver EM check is left out, for iterating. The gate then still reports it as missing, so nothing can be released.
    """
    from . import checks, controller, ground, route_classes
    from .project import Project
    try:
        built = build(bench_path, out, footprint_dirs)
    except BuildError as e:
        return {"made": False, "stopped_at": "build", "refused": str(e)}
    p = Project(out)
    steps = {"build": {k: built[k] for k in ("parts", "span_mm", "outline_mm", "unlisted_pads")}, "ground": ground.pad_vias(p)}
    rf, cfg = p.section("rf"), p.section("route")
    routed = route_classes.route(p.board, p.root / rf["chains"], p.section("power").get("rails", []), p.root / ".edaloop" / "route_classes",
                                 list(cfg.get("keep_nets") or ["GND"]), max_rise_c=float(p.section("power").get("max_rise_c", 10)),
                                 rf_target_ohm=float((rf.get("impedance") or {}).get("target", 50)),
                                 reference_layers=tuple(cfg.get("reference_layers") or ("In1.Cu",)),
                                 power_layers=tuple(cfg["power_layers"]) if cfg.get("power_layers") else None,
                                 layer_costs=cfg.get("layer_costs"))
    (out / "route.log").write_text(json.dumps(routed, indent=1))
    steps["route"] = {"final_drc": routed.get("final_drc"), "rf_width": routed["rf_width_loop"][-1], "stopped_at": routed.get("stopped_at")}
    if routed.get("stopped_at"):
        return {"made": False, "stopped_at": "route", "steps": steps}
    steps["finish"] = ground.finish(p)
    p = Project(out)
    steps["checks"] = controller.run_all(p, skip=("em_crosscheck",) if quick else ())
    steps["review"] = checks.review_pack(p)
    return {"made": True, "ready_to_release": steps["review"]["ready_to_release"], "next_action": controller.next_action(p), "steps": steps}


def build(bench_path: Path, out: Path, footprint_dirs: list[Path]) -> dict:
    """Write a project into `out`, which must be empty or absent. Nothing outside it is touched."""
    bench = yaml.safe_load(Path(bench_path).read_text())
    name = bench["name"]
    needed = [f"project.rails: {x['net']} load at {k}" for x in bench["project"]["rails"] for k, v in x["loads"].items() if not isinstance(v, (int, float))]
    if needed:
        raise BuildError("the description still needs these currents, in amps: " + "; ".join(needed))
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise BuildError(f"{out} isn't empty. Build into a new folder, so that no routed board is overwritten")
    libs = {f.split(":")[0] for f in bench["layout"]["footprints"].values()}
    table = []
    for root in footprint_dirs:
        for lib in sorted(libs):
            if (Path(root) / f"{lib}.pretty").is_dir():
                shutil.copytree(Path(root) / f"{lib}.pretty", out / f"{lib}.pretty")
                table.append(f'  (lib (name "{lib}")(type "KiCad")(uri "${{KIPRJMOD}}/{lib}.pretty")(options "")(descr ""))')
    (out / "fp-lib-table").write_text("(fp_lib_table\n  (version 7)\n" + "\n".join(table) + "\n)\n")
    sheets = drawing.project_sheets(bench, name)
    for filename, text in sheets["files"].items():
        (out / filename).write_text(text)
    (out / "edaloop.kicad_sym").write_text(sheets["library"])
    (out / "sym-lib-table").write_text('(sym_lib_table\n  (version 7)\n  (lib (name "edaloop")(type "KiCad")'
                                       '(uri "${KIPRJMOD}/edaloop.kicad_sym")(options "")(descr ""))\n)\n')
    shutil.copy(bench_path, out / "board_description.yaml")
    chains = f"chains_{name}.py"
    (out / chains).write_text(chains_file(bench))
    (out / "edaloop.yaml").write_text(yaml.safe_dump(project_yaml(bench, name, chains), sort_keys=False, width=120))
    seed = out / ".seed.kicad_pcb"
    seed.write_text(seed_board(bench))
    spec = out / ".build_spec.json"
    spec.write_text(json.dumps({
        "parts": bench["parts"], "chain": [*bench["chain"]], "nets": bench["nets"], "pins": drawing.pin_map(bench), "open_pins": bench.get("open_pins") or [], "layout": bench["layout"],
        "outline": bench.get("outline_mm"), "lib_dirs": [str(out), *[str(x) for x in footprint_dirs], *STANDARD_LIBS],
        "uuids": sheets["uuids"], "sheet_of": sheets["placed"], "root": _uid(name, "root"),
        "sheet": "Root"}))
    r = toolchain.run([toolchain.paths()["kicad_python"], OPS, "build_board", seed, spec, out / f"{name}.kicad_pcb"], timeout=600)
    netlist = out / ".schematic_netlist.xml"
    x = toolchain.run([toolchain.paths()["kicad_cli"], "sch", "export", "netlist", "--format", "kicadxml", "-o", netlist, out / f"{name}.kicad_sch"], timeout=120)
    if x.returncode != 0 or not netlist.exists():
        raise BuildError(f"the drawn schematic won't export a netlist: {x.stderr.strip()[-300:]}")
    problems = drawing.check_netlist(netlist, bench)
    netlist.unlink()
    if problems:
        raise BuildError("the drawn schematic doesn't match the description: " + "; ".join(problems[:8]))
    # Written last: saving a board that had no project beside it writes one full of KiCad's default rules.
    (out / f"{name}.kicad_pro").write_text(json.dumps({**project_file(bench, name), "sheets": sheets["sheets"]}, indent=2))
    seed.unlink(missing_ok=True)
    spec.unlink(missing_ok=True)
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode != 0 or not lines:
        raise BuildError(f"placement failed: {(r.stdout + r.stderr).strip()[-600:]}")
    return json.loads(lines[-1])
