# openEMS reads high on narrow coplanar lines

Found on 2026-11-03 with circuitRF 1.6.5 and openEMS 0.37.0-rc3, on a 0.355 mm line with 0.3 mm gaps, on 0.2104 mm of FR-4 at 3 GHz. The other readings at the same width:

| Method | Z0 |
|---|---:|
| EMerge, Accurate | 49.57 Ω |
| Palace, Standard | 49.50 Ω |
| 2D cross-section (`em_reference.py`) | 50.08 Ω |

## openEMS with its grid refined by hand

| x cell at the edges | Cells through the 0.21 mm dielectric | Z0 | eps_eff |
|---:|---:|---:|---:|
| 129 µm (the default at any cells-per-wavelength tried) | 7, graded | 53.24 Ω | 3.19 |
| 40 µm | 12 | 52.71 Ω | 3.28 |
| 20 µm | 12 | 52.13 Ω | 3.29 |
| 20 µm | 24 | 51.77 Ω | 3.22 |
| 10 µm | 24 | 51.43 Ω | 3.22 |

Other runs:

- Refining x alone, leaving the default z lines, pushes eps_eff up to about 3.67, so z has to be refined too.
- `ThirdsRule: false` reads 45.31 Ω on the default grid.
- A 20 mm line instead of 10 mm reads 53.01 Ω, so about 0.2 Ω of the excess comes from the port.

## What's established

- The edge cell comes from a clamp: 3/7 of the gap in the same layer, or 129 µm here. Raising cells per wavelength from 40 to 100 didn't change it, or the result by more than 0.1 Ω.
- Refinement converges slowly towards the other readings, and stops about 3% above the 2D reference at 567,000 cells.
- circuitRF 1.0.24 read 48.99 Ω on a 0.36 mm coupon, with a different grid. It wasn't rerun here, so its agreement isn't proof that its grid was better. Grid lines on the edges read low on this grid.

## What isn't

- Which part of the remaining 3% comes from the grid, the port or the extraction.
- Whether a thirds rule on the copper's top and bottom faces would close it.

## Three solvers on gain_block_demo

Run on 2026-11-07 with circuitRF 1.7.0 on gain_block_demo's 0.36 mm coupon at 3 GHz. Palace 0.18.1 was built through Spack and openEMS was 0.37.0:

| Solver | Z0 | eps_eff | Against the 2D reference (49.7 ohm) |
|---|---:|---:|---:|
| Palace, Standard | 49.15 ohm | 3.319 | -1.1% |
| EMerge, Accurate | 49.17 ohm | 3.228 | -1.1% |
| openEMS, 40 cells per wavelength | 52.81 ohm | 3.194 | +6.3% |

Palace and EMerge agree within 0.04%, and the 2D reference backs both. `em_crosscheck` names openEMS as the outlier (+7.4% in Zc) and still fails, as its 2% spread limit says it should. The Palace solve took 29 minutes on 4 cores.

## In edaloop

The cross-check reports each solver against the 2D reference, and still fails when solvers differ by more than the limit. A person decides whether to waive that.

## Reading the line away from its ports

To rule out port distortion, voltage and current probes were added at mid-line, with the same model and only the grid lines changed. The ratio is taken going each way, and the geometric mean cancels any small reflection. This run used a 0.36 mm line at 3.92 GHz, where the 2D reference reads 49.7 Ω.

| Edge cell in x | Cells through the dielectric | FDTD cells | Z0 at mid-line |
|---:|---:|---:|---:|
| Default grid | Default | 124,362 | 51.84 Ω |
| 129 µm | 7 | 166,698 | 52.12 Ω |
| 60 µm | 14 | 176,351 | 54.83 Ω |
| 30 µm | 14 | 200,263 | 54.20 Ω |
| 15 µm | 20 | 279,055 | 53.72 Ω |

The excess stays with the ports far away, so the port isn't its cause. The finest grid here is about 8% high, and refining from 60 µm brings it down by only about 1 Ω. Re-meshing doesn't bring openEMS within 2% at these cell counts, so it can't clear a 2% agreement requirement. The cause is still unknown.
