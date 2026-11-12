"""The project file: one edaloop.yaml beside the design."""
from __future__ import annotations

import datetime
import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .ledger import Finding, Ledger

FILE = "edaloop.yaml"


class ProjectError(Exception):
    pass


@dataclass
class Waiver:
    """A person's decision to accept one finding. It names the check it's for, and it's bound either to the reading it accepted
    (`message`, the finding's text exactly) or, on purpose, to any reading (`any_reading: true`). With `until`, it lapses after that date.
    """
    match: str
    by: str
    reason: str
    message: str | None = None
    any_reading: bool = False
    until: datetime.date | None = None

    def applies(self, finding_id: str, message: str, today: datetime.date | None = None) -> bool:
        if not fnmatch.fnmatch(finding_id, self.match):
            return False
        if self.message is not None and message != self.message:
            return False                    # the reading changed since the person accepted it, so it needs their eyes again
        return self.until is None or (today or datetime.date.today()) <= self.until


GLOB = re.compile(r"[*?\[\]]")


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
        from .checks import REGISTRY  # here, because checks imports this module
        out = []
        for w in self.cfg.get("waivers") or []:
            match = str(w.get("match") or "")
            if not w.get("by") or not w.get("reason"):
                raise ProjectError(f"waiver {match!r} needs both 'by' and 'reason'")
            check, _, rest = match.partition(":")
            # A waiver is for findings of one named check. "*" or "rf_*:..." would accept findings nobody has read.
            if not rest or GLOB.search(check) or check not in REGISTRY:
                raise ProjectError(f"waiver {match!r} must start with one check's exact name and a colon, then the finding, "
                                   f"like 'drc:clearance*'. Checks: {', '.join(sorted(REGISTRY))}")
            message, any_reading = w.get("message"), w.get("any_reading") is True
            if (message is None) == (not any_reading):
                raise ProjectError(f"waiver {match!r} needs either 'message', the finding's text exactly as reported, so a different "
                                   f"reading needs a fresh decision, or 'any_reading: true' to accept whatever it reads. Not both")
            until = w.get("until")
            if until is not None and not isinstance(until, datetime.date):
                try:
                    until = datetime.date.fromisoformat(str(until))
                except ValueError:
                    raise ProjectError(f"waiver {match!r}: until must be a date, like 2027-03-31, not {until!r}") from None
            out.append(Waiver(match, str(w["by"]), str(w["reason"]), None if message is None else str(message), any_reading, until))
        return out

    def apply_waivers(self, findings: list[Finding]) -> list[Finding]:
        waivers = self.waivers()
        for f in findings:
            for w in waivers:
                if w.applies(f.id, f.message):
                    f.waived_by, f.waiver_reason = w.by, w.reason
                    break
        return findings
