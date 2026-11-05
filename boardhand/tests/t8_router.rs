//! Required test 8, and the rest of the router group.
mod common;
use common::*;
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};

fn stub_exporter(dir: &Path) -> PathBuf {
    let p = dir.join("stub exporter");
    script(&p, "printf '(pcb stub)\\n' > \"$2\"");
    p
}

struct Rig {
    dir: PathBuf,
    board: PathBuf,
    manifest: PathBuf,
    m: Mcp,
}

/// A prepared mini board: one locked segment, one free segment, one GND via.
fn rig(name: &str) -> Rig {
    let dir = work(name);
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    fs::write(dir.join("mini.kicad_pro"), "{}").unwrap();
    let exp = stub_exporter(&dir);
    let mut m = Mcp::start(&dir, &[("BOARDHAND_DSN_EXPORTER", exp.to_str().unwrap())]);
    m.initialize();
    m.load(json!("router"));
    let out = dir.join("route");
    let r = m.call("route_prepare", json!({"board": board, "output_folder": out}));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["protected_items"], 2);
    let manifest = PathBuf::from(r["result"]["manifest"].as_str().unwrap());
    assert!(Path::new(r["result"]["dsn"].as_str().unwrap()).exists());
    let doc: Value = serde_json::from_slice(&fs::read(&manifest).unwrap()).unwrap();
    assert_eq!(doc["revision"], sha(&board));
    assert_eq!(doc["protected_nets"], json!(["GND"]));
    assert!(doc["items"].as_array().unwrap().iter().all(|i| i["digest"].as_str().unwrap().len() == 64));
    Rig { dir, board, manifest, m }
}

// Hand-written sessions. Specctra's Y is the negative of KiCad's.
const SES_HEAD: &str = "(session mini.ses\n (base_design mini.dsn)\n (routes\n  (resolution um 10)\n  (parser (string_quote \") (host_cad \"KiCad's Pcbnew\"))\n  (library_out\n   (padstack \"Via[0-1]_600:300_um\" (shape (circle F.Cu 6000 0 0)) (shape (circle B.Cu 6000 0 0)) (attach off)))\n  (network_out\n";
const SES_TAIL: &str = "  )\n )\n)\n";
const LOCKED_WIRE: &str = "(wire (path F.Cu 2500 90000 -100000 50000 -100000) (type protect))";
const NEW_WIRE: &str = "(wire (path F.Cu 2500 50000 -100000 50000 -150000 70000 -150000) (type route))";
const GND_VIA: &str = "(via \"Via[0-1]_600:300_um\" 200000 -200000 (type protect))";
const NEW_VIA: &str = "(via \"Via[0-1]_600:300_um\" 100000 -220000)";

fn session(dir: &Path, name: &str, sig: &str, gnd: &str) -> PathBuf {
    let p = dir.join(name);
    fs::write(&p, format!("{SES_HEAD}(net SIG {sig})\n(net \"GND\" {gnd})\n{SES_TAIL}")).unwrap();
    p
}

#[test]
fn t8_session_omitting_a_protected_via_is_refused() {
    let mut r = rig("t8_omit_via");
    let ses = session(&r.dir, "omit.ses", &format!("{LOCKED_WIRE} {NEW_WIRE}"), NEW_VIA);
    let out = r.dir.join("out.kicad_pcb");
    let rev = sha(&r.board);
    let (a, err) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": out}));
    println!("t8 omit via: {a}");
    assert!(err);
    assert_eq!(a["outcome"], "refused");
    assert_eq!(a["reason"], "protected_copper_lost");
    let lost = a["result"]["lost"].as_array().unwrap();
    assert_eq!(lost.len(), 1);
    assert_eq!(lost[0]["uuid"], "20000000-0000-4000-8000-000000000003");
    assert_eq!(lost[0]["net"], "GND");
    assert_eq!(a["result"]["output_deleted"], true);
    assert!(!out.exists(), "the output must be deleted");
    assert_eq!(sha(&r.board), rev, "the input is never touched");
}

#[test]
fn t8_session_omitting_a_locked_segment_is_refused() {
    let mut r = rig("t8_omit_seg");
    let ses = session(&r.dir, "omit.ses", NEW_WIRE, GND_VIA);
    let out = r.dir.join("out.kicad_pcb");
    let (a, err) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": out}));
    assert!(err);
    assert_eq!(a["reason"], "protected_copper_lost");
    assert_eq!(a["result"]["lost"][0]["uuid"], "20000000-0000-4000-8000-000000000001");
    assert!(!out.exists());
}

#[test]
fn t8_session_preserving_protected_items_applies_and_kicad_loads_it() {
    let mut r = rig("t8_keep");
    let ses = session(&r.dir, "keep.ses", &format!("{LOCKED_WIRE} {NEW_WIRE}"), &format!("{GND_VIA} {NEW_VIA}"));
    let out = r.dir.join("out.kicad_pcb");
    let rev = sha(&r.board);

    // Plan first: it writes nothing, and counts per net.
    let p = r.m.call("route_plan_import", json!({"manifest": r.manifest, "session": ses}));
    println!("t8 plan: {p}");
    assert_eq!(p["outcome"], "complete");
    assert_eq!(p["result"]["per_net"]["SIG"], json!({"wires": 2, "vias": 0}));
    assert_eq!(p["result"]["per_net"]["GND"], json!({"wires": 0, "vias": 2}));
    assert_eq!(p["result"]["unknown_nets"], json!([]));
    assert_eq!(p["result"]["unknown_layers"], json!([]));
    assert_eq!(p["result"]["protected_items_omitted_by_session"], json!([]));
    assert_eq!(sha(&r.board), rev);
    assert!(!out.exists());

    let (a, err) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": out}));
    println!("t8 keep: {a}");
    assert!(!err, "{a}");
    assert_eq!(a["outcome"], "complete");
    assert_eq!(a["target"], fs::canonicalize(&out).unwrap().to_str().unwrap());
    assert_eq!(a["revision_before"], rev);
    assert_eq!(a["revision_after"], sha(&out));
    assert_eq!(a["result"]["input_unchanged"], true);
    assert_eq!(a["result"]["retained_existing"], 2);
    assert_eq!(a["result"]["added_segments"], 2);
    assert_eq!(a["result"]["added_vias"], 1);
    assert_eq!(a["result"]["removed_existing"], 1);
    assert_eq!(a["result"]["items_in_output"], 5);
    assert_eq!(sha(&r.board), rev);

    let text = fs::read_to_string(&out).unwrap();
    // Retained items keep their bytes, uuid and lock state.
    assert!(text.contains("(start 9 10)\n\t\t(end 5 10)\n\t\t(width 0.25)\n\t\t(locked yes)"));
    assert!(text.contains("20000000-0000-4000-8000-000000000001"));
    assert!(text.contains("20000000-0000-4000-8000-000000000003"));
    // The dropped free segment is gone, replaced by the routed ones, in mm.
    assert!(!text.contains("20000000-0000-4000-8000-000000000002"));
    assert!(text.contains("(start 5 10)\n\t\t(end 5 15)\n\t\t(width 0.25)"), "{text}");
    assert!(text.contains("(start 5 15)\n\t\t(end 7 15)"));
    assert!(text.contains("(at 10 22)\n\t\t(size 0.6)\n\t\t(drill 0.3)"));
    // Everything that isn't copper is unchanged.
    assert!(text.ends_with("(embedded_fonts no)\n)\n"));
    assert!(kicad_loads(&out).is_some() || !Path::new(kicad_cli()).exists());
    let py = "import pcbnew,sys\nb=pcbnew.LoadBoard(sys.argv[1])\nv=[t for t in b.GetTracks() if t.GetClass()=='PCB_VIA']\nprint(len(b.GetTracks()),len(v),sorted((t.GetPosition().x,t.GetPosition().y,t.GetNetname(),t.GetDrill()) for t in v))";
    if let Some(o) = pcbnew(py, &[&out]) {
        println!("t8 pcbnew: {o}");
        assert!(o.contains("5 2 [(10000000, 22000000, 'GND', 300000), (20000000, 20000000, 'GND', 300000)]"), "{o}");
    }

    // A second apply won't overwrite the output.
    let before = sha(&out);
    let (a, _) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": out}));
    assert_eq!(a["reason"], "output_exists");
    assert_eq!(sha(&out), before);
    // And the input is never a valid output.
    let (a, _) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": r.board}));
    assert_eq!(a["reason"], "output_is_input");
    assert_eq!(sha(&r.board), rev);
}

#[test]
fn t8_units_come_from_the_resolution_statement() {
    let mut r = rig("t8_units");
    // The same geometry in mils at 1000 per mil, against locked and GND items in mm.
    let mil = |mm: f64| (mm / 0.0254 * 1000.0).round() as i64;
    let body = format!(
        "(session m (routes (resolution mil 1000) (network_out (net SIG (wire (path F.Cu {} {} {} {} {}))) (net GND (via \"Via[0-1]_600:300_um\" {} {} ))) (library_out (padstack \"Via[0-1]_600:300_um\" (shape (circle F.Cu {} 0 0)) (shape (circle B.Cu {} 0 0))))))",
        mil(0.25), mil(9.0), -mil(10.0), mil(5.0), -mil(10.0), mil(20.0), -mil(20.0), mil(0.6), mil(0.6)
    );
    let ses = r.dir.join("mil.ses");
    fs::write(&ses, body).unwrap();
    let out = r.dir.join("out.kicad_pcb");
    let (a, err) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": out}));
    assert!(!err, "{a}");
    assert_eq!(a["result"]["retained_existing"], 2, "{a}");
    assert_eq!(a["result"]["added_segments"], 0);
}

#[test]
fn t8_plan_and_apply_refusals() {
    let mut r = rig("t8_refusals");
    let out = r.dir.join("out.kicad_pcb");
    // Unknown net and layer are reported by the plan and refused by the apply.
    let bad = r.dir.join("bad.ses");
    fs::write(
        &bad,
        format!("{SES_HEAD}(net GHOST (wire (path F.Cu 100 0 0 10 10)))\n(net SIG (wire (path Nope.Cu 100 0 0 10 10)))\n{SES_TAIL}"),
    )
    .unwrap();
    let p = r.m.call("route_plan_import", json!({"manifest": r.manifest, "session": bad}));
    assert_eq!(p["outcome"], "complete");
    assert_eq!(p["result"]["unknown_nets"], json!(["GHOST"]));
    assert_eq!(p["result"]["unknown_layers"], json!(["Nope.Cu"]));
    let a = r.m.call("route_apply", json!({"manifest": r.manifest, "session": bad, "output": out}));
    assert_eq!(a["reason"], "unknown_net");
    assert!(!out.exists());
    let badl = r.dir.join("badl.ses");
    fs::write(&badl, format!("{SES_HEAD}(net SIG (wire (path Nope.Cu 100 0 0 10 10)))\n{SES_TAIL}")).unwrap();
    assert_eq!(r.m.call("route_apply", json!({"manifest": r.manifest, "session": badl, "output": out}))["reason"], "unknown_layer");

    let garbage = r.dir.join("garbage.ses");
    fs::write(&garbage, "(session").unwrap();
    assert_eq!(r.m.call("route_plan_import", json!({"manifest": r.manifest, "session": garbage}))["reason"], "session_parse_error");
    let arc = r.dir.join("arc.ses");
    fs::write(&arc, format!("{SES_HEAD}(net SIG {LOCKED_WIRE} (wire (qarc F.Cu 100 0 0 10 10 5 5))) (net GND {GND_VIA})\n{SES_TAIL}"))
        .unwrap();
    assert_eq!(r.m.call("route_apply", json!({"manifest": r.manifest, "session": arc, "output": out}))["reason"], "unsupported_wire_shape");
    let nopad = r.dir.join("nopad.ses");
    fs::write(&nopad, format!("{SES_HEAD}(net SIG {LOCKED_WIRE}) (net GND {GND_VIA} (via \"Mystery\" 1000 -1000))\n{SES_TAIL}")).unwrap();
    assert_eq!(r.m.call("route_apply", json!({"manifest": r.manifest, "session": nopad, "output": out}))["reason"], "unknown_via_padstack");
    assert!(!out.exists());

    // A board that changed after route_prepare: refused, by both tools.
    let good = session(&r.dir, "good.ses", &format!("{LOCKED_WIRE} {NEW_WIRE}"), GND_VIA);
    let mut text = fs::read(&r.board).unwrap();
    text.extend_from_slice(b"\n");
    fs::write(&r.board, text).unwrap();
    for tool in ["route_plan_import", "route_apply"] {
        let mut args = json!({"manifest": r.manifest, "session": good});
        if tool == "route_apply" {
            args["output"] = json!(out);
        }
        let (a, err) = r.m.call_full(tool, args);
        assert!(err);
        assert_eq!(a["reason"], "revision_conflict", "{a}");
        assert_eq!(a["result"]["current_revision"], sha(&r.board));
    }
    assert!(!out.exists());
    fs::write(r.dir.join("bad.json"), "{}").unwrap();
    assert_eq!(r.m.call("route_plan_import", json!({"manifest": r.dir.join("bad.json"), "session": good}))["reason"], "invalid_manifest");
}

#[test]
fn t8_retain_protected_reinstates_what_the_session_omits() {
    let mut r = rig("t8_retain");
    let ses = session(&r.dir, "omit.ses", NEW_WIRE, NEW_VIA);
    let out = r.dir.join("out.kicad_pcb");
    let (a, err) = r.m.call_full("route_apply", json!({"manifest": r.manifest, "session": ses, "output": out, "retain_protected": true}));
    assert!(!err, "{a}");
    assert_eq!(a["result"]["reinstated_protected"], 2);
    let t = fs::read_to_string(&out).unwrap();
    assert!(t.contains("20000000-0000-4000-8000-000000000001") && t.contains("20000000-0000-4000-8000-000000000003"));
    assert!(kicad_loads(&out).is_some() || !Path::new(kicad_cli()).exists());
}

#[test]
fn route_prepare_failures() {
    let dir = work("t8_prepare");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    let out = dir.join("route");
    {
        let mut m = Mcp::start(&dir, &[]);
        m.initialize();
        m.load(json!("router"));
        let (r, err) = m.call_full("route_prepare", json!({"board": board, "output_folder": out}));
        assert!(err);
        assert_eq!(r["outcome"], "unavailable");
        assert_eq!(r["reason"], "no_exporter");
        assert!(!out.exists());
    }
    let run = |name: &str, body: &str, extra: Value| -> Value {
        let exp = dir.join(name);
        script(&exp, body);
        let mut m = Mcp::start(&dir, &[("BOARDHAND_DSN_EXPORTER", exp.to_str().unwrap())]);
        m.initialize();
        m.load(json!("router"));
        let mut a = json!({"board": board, "output_folder": out});
        a.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        let r = m.call("route_prepare", a);
        let left: Vec<_> = fs::read_dir(&out).map(|d| d.flatten().map(|e| e.file_name()).collect()).unwrap_or_default();
        assert!(left.is_empty(), "{name} left {left:?}");
        r
    };
    assert_eq!(run("e1", "exit 4", json!({}))["reason"], "exporter_failed");
    assert_eq!(run("e2", "exit 0", json!({}))["reason"], "no_dsn");
    assert_eq!(run("e3", "echo partial > \"$2\"; kill -9 $$", json!({}))["reason"], "exporter_crashed");
    assert_eq!(run("e4", "echo partial > \"$2\"; sleep 100", json!({"timeout_s": 1}))["reason"], "exporter_timeout");
    assert_eq!(run("e5", "exit 0", json!({"protected_nets": ["NOPE"]}))["reason"], "unknown_net");

    // An exporter command with its own arguments, run through the shell, sees both paths.
    let exp = dir.join("with args");
    script(&exp, "echo \"$1\" > \"$3.args\"; printf '(pcb)' > \"$3\"");
    let cmd = format!("'{}' --flag", exp.display());
    let mut m = Mcp::start(&dir, &[("BOARDHAND_DSN_EXPORTER", &cmd)]);
    m.initialize();
    m.load(json!("router"));
    // $1 is --flag, $2 the board, $3 the DSN.
    let r = m.call("route_prepare", json!({"board": board, "output_folder": dir.join("route2"), "protected_nets": []}));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["protected_items"], 1, "the locked segment, with no nets protected");
}

#[test]
fn route_run_with_a_fake_java() {
    let dir = work("t8_run");
    let bin = dir.join("bin");
    fs::create_dir_all(&bin).unwrap();
    let folder = dir.join("route");
    fs::create_dir_all(&folder).unwrap();
    fs::write(folder.join("b.dsn"), "(pcb)").unwrap();
    let jar = dir.join("fake.jar");
    fs::write(&jar, "x").unwrap();
    let path = format!("{}:/usr/bin:/bin", bin.display());
    let run = |body: &str, extra: Value| -> Value {
        script(&bin.join("java"), body);
        let mut m = Mcp::start(&dir, &[("PATH", &path)]);
        m.initialize();
        m.load(json!("router"));
        let mut a = json!({"folder": folder, "jar": jar});
        a.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        m.call("route_run", a)
    };
    let good_ses = "(session s (routes (resolution um 10) (network_out (net A (via v 0 0)))))";
    let ok_body = format!(
        "pwd > \"{d}/cwd\"; env > \"{d}/env\"; echo \"$@\" > \"{d}/args\"\nwhile [ $# -gt 0 ]; do if [ \"$1\" = \"-do\" ]; then printf '%s' '{good_ses}' > \"$2\"; fi; shift; done",
        d = dir.display()
    );
    let r = run(&ok_body, json!({"max_passes": 3}));
    println!("route_run: {r}");
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["vias"], 1);
    let ses = folder.join("b.ses");
    assert_eq!(fs::read_to_string(&ses).unwrap(), good_ses);
    assert!(fs::read_to_string(dir.join("env")).unwrap().contains("FREEROUTING__USAGE_AND_DIAGNOSTIC_DATA__DISABLE_ANALYTICS=true"));
    let cwd = fs::read_to_string(dir.join("cwd")).unwrap();
    assert_eq!(fs::canonicalize(cwd.trim()).unwrap(), fs::canonicalize(&folder).unwrap());
    let args = fs::read_to_string(dir.join("args")).unwrap();
    assert!(args.contains("-jar") && args.contains("-mp 3") && args.contains("-de"));
    fs::remove_file(&ses).unwrap();

    let marker = dir.join("alive");
    let r = run(&format!("(sleep 3; touch \"{}\") &\nsleep 100", marker.display()), json!({"timeout_s": 1}));
    assert_eq!(r["reason"], "route_timeout", "{r}");
    std::thread::sleep(std::time::Duration::from_secs(4));
    assert!(!marker.exists(), "a child outlived the kill");
    assert_eq!(run("exit 1", json!({}))["reason"], "route_failed");
    assert_eq!(run("exit 0", json!({}))["reason"], "no_session");
    assert_eq!(run("kill -9 $$", json!({}))["reason"], "route_crashed");
    let left: Vec<_> =
        fs::read_dir(&folder).unwrap().flatten().map(|e| e.file_name()).filter(|n| n.to_string_lossy().ends_with(".ses")).collect();
    assert!(left.is_empty(), "{left:?}");
    assert_eq!(run("exit 0", json!({"jar": dir.join("absent.jar")}))["reason"], "jar_not_found");
}

/// The whole path on real tools: KiCad's exporter, real Freerouting, real session.
#[test]
fn real_round_trip_with_kicad_and_freerouting() {
    let jar = Path::new(&std::env::var("HOME").unwrap_or_default()).join(".kicad-mcp/freerouting.jar");
    if !jar.exists() || !Path::new(kicad_py()).exists() {
        println!("NOTICE: the Freerouting jar or KiCad's Python is absent, skipping");
        return;
    }
    let dir = work("t8_real");
    let Some(board) = copy_fixture(small(), &dir) else { return };
    let exp = dir.join("exporter");
    script(&exp, &format!("exec '{}' -I -c \"import pcbnew,sys; sys.exit(0 if pcbnew.ExportSpecctraDSN(pcbnew.LoadBoard(sys.argv[1]),sys.argv[2]) else 1)\" \"$1\" \"$2\"", kicad_py()));
    let rev = sha(&board);
    // Java needs a writable home for its own log files. Keep it under target/.
    let home = dir.join("home");
    fs::create_dir_all(&home).unwrap();
    let mut m = Mcp::start(&dir, &[("BOARDHAND_DSN_EXPORTER", exp.to_str().unwrap()), ("HOME", home.to_str().unwrap())]);
    m.initialize();
    m.load(json!(["router", "board_read"]));
    let folder = dir.join("route");
    let p = m.call("route_prepare", json!({"board": board, "output_folder": folder}));
    println!("real prepare: {p}");
    assert_eq!(p["outcome"], "complete", "{p}");
    let manifest = p["result"]["manifest"].as_str().unwrap().to_string();
    let r = m.call("route_run", json!({"folder": folder, "jar": jar, "max_passes": 2, "timeout_s": 300}));
    println!(
        "real run: outcome={} wires={} vias={} ms={}",
        r["outcome"], r["result"]["wires"], r["result"]["vias"], r["result"]["duration_ms"]
    );
    assert_eq!(r["outcome"], "complete", "{r}");
    let ses = r["result"]["session"].as_str().unwrap().to_string();
    let plan = m.call("route_plan_import", json!({"manifest": manifest, "session": ses}));
    println!(
        "real plan: per_net={} unknown_nets={} unknown_layers={} omitted={}",
        plan["result"]["per_net"],
        plan["result"]["unknown_nets"],
        plan["result"]["unknown_layers"],
        plan["result"]["protected_items_omitted_by_session"].as_array().map(Vec::len).unwrap_or(0)
    );
    assert_eq!(plan["outcome"], "complete");
    // Units and the Y flip are right only if real items line up with the board.
    assert!(plan["result"]["items_matching_board"].as_u64().unwrap() >= 40, "{}", plan["result"]["items_matching_board"]);
    assert_eq!(plan["result"]["unknown_nets"], json!([]), "{plan}");
    assert_eq!(plan["result"]["unknown_layers"], json!([]), "aliases such as GND.plane must resolve: {plan}");

    let out = dir.join("routed.kicad_pcb");
    let (a, err) = m.call_full("route_apply", json!({"manifest": manifest, "session": ses, "output": out}));
    println!(
        "real apply: outcome={} reason={} lost={}",
        a["outcome"],
        a["reason"],
        a["result"]["lost"].as_array().map(Vec::len).unwrap_or(0)
    );
    if a["outcome"] == "refused" {
        // Freerouting drops copper that it was told is fixed. This is the case
        // the manifest exists for.
        assert!(err);
        assert_eq!(a["reason"], "protected_copper_lost");
        assert!(!out.exists());
        let (a, err) = m.call_full("route_apply", json!({"manifest": manifest, "session": ses, "output": out, "retain_protected": true}));
        println!("real apply retained: {}", a["result"]);
        assert!(!err, "{a}");
    }
    assert_eq!(sha(&board), rev);
    let report = kicad_loads(&out).expect("KiCad loads the routed board");
    println!(
        "real drc: violations={} unconnected={}",
        report["violations"].as_array().unwrap().len(),
        report["unconnected_items"].as_array().unwrap().len()
    );
    let s = m.call("board_summary", json!({"board": out}));
    assert_eq!(s["outcome"], "complete");
    let vias = s["result"]["counts"]["vias"].as_u64().unwrap();
    assert!(vias >= 121, "all 121 GND vias remain, got {vias}");
}
