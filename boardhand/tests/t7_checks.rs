//! Required test 7: DRC on a copy of the small fixture, and a fake kicad-cli for the failures.
mod common;
use common::*;
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Instant;

fn fake(dir: &Path, body: &str) -> PathBuf {
    let p = dir.join("fake kicad-cli");
    script(&p, body);
    p
}

fn drc_with_fake(name: &str, body: &str, extra: Value) -> (Value, Vec<String>, PathBuf) {
    let dir = work(name);
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    fs::write(dir.join("mini.kicad_pro"), "{}").unwrap();
    let cli = fake(&dir, body);
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", cli.to_str().unwrap())]);
    m.initialize();
    m.load(json!("checks"));
    let mut args = json!({"board": board});
    args.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
    let began = Instant::now();
    let (r, is_err) = m.call_full("drc_run", args);
    println!("{name}: {r} ({:?})", began.elapsed());
    assert!(is_err);
    assert_eq!(r["outcome"], "unavailable", "{r}");
    assert_eq!(r["source"], "cli");
    assert!(r["result"].get("total").is_none(), "no findings are claimed");
    assert!(r["result"].get("items").is_none());
    assert!(r["detail"].as_str().unwrap().contains("No findings"));
    let left: Vec<String> = fs::read_dir(dir.join("tmp")).unwrap().flatten().map(|e| e.file_name().to_string_lossy().to_string()).collect();
    (r, left, dir)
}

#[test]
fn t7_real_drc_on_small_copy() {
    let dir = work("t7_real");
    let Some(board) = copy_fixture(small(), &dir) else { return };
    if !Path::new(KICAD_CLI).exists() {
        println!("NOTICE: kicad-cli is absent, skipping");
        return;
    }
    assert!(board.with_extension("kicad_pro").exists());
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", KICAD_CLI)]);
    m.initialize();
    m.load(json!("checks"));
    let rev = sha(&board);
    let (r, is_err) = m.call_full("drc_run", json!({"board": board}));
    println!("t7 real: total={} types={} kicad={} ms={}", r["result"]["total"], r["result"]["counts_by_type"], r["result"]["kicad_version"], r["result"]["duration_ms"]);
    assert_eq!(r["outcome"], "complete", "{r}");
    assert!(!is_err, "findings are not an error");
    assert_eq!(r["source"], "cli");
    assert_eq!(r["revision_before"], rev);
    assert_eq!(r["revision_after"], rev);
    assert_eq!(sha(&board), rev);
    assert_eq!(r["result"]["default_rules_used"], false);
    assert!(r["result"]["total"].is_u64());
    assert_eq!(r["result"]["items_returned"], r["result"]["total"].as_u64().unwrap().min(200));
    let n: u64 = r["result"]["counts_by_type"].as_object().unwrap().values().map(|v| v.as_u64().unwrap()).sum();
    assert_eq!(n, r["result"]["total"].as_u64().unwrap());
    assert!(fs::read_dir(dir.join("tmp")).unwrap().next().is_none(), "temporary folders were left");
}

#[test]
fn t7_real_erc() {
    let dir = work("t7_erc");
    let src = Path::new(small()).parent().unwrap();
    if !src.join("chain.kicad_sch").exists() || !Path::new(KICAD_CLI).exists() {
        println!("NOTICE: schematic or kicad-cli absent, skipping");
        return;
    }
    let stem = Path::new(small()).file_stem().unwrap().to_str().unwrap();
    let sch = dir.join(format!("{stem}.kicad_sch"));
    fs::copy(src.join(format!("{stem}.kicad_sch")), &sch).unwrap();
    fs::copy(src.join("chain.kicad_sch"), dir.join("chain.kicad_sch")).unwrap();
    fs::copy(src.join(format!("{stem}.kicad_pro")), dir.join(format!("{stem}.kicad_pro"))).unwrap();
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", KICAD_CLI)]);
    m.initialize();
    m.load(json!("checks"));
    let (r, is_err) = m.call_full("erc_run", json!({"schematic": sch}));
    println!("t7 erc: total={} types={}", r["result"]["total"], r["result"]["counts_by_type"]);
    assert_eq!(r["outcome"], "complete", "{r}");
    assert!(!is_err);
}

#[test]
fn t7_no_project_file() {
    let dir = work("t7_project");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    // A differently named project isn't the one KiCad would load.
    fs::write(dir.join("other.kicad_pro"), "{}").unwrap();
    let body = "for a in \"$@\"; do if [ \"$prev\" = \"--output\" ]; then printf '%s' '{\"violations\":[{\"type\":\"x\",\"severity\":\"error\"}],\"unconnected_items\":[],\"schematic_parity\":[]}' > \"$a\"; fi; prev=\"$a\"; done; exit 0";
    let cli = fake(&dir, body);
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", cli.to_str().unwrap())]);
    m.initialize();
    m.load(json!("checks"));
    let (r, err) = m.call_full("drc_run", json!({"board": board}));
    assert!(err);
    assert_eq!(r["reason"], "no_project_file");
    let (r, err) = m.call_full("drc_run", json!({"board": board, "allow_default_rules": true}));
    assert!(!err, "{r}");
    assert_eq!(r["result"]["default_rules_used"], true);
    assert_eq!(r["result"]["total"], 1);
    assert_eq!(r["result"]["counts_by_type"]["x"], 1);
    assert_eq!(m.call("erc_run", json!({"schematic": dir.join("mini.kicad_pcb")}))["reason"], "no_project_file");
}

#[test]
fn t7_items_are_limited_to_200_with_a_total() {
    let dir = work("t7_limit");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    fs::write(dir.join("mini.kicad_pro"), "{}").unwrap();
    let items: Vec<Value> = (0..250).map(|i| json!({"type": if i % 2 == 0 {"a"} else {"b"}, "severity": "error"})).collect();
    let report = dir.join("canned.json");
    fs::write(&report, json!({"violations": items, "unconnected_items": [{"type": "u", "severity": "warning"}], "schematic_parity": []}).to_string()).unwrap();
    let body = format!("for a in \"$@\"; do if [ \"$prev\" = \"--output\" ]; then cp \"{}\" \"$a\"; fi; prev=\"$a\"; done", report.display());
    let cli = fake(&dir, &body);
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", cli.to_str().unwrap())]);
    m.initialize();
    m.load(json!("checks"));
    let (r, err) = m.call_full("drc_run", json!({"board": board}));
    assert!(!err);
    assert_eq!(r["outcome"], "complete");
    assert_eq!(r["result"]["total"], 251);
    assert_eq!(r["result"]["items"].as_array().unwrap().len(), 200);
    assert_eq!(r["result"]["counts_by_type"]["a"], 125);
    assert_eq!(r["result"]["counts_by_type"]["u"], 1);
    assert_eq!(r["result"]["counts_by_severity"]["warning"], 1);
}

#[test]
fn t7_cli_crashed() {
    let (r, left, _) = drc_with_fake("t7_crash", "echo crash-diagnostic >&2\nkill -SEGV $$", json!({}));
    assert_eq!(r["reason"], "cli_crashed");
    assert_eq!(r["result"]["signal"], 11);
    assert!(r["result"]["stderr_tail"].as_str().unwrap().contains("crash-diagnostic"));
    assert!(left.is_empty(), "{left:?}");
}

#[test]
fn t7_crash_after_a_partial_report_is_still_a_crash() {
    let body = "for a in \"$@\"; do if [ \"$prev\" = \"--output\" ]; then echo '{\"violations\":[]' > \"$a\"; fi; prev=\"$a\"; done\nkill -KILL $$";
    let (r, left, _) = drc_with_fake("t7_crash2", body, json!({}));
    assert_eq!(r["reason"], "cli_crashed");
    assert_eq!(r["result"]["signal"], 9);
    assert!(left.is_empty(), "{left:?}");
}

#[test]
fn t7_cli_timeout_kills_the_process_group() {
    let dir_marker = work("t7_timeout_marker").join("alive");
    let body = format!("echo before-hang >&2\n(sleep 3; touch \"{}\") &\nsleep 300", dir_marker.display());
    let began = Instant::now();
    let (r, left, _) = drc_with_fake("t7_timeout", &body, json!({"timeout_s": 1}));
    assert_eq!(r["reason"], "cli_timeout");
    assert!(began.elapsed().as_secs() < 20, "{:?}", began.elapsed());
    assert!(r["result"]["stderr_tail"].as_str().unwrap().contains("before-hang"));
    assert!(left.is_empty(), "{left:?}");
    std::thread::sleep(std::time::Duration::from_secs(4));
    assert!(!dir_marker.exists(), "a child outlived the kill");
}

#[test]
fn t7_no_report() {
    let (r, left, _) = drc_with_fake("t7_no_report", "echo nothing >&2\nexit 0", json!({}));
    assert_eq!(r["reason"], "no_report");
    assert_eq!(r["result"]["exit_code"], 0);
    assert!(left.is_empty(), "{left:?}");
    let (r, _, _) = drc_with_fake("t7_no_report3", "exit 3", json!({}));
    assert_eq!(r["reason"], "no_report");
    assert_eq!(r["result"]["exit_code"], 3);
    // An empty report isn't a report.
    let body = "for a in \"$@\"; do if [ \"$prev\" = \"--output\" ]; then : > \"$a\"; fi; prev=\"$a\"; done";
    let (r, _, _) = drc_with_fake("t7_no_report_empty", body, json!({}));
    assert_eq!(r["reason"], "no_report");
}

#[test]
fn t7_bad_reports_are_never_zero_findings() {
    let write = |text: &str| format!("for a in \"$@\"; do if [ \"$prev\" = \"--output\" ]; then printf '%s' '{text}' > \"$a\"; fi; prev=\"$a\"; done");
    let (r, _, _) = drc_with_fake("t7_bad1", &write("{not json"), json!({}));
    assert_eq!(r["reason"], "bad_report");
    let (r, _, _) = drc_with_fake("t7_bad2", &write("{\"other\":1}"), json!({}));
    assert_eq!(r["reason"], "bad_report");
    let (r, _, _) = drc_with_fake("t7_bad3", &format!("{}\nexit 1", write("{\"violations\":[],\"unconnected_items\":[],\"schematic_parity\":[]}")), json!({}));
    assert_eq!(r["reason"], "cli_failed");
    assert_eq!(r["result"]["exit_code"], 1);
}

#[test]
fn t7_cli_missing() {
    let dir = work("t7_missing");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    fs::write(dir.join("mini.kicad_pro"), "{}").unwrap();
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", "/nonexistent/kicad-cli")]);
    m.initialize();
    m.load(json!("checks"));
    let (r, err) = m.call_full("drc_run", json!({"board": board}));
    assert!(err);
    assert_eq!(r["outcome"], "unavailable");
    assert_eq!(r["reason"], "cli_missing");
}

#[test]
fn server_answers_ping_while_a_check_runs() {
    let dir = work("t7_ping");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    fs::write(dir.join("mini.kicad_pro"), "{}").unwrap();
    let cli = fake(&dir, "sleep 300");
    let mut m = Mcp::start(&dir, &[("BOARDHAND_KICAD_CLI", cli.to_str().unwrap())]);
    m.initialize();
    m.load(json!("checks"));
    m.send_raw(&json!({"jsonrpc":"2.0","id":"drc","method":"tools/call","params":{"name":"drc_run","arguments":{"board":board,"timeout_s":3}}}).to_string());
    m.send_raw(r#"{"jsonrpc":"2.0","id":"p","method":"ping"}"#);
    let first = m.recv();
    assert_eq!(first["id"], "p", "{first}");
    let second = m.recv();
    assert_eq!(second["id"], "drc");
}

#[test]
fn t7_stderr_tail_is_the_last_2000_bytes() {
    let body = "i=0; while [ $i -lt 600 ]; do printf 'line-%04d\\n' $i >&2; i=$((i+1)); done; exit 0";
    let (r, _, _) = drc_with_fake("t7_tail", body, json!({}));
    let tail = r["result"]["stderr_tail"].as_str().unwrap();
    assert_eq!(tail.len(), 2000);
    assert!(tail.ends_with("line-0599\n"), "{tail}");
    assert!(!tail.contains("line-0000"));
}
