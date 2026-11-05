"""MCP face of edaloop: the controller and sensors. Edits stay with the KiCad and circuitRF servers.

There is deliberately no tool to add a waiver or change a requirement: those are
a person's decisions, made in edaloop.yaml. No tool here writes edaloop.yaml, and
the two tools that take an output path (make_board, describe_design) are confined
to the server's working root. See _confine.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import checks, controller, route, toolchain
from .project import FILE, Project, ProjectError
from .scripts.kicad_pcb import ChainSpecError

mcp = MCPServer("edaloop")

# Folders no tool may write into: the ledger and its work files belong to the checks.
_PRIVATE = (".edaloop",)


def _project(project_dir: str | None) -> Project:
    """The project a tool acts on: the server's own, or a board built inside it. Never a folder elsewhere.

    Tools such as adopt_route, release and review_pack write into the project, so a project_dir that
    resolves outside the write root (after symlinks and `..`) is refused rather than acted on.
    """
    root = _write_root()
    if not project_dir:
        return Project(root)
    raw = Path(project_dir).expanduser()
    target = (raw if raw.is_absolute() else root / raw).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"refused: project_dir {project_dir!r} resolves to {target}, outside {root}. "
                        f"This server acts only on the project it was started for, and boards built inside it")
    return Project(target)


def _write_root() -> Path:
    """The one folder that output paths may land in: the project this server was started for, else its working directory.

    `edaloop init` starts the server with EDALOOP_PROJECT set to the project folder, so new boards and
    descriptions go inside the project the person wired up, and nowhere else on the machine.
    """
    return Path(os.environ.get("EDALOOP_PROJECT") or ".").resolve()


def _confine(path: str, what: str) -> Path:
    """Resolve `path` against the write root, following symlinks and `..`, and refuse anything that lands outside it.

    A relative path is taken from the write root, not from wherever the server happens to run.
    """
    root = _write_root()
    raw = Path(path).expanduser()
    target = (raw if raw.is_absolute() else root / raw).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"refused: {what} {path!r} resolves to {target}, outside {root}. "
                        f"This server writes only inside the project folder it was started for")
    if any(part in _PRIVATE for part in target.relative_to(root).parts):
        raise ToolError(f"refused: {what} {path!r} is inside .edaloop, which holds the evidence ledger")
    if target.name == FILE or raw.name == FILE:
        raise ToolError(f"refused: {what} {path!r} is {FILE}, which holds a person's requirements and waivers. No tool writes it")
    return target


def _tool(fn):
    """Register `fn` as a tool, turning an anticipated refusal into a clear tool error rather than a crash.

    ProjectError (including an unknown check name) and a malformed chain spec are things the agent can
    read and correct. Anything else still surfaces as a crash, never as a result that could read as a pass.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ProjectError, ChainSpecError) as e:
            raise ToolError(f"refused: {e}") from e
    return mcp.tool()(wrapper)


@_tool
def next_action(project_dir: str | None = None) -> dict:
    """The one thing to do next: run a stale check, fix named findings, or release. Call this first and after every edit."""
    return controller.next_action(_project(project_dir))


@_tool
def gate_status(project_dir: str | None = None) -> dict:
    """Every gated check's state, resolved against the files as they are now. Saved boards make old evidence stale."""
    return controller.status(_project(project_dir))


@_tool
def run_check(check: str, project_dir: str | None = None) -> dict:
    """Run one check and record the evidence. See list_checks. em_crosscheck can take many minutes.

    An unknown check name is refused with the list of checks there are, and nothing is recorded.
    """
    p = _project(project_dir)
    checks.require_check(check)
    return checks.run_check(p, check).__dict__


@_tool
def list_checks() -> dict:
    """Available checks, their stage, and what each one proves."""
    return controller.describe_checks()


@_tool
def route_board(runs: int = 3, passes: int = 20, project_dir: str | None = None) -> dict:
    """Route unlocked nets in isolated copies and rank them. The project board is not changed."""
    return route.tournament(_project(project_dir), runs, passes)


@_tool
def adopt_route(rank: int = 0, project_dir: str | None = None) -> dict:
    """Replace the project board with a ranked tournament result. Refuses any result that moved RF copper."""
    return route.adopt(_project(project_dir), rank)


@_tool
def make_board(description_file: str, output_dir: str, footprint_dirs: list[str] | None = None, quick: bool = False) -> dict:
    """Build a new board from a written description, through placement, routing and the checks.

    What it may write: a new project in `output_dir` and nothing else. `output_dir` must resolve, after
    symlinks and `..`, inside the project folder this server was started for (EDALOOP_PROJECT, else its
    working directory); a relative path is taken from there. It must be empty or absent, so a routed board
    is never overwritten, and it can't be edaloop.yaml or inside .edaloop. The new project gets its own
    edaloop.yaml, written from the description; no existing one is touched. This doesn't release anything.
    `quick` leaves out the slow three-solver EM check, for iterating, and the gate then reports it as missing.
    """
    from . import boardgen
    out = _confine(output_dir, "output_dir")
    if out == _write_root():
        raise ToolError("refused: output_dir is the project folder itself. Build into a new folder inside it")
    if out.exists() and not out.is_dir():
        raise ToolError(f"refused: output_dir {output_dir!r} is an existing file, not a folder")
    if out.is_dir() and any(out.iterdir()):
        raise ToolError(f"refused: output_dir {output_dir!r} isn't empty. Build into a new folder, so that no routed board is overwritten")
    return boardgen.make(Path(description_file), out, [Path(x) for x in footprint_dirs or []], quick)


@_tool
def describe_design(source_file: str, output_file: str, band_mhz: list[float], name: str | None = None) -> dict:
    """Write a board description from a .kicad_sch, or a .kicad_pcb if there's no schematic. Read back `required` and `assumed` before building.

    `required` lists what the source can't say and a build refuses without. `assumed` lists defaults, such as the stackup, that a person should check.

    What it may write: one new .yaml file, `output_file`, and nothing else. It must resolve, after symlinks
    and `..`, inside the project folder this server was started for (EDALOOP_PROJECT, else its working
    directory); a relative path is taken from there. Its folder must already exist. It can't be edaloop.yaml,
    can't be inside .edaloop, and can't be a file that already exists: a description a person has filled in
    is never overwritten, so write a new one under a new name.
    """
    import tempfile

    from . import describe as described
    out = _confine(output_file, "output_file")
    if out.suffix.lower() not in (".yaml", ".yml"):
        raise ToolError(f"refused: output_file {output_file!r} must be a .yaml file")
    if out.exists() or out.is_symlink():
        raise ToolError(f"refused: output_file {output_file!r} already exists, and a description may hold a person's answers. Name a new file")
    if not out.parent.is_dir():
        raise ToolError(f"refused: the folder for output_file {output_file!r} doesn't exist")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return described.describe_schematic(Path(source_file), name or Path(source_file).stem, list(band_mhz), out, Path(tmp))
        except described.DescribeError as e:
            return {"written": False, "error": str(e)}


@_tool
def make_mechanical(project_dir: str | None = None, render: bool = True) -> dict:
    """Write an OpenSCAD test mount and case for the project's board, and render them to STL if OpenSCAD is installed.

    Read `assumed` and `problems` before trusting the fit: part heights are rough figures unless the project gives them.
    """
    from . import mechanical
    return mechanical.build(_project(project_dir), render=render)


@_tool
def review_pack(project_dir: str | None = None) -> dict:
    """Export the schematic, renders, BOM and gate status for a person to review. It holds no fabrication files."""
    return checks.review_pack(_project(project_dir))


@_tool
def release(project_dir: str | None = None) -> dict:
    """Export fabrication files, only if every gated check holds on the current bytes. Otherwise lists the blockers."""
    return checks.release(_project(project_dir))


@_tool
def doctor() -> list[dict]:
    """Run every external tool and report what works on this machine."""
    return [p.__dict__ for p in toolchain.doctor()]


def main():
    mcp.run()


if __name__ == "__main__":
    main()
