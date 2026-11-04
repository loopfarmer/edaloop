//! Runs external programs with a timeout, in their own process group, with
//! output sent to files so a stuck grandchild can't hold a pipe open.
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Ended {
    Exited(i32),
    Signaled(i32),
    TimedOut,
}

#[derive(Debug, Clone)]
pub struct Finished {
    pub ended: Ended,
    pub stdout_tail: String,
    pub stderr_tail: String,
    pub elapsed: Duration,
}

pub const TAIL_BYTES: u64 = 2000;

static ACTIVE: Mutex<Option<HashSet<i32>>> = Mutex::new(None);

fn register(pgid: i32, add: bool) {
    if let Ok(mut g) = ACTIVE.lock() {
        let set = g.get_or_insert_with(HashSet::new);
        if add {
            set.insert(pgid);
        } else {
            set.remove(&pgid);
        }
    }
}

fn kill_group(pgid: i32, signal: i32) {
    // SAFETY: kill(2) takes plain integers and has no memory effects.
    unsafe {
        libc::kill(-pgid, signal);
    }
}

/// Stop every running child. Used when the client has gone away.
pub fn kill_all_active() {
    if let Ok(g) = ACTIVE.lock() {
        for pgid in g.iter().flatten() {
            kill_group(*pgid, libc::SIGKILL);
        }
    }
}

fn tail(path: &Path) -> String {
    use std::io::{Read, Seek, SeekFrom};
    let Ok(mut f) = fs::File::open(path) else { return String::new() };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let _ = f.seek(SeekFrom::Start(len.saturating_sub(TAIL_BYTES)));
    let mut buf = Vec::new();
    let _ = f.read_to_end(&mut buf);
    String::from_utf8_lossy(&buf).into_owned()
}

pub struct Spec<'a> {
    pub program: &'a Path,
    pub args: Vec<OsString>,
    pub cwd: Option<&'a Path>,
    pub env: Vec<(&'a str, &'a str)>,
    pub timeout: Duration,
    /// Where stdout and stderr captures go. The caller owns and removes it.
    pub capture_dir: &'a Path,
}

pub fn run(spec: Spec<'_>) -> io::Result<Finished> {
    let out_path = spec.capture_dir.join("stdout.txt");
    let err_path = spec.capture_dir.join("stderr.txt");
    let mut cmd = Command::new(spec.program);
    cmd.args(&spec.args)
        .stdin(Stdio::null())
        .stdout(fs::File::create(&out_path)?)
        .stderr(fs::File::create(&err_path)?)
        .process_group(0);
    if let Some(d) = spec.cwd {
        cmd.current_dir(d);
    }
    for (k, v) in &spec.env {
        cmd.env(k, v);
    }
    let began = Instant::now();
    let mut child = cmd.spawn()?;
    let pgid = child.id() as i32;
    register(pgid, true);
    let mut timed_out = false;
    let status = loop {
        if let Some(st) = child.try_wait()? {
            break st;
        }
        if !timed_out && began.elapsed() >= spec.timeout {
            timed_out = true;
            kill_group(pgid, libc::SIGTERM);
            let grace = Instant::now();
            while grace.elapsed() < Duration::from_secs(2) && matches!(child.try_wait(), Ok(None)) {
                std::thread::sleep(Duration::from_millis(20));
            }
            kill_group(pgid, libc::SIGKILL);
        }
        std::thread::sleep(Duration::from_millis(20));
    };
    if timed_out {
        kill_group(pgid, libc::SIGKILL);
    }
    register(pgid, false);
    let ended = if timed_out {
        Ended::TimedOut
    } else if let Some(sig) = status.signal() {
        Ended::Signaled(sig)
    } else {
        Ended::Exited(status.code().unwrap_or(-1))
    };
    Ok(Finished { ended, stdout_tail: tail(&out_path), stderr_tail: tail(&err_path), elapsed: began.elapsed() })
}

fn is_executable(p: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    fs::metadata(p).map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
}

/// Section 10's search order. Returns the path, or the reason there is none.
pub fn resolve_kicad_cli() -> Result<PathBuf, String> {
    if let Some(v) = std::env::var_os("BOARDHAND_KICAD_CLI") {
        let p = PathBuf::from(&v);
        return if is_executable(&p) {
            Ok(p)
        } else {
            Err(format!("BOARDHAND_KICAD_CLI is set to {} but that isn't an executable file", p.display()))
        };
    }
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            let p = dir.join("kicad-cli");
            if is_executable(&p) {
                return Ok(p);
            }
        }
    }
    for known in ["/opt/homebrew/bin/kicad-cli", "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"] {
        if is_executable(Path::new(known)) {
            return Ok(PathBuf::from(known));
        }
    }
    Err("kicad-cli was not found on PATH or in the known install locations".to_string())
}

pub fn find_on_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|d| d.join(name)).find(|p| is_executable(p))
}
