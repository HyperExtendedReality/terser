//! Exact "nothing to do" gates that let terser-fast answer hook events without starting
//! Python. Every gate either proves the Python handler would emit nothing (and change no
//! state), or returns `false` so the caller delegates as before. They never produce output
//! of their own, so the agent sees byte-identical results either way.

use crate::config::{now_secs, safe_sid_filename, state_home, valid_sid, Config};
use serde_json::{Map, Value};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

/// Mirrors `runtime/loop_guard.py::FORM_REPAIR_GATE_FILE` / `FORM_REPAIR_GATE_TTL_S`.
const FORM_REPAIR_GATE_FILE: &str = "form-repair-gate.json";
const FORM_REPAIR_GATE_TTL_S: f64 = 600.0;
/// Mirrors `runtime/unified_hook.py::_ROLLUP_LEASE_SECONDS`.
pub const ROLLUP_LEASE_SECONDS: f64 = 30.0;

/// Mirrors `unified_hook._workspace`.
pub fn workspace(payload: &Map<String, Value>) -> String {
    if let Some(Value::Array(items)) = payload.get("workspacePaths") {
        for item in items {
            if let Some(s) = item.as_str() {
                if !s.trim().is_empty() {
                    return s.trim().to_string();
                }
            }
        }
    }
    if let Some(s) = payload.get("cwd").and_then(Value::as_str) {
        if !s.trim().is_empty() {
            return s.trim().to_string();
        }
    }
    if let Ok(dir) = env::var("CLAUDE_PROJECT_DIR") {
        if !dir.is_empty() {
            return dir;
        }
    }
    env::current_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// True when a `.contextignore` exists for this workspace or globally. Reads then go to
/// Python, which owns the gitignore-style matching (`runtime/user_filters.py`).
pub fn contextignore_present(ws: &str) -> bool {
    (!ws.is_empty() && Path::new(ws).join(".contextignore").is_file())
        || state_home().join(".contextignore").is_file()
}

/// True when a `command-filters.toml` exists (user or project level). Shell output then
/// goes to Python, which applies the user's filters and exclusions.
pub fn command_filters_present(ws: &str) -> bool {
    (!ws.is_empty()
        && Path::new(ws)
            .join(".terser")
            .join("command-filters.toml")
            .is_file())
        || state_home().join("command-filters.toml").is_file()
}

/// `[mtime_ns, size]`, matching Python's `os.stat` (`st_mtime_ns`, `st_size`).
fn stat_sig(path: &str) -> Option<(u128, u64)> {
    let meta = fs::metadata(path).ok()?;
    let mtime = meta
        .modified()
        .ok()?
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_nanos();
    Some((mtime, meta.len()))
}

fn sig_matches(recorded: Option<&Value>, path: &str) -> bool {
    let actual = if path.is_empty() {
        None
    } else {
        stat_sig(path)
    };
    match (recorded, actual) {
        (None | Some(Value::Null), None) => true,
        (Some(Value::Array(pair)), Some((mtime, size))) if pair.len() == 2 => {
            pair[0].as_u64().map(u128::from) == Some(mtime) && pair[1].as_u64() == Some(size)
        }
        _ => false,
    }
}

fn env_str(key: &str) -> String {
    env::var(key).unwrap_or_default()
}

/// Claude Bash PreToolUse: true when `loop_guard.apply_form_repair` provably repairs nothing.
pub fn bash_pre_is_noop(
    host: &str,
    payload: &Map<String, Value>,
    tool_input: &Map<String, Value>,
    cfg: &Config,
) -> bool {
    if host != "claude" {
        return false;
    }
    let key = if tool_input.contains_key("command") {
        "command"
    } else {
        "cmd"
    };
    let cmd = match tool_input.get(key) {
        Some(Value::String(s)) => s,
        _ => return true, // Python only acts on a non-empty string command
    };
    if cmd.trim().is_empty() || !cfg.form_repair || cfg.shadow {
        return true;
    }
    let ws = workspace(payload);
    if ws.is_empty() {
        return true;
    }
    let Ok(text) = fs::read_to_string(state_home().join(FORM_REPAIR_GATE_FILE)) else {
        return false;
    };
    let Ok(Value::Object(gate)) = serde_json::from_str::<Value>(&text) else {
        return false;
    };
    let Some(Value::Object(entry)) = gate.get(&format!("{host}|{ws}")) else {
        return false;
    };
    let s = |k: &str| entry.get(k).and_then(Value::as_str).unwrap_or("");
    let fresh = entry
        .get("at")
        .and_then(Value::as_f64)
        .map(|at| (now_secs() - at) < FORM_REPAIR_GATE_TTL_S && at <= now_secs() + 5.0)
        .unwrap_or(false);
    fresh
        && entry.get("has").and_then(Value::as_bool) == Some(false)
        && s("te_home") == env_str("TOKENECONOMIZER_HOME")
        && s("te_repo") == env_str("TOKENECONOMIZER_REPO")
        && s("ceilings") == env_str("GIT_CEILING_DIRECTORIES")
        && s("home") == env_str("HOME")
        && !s("git_config").is_empty()
        && sig_matches(entry.get("facts_sig"), s("facts"))
        && sig_matches(entry.get("git_sig"), s("git_config"))
}

fn code_index_dir() -> PathBuf {
    let raw = env_str("TERSER_INDEX_PATH");
    let raw = raw.trim();
    if raw.is_empty() {
        return state_home().join("code-index");
    }
    if let Some(rest) = raw.strip_prefix("~/").or_else(|| raw.strip_prefix("~\\")) {
        let home = env::var("USERPROFILE")
            .or_else(|_| env::var("HOME"))
            .unwrap_or_default();
        return Path::new(&home).join(rest);
    }
    PathBuf::from(raw)
}

/// Claude/Antigravity Grep PreToolUse: true when `index_bridge.try_answer_large_grep` would
/// return None before touching the index (bad pattern, feature off, or no code index on disk).
pub fn grep_pre_is_noop(host: &str, tool_input: &Map<String, Value>, cfg: &Config) -> bool {
    if host == "codex" || !cfg.index_grep_answer || cfg.shadow {
        return true;
    }
    let raw_pat = tool_input
        .get("pattern")
        .or_else(|| tool_input.get("Query"))
        .or_else(|| tool_input.get("query"));
    let pattern = match raw_pat {
        Some(Value::String(s)) => s.trim().to_string(),
        Some(Value::Null) | None => String::new(),
        Some(_) => return false, // Python str()-coerces odd types; let it decide
    };
    if pattern.chars().count() < 3 || pattern.chars().any(|c| "*?[](){}^$\\|.+".contains(c)) {
        return true;
    }
    let dir = code_index_dir();
    let Ok(rd) = fs::read_dir(&dir) else {
        return true; // `idx_dir.is_dir()` is False
    };
    // Python only consults *.json metadata files; none means no indexed repo can match.
    !rd.flatten()
        .any(|e| e.file_name().to_string_lossy().ends_with(".json"))
}

/// Mirrors `unified_hook._session_flag_path`.
fn session_flags(sid: &str) -> Map<String, Value> {
    let safe: String = sid
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .take(80)
        .collect();
    let path = state_home()
        .join("session-flags")
        .join(format!("{safe}.json"));
    match fs::read_to_string(path).map(|t| serde_json::from_str::<Value>(&t)) {
        Ok(Ok(Value::Object(m))) => m,
        _ => Map::new(),
    }
}

fn flag_set(flags: &Map<String, Value>, key: &str) -> bool {
    match flags.get(key) {
        None | Some(Value::Null) | Some(Value::Bool(false)) => false,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(_) => true,
    }
}

/// Last `max_bytes` of a file as lines (first partial line dropped), like `autopilot._tail_lines`.
fn tail_lines(path: &Path, max_bytes: u64) -> Vec<String> {
    use std::io::{Read, Seek, SeekFrom};
    let Ok(mut fh) = fs::File::open(path) else {
        return Vec::new();
    };
    let size = fh.metadata().map(|m| m.len()).unwrap_or(0);
    let start = size.saturating_sub(max_bytes);
    if fh.seek(SeekFrom::Start(start)).is_err() {
        return Vec::new();
    }
    let mut buf = Vec::new();
    if fh.read_to_end(&mut buf).is_err() {
        return Vec::new();
    }
    let text = String::from_utf8_lossy(&buf);
    let mut lines: Vec<String> = text.lines().map(str::to_owned).collect();
    if start > 0 && !lines.is_empty() {
        lines.remove(0);
    }
    lines
}

fn as_u64(v: Option<&Value>) -> u64 {
    v.and_then(Value::as_u64).unwrap_or(0)
}

/// Mirrors `autopilot.context_fill`: latest context fill fraction from a transcript tail.
pub fn context_fill(transcript_path: &str) -> Option<f64> {
    if transcript_path.is_empty() {
        return None;
    }
    for line in tail_lines(Path::new(transcript_path), 256 * 1024)
        .iter()
        .rev()
    {
        if !line.contains("\"usage\"") && !line.contains("\"token_count\"") {
            continue;
        }
        let Ok(Value::Object(rec)) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if let Some(Value::Object(p)) = rec.get("payload") {
            if p.get("type").and_then(Value::as_str) == Some("token_count") {
                let Some(Value::Object(info)) = p.get("info") else {
                    continue;
                };
                let window = as_u64(info.get("model_context_window"));
                let used = info
                    .get("last_token_usage")
                    .map(|u| as_u64(u.get("input_tokens")))
                    .unwrap_or(0);
                if window > 0 && used > 0 {
                    return Some((used as f64 / window as f64).min(1.0));
                }
                continue;
            }
        }
        let Some(Value::Object(msg)) = rec.get("message") else {
            continue;
        };
        if rec
            .get("isSidechain")
            .is_some_and(|v| v.as_bool() == Some(true))
        {
            continue;
        }
        let Some(Value::Object(usage)) = msg.get("usage") else {
            continue;
        };
        let used: u64 = [
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ]
        .iter()
        .map(|k| as_u64(usage.get(*k)))
        .sum();
        if used > 0 {
            let model = msg
                .get("model")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_ascii_lowercase();
            let window = if model.contains("[1m]") {
                1_000_000.0
            } else {
                200_000.0
            };
            return Some((used as f64 / window).min(1.0));
        }
    }
    None
}

/// Conservative superset of `autopilot.intent_hint` triggers (false positives only cost a
/// Python delegation; the Python side makes the exact decision).
fn prompt_may_have_intent(prompt: &str) -> bool {
    let p = prompt.to_ascii_lowercase();
    if p.trim_start().starts_with('/') {
        return false;
    }
    p.contains("terse")
        || p.contains("omitted")
        || p.contains("truncated")
        || p.contains("elided")
        || (["full ", "whole ", "entire ", "complete ", "rest of the "]
            .iter()
            .any(|w| p.contains(w))
            && ["output", "log", "result"].iter().any(|w| p.contains(w)))
}

/// True when `unified_hook._autopilot_prompt_context` provably adds nothing.
fn autopilot_prompt_is_noop(sid: &str, payload: &Map<String, Value>, cfg: &Config) -> bool {
    if cfg.shadow {
        return true;
    }
    let flags = session_flags(sid);
    if matches!(flags.get("auto_index_pending"), Some(Value::String(s)) if !s.is_empty()) {
        return false;
    }
    // Post-compaction recall: Python restores matching logged events for a few prompts.
    if cfg.compact_recall && flag_set(&flags, "recall_pending") {
        return false;
    }
    if cfg.intent_hints {
        let prompt = payload.get("prompt").and_then(Value::as_str).unwrap_or("");
        if prompt_may_have_intent(prompt) {
            return false;
        }
    }
    if cfg.context_fill_nudges && !sid.is_empty() && !flag_set(&flags, "fill_85") {
        let tp = payload
            .get("transcript_path")
            .and_then(Value::as_str)
            .unwrap_or("");
        if let Some(fill) = context_fill(tp) {
            if fill >= 0.85 || (fill >= 0.70 && !flag_set(&flags, "fill_70")) {
                return false;
            }
        }
    }
    true
}

/// Claude/Codex UserPromptSubmit: true when neither a loop-guard nudge nor any autopilot
/// context (index-ready, intent hint, context fill) would be emitted.
pub fn prompt_submit_is_noop(sid: &str, payload: &Map<String, Value>, cfg: &Config) -> bool {
    loop_nudge_is_noop(sid, cfg) && autopilot_prompt_is_noop(sid, payload, cfg)
}

/// Mirrors `autopilot.session_saved_tokens` + `next_milestone` for the Stop summary.
pub fn stop_summary_is_noop(sid: &str, payload: &Map<String, Value>, cfg: &Config) -> bool {
    if !cfg.session_summary
        || cfg.shadow
        || payload
            .get("stop_hook_active")
            .is_some_and(|v| v.as_bool() == Some(true))
    {
        return true;
    }
    let flags = session_flags(sid);
    let Some(started) = flags.get("started_at").and_then(Value::as_f64) else {
        return true;
    };
    let last = flags
        .get("summary_shown")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    let savings_path = state_home().join("savings.jsonl");
    // O(1) fast-path: if savings.jsonl hasn't been modified since this session started, 0 tokens saved.
    if let Ok(meta) = fs::metadata(&savings_path) {
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        if mtime + 1.0 < started {
            return true;
        }
    } else {
        return true;
    }
    let mut saved: u64 = 0;
    // Read progressively (64 KB first, expanding to 1 MB only if the session spans >64 KB of logs).
    for window in [64 * 1024_u64, 1024 * 1024_u64] {
        saved = 0;
        let lines = tail_lines(&savings_path, window);
        let mut reached_start = false;
        for line in lines.iter().rev() {
            let Ok(Value::Object(rec)) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            if rec.get("ts").and_then(Value::as_f64).unwrap_or(0.0) < started {
                reached_start = true;
                break;
            }
            saved += as_u64(rec.get("saved_tok"));
        }
        if reached_start || saved >= 5000 && (last == 0 || saved >= 2 * last) {
            break;
        }
        if fs::metadata(&savings_path).map(|m| m.len()).unwrap_or(0) <= window {
            break;
        }
    }
    saved < 5000 || (last > 0 && saved < 2 * last)
}

fn loop_nudge_is_noop(sid: &str, cfg: &Config) -> bool {
    if !cfg.loop_guard || cfg.shadow || !valid_sid(sid) {
        return true;
    }
    let path = state_home()
        .join("loop-state")
        .join(format!("{}.json", safe_sid_filename(sid)));
    let Ok(text) = fs::read_to_string(&path) else {
        return true; // `_load` returns {} for a missing or unreadable file
    };
    match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(state)) => !matches!(
            state.get("pending_nudge"),
            Some(Value::String(n)) if !n.trim().is_empty()
        ),
        Ok(_) => true,
        Err(_) => !text.contains("pending_nudge"),
    }
}

/// True while the Stop/SessionEnd rollup lease is still held (Python would do nothing).
pub fn rollup_lease_held(host: &str) -> bool {
    let lease = state_home().join(format!("rollup-{host}.lease"));
    fs::metadata(lease)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| now_secs() - d.as_secs_f64() < ROLLUP_LEASE_SECONDS)
        .unwrap_or(false)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grep_gate_rejects_regex_and_short_patterns() {
        let cfg = Config::default();
        let mut ti = Map::new();
        ti.insert("pattern".into(), Value::String("ab".into()));
        assert!(grep_pre_is_noop("claude", &ti, &cfg));
        ti.insert("pattern".into(), Value::String("foo.*bar".into()));
        assert!(grep_pre_is_noop("claude", &ti, &cfg));
    }

    #[test]
    fn bash_gate_skips_empty_and_disabled() {
        let mut cfg = Config::default();
        let payload = Map::new();
        let mut ti = Map::new();
        ti.insert("command".into(), Value::String("   ".into()));
        assert!(bash_pre_is_noop("claude", &payload, &ti, &cfg));
        ti.insert("command".into(), Value::String("pytest".into()));
        cfg.form_repair = false;
        assert!(bash_pre_is_noop("claude", &payload, &ti, &cfg));
        assert!(!bash_pre_is_noop("codex", &payload, &ti, &cfg));
    }

    #[test]
    fn sig_matching_handles_missing_files() {
        assert!(sig_matches(None, "/definitely/not/here.json"));
        assert!(sig_matches(Some(&Value::Null), "/definitely/not/here.json"));
        assert!(!sig_matches(
            Some(&serde_json::json!([1, 2])),
            "/definitely/not/here.json"
        ));
    }

    #[test]
    fn intent_gate_is_superset_of_python_triggers() {
        assert!(prompt_may_have_intent("how much has terser saved?"));
        assert!(prompt_may_have_intent("show me the full output of that"));
        assert!(prompt_may_have_intent("what were the omitted lines"));
        assert!(!prompt_may_have_intent("/terser:status"));
        assert!(!prompt_may_have_intent("fix the login bug"));
    }

    #[test]
    fn context_fill_reads_claude_and_codex_transcripts() {
        let dir = env::temp_dir().join(format!("terser-gate-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let claude = dir.join("claude.jsonl");
        fs::write(
            &claude,
            concat!(
                "{\"type\":\"user\",\"message\":{\"content\":\"hi\"}}\n",
                "{\"type\":\"assistant\",\"message\":{\"model\":\"claude-x\",\"usage\":",
                "{\"input_tokens\":10,\"cache_read_input_tokens\":149990}}}\n",
            ),
        )
        .unwrap();
        let fill = context_fill(claude.to_str().unwrap()).unwrap();
        assert!((fill - 0.75).abs() < 1e-9);

        let codex = dir.join("codex.jsonl");
        fs::write(
            &codex,
            "{\"type\":\"event_msg\",\"payload\":{\"type\":\"token_count\",\"info\":{\"last_token_usage\":{\"input_tokens\":900},\"model_context_window\":1000}}}\n",
        )
        .unwrap();
        let fill = context_fill(codex.to_str().unwrap()).unwrap();
        assert!((fill - 0.9).abs() < 1e-9);
        assert!(context_fill(dir.join("missing.jsonl").to_str().unwrap()).is_none());
        let _ = fs::remove_dir_all(&dir);
    }
}
