"""Which parts off the RF rows belong to one channel, from the nets alone.

Stdlib only, since KiCad's Python runs it as well.
"""
from __future__ import annotations

GROUND = {"GND", "AGND", "DGND", "PGND"}
BUS_PINS = 12     # a net on this many pins serves more than one channel, whatever it's called


def channel_owners(pins: dict[str, dict[str, str]], rows: list[set[str]]) -> dict[str, int]:
    """Each off-row part that serves exactly one row, as {ref: row index}. Parts serving several rows are left out.

    Ownership spreads out from each row at once, over nets that aren't ground or a bus. A net that ends up touching
    two rows is a bus. A part whose own nets reach two rows is shared, and ownership doesn't pass through it. Each
    such discovery restarts the spread, because anything labelled by way of it was labelled wrongly.
    """
    on_net: dict[str, set[str]] = {}
    for ref, by_pin in pins.items():
        for net in by_pin.values():
            on_net.setdefault(net, set()).add(ref)
    seeds = {ref: i for i, row in enumerate(rows) for ref in row}
    buses = {n for n, refs in on_net.items() if n in GROUND or len(refs) >= BUS_PINS}
    shared: set[str] = set()
    while True:
        owner = dict(seeds)
        frontier = dict(seeds)
        while frontier:
            found: dict[str, set[int]] = {}
            for ref, i in frontier.items():
                for net in set(pins.get(ref, {}).values()) - buses:
                    for other in on_net[net]:
                        if other not in owner and other not in shared:
                            found.setdefault(other, set()).add(i)
            frontier = {}
            for ref, reached in found.items():
                if len(reached) > 1:
                    shared.add(ref)
                else:
                    frontier[ref] = owner[ref] = next(iter(reached))
        new_buses = {n for n, refs in on_net.items() if n not in buses and len({owner[r] for r in refs if r in owner}) > 1}
        if new_buses:
            buses |= new_buses
            continue
        # A bus is a bus to its row parts, but an off-row part on two rows' private nets is shared, not owned.
        wrong = set()
        for ref in owner:
            if ref in seeds:
                continue
            nets = set(pins[ref].values()) - buses
            if len({owner.get(r, -1) for n in nets for r in on_net[n] if r != ref and r in owner}) > 1:
                wrong.add(ref)
        if not wrong:
            return {ref: i for ref, i in owner.items() if ref not in seeds}
        shared |= wrong


def _natural(ref: str) -> tuple:
    letters = ref.rstrip("0123456789")
    digits = ref[len(letters):]
    return letters, int(digits) if digits else -1


def spread_bypass(pins: dict[str, dict[str, str]], rows: list[set[str]], owners: dict[str, int]) -> dict[str, int]:
    """Decoupling that the nets can't tie to a row: when a rail has at least one capacitor per row, give each row an equal share.

    A capacitor from a shared rail to ground touches no row's own net, so nothing above places it. The schematic only says how many
    there are, and a rail with one per channel means one per channel. Any left over stay with the shared parts, at the rail's source.
    In reference order, since channels are numbered in that order.
    """
    seated = set(owners).union(*rows) if rows else set(owners)
    by_rail: dict[str, list[str]] = {}
    for ref, by_pin in pins.items():
        nets = set(by_pin.values())
        if ref in seated or len(by_pin) != 2 or len(nets) != 2 or not ref.startswith("C") or not nets & GROUND:
            continue
        by_rail.setdefault(next(iter(nets - GROUND)), []).append(ref)
    out = {}
    for caps in by_rail.values():
        if len(rows) > 1 and len(caps) >= len(rows):
            share = len(caps) // len(rows)
            for i, ref in enumerate(sorted(caps, key=_natural)[:share * len(rows)]):
                out[ref] = i // share
    return out


def _overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def seat(shapes: dict[float, dict], obstacles: list, net_pads: dict[str, list], region: tuple, growth_cost: float = 6.0,
         step: float = 0.25, stack_low: float | None = None, stack_high: float | None = None, axis: str = "x"):
    """The cheapest free place for one part. Returns (x, y, rotation) or None.

    `shapes` holds each rotation's box around the part origin and its pads as (net, dx, dy). The cost is the Manhattan distance from
    each pad to the nearest pad on its net, plus a charge for pushing the row past its present extent, since that costs board length.
    Every position is costed first and then tried cheapest first, so most are never tested for overlap.
    """
    x0, y0, x1, y1 = region
    candidates = []
    nx, ny = int((x1 - x0) / step) + 1, int((y1 - y0) / step) + 1
    for rot, shape in shapes.items():
        bx0, by0, bx1, by1 = shape["box"]
        linked = [(dx, dy, net_pads[net]) for net, dx, dy in shape["pads"] if net in net_pads]
        for i in range(nx):
            x = x0 + i * step
            if x + bx0 < x0 or x + bx1 > x1:
                continue
            for j in range(ny):
                y = y0 + j * step
                if y + by0 < y0 or y + by1 > y1:
                    continue
                cost = 0.0
                for dx, dy, targets in linked:
                    cost += min(abs(x + dx - tx) + abs(y + dy - ty) for tx, ty in targets)
                low, high = (y + by0, y + by1) if axis == "x" else (x + bx0, x + bx1)
                if stack_high is not None and high > stack_high:
                    cost += growth_cost * (high - stack_high)
                if stack_low is not None and low < stack_low:
                    cost += growth_cost * (stack_low - low)
                candidates.append((cost, x, y, rot))
    candidates.sort(key=lambda c: c[0])
    for _, x, y, rot in candidates:
        bx0, by0, bx1, by1 = shapes[rot]["box"]
        box = (x + bx0, y + by0, x + bx1, y + by1)
        if not any(_overlap(box, o) for o in obstacles):
            return x, y, rot
    return None


def seating_order(refs: list[str], pins: dict[str, dict[str, str]], seated: set[str]) -> list[str]:
    """Most connected to what's already down first, then the part with most pins, so a chain of parts grows outward from the row."""
    left, order, seated = set(refs), [], set(seated)
    on_net: dict[str, set[str]] = {}
    for ref, by_pin in pins.items():
        for net in set(by_pin.values()) - GROUND:
            on_net.setdefault(net, set()).add(ref)
    while left:
        def rank(ref):
            links = sum(1 for n in set(pins[ref].values()) - GROUND if on_net[n] & seated)
            return (-links, -len(pins[ref]), _natural(ref))
        best = min(left, key=rank)
        order.append(best)
        left.discard(best)
        seated.add(best)
    return order
