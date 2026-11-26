mod compress;
mod config;
mod loop_guard;
mod read_funnel;

use crate::config::{load_config, record_savings, Config};
use crate::read_funnel::FunnelOutcome;
use serde_json::{json, Map, Value};
use std::env;
use std::io::{self, Read, Write};
use std::process::{Command, Stdio};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const STDIN_MAX_BYTES: usize = 5 * 1024 * 1024;
const EXIT_DELEGATE: i32 = 2;

struct Args {
    host: Option<String>,
    event: String,
    python: Option<String>,
    script: Option<String>,
    version: bool,
}

fn parse_args() -> Args {
    let mut out = Args {
        host: None,
        event: String::new(),
        python: None,
        script: None,
        version: false,
    };
    let mut it = env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--version" | "-V" => out.version = true,
            "--host" => out.host = it.next(),
            "--event" => out.event = it.next().unwrap_or_default(),
            "--python" => out.python = it.next(),
            "--script" => out.script = it.next(),
            _ => {}
        }
    }
    out
}

fn detect_host(payload: &Map<String, Value>, hint: Option<&str>) -> String {
    if let Some(h) = hint {
        if matches!(h, "antigravity" | "claude" | "codex") {
            return h.to_string();
        }
    }
    if payload.get("conversationId").and_then(|v| v.as_str()).is_some()
        || payload.contains_key("toolCall")
        || payload.contains_key("workspacePaths")
    {
        return "antigravity".to_string();
    }
    let tp = payload
        .get("transcript_path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .replace('\\', "/");
    let fname = tp.rsplit('/').next().unwrap_or("");
    if payload.contains_key("turn_id") || tp.contains("/.codex/") || fname.starts_with("rollout-") {
        return "codex".to_string();
    }
    if env::var("ANTIGRAVITY_AGENT").ok().as_deref() == Some("1")
        || env::var("ANTIGRAVITY_CONVERSATION_ID").is_ok()
    {
        return "antigravity".to_string();
    }
    if env::var("CODEX_THREAD_ID").is_ok() || env::var("CODEX_HOME").is_ok() {
        return "codex".to_string();
    }
    "claude".to_string()
}

fn session_id(payload: &Map<String, Value>, host: &str) -> String {
    let key = if host == "antigravity" {
        "conversationId"
    } else {
        "session_id"
    };
    payload
        .get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

fn parse_int_val(val: Option<&Value>) -> Option<i64> {
    match val? {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn delegate_to_python(args: &Args, raw_stdin: &[u8]) -> i32 {
    let (Some(py), Some(script)) = (&args.python, &args.script) else {
        return EXIT_DELEGATE;
    };
    let mut cmd = Command::new(py);
    cmd.args(["-E", "-s", "-X", "utf8", script]);
    if let Some(ref h) = args.host {
        cmd.args(["--host", h]);
    }
    if !args.event.is_empty() {
        cmd.args(["--event", &args.event]);
    }
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    if let Ok(mut child) = cmd.spawn() {
        if let Some(mut stdin) = child.stdin.take() {
            let _ = stdin.write_all(raw_stdin);
        }
        let _ = child.wait();
    }
    0 // Always fail-open
}

enum DispatchResult {
    Done(Option<Value>),
    Delegate,
}

fn handle_pre_tool_use(host: &str, payload: &Map<String, Value>, cfg: &Config) -> DispatchResult {
    let sid = session_id(payload, host);

    if host == "antigravity" {
        let Some(tc) = payload.get("toolCall").and_then(|v| v.as_object()) else {
            return DispatchResult::Done(None);
        };
        let tool_name = tc.get("name").and_then(|v| v.as_str()).unwrap_or("");
        let empty = Map::new();
        let args = tc
            .get("args")
            .and_then(|v| v.as_object())
            .unwrap_or(&empty);

        if matches!(tool_name, "read_file" | "view_file") {
            let fp = args
                .get("Path")
                .or_else(|| args.get("AbsolutePath"))
                .or_else(|| args.get("path"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            return match read_funnel::check_read(
                &sid,
                fp,
                parse_int_val(args.get("StartLine")),
                parse_int_val(args.get("EndLine")),
                parse_int_val(args.get("ByteOffset")),
                cfg,
                host,
            ) {
                FunnelOutcome::Allow => DispatchResult::Done(None),
                FunnelOutcome::Deny(reason) => {
                    DispatchResult::Done(Some(json!({"decision": "deny", "reason": reason})))
                }
                FunnelOutcome::Delegate => DispatchResult::Delegate,
            };
        }

        if tool_name == "call_mcp_tool" || tool_name.starts_with("mcp_") {
            let (disp, inner_args) = if tool_name == "call_mcp_tool" {
                let srv = args.get("ServerName").and_then(|v| v.as_str()).unwrap_or("");
                let inner = args.get("ToolName").and_then(|v| v.as_str()).unwrap_or("");
                let d = if srv.is_empty() {
                    inner.to_string()
                } else {
                    format!("{srv}:{inner}")
                };
                (d, args.get("Arguments").cloned().unwrap_or(Value::Null))
            } else {
                (tool_name.to_string(), Value::Object(args.clone()))
            };
            return match read_funnel::check_mcp_refetch(&sid, &disp, &inner_args, cfg, host) {
                FunnelOutcome::Allow => DispatchResult::Done(None),
                FunnelOutcome::Deny(reason) => {
                    DispatchResult::Done(Some(json!({"decision": "deny", "reason": reason})))
                }
                FunnelOutcome::Delegate => DispatchResult::Delegate,
            };
        }

        if tool_name == "run_command" {
            return DispatchResult::Delegate;
        }
        return DispatchResult::Done(None);
    }

    // Claude Code & Codex PreToolUse
    let tool_name = payload
        .get("tool_name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    let empty = Map::new();
    let tool_input = payload
        .get("tool_input")
        .and_then(|v| v.as_object())
        .unwrap_or(&empty);

    if tool_name == "Read" {
        let fp = tool_input
            .get("file_path")
            .or_else(|| tool_input.get("path"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let offset = parse_int_val(tool_input.get("offset"));
        let limit = parse_int_val(tool_input.get("limit"));
        let end_line = match (offset, limit) {
            (Some(o), Some(l)) => Some(o + l - 1),
            _ => limit,
        };
        return match read_funnel::check_read(&sid, fp, offset, end_line, None, cfg, host) {
            FunnelOutcome::Allow => DispatchResult::Done(None),
            FunnelOutcome::Deny(reason) => DispatchResult::Done(Some(json!({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }))),
            FunnelOutcome::Delegate => DispatchResult::Delegate,
        };
    }

    if tool_name.starts_with("mcp__") {
        return match read_funnel::check_mcp_refetch(
            &sid,
            tool_name,
            &Value::Object(tool_input.clone()),
            cfg,
            host,
        ) {
            FunnelOutcome::Allow => DispatchResult::Done(None),
            FunnelOutcome::Deny(reason) => DispatchResult::Done(Some(json!({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }))),
            FunnelOutcome::Delegate => DispatchResult::Delegate,
        };
    }

    if matches!(tool_name, "Grep" | "Bash" | "bash" | "shell" | "exec_command") {
        return DispatchResult::Delegate;
    }

    DispatchResult::Done(None)
}

fn handle_post_tool_use(
    host: &str,
    payload: &Map<String, Value>,
    cfg: &Config,
    is_failure_event: bool,
) -> DispatchResult {
    let sid = session_id(payload, host);

    if host == "antigravity" {
        let empty = Map::new();
        let tc = payload
            .get("toolCall")
            .and_then(|v| v.as_object())
            .unwrap_or(&empty);
        let tool_name = tc.get("name").and_then(|v| v.as_str()).unwrap_or("");
        // Edits need cache invalidation + background AST reindex -> Python
        if matches!(tool_name, "edit_file" | "write_file") {
            return DispatchResult::Delegate;
        }
        let args = tc
            .get("args")
            .and_then(|v| v.as_object())
            .unwrap_or(&empty);
        let is_err = payload
            .get("error")
            .and_then(|v| v.as_str())
            .map(|s| !s.trim().is_empty())
            .unwrap_or(false);
        let _ = loop_guard::record_tool_event(&sid, tool_name, args, is_err, cfg);
        return DispatchResult::Done(None);
    }

    // Claude Code & Codex PostToolUse
    let tool_name = payload
        .get("tool_name")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if matches!(
        tool_name,
        "Edit" | "Write" | "MultiEdit" | "NotebookEdit" | "apply_patch"
    ) {
        return DispatchResult::Delegate;
    }

    let empty = Map::new();
    let tool_input = payload
        .get("tool_input")
        .and_then(|v| v.as_object())
        .unwrap_or(&empty);
    let tool_resp = payload.get("tool_response");

    // Delegate heavy output compression / archival on Claude Code
    if host == "claude" && !cfg.shadow {
        if tool_name == "Bash" && cfg.bash_compress {
            if let Some(stdout) = tool_resp
                .and_then(|v| v.as_object())
                .and_then(|o| o.get("stdout"))
                .and_then(|v| v.as_str())
            {
                if stdout.len() > 300 {
                    return DispatchResult::Delegate;
                }
            }
        }
        if tool_name.starts_with("mcp__") && cfg.archive_large_outputs {
            let approx_len = tool_resp.map(|v| v.to_string().len()).unwrap_or(0);
            if approx_len > cfg.archive_threshold_bytes {
                return DispatchResult::Delegate;
            }
        }
        if matches!(tool_name, "Glob" | "Grep") && cfg.archive_large_outputs {
            let approx_len = tool_resp.map(|v| v.to_string().len()).unwrap_or(0);
            if approx_len > 8192 {
                return DispatchResult::Delegate;
            }
        }
    }

    let is_err = is_failure_event
        || payload
            .get("error")
            .map(|v| !v.is_null() && v.as_str() != Some(""))
            .unwrap_or(false);

    if let Some(nudge) = loop_guard::record_tool_event(&sid, tool_name, tool_input, is_err, cfg) {
        let ev_name = if is_failure_event {
            "PostToolUseFailure"
        } else {
            "PostToolUse"
        };
        return DispatchResult::Done(Some(json!({
            "hookSpecificOutput": {
                "hookEventName": ev_name,
                "additionalContext": nudge,
            }
        })));
    }
    DispatchResult::Done(None)
}

fn handle_subagent_start(cfg: &Config) -> DispatchResult {
    if cfg.shadow {
        return DispatchResult::Done(None);
    }
    let brief = "[Terser Subagent] Prefer `terser-index` (`order`/`route`) or ranged reads (`offset`/`limit`); \
                 scope CLI output; return a concise summary with exact file paths and line spans—never dump raw files.";
    DispatchResult::Done(Some(json!({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": brief,
        }
    })))
}

fn run_exec_mode(raw_args: &[String]) -> i32 {
    // Syntax: terser-fast exec [--host <h>] -- <prog> [args...]
    let mut host = "antigravity".to_string();
    let mut idx = 0;
    while idx < raw_args.len() {
        if raw_args[idx] == "--" {
            idx += 1;
            break;
        }
        if raw_args[idx] == "--host" && idx + 1 < raw_args.len() {
            host = raw_args[idx + 1].clone();
            idx += 2;
            continue;
        }
        break;
    }
    if idx >= raw_args.len() {
        return 2;
    }
    let prog = &raw_args[idx];
    let prog_args = &raw_args[idx + 1..];
    let cmd_str = raw_args[idx..].join(" ");

    let output = match Command::new(prog).args(prog_args).output() {
        Ok(o) => o,
        Err(e) => {
            let _ = writeln!(io::stderr(), "[Terser] Failed to execute {prog}: {e}");
            return 127;
        }
    };

    let code = output.status.code().unwrap_or(1);
    let stdout_str = String::from_utf8_lossy(&output.stdout);
    let stderr_str = String::from_utf8_lossy(&output.stderr);

    // Tee-on-failure: preserve 100% of error output verbatim on non-zero exit
    if code != 0 {
        let _ = io::stdout().write_all(&output.stdout);
        let _ = io::stderr().write_all(&output.stderr);
        return code;
    }

    if stdout_str.len() > 300 {
        let compressed = compress::compress_output(&stdout_str, &cmd_str);
        if compressed.len() < (stdout_str.len() * 85) / 100 {
            record_savings("bash_compress", stdout_str.len(), compressed.len(), &host, prog);
            let _ = io::stdout().write_all(compressed.as_bytes());
            if !compressed.ends_with('\n') {
                let _ = io::stdout().write_all(b"\n");
            }
        } else {
            let _ = io::stdout().write_all(&output.stdout);
        }
    } else {
        let _ = io::stdout().write_all(&output.stdout);
    }

    if !stderr_str.is_empty() {
        let _ = io::stderr().write_all(&output.stderr);
    }
    0
}

fn run_compress_mode(raw_args: &[String]) -> i32 {
    let mut cmd_label = String::new();
    let mut i = 0;
    while i < raw_args.len() {
        if raw_args[i] == "--cmd" && i + 1 < raw_args.len() {
            cmd_label = raw_args[i + 1].clone();
            i += 2;
        } else {
            i += 1;
        }
    }
    let mut buf = Vec::new();
    let _ = io::stdin().take((STDIN_MAX_BYTES + 1) as u64).read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf);
    let out = compress::compress_output(&text, &cmd_label);
    let _ = io::stdout().write_all(out.as_bytes());
    0
}

fn main() {
    let all_args: Vec<String> = env::args().skip(1).collect();
    if let Some(first) = all_args.first().map(String::as_str) {
        if first == "exec" {
            std::process::exit(run_exec_mode(&all_args[1..]));
        }
        if first == "compress" {
            std::process::exit(run_compress_mode(&all_args[1..]));
        }
    }

    let args = parse_args();
    if args.version {
        println!("terser-fast {VERSION}");
        return;
    }

    let mut raw_stdin = Vec::new();
    let _ = io::stdin()
        .take((STDIN_MAX_BYTES + 1) as u64)
        .read_to_end(&mut raw_stdin);
    if raw_stdin.len() > STDIN_MAX_BYTES {
        return;
    }

    let cfg = load_config();
    if !cfg.enabled {
        return;
    }

    // Fast-path only handles PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart
    if !matches!(
        args.event.as_str(),
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" | "SubagentStart"
    ) {
        std::process::exit(delegate_to_python(&args, &raw_stdin));
    }

    let payload: Map<String, Value> = if raw_stdin.is_empty() {
        Map::new()
    } else {
        match serde_json::from_slice::<Value>(&raw_stdin) {
            Ok(Value::Object(m)) => m,
            _ => return, // fail-open on malformed JSON
        }
    };

    let host = detect_host(&payload, args.host.as_deref());
    let outcome = match args.event.as_str() {
        "PreToolUse" => handle_pre_tool_use(&host, &payload, &cfg),
        "PostToolUse" => handle_post_tool_use(&host, &payload, &cfg, false),
        "PostToolUseFailure" => handle_post_tool_use(&host, &payload, &cfg, true),
        "SubagentStart" => handle_subagent_start(&cfg),
        _ => DispatchResult::Delegate,
    };

    match outcome {
        DispatchResult::Done(Some(val)) => {
            if let Ok(s) = serde_json::to_string(&val) {
                println!("{s}");
            }
        }
        DispatchResult::Done(None) => {}
        DispatchResult::Delegate => {
            std::process::exit(delegate_to_python(&args, &raw_stdin));
        }
    }
}
