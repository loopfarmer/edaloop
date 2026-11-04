"""Older and foreign formats for a finished board, for tools that can't read KiCad 10's files. These aren't fabrication files, and the gate doesn't hold them back."""
from __future__ import annotations

from pathlib import Path

from . import export_kicad6, toolchain
from .project import Project

FORMATS = ("kicad6", "eagle")
EAGLE = Path(__file__).with_name("export_eagle.py")


def run(p: Project, formats: list[str] | None = None, out: Path | None = None) -> dict:
    """Write each format into its own folder, read it back, and report. A format that fails its read-back says so, and its files aren't to be trusted."""
    root = out or p.root / "export"
    paths = toolchain.paths()
    result: dict = {"folder": str(root), "formats": {}}
    for name in formats or FORMATS:
        dst = root / name
        if name == "kicad6":
            if not p.schematic:
                result["formats"][name] = {"ok": False, "problems": ["the project names no schematic, and this format converts one"]}
                continue
            done = export_kicad6.export(str(p.root), str(p.board), str(p.schematic), str(dst), cli=paths["kicad_cli"])
            result["formats"][name] = {"ok": not done["problems"], **done}
        elif name == "eagle":
            cmd = [paths["kicad_python"], EAGLE, p.board, p.board.stem, dst, paths["kicad_cli"]]
            if p.schematic:
                cmd.append(p.schematic)
            r = toolchain.run(cmd, timeout=1800)
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "export_report.txt").write_text(r.stdout)      # KiCad's bindings print debug noise to stderr, which only matters on a crash
            failed = [x.split("FAILED: ", 1)[1] for x in r.stdout.splitlines() if "FAILED: " in x]
            ok = r.returncode == 0 and any(x.strip() == "OK" for x in r.stdout.splitlines())
            result["formats"][name] = {"ok": ok, "problems": failed or ([] if ok else [(r.stdout + r.stderr).strip()[-300:]]), "report": "export_report.txt"}
        else:
            raise ValueError(f"no such format: {name}. There are {list(FORMATS)}")
    result["ok"] = all(x["ok"] for x in result["formats"].values())
    return result
