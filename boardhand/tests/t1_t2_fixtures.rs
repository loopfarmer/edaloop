//! Required tests 1 and 2, plus the counts section 13 states for each fixture.
mod common;
use boardhand::board::{summary_json, Board, TrackKind};
use boardhand::sexpr;
use common::*;
use std::fs;
use std::time::Instant;

#[test]
fn t1_byte_identical_parse_and_write_on_both_boards() {
    for f in [small(), large()] {
        if !std::path::Path::new(f).exists() {
            println!("NOTICE: skipping, fixture {f} is absent");
            continue;
        }
        // Read only: this test never opens the original for writing.
        let orig = fs::read(f).unwrap();
        let tree = sexpr::parse(orig.clone()).unwrap();
        let out = tree.write_out();
        assert!(out == orig, "{f} didn't round trip");
        println!("t1: {f}: {} bytes, {} nodes, identical", orig.len(), tree.nodes.len());
    }
}

#[test]
fn t2_parse_time_on_the_large_board() {
    if !std::path::Path::new(large()).exists() {
        println!("NOTICE: skipping, fixture {} is absent", large());
        return;
    }
    let bytes = fs::read(large()).unwrap();
    let began = Instant::now();
    let b = Board::parse(bytes).unwrap();
    let elapsed = began.elapsed();
    println!("t2: parsed {} bytes in {elapsed:?} (release={})", b.tree.src.len(), !cfg!(debug_assertions));
    if cfg!(debug_assertions) {
        println!("NOTICE: debug build, the 2 s limit is asserted only under --release");
    } else {
        assert!(elapsed.as_secs_f64() < 2.0, "{elapsed:?}");
    }
}

#[test]
fn small_expected_counts() {
    // These counts describe the original private fixture, so another board opts out by leaving this unset.
    if std::env::var("BOARDHAND_FIXTURE_SMALL_IS_ORIGINAL").is_err() {
        println!("NOTICE: skipping, the expected counts belong to the original fixture");
        return;
    }
    if !std::path::Path::new(small()).exists() {
        println!("NOTICE: skipping, fixture {} is absent", small());
        return;
    }
    let b = Board::parse(fs::read(small()).unwrap()).unwrap();
    let t = b.tracks();
    let vias: Vec<_> = t.iter().filter(|x| x.kind == TrackKind::Via).collect();
    assert_eq!(vias.len(), 121);
    assert!(vias.iter().all(|v| v.net.as_deref() == Some("GND")));
    let nets = b.nets();
    assert_eq!(nets.len(), 12);
    assert!(nets.values().all(|c| *c > 0), "12 nets by pad usage");
    let s = summary_json(&b);
    assert_eq!(s["copper_layer_count"], 4);
    assert_eq!(s["counts"]["vias"], 121);
    assert_eq!(s["counts"]["footprints"], 18);
    assert_eq!(s["counts"]["segments"], 19);
    assert_eq!(s["counts"]["zones"], 4);
    assert_eq!(s["counts"]["nets"], 12);
    assert_eq!(s["layers"][1], "In1.Cu");
    println!("t: small summary {s}");
}

#[test]
fn large_expected_counts() {
    if !std::path::Path::new(large()).exists() {
        println!("NOTICE: skipping, fixture {} is absent", large());
        return;
    }
    let b = Board::parse(fs::read(large()).unwrap()).unwrap();
    let s = summary_json(&b);
    assert_eq!(s["copper_layer_count"], 8);
    assert_eq!(s["counts"]["segments"], 7056);
    assert_eq!(s["counts"]["vias"], 2229);
    assert_eq!(s["counts"]["footprints"], 690);
    assert_eq!(s["counts"]["zones"], 72);
    assert_eq!(s["counts"]["nets"], 788);
    // Its properties say "locked" and its texts say "unlocked yes". Neither is a lock.
    assert_eq!(s["counts"]["locked_items"], 10);
    assert_eq!(s["locked_by_kind"]["footprint"], 10);
    println!("t: large summary {s}");
}
