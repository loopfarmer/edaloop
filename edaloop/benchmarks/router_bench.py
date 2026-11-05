#!/usr/bin/env python3
"""Freerouting against KiCadRoutingTools, on the same stripped boards, graded the same way.

Each board is copied, its unlocked non-ground copper is removed, and each router
routes the result. Grading is KiCad's DRC with zones refilled, plus a check that
locked copper and ground vias survived. Run from the edaloop folder:

    .venv/bin/python benchmarks/router_bench.py OUT_DIR NAME=PATH.kicad_pcb ...
"""
from __future__ import annotations

import collections
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from edaloop import route_classes, toolchain
from edaloop.pcbfile import Board, compare_copper

OPS = Path(toolchain.__file__).with_name("kicad_ops.py")
KRT_PY = Path.home() / ".cache/edaloop/krt-venv/bin/python"
TIMEOUT = int(os.environ.get("BENCH_TIMEOUT", "1500"))
KEEP = os.environ.get("BENCH_KEEP_NETS", "GND")


def sh(cmd, cwd=None, env=None, timeout=TIMEOUT):
    t0 = time.time()
    try:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env={**os.environ, **(env or {})}, timeout=timeout,
                           capture_output=True, text=True, errors="replace")
        return r.returncode, r.stdout + r.stderr, round(time.time() - t0, 1)
    except subprocess.TimeoutExpired as e:
        return "timeout", str(e.stdout or "")[-2000:], round(time.time() - t0, 1)


def grade(path: Path, base_items: dict, base_vias: int) -> dict:
    cli = toolchain.paths()["kicad_cli"]
    out = path.with_suffix(".drc.json")
    rc, log, secs = sh([cli, "pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "-o", out, path], timeout=900)
    row: dict = {"drc_rc": rc, "drc_s": secs}
    if out.exists():
        d = json.loads(out.read_text())
        kinds = collections.Counter(v["type"] for v in d["violations"])
        row.update(unconnected=len(d["unconnected_items"]), violations=len(d["violations"]),
                   errors=sum(v["severity"] == "error" for v in d["violations"]), top=kinds.most_common(6))
    else:
        row["drc_failed"] = log[-300:]
    b = Board(path)
    delta = compare_copper(base_items, b.copper_items(list(base_items)))
    keep = KEEP.split(",")
    row.update(b.stats(), locked_lost=sum(delta["lost"].values()), ground_vias=b.ground_vias(keep),
               ground_vias_lost=base_vias - b.ground_vias(keep))
    return row


def bench_board(name: str, source: Path, out: Path, routers: list[str]) -> dict:
    d = out / name
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    stem = source.stem
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if source.with_suffix(ext).exists():
            shutil.copy(source.with_suffix(ext), d / f"{stem}{ext}")
    for table in ("fp-lib-table", "sym-lib-table"):
        if (source.parent / table).exists():
            shutil.copy(source.parent / table, d / table)
    py = toolchain.paths()["kicad_python"]
    original = d / f"{stem}.kicad_pcb"
    stats = Board(original)
    result: dict = {"board": name, "as_built": stats.stats()}
    locked_nets = sorted({t.net for t in stats.tracks if t.locked and t.kind != "via" and t.net})
    result["locked_nets"] = len(locked_nets)
    base_items = stats.copper_items(locked_nets)
    base_vias = stats.ground_vias(KEEP.split(","))

    result["as_built_grade"] = grade(original, base_items, base_vias)
    stripped = d / "stripped.kicad_pcb"
    for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        if original.with_suffix(ext).exists():
            shutil.copy(original.with_suffix(ext), stripped.with_suffix(ext))
    dsn = d / "stripped.dsn"
    rc, log, secs = sh([py, OPS, "export_dsn", original, stripped, dsn, KEEP], timeout=300)
    result["strip"] = {"rc": rc, "seconds": secs, "log": log.strip().splitlines()[-1][:200] if log.strip() else ""}
    if rc != 0:
        result["strip_error"] = log[-400:]
        return result
    # A stripped copy shows what is left to route. Copper kept here is what each router must preserve.
    locked_after_strip = Board(stripped).copper_items(locked_nets)
    result["stripped"] = Board(stripped).stats()

    for router in routers:
        target = d / f"{router}.kicad_pcb"
        for ext in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
            if original.with_suffix(ext).exists():
                shutil.copy(original.with_suffix(ext), target.with_suffix(ext))
        row: dict = {"router": router}
        if router == "freerouting":
            tp = toolchain.paths()
            ses = d / "freerouting.ses"
            rc, log, secs = sh([tp["java"], "-jar", tp["freerouting_jar"], "-de", dsn, "-do", ses, "-mp", 20, "-mt", 6],
                               cwd=d, env=toolchain.freerouting_env())
            row.update(route_rc=rc, route_s=secs)
            if ses.exists():
                rc2, log2, secs2 = sh([py, OPS, "import_ses", stripped, ses, target], timeout=600)
                row["import_rc"], row["import_s"] = rc2, secs2
                if rc2 != 0:
                    row["error"] = log2[-300:]
            else:
                row["error"] = log[-300:]
        else:
            krt = Path(toolchain.paths()["krt"])
            js = d / "krt.json"
            pinned = route_classes.krt_rule_args(route_classes.project_rules(original.with_suffix(".kicad_pro")))
            rc, log, secs = sh([KRT_PY, krt / "py_router/route.py", stripped, target, "--nets", "*", f"!{KEEP.split(',')[0]}",
                                "--json-out", js, *pinned], cwd=krt, env={"PYTHONDONTWRITEBYTECODE": "1"})
            # Grade against the project as it was, whatever the router wrote beside its output.
            row["rules_rewritten_by_router"] = route_classes.project_rules(target.with_suffix(".kicad_pro")) != route_classes.project_rules(original.with_suffix(".kicad_pro"))
            shutil.copy(original.with_suffix(".kicad_pro"), target.with_suffix(".kicad_pro"))
            row.update(route_rc=rc, route_s=secs)
            (d / "krt.log").write_text(log)
            if js.exists():
                try:
                    data = json.loads(js.read_text())
                    row["krt_json"] = {k: v for k, v in data.items() if isinstance(v, (int, float, str, bool))}
                except json.JSONDecodeError:
                    pass
            if rc != 0 and not target.exists():
                row["error"] = log[-400:]
        if target.exists():
            row.update(grade(target, locked_after_strip, base_vias))
        result[router] = row
        print(json.dumps({"board": name, **row}, default=str)[:600], flush=True)
    return result


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    routers = os.environ.get("BENCH_ROUTERS", "freerouting,krt").split(",")
    results = []
    for spec in sys.argv[2:]:
        name, path = spec.split("=", 1)
        results.append(bench_board(name, Path(path).expanduser(), out, routers))
        (out / "results.json").write_text(json.dumps(results, indent=1, default=str))
    print("done", flush=True)


if __name__ == "__main__":
    main()
