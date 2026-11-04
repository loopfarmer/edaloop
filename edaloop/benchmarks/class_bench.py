#!/usr/bin/env python3
"""One board routed three ways from nothing but ground: Freerouting, KiCadRoutingTools, and by class.

    .venv/bin/python benchmarks/class_bench.py OUT NAME BOARD CHAINS RAILS.json
"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from edaloop import power, route_classes, toolchain                    # noqa: E402
from edaloop.pcbfile import Board                                      # noqa: E402

RF_DRIVER = """
import json, sys
sys.path.insert(0, sys.argv[1]); sys.dont_write_bytecode = True
import rf_path
chains, _, _ = rf_path.load_chains(sys.argv[3])
print(json.dumps(rf_path.measure(rf_path.Board(sys.argv[2]), chains), default=str))
"""


def grade(path: Path, chains: Path, rails: list, keep: str) -> dict:
    cli = toolchain.paths()["kicad_cli"]
    out = path.with_suffix(".drc.json")
    subprocess.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "-o", str(out), str(path)], capture_output=True)
    d = json.loads(out.read_text())
    row = {"unconnected": len(d["unconnected_items"]), "errors": sum(v["severity"] == "error" for v in d["violations"])}
    lib = Path(toolchain.paths()["rf_layout_skill"]) / "lib"
    r = subprocess.run(["python3", "-c", RF_DRIVER, str(lib), str(path), str(chains)], capture_output=True, text=True)
    rows = json.loads(r.stdout)
    row["rf_floor_mm"] = round(sum(x.get("floor") or 0 for x in rows), 2)
    row["rf_routed_mm"] = round(sum(x.get("routed") or 0 for x in rows), 2)
    row["rf_vias"] = sum(x.get("vias") or 0 for x in rows)
    b = Board(path)
    rf_nets = [x["net"] for x in rows]
    row["rf_widths_mm"] = sorted({t.width for t in b.tracks if t.net in rf_nets and t.kind != "via"})
    row["rf_copper_mm"] = round(sum(__import__("math").dist(t.start, t.end) for t in b.tracks if t.net in rf_nets and t.kind != "via"), 2)
    ref = "In1.Cu"
    row["copper_on_rf_reference_mm"] = round(sum(__import__("math").dist(t.start, t.end) for t in b.tracks
                                                 if t.layer == ref and t.kind != "via" and t.net not in keep.split(",")), 2)
    row["vias"] = b.stats()["vias"]
    row["track_mm"] = b.stats()["track_mm"]
    th = b.copper_thickness_um()
    worst = 0.0
    narrow = None
    for rail in rails:
        net = rail["net"]
        res = power.solve_rail(net, [t for t in b.tracks if t.net == net], b.net_pads(net), rail["source"],
                               {k: float(v) for k, v in rail["loads"].items()}, thickness_um=th, board_height_mm=b.board_height_mm())
        if res.unreached:
            row.setdefault("power_unreached", []).extend(res.unreached)
        if res.worst_rise_c >= worst:
            worst = res.worst_rise_c
            narrow = res.worst_segment
    row["power_worst_rise_c"] = worst
    row["power_worst_segment"] = narrow and (narrow["width_mm"], narrow["current_a"], narrow["layer"])
    widths = sorted({t.width for rail in rails for t in b.tracks if t.net == rail["net"] and t.kind != "via"})
    row["power_widths_mm"] = widths
    return row


def main():
    out, name, board, chains, rails_file = sys.argv[1:6]
    out = Path(out) / name
    out.mkdir(parents=True, exist_ok=True)
    board, chains = Path(board).expanduser(), Path(chains).expanduser()
    rails = json.loads(Path(rails_file).read_text())
    keep = sys.argv[6] if len(sys.argv) > 6 else "GND"
    results = {}

    def setup(stem):
        for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
            if board.with_suffix(ext).exists():
                shutil.copy(board.with_suffix(ext), out / f"{stem}{ext}")
        for t in ("fp-lib-table", "sym-lib-table"):
            if (board.parent / t).exists():
                shutil.copy(board.parent / t, out / t)
    setup("orig")
    shutil.copy(board, out / "orig.kicad_pcb")
    results["as_built"] = grade(out / "orig.kicad_pcb", chains, rails, keep)

    setup("stripped")
    py = toolchain.paths()["kicad_python"]
    subprocess.run([py, route_classes.OPS, "export_dsn", board, out / "stripped.kicad_pcb", out / "stripped.dsn", keep, "all"], capture_output=True)

    setup("free")
    tp = toolchain.paths()
    t0 = time.time()
    subprocess.run([tp["java"], "-jar", tp["freerouting_jar"], "-de", out / "stripped.dsn", "-do", out / "free.ses", "-mp", "20", "-mt", "6"],
                   capture_output=True, cwd=out, env={**__import__("os").environ, **toolchain.freerouting_env()}, timeout=1800)
    secs = time.time() - t0
    subprocess.run([py, route_classes.OPS, "import_ses", out / "stripped.kicad_pcb", out / "free.ses", out / "free.kicad_pcb"], capture_output=True)
    results["freerouting"] = {"seconds": round(secs, 1), **grade(out / "free.kicad_pcb", chains, rails, keep)}

    setup("krt")
    t0 = time.time()
    route_classes._krt(out / "stripped.kicad_pcb", out / "krt.kicad_pcb", ["*", f"!{keep.split(',')[0]}"])
    results["krt_all_at_once"] = {"seconds": round(time.time() - t0, 1), **grade(out / "krt.kicad_pcb", chains, rails, keep)}

    t0 = time.time()
    log = route_classes.route(board, chains, rails, out / "classes", keep.split(","), power_layers=("F.Cu", "B.Cu"))
    secs = time.time() - t0
    results["by_class_log"] = log
    if "final" in log:
        results["by_class"] = {"seconds": round(secs, 1), **grade(Path(log["final"]), chains, rails, keep)}
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str))
    for k, v in results.items():
        print(k, json.dumps(v, default=str)[:520])


main()
