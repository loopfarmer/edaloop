//! From a board's copper to the two fields: current on each rail, then temperature on the whole board.

use crate::solve::{solve_cpu, Gpu, SolveInfo, Stencil};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, VecDeque};

pub const COPPER_RHO_OHM_M: f64 = 1.72e-8;
pub const COPPER_ALPHA: f64 = 0.00393;
pub const COPPER_K: f64 = 390.0;
pub const PLATING_M: f64 = 25e-6;

#[derive(Deserialize, Clone)]
pub struct Layer {
    pub name: String,
    pub thickness_um: f64,
}

#[derive(Deserialize, Clone)]
pub struct Track {
    pub layer: usize,
    pub net: String,
    pub a: [f64; 2],
    pub b: [f64; 2],
    pub w: f64,
}

#[derive(Deserialize, Clone)]
pub struct Via {
    pub net: String,
    pub x: f64,
    pub y: f64,
    pub size: f64,
    pub drill: f64,
}

#[derive(Deserialize, Clone)]
pub struct Pad {
    pub name: String,
    pub net: String,
    pub layers: Vec<usize>,
    pub x: f64,
    pub y: f64,
    pub hw: f64,
    pub hh: f64,
    #[serde(default)]
    pub drill: f64,
}

#[derive(Deserialize, Clone)]
pub struct Poly {
    pub layer: usize,
    pub net: String,
    pub pts: Vec<[f64; 2]>,
}

#[derive(Deserialize, Clone)]
pub struct Rail {
    pub net: String,
    pub source: String,
    pub loads: BTreeMap<String, f64>,
}

#[derive(Deserialize, Clone)]
pub struct Part {
    pub name: String,
    pub layer: usize,
    pub x: f64,
    pub y: f64,
    pub hw: f64,
    pub hh: f64,
    pub watts: f64,
}

#[derive(Deserialize, Clone)]
pub struct Settings {
    pub dx_mm: f64,
    #[serde(default = "d_h")]
    pub h_w_m2k: f64,
    #[serde(default = "d_kin")]
    pub k_in_plane: f64,
    #[serde(default = "d_kz")]
    pub k_through: f64,
    #[serde(default = "d_amb")]
    pub ambient_c: f64,
    #[serde(default = "d_cpl")]
    pub coupled: usize,
    #[serde(default = "d_tol")]
    pub tol: f64,
    #[serde(default = "d_lvl")]
    pub levels: usize,
    /// Thermal-only sheets inside each laminate. Without them a narrow trace's spreading resistance is lost.
    #[serde(default = "d_sub")]
    pub sublayers: usize,
    #[serde(default = "d_ttol")]
    pub thermal_tol: f64,
    /// Heat spreads more smoothly than current, so its grid can be coarser. 0 means twice dx_mm.
    #[serde(default)]
    pub thermal_dx_mm: f64,
}

impl Settings {
    pub fn thermal_dx(&self) -> f64 {
        if self.thermal_dx_mm > 0.0 {
            self.thermal_dx_mm
        } else {
            self.dx_mm * 2.0
        }
    }
}
fn d_h() -> f64 {
    10.0
}
fn d_kin() -> f64 {
    0.8
}
fn d_kz() -> f64 {
    0.35
}
fn d_amb() -> f64 {
    25.0
}
fn d_cpl() -> usize {
    2
}
fn d_tol() -> f64 {
    1e-4
}
fn d_lvl() -> usize {
    6
}
fn d_sub() -> usize {
    3
}
fn d_ttol() -> f64 {
    1e-5
}

#[derive(Deserialize, Clone)]
pub struct Problem {
    pub bbox: [f64; 4],
    pub layers: Vec<Layer>,
    pub dielectrics_mm: Vec<f64>,
    #[serde(default)]
    pub tracks: Vec<Track>,
    #[serde(default)]
    pub vias: Vec<Via>,
    #[serde(default)]
    pub pads: Vec<Pad>,
    #[serde(default)]
    pub polys: Vec<Poly>,
    #[serde(default)]
    pub rails: Vec<Rail>,
    #[serde(default)]
    pub parts: Vec<Part>,
    pub settings: Settings,
}

#[derive(Clone, Copy, PartialEq)]
pub enum Backend {
    Cpu,
    Gpu,
}

/// Coverage at 16 sample points per cell, one bit each, so a thin track keeps its true width on a coarse grid.
pub struct Mask {
    pub nx: usize,
    pub ny: usize,
    pub nz: usize,
    pub dx: f64,
    pub x0: f64,
    pub y0: f64,
    pub bits: Vec<u16>,
}

impl Mask {
    pub fn new(bbox: [f64; 4], nz: usize, dx: f64) -> Mask {
        let nx = (((bbox[2] - bbox[0]) / dx).ceil() as usize).max(1);
        let ny = (((bbox[3] - bbox[1]) / dx).ceil() as usize).max(1);
        Mask { nx, ny, nz, dx, x0: bbox[0], y0: bbox[1], bits: vec![0; nx * ny * nz] }
    }

    #[inline]
    pub fn idx(&self, i: usize, j: usize, k: usize) -> usize {
        k * self.nx * self.ny + j * self.nx + i
    }

    pub fn cell_of(&self, x: f64, y: f64) -> Option<(usize, usize)> {
        let i = ((x - self.x0) / self.dx).floor();
        let j = ((y - self.y0) / self.dx).floor();
        if i < 0.0 || j < 0.0 || i as usize >= self.nx || j as usize >= self.ny {
            return None;
        }
        Some((i as usize, j as usize))
    }

    /// Fraction of the shared east face that has copper on both sides. Current crosses a face, so a
    /// staircase along a diagonal keeps its true conductance, which averaging coverage does not.
    #[inline]
    pub fn face_e(&self, c: usize) -> f64 {
        (((self.bits[c] & 0x8888) >> 3) & (self.bits[c + 1] & 0x1111)).count_ones() as f64 / 4.0
    }

    #[inline]
    pub fn face_s(&self, c: usize) -> f64 {
        (((self.bits[c] & 0xF000) >> 12) & (self.bits[c + self.nx] & 0x000F)).count_ones() as f64 / 4.0
    }

    /// Cells a via's plated wall passes through, on one layer's plan.
    pub fn barrel_cells(&self, x: f64, y: f64, drill_mm: f64) -> Vec<(usize, usize)> {
        let r = drill_mm / 2.0 + PLATING_M * 1e3;
        let mut out = Vec::new();
        let (i0, i1) = self.range(x - r, x + r, self.x0, self.nx);
        let (j0, j1) = self.range(y - r, y + r, self.y0, self.ny);
        for j in j0..j1 {
            for i in i0..i1 {
                let cx = self.x0 + (i as f64 + 0.5) * self.dx;
                let cy = self.y0 + (j as f64 + 0.5) * self.dx;
                if (cx - x).hypot(cy - y) <= r {
                    out.push((i, j));
                }
            }
        }
        if out.is_empty() {
            if let Some(c) = self.cell_of(x, y) {
                out.push(c);
            }
        }
        out
    }

    fn range(&self, lo: f64, hi: f64, origin: f64, n: usize) -> (usize, usize) {
        let a = (((lo - origin) / self.dx).floor().max(0.0)) as usize;
        let b = (((hi - origin) / self.dx).ceil().max(0.0)) as usize;
        (a.min(n), b.min(n))
    }

    pub fn add_capsule(&mut self, k: usize, a: [f64; 2], b: [f64; 2], w: f64) {
        let r = w / 2.0;
        let (i0, i1) = self.range(a[0].min(b[0]) - r, a[0].max(b[0]) + r, self.x0, self.nx);
        let (j0, j1) = self.range(a[1].min(b[1]) - r, a[1].max(b[1]) + r, self.y0, self.ny);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let len2 = (dx * dx + dy * dy).max(1e-18);
        for j in j0..j1 {
            for i in i0..i1 {
                let mut m = 0u16;
                for s in 0..16u16 {
                    let px = self.x0 + (i as f64 + ((s & 3) as f64 + 0.5) / 4.0) * self.dx;
                    let py = self.y0 + (j as f64 + (((s >> 2) & 3) as f64 + 0.5) / 4.0) * self.dx;
                    let u = (((px - a[0]) * dx + (py - a[1]) * dy) / len2).clamp(0.0, 1.0);
                    let (qx, qy) = (a[0] + u * dx - px, a[1] + u * dy - py);
                    if qx * qx + qy * qy <= r * r {
                        m |= 1 << s;
                    }
                }
                let c = self.idx(i, j, k);
                self.bits[c] |= m;
            }
        }
    }

    pub fn add_rect(&mut self, k: usize, x: f64, y: f64, hw: f64, hh: f64) {
        let (i0, i1) = self.range(x - hw, x + hw, self.x0, self.nx);
        let (j0, j1) = self.range(y - hh, y + hh, self.y0, self.ny);
        for j in j0..j1 {
            for i in i0..i1 {
                let mut m = 0u16;
                for s in 0..16u16 {
                    let px = self.x0 + (i as f64 + ((s & 3) as f64 + 0.5) / 4.0) * self.dx;
                    let py = self.y0 + (j as f64 + (((s >> 2) & 3) as f64 + 0.5) / 4.0) * self.dx;
                    if (px - x).abs() <= hw && (py - y).abs() <= hh {
                        m |= 1 << s;
                    }
                }
                let c = self.idx(i, j, k);
                self.bits[c] |= m;
            }
        }
    }

    /// Even-odd scanline fill. KiCad's filled polygons are single outlines with keyholes for their holes.
    pub fn add_poly(&mut self, k: usize, pts: &[[f64; 2]]) {
        if pts.len() < 3 {
            return;
        }
        let ymin = pts.iter().map(|p| p[1]).fold(f64::INFINITY, f64::min);
        let ymax = pts.iter().map(|p| p[1]).fold(f64::NEG_INFINITY, f64::max);
        let (j0, j1) = self.range(ymin, ymax, self.y0, self.ny);
        let mut xs: Vec<f64> = Vec::new();
        for j in j0..j1 {
            for row in 0..4u16 {
                let y = self.y0 + (j as f64 + (row as f64 + 0.5) / 4.0) * self.dx;
                xs.clear();
                for e in 0..pts.len() {
                    let (a, b) = (pts[e], pts[(e + 1) % pts.len()]);
                    if (a[1] <= y) != (b[1] <= y) {
                        xs.push(a[0] + (y - a[1]) / (b[1] - a[1]) * (b[0] - a[0]));
                    }
                }
                xs.sort_by(|p, q| p.partial_cmp(q).unwrap());
                for &[x_in, x_out] in xs.as_chunks::<2>().0 {
                    let lo = (x_in - self.x0) / self.dx * 4.0 - 0.5;
                    let hi = (x_out - self.x0) / self.dx * 4.0 - 0.5;
                    let (s0, s1) = (lo.ceil().max(0.0) as usize, (hi.floor().max(-1.0) + 1.0) as usize);
                    for sx in s0..s1.min(self.nx * 4) {
                        let c = self.idx(sx / 4, j, k);
                        self.bits[c] |= 1 << ((row << 2) | (sx % 4) as u16);
                    }
                }
            }
        }
    }
}

fn raster(p: &Problem, net: Option<&str>, dx: f64) -> Mask {
    let mut m = Mask::new(p.bbox, p.layers.len(), dx);
    let want = |n: &str| net.is_none_or(|w| w == n);
    for t in p.tracks.iter().filter(|t| want(&t.net)) {
        m.add_capsule(t.layer, t.a, t.b, t.w);
    }
    for v in p.vias.iter().filter(|v| want(&v.net)) {
        for k in 0..p.layers.len() {
            m.add_capsule(k, [v.x, v.y], [v.x, v.y], v.size);
        }
    }
    for q in p.pads.iter().filter(|q| want(&q.net)) {
        for &k in &q.layers {
            m.add_rect(k, q.x, q.y, q.hw, q.hh);
        }
    }
    for g in p.polys.iter().filter(|g| want(&g.net)) {
        m.add_poly(g.layer, &g.pts);
    }
    m
}

fn annulus_m2(drill_mm: f64) -> f64 {
    let r = drill_mm * 0.5e-3;
    std::f64::consts::PI * ((r + PLATING_M).powi(2) - r * r)
}

/// Board thickness between two copper layers, in metres.
fn gap_m(p: &Problem, k: usize) -> f64 {
    p.dielectrics_mm.get(k).copied().unwrap_or(0.2) * 1e-3
}

#[derive(Serialize, Clone)]
pub struct LevelInfo {
    pub dx_mm: f64,
    pub cells: usize,
    pub active: usize,
    pub omega: f64,
    pub info: SolveInfo,
}

#[derive(Serialize, Clone)]
pub struct RailResult {
    pub net: String,
    pub total_a: f64,
    pub drops_mv: BTreeMap<String, f64>,
    pub unreached: Vec<String>,
    pub watts: f64,
    pub peak_a_per_mm: f64,
    pub peak_at: [f64; 2],
    pub peak_layer: String,
    pub balance_error_pct: f64,
    pub levels: Vec<LevelInfo>,
}

#[derive(Serialize, Clone)]
pub struct Hotspot {
    pub rise_c: f64,
    pub temp_c: f64,
    pub x: f64,
    pub y: f64,
    pub layer: String,
}

#[derive(Serialize, Clone)]
pub struct Timing {
    pub raster_s: f64,
    pub assemble_s: f64,
    pub solve_s: f64,
    pub total_s: f64,
    pub sweeps: u64,
    pub cell_updates: f64,
    pub mcells_per_s: f64,
    /// Time inside sweeps alone. The rest of solve_s is reading back and the 64-bit residual.
    pub sweep_s: f64,
}

#[derive(Serialize, Clone)]
pub struct Outcome {
    pub backend: String,
    pub device: String,
    pub grid: [usize; 3],
    pub dx_mm: f64,
    pub rails: Vec<RailResult>,
    pub hotspot: Hotspot,
    pub heat_in_w: f64,
    pub heat_out_w: f64,
    pub energy_error_pct: f64,
    pub thermal_levels: Vec<LevelInfo>,
    pub coupled_passes: usize,
    pub converged: bool,
    pub timing: Timing,
    #[serde(skip)]
    pub temperature: Vec<f32>,
    /// The rise above ambient, in C, at the two ends of the --png colour ramp, so a plot can carry a scale.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub png_rise_range_c: Option<[f64; 2]>,
}

struct Runner<'a> {
    backend: Backend,
    gpu: Option<&'a Gpu>,
    solve_s: f64,
    sweeps: u64,
    updates: f64,
}

impl Runner<'_> {
    fn solve(&mut self, s: &Stencil, x: &mut [f32], omega: f64, tol: f64, max_sweeps: u32) -> Result<SolveInfo, String> {
        let info = match self.backend {
            Backend::Cpu => solve_cpu(s, x, omega, tol, max_sweeps),
            Backend::Gpu => self.gpu.ok_or("no GPU")?.solve(s, x, omega, tol, max_sweeps)?,
        };
        self.solve_s += info.seconds;
        self.sweeps += info.sweeps as u64;
        let touched = if crate::solve::sparse_lists(s).is_some() { s.active() } else { s.cells() };
        self.updates += info.sweeps as f64 * touched as f64;
        Ok(info)
    }
}

/// Over-relaxation for a domain of a given length in cells: the classic optimum for a Laplace problem.
fn omega_for(length_cells: f64) -> f64 {
    (2.0 / (1.0 + (std::f64::consts::PI / length_cells.max(2.0)).sin())).min(1.998)
}

fn level_dxs(dx: f64, levels: usize, bbox: [f64; 4]) -> Vec<f64> {
    let longest = (bbox[2] - bbox[0]).max(bbox[3] - bbox[1]);
    let mut out = vec![dx];
    let mut d = dx;
    for _ in 1..levels {
        d *= 2.0;
        if longest / d < 16.0 {
            break;
        }
        out.push(d);
    }
    out.reverse();
    out
}

/// Prolong a coarse solution to a finer grid, cell for cell. SOR removes the rough edges this leaves.
fn prolong(coarse: &[f32], from: (usize, usize, usize), to: (usize, usize, usize), ratio: usize) -> Vec<f32> {
    let (cx, cy, nz) = from;
    let (fx, fy, _) = to;
    let mut out = vec![0f32; fx * fy * nz];
    for k in 0..nz {
        for j in 0..fy {
            let cj = (j / ratio).min(cy - 1);
            for i in 0..fx {
                let ci = (i / ratio).min(cx - 1);
                out[k * fx * fy + j * fx + i] = coarse[k * cx * cy + cj * cx + ci];
            }
        }
    }
    out
}

struct RailGrid {
    stencil: Stencil,
    fixed_source: bool,
    depth: usize,
    unreached: Vec<String>,
    /// load name -> its cells
    load_cells: BTreeMap<String, Vec<usize>>,
}

fn pad_cells(m: &Mask, pad: &Pad) -> Vec<usize> {
    let mut out = Vec::new();
    for &k in &pad.layers {
        let (mut any, cx, cy) = (false, pad.x, pad.y);
        let i0 = ((cx - pad.hw - m.x0) / m.dx).floor().max(0.0) as usize;
        let i1 = (((cx + pad.hw - m.x0) / m.dx).ceil().max(0.0) as usize).min(m.nx);
        let j0 = ((cy - pad.hh - m.y0) / m.dx).floor().max(0.0) as usize;
        let j1 = (((cy + pad.hh - m.y0) / m.dx).ceil().max(0.0) as usize).min(m.ny);
        for j in j0..j1 {
            for i in i0..i1 {
                let c = m.idx(i, j, k);
                if m.bits[c] != 0 {
                    out.push(c);
                    any = true;
                }
            }
        }
        if !any {
            if let Some((i, j)) = m.cell_of(cx, cy) {
                out.push(m.idx(i, j, k));
            }
        }
    }
    out
}

/// Build one rail's stencil. `temp` is the temperature rise per cell, for copper resistivity, on this grid.
fn rail_grid(p: &Problem, rail: &Rail, m: &Mask, temp: Option<&[f32]>) -> Result<RailGrid, String> {
    let (nx, ny, nz) = (m.nx, m.ny, m.nz);
    let mut s = Stencil::new(nx, ny, nz);
    let sheet = |c: usize, k: usize| -> f64 {
        let t = p.layers[k].thickness_um * 1e-6;
        let rise = temp.map_or(0.0, |x| x[c] as f64);
        t / (COPPER_RHO_OHM_M * (1.0 + COPPER_ALPHA * rise))
    };
    let plane = nx * ny;
    for k in 0..nz {
        for j in 0..ny {
            for i in 0..nx {
                let c = m.idx(i, j, k);
                if m.bits[c] == 0 {
                    continue;
                }
                let sa = sheet(c, k);
                if i + 1 < nx {
                    let face = m.face_e(c);
                    if face > 0.0 {
                        let sb = sheet(c + 1, k);
                        s.g_e[c] = (face * 2.0 * sa * sb / (sa + sb)) as f32;
                    }
                }
                if j + 1 < ny {
                    let face = m.face_s(c);
                    if face > 0.0 {
                        let sb = sheet(c + nx, k);
                        s.g_s[c] = (face * 2.0 * sa * sb / (sa + sb)) as f32;
                    }
                }
            }
        }
    }
    // Barrels join the layers. A via's drop is its plated wall, shared by the cells that wall passes through.
    let mut barrel = |x: f64, y: f64, drill: f64| {
        let cells = m.barrel_cells(x, y, drill);
        for k in 0..nz.saturating_sub(1) {
            let g = annulus_m2(drill) / (COPPER_RHO_OHM_M * gap_m(p, k)) / cells.len() as f64;
            for &(i, j) in &cells {
                let c = m.idx(i, j, k);
                s.g_u[c] += g as f32;
            }
        }
    };
    for v in p.vias.iter().filter(|v| v.net == rail.net) {
        barrel(v.x, v.y, v.drill);
    }
    for q in p.pads.iter().filter(|q| q.net == rail.net && q.drill > 0.0) {
        barrel(q.x, q.y, q.drill);
    }

    let find = |name: &str| p.pads.iter().find(|q| q.net == rail.net && q.name == name);
    let src = find(&rail.source).ok_or_else(|| format!("{}: source {} isn't a pad on this net", rail.net, rail.source))?;
    let src_cells = pad_cells(m, src);
    let mut depth = vec![u32::MAX; s.cells()];
    let mut queue = VecDeque::new();
    for &c in &src_cells {
        depth[c] = 0;
        queue.push_back(c);
    }
    let mut max_depth = 0u32;
    while let Some(c) = queue.pop_front() {
        let d = depth[c] + 1;
        let i = c % nx;
        let j = (c / nx) % ny;
        let k = c / plane;
        let mut visit = |n: usize, q: &mut VecDeque<usize>| {
            if depth[n] == u32::MAX {
                depth[n] = d;
                max_depth = max_depth.max(d);
                q.push_back(n);
            }
        };
        if i + 1 < nx && s.g_e[c] > 0.0 {
            visit(c + 1, &mut queue);
        }
        if i > 0 && s.g_e[c - 1] > 0.0 {
            visit(c - 1, &mut queue);
        }
        if j + 1 < ny && s.g_s[c] > 0.0 {
            visit(c + nx, &mut queue);
        }
        if j > 0 && s.g_s[c - nx] > 0.0 {
            visit(c - nx, &mut queue);
        }
        if k + 1 < nz && s.g_u[c] > 0.0 {
            visit(c + plane, &mut queue);
        }
        if k > 0 && s.g_u[c - plane] > 0.0 {
            visit(c - plane, &mut queue);
        }
    }
    let is_fixed: std::collections::HashSet<usize> = src_cells.iter().copied().collect();
    let mut load_cells = BTreeMap::new();
    let mut unreached = Vec::new();
    for (name, amps) in &rail.loads {
        let pad = find(name).ok_or_else(|| format!("{}: load {} isn't a pad on this net", rail.net, name))?;
        let cells: Vec<usize> = pad_cells(m, pad).into_iter().filter(|c| depth[*c] != u32::MAX && !is_fixed.contains(c)).collect();
        if cells.is_empty() {
            unreached.push(name.clone());
            continue;
        }
        for &c in &cells {
            s.rhs[c] += (*amps / cells.len() as f64) as f32;
        }
        load_cells.insert(name.clone(), cells);
    }
    // A free cell's diagonal is the sum of its links. Unreachable and source cells stay out of the solve.
    for c in 0..s.cells() {
        if depth[c] == u32::MAX || is_fixed.contains(&c) {
            s.diag[c] = 0.0;
            continue;
        }
        let i = c % nx;
        let j = (c / nx) % ny;
        let k = c / plane;
        let mut d = 0.0f64;
        if i + 1 < nx {
            d += s.g_e[c] as f64;
        }
        if i > 0 {
            d += s.g_e[c - 1] as f64;
        }
        if j + 1 < ny {
            d += s.g_s[c] as f64;
        }
        if j > 0 {
            d += s.g_s[c - nx] as f64;
        }
        if k + 1 < nz {
            d += s.g_u[c] as f64;
        }
        if k > 0 {
            d += s.g_u[c - plane] as f64;
        }
        s.diag[c] = d as f32;
    }
    Ok(RailGrid { stencil: s, fixed_source: !src_cells.is_empty(), depth: max_depth as usize, unreached, load_cells })
}

fn solve_rail(
    p: &Problem,
    rail: &Rail,
    dxs: &[f64],
    temp: Option<&[f32]>,
    r: &mut Runner,
    assemble_s: &mut f64,
    raster_s: &mut f64,
) -> Result<(RailResult, Vec<f32>, Stencil, Mask), String> {
    let tol = p.settings.tol;
    let mut x: Vec<f32> = Vec::new();
    let mut last: Option<(usize, usize, usize)> = None;
    let mut levels = Vec::new();
    let mut final_grid = None;
    for (lvl, &dx) in dxs.iter().enumerate() {
        let finest = lvl + 1 == dxs.len();
        let t = std::time::Instant::now();
        let m = raster(p, Some(&rail.net), dx);
        *raster_s += t.elapsed().as_secs_f64();
        let t = std::time::Instant::now();
        // The temperature grid is the finest one, so only the finest level sees it.
        let g = rail_grid(p, rail, &m, if finest { temp } else { None })?;
        *assemble_s += t.elapsed().as_secs_f64();
        let start = match last {
            Some(from) => prolong(&x, from, (m.nx, m.ny, m.nz), 2),
            None => vec![0f32; g.stencil.cells()],
        };
        let mut y = start;
        y.par_iter_mut().zip(g.stencil.diag.par_iter()).for_each(|(a, d)| {
            if *d <= 0.0 {
                *a = 0.0
            }
        });
        let omega = omega_for(g.depth as f64);
        // Coarse levels only supply a starting point, so they needn't be solved tightly.
        let level_tol = if finest { tol } else { tol * 10.0 };
        let info = r.solve(&g.stencil, &mut y, omega, level_tol, 400_000)?;
        levels.push(LevelInfo { dx_mm: dx, cells: g.stencil.cells(), active: g.stencil.active(), omega, info });
        x = y;
        last = Some((m.nx, m.ny, m.nz));
        if finest {
            final_grid = Some((g, m));
        }
    }
    let (g, m) = final_grid.ok_or("no levels")?;
    if !g.fixed_source {
        return Err(format!("{}: source has no cells", rail.net));
    }
    let s = &g.stencil;
    let mut drops = BTreeMap::new();
    let mut delivered = 0.0;
    for (name, cells) in &g.load_cells {
        let v: f64 = cells.iter().map(|c| x[*c] as f64).sum::<f64>() / cells.len() as f64;
        drops.insert(name.clone(), v * 1000.0);
        delivered += rail.loads[name] * v;
    }
    let (nx, plane) = (s.nx, s.nx * s.ny);
    // Dissipation per link, split between its two cells, and the sheet current density along it.
    let mut watts = 0.0f64;
    let (mut peak, mut peak_c) = (0.0f64, 0usize);
    let dx_mm = m.dx;
    for c in 0..s.cells() {
        let i = c % nx;
        let j = (c / nx) % s.ny;
        let k = c / plane;
        if i + 1 < nx && s.g_e[c] > 0.0 {
            let dv = (x[c] - x[c + 1]) as f64;
            watts += s.g_e[c] as f64 * dv * dv;
            let a = (s.g_e[c] as f64 * dv).abs() / (dx_mm * m.face_e(c).max(1e-3));
            if a > peak {
                peak = a;
                peak_c = c;
            }
        }
        if j + 1 < s.ny && s.g_s[c] > 0.0 {
            let dv = (x[c] - x[c + nx]) as f64;
            watts += s.g_s[c] as f64 * dv * dv;
            let a = (s.g_s[c] as f64 * dv).abs() / (dx_mm * m.face_s(c).max(1e-3));
            if a > peak {
                peak = a;
                peak_c = c;
            }
        }
        if k + 1 < s.nz && s.g_u[c] > 0.0 {
            let dv = (x[c] - x[c + plane]) as f64;
            watts += s.g_u[c] as f64 * dv * dv;
        }
    }
    let balance = if delivered > 0.0 { 100.0 * (watts - delivered) / delivered } else { 0.0 };
    let peak_at = [m.x0 + ((peak_c % nx) as f64 + 0.5) * m.dx, m.y0 + (((peak_c / nx) % s.ny) as f64 + 0.5) * m.dx];
    let res = RailResult {
        net: rail.net.clone(),
        total_a: rail.loads.values().sum(),
        drops_mv: drops,
        unreached: g.unreached.clone(),
        watts,
        peak_a_per_mm: peak,
        peak_at,
        peak_layer: p.layers[peak_c / plane].name.clone(),
        balance_error_pct: balance,
        levels,
    };
    Ok((res, x, g.stencil, m))
}

/// Add a rail's dissipation to the heat per cell.
fn add_heat(q: &mut [f64], s: &Stencil, x: &[f32]) {
    let (nx, plane) = (s.nx, s.nx * s.ny);
    for c in 0..s.cells() {
        let i = c % nx;
        let j = (c / nx) % s.ny;
        let k = c / plane;
        if i + 1 < nx && s.g_e[c] > 0.0 {
            let dv = (x[c] - x[c + 1]) as f64;
            let w = s.g_e[c] as f64 * dv * dv / 2.0;
            q[c] += w;
            q[c + 1] += w;
        }
        if j + 1 < s.ny && s.g_s[c] > 0.0 {
            let dv = (x[c] - x[c + nx]) as f64;
            let w = s.g_s[c] as f64 * dv * dv / 2.0;
            q[c] += w;
            q[c + nx] += w;
        }
        if k + 1 < s.nz && s.g_u[c] > 0.0 {
            let dv = (x[c] - x[c + plane]) as f64;
            let w = s.g_u[c] as f64 * dv * dv / 2.0;
            q[c] += w;
            q[c + plane] += w;
        }
    }
}

/// Thermal sheets through the board: each copper layer, plus sublayers inside each laminate.
fn thermal_nz(p: &Problem) -> usize {
    let n = p.layers.len();
    n + n.saturating_sub(1) * p.settings.sublayers
}

/// The thermal stencil: every sheet's laminate and copper conduct, and the two outer faces shed heat.
fn thermal_stencil(p: &Problem, m: &Mask, q: &[f64]) -> Stencil {
    let (nx, ny, ncu) = (m.nx, m.ny, m.nz);
    let sub = p.settings.sublayers;
    let per = sub + 1;
    let nz = thermal_nz(p);
    let mut s = Stencil::new(nx, ny, nz);
    let dx_m = m.dx * 1e-3;
    let area = dx_m * dx_m;
    let step = |t: usize| -> f64 { gap_m(p, t / per) / per as f64 }; // thickness from sheet t to t + 1
    let share = |t: usize| -> f64 {
        let below = if t + 1 < nz { step(t) / 2.0 } else { 0.0 };
        let above = if t > 0 { step(t - 1) / 2.0 } else { 0.0 };
        below + above
    };
    let sheet: Vec<f64> = (0..nz).map(|t| p.settings.k_in_plane * share(t)).collect();
    let cu: Vec<f64> = (0..ncu).map(|k| COPPER_K * p.layers[k].thickness_um * 1e-6).collect();
    for t in 0..nz {
        let copper = if t % per == 0 { Some(t / per) } else { None };
        for j in 0..ny {
            for i in 0..nx {
                let c = t * nx * ny + j * nx + i;
                let (mut e, mut so) = (sheet[t], sheet[t]);
                if let Some(k) = copper {
                    let mc = m.idx(i, j, k);
                    if i + 1 < nx {
                        e += cu[k] * m.face_e(mc);
                    }
                    if j + 1 < ny {
                        so += cu[k] * m.face_s(mc);
                    }
                    s.rhs[c] = q[mc] as f32;
                }
                if i + 1 < nx {
                    s.g_e[c] = e as f32;
                }
                if j + 1 < ny {
                    s.g_s[c] = so as f32;
                }
                if t + 1 < nz {
                    s.g_u[c] = (p.settings.k_through * area / step(t)) as f32;
                }
            }
        }
    }
    let mut barrel = |x: f64, y: f64, drill: f64| {
        let cells = m.barrel_cells(x, y, drill);
        for t in 0..nz.saturating_sub(1) {
            let g = COPPER_K * annulus_m2(drill) / step(t) / cells.len() as f64;
            for &(i, j) in &cells {
                s.g_u[t * nx * ny + j * nx + i] += g as f32;
            }
        }
    };
    for v in &p.vias {
        barrel(v.x, v.y, v.drill);
    }
    for q2 in p.pads.iter().filter(|q2| q2.drill > 0.0) {
        barrel(q2.x, q2.y, q2.drill);
    }
    let h = p.settings.h_w_m2k * area;
    let plane = nx * ny;
    for c in 0..s.cells() {
        let i = c % nx;
        let j = (c / nx) % ny;
        let t = c / plane;
        let mut d = 0.0f64;
        if i + 1 < nx {
            d += s.g_e[c] as f64;
        }
        if i > 0 {
            d += s.g_e[c - 1] as f64;
        }
        if j + 1 < ny {
            d += s.g_s[c] as f64;
        }
        if j > 0 {
            d += s.g_s[c - nx] as f64;
        }
        if t + 1 < nz {
            d += s.g_u[c] as f64;
        }
        if t > 0 {
            d += s.g_u[c - plane] as f64;
        }
        let mut sink = 0.0f64;
        if t == 0 {
            sink += h;
        }
        if t + 1 == nz {
            sink += h;
        }
        s.sink[c] = sink as f32;
        s.diag[c] = (d + sink) as f32;
    }
    s
}

fn coarsen_heat(q: &[f64], fine: (usize, usize, usize), coarse: (usize, usize)) -> Vec<f64> {
    let (fx, fy, nz) = fine;
    let (cx, cy) = coarse;
    let mut out = vec![0.0; cx * cy * nz];
    for k in 0..nz {
        for j in 0..fy {
            for i in 0..fx {
                let (ci, cj) = ((i / 2).min(cx - 1), (j / 2).min(cy - 1));
                out[k * cx * cy + cj * cx + ci] += q[k * fx * fy + j * fx + i];
            }
        }
    }
    out
}

fn solve_thermal(
    p: &Problem,
    dxs: &[f64],
    q_fine: &[f64],
    r: &mut Runner,
    assemble_s: &mut f64,
    raster_s: &mut f64,
) -> Result<(Vec<f32>, Vec<LevelInfo>, Stencil, Mask), String> {
    // Heat restricted from the current grid, level by level, so that every level sees the same watts.
    let mut heats: Vec<Vec<f64>> = vec![q_fine.to_vec()];
    let e = Mask::new(p.bbox, p.layers.len(), p.settings.dx_mm);
    let mut shape = (e.nx, e.ny, e.nz);
    let mut d = p.settings.dx_mm;
    while d < *dxs.last().unwrap() - 1e-9 {
        d *= 2.0;
        let c = Mask::new(p.bbox, p.layers.len(), d);
        let next = coarsen_heat(heats.last().unwrap(), shape, (c.nx, c.ny));
        heats = vec![next];
        shape = (c.nx, c.ny, c.nz);
    }
    for &dx in dxs.iter().rev().skip(1) {
        let c = Mask::new(p.bbox, p.layers.len(), dx);
        let next = coarsen_heat(heats.last().unwrap(), shape, (c.nx, c.ny));
        heats.push(next);
        shape = (c.nx, c.ny, c.nz);
    }
    heats.reverse();
    let mut x: Vec<f32> = Vec::new();
    let mut last = None;
    let mut levels = Vec::new();
    let mut out = None;
    for (lvl, &dx) in dxs.iter().enumerate() {
        let t = std::time::Instant::now();
        let m = raster(p, None, dx);
        *raster_s += t.elapsed().as_secs_f64();
        let t = std::time::Instant::now();
        let s = thermal_stencil(p, &m, &heats[lvl]);
        *assemble_s += t.elapsed().as_secs_f64();
        let mut y = match last {
            Some(from) => prolong(&x, from, (m.nx, m.ny, s.nz), 2),
            None => vec![0f32; s.cells()],
        };
        let omega = omega_for(m.nx.max(m.ny) as f64);
        let finest = lvl + 1 == dxs.len();
        let tol = p.settings.thermal_tol;
        let info = r.solve(&s, &mut y, omega, if finest { tol } else { tol * 10.0 }, 400_000)?;
        levels.push(LevelInfo { dx_mm: dx, cells: s.cells(), active: s.cells(), omega, info });
        x = y;
        last = Some((m.nx, m.ny, s.nz));
        if finest {
            out = Some((s, m));
        }
    }
    let (s, m) = out.ok_or("no levels")?;
    Ok((x, levels, s, m))
}

/// Current on every rail, then temperature, repeated so that hot copper's higher resistance feeds back.
pub fn run(p: &Problem, backend: Backend, gpu: Option<&Gpu>) -> Result<Outcome, String> {
    let t0 = std::time::Instant::now();
    let dxs = level_dxs(p.settings.dx_mm, p.settings.levels, p.bbox);
    let thermal_dxs = level_dxs(p.settings.thermal_dx(), p.settings.levels, p.bbox);
    let t_mask = Mask::new(p.bbox, p.layers.len(), p.settings.thermal_dx());
    let mut runner = Runner { backend, gpu, solve_s: 0.0, sweeps: 0, updates: 0.0 };
    let (mut assemble_s, mut raster_s) = (0.0, 0.0);
    let finest = Mask::new(p.bbox, p.layers.len(), p.settings.dx_mm);
    let n = finest.nx * finest.ny * finest.nz;
    let mut temp: Option<Vec<f32>> = None;
    let mut rails = Vec::new();
    let mut thermal_levels = Vec::new();
    let mut last_stencil = None;
    let mut x_t: Vec<f32> = Vec::new();
    let mut heat_in = 0.0;
    let mut coupled_passes = 0;
    for _ in 0..p.settings.coupled.max(1) {
        coupled_passes += 1;
        let mut q = vec![0f64; n];
        rails.clear();
        for rail in &p.rails {
            let (res, x, s, _m) = solve_rail(p, rail, &dxs, temp.as_deref(), &mut runner, &mut assemble_s, &mut raster_s)?;
            add_heat(&mut q, &s, &x);
            rails.push(res);
        }
        for part in &p.parts {
            let mut m = Mask::new(p.bbox, p.layers.len(), p.settings.dx_mm);
            m.add_rect(part.layer, part.x, part.y, part.hw, part.hh);
            let cells: Vec<usize> = (0..n).filter(|c| m.bits[*c] != 0).collect();
            if cells.is_empty() {
                return Err(format!("part {} is off the board", part.name));
            }
            for c in &cells {
                q[*c] += part.watts / cells.len() as f64;
            }
        }
        heat_in = q.iter().sum();
        let (x, levels, s, _m) = solve_thermal(p, &thermal_dxs, &q, &mut runner, &mut assemble_s, &mut raster_s)?;
        thermal_levels = levels;
        // Copper resistivity follows the temperature of the copper sheets, on the current grid.
        let per = p.settings.sublayers + 1;
        let tplane = t_mask.nx * t_mask.ny;
        let on_copper: Vec<f32> = (0..p.layers.len()).flat_map(|k| x[k * per * tplane..(k * per + 1) * tplane].iter().copied()).collect();
        let ratio = (p.settings.thermal_dx() / p.settings.dx_mm).round() as usize;
        let hottest = on_copper.iter().cloned().fold(0f32, f32::max) as f64;
        temp = Some(prolong(&on_copper, (t_mask.nx, t_mask.ny, t_mask.nz), (finest.nx, finest.ny, finest.nz), ratio.max(1)));
        x_t = x;
        last_stencil = Some(s);
        // Skip further passes once copper's resistance has barely moved.
        if hottest * COPPER_ALPHA < 0.01 {
            break;
        }
        continue;
    }
    let s = last_stencil.ok_or("nothing solved")?;
    let plane = s.nx * s.ny;
    let per = p.settings.sublayers + 1;
    let copper_sheet = |c: usize| (c / plane).is_multiple_of(per);
    let (mut hot, mut hot_c) = (f32::NEG_INFINITY, 0usize);
    for (c, v) in x_t.iter().enumerate() {
        if copper_sheet(c) && *v > hot {
            hot = *v;
            hot_c = c;
        }
    }
    // Heat leaves only through the two outer faces, so this is an independent check on the temperature solve.
    let h = p.settings.h_w_m2k * (p.settings.thermal_dx() * 1e-3).powi(2);
    let mut heat_out = 0.0f64;
    for j in 0..s.ny {
        for i in 0..s.nx {
            heat_out += h * x_t[j * s.nx + i] as f64;
            heat_out += h * x_t[(s.nz - 1) * plane + j * s.nx + i] as f64;
        }
    }
    let total = t0.elapsed().as_secs_f64();
    let converged =
        thermal_levels.last().is_some_and(|l| l.info.converged) && rails.iter().all(|r| r.levels.last().is_some_and(|l| l.info.converged));
    Ok(Outcome {
        backend: match backend {
            Backend::Cpu => "cpu".into(),
            Backend::Gpu => gpu.map_or("gpu".into(), |g| g.backend.clone()),
        },
        device: match backend {
            Backend::Cpu => format!("{} threads", rayon::current_num_threads()),
            Backend::Gpu => gpu.map_or(String::new(), |g| g.name.clone()),
        },
        grid: [s.nx, s.ny, p.layers.len()],
        dx_mm: p.settings.dx_mm,
        rails,
        hotspot: Hotspot {
            rise_c: hot as f64,
            temp_c: p.settings.ambient_c + hot as f64,
            x: p.bbox[0] + ((hot_c % s.nx) as f64 + 0.5) * p.settings.thermal_dx(),
            y: p.bbox[1] + (((hot_c / s.nx) % s.ny) as f64 + 0.5) * p.settings.thermal_dx(),
            layer: p.layers[hot_c / plane / per].name.clone(),
        },
        heat_in_w: heat_in,
        heat_out_w: heat_out,
        energy_error_pct: if heat_in > 0.0 { 100.0 * (heat_out - heat_in) / heat_in } else { 0.0 },
        thermal_levels,
        coupled_passes,
        converged,
        timing: Timing {
            raster_s,
            assemble_s,
            solve_s: runner.solve_s,
            total_s: total,
            sweeps: runner.sweeps,
            cell_updates: runner.updates,
            mcells_per_s: runner.updates / runner.solve_s.max(1e-9) / 1e6,
            sweep_s: crate::solve::SWEEP_NANOS.load(std::sync::atomic::Ordering::Relaxed) as f64 * 1e-9,
        },
        png_rise_range_c: None,
        temperature: (0..p.layers.len()).flat_map(|k| x_t[k * per * plane..(k * per + 1) * plane].iter().copied()).collect(),
    })
}
