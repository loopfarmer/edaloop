//! Small helpers shared by the modules: hashing, number formatting, durable writes.
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

pub fn sha256_hex(bytes: &[u8]) -> String {
    let d = Sha256::digest(bytes);
    let mut s = String::with_capacity(64);
    for b in d {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

pub fn sha256_raw(bytes: &[u8]) -> [u8; 32] {
    Sha256::digest(bytes).into()
}

/// Format millimetres the way KiCad does: at most six decimals, no trailing zeros.
pub fn fmt_mm(v: f64) -> String {
    let s = format!("{v:.6}");
    let t = s.trim_end_matches('0').trim_end_matches('.');
    if t == "-0" || t.is_empty() {
        "0".to_string()
    } else {
        t.to_string()
    }
}

pub fn to_nm(mm: f64) -> i64 {
    (mm * 1_000_000.0).round() as i64
}

/// Quote a string for a KiCad S-expression.
pub fn quote(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '\\' => o.push_str("\\\\"),
            '"' => o.push_str("\\\""),
            '\n' => o.push_str("\\n"),
            '\r' => o.push_str("\\r"),
            '\t' => o.push_str("\\t"),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

pub fn sync_dir(dir: &Path) -> io::Result<()> {
    fs::File::open(dir)?.sync_all()
}

pub fn parent_of(path: &Path) -> PathBuf {
    match path.parent() {
        Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

/// Create a file with these bytes and sync it. Fails if the file already exists.
pub fn write_new_synced(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut f = fs::OpenOptions::new().write(true).create_new(true).open(path)?;
    f.write_all(bytes)?;
    f.flush()?;
    f.sync_all()
}

/// Temporary name beside `path`, in the same folder so a rename is atomic.
pub fn temp_beside(path: &Path) -> PathBuf {
    let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
    parent_of(path).join(format!(".{name}.boardhand-{}.tmp", uuid::Uuid::new_v4()))
}

/// Write, sync, rename over the target, sync the folder. Keeps the target's mode.
pub fn atomic_replace(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let tmp = temp_beside(path);
    let r = (|| {
        write_new_synced(&tmp, bytes)?;
        if let Ok(meta) = fs::metadata(path) {
            fs::set_permissions(&tmp, meta.permissions())?;
        }
        fs::rename(&tmp, path)
    })();
    if r.is_err() {
        let _ = fs::remove_file(&tmp);
        return r;
    }
    sync_dir(&parent_of(path))
}

/// A private scratch folder that is removed when dropped.
pub struct ScratchDir(pub PathBuf);

impl ScratchDir {
    pub fn new() -> io::Result<ScratchDir> {
        let p = std::env::temp_dir().join(format!("boardhand-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&p)?;
        Ok(ScratchDir(p))
    }
}

impl Drop for ScratchDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
