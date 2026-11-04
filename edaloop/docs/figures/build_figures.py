#!/usr/bin/env python3
"""Draw the README's figures. The step figures are real output from `testbenches/gain_block_demo.yaml`, so they can be remade by anyone.

    uv run python docs/figures/build_figures.py STEPS_DIR DEMO_DIR

STEPS_DIR holds a copy of the board after each stage. DEMO_DIR is the finished project, with its ledger.
"""
import json
import subprocess
import sys
import tempfile
import textwrap
from html import escape
from pathlib import Path

import yaml

from edaloop import ground
from edaloop.pcbfile import Board

OUT = Path(__file__).parent
INK, MUTED, LINE, PAPER, TINT = "#1f2933", "#5f6b7a", "#c5ccd6", "#ffffff", "#f3f5f8"
RF, POWER, SIGNAL, GROUND, PART = "#c8324a", "#b7791f", "#1f6f8b", "#2f855a", "#8a94a3"
GOOD, WARN, BAD = "#2f855a", "#b7791f", "#c8324a"
FONT = "font-family='-apple-system,Segoe UI,Helvetica,Arial,sans-serif'"


def text(x, y, s, size=13, fill=INK, anchor="start", weight="400", mono=False):
    family = " font-family='SFMono-Regular,Menlo,Consolas,monospace'" if mono else ""
    return f"<text x='{x:.1f}' y='{y:.1f}' font-size='{size}' fill='{fill}' text-anchor='{anchor}' font-weight='{weight}'{family}>{escape(str(s))}</text>"


def svg(w, h, body, label):
    return (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {w} {h}' role='img' aria-label='{escape(label, quote=True)}' {FONT}>"
            f"<rect width='{w}' height='{h}' fill='{PAPER}' rx='8'/>"
            "<defs><marker id='a' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
            f"<path d='M0 0L10 5L0 10z' fill='{MUTED}'/></marker></defs>{body}</svg>\n")


def box(x, y, w, h, title, lines=(), fill=TINT, edge=LINE, title_fill=INK):
    out = f"<rect x='{x}' y='{y}' width='{w}' height='{h}' rx='6' fill='{fill}' stroke='{edge}'/>"
    out += text(x + w / 2, y + 22, title, 14, title_fill, "middle", "600")
    for i, line in enumerate(lines):
        out += text(x + w / 2, y + 41 + 16 * i, line, 12, MUTED, "middle")
    return out


def arrow(points, label=None, at=None, dash=False, anchor="middle"):
    d = "M" + " L".join(f"{x} {y}" for x, y in points)
    out = f"<path d='{d}' fill='none' stroke='{MUTED}' stroke-width='1.5' marker-end='url(#a)'{' stroke-dasharray=\"5 4\"' if dash else ''}/>"
    if label:
        out += text(at[0], at[1], label, 12, MUTED, anchor)
    return out


def data_flow() -> str:
    b = text(30, 34, "How data moves through edaloop", 18, INK, "start", "600")
    b += text(30, 54, "Every result is tied to a hash of the files it measured, so changing a file retires what was said about it.", 13, MUTED)
    b += box(30, 80, 180, 66, "Written description", ["parts, nets, RF path (YAML)"])
    b += box(30, 170, 180, 66, "Existing schematic", ["edaloop describe"])
    b += box(280, 125, 150, 66, "edaloop build", ["places and draws"])
    b += box(500, 110, 170, 96, "KiCad project", ["board and schematic", "edaloop.yaml", "chain spec"], "#eaf2f8", SIGNAL)
    b += box(730, 110, 150, 96, "Routers", ["work on copies", "adopted only if RF", "copper holds"])
    b += box(500, 280, 170, 80, "Checks", ["one authority", "for each question"])
    b += box(730, 280, 150, 80, "Tools", ["KiCad, circuitRF,", "ngspice, EM solvers"])
    b += box(280, 280, 150, 80, "Evidence ledger", ["result + input hashes"], "#eef7f1", GROUND)
    b += box(280, 430, 150, 80, "Gate", ["still the same bytes?", "all checks hold?"])
    b += box(500, 430, 170, 80, "next_action", ["says what to fix or run", "agent or person acts"])
    b += box(730, 430, 150, 80, "Release", ["fab files, only if", "the gate holds"], "#eef7f1", GROUND)
    b += box(30, 430, 180, 80, "A person", ["owns requirements", "and named waivers"], "#fdf6e7", WARN)
    b += arrow([(210, 113), (250, 113), (250, 150), (278, 150)]) + arrow([(210, 203), (250, 203), (250, 166), (278, 166)])
    b += arrow([(430, 158), (498, 158)])
    b += arrow([(670, 142), (728, 142)], "copy", (699, 134))
    b += arrow([(728, 174), (672, 174)], "adopt", (699, 192))
    b += arrow([(585, 206), (585, 278)], "reads the files", (577, 247), anchor="end")
    b += arrow([(670, 320), (728, 320)], "runs", (711, 312))
    b += arrow([(498, 320), (432, 320)], "records", (465, 312))
    b += arrow([(355, 360), (355, 428)], "reads", (363, 399), anchor="start")
    b += arrow([(430, 470), (498, 470)], "fails", (464, 462))
    b += arrow([(430, 496), (462, 496), (462, 530), (805, 530), (805, 512)], "holds", (640, 524))
    b += arrow([(670, 470), (686, 470), (686, 245), (640, 245), (640, 208)], "changes the design", (694, 392), anchor="start")
    b += arrow([(120, 430), (120, 262), (530, 262), (530, 208)], "requirements and waivers go in edaloop.yaml", (128, 256), anchor="start", dash=True)
    return svg(910, 550, b, "Data flow: a description or schematic is built into a KiCad project. Routers work on copies. Checks measure the files and record results in a ledger. The gate reads the ledger and either names the next action or allows release.")


STAGES = [
    ("Describe", "edaloop describe", "edaloop", "Board description (YAML)", "bench"),
    ("Place", "edaloop build", "KiCad", "Placed board, schematic, pours", "rf_path"),
    ("Ground", "edaloop ground", "KiCad", "Welded vias, maker's patterns", "reports any shortfall"),
    ("Route RF", "edaloop route --by-class", "edaloop", "Straight, locked RF copper", "rf_path, rf_preserved, rf_reference, impedance"),
    ("Route power", "(same command)", "KiCadRoutingTools", "Rails, on an inner layer", "power, thermal_field, drc"),
    ("Route signal", "(same command)", "KiCadRoutingTools", "Everything else", "drc, rf_preserved"),
    ("Finish", "edaloop finish", "KiCad", "Fence, stitching, refilled pours", "DRC removes any bad via"),
    ("Verify", "edaloop run all", "kicad-cli, circuitRF, boardfield", "Evidence", "erc, drc, rules, rf_layout, power, thermal_field"),
    ("Simulate", "(same command)", "EMerge, openEMS, Palace, ngspice", "Evidence", "em_hops, em_crosscheck, cascade, spice"),
    ("Evaluate", "edaloop status", "edaloop", "Gate state, next action", "requirements"),
    ("Release", "edaloop release", "kicad-cli", "Fabrication files", "refuses unless the gate holds"),
]


def stage_sequence() -> str:
    x = [66, 182, 392, 606, 806]
    top = 92
    row = 40
    b = text(30, 34, "The stages, in order", 18, INK, "start", "600")
    b += text(30, 54, "Run edaloop stage NAME to check a stage before moving on. edaloop next names whatever needs doing.", 13, MUTED)
    for pos, head in zip(x, ("Stage", "Command", "Tool", "Writes", "Checked by")):
        b += text(pos, top - 10, head, 12, MUTED, "start", "600")
    groups = {0: "#fdf6e7", 1: "#eaf2f8", 2: "#eaf2f8", 3: "#fbeff1", 4: "#fbeff1", 5: "#fbeff1", 6: "#eaf2f8", 7: "#eef7f1", 8: "#eef7f1", 9: "#eef7f1", 10: "#fdf6e7"}
    for i, (stage, command, tool, writes, sensors) in enumerate(STAGES):
        y = top + i * row
        b += f"<rect x='30' y='{y}' width='1040' height='{row - 4}' rx='5' fill='{groups[i]}'/>"
        b += f"<circle cx='48' cy='{y + 18}' r='11' fill='{INK}'/>" + text(48, y + 22, i + 1, 12, PAPER, "middle", "600")
        b += text(x[0], y + 23, stage, 14, INK, "start", "600") + text(x[1], y + 23, command, 12, INK, mono=True)
        b += text(x[2], y + 23, tool, 12, MUTED) + text(x[3], y + 23, writes, 12, INK)
        parts = sensors.split(", ")
        if len(parts) > 2:
            b += text(x[4], y + 15, ", ".join(parts[:2]) + ",", 11.5, MUTED) + text(x[4], y + 30, ", ".join(parts[2:]), 11.5, MUTED)
        else:
            b += text(x[4], y + 23, sensors, 11.5, MUTED)
    y = top + len(STAGES) * row
    b += f"<path d='M1086 {y - 22} L1108 {y - 22} L1108 {top + 18} L1074 {top + 18}' fill='none' stroke='{MUTED}' stroke-width='1.5' stroke-dasharray='5 4' marker-end='url(#a)'/>"
    b += f"<text transform='translate(1122 {top + 210}) rotate(90)' font-size='12' fill='{MUTED}' text-anchor='middle'>a failure goes back to the stage that can fix it</text>"
    return svg(1150, y + 20, b, "The stages in order: describe, place, ground, route RF, route power, route signal, finish, verify, simulate, evaluate and release, with the command, tool, output and checks for each.")


def board_figure(path: Path, title: str, note: str, chain_nets: set, rail_nets: set, labels: set) -> str:
    with tempfile.TemporaryDirectory() as work:
        facts = ground._facts(path, Path(work))
    board = Board(path)
    x1, y1, x2, y2 = facts["outline"]
    scale, ox, oy = 19.0, 26, 74
    px = lambda v: ox + (v - x1) * scale
    py = lambda v: oy + (v - y1) * scale
    width, height = (x2 - x1) * scale, (y2 - y1) * scale
    b = text(26, 30, title, 17, INK, "start", "600") + text(26, 52, note, 13, MUTED)
    b += f"<rect x='{ox}' y='{oy}' width='{width:.1f}' height='{height:.1f}' fill='#f7f8fa' stroke='{INK}' stroke-width='1.2'/>"
    for pad in facts["pads"]:
        colour = RF if pad["net"] in chain_nets else GROUND if pad["net"] == "GND" else PART
        w, h = pad["w"] * scale, pad["h"] * scale
        b += f"<rect x='{px(pad['x']) - w / 2:.1f}' y='{py(pad['y']) - h / 2:.1f}' width='{w:.1f}' height='{h:.1f}' fill='{colour}' fill-opacity='.28' stroke='{colour}' stroke-width='.7' transform='rotate({-pad['rot']:.1f} {px(pad['x']):.1f} {py(pad['y']):.1f})'/>"
    for t in sorted((t for t in board.tracks if t.kind != "via"), key=lambda t: t.layer != "F.Cu"):
        colour = RF if t.net in chain_nets else POWER if t.net in rail_nets else SIGNAL
        dash = " stroke-dasharray='6 4'" if t.layer != "F.Cu" else ""
        b += f"<line x1='{px(t.start[0]):.1f}' y1='{py(t.start[1]):.1f}' x2='{px(t.end[0]):.1f}' y2='{py(t.end[1]):.1f}' stroke='{colour}' stroke-width='{max(t.width * scale, 1.4):.1f}' stroke-linecap='round'{dash}/>"
    for t in (t for t in board.tracks if t.kind == "via"):
        colour = GROUND if t.net == "GND" else POWER
        b += f"<circle cx='{px(t.start[0]):.1f}' cy='{py(t.start[1]):.1f}' r='{t.width * scale / 2:.1f}' fill='{colour}' stroke='{PAPER}' stroke-width='.8'/>"
    for yard in facts["courtyards"]:
        if yard["ref"] in labels:
            b += text((px(yard["x1"]) + px(yard["x2"])) / 2, py(yard["y1"]) - 3, yard["ref"], 11, INK, "middle", "600")
    y = oy + height + 24
    legend = [(RF, "RF line, or pad on it", 0), (POWER, "Supply", 0), (SIGNAL, "Other signal", 0), (GROUND, "Ground pad or via", 0)]
    pos = ox
    for colour, name, _ in legend:
        b += f"<rect x='{pos}' y='{y - 10}' width='14' height='10' fill='{colour}'/>" + text(pos + 19, y, name, 12, MUTED)
        pos += 28 + len(name) * 6.6
    b += f"<line x1='{pos}' y1='{y - 5}' x2='{pos + 26}' y2='{y - 5}' stroke='{POWER}' stroke-width='3' stroke-dasharray='6 4'/>" + text(pos + 32, y, "Inner layer", 12, MUTED)
    return svg(int(width + 52), int(y + 18), b, f"{title}. {note}")


def counts(path: Path) -> dict:
    board = Board(path)
    return {"segments": sum(1 for t in board.tracks if t.kind != "via"), "ground_vias": sum(1 for t in board.tracks if t.kind == "via" and t.net == "GND"),
            "signal_vias": sum(1 for t in board.tracks if t.kind == "via" and t.net != "GND")}


def describe_figure(bench: dict) -> str:
    chain = bench["chain"]
    refs = [chain[0][1].split(".")[0], *[c[2].split(".")[0] for c in chain]]
    parts = bench["parts"]
    b = text(26, 30, "1. Describe", 17, INK, "start", "600") + text(26, 52, "A description holds the parts, nets and RF path. edaloop describe writes one from a schematic.", 13, MUTED)
    w, gap, x = 94, 22, 26
    for i, ref in enumerate(refs):
        value = str((parts.get(ref) or {}).get("value", ""))
        b += box(x + i * (w + gap), 76, w, 62, ref, [value.split(" and")[0][:13]], "#fbeff1" if ref[0] in "JU" else TINT)
        if i < len(refs) - 1:
            b += arrow([(x + i * (w + gap) + w, 107), (x + (i + 1) * (w + gap) - 2, 107)])
    b += text(26, 170, "From testbenches/gain_block_demo.yaml", 12, MUTED, "start", "600")
    snippet = [f"name: {bench['name']}", f"band_mhz: {bench['band_mhz']}", f"outline_mm: {bench['outline_mm']}", "chain:", *[f"  - {row}" for row in chain[:3]], "  # ...and three more hops"]
    for i, line in enumerate(snippet):
        b += text(26, 192 + i * 18, line.replace("'", ""), 12, INK, mono=True)
    return svg(26 * 2 + len(refs) * w + (len(refs) - 1) * gap, 192 + len(snippet) * 18 + 8, b, "A description's RF path from input connector to output connector, with part of its YAML.")


def _latest(demo: Path) -> dict:
    latest = {}
    for line in (demo / ".edaloop/ledger.jsonl").read_text().splitlines():
        row = json.loads(line)
        latest[row["check"]] = row
    return latest


def gate_figure(demo: Path) -> str:
    latest = _latest(demo)
    project = yaml.safe_load((demo / "edaloop.yaml").read_text())
    colours = {"pass": GOOD, "warn": WARN, "fail": BAD, "error": BAD}
    marks = {"pass": "pass", "warn": "warning", "fail": "FAIL", "error": "ERROR"}
    b = text(26, 30, "8. Verify", 17, INK, "start", "600") + text(26, 52, "Every gated check on the finished demo, simulation included. A warning goes to a person, and a failure blocks release.", 13, MUTED)
    y = 80
    for name in project["gate"]:
        row = latest.get(name)
        if not row:
            continue
        status = row["status"]
        b += f"<circle cx='36' cy='{y - 4}' r='5' fill='{colours[status]}'/>" + text(50, y, name, 13, INK, mono=True) + text(190, y, marks[status], 12, colours[status], "start", "600")
        first = next((f["message"] for f in row["findings"] if not f.get("waived_by")), "")
        b += text(262, y, (first[:88] + "…") if len(first) > 88 else first, 12, MUTED)
        y += 24
    return svg(860, y + 4, b, "The gate's checks on the demo board, each with its result and first finding.")


def simulate_figure(demo: Path) -> str:
    latest = _latest(demo)
    hops = latest["em_hops"]["metrics"]["hops"]
    b = text(26, 30, "9. Simulate", 17, INK, "start", "600") + text(26, 52, "Each RF hop is solved as routed, with its pads. Three solvers then check the line itself.", 13, MUTED)
    b += text(26, 84, "Return loss per hop, dB (limit 15, higher is better)", 13, INK, "start", "600")
    y, scale = 106, 7.0
    for net, row in hops.items():
        b += text(26, y + 11, net, 12, INK, mono=True)
        b += f"<rect x='104' y='{y}' width='{row['rl_db'] * scale:.1f}' height='14' rx='3' fill='{SIGNAL}'/>" + text(110 + row["rl_db"] * scale, y + 11, row["rl_db"], 12, INK)
        y += 22
    b += f"<line x1='{104 + 15 * scale}' y1='98' x2='{104 + 15 * scale}' y2='{y}' stroke='{INK}' stroke-dasharray='4 3'/>" + text(104 + 15 * scale, y + 14, "15 dB", 11, MUTED, "middle")
    y += 40
    cross = latest.get("em_crosscheck")
    if cross:
        metrics = cross["metrics"]
        b += text(26, y, "The same line in three solvers, ohm", 13, INK, "start", "600")
        y += 22
        for name, row in metrics["solvers"].items():
            zc = row.get("zc_ohm", row.get("z0_ohm"))
            b += text(26, y + 11, name, 12, INK, mono=True)
            if zc:
                b += f"<circle cx='{240 + (zc - 45) * 28:.1f}' cy='{y + 7}' r='6' fill='{SIGNAL}'/>" + text(252 + (zc - 45) * 28, y + 11, f"{zc:.2f}", 12, INK)
            y += 24
        diagnosis = metrics.get("diagnosis") or {}
        b += text(26, y + 8, f"Verdict: {diagnosis.get('verdict')}. Spread {metrics.get('spread_pct')}% against a 2% limit. {cross['summary']}.", 12, MUTED)
        y += 12
    return svg(640, y + 22, b, "Return loss for each RF hop, and the line's impedance as read by three solvers.")


def next_figure(demo: Path, status_text: str, next_text: str) -> str:
    b = text(26, 30, "10. Evaluate", 17, INK, "start", "600") + text(26, 52, "The gate gives one answer to what's next. A waiver is a person's decision, so an agent stops and asks.", 13, MUTED)
    y = 80
    b += text(26, y, "$ edaloop next", 12, INK, mono=True, weight="600")
    for line in next_text.splitlines():
        y += 18
        b += text(26, y, line, 12, MUTED, mono=True)
    y += 30
    b += text(26, y, "$ edaloop status", 12, INK, mono=True, weight="600")
    y += 18
    b += text(26, y, status_text, 12, INK, mono=True)
    return svg(860, y + 20, b, "edaloop next and edaloop status on the demo board.")


def main():
    steps, demo = Path(sys.argv[1]), Path(sys.argv[2])
    bench = yaml.safe_load((demo / "board_description.yaml").read_text())
    chain = {c[0] for c in bench["chain"]}
    rails = {r["net"] for r in bench["project"]["rails"]}
    labels = {x for row in bench["chain"] for x in (row[1].split(".")[0], row[2].split(".")[0])}
    (OUT / "data-flow.svg").write_text(data_flow())
    (OUT / "stage-sequence.svg").write_text(stage_sequence())
    (OUT / "step-1-describe.svg").write_text(describe_figure(bench))
    plain = [("1_place", "step-2-place", "2. Place", "Each part's rotation and position come from the nets and courtyards, with RF pads on one line."),
             ("2_ground", "step-3-ground", "3. Ground", "{ground_vias} vias welded to the ground pads, before anything routes."),
             ("1_rf", "step-4-route-rf", "4. Route RF", "RF first: straight, locked and with no vias. {segments} segments."),
             ("2_power", "step-5-route-power", "5. Route power", "Supply goes to an inner layer, with a via and short stub at each pad. {signal_vias} vias."),
             ("3_signal", "step-6-route-signal", "6. Route signal", "What's left. The router can't use the plane under the RF line. {segments} segments in all."),
             ("6_finish", "step-7-finish", "7. Finish", "A fence on both sides of the RF line, then stitching and refilled pours. {ground_vias} ground vias.")]
    for source, name, title, note in plain:
        path = steps / f"{source}.kicad_pcb"
        (OUT / f"{name}.svg").write_text(board_figure(path, title, note.format(**counts(path)), chain, rails, labels))
    if (demo / ".edaloop/ledger.jsonl").exists():
        latest = _latest(demo)
        if "em_hops" in latest:
            (OUT / "step-8-verify.svg").write_text(gate_figure(demo))
            (OUT / "step-9-simulate.svg").write_text(simulate_figure(demo))
            tool = str(Path(sys.executable).parent / "edaloop")
            step = json.loads(subprocess.run([tool, "-C", demo, "next"], capture_output=True, text=True, check=True).stdout)
            ready = next((x for x in subprocess.run([tool, "-C", demo, "status"], capture_output=True, text=True).stdout.splitlines() if x.startswith("ready")), "")
            lines = [f"action:   {step.get('action')}"]
            if step.get("check"):
                lines.append(f"check:    {step['check']}")
            for key in ("reason", "question", "summary", "command", "remedy", "do_not"):
                if step.get(key):
                    lines += [f"{(key + ':') if i == 0 else '':9} {x}" for i, x in enumerate(textwrap.wrap(str(step[key]), 96)[:3])]
            (OUT / "step-10-evaluate.svg").write_text(next_figure(demo, ready, "\n".join(lines)))


if __name__ == "__main__":
    main()
