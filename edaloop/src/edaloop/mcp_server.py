"""MCP face of edaloop: the controller and sensors. Edits stay with the KiCad and circuitRF servers.

There is deliberately no tool to add a waiver or change a requirement: those are
a person's decisions, made in edaloop.yaml.
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import checks, controller, route, toolchain
from .project import Project

mcp = MCPServer("edaloop")


def _project(project_dir: str | None) -> Project:
    return Project(Path(project_dir or os.environ.get("EDALOOP_PROJECT", ".")))


@mcp.tool()
def next_action(project_dir: str | None = None) -> dict:
    """The one thing to do next: run a stale check, fix named findings, or release. Call this first and after every edit."""
    return controller.next_action(_project(project_dir))


@mcp.tool()
def gate_status(project_dir: str | None = None) -> dict:
    """Every gated check's state, resolved against the files as they are now. Saved boards make old evidence stale."""
    return controller.status(_project(project_dir))


@mcp.tool()
def run_check(check: str, project_dir: str | None = None) -> dict:
    """Run one check and record the evidence. See list_checks. em_crosscheck can take many minutes."""
    ev = checks.run_check(_project(project_dir), check)
    return ev.__dict__


@mcp.tool()
def list_checks() -> dict:
    """Available checks, their stage, and what each one proves."""
    return controller.describe_checks()


@mcp.tool()
def route_board(runs: int = 3, passes: int = 20, project_dir: str | None = None) -> dict:
    """Route unlocked nets in isolated copies and rank them. The project board is not changed."""
    return route.tournament(_project(project_dir), runs, passes)


@mcp.tool()
def adopt_route(rank: int = 0, project_dir: str | None = None) -> dict:
    """Replace the project board with a ranked tournament result. Refuses any result that moved RF copper."""
    return route.adopt(_project(project_dir), rank)


@mcp.tool()
def make_board(description_file: str, output_dir: str, footprint_dirs: list[str] | None = None, quick: bool = False) -> dict:
    """Build a new board from a written description, through placement, routing and the checks.

    The output folder must be empty or absent: a routed board is never overwritten. This doesn't release anything.
    `quick` leaves out the slow three-solver EM check, for iterating, and the gate then reports it as missing.
    """
    from . import boardgen
    return boardgen.make(Path(description_file), Path(output_dir), [Path(x) for x in footprint_dirs or []], quick)


@mcp.tool()
def describe_design(source_file: str, output_file: str, band_mhz: list[float], name: str | None = None) -> dict:
    """Write a board description from a .kicad_sch, or a .kicad_pcb if there's no schematic. Read back `required` and `assumed` before building.

    `required` lists what the source can't say and a build refuses without. `assumed` lists defaults, such as the stackup, that a person should check.
    """
    import tempfile

    from . import describe as described
    with tempfile.TemporaryDirectory() as tmp:
        try:
            return described.describe_schematic(Path(source_file), name or Path(source_file).stem, list(band_mhz), Path(output_file), Path(tmp))
        except described.DescribeError as e:
            return {"written": False, "error": str(e)}


@mcp.tool()
def make_mechanical(project_dir: str | None = None, render: bool = True) -> dict:
    """Write an OpenSCAD test mount and case for the project's board, and render them to STL if OpenSCAD is installed.

    Read `assumed` and `problems` before trusting the fit: part heights are rough figures unless the project gives them.
    """
    from . import mechanical
    return mechanical.build(_project(project_dir), render=render)


@mcp.tool()
def review_pack(project_dir: str | None = None) -> dict:
    """Export the schematic, renders, BOM and gate status for a person to review. It holds no fabrication files."""
    return checks.review_pack(_project(project_dir))


@mcp.tool()
def release(project_dir: str | None = None) -> dict:
    """Export fabrication files, only if every gated check holds on the current bytes. Otherwise lists the blockers."""
    return checks.release(_project(project_dir))


@mcp.tool()
def doctor() -> list[dict]:
    """Run every external tool and report what works on this machine."""
    return [p.__dict__ for p in toolchain.doctor()]


def main():
    mcp.run()


if __name__ == "__main__":
    main()
