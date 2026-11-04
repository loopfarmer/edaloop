//! The solver against answers worked out by hand, on boards simple enough to have one.
//!
//! These run the built binary on a problem file, as edaloop does.

use serde_json::{json, Value};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};

const RHO: f64 = 1.72e-8; // ohm m, the copper resistivity the solver states
const ALPHA: f64 = 0.00393; // per C

fn run(problem: &Value) -> Value {
    // Tests run in parallel, sometimes on the same problem, so each run gets its own folder.
    static RUNS: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!("boardfield_test_{}_{}", std::process::id(), RUNS.fetch_add(1, Ordering::SeqCst)));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("problem.json");
    std::fs::write(&path, problem.to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_boardfield")).arg(&path).output().expect("boardfield runs");
    assert!(out.status.success(), "boardfield failed: {}", String::from_utf8_lossy(&out.stderr));
    let results: Value = serde_json::from_slice(&out.stdout).expect("boardfield prints JSON");
    std::fs::remove_dir_all(&dir).ok();
    results[0].clone()
}

/// One straight track on F.Cu, fed at one end and loaded at the other.
fn strip(width_mm: f64, length_mm: f64, amps: f64) -> Value {
    json!({
        "bbox": [-2.0, -3.0, length_mm + 2.0, 3.0],
        "layers": [{"name": "F.Cu", "thickness_um": 35.0}, {"name": "B.Cu", "thickness_um": 35.0}],
        "dielectrics_mm": [1.5],
        "tracks": [{"layer": 0, "net": "V", "a": [0.0, 0.0], "b": [length_mm, 0.0], "w": width_mm}],
        "pads": [
            {"name": "S", "net": "V", "layers": [0], "x": 0.0, "y": 0.0, "hw": 0.25, "hh": 0.25},
            {"name": "L", "net": "V", "layers": [0], "x": length_mm, "y": 0.0, "hw": 0.25, "hh": 0.25}
        ],
        "rails": [{"net": "V", "source": "S", "loads": {"L": amps}}],
        "settings": {"dx_mm": 0.1, "levels": 4}
    })
}

#[test]
fn strip_drop_matches_rho_l_over_wt() {
    let (w, l, i) = (1.0, 10.0, 1.0);
    let o = run(&strip(w, l, i));
    assert_eq!(o["converged"], true);
    let drop_mv = o["rails"][0]["drops_mv"]["L"].as_f64().unwrap();
    let cold_mv = RHO * (l * 1e-3) / (w * 1e-3 * 35e-6) * i * 1e3;
    // Copper heats and its resistance rises, so the drop sits between cold and the hottest point's value.
    let rise = o["hotspot"]["rise_c"].as_f64().unwrap();
    let hot_mv = cold_mv * (1.0 + ALPHA * rise);
    assert!(
        drop_mv > cold_mv * 0.97 && drop_mv < hot_mv * 1.03,
        "drop {drop_mv:.3} mV, hand calculation {cold_mv:.3} mV cold to {hot_mv:.3} mV at the hotspot"
    );
}

#[test]
fn twice_the_width_gives_half_the_drop() {
    let narrow = run(&strip(1.0, 10.0, 1.0))["rails"][0]["drops_mv"]["L"].as_f64().unwrap();
    let wide = run(&strip(2.0, 10.0, 1.0))["rails"][0]["drops_mv"]["L"].as_f64().unwrap();
    let ratio = narrow / wide;
    // A little over 2: the narrow track runs hotter.
    assert!(ratio > 1.95 && ratio < 2.15, "narrow/wide drop ratio {ratio:.3}");
}

#[test]
fn heat_and_current_balance() {
    let o = run(&strip(1.0, 10.0, 1.0));
    let rail = &o["rails"][0];
    assert!(rail["balance_error_pct"].as_f64().unwrap().abs() < 0.1, "current balance {}", rail["balance_error_pct"]);
    assert!(o["energy_error_pct"].as_f64().unwrap().abs() < 1.0, "energy balance {}", o["energy_error_pct"]);
    // Joule heat in the copper is the drop times the current, and it's all the heat there is.
    let p_drop = rail["drops_mv"]["L"].as_f64().unwrap() * 1e-3 * 1.0;
    let heat = o["heat_in_w"].as_f64().unwrap();
    assert!((heat - p_drop).abs() < 0.01 * p_drop, "heat in {heat} W, I*V {p_drop} W");
}

#[test]
fn no_current_no_rise() {
    let o = run(&strip(1.0, 10.0, 0.0));
    assert!(o["hotspot"]["rise_c"].as_f64().unwrap().abs() < 1e-6, "rise {} C with no current", o["hotspot"]["rise_c"]);
}

/// The GPU solver must agree with the CPU one. It skips, with a notice, where there is no GPU, as on CI.
#[test]
fn gpu_agrees_with_cpu() {
    static RUNS: AtomicUsize = AtomicUsize::new(0);
    let dir = std::env::temp_dir().join(format!("boardfield_gpu_{}_{}", std::process::id(), RUNS.fetch_add(1, Ordering::SeqCst)));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("problem.json");
    std::fs::write(&path, strip(1.0, 10.0, 1.0).to_string()).unwrap();
    let out = Command::new(env!("CARGO_BIN_EXE_boardfield")).arg(&path).args(["--backend", "both"]).output().unwrap();
    std::fs::remove_dir_all(&dir).ok();
    if out.status.code() == Some(3) {
        println!("NOTICE: skipping, no GPU adapter: {}", String::from_utf8_lossy(&out.stderr).trim());
        return;
    }
    assert!(out.status.success(), "boardfield failed: {}", String::from_utf8_lossy(&out.stderr));
    let results: Value = serde_json::from_slice(&out.stdout).unwrap();
    let (cpu, gpu) = (&results[0], &results[1]);
    // The GPU result names its graphics API (vulkan, metal, ...), never "cpu".
    assert_eq!(cpu["backend"].as_str(), Some("cpu"));
    assert_ne!(gpu["backend"].as_str(), Some("cpu"));
    let read = |o: &Value| (o["rails"][0]["drops_mv"]["L"].as_f64().unwrap(), o["hotspot"]["rise_c"].as_f64().unwrap());
    let ((d_cpu, r_cpu), (d_gpu, r_gpu)) = (read(cpu), read(gpu));
    // The GPU sweeps in single precision, so allow 1%.
    assert!((d_gpu - d_cpu).abs() < 0.01 * d_cpu, "drop: cpu {d_cpu} mV, gpu {d_gpu} mV");
    assert!((r_gpu - r_cpu).abs() < 0.01 * r_cpu.max(0.1), "rise: cpu {r_cpu} C, gpu {r_gpu} C");
}
