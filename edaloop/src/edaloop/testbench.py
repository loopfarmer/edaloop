"""Test benches from a written description of a board: no KiCad files and no layout needed.

Each bench states its parts, nets, RF path, stage table and DC bias. Running one checks that the
description holds together, that Friis returns the recorded cascade, and that SPICE returns the
hand-worked bias. A check that can't run is reported as skipped, never as a pass.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from . import spice, toolchain
from .checks import friis

BENCHES = Path(__file__).resolve().parents[2] / "testbenches"
GAIN_TOL_DB = 0.03      # the source tables round each row to 0.01 dB
NF_TOL_DB = 0.02


def available() -> list[Path]:
    return sorted(BENCHES.glob("*.yaml"))


def find(name: str) -> Path:
    """A bench by path, or by name from the shipped ones: `four_channel_demo` or `four_channel_demo.yaml`."""
    from .project import ProjectError
    path = Path(name)
    if path.is_file():
        return path
    shipped = BENCHES / (path.name if path.suffix == ".yaml" else f"{path.name}.yaml")
    if shipped.is_file():
        return shipped
    raise ProjectError(f"no bench {name!r}. Shipped benches: {', '.join(p.stem for p in available())}")


def _row(check: str, problems: list[str], detail: str) -> dict:
    return {"check": check, "status": "fail" if problems else "pass",
            "detail": "; ".join(problems[:6]) if problems else detail}


def _pin(text: str) -> tuple[str, str]:
    ref, _, pin = str(text).partition(".")
    return ref, pin


def check_connectivity(bench: dict) -> dict:
    """Every pin names a real part and sits on one net, and every fitted part is connected."""
    parts, problems, seen = bench["parts"], [], {}
    for net, pins in bench["nets"].items():
        for pin in pins:
            if _pin(pin)[0] not in parts:
                problems.append(f"{net}: {pin} names no part")
            if pin in seen:
                problems.append(f"{pin} is on both {seen[pin]} and {net}")
            seen[pin] = net
    for pin in bench.get("open_pins") or []:
        if pin in seen:
            problems.append(f"{pin} is listed as open, but is on {seen[pin]}")
    used = {_pin(p)[0] for p in seen}
    problems += [f"{ref} has no connections" for ref, x in parts.items() if ref not in used and not x.get("dnp")]
    return _row("connectivity", problems, f"{len(parts)} parts, {len(bench['nets'])} nets, {len(seen)} pins")


def check_chain(bench: dict) -> dict:
    """The RF path runs unbroken from one connector to another, through each part in turn."""
    chain, nets, problems = bench["chain"], bench["nets"], []
    for net, entry, leave in chain:
        for pin in (entry, leave):
            if pin not in nets.get(net, []):
                problems.append(f"{pin} isn't on {net}")
    # A connector ends one channel, and the next row starts another.
    channels, current = [], []
    for row in chain:
        current.append(row)
        if _pin(row[2])[0].startswith("J"):
            channels.append(current)
            current = []
    if current:
        channels.append(current)
    for channel in channels:
        for (net_a, _, leave), (net_b, entry, _) in zip(channel, channel[1:]):
            if _pin(leave)[0] != _pin(entry)[0]:
                problems.append(f"{net_a} ends on {leave}, but {net_b} starts on {entry}")
        for pin in (channel[0][1], channel[-1][2]):
            if not _pin(pin)[0].startswith("J"):
                problems.append(f"the path ends on {pin}, which isn't a connector")
    path = " > ".join([_pin(chain[0][1])[0], *[_pin(x[2])[0] for x in channels[0]]])
    return _row("rf_chain", problems, f"{len(channels)} channels, the first {path}" if len(channels) > 1 else path)


def check_design_table(bench: dict) -> dict:
    """Friis over the rows must give the cumulative gain and noise figure the design recorded."""
    table = bench["design_table"]
    f = float(table["freq_mhz"])
    stages, problems = [], []
    for row in table["rows"]:
        stages.append({"gain_db": [[f, row["gain_db"]]], "nf_db": [[f, row["nf_db"]]]})
        got = friis(stages, f)
        if abs(got["gain_db"] - row["cum_gain_db"]) > GAIN_TOL_DB:
            problems.append(f"{row['stage']}: gain {got['gain_db']} dB, recorded {row['cum_gain_db']}")
        if abs(got["nf_db"] - row["cum_nf_db"]) > NF_TOL_DB:
            problems.append(f"{row['stage']}: NF {got['nf_db']} dB, recorded {row['cum_nf_db']}")
    return _row("design_table", problems, f"{got['gain_db']} dB gain, {got['nf_db']} dB NF at {f:g} MHz")


def check_band(bench: dict) -> dict | None:
    """A regression on the band edges from the stage table, parts only. It catches a changed table or a Friis change."""
    if not bench.get("stages"):
        return None
    low, high = bench["band_mhz"]
    edges = {f: friis(bench["stages"], float(f)) for f in (low, high)}
    problems = []
    for f, got in edges.items():
        want = (bench.get("band_expect") or {}).get(f)
        if not want:
            problems.append(f"no recorded values at {f} MHz")
            continue
        for key, value in want.items():
            if got[key] is None or abs(got[key] - value) > 0.011:
                problems.append(f"{key} at {f} MHz: {got[key]}, recorded {value}")
    summary = ", ".join(f"{f} MHz {v['gain_db']} dB / NF {v['nf_db']} dB" for f, v in edges.items())
    return _row("band_edges", problems, summary)


def check_bias(bench: dict, work: Path, name: str) -> dict:
    """DC bias through ngspice, from the description's nets. Each part needs a model or a stated reason."""
    if not shutil.which(str(toolchain.paths()["ngspice"])):
        return {"check": "dc_bias", "status": "skipped", "detail": "ngspice isn't installed, so this didn't run"}
    cfg = bench["spice"]
    pins: dict[str, dict[str, str]] = {ref: {} for ref in bench["parts"]}
    for net, members in bench["nets"].items():
        for member in members:
            ref, pin = _pin(member)
            pins[ref][pin] = net
    parts = [spice.Part(ref, str(x.get("value", "")), str(x.get("mpn", "")), pins[ref]) for ref, x in bench["parts"].items()]
    registry = spice.load_registry()
    models = {x.ref: spice.resolve(x, cfg.get("models") or {}, registry) for x in parts}
    missing = [ref for ref, m in models.items() if m["kind"] == "missing"]
    if missing:
        return _row("dc_bias", [f"{ref} has no model and no stated exclusion" for ref in missing], "")
    run = spice.run_deck(spice.build_deck(name, parts, models, cfg["bench"]), work, f"{name}_{cfg['bench']['name']}")
    problems = []
    for key, want in cfg["expect"].items():
        got = run["values"].get(key)
        if got is None:
            problems.append(f"{key} wasn't measured")
        elif abs(got - want["value"]) > want["tol"]:
            problems.append(f"{key} is {got:.4g}, expected {want['value']} +/- {want['tol']}")
    return _row("dc_bias", problems, ", ".join(f"{k} = {run['values'][k]:.4g}" for k in cfg["expect"] if k in run["values"]))


def run(path: Path, work: Path) -> dict:
    bench = yaml.safe_load(Path(path).read_text())
    name = bench["name"]
    rows = [check_connectivity(bench), check_chain(bench)]
    if bench.get("design_table"):
        rows.append(check_design_table(bench))
    band = check_band(bench)
    if band:
        rows.append(band)
    rows.append(check_bias(bench, work, name))
    return {"bench": name, "ok": all(r["status"] != "fail" for r in rows), "checks": rows}
