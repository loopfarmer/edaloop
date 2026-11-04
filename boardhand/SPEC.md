# boardhand: functional specification

`boardhand` is a Model Context Protocol server, written in Rust, through which an AI agent safely reads and edits KiCad 10 board files. It's a single binary. Its purpose is that an agent can trust each reply.

This document states required behaviour. Design the implementation yourself.

## 1. Principles

- A reply reports what was observed. It never echoes the request as proof.
- A check that couldn't run is never reported as a pass or as zero findings.
- A refused operation changes nothing, and says so.
- After a possible change that can't be confirmed, the reply says the state is uncertain.

## 2. Reply format

Every tool reply's text content is one JSON object with:

- `outcome`: one of `complete`, `partial`, `refused`, `uncertain`, `unavailable`.
- `source`: one of `file`, `cli`, `none`.
- `target`: the absolute path acted on, when there is one.
- `revision_before`, `revision_after`: lowercase SHA-256 of the file's bytes, when known.
- `result`: tool-specific data.
- `reason`: a stable snake_case code, required unless `outcome` is `complete`.
- `detail`: a human sentence, required unless `outcome` is `complete`.

Set the MCP `isError` flag for `refused`, `uncertain` and `unavailable`. Don't set it for a check that ran and found violations, since that's a completed check.

## 3. Protocol

- MCP over stdio, newline-delimited JSON-RPC 2.0. Implement `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
- Log to stderr only. Stdout carries protocol messages only.
- Malformed JSON gets a JSON-RPC parse error, and the server keeps running.

## 4. Tool groups

Domain tools are grouped, and only loaded groups appear in `tools/list`. This keeps the agent's context small.

- These tools are always listed: `groups_list`, `groups_load` (one name or an array), `groups_unload`, `recent_calls`, `call_stats`, `server_info`.
- At startup only the `board_read` group is loaded.
- Loading or unloading sends `notifications/tools/list_changed`.
- Calling a tool in an unloaded group returns `refused` with reason `group_not_loaded` and names the group. Calling a nonexistent tool returns `refused` with reason `unknown_tool`. These must be distinguishable.

## 5. Argument validation

Validate each call against the tool's advertised JSON Schema before running it. Check required fields, types, integer semantics, bounds, nested arrays and objects, and unknown keys where the schema sets `additionalProperties: false`. A failure returns `refused`, reason `invalid_argument`, and names the field path, for example `items[2].width`. A fractional number for an integer field is refused, not truncated.

## 6. Observability

- Keep the last 100 calls in memory: call id, tool, duration in ms, outcome, reason. `recent_calls` returns the newest first, limit 20 by default.
- `call_stats` returns uptime and per-tool totals and errors.
- Append each call to `$XDG_STATE_HOME/boardhand/calls.jsonl`, or `~/.local/state/boardhand/calls.jsonl`. If logging fails, the tool call still succeeds.
- `server_info` returns the crate version, executable path, resolved `kicad-cli` path and its reported version, or an explicit `null` with a reason.

## 7. KiCad file parsing

KiCad board files are S-expressions. Tokens are parenthesised lists, bare atoms and double-quoted strings with backslash escapes.

- Parsing keeps each node's byte span.
- Edits are byte splices. Every byte outside the edited node stays identical, including comments, whitespace and number formatting.
- Parsing then writing an unedited file reproduces it byte for byte. This is tested on the real boards in section 13.
- Net references occur in two forms: `(net 3)` referring to a numbered table, and `(net "NAME")`. Handle both. Report net names in all output.
- A board of 10 MB must parse in under 2 s in a release build.

## 8. Safe writes

Every write to a file:

1. Refuses with reason `editor_lock` if a KiCad lock file for the board exists in its folder. KiCad's lock file is named `~<board-file-name>.lck`, and a project lock `~<project-name>.kicad_pro.lck` also counts. A parameter `ignore_lock` isn't offered.
2. Requires `expected_revision`, a SHA-256. If the file's current hash differs, it refuses with reason `revision_conflict` and returns the current hash. Nothing is written.
3. Writes to a temporary file in the same folder, flushes and syncs it, renames it over the target, then syncs the folder.
4. Reads the file back, parses it, and confirms that the requested change is present and that all other items are unchanged, by comparing a digest of every top-level node outside the edit.
5. If the readback fails after the rename, returns `uncertain`, reason `readback_failed`.

### Multi-file transactions

`transaction_apply` writes several files as one unit:

- Before changing anything, write a journal to `<folder>/.boardhand-journal/<id>/` with each file's original bytes, original hash, new bytes and new hash, and sync it.
- Apply the files one at a time. Then delete the journal.
- If the process dies midway, the journal survives.
- `boardhand transaction list <folder>`, `boardhand transaction recover <id> <folder>` and `boardhand transaction abandon <id> <folder>` are command-line subcommands. Recover restores each file only if its hash matches either the original or the new hash. If a file matches neither, recover changes nothing and reports the file as `divergent`. Abandon deletes the journal and leaves files as they are.
- Starting the server must never recover or delete a journal by itself. `server_info` lists pending journals found under folders it has been asked about.

## 9. Tools

Groups and tools. `board` is an absolute path in every tool. Units are millimetres.

### `board_read` (file source)

- `board_summary`: revision, layer names, copper layer count, outline bounding box, and counts of footprints, pads, segments, arcs, vias, zones, nets and locked items.
- `board_nets`: name and pad count per net.
- `board_tracks`: segments, arcs and vias, filterable by `net` and `layer`, with a limit and offset. Each item has uuid, kind, net, layer or layers, coordinates, width or size and drill, and locked state.
- `board_footprints`: reference, footprint id, position, rotation, side, locked state, and pads with number and net. Filterable by reference.
- `board_stackup`: layers in order with type, thickness and epsilon_r where the file states them. If the file has no stackup, return `partial`, reason `no_stackup`.

### `board_edit` (file source, section 8 applies)

Each of these returns `revision_after` and the uuid of what it created or changed:

- `track_add`: start, end, width, layer, net. Refuses `unknown_net` and `unknown_layer`.
- `via_add`: position, size, drill, net. Refuses if the drill isn't smaller than the size.
- `item_delete`: uuid of a track or via. Refuses `not_found`. Refuses `item_locked` for a locked item.
- `item_set_locked`: uuid and a boolean.
- `footprint_move`: reference, position, optional rotation. Refuses `item_locked` for a locked footprint.
- `transaction_apply`: section 8.

New items get a fresh random UUIDv4, and match the file's own net form from section 7.

### `checks` (cli source)

- `drc_run`: runs `kicad-cli pcb drc --format json --severity-all`, with a timeout argument defaulting to 900 s. Returns counts by type, and items limited to 200 with a total. Findings don't make the outcome anything other than `complete`.
- `erc_run`: the same for a schematic.
- Refuses `no_project_file`, unless `allow_default_rules` is true, when the board has no `.kicad_pro` beside it. KiCad would otherwise apply default rules and report false violations.
- If `kicad-cli` is missing, times out, is killed by a signal, or exits without writing a report, return `unavailable` with reason `cli_missing`, `cli_timeout`, `cli_crashed` or `no_report`, and include the signal or exit code and the last 2,000 bytes of stderr. Never report zero findings in these cases.
- On a report path, never leave partial files behind; use a temporary folder.

### `router` (file and external process)

The purpose is to bring autorouter results back without losing anything.

- `route_prepare`: takes a board and an output folder. It writes a manifest JSON there with the board's revision and the digest of every locked track and via, and every via on the nets named in `protected_nets`, default `["GND"]`. It then runs the configured exporter command to write a `.dsn`. Configure the exporter through `BOARDHAND_DSN_EXPORTER`, a command given the board path and the DSN path. If it's unset, return `unavailable`, reason `no_exporter`.
- `route_run`: runs Freerouting with Java, given the jar path, in that folder, with a timeout. It sets the environment variable `FREEROUTING__USAGE_AND_DIAGNOSTIC_DATA__DISABLE_ANALYTICS=true`. The process is killed on timeout.
- `route_plan_import`: takes a manifest and a Specctra session file and parses the session's routes and vias. It refuses if the board's revision no longer matches the manifest, with reason `revision_conflict`. It reports per-net counts of wires and vias, any net unknown to the board, and any layer unknown to the board. It writes nothing.
- `route_apply`: writes the session's wires and vias into a **new** board file at `output`, never over the input. Then it checks that every manifest item is still present. If any isn't, it deletes the output and returns `refused`, reason `protected_copper_lost`, listing them. The Specctra session format's units come from its `resolution` statement; honour them.

## 10. Configuration

`BOARDHAND_KICAD_CLI` overrides the `kicad-cli` path. Otherwise search `PATH`, then `/opt/homebrew/bin/kicad-cli`, then `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`.

## 11. Command line

`boardhand` with no arguments runs the server. `boardhand --version`, `boardhand --help`, and `boardhand transaction ...` are the other forms. An unrecognised argument is an error and exit code 2.

## 12. Engineering

- Stable Rust no newer than 1.91. Keep dependencies few: `serde`, `serde_json`, `sha2`, `uuid` and a JSON Schema validator are reasonable. Don't add an async runtime unless you need one.
- MIT licence, stated in `Cargo.toml` and a `LICENSE` file with the copyright holder left as `boardhand contributors`.
- `cargo test` passes, and `cargo clippy -- -D warnings` is clean.

## 13. Acceptance tests

Copy fixtures before use, and never write to these originals. Skip with a printed notice if a file is absent.

- The small fixture (a private board, named by `BOARDHAND_FIXTURE_SMALL`) is a 4-layer board. Expected: 121 vias, all on net `GND`; 12 nets reported by pad usage.
- The large fixture (a private board, named by `BOARDHAND_FIXTURE_LARGE`) is a 9.7 MB 8-layer board. Its folder may hold a live lock file, so copy only the `.kicad_pcb`, `.kicad_pro` and `.kicad_dru` files.

Required tests:

1. Byte-identical parse and write on both boards.
2. Parse time under 2 s on the large board, in a release build.
3. An end-to-end MCP session against the release binary over stdio: initialize, list tools, load a group, get the list-changed notification, call a tool, and exercise each `refused` reason in section 4 and 5.
4. Add a track to a copy, then delete it. The file afterwards must be byte-identical to the original apart from what your write format can't avoid, and if anything differs, explain it in `NOTES.md`.
5. Revision conflict, editor lock and locked-item refusals each leave the file unchanged.
6. Kill test for transactions: leave a journal behind midway, then test recover and abandon, including a divergent file.
7. `drc_run` on a copy of the small fixture, with its `.kicad_pro` beside it, where `kicad-cli` is available. Use a fake `kicad-cli` script to test `cli_crashed`, `cli_timeout` and `no_report`.
8. `route_apply` with a hand-written small session file that omits a protected item, and one where it's preserved. KiCad should load the result (`kicad-cli pcb drc` writes a report).

## 14. What to deliver

The crate in the working folder, a short `README.md` for users, and `NOTES.md` listing anything in this spec you didn't do or couldn't verify, stated plainly, with the test output summary.
