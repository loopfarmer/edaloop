"""The evidence ledger: what was measured, on exactly which bytes.

Every check result is appended with the sha256 of each input it read. A result
counts only while those hashes still match, so saving the board silently
retires every claim made about the old one.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

PASS, WARN, FAIL, ERROR = "pass", "warn", "fail", "error"
FRESH_OK = {PASS, WARN}


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(Path(path).read_bytes())


@dataclass
class Finding:
    id: str
    message: str
    severity: str = "error"           # error | warning
    waived_by: str = ""
    waiver_reason: str = ""


@dataclass
class Evidence:
    check: str
    status: str
    summary: str
    inputs: dict[str, str]
    metrics: dict = field(default_factory=dict)
    findings: list[dict] = field(default_factory=list)
    tools: dict[str, str] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    seconds: float = 0.0
    when: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class Ledger:
    def __init__(self, root: Path):
        self.dir = Path(root) / ".edaloop"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "ledger.jsonl"

    def append(self, ev: Evidence) -> Evidence:
        ev.when = ev.when or time.strftime("%Y-%m-%dT%H:%M:%S")
        with self.path.open("a") as f:
            f.write(ev.to_json() + "\n")
        return ev

    def all(self) -> list[Evidence]:
        if not self.path.exists():
            return []
        return [Evidence(**json.loads(ln)) for ln in self.path.read_text().splitlines() if ln.strip()]

    def latest(self) -> dict[str, Evidence]:
        latest: dict[str, Evidence] = {}
        for ev in self.all():
            latest[ev.check] = ev
        return latest
