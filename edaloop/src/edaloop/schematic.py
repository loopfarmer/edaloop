"""A drawn schematic from a board description: the RF path as a wired row, bias parts on rails above it.

Each part gets its own symbol, with its pins where this drawing needs them. Nets are bare global names, so the board and
schematic agree. Connectivity is proved afterwards against the description by `check_netlist`: a drawing is never trusted.
"""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

G = 1.27            # every coordinate is a multiple of this. A pin 0.01 mm off a wire is an open circuit.
NS = uuid.UUID("6f2a6c1e-0b5e-4d0a-9f55-0d3b7d1f7a10")
FONT = "(effects (font (size 1.27 1.27)))"
HIDE = "(effects (font (size 1.27 1.27)) (hide yes))"
LEFT = "(effects (font (size 1.27 1.27)) (justify left))"


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


def _q(text) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _key(pin: str):
    return (0, int(pin)) if pin.isdigit() else (1, pin)


def _snap(v: float) -> float:
    return round(round(v / G) * G, 2)


class Sheet:
    def __init__(self, name: str, project: str | None = None, path: str | None = None, power_from: int = 0):
        """`name` also seeds the uuids, so a page of a larger project names itself apart. `path` is where its symbols sit in the hierarchy."""
        self.name, self.root = name, _uid(name, "root")
        self.project = project or name
        self.path = path or "/" + self.root
        self.libs: list[str] = []
        self.items: list[str] = []
        self.power = power_from
        self.seen: set[tuple] = set()

    def wire(self, a: tuple, b: tuple) -> None:
        if a == b:
            return
        key = (a, b)
        self.seen.add(key)
        self.items.append(f'(wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) (stroke (width 0) (type default)) (uuid {_q(_uid(self.name, "w", str(key)))}))')

    def junction(self, at: tuple) -> None:
        self.items.append(f'(junction (at {at[0]} {at[1]}) (diameter 0) (color 0 0 0 0) (uuid {_q(_uid(self.name, "j", str(at)))}))')

    def label(self, net: str, at: tuple, angle: int = 0) -> None:
        """A global label whose point is at `at`. Angle 0 reads to the right of it, 90 up, 180 to the left."""
        justify = "right" if angle in (180, 270) else "left"
        self.items.append(f'(global_label {_q(net)} (shape bidirectional) (at {at[0]} {at[1]} {angle}) '
                          f'(effects (font (size 1.27 1.27)) (justify {justify})) (uuid {_q(_uid(self.name, "l", net, str(at)))}) '
                          f'(property "Intersheetrefs" "" (at {at[0]} {at[1]} 0) {HIDE}))')

    def no_connect(self, at: tuple) -> None:
        self.items.append(f'(no_connect (at {at[0]} {at[1]}) (uuid {_q(_uid(self.name, "nc", str(at)))}))')

    def text(self, body: str, at: tuple, size: float = 1.27, bold: bool = False) -> None:
        escaped = body.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        style = " (bold yes)" if bold else ""
        self.items.append(f'(text "{escaped}" (exclude_from_sim yes) (at {at[0]} {at[1]} 0) '
                          f'(effects (font (size {size} {size}){style}) (justify left top)) (uuid {_q(_uid(self.name, "t", body[:40], str(at)))}))')

    def ground(self, at: tuple) -> None:
        """A ground symbol with its pin at `at`, below a wire or pin."""
        self.power += 1
        ref = f"#PWR{self.power:03d}"
        self.items.append(_power_instance("edaloop:GND", ref, "GND", at, self))

    def flag(self, at: tuple) -> None:
        """Tells ERC that ground is driven, since nothing on these boards has a power output pin."""
        self.power += 1
        ref = f"#FLG{self.power:03d}"
        self.items.append(_power_instance("edaloop:PWR_FLAG", ref, "PWR_FLAG", at, self))


def _power_instance(lib: str, ref: str, value: str, at: tuple, sheet: Sheet) -> str:
    return (f'(symbol (lib_id {_q(lib)}) (at {at[0]} {at[1]} 0) (unit 1) (exclude_from_sim no) (in_bom no) (on_board no) (dnp no) '
            f'(uuid {_q(_uid(sheet.name, "p", ref))}) '
            f'(property "Reference" {_q(ref)} (at {at[0]} {at[1] + 6} 0) {HIDE}) '
            f'(property "Value" {_q(value)} (at {at[0]} {at[1] + 4} 0) {HIDE}) '
            f'(property "Footprint" "" (at {at[0]} {at[1]} 0) {HIDE}) (property "Datasheet" "" (at {at[0]} {at[1]} 0) {HIDE}) '
            f'(pin "1" (uuid {_q(_uid(sheet.name, "pp", ref))})) '
            f'(instances (project {_q(sheet.project)} (path {_q(sheet.path)} (reference {_q(ref)}) (unit 1)))))')


POWER_LIBS = [
    '(symbol "edaloop:GND" (power) (pin_names (offset 0) (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes) '
    '(property "Reference" "#PWR" (at 0 -6.35 0) ' + HIDE + ') (property "Value" "GND" (at 0 -3.81 0) ' + HIDE + ') '
    '(property "Footprint" "" (at 0 0 0) ' + HIDE + ') (property "Datasheet" "" (at 0 0 0) ' + HIDE + ') '
    '(symbol "GND_0_1" (polyline (pts (xy 0 0) (xy 0 -1.27) (xy 1.27 -1.27) (xy 0 -2.54) (xy -1.27 -1.27) (xy 0 -1.27)) '
    '(stroke (width 0) (type default)) (fill (type none)))) '
    '(symbol "GND_1_1" (pin power_in line (at 0 0 270) (length 0) (name "GND" ' + FONT + ') (number "1" ' + FONT + '))))',
    '(symbol "edaloop:PWR_FLAG" (power) (pin_numbers (hide yes)) (pin_names (offset 0) (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes) '
    '(property "Reference" "#FLG" (at 0 1.905 0) ' + HIDE + ') (property "Value" "PWR_FLAG" (at 0 3.81 0) ' + HIDE + ') '
    '(property "Footprint" "" (at 0 0 0) ' + HIDE + ') (property "Datasheet" "" (at 0 0 0) ' + HIDE + ') '
    '(symbol "PWR_FLAG_0_0" (pin power_out line (at 0 0 90) (length 0) (name "pwr" ' + FONT + ') (number "1" ' + FONT + '))) '
    '(symbol "PWR_FLAG_0_1" (polyline (pts (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27)) '
    '(stroke (width 0) (type default)) (fill (type none)))))',
]


def _body(kind: str, w: float, h: float) -> str:
    stroke = '(stroke (width 0.254) (type default))'
    if kind == "R_h":
        return f'(rectangle (start -2.54 1.016) (end 2.54 -1.016) {stroke} (fill (type none)))'
    if kind == "R_v":
        return f'(rectangle (start -1.016 2.54) (end 1.016 -2.54) {stroke} (fill (type none)))'
    if kind == "C_h":
        return (f'(polyline (pts (xy -0.635 -2.032) (xy -0.635 2.032)) {stroke} (fill (type none))) '
                f'(polyline (pts (xy 0.635 -2.032) (xy 0.635 2.032)) {stroke} (fill (type none)))')
    if kind == "C_v":
        return (f'(polyline (pts (xy -2.032 0.635) (xy 2.032 0.635)) {stroke} (fill (type none))) '
                f'(polyline (pts (xy -2.032 -0.635) (xy 2.032 -0.635)) {stroke} (fill (type none)))')
    if kind == "L_h":
        return " ".join(f'(arc (start {x} 0) (mid {x + 0.635} 0.635) (end {x + 1.27} 0) {stroke} (fill (type none)))' for x in (-2.54, -1.27, 0.0, 1.27))
    if kind == "L_v":
        return " ".join(f'(arc (start 0 {y}) (mid 0.635 {y + 0.635}) (end 0 {y + 1.27}) {stroke} (fill (type none)))' for y in (-2.54, -1.27, 0.0, 1.27))
    return f'(rectangle (start {-w / 2} {h / 2}) (end {w / 2} {-h / 2}) {stroke} (fill (type background)))'


def shown_value(ref: str, part: dict) -> str:
    """The Value written for a part, on the sheet and on the board alike. KiCad reads a symbol's empty Value as its reference, which is also its
    library name here, but keeps a footprint's empty, so a part with no value failed schematic parity. kicad_ops.py writes the same."""
    return str(part.get("value") or ref)


def add_part(sheet: Sheet, ref: str, part: dict, footprint: str, at: tuple, pins: dict[str, tuple], kind: str, w: float = 0.0, h: float = 0.0,
             show_numbers: bool = False, fields_at: tuple | None = None) -> None:
    """A part at `at`, with each pin number at a (dx, dy) offset on the sheet, y down. The symbol is written to match."""
    lines = []
    for number in sorted(pins, key=_key):
        dx, dy, inward = pins[number]
        lines.append(f'(pin passive line (at {dx} {-dy} {inward}) (length 1.27) (name {_q(number)} {HIDE}) (number {_q(number)} {FONT if show_numbers else HIDE}))')
    libsym = (f'(symbol {_q("edaloop:" + ref)} (pin_names (offset 0) (hide yes)) (exclude_from_sim no) (in_bom yes) (on_board yes) '
              f'(property "Reference" {_q(ref[0])} (at 0 {h / 2 + 2.54} 0) {FONT}) (property "Value" "" (at 0 {-h / 2 - 2.54} 0) {FONT}) '
              f'(property "Footprint" "" (at 0 0 0) {HIDE}) (property "Datasheet" "" (at 0 0 0) {HIDE}) '
              f'(symbol {_q(ref + "_0_1")} {_body(kind, w, h)}) (symbol {_q(ref + "_1_1")} {" ".join(lines)}))')
    sheet.libs.append(libsym)
    fx, fy = fields_at or (at[0], at[1] - 5.08)
    numbers = " ".join(f'(pin {_q(n)} (uuid {_q(_uid(sheet.name, "pin", ref, n))}))' for n in sorted(pins, key=_key))
    dnp = "yes" if part.get("dnp") else "no"
    sheet.items.append(
        f'(symbol (lib_id {_q("edaloop:" + ref)}) (at {at[0]} {at[1]} 0) (unit 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp {dnp}) '
        f'(uuid {_q(_uid(sheet.name, "symbol", ref))}) '
        f'(property "Reference" {_q(ref)} (at {fx} {fy} 0) (effects (font (size 1.27 1.27)) (justify left))) '
        f'(property "Value" {_q(shown_value(ref, part))} (at {fx} {fy + 2.54} 0) (effects (font (size 1.27 1.27)) (justify left))) '
        f'(property "Footprint" {_q(footprint)} (at {at[0]} {at[1]} 0) {HIDE}) (property "Datasheet" "" (at {at[0]} {at[1]} 0) {HIDE}) '
        f'(property "MPN" {_q(part.get("mpn", ""))} (at {fx} {fy + 5.08} 0) {(HIDE if not part.get("mpn") or ref[0] in "RCL" else LEFT)}) {numbers} '
        f'(instances (project {_q(sheet.project)} (path {_q(sheet.path)} (reference {_q(ref)}) (unit 1)))))')


def _kind(ref: str, value: str) -> str:
    return {"R": "R", "C": "C", "L": "L"}.get(ref[0], "box")


def pin_map(bench: dict) -> dict[str, dict[str, str]]:
    pins: dict[str, dict[str, str]] = {ref: {} for ref in bench["parts"]}
    for net, members in bench["nets"].items():
        for member in members:
            ref, _, pin = member.partition(".")
            pins[ref][pin] = net
    return pins


def draw(bench: dict, name: str, sheet: Sheet | None = None) -> Sheet:
    sheet = sheet or Sheet(name)
    parts, footprints = bench["parts"], bench["layout"]["footprints"]
    pins = pin_map(bench)
    opens: dict[str, list[str]] = {}
    for member in bench.get("open_pins") or []:
        ref, _, pin = member.partition(".")
        opens.setdefault(ref, []).append(pin)
    chain = bench["chain"]
    chain_nets = {c[0] for c in chain}
    placed: set[str] = set()
    row = _snap(101.6)
    first_row = row
    x = _snap(30.48)
    if sheet.power % 1000 == 0 and sheet.power <= 1000:
        # One flag for the whole project: two on one net is a pin conflict, and pages share ground by name.
        sheet.flag((_snap(20.32), _snap(row + 25.4)))
        sheet.ground((_snap(20.32), _snap(row + 25.4)))

    def gnd(at: tuple) -> None:
        sheet.ground(at)

    def inline(ref: str, entry: str | None, leave: str | None, at_x: float) -> tuple[float, float]:
        """A part on the row. Returns the x of its input and output pins."""
        part = parts[ref]
        others = sorted([p for p in pins[ref] if p not in (entry, leave)] + opens.get(ref, []), key=_key)
        if ref[0] in "RCL" and len(pins[ref]) == 2:
            half = _snap(5.08)
            k = _kind(ref, part.get("value", "")) + "_h"
            at = (_snap(at_x + half), row)
            add_part(sheet, ref, part, footprints[ref], at, {entry: (-half, 0, 0), leave: (half, 0, 180)}, k, fields_at=(at[0] - 2.54, at[1] + 3.81))
            return _snap(at[0] - half), _snap(at[0] + half)
        width = max(_snap(2.54 * (len(others) + 1)), _snap(10.16))
        half = _snap(width / 2)
        at = (_snap(at_x + half + 2.54), row)
        layout: dict[str, tuple] = {}
        if entry:
            layout[entry] = (-half - 2.54, 0, 0)
        if leave:
            layout[leave] = (half + 2.54, 0, 180)
        ys = _snap(5.08 + 2.54)
        starts = [-half + 1.27 + 2.54 * i for i in range(len(others))]
        for pin, dx in zip(others, starts):
            layout[pin] = (_snap(dx), ys, 90)
        add_part(sheet, ref, part, footprints[ref], at, layout, "box", w=width, h=10.16, show_numbers=True, fields_at=(at[0] - half, at[1] - 12.7))
        grounded = []
        for pin, dx in zip(others, starts):
            point = (_snap(at[0] + dx), _snap(at[1] + ys))
            if pin in opens.get(ref, []):
                sheet.no_connect(point)
            elif pins[ref].get(pin) == "GND":
                grounded.append(point)
            else:
                sheet.label(pins[ref][pin], point, 270)
        if grounded:
            bar = _snap(at[1] + ys + 2.54)
            for point in grounded:
                sheet.wire(point, (point[0], bar))
            sheet.wire((grounded[0][0], bar), (grounded[-1][0], bar))
            for point in grounded[1:-1]:
                sheet.junction((point[0], bar))
            gnd((grounded[0][0], bar))
        return _snap(at[0] - half - 2.54), _snap(at[0] + half + 2.54)

    def tap(ref: str, on_net: str, at_x: float) -> None:
        """A two-pin part hanging off the row, up if its far end is a rail and down if it's ground."""
        part = parts[ref]
        pin = next(p for p, n in pins[ref].items() if n == on_net)
        far = next((p for p in pins[ref] if p != pin), None)
        k = _kind(ref, part.get("value", "")) + "_v"
        far_net = pins[ref].get(far)
        if far_net == "GND" or far is None or far_net is None:
            at = (at_x, _snap(row + 7.62))
            layout = {pin: (0, -5.08, 270), **({far: (0, 5.08, 90)} if far else {})}
            add_part(sheet, ref, part, footprints[ref], at, layout, k, fields_at=(at[0] + 2.54, at[1] - 1.27))
            sheet.wire((at_x, row), (at_x, at[1] - 5.08))
            if far:
                gnd((at_x, at[1] + 5.08))
        else:
            at = (at_x, _snap(row - 10.16))
            add_part(sheet, ref, part, footprints[ref], at, {pin: (0, 5.08, 90), far: (0, -5.08, 270)}, k, fields_at=(at[0] + 2.54, at[1] - 1.27))
            sheet.wire((at_x, row), (at_x, at[1] + 5.08))
            sheet.label(far_net, (at_x, at[1] - 5.08), 90)
        placed.add(ref)

    # Each channel's row: connector, then each part, with each hop's taps and its net name between them.
    channels, current = [], []
    for step in chain:
        current.append(step)
        if step[2].split(".")[0].startswith("J"):
            channels.append(current)
            current = []
    if current:
        channels.append(current)
    width = 0.0
    for index, channel in enumerate(channels):
        if index:
            row = _snap(row + 50.8)
        entry_ref, entry_pin = channel[0][1].split(".")
        placed.add(entry_ref)
        _, out_x = inline(entry_ref, None, entry_pin, x)
        for net, entry, leave in channel:
            ref, pin_in = leave.split(".")
            members = [m.split(".")[0] for m in bench["nets"][net] if m not in (entry, leave)]
            taps = [m for m in members if m not in placed and len(pins[m]) <= 2]
            span = _snap(7.62 + 7.62 * (len(taps) + 1))
            stops = [_snap(out_x + 7.62 * (i + 1)) for i in range(len(taps) + 1)]
            next_in = _snap(out_x + span)
            points = [out_x, *stops, next_in]
            for a, b in zip(points, points[1:]):
                sheet.wire((a, row), (b, row))
            for stop in stops:
                sheet.junction((stop, row))
            # The net's name on a short stub, above the row.
            sheet.wire((stops[0], row), (stops[0], _snap(row - 3.81)))
            sheet.label(net, (stops[0], _snap(row - 3.81)), 90)
            for t_ref, stop in zip(taps, stops[1:]):
                tap(t_ref, net, stop)
            chained = [c for c in channel if c[1].split(".")[0] == ref]
            pin_out = chained[0][1].split(".")[1] if chained else None
            placed.add(ref)
            in_x, out_x = inline(ref, pin_in, pin_out, next_in)
            assert in_x == next_in, (ref, in_x, next_in)
        width = max(width, out_x + 30)

    # Rails above the row, one per remaining net, each with the parts not yet drawn.
    y = _snap(first_row - 45.72)
    rail_x = _snap(30.48)
    rails = [n for n in bench["nets"] if n != "GND" and n not in chain_nets]
    for net in rails:
        members = [m.split(".")[0] for m in bench["nets"][net]]
        hanging = [m for m in dict.fromkeys(members) if m not in placed and len(pins[m]) == 2]
        if not hanging:
            continue    # the pins' own labels already connect it, so a rail here would be a stub to nothing
        start = rail_x
        sheet.label(net, (start, y), 180)
        # Room for each part's value, which reads to the right of it, so that long ones don't run into the next.
        stops, reach = [], _snap(start + 7.62)
        for ref in hanging:
            stops.append(reach)
            reach = _snap(reach + max(12.7, 3.81 + 1.0 * len(str(parts[ref].get("value", "")))))
        points = [start, *stops]
        for a, b in zip(points, points[1:]):
            sheet.wire((a, y), (b, y))
        for stop in stops[:-1]:
            sheet.junction((stop, y))
        for ref, stop in zip(hanging, stops):
            placed.add(ref)
            part = parts[ref]
            pin = next(p for p, n in pins[ref].items() if n == net)
            far = next(p for p in pins[ref] if p != pin)
            k = _kind(ref, part.get("value", "")) + "_v"
            at = (stop, _snap(y + 7.62))
            add_part(sheet, ref, part, footprints[ref], at, {pin: (0, -5.08, 270), far: (0, 5.08, 90)}, k, fields_at=(at[0] + 2.54, at[1] - 1.27))
            sheet.wire((stop, y), (stop, at[1] - 5.08))
            bottom = (stop, at[1] + 5.08)
            if pins[ref][far] == "GND":
                gnd(bottom)
            else:
                sheet.label(pins[ref][far], bottom, 270)
        rail_x = _snap(stops[-1] + 25.4)
    width = max(width, rail_x + 20)

    # Everything else, such as regulators and headers, as a box with its nets named on its pins.
    y = _snap(row + 45.72)
    box_x = _snap(38.1)
    for ref in [r for r in parts if r not in placed]:
        numbers = sorted(set(pins[ref]) | set(opens.get(ref, [])), key=_key)
        half = max(1, (len(numbers) + 1) // 2)
        height = _snap(2.54 * (half + 1))
        at = (box_x, _snap(y + height / 2))
        layout = {}
        for k, number in enumerate(numbers):
            left = k < half
            dy = _snap(-height / 2 + 2.54 * ((k if left else k - half) + 1))
            layout[number] = (-15.24 if left else 15.24, dy, 0 if left else 180)
        add_part(sheet, ref, parts[ref], footprints[ref], at, layout, "box", w=25.4, h=height, show_numbers=True, fields_at=(at[0] - 12.7, at[1] - height / 2 - 7.62))
        for number, (dx, dy, _) in layout.items():
            point = (_snap(at[0] + dx), _snap(at[1] + dy))
            net = pins[ref].get(number)
            if net is None:
                sheet.no_connect(point)
            elif net == "GND":
                gnd(point)
            else:
                sheet.label(net, point, 180 if dx < 0 else 0)
        placed.add(ref)
        box_x = _snap(box_x + 76.2)
        if box_x > max(width, 500):
            box_x = _snap(38.1)
            y = _snap(y + height + 25.4)
    missing = [r for r in parts if r not in placed]
    if missing:
        raise ValueError(f"no place on the drawing for {missing}")
    sheet.height = y + 60

    sheet.text(name, (_snap(20.32), _snap(15.24)), 2.54, bold=True)
    sheet.text(" ".join(str(bench.get("summary", "")).split()), (_snap(20.32), _snap(21.59)))
    table = bench.get("design_table")
    if table:
        rows = [f"Cascade at {table['freq_mhz']} MHz: stage, gain dB, NF dB, cumulative gain dB, cumulative NF dB"]
        rows += [f"{r['stage']}: {r['gain_db']:+.2f}, {r['nf_db']:.2f}, {r['cum_gain_db']:+.2f}, {r['cum_nf_db']:.2f}" for r in table["rows"]]
        sheet.text("\n".join(rows), (_snap(20.32), _snap(first_row + 38.1)))
    sheet.width = width
    return sheet


def _paper(width: float, height: float) -> str:
    for name, wide, tall in (("A3", 400, 280), ("A2", 570, 400), ("A1", 820, 570), ("A0", 1170, 820)):
        if width < wide and height < tall:
            return name
    return "A0"


def schematic(bench: dict, name: str) -> str:
    sheet = draw(bench, name)
    libs = "\n".join([*POWER_LIBS, *sheet.libs])
    return (f'(kicad_sch (version 20250114) (generator "edaloop") (generator_version "10.0") (uuid {_q(sheet.root)}) (paper "{_paper(sheet.width, sheet.height)}") (title_block (title {_q(name)}) (comment 1 "Generated from the board description. Edit that, not this."))\n'
            f'(lib_symbols\n{libs}\n)\n' + "\n".join(sheet.items) + '\n(sheet_instances (path "/" (page "1"))) (embedded_fonts no))\n')


def _channels(chain: list) -> list[list]:
    """Rows of the chain, one list per path, as the drawing splits them."""
    channels, current = [], []
    for step in chain:
        current.append(step)
        if step[2].split(".")[0].startswith("J"):
            channels.append(current)
            current = []
    if current:
        channels.append(current)
    return channels


def page_plan(bench: dict) -> list[dict]:
    """One page per RF path with the parts only it uses, and one for what they share. A board with one path stays on one page."""
    channels = _channels(bench["chain"])
    if len(channels) < 2 or bench["layout"].get("schematic_pages") == "one":
        return []
    from .channel_groups import channel_owners, spread_bypass
    pins = pin_map(bench)
    seeds = [{m.split(".")[0] for row in channel for m in row[1:]} for channel in channels]
    owners = channel_owners(pins, seeds)
    owners.update(spread_bypass(pins, seeds, owners))
    plan = []
    for i, (channel, seed) in enumerate(zip(channels, seeds)):
        refs = seed | {r for r, o in owners.items() if o == i}
        first = channel[0][1].split(".")[0]
        last = channel[-1][2].split(".")[0]
        plan.append({"title": f"Channel {i + 1}", "file": f"ch{i + 1}", "refs": refs, "chain": channel, "note": f"{first} to {last}"})
    shared = set(bench["parts"]) - {r for page in plan for r in page["refs"]}
    if shared:
        plan.append({"title": "Shared", "file": "shared", "refs": shared, "chain": [], "note": "Parts serving more than one channel"})
    return plan


def _page_bench(bench: dict, page: dict) -> dict:
    refs = page["refs"]
    nets = {n: [m for m in ms if m.split(".")[0] in refs] for n, ms in bench["nets"].items()}
    return {**bench, "parts": {r: p for r, p in bench["parts"].items() if r in refs}, "chain": page["chain"],
            "nets": {n: ms for n, ms in nets.items() if ms}, "design_table": None,
            "open_pins": [m for m in bench.get("open_pins") or [] if m.split(".")[0] in refs], "summary": page["note"]}


def _file(uid: str, paper: str, title: str, libs: list[str], items: list[str], pages: str) -> str:
    return (f'(kicad_sch (version 20250114) (generator "edaloop") (generator_version "10.0") (uuid {_q(uid)}) (paper "{paper}") (title_block (title {_q(title)}) (comment 1 "Generated from the board description. Edit that, not this."))\n'
            f'(lib_symbols\n' + "\n".join([*POWER_LIBS, *libs]) + '\n)\n' + "\n".join(items) + f'\n{pages} (embedded_fonts no))\n')


def project_sheets(bench: dict, name: str) -> dict:
    """Every schematic file of a board, and what its board needs to agree with them. `files` always starts with the root."""
    plan = page_plan(bench)
    if not plan:
        text = schematic(bench, name)
        return {"files": {f"{name}.kicad_sch": text}, "library": symbol_library(text), "sheets": [[_uid(name, "root"), "Root"]],
                "uuids": {ref: _uid(name, "symbol", ref) for ref in bench["parts"]}, "placed": {}}
    root_id = _uid(name, "root")
    files, libs, uuids, placed, items, sheets = {}, [], {}, {}, [], [[root_id, "Root"]]
    columns = 4
    w, h = 50.8, 25.4
    for i, page in enumerate(plan):
        block = _uid(name, "sheet", page["file"])
        page_name = f"{name}/{page['file']}"
        sub = _page_bench(bench, page)
        sheet = Sheet(page_name, project=name, path=f"/{root_id}/{block}", power_from=(i + 1) * 1000)
        draw(sub, f"{name}: {page['title']}", sheet)
        filename = f"{name}_{page['file']}.kicad_sch"
        files[filename] = _file(_uid(name, "file", page["file"]), _paper(sheet.width, sheet.height), f"{name}: {page['title']}", sheet.libs, sheet.items, "")
        libs += sheet.libs
        sheets.append([block, page["title"]])
        for ref in page["refs"]:
            uuids[ref] = f"{block}/{_uid(page_name, 'symbol', ref)}"
            placed[ref] = {"name": f"/{page['title']}/", "file": filename}
        x, y = _snap(25.4 + (i % columns) * (w + 25.4)), _snap(50.8 + (i // columns) * (h + 17.78))
        items.append(f'(sheet (at {x} {y}) (size {w} {h}) (fields_autoplaced yes) (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0)) (uuid {_q(block)}) '
                     f'(property "Sheetname" {_q(page["title"])} (at {x} {_snap(y - 1.27)} 0) (effects (font (size 1.27 1.27)) (justify left bottom))) '
                     f'(property "Sheetfile" {_q(filename)} (at {x} {_snap(y + h + 1.27)} 0) (effects (font (size 1.27 1.27)) (justify left top))) '
                     f'(instances (project {_q(name)} (path {_q("/" + root_id)} (page {_q(str(i + 2))}))))) ')
        items.append(f'(text {_q(page["note"])} (exclude_from_sim yes) (at {_snap(x + 2.54)} {_snap(y + 7.62)} 0) (effects (font (size 1.27 1.27)) (justify left top)) (uuid {_q(_uid(name, "note", page["file"]))}))')
    items.append(f'(text {_q(name)} (exclude_from_sim yes) (at 25.4 25.4 0) (effects (font (size 2.54 2.54) (bold yes)) (justify left top)) (uuid {_q(_uid(name, "title"))}))')
    items.append(f'(text {_q(" ".join(str(bench.get("summary", "")).split()))} (exclude_from_sim yes) (at 25.4 31.75 0) (effects (font (size 1.27 1.27)) (justify left top)) (uuid {_q(_uid(name, "summary"))}))')
    root = _file(root_id, "A3", name, [], items, '(sheet_instances (path "/" (page "1")))')
    library = "(lib_symbols\n" + "\n".join([*POWER_LIBS, *libs]) + "\n)\n"
    return {"files": {f"{name}.kicad_sch": root, **files}, "library": symbol_library(library), "sheets": sheets, "uuids": uuids, "placed": placed}


def symbol_library(text: str) -> str:
    """The schematic's own symbols as a project library, so that ERC has one to compare them with."""
    start = text.index("(lib_symbols\n") + len("(lib_symbols\n")
    end = text.index("\n)\n", start)
    rows = text[start:end].replace('(symbol "edaloop:', '(symbol "')
    return f'(kicad_symbol_lib (version 20241209) (generator "edaloop") (generator_version "10.0")\n{rows}\n)\n'


def check_netlist(xml_path: Path, bench: dict) -> list[str]:
    """Compare an exported netlist with the description. Returns every difference. Connectivity is what a drawing must not change."""
    root = ET.parse(xml_path).getroot()
    got: dict[str, set] = {}
    for net in root.find("nets"):
        name = net.get("name")
        members = {f"{n.get('ref')}.{n.get('pin')}" for n in net}
        if not name.startswith("unconnected-"):
            got[name] = members
    want = {n: set(m) for n, m in bench["nets"].items()}
    problems = [f"{n}: missing from the schematic" for n in want if n not in got]
    problems += [f"{n}: not in the description" for n in got if n not in want and got[n]]
    for n in want:
        if n in got and got[n] != want[n]:
            problems.append(f"{n}: lacks {sorted(want[n] - got[n])}, adds {sorted(got[n] - want[n])}")
    refs = {c.get("ref") for c in root.find("components")}
    problems += [f"{r}: missing from the schematic" for r in bench["parts"] if r not in refs]
    return problems
