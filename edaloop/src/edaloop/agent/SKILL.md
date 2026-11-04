---
name: edaloop
description: Drive a circuit design from capture to fabrication through one evidence-gated loop over atopile, KiCad, Freerouting and the circuitRF EM solvers. Use when designing, laying out, routing, simulating, reviewing or releasing a board in a folder with an edaloop.yaml, or when asked to judge whether a board is ready.
---

# The design loop

Call `next_action`. Do what it says. Call it again. The loop ends when `release` succeeds.

Tools come in two kinds, and keeping them apart is what makes the loop reliable:

- **Sensors** report on the design and change nothing: the edaloop checks.
- **Actuators** change the design: the KiCad MCP, `.ato` source, the routers, and the KiCad-side skills.

After any actuator, earlier evidence on those files is stale automatically. Never reason from a result you remember; ask for the gate.

## Which tool answers which question

| Question | Authority | Not this, and why |
|---|---|---|
| Is the wiring right? DRC, ERC | `kicad-cli`, through `drc` and `erc` | circuitRF DRC: its KiCad import carries no rules |
| Is the RF path straight and short? | `rf_path` | DRC cannot see a jog or a detour |
| Is it 50 ohm on the real copper? | `impedance`, then `em_crosscheck` | One solver alone: Palace Draft read 36.2 ohm on a 49 ohm line, with no warning |
| What does the chain do? | `cascade`, circuitRF `sparam` and `hb` | |
| Which parts? | atopile picking, pcbparts, JLCPCB tools | |
| Routing | `route_board`, or `edaloop route --by-class` for RF and power boards, then `adopt_route` | OrthoRoute, except on a large regular backplane |
| Will a trace heat or droop? | `power` | DRC: no rule knows the current |
| Is the RF plane intact? | `rf_reference`, and `rf_layout` across the trace's width | DRC allows copper under an RF line |
| Fence, maker's via patterns, pad gaps, supply on top, bias order | `rf_layout`. The rules and their reasons are in `docs/rf-layout-rules.md` | DRC can't see any of them |
| DC, transient, noise | `spice`, on the schematic's own netlist | circuitRF has none of these |
| Agent edits to a board | `boardhand`, which needs `expected_revision` | Writing the file directly |
| What does the routed copper add? | `em_hops`, each RF hop solved as routed | `em_crosscheck`, which only sees a straight coupon |
| New board from nothing | `make_board`, from a written description | Hand-placing an RF cascade |

## Starting from a description

An existing design gets a description from `describe_design`. Check its RF path, fill in `required`, and tell a person about `assumed`. `docs/board-description.md` in the edaloop repository lists the layout options, such as `columns` for a board with many channels.

`edaloop build` only places parts and draws the schematic. It doesn't route, so DRC and RF checks on its output fail and an EM run there is wasted time. To get a routed board, use `edaloop make`.

`make_board` takes a description like those in `testbenches/`, and returns `next_action` when it's done. Change the description and build into a new folder, never the output by hand. `review_pack` exports what a person needs to review, and only `release` writes fabrication files.

## Rules

1. The `.kicad_pcb` is the geometry authority. circuitRF reads it. Do not bring a circuitRF export back into KiCad: it loses pad nets.
2. Lay RF first, lock it, then route everything else. Ground vias go in after the route.
3. Do not run a router on the project board. `route_board` works on copies, and `adopt_route` refuses anything that moved RF copper or lost ground vias.
4. A clean DRC proves connectivity and clearance only. It says nothing about current, impedance or return paths.
5. Do not edit `requirements` or `waivers` to clear a failure. Report the failure and let a person decide. The same goes for the `gate:` list and for `.edaloop/rf_baseline.json`: an empty gate never releases, and a replaced baseline fails `rf_preserved` until a person restores it or waives `rf_preserved:baseline_changed`.
6. Route RF first, then power, then signal, and run that stage's sensors with `edaloop stage <name>` after each. Never let a router's width or clearance go below the board's rules, and never grade its output against a project file it wrote. `edaloop` pins and restores the rules. If you run a router by hand, do the same.
7. Read `docs/rf-layout-rules.md` in the edaloop repository before answering a review or choosing DC blocks, bypass capacitors or via fences.

## Reporting a result

Lead with the verdict, then the one thing that blocks it, then what a person has to do. Take every number from `edaloop --json status`, the ledger or a check's metrics, and don't quote one from memory.

- **`edaloop.yaml` decides what's waived.** A waiver in the ledger is history. If the two differ, say so and ask whether it was withdrawn on purpose.
- **Check what you're told against the ledger.** "It passed last week" and "I only nudged a track" are claims. Compare the file with the last checked copy, and report what really changed and by how much.
- **`em_crosscheck` solves a straight coupon from the stackup, so a board edit doesn't make it stale.** It says nothing about a track someone changed. Use `impedance` and `em_hops` for that.
- **`impedance` and `em_hops` answer different questions.** `impedance` reads the line. `em_hops` reads the whole hop, with its pads, and a pad's capacitance can hide a narrow track. A good hop reading doesn't approve a change that `rf_preserved` flagged.
- **Reasons are yours to label.** Say what you observed and what you're guessing. "Cause not diagnosed" is a good report. An invented cause isn't.
- **An `edaloop eval` scorecard isn't the release gate.** A gap in it is about the tool or a model, and `ready to release` on the evaluation's copy says nothing about the released product.

## When a reviewer asks for a part value

Measure before you change it. A value quoted as an ideal reactance can be wrong for a real part, and `rf_layout` reads a capacitor's estimate only to send you to its measured S-parameters. Report the numbers and a one-line way to change it, and let a person decide. Layout comments, such as moving a net inside or adding a via, are cheap and usually right, so make them and measure the result.

## Values you don't have

Currents, part heights, inductor values and limits come from the person, a datasheet or a measurement. Don't fill in a placeholder to get further. If another file in the repository seems to hold the value, it may be for a different revision: name the file, list what you took from it, and mark each value unconfirmed in the description and your report. When you can't get further without a value, stop there and ask for it.

Leave long commands running in the foreground, or wait for their process to end. `edaloop eval` prints progress to stderr, and the regenerate level can take many minutes.

## When `next_action` says `ask_person`

Stop and put its `question` to a person, with its `if_waived` block. Adding a waiver is their decision, not yours.

## When EM solvers disagree

`em_crosscheck` runs the solvers and returns a `diagnosis`. Act on its verdict:

- `agree`: use `zc_trusted_ohm`.
- `outlier`: the named solver is wrong and the others support each other. Try the `actions` entry on that solver only, then rerun. Finished solvers are not rerun.
- `no_majority`: nothing can be trusted yet. Add a solver or refine each one. Do not average them.
- `design_off_target`: the setup is fine and the design is not. Change the cross-section.

Use `em_fast` between edits. It's a light adaptive mesh that takes seconds and reads within about 2%, so treat it as a trend and not as release evidence.

An `actions` entry with `confidence: hypothesis` is a thing to test, so say so if you report it. A `loss_note` is often a modelling difference and not a fault.
