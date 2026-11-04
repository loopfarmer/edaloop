//! Autorouter round trip: manifest, export, Freerouting, session plan and apply.
use crate::board::{geometry_digest, Board, BoardError, Track, TrackKind};
use crate::edit::{check_lock, not_absolute, segment_fields, via_fields, insertion_point, Layout};
use crate::proc::{self, Ended, Spec};
use crate::reply::{Reply, Source};
use crate::sexpr::{apply_splices, Kind, Splice};
use crate::specctra::{self, Session};
use crate::util::{self, sha256_hex, ScratchDir};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::Duration;

const SRC: Source = Source::File;
pub const FORMAT: &str = "boardhand-route-manifest";

fn abs(args: &Value, field: &str) -> Result<PathBuf, Reply> {
    let raw = args[field].as_str().unwrap_or("");
    if Path::new(raw).is_absolute() {
        Ok(PathBuf::from(raw))
    } else {
        Err(not_absolute(field, raw))
    }
}

fn load_board(path: &Path) -> Result<Board, Reply> {
    let bytes = fs::read(path).map_err(|e| Reply::refused(SRC, "file_not_found", format!("{}: {e}.", path.display())).target(path))?;
    let rev = sha256_hex(&bytes);
    Board::parse(bytes).map_err(|e: BoardError| Reply::refused(SRC, "parse_error", format!("{}: {e}.", path.display())).target(path).revs(Some(&rev), None))
}

fn manifest_item(t: &Track, locked: bool, on_protected: bool) -> Value {
    let mut why = vec![];
    if locked {
        why.push("locked");
    }
    if on_protected {
        why.push("protected_net");
    }
    let mut v = crate::board::track_json(t);
    v["digest"] = json!(geometry_digest(t));
    v["protected_because"] = json!(why);
    v
}

pub fn route_prepare(args: &Value) -> Reply {
    let board_path = match abs(args, "board") {
        Ok(p) => p,
        Err(r) => return r,
    };
    let folder = match abs(args, "output_folder") {
        Ok(p) => p,
        Err(r) => return r,
    };
    let Some(exporter) = std::env::var_os("BOARDHAND_DSN_EXPORTER").filter(|v| !v.is_empty()) else {
        return Reply::unavailable(SRC, "no_exporter", "BOARDHAND_DSN_EXPORTER isn't set, so no DSN can be written. No files were written.").target(&board_path);
    };
    let b = match load_board(&board_path) {
        Ok(b) => b,
        Err(r) => return r,
    };
    let rev = b.revision();
    let known = b.known_nets();
    let explicit = args.get("protected_nets").and_then(Value::as_array);
    let nets: Vec<String> = match explicit {
        Some(a) => a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
        None => vec!["GND".to_string()],
    };
    if explicit.is_some() {
        if let Some(bad) = nets.iter().find(|n| !known.contains(*n)) {
            return Reply::refused(SRC, "unknown_net", format!("protected_nets names '{bad}', which this board doesn't have.")).target(&board_path).revs(Some(&rev), None);
        }
    }
    let absent: Vec<&String> = nets.iter().filter(|n| !known.contains(*n)).collect();
    let net_set: HashSet<&str> = nets.iter().map(String::as_str).collect();
    let mut items = vec![];
    for t in b.tracks() {
        let on_protected = t.kind == TrackKind::Via && t.net.as_deref().is_some_and(|n| net_set.contains(n));
        if t.locked || on_protected {
            items.push(manifest_item(&t, t.locked, on_protected));
        }
    }
    if let Err(e) = fs::create_dir_all(&folder) {
        return Reply::refused(SRC, "write_failed", format!("Couldn't create {}: {e}.", folder.display())).target(&board_path);
    }
    let stem = board_path.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_else(|| "board".into());
    let manifest_path = folder.join(format!("{stem}.boardhand-manifest.json"));
    let dsn_path = folder.join(format!("{stem}.dsn"));
    let doc = json!({
        "format": FORMAT,
        "version": 1,
        "board": board_path.to_string_lossy(),
        "revision": rev,
        "protected_nets": nets,
        "items": items,
    });
    let text = serde_json::to_vec_pretty(&doc).unwrap_or_default();
    if let Err(e) = util::atomic_replace(&manifest_path, &text) {
        return Reply::refused(SRC, "write_failed", format!("Couldn't write the manifest: {e}.")).target(&board_path);
    }
    // The exporter writes to a temporary name, so a failed run leaves no partial DSN.
    let tmp_dsn = folder.join(format!(".{stem}.boardhand-{}.dsn", uuid::Uuid::new_v4()));
    let timeout = args["timeout_s"].as_f64().unwrap_or(300.0) as u64;
    let cleanup = |reason: &str, detail: String, extra: Value| {
        let _ = fs::remove_file(&tmp_dsn);
        let _ = fs::remove_file(&manifest_path);
        Reply::unavailable(SRC, reason, format!("{detail} The manifest was removed and no DSN was left."))
            .target(&board_path)
            .revs(Some(&rev), None)
            .with(extra)
    };
    let scratch = match ScratchDir::new() {
        Ok(s) => s,
        Err(e) => return cleanup("scratch_failed", format!("Couldn't create a temporary folder: {e}."), json!({})),
    };
    let exporter_path = PathBuf::from(&exporter);
    let (program, cmd_args): (PathBuf, Vec<OsString>) = if exporter_path.is_file() {
        (exporter_path, vec![board_path.clone().into_os_string(), tmp_dsn.clone().into_os_string()])
    } else {
        let script = format!("{} \"$1\" \"$2\"", exporter.to_string_lossy());
        (
            PathBuf::from("/bin/sh"),
            vec!["-c".into(), script.into(), "boardhand".into(), board_path.clone().into_os_string(), tmp_dsn.clone().into_os_string()],
        )
    };
    let tmpdir = scratch.0.to_string_lossy().to_string();
    let finished = match proc::run(Spec { program: &program, args: cmd_args, cwd: None, env: vec![("TMPDIR", &tmpdir)], timeout: Duration::from_secs(timeout), capture_dir: &scratch.0 }) {
        Ok(f) => f,
        Err(e) => return cleanup("exporter_failed", format!("The exporter couldn't be started: {e}."), json!({})),
    };
    let diag = json!({"stderr_tail": finished.stderr_tail, "stdout_tail": finished.stdout_tail});
    match finished.ended {
        Ended::TimedOut => return cleanup("exporter_timeout", format!("The exporter was still running after {timeout} s and was killed."), diag),
        Ended::Signaled(s) => return cleanup("exporter_crashed", format!("The exporter was killed by signal {s}."), diag),
        Ended::Exited(0) => {}
        Ended::Exited(c) => return cleanup("exporter_failed", format!("The exporter exited with code {c}."), diag),
    }
    if fs::metadata(&tmp_dsn).map(|m| m.len()).unwrap_or(0) == 0 {
        return cleanup("no_dsn", "The exporter exited without writing a DSN file.".to_string(), diag);
    }
    let now = fs::read(&board_path).map(|b| sha256_hex(&b)).unwrap_or_default();
    if now != rev {
        return cleanup("revision_conflict", "The board changed while the DSN was exported.".to_string(), json!({"current_revision": now}));
    }
    if let Err(e) = fs::rename(&tmp_dsn, &dsn_path) {
        return cleanup("write_failed", format!("Couldn't move the DSN into place: {e}."), json!({}));
    }
    let _ = util::sync_dir(&folder);
    Reply::complete(SRC, json!({
        "manifest": manifest_path.to_string_lossy(),
        "dsn": dsn_path.to_string_lossy(),
        "protected_items": doc["items"].as_array().map(Vec::len),
        "protected_nets": doc["protected_nets"],
        "protected_nets_absent_from_board": absent,
    }))
    .target(&board_path)
    .revs(Some(&rev), Some(&rev))
}

pub fn route_run(args: &Value) -> Reply {
    let folder = match abs(args, "folder") {
        Ok(p) => p,
        Err(r) => return r,
    };
    let jar = match abs(args, "jar") {
        Ok(p) => p,
        Err(r) => return r,
    };
    if !folder.is_dir() {
        return Reply::refused(SRC, "file_not_found", format!("{} isn't a folder.", folder.display())).target(&folder);
    }
    if !jar.is_file() {
        return Reply::refused(SRC, "jar_not_found", format!("{} doesn't exist.", jar.display())).target(&folder);
    }
    let dsn = match args.get("dsn").and_then(Value::as_str) {
        Some(d) => {
            if !Path::new(d).is_absolute() {
                return not_absolute("dsn", d);
            }
            PathBuf::from(d)
        }
        None => {
            let found: Vec<PathBuf> = fs::read_dir(&folder)
                .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "dsn") && !p.file_name().is_some_and(|n| n.to_string_lossy().starts_with('.'))).collect())
                .unwrap_or_default();
            match found.as_slice() {
                [one] => one.clone(),
                [] => return Reply::refused(SRC, "file_not_found", "The folder holds no .dsn file.").target(&folder),
                many => return Reply::refused(SRC, "ambiguous_dsn", format!("The folder holds {} .dsn files. Name one with dsn.", many.len())).target(&folder),
            }
        }
    };
    if !dsn.is_file() {
        return Reply::refused(SRC, "file_not_found", format!("{} doesn't exist.", dsn.display())).target(&folder);
    }
    let Some(java) = proc::find_on_path("java") else {
        return Reply::unavailable(SRC, "java_missing", "java wasn't found on PATH. The router didn't run.").target(&folder);
    };
    let scratch = match ScratchDir::new() {
        Ok(s) => s,
        Err(e) => return Reply::unavailable(SRC, "scratch_failed", format!("Couldn't create a temporary folder: {e}.")).target(&folder),
    };
    let session_path = dsn.with_extension("ses");
    let tmp_ses = folder.join(format!(".boardhand-{}.ses", uuid::Uuid::new_v4()));
    let mut cmd: Vec<OsString> = vec![
        "-Djava.awt.headless=true".into(), "-jar".into(), jar.clone().into_os_string(),
        "-de".into(), dsn.clone().into_os_string(), "-do".into(), tmp_ses.clone().into_os_string(),
        "--gui.enabled=false".into(),
    ];
    if let Some(p) = args.get("max_passes").and_then(Value::as_f64) {
        cmd.push("-mp".into());
        cmd.push((p as u64).to_string().into());
    }
    let timeout = args["timeout_s"].as_f64().unwrap_or(3600.0) as u64;
    let run = proc::run(Spec {
        program: &java,
        args: cmd,
        cwd: Some(&folder),
        env: vec![("FREEROUTING__USAGE_AND_DIAGNOSTIC_DATA__DISABLE_ANALYTICS", "true")],
        timeout: Duration::from_secs(timeout),
        capture_dir: &scratch.0,
    });
    let finished = match run {
        Ok(f) => f,
        Err(e) => return Reply::unavailable(SRC, "java_missing", format!("java couldn't be started: {e}.")).target(&folder),
    };
    let (code, signal) = match finished.ended {
        Ended::Exited(c) => (Some(c), None),
        Ended::Signaled(s) => (None, Some(s)),
        Ended::TimedOut => (None, None),
    };
    let diag = json!({"exit_code": code, "signal": signal, "stderr_tail": finished.stderr_tail, "stdout_tail": finished.stdout_tail, "duration_ms": finished.elapsed.as_millis() as u64});
    let fail = |reason: &str, msg: String| {
        let _ = fs::remove_file(&tmp_ses);
        Reply::unavailable(SRC, reason, format!("{msg} No session was left.")).target(&folder).with(diag.clone())
    };
    match finished.ended {
        Ended::TimedOut => return fail("route_timeout", format!("Freerouting was still running after {timeout} s and was killed.")),
        Ended::Signaled(s) => return fail("route_crashed", format!("Freerouting was killed by signal {s}.")),
        Ended::Exited(c) if c != 0 => return fail("route_failed", format!("Freerouting exited with code {c}.")),
        Ended::Exited(_) => {}
    }
    let Ok(bytes) = fs::read(&tmp_ses) else {
        return fail("no_session", "Freerouting exited without writing a session file.".to_string());
    };
    let parsed = match specctra::parse(&bytes) {
        Ok(s) => s,
        Err(e) => return fail("bad_session", format!("Freerouting's session file doesn't parse: {e}.")),
    };
    if let Err(e) = fs::rename(&tmp_ses, &session_path) {
        return fail("write_failed", format!("Couldn't move the session into place: {e}."));
    }
    let _ = util::sync_dir(&folder);
    Reply::complete(SRC, json!({
        "session": session_path.to_string_lossy(),
        "session_sha256": sha256_hex(&bytes),
        "wires": parsed.wires.len(),
        "vias": parsed.vias.len(),
    }))
    .target(&folder)
    .with(diag)
}

struct Inputs {
    board_path: PathBuf,
    board: Board,
    manifest: Value,
    session: Session,
    session_path: PathBuf,
}

fn load_inputs(args: &Value) -> Result<Inputs, Reply> {
    let manifest_path = abs(args, "manifest")?;
    let session_path = abs(args, "session")?;
    let bytes = fs::read(&manifest_path).map_err(|e| Reply::refused(SRC, "file_not_found", format!("{}: {e}.", manifest_path.display())).target(&manifest_path))?;
    let manifest: Value = serde_json::from_slice(&bytes).map_err(|e| Reply::refused(SRC, "invalid_manifest", format!("The manifest isn't JSON: {e}.")).target(&manifest_path))?;
    let good = manifest["format"] == FORMAT && manifest["revision"].is_string() && manifest["items"].is_array() && manifest["board"].is_string();
    if !good {
        return Err(Reply::refused(SRC, "invalid_manifest", "The file isn't a boardhand route manifest.").target(&manifest_path));
    }
    let board_path = match args.get("board").and_then(Value::as_str) {
        Some(_) => abs(args, "board")?,
        None => PathBuf::from(manifest["board"].as_str().unwrap_or("")),
    };
    let board = load_board(&board_path)?;
    let rev = board.revision();
    let want = manifest["revision"].as_str().unwrap_or("");
    if rev != want {
        return Err(Reply::refused(SRC, "revision_conflict", format!("The board's hash is {rev}, but the manifest was prepared for {want}."))
            .target(&board_path)
            .revs(Some(&rev), None)
            .with(json!({"current_revision": rev, "manifest_revision": want})));
    }
    let ses = fs::read(&session_path).map_err(|e| Reply::refused(SRC, "file_not_found", format!("{}: {e}.", session_path.display())).target(&board_path))?;
    let session = specctra::parse(&ses).map_err(|e| Reply::refused(SRC, "session_parse_error", format!("{}: {e}.", session_path.display())).target(&board_path).revs(Some(&rev), None))?;
    Ok(Inputs { board_path, board, manifest, session, session_path })
}

/// How the session's copper resolves against this board.
struct Resolved {
    per_net: BTreeMap<String, (u64, u64)>,
    unknown_nets: BTreeSet<String>,
    unknown_layers: BTreeSet<String>,
}

/// A padstack's outer copper layers, size and drill, from the session and its naming.
struct ViaDims {
    size_nm: Option<i64>,
    drill_nm: Option<i64>,
    layers: Option<(String, String)>,
}

fn via_dims(b: &Board, s: &Session, name: &str) -> ViaDims {
    let by_name = specctra::padstack_name_dims(name);
    let copper: Vec<String> = b.copper_layers().iter().map(|l| l.name.clone()).collect();
    let ps = s.padstacks.get(name);
    let size_nm = ps.and_then(|p| p.size_nm).or(by_name.map(|d| d.0));
    let drill_nm = by_name.map(|d| d.1);
    let pos = |n: &str| copper.iter().position(|c| *c == n);
    let mut span: Option<(usize, usize)> = None;
    if let Some(p) = ps {
        for l in &p.layers {
            if let Some(i) = b.resolve_copper(l).and_then(|c| pos(&c)) {
                span = Some(match span {
                    None => (i, i),
                    Some((a, z)) => (a.min(i), z.max(i)),
                });
            }
        }
    }
    if span.is_none() {
        span = by_name.map(|d| (d.2, d.3)).filter(|d| d.1 < copper.len() && d.0 < d.1);
    }
    let layers = span.filter(|d| d.0 < d.1).map(|d| (copper[d.0].clone(), copper[d.1].clone()));
    ViaDims { size_nm, drill_nm, layers }
}

fn resolve(b: &Board, s: &Session) -> Resolved {
    let known = b.known_nets();
    let mut r = Resolved { per_net: BTreeMap::new(), unknown_nets: BTreeSet::new(), unknown_layers: BTreeSet::new() };
    for n in &s.nets {
        r.per_net.entry(n.clone()).or_insert((0, 0));
        if !known.contains(n) {
            r.unknown_nets.insert(n.clone());
        }
    }
    for w in &s.wires {
        r.per_net.entry(w.net.clone()).or_insert((0, 0)).0 += 1;
        if b.resolve_copper(&w.layer).is_none() {
            r.unknown_layers.insert(w.layer.clone());
        }
    }
    for v in &s.vias {
        r.per_net.entry(v.net.clone()).or_insert((0, 0)).1 += 1;
        if let Some(p) = s.padstacks.get(&v.padstack) {
            for l in &p.layers {
                if b.resolve_copper(l).is_none() {
                    r.unknown_layers.insert(l.clone());
                }
            }
        }
    }
    r
}

/// A segment the session adds: net, layer, width and both ends, in nm.
type NewSegment = (String, String, i64, (i64, i64), (i64, i64));

/// The session's copper matched against the board's existing items.
struct Matched {
    retained: HashSet<u32>,
    new_segments: Vec<NewSegment>,
    new_vias: Vec<usize>,
}

fn match_session(b: &Board, s: &Session, tracks: &[Track]) -> Matched {
    let tol = (s.nm_per_unit / 2.0).ceil() as i64 + 1;
    let mm = |v: f64| crate::util::to_nm(v);
    let close = |a: (i64, i64), b: (i64, i64)| (a.0 - b.0).abs() <= tol && (a.1 - b.1).abs() <= tol;
    let mut by_net: HashMap<&str, Vec<&Track>> = HashMap::new();
    for t in tracks {
        by_net.entry(t.net.as_deref().unwrap_or("")).or_default().push(t);
    }
    let mut retained = HashSet::new();
    let mut seen_new = HashSet::new();
    let mut m = Matched { retained: HashSet::new(), new_segments: vec![], new_vias: vec![] };
    for w in &s.wires {
        let layer = b.resolve_copper(&w.layer).unwrap_or_else(|| w.layer.clone());
        for pair in w.points_nm.windows(2) {
            let (p, q) = (pair[0], pair[1]);
            if p == q {
                continue;
            }
            let hit = by_net.get(w.net.as_str()).and_then(|c| {
                c.iter().find(|t| {
                    t.kind == TrackKind::Segment
                        && !retained.contains(&t.node)
                        && t.layers.first() == Some(&layer)
                        && (mm(t.width) - w.width_nm).abs() <= tol
                        && ((close((mm(t.start.0), mm(t.start.1)), p) && close((mm(t.end.0), mm(t.end.1)), q))
                            || (close((mm(t.start.0), mm(t.start.1)), q) && close((mm(t.end.0), mm(t.end.1)), p)))
                })
            });
            match hit {
                Some(t) => {
                    retained.insert(t.node);
                }
                None => {
                    let key = (w.net.clone(), layer.clone(), w.width_nm, p.min(q), p.max(q));
                    if seen_new.insert(key) {
                        m.new_segments.push((w.net.clone(), layer.clone(), w.width_nm, p, q));
                    }
                }
            }
        }
    }
    let mut seen_vias = HashSet::new();
    for (i, v) in s.vias.iter().enumerate() {
        let size = via_dims(b, s, &v.padstack).size_nm;
        let hit = by_net.get(v.net.as_str()).and_then(|c| {
            c.iter().find(|t| {
                t.kind == TrackKind::Via
                    && !retained.contains(&t.node)
                    && close((mm(t.start.0), mm(t.start.1)), v.at_nm)
                    && size.is_none_or(|z| (mm(t.width) - z).abs() <= tol)
            })
        });
        match hit {
            Some(t) => {
                retained.insert(t.node);
            }
            None => {
                if seen_vias.insert((v.net.clone(), v.at_nm, v.padstack.clone())) {
                    m.new_vias.push(i);
                }
            }
        }
    }
    m.retained = retained;
    m
}

/// Manifest items with no matching item on this board, counting duplicates.
fn missing_items(manifest: &Value, b: &Board) -> Vec<Value> {
    let mut have: HashMap<String, Vec<bool>> = HashMap::new();
    for t in b.tracks() {
        have.entry(geometry_digest(&t)).or_default().push(t.locked);
    }
    let mut missing = vec![];
    for item in manifest["items"].as_array().into_iter().flatten() {
        let digest = item["digest"].as_str().unwrap_or("");
        let want_locked = item["locked"].as_bool().unwrap_or(false);
        let slot = have.get_mut(digest).and_then(|v| {
            // A locked item must come back locked. Prefer an exact match.
            v.iter().position(|l| *l == want_locked).or(if want_locked { None } else { v.first().map(|_| 0) }).map(|i| v.remove(i))
        });
        if slot.is_none() {
            missing.push(item.clone());
        }
    }
    missing
}

pub fn route_plan_import(args: &Value) -> Reply {
    let i = match load_inputs(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let r = resolve(&i.board, &i.session);
    let tracks = i.board.tracks();
    let m = match_session(&i.board, &i.session, &tracks);
    let per_net: BTreeMap<&String, Value> = r.per_net.iter().map(|(k, v)| (k, json!({"wires": v.0, "vias": v.1}))).collect();
    // What the session would leave protected, had it been applied alone.
    let kept: HashSet<String> = tracks.iter().filter(|t| m.retained.contains(&t.node)).map(geometry_digest).collect();
    let would_lose: Vec<&Value> = i.manifest["items"].as_array().into_iter().flatten().filter(|it| !kept.contains(it["digest"].as_str().unwrap_or(""))).collect();
    let unknown_padstacks: BTreeSet<&String> = i.session.vias.iter().filter(|v| {
        let d = via_dims(&i.board, &i.session, &v.padstack);
        m.new_vias.iter().any(|k| i.session.vias[*k].padstack == v.padstack) && (d.size_nm.is_none() || d.drill_nm.is_none() || d.layers.is_none())
    }).map(|v| &v.padstack).collect();
    let rev = i.board.revision();
    Reply::complete(SRC, json!({
        "session": i.session_path.to_string_lossy(),
        "resolution": {"unit": i.session.resolution_unit, "value": i.session.resolution_value},
        "per_net": per_net,
        "total_wires": i.session.wires.len(),
        "total_vias": i.session.vias.len(),
        "unknown_nets": r.unknown_nets,
        "unknown_layers": r.unknown_layers,
        "unknown_padstacks": unknown_padstacks,
        "unsupported_wires": i.session.unsupported_wires.iter().map(|w| json!({"net": w.0, "shape": w.1})).collect::<Vec<_>>(),
        "placements_ignored": i.session.placements,
        "items_matching_board": m.retained.len(),
        "new_segments": m.new_segments.len(),
        "new_vias": m.new_vias.len(),
        "existing_tracks_not_in_session": tracks.len() - m.retained.len(),
        "protected_items_omitted_by_session": would_lose,
        "changed": false,
    }))
    .target(&i.board_path)
    .revs(Some(&rev), Some(&rev))
}

fn nm_mm(v: i64) -> f64 {
    v as f64 / 1_000_000.0
}

pub fn route_apply(args: &Value) -> Reply {
    let output = match abs(args, "output") {
        Ok(p) => p,
        Err(r) => return r,
    };
    let i = match load_inputs(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let b = &i.board;
    let s = &i.session;
    let rev = b.revision();
    let refuse = |reason: &str, d: String| Reply::refused(SRC, reason, d).target(&i.board_path).revs(Some(&rev), None);
    // The output must be a new file.
    let Some(parent) = output.parent().filter(|p| p.is_dir()) else {
        return refuse("output_folder_missing", format!("The folder of {} doesn't exist.", output.display()));
    };
    let Some(name) = output.file_name() else {
        return refuse("invalid_argument", "output: has no file name.".to_string()).with(json!({"field": "output"}));
    };
    let real_parent = fs::canonicalize(parent).unwrap_or_else(|_| parent.to_path_buf());
    let real_output = real_parent.join(name);
    let real_input = fs::canonicalize(&i.board_path).unwrap_or_else(|_| i.board_path.clone());
    if real_output == real_input {
        return refuse("output_is_input", "route_apply never writes over the input board.".to_string());
    }
    if fs::symlink_metadata(&real_output).is_ok() {
        return refuse("output_exists", format!("{} already exists.", output.display()));
    }
    if let Err(r) = check_lock(&real_output) {
        return r.revs(Some(&rev), None);
    }
    let r = resolve(b, s);
    if let Some(bad) = r.unknown_nets.iter().next() {
        return refuse("unknown_net", format!("The session names net '{bad}', which this board doesn't have ({} unknown in all).", r.unknown_nets.len())).with(json!({"unknown_nets": r.unknown_nets}));
    }
    if let Some(bad) = r.unknown_layers.iter().next() {
        return refuse("unknown_layer", format!("The session names layer '{bad}', which isn't a copper layer of this board.")).with(json!({"unknown_layers": r.unknown_layers}));
    }
    if !s.unsupported_wires.is_empty() {
        return refuse("unsupported_wire_shape", format!("The session has {} wires in shapes this reader can't convert, starting with '{}' on net '{}'.", s.unsupported_wires.len(), s.unsupported_wires[0].1, s.unsupported_wires[0].0));
    }
    let tracks = b.tracks();
    let mut m = match_session(b, s, &tracks);
    let keep_protected = args["retain_protected"].as_bool().unwrap_or(false);
    let mut reinstated = 0;
    if keep_protected {
        let wanted: HashSet<&str> = i.manifest["items"].as_array().into_iter().flatten().filter_map(|it| it["digest"].as_str()).collect();
        for t in &tracks {
            if wanted.contains(geometry_digest(t).as_str()) && m.retained.insert(t.node) {
                reinstated += 1;
            }
        }
    }

    // Build the new tracks' text from the file's own layout.
    let t = &b.tree;
    let top = &b.top;
    let is_track = |id: u32| t.kind(id) == Kind::List && matches!(t.head(id), "segment" | "arc" | "via");
    let last_kept = top.iter().rposition(|id| is_track(*id) && m.retained.contains(id));
    let first_track = top.iter().position(|id| is_track(*id));
    let (offset, sibling) = match (last_kept, first_track) {
        (Some(k), _) => (t.span(top[k]).1, top[k]),
        (None, Some(f)) => (gap_start(b, f), top[f]),
        (None, None) => {
            let (o, _, s) = insertion_point(b);
            (o, s)
        }
    };
    let layout = Layout::from_sibling(t, sibling);
    let mut new_text = vec![];
    for (net, layer, w, p, q) in &m.new_segments {
        let id = uuid::Uuid::new_v4().to_string();
        let Some(f) = segment_fields(b, (nm_mm(p.0), nm_mm(p.1)), (nm_mm(q.0), nm_mm(q.1)), nm_mm(*w), layer, net, &id) else {
            return refuse("unknown_net", format!("Net '{net}' has no number in this board's table."));
        };
        new_text.extend(layout.render("segment", &f));
    }
    for k in &m.new_vias {
        let v = &s.vias[*k];
        let d = via_dims(b, s, &v.padstack);
        let (Some(size), Some(drill), Some(layers)) = (d.size_nm, d.drill_nm, d.layers) else {
            return refuse("unknown_via_padstack", format!("Via padstack '{}' doesn't state a size, drill and layers this reader can determine.", v.padstack));
        };
        if drill >= size {
            return refuse("unknown_via_padstack", format!("Via padstack '{}' has a drill no smaller than its size.", v.padstack));
        }
        let id = uuid::Uuid::new_v4().to_string();
        let Some(f) = via_fields(b, (nm_mm(v.at_nm.0), nm_mm(v.at_nm.1)), nm_mm(size), nm_mm(drill), (&layers.0, &layers.1), &v.net, &id) else {
            return refuse("unknown_net", format!("Net '{}' has no number in this board's table.", v.net));
        };
        new_text.extend(layout.render("via", &f));
    }
    let mut splices = vec![Splice { start: offset, end: offset, text: new_text }];
    let mut removed = 0;
    for (idx, id) in top.iter().enumerate() {
        if is_track(*id) && !m.retained.contains(id) {
            splices.push(Splice { start: gap_start(b, idx), end: t.span(*id).1, text: vec![] });
            removed += 1;
        }
    }
    let new_bytes = apply_splices(&t.src, &splices);

    // Write the new file without ever replacing an existing one.
    let tmp = util::temp_beside(&real_output);
    let written = (|| {
        util::write_new_synced(&tmp, &new_bytes)?;
        fs::hard_link(&tmp, &real_output)?;
        fs::remove_file(&tmp)?;
        util::sync_dir(&real_parent)
    })();
    if let Err(e) = written {
        let _ = fs::remove_file(&tmp);
        let exists = fs::symlink_metadata(&real_output).is_ok();
        if exists {
            return Reply::uncertain(SRC, "write_failed", format!("Writing {} failed: {e}. A file exists there, and its state is uncertain.", real_output.display())).target(&real_output).revs(Some(&rev), None);
        }
        return refuse("write_failed", format!("Couldn't write {}: {e}.", real_output.display()));
    }

    // Everything from here on looks at the written file, not the plan.
    let uncertain = |d: String, after: Option<&str>| Reply::uncertain(SRC, "readback_failed", d).target(&real_output).revs(Some(&rev), after);
    let back = match fs::read(&real_output) {
        Ok(v) => v,
        Err(e) => return uncertain(format!("The output couldn't be read back: {e}. Its state is uncertain."), None),
    };
    let after = sha256_hex(&back);
    let nb = match Board::parse(back) {
        Ok(v) => v,
        Err(e) => return uncertain(format!("The output doesn't parse: {e}. Its state is uncertain."), Some(&after)),
    };
    let lost = missing_items(&i.manifest, &nb);
    if !lost.is_empty() {
        let del = fs::remove_file(&real_output).and_then(|()| util::sync_dir(&real_parent));
        if let Err(e) = del {
            return Reply::uncertain(SRC, "output_delete_failed", format!("Protected items were lost ({} in all), and the output couldn't be deleted: {e}.", lost.len())).target(&real_output).revs(Some(&rev), Some(&after)).with(json!({"lost": lost}));
        }
        let gone = fs::symlink_metadata(&real_output).is_err();
        return Reply::refused(SRC, "protected_copper_lost", format!("Protected items would have been lost ({} in all), so the output was deleted.", lost.len()))
            .target(&i.board_path)
            .revs(Some(&rev), None)
            .with(json!({"lost": lost, "output_deleted": gone}));
    }
    let non_track = |bd: &Board| -> Vec<[u8; 32]> {
        bd.top.iter().filter(|id| !(bd.tree.kind(**id) == Kind::List && matches!(bd.tree.head(**id), "segment" | "arc" | "via"))).map(|id| util::sha256_raw(bd.tree.bytes(*id))).collect()
    };
    if non_track(b) != non_track(&nb) {
        return uncertain("A top-level node other than a track differs in the output. Its state is uncertain.".to_string(), Some(&after));
    }
    let count = nb.tracks().len();
    let expected = m.retained.len() + m.new_segments.len() + m.new_vias.len();
    if count != expected {
        return uncertain(format!("The output holds {count} tracks and vias, but {expected} were expected. Its state is uncertain."), Some(&after));
    }
    let per_net: BTreeMap<&String, Value> = r.per_net.iter().map(|(k, v)| (k, json!({"wires": v.0, "vias": v.1}))).collect();
    Reply::complete(SRC, json!({
        "output": real_output.to_string_lossy(),
        "input_unchanged": fs::read(&i.board_path).map(|b| sha256_hex(&b) == rev).unwrap_or(false),
        "items_in_output": count,
        "retained_existing": m.retained.len(),
        "reinstated_protected": reinstated,
        "added_segments": m.new_segments.len(),
        "added_vias": m.new_vias.len(),
        "removed_existing": removed,
        "protected_items_checked": i.manifest["items"].as_array().map(Vec::len),
        "per_net": per_net,
        "placements_ignored": s.placements,
        "changed": true,
    }))
    .target(&real_output)
    .revs(Some(&rev), Some(&after))
}

fn gap_start(b: &Board, top_idx: usize) -> usize {
    let t = &b.tree;
    if top_idx == 0 {
        // The first child after the head.
        t.children(t.root).next().map(|h| t.span(h).1).unwrap_or(t.span(t.root).0 + 1)
    } else {
        t.span(b.top[top_idx - 1]).1
    }
}
