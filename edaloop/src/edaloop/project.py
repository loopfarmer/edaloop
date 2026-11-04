"""The project file: one edaloop.yaml beside the design."""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from .ledger import Ledger, Finding

FILE = "edaloop.yaml"


class ProjectError(Exception):
    pass


@dataclass
class Waiver:
    match: str
    by: str
    reason: str


class Project:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        cfg = self.root / FILE
        if not cfg.exists():
            raise ProjectError(f"no {FILE} in {self.root}")
        self.cfg_path = cfg
        self.cfg = yaml.safe_load(cfg.read_text()) or {}
        self.name = self.cfg.get("name", self.root.name)
        self.ledger = Ledger(self.root)
        self.work = self.root / ".edaloop" / "work"
        self.work.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path | None:
        v = self.cfg.get(key)
        return self.root / v if v else None

    @property
    def board(self) -> Path:
        b = self.path("board")
        if not b:
            raise ProjectError("edaloop.yaml names no board")
        return b

    @property
    def schematic(self) -> Path | None:
        return self.path("schematic")

    @property
    def project_file(self) -> Path:
        return self.board.with_suffix(".kicad_pro")

    def section(self, name: str) -> dict:
        return self.cfg.get(name) or {}

    def gate(self) -> list[str]:
        return list(self.cfg.get("gate") or [])

    def waivers(self) -> list[Waiver]:
        out = []
        for w in self.cfg.get("waivers") or []:
            if not w.get("by") or not w.get("reason"):
                raise ProjectError(f"waiver {w.get('match')!r} needs both 'by' and 'reason'")
            out.append(Waiver(w["match"], w["by"], w["reason"]))
        return out

    def apply_waivers(self, findings: list[Finding]) -> list[Finding]:
        waivers = self.waivers()
        for f in findings:
            for w in waivers:
                if fnmatch.fnmatch(f.id, w.match):
                    f.waived_by, f.waiver_reason = w.by, w.reason
                    break
        return findings
