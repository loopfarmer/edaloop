//! The safe-write path of section 8 and the `board_edit` tools.
use crate::board::{footprint_json, node_uuid, track_json, Board, NetForm, Track, TrackKind};
use crate::reply::{Reply, Source};
use crate::sexpr::{apply_splices, Id, Kind, Splice, Tree};
use crate::util::{self, fmt_mm, quote, sha256_hex, to_nm};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

pub const SRC: Source = Source::File;

/// Called between the rename and the readback. Only tests pass one.
pub type Hook<'a> = Option<&'a dyn Fn(&Path)>;

pub fn lock_files(path: &Path) -> Vec<PathBuf> {
    let dir = util::parent_of(path);
    let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    let stem = path.file_stem().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    vec![dir.join(format!("~{name}.lck")), dir.join(format!("~{stem}.kicad_pro.lck"))]
}

/// A refusal if a KiCad lock file for this board exists.
pub fn check_lock(path: &Path) -> Result<(), Reply> {
    for l in lock_files(path) {
        if fs::symlink_metadata(&l).is_ok() {
            return Err(Reply::refused(
                SRC,
                "editor_lock",
                format!("KiCad's lock file {} exists, so the board may be open in an editor.", l.display()),
            )
            .target(path)
            .with(json!({"lock_file": l.to_string_lossy()})));
        }
    }
    Ok(())
}

/// How the top-level node list is expected to differ after the edit.
#[derive(Clone, Copy, Debug)]
pub enum Change {
    Insert(usize),
    Replace(usize),
    Delete(usize),
}

fn digests_match(before: &[[u8; 32]], after: &[[u8; 32]], change: Change) -> bool {
    match change {
        Change::Insert(i) => {
            after.len() == before.len() + 1 && i <= before.len() && before[..i] == after[..i] && before[i..] == after[i + 1..]
        }
        Change::Replace(i) => {
            after.len() == before.len() && i < before.len() && before[..i] == after[..i] && before[i + 1..] == after[i + 1..]
        }
        Change::Delete(i) => {
            after.len() + 1 == before.len() && i < before.len() && before[..i] == after[..i] && before[i + 1..] == after[i..]
        }
    }
}

type Verify<'a> = &'a dyn Fn(&Board) -> Result<Value, String>;

fn check(before: &[[u8; 32]], bytes: &[u8], change: Change, verify: Verify<'_>) -> Result<Value, String> {
    let b = Board::parse(bytes.to_vec()).map_err(|e| format!("the file doesn't parse: {e}"))?;
    if !digests_match(before, &b.digests(), change) {
        return Err("a top-level node outside the edit differs".to_string());
    }
    verify(&b)
}

/// Sections 8.3 to 8.5: temp file, sync, rename, sync folder, read back, verify.
pub fn commit(path: &Path, before: &Board, new_bytes: Vec<u8>, change: Change, verify: Verify<'_>, hook: Hook<'_>) -> Reply {
    let old_rev = before.revision();
    let digests = before.digests();
    // Check the result in memory first, so a faulty plan never reaches the disk.
    if let Err(e) = check(&digests, &new_bytes, change, verify) {
        return Reply::refused(SRC, "edit_check_failed", format!("The planned edit failed its check before writing: {e}."))
            .target(path)
            .revs(Some(&old_rev), None);
    }
    let meta = match fs::metadata(path) {
        Ok(m) => m,
        Err(e) => {
            return Reply::refused(SRC, "write_failed", format!("Couldn't stat the board: {e}.")).target(path).revs(Some(&old_rev), None)
        }
    };
    let tmp = util::temp_beside(path);
    let prepared = (|| {
        util::write_new_synced(&tmp, &new_bytes)?;
        fs::set_permissions(&tmp, meta.permissions())
    })();
    if let Err(e) = prepared {
        let _ = fs::remove_file(&tmp);
        return Reply::refused(SRC, "write_failed", format!("Couldn't write a temporary file beside the board: {e}."))
            .target(path)
            .revs(Some(&old_rev), None);
    }
    // Narrow the race: look at the lock and the hash again just before the rename.
    if let Err(r) = check_lock(path) {
        let _ = fs::remove_file(&tmp);
        return r.revs(Some(&old_rev), None);
    }
    match fs::read(path) {
        Ok(cur) if sha256_hex(&cur) == old_rev => {}
        Ok(cur) => {
            let _ = fs::remove_file(&tmp);
            let h = sha256_hex(&cur);
            return Reply::refused(SRC, "revision_conflict", "The file changed while the edit was being prepared.")
                .target(path)
                .revs(Some(&h), None)
                .with(json!({"current_revision": h}));
        }
        Err(e) => {
            let _ = fs::remove_file(&tmp);
            return Reply::refused(SRC, "write_failed", format!("Couldn't re-read the board: {e}."))
                .target(path)
                .revs(Some(&old_rev), None);
        }
    }
    if let Err(e) = fs::rename(&tmp, path) {
        let _ = fs::remove_file(&tmp);
        return Reply::refused(SRC, "write_failed", format!("Couldn't rename the temporary file over the board: {e}."))
            .target(path)
            .revs(Some(&old_rev), None);
    }
    let dir_sync = util::sync_dir(&util::parent_of(path));
    if let Some(h) = hook {
        h(path);
    }
    let read_back = match fs::read(path) {
        Ok(b) => b,
        Err(e) => {
            return Reply::uncertain(
                SRC,
                "readback_failed",
                format!("The rename happened, but the file couldn't be read back: {e}. Its state is uncertain."),
            )
            .target(path)
            .revs(Some(&old_rev), None);
        }
    };
    let after = sha256_hex(&read_back);
    if read_back != new_bytes {
        return Reply::uncertain(SRC, "readback_failed", "The file read back differs from the bytes written. Its state is uncertain.")
            .target(path)
            .revs(Some(&old_rev), Some(&after));
    }
    let observed = match check(&digests, &read_back, change, verify) {
        Ok(v) => v,
        Err(e) => {
            return Reply::uncertain(SRC, "readback_failed", format!("The readback check failed: {e}. The file's state is uncertain."))
                .target(path)
                .revs(Some(&old_rev), Some(&after));
        }
    };
    if let Err(e) = dir_sync {
        return Reply::uncertain(
            SRC,
            "folder_sync_failed",
            format!("The file reads back correctly, but syncing its folder failed, so the rename may not survive a power loss: {e}."),
        )
        .target(path)
        .revs(Some(&old_rev), Some(&after))
        .with(json!({"observed": observed}));
    }
    Reply::complete(SRC, observed).target(path).revs(Some(&old_rev), Some(&after))
}

/// Common start of an edit tool: real path, lock check, revision check, parse.
pub fn open_for_edit(args: &Value) -> Result<(PathBuf, Board), Reply> {
    let raw = args["board"].as_str().unwrap_or("");
    let p = Path::new(raw);
    if !p.is_absolute() {
        return Err(not_absolute("board", raw));
    }
    let real = match fs::canonicalize(p) {
        Ok(r) => r,
        Err(e) => return Err(Reply::refused(SRC, "file_not_found", format!("{raw}: {e}.")).target(p)),
    };
    check_lock(&real)?;
    let bytes = match fs::read(&real) {
        Ok(b) => b,
        Err(e) => return Err(Reply::refused(SRC, "read_failed", format!("{raw}: {e}.")).target(&real)),
    };
    let cur = sha256_hex(&bytes);
    let expected = args["expected_revision"].as_str().unwrap_or("");
    if cur != expected {
        return Err(Reply::refused(SRC, "revision_conflict", format!("The file's hash is {cur}, not the expected {expected}."))
            .target(&real)
            .revs(Some(&cur), None)
            .with(json!({"current_revision": cur})));
    }
    let board =
        Board::parse(bytes).map_err(|e| Reply::refused(SRC, "parse_error", format!("{raw}: {e}.")).target(&real).revs(Some(&cur), None))?;
    Ok((real, board))
}

pub fn not_absolute(field: &str, raw: &str) -> Reply {
    Reply::refused(SRC, "invalid_argument", format!("{field}: must be an absolute path, got '{raw}'.")).with(json!({"field": field}))
}

// ---- text generation ----

/// Whitespace before a node, from the end of its previous sibling.
fn gap_before(t: &Tree, id: Id) -> (usize, usize) {
    let (start, _) = t.span(id);
    let mut prev_end = t.span(t.root).0 + 1;
    for c in t.children(t.root) {
        if c == id {
            break;
        }
        prev_end = t.span(c).1;
    }
    (prev_end, start)
}

/// Line ending and child indent for new text, copied from the file's own layout.
pub struct Layout {
    pub lead: Vec<u8>,
    pub inline: bool,
    pub nl: String,
    pub indent: String,
    pub child: String,
}

impl Layout {
    pub fn from_sibling(t: &Tree, sibling: Id) -> Layout {
        let (a, b) = gap_before(t, sibling);
        let lead = t.src[a..b].to_vec();
        let text = String::from_utf8_lossy(&lead).to_string();
        // An inline track means an older, single-line layout. Other short nodes say nothing.
        let inline_sibling = matches!(t.head(sibling), "segment" | "arc" | "via") && !t.bytes(sibling).contains(&b'\n');
        let Some(pos) = text.rfind('\n') else {
            return Layout {
                lead: if lead.is_empty() { b" ".to_vec() } else { lead },
                inline: true,
                nl: String::new(),
                indent: String::new(),
                child: String::new(),
            };
        };
        let indent = text[pos + 1..].to_string();
        let nl = if text[..pos].ends_with('\r') { "\r\n" } else { "\n" }.to_string();
        let child = if indent.contains('\t') || indent.is_empty() { format!("{indent}\t") } else { format!("{indent}  ") };
        Layout { lead, inline: inline_sibling, nl, indent, child }
    }

    pub fn render(&self, head: &str, fields: &[String]) -> Vec<u8> {
        let mut s = String::from("(");
        s.push_str(head);
        if self.inline {
            for f in fields {
                s.push(' ');
                s.push_str(f);
            }
            s.push(')');
        } else {
            for f in fields {
                s.push_str(&self.nl);
                s.push_str(&self.child);
                s.push_str(f);
            }
            s.push_str(&self.nl);
            s.push_str(&self.indent);
            s.push(')');
        }
        let mut out = self.lead.clone();
        out.extend_from_slice(s.as_bytes());
        out
    }
}

/// Where a new track goes: after the last track, else before the first node that
/// KiCad writes after tracks, else at the end. Returns (byte offset, top index, sibling).
pub fn insertion_point(b: &Board) -> (usize, usize, Id) {
    let t = &b.tree;
    let is_track = |id: Id| t.kind(id) == Kind::List && matches!(t.head(id), "segment" | "arc" | "via");
    if let Some(i) = b.top.iter().rposition(|id| is_track(*id)) {
        return (t.span(b.top[i]).1, i + 1, b.top[i]);
    }
    let later = |id: Id| matches!(t.head(id), "zone" | "group" | "generated" | "embedded_fonts" | "embedded_files");
    if let Some(i) = b.top.iter().position(|id| later(*id)) {
        return (gap_before(t, b.top[i]).0, i, b.top[i]);
    }
    let last = *b.top.last().expect("a kicad_pcb has at least one child to edit");
    (t.span(last).1, b.top.len(), last)
}

/// The file's own reference form, or None when the net has no number.
pub fn net_field(b: &Board, form: NetForm, name: &str) -> Option<String> {
    match form {
        NetForm::Named => Some(format!("(net {})", quote(name))),
        NetForm::Numbered => b.net_number(name).map(|n| format!("(net {n})")),
    }
}

pub fn id_field(b: &Board, uuid: &str) -> String {
    let t = &b.tree;
    let old = b.top.iter().any(|id| t.kind(*id) == Kind::List && t.find(*id, "tstamp").is_some());
    if old {
        format!("(tstamp {uuid})")
    } else {
        format!("(uuid {})", quote(uuid))
    }
}

pub fn segment_fields(
    b: &Board,
    start: (f64, f64),
    end: (f64, f64),
    width: f64,
    layer: &str,
    net: &str,
    uuid: &str,
) -> Option<Vec<String>> {
    Some(vec![
        format!("(start {} {})", fmt_mm(start.0), fmt_mm(start.1)),
        format!("(end {} {})", fmt_mm(end.0), fmt_mm(end.1)),
        format!("(width {})", fmt_mm(width)),
        format!("(layer {})", quote(layer)),
        net_field(b, b.net_form(), net)?,
        id_field(b, uuid),
    ])
}

pub fn via_fields(b: &Board, at: (f64, f64), size: f64, drill: f64, layers: (&str, &str), net: &str, uuid: &str) -> Option<Vec<String>> {
    Some(vec![
        format!("(at {} {})", fmt_mm(at.0), fmt_mm(at.1)),
        format!("(size {})", fmt_mm(size)),
        format!("(drill {})", fmt_mm(drill)),
        format!("(layers {} {})", quote(layers.0), quote(layers.1)),
        net_field(b, b.net_form(), net)?,
        id_field(b, uuid),
    ])
}

fn pt(v: &Value) -> (f64, f64) {
    (v["x"].as_f64().unwrap_or(0.0), v["y"].as_f64().unwrap_or(0.0))
}

fn near(a: f64, b: f64) -> bool {
    to_nm(a) == to_nm(b)
}

fn track_by_uuid(b: &Board, uuid: &str) -> Vec<Track> {
    b.tracks().into_iter().filter(|t| t.uuid.as_deref() == Some(uuid)).collect()
}

fn top_index(b: &Board, id: Id) -> usize {
    b.top.iter().position(|x| *x == id).expect("node came from this board's top level")
}

fn add_item(path: &Path, b: &Board, head: &str, fields: &[String], uuid: &str, verify: Verify<'_>, hook: Hook<'_>) -> Reply {
    let (offset, index, sib) = insertion_point(b);
    let text = Layout::from_sibling(&b.tree, sib).render(head, fields);
    let new = apply_splices(&b.tree.src, &[Splice { start: offset, end: offset, text }]);
    let r = commit(path, b, new, Change::Insert(index), verify, hook);
    r.with(json!({"uuid": uuid}))
}

fn resolve_net_or_refuse(b: &Board, path: &Path, net: &str) -> Result<(), Reply> {
    if !net.is_empty() && b.known_nets().contains(net) && net_field(b, b.net_form(), net).is_some() {
        return Ok(());
    }
    Err(Reply::refused(SRC, "unknown_net", format!("The board has no net named '{net}'.")).target(path).revs(Some(&b.revision()), None))
}

pub fn track_add(args: &Value, hook: Hook<'_>) -> Reply {
    let (path, b) = match open_for_edit(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let (start, end) = (pt(&args["start"]), pt(&args["end"]));
    let width = args["width"].as_f64().unwrap_or(0.0);
    let net = args["net"].as_str().unwrap_or("").to_string();
    let layer_in = args["layer"].as_str().unwrap_or("");
    if to_nm(start.0) == to_nm(end.0) && to_nm(start.1) == to_nm(end.1) {
        return Reply::refused(SRC, "invalid_argument", "end: a track's end must differ from its start.")
            .target(&path)
            .with(json!({"field": "end"}));
    }
    let Some(layer) = b.resolve_copper(layer_in) else {
        return Reply::refused(SRC, "unknown_layer", format!("'{layer_in}' isn't a copper layer of this board."))
            .target(&path)
            .revs(Some(&b.revision()), None);
    };
    if let Err(r) = resolve_net_or_refuse(&b, &path, &net) {
        return r;
    }
    let uuid = uuid::Uuid::new_v4().to_string();
    let Some(fields) = segment_fields(&b, start, end, width, &layer, &net, &uuid) else {
        return Reply::refused(SRC, "unknown_net", format!("No number for net '{net}'.")).target(&path);
    };
    let (u2, n2, l2) = (uuid.clone(), net.clone(), layer.clone());
    let verify = move |nb: &Board| -> Result<Value, String> {
        let found = track_by_uuid(nb, &u2);
        let [tr] = found.as_slice() else { return Err(format!("{} items carry the new uuid", found.len())) };
        let good = tr.kind == TrackKind::Segment
            && tr.net.as_deref() == Some(&n2)
            && tr.layers == [l2.clone()]
            && near(tr.start.0, start.0)
            && near(tr.start.1, start.1)
            && near(tr.end.0, end.0)
            && near(tr.end.1, end.1)
            && near(tr.width, width);
        if good {
            Ok(json!({"changed": true, "item": track_json(tr)}))
        } else {
            Err("the new track doesn't match the request".into())
        }
    };
    add_item(&path, &b, "segment", &fields, &uuid, &verify, hook)
}

pub fn via_add(args: &Value, hook: Hook<'_>) -> Reply {
    let (path, b) = match open_for_edit(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let at = pt(&args["position"]);
    let (size, drill) = (args["size"].as_f64().unwrap_or(0.0), args["drill"].as_f64().unwrap_or(0.0));
    let net = args["net"].as_str().unwrap_or("").to_string();
    if drill >= size {
        return Reply::refused(SRC, "invalid_argument", format!("drill: the drill {drill} must be smaller than the size {size}."))
            .target(&path)
            .with(json!({"field": "drill"}));
    }
    if let Err(r) = resolve_net_or_refuse(&b, &path, &net) {
        return r;
    }
    let copper = b.copper_layers();
    let (Some(first), Some(last)) = (copper.first().map(|l| l.name.clone()), copper.last().map(|l| l.name.clone())) else {
        return Reply::refused(SRC, "unknown_layer", "The board lists no copper layers.").target(&path);
    };
    if first == last {
        return Reply::refused(SRC, "unknown_layer", "A via needs two copper layers, and this board has one.").target(&path);
    }
    let uuid = uuid::Uuid::new_v4().to_string();
    let Some(fields) = via_fields(&b, at, size, drill, (&first, &last), &net, &uuid) else {
        return Reply::refused(SRC, "unknown_net", format!("No number for net '{net}'.")).target(&path);
    };
    let (u2, n2) = (uuid.clone(), net.clone());
    let verify = move |nb: &Board| -> Result<Value, String> {
        let found = track_by_uuid(nb, &u2);
        let [tr] = found.as_slice() else { return Err(format!("{} items carry the new uuid", found.len())) };
        let good = tr.kind == TrackKind::Via
            && tr.net.as_deref() == Some(&n2)
            && near(tr.start.0, at.0)
            && near(tr.start.1, at.1)
            && near(tr.width, size)
            && near(tr.drill, drill);
        if good {
            Ok(json!({"changed": true, "item": track_json(tr)}))
        } else {
            Err("the new via doesn't match the request".into())
        }
    };
    add_item(&path, &b, "via", &fields, &uuid, &verify, hook)
}

pub fn item_delete(args: &Value, hook: Hook<'_>) -> Reply {
    let (path, b) = match open_for_edit(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let rev = b.revision();
    let uuid = args["uuid"].as_str().unwrap_or("").to_string();
    let found = track_by_uuid(&b, &uuid);
    let tr = match found.as_slice() {
        [] => {
            let other = b.top.iter().any(|id| b.tree.kind(*id) == Kind::List && node_uuid(&b.tree, *id).as_deref() == Some(&uuid));
            let what = if other { "It belongs to an item that isn't a track or via." } else { "No item has it." };
            return Reply::refused(SRC, "not_found", format!("No track or via has uuid {uuid}. {what}"))
                .target(&path)
                .revs(Some(&rev), None);
        }
        [one] => one,
        many => {
            return Reply::refused(SRC, "ambiguous_uuid", format!("{} items share uuid {uuid}.", many.len()))
                .target(&path)
                .revs(Some(&rev), None)
        }
    };
    if tr.locked {
        return Reply::refused(SRC, "item_locked", format!("The {} {uuid} is locked.", tr.kind.as_str()))
            .target(&path)
            .revs(Some(&rev), None);
    }
    let (gap_start, _) = gap_before(&b.tree, tr.node);
    let (_, end) = b.tree.span(tr.node);
    let new = apply_splices(&b.tree.src, &[Splice { start: gap_start, end, text: vec![] }]);
    let removed = track_json(tr);
    let u2 = uuid.clone();
    let verify = move |nb: &Board| -> Result<Value, String> {
        if track_by_uuid(nb, &u2).is_empty() {
            Ok(json!({"changed": true, "removed": removed}))
        } else {
            Err("the item is still present".into())
        }
    };
    // A group that names the item keeps a dangling member. KiCad tolerates that;
    // removing it would edit a second node, so it's reported instead.
    let t = &b.tree;
    let mentioned = b.top.iter().any(|id| {
        matches!(t.head(*id), "group" | "generated") && t.find(*id, "members").is_some_and(|m| t.args(m).any(|a| t.text(a) == uuid))
    });
    let r = commit(&path, &b, new, Change::Delete(top_index(&b, tr.node)), &verify, hook).with(json!({"uuid": uuid}));
    if mentioned {
        r.with(json!({"note": "a group still lists this uuid as a member"}))
    } else {
        r
    }
}

pub fn item_set_locked(args: &Value, hook: Hook<'_>) -> Reply {
    let (path, b) = match open_for_edit(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let rev = b.revision();
    let uuid = args["uuid"].as_str().unwrap_or("").to_string();
    let want = args["locked"].as_bool().unwrap_or(false);
    let t = &b.tree;
    let matches: Vec<Id> =
        b.top.iter().copied().filter(|id| t.kind(*id) == Kind::List && node_uuid(t, *id).as_deref() == Some(&uuid)).collect();
    let node = match matches.as_slice() {
        [] => return Reply::refused(SRC, "not_found", format!("No item has uuid {uuid}.")).target(&path).revs(Some(&rev), None),
        [one] => *one,
        many => {
            return Reply::refused(SRC, "ambiguous_uuid", format!("{} items share uuid {uuid}.", many.len()))
                .target(&path)
                .revs(Some(&rev), None)
        }
    };
    if crate::board::node_locked(t, node) == want {
        return Reply::complete(SRC, json!({"changed": false, "uuid": uuid, "locked": want})).target(&path).revs(Some(&rev), Some(&rev));
    }
    let splices = lock_splices(t, node, want);
    let new = apply_splices(&t.src, &splices);
    let u2 = uuid.clone();
    let verify = move |nb: &Board| -> Result<Value, String> {
        let t = &nb.tree;
        let found = nb.top.iter().find(|id| t.kind(**id) == Kind::List && node_uuid(t, **id).as_deref() == Some(&u2));
        match found {
            Some(id) if crate::board::node_locked(t, *id) == want => Ok(json!({"changed": true, "uuid": u2, "locked": want})),
            Some(_) => Err("the lock state didn't change".into()),
            None => Err("the item is gone".into()),
        }
    };
    commit(&path, &b, new, Change::Replace(top_index(&b, node)), &verify, hook)
}

/// Remove or add the marker so that lock then unlock restores the original bytes.
fn lock_splices(t: &Tree, node: Id, want: bool) -> Vec<Splice> {
    let mut kids: Vec<Id> = t.children(node).collect();
    let marker = kids
        .iter()
        .position(|c| (t.kind(*c) == Kind::List && t.head(*c) == "locked") || (t.kind(*c) == Kind::Atom && t.bytes(*c) == b"locked"));
    if let Some(i) = marker.filter(|i| *i > 0) {
        let c = kids[i];
        if want {
            // An explicit `(locked no)`: state the new value in place.
            let (s, e) = t.span(c);
            return vec![Splice { start: s, end: e, text: b"(locked yes)".to_vec() }];
        }
        let (_, e) = t.span(c);
        return vec![Splice { start: t.span(kids[i - 1]).1, end: e, text: vec![] }];
    }
    // KiCad's own position: before layer in tracks, before net in vias, first in
    // footprints, otherwise before the uuid. Its parser accepts any of them.
    let head = t.head(node).to_string();
    let wanted: &[&str] = match head.as_str() {
        "segment" | "arc" => &["layer", "net"],
        "via" => &["net", "tstamp", "uuid"],
        "footprint" => &["placed", "layer"],
        _ => &["uuid", "tstamp"],
    };
    kids.remove(0);
    let before = wanted.iter().find_map(|h| t.find(node, h));
    let anchor = before.or_else(|| kids.last().copied());
    let (prev_end, node_start) = match anchor {
        Some(a) => {
            let siblings: Vec<Id> = t.children(node).collect();
            let idx = siblings.iter().position(|x| *x == a).unwrap_or(0);
            let prev = if idx > 0 { t.span(siblings[idx - 1]).1 } else { t.span(node).0 + 1 };
            (prev, t.span(a).0)
        }
        None => (t.span(node).1 - 1, t.span(node).1 - 1),
    };
    let gap = &t.src[prev_end..node_start];
    let gap = if gap.is_empty() { b" ".as_slice() } else { gap };
    let mut text = gap.to_vec();
    text.extend_from_slice(b"(locked yes)");
    vec![Splice { start: prev_end, end: prev_end, text }]
}

fn norm_angle(a: f64) -> f64 {
    let mut x = a.rem_euclid(360.0);
    if x > 180.0 {
        x -= 360.0;
    }
    x
}

pub fn footprint_move(args: &Value, hook: Hook<'_>) -> Reply {
    let (path, b) = match open_for_edit(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let rev = b.revision();
    let reference = args["reference"].as_str().unwrap_or("").to_string();
    let all = b.footprints();
    let found: Vec<_> = all.iter().filter(|f| f.reference.as_deref() == Some(&reference)).collect();
    let fp = match found.as_slice() {
        [] => {
            return Reply::refused(SRC, "not_found", format!("No footprint has reference '{reference}'."))
                .target(&path)
                .revs(Some(&rev), None)
        }
        [one] => *one,
        many => {
            return Reply::refused(SRC, "ambiguous_reference", format!("{} footprints share reference '{reference}'.", many.len()))
                .target(&path)
                .revs(Some(&rev), None)
        }
    };
    if fp.locked {
        return Reply::refused(SRC, "item_locked", format!("Footprint {reference} is locked.")).target(&path).revs(Some(&rev), None);
    }
    let to = pt(&args["position"]);
    let rotation = args.get("rotation").and_then(Value::as_f64);
    let new_rot = rotation.unwrap_or(fp.rotation);
    let delta = norm_angle(new_rot - fp.rotation);
    let moved = to_nm(to.0) != to_nm(fp.at.0) || to_nm(to.1) != to_nm(fp.at.1);
    let t = &b.tree;
    if !moved && delta.abs() < 1e-9 {
        return Reply::complete(SRC, json!({"changed": false, "uuid": fp.uuid, "footprint": footprint_json(fp)}))
            .target(&path)
            .revs(Some(&rev), Some(&rev));
    }
    let Some(at_node) = t.find(fp.node, "at") else {
        return Reply::refused(SRC, "parse_error", format!("Footprint {reference} has no position.")).target(&path);
    };
    let mut splices = vec![];
    let (s, e) = t.span(at_node);
    let rot_text = if rotation.is_some() {
        let r = norm_angle(new_rot);
        if r == 0.0 {
            String::new()
        } else {
            format!(" {}", fmt_mm(r))
        }
    } else {
        t.arg(at_node, 2).map(|a| format!(" {}", t.text(a))).unwrap_or_default()
    };
    splices.push(Splice { start: s, end: e, text: format!("(at {} {}{rot_text})", fmt_mm(to.0), fmt_mm(to.1)).into_bytes() });

    for c in t.children(fp.node) {
        if t.kind(c) != Kind::List {
            continue;
        }
        match t.head(c) {
            // Pads and texts are stored relative to the footprint, but their
            // angle is absolute on screen, so a rotation must carry to them.
            "pad" | "property" | "fp_text" if delta.abs() > 1e-9 => {
                if let Some(a) = t.find(c, "at") {
                    if let (Some(x), Some(y)) = (t.arg_text(a, 0), t.arg_text(a, 1)) {
                        let old = t.arg_num(a, 2).unwrap_or(0.0);
                        let keep: Vec<String> = t.args(a).skip(3).map(|n| String::from_utf8_lossy(t.bytes(n)).to_string()).collect();
                        let mut text = format!("(at {x} {y} {}", fmt_mm((old + delta).rem_euclid(360.0)));
                        for k in keep {
                            text.push(' ');
                            text.push_str(&k);
                        }
                        text.push(')');
                        let (s, e) = t.span(a);
                        splices.push(Splice { start: s, end: e, text: text.into_bytes() });
                    }
                }
            }
            // Zones inside a footprint are stored in board coordinates.
            "zone" => zone_splices(t, c, fp.at, to, delta, &mut splices),
            _ => {}
        }
    }
    let new = apply_splices(&t.src, &splices);
    let (r2, want_rot) = (reference.clone(), new_rot);
    let verify = move |nb: &Board| -> Result<Value, String> {
        let fps = nb.footprints();
        let found: Vec<_> = fps.iter().filter(|f| f.reference.as_deref() == Some(&r2)).collect();
        let [f] = found.as_slice() else { return Err("the footprint can't be found uniquely".into()) };
        let same_angle = norm_angle(f.rotation - want_rot).abs() < 1e-6;
        if near(f.at.0, to.0) && near(f.at.1, to.1) && same_angle {
            Ok(json!({"changed": true, "uuid": f.uuid, "footprint": footprint_json(f)}))
        } else {
            Err("the footprint isn't at the requested position".into())
        }
    };
    commit(&path, &b, new, Change::Replace(top_index(&b, fp.node)), &verify, hook)
}

fn zone_splices(t: &Tree, zone: Id, from: (f64, f64), to: (f64, f64), delta: f64, out: &mut Vec<Splice>) {
    for head in ["polygon", "filled_polygon"] {
        for p in t.find_all(zone, head) {
            let Some(pts) = t.find(p, "pts") else { continue };
            for xy in t.find_all(pts, "xy") {
                if let (Some(x), Some(y)) = (t.arg_num(xy, 0), t.arg_num(xy, 1)) {
                    let (nx, ny) = crate::board::local_to_board((x - from.0, y - from.1), to, delta);
                    let (s, e) = t.span(xy);
                    out.push(Splice { start: s, end: e, text: format!("(xy {} {})", fmt_mm(nx), fmt_mm(ny)).into_bytes() });
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn digest_comparison_catches_outside_changes() {
        let a = [[1u8; 32], [2u8; 32], [3u8; 32]];
        let ins = [[1u8; 32], [9u8; 32], [2u8; 32], [3u8; 32]];
        assert!(digests_match(&a, &ins, Change::Insert(1)));
        assert!(!digests_match(&a, &ins, Change::Insert(2)));
        let del = [[1u8; 32], [3u8; 32]];
        assert!(digests_match(&a, &del, Change::Delete(1)));
        assert!(!digests_match(&a, &del, Change::Delete(0)));
        let rep = [[1u8; 32], [9u8; 32], [3u8; 32]];
        assert!(digests_match(&a, &rep, Change::Replace(1)));
        assert!(!digests_match(&a, &rep, Change::Replace(0)));
        assert!(!digests_match(&a, &rep, Change::Replace(9)));
    }

    #[test]
    fn inline_layout_matches_old_files() {
        let b =
            Board::parse(br#"(kicad_pcb (segment (start 0 0) (end 1 1) (width 1) (layer "F.Cu") (net 1) (tstamp a)))"#.to_vec()).unwrap();
        let l = Layout::from_sibling(&b.tree, b.top[0]);
        assert!(l.inline);
        assert_eq!(l.render("x", &["(a)".into()]), b" (x (a))");
    }
}
