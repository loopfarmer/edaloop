# Exports

`edaloop export` writes a finished board in formats that tools other than KiCad 10 can open. Altium's Import Wizard, for example, refuses KiCad files newer than 6.0.

```bash
edaloop export
```

```bash
edaloop export --format eagle -o handoff
```

It writes into an `export` folder in the project, one folder per format. The command exits non-zero if any read-back check fails, and you shouldn't trust that format's files if it does. These aren't fabrication files, and `edaloop release` still makes those.

| Format | Folder | What's in it | How it's checked |
|---|---|---|---|
| KiCad 6.0 | `kicad6/` | The project in 6.0 syntax, with its local symbol and footprint libraries, and tables that point to them | KiCad's own 6.0 parser reads it back, and the netlist must match the original's net by net. Every token must be in 6.0's grammar, every symbol must keep its reference, and every board object must keep its geometry |
| EAGLE 7.7 XML | `eagle/` | `.brd` and `.sch` | Both validate against the 7.7 DTD. KiCad's EAGLE importer reads the board back and compares pads, nets, vias, tracks, pours, keepouts and outline. The schematic is parsed back and compared with the project's netlist. Results are in `export_report.txt` |

## Limits

- **Neither format has been opened in the tool it's for.** The checks use KiCad's readers, so they test the file, not Altium's or EAGLE's importer.
- **The EAGLE schematic is a connectivity carrier.** It has one boxed symbol per part with each pin on a labelled stub, so that an importer links schematic and board by net name. Keep the KiCad schematic's PDF as the drawing.
- **The EAGLE schematic has no independent reader here.** kicad-cli can't import an EAGLE schematic, so it's parsed back from its own XML and held against KiCad's netlist.
- **EAGLE export handles four-layer boards only.** A board with another layer count is refused.
- **EAGLE carries less than KiCad.** Pours are outlines that the importer refills. Courtyards aren't carried, and inner-layer keepouts become pour cutouts. A pad corner EAGLE can't express is listed in the report.
- **`xmllint` is needed for the DTD check.** Without it, the EAGLE export reports a failure.

`data/eagle-7.7.0.dtd` is CadSoft's DTD, unmodified, under CC BY-ND 3.0. Everything else here is under this repository's licence.
