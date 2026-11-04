# boardhand

An MCP server, in one Rust binary, through which an AI agent reads and edits KiCad 10 board files. Every reply states what was observed in the file or from `kicad-cli`, not what was asked for.

## Build and run

```
cargo build --release
./target/release/boardhand            # MCP server on stdio
./target/release/boardhand --version
./target/release/boardhand --help
```

Example client configuration:

```json
{"mcpServers": {"boardhand": {"command": "/path/to/boardhand"}}}
```

## Tool groups

Only `board_read` starts loaded. Use `groups_list`, then `groups_load` with a name or an array of names.

| Group | Tools |
|---|---|
| always listed | `groups_list`, `groups_load`, `groups_unload`, `recent_calls`, `call_stats`, `server_info` |
| `board_read` | `board_summary`, `board_nets`, `board_tracks`, `board_footprints`, `board_stackup` |
| `board_edit` | `track_add`, `via_add`, `item_delete`, `item_set_locked`, `footprint_move`, `transaction_apply` |
| `checks` | `drc_run`, `erc_run` |
| `router` | `route_prepare`, `route_run`, `route_plan_import`, `route_apply` |

All paths are absolute, and all lengths are millimetres. Every edit needs `expected_revision`, the SHA-256 from an earlier reply.

## Replies

Each reply is one JSON object with `outcome` (`complete`, `partial`, `refused`, `uncertain` or `unavailable`), `source`, `target`, `revision_before`, `revision_after`, `result`, and, unless complete, `reason` and `detail`. `isError` is set for `refused`, `uncertain` and `unavailable`. A DRC that ran and found violations is `complete`.

## Autorouting

1. `route_prepare` writes a manifest of protected copper (locked tracks and vias, and every via on `protected_nets`, default `GND`), then runs `BOARDHAND_DSN_EXPORTER` with the board path and DSN path.
2. `route_run` runs Freerouting in that folder.
3. `route_plan_import` reports what the session holds and writes nothing.
4. `route_apply` writes a new board. The session replaces the input's tracks and vias. If any protected item isn't in the result, the output is deleted and the reply is `protected_copper_lost`.

Freerouting commonly leaves fixed copper out of its session, so a real session is often refused. Pass `retain_protected: true` to keep those items from the input board. That argument is an addition to the specification.

KiCad's Python can serve as the exporter:

```sh
#!/bin/sh
exec /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9 -I -c \
  "import pcbnew,sys; sys.exit(0 if pcbnew.ExportSpecctraDSN(pcbnew.LoadBoard(sys.argv[1]),sys.argv[2]) else 1)" "$1" "$2"
```

## Interrupted transactions

```
boardhand transaction list <folder>
boardhand transaction recover <id> <folder>
boardhand transaction abandon <id> <folder>
```

The server never recovers or deletes a journal itself. `recover` restores the original bytes, and changes nothing if any file matches neither recorded hash. `abandon` deletes the journal and leaves files as they are.

## Environment

| Variable | Purpose |
|---|---|
| `BOARDHAND_KICAD_CLI` | Path of `kicad-cli`. Otherwise `PATH`, then the standard macOS locations. |
| `BOARDHAND_DSN_EXPORTER` | DSN exporter. An executable path, or a shell command that gets the two paths appended. |
| `XDG_STATE_HOME` | Call log folder. The log is `boardhand/calls.jsonl`, by default under `~/.local/state`. |

Licensed under MIT. See `LICENSE`.
