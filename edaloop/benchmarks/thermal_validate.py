#!/usr/bin/env python3
"""Check boardfield against closed forms, on both backends. Prints one row per case and exits non-zero on a miss.

    .venv/bin/python benchmarks/thermal_validate.py WORK_DIR [BOARDFIELD]
"""
import json
import math
import subprocess
import sys
from pathlib import Path

RHO = 1.72e-8
K_CU = 390.0
T_CU = 35e-6
SHEET_R = RHO / T_CU
LAYERS2 = [{"name": "F.Cu", "thickness_um": 35}, {"name": "B.Cu", "thickness_um": 35}]


def pad(name, net, x, y, hw=0.05, hh=0.05, layers=(0,)):
    return {"name": name, "net": net, "layers": list(layers), "x": x, "y": y, "hw": hw, "hh": hh}


def run(work: Path, name: str, problem: dict, backend="both"):
    f = work / f"{name}.json"
    out = work / f"{name}.out.json"
    problem.setdefault("settings", {})
    f.write_text(json.dumps(problem))
    r = subprocess.run([sys.argv[2] if len(sys.argv) > 2 else str(BIN), f, "--backend", backend, "-o", out], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise SystemExit(f"{name}: boardfield failed: {r.stderr[-300:]}")
    return json.loads(out.read_text())


def row(rows, case, measure, value, expected, limit_pct, backends):
    err = 100 * (value - expected) / expected
    ok = abs(err) <= limit_pct
    rows.append({"case": case, "measure": measure, "boardfield": round(value, 4), "closed_form": round(expected, 4),
                 "error_pct": round(err, 3), "limit_pct": limit_pct, "ok": ok, "gpu_vs_cpu_pct": backends})
    print(f"{'ok  ' if ok else 'MISS'} {case:34} {measure:12} {value:12.4f} vs {expected:12.4f}  {err:+7.3f}%  gpu-cpu {backends}%", flush=True)


def spread(results, get):
    values = [get(o) for o in results]
    return round(100 * abs(values[0] - values[-1]) / max(abs(values[0]), 1e-30), 5)


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    rows = []

    # 1. A straight trace at three grids, including a grid coarser than the trace.
    for dx in (0.4, 0.2, 0.1, 0.05):
        length, width, amps = 40.0, 0.2, 1.0
        p = {"bbox": [0, -2, length, 2], "layers": LAYERS2, "dielectrics_mm": [1.5],
             "tracks": [{"layer": 0, "net": "V", "a": [0, 0], "b": [length, 0], "w": width}],
             # Pads cover whole cells at each end, so the held length is known exactly.
             "pads": [pad("S.1", "V", dx / 2, 0, dx / 2, dx), pad("L.1", "V", length - dx / 2, 0, dx / 2, dx)],
             "rails": [{"net": "V", "source": "S.1", "loads": {"L.1": amps}}],
             "settings": {"dx_mm": dx, "coupled": 1}}
        res = run(work, f"trace_{dx}", p)
        got = res[-1]["rails"][0]["drops_mv"]["L.1"]
        # The source is held at one cell's centre and the load is read at the far cell's, so L - dx apart.
        expect = amps * SHEET_R * ((length - dx) * 1e-3) / (width * 1e-3) * 1000
        row(rows, f"straight trace, dx {dx} mm", "drop mV", got, expect, 1.5, spread(res, lambda o: o["rails"][0]["drops_mv"]["L.1"]))

    # 2. A trace at 45 degrees, the worst case for a square grid.
    for dx in (0.1, 0.05):
        side = 20.0
        length = side * math.sqrt(2)
        p = {"bbox": [-1, -1, side + 1, side + 1], "layers": LAYERS2, "dielectrics_mm": [1.5],
             "tracks": [{"layer": 0, "net": "V", "a": [0, 0], "b": [side, side], "w": 0.3}],
             "pads": [pad("S.1", "V", 0, 0, 0.3, 0.3), pad("L.1", "V", side, side, 0.3, 0.3)],
             "rails": [{"net": "V", "source": "S.1", "loads": {"L.1": 1.0}}],
             "settings": {"dx_mm": dx, "coupled": 1}}
        res = run(work, f"diag_{dx}", p)
        got = res[-1]["rails"][0]["drops_mv"]["L.1"]
        expect = SHEET_R * (length * 1e-3) / 0.3e-3 * 1000
        row(rows, f"45-degree trace, dx {dx} mm", "drop mV", got, expect, 5.0, spread(res, lambda o: o["rails"][0]["drops_mv"]["L.1"]))

    # 3. Current splits between two parallel paths by their conductance.
    dx = 0.05
    p = {"bbox": [-1, -3, 21, 3], "layers": LAYERS2, "dielectrics_mm": [1.5],
         "tracks": [{"layer": 0, "net": "V", "a": [0, -2], "b": [0, 2], "w": 1.0},
                    {"layer": 0, "net": "V", "a": [20, -2], "b": [20, 2], "w": 1.0},
                    {"layer": 0, "net": "V", "a": [0, 2], "b": [20, 2], "w": 0.2},
                    {"layer": 0, "net": "V", "a": [0, -2], "b": [20, -2], "w": 0.6}],
         "pads": [pad("S.1", "V", 0, 0, 0.5, 0.5), pad("L.1", "V", 20, 0, 0.5, 0.5)],
         "rails": [{"net": "V", "source": "S.1", "loads": {"L.1": 2.0}}],
         "settings": {"dx_mm": dx, "coupled": 1}}
    res = run(work, "parallel", p)
    got = res[-1]["rails"][0]["drops_mv"]["L.1"]
    # Each path: the 1 mm bar from pad edge to corner at both ends (1.5 mm each), and an arm that shows
    # 19 mm between the two 1 mm bars. The corners make this an estimate, hence the wider limit.
    bars = 2 * SHEET_R * 1.5e-3 / 1e-3
    path = lambda w: bars + SHEET_R * 19e-3 / (w * 1e-3)
    expect = 2.0 * (path(0.2) * path(0.6) / (path(0.2) + path(0.6))) * 1000
    row(rows, "two parallel paths", "drop mV", got, expect, 5.0, spread(res, lambda o: o["rails"][0]["drops_mv"]["L.1"]))

    # 4. Through a via: a trace on each layer and one barrel.
    p = {"bbox": [-1, -1, 21, 1], "layers": LAYERS2, "dielectrics_mm": [1.5],
         "tracks": [{"layer": 0, "net": "V", "a": [0, 0], "b": [10, 0], "w": 1.0}, {"layer": 1, "net": "V", "a": [10, 0], "b": [20, 0], "w": 1.0}],
         "vias": [{"net": "V", "x": 10, "y": 0, "size": 0.6, "drill": 0.3}],
         "pads": [pad("S.1", "V", 0.05, 0, 0.05, 0.5), pad("L.1", "V", 19.95, 0, 0.05, 0.5, (1,))],
         "rails": [{"net": "V", "source": "S.1", "loads": {"L.1": 1.0}}],
         "settings": {"dx_mm": 0.05, "coupled": 1}}
    res = run(work, "via", p)
    got = res[-1]["rails"][0]["drops_mv"]["L.1"]
    r_via = RHO * 1.5e-3 / (math.pi * ((0.15e-3 + 25e-6) ** 2 - (0.15e-3) ** 2))
    expect = (SHEET_R * 19.9e-3 / 1e-3 + r_via) * 1000
    row(rows, "trace, via, trace", "drop mV", got, expect, 5.0, spread(res, lambda o: o["rails"][0]["drops_mv"]["L.1"]))

    # 5. Uniform copper, evenly heated: each face sheds half, so the rise is exact.
    p = {"bbox": [0, 0, 20, 20], "layers": LAYERS2, "dielectrics_mm": [1.5],
         "polys": [{"layer": 0, "net": "G", "pts": [[0, 0], [20, 0], [20, 20], [0, 20]]}],
         "parts": [{"name": "H", "layer": 0, "x": 10, "y": 10, "hw": 10, "hh": 10, "watts": 1.0}],
         "settings": {"dx_mm": 0.2, "thermal_dx_mm": 0.2, "h_w_m2k": 10, "coupled": 1}}
    res = run(work, "plate", p)
    # Heat goes in on the top layer, so half crosses 1.5 mm of laminate to leave by the bottom face.
    area = (20e-3) ** 2
    expect = 1.0 / (2 * 10 * area) + 0.5 * (0.5 / area) * 1.5e-3 / 0.35
    row(rows, "evenly heated plate, top layer", "rise C", res[-1]["hotspot"]["rise_c"], expect, 1.0, spread(res, lambda o: o["hotspot"]["rise_c"]))
    row(rows, "evenly heated plate, top layer", "heat out W", res[-1]["heat_out_w"], 1.0, 2.0, spread(res, lambda o: o["heat_out_w"]))

    # 6. A heated line across a long strip: the 1D fin, T = P' / (2 K m) at the line, m^2 = 2h / K.
    length, width = 400.0, 2.0
    poly = [[0, 0], [length, 0], [length, width], [0, width]]
    p = {"bbox": [0, 0, length, width], "layers": LAYERS2, "dielectrics_mm": [0.1],
         "polys": [{"layer": 0, "net": "G", "pts": poly}, {"layer": 1, "net": "G", "pts": poly}],
         # Heated equally on both layers, so that no heat crosses the laminate and the problem is truly 1D.
         "parts": [{"name": "H", "layer": 0, "x": length / 2, "y": width / 2, "hw": 0.1, "hh": width / 2, "watts": 0.1},
                   {"name": "I", "layer": 1, "x": length / 2, "y": width / 2, "hw": 0.1, "hh": width / 2, "watts": 0.1}],
         "settings": {"dx_mm": 0.2, "thermal_dx_mm": 0.2, "h_w_m2k": 10, "k_in_plane": 0.8, "coupled": 1}}
    res = run(work, "fin", p)
    sheet = 2 * K_CU * T_CU + 0.8 * 0.1e-3      # both copper layers, plus the laminate they share
    m = math.sqrt(2 * 10 / sheet)
    expect = (0.2 / (width * 1e-3)) / (2 * sheet * m)
    row(rows, "1D fin, heated line", "rise C", res[-1]["hotspot"]["rise_c"], expect, 2.0, spread(res, lambda o: o["hotspot"]["rise_c"]))

    misses = [r for r in rows if not r["ok"]]
    (work / "validation.json").write_text(json.dumps(rows, indent=1))
    print(f"{len(rows) - len(misses)} of {len(rows)} within limits")
    sys.exit(1 if misses else 0)


BIN = Path(__file__).resolve().parent.parent.parent / "boardfield/target/release/boardfield"
main()
