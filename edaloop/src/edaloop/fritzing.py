"""Fritzing parts: reading Adafruit's as a second opinion on a board, and writing one for a generated board.

Adafruit publishes a Fritzing part for most of its boards. Its connector names and pad positions were written separately from the board's
netlist, so where they agree with what edaloop read, that's independent support. A .fzpz is a zip holding one XML part file and an SVG per view.
"""
from __future__ import annotations

import io
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from html import escape
from pathlib import Path

from . import ground
from .mechanical import part_kind

LIBRARY_RAW = "https://raw.githubusercontent.com/adafruit/Fritzing-Library/{commit}/{path}"
CACHE = Path.home() / ".cache" / "edaloop" / "evaluation" / "fritzing"
SVG = "{http://www.w3.org/2000/svg}"


def fetch_part(commit: str, path: str) -> Path:
    """A part from Adafruit's library at a pinned commit, kept in a cache. Only ever opened as a zip of data."""
    out = CACHE / commit / Path(path).name
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(LIBRARY_RAW.format(commit=commit, path=urllib.parse.quote(path)), timeout=60) as r:
            data = r.read()
        out.with_suffix(".tmp").write_bytes(data)
        out.with_suffix(".tmp").rename(out)
    return out


def _to_mm(value: str | None) -> float | None:
    m = re.match(r"\s*([\d.]+)\s*(in|mm|px|pt)?", value or "")
    if not m:
        return None
    return float(m[1]) * {"in": 25.4, "mm": 1.0, "pt": 25.4 / 72, "px": 25.4 / 90, None: 25.4 / 90}[m[2]]


def read_part(path: Path) -> dict:
    """Title, connectors, and the PCB view's size and pad positions in millimetres, with y down as in the SVG."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        fzp = next(n for n in names if n.lower().endswith(".fzp"))
        root = ET.fromstring(z.read(fzp))
        pcb = next((n for n in names if n.startswith("svg.pcb.")), None)
        connectors = [{"id": c.get("id"), "name": c.get("name"), "type": c.get("type")} for c in root.iter("connector")]
        out = {"title": root.findtext("title") or "", "connectors": connectors, "size_mm": None, "pads": {}}
        if not pcb:
            return out
        svg = ET.fromstring(re.sub(rb"^\s*<\?xml[^>]*\?>", b"", z.read(pcb)))
    box = [float(v) for v in re.split(r"[ ,]+", svg.get("viewBox", "").strip())] if svg.get("viewBox") else None
    w, h = _to_mm(svg.get("width")), _to_mm(svg.get("height"))
    if not (box and len(box) == 4 and w and h and box[2] and box[3]):
        return out
    out["size_mm"] = [round(w, 3), round(h, 3)]
    sx, sy = w / box[2], h / box[3]
    for e in svg.iter():
        ident = e.get("id") or ""
        m = re.fullmatch(r"(connector\d+)pad", ident)
        if not m:
            continue
        if e.get("cx") is not None:
            x, y = float(e.get("cx")), float(e.get("cy"))
        elif e.get("x") is not None and e.get("width") is not None:
            x, y = float(e.get("x")) + float(e.get("width")) / 2, float(e.get("y")) + float(e.get("height")) / 2
        else:
            continue
        # A pad inside a transformed group isn't at its stated position. Leave it out rather than misplace it.
        out["pads"].setdefault(m[1], [(x - box[0]) * sx, (y - box[1]) * sy])
    return out


def clean_name(name: str) -> str:
    """A pin or net name, for comparing across tools. KiCad's import adds marks that aren't in the name."""
    text = (name or "").upper().replace("~{", "").replace("UNK", "", 1) if (name or "").upper().startswith(("UNK", "~{")) else (name or "").upper()
    return re.sub(r"[^A-Z0-9]", "", text)


ALIASES = {"33V": {"3V", "3V3", "33V", "3V0"}, "3V3": {"3V", "33V", "3V3"}, "5V": {"5V", "VUSB", "USB"}, "VIN": {"VIN", "VCC", "V"}}


def pin_agreement(part: dict, nets: dict[str, list[str]]) -> dict:
    """How many of the part's named pins are nets the board really has."""
    live = [n for n in nets if not n.startswith("unconnected-")]
    board_names = {clean_name(n) for n in live}
    # A level shifter's pin called CS can sit on nets called CS_3V and CS_5V.
    board_names |= {clean_name(n.split("_")[0]) for n in live if "_" in n}
    known = {x for n in board_names for x in ALIASES.get(n, {n})} | board_names
    names = [c["name"] for c in part["connectors"] if c.get("name")]
    missing = sorted({n for n in names if clean_name(n) not in known})
    found = sum(1 for n in names if clean_name(n) in known)
    return {"pins": len(names), "found": found, "missing": missing}


def _turned(points: list, turn: int) -> list:
    if turn == 0:
        return points
    if turn == 1:
        return [[y, -x] for x, y in points]
    if turn == 2:
        return [[-x, -y] for x, y in points]
    return [[-y, x] for x, y in points]


def pad_agreement(part_pads: list, board_pads: list, reach: float = 0.5) -> dict:
    """The share of the part's pads with a board pad under them, at the best of four turns, once both are centred."""
    if not part_pads or not board_pads:
        return {"share": None, "compared": 0}
    best = 0.0
    for turn in range(4):
        pts = _turned(part_pads, turn)
        cx, cy = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2, (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
        hit_best = 0
        # Line up on the nearest candidates, as a part can leave pads out of its view. Try each board pad under the first part pad.
        for bx, by in board_pads:
            dx, dy = bx - pts[0][0], by - pts[0][1]
            hits = sum(1 for x, y in pts if any(math.hypot(x + dx - qx, y + dy - qy) <= reach for qx, qy in board_pads))
            hit_best = max(hit_best, hits)
        best = max(best, hit_best / len(pts))
        _ = cx, cy
    return {"share": round(best, 3), "compared": len(part_pads)}


def _svg(width: float, height: float, body: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" version="1.2" baseProfile="tiny" x="0in" y="0in" '
            f'width="{width / 25.4:.5f}in" height="{height / 25.4:.5f}in" viewBox="0 0 {width:.4f} {height:.4f}">\n{body}\n</svg>\n')


def write_part(name: str, outline: list[float], parts: list[dict], pins: list[dict], out: Path, author: str = "edaloop") -> Path:
    """A .fzpz for a board: its top view for the breadboard, its pads for the PCB, and a box with named pins for the schematic."""
    x1, y1, x2, y2 = outline
    w, h = x2 - x1, y2 - y1
    module = re.sub(r"\W", "_", f"edaloop_{name}")
    bb = [f'<g id="breadboard"><rect x="0" y="0" width="{w:.4f}" height="{h:.4f}" fill="#1f5d42" stroke="#0e3323" stroke-width="0.2"/>']
    for p in parts:
        if p["back"]:
            continue
        bb.append(f'<rect x="{p["x"] - x1 - p["w"] / 2:.3f}" y="{p["y"] - y1 - p["h"] / 2:.3f}" width="{p["w"]:.3f}" height="{p["h"]:.3f}" fill="#30343a" '
                  f'transform="rotate({-p["rot"]:.2f} {p["x"] - x1:.3f} {p["y"] - y1:.3f})"/>')
    pcb = [f'<g id="silkscreen"><rect x="0.1" y="0.1" width="{w - 0.2:.4f}" height="{h - 0.2:.4f}" fill="none" stroke="#ffffff" stroke-width="0.2"/></g>', '<g id="copper1"><g id="copper0">']
    rows = max(len(pins), 1)
    sw, sh = 30.0, 6.0 * rows + 6.0
    sch = [f'<g id="schematic"><rect x="6" y="1" width="{sw - 12:.1f}" height="{sh - 2:.1f}" fill="#ffffff" stroke="#000000" stroke-width="0.3"/>',
           f'<text x="{sw / 2:.1f}" y="4" font-size="2" text-anchor="middle">{escape(name)}</text>']
    connectors = []
    for i, pin in enumerate(pins):
        cid = f"connector{i}"
        x, y = pin["x"] - x1, pin["y"] - y1
        r = max(min(pin.get("w", 1.0), pin.get("h", 1.0)) / 2, 0.3)
        bb.append(f'<circle id="{cid}pin" cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" fill="#d8b23a"/><rect id="{cid}terminal" x="{x:.3f}" y="{y:.3f}" width="0.001" height="0.001" fill="none"/>')
        pcb.append(f'<circle id="{cid}pad" cx="{x:.3f}" cy="{y:.3f}" r="{r:.3f}" fill="#F7BD13" connectorname="{escape(pin["net"])}"/>')
        py = 7.0 + 6.0 * i
        sch.append(f'<rect id="{cid}pin" x="0" y="{py - 0.1:.2f}" width="6" height="0.2" fill="#000000"/><rect id="{cid}terminal" x="0" y="{py - 0.1:.2f}" width="0.001" height="0.2" fill="none"/>'
                   f'<text x="7" y="{py + 0.7:.2f}" font-size="2">{escape(pin["net"])}</text>')
        connectors.append(f'''  <connector name="{escape(pin['net'])}" id="{cid}" type="male">
   <description>{escape(pin['pad'])}</description>
   <views>
    <breadboardView><p svgId="{cid}pin" layer="breadboard" terminalId="{cid}terminal"/></breadboardView>
    <schematicView><p svgId="{cid}pin" layer="schematic" terminalId="{cid}terminal"/></schematicView>
    <pcbView><p svgId="{cid}pad" layer="copper1"/><p svgId="{cid}pad" layer="copper0"/></pcbView>
   </views>
  </connector>''')
    bb.append("</g>")
    pcb.append("</g></g>")
    sch.append("</g>")
    fzp = f'''<?xml version="1.0" encoding="UTF-8"?>
<module moduleId="{module}" fritzingVersion="0.9.3">
 <version>1</version>
 <author>{escape(author)}</author>
 <title>{escape(name)}</title>
 <description>Generated by edaloop from the board file.</description>
 <tags><tag>edaloop</tag></tags>
 <properties><property name="family">{escape(name)}</property><property name="variant">generated</property></properties>
 <views>
  <iconView><layers image="icon/{module}_icon.svg"><layer layerId="icon"/></layers></iconView>
  <breadboardView><layers image="breadboard/{module}_breadboard.svg"><layer layerId="breadboard"/></layers></breadboardView>
  <schematicView><layers image="schematic/{module}_schematic.svg"><layer layerId="schematic"/></layers></schematicView>
  <pcbView><layers image="pcb/{module}_pcb.svg"><layer layerId="copper1"/><layer layerId="silkscreen"/><layer layerId="copper0"/></layers></pcbView>
 </views>
 <connectors>
{chr(10).join(connectors)}
 </connectors>
</module>
'''
    breadboard = _svg(w, h, "\n".join(bb))
    icon = breadboard.replace('id="breadboard"', 'id="icon"')
    target = out / f"{name}.fzpz"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"part.{module}.fzp", fzp)
        z.writestr(f"svg.breadboard.{module}_breadboard.svg", breadboard)
        z.writestr(f"svg.icon.{module}_icon.svg", icon)
        z.writestr(f"svg.schematic.{module}_schematic.svg", _svg(sw, sh, "\n".join(sch)))
        z.writestr(f"svg.pcb.{module}_pcb.svg", _svg(w, h, "\n".join(pcb)))
    out.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buffer.getvalue())
    return target


def export(p, output: Path | None = None) -> dict:
    """Write a Fritzing part for a project's board, with a pin for each connector pad."""
    from .mechanical import part_boxes
    out = Path(output) if output else p.root / "fritzing"
    facts = ground._facts(p.board, p.work)
    pins = [{"pad": f"{x['ref']}.{x['pad']}", "net": x["net"], "x": x["x"], "y": x["y"], "w": x["w"], "h": x["h"]} for x in facts["pads"]
            if part_kind(x["ref"]) == "conn" and x["net"] and not x["net"].startswith("unconnected-")]
    path = write_part(p.name, facts["outline"], part_boxes(facts), pins, out)
    back = read_part(path)
    problems = []
    if len(back["connectors"]) != len(pins):
        problems.append(f"read back {len(back['connectors'])} pins from {len(pins)} written")
    if len(back["pads"]) != len(pins):
        problems.append(f"read back {len(back['pads'])} pads from {len(pins)} written")
    return {"part": str(path), "pins": [{"pad": x["pad"], "net": x["net"]} for x in pins], "size_mm": back["size_mm"], "problems": problems}
