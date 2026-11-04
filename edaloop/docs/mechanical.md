# Test mounts and cases

```bash
edaloop mechanical
```

This writes `mechanical/<name>_mechanical.scad` from the board and, if OpenSCAD is installed, renders each part to STL and checks that each is a closed solid. Set `openscad` in the toolchain config if it's somewhere else.

| Part | What it is |
|---|---|
| `test_mount` | A tray that carries the board on a rim, with locating pins in any mounting holes and pogo-pin holes for pads reachable from below. |
| `test_clamp` | A plate that screws to the mount, holds the board down, and carries pogo-pin holes for pads on top. |
| `case_bottom`, `case_top` | A tray and a screw-on lid, with openings for connectors. |
| `assembly` | The case and board together, with the lid lifted, for checking by eye. |

## What comes from the board

- Outline, thickness, and mounting holes of 2 mm drill or larger.
- Where each part sits, and which connectors reach an edge. Those open through the wall, and connectors further in open through the lid.
- Probe points at each rail's source and loads, plus ground, from `power.rails`.
- Pegs near each corner, on clear ground, to hold down a board with no mounting holes.

## What to check

- **Part heights are rough figures by part type.** A board file doesn't hold them. Give real ones under `mechanical.part_height_mm: {U1: 2.4}`, because the lid's clearance depends on the tallest part. The result lists every height it assumed.
- **Outlines are treated as rectangles.** A board with a cut-out or a curved edge gets a case round its bounding box.
- **Nothing has been printed.** The closed-solid check says a part should slice, not that it fits. Print the mount first and try a board in it.
- **A closed case traps heat,** and the thermal check assumes open air. See `thermal_field` before closing up a warm board.
