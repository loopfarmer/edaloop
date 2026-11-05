#!/usr/bin/env python3
"""Route boards by class, then grade each result through the real gate checks.

    .venv/bin/python benchmarks/class_regress.py OUT NAME=BOARD:CHAINS ...
"""
import json
import math
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from edaloop import checks, route_classes
from edaloop.pcbfile import Board
from edaloop.project import Project

GATE = ["drc", "rf_path", "rf_reference", "rules", "impedance"]


def grade(name: str, routed: Path, source: Path, chains: Path, out: Path) -> dict:
    proj = out / "graded"
    proj.mkdir(parents=True, exist_ok=True)
    shutil.copy(routed, proj / source.name)
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if source.with_suffix(ext).exists():
            shutil.copy(source.with_suffix(ext), proj / source.with_suffix(ext).name)
    for t in ("fp-lib-table", "sym-lib-table"):
        if (source.parent / t).exists():
            shutil.copy(source.parent / t, proj / t)
    shutil.copy(chains, proj / chains.name)
    (proj / "edaloop.yaml").write_text(json.dumps({
        "name": name, "board": source.name, "rf": {"chains": chains.name, "max_over_pct": 5,
        "impedance": {"target": 50, "tol": 10, "max_freq_ghz": 1.25, "min_section_rl_db": 20}},
        "drc": {"refill_zones": True}, "route": {"reference_layers": ["In1.Cu"], "keep_nets": ["GND"]},
        "rules": {"profile": "manufacture/summit_standard.yaml"}, "gate": GATE}))
    p = Project(proj)
    row = {}
    for c in GATE:
        ev = checks.run_check(p, c)
        kinds = {}
        for f in ev.findings:
            k = f["id"].split(":")[-1] if c != "drc" else f["id"].split(":", 1)[-1]
            kinds[k] = kinds.get(k, 0) + 1
        row[c] = {"status": ev.status, "findings": kinds}
        if c == "rf_path":
            row["rf"] = {k: ev.metrics.get(k) for k in ("floor_mm", "routed_mm", "vias", "over_pct")}
        if c == "impedance":
            row["z_avg_range"] = (ev.metrics.get("z_avg_min"), ev.metrics.get("z_avg_max"))
            row["worst_section_rl_db"] = ev.metrics.get("worst_section_rl_db")
        if c == "rf_reference":
            row["on_plane_mm"] = ev.metrics.get("non_ground_on_reference_mm")
        row.setdefault("detail", {})[c] = [f["message"][:150] for f in ev.findings if f["severity"] == "error"][:6]
    return row


def main():
    out = Path(sys.argv[1])
    results = {}
    for spec in sys.argv[2:]:
        name, rest = spec.split("=", 1)
        board, chains = (Path(x).expanduser() for x in rest.split(":"))
        d = out / name
        t0 = time.time()
        log = route_classes.route(board, chains, [], d / "classes", ["GND"])
        row = {"seconds": round(time.time() - t0, 1), "steps": [
            {k: v for k, v in s.items() if k in ("class", "failed", "successful", "necks", "unreached", "drc")} for s in log["steps"]],
            "stopped_at": log.get("stopped_at"), "final_drc": log.get("final_drc"), "width": log.get("rf_width_loop", [{}])[-1]}
        if log.get("final"):
            row.update(grade(name, Path(log["final"]), board, chains, d))
            b = Board(log["final"])
            nets = [c[0] for c in route_classes._load_chains(chains)[0]]
            widths = {}
            for t in b.tracks:
                if t.net in nets and t.kind != "via":
                    w = round(t.width, 3)
                    widths[w] = round(widths.get(w, 0) + math.dist(t.start, t.end), 2)
            row["rf_mm_by_width"] = dict(sorted(widths.items()))
        base = grade(name, board, board, chains, d / "as_built")
        row["as_built"] = {"rf": base.get("rf"), "rf_path": base["rf_path"]["findings"], "drc": base["drc"]["status"], "z": base.get("z_avg_range")}
        results[name] = row
        print(name, json.dumps(row, default=str), flush=True)
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))


if __name__ == "__main__":
    main()
