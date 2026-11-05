//! The linear solve: A x = b on a layered grid, by red-black SOR.
//!
//! Current and heat have the same shape: each cell couples to its in-plane neighbours and to the layers
//! above and below. So one stencil serves both, and the GPU and CPU run the same update, in the same order.

use bytemuck::{Pod, Zeroable};
use rayon::prelude::*;

/// A stencil. A cell with diag == 0 is fixed at its current value (inactive, or held by a boundary).
#[derive(Clone)]
pub struct Stencil {
    pub nx: usize,
    pub ny: usize,
    pub nz: usize,
    pub g_e: Vec<f32>,
    pub g_s: Vec<f32>,
    pub g_u: Vec<f32>,
    pub diag: Vec<f32>,
    /// Conductance to the reference, such as convection to ambient. Kept apart from `diag`, see `residual_into`.
    pub sink: Vec<f32>,
    pub rhs: Vec<f32>,
}

impl Stencil {
    pub fn new(nx: usize, ny: usize, nz: usize) -> Self {
        let n = nx * ny * nz;
        Stencil {
            nx,
            ny,
            nz,
            g_e: vec![0.0; n],
            g_s: vec![0.0; n],
            g_u: vec![0.0; n],
            diag: vec![0.0; n],
            sink: vec![0.0; n],
            rhs: vec![0.0; n],
        }
    }

    pub fn cells(&self) -> usize {
        self.nx * self.ny * self.nz
    }

    pub fn active(&self) -> usize {
        self.diag.par_iter().filter(|d| **d > 0.0).count()
    }

    /// r = b - A x in double precision, into `r`. Returns ||r|| / ||b||, over free cells.
    pub fn residual_into(&self, x: &[f64], r: &mut [f32]) -> f64 {
        let (nx, ny) = (self.nx, self.ny);
        let plane = nx * ny;
        let (num, den) = r
            .par_iter_mut()
            .enumerate()
            .fold(
                || (0.0f64, 0.0f64),
                |(a, b), (c, out)| {
                    let d = self.diag[c];
                    if d <= 0.0 {
                        *out = 0.0;
                        return (a, b);
                    }
                    // Written as differences, never through `diag`. A 32-bit diagonal differs from the sum of
                    // its links by rounding, which acts as a false leak in every cell. On uniform copper the
                    // rounding has one sign, and it added up to a 1.5% error in a rail's drop.
                    let here = x[c];
                    let mut s = self.rhs[c] as f64 - self.sink[c] as f64 * here;
                    let i = c % nx;
                    let j = (c / nx) % ny;
                    let k = c / plane;
                    if i + 1 < nx {
                        s += self.g_e[c] as f64 * (x[c + 1] - here);
                    }
                    if i > 0 {
                        s += self.g_e[c - 1] as f64 * (x[c - 1] - here);
                    }
                    if j + 1 < ny {
                        s += self.g_s[c] as f64 * (x[c + nx] - here);
                    }
                    if j > 0 {
                        s += self.g_s[c - nx] as f64 * (x[c - nx] - here);
                    }
                    if k + 1 < self.nz {
                        s += self.g_u[c] as f64 * (x[c + plane] - here);
                    }
                    if k > 0 {
                        s += self.g_u[c - plane] as f64 * (x[c - plane] - here);
                    }
                    *out = s as f32;
                    (a + s * s, b + (self.rhs[c] as f64).powi(2))
                },
            )
            .reduce(|| (0.0, 0.0), |p, q| (p.0 + q.0, p.1 + q.1));
        if den == 0.0 {
            return if num == 0.0 { 0.0 } else { f64::INFINITY };
        }
        (num / den).sqrt()
    }
}

#[derive(Clone, Copy, Debug, serde::Serialize)]
pub struct SolveInfo {
    pub sweeps: u32,
    pub residual: f64,
    pub seconds: f64,
    pub converged: bool,
}

/// Number of sweeps between residual checks. Reading back is the slow part on the GPU.
pub const CHECK_EVERY: u32 = 100;

/// Sweeps between checks, overridable to measure the cost of reading back.
pub fn check_every() -> u32 {
    std::env::var("BOARDFIELD_CHECK_EVERY").ok().and_then(|v| v.parse().ok()).unwrap_or(CHECK_EVERY)
}

/// Reporting only: time spent on sweeps, against reading back and computing the residual.
pub static SWEEP_NANOS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

struct Shared<T>(*mut T);
unsafe impl<T> Send for Shared<T> {}
unsafe impl<T> Sync for Shared<T> {}
impl<T> Shared<T> {
    #[inline(always)]
    fn ptr(&self) -> *mut T {
        self.0
    }
}

/// One red-black half sweep over one colour. It solves for the correction, so its right side is the residual.
fn half_sweep(s: &Stencil, rhs: &[f32], x: &mut [f32], color: usize, omega: f32) {
    let (nx, ny, nz) = (s.nx, s.ny, s.nz);
    let plane = nx * ny;
    let base = Shared(x.as_mut_ptr());
    let one_minus = 1.0 - omega;
    (0..ny * nz).into_par_iter().for_each(|row| {
        let j = row % ny;
        let k = row / ny;
        let start = (j + k + color) & 1;
        let p = base.ptr();
        let mut i = start;
        while i < nx {
            let c = k * plane + j * nx + i;
            let d = s.diag[c];
            if d > 0.0 {
                // Safety: cells of one colour only read cells of the other, so no write is read or repeated.
                unsafe {
                    let mut sum = rhs[c];
                    if i + 1 < nx {
                        sum += s.g_e[c] * *p.add(c + 1);
                    }
                    if i > 0 {
                        sum += s.g_e[c - 1] * *p.add(c - 1);
                    }
                    if j + 1 < ny {
                        sum += s.g_s[c] * *p.add(c + nx);
                    }
                    if j > 0 {
                        sum += s.g_s[c - nx] * *p.add(c - nx);
                    }
                    if k + 1 < nz {
                        sum += s.g_u[c] * *p.add(c + plane);
                    }
                    if k > 0 {
                        sum += s.g_u[c - plane] * *p.add(c - plane);
                    }
                    *p.add(c) = one_minus * *p.add(c) + omega * (sum / d);
                }
            }
            i += 2;
        }
    });
}

/// Above this share of free cells, sweeping the whole grid is cheaper than walking a list.
const SPARSE_BELOW: f64 = 0.25;

/// Free cells by colour, when few enough to be worth a list. A rail is copper on a mostly empty grid.
pub fn sparse_lists(s: &Stencil) -> Option<(Vec<u32>, Vec<u32>)> {
    if (s.active() as f64) >= SPARSE_BELOW * s.cells() as f64 {
        return None;
    }
    let (nx, plane) = (s.nx, s.nx * s.ny);
    let (mut a, mut b) = (Vec::new(), Vec::new());
    for c in 0..s.cells() {
        if s.diag[c] > 0.0 {
            let colour = (c % nx + (c / nx) % s.ny + c / plane) & 1;
            if colour == 0 {
                a.push(c as u32)
            } else {
                b.push(c as u32)
            }
        }
    }
    Some((a, b))
}

fn sparse_sweep(s: &Stencil, rhs: &[f32], x: &mut [f32], list: &[u32], omega: f32) {
    let (nx, ny, nz) = (s.nx, s.ny, s.nz);
    let plane = nx * ny;
    let base = Shared(x.as_mut_ptr());
    list.par_iter().for_each(|&c32| {
        let c = c32 as usize;
        let p = base.ptr();
        let (i, j, k) = (c % nx, (c / nx) % ny, c / plane);
        // Safety: as in half_sweep, one colour only reads the other.
        unsafe {
            let mut sum = rhs[c];
            if i + 1 < nx {
                sum += s.g_e[c] * *p.add(c + 1);
            }
            if i > 0 {
                sum += s.g_e[c - 1] * *p.add(c - 1);
            }
            if j + 1 < ny {
                sum += s.g_s[c] * *p.add(c + nx);
            }
            if j > 0 {
                sum += s.g_s[c - nx] * *p.add(c - nx);
            }
            if k + 1 < nz {
                sum += s.g_u[c] * *p.add(c + plane);
            }
            if k > 0 {
                sum += s.g_u[c - plane] * *p.add(c - plane);
            }
            *p.add(c) = (1.0 - omega) * *p.add(c) + omega * (sum / s.diag[c]);
        }
    });
}

/// Mixed-precision refinement: the residual is exact in 64 bits, and single precision only finds the correction.
/// Single precision alone stalls near 1e-3, because large conductances cancel.
pub fn solve_cpu(s: &Stencil, x: &mut [f32], omega: f64, tol: f64, max_sweeps: u32) -> SolveInfo {
    let t0 = std::time::Instant::now();
    let lists = sparse_lists(s);
    let mut x64: Vec<f64> = x.iter().map(|v| *v as f64).collect();
    let mut r = vec![0f32; x.len()];
    let mut delta = vec![0f32; x.len()];
    let mut sweeps = 0;
    let mut residual = s.residual_into(&x64, &mut r);
    while residual >= tol && sweeps < max_sweeps {
        delta.par_iter_mut().for_each(|d| *d = 0.0);
        let tick = std::time::Instant::now();
        for _ in 0..check_every() {
            match &lists {
                Some((a, b)) => {
                    sparse_sweep(s, &r, &mut delta, a, omega as f32);
                    sparse_sweep(s, &r, &mut delta, b, omega as f32);
                }
                None => {
                    half_sweep(s, &r, &mut delta, 0, omega as f32);
                    half_sweep(s, &r, &mut delta, 1, omega as f32);
                }
            }
        }
        SWEEP_NANOS.fetch_add(tick.elapsed().as_nanos() as u64, std::sync::atomic::Ordering::Relaxed);
        sweeps += check_every();
        x64.par_iter_mut().zip(delta.par_iter()).for_each(|(a, d)| *a += *d as f64);
        residual = s.residual_into(&x64, &mut r);
    }
    x.par_iter_mut().zip(x64.par_iter()).for_each(|(a, b)| *a = *b as f32);
    SolveInfo { sweeps, residual, seconds: t0.elapsed().as_secs_f64(), converged: residual < tol }
}

#[repr(C)]
#[derive(Clone, Copy, Pod, Zeroable)]
struct Params {
    nx: u32,
    ny: u32,
    nz: u32,
    color: u32,
    omega: f32,
    _pad: [u32; 3],
}

const SHADER: &str = r#"
struct P { nx: u32, ny: u32, nz: u32, color: u32, omega: f32, p0: u32, p1: u32, p2: u32 };
@group(0) @binding(0) var<uniform> p: P;
@group(0) @binding(1) var<storage, read_write> x: array<f32>;
@group(0) @binding(2) var<storage, read> g_e: array<f32>;
@group(0) @binding(3) var<storage, read> g_s: array<f32>;
@group(0) @binding(4) var<storage, read> g_u: array<f32>;
@group(0) @binding(5) var<storage, read> diag: array<f32>;
@group(0) @binding(6) var<storage, read> rhs: array<f32>;

@compute @workgroup_size(16, 16, 1)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    // Only this colour's cells are dispatched, so no invocation is spent returning early.
    let j = id.y; let k = id.z;
    if (j >= p.ny || k >= p.nz) { return; }
    let i = 2u * id.x + ((j + k + p.color) & 1u);
    if (i >= p.nx) { return; }
    let plane = p.nx * p.ny;
    let c = k * plane + j * p.nx + i;
    let d = diag[c];
    if (d <= 0.0) { return; }
    var sum = rhs[c];
    if (i + 1u < p.nx) { sum += g_e[c] * x[c + 1u]; }
    if (i > 0u) { sum += g_e[c - 1u] * x[c - 1u]; }
    if (j + 1u < p.ny) { sum += g_s[c] * x[c + p.nx]; }
    if (j > 0u) { sum += g_s[c - p.nx] * x[c - p.nx]; }
    if (k + 1u < p.nz) { sum += g_u[c] * x[c + plane]; }
    if (k > 0u) { sum += g_u[c - plane] * x[c - plane]; }
    x[c] = (1.0 - p.omega) * x[c] + p.omega * (sum / d);
}
"#;

const SPARSE_SHADER: &str = r#"
struct P { nx: u32, ny: u32, nz: u32, color: u32, omega: f32, p0: u32, p1: u32, p2: u32 };
@group(0) @binding(0) var<uniform> p: P;
@group(0) @binding(1) var<storage, read_write> x: array<f32>;
@group(0) @binding(2) var<storage, read> g_e: array<f32>;
@group(0) @binding(3) var<storage, read> g_s: array<f32>;
@group(0) @binding(4) var<storage, read> g_u: array<f32>;
@group(0) @binding(5) var<storage, read> diag: array<f32>;
@group(0) @binding(6) var<storage, read> rhs: array<f32>;
@group(0) @binding(7) var<storage, read> list: array<u32>;

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if (id.x >= arrayLength(&list)) { return; }
    let c = list[id.x];
    let plane = p.nx * p.ny;
    let k = c / plane;
    let r = c - k * plane;
    let j = r / p.nx;
    let i = r - j * p.nx;
    var sum = rhs[c];
    if (i + 1u < p.nx) { sum += g_e[c] * x[c + 1u]; }
    if (i > 0u) { sum += g_e[c - 1u] * x[c - 1u]; }
    if (j + 1u < p.ny) { sum += g_s[c] * x[c + p.nx]; }
    if (j > 0u) { sum += g_s[c - p.nx] * x[c - p.nx]; }
    if (k + 1u < p.nz) { sum += g_u[c] * x[c + plane]; }
    if (k > 0u) { sum += g_u[c - plane] * x[c - plane]; }
    x[c] = (1.0 - p.omega) * x[c] + p.omega * (sum / diag[c]);
}
"#;

pub struct Gpu {
    device: wgpu::Device,
    queue: wgpu::Queue,
    pipeline: wgpu::ComputePipeline,
    sparse_pipeline: wgpu::ComputePipeline,
    pub name: String,
    pub backend: String,
}

impl Gpu {
    pub fn new() -> Result<Gpu, String> {
        let instance = wgpu::Instance::default();
        let adapter = pollster::block_on(instance.request_adapter(&wgpu::RequestAdapterOptions {
            power_preference: wgpu::PowerPreference::HighPerformance,
            ..Default::default()
        }))
        .map_err(|e| format!("no GPU adapter: {e}"))?;
        let info = adapter.get_info();
        let (device, queue) = pollster::block_on(adapter.request_device(&wgpu::DeviceDescriptor {
            label: Some("boardfield"),
            required_limits: adapter.limits(),
            ..Default::default()
        }))
        .map_err(|e| format!("no GPU device: {e}"))?;
        let module = device
            .create_shader_module(wgpu::ShaderModuleDescriptor { label: Some("sor"), source: wgpu::ShaderSource::Wgsl(SHADER.into()) });
        let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("sor"),
            layout: None,
            module: &module,
            entry_point: Some("main"),
            compilation_options: Default::default(),
            cache: None,
        });
        let sparse_module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("sor-sparse"),
            source: wgpu::ShaderSource::Wgsl(SPARSE_SHADER.into()),
        });
        let sparse_pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("sor-sparse"),
            layout: None,
            module: &sparse_module,
            entry_point: Some("main"),
            compilation_options: Default::default(),
            cache: None,
        });
        Ok(Gpu { device, queue, pipeline, sparse_pipeline, name: info.name, backend: format!("{:?}", info.backend) })
    }

    fn storage(&self, data: &[f32], writable: bool) -> wgpu::Buffer {
        use wgpu::util::DeviceExt;
        let usage = if writable {
            wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST
        } else {
            wgpu::BufferUsages::STORAGE
        };
        self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor { label: None, contents: bytemuck::cast_slice(data), usage })
    }

    pub fn solve(&self, s: &Stencil, x: &mut [f32], omega: f64, tol: f64, max_sweeps: u32) -> Result<SolveInfo, String> {
        use wgpu::util::DeviceExt;
        let t0 = std::time::Instant::now();
        let mut x64: Vec<f64> = x.iter().map(|v| *v as f64).collect();
        let mut r = vec![0f32; x.len()];
        let zeros = vec![0f32; x.len()];
        let mut residual = s.residual_into(&x64, &mut r);
        let bx = self.storage(&zeros, true);
        let (be, bs, bu, bd, br) = (
            self.storage(&s.g_e, false),
            self.storage(&s.g_s, false),
            self.storage(&s.g_u, false),
            self.storage(&s.diag, false),
            self.storage(&r, true),
        );
        let bytes = std::mem::size_of_val(x) as u64;
        let staging = self.device.create_buffer(&wgpu::BufferDescriptor {
            label: None,
            size: bytes,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let lists = sparse_lists(s);
        let pipeline = if lists.is_some() { &self.sparse_pipeline } else { &self.pipeline };
        let layout = pipeline.get_bind_group_layout(0);
        let list_buffers: Vec<wgpu::Buffer> = lists
            .iter()
            .flat_map(|(a, b)| [a, b])
            .map(|l| {
                // An empty colour still needs a buffer to bind.
                let data: &[u32] = if l.is_empty() { &[u32::MAX] } else { l };
                self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                    label: None,
                    contents: bytemuck::cast_slice(data),
                    usage: wgpu::BufferUsages::STORAGE,
                })
            })
            .collect();
        let groups: Vec<wgpu::BindGroup> = (0..2u32)
            .map(|color| {
                let params = Params { nx: s.nx as u32, ny: s.ny as u32, nz: s.nz as u32, color, omega: omega as f32, _pad: [0; 3] };
                let ub = self.device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                    label: None,
                    contents: bytemuck::bytes_of(&params),
                    usage: wgpu::BufferUsages::UNIFORM,
                });
                let mut entries = vec![
                    wgpu::BindGroupEntry { binding: 0, resource: ub.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 1, resource: bx.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 2, resource: be.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 3, resource: bs.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 4, resource: bu.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 5, resource: bd.as_entire_binding() },
                    wgpu::BindGroupEntry { binding: 6, resource: br.as_entire_binding() },
                ];
                if !list_buffers.is_empty() {
                    entries.push(wgpu::BindGroupEntry { binding: 7, resource: list_buffers[color as usize].as_entire_binding() });
                }
                self.device.create_bind_group(&wgpu::BindGroupDescriptor { label: None, layout: &layout, entries: &entries })
            })
            .collect();
        let dense = ((s.nx as u32).div_ceil(2).div_ceil(16), (s.ny as u32).div_ceil(16), s.nz as u32);
        let counts: Vec<(u32, u32, u32)> = match &lists {
            Some((a, b)) => vec![((a.len() as u32).div_ceil(256), 1, 1), ((b.len() as u32).div_ceil(256), 1, 1)],
            None => vec![dense, dense],
        };
        let mut sweeps = 0;
        let mut correction = vec![0f32; x.len()];
        while residual >= tol && sweeps < max_sweeps {
            self.queue.write_buffer(&bx, 0, bytemuck::cast_slice(&zeros));
            self.queue.write_buffer(&br, 0, bytemuck::cast_slice(&r));
            let mut enc = self.device.create_command_encoder(&Default::default());
            for _ in 0..check_every() {
                for (g, n) in groups.iter().zip(&counts) {
                    let mut pass = enc.begin_compute_pass(&Default::default());
                    pass.set_pipeline(pipeline);
                    pass.set_bind_group(0, g, &[]);
                    pass.dispatch_workgroups(n.0, n.1, n.2);
                }
            }
            enc.copy_buffer_to_buffer(&bx, 0, &staging, 0, bytes);
            let tick = std::time::Instant::now();
            self.queue.submit([enc.finish()]);
            sweeps += check_every();
            let slice = staging.slice(..);
            let (tx, rx) = std::sync::mpsc::channel();
            slice.map_async(wgpu::MapMode::Read, move |r| {
                let _ = tx.send(r);
            });
            self.device.poll(wgpu::PollType::wait_indefinitely()).map_err(|e| format!("GPU poll failed: {e}"))?;
            rx.recv().map_err(|e| e.to_string())?.map_err(|e| format!("GPU read failed: {e}"))?;
            SWEEP_NANOS.fetch_add(tick.elapsed().as_nanos() as u64, std::sync::atomic::Ordering::Relaxed);
            {
                // wgpu 30 reports a mapping it can't give as an error. That stops the solve rather than reading garbage.
                let view = slice.get_mapped_range().map_err(|e| format!("GPU read failed: {e}"))?;
                correction.copy_from_slice(bytemuck::cast_slice(&view));
            }
            staging.unmap();
            x64.par_iter_mut().zip(correction.par_iter()).for_each(|(a, d)| *a += *d as f64);
            residual = s.residual_into(&x64, &mut r);
        }
        x.par_iter_mut().zip(x64.par_iter()).for_each(|(a, b)| *a = *b as f32);
        Ok(SolveInfo { sweeps, residual, seconds: t0.elapsed().as_secs_f64(), converged: residual < tol })
    }
}
