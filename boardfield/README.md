# boardfield

Solves current and temperature on a board's own copper, on the CPU or on the GPU through `wgpu` (Metal, Vulkan, DX12), with the same algorithm and matching answers.

A track-based check can't see pours, current crowding or nearby copper spreading heat. This rasterises every layer, solves each rail's potential, turns the dissipation into heat, and solves temperature across the whole board. Copper's resistance follows its temperature when the rise is large enough to matter.

```bash
cargo build --release
./target/release/boardfield problem.json -o result.json --png heat.png
```

`edaloop` writes `problem.json` from a `.kicad_pcb`, through its `thermal_field` check. Exit status is 0 if every solve converged, 1 if one didn't, and 2 on bad input.

## How it works

- **Coverage per cell.** 16 sample points in each cell, so a thin track keeps its width on a coarse grid. Conductance comes from how much copper crosses each face.
- **One stencil.** Current and heat share a layered 7-point stencil, solved by red-black SOR from coarse grids to fine.
- **Mixed precision.** The residual is exact in 64 bits on the CPU, and single precision finds the correction. It's written as conductance differences, because a 32-bit diagonal acts as a false leak.
- **Light meshes.** Heat uses a coarser grid than current, and laminates get thermal sublayers so that a narrow trace's spreading resistance isn't lost. Rails sweep only the cells holding copper.

## Accuracy

Run `benchmarks/thermal_validate.py` from `edaloop/` for the closed-form suite. GPU and CPU agree on every case.

Two laminate cases were also compared with a 3D finite-element solve (1 W, first-order elements). The field solver reads a little hot:

| Case | FEM rise | boardfield, 3 sublayers | Difference |
|---|---:|---:|---:|
| Narrow trace | 241.75 °C | 246.9 °C | +2.1% |
| Square patch | 164.25 °C | 171.47 °C | +4.4% |

Treat these as a rough check. The result still rises with more sublayers (trace: 237.8 °C at 1, 251.9 °C at 7), and first-order elements tend to read low. A refined FEM run to pin this down didn't finish.

## CPU or GPU

The CPU is the default, because the GPU isn't faster on the one machine tested, an M3 Pro that was also running other jobs. On a whole 8-layer board's temperature, with matching answers:

| Cells | GPU | CPU |
|---:|---:|---:|
| 7.7 million | 17.3 s, 1.12 G updates/s | 16.8 s, 1.14 G updates/s |
| 30.8 million | 131.8 s, 1.16 G updates/s | 121.3 s, 1.25 G updates/s |

Reading results back isn't the cause, since the sweep-only rate is the same. Shared memory bandwidth may be the limit, but that isn't proven. A discrete GPU hasn't been tried, so pass `--backend gpu` to find out. The large speedups came from the method: sweeping only the cells with copper took one case from 45 s to 11 s, and a coarser thermal grid took another from 221 s to 46 s.

## Limits

- This is a prototype. Convection is a single assumed coefficient on the two outer faces, 10 W/m2K by default, with insulated edges. Enclosures, airflow and radiation aren't modelled.
- A 45-degree trace reads about 4% high.
- The board is treated as its bounding rectangle.
- Part dissipation only counts where you state it.
- No result here has been compared with a measurement on a real board.
