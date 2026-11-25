"""Why placed parts don't fit their outline, as numbers and as the one change that would fit them.

No dependencies: `kicad_ops.py` imports it under KiCad's Python, and `boardgen` and the tests import it as part of edaloop.
"""
from __future__ import annotations

MARGIN_MM = 0.5


def overflow(span: tuple, outline: tuple, axis: str, squared: list | None = None, margin: float = MARGIN_MM) -> dict | None:
    """None when parts spanning (left, right, top, bottom) sit inside a w x h outline from the origin. Otherwise the overflow:
    how far past each edge, the row's length along `axis` and its width across, and a hint naming what to change."""
    left, right, top, bottom = (float(v) for v in span)
    w, h = (float(v) for v in outline)
    over = {"left": margin - left, "right": right - (w - margin), "top": margin - top, "bottom": bottom - (h - margin)}
    over = {k: round(v, 2) for k, v in over.items() if v > 1e-9}
    if not over:
        return None
    other = "y" if axis == "x" else "x"
    size = {"x": right - left, "y": bottom - top}
    room = {"x": w - 2 * margin, "y": h - 2 * margin}
    along, across = size[axis], size[other]
    # The row runs along the axis. If it's too long for the board that way but would fit turned, the axis is the wrong one.
    turned = along > room[axis] and along <= room[other] and across <= room[axis]
    if turned:
        hint = (f"the RF row runs along {axis} and is {along:.2f} mm long, but the board is {w:g} mm along x and {h:g} mm along y: "
                f"the row fits along {other}. Set layout.axis: {other}")
    elif along > room[axis]:
        hint = (f"the RF row is {along:.2f} mm long along {axis}, {along - room[axis]:.2f} mm more than the {room[axis]:g} mm inside the "
                f"margins. Shorten it: layout.gap_mm sets the space between parts")
    elif across > room[other]:
        hint = (f"the parts are {across:.2f} mm wide across the row, {across - room[other]:.2f} mm more than the {room[other]:g} mm "
                f"inside the margins along {other}")
    else:
        low, high = ("top", "bottom") if axis == "x" else ("left", "right")
        start, end = ("left", "right") if axis == "x" else ("top", "bottom")
        if low not in over and high not in over:
            # Past an end of the row only, with its length fitting: the placer started it too near that end. Nothing in the description
            # moves the start, so the hint says what happened rather than what to set.
            edge = start if start in over else end
            hint = (f"the parts sit {over[edge]:.2f} mm past the {edge} edge, though they span {along:.2f} mm along {axis}, which fits "
                    f"inside the margins: the placer started them too near that edge")
        elif low in over:
            hint = f"the parts sit {over[low]:.2f} mm past the {low} edge: move layout.line {over[low]:.2f} mm or more away from it"
        else:
            hint = f"the parts sit {over[high]:.2f} mm past the {high} edge: move layout.line {over[high]:.2f} mm or more away from it"
        if (low in over or high in over) and along <= room[axis] and (over.get(start) or over.get(end)):
            hint += f", and the row is {along:.2f} mm long along {axis}, which fits"
    return {"error": "outline_overflow", "span_mm": {"x": [round(left, 2), round(right, 2)], "y": [round(top, 2), round(bottom, 2)]},
            "outline_mm": [w, h], "margin_mm": margin, "axis": axis, "row_mm": round(along, 2), "across_mm": round(across, 2),
            "over_mm": over, "squared": list(squared or []), "fits_on_axis": other if turned else None, "hint": hint}


def message(report: dict) -> str:
    """The overflow as one line: the span, the outline, and the hint."""
    (left, right), (top, bottom) = report["span_mm"]["x"], report["span_mm"]["y"]
    w, h = report["outline_mm"]
    squared = report["squared"]
    return ("parts span x %.2f..%.2f, y %.2f..%.2f, outside the %g x %g mm outline with a %g mm margin%s. %s"
            % (left, right, top, bottom, w, h, report["margin_mm"], ", with %s set square" % ", ".join(squared) if squared else "",
               report["hint"][0].upper() + report["hint"][1:]))
