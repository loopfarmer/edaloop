//! Tool catalogue: groups, schemas and the file-based read tools.
use crate::board::{footprint_json, stackup_json, summary_json, track_json, Board};
use crate::reply::{Reply, Source};
use crate::util::sha256_hex;
use crate::validate::SHA256_PATTERN;
use serde_json::{json, Value};
use std::fs;
use std::path::Path;

pub struct Tool {
    pub name: &'static str,
    pub group: Option<&'static str>,
    pub description: &'static str,
    pub schema: Value,
    pub read_only: bool,
    /// Runs on its own thread so the server keeps answering meanwhile.
    pub long_running: bool,
}

pub const GROUPS: &[(&str, &str)] = &[
    ("board_read", "Read board structure from the file."),
    ("board_edit", "Edit a board file with safe writes."),
    ("checks", "Run DRC and ERC through kicad-cli."),
    ("router", "Autorouter round trip that protects locked copper."),
];

fn obj(required: &[&str], props: Value) -> Value {
    json!({"type": "object", "required": required, "properties": props, "additionalProperties": false})
}

fn path(desc: &str) -> Value {
    json!({"type": "string", "minLength": 1, "description": desc})
}

fn board() -> Value {
    path("Absolute path of the .kicad_pcb file.")
}

fn rev() -> Value {
    json!({"type": "string", "pattern": SHA256_PATTERN, "description": "SHA-256 of the file as you last read it."})
}

fn coord(desc: &str) -> Value {
    json!({"type": "number", "minimum": -2000, "maximum": 2000, "description": desc})
}

fn point() -> Value {
    obj(&["x", "y"], json!({"x": coord("X in mm."), "y": coord("Y in mm.")}))
}

fn length(desc: &str) -> Value {
    json!({"type": "number", "minimum": 0.000001, "maximum": 1000, "description": desc})
}

fn paging(default: u64) -> (Value, Value) {
    (
        json!({"type": "integer", "minimum": 1, "maximum": 1000, "default": default, "description": "Maximum items to return."}),
        json!({"type": "integer", "minimum": 0, "default": 0, "description": "Items to skip."}),
    )
}

fn group_names() -> Value {
    json!({"anyOf": [
        {"type": "string", "minLength": 1},
        {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
    ], "description": "One group name or an array of names."})
}

fn timeout(default: u64) -> Value {
    json!({"type": "integer", "minimum": 1, "maximum": 86400, "default": default, "description": "Timeout in seconds."})
}

pub fn catalogue() -> Vec<Tool> {
    let t = |name, group, description, schema, read_only, long_running| Tool { name, group, description, schema, read_only, long_running };
    let (lim, off) = paging(100);
    vec![
        t("groups_list", None, "List tool groups, their tools and whether each is loaded.", obj(&[], json!({})), true, false),
        t("groups_load", None, "Load tool groups, adding their tools to tools/list.", obj(&["groups"], json!({"groups": group_names()})), false, false),
        t("groups_unload", None, "Unload tool groups, removing their tools from tools/list.", obj(&["groups"], json!({"groups": group_names()})), false, false),
        t("recent_calls", None, "Recent tool calls, newest first.", obj(&[], json!({"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20, "description": "Maximum calls to return."}})), true, false),
        t("call_stats", None, "Uptime, and totals and errors per tool.", obj(&[], json!({})), true, false),
        t("server_info", None, "Version, executable, kicad-cli and pending transaction journals.", obj(&[], json!({})), true, false),

        t("board_summary", Some("board_read"), "Revision, layers, outline box and item counts.", obj(&["board"], json!({"board": board()})), true, false),
        t("board_nets", Some("board_read"), "Each net's name and pad count.", obj(&["board"], json!({"board": board()})), true, false),
        t("board_tracks", Some("board_read"), "Segments, arcs and vias, filterable by net and layer.", obj(&["board"], json!({
            "board": board(),
            "net": {"type": "string", "description": "Only this net."},
            "layer": {"type": "string", "description": "Only items on this copper layer. A via counts on every layer it spans."},
            "limit": lim, "offset": off,
        })), true, false),
        t("board_footprints", Some("board_read"), "Footprints with their position, rotation, side, lock state and pads.", obj(&["board"], json!({
            "board": board(),
            "reference": {"type": "string", "description": "Only this reference."},
            "limit": lim, "offset": off,
        })), true, false),
        t("board_stackup", Some("board_read"), "Stackup layers in order.", obj(&["board"], json!({"board": board()})), true, false),

        t("track_add", Some("board_edit"), "Add a track segment.", obj(&["board", "expected_revision", "start", "end", "width", "layer", "net"], json!({
            "board": board(), "expected_revision": rev(), "start": point(), "end": point(),
            "width": length("Width in mm."),
            "layer": {"type": "string", "minLength": 1, "description": "Copper layer name."},
            "net": {"type": "string", "minLength": 1, "description": "Net name."},
        })), false, false),
        t("via_add", Some("board_edit"), "Add a through via between the outer copper layers.", obj(&["board", "expected_revision", "position", "size", "drill", "net"], json!({
            "board": board(), "expected_revision": rev(), "position": point(),
            "size": length("Pad diameter in mm."), "drill": length("Drill diameter in mm. Must be smaller than size."),
            "net": {"type": "string", "minLength": 1, "description": "Net name."},
        })), false, false),
        t("item_delete", Some("board_edit"), "Delete an unlocked track or via by uuid.", obj(&["board", "expected_revision", "uuid"], json!({
            "board": board(), "expected_revision": rev(), "uuid": {"type": "string", "minLength": 1, "description": "The item's uuid."},
        })), false, false),
        t("item_set_locked", Some("board_edit"), "Lock or unlock an item by uuid.", obj(&["board", "expected_revision", "uuid", "locked"], json!({
            "board": board(), "expected_revision": rev(), "uuid": {"type": "string", "minLength": 1, "description": "The item's uuid."},
            "locked": {"type": "boolean", "description": "The new state."},
        })), false, false),
        t("footprint_move", Some("board_edit"), "Move an unlocked footprint, optionally rotating it. Connected tracks don't follow.", obj(&["board", "expected_revision", "reference", "position"], json!({
            "board": board(), "expected_revision": rev(),
            "reference": {"type": "string", "minLength": 1, "description": "Reference designator."},
            "position": point(),
            "rotation": {"type": "number", "minimum": -3600, "maximum": 3600, "description": "New rotation in degrees."},
        })), false, false),
        t("transaction_apply", Some("board_edit"), "Replace several files as one journalled unit.", obj(&["folder", "files"], json!({
            "folder": path("Absolute folder that holds the files and the journal."),
            "files": {"type": "array", "minItems": 1, "maxItems": 64, "items": obj(&["path", "expected_revision", "content"], json!({
                "path": path("Absolute path of an existing file in the folder."),
                "expected_revision": rev(),
                "content": {"type": "string", "description": "The file's complete new text."},
            }))},
        })), false, false),

        t("drc_run", Some("checks"), "Run KiCad's DRC. Findings are a completed check, not an error.", obj(&["board"], json!({
            "board": board(), "timeout_s": timeout(900),
            "allow_default_rules": {"type": "boolean", "default": false, "description": "Run even with no .kicad_pro, on KiCad's default rules."},
        })), true, true),
        t("erc_run", Some("checks"), "Run KiCad's ERC on a schematic.", obj(&["schematic"], json!({
            "schematic": path("Absolute path of the .kicad_sch file."), "timeout_s": timeout(900),
            "allow_default_rules": {"type": "boolean", "default": false, "description": "Run even with no .kicad_pro, on KiCad's default rules."},
        })), true, true),

        t("route_prepare", Some("router"), "Write a manifest of protected copper and export a DSN through BOARDHAND_DSN_EXPORTER.", obj(&["board", "output_folder"], json!({
            "board": board(), "output_folder": path("Absolute folder for the manifest and the DSN."),
            "protected_nets": {"type": "array", "items": {"type": "string", "minLength": 1}, "default": ["GND"], "description": "Nets whose vias are protected."},
            "timeout_s": timeout(300),
        })), false, true),
        t("route_run", Some("router"), "Run Freerouting on the DSN in a folder.", obj(&["folder", "jar"], json!({
            "folder": path("Absolute folder holding the DSN."), "jar": path("Absolute path of the Freerouting jar."),
            "dsn": path("Absolute path of the DSN, when the folder holds several."),
            "max_passes": {"type": "integer", "minimum": 1, "maximum": 10000, "description": "Upper limit of routing passes."},
            "timeout_s": timeout(3600),
        })), false, true),
        t("route_plan_import", Some("router"), "Report what a session file holds. Writes nothing.", obj(&["manifest", "session"], json!({
            "manifest": path("Absolute path of the manifest."), "session": path("Absolute path of the .ses file."),
            "board": path("Absolute board path, if it differs from the manifest's."),
        })), true, false),
        t("route_apply", Some("router"), "Write the session's routes to a new board file, replacing the input's tracks and vias. Refuses if protected copper would be lost.", obj(&["manifest", "session", "output"], json!({
            "manifest": path("Absolute path of the manifest."), "session": path("Absolute path of the .ses file."),
            "output": path("Absolute path of the new board. It must not exist."),
            "board": path("Absolute board path, if it differs from the manifest's."),
            "retain_protected": {"type": "boolean", "default": false, "description": "Keep protected items the session omits, rather than refusing."},
        })), false, false),
    ]
}

/// Read a board for the read tools. Hash and parse come from the same bytes.
pub fn read_board(args: &Value) -> Result<(String, Board, String), Reply> {
    let raw = args["board"].as_str().unwrap_or("");
    if !Path::new(raw).is_absolute() {
        return Err(crate::edit::not_absolute("board", raw));
    }
    let bytes = fs::read(raw).map_err(|e| Reply::refused(Source::File, "file_not_found", format!("{raw}: {e}.")).target(raw))?;
    let rev = sha256_hex(&bytes);
    let b = Board::parse(bytes)
        .map_err(|e| Reply::refused(Source::File, "parse_error", format!("{raw}: {e}.")).target(raw).revs(Some(&rev), Some(&rev)))?;
    Ok((raw.to_string(), b, rev))
}

fn usize_arg(args: &Value, field: &str, default: usize) -> usize {
    args.get(field).and_then(Value::as_f64).map(|f| f as usize).unwrap_or(default)
}

pub fn board_read(tool: &str, args: &Value) -> Reply {
    let (raw, b, rev) = match read_board(args) {
        Ok(v) => v,
        Err(r) => return r,
    };
    let src = Source::File;
    let r = match tool {
        "board_summary" => {
            let s = summary_json(&b);
            if s["outline_bbox"].is_null() {
                Reply::partial(src, "no_outline", "The board has nothing on Edge.Cuts, so it has no outline box.", s)
            } else {
                Reply::complete(src, s)
            }
        }
        "board_nets" => {
            let nets: Vec<Value> = b.nets().into_iter().map(|(name, count)| json!({"name": name, "pad_count": count})).collect();
            Reply::complete(src, json!({"count": nets.len(), "nets": nets}))
        }
        "board_tracks" => {
            let net = args.get("net").and_then(Value::as_str);
            if let Some(n) = net {
                if !b.known_nets().contains(n) {
                    return Reply::refused(src, "unknown_net", format!("The board has no net named '{n}'."))
                        .target(&raw)
                        .revs(Some(&rev), Some(&rev));
                }
            }
            let mut layer = None;
            if let Some(l) = args.get("layer").and_then(Value::as_str) {
                match b.resolve_copper(l) {
                    Some(c) => layer = Some(c),
                    None => {
                        return Reply::refused(src, "unknown_layer", format!("'{l}' isn't a copper layer of this board."))
                            .target(&raw)
                            .revs(Some(&rev), Some(&rev))
                    }
                }
            }
            let matching: Vec<_> = b
                .tracks()
                .into_iter()
                .filter(|t| net.is_none_or(|n| t.net.as_deref() == Some(n)))
                .filter(|t| layer.as_deref().is_none_or(|l| b.track_on_layer(t, l)))
                .collect();
            let (limit, offset) = (usize_arg(args, "limit", 100), usize_arg(args, "offset", 0));
            let items: Vec<Value> = matching.iter().skip(offset).take(limit).map(track_json).collect();
            Reply::complete(src, json!({"total": matching.len(), "offset": offset, "returned": items.len(), "items": items}))
        }
        "board_footprints" => {
            let reference = args.get("reference").and_then(Value::as_str);
            let matching: Vec<_> =
                b.footprints().into_iter().filter(|f| reference.is_none_or(|r| f.reference.as_deref() == Some(r))).collect();
            if let (Some(r), true) = (reference, matching.is_empty()) {
                return Reply::refused(src, "not_found", format!("No footprint has reference '{r}'."))
                    .target(&raw)
                    .revs(Some(&rev), Some(&rev));
            }
            let (limit, offset) = (usize_arg(args, "limit", 100), usize_arg(args, "offset", 0));
            let items: Vec<Value> = matching.iter().skip(offset).take(limit).map(footprint_json).collect();
            Reply::complete(src, json!({"total": matching.len(), "offset": offset, "returned": items.len(), "items": items}))
        }
        "board_stackup" => match stackup_json(&b) {
            Some(layers) => Reply::complete(src, json!({"layers": layers})),
            None => Reply::partial(src, "no_stackup", "The file has no stackup section.", json!({"layers": []})),
        },
        other => Reply::refused(src, "unknown_tool", format!("No read tool '{other}'.")),
    };
    r.target(&raw).revs(Some(&rev), Some(&rev))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::validate::KEYWORDS;

    fn walk(name: &str, schema: &Value) {
        let Some(o) = schema.as_object() else { return };
        for (k, v) in o {
            assert!(KEYWORDS.contains(&k.as_str()), "{name} uses unsupported keyword {k}");
            match k.as_str() {
                "properties" => v.as_object().unwrap().values().for_each(|p| walk(name, p)),
                "items" => walk(name, v),
                "anyOf" => v.as_array().unwrap().iter().for_each(|p| walk(name, p)),
                _ => {}
            }
        }
    }

    #[test]
    fn schemas_use_only_validated_keywords_and_are_closed() {
        for t in catalogue() {
            walk(t.name, &t.schema);
            assert_eq!(t.schema["additionalProperties"], false, "{}", t.name);
            assert_eq!(t.schema["type"], "object");
        }
    }

    #[test]
    fn tool_names_are_unique_and_groups_exist() {
        let c = catalogue();
        let mut names: Vec<_> = c.iter().map(|t| t.name).collect();
        names.sort_unstable();
        names.dedup();
        assert_eq!(names.len(), c.len());
        for t in &c {
            if let Some(g) = t.group {
                assert!(GROUPS.iter().any(|(n, _)| *n == g));
            }
        }
    }
}
