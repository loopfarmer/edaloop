//! boardfield: current and temperature on a board's own copper, on the GPU or the CPU.
//!
//!     boardfield problem.json [--backend gpu|cpu|both] [-o result.json] [--png heat.png]
//!
//! The result is JSON. Exit status is 0 if every solve converged, 1 if one didn't, 2 on bad input.

// Grid loops index several arrays by the same cell number, which reads better than zipped iterators.
#![allow(clippy::needless_range_loop)]

mod model;
mod solve;

use model::{run, Backend, Outcome, Problem};

/// The hottest layer at each cell, coolest cell dark and hottest bright. The field is the rise above ambient, and so is the range returned.
fn heat_png(path: &str, o: &Outcome) -> Result<[f64; 2], String> {
    let [nx, ny, nz] = o.grid;
    let mut hottest = vec![0f32; nx * ny];
    for k in 0..nz {
        for c in 0..nx * ny {
            hottest[c] = hottest[c].max(o.temperature[k * nx * ny + c]);
        }
    }
    // From the coolest cell to the hottest, not from 0 C: a board at 25 to 57 C otherwise uses only the top of the ramp.
    let top = hottest.iter().cloned().fold(f32::MIN, f32::max);
    let low = hottest.iter().cloned().fold(f32::MAX, f32::min);
    let span = (top - low).max(1e-6);
    let mut rgb = Vec::with_capacity(nx * ny * 3);
    for v in &hottest {
        // A dark-to-hot ramp, in equal steps of temperature.
        let t = ((*v - low) / span).clamp(0.0, 1.0);
        let r = (t * 2.2).min(1.0);
        let g = ((t - 0.4) * 2.0).clamp(0.0, 1.0);
        let b = ((t - 0.75) * 4.0).clamp(0.0, 1.0);
        rgb.extend_from_slice(&[(r * 255.0) as u8, (g * 255.0) as u8, (20.0 + b * 235.0) as u8]);
    }
    let file = std::fs::File::create(path).map_err(|e| e.to_string())?;
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), nx as u32, ny as u32);
    enc.set_color(png::ColorType::Rgb);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header().and_then(|mut w| w.write_image_data(&rgb)).map_err(|e| e.to_string())?;
    Ok([low as f64, top as f64])
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let opt = |flag: &str| args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).cloned();
    if args.len() < 2 || args[1] == "--help" {
        eprintln!("usage: boardfield problem.json [--backend gpu|cpu|both] [-o result.json] [--png heat.png]");
        std::process::exit(2);
    }
    let text = std::fs::read_to_string(&args[1]).unwrap_or_else(|e| {
        eprintln!("boardfield: {}: {e}", args[1]);
        std::process::exit(2)
    });
    let problem: Problem = serde_json::from_str(&text).unwrap_or_else(|e| {
        eprintln!("boardfield: bad problem file: {e}");
        std::process::exit(2)
    });
    let which = opt("--backend").unwrap_or_else(|| "cpu".into());
    let mut results = Vec::new();
    if which == "cpu" || which == "both" {
        match run(&problem, Backend::Cpu, None) {
            Ok(o) => results.push(o),
            Err(e) => {
                eprintln!("boardfield: {e}");
                std::process::exit(2)
            }
        }
    }
    if which == "gpu" || which == "both" {
        let gpu = solve::Gpu::new().unwrap_or_else(|e| {
            eprintln!("boardfield: {e}");
            std::process::exit(3)
        });
        match run(&problem, Backend::Gpu, Some(&gpu)) {
            Ok(o) => results.push(o),
            Err(e) => {
                eprintln!("boardfield: {e}");
                std::process::exit(2)
            }
        }
    }
    let converged = results.iter().all(|r| r.converged);
    if let (Some(path), Some(last)) = (opt("--png"), results.last_mut()) {
        match heat_png(&path, last) {
            Ok(range) => last.png_rise_range_c = Some(range),
            Err(e) => eprintln!("boardfield: png: {e}"),
        }
    }
    let json = serde_json::to_string_pretty(&results).expect("results serialise");
    match opt("-o") {
        Some(path) => std::fs::write(&path, json).unwrap_or_else(|e| eprintln!("boardfield: {path}: {e}")),
        None => println!("{json}"),
    }
    std::process::exit(if converged { 0 } else { 1 });
}
