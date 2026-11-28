"""Where each external tool lives, and whether it actually runs.

Nothing here assumes a tool works because a file exists: each probe runs the
tool. Paths can be overridden in ~/.config/edaloop/toolchain.yaml, one key per
tool name below.
"""
from __future__ import annotations

import functools
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import yaml

HOME = Path.home()
CACHE = HOME / ".cache" / "edaloop"
_CONFIG = HOME / ".config" / "edaloop" / "toolchain.yaml"


def config_path() -> Path:
    """Toolchain YAML path: $EDALOOP_TOOLCHAIN_CONFIG when set, else ~/.config/edaloop/toolchain.yaml."""
    env = os.environ.get("EDALOOP_TOOLCHAIN_CONFIG")
    return Path(env).expanduser() if env else _CONFIG

DEFAULTS = {
    "kicad_cli": "/opt/homebrew/bin/kicad-cli",
    "kicad_python": "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3",
    # How board edits reach KiCad: "swig" runs kicad_ops.py under kicad_python with KiCad 10's pcbnew module; "ipc" runs
    # kicad_ipc_ops.py under ipc_python through KiCad 11's IPC API, which replaces pcbnew. "auto" takes swig wherever
    # kicad_python still imports pcbnew, and ipc otherwise.
    "kicad_backend": "auto",
    # A Python with kicad-python 0.10+ and shapely; edaloop's own by default.
    "ipc_python": sys.executable,
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
    cfg = config_path()
    if cfg.exists():
        merged.update({k: str(v) for k, v in (yaml.safe_load(cfg.read_text()) or {}).items()})
    return merged


class ToolMissing(FileNotFoundError):
    """An external tool that isn't where edaloop looks for it. A FileNotFoundError, so existing handlers still catch it."""


def json_lines(stdout: str) -> list:
    """The JSON objects a pcbnew script printed, in order. KiCad's own chatter shares the stream, and SWIG's exit-time leak
    warnings are written from C, so one can land in front of ours on the same line, as `swig/pytho{"removed": 1169, ...}`:
    each line is read from its first `{`, not only when it starts with one."""
    out = []
    for line in stdout.splitlines():
        i = line.find("{")
        if i < 0:
            continue
        try:
            out.append(json.loads(line[i:]))
        except json.JSONDecodeError:
            continue
    return out


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


SWIG_OPS = Path(__file__).with_name("kicad_ops.py")
IPC_OPS = Path(__file__).with_name("kicad_ipc_ops.py")
BACKENDS = ("swig", "ipc")


@functools.lru_cache(maxsize=None)
def _imports(python: str, module: str) -> bool:
    """Whether `python` imports `module`. Run once per pair: every board edit asks."""
    if not (Path(python).exists() or shutil.which(python)):
        return False
    try:
        return run([python, "-c", f"import {module}"], timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def kicad_major(cli: str | None = None) -> int:
    """Major version of `kicad-cli` (e.g. 10 for 10.0.x / 10.99.x, 11 for 11.x), falling back to 10."""
    exe = cli or paths()["kicad_cli"]
    if not shutil.which(exe):
        return 10
    try:
        r = run([exe, "--version"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 10
    if r.returncode != 0:
        return 10
    for token in (r.stdout + "\n" + r.stderr).split():
        head = token.split(".", 1)[0]
        if head.isdigit():
            return int(head)
    return 10


def kicad_backend() -> str:
    """"swig" or "ipc": which way this install's board edits reach KiCad. See `kicad_backend` in DEFAULTS."""
    p = paths()
    want = p.get("kicad_backend", "auto")
    if want in BACKENDS:
        return want
    if want != "auto":
        raise ValueError(f"kicad_backend is {want!r} in {config_path()}; it takes auto, swig or ipc")
    if has_api_server(p["kicad_cli"]) and _imports(p["ipc_python"], "kipy"):
        return "ipc"
    return "swig" if _imports(p["kicad_python"], "pcbnew") else "ipc"


def kicad_op(*args, timeout=600) -> subprocess.CompletedProcess:
    """Run one board edit, `op arg...`, on whichever backend this install has. Both print one JSON line and exit
    non-zero when the edit didn't happen."""
    p = paths()
    if kicad_backend() == "swig":
        return run([p["kicad_python"], SWIG_OPS, *args], timeout=timeout)
    env = {"EDALOOP_KICAD_CLI": p["kicad_cli"]}
    if os.environ.get("EDALOOP_IPC_SOCKET"):
        env["EDALOOP_IPC_SOCKET"] = os.environ["EDALOOP_IPC_SOCKET"]
    return run([p["ipc_python"], IPC_OPS, *args], env=env, timeout=timeout)


_IPC_READY_PREFIX = "KiCad API server listening at "


@contextmanager
def ipc_session(timeout_s: float = 30.0):
    """Share one headless `kicad-cli api-server` across multiple `kicad_op` calls when using the KiCad 11 IPC backend.

    Re-entrant: nested sessions reuse the outer `$EDALOOP_IPC_SOCKET`. No-op under SWIG or when `api-server`/`kipy` aren't usable.
    """
    if os.environ.get("EDALOOP_IPC_SOCKET") or kicad_backend() != "ipc":
        yield None
        return
    p = paths()
    if not _imports(p["ipc_python"], "kipy") or not has_api_server(p["kicad_cli"]):
        yield None
        return
    sock_dir = Path(tempfile.gettempdir()) / "kicad" / "edaloop-ipc"
    sock_dir.mkdir(parents=True, exist_ok=True)
    sock_path = sock_dir / f"edaloop-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    if sock_path.exists():
        sock_path.unlink()
    sock_url = f"ipc://{sock_path}"
    proc = subprocess.Popen(
        [p["kicad_cli"], "api-server", "--socket", str(sock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    ready = threading.Event()
    logs: list[str] = []

    def _drain():
        if proc.stdout is None:
            return
        for line in proc.stdout:
            text = line.rstrip("\n")
            logs.append(text)
            if _IPC_READY_PREFIX in text:
                ready.set()

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()
    prev_sock = os.environ.get("EDALOOP_IPC_SOCKET")
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"kicad-cli api-server exited ({proc.returncode}): {' | '.join(logs[-5:])}")
            if sock_path.exists():
                probe = run(
                    [p["ipc_python"], "-c", f"from kipy import KiCad; k=KiCad(socket_path={sock_url!r}, timeout_ms=500); k.ping(); k.close()"],
                    timeout=5,
                )
                if probe.returncode == 0:
                    break
            time.sleep(0.05)
        else:
            raise TimeoutError(f"Timed out waiting for kicad-cli api-server socket at {sock_path}")
        os.environ["EDALOOP_IPC_SOCKET"] = sock_url
        yield sock_url
    finally:
        if prev_sock is None:
            os.environ.pop("EDALOOP_IPC_SOCKET", None)
        else:
            os.environ["EDALOOP_IPC_SOCKET"] = prev_sock
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        try:
            sock_path.unlink(missing_ok=True)
        except OSError:
            pass


def missing_for_make() -> list[dict]:
    """The tools `edaloop make` needs that aren't where edaloop looks, found without running anything.

    `doctor` runs each tool and says more. This is the quick look `make` takes before it places a part, so a
    missing router stops it at the start rather than after placement and grounding.
    """
    p = paths()
    edits = (("kicad_python", "KiCad's Python", "board edits through pcbnew", "install KiCad 10") if kicad_backend() == "swig" else
             ("ipc_python", "kicad-python", "board edits through KiCad 11's IPC API", IPC_FIX))
    wanted = [("kicad_cli", "kicad-cli", "placement, DRC and fabrication", "install KiCad 10 or 11"), edits,
              ("circuitrf", "circuitRF", "the RF line width", "clone potatobeanradio/circuitRF to ~/.cache/edaloop/circuitRF, then dotnet build src/Cli -c Release"),
              ("krt", "KiCadRoutingTools", "power and signal routing", "clone it to ~/.cache/edaloop/ext and run build_router.py")]
    out = []
    for key, name, role, fix in wanted:
        where = Path(p[key]) / "rust_router/grid_router.so" if key == "krt" else Path(p[key])
        found = where.exists() or (key == "kicad_cli" and shutil.which(p[key]))
        if key == "ipc_python":
            found = _imports(p[key], "kipy")
        if not found:
            out.append({"tool": name, "key": key, "for": role, "looked_at": str(where), "fix": fix})
    return out


IPC_FIX = ("install kicad-python 0.10 or later into edaloop's environment (uv pip install kicad-python, or from a "
           "gitlab.com/kicad/code/kicad-python checkout until 0.10 is on PyPI), or set ipc_python to a Python that has it")


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
    missing = [k for k in kinds if not solver_listed(listed, EM_SOLVER_NAMES.get(k, k))]
    # Palace solves on a mesh it doesn't make: without Gmsh, circuitRF refuses the run after the other solvers have spent theirs.
    if "palace" in kinds and not solver_listed(listed, "Gmsh"):
        missing.append("gmsh")
    return missing


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

    out.append(_board_edits(p))

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


def _board_edits(p) -> Probe:
    """The backend board edits will take, and whether it runs. Named pcbnew, as it was before KiCad 11 had another way."""
    role = "board edits, Specctra round trip"
    try:
        backend = kicad_backend()
    except ValueError as e:
        return Probe("pcbnew", role, False, str(e))
    if backend == "swig":
        r = run([p["kicad_python"], "-c", "import pcbnew;print(pcbnew.Version())"]) if Path(p["kicad_python"]).exists() else None
        ok = bool(r and r.returncode == 0)
        return Probe("pcbnew", role, ok, f"pcbnew {_first_line(r.stdout)}, through KiCad's Python" if ok else "KiCad Python not usable",
                     "" if ok else "install KiCad 10, or KiCad 11 with kicad-python for the IPC API")
    if not _imports(p["ipc_python"], "kipy"):
        return Probe("pcbnew", role, False, f"KiCad's Python has no pcbnew, and {p['ipc_python']} has no kicad-python", IPC_FIX)
    if not has_api_server(p["kicad_cli"]):
        return Probe("pcbnew", role, False, "this kicad-cli has no api-server: the IPC API runs headless from KiCad 11",
                     "install KiCad 11, or KiCad 10 for pcbnew")
    v = run([p["ipc_python"], "-c", "import importlib.metadata as m;print(m.version('kicad-python'))"])
    still = _unported()
    detail = f"IPC API through kicad-cli api-server, kicad-python {_first_line(v.stdout)}"
    if still:
        detail += f"; {', '.join(still)} still need{'s' if len(still) == 1 else ''} KiCad 10"
    return Probe("pcbnew", role, True, detail)


def at_poses(board, folder) -> Path:
    """The board as a tool that finds a footprint's place only by its (at ...) can read it: KiCadRoutingTools, and
    circuitRF's KiCad import. Given a KiCad 11 board, which writes each pose as (transform ...), both put every pad in
    its footprint's own frame, KiCadRoutingTools routing nets it can't join and circuitRF reading lines with no lands on
    them, and say nothing. Such a board goes to them as a copy in `folder`, named as the original, its poses written
    back as (at ...) and its project and rules beside it. Any other board, or one that isn't there, is passed as it is."""
    from .scripts.kicad_pcb import poses_as_at
    board = Path(board)
    if not board.is_file():
        return board
    text = board.read_text()
    if "(transform" not in text:
        return board
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    copy = folder / board.name
    copy.write_text(poses_as_at(text))
    for ext in (".kicad_pro", ".kicad_dru", ".kicad_prl"):
        if board.with_suffix(ext).exists():
            shutil.copy(board.with_suffix(ext), copy.with_suffix(ext))
    return copy


def has_api_server(cli: str) -> bool:
    """Whether this kicad-cli serves the IPC API headless, as KiCad 11's does. KiCad 10's answers an unknown subcommand with
    its general help and exit status 0, so the status alone doesn't say."""
    if not shutil.which(cli):
        return False
    try:
        r = run([cli, "api-server", "--help"], timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and "IPC API server" in r.stdout


def _unported() -> list[str]:
    """The ops kicad_ipc_ops.py refuses, read from its source: it imports kicad-python, which this process may not have."""
    import ast
    for node in ast.parse(IPC_OPS.read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "UNPORTED":
            return sorted(ast.literal_eval(node.value))
    return []


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
