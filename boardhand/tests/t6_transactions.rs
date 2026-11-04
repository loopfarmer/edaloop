//! Required test 6: kill test for transactions.
mod common;
use common::*;
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn cli(args: &[&str]) -> (i32, Value) {
    let out = Command::new(exe()).args(args).output().unwrap();
    let v = serde_json::from_slice(&out.stdout).unwrap_or(Value::Null);
    (out.status.code().unwrap(), v)
}

struct Setup {
    dir: PathBuf,
    a: PathBuf,
    b: PathBuf,
    orig_a: String,
    orig_b: String,
}

const NEW_A: &str = "(kicad_pcb (version 20260206) (generator \"new-a\"))\n";
const NEW_B: &str = "new b text\n";

fn setup(name: &str) -> Setup {
    let dir = work(name);
    let a = dir.join("a.kicad_pcb");
    let b = dir.join("notes.txt");
    fs::write(&a, MINI_BOARD).unwrap();
    fs::write(&b, "old b text\n").unwrap();
    Setup { orig_a: sha(&a), orig_b: sha(&b), dir, a, b }
}

fn args(s: &Setup) -> Value {
    json!({"folder": s.dir, "files": [
        {"path": s.a, "expected_revision": s.orig_a, "content": NEW_A},
        {"path": s.b, "expected_revision": s.orig_b, "content": NEW_B},
    ]})
}

/// Run a transaction under a server that SIGKILLs itself after the first file.
fn die_midway(s: &Setup) -> String {
    let mut m = Mcp::start(&s.dir, &[("BOARDHAND_TEST_KILL_AFTER_FILES", "1")]);
    m.initialize();
    m.load(json!("board_edit"));
    m.send_raw(&json!({"jsonrpc": "2.0", "id": 99, "method": "tools/call", "params": {"name": "transaction_apply", "arguments": args(s)}}).to_string());
    let status = m.child.wait().unwrap();
    use std::os::unix::process::ExitStatusExt;
    assert_eq!(status.signal(), Some(libc_sigkill()), "the server should have been killed");
    // Midway: the first file is new, the second is still old, the journal survives.
    assert_eq!(fs::read_to_string(&s.a).unwrap(), NEW_A);
    assert_eq!(sha(&s.b), s.orig_b);
    let (code, l) = cli(&["transaction", "list", s.dir.to_str().unwrap()]);
    assert_eq!(code, 0);
    let j = &l["journals"][0];
    assert_eq!(j["complete"], true);
    assert_eq!(j["files"][0]["state"], "new");
    assert_eq!(j["files"][1]["state"], "original");
    j["id"].as_str().unwrap().to_string()
}

fn libc_sigkill() -> i32 {
    9
}

fn journal_dir(dir: &Path, id: &str) -> PathBuf {
    dir.join(".boardhand-journal").join(id)
}

#[test]
fn t6_complete_transaction_removes_its_journal() {
    let s = setup("t6_ok");
    let mut m = Mcp::start(&s.dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let r = m.call("transaction_apply", args(&s));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(fs::read_to_string(&s.a).unwrap(), NEW_A);
    assert_eq!(fs::read_to_string(&s.b).unwrap(), NEW_B);
    assert!(!s.dir.join(".boardhand-journal").exists());
    assert_eq!(r["result"]["files"][0]["revision_after"], sha(&s.a));
}

#[test]
fn t6_refusals_touch_nothing() {
    let s = setup("t6_refuse");
    let mut m = Mcp::start(&s.dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let mut a = args(&s);
    a["files"][1]["expected_revision"] = json!("0".repeat(64));
    let r = m.call("transaction_apply", a);
    assert_eq!(r["reason"], "revision_conflict");
    assert_eq!(r["result"]["current_revision"], s.orig_b);
    let mut a = args(&s);
    a["files"][0]["content"] = json!("(not closed");
    assert_eq!(m.call("transaction_apply", a)["reason"], "parse_error");
    let lock = s.dir.join("~a.kicad_pro.lck");
    fs::write(&lock, "").unwrap();
    assert_eq!(m.call("transaction_apply", args(&s))["reason"], "editor_lock");
    fs::remove_file(&lock).unwrap();
    let mut a = args(&s);
    a["files"][1]["path"] = a["files"][0]["path"].clone();
    a["files"][1]["expected_revision"] = a["files"][0]["expected_revision"].clone();
    assert_eq!(m.call("transaction_apply", a)["result"]["field"], "files[1].path");
    let outside = work("t6_refuse_outside").join("x.txt");
    fs::write(&outside, "x").unwrap();
    let mut a = args(&s);
    a["files"][1]["path"] = json!(outside);
    a["files"][1]["expected_revision"] = json!(sha(&outside));
    assert_eq!(m.call("transaction_apply", a)["reason"], "path_outside_folder");
    assert_eq!(sha(&s.a), s.orig_a);
    assert_eq!(sha(&s.b), s.orig_b);
    assert!(!s.dir.join(".boardhand-journal").exists());
}

#[test]
fn t6_kill_then_recover_restores_originals() {
    let s = setup("t6_recover");
    let id = die_midway(&s);
    let d = journal_dir(&s.dir, &id);
    assert!(d.join("journal.json").exists());
    let (code, r) = cli(&["transaction", "recover", &id, s.dir.to_str().unwrap()]);
    assert_eq!(code, 0, "{r}");
    assert_eq!(r["status"], "recovered");
    assert_eq!(r["restored"], json!(["a.kicad_pcb"]));
    assert_eq!(sha(&s.a), s.orig_a);
    assert_eq!(sha(&s.b), s.orig_b);
    assert!(!s.dir.join(".boardhand-journal").exists());
    assert_eq!(fs::read_to_string(&s.a).unwrap(), MINI_BOARD);
}

#[test]
fn t6_kill_then_abandon_leaves_files() {
    let s = setup("t6_abandon");
    let id = die_midway(&s);
    let (code, r) = cli(&["transaction", "abandon", &id, s.dir.to_str().unwrap()]);
    assert_eq!(code, 0, "{r}");
    assert_eq!(r["status"], "abandoned");
    assert_eq!(fs::read_to_string(&s.a).unwrap(), NEW_A);
    assert_eq!(sha(&s.b), s.orig_b);
    assert!(!s.dir.join(".boardhand-journal").exists());
}

#[test]
fn t6_divergent_file_makes_recover_change_nothing() {
    let s = setup("t6_divergent");
    let id = die_midway(&s);
    fs::write(&s.b, "someone else edited this\n").unwrap();
    let (a_before, b_before) = (sha(&s.a), sha(&s.b));
    let (code, r) = cli(&["transaction", "recover", &id, s.dir.to_str().unwrap()]);
    assert_eq!(code, 1, "{r}");
    assert_eq!(r["status"], "divergent");
    assert_eq!(r["changed"], false);
    assert_eq!(r["divergent"][0]["path"], "notes.txt");
    assert_eq!(r["divergent"][0]["state"], "divergent");
    assert_eq!(sha(&s.a), a_before, "the matching file must not be restored either");
    assert_eq!(sha(&s.b), b_before);
    assert!(journal_dir(&s.dir, &id).exists());
    // A deleted file is divergent as well.
    fs::remove_file(&s.b).unwrap();
    let (code, r) = cli(&["transaction", "recover", &id, s.dir.to_str().unwrap()]);
    assert_eq!((code, r["status"].as_str()), (1, Some("divergent")));
    // Abandon still works.
    let (code, _) = cli(&["transaction", "abandon", &id, s.dir.to_str().unwrap()]);
    assert_eq!(code, 0);
}

#[test]
fn t6_recover_respects_locks_bad_ids_and_corrupt_journals() {
    let s = setup("t6_guard");
    let id = die_midway(&s);
    let dir = s.dir.to_str().unwrap();
    let lock = s.dir.join("~a.kicad_pcb.lck");
    fs::write(&lock, "").unwrap();
    let (code, r) = cli(&["transaction", "recover", &id, dir]);
    assert_eq!((code, r["status"].as_str()), (1, Some("editor_lock")));
    assert_eq!(fs::read_to_string(&s.a).unwrap(), NEW_A);
    fs::remove_file(&lock).unwrap();
    for bad in ["../..", "a/b", "", "missing-id"] {
        let (code, r) = cli(&["transaction", "recover", bad, dir]);
        assert_eq!(code, 1, "{bad} {r}");
        let (code, _) = cli(&["transaction", "abandon", bad, dir]);
        assert_eq!(code, 1, "{bad}");
    }
    assert!(s.dir.exists());
    let orig = journal_dir(&s.dir, &id).join("0.original");
    fs::write(&orig, "corrupted").unwrap();
    let (code, r) = cli(&["transaction", "recover", &id, dir]);
    assert_eq!((code, r["status"].as_str()), (1, Some("journal_corrupt")));
    assert_eq!(fs::read_to_string(&s.a).unwrap(), NEW_A);
    // A journal that died before its index was written is reported, and has changed nothing.
    let partial = journal_dir(&s.dir, "partial-1");
    fs::create_dir_all(&partial).unwrap();
    let (_, l) = cli(&["transaction", "list", dir]);
    assert!(l["journals"].as_array().unwrap().iter().any(|j| j["id"] == "partial-1" && j["complete"] == false));
    let (code, r) = cli(&["transaction", "recover", "partial-1", dir]);
    assert_eq!((code, r["status"].as_str()), (1, Some("journal_incomplete")));
}

#[test]
fn t6_server_start_leaves_journals_and_lists_them_once_asked() {
    let s = setup("t6_server");
    let id = die_midway(&s);
    let a_now = sha(&s.a);
    let mut m = Mcp::start(&s.dir, &[]);
    m.initialize();
    let before = m.call("server_info", json!({}));
    assert_eq!(before["result"]["pending_journals"], json!([]), "it hasn't been asked about this folder");
    assert!(journal_dir(&s.dir, &id).exists());
    let _ = m.call("board_nets", json!({"board": s.b}));
    let after = m.call("server_info", json!({}));
    assert_eq!(after["result"]["pending_journals"][0]["id"], id.as_str());
    assert!(journal_dir(&s.dir, &id).exists());
    assert_eq!(sha(&s.a), a_now);
}
