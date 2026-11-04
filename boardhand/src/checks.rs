//! DRC and ERC through kicad-cli. A check that couldn't run never reads as a pass.
use crate::proc::{self, Ended, Spec};
use crate::reply::{Reply, Source};
use crate::util::{sha256_hex, ScratchDir};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::fs;
use std::path::Path;
use std::time::Duration;

const SRC: Source = Source::Cli;
pub const ITEM_LIMIT: usize = 200;

#[derive(Clone, Copy)]
pub enum Check {
    Drc,
    Erc,
}

/// Findings from a report, or None if it isn't the shape this check writes.
pub fn findings(check: Check, report: &Value) -> Option<Vec<Value>> {
    let mut out = vec![];
    let mut take = |section: &str, arr: &Vec<Value>| {
        for item in arr {
            let mut i = item.clone();
            if let Some(o) = i.as_object_mut() {
                o.insert("section".into(), json!(section));
            }
            out.push(i);
        }
    };
    match check {
        Check::Drc => {
            take("violations", report.get("violations")?.as_array()?);
            take("unconnected_items", report.get("unconnected_items")?.as_array()?);
            take("schematic_parity", report.get("schematic_parity")?.as_array()?);
        }
        Check::Erc => {
            for sheet in report.get("sheets")?.as_array()? {
                take("violations", sheet.get("violations")?.as_array()?);
            }
        }
    }
    Some(out)
}

pub fn run(check: Check, args: &Value) -> Reply {
    let field = match check {
        Check::Drc => "board",
        Check::Erc => "schematic",
    };
    let raw = args[field].as_str().unwrap_or("");
    let path = Path::new(raw);
    if !path.is_absolute() {
        return crate::edit::not_absolute(field, raw).source_cli();
    }
    let Ok(bytes) = fs::read(path) else {
        return Reply::refused(SRC, "file_not_found", format!("{raw} can't be read.")).target(path);
    };
    let rev_before = sha256_hex(&bytes);
    drop(bytes);
    let project = path.with_extension("kicad_pro");
    let default_rules = !project.is_file();
    let allow = args["allow_default_rules"].as_bool().unwrap_or(false);
    if default_rules && !allow {
        return Reply::refused(SRC, "no_project_file", format!("{} doesn't exist, so KiCad would apply default rules and could report false violations. Pass allow_default_rules to run anyway.", project.display()))
            .target(path)
            .revs(Some(&rev_before), None);
    }
    let base = |r: Reply| r.target(path).revs(Some(&rev_before), None);
    let cli = match proc::resolve_kicad_cli() {
        Ok(c) => c,
        Err(why) => return base(Reply::unavailable(SRC, "cli_missing", format!("{why}. The check didn't run."))),
    };
    let scratch = match ScratchDir::new() {
        Ok(s) => s,
        Err(e) => {
            return base(Reply::unavailable(
                SRC,
                "scratch_failed",
                format!("Couldn't create a temporary folder: {e}. The check didn't run."),
            ))
        }
    };
    let report_path = scratch.0.join("report.json");
    let (group, sub) = match check {
        Check::Drc => ("pcb", "drc"),
        Check::Erc => ("sch", "erc"),
    };
    let cmd_args: Vec<OsString> = vec![
        group.into(),
        sub.into(),
        "--format".into(),
        "json".into(),
        "--severity-all".into(),
        "--output".into(),
        report_path.clone().into_os_string(),
        path.as_os_str().to_os_string(),
    ];
    let timeout = args["timeout_s"].as_f64().unwrap_or(900.0) as u64;
    // kicad-cli's own temporary files go in the scratch folder too, and leave with it.
    let tmpdir = scratch.0.to_string_lossy().to_string();
    let finished = match proc::run(Spec {
        program: &cli,
        args: cmd_args,
        cwd: None,
        env: vec![("TMPDIR", &tmpdir)],
        timeout: Duration::from_secs(timeout),
        capture_dir: &scratch.0,
    }) {
        Ok(f) => f,
        Err(e) => {
            return base(Reply::unavailable(
                SRC,
                "cli_missing",
                format!("{} couldn't be started: {e}. The check didn't run.", cli.display()),
            ))
            .with(json!({"cli": cli.to_string_lossy()}))
        }
    };
    let (exit_code, signal) = match finished.ended {
        Ended::Exited(c) => (Some(c), None),
        Ended::Signaled(s) => (None, Some(s)),
        Ended::TimedOut => (None, None),
    };
    let diag = json!({
        "cli": cli.to_string_lossy(),
        "exit_code": exit_code,
        "signal": signal,
        "stderr_tail": finished.stderr_tail,
        "stdout_tail": finished.stdout_tail,
        "duration_ms": finished.elapsed.as_millis() as u64,
    });
    let fail =
        |reason: &str, detail: String| base(Reply::unavailable(SRC, reason, format!("{detail} No findings are known."))).with(diag.clone());
    match finished.ended {
        Ended::TimedOut => return fail("cli_timeout", format!("kicad-cli was still running after {timeout} s and was killed.")),
        Ended::Signaled(s) => return fail("cli_crashed", format!("kicad-cli was killed by signal {s}.")),
        Ended::Exited(_) => {}
    }
    let report_bytes = fs::read(&report_path).ok().filter(|b| !b.is_empty());
    let Some(report_bytes) = report_bytes else {
        return fail("no_report", format!("kicad-cli exited with code {} without writing a report.", exit_code.unwrap_or(-1)));
    };
    if exit_code != Some(0) {
        return fail("cli_failed", format!("kicad-cli exited with code {}, so its report can't be trusted.", exit_code.unwrap_or(-1)));
    }
    let Ok(report) = serde_json::from_slice::<Value>(&report_bytes) else {
        return fail("bad_report", "kicad-cli's report isn't valid JSON.".to_string());
    };
    let Some(all) = findings(check, &report) else {
        return fail("bad_report", "kicad-cli's report lacks the expected sections.".to_string());
    };
    let mut by_type: BTreeMap<String, u64> = BTreeMap::new();
    let mut by_severity: BTreeMap<String, u64> = BTreeMap::new();
    for i in &all {
        *by_type.entry(i["type"].as_str().unwrap_or("unknown").to_string()).or_insert(0) += 1;
        *by_severity.entry(i["severity"].as_str().unwrap_or("unknown").to_string()).or_insert(0) += 1;
    }
    let rev_after = fs::read(path).ok().map(|b| sha256_hex(&b));
    let result = json!({
        "total": all.len(),
        "counts_by_type": by_type,
        "counts_by_severity": by_severity,
        "items": all.iter().take(ITEM_LIMIT).collect::<Vec<_>>(),
        "items_returned": all.len().min(ITEM_LIMIT),
        "default_rules_used": default_rules,
        "kicad_version": report.get("kicad_version"),
        "cli": cli.to_string_lossy(),
        "exit_code": exit_code,
        "duration_ms": finished.elapsed.as_millis() as u64,
    });
    if rev_after.as_deref() != Some(rev_before.as_str()) {
        return Reply::partial(
            SRC,
            "file_changed_during_check",
            "The file changed while the check ran, so these findings may describe the older version.",
            result,
        )
        .target(path)
        .revs(Some(&rev_before), rev_after.as_deref());
    }
    Reply::complete(SRC, result).target(path).revs(Some(&rev_before), rev_after.as_deref())
}
