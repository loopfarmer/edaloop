//! MCP over stdio: newline-delimited JSON-RPC 2.0.
use crate::checks::{self, Check};
use crate::journal;
use crate::obs::Observer;
use crate::proc;
use crate::reply::{Reply, Source};
use crate::router;
use crate::tools::{self, catalogue, Tool, GROUPS};
use crate::util;
use crate::validate::validate;
use serde_json::{json, Value};
use std::collections::{BTreeSet, HashSet};
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Instant;

const PROTOCOL_VERSIONS: &[&str] = &["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

pub struct State {
    pub loaded: Mutex<BTreeSet<String>>,
    pub obs: Mutex<Observer>,
    pub folders: Mutex<BTreeSet<PathBuf>>,
    pub tools: Vec<Tool>,
    out: Mutex<io::Stdout>,
}

impl State {
    fn new() -> State {
        let mut loaded = BTreeSet::new();
        loaded.insert("board_read".to_string());
        State {
            loaded: Mutex::new(loaded),
            obs: Mutex::new(Observer::new()),
            folders: Mutex::new(BTreeSet::new()),
            tools: catalogue(),
            out: Mutex::new(io::stdout()),
        }
    }

    fn send(&self, v: &Value) {
        if let Ok(mut o) = self.out.lock() {
            let line = format!("{v}\n");
            if o.write_all(line.as_bytes()).and_then(|()| o.flush()).is_err() {
                eprintln!("boardhand: couldn't write to stdout");
            }
        }
    }
}

fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn notify_list_changed(s: &State) {
    s.send(&json!({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}));
}

fn names_arg(args: &Value) -> Vec<String> {
    match &args["groups"] {
        Value::String(g) => vec![g.clone()],
        Value::Array(a) => a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
        _ => vec![],
    }
}

fn group_change(s: &State, args: &Value, load: bool) -> Reply {
    let names = names_arg(args);
    if let Some(bad) = names.iter().find(|n| !GROUPS.iter().any(|(g, _)| g == n)) {
        let known: Vec<&str> = GROUPS.iter().map(|g| g.0).collect();
        return Reply::refused(Source::None, "unknown_group", format!("There's no group '{bad}'. The groups are {}.", known.join(", ")));
    }
    let loaded_now: Vec<String> = {
        let mut l = lock(&s.loaded);
        for n in &names {
            if load {
                l.insert(n.clone());
            } else {
                l.remove(n);
            }
        }
        l.iter().cloned().collect()
    };
    notify_list_changed(s);
    Reply::complete(Source::None, json!({"loaded": loaded_now}))
}

fn groups_list(s: &State) -> Reply {
    let l = lock(&s.loaded);
    let groups: Vec<Value> = GROUPS
        .iter()
        .map(|(g, d)| {
            let tools: Vec<&str> = s.tools.iter().filter(|t| t.group == Some(g)).map(|t| t.name).collect();
            json!({"name": g, "description": d, "loaded": l.contains(*g), "tools": tools})
        })
        .collect();
    Reply::complete(Source::None, json!({"groups": groups}))
}

fn server_info(s: &State) -> Reply {
    let exe = std::env::current_exe().map(|p| p.to_string_lossy().to_string()).ok();
    let kicad = match proc::resolve_kicad_cli() {
        Ok(p) => match kicad_version(&p) {
            Ok(v) => json!({"path": p.to_string_lossy(), "version": v, "reason": null}),
            Err(why) => json!({"path": p.to_string_lossy(), "version": null, "reason": why}),
        },
        Err(why) => json!({"path": null, "version": null, "reason": why}),
    };
    let folders: Vec<PathBuf> = lock(&s.folders).iter().cloned().collect();
    let pending: Vec<Value> = folders.iter().flat_map(|f| journal::list(f)).collect();
    Reply::complete(
        Source::None,
        json!({
            "name": env!("CARGO_PKG_NAME"),
            "version": env!("CARGO_PKG_VERSION"),
            "executable": exe,
            "kicad_cli": kicad,
            "pending_journals": pending,
            "folders_scanned": folders,
        }),
    )
}

fn kicad_version(cli: &Path) -> Result<String, String> {
    let scratch = util::ScratchDir::new().map_err(|e| e.to_string())?;
    let tmpdir = scratch.0.to_string_lossy().to_string();
    let f = proc::run(proc::Spec {
        program: cli,
        args: vec!["--version".into()],
        cwd: None,
        env: vec![("TMPDIR", &tmpdir)],
        timeout: std::time::Duration::from_secs(15),
        capture_dir: &scratch.0,
    })
    .map_err(|e| format!("couldn't run it: {e}"))?;
    match f.ended {
        proc::Ended::Exited(0) if !f.stdout_tail.trim().is_empty() => Ok(f.stdout_tail.trim().to_string()),
        other => Err(format!("--version ended with {other:?} and output '{}'", f.stdout_tail.trim())),
    }
}

/// Remember which folders the server has been asked about.
fn note_folders(s: &State, args: &Value) {
    let add = |p: &str, is_folder: bool| {
        let path = Path::new(p);
        if !path.is_absolute() {
            return;
        }
        let f = if is_folder { path.to_path_buf() } else { util::parent_of(path) };
        lock(&s.folders).insert(std::fs::canonicalize(&f).unwrap_or(f));
    };
    for key in ["board", "schematic", "manifest", "session", "output"] {
        if let Some(p) = args.get(key).and_then(Value::as_str) {
            add(p, false);
        }
    }
    for key in ["folder", "output_folder"] {
        if let Some(p) = args.get(key).and_then(Value::as_str) {
            add(p, true);
        }
    }
}

/// Dispatch one call, up to and including group and validation checks.
pub fn call_tool(s: &State, name: &str, args: &Value) -> Reply {
    let Some(tool) = s.tools.iter().find(|t| t.name == name) else {
        return Reply::refused(Source::None, "unknown_tool", format!("There's no tool named '{name}'.")).with(json!({"tool": name}));
    };
    if let Some(g) = tool.group {
        if !lock(&s.loaded).contains(g) {
            return Reply::refused(
                Source::None,
                "group_not_loaded",
                format!("Tool '{name}' is in group '{g}', which isn't loaded. Call groups_load with it first."),
            )
            .with(json!({"tool": name, "group": g}));
        }
    }
    if let Err(e) = validate(&tool.schema, args, "") {
        return Reply::refused(Source::None, "invalid_argument", format!("{}: {}.", e.path, e.message)).with(json!({"field": e.path}));
    }
    note_folders(s, args);
    match name {
        "groups_list" => groups_list(s),
        "groups_load" => group_change(s, args, true),
        "groups_unload" => group_change(s, args, false),
        "recent_calls" => {
            let limit = args.get("limit").and_then(Value::as_f64).unwrap_or(20.0) as usize;
            Reply::complete(Source::None, lock(&s.obs).recent_json(limit))
        }
        "call_stats" => Reply::complete(Source::None, lock(&s.obs).stats_json()),
        "server_info" => server_info(s),
        "board_summary" | "board_nets" | "board_tracks" | "board_footprints" | "board_stackup" => tools::board_read(name, args),
        "track_add" => crate::edit::track_add(args, None),
        "via_add" => crate::edit::via_add(args, None),
        "item_delete" => crate::edit::item_delete(args, None),
        "item_set_locked" => crate::edit::item_set_locked(args, None),
        "footprint_move" => crate::edit::footprint_move(args, None),
        "transaction_apply" => journal::transaction_apply(args),
        "drc_run" => checks::run(Check::Drc, args),
        "erc_run" => checks::run(Check::Erc, args),
        "route_prepare" => router::route_prepare(args),
        "route_run" => router::route_run(args),
        "route_plan_import" => router::route_plan_import(args),
        "route_apply" => router::route_apply(args),
        other => Reply::refused(Source::None, "unknown_tool", format!("Tool '{other}' has no handler.")),
    }
}

fn run_call(s: &State, id: Value, name: String, args: Value) {
    let began = Instant::now();
    let reply = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| call_tool(s, &name, &args))).unwrap_or_else(|_| {
        Reply::uncertain(
            Source::None,
            "internal_error",
            "The tool panicked. Any file it was working on is in an uncertain state, so read it again.",
        )
    });
    let ms = began.elapsed().as_millis() as u64;
    // Bound what an unknown name can add to the statistics.
    let label: String = name.chars().take(128).collect();
    lock(&s.obs).record(&label, ms, reply.outcome.as_str(), reply.reason.as_deref(), reply.is_error());
    let text = reply.to_json().to_string();
    let result = json!({"content": [{"type": "text", "text": text}], "isError": reply.is_error()});
    s.send(&json!({"jsonrpc": "2.0", "id": id, "result": result}));
}

fn tools_list(s: &State) -> Value {
    let l = lock(&s.loaded);
    let tools: Vec<Value> = s
        .tools
        .iter()
        .filter(|t| t.group.is_none_or(|g| l.contains(g)))
        .map(|t| json!({"name": t.name, "description": t.description, "inputSchema": t.schema, "annotations": {"readOnlyHint": t.read_only}}))
        .collect();
    json!({"tools": tools})
}

fn error(id: Value, code: i32, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

fn handle(s: &Arc<State>, msg: Value) {
    let Some(obj) = msg.as_object() else {
        s.send(&error(Value::Null, -32600, "Invalid Request: expected a JSON object"));
        return;
    };
    let method = obj.get("method").and_then(Value::as_str);
    let id = obj.get("id").cloned();
    let valid_id = id.as_ref().is_some_and(|i| i.is_string() || i.is_number());
    let Some(method) = method else {
        // A response to something we never sent has no method. Only reply to requests.
        if !(obj.contains_key("result") || obj.contains_key("error")) {
            s.send(&error(id.unwrap_or(Value::Null), -32600, "Invalid Request: no method"));
        }
        return;
    };
    if id.is_none() {
        return; // Notifications, known or unknown, get no reply.
    }
    let id = id.unwrap_or(Value::Null);
    if !valid_id {
        s.send(&error(Value::Null, -32600, "Invalid Request: id must be a string or a number"));
        return;
    }
    match method {
        "initialize" => {
            let asked = obj.get("params").and_then(|p| p.get("protocolVersion")).and_then(Value::as_str);
            let version = asked.filter(|a| PROTOCOL_VERSIONS.contains(a)).unwrap_or(PROTOCOL_VERSIONS[0]);
            s.send(&json!({"jsonrpc": "2.0", "id": id, "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": true}},
                "serverInfo": {"name": env!("CARGO_PKG_NAME"), "version": env!("CARGO_PKG_VERSION")},
                "instructions": "Reads and edits KiCad boards. Only the board_read group starts loaded: call groups_list and groups_load for the others. Edits need expected_revision, the SHA-256 from a previous reply.",
            }}));
        }
        "ping" => s.send(&json!({"jsonrpc": "2.0", "id": id, "result": {}})),
        "tools/list" => s.send(&json!({"jsonrpc": "2.0", "id": id, "result": tools_list(s)})),
        "tools/call" => {
            let params = obj.get("params");
            let Some(name) = params.and_then(|p| p.get("name")).and_then(Value::as_str).map(str::to_string) else {
                s.send(&error(id, -32602, "Invalid params: tools/call needs a tool name"));
                return;
            };
            let args = params.and_then(|p| p.get("arguments")).cloned().unwrap_or_else(|| json!({}));
            let long = s.tools.iter().find(|t| t.name == name).is_some_and(|t| t.long_running);
            if long {
                // Keep answering while an external program runs. Everything else
                // stays on this thread, so pipelined requests keep their order.
                let shared = Arc::clone(s);
                std::thread::spawn(move || run_call(&shared, id, name, args));
            } else {
                run_call(s, id, name, args);
            }
        }
        other => s.send(&error(id, -32601, &format!("Method not found: {other}"))),
    }
}

pub fn serve() -> i32 {
    let state = Arc::new(State::new());
    eprintln!("boardhand {} ready on stdio", env!("CARGO_PKG_VERSION"));
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let mut line = Vec::new();
    loop {
        line.clear();
        match reader.read_until(b'\n', &mut line) {
            Ok(0) => break,
            Ok(_) => {}
            Err(e) => {
                eprintln!("boardhand: stdin error: {e}");
                break;
            }
        }
        if line.iter().all(u8::is_ascii_whitespace) {
            continue;
        }
        match serde_json::from_slice::<Value>(&line) {
            Ok(msg) => handle(&state, msg),
            Err(e) => {
                eprintln!("boardhand: malformed input: {e}");
                state.send(&error(Value::Null, -32700, "Parse error"));
            }
        }
    }
    // The client has gone. Don't leave its external programs running.
    proc::kill_all_active();
    0
}

/// Names of the tools that are always listed. Used by tests.
pub fn always_listed() -> HashSet<&'static str> {
    catalogue().iter().filter(|t| t.group.is_none()).map(|t| t.name).collect()
}
