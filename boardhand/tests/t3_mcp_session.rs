//! Required test 3: an end-to-end MCP session over stdio against the built binary.
mod common;
use common::*;
use serde_json::{json, Value};
use std::fs;
use std::process::Command;

#[test]
fn t3_end_to_end_session() {
    let dir = work("t3");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    let b = board.to_str().unwrap();
    let mut m = Mcp::start(&dir, &[]);

    let init = m.initialize();
    assert_eq!(init["result"]["serverInfo"]["name"], "boardhand");
    assert_eq!(init["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(init["result"]["capabilities"]["tools"]["listChanged"], true);
    assert_eq!(m.request("ping", json!({}))["result"], json!({}));

    // Startup listing: the always-on tools plus board_read, nothing else.
    let names = m.tool_names();
    for n in ["groups_list", "groups_load", "groups_unload", "recent_calls", "call_stats", "server_info", "board_summary", "board_nets", "board_tracks", "board_footprints", "board_stackup"] {
        assert!(names.contains(&n.to_string()), "{n}");
    }
    assert_eq!(names.len(), 11, "{names:?}");

    // Section 4: the two refusals are distinguishable.
    let (r, err) = m.call_full("track_add", json!({}));
    assert!(err);
    assert_eq!(r["outcome"], "refused");
    assert_eq!(r["reason"], "group_not_loaded");
    assert_eq!(r["result"]["group"], "board_edit");
    assert!(r["detail"].as_str().unwrap().contains("board_edit"));
    let (r, err) = m.call_full("no_such_tool", json!({}));
    assert!(err);
    assert_eq!(r["reason"], "unknown_tool");

    // Load a group by name, see the notification and the new listing.
    m.notifications.clear();
    let r = m.call("groups_load", json!({"groups": "board_edit"}));
    assert_eq!(r["outcome"], "complete");
    assert!(m.notifications.iter().any(|n| n["method"] == "notifications/tools/list_changed"), "{:?}", m.notifications);
    assert!(m.tool_names().contains(&"track_add".to_string()));

    // Load as an array, unload, and an unknown group.
    m.notifications.clear();
    m.call("groups_load", json!({"groups": ["checks", "router"]}));
    assert_eq!(m.notifications.len(), 1);
    assert!(m.tool_names().contains(&"drc_run".to_string()));
    m.notifications.clear();
    m.call("groups_unload", json!({"groups": "checks"}));
    assert_eq!(m.notifications.len(), 1);
    assert!(!m.tool_names().contains(&"drc_run".to_string()));
    let (r, _) = m.call_full("groups_load", json!({"groups": ["board_read", "nope"]}));
    assert_eq!(r["reason"], "unknown_group");
    let groups = m.call("groups_list", json!({}));
    let g = groups["result"]["groups"].as_array().unwrap();
    assert_eq!(g.iter().find(|x| x["name"] == "checks").unwrap()["loaded"], false);
    assert_eq!(g.iter().find(|x| x["name"] == "router").unwrap()["loaded"], true);

    // A real tool call, reporting from the file.
    let s = m.call("board_summary", json!({"board": b}));
    assert_eq!(s["outcome"], "complete");
    assert_eq!(s["source"], "file");
    assert_eq!(s["target"], b);
    assert_eq!(s["revision_before"], sha(&board));
    assert_eq!(s["result"]["counts"]["vias"], 1);
    assert_eq!(s["result"]["counts"]["locked_items"], 1);
    assert_eq!(s["result"]["outline_bbox"]["max_x"].as_f64(), Some(30.0));
    let rev = sha(&board);

    // Section 5: each invalid_argument shape, naming the field path.
    let before = fs::read(&board).unwrap();
    let cases: Vec<(&str, Value, &str)> = vec![
        ("board_summary", json!({}), "board"),
        ("board_summary", json!({"board": 3}), "board"),
        ("board_summary", json!({"board": b, "x": 1}), "x"),
        ("board_tracks", json!({"board": b, "limit": 2.5}), "limit"),
        ("board_tracks", json!({"board": b, "limit": "2"}), "limit"),
        ("board_tracks", json!({"board": b, "limit": 0}), "limit"),
        ("board_tracks", json!({"board": b, "offset": -1}), "offset"),
        ("board_summary", json!({"board": "relative/path.kicad_pcb"}), "board"),
        ("track_add", json!({"board": b, "expected_revision": rev, "start": {"x": 1, "y": 2}, "end": {"x": 1}, "width": 1, "layer": "F.Cu", "net": "GND"}), "end.y"),
        ("track_add", json!({"board": b, "expected_revision": rev, "start": {"x": 1, "y": 2, "z": 3}, "end": {"x": 1, "y": 1}, "width": 1, "layer": "F.Cu", "net": "GND"}), "start.z"),
        ("track_add", json!({"board": b, "expected_revision": rev, "start": {"x": 1, "y": 2}, "end": {"x": 1, "y": 1}, "width": -1, "layer": "F.Cu", "net": "GND"}), "width"),
        ("track_add", json!({"board": b, "expected_revision": "abc", "start": {"x": 1, "y": 2}, "end": {"x": 1, "y": 1}, "width": 1, "layer": "F.Cu", "net": "GND"}), "expected_revision"),
        ("transaction_apply", json!({"folder": dir, "files": [
            {"path": b, "expected_revision": rev, "content": "x"},
            {"path": b, "expected_revision": rev, "content": 5}]}), "files[1].content"),
        ("transaction_apply", json!({"folder": dir, "files": []}), "files"),
        ("groups_load", json!({"groups": ["a", 1]}), "groups[1]"),
        ("recent_calls", json!({"limit": 101}), "limit"),
    ];
    for (tool, args, field) in cases {
        let (r, err) = m.call_full(tool, args.clone());
        assert!(err, "{tool} {args}");
        assert_eq!(r["outcome"], "refused", "{r}");
        assert_eq!(r["reason"], "invalid_argument", "{r}");
        assert_eq!(r["result"]["field"], field, "{r}");
        assert!(r["detail"].as_str().unwrap().contains(field), "{r}");
    }
    // Arguments that aren't an object at all.
    let raw = m.request("tools/call", json!({"name": "board_summary", "arguments": [1]}));
    let r: Value = serde_json::from_str(raw["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(r["reason"], "invalid_argument");
    assert_eq!(fs::read(&board).unwrap(), before);

    // An integer written as 2.0 isn't fractional and is honoured.
    let r = m.call("board_tracks", json!({"board": b, "limit": 2.0}));
    assert_eq!(r["result"]["returned"], 2);

    // Filtering, and a filter naming nothing that exists.
    let r = m.call("board_tracks", json!({"board": b, "net": "GND", "layer": "B.Cu"}));
    assert_eq!(r["result"]["total"], 1);
    assert_eq!(r["result"]["items"][0]["kind"], "via");
    assert_eq!(m.call("board_tracks", json!({"board": b, "net": "NOPE"}))["reason"], "unknown_net");
    assert_eq!(m.call("board_tracks", json!({"board": b, "layer": "NOPE"}))["reason"], "unknown_layer");
    let r = m.call("board_footprints", json!({"board": b, "reference": "R1"}));
    assert_eq!(r["result"]["items"][0]["pads"][0]["net"], "SIG");
    assert_eq!(r["result"]["items"][0]["side"], "front");
    let (r, err) = m.call_full("board_stackup", json!({"board": b}));
    assert_eq!(r["outcome"], "partial");
    assert_eq!(r["reason"], "no_stackup");
    assert!(!err);
    let r = m.call("board_nets", json!({"board": b}));
    assert_eq!(r["result"]["count"], 2);

    // Malformed input gets a parse error, and the server keeps running.
    m.send_raw("{ this is not json");
    let e = m.recv();
    assert_eq!(e["error"]["code"], -32700);
    assert_eq!(e["id"], Value::Null);
    m.send_raw("[1,2]");
    assert_eq!(m.recv()["error"]["code"], -32600);
    assert_eq!(m.request("no/such/method", json!({}))["error"]["code"], -32601);
    assert_eq!(m.request("tools/call", json!({}))["error"]["code"], -32602);
    m.send_raw(r#"{"jsonrpc":"2.0","method":"notifications/unknown"}"#);
    assert_eq!(m.request("ping", json!({}))["result"], json!({}));

    // Observability.
    let r = m.call("recent_calls", json!({"limit": 2}));
    let calls = r["result"]["calls"].as_array().unwrap();
    assert_eq!(calls.len(), 2);
    assert_eq!(calls[0]["tool"], "board_nets");
    assert!(calls[0]["call_id"].as_u64() > calls[1]["call_id"].as_u64());
    assert!(calls[0]["duration_ms"].is_u64());
    assert_eq!(calls[1]["tool"], "board_stackup");
    assert_eq!(calls[1]["outcome"], "partial");
    assert_eq!(calls[1]["reason"], "no_stackup");
    assert_eq!(m.call("recent_calls", json!({}))["result"]["calls"].as_array().unwrap().len(), 20);
    let st = m.call("call_stats", json!({}));
    assert!(st["result"]["uptime_s"].is_u64());
    assert!(st["result"]["tools"]["track_add"]["errors"].as_u64().unwrap() >= 1);
    assert_eq!(st["result"]["tools"]["board_nets"]["errors"], 0);
    let log = fs::read_to_string(dir.join("state/boardhand/calls.jsonl")).unwrap();
    assert!(log.lines().count() > 30);
    assert!(log.lines().all(|l| serde_json::from_str::<Value>(l).is_ok()));

    let si = m.call("server_info", json!({}));
    assert_eq!(si["result"]["version"], env!("CARGO_PKG_VERSION"));
    assert!(si["result"]["executable"].as_str().unwrap().contains("boardhand"));
    assert!(si["result"].get("kicad_cli").is_some());
    println!("t3: server_info {si}");
}

#[test]
fn ring_keeps_the_last_100_and_logging_failure_is_harmless() {
    let dir = work("t3_ring");
    // The state home is a file, so the log can't be created.
    let blocker = dir.join("state");
    fs::write(&blocker, "x").unwrap();
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    let mut m = Mcp::start(&dir, &[("XDG_STATE_HOME", blocker.to_str().unwrap())]);
    m.initialize();
    for _ in 0..105 {
        assert_eq!(m.call("board_nets", json!({"board": board}))["outcome"], "complete");
    }
    let r = m.call("recent_calls", json!({"limit": 100}));
    let c = r["result"]["calls"].as_array().unwrap();
    assert_eq!(c.len(), 100);
    assert_eq!(c[0]["call_id"], 105);
    assert_eq!(m.call("call_stats", json!({}))["result"]["tools"]["board_nets"]["total"], 105);
}

#[test]
fn explicit_null_when_cli_is_missing() {
    let dir = work("t3_info");
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", "/nonexistent/kicad-cli")]);
    m.initialize();
    let r = m.call("server_info", json!({}));
    assert!(r["result"]["kicad_cli"]["path"].is_null());
    assert!(r["result"]["kicad_cli"]["version"].is_null());
    assert!(r["result"]["kicad_cli"]["reason"].is_string());
}

#[test]
fn command_line_forms() {
    let out = Command::new(exe()).arg("--version").output().unwrap();
    assert!(out.status.success());
    assert!(String::from_utf8_lossy(&out.stdout).contains(env!("CARGO_PKG_VERSION")));
    assert!(Command::new(exe()).arg("--help").output().unwrap().status.success());
    for bad in [vec!["--bogus"], vec!["bogus"], vec!["transaction"], vec!["transaction", "nope"], vec!["--version", "x"]] {
        let code = Command::new(exe()).args(&bad).output().unwrap().status.code();
        assert_eq!(code, Some(2), "{bad:?}");
    }
}
