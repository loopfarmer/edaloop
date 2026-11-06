"""Where each external tool lives, and whether it actually runs.

Nothing here assumes a tool works because a file exists: each probe runs the
tool. Paths can be overridden in ~/.config/edaloop/toolchain.yaml, one key per
tool name below.
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

HOME = Path.home()
CACHE = HOME / ".cache" / "edaloop"
_CONFIG = HOME / ".config" / "edaloop" / "toolchain.yaml"

DEFAULTS = {
    "kicad_cli": "/opt/homebrew/bin/kicad-cli",
    "kicad_python": "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    "java": "java",
    "freerouting_jar": str(HOME / ".kicad-mcp/freerouting.jar"),
    # 1.6.5 or later: earlier versions have no EMerge or compare mode.
    "circuitrf": str(CACHE / "circuitRF/src/Cli/bin/Release/net10.0/CircuitRF.Cli"),
    "dotnet_root": "/opt/homebrew/opt/dotnet/libexec",
    "ato": "ato",
    "orthoroute": str(HOME / "OrthoRoute"),
    "orthoroute_python": str(CACHE / "orthoroute-venv/bin/python"),
    "kicad_mcp": str(HOME / "mcp-servers/KiCAD-MCP-Server/dist/index.js"),
    "schematic_skill": str(HOME / ".claude/skills/kicad-drawn-schematic"),
    "lessons_skill": str(HOME / ".claude/skills/rf-board-design-lessons"),
    "rule_pack": str(HOME / "kicad_mcp_rules-1.0.0/rules"),
    "ngspice": "ngspice",
    "boardhand": str(Path(__file__).resolve().parents[3] / "boardhand/target/release/boardhand"),
    "pcbparts_db": str(HOME / "mcp-servers/pcbparts-mcp/data/components.db"),
    "boardfield": str(Path(__file__).resolve().parents[3] / "boardfield/target/release/boardfield"),
    "krt": str(CACHE / "ext/KiCadRoutingTools"),
    "openscad": "/Applications/OpenSCAD-2021.01.app/Contents/MacOS/OpenSCAD",
}


def paths() -> dict[str, str]:
    merged = dict(DEFAULTS)
    if _CONFIG.exists():
        merged.update({k: str(v) for k, v in (yaml.safe_load(_CONFIG.read_text()) or {}).items()})
    return merged


class ToolMissing(FileNotFoundError):
    """An external tool that isn't where edaloop looks for it. A FileNotFoundError, so existing handlers still catch it."""


def run(cmd, cwd=None, env=None, timeout=None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    try:
        return subprocess.run([str(c) for c in cmd], cwd=cwd, env=full_env, timeout=timeout,
                              capture_output=True, text=True, errors="replace")
    except FileNotFoundError as e:
        exe = str(cmd[0])
        if cwd is not None and not Path(cwd).is_dir():
            raise                       # the folder is what's missing, not the tool
        name = next((k for k, v in paths().items() if str(v) == exe), Path(exe).name)
        raise ToolMissing(f"{name} isn't installed where edaloop looks for it ({exe}). Run `edaloop doctor` "
                          f"for how to set it up, or set {name} in ~/.config/edaloop/toolchain.yaml") from e


def missing_for_make() -> list[dict]:
    """The tools `edaloop make` needs that aren't where edaloop looks, found without running anything.

    `doctor` runs each tool and says more. This is the quick look `make` takes before it places a part, so a
    missing router stops it at the start rather than after placement and grounding.
    """
    p = paths()
    wanted = [("kicad_cli", "kicad-cli", "placement, DRC and fabrication", "install KiCad 10"),
              ("kicad_python", "KiCad's Python", "board edits through pcbnew", "install KiCad 10"),
              ("circuitrf", "circuitRF", "the RF line width", "clone potatobeanradio/circuitRF to ~/.cache/edaloop/circuitRF, then dotnet build src/Cli -c Release"),
              ("krt", "KiCadRoutingTools", "power and signal routing", "clone it to ~/.cache/edaloop/ext and run build_router.py")]
    out = []
    for key, name, role, fix in wanted:
        where = Path(p[key]) / "rust_router/grid_router.so" if key == "krt" else Path(p[key])
        if not (where.exists() or (key == "kicad_cli" and shutil.which(p[key]))):
            out.append({"tool": name, "key": key, "for": role, "looked_at": str(where), "fix": fix})
    return out


def circuitrf_env() -> dict[str, str]:
    return {"DOTNET_ROOT": paths()["dotnet_root"], "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}


EM_SOLVER_NAMES = {"palace": "Palace", "openems": "openEMS", "emerge": "EMerge"}


def em_solvers_missing(kinds) -> list[str]:
    """The configured EM solvers circuitRF reports as not installed, by their names in edaloop.yaml. Read once per call, from
    `circuitrf solver list`, so a check can refuse before spending a solve on the ones that are there. A circuitRF that can't
    list its solvers is not taken to have them."""
    kinds = sorted({str(k).lower() for k in kinds})
    exe = paths()["circuitrf"]
    if not Path(exe).exists():
        raise ToolMissing(f"circuitRF is not built at {exe}. Run edaloop doctor for how to build it")
    r = run([exe, "solver", "list"], env=circuitrf_env())
    listed = r.stdout if r.returncode == 0 else ""
    return [k for k in kinds if not solver_listed(listed, EM_SOLVER_NAMES.get(k, k))]


def solver_listed(listing: str, name: str) -> bool:
    """Whether `circuitrf solver list` reports a solver as usable. It writes `Name: installed by circuitRF` for one it installed,
    `Name: found` for one on PATH or in another install, and `Name: not found` or `not installed` otherwise."""
    for line in listing.splitlines():
        if line.startswith(f"{name}:"):
            status = line[len(name) + 1:].strip().lower()
            return status.startswith(("installed", "found"))
    return False


def ato_shim() -> Path:
    return CACHE / "ato-shim"


def ato_env() -> dict[str, str]:
    # The installed atopile imports more_itertools.zip_equal, which version 11
    # removed, and its log database in ~/Library/Logs has a stale schema. Both
    # are worked around from here so the user's install stays untouched.
    log_dir = CACHE / "ato-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return {"PYTHONPATH": str(ato_shim()), "FBRK_LOG_DIR": str(log_dir)}


def freerouting_env() -> dict[str, str]:
    # Freerouting reports usage to a remote endpoint unless told not to.
    return {"FREEROUTING__USAGE_AND_DIAGNOSTIC_DATA__DISABLE_ANALYTICS": "true",
            "FREEROUTING__GUI__ENABLED": "false"}


@dataclass
class Probe:
    name: str
    role: str
    ok: bool
    detail: str
    fix: str = ""


def _first_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.strip() and "Fontconfig" not in ln]
    return lines[-1].strip()[:120] if lines else ""


def _exists(name, role, path, fix="") -> Probe:
    ok = Path(path).exists()
    return Probe(name, role, ok, str(path) if ok else f"not found at {path}", fix)


def doctor() -> list[Probe]:
    p = paths()
    out: list[Probe] = []

    r = run([p["kicad_cli"], "version"]) if shutil.which(p["kicad_cli"]) else None
    out.append(Probe("kicad-cli", "DRC, ERC and fabrication authority", bool(r and r.returncode == 0),
                     _first_line(r.stdout) if r else "not found"))

    r = run([p["kicad_python"], "-c", "import pcbnew;print(pcbnew.Version())"]) \
        if Path(p["kicad_python"]).exists() else None
    out.append(Probe("pcbnew", "board edits, Specctra round trip", bool(r and r.returncode == 0),
                     f"pcbnew {_first_line(r.stdout)}" if r and r.returncode == 0 else "KiCad Python not usable"))

    jar_ok = Path(p["freerouting_jar"]).exists() and shutil.which(p["java"]) is not None
    r = run([p["java"], "-jar", p["freerouting_jar"], "--version"], env=freerouting_env(), timeout=60) if jar_ok else None
    version = next((ln.split("INFO")[-1].strip() for ln in (r.stdout + r.stderr).splitlines() if "Freerouting v" in ln), "") if r else ""
    out.append(Probe("freerouting", "general autorouter", bool(version), version or "jar or java missing"))

    r = run([p["circuitrf"], "--version"], env=circuitrf_env()) if Path(p["circuitrf"]).exists() else None
    out.append(Probe("circuitrf", "impedance, S-parameters, HB, EM driver", bool(r and r.returncode == 0),
                     _first_line(r.stdout) if r and r.returncode == 0 else "not built",
                     "clone potatobeanradio/circuitRF to ~/.cache/edaloop/circuitRF, then dotnet build src/Cli -c Release"))
    if r and r.returncode == 0:
        s = run([p["circuitrf"], "solver", "list"], env=circuitrf_env()).stdout
        for solver in ("Palace", "openEMS", "EMerge", "Gmsh"):
            ok = solver_listed(s, solver)
            out.append(Probe(solver.lower(), "EM solver via circuitRF" if solver != "Gmsh" else "mesher",
                             ok, "installed, validated" if ok else "not installed",
                             f"circuitrf solver install {solver.lower()} --yes"))

    if shutil.which(p["ato"]):
        r = run([p["ato"], "--version"], env=ato_env())
        ok = r.returncode == 0
        out.append(Probe("atopile", "code-first capture, parametric picking", ok,
                         _first_line(r.stdout) if ok else "import fails",
                         "edaloop doctor --fix"))
    else:
        out.append(Probe("atopile", "code-first capture, parametric picking", False, "ato not found"))

    op = Path(p["orthoroute_python"])
    if Path(p["orthoroute"]).exists() and op.exists():
        r = run([op, "-c", "import numpy,scipy,psutil;print('deps ok')"])
        out.append(Probe("orthoroute", "large regular backplanes only", r.returncode == 0, p["orthoroute"],
                         "edaloop doctor --fix"))
    else:
        out.append(Probe("orthoroute", "large regular backplanes only", False,
                         "checkout or venv missing", "edaloop doctor --fix"))

    out.append(_exists("openscad", "test mounts and cases", p["openscad"], "install OpenSCAD, or set openscad in the toolchain config"))
    out.append(_exists("boardfield", "current and temperature fields", p["boardfield"], "cargo build --release in boardfield"))
    out.append(_exists("kicad-mcp", "agent edits to KiCad files", p["kicad_mcp"]))
    out.append(_exists("boardhand", "guarded agent edits, MIT", p["boardhand"]))
    krt_ok = (Path(p["krt"]) / "rust_router/grid_router.so").exists()
    out.append(Probe("kicadroutingtools", "default autorouter", krt_ok, p["krt"] if krt_ok else "not built",
                     "clone it to ~/.cache/edaloop/ext and run build_router.py"))
    out.append(_exists("schematic-skill", "drawn schematics", Path(p["schematic_skill"]) / "lib/sch_verify.py", "optional: a Claude skill that edaloop doesn't need to run"))
    out.append(_exists("lessons-skill", "project design knowledge", Path(p["lessons_skill"]) / "SKILL.md", "optional: a Claude skill that edaloop doesn't need to run"))
    out.append(_exists("rule-pack", "fabricator limits", Path(p["rule_pack"]) / "PROMPT.md"))

    try:
        import skrf
        out.append(Probe("scikit-rf", "network cascades", True, skrf.__version__))
    except ImportError:
        out.append(Probe("scikit-rf", "network cascades", False, "not installed", "uv sync"))

    ng = shutil.which(p["ngspice"])
    out.append(Probe("ngspice", "transient and noise, which circuitRF lacks", bool(ng), ng or "not installed",
                     "brew install ngspice"))
    return out


def fix() -> list[str]:
    """Set up the private shims doctor names. Writes only under ~/.cache/edaloop."""
    p = paths()
    done = []
    CACHE.mkdir(parents=True, exist_ok=True)
    ato = shutil.which(p["ato"])
    if ato and not (ato_shim() / "more_itertools").exists():
        py = Path(ato).parent / "python"
        r = run(["uv", "pip", "install", "-q", "--python", py, "--target", ato_shim(), "more_itertools<11"])
        done.append(f"ato shim: {'ok' if r.returncode == 0 else r.stderr.strip()}")
    if Path(p["orthoroute"]).exists() and not Path(p["orthoroute_python"]).exists():
        venv = Path(p["orthoroute_python"]).parent.parent
        run(["uv", "venv", "-q", "--python", "3.12", venv])
        r = run(["uv", "pip", "install", "-q", "--python", p["orthoroute_python"], "numpy", "scipy", "psutil"])
        done.append(f"orthoroute venv: {'ok' if r.returncode == 0 else r.stderr.strip()}")
    return done or ["nothing to do"]


def glob_one(pattern: str) -> str | None:
    hits = sorted(glob.glob(pattern))
    return hits[0] if hits else None
