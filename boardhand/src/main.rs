use boardhand::journal;
use std::path::{Path, PathBuf};

const HELP: &str = "boardhand: MCP server for reading and editing KiCad 10 boards

USAGE:
  boardhand                                   Run the MCP server on stdio
  boardhand --version                         Print the version
  boardhand --help                            Print this help
  boardhand transaction list <folder>         List journals left in a folder
  boardhand transaction recover <id> <folder> Restore each file to its original bytes
  boardhand transaction abandon <id> <folder> Delete a journal, leaving files as they are

ENVIRONMENT:
  BOARDHAND_KICAD_CLI      Path of kicad-cli
  BOARDHAND_DSN_EXPORTER   Command given the board path and the DSN path
  XDG_STATE_HOME           Folder for boardhand/calls.jsonl
";

fn usage(msg: &str) -> i32 {
    eprintln!("boardhand: {msg}\n\n{HELP}");
    2
}

fn folder_arg(a: &str) -> Result<PathBuf, i32> {
    std::fs::canonicalize(Path::new(a)).map_err(|e| {
        eprintln!("boardhand: {a}: {e}");
        1
    })
}

fn transaction(args: &[String]) -> i32 {
    let strs: Vec<&str> = args.iter().map(String::as_str).collect();
    match strs.as_slice() {
        ["list", folder] => match folder_arg(folder) {
            Ok(f) => {
                println!("{}", serde_json::json!({"journals": journal::list(&f)}));
                0
            }
            Err(c) => c,
        },
        ["recover", id, folder] | ["abandon", id, folder] => match folder_arg(folder) {
            Ok(f) => {
                let r = if strs[0] == "recover" { journal::recover(id, &f) } else { journal::abandon(id, &f) };
                println!("{}", r.report);
                r.code
            }
            Err(c) => c,
        },
        _ => usage("expected: transaction list <folder> | recover <id> <folder> | abandon <id> <folder>"),
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let code = match args.first().map(String::as_str) {
        None => boardhand::server::serve(),
        Some("--version") if args.len() == 1 => {
            println!("boardhand {}", env!("CARGO_PKG_VERSION"));
            0
        }
        Some("--help") if args.len() == 1 => {
            print!("{HELP}");
            0
        }
        Some("transaction") => transaction(&args[1..]),
        Some(other) => usage(&format!("unrecognised argument '{other}'")),
    };
    std::process::exit(code);
}
