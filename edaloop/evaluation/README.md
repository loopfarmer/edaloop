# Evaluation samples

These are production boards. Most are Adafruit's. They ship as products and their design files are public, so they're known to work. If edaloop fails one, that's a finding about edaloop first: it either misread the board, or one of its checks is wrong for a design that demonstrably works.

```bash
uv run edaloop eval
```

```bash
uv run edaloop eval lora buck qtpy --level checks
```

`samples.yaml` pins each board to a commit. The files are fetched into `~/.cache/edaloop/evaluation` and aren't stored in this repository. Adafruit's designs are CC BY-SA 3.0 and stay under that licence, in Adafruit's repositories.

The last five samples are open RF designs in native KiCad, from Great Scott Gadgets, LibreVNA and Lime Microsystems. Each entry names its own licence. A KiCad file gets upgraded on a copy with `kicad-cli pcb upgrade`, and `import.*` then holds that copy against the file as released. A sample that names its `project` also gets that project's custom rules file, `.kicad_dru`, when the commit has one, since KiCad's DRC reads it. These boards have no verified RF path, stated load or stackup here unless `samples.yaml` says so, so most readings on them are reported, not judged.

`--level regenerate` rebuilds a board from its netlist and holds the result against the released layout. It needs an RF path, because the placer only handles RF rows.

The scorecard judges edaloop, not the board, and it isn't the release gate. A check can read `ready to release` on the evaluation's copy while the scorecard has a gap, because the gate only needs warnings to be seen. Progress goes to stderr and the scorecard to `scorecard.json`.

## Results

Each item is one of:

| Result | Meaning |
|---|---|
| `pass` | edaloop agrees with the released file or the board's stated facts. |
| `gap` | edaloop disagrees with a working board, or can't handle it. |
| `info` | A reading with nothing known to hold it against. |

## What's compared

| Item | Known result it's held against |
|---|---|
| `import.copper`, `import.nets` | Track, via and net counts in the released Eagle or KiCad file. |
| `describe.rf_path` | The RF path each board really has, or that it has none. |
| `known.opens`, `known.drc` | A shipping board has no opens and meets the clearances in its own file. |
| `known.fabricator` | It was fabricated, so a fabricator-limit failure is suspect. |
| `regen.connects` | A rebuild has no opens or errors, and joins the same pads as the released board. |
| `fritzing.pins`, `fritzing.pads`, `fritzing.size` | Adafruit's own Fritzing part for the board, drawn separately from its netlist. Parts are pinned by commit, fetched at run time, and CC BY-SA. |
| `power.drop`, `power.heat` | The load the README states it runs at continuously. |

## What this doesn't prove

- These boards are 2-layer or simple 4-layer, with no stated stackup except where `samples.yaml` says so. Impedance and EM readings use assumed stackups, so they're reported, not judged.
- No board comes with measured results. "Known good" means it ships and works, not that any number here has been measured.
- A pass means edaloop raised no false alarm on a good board. It doesn't show that edaloop catches faults.
