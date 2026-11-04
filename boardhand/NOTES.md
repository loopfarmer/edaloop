# boardhand: implementation notes

Fixture names and paths in this file and in SPEC.md were replaced with generic ones before publishing, and wording about the project's origin was edited. The results below didn't change.

## Sources consulted

- `SPEC.md`, the only source of requirements.
- Local tools, run on copies and observed directly:
  - `kicad-cli` 10.0.5 and its `--help` text
  - KiCad 10.0.5's bundled Python `pcbnew`, which exported a DSN and served as a reference for footprint moves
  - Freerouting 2.3.0 from `~/.kicad-mcp/freerouting.jar`: its `--help` text and the DSN and session files from real runs
- The fixture boards named in section 13, and the report JSON beside the small fixture, which showed kicad-cli's report shape.
- My general knowledge of KiCad's file format, the Specctra format, MCP and JSON-RPC.
- crates.io, through Cargo, to download dependencies: `serde_json`, `sha2`, `uuid`, `libc` and what they pull in.

I fetched no web pages, and didn't look at any other KiCad MCP server's code.

## Test results

Final run, 2026-11-03, rustc 1.91.1:

| Command | Result |
|---|---|
| `cargo build --release` | builds |
| `cargo test` (debug) | 71 passed, 0 failed |
| `cargo test --release` | 71 passed, 0 failed, 0 ignored |
| `cargo clippy -- -D warnings` | clean |
| `cargo clippy --all-targets -- -D warnings` | clean |

After the folder was renamed, a clean rebuild's first `cargo test --release` failed `t7_cli_timeout_kills_the_process_group` once, and its message wasn't captured. The next five full runs and eight runs of that test alone all passed, so treat it as a possible timing flake under load.

No test skipped: all fixtures, `kicad-cli`, KiCad's Python and the Freerouting jar were present, and the release run printed no skip notices.

| # | Required test | Result |
|---|---|---|
| 1 | Byte-identical parse and write | Pass on both boards: the small fixture 114,673 bytes, large board 9,709,779 bytes |
| 2 | Parse under 2 s, release | Pass: 12.9 ms in the final run, for the parse alone after the file was read |
| 3 | End-to-end MCP session | Pass, against `target/release/boardhand` under `cargo test --release` |
| 4 | Add then delete | Pass on both boards: byte-identical, same SHA-256 |
| 5 | Refusals leave the file unchanged | Pass: revision conflict, both lock names, locked segment and via, and the named refusals |
| 6 | Kill test | Pass: recover, abandon, divergent file |
| 7 | DRC and fake `kicad-cli` | Pass. Real DRC on an the small fixture copy: 4 findings, all `lib_footprint_issues`. `cli_crashed`, `cli_timeout` and `no_report` pass with fakes |
| 8 | `route_apply` | Pass: omitted via and omitted locked segment refused and output deleted; preserved session applied, and KiCad loaded it |

Section 13 expected values are asserted: the small fixture has 121 vias, all on `GND`, and 12 nets by pad usage.

### Test 4: what differs

Nothing differs. After add then delete, both files match the originals byte for byte. The add inserts one span, and the delete removes that span and the whitespace before it. So there's no format difference to explain.

### Real round trip (beyond the required tests)

On an the small fixture copy, using KiCad's exporter and real Freerouting:

- The session held 14 wires and 53 vias. All 7 nets and all layers resolved.
- Freerouting's session omitted 76 of the protected items. `route_apply` refused with `protected_copper_lost` and deleted the output.
- With `retain_protected: true`, the output loaded in KiCad and kept all 121 vias.
- That output's DRC showed 125 violations, but the check ran with no `.kicad_pro` beside the output, so KiCad used default rules. I didn't assess routing quality.

## Interpretations you may want to review

- **`route_apply` replaces copper.** The session is authoritative for tracks and vias, as section 13's test requires: if the input's protected items were always kept, a session could never lose one. A session item that matches an existing item within half a resolution step keeps that item's original bytes, uuid and lock.
- **Consequence on real boards.** On the small fixture, real Freerouting always omits protected copper, so a plain `route_apply` is refused. I added an optional `retain_protected` argument, which the spec doesn't ask for and which is off by default.
- **Y axis.** Session Y is negated. The spec doesn't state this. I confirmed it from a real KiCad DSN and a real session lining up with the board.
- **Protected-item matching is exact per segment.** A router that merges or splits a protected segment makes it read as lost, which errs towards refusing.
- **Locked arcs.** A session can't express them through `path` wires, so a locked arc always counts as lost unless `retain_protected` is set. `qarc` wires are refused with `unsupported_wire_shape`. Neither fixture has arcs, so arc reading has only a hand-written unit test, and this arc path through `route_apply` wasn't exercised.
- **Drill not smaller than size** returns `invalid_argument` naming `drill`, because the spec gives no specific code.
- **`no_exporter`** writes nothing, including the manifest. An exporter that fails also leaves no manifest and no DSN.
- **Non-zero exit with a report** returns `unavailable` / `cli_failed`. An unusable report returns `bad_report`. Neither reason is in the spec. Both exist so such a run can't read as zero findings.
- **Integers.** `2.0` counts as an integer, as JSON Schema defines it. `2.5` is refused.
- **Schema validation** is my own subset validator, not a crate. A unit test fails if a tool schema uses a keyword it doesn't handle. `pattern` supports only the SHA-256 pattern.
- **`board_tracks` filters** naming an unknown net or layer are refused, not answered with an empty list. `board_footprints` likewise refuses an unknown reference. Paging on `board_footprints` is an addition.
- **Layer aliases.** Real DSNs use user names such as `GND.plane`. Inputs may use them, and the file always gets the canonical name.
- **`erc_run` takes `schematic`**, not `board`. The project file must share the schematic's or board's stem, since that's the one KiCad loads.
- **No report-path argument.** Reports always go in a temporary folder, which is removed. `kicad-cli`'s `TMPDIR` points there too.

## Not done, or not verified

- `readback_failed` is tested through a library hook, not through a live MCP call.
- Never exercised: `write_failed`, `folder_sync_failed`, `apply_failed` and its rollback, `output_delete_failed`, `internal_error`, `file_changed_during_check`, `no_outline`, `ambiguous_uuid`, `ambiguous_reference`, `ambiguous_dsn` and `java_missing`.
- The kill test uses a test-only environment variable, `BOARDHAND_TEST_KILL_AFTER_FILES`, which SIGKILLs the process at one point: after the first file. It stays in the shipped binary, active only when set. A death while the journal is being written is simulated by an empty journal folder.
- Failure paths of `erc_run` share code with `drc_run` and were tested only through `drc_run`. `erc_run` itself was run for real and passed.
- `cli_timeout`, `cli_crashed` and `no_report` were seen only with fake scripts, never with a real `kicad-cli` failure.
- The numbered net form `(net 3)` is tested only on hand-written boards. No real older KiCad board was used, and KiCad 10 wasn't asked to load one.
- `footprint_move` rotation was compared with KiCad's own move for pad positions and angles and the footprint's own position and rotation. Text angles were written but not compared. A zone's move was compared, without rotation. `fp_text_box` and dimensions inside footprints aren't adjusted on rotation.
- Connected tracks don't follow `footprint_move`. Session placements are ignored and counted. Deleting a track that a group lists leaves a dangling member, and the reply has a note saying so.
- `notifications/cancelled` isn't implemented. Requests sent before `initialize` are accepted. JSON-RPC batches are rejected.
- When stdin closes, running child programs are killed and their calls get no reply.
- Unix only, and run only on this macOS machine. Process groups and signals won't build on Windows.
- `drc_run` on the copy created a `.kicad_prl` beside the board. That's `kicad-cli`'s behaviour, and boardhand doesn't remove it.

## Effects outside this folder

- Fixture originals: both boards' SHA-256 values and modification times were checked after the final run and are unchanged. Copies live under `target/fixtures`.
- My first probe, `java -jar freerouting.jar --help`, ran without the analytics opt-out, and its log showed an analytics request that was rejected with HTTP 403. Later runs set the opt-out.
- Freerouting's settings file, `~/Library/Application Support/freerouting/freerouting.json`, still has its 5 October modification time, so these runs didn't change it. The automated test points `HOME` under `target/`.
- `~/Library/Preferences/kicad/10.0/kicad_common.json` has a modification time from this session, so `kicad-cli` or `pcbnew` rewrote it. I didn't compare its contents.
- The fake `kicad-cli` that signals itself with SIGSEGV makes macOS write crash reports to `~/Library/Logs/DiagnosticReports`, named `bash-2026-11-03-*.ips`. Each test run adds more, and they can be deleted.
- Cargo used `~/.cargo` for its registry cache.
- Nothing was committed, pushed or published.
