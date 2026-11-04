//! Multi-file transactions and their on-disk journal.
use crate::edit::check_lock;
use crate::reply::{Reply, Source};
use crate::util::{self, sha256_hex};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

const DIR: &str = ".boardhand-journal";
const INDEX: &str = "journal.json";

fn journal_root(folder: &Path) -> PathBuf {
    folder.join(DIR)
}

fn valid_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 64 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

/// Test hook for the kill test: SIGKILL this process after applying N files.
fn maybe_die(applied: usize) {
    if let Some(n) = std::env::var("BOARDHAND_TEST_KILL_AFTER_FILES").ok().and_then(|v| v.parse::<usize>().ok()) {
        if n == applied {
            // SAFETY: raising a signal against our own pid has no memory effects.
            unsafe {
                libc::kill(libc::getpid(), libc::SIGKILL);
            }
        }
    }
}

struct Entry {
    rel: PathBuf,
    real: PathBuf,
    old: Vec<u8>,
    new: Vec<u8>,
}

fn write_journal(folder: &Path, id: &str, entries: &[Entry]) -> std::io::Result<PathBuf> {
    let root = journal_root(folder);
    fs::create_dir_all(&root)?;
    let dir = root.join(id);
    fs::create_dir(&dir)?;
    let mut index = vec![];
    for (i, e) in entries.iter().enumerate() {
        let (o, n) = (format!("{i}.original"), format!("{i}.new"));
        util::write_new_synced(&dir.join(&o), &e.old)?;
        util::write_new_synced(&dir.join(&n), &e.new)?;
        index.push(json!({
            "path": e.rel.to_string_lossy(),
            "original_file": o,
            "new_file": n,
            "original_hash": sha256_hex(&e.old),
            "new_hash": sha256_hex(&e.new),
        }));
    }
    // The index goes last, so its presence means the journal is whole.
    let doc = json!({"format": "boardhand-journal", "version": 1, "id": id, "files": index});
    util::write_new_synced(&dir.join(INDEX), doc.to_string().as_bytes())?;
    util::sync_dir(&dir)?;
    util::sync_dir(&root)?;
    util::sync_dir(folder)?;
    Ok(dir)
}

fn delete_journal(folder: &Path, id: &str) -> std::io::Result<()> {
    let root = journal_root(folder);
    fs::remove_dir_all(root.join(id))?;
    util::sync_dir(&root)?;
    if fs::read_dir(&root)?.next().is_none() {
        fs::remove_dir(&root)?;
        util::sync_dir(folder)?;
    }
    Ok(())
}

pub fn transaction_apply(args: &Value) -> Reply {
    let src = Source::File;
    let raw = args["folder"].as_str().unwrap_or("");
    if !Path::new(raw).is_absolute() {
        return crate::edit::not_absolute("folder", raw);
    }
    let folder = match fs::canonicalize(raw) {
        Ok(f) => f,
        Err(e) => return Reply::refused(src, "file_not_found", format!("{raw}: {e}.")).target(raw),
    };
    let empty = vec![];
    let files = args["files"].as_array().unwrap_or(&empty);
    let mut entries: Vec<Entry> = vec![];
    for (i, f) in files.iter().enumerate() {
        let field = format!("files[{i}].path");
        let p = f["path"].as_str().unwrap_or("");
        if !Path::new(p).is_absolute() {
            return crate::edit::not_absolute(&field, p);
        }
        let real = match fs::canonicalize(p) {
            Ok(r) => r,
            Err(e) => {
                return Reply::refused(src, "file_not_found", format!("{field}: {p}: {e}.")).target(&folder).with(json!({"field": field}))
            }
        };
        let Ok(rel) = real.strip_prefix(&folder).map(Path::to_path_buf) else {
            return Reply::refused(src, "path_outside_folder", format!("{field}: {p} isn't inside {}.", folder.display()))
                .target(&folder)
                .with(json!({"field": field}));
        };
        if entries.iter().any(|e| e.real == real) {
            return Reply::refused(src, "invalid_argument", format!("{field}: {p} appears twice."))
                .target(&folder)
                .with(json!({"field": field}));
        }
        if let Err(r) = check_lock(&real) {
            return r.with(json!({"field": field}));
        }
        let old = match fs::read(&real) {
            Ok(b) => b,
            Err(e) => return Reply::refused(src, "read_failed", format!("{field}: {e}.")).target(&folder),
        };
        let cur = sha256_hex(&old);
        if cur != f["expected_revision"].as_str().unwrap_or("") {
            return Reply::refused(src, "revision_conflict", format!("{field}: {p} has hash {cur}, not the expected one."))
                .target(&real)
                .with(json!({"field": field, "current_revision": cur}));
        }
        let new = f["content"].as_str().unwrap_or("").as_bytes().to_vec();
        if real.extension().is_some_and(|x| x == "kicad_pcb") {
            if let Err(e) = crate::board::Board::parse(new.clone()) {
                return Reply::refused(src, "parse_error", format!("files[{i}].content: the new board doesn't parse: {e}."))
                    .target(&real)
                    .with(json!({"field": format!("files[{i}].content")}));
            }
        }
        entries.push(Entry { rel, real, old, new });
    }
    let id = uuid::Uuid::new_v4().to_string();
    if let Err(e) = write_journal(&folder, &id, &entries) {
        let _ = delete_journal(&folder, &id);
        return Reply::refused(src, "journal_failed", format!("Couldn't write the journal: {e}.")).target(&folder);
    }
    let mut applied = 0;
    let mut failure = None;
    for e in &entries {
        match util::atomic_replace(&e.real, &e.new) {
            Ok(()) => {
                applied += 1;
                maybe_die(applied);
            }
            Err(err) => {
                failure = Some(format!("{}: {err}", e.real.display()));
                break;
            }
        }
    }
    if let Some(msg) = failure {
        // Undo what went through, so the unit stays whole.
        let rolled_back = entries[..applied]
            .iter()
            .all(|e| util::atomic_replace(&e.real, &e.old).is_ok() && fs::read(&e.real).map(|b| b == e.old).unwrap_or(false));
        if rolled_back && delete_journal(&folder, &id).is_ok() {
            return Reply::refused(
                src,
                "apply_failed",
                format!("Writing failed ({msg}), and every file was restored to its original bytes."),
            )
            .target(&folder);
        }
        return Reply::uncertain(
            src,
            "apply_failed",
            format!("Writing failed ({msg}), and rollback didn't finish. Journal {id} is kept. Use `boardhand transaction recover`."),
        )
        .target(&folder)
        .with(json!({"journal": id}));
    }
    let mut out = vec![];
    for e in &entries {
        match fs::read(&e.real) {
            Ok(b) if b == e.new => out.push(json!({
                "path": e.real.to_string_lossy(),
                "revision_before": sha256_hex(&e.old),
                "revision_after": sha256_hex(&b),
            })),
            other => {
                let seen = other.ok().map(|b| sha256_hex(&b));
                return Reply::uncertain(
                    src,
                    "readback_failed",
                    format!("{} doesn't read back as written. Journal {id} is kept. The state is uncertain.", e.real.display()),
                )
                .target(&e.real)
                .with(json!({"journal": id, "observed_hash": seen}));
            }
        }
    }
    let mut r = Reply::complete(src, json!({"changed": true, "transaction": id, "files": out})).target(&folder);
    if let Err(e) = delete_journal(&folder, &id) {
        r = r.with(json!({"journal_not_removed": e.to_string(), "journal": id}));
    }
    r
}

// ---- journal inspection, used by the CLI and server_info ----

fn file_state(folder: &Path, f: &Value) -> Value {
    let rel = f["path"].as_str().unwrap_or("");
    let (orig, new) = (f["original_hash"].as_str().unwrap_or(""), f["new_hash"].as_str().unwrap_or(""));
    let cur = fs::read(folder.join(rel)).ok().map(|b| sha256_hex(&b));
    let state = match cur.as_deref() {
        // Both match when a transaction left a file unchanged. Nothing to restore.
        Some(h) if h == orig => "original",
        Some(h) if h == new => "new",
        _ => "divergent",
    };
    json!({"path": rel, "state": state, "current_hash": cur, "original_hash": orig, "new_hash": new})
}

fn read_index(folder: &Path, id: &str) -> Option<Value> {
    let b = fs::read(journal_root(folder).join(id).join(INDEX)).ok()?;
    serde_json::from_slice::<Value>(&b).ok().filter(|v| v["files"].is_array())
}

pub fn list(folder: &Path) -> Vec<Value> {
    let Ok(rd) = fs::read_dir(journal_root(folder)) else { return vec![] };
    let mut names: Vec<String> = rd.flatten().filter(|e| e.path().is_dir()).map(|e| e.file_name().to_string_lossy().to_string()).collect();
    names.sort();
    names
        .into_iter()
        .map(|id| match read_index(folder, &id) {
            Some(ix) => {
                let files: Vec<Value> =
                    ix["files"].as_array().map(|a| a.iter().map(|f| file_state(folder, f)).collect()).unwrap_or_default();
                json!({"id": id, "folder": folder.to_string_lossy(), "complete": true, "files": files})
            }
            // The process died while writing the journal: no target file was touched.
            None => json!({"id": id, "folder": folder.to_string_lossy(), "complete": false, "files": []}),
        })
        .collect()
}

pub struct CliResult {
    pub code: i32,
    pub report: Value,
}

fn cli_err(status: &str, detail: String) -> CliResult {
    CliResult { code: 1, report: json!({"status": status, "changed": false, "detail": detail}) }
}

pub fn recover(id: &str, folder: &Path) -> CliResult {
    if !valid_id(id) {
        return cli_err("invalid_id", format!("'{id}' isn't a journal id"));
    }
    let dir = journal_root(folder).join(id);
    if !dir.is_dir() {
        return cli_err("not_found", format!("no journal {id} in {}", folder.display()));
    }
    let Some(ix) = read_index(folder, id) else {
        return cli_err("journal_incomplete", "the journal was never finished, so no file was changed by it; use abandon".into());
    };
    let empty = vec![];
    let files = ix["files"].as_array().unwrap_or(&empty);
    let states: Vec<Value> = files.iter().map(|f| file_state(folder, f)).collect();
    let divergent: Vec<&Value> = states.iter().filter(|s| s["state"] == "divergent").collect();
    if !divergent.is_empty() {
        return CliResult {
            code: 1,
            report: json!({"status": "divergent", "changed": false, "divergent": divergent, "files": states, "detail": "a file matches neither hash; nothing was changed and the journal is kept"}),
        };
    }
    // Check the journal's own bytes and every lock before changing anything.
    let mut work = vec![];
    for (f, s) in files.iter().zip(&states) {
        let rel = f["path"].as_str().unwrap_or("");
        let Ok(orig) = fs::read(dir.join(f["original_file"].as_str().unwrap_or(""))) else {
            return cli_err("journal_corrupt", format!("the original bytes of {rel} are missing"));
        };
        if sha256_hex(&orig) != f["original_hash"].as_str().unwrap_or("") {
            return cli_err("journal_corrupt", format!("the original bytes of {rel} don't match their hash"));
        }
        if s["state"] == "new" {
            let target = folder.join(rel);
            if let Err(r) = check_lock(&target) {
                return cli_err("editor_lock", r.detail.unwrap_or_default().to_string());
            }
            work.push((rel.to_string(), target, orig));
        }
    }
    let mut restored = vec![];
    for (rel, target, orig) in &work {
        let good = util::atomic_replace(target, orig).is_ok() && fs::read(target).map(|b| b == *orig).unwrap_or(false);
        if !good {
            return CliResult {
                code: 1,
                report: json!({"status": "uncertain", "changed": !restored.is_empty(), "restored": restored, "failed": rel, "detail": "restoring failed; the journal is kept"}),
            };
        }
        restored.push(rel.clone());
    }
    let removed = delete_journal(folder, id);
    CliResult {
        code: 0,
        report: json!({"status": "recovered", "changed": !restored.is_empty(), "restored": restored, "files": states, "journal_removed": removed.is_ok()}),
    }
}

pub fn abandon(id: &str, folder: &Path) -> CliResult {
    if !valid_id(id) {
        return cli_err("invalid_id", format!("'{id}' isn't a journal id"));
    }
    if !journal_root(folder).join(id).is_dir() {
        return cli_err("not_found", format!("no journal {id} in {}", folder.display()));
    }
    match delete_journal(folder, id) {
        Ok(()) => CliResult {
            code: 0,
            report: json!({"status": "abandoned", "changed": false, "detail": "the journal was deleted and files were left as they are"}),
        },
        Err(e) => cli_err("delete_failed", e.to_string()),
    }
}
