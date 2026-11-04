#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Measure the RF path: where it is straight, where it is not, what it costs.

Running this IS the layout review. For every hop of every cascade it
reports

    off      how far the two pads sit off a common axis. A well-placed
             hop is axial -- the pads share an x or they share a y -- so
             the OFF-AXIS deviation is min(|dx|, |dy|) and a good one is
             zero. Do not assume the cascade runs horizontally: a board
             that is long and narrow runs it down the long side, and
             measuring dy alone then flags every correctly placed hop.
             Anything above 0.05 mm is placement, not routing.
    floor    the spanning-tree lower bound over the net's OWN pads --
             not the distance between the two endpoints, which counts a
             legitimate branch as excess on any net with a tap
    routed   what the router actually laid down
    over     routed - floor, absolute and per cent
    vias     vias in the net. On a surface-mount RF cascade the right
             number is usually zero, and a via that is not needed is a
             pair of impedance discontinuities and a stub.
    bends    direction changes on the net's polyline

    python3 rf_path.py --pcb board.kicad_pcb --chains chains.py
    python3 rf_path.py --pcb board.kicad_pcb --chains chains.py --all

--all lists every hop; the default lists only the ones with something
wrong and totals the rest.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kicad_pcb import Board, load_chains

DY_FLAG = 0.05          # mm off the common line before it is a defect


def measure(board, chains):
    rows = []
    for net, a, z in chains:
        pa, pz = board.index.get(a), board.index.get(z)
        if not pa or not pz:
            rows.append(dict(net=net, missing="%s.%s / %s.%s"
                             % (a[0], a[1], z[0], z[1])))
            continue
        fl = board.floor(net, a, z)
        rt = board.net_len(net)
        rows.append(dict(
            net=net, a=pa, b=pz, npads=len(board.net_pads(net)),
            off=min(abs(pz.x - pa.x), abs(pz.y - pa.y)),
            floor=fl, routed=rt, over=rt - fl,
            vias=board.net_vias(net), bends=board.net_bends(net),
            missing=None))
    return rows


def report(rows, title="RF paths", show_all=False):
    W = 92
    print("\n" + "=" * W)
    print("  %s" % title)
    print("=" * W)
    print("  %-14s %-19s %3s %6s %8s %8s %8s %5s %5s"
          % ("net", "entry-exit", "n", "off", "floor", "routed", "over",
             "vias", "bend"))
    tf = tr = 0.0
    tv = 0
    hidden = 0
    for r in rows:
        if r["missing"]:
            print("  %-14s MISSING PAD %s" % (r["net"], r["missing"]))
            continue
        tf += r["floor"]
        tr += r["routed"]
        tv += r["vias"]
        # dy only means something on a true in-line hop. A combining
        # node or any net that deliberately spans rows will show a large
        # dy and be perfectly well placed.
        r["off_bad"] = r["off"] > DY_FLAG and r["npads"] == 2
        interesting = (r["off_bad"] or r["vias"] or r["over"] > 0.05)
        if not (show_all or interesting):
            hidden += 1
            continue
        flag = ""
        if r["off_bad"]:
            flag += "  << off-axis"
        if r["vias"]:
            flag += "  << vias"
        print("  %-14s %-19s %3d %6.2f %8.2f %8.2f %8.2f %5d %5d%s"
              % (r["net"], "%s.%s-%s.%s" % (r["a"].ref, r["a"].num,
                                            r["b"].ref, r["b"].num),
                 r["npads"], r["off"], r["floor"], r["routed"], r["over"],
                 r["vias"], r["bends"], flag))
    if hidden:
        print("  %-14s %d hop(s) at the floor, no vias" % ("...", hidden))
    if tf:
        print("  %-38s %8.2f %8.2f %8.2f %5d  (%+.1f%%)"
              % ("TOTAL", tf, tr, tr - tf, tv, 100.0 * (tr - tf) / tf))
    return tf, tr, tv


def main():
    av = sys.argv[1:]

    def arg(flag, default=None):
        return av[av.index(flag) + 1] if flag in av else default

    pcb, ch = arg("--pcb"), arg("--chains")
    if not pcb or not ch:
        raise SystemExit(__doc__.strip())
    chains, _, _ = load_chains(ch)
    b = Board(pcb)
    rows = measure(b, chains)
    tf, tr, tv = report(rows, os.path.basename(pcb), "--all" in av)
    bad = [r for r in rows if not r["missing"] and r.get("off_bad")]
    if bad:
        print("\n  %d two-pad hop(s) off axis. That is PLACEMENT, and no"
              % len(bad))
        print("  amount of re-routing fixes it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
