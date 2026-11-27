#![cfg_attr(all(windows, not(test)), windows_subsystem = "windows")]

mod compress;
mod config;
mod difflib;
mod gates;
mod loop_guard;
mod read_funnel;
mod worker;

use crate::config::{
    atomic_write_json, load_config, now_secs, record_savings, safe_sid_filename, state_home, Config,
};
use crate::read_funnel::FunnelOutcome;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::io::{self, Read, Write};
use std::process::{Command, Stdio};

const VERSION: &str = env!("CARGO_PKG_VERSION");
const STDIN_MAX_BYTES: usize = 5 * 1024 * 1024;
const EXIT_DELEGATE: i32 = 2;
const SUBAGENT_BRIEF_AG: &str = "[Terser Subagent] Prefer `terser-index` (`order`/`route`, if available) or ranged reads (`StartLine`/`EndLine`); batch parallel reads; scope CLI output; return a concise summary with exact file paths and line spans; quote code verbatim only when the task asks for it.";
const SUBAGENT_BRIEF_CC: &str = "[Terser Subagent] Prefer `terser-index` (`order`/`route`, if available) or ranged reads (`offset`/`limit`); batch parallel reads; scope CLI output; return a concise summary with exact file paths and line spans; quote code verbatim only when the task asks for it.";

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
    if payload
        .get("conversationId")
        .and_then(|v| v.as_str())
        .is_some()
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

fn sanitize_sid_part(raw: &str) -> String {
    raw.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

fn session_id(payload: &Map<String, Value>, host: &str) -> String {
    let key = if host == "antigravity" {
        "conversationId"
    } else {
        "session_id"
    };
    let base = payload
        .get(key)
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim();
    let agent = ["agent_id", "subagent_id", "agentId"]
        .iter()
        .find_map(|k| {
            payload
                .get(*k)
                .and_then(|v| v.as_str())
                .map(str::trim)
                .filter(|s| !s.is_empty())
        })
        .unwrap_or("");
    if !base.is_empty() && !agent.is_empty() && agent != base {
        let b = sanitize_sid_part(base);
        let a = sanitize_sid_part(agent);
        let composed = format!("{b}--{a}");
        if composed.len() > 78 {
            let bp: String = b.chars().take(44).collect();
            let ap: String = a.chars().take(32).collect();
            return format!("{bp}--{ap}");
        }
        return composed;
    }
    base.to_string()
}

fn parse_int_val(val: Option<&Value>) -> Option<i64> {
    match val? {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn find_python_bin() -> Option<(String, Vec<&'static str>)> {
    if let Ok(explicit) = env::var("TERSER_PYTHON") {
        let trimmed = explicit.trim();
        if !trimmed.is_empty() && std::path::Path::new(trimmed).is_file() {
            return Some((trimmed.to_string(), vec![]));
        }
    }
    let path_var = env::var_os("PATH").unwrap_or_default();
    #[cfg(windows)]
    {
        for name in ["pythonw.exe", "python.exe"] {
            for dir in env::split_paths(&path_var) {
                let cand = dir.join(name);
                if cand.is_file() {
                    let s = cand.to_string_lossy();
                    if !s.to_ascii_lowercase().contains("windowsapps") {
                        return Some((s.into_owned(), vec![]));
                    }
                }
            }
        }
        for dir in env::split_paths(&path_var) {
            let cand = dir.join("py.exe");
            if cand.is_file() {
                return Some((cand.to_string_lossy().into_owned(), vec!["-3"]));
            }
        }
        None
    }
    #[cfg(not(windows))]
    {
        for abs in [
            "/usr/bin/python3",
            "/usr/local/bin/python3",
            "/opt/homebrew/bin/python3",
        ] {
            if std::path::Path::new(abs).is_file() {
                return Some((abs.to_string(), vec![]));
            }
        }
        for name in ["python3", "python"] {
            for dir in env::split_paths(&path_var) {
                let cand = dir.join(name);
                if cand.is_file() {
                    return Some((cand.to_string_lossy().into_owned(), vec![]));
                }
            }
        }
        None
    }
}

fn resolve_hook_script(explicit: Option<&str>) -> Option<String> {
    if let Some(s) = explicit {
        if !s.trim().is_empty() {
            return Some(s.to_string());
        }
    }
    let exe = env::current_exe().ok()?;
    let cand = exe
        .parent()?
        .parent()?
        .join("runtime")
        .join("unified_hook.py");
    if cand.is_file() {
        return Some(cand.to_string_lossy().into_owned());
    }
    None
}

fn delegate_to_python(args: &Args, raw_stdin: &[u8], cfg: &Config) -> i32 {
    let Some(script) = resolve_hook_script(args.script.as_deref()) else {
        return EXIT_DELEGATE;
    };
    let (py, prefix_args) = if let Some(ref p) = args.python {
        (p.clone(), vec![])
    } else if let Some(found) = find_python_bin() {
        found
    } else {
        return EXIT_DELEGATE;
    };
    if cfg.python_worker {
        let key = worker::worker_key(&script);
        match worker::call(&key, args.host.as_deref(), &args.event, raw_stdin) {
            worker::Outcome::Handled(out) => {
                let mut stdout = io::stdout().lock();
                let _ = stdout.write_all(&out);
                let _ = stdout.flush();
                return 0;
            }
            worker::Outcome::Lost => return 0, // Fail-open, exactly like a hook timeout
            worker::Outcome::Unavailable => worker::spawn(&key, &py, &prefix_args, &script),
        }
    }
    let mut cmd = Command::new(&py);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    for pa in prefix_args {
        cmd.arg(pa);
    }
    cmd.args(["-E", "-s", "-X", "utf8", &script]);
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

#[cfg(test)]
fn is_low_complexity_task(prompt: &str) -> bool {
    let text = prompt.trim();
    let low = text.to_ascii_lowercase();
    let mut score: i32 = 40;
    if text.len() > 500 {
        score += 15;
    } else if text.len() < 80 {
        score -= 10;
    }

    const HIGH_35: &[&str] = &[
        "architect",
        "redesign",
        "migrate",
        "refactor across",
        "multi-file",
        "multifile",
        "distributed",
        "concurren",
        "race condition",
        "deadlock",
        "memory leak",
        "security audit",
        "cryptograph",
        "zero-day",
        "zeroday",
        "kernel",
        "compiler",
        "optimizer",
    ];
    const HIGH_25: &[&str] = &[
        "root cause",
        "intermittent",
        "flaky",
        "heisenbug",
        "performance regression",
        "profiling",
        "benchmark",
        "algorithm",
    ];
    const HIGH_20: &[&str] = &[
        "design a",
        "from scratch",
        "end-to-end",
        "endtoend",
        "system design",
        "state machine",
    ];
    const LOW_25: &[&str] = &[
        "typo",
        "rename",
        "format",
        "lint",
        "comment",
        "docstring",
        "bump version",
        "add import",
        "fix warning",
        "changelog",
        "readme",
        "gitignore",
        "boilerplate",
        "simple test",
        "unit test for",
    ];
    const LOW_15: &[&str] = &[
        "explain",
        "summarize",
        "what does",
        "where is",
        "find where",
        "list all",
        "show me",
    ];

    if HIGH_35.iter().any(|k| low.contains(k)) {
        score += 35;
    }
    if HIGH_25.iter().any(|k| low.contains(k)) {
        score += 25;
    }
    if HIGH_20.iter().any(|k| low.contains(k)) {
        score += 20;
    }
    if LOW_25.iter().any(|k| low.contains(k)) {
        score -= 25;
    }
    if LOW_15.iter().any(|k| low.contains(k)) {
        score -= 15;
    }
    score.clamp(0, 100) < 30
}

fn parse_payload(raw: &[u8]) -> Option<Map<String, Value>> {
    if raw.is_empty() {
        return Some(Map::new());
    }
    match serde_json::from_slice::<Value>(raw) {
        Ok(Value::Object(m)) => Some(m),
        _ => None,
    }
}

/// Events the fast path otherwise hands to Python: true when the Python handler provably
/// emits nothing and changes no state, so skipping it is indistinguishable.
fn slow_event_is_noop(event: &str, host: &str, payload: &Map<String, Value>, cfg: &Config) -> bool {
    match event {
        // handle_pre_invocation for Claude/Codex pops a queued loop-guard nudge and adds
        // autopilot context (index-ready, plain-language intent hint, context fill).
        "UserPromptSubmit" if host != "antigravity" => {
            gates::prompt_submit_is_noop(&session_id(payload, host), payload, cfg)
        }
        // handle_stop starts the lease-debounced background fold; on Stop it may also
        // show the milestone-throttled session savings line.
        "StopFailure" | "SessionEnd" if host == "claude" => {
            cfg.shadow || gates::rollup_lease_held(host)
        }
        "Stop" if host == "claude" => {
            cfg.shadow
                || (gates::rollup_lease_held(host)
                    && gates::stop_summary_is_noop(&session_id(payload, host), payload, cfg))
        }
        _ => false,
    }
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
        let args = tc.get("args").and_then(|v| v.as_object()).unwrap_or(&empty);

        if matches!(tool_name, "read_file" | "view_file") {
            if gates::contextignore_present(&gates::workspace(payload)) {
                return DispatchResult::Delegate;
            }
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
                let srv = args
                    .get("ServerName")
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
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

        if tool_name == "grep_search" {
            if gates::grep_pre_is_noop(host, args, cfg) {
                return DispatchResult::Done(None);
            }
            return DispatchResult::Delegate;
        }
        if tool_name == "run_command" {
            return DispatchResult::Delegate;
        }

        if !cfg.shadow {
            if tool_name == "invoke_subagent" {
                let type_name = args
                    .get("TypeName")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_ascii_lowercase();
                if !matches!(type_name.as_str(), "browser" | "image-generator") {
                    let req_model = args
                        .get("Model")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim()
                        .to_ascii_lowercase();
                    if let Some(prompt) = args.get("Prompt").and_then(|v| v.as_str()) {
                        if !prompt.trim().is_empty() && !prompt.contains("[Terser Subagent]") {
                            let updated = format!("{}\n\n{SUBAGENT_BRIEF_AG}", prompt.trim_end());
                            let mut overwrite = Map::new();
                            overwrite.insert("Prompt".into(), Value::String(updated));
                            if cfg.subagent_model_routing
                                && matches!(type_name.as_str(), "research" | "explore" | "general-purpose" | "")
                                && matches!(req_model.as_str(), "" | "inherit")
                            {
                                return DispatchResult::Delegate;
                            }
                            return DispatchResult::Done(Some(json!({
                                "decision": "allow",
                                "overwrite": Value::Object(overwrite),
                            })));
                        }
                    }
                }
            } else if tool_name == "swarm" {
                let action = args
                    .get("action")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .trim()
                    .to_ascii_lowercase();
                if action == "add" {
                    let req_model = args
                        .get("model")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .trim();
                    if cfg.subagent_model_routing && req_model.is_empty() {
                        return DispatchResult::Delegate;
                    }
                    if let Some(brief) = args.get("brief").and_then(|v| v.as_str()) {
                        if !brief.trim().is_empty() && !brief.contains("[Terser Subagent]") {
                            let updated = format!("{}\n\n{SUBAGENT_BRIEF_AG}", brief.trim_end());
                            return DispatchResult::Done(Some(json!({
                                "decision": "allow",
                                "overwrite": {"brief": updated},
                            })));
                        }
                    }
                }
            } else if tool_name == "define_subagent" {
                if let Some(sys_prompt) = args.get("SystemPrompt").and_then(|v| v.as_str()) {
                    if !sys_prompt.trim().is_empty() && !sys_prompt.contains("[Terser Subagent]") {
                        let updated = format!("{}\n\n{SUBAGENT_BRIEF_AG}", sys_prompt.trim_end());
                        return DispatchResult::Done(Some(json!({
                            "decision": "allow",
                            "overwrite": {"SystemPrompt": updated},
                        })));
                    }
                }
            }
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
        if gates::contextignore_present(&gates::workspace(payload)) {
            return DispatchResult::Delegate;
        }
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

    // Checkpoint before subagent fan-out: let Python start the detached continuity
    // capture, at most once per rollup lease window.
    if host == "claude"
        && matches!(tool_name, "Agent" | "Task")
        && cfg.fanout_checkpoint
        && !cfg.shadow
        && !gates::rollup_lease_held(host)
    {
        return DispatchResult::Delegate;
    }

    if host == "claude"
        && matches!(tool_name, "Agent" | "Task")
        && cfg.subagent_model_routing
        && !cfg.shadow
    {
        // Routing (classifier, write-intent veto, backoff, verify contract) lives in Python.
        return DispatchResult::Delegate;
    }

    if tool_name == "Grep" {
        if gates::grep_pre_is_noop(host, tool_input, cfg) {
            return DispatchResult::Done(None);
        }
        return DispatchResult::Delegate;
    }
    if is_shell_tool(tool_name) {
        if gates::bash_pre_is_noop(host, payload, tool_input, cfg) {
            return DispatchResult::Done(None);
        }
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
        if matches!(
            tool_name,
            "edit_file" | "write_file" | "replace_file_content" | "multi_replace_file_content"
        ) {
            return DispatchResult::Delegate;
        }
        let args = tc.get("args").and_then(|v| v.as_object()).unwrap_or(&empty);
        if !cfg.shadow && (tool_name == "call_mcp_tool" || tool_name.starts_with("mcp_")) {
            let srv = args
                .get("ServerName")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let srv_low = srv.to_ascii_lowercase();
            let tn_low = tool_name.to_ascii_lowercase();
            if srv_low.contains("terser-index")
                || srv_low.contains("terser_index")
                || tn_low.contains("terser")
            {
                return DispatchResult::Delegate;
            }
            let is_err_mcp = payload
                .get("error")
                .and_then(|v| v.as_str())
                .map(|s| !s.trim().is_empty())
                .unwrap_or(false);
            if !is_err_mcp {
                let (disp, inner_args) = if tool_name == "call_mcp_tool" {
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
                if let Some(resp_val) = payload
                    .get("response")
                    .or_else(|| payload.get("result"))
                    .or_else(|| payload.get("toolResponse"))
                {
                    let resp_len = resp_val
                        .as_str()
                        .map(str::len)
                        .unwrap_or_else(|| resp_val.to_string().len());
                    read_funnel::record_mcp_response_size(&sid, &disp, &inner_args, resp_len);
                }
            }
        }
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
    // Low-cost routed subagents carry the verify contract; Python checks their results.
    if matches!(tool_name, "Agent" | "Task")
        && payload
            .get("tool_input")
            .and_then(|v| v.get("prompt"))
            .and_then(|v| v.as_str())
            .is_some_and(|p| p.contains("[Terser Verify]"))
    {
        return DispatchResult::Delegate;
    }

    let empty = Map::new();
    let tool_input = payload
        .get("tool_input")
        .and_then(|v| v.as_object())
        .unwrap_or(&empty);
    let tool_resp = payload.get("tool_response");

    // Handle Bash/Shell output compression natively in Rust for non-JSON <=16KB outputs (~10ms vs ~140ms Python);
    // delegate JSON/NDJSON or >16KB outputs (SmartCrusher + SQLite FTS5 archival) to Python.
    let mut native_updated_resp: Option<Value> = None;
    if matches!(host, "claude" | "codex") && !cfg.shadow {
        if is_shell_tool(tool_name)
            && cfg.bash_compress
            && gates::command_filters_present(&gates::workspace(payload))
        {
            return DispatchResult::Delegate;
        }
        if is_shell_tool(tool_name) && cfg.bash_compress {
            if let Some(obj) = tool_resp.and_then(|v| v.as_object()) {
                let stdout_s = obj.get("stdout").and_then(|v| v.as_str()).unwrap_or("");
                let stderr_s = obj.get("stderr").and_then(|v| v.as_str()).unwrap_or("");
                let try_native_json = |s: &str| -> Result<Option<String>, ()> {
                    if s.len() <= 300 {
                        return Ok(None);
                    }
                    let t = s.trim_start();
                    if t.starts_with('{') || t.starts_with('[') {
                        if s.len() > 65536 {
                            return Err(());
                        }
                        if let Some(crushed) = compress::crush_json_lossless(s) {
                            return Ok(Some(crushed));
                        }
                        // Delegate NDJSON, >80-item arrays, or complex nested envelopes to Python SmartCrusher.
                        return Err(());
                    }
                    if s.len() > 16384 {
                        return Err(());
                    }
                    Ok(None)
                };
                let Ok(stdout_json) = try_native_json(stdout_s) else {
                    return DispatchResult::Delegate;
                };
                let Ok(stderr_json) = try_native_json(stderr_s) else {
                    return DispatchResult::Delegate;
                };
                if stdout_s.len() > 300 || stderr_s.len() > 300 {
                    let cmd = tool_input
                        .get("command")
                        .or_else(|| tool_input.get("cmd"))
                        .and_then(|v| v.as_str())
                        .unwrap_or("");
                    let ws_opt = payload
                        .get("cwd")
                        .and_then(|v| v.as_str())
                        .filter(|s| !s.is_empty());
                    let rc = if is_failure_event
                        || payload
                            .get("error")
                            .map(|v| !v.is_null() && v.as_str() != Some(""))
                            .unwrap_or(false)
                    {
                        1
                    } else {
                        0
                    };
                    let mut updated_obj = obj.clone();
                    let mut changed = false;
                    if stdout_s.len() > 300 {
                        let mut comp = stdout_json
                            .unwrap_or_else(|| compress::compress_output_with_ws(stdout_s, cmd, ws_opt));
                        let comp_len_before_delta = comp.len();
                        let mut delta_used = false;
                        if is_delta_eligible_cmd(cmd) {
                            let (delta_out, applied) =
                                apply_native_cmd_delta(&sid, cmd, &comp, rc, host);
                            comp = delta_out;
                            delta_used = applied;
                        }
                        if comp.len() < (stdout_s.len() * 95) / 100
                            && stdout_s.len().saturating_sub(comp.len()) >= 40
                        {
                            if !delta_used && comp_len_before_delta < stdout_s.len() {
                                record_savings(
                                    "output_compress",
                                    stdout_s.len(),
                                    comp_len_before_delta,
                                    host,
                                    &cmd.chars().take(60).collect::<String>(),
                                );
                            }
                            updated_obj.insert("stdout".into(), Value::String(comp));
                            changed = true;
                        }
                    }
                    if stderr_s.len() > 300 {
                        let comp_err = stderr_json
                            .unwrap_or_else(|| compress::compress_output_with_ws(stderr_s, cmd, ws_opt));
                        if comp_err.len() < (stderr_s.len() * 95) / 100
                            && stderr_s.len().saturating_sub(comp_err.len()) >= 40
                        {
                            record_savings(
                                "output_compress",
                                stderr_s.len(),
                                comp_err.len(),
                                host,
                                &cmd.chars().take(60).collect::<String>(),
                            );
                            updated_obj.insert("stderr".into(), Value::String(comp_err));
                            changed = true;
                        }
                    }
                    if changed {
                        native_updated_resp = Some(Value::Object(updated_obj));
                    }
                }
            }
        }
        if host == "claude" && tool_name == "WebFetch" {
            let approx_len = tool_resp
                .map(|v| {
                    v.as_str()
                        .map(str::len)
                        .unwrap_or_else(|| v.to_string().len())
                })
                .unwrap_or(0);
            if approx_len > 800 {
                return DispatchResult::Delegate;
            }
        }
        if tool_name.starts_with("mcp__") {
            let tn_low = tool_name.to_ascii_lowercase();
            if tn_low.contains("terser-index") || tn_low.contains("terser_index") {
                return DispatchResult::Delegate;
            }
            let approx_len = tool_resp
                .map(|v| {
                    v.as_str()
                        .map(str::len)
                        .unwrap_or_else(|| v.to_string().len())
                })
                .unwrap_or(0);
            if (cfg.archive_large_outputs && approx_len > cfg.archive_threshold_bytes)
                || (cfg.enhanced_compression && approx_len > 1500)
            {
                return DispatchResult::Delegate;
            }
            if approx_len > 0 && !is_failure_event {
                read_funnel::record_mcp_response_size(
                    &sid,
                    tool_name,
                    &Value::Object(tool_input.clone()),
                    approx_len,
                );
            }
        }
        if matches!(tool_name, "Glob" | "Grep") && cfg.archive_large_outputs {
            let approx_len = tool_resp.map(|v| v.to_string().len()).unwrap_or(0);
            if approx_len > 600 {
                return DispatchResult::Delegate;
            }
        }
    }

    let is_err = is_failure_event
        || payload
            .get("error")
            .map(|v| !v.is_null() && v.as_str() != Some(""))
            .unwrap_or(false);

    let nudge = loop_guard::record_tool_event(&sid, tool_name, tool_input, is_err, cfg);
    if native_updated_resp.is_some() || nudge.is_some() {
        let ev_name = if is_failure_event {
            "PostToolUseFailure"
        } else {
            "PostToolUse"
        };
        let mut hso = Map::new();
        hso.insert("hookEventName".into(), Value::String(ev_name.into()));
        if let Some(upd) = native_updated_resp {
            hso.insert("updatedToolOutput".into(), upd);
        }
        if let Some(msg) = nudge {
            hso.insert("additionalContext".into(), Value::String(msg));
        }
        return DispatchResult::Done(Some(json!({ "hookSpecificOutput": Value::Object(hso) })));
    }
    DispatchResult::Done(None)
}

fn handle_subagent_start(cfg: &Config) -> DispatchResult {
    if cfg.shadow {
        return DispatchResult::Done(None);
    }
    DispatchResult::Done(Some(json!({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": SUBAGENT_BRIEF_CC,
        }
    })))
}

fn handle_subagent_stop(host: &str, payload: &Map<String, Value>) -> DispatchResult {
    let sid = session_id(payload, host);
    if sid.contains("--") {
        let fname = format!("{}.json", safe_sid_filename(&sid));
        let home = state_home();
        for dir in ["read-cache", "loop-state", "cmd-delta"] {
            let _ = fs::remove_file(home.join(dir).join(&fname));
        }
    }
    DispatchResult::Done(None)
}

fn sha256_str(text: &str) -> String {
    let mut h = Sha256::new();
    h.update(text.as_bytes());
    let d = h.finalize();
    let mut out = String::with_capacity(64);
    for b in d {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

fn is_volatile_duration_unit(rest: &str) -> Option<usize> {
    let bytes = rest.as_bytes();
    let mut i = 0;
    while i < bytes.len() && bytes[i] == b' ' {
        i += 1;
    }
    let tail = &rest[i..];
    for unit in ["seconds", "secs", "sec", "ms", "us", "ns", "µs", "s"] {
        if tail.len() >= unit.len() && tail[..unit.len()].eq_ignore_ascii_case(unit) {
            let after = i + unit.len();
            let boundary_ok =
                after >= rest.len() || !rest.as_bytes()[after].is_ascii_alphanumeric();
            if boundary_ok {
                return Some(after);
            }
        }
    }
    None
}

fn normalize_volatile_for_delta(text: &str) -> String {
    // Normalize ONLY volatile timestamps, durations, and PIDs — never test counts or line numbers!
    let bytes = text.as_bytes();
    let n = bytes.len();
    let mut out = String::with_capacity(n);
    let mut i = 0;

    while i < n {
        // pid=1234 / pid: 1234 / pid 1234
        if i + 4 < n
            && (i == 0 || !bytes[i - 1].is_ascii_alphanumeric())
            && text[i..i + 3].eq_ignore_ascii_case("pid")
            && matches!(bytes[i + 3], b'=' | b':' | b' ')
        {
            let mut j = i + 4;
            while j < n && matches!(bytes[j], b'=' | b':' | b' ') {
                j += 1;
            }
            let dstart = j;
            while j < n && bytes[j].is_ascii_digit() {
                j += 1;
            }
            if j > dstart && (j == n || !bytes[j].is_ascii_alphanumeric()) {
                out.push_str("<T>");
                i = j;
                continue;
            }
        }

        // ISO date-time YYYY-MM-DD[T ]HH:MM:SS(.fff)?Z?
        if i + 19 <= n
            && (i == 0 || !bytes[i - 1].is_ascii_alphanumeric())
            && bytes[i..i + 4].iter().all(|b| b.is_ascii_digit())
            && bytes[i + 4] == b'-'
            && bytes[i + 5..i + 7].iter().all(|b| b.is_ascii_digit())
            && bytes[i + 7] == b'-'
            && bytes[i + 8..i + 10].iter().all(|b| b.is_ascii_digit())
            && matches!(bytes[i + 10], b'T' | b' ')
            && bytes[i + 11..i + 13].iter().all(|b| b.is_ascii_digit())
            && bytes[i + 13] == b':'
            && bytes[i + 14..i + 16].iter().all(|b| b.is_ascii_digit())
            && bytes[i + 16] == b':'
            && bytes[i + 17..i + 19].iter().all(|b| b.is_ascii_digit())
        {
            let mut j = i + 19;
            if j < n && bytes[j] == b'.' {
                j += 1;
                while j < n && bytes[j].is_ascii_digit() {
                    j += 1;
                }
            }
            if j < n && matches!(bytes[j], b'Z' | b'z') {
                j += 1;
            }
            out.push_str("<T>");
            i = j;
            continue;
        }

        // Time HH:MM:SS(.fff)?
        if i + 8 <= n
            && (i == 0 || !bytes[i - 1].is_ascii_alphanumeric())
            && bytes[i..i + 2].iter().all(|b| b.is_ascii_digit())
            && bytes[i + 2] == b':'
            && bytes[i + 3..i + 5].iter().all(|b| b.is_ascii_digit())
            && bytes[i + 5] == b':'
            && bytes[i + 6..i + 8].iter().all(|b| b.is_ascii_digit())
        {
            let mut j = i + 8;
            if j < n && bytes[j] == b'.' {
                j += 1;
                while j < n && bytes[j].is_ascii_digit() {
                    j += 1;
                }
            }
            out.push_str("<T>");
            i = j;
            continue;
        }

        // Numeric duration: 12ms, 1.45s, 200 us, 3.2 seconds
        if bytes[i].is_ascii_digit() && (i == 0 || !bytes[i - 1].is_ascii_alphanumeric()) {
            let mut j = i;
            while j < n && bytes[j].is_ascii_digit() {
                j += 1;
            }
            if j < n && bytes[j] == b'.' && j + 1 < n && bytes[j + 1].is_ascii_digit() {
                j += 1;
                while j < n && bytes[j].is_ascii_digit() {
                    j += 1;
                }
            }
            if let Some(unit_len) = is_volatile_duration_unit(&text[j..]) {
                out.push_str("<T>");
                i = j + unit_len;
                continue;
            }
        }

        let ch = text[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn compact_line_diff(prev_text: &str, cur_text: &str) -> Option<String> {
    let prev_lines: Vec<&str> = prev_text.lines().collect();
    let cur_lines: Vec<&str> = cur_text.lines().collect();
    if cur_lines.len() < 10 || prev_lines.len() > 600 || cur_lines.len() > 600 {
        return None;
    }
    // Trim common prefix and suffix for O(changed^2) LCS
    let mut pref = 0usize;
    while pref < prev_lines.len() && pref < cur_lines.len() && prev_lines[pref] == cur_lines[pref] {
        pref += 1;
    }
    let mut suff = 0usize;
    while suff < (prev_lines.len() - pref)
        && suff < (cur_lines.len() - pref)
        && prev_lines[prev_lines.len() - 1 - suff] == cur_lines[cur_lines.len() - 1 - suff]
    {
        suff += 1;
    }
    let a = &prev_lines[pref..prev_lines.len() - suff];
    let b = &cur_lines[pref..cur_lines.len() - suff];
    if a.is_empty() && b.is_empty() {
        return None;
    }
    if a.len() + b.len() > 160 {
        return None;
    }

    // Standard LCS table on changed middle region
    let m = a.len();
    let n = b.len();
    let mut dp = vec![vec![0u16; n + 1]; m + 1];
    for i in (0..m).rev() {
        for j in (0..n).rev() {
            dp[i][j] = if a[i] == b[j] {
                dp[i + 1][j + 1] + 1
            } else {
                dp[i + 1][j].max(dp[i][j + 1])
            };
        }
    }

    let mut diff_lines: Vec<String> = Vec::new();
    diff_lines.push("--- prev_run".to_string());
    diff_lines.push("+++ this_run".to_string());
    if pref > 0 {
        diff_lines.push(format!(
            "@@ -{},{} +{},{} @@",
            pref,
            a.len() + 1,
            pref,
            b.len() + 1
        ));
        diff_lines.push(format!(" {}", prev_lines[pref - 1]));
    } else {
        diff_lines.push(format!("@@ -1,{} +1,{} @@", a.len(), b.len()));
    }
    let mut i = 0;
    let mut j = 0;
    while i < m && j < n {
        if a[i] == b[j] {
            diff_lines.push(format!(" {}", a[i]));
            i += 1;
            j += 1;
        } else if dp[i + 1][j] >= dp[i][j + 1] {
            diff_lines.push(format!("-{}", a[i]));
            i += 1;
        } else {
            diff_lines.push(format!("+{}", b[j]));
            j += 1;
        }
    }
    while i < m {
        diff_lines.push(format!("-{}", a[i]));
        i += 1;
    }
    while j < n {
        diff_lines.push(format!("+{}", b[j]));
        j += 1;
    }
    if suff > 0 {
        diff_lines.push(format!(" {}", cur_lines[cur_lines.len() - suff]));
    }

    if diff_lines.len() > 45 || (diff_lines.len() as f64 / cur_lines.len() as f64) > 0.45 {
        return None;
    }
    Some(diff_lines.join("\n"))
}

fn apply_native_cmd_delta(
    sid: &str,
    cmd_str: &str,
    output: &str,
    rc: i32,
    host: &str,
) -> (String, bool) {
    if sid.is_empty() || output.len() < 300 {
        return (output.to_string(), false);
    }
    let norm_cmd: String = cmd_str.split_whitespace().collect::<Vec<_>>().join(" ");
    let key = sha256_str(&norm_cmd)[..20].to_string();
    let cur_sha = sha256_str(output);
    let cur_norm_sha = sha256_str(&normalize_volatile_for_delta(output));
    let now = now_secs();

    let path = state_home()
        .join("cmd-delta")
        .join(format!("{}.json", safe_sid_filename(sid)));
    let mut store: Map<String, Value> = fs::read_to_string(&path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();

    let mut result = output.to_string();
    let mut applied = false;
    if let Some(prev) = store.get(&key).and_then(|v| v.as_object()) {
        let prev_ts = prev.get("ts").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let prev_rc = prev.get("rc").and_then(|v| v.as_i64()).unwrap_or(-999) as i32;
        let prev_sha = prev.get("sha").and_then(|v| v.as_str()).unwrap_or("");
        let prev_norm = prev
            .get("norm_sha")
            .and_then(|v| v.as_str())
            .unwrap_or(prev_sha);
        if (now - prev_ts) <= 1800.0 {
            let elapsed = ((now - prev_ts) as i64).max(1);
            let tail = output
                .lines()
                .rev()
                .map(str::trim)
                .find(|l| !l.is_empty())
                .unwrap_or("");
            let short_cmd: String = cmd_str.chars().take(50).collect();

            if prev_rc == rc && (prev_sha == cur_sha || prev_norm == cur_norm_sha) {
                let line_count = output.lines().count();
                let tail_part = if tail.is_empty() {
                    String::new()
                } else {
                    let short_tail: String = tail.chars().take(160).collect();
                    format!(" Last line: `{short_tail}`")
                };
                result = format!(
                    "[Terser Delta] Identical output to previous `{short_cmd}` run ({elapsed}s ago, exit={rc}, {line_count} lines).{tail_part}"
                );
                record_savings("cmd_delta", output.len(), result.len(), host, &short_cmd);
                applied = true;
            } else if let Some(prev_text) = prev.get("text").and_then(|v| v.as_str()) {
                if let Some(diff_body) = compact_line_diff(prev_text, output) {
                    let mut candidate = format!(
                        "[Terser Delta vs prior run ({elapsed}s ago, exit {prev_rc}->{rc})]\n```diff\n{diff_body}\n```"
                    );
                    if !tail.is_empty() && !diff_body.contains(tail) {
                        let short_tail: String = tail.chars().take(160).collect();
                        candidate.push_str(&format!("\nSummary: {short_tail}"));
                    }
                    if candidate.len() < (output.len() * 75) / 100 {
                        record_savings(
                            "cmd_delta",
                            output.len(),
                            candidate.len(),
                            host,
                            &short_cmd,
                        );
                        result = candidate;
                        applied = true;
                    }
                }
            }
        }
    }

    let snap: String = output.chars().take(32768).collect();
    store.insert(
        key,
        json!({
            "sha": cur_sha,
            "norm_sha": cur_norm_sha,
            "rc": rc,
            "ts": now,
            "text": snap,
        }),
    );
    const MAX_CMD_DELTA_ENTRIES: usize = 40;
    if store.len() > MAX_CMD_DELTA_ENTRIES {
        let mut entries: Vec<(String, f64)> = store
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    v.get("ts").and_then(|t| t.as_f64()).unwrap_or(0.0),
                )
            })
            .collect();
        entries.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        let keep: std::collections::HashSet<String> = entries
            .into_iter()
            .take(MAX_CMD_DELTA_ENTRIES)
            .map(|(k, _)| k)
            .collect();
        store.retain(|k, _| keep.contains(k));
    }
    atomic_write_json(&path, &Value::Object(store));
    (result, applied)
}

/// Shell tools whose commands get FormRepair and whose stdout/stderr get lossless compression.
fn is_shell_tool(tool_name: &str) -> bool {
    matches!(
        tool_name,
        "Bash" | "bash" | "shell" | "exec_command" | "PowerShell" | "powershell"
    )
}

fn is_delta_eligible_cmd(cmd: &str) -> bool {
    let low = cmd.trim().to_ascii_lowercase();
    let toks: Vec<&str> = low.split_whitespace().collect();
    if toks.is_empty() {
        return false;
    }
    let (head, rest) =
        if matches!(toks[0], "python" | "python3") && toks.len() >= 3 && toks[1] == "-m" {
            (toks[2], &toks[3..])
        } else if toks[0] == "uv" && toks.len() >= 3 && toks[1] == "run" {
            (toks[2], &toks[3..])
        } else if toks[0] == "npx" && toks.len() >= 2 {
            (toks[1], &toks[2..])
        } else {
            (toks[0], &toks[1..])
        };
    let sub = rest.first().copied().unwrap_or("");
    let sub2 = rest.get(1).copied().unwrap_or("");
    matches!(
        head,
        "pytest"
            | "unittest"
            | "jest"
            | "vitest"
            | "mocha"
            | "tsc"
            | "mypy"
            | "pyright"
            | "ruff"
            | "eslint"
            | "biome"
            | "oxlint"
            | "flake8"
    ) || (matches!(head, "npm" | "pnpm" | "yarn")
        && (matches!(sub, "test" | "lint" | "build" | "check")
            || (sub == "run" && matches!(sub2, "test" | "lint" | "build" | "check" | "typecheck"))))
        || (head == "cargo" && matches!(sub, "test" | "check" | "build" | "clippy"))
        || (head == "go" && sub == "test")
        || (head == "git" && matches!(sub, "status" | "diff" | "log"))
        || (head == "dotnet" && matches!(sub, "test" | "build"))
        || (matches!(head, "gradle" | "gradlew" | "mvn" | "mvnw")
            && matches!(sub, "test" | "build" | "check"))
        || (head == "gh" && ((sub == "pr" && sub2 == "checks") || (sub == "run" && sub2 == "view")))
        || (matches!(head, "terraform" | "tofu") && sub == "plan")
        || (head == "kubectl" && sub == "get")
        || (head == "docker" && sub == "ps")
}

fn redact_basic_secrets(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for line in text.split_inclusive('\n') {
        let lower = line.to_ascii_lowercase();
        let has_secret = line.contains("sk-")
            || line.contains("ghp_")
            || line.contains("gho_")
            || line.contains("ghs_")
            || line.contains("github_pat_")
            || line.contains("AKIA")
            || line.contains("AIza")
            || line.contains("xoxb-")
            || line.contains("xoxp-")
            || lower.contains("-----begin ") && lower.contains("private key-----");
        if has_secret {
            out.push_str("[REDACTED LINE]\n");
        } else {
            out.push_str(line);
        }
    }
    out
}

/// Retrieval pointer for an archived output.
///
/// Native exec only runs when a Terser hook rewrote an agent command, so this
/// points at the always-available MCP `order` tool instead of the `terse` CLI,
/// which is usually not on the agent's `PATH`.
fn archived_hint(aid: &str) -> String {
    format!(
        "\n[Terser] Full output archived (`order(action=\"search_sandbox\", args={{\"archive_id\": \"{aid}\", \"query\": \"<terms>\"}})` or `order(action=\"expand_sandbox\", args={{\"archive_id\": \"{aid}\", \"start_line\": 1, \"end_line\": 100}})`)."
    )
}

fn archive_raw_sidecar(raw: &str) -> Option<(String, String)> {
    let safe = redact_basic_secrets(raw);
    let id = sha256_str(&safe)[..12].to_string();
    let dir = state_home().join("raw-archives");
    fs::create_dir_all(&dir).ok()?;
    // Prune archives older than 7 days or beyond 50 files
    if let Ok(rd) = fs::read_dir(&dir) {
        let cutoff = now_secs() - 7.0 * 86400.0;
        let mut kept: Vec<(f64, std::path::PathBuf)> = Vec::new();
        for entry in rd.flatten() {
            let p = entry.path();
            if !p.is_file() {
                continue;
            }
            let mtime = entry
                .metadata()
                .ok()
                .and_then(|m| m.modified().ok())
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            if mtime < cutoff {
                let _ = fs::remove_file(&p);
            } else {
                kept.push((mtime, p));
            }
        }
        if kept.len() > 50 {
            kept.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
            let excess = kept.len() - 50;
            for (_, p) in kept.into_iter().take(excess) {
                let _ = fs::remove_file(p);
            }
        }
    }
    let path = dir.join(format!("{id}.txt"));
    if !path.is_file() {
        fs::write(&path, safe.as_bytes()).ok()?;
    }
    Some((id, path.to_string_lossy().into_owned()))
}

fn run_exec_mode(raw_args: &[String]) -> i32 {
    // Syntax: terser-fast exec [--host <h>] [--sid <sid>] -- <prog> [args...]
    let mut host = "antigravity".to_string();
    let mut sid = String::new();
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
        if raw_args[idx] == "--sid" && idx + 1 < raw_args.len() {
            sid = raw_args[idx + 1].clone();
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
    if host == "codex" {
        if let (Some(hook), Some((python, prefix))) = (resolve_hook_script(None), find_python_bin())
        {
            let script = std::path::Path::new(&hook).with_file_name("output_compressor.py");
            if script.is_file() {
                let mut wrapper = Command::new(python);
                wrapper
                    .args(prefix)
                    .args(["-E", "-s", "-X", "utf8"])
                    .arg(script);
                wrapper.arg("--codex-exec");
                if !sid.is_empty() {
                    wrapper.args(["--sid", &sid]);
                }
                if let Ok(status) = wrapper.arg("--").args(&raw_args[idx..]).status() {
                    return status.code().unwrap_or(1);
                }
            }
        }
        // Native-only installs can still run the original argv without filtering.
        return match Command::new(prog).args(prog_args).status() {
            Ok(status) => status.code().unwrap_or(1),
            Err(err) => {
                let _ = writeln!(io::stderr(), "{prog}: {err}");
                if err.kind() == io::ErrorKind::NotFound {
                    127
                } else {
                    126
                }
            }
        };
    }
    let cmd_str = raw_args[idx..].join(" ");

    let mut cmd = Command::new(prog);
    cmd.args(prog_args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let output = match cmd.output() {
        Ok(o) => o,
        Err(e) => {
            let _ = writeln!(io::stderr(), "[Terser] Failed to execute {prog}: {e}");
            return 127;
        }
    };

    let code = output.status.code().unwrap_or(1);
    let stdout_str = String::from_utf8_lossy(&output.stdout);
    let stderr_str = String::from_utf8_lossy(&output.stderr);
    let cwd_str = env::current_dir()
        .ok()
        .map(|p| p.to_string_lossy().into_owned());
    let ws_opt = cwd_str.as_deref();

    // On non-zero exit: preserve 100% of errors, warnings, tracebacks, diffs, and exit code,
    // while still stripping strictly lossless ANSI/spinner/build-progress/note/framework-frame noise.
    if code != 0 {
        if stdout_str.len() > 300 {
            let cleaned = compress::strip_ansi_and_progress(&stdout_str);
            let cleaned = compress::elide_lockfile_diffs(&cleaned);
            let cleaned = compress::compact_diff_headers(&cleaned);
            let cleaned = compress::fold_framework_stack_frames(&cleaned, 3);
            let cleaned = compress::dedup_compiler_notes(&cleaned);
            let cleaned = compress::apply_stream_filters(&cleaned, &cmd_str);
            let cleaned = compress::elide_workspace_paths(&cleaned, ws_opt);
            let mut served = if cleaned.len() < (stdout_str.len() * 90) / 100 {
                cleaned
            } else {
                stdout_str.to_string()
            };
            let cap = compress::window_limit(&cmd_str, 16384);
            if served.len() > cap {
                let raw_arch = archive_raw_sidecar(&stdout_str);
                served = compress::error_pinned_window(&served, cap);
                if let Some((aid, _rp)) = raw_arch {
                    served.push_str(&archived_hint(&aid));
                }
            }
            let mut delta_used = false;
            if !sid.is_empty() {
                let (delta_out, applied) =
                    apply_native_cmd_delta(&sid, &cmd_str, &served, code, &host);
                served = delta_out;
                delta_used = applied;
            }
            if !delta_used && served.len() < output.stdout.len() {
                record_savings(
                    "bash_compress",
                    output.stdout.len(),
                    served.len(),
                    &host,
                    prog,
                );
            }
            let _ = io::stdout().write_all(served.as_bytes());
            if !served.ends_with('\n') {
                let _ = io::stdout().write_all(b"\n");
            }
        } else {
            let _ = io::stdout().write_all(&output.stdout);
        }
        if stderr_str.len() > 300 {
            let cleaned_err = compress::strip_ansi_and_progress(&stderr_str);
            let cleaned_err = compress::fold_framework_stack_frames(&cleaned_err, 3);
            let cleaned_err = compress::dedup_compiler_notes(&cleaned_err);
            let cleaned_err = compress::apply_stream_filters(&cleaned_err, &cmd_str);
            let cleaned_err = compress::elide_workspace_paths(&cleaned_err, ws_opt);
            let mut served_err = if cleaned_err.len() < (stderr_str.len() * 90) / 100 {
                cleaned_err
            } else {
                stderr_str.into_owned()
            };
            if served_err.len() > 16384 {
                served_err = compress::error_pinned_window(&served_err, 16384);
            }
            let _ = io::stderr().write_all(served_err.as_bytes());
            if !served_err.ends_with('\n') {
                let _ = io::stderr().write_all(b"\n");
            }
        } else if !stderr_str.is_empty() {
            let _ = io::stderr().write_all(&output.stderr);
        }
        return code;
    }

    if stdout_str.len() > 300 {
        let mut compressed = compress::compress_output_with_ws(&stdout_str, &cmd_str, ws_opt);
        if compressed.len() < (stdout_str.len() * 85) / 100 {
            record_savings(
                "bash_compress",
                stdout_str.len(),
                compressed.len(),
                &host,
                prog,
            );
        } else {
            compressed = stdout_str.to_string();
        }
        let cap = compress::window_limit(&cmd_str, 12000);
        if compressed.len() > cap {
            let raw_arch = archive_raw_sidecar(&stdout_str);
            compressed = compress::error_pinned_window(&compressed, cap);
            if let Some((aid, _rp)) = raw_arch {
                compressed.push_str(&archived_hint(&aid));
            }
        }
        if !sid.is_empty() {
            let (delta_out, _applied) =
                apply_native_cmd_delta(&sid, &cmd_str, &compressed, 0, &host);
            compressed = delta_out;
        }
        let _ = io::stdout().write_all(compressed.as_bytes());
        if !compressed.ends_with('\n') {
            let _ = io::stdout().write_all(b"\n");
        }
    } else {
        let _ = io::stdout().write_all(&output.stdout);
    }

    if stderr_str.len() > 300 {
        let cleaned_err = compress::compress_output_with_ws(&stderr_str, &cmd_str, ws_opt);
        let served_err = if cleaned_err.len() < (stderr_str.len() * 85) / 100 {
            cleaned_err
        } else {
            stderr_str.into_owned()
        };
        let _ = io::stderr().write_all(served_err.as_bytes());
        if !served_err.ends_with('\n') {
            let _ = io::stderr().write_all(b"\n");
        }
    } else if !stderr_str.is_empty() {
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
    let _ = io::stdin()
        .take((STDIN_MAX_BYTES + 1) as u64)
        .read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf);
    let out = compress::compress_output(&text, &cmd_label);
    let _ = io::stdout().write_all(out.as_bytes());
    0
}

fn main() {
    let all_args: Vec<String> = env::args().skip(1).collect();
    if let Some(first) = all_args.first().map(String::as_str) {
        if first == "--codex-hook-protocol" {
            println!("1");
            return;
        }
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

    // Fast-path only handles PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop
    if !matches!(
        args.event.as_str(),
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" | "SubagentStart" | "SubagentStop"
    ) {
        if let Some(payload) = parse_payload(&raw_stdin) {
            let host = detect_host(&payload, args.host.as_deref());
            if slow_event_is_noop(&args.event, &host, &payload, &cfg) {
                return;
            }
        }
        std::process::exit(delegate_to_python(&args, &raw_stdin, &cfg));
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
    // Keep stateful Codex decisions in the Python adapter.
    if host == "codex" {
        if args.event == "PreToolUse" {
            let tool = payload
                .get("tool_name")
                .and_then(Value::as_str)
                .unwrap_or("");
            let needs_python = match tool {
                "Bash" => cfg.codex_form_repair,
                tool if tool.starts_with("mcp__") => {
                    let explicit_reuse = payload
                        .get("tool_input")
                        .and_then(|input| input.pointer("/args/reuse_visible_result"))
                        .and_then(Value::as_bool)
                        == Some(true);
                    cfg.mcp_refetch_guard && (cfg.codex_mcp_refetch_guard || explicit_reuse)
                }
                _ => false,
            };
            if cfg.shadow || !needs_python {
                return;
            }
        }
        if args.event == "SubagentStart" {
            if let DispatchResult::Done(Some(value)) = handle_subagent_start(&cfg) {
                println!("{value}");
            }
            return;
        }
        let args = Args {
            host: Some(host),
            ..args
        };
        std::process::exit(delegate_to_python(&args, &raw_stdin, &cfg));
    }
    let outcome = match args.event.as_str() {
        "PreToolUse" => handle_pre_tool_use(&host, &payload, &cfg),
        "PostToolUse" => handle_post_tool_use(&host, &payload, &cfg, false),
        "PostToolUseFailure" => handle_post_tool_use(&host, &payload, &cfg, true),
        "SubagentStart" => handle_subagent_start(&cfg),
        "SubagentStop" => handle_subagent_stop(&host, &payload),
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
            std::process::exit(delegate_to_python(&args, &raw_stdin, &cfg));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn volatile_norm_preserves_counts_and_line_numbers() {
        let a = "2026-11-27 10:00:01 line 42: 1 failed, 9 passed in 1.23s (pid=1001)";
        let b = "2026-11-27 10:00:09 line 42: 1 failed, 9 passed in 2.45s (pid=2002)";
        let c = "2026-11-27 10:00:09 line 99: 9 failed, 1 passed in 2.45s (pid=2002)";
        assert_eq!(
            normalize_volatile_for_delta(a),
            normalize_volatile_for_delta(b)
        );
        assert_ne!(
            normalize_volatile_for_delta(a),
            normalize_volatile_for_delta(c)
        );
    }

    #[test]
    fn compact_line_diff_handles_small_test_change() {
        let prev = (1..=20)
            .map(|i| format!("test_{i} ok"))
            .collect::<Vec<_>>()
            .join("\n");
        let cur = (1..=20)
            .map(|i| {
                if i == 10 {
                    "test_10 FAILED".to_string()
                } else {
                    format!("test_{i} ok")
                }
            })
            .collect::<Vec<_>>()
            .join("\n");
        let diff = compact_line_diff(&prev, &cur).expect("expected small diff");
        assert!(diff.contains("-test_10 ok"));
        assert!(diff.contains("+test_10 FAILED"));
    }

    #[test]
    fn task_complexity_routing_matches_python() {
        assert!(is_low_complexity_task("find where foo is defined"));
        assert!(!is_low_complexity_task(
            "architect a distributed lock manager with deadlock detection"
        ));
    }

    #[test]
    fn delta_eligible_cmds_and_redaction_match_python() {
        assert!(is_delta_eligible_cmd("npm run lint"));
        assert!(is_delta_eligible_cmd("gh pr checks 42"));
        assert!(is_delta_eligible_cmd("dotnet test"));
        assert!(is_delta_eligible_cmd("python -m unittest"));
        assert!(!is_delta_eligible_cmd("rm -rf build"));
        let scrubbed = redact_basic_secrets("ok\ntoken=sk-1234567890abcdefghij\nend\n");
        assert!(scrubbed.contains("[REDACTED LINE]"));
        assert!(!scrubbed.contains("sk-1234567890"));
    }
}
