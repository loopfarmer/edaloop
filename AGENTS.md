# Working on edaloop

Instructions for AI coding agents working in this repository. People should start with [README.md](README.md).

`CLAUDE.md` is a symlink to this file, so edit this one.

This file is about changing edaloop. To design a board with it, read [`edaloop/src/edaloop/agent/SKILL.md`](edaloop/src/edaloop/agent/SKILL.md).

## Layout

| Folder | Language | What it holds |
|---|---|---|
| `edaloop/src/edaloop/` | Python | The checks, the gate, the ledger, placement, routing and the MCP server |
| `edaloop/src/edaloop/scripts/` | Python | Standalone scripts with no dependencies. `rf_path.py` measures the RF path, and `kicad_pcb.py` reads a board as geometry |
| `edaloop/src/edaloop/export*.py` | Python | KiCad 6.0 and EAGLE 7.7 exports. `export_eagle.py` runs under KiCad's Python, because it needs `pcbnew` |
| `edaloop/tests/` | Python | The test suite, in `test_core.py` |
| `edaloop/testbenches/` | YAML | Board descriptions, which run with no KiCad files |
| `edaloop/evaluation/` | YAML | Production boards to test edaloop against, fetched at pinned commits |
| `edaloop/docs/` | Markdown, SVG | Reference docs, and the figures with the script that draws them |
| `boardfield/` | Rust, WGSL | Current and temperature fields |
| `boardhand/` | Rust | The guarded board-editing MCP server |

## Commands

Run these from `edaloop/`:

```bash
uv sync --extra dev
```

```bash
uv run pytest
```

```bash
uv run edaloop doctor
```

```bash
uv run edaloop bench
```

Build and test the Rust crates from their own folders with `cargo build --release` and `cargo test`.

To measure a board's RF path directly, without the ledger:

```bash
python3 src/edaloop/scripts/rf_path.py --pcb board.kicad_pcb --chains chains.yaml --all
```

It reports each hop's distance off axis, its spanning-tree floor, its routed length, its vias and its bends. `chains.yaml` lists each RF net's entry and exit pads, and `edaloop build` writes one. It is data, never code: a Python `chains.py` is refused, and `edaloop convert-chains chains.py` turns one into YAML. A hop off axis is a placement fault that re-routing can't fix. The floor is measured over the net's own pads, so a tap doesn't read as waste. `kicad_pcb.py` is the one place the pad transform lives, so don't copy it elsewhere.

The Python tests need no KiCad or solvers. They skip what they can't reach. A real change to layout or routing also needs a real run:

```bash
uv run edaloop make testbenches/gain_block_demo.yaml -o /tmp/demo --quick
```

If it changes what the figures show, redraw them with `docs/figures/build_figures.py`.

## Rules

1. **Never commit a real board.** No private board files, paths, project names or credentials. `.gitignore` blocks KiCad and fabrication files, and `gain_block_demo` is the board to use in examples. Search your diff for your own paths before committing.
2. **Commit and push only when asked.**
3. **Requirements and waivers belong to a person.** Don't add a tool, default or shortcut that changes them. A waiver needs a name and a reason, names one check exactly, and is bound to the finding's message or says `any_reading: true`. Waivers the controller suggests quote the message, so keep it that way.
4. **A crash is an error, never a pass.** A check that can't run must say so, and an unavailable reading must never look like a clean one.
5. **Evidence is tied to bytes.** If a check reads a new input, add it to the check's `inputs` so that changing it makes the result stale. A key that isn't a file goes in `checks.CONFIG_DIGESTS` and is recorded through `_config`, so the check and `current_inputs` work it out the same way. A file that isn't there is recorded as absent, so creating it makes the result stale. Each result also records its check's `version` and the tools it ran: list a check's toolchain keys in `@check(tools=...)`.
6. **Routers work on copies.** Nothing may move locked RF copper or lose a ground via without the gate seeing it.
7. **Say what a number is.** Mark assumptions and estimates, in code and in reports. Don't claim agreement with a reference you haven't compared against, and don't tune a setting until a result passes.

## Adding things

- **A check:** add it with `@check` in `checks.py`, with the toolchain keys it runs in `tools=`. Raise its `version` whenever a change could turn an old result over, such as a new rule, limit or default, so evidence from the old logic goes stale. Add it to the gate in `boardgen.project_yaml` if it should block release, and give `controller.REMEDY` an entry. Then say why it's the authority in `docs/AUTHORITY.md`.
- **An RF layout rule:** add it to `rf_rules.py`, build to it in `ground.py` or `route_classes.py`, and list it in `docs/rf-layout-rules.md`. Every limit must be changeable under `rf_layout:` in `edaloop.yaml`.
- **An export format:** add it to `export.py`. Read every file back through an independent reader where one exists, compare nets as well as positions, and fail loudly. Limits go in `docs/exports.md`.
- **A bundled stand-in footprint:** put it in `src/edaloop/data/footprints/`, begin its `descr` with `edaloop stand-in.`, say where it was copied from, and write `ASSUMED` if the package is a guess. `project_files` reads those words on the board, so a guessed package can't reach release unseen.
- **A part's ground-via pattern:** add it to `LAND_PATTERNS` in `rf_rules.py`, from the maker's own drawing.
- **An evaluation board:** pin a commit in `evaluation/samples.yaml` and name its licence. Never copy the board into this repository.
