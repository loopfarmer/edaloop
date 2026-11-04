"""SPICE benches built from the real schematic, with every model accounted for.

The netlist comes from KiCad, so a bench can't drift from the design. Each part
must resolve to a model, and each model records where it came from. A part with
no model fails the bench, since silently dropping it would simulate some other
circuit. `open` is allowed, but it needs a stated reason, which appears in the
evidence.

circuitRF covers S-parameters and harmonic balance. This covers what it lacks:
DC bias, transient and noise.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import toolchain

LIBRARY = Path(__file__).parent / "spice_models"
_VALUE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(p|n|u|µ|m|k|K|M|R|G)?(\d*)\s*(F|H|Ω|ohm|Ohm)?\b", re.I)
_SPICE_SUFFIX = {"p": "p", "n": "n", "u": "u", "µ": "u", "m": "m", "k": "k", "K": "k", "M": "Meg", "G": "G", "R": ""}
_MEAS = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*=\s*([-+0-9.eE]+)", re.I | re.M)
_FAILED = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*=?\s*failed", re.I | re.M)
ROUND_TRIP_ZERO_OHM = 1e-3      # a 0R link stays a real element so that its nets stay distinct


class BenchError(Exception):
    pass


@dataclass
class Part:
    ref: str
    value: str
    mpn: str
    pins: dict[str, str]      # pin number -> net


def read_netlist(schematic: Path, out: Path) -> list[Part]:
    """The design as KiCad exports it, so that a bench can't disagree with the schematic."""
    cli = toolchain.paths()["kicad_cli"]
    r = toolchain.run([cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", out, schematic], timeout=300)
    if r.returncode != 0 or not out.exists():
        raise BenchError(f"netlist export failed: {r.stderr.strip()[-300:]}")
    root = ET.parse(out).getroot()
    parts: dict[str, Part] = {}
    for comp in root.find("components"):
        fields = {f.get("name"): (f.text or "") for f in (comp.find("fields") if comp.find("fields") is not None else [])}
        mpn = fields.get("MPN") or fields.get("Partnumber") or fields.get("Manufacturer Part Number") or comp.findtext("value") or ""
        parts[comp.get("ref")] = Part(comp.get("ref"), comp.findtext("value") or "", mpn, {})
    for net in root.find("nets"):
        name = net.get("name").lstrip("/")
        for node in net:
            parts[node.get("ref")].pins[node.get("pin")] = name
    return list(parts.values())


def parse_value(text: str) -> str | None:
    """A KiCad value as a SPICE number: 100nF to 100n, 1k0 to 1.0k, 4R7 to 4.7, 0R to 0."""
    m = _VALUE.match(text)
    if not m:
        return None
    whole, prefix, frac = m.group(1), m.group(2), m.group(3)
    number = f"{whole}.{frac}" if frac else whole
    if prefix is None:
        return number
    key = prefix if prefix in _SPICE_SUFFIX else prefix.lower() if prefix.lower() in _SPICE_SUFFIX else "R"
    return number + _SPICE_SUFFIX[key]


def load_registry(extra: Path | None = None) -> dict:
    """Vendor and behavioural models, by part number. A project's own entries win."""
    registry = yaml.safe_load((LIBRARY / "index.yaml").read_text())["models"]
    for entry in registry.values():
        entry["file"] = str(LIBRARY / Path(entry["file"]).expanduser())   # an absolute path stays as it is
    if extra and extra.exists():
        for mpn, entry in (yaml.safe_load(extra.read_text()) or {}).get("models", {}).items():
            entry["file"] = str(extra.parent / entry["file"])
            registry[mpn] = entry
    return registry


def _node(net: str) -> str:
    return "0" if net.upper() in ("GND", "GROUND", "0") else re.sub(r"[^A-Za-z0-9_]", "_", net)


def resolve(part: Part, overrides: dict, registry: dict) -> dict:
    """The model for one part. Order: explicit override, registry by part number, then value."""
    rule = overrides.get(part.ref)
    if rule is None:
        rule = next((v for k, v in overrides.items() if fnmatch.fnmatch(part.value, k)), None)
    if rule is not None:
        kind = rule["kind"]
        if kind == "open":
            if not rule.get("reason"):
                raise BenchError(f"{part.ref}: an open model needs a reason")
            return {"kind": "open", "provenance": "excluded", "note": rule["reason"]}
        if kind == "value":
            return {"kind": "value", "value": str(rule["value"]), "provenance": "stated", "note": rule.get("reason", "")}
        if kind == "subckt":
            return {**rule, "provenance": rule.get("provenance", "project")}
        raise BenchError(f"{part.ref}: unknown model kind {kind}")
    if part.mpn in registry:
        return {"kind": "subckt", **registry[part.mpn]}
    if part.ref[0] in "RCL":
        value = parse_value(part.value)
        if value is not None:
            return {"kind": "value", "value": value, "provenance": "ideal, from schematic value", "note": ""}
    return {"kind": "missing", "provenance": "none", "note": ""}


def build_deck(title: str, parts: list[Part], models: dict[str, dict], bench: dict) -> str:
    lines = [f"* {title}", ".option noacct"]
    includes = sorted({m["file"] for m in models.values() if m["kind"] == "subckt"})
    lines += [f'.include "{f}"' for f in includes]
    for part in parts:
        model = models[part.ref]
        if model["kind"] == "open":
            lines.append(f"* {part.ref} excluded: {model['note']}")
        elif model["kind"] == "value":
            nodes = [_node(part.pins[k]) for k in sorted(part.pins)]
            if len(nodes) != 2:
                raise BenchError(f"{part.ref}: a value model needs 2 connected pins, found {len(nodes)}")
            value = model["value"]
            if part.ref[0] == "R" and re.fullmatch(r"0(\.0*)?", value):
                value = str(ROUND_TRIP_ZERO_OHM)
            lines.append(f"{part.ref[0]}{part.ref} {nodes[0]} {nodes[1]} {value}")
        else:
            order = model["pins"]            # the model's pin order, as design pin names
            missing = [p for p in order if str(p) not in part.pins]
            if missing:
                raise BenchError(f"{part.ref}: pins {missing} of its model are not in the design")
            nodes = " ".join(_node(part.pins[str(p)]) for p in order)
            if model.get("device"):
                lines.append(f"{model['device']}{part.ref} {nodes} {model['model']}")
            else:
                lines.append(f"X{part.ref} {nodes} {model['subckt']}")
    lines += [str(x) for x in bench.get("stimulus", [])]
    lines += [str(x) for x in bench.get("analysis", [])]
    lines += [str(x) for x in bench.get("measure", [])]
    lines.append(".end")
    return "\n".join(lines) + "\n"


def run_deck(deck: str, workdir: Path, name: str, timeout: int = 300) -> dict:
    """Run ngspice in batch mode. Returns measurements, or raises if it can't be trusted."""
    workdir.mkdir(parents=True, exist_ok=True)
    cir = workdir / f"{name}.cir"
    log = workdir / f"{name}.log"
    cir.write_text(deck)
    r = toolchain.run([toolchain.paths()["ngspice"], "-b", "-o", log, cir], cwd=workdir, timeout=timeout)
    text = log.read_text(errors="replace") if log.exists() else ""
    fatal = [ln for ln in text.splitlines() if re.search(r"^\s*(error|.*fatal|.*singular matrix|.*no convergence|.*timestep too small)", ln, re.I)]
    if r.returncode != 0 or fatal:
        raise BenchError(f"ngspice failed (exit {r.returncode}): {' | '.join(fatal[:3]) or r.stderr.strip()[-200:]}")
    values = {m.group(1).lower(): float(m.group(2)) for m in _MEAS.finditer(text)
              if re.fullmatch(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", m.group(2))}
    failed = [m.group(1).lower() for m in _FAILED.finditer(text)]
    return {"values": values, "failed": failed, "log": str(log), "deck_sha256": hashlib.sha256(deck.encode()).hexdigest()}
