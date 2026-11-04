"""Command line. Every command takes --json so that scripts and agents get the same data as people."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import checks, controller, route, toolchain
from .project import Project, ProjectError


def _out(data, as_json, text):
    print(json.dumps(data, indent=1, default=str) if as_json else text(data))


def _table(rows, cols):
    widths = [max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows else len(c) for c in cols]
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, widths))]
    lines += ["  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)) for r in rows]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="edaloop", description=__doc__)
    ap.add_argument("-C", "--project", default=".", help="project directory (default: here)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="run every external tool and report what works")
    d.add_argument("--fix", action="store_true", help="set up private shims under ~/.cache/edaloop")
    sub.add_parser("status", help="the gate, with staleness resolved against the current files")
    sub.add_parser("next", help="the one next action")
    r = sub.add_parser("run", help="run a check, or 'all' for every missing or stale gated check")
    r.add_argument("check")
    rt = sub.add_parser("route", help="route the unlocked nets in isolated copies and rank the results")
    rt.add_argument("--runs", type=int, default=3)
    rt.add_argument("--passes", type=int, default=20)
    rt.add_argument("--by-class", action="store_true", help="route RF, then power, then signal, with a sensor after each")
    st = sub.add_parser("stage", help="run the sensors a routing stage names in edaloop.yaml, quickest first")
    st.add_argument("name")
    a = sub.add_parser("adopt", help="replace the project board with a tournament result")
    a.add_argument("--rank", type=int, default=0)
    sub.add_parser("release", help="export fabrication files if the gate holds")
    sub.add_parser("review", help="export schematic, renders, BOM and gate status for a person to review, with no Gerbers")
    rep = sub.add_parser("report", help="write the HTML scorecard")
    rep.add_argument("-o", "--output", default="scorecard.html")
    sub.add_parser("checks", help="list the available checks")
    sub.add_parser("init", help="wire this folder into the loop: project file, MCP servers, hook, skill")
    sub.add_parser("hook", help="for the Claude Code hook: report what a design edit made stale")
    tb = sub.add_parser("bench", help="run the written test benches, or the ones named, with no KiCad files")
    tb.add_argument("files", nargs="*", type=Path)
    bd = sub.add_parser("build", help="make a placed KiCad project from a written board description")
    bd.add_argument("bench", type=Path)
    bd.add_argument("-o", "--output", type=Path, required=True)
    bd.add_argument("--footprints", type=Path, action="append", default=[], help="folder holding extra .pretty libraries")
    mk = sub.add_parser("make", help="description to reviewed layout: build, ground, route, finish, every check, and a review folder")
    mk.add_argument("bench", type=Path)
    mk.add_argument("-o", "--output", type=Path, required=True)
    mk.add_argument("--footprints", type=Path, action="append", default=[], help="folder holding extra .pretty libraries")
    mk.add_argument("--quick", action="store_true", help="leave out the three-solver EM check, for iterating. Nothing can be released without it")
    ev = sub.add_parser("eval", help="run the framework against known-good production boards and print a scorecard")
    ev.add_argument("samples", nargs="*", help="sample names from evaluation/samples.yaml, by default all of them")
    ev.add_argument("--level", choices=["describe", "checks", "regenerate"], default="describe", help="describe: import and read each board. checks: also run the checks on its released copper. regenerate: rebuild it from its netlist and compare")
    ev.add_argument("-o", "--output", type=Path, default=Path("evaluation-work"))
    me = sub.add_parser("mechanical", help="write an OpenSCAD test mount and case for this board, and render them if OpenSCAD is installed")
    me.add_argument("-o", "--output", type=Path, default=None, help="by default a mechanical folder in the project")
    me.add_argument("--no-render", action="store_true", help="write the .scad only")
    ex = sub.add_parser("export", help="write this board in KiCad 6.0 and EAGLE 7.7 formats, read each back, and report")
    ex.add_argument("--format", nargs="+", choices=["kicad6", "eagle"], default=None, help="by default, both")
    ex.add_argument("-o", "--output", type=Path, default=None, help="by default an export folder in the project")
    fz = sub.add_parser("fritzing", help="write a Fritzing part (.fzpz) for this board")
    fz.add_argument("-o", "--output", type=Path, default=None, help="by default a fritzing folder in the project")
    cm = sub.add_parser("compare", help="two boards side by side: size, RF length, vias, and whether they join the same pads")
    cm.add_argument("first", type=Path)
    cm.add_argument("second", type=Path)
    cm.add_argument("--rf-net", action="append", default=[], help="an RF net to compare by length. Repeat it, or leave it out to use the chains in the first board's edaloop.yaml")
    ds = sub.add_parser("describe", help="write a board description from an existing KiCad schematic or board")
    ds.add_argument("schematic", type=Path, help=".kicad_sch, or a .kicad_pcb when there is no schematic, as with an atopile build")
    ds.add_argument("-o", "--output", type=Path, required=True)
    ds.add_argument("--band-mhz", type=float, nargs=2, required=True, metavar=("LOW", "HIGH"))
    ds.add_argument("--name", help="board name, by default the schematic's")
    sub.add_parser("ground", help="place ground vias at ground pads, before routing")
    sub.add_parser("finish", help="adopt the by-class route, add the RF fence and stitching, and refill")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "doctor":
            if args.fix:
                print("\n".join(toolchain.fix()), file=sys.stderr)
            probes = [p.__dict__ for p in toolchain.doctor()]
            for row in probes:
                row["state"] = "ok" if row["ok"] else "MISSING"
            _out(probes, args.json, lambda x: _table(x, ["name", "state", "role", "detail", "fix"]))
            return 0
        if args.cmd == "checks":
            _out(controller.describe_checks(), args.json,
                 lambda x: "\n".join(f"{k:15} {v['stage']:9} {v['doc']}" for k, v in x.items()))
            return 0
        if args.cmd == "init":
            from .scaffold import init
            _out(init(Path(args.project)), True, str)
            return 0
        if args.cmd == "hook":
            from .scaffold import hook_message
            msg = hook_message(Path(args.project).resolve(), json.load(sys.stdin))
            if msg:
                print(json.dumps(msg))
            return 0
        if args.cmd == "bench":
            import tempfile
            from . import testbench
            with tempfile.TemporaryDirectory() as tmp:
                data = [testbench.run(f, Path(tmp)) for f in (args.files or testbench.available())]
            rows = [{"bench": b["bench"], **r} for b in data for r in b["checks"]]
            _out(data, args.json, lambda _: _table(rows, ["bench", "check", "status", "detail"]))
            return 0 if data and all(b["ok"] for b in data) else 1
        if args.cmd == "eval":
            from . import evaluate
            card = evaluate.run(args.samples, args.output, args.level)
            _out(card, True, str)
            return 0
        if args.cmd == "compare":
            from . import compare as comparing
            nets = args.rf_net
            if not nets and (args.first.parent / "edaloop.yaml").exists():
                nets = checks._chain_nets(Project(args.first.parent))
            _out(comparing.compare(args.first, args.second, nets), True, str)
            return 0
        if args.cmd == "describe":
            import tempfile
            from . import describe as described
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    result = described.describe_schematic(args.schematic, args.name or args.schematic.stem, list(args.band_mhz), args.output, Path(tmp))
                except described.DescribeError as e:
                    print(f"edaloop: {e}", file=sys.stderr)
                    return 1
            _out(result, True, str)
            return 0
        if args.cmd == "make":
            from . import boardgen
            result = boardgen.make(args.bench, args.output, args.footprints, args.quick)
            _out(result, True, str)
            return 0 if result.get("made") else 1
        if args.cmd == "fritzing":
            from . import fritzing
            result = fritzing.export(Project(Path(args.project)), args.output)
            _out(result, True, str)
            return 0 if not result["problems"] else 1
        if args.cmd == "export":
            from . import export
            result = export.run(Project(Path(args.project)), args.format, args.output)
            _out(result, True, str)
            return 0 if result["ok"] else 1
        if args.cmd == "mechanical":
            from . import mechanical
            p = Project(Path(args.project))
            result = mechanical.build(p, args.output, render=not args.no_render)
            _out(result, True, str)
            return 0 if not result.get("problems") else 1
        if args.cmd == "build":
            from . import boardgen
            result = boardgen.build(args.bench, args.output, args.footprints)
            _out(result, True, str)
            return 0
        p = Project(Path(args.project))
        if args.cmd == "ground":
            from . import ground
            _out(ground.pad_vias(p), True, str)
            return 0
        if args.cmd == "finish":
            from . import ground
            _out(ground.finish(p), True, str)
            return 0
        if args.cmd == "status":
            data = controller.status(p)
            _out(data, args.json, lambda x: _table(x["gate"], ["check", "stage", "state", "summary"])
                 + f"\n\nready to release: {x['ready_to_release']}")
        elif args.cmd == "next":
            _out(controller.next_action(p), True, str)
        elif args.cmd == "run":
            if args.check == "all":
                data = controller.run_all(p)
                _out(data, args.json, lambda x: _table(x, ["check", "status", "seconds", "summary"]))
            else:
                if args.check not in checks.REGISTRY:
                    raise ProjectError(f"no check named {args.check}")
                ev = checks.run_check(p, args.check)
                _out(ev.__dict__, args.json, lambda e: f"{e['check']}: {e['status']} - {e['summary']}" + "".join(
                    f"\n  {'waived ' if f['waived_by'] else ''}{f['severity']}: {f['message']}" for f in e['findings'][:25]))
                return 0 if ev.status in ("pass", "warn") else 1
        elif args.cmd == "stage":
            names = (p.section("stages") or {}).get(args.name)
            if not names:
                raise ProjectError(f"edaloop.yaml has no stage named {args.name}")
            data = [checks.run_check(p, n) for n in names]
            rows = [{"check": e.check, "status": e.status, "seconds": e.seconds, "summary": e.summary} for e in data]
            _out(rows, args.json, lambda x: _table(x, ["check", "status", "seconds", "summary"]))
            return 0 if all(e.status in ("pass", "warn") for e in data) else 1
        elif args.cmd == "route" and args.by_class:
            from . import route_classes
            rf = p.section("rf")
            cfg = p.section("route")
            data = route_classes.route(p.board, p.root / rf["chains"], p.section("power").get("rails", []),
                                       p.root / ".edaloop" / "route_classes", list(cfg.get("keep_nets") or ["GND"]),
                                       max_rise_c=float(p.section("power").get("max_rise_c", 10)),
                                       rf_target_ohm=float((rf.get("impedance") or {}).get("target", 50)),
                                       reference_layers=tuple(cfg.get("reference_layers") or ("In1.Cu",)),
                                       power_layers=tuple(cfg["power_layers"]) if cfg.get("power_layers") else None,
                                 layer_costs=cfg.get("layer_costs"))
            _out(data, True, str)
        elif args.cmd == "route":
            data = route.tournament(p, args.runs, args.passes)
            cols = ["name", "admissible", "unconnected", "errors", "rf_lost", "rf_added_mm", "ground_vias", "vias", "track_mm", "seconds"]
            _out(data, args.json, lambda x: _table([x["baseline"], *x["ranked"]], cols)
                 + f"\n\nfailed runs: {len(x['failed'])}\nrouters: {x['verdict']['reasons']}")
        elif args.cmd == "adopt":
            _out(route.adopt(p, args.rank), True, str)
        elif args.cmd == "review":
            _out(checks.review_pack(p), True, str)
        elif args.cmd == "release":
            data = checks.release(p)
            _out(data, True, str)
            return 0 if data["released"] else 1
        elif args.cmd == "report":
            from .report import write_report
            out = write_report(p, p.root / args.output)
            print(out)
        return 0
    except ProjectError as e:
        print(f"edaloop: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
