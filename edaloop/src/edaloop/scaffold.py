"""`edaloop init`: wire a design folder into one agent loop.

Writes the project file, the MCP composition, a hook and the skill. It never
overwrites an existing file, and reports what it left alone.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

from . import toolchain

SKILL = Path(__file__).parent / "agent" / "SKILL.md"


def _gate(has_sch: bool, has_chains: bool, has_ato: bool, has_rules: bool = False) -> list[str]:
    gate = ["capture"] if has_ato else []
    gate += ["project_files"] + (["erc"] if has_sch else []) + ["drc"] + (["rules"] if has_rules else [])
    if has_chains:
        gate += ["rf_path", "rf_preserved", "rf_reference", "impedance"]
    return gate + ["requirements"]


def project_yaml(root: Path) -> dict:
    boards = sorted(root.glob("*.kicad_pcb")) or sorted(root.glob("layout/**/*.kicad_pcb"))
    board = boards[0] if boards else None
    sch = board.with_suffix(".kicad_sch") if board else None
    has_sch = bool(sch and sch.exists())
    chains = sorted(root.glob("chains*.yaml"))
    has_ato = (root / "ato.yaml").exists()
    cfg: dict = {"name": root.name}
    if board:
        cfg["board"] = str(board.relative_to(root))
    if has_sch:
        cfg["schematic"] = str(sch.relative_to(root))
    if has_ato:
        cfg["capture"] = {"kind": "atopile", "build": "default"}
    if chains:
        cfg["rf"] = {"chains": chains[0].name, "max_over_pct": 5, "impedance": {"target": 50, "tol": 10}}
    pack = Path(toolchain.paths()["rule_pack"]).expanduser()
    if (pack / "manufacture/summit_standard.yaml").exists():
        cfg["rules"] = {"pack": str(pack), "profile": "manufacture/summit_standard.yaml"}
    cfg["requirements"] = [{"id": "R1", "text": "No DRC violations", "metric": "drc.violations", "max": 0},
                           {"id": "R2", "text": "Board fully connected", "metric": "drc.unconnected", "max": 0}]
    cfg["gate"] = _gate(has_sch, bool(chains), has_ato, "rules" in cfg)
    cfg["waivers"] = []
    return cfg


def mcp_config(root: Path) -> dict:
    p = toolchain.paths()
    servers: dict = {"edaloop": {"command": sys.executable, "args": ["-m", "edaloop.mcp_server"],
                                 "env": {"EDALOOP_PROJECT": str(root)}}}
    if Path(p["kicad_mcp"]).exists():
        servers["kicad"] = {"command": "node", "args": [p["kicad_mcp"]], "env": {"KICAD_BACKEND": "auto"}}
    if Path(p["boardhand"]).exists():
        servers["boardhand"] = {"command": p["boardhand"], "args": []}
    if Path(p["circuitrf"]).exists():
        servers["circuitrf"] = {"command": p["circuitrf"], "args": ["serve", "--root", str(root)],
                                "env": toolchain.circuitrf_env()}
    return {"mcpServers": servers}


HOOKS = {"hooks": {"PostToolUse": [{
    "matcher": "mcp__kicad__.*|Edit|Write|Bash",
    "hooks": [{"type": "command", "command": f"{sys.executable} -m edaloop.cli hook"}],
}]}}

CLAUDE_MD = """# Design loop

Read the `edaloop` skill before changing this design. Call `next_action` first and after every edit.

- The KiCad board is the source of truth for geometry. circuitRF reads it and never writes it back.
- Requirements and waivers in edaloop.yaml are a person's decisions. Report them. Do not edit them.
- A board is finished only when `release` succeeds.
"""


def init(root: Path) -> dict:
    root = Path(root).resolve()
    wrote, kept = [], []

    def put(rel: str, text: str):
        path = root / rel
        if path.exists():
            kept.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        wrote.append(rel)

    put("edaloop.yaml", yaml.safe_dump(project_yaml(root), sort_keys=False))
    put(".mcp.json", json.dumps(mcp_config(root), indent=2) + "\n")
    put(".claude/settings.json", json.dumps(HOOKS, indent=2) + "\n")
    put("CLAUDE.md", CLAUDE_MD)
    dest = root / ".claude/skills/edaloop/SKILL.md"
    if dest.exists():
        kept.append(".claude/skills/edaloop/SKILL.md")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SKILL, dest)
        wrote.append(".claude/skills/edaloop/SKILL.md")
    out = {"wrote": wrote, "left_alone": kept}
    # A Python chain spec isn't picked up any more. Say so rather than leave the RF checks out without a word.
    old = [x.name for x in sorted(root.glob("chains*.py")) if not x.with_suffix(".yaml").exists()]
    if old:
        out["convert"] = [f"edaloop convert-chains {name}" for name in old]
    return out


def hook_message(root: Path, hook_input: dict) -> dict | None:
    """After a tool call that changed the design, tell the agent what the gate now needs."""
    from . import controller
    from .project import Project
    tool = hook_input.get("tool_name", "")
    target = str((hook_input.get("tool_input") or {}).get("file_path", ""))
    relevant = tool.startswith("mcp__kicad__") or target.endswith((".kicad_pcb", ".kicad_sch", ".ato", "edaloop.yaml"))
    if not relevant or not (root / "edaloop.yaml").exists():
        return None
    p = Project(root)
    state = controller.status(p)
    stale = [r["check"] for r in state["gate"] if r["state"] == "stale"]
    if not stale:
        return None
    action = controller.next_action(p)
    text = f"edaloop: this change made {', '.join(stale)} stale. Next: {action.get('command') or action.get('then')}"
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def convert_chains(src: Path, out: Path | None = None) -> dict:
    """Turn a Python chain spec (CHAINS, WIDTH, NET_WIDTH) into the YAML one edaloop reads.

    This runs the Python file, once, because a person asked for it by name. Nothing else in
    edaloop runs a chain spec. The YAML is read back and must say the same thing, and an
    existing file is never overwritten.
    """
    from .scripts.kicad_pcb import dump_chains, load_chains
    src = Path(src)
    out = Path(out) if out else src.with_suffix(".yaml")
    if out.exists():
        raise FileExistsError(f"{out} already exists. Move it, or name another with -o")
    ns: dict = {}
    exec(compile(src.read_text(), str(src), "exec"), ns)        # the one place, and only on request
    chains = [(str(net), (str(a[0]), str(a[1])), (str(b[0]), str(b[1]))) for net, a, b in ns["CHAINS"]]
    width, net_width = float(ns.get("WIDTH", 0.3)), {str(k): float(v) for k, v in (ns.get("NET_WIDTH") or {}).items()}
    out.write_text(dump_chains(chains, width, net_width, f"Converted from {src.name} by edaloop convert-chains."))
    if load_chains(out) != (chains, width, net_width):
        out.unlink()
        raise RuntimeError(f"the YAML written for {src.name} didn't read back the same, so it was removed")
    return {"written": str(out), "chains": len(chains),
            "next": f"set rf.chains to {out.name} in edaloop.yaml, then delete {src.name}"}
