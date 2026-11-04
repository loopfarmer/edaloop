# Routing by class, with solvers at each stage

Nets differ in what makes them good, so one router pass with one cost function serves them badly. `edaloop route --by-class` routes in order, on copies.

| Order | Class | What decides it | Sensor after the pass |
|---|---|---|---|
| 1 | RF | Straight spine from entry to exit pad, locked. Width found by the field-solver loop. Taps escape outward and keep clear of neighbouring pins | DRC during routing. `rf_path`, `rf_preserved`, `rf_reference`, `impedance`, `em_fast` afterwards |
| 2 | Power | Width from current, neck-down off, and kept off the RF reference plane | `power`, `drc` |
| 3 | Signal | Connect everything else, around 1 and 2, off the reference plane | `drc`, `rf_preserved`, `rules` |
| 4 | Release | Everything again, plus three-solver EM | the gate |

## What the RF pass does at a fine-pitch part

These came from a 49-net RF test board, where the first version shorted a QFN pin row.

- **Taps leave outward.** A stub runs out of the pad's far end, then bends at 45 degrees, and never runs along the pin row.
- **Necks are short.** A line narrows only where neighbouring pins require it, using the widest width that clears. The rest stays at full width.
- **Pins keep their escape room.** An IC pin gets a 1.2 mm corridor beyond its end, so a stub can't wall it off. A tap crosses a corridor only if it has no other way.
- **Unreachable taps are handed over.** They route at the line's width, and the log says which ones.

## The router must keep to your rules

Left alone, KiCadRoutingTools rewrites the project's rules beside its output so that its copper passes: on that test board, minimum via 0.56 to 0.25 mm, hole 0.2 to 0.15 mm, annular ring 0.178 to 0.05 mm, clearance 0.2 to 0.175 mm. So every call now:

- passes `--no-fix-drc-settings`, `--escalation board` and the project's own sizes,
- puts the original project file back afterwards and logs whether the router changed it,
- is graded by DRC against the original rules.

## Retries

If signals leave opens, the failed nets route first on the next attempt, one at a time in the order they failed, and everything else routes around them. There are up to four attempts, and the best is kept. KiCad's DRC decides whether a net connected, because the router's own report can disagree with it.

## Solver fidelity per stage

| Stage | Sensor | Time here | Purpose |
|---|---|---|---|
| While choosing width | circuitRF cross-section | about 3 s a step | Finds the width from the board's own stackup |
| Between RF edits | `em_fast`, EMerge Draft with adaptive mesh | 8 s | Trend. Within about 2% of the converged value here |
| Milestones | openEMS | 82 s | Shares no method with FEM |
| Release | Palace | 43 min under load | Third vote |

## Adaptive meshing results

All on the same 10 mm coupon, reading Zc at 3 GHz. Converged answers are about 49.1 ohm.

| EMerge setup | Tetrahedra | Time | Zc |
|---|---|---|---|
| Standard, no adaptation | 4,956 | 17 s under load | 43.8 |
| Standard, tighter edge mesh | 12,237 | 10 s | 46.7 |
| Draft, 4 refinement passes | 6,970 | 8 s | 48.1 |
| Standard, 3 refinement passes | 8,565 | 9 s | 48.1 |
| Standard, 6 refinement passes | 8,565 | 8 s | 48.1, stopped itself at the same mesh |
| Accurate, 3 refinement passes | 21,117 | 21 s | 49.2 |

Refining where the error estimate points beat spending nearly twice the elements uniformly. The remaining 2% gap to the converged value is why `em_fast` is a trend sensor and not release evidence.

## Limits

- RF spines are straight or use one 45-degree dogleg, with no obstacle avoidance. DRC stops the run if one is blocked.
- Taps can be longer than as-built: one test board has a combining net 15.7% over its floor where its hand-laid version is within 5%, and escape necks on the 49-net board total 4.6 mm at 0.3 mm.
- The corridor length is a fixed guess that suited a 0.56 mm via and a 0.5 mm pitch part.
- Retries stop at the first attempt with no opens, so they don't look for a better route.
- Via fences and pad tapers aren't drawn yet.
- `power` uses a resistor network and IPC-2221. It doesn't model pours or neighbouring heat. Via heating isn't checked. Thermal FEM isn't wired in.
- Power width is chosen from the rail's total current, once. There's no automatic retry if `power` then fails.
