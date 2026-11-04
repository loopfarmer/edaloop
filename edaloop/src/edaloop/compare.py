"""Two boards side by side: is what the flow made as good as the board it stands against?

Reads the files only. It's for a generated board against a hand-routed one of the same design, so it first says whether they are the same design.
"""
from __future__ import annotations

import math
from pathlib import Path

from .pcbfile import Board
from .thermal import _edge_bbox


def _facts(path: Path, rf_nets: list[str]) -> dict:
    board = Board(path)
    x1, y1, x2, y2 = _edge_bbox(board)
    length: dict[str, float] = {}
    vias: dict[str, int] = {}
    for t in board.tracks:
        if t.kind == "via":
            vias[t.net] = vias.get(t.net, 0) + 1
        else:
            length[t.net] = length.get(t.net, 0.0) + math.hypot(t.end[0] - t.start[0], t.end[1] - t.start[1])
    nets: dict[str, set] = {}
    for p in board.all_pads():
        if p["net"] and not p["net"].startswith("unconnected-"):
            nets.setdefault(p["net"], set()).add(p["name"])
    return {"size_mm": [round(x2 - x1, 2), round(y2 - y1, 2)], "area_mm2": round((x2 - x1) * (y2 - y1), 1), "copper_mm": round(sum(length.values()), 1),
            "vias": sum(vias.values()), "ground_vias": vias.get("GND", 0), "rf_mm": {n: round(length.get(n, 0.0), 2) for n in rf_nets},
            "rf_vias": sum(vias.get(n, 0) for n in rf_nets), "nets": nets}


def compare(first: Path, second: Path, rf_nets: list[str] | None = None) -> dict:
    """Facts for each board and the differences that matter. Nets that differ mean the two aren't the same design, and lengths then don't compare."""
    nets = list(rf_nets or [])
    a, b = _facts(first, nets), _facts(second, nets)
    a_nets, b_nets = a.pop("nets"), b.pop("nets")
    # Names can differ between flows, so a connection is the same if the same pads are joined. Only ground-free nets are compared by pads.
    a_sets = {frozenset(v) for v in a_nets.values()}
    b_sets = {frozenset(v) for v in b_nets.values()}
    only_a = sorted(n for n, v in a_nets.items() if frozenset(v) not in b_sets)
    only_b = sorted(n for n, v in b_nets.items() if frozenset(v) not in a_sets)
    a_rf, b_rf = sum(a["rf_mm"].values()), sum(b["rf_mm"].values())
    return {"first": str(first), "second": str(second), "same_connections": not only_a and not only_b,
            "nets_only_in_first": only_a, "nets_only_in_second": only_b, "first_board": a, "second_board": b,
            "rf_net_changes_mm": {n: round(a["rf_mm"][n] - b["rf_mm"][n], 2) for n in nets if abs(a["rf_mm"][n] - b["rf_mm"][n]) >= 0.01},
            "difference": {"rf_mm": round(a_rf - b_rf, 2), "rf_vias": a["rf_vias"] - b["rf_vias"], "area_mm2": round(a["area_mm2"] - b["area_mm2"], 1),
                           "copper_mm": round(a["copper_mm"] - b["copper_mm"], 1), "vias": a["vias"] - b["vias"]}}
