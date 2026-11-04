"""Router selection and the tournament.

Candidates are routed in isolated copies and scored on the same checks the gate
uses. The project board is only ever replaced by an explicit adopt, so no router
can overwrite the design as a side effect.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import toolchain
from .ledger import sha_file
from .pcbfile import Board, compare_copper
from .project import Project

OPS = Path(__file__).with_name("kicad_ops.py")

# OrthoRoute's own README and the PCBWorld benchmark limit it to very large,
# regular boards: 1-2% clean pass and 30-51% routability on ordinary ones.
ORTHOROUTE_MIN_NETS = 1000


def classify(board: Board) -> dict:
    """Decide which routers may run on this board, and say why."""
    nets = board.stats()["nets"]
    allowed = ["freerouting"]
    reasons = {"freerouting": "general-purpose default"}
    if nets >= ORTHOROUTE_MIN_NETS:
        reasons["orthoroute"] = (f"{nets} nets: in scale, but its headless mode crashed on a file-based export of "
                                 "its own TestBackplane here. Use its KiCad plugin, then rerun the gate")
    else:
        reasons["orthoroute"] = f"excluded: {nets} nets, and it only suits regular boards of {ORTHOROUTE_MIN_NETS}+"
    return {"nets": nets, "allowed": allowed, "reasons": reasons}


def _kicad_op(*args, timeout=900) -> dict:
    py = toolchain.paths()["kicad_python"]
    r = toolchain.run([py, OPS, *args], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"pcbnew {args[0]} failed: {(r.stderr + r.stdout).strip()[-400:]}")
    for line in reversed(r.stdout.strip().splitlines()):   # pcbnew prints its own chatter around ours
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError(f"pcbnew {args[0]} returned no result: {r.stdout.strip()[-300:]}")


def _drc(board: Path, out: Path) -> dict:
    cli = toolchain.paths()["kicad_cli"]
    # Refill: a router's output carries a stale fill, which reads as false shorts.
    r = toolchain.run([cli, "pcb", "drc", "--format", "json", "--severity-all", "--refill-zones", "-o", out, board], timeout=900)
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"drc failed: {r.stderr.strip()[-300:]}")
    d = json.loads(out.read_text())
    errors = [v for v in d["violations"] if v["severity"] == "error"]
    return {"violations": len(d["violations"]), "errors": len(errors), "unconnected": len(d["unconnected_items"])}


def _krt_route(p: Project, stripped: Path, run_dir: Path, keep_nets: list[str]) -> dict:
    """KiCadRoutingTools on the same stripped board, held to the project's own rules."""
    from .route_classes import _krt
    out = run_dir / stripped.name
    t0 = time.time()
    info = _krt(stripped, out, ["*", *[f"!{n}" for n in keep_nets]], timeout=3600, project=stripped.with_suffix(".kicad_pro"))
    row = {"name": "kicadroutingtools", "seconds": round(time.time() - t0, 1), "dir": str(run_dir.relative_to(p.root))}
    if not out.exists():
        row["error"] = info.get("tail", "no output")
    return row


def tournament(p: Project, runs: int = 3, passes: int = 20, threads: int = 4) -> dict:
    """Route the unlocked nets with each router, in copies, since Freerouting differs run to run, and rank them."""
    from .checks import _chain_nets
    tp = toolchain.paths()
    root = p.root / ".edaloop" / "route" / time.strftime("%Y%m%d-%H%M%S")
    root.mkdir(parents=True)
    source = Board(p.board)
    rf_nets = _chain_nets(p) if p.section("rf").get("chains") else []
    rf_before = source.copper_items(rf_nets)
    keep_nets = list(p.section("route").get("keep_nets") or ["GND"])
    max_added = float(p.section("route").get("max_rf_added_mm", 0.01))
    verdict = classify(source)
    baseline = {"name": "current board", **source.stats(), **_drc(p.board, root / "baseline_drc.json"),
                "rf_lost": 0, "rf_added_mm": 0.0, "ground_vias": source.ground_vias(keep_nets),
                "rf_vias": sum(t.kind == "via" and t.net in rf_nets for t in source.tracks)}
    stripped = root / f"{p.board.stem}.kicad_pcb"
    for ext in (".kicad_pro", ".kicad_prl"):
        src = p.board.with_suffix(ext)
        if src.exists():
            shutil.copy(src, root / src.name)
    for table in ("fp-lib-table", "sym-lib-table"):
        if (p.board.parent / table).exists():
            shutil.copy(p.board.parent / table, root / table)
    dsn = root / "board.dsn"
    strip = _kicad_op("export_dsn", p.board, stripped, dsn, ",".join(keep_nets))
    results = []
    for i in range(1, runs + 1):
        run_dir = root / f"run{i}"
        run_dir.mkdir()
        ses = run_dir / "board.ses"
        t0 = time.time()
        r = toolchain.run([tp["java"], "-jar", tp["freerouting_jar"], "-de", dsn, "-do", ses,
                           "-mp", passes, "-mt", threads], cwd=run_dir, env=toolchain.freerouting_env(), timeout=3600)
        row = {"name": f"freerouting run {i}", "seconds": round(time.time() - t0, 1), "dir": str(run_dir.relative_to(p.root))}
        results.append(row)
        if not ses.exists():
            row["error"] = (r.stdout + r.stderr).strip()[-300:]
            continue
        for f in root.iterdir():
            if f.is_file() and f.suffix in (".kicad_pro", ".kicad_prl") or f.name in ("fp-lib-table", "sym-lib-table"):
                shutil.copy(f, run_dir / f.name)
        out = run_dir / stripped.name
        _kicad_op("import_ses", stripped, ses, out)
        board = Board(out)
        row.update(board.stats())
        row.update(_drc(out, run_dir / "drc.json"))
        delta = compare_copper(rf_before, board.copper_items(rf_nets))
        row["rf_lost"] = sum(delta["lost"].values())
        row["rf_added_mm"] = delta["added_mm"]
        row["rf_vias"] = sum(t.kind == "via" and t.net in rf_nets for t in board.tracks)
        row["ground_vias"] = board.ground_vias(keep_nets)
        row["admissible"] = (row["rf_lost"] == 0 and row["rf_added_mm"] <= max_added and delta["new_vias"] == 0
                             and row["ground_vias"] >= baseline["ground_vias"])
        row["board"] = str(out.relative_to(p.root))
        row["sha256"] = sha_file(out)
    if (Path(tp["krt"]) / "py_router/route.py").exists():
        run_dir = root / "krt"
        run_dir.mkdir()
        for f in root.iterdir():
            if f.is_file() and (f.suffix in (".kicad_pro", ".kicad_prl", ".kicad_dru") or f.name in ("fp-lib-table", "sym-lib-table")):
                shutil.copy(f, run_dir / f.name)
        row = _krt_route(p, stripped, run_dir, keep_nets)
        results.append(row)
        out = run_dir / stripped.name
        if "error" not in row:
            board = Board(out)
            row.update(board.stats())
            row.update(_drc(out, run_dir / "drc.json"))
            delta = compare_copper(rf_before, board.copper_items(rf_nets))
            row["rf_lost"] = sum(delta["lost"].values())
            row["rf_added_mm"] = delta["added_mm"]
            row["rf_vias"] = sum(t.kind == "via" and t.net in rf_nets for t in board.tracks)
            row["ground_vias"] = board.ground_vias(keep_nets)
            row["admissible"] = (row["rf_lost"] == 0 and row["rf_added_mm"] <= max_added and delta["new_vias"] == 0
                                 and row["ground_vias"] >= baseline["ground_vias"])
            row["board"] = str(out.relative_to(p.root))
            row["sha256"] = sha_file(out)
    usable = [r for r in results if "error" not in r]
    usable.sort(key=lambda r: (not r["admissible"], r["unconnected"], r["errors"], r["rf_vias"], r["vias"], r["track_mm"]))
    summary = {"verdict": verdict, "strip": strip, "baseline": baseline, "ranked": usable,
               "failed": [r for r in results if "error" in r], "dir": str(root.relative_to(p.root))}
    (root / "tournament.json").write_text(json.dumps(summary, indent=1))
    (p.root / ".edaloop" / "last_tournament.json").write_text(json.dumps(summary, indent=1))
    return summary


def adopt(p: Project, rank: int = 0) -> dict:
    """Copy a tournament result over the project board. Every claim about the old board goes stale."""
    state = p.root / ".edaloop" / "last_tournament.json"
    if not state.exists():
        raise RuntimeError("no tournament to adopt from")
    ranked = json.loads(state.read_text())["ranked"]
    row = ranked[rank]
    if not row["admissible"] or row["unconnected"] or row["errors"]:
        raise RuntimeError(f"refused: {row['name']} is not admissible: lost {row['rf_lost']} RF item(s), "
                           f"added {row['rf_added_mm']} mm of RF copper, {row['ground_vias']} ground vias, "
                           f"{row['unconnected']} open(s), {row['errors']} DRC error(s)")
    keep = p.root / ".edaloop" / f"{p.board.name}.before_adopt"
    shutil.copy(p.board, keep)
    shutil.copy(p.root / row["board"], p.board)
    return {"adopted": row["name"], "backup": str(keep.relative_to(p.root)), "sha256": sha_file(p.board)}
