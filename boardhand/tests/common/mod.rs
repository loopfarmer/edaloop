#![allow(dead_code)]
//! Shared test support. Fixtures are copied, and every file a test touches stays under target/fixtures.
use serde_json::{json, Value};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{channel, Receiver};
use std::time::Duration;

pub const SMALL_VAR: &str = "BOARDHAND_FIXTURE_SMALL";
pub const LARGE_VAR: &str = "BOARDHAND_FIXTURE_LARGE";

fn fixture_path(var: &str) -> &'static str {
    Box::leak(std::env::var(var).unwrap_or_else(|_| format!("<{var} is not set>")).into_boxed_str())
}

/// The small and large fixture boards are private, so they're named by environment variable. A test skips without them.
pub fn small() -> &'static str {
    fixture_path(SMALL_VAR)
}

pub fn large() -> &'static str {
    fixture_path(LARGE_VAR)
}
pub const KICAD_CLI: &str = "/opt/homebrew/bin/kicad-cli";
pub const KICAD_PY: &str = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9";

pub fn work(name: &str) -> PathBuf {
    let d = Path::new(env!("CARGO_MANIFEST_DIR")).join("target/fixtures").join(name);
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

/// Copy a fixture's board, plus its .kicad_pro and .kicad_dru when present, and nothing else.
pub fn copy_fixture(original: &str, dir: &Path) -> Option<PathBuf> {
    let src = Path::new(original);
    if !src.exists() {
        println!("NOTICE: skipping, fixture {original} is absent");
        return None;
    }
    let dst = dir.join(src.file_name().unwrap());
    fs::copy(src, &dst).unwrap();
    for ext in ["kicad_pro", "kicad_dru"] {
        let side = src.with_extension(ext);
        if side.exists() {
            fs::copy(&side, dst.with_extension(ext)).unwrap();
        }
    }
    Some(dst)
}

pub fn exe() -> &'static str {
    env!("CARGO_BIN_EXE_boardhand")
}

pub fn sha(p: &Path) -> String {
    boardhand::util::sha256_hex(&fs::read(p).unwrap())
}

pub struct Mcp {
    pub child: Child,
    stdin: ChildStdin,
    rx: Receiver<Value>,
    next: u64,
    pub notifications: Vec<Value>,
    pub dir: PathBuf,
}

impl Mcp {
    /// Start the server with a private temp and state folder under `dir`.
    pub fn start(dir: &Path, env: &[(&str, &str)]) -> Mcp {
        let tmp = dir.join("tmp");
        fs::create_dir_all(&tmp).unwrap();
        let mut cmd = Command::new(exe());
        cmd.env("TMPDIR", &tmp)
            .env("XDG_STATE_HOME", dir.join("state"))
            .env_remove("BOARDHAND_KICAD_CLI")
            .env_remove("BOARDHAND_DSN_EXPORTER")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for (k, v) in env {
            cmd.env(k, v);
        }
        let mut child = cmd.spawn().unwrap();
        let stdin = child.stdin.take().unwrap();
        let out = child.stdout.take().unwrap();
        let (tx, rx) = channel();
        std::thread::spawn(move || {
            for line in BufReader::new(out).lines() {
                let Ok(line) = line else { break };
                // Stdout carries protocol messages only: every line must be JSON-RPC.
                let v: Value = serde_json::from_str(&line).unwrap_or_else(|e| panic!("non-JSON on stdout: {line}: {e}"));
                assert_eq!(v["jsonrpc"], "2.0", "{line}");
                if tx.send(v).is_err() {
                    break;
                }
            }
        });
        Mcp { child, stdin, rx, next: 1, notifications: vec![], dir: dir.to_path_buf() }
    }

    pub fn send_raw(&mut self, line: &str) {
        self.stdin.write_all(line.as_bytes()).unwrap();
        self.stdin.write_all(b"\n").unwrap();
        self.stdin.flush().unwrap();
    }

    pub fn recv(&mut self) -> Value {
        self.rx.recv_timeout(Duration::from_secs(600)).expect("no message from the server")
    }

    pub fn request(&mut self, method: &str, params: Value) -> Value {
        let id = self.next;
        self.next += 1;
        self.send_raw(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string());
        loop {
            let m = self.recv();
            if m["id"] == json!(id) {
                return m;
            }
            assert!(m.get("id").is_none(), "unexpected response {m}");
            self.notifications.push(m);
        }
    }

    pub fn initialize(&mut self) -> Value {
        let r = self.request(
            "initialize",
            json!({"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}),
        );
        self.send_raw(r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#);
        r
    }

    /// Call a tool and return (parsed reply, isError).
    pub fn call_full(&mut self, name: &str, args: Value) -> (Value, bool) {
        let r = self.request("tools/call", json!({"name": name, "arguments": args}));
        let text = r["result"]["content"][0]["text"].as_str().unwrap_or_else(|| panic!("no text in {r}"));
        let reply: Value = serde_json::from_str(text).unwrap();
        let outcome = reply["outcome"].as_str().unwrap();
        assert!(["complete", "partial", "refused", "uncertain", "unavailable"].contains(&outcome));
        assert!(["file", "cli", "none"].contains(&reply["source"].as_str().unwrap()));
        if outcome != "complete" {
            assert!(reply["reason"].is_string() && reply["detail"].is_string(), "{reply}");
        }
        let is_err = r["result"]["isError"].as_bool().unwrap();
        assert_eq!(is_err, ["refused", "uncertain", "unavailable"].contains(&outcome), "{reply}");
        (reply, is_err)
    }

    pub fn call(&mut self, name: &str, args: Value) -> Value {
        self.call_full(name, args).0
    }

    pub fn load(&mut self, groups: Value) {
        let r = self.call("groups_load", json!({"groups": groups}));
        assert_eq!(r["outcome"], "complete");
    }

    pub fn tool_names(&mut self) -> Vec<String> {
        let r = self.request("tools/list", json!({}));
        r["result"]["tools"].as_array().unwrap().iter().map(|t| t["name"].as_str().unwrap().to_string()).collect()
    }
}

impl Drop for Mcp {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn script(path: &Path, body: &str) {
    use std::os::unix::fs::PermissionsExt;
    fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
}

/// Run a Python snippet with KiCad's own pcbnew, if it's installed.
pub fn pcbnew(snippet: &str, args: &[&Path]) -> Option<String> {
    if !Path::new(KICAD_PY).exists() {
        println!("NOTICE: KiCad's Python is absent, skipping the pcbnew check");
        return None;
    }
    let out = Command::new(KICAD_PY).arg("-I").arg("-c").arg(snippet).args(args).output().unwrap();
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    assert!(out.status.success(), "pcbnew failed: {}\n{stdout}", String::from_utf8_lossy(&out.stderr));
    Some(stdout)
}

/// KiCad itself loads this board and writes a DRC report for it. Returns the report.
pub fn kicad_loads(board: &Path) -> Option<Value> {
    if !Path::new(KICAD_CLI).exists() {
        println!("NOTICE: kicad-cli is absent, skipping the load check");
        return None;
    }
    let report = board.with_extension("drc.json");
    let out = Command::new(KICAD_CLI)
        .args(["pcb", "drc", "--format", "json", "--severity-all", "--output"])
        .arg(&report)
        .arg(board)
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    Some(serde_json::from_slice(&fs::read(&report).expect("KiCad wrote no report")).unwrap())
}

/// A small hand-written board with a locked segment, a GND via and a keepout.
pub const MINI_BOARD: &str = r#"(kicad_pcb
	(version 20260206)
	(generator "test")
	(generator_version "10.0")
	(general
		(thickness 1.6)
	)
	(paper "A4")
	(layers
		(0 "F.Cu" signal)
		(2 "B.Cu" signal)
		(25 "Edge.Cuts" user)
	)
	(setup
		(pad_to_mask_clearance 0)
	)
	(footprint "T:R"
		(layer "F.Cu")
		(uuid "10000000-0000-4000-8000-000000000001")
		(at 10 10)
		(property "Reference" "R1"
			(at 0 -1 0)
			(layer "F.Fab")
			(uuid "10000000-0000-4000-8000-000000000002")
			(effects
				(font
					(size 1 1)
				)
			)
		)
		(pad "1" smd rect
			(at -1 0)
			(size 1 1)
			(layers "F.Cu")
			(net "SIG")
			(uuid "10000000-0000-4000-8000-000000000003")
		)
		(pad "2" smd rect
			(at 1 0)
			(size 1 1)
			(layers "F.Cu")
			(net "GND")
			(uuid "10000000-0000-4000-8000-000000000004")
		)
	)
	(gr_rect
		(start 0 0)
		(end 30 30)
		(stroke
			(width 0.1)
			(type solid)
		)
		(fill no)
		(layer "Edge.Cuts")
		(uuid "10000000-0000-4000-8000-000000000005")
	)
	(segment
		(start 9 10)
		(end 5 10)
		(width 0.25)
		(locked yes)
		(layer "F.Cu")
		(net "SIG")
		(uuid "20000000-0000-4000-8000-000000000001")
	)
	(segment
		(start 5 10)
		(end 5 20)
		(width 0.25)
		(layer "F.Cu")
		(net "SIG")
		(uuid "20000000-0000-4000-8000-000000000002")
	)
	(via
		(at 20 20)
		(size 0.6)
		(drill 0.3)
		(layers "F.Cu" "B.Cu")
		(net "GND")
		(uuid "20000000-0000-4000-8000-000000000003")
	)
	(embedded_fonts no)
)
"#;
