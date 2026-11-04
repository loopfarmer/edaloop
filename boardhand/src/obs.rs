//! Call history, statistics and the append-only call log.
use serde_json::{json, Value};
use std::collections::{BTreeMap, VecDeque};
use std::io::Write;
use std::path::PathBuf;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

const KEEP: usize = 100;

pub struct CallRecord {
    pub id: u64,
    pub tool: String,
    pub duration_ms: u64,
    pub outcome: String,
    pub reason: Option<String>,
}

pub struct Observer {
    started: Instant,
    next_id: u64,
    recent: VecDeque<CallRecord>,
    stats: BTreeMap<String, (u64, u64)>,
    log_path: Option<PathBuf>,
}

pub fn log_path() -> Option<PathBuf> {
    // The XDG specification says relative values must be ignored.
    if let Some(x) = std::env::var_os("XDG_STATE_HOME").map(PathBuf::from) {
        if x.is_absolute() {
            return Some(x.join("boardhand").join("calls.jsonl"));
        }
    }
    let home = std::env::var_os("HOME")?;
    Some(PathBuf::from(home).join(".local/state/boardhand/calls.jsonl"))
}

impl Observer {
    pub fn new() -> Observer {
        Observer { started: Instant::now(), next_id: 1, recent: VecDeque::new(), stats: BTreeMap::new(), log_path: log_path() }
    }

    pub fn record(&mut self, tool: &str, duration_ms: u64, outcome: &str, reason: Option<&str>, is_error: bool) {
        let id = self.next_id;
        self.next_id += 1;
        let e = self.stats.entry(tool.to_string()).or_insert((0, 0));
        e.0 += 1;
        if is_error {
            e.1 += 1;
        }
        let rec = CallRecord { id, tool: tool.to_string(), duration_ms, outcome: outcome.to_string(), reason: reason.map(str::to_string) };
        self.append(&rec);
        self.recent.push_front(rec);
        self.recent.truncate(KEEP);
    }

    /// A logging failure is noted on stderr and never reaches the tool call.
    fn append(&self, rec: &CallRecord) {
        let Some(path) = &self.log_path else { return };
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as u64).unwrap_or(0);
        let mut line = rec_json(rec);
        line["unix_ms"] = json!(ts);
        let r = (|| -> std::io::Result<()> {
            if let Some(p) = path.parent() {
                std::fs::create_dir_all(p)?;
            }
            let mut f = std::fs::OpenOptions::new().create(true).append(true).open(path)?;
            f.write_all(format!("{line}\n").as_bytes())
        })();
        if let Err(e) = r {
            eprintln!("boardhand: couldn't append to {}: {e}", path.display());
        }
    }

    pub fn recent_json(&self, limit: usize) -> Value {
        json!({"calls": self.recent.iter().take(limit).map(rec_json).collect::<Vec<_>>()})
    }

    pub fn stats_json(&self) -> Value {
        let tools: BTreeMap<&String, Value> = self.stats.iter().map(|(k, v)| (k, json!({"total": v.0, "errors": v.1}))).collect();
        json!({"uptime_s": self.started.elapsed().as_secs(), "tools": tools})
    }
}

impl Default for Observer {
    fn default() -> Self {
        Observer::new()
    }
}

fn rec_json(r: &CallRecord) -> Value {
    json!({"call_id": r.id, "tool": r.tool, "duration_ms": r.duration_ms, "outcome": r.outcome, "reason": r.reason})
}
