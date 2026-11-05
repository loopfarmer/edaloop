//! Required tests 4 and 5, and the other board_edit behaviour.
mod common;
use boardhand::board::Board;
use boardhand::edit;
use common::*;
use serde_json::{json, Value};
use std::fs;
use std::path::Path;

fn add_args(board: &Path, rev: &str) -> Value {
    add_args_net(board, rev, "GND")
}

fn add_args_net(board: &Path, rev: &str, net: &str) -> Value {
    json!({"board": board, "expected_revision": rev, "start": {"x": 1.5, "y": 2.25}, "end": {"x": 3.123456, "y": 2.25}, "width": 0.2, "layer": "F.Cu", "net": net})
}

fn t4_on(original: &str, name: &str) {
    let dir = work(name);
    let Some(board) = copy_fixture(original, &dir) else { return };
    let orig = fs::read(&board).unwrap();
    let rev = sha(&board);
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));

    // Use a net the file really has. The large board has no net called GND.
    let nets = m.call("board_nets", json!({"board": board}));
    let net = nets["result"]["nets"][0]["name"].as_str().unwrap().to_string();
    let r = m.call("track_add", add_args_net(&board, &rev, &net));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["revision_before"], rev);
    assert_eq!(r["revision_after"], sha(&board));
    assert_ne!(r["revision_after"], rev);
    let uuid = r["result"]["uuid"].as_str().unwrap().to_string();
    assert_eq!(uuid::Uuid::parse_str(&uuid).unwrap().get_version_num(), 4);
    assert_eq!(r["result"]["item"]["net"], net.as_str());
    assert_eq!(r["result"]["item"]["end"]["x"].as_f64(), Some(3.123456));
    assert_eq!(r["result"]["item"]["locked"], false);

    // Only an insertion: the original bytes remain, in order, around one new span.
    let added = fs::read(&board).unwrap();
    let extra = added.len() - orig.len();
    let split = orig.iter().zip(&added).position(|(a, b)| a != b).unwrap();
    assert_eq!(&added[split + extra..], &orig[split..]);
    let inserted = String::from_utf8_lossy(&added[split..split + extra]).to_string();
    println!("t4: {name}: inserted {extra} bytes at {split}");
    assert!(inserted.contains(&format!("(net \"{net}\")")) && inserted.contains(&uuid));
    assert!(inserted.contains("\n\t\t(width 0.2)\n"), "the file's own layout: {inserted}");
    // A reader of the same file finds it.
    let list = m.call("board_tracks", json!({"board": board, "net": net, "layer": "F.Cu", "limit": 1000}));
    let items = list["result"]["items"].as_array().unwrap();
    assert!(items.iter().any(|i| i["uuid"] == uuid.as_str()));

    let r = m.call("item_delete", json!({"board": board, "expected_revision": r["revision_after"], "uuid": uuid}));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["uuid"], uuid.as_str());
    let after = fs::read(&board).unwrap();
    assert!(after == orig, "{name}: not byte-identical after add and delete");
    assert_eq!(r["revision_after"], rev);
    println!("t4: {name}: byte-identical after add and delete, sha {rev}");
    // No temporary files left beside it.
    let leftovers: Vec<_> = fs::read_dir(&dir).unwrap().flatten().filter(|e| e.file_name().to_string_lossy().contains(".tmp")).collect();
    assert!(leftovers.is_empty());
}

#[test]
fn t4_add_then_delete_small() {
    t4_on(small(), "t4_small");
}

#[test]
fn t4_add_then_delete_large() {
    t4_on(large(), "t4_large");
}

#[test]
fn t4_kicad_loads_a_board_with_added_items() {
    let dir = work("t4_kicad");
    let Some(board) = copy_fixture(small(), &dir) else { return };
    // Track and via counts as pcbnew reads them, before the edit and after.
    let py = "import pcbnew,sys\nb=pcbnew.LoadBoard(sys.argv[1])\nprint(len([t for t in b.GetTracks() if t.GetClass()=='PCB_VIA']), len(b.GetTracks()))";
    let counts = |out: &str| -> (usize, usize) {
        let n: Vec<usize> = out.split_whitespace().filter_map(|x| x.parse().ok()).collect();
        (n[0], n[1])
    };
    let before = pcbnew(py, &[&board]).map(|o| counts(&o));
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let r = m.call("track_add", add_args(&board, &sha(&board)));
    assert_eq!(r["outcome"], "complete");
    let v = m.call("via_add", json!({"board": board, "expected_revision": r["revision_after"], "position": {"x": 4, "y": 4}, "size": 0.6, "drill": 0.3, "net": "GND"}));
    assert_eq!(v["outcome"], "complete", "{v}");
    assert_eq!(v["result"]["item"]["layers"], json!(["F.Cu", "B.Cu"]));
    assert!(kicad_loads(&board).is_some());
    if let (Some(out), Some((vias, tracks))) = (pcbnew(py, &[&board]), before) {
        // Any board: KiCad sees exactly the one via and the one track added, as well as what was there.
        assert_eq!(counts(&out), (vias + 1, tracks + 2), "pcbnew read {out}, before the edit {vias} vias in {tracks} tracks");
        // The original fixture's own counts, which another board can't be held to.
        if std::env::var("BOARDHAND_FIXTURE_SMALL_IS_ORIGINAL").is_ok() {
            assert!(out.contains("122 142"), "{out}");
        }
    }
}

#[test]
fn t5_refusals_leave_the_file_unchanged() {
    let dir = work("t5");
    let Some(board) = copy_fixture(small(), &dir) else { return };
    let orig = fs::read(&board).unwrap();
    let rev = sha(&board);
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let same = |why: &str| assert!(fs::read(&board).unwrap() == orig, "{why} changed the file");

    // Revision conflict.
    let wrong = "0".repeat(64);
    let (r, err) = m.call_full("track_add", add_args(&board, &wrong));
    assert!(err);
    assert_eq!(r["reason"], "revision_conflict");
    assert_eq!(r["result"]["current_revision"], rev);
    assert_eq!(r["result"]["changed"], false);
    assert!(r["detail"].as_str().unwrap().contains("Nothing was changed"));
    same("revision_conflict");

    // Editor lock, both names. Lock outranks the revision check.
    let name = board.file_name().unwrap().to_string_lossy().to_string();
    let stem = board.file_stem().unwrap().to_string_lossy().to_string();
    for lock in [dir.join(format!("~{name}.lck")), dir.join(format!("~{stem}.kicad_pro.lck"))] {
        fs::write(&lock, "{}").unwrap();
        let (r, _) = m.call_full("track_add", add_args(&board, &rev));
        assert_eq!(r["reason"], "editor_lock", "{r}");
        let (r, _) = m.call_full("track_add", add_args(&board, &wrong));
        assert_eq!(r["reason"], "editor_lock");
        same("editor_lock");
        fs::remove_file(&lock).unwrap();
    }

    // Locked items. Pick them from the reply, not from a guess.
    let t = m.call("board_tracks", json!({"board": board, "limit": 1000}));
    let items = t["result"]["items"].as_array().unwrap();
    let locked_seg = items.iter().find(|i| i["locked"] == true && i["kind"] == "segment");
    let locked_via = items.iter().find(|i| i["locked"] == true && i["kind"] == "via");
    match (locked_seg, locked_via) {
        (Some(seg), Some(via)) => {
            for item in [seg, via] {
                let (r, err) = m.call_full("item_delete", json!({"board": board, "expected_revision": rev, "uuid": item["uuid"]}));
                assert!(err);
                assert_eq!(r["reason"], "item_locked", "{r}");
                same("item_locked");
            }
        }
        // The original fixture has both, so missing them there is a failure, not a skip.
        _ if std::env::var("BOARDHAND_FIXTURE_SMALL_IS_ORIGINAL").is_ok() => {
            panic!("the original fixture has a locked segment and a locked via")
        }
        // lock_then_unlock_is_byte_identical_and_locked_footprints_refuse_to_move covers item_locked on its own board.
        _ => println!("NOTICE: this fixture has no locked segment and via, so item_locked isn't checked on it here"),
    }

    // The other named refusals.
    let refuse = |m: &mut Mcp, tool: &str, args: Value, reason: &str| {
        let (r, err) = m.call_full(tool, args);
        assert!(err, "{r}");
        assert_eq!(r["outcome"], "refused", "{r}");
        assert_eq!(r["reason"], reason, "{r}");
    };
    let mut a = add_args(&board, &rev);
    a["net"] = json!("NOPE");
    refuse(&mut m, "track_add", a, "unknown_net");
    let mut a = add_args(&board, &rev);
    a["layer"] = json!("Nope.Cu");
    refuse(&mut m, "track_add", a, "unknown_layer");
    let mut a = add_args(&board, &rev);
    a["layer"] = json!("F.SilkS");
    refuse(&mut m, "track_add", a, "unknown_layer");
    refuse(
        &mut m,
        "via_add",
        json!({"board": board, "expected_revision": rev, "position": {"x": 1, "y": 1}, "size": 0.3, "drill": 0.3, "net": "GND"}),
        "invalid_argument",
    );
    refuse(
        &mut m,
        "via_add",
        json!({"board": board, "expected_revision": rev, "position": {"x": 1, "y": 1}, "size": 0.3, "drill": 0.4, "net": "GND"}),
        "invalid_argument",
    );
    refuse(
        &mut m,
        "via_add",
        json!({"board": board, "expected_revision": rev, "position": {"x": 1, "y": 1}, "size": 0.6, "drill": 0.3, "net": "NOPE"}),
        "unknown_net",
    );
    refuse(
        &mut m,
        "item_delete",
        json!({"board": board, "expected_revision": rev, "uuid": "00000000-0000-4000-8000-000000000000"}),
        "not_found",
    );
    refuse(
        &mut m,
        "item_set_locked",
        json!({"board": board, "expected_revision": rev, "uuid": "00000000-0000-4000-8000-000000000000", "locked": true}),
        "not_found",
    );
    refuse(
        &mut m,
        "footprint_move",
        json!({"board": board, "expected_revision": rev, "reference": "NOPE", "position": {"x": 1, "y": 1}}),
        "not_found",
    );
    same("the named refusals");
    assert!(m.call("board_summary", json!({"board": dir.join("absent.kicad_pcb")}))["reason"] == "file_not_found");
}

#[test]
fn lock_then_unlock_is_byte_identical_and_locked_footprints_refuse_to_move() {
    let dir = work("t5_lock");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    let orig = fs::read(&board).unwrap();
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let fp_uuid = "10000000-0000-4000-8000-000000000001";
    let seg = "20000000-0000-4000-8000-000000000002";
    let via = "20000000-0000-4000-8000-000000000003";
    for uuid in [seg, via, fp_uuid] {
        let r = m.call("item_set_locked", json!({"board": board, "expected_revision": sha(&board), "uuid": uuid, "locked": true}));
        assert_eq!(r["outcome"], "complete", "{r}");
        assert_eq!(r["result"]["locked"], true);
        assert_eq!(r["revision_after"], sha(&board));
        if uuid == fp_uuid {
            let (r, _) = m.call_full(
                "footprint_move",
                json!({"board": board, "expected_revision": sha(&board), "reference": "R1", "position": {"x": 1, "y": 1}}),
            );
            assert_eq!(r["reason"], "item_locked");
            assert!(kicad_loads(&board).is_some() || !Path::new(kicad_cli()).exists());
        }
        // Setting the same state again writes nothing.
        let before = sha(&board);
        let r = m.call("item_set_locked", json!({"board": board, "expected_revision": before, "uuid": uuid, "locked": true}));
        assert_eq!(r["result"]["changed"], false);
        assert_eq!(sha(&board), before);
        let r = m.call("item_set_locked", json!({"board": board, "expected_revision": before, "uuid": uuid, "locked": false}));
        assert_eq!(r["outcome"], "complete", "{r}");
        assert!(fs::read(&board).unwrap() == orig, "{uuid}: not restored");
    }
    // Delete refused while locked, allowed once unlocked.
    let r = m.call("item_set_locked", json!({"board": board, "expected_revision": sha(&board), "uuid": via, "locked": true}));
    assert_eq!(r["outcome"], "complete");
    assert_eq!(m.call("item_delete", json!({"board": board, "expected_revision": sha(&board), "uuid": via}))["reason"], "item_locked");
}

#[test]
fn old_format_numbered_nets_and_inline_locked_markers() {
    let dir = work("t5_numbered");
    let board = dir.join("old.kicad_pcb");
    let text = r#"(kicad_pcb (version 20221018) (generator pcbnew)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "GND")
  (net 2 "A B")
  # a comment that must survive
  (footprint "R" locked (layer "F.Cu") (tstamp ffff) (at 1 2)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS") (effects (font (size 1 1))))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "A B")))
  (segment (start 0 0) (end 1.50 0) (width 0.200) (layer "F.Cu") (net 2) (tstamp aaaa))
  (segment locked (start 0 1) (end 1 1) (width 0.2) (layer "F.Cu") (net 1) (tstamp bbbb))
  (gr_rect (start 0 0) (end 5 5) (layer "Edge.Cuts") (width 0.1) (tstamp cccc))
)
"#;
    fs::write(&board, text).unwrap();
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let t = m.call("board_tracks", json!({"board": board}));
    assert_eq!(t["result"]["items"][0]["net"], "A B");
    assert_eq!(t["result"]["items"][1]["locked"], true);
    let r = m.call("track_add", json!({"board": board, "expected_revision": sha(&board), "start": {"x": 0, "y": 2}, "end": {"x": 1, "y": 2}, "width": 0.3, "layer": "B.Cu", "net": "A B"}));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["item"]["net"], "A B");
    let uuid = r["result"]["uuid"].as_str().unwrap();
    let now = fs::read_to_string(&board).unwrap();
    assert!(now.contains(&format!("(net 2) (tstamp {uuid}))")), "the file's own form: {now}");
    assert!(now.contains("# a comment that must survive") && now.contains("(end 1.50 0) (width 0.200)"));
    assert_eq!(m.call("item_delete", json!({"board": board, "expected_revision": sha(&board), "uuid": "bbbb"}))["reason"], "item_locked");
    assert_eq!(
        m.call(
            "footprint_move",
            json!({"board": board, "expected_revision": sha(&board), "reference": "R1", "position": {"x": 1, "y": 1}})
        )["reason"],
        "item_locked"
    );
    let r = m.call("item_delete", json!({"board": board, "expected_revision": sha(&board), "uuid": uuid}));
    assert_eq!(r["outcome"], "complete");
    assert_eq!(fs::read_to_string(&board).unwrap(), text);
    let r = m.call("item_set_locked", json!({"board": board, "expected_revision": sha(&board), "uuid": "bbbb", "locked": false}));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert!(fs::read_to_string(&board).unwrap().contains("(segment (start 0 1)"));
    let r = m.call("item_set_locked", json!({"board": board, "expected_revision": sha(&board), "uuid": "bbbb", "locked": true}));
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(m.call("item_delete", json!({"board": board, "expected_revision": sha(&board), "uuid": "bbbb"}))["reason"], "item_locked");
}

#[test]
fn footprint_move_agrees_with_kicad() {
    let dir = work("t5_move");
    let Some(board) = copy_fixture(small(), &dir) else { return };
    let py_before = "import pcbnew,sys\nb=pcbnew.LoadBoard(sys.argv[1])\nf=b.FindFootprintByReference('J1')\nprint(f.GetPosition().x,f.GetPosition().y,f.GetOrientationDegrees())\nfor p in f.Pads(): print(p.GetNumber(),p.GetPosition().x,p.GetPosition().y,p.GetOrientationDegrees())\nfor z in f.Zones(): print('zone',z.Outline().BBox().GetX(),z.Outline().BBox().GetY())";
    let Some(_) = pcbnew(py_before, &[&board]) else { return };
    // KiCad's own move, on a second copy, is the reference.
    let reference = dir.join("ref.kicad_pcb");
    fs::copy(&board, &reference).unwrap();
    let py_move = "import pcbnew,sys\nb=pcbnew.LoadBoard(sys.argv[1])\nf=b.FindFootprintByReference('J1')\nf.SetOrientationDegrees(45)\nf.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20),pcbnew.FromMM(30)))\nfor p in f.Pads(): print(p.GetNumber(),p.GetPosition().x,p.GetPosition().y,round(p.GetOrientationDegrees(),3))\nprint(f.GetPosition().x,f.GetPosition().y,f.GetOrientationDegrees())";
    let want = pcbnew(py_move, &[&reference]).unwrap();

    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let r = m.call(
        "footprint_move",
        json!({"board": board, "expected_revision": sha(&board), "reference": "J1", "position": {"x": 20, "y": 30}, "rotation": 45}),
    );
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["footprint"]["rotation"].as_f64(), Some(45.0));
    assert_eq!(r["result"]["footprint"]["position"]["y"].as_f64(), Some(30.0));
    assert!(r["result"]["uuid"].is_string());
    let py_read = "import pcbnew,sys\nb=pcbnew.LoadBoard(sys.argv[1])\nf=b.FindFootprintByReference('J1')\nfor p in f.Pads(): print(p.GetNumber(),p.GetPosition().x,p.GetPosition().y,round(p.GetOrientationDegrees(),3))\nprint(f.GetPosition().x,f.GetPosition().y,f.GetOrientationDegrees())";
    let got = pcbnew(py_read, &[&board]).unwrap();
    println!("t5 move: kicad's own {want}\nours {got}");
    // Compare within a nanometre or two: KiCad rounds its rotated pad positions.
    let parse = |s: &str| -> Vec<Vec<f64>> {
        s.lines().filter(|l| !l.trim().is_empty()).map(|l| l.split_whitespace().filter_map(|x| x.parse().ok()).collect()).collect()
    };
    let (a, b) = (parse(&want), parse(&got));
    assert_eq!(a.len(), b.len());
    for (x, y) in a.iter().zip(&b) {
        for (p, q) in x.iter().zip(y) {
            assert!((p - q).abs() <= 2.0, "{want}\n{got}");
        }
    }
    assert!(kicad_loads(&board).is_some());
}

#[test]
fn footprint_zone_moves_with_its_footprint() {
    let dir = work("t5_zone");
    let Some(board) = copy_fixture(small(), &dir) else { return };
    if !Path::new(kicad_py()).exists() {
        println!("NOTICE: KiCad's Python is absent, skipping");
        return;
    }
    let py = "import pcbnew,sys\nb=pcbnew.LoadBoard(sys.argv[1])\nfor f in b.GetFootprints():\n  zs=list(f.Zones())\n  if zs:\n    z=zs[0]; bb=z.Outline().BBox()\n    print(f.GetReference(),f.GetPosition().x,f.GetPosition().y,bb.GetX(),bb.GetY(),bb.GetWidth(),bb.GetHeight())\n    break";
    let before = pcbnew(py, &[&board]).unwrap();
    let line = before.lines().rfind(|l| !l.is_empty()).unwrap_or("");
    let v: Vec<&str> = line.split_whitespace().collect();
    let refdes = v[0].to_string();
    let n = |i: usize| v[i].parse::<i64>().unwrap();
    let (fx, fy, bx, by, bw, bh) = (n(1), n(2), n(3), n(4), n(5), n(6));
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    let (tx, ty) = (fx as f64 / 1e6 + 1.0, fy as f64 / 1e6 + 2.0);
    let r = m.call(
        "footprint_move",
        json!({"board": board, "expected_revision": sha(&board), "reference": refdes, "position": {"x": tx, "y": ty}}),
    );
    assert_eq!(r["outcome"], "complete", "{r}");
    let after = pcbnew(py, &[&board]).unwrap();
    let line = after.lines().rfind(|l| !l.is_empty()).unwrap_or("");
    let w: Vec<&str> = line.split_whitespace().collect();
    let k = |i: usize| w[i].parse::<i64>().unwrap();
    assert_eq!((k(3), k(4), k(5), k(6)), (bx + 1_000_000, by + 2_000_000, bw, bh), "{before} -> {after}");
}

#[test]
fn readback_failure_reports_uncertain() {
    let dir = work("t5_readback");
    let board = dir.join("mini.kicad_pcb");
    fs::write(&board, MINI_BOARD).unwrap();
    let args = json!({"board": board, "expected_revision": sha(&board), "start": {"x": 1, "y": 1}, "end": {"x": 2, "y": 1}, "width": 0.2, "layer": "F.Cu", "net": "GND"});
    // Something else rewrites the file between the rename and the readback.
    let tamper = |p: &Path| {
        let mut b = fs::read(p).unwrap();
        b.extend_from_slice(b"\n");
        fs::write(p, b).unwrap();
    };
    let r = edit::track_add(&args, Some(&tamper));
    assert_eq!(r.outcome.as_str(), "uncertain");
    assert_eq!(r.reason.as_deref(), Some("readback_failed"));
    assert_eq!(r.revision_after.as_deref(), Some(sha(&board).as_str()));
    assert!(r.is_error());
    // Removing the file gives the same answer, with no claimed revision.
    fs::write(&board, MINI_BOARD).unwrap();
    let args2 = json!({"board": board, "expected_revision": sha(&board), "start": {"x": 1, "y": 1}, "end": {"x": 2, "y": 1}, "width": 0.2, "layer": "F.Cu", "net": "GND"});
    let remove = |p: &Path| fs::remove_file(p).unwrap();
    let r = edit::track_add(&args2, Some(&remove));
    assert_eq!(r.reason.as_deref(), Some("readback_failed"));
    assert!(r.revision_after.is_none());
}

#[test]
fn writes_keep_file_mode_and_follow_symlinks() {
    use std::os::unix::fs::PermissionsExt;
    let dir = work("t5_mode");
    let real_dir = dir.join("real");
    fs::create_dir_all(&real_dir).unwrap();
    let real = real_dir.join("mini.kicad_pcb");
    fs::write(&real, MINI_BOARD).unwrap();
    fs::set_permissions(&real, fs::Permissions::from_mode(0o640)).unwrap();
    let link = dir.join("link.kicad_pcb");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let args = json!({"board": link, "expected_revision": sha(&real), "start": {"x": 1, "y": 1}, "end": {"x": 2, "y": 1}, "width": 0.2, "layer": "F.Cu", "net": "GND"});
    let r = edit::track_add(&args, None);
    assert_eq!(r.outcome.as_str(), "complete", "{:?}", r.detail);
    assert!(fs::symlink_metadata(&link).unwrap().file_type().is_symlink());
    assert_eq!(fs::metadata(&real).unwrap().permissions().mode() & 0o777, 0o640);
    assert_eq!(Board::parse(fs::read(&real).unwrap()).unwrap().tracks().len(), 4);
}

#[test]
fn adding_to_a_board_with_no_tracks_uses_its_layout_and_net_form() {
    let dir = work("t5_no_tracks");
    let mut m = Mcp::start(&dir, &[]);
    m.initialize();
    m.load(json!("board_edit"));
    // Modern layout, tracks stripped, net known from a pad.
    let modern = dir.join("modern.kicad_pcb");
    let t = Board::parse(MINI_BOARD.as_bytes().to_vec()).unwrap();
    let mut text = MINI_BOARD.to_string();
    for tr in t.tracks().iter().rev() {
        let (a, b) = t.tree.span(tr.node);
        text.replace_range(a..b, "");
    }
    fs::write(&modern, &text).unwrap();
    let v =
        json!({"board": modern, "expected_revision": sha(&modern), "position": {"x": 1, "y": 1}, "size": 0.6, "drill": 0.3, "net": "GND"});
    let r = m.call("via_add", v);
    assert_eq!(r["outcome"], "complete", "{r}");
    let now = fs::read_to_string(&modern).unwrap();
    assert!(
        now.contains("\n\t(via\n\t\t(at 1 1)\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(layers \"F.Cu\" \"B.Cu\")\n\t\t(net \"GND\")"),
        "{now}"
    );
    assert!(now.find("(via").unwrap() < now.find("(embedded_fonts").unwrap());
    // A numbered table with no tracks and nothing after them: added at the end.
    let old = dir.join("old.kicad_pcb");
    fs::write(&old, "(kicad_pcb (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal))\n  (net 0 \"\")\n  (net 7 \"X\")\n)\n").unwrap();
    let a = json!({"board": old, "expected_revision": sha(&old), "start": {"x": 0, "y": 0}, "end": {"x": 1, "y": 0}, "width": 0.2, "layer": "F.Cu", "net": "X"});
    let r = m.call("track_add", a);
    assert_eq!(r["outcome"], "complete", "{r}");
    assert_eq!(r["result"]["item"]["net"], "X");
    assert!(fs::read_to_string(&old).unwrap().contains("(net 7)"));
}
