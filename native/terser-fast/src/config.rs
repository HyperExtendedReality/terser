use serde_json::{json, Value};
use std::collections::HashMap;
use std::env;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone)]
pub struct Config {
    pub enabled: bool,
    pub shadow: bool,
    pub read_cache: bool,
    pub delta_diff: bool,
    pub outline_first: bool,
    pub outline_min_bytes: u64,
    pub outline_min_lines: usize,
    pub outline_backoff_limit: i64,
    pub mcp_refetch_guard: bool,
    pub codex_form_repair: bool,
    pub codex_mcp_refetch_guard: bool,
    pub mcp_dedup_window_s: f64,
    pub loop_guard: bool,
    pub thrash_streak: i64,
    pub fail_streak: i64,
    pub nudge_cooldown_s: f64,
    pub bash_compress: bool,
    pub enhanced_compression: bool,
    pub archive_large_outputs: bool,
    pub archive_threshold_bytes: usize,
    pub subagent_model_routing: bool,
    pub form_repair: bool,
    pub index_grep_answer: bool,
    pub python_worker: bool,
    pub context_fill_nudges: bool,
    pub compact_recall: bool,
    pub fanout_checkpoint: bool,
    pub intent_hints: bool,
    pub session_summary: bool,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            enabled: true,
            shadow: false,
            read_cache: true,
            delta_diff: true,
            outline_first: true,
            outline_min_bytes: 10240,
            outline_min_lines: 250,
            outline_backoff_limit: 2,
            mcp_refetch_guard: true,
            codex_form_repair: false,
            codex_mcp_refetch_guard: false,
            mcp_dedup_window_s: 120.0,
            loop_guard: true,
            thrash_streak: 3,
            fail_streak: 3,
            nudge_cooldown_s: 45.0,
            bash_compress: true,
            enhanced_compression: true,
            archive_large_outputs: true,
            archive_threshold_bytes: 16384,
            subagent_model_routing: false,
            form_repair: true,
            index_grep_answer: true,
            python_worker: true,
            context_fill_nudges: true,
            compact_recall: true,
            fanout_checkpoint: true,
            intent_hints: true,
            session_summary: true,
        }
    }
}

pub fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn home_dir() -> PathBuf {
    if let Ok(h) = env::var("USERPROFILE") {
        if !h.trim().is_empty() {
            return PathBuf::from(h);
        }
    }
    if let Ok(h) = env::var("HOME") {
        if !h.trim().is_empty() {
            return PathBuf::from(h);
        }
    }
    PathBuf::from(".")
}

pub fn state_home() -> PathBuf {
    if let Ok(raw) = env::var("TERSER_HOME") {
        let trimmed = raw.trim();
        if !trimmed.is_empty() {
            if let Some(rest) = trimmed.strip_prefix("~/") {
                return home_dir().join(rest);
            }
            return PathBuf::from(trimmed);
        }
    }
    home_dir().join(".terser")
}

fn is_truthy(val: &str) -> bool {
    matches!(
        val.trim().to_ascii_lowercase().as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn is_falsy(val: &str) -> bool {
    matches!(
        val.trim().to_ascii_lowercase().as_str(),
        "0" | "false" | "no" | "off"
    )
}

fn env_bool(key: &str) -> Option<bool> {
    let v = env::var(key).ok()?;
    if v.trim().is_empty() {
        return None;
    }
    if is_falsy(&v) {
        Some(false)
    } else if is_truthy(&v) {
        Some(true)
    } else {
        None
    }
}

pub fn load_config() -> Config {
    let mut cfg = Config::default();
    let cfg_file = state_home().join("config.json");
    if let Ok(text) = fs::read_to_string(&cfg_file) {
        if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&text) {
            if let Some(b) = map.get("enabled").and_then(|v| v.as_bool()) {
                cfg.enabled = b;
            }
            if let Some(b) = map.get("shadow").and_then(|v| v.as_bool()) {
                cfg.shadow = b;
            }
            if let Some(b) = map.get("read_cache").and_then(|v| v.as_bool()) {
                cfg.read_cache = b;
            }
            if let Some(b) = map.get("delta_diff").and_then(|v| v.as_bool()) {
                cfg.delta_diff = b;
            }
            if let Some(b) = map.get("outline_first").and_then(|v| v.as_bool()) {
                cfg.outline_first = b;
            }
            if let Some(n) = map.get("outline_min_bytes").and_then(|v| v.as_u64()) {
                cfg.outline_min_bytes = n;
            }
            if let Some(n) = map.get("outline_min_lines").and_then(|v| v.as_u64()) {
                cfg.outline_min_lines = n as usize;
            }
            if let Some(n) = map.get("outline_backoff_limit").and_then(|v| v.as_i64()) {
                cfg.outline_backoff_limit = n;
            }
            if let Some(b) = map.get("mcp_refetch_guard").and_then(|v| v.as_bool()) {
                cfg.mcp_refetch_guard = b;
            }
            if let Some(b) = map.get("codex_form_repair").and_then(|v| v.as_bool()) {
                cfg.codex_form_repair = b;
            }
            if let Some(b) = map.get("codex_mcp_refetch_guard").and_then(|v| v.as_bool()) {
                cfg.codex_mcp_refetch_guard = b;
            }
            if let Some(n) = map.get("mcp_dedup_window_s").and_then(|v| v.as_f64()) {
                cfg.mcp_dedup_window_s = n;
            }
            if let Some(b) = map.get("loop_guard").and_then(|v| v.as_bool()) {
                cfg.loop_guard = b;
            }
            if let Some(n) = map.get("thrash_streak").and_then(|v| v.as_i64()) {
                cfg.thrash_streak = n;
            }
            if let Some(b) = map.get("subagent_model_routing").and_then(|v| v.as_bool()) {
                cfg.subagent_model_routing = b;
            }
            if let Some(b) = map.get("form_repair").and_then(|v| v.as_bool()) {
                cfg.form_repair = b;
            }
            if let Some(b) = map.get("index_grep_answer").and_then(|v| v.as_bool()) {
                cfg.index_grep_answer = b;
            }
            if let Some(b) = map.get("python_worker").and_then(|v| v.as_bool()) {
                cfg.python_worker = b;
            }
            if let Some(b) = map.get("context_fill_nudges").and_then(|v| v.as_bool()) {
                cfg.context_fill_nudges = b;
            }
            if let Some(b) = map.get("compact_recall").and_then(|v| v.as_bool()) {
                cfg.compact_recall = b;
            }
            if let Some(b) = map.get("fanout_checkpoint").and_then(|v| v.as_bool()) {
                cfg.fanout_checkpoint = b;
            }
            if let Some(b) = map.get("intent_hints").and_then(|v| v.as_bool()) {
                cfg.intent_hints = b;
            }
            if let Some(b) = map.get("session_summary").and_then(|v| v.as_bool()) {
                cfg.session_summary = b;
            }
            if let Some(n) = map.get("fail_streak").and_then(|v| v.as_i64()) {
                cfg.fail_streak = n;
            }
            if let Some(n) = map.get("nudge_cooldown_s").and_then(|v| v.as_f64()) {
                cfg.nudge_cooldown_s = n;
            }
            if let Some(b) = map.get("bash_compress").and_then(|v| v.as_bool()) {
                cfg.bash_compress = b;
            }
            if let Some(b) = map.get("enhanced_compression").and_then(|v| v.as_bool()) {
                cfg.enhanced_compression = b;
            }
            if let Some(b) = map.get("archive_large_outputs").and_then(|v| v.as_bool()) {
                cfg.archive_large_outputs = b;
            }
            if let Some(n) = map.get("archive_threshold_bytes").and_then(|v| v.as_u64()) {
                cfg.archive_threshold_bytes = n as usize;
            }
        }
    }

    if env::var("TERSER_DISABLE")
        .map(|v| is_truthy(&v))
        .unwrap_or(false)
        || env::var("TE_SIDECALL")
            .map(|v| is_truthy(&v))
            .unwrap_or(false)
    {
        cfg.enabled = false;
    }
    if env::var("TERSER_SHADOW")
        .map(|v| is_truthy(&v))
        .unwrap_or(false)
    {
        cfg.shadow = true;
    }
    if let Some(b) = env_bool("TERSER_READ_CACHE") {
        cfg.read_cache = b;
    }
    if let Some(b) = env_bool("TERSER_OUTLINE_FIRST") {
        cfg.outline_first = b;
    }
    if let Some(b) = env_bool("TERSER_MCP_REFETCH_GUARD") {
        cfg.mcp_refetch_guard = b;
    }
    if let Some(b) = env_bool("TERSER_LOOP_GUARD") {
        cfg.loop_guard = b;
    }
    if let Some(b) = env_bool("TERSER_BASH_COMPRESS") {
        cfg.bash_compress = b;
    }
    if let Some(b) = env_bool("TERSER_FORM_REPAIR") {
        cfg.form_repair = b;
    }
    if let Some(b) = env_bool("TERSER_GREP_ANSWER") {
        cfg.index_grep_answer = b;
    }
    if let Some(b) = env_bool("TERSER_SUBAGENT_ROUTING") {
        cfg.subagent_model_routing = b;
    }
    if let Some(b) = env_bool("TERSER_PYTHON_WORKER") {
        cfg.python_worker = b;
    }
    if let Some(b) = env_bool("TERSER_INTENT_HINTS") {
        cfg.intent_hints = b;
    }
    if let Some(b) = env_bool("TERSER_SESSION_SUMMARY") {
        cfg.session_summary = b;
    }
    cfg
}

fn atomic_write_bytes(path: &Path, bytes: &[u8]) {
    if let Some(parent) = path.parent() {
        if fs::create_dir_all(parent).is_err() {
            return;
        }
        let pid = std::process::id();
        let ts = (now_secs() * 1_000_000.0) as u64;
        let tmp = parent.join(format!(".terser.{pid}.{ts}.tmp"));
        if fs::write(&tmp, bytes).is_ok() {
            if fs::rename(&tmp, path).is_err() {
                let _ = fs::remove_file(path);
                if fs::rename(&tmp, path).is_err() {
                    let _ = fs::remove_file(&tmp);
                }
            }
        } else {
            let _ = fs::remove_file(&tmp);
        }
    }
}

pub fn atomic_write_json(path: &Path, data: &Value) {
    if let Ok(bytes) = serde_json::to_vec(data) {
        atomic_write_bytes(path, &bytes);
    }
}

pub fn atomic_write_map(path: &Path, data: &serde_json::Map<String, Value>) {
    if let Ok(bytes) = serde_json::to_vec(data) {
        atomic_write_bytes(path, &bytes);
    }
}

const SAVINGS_MAX_BYTES: u64 = 5 * 1024 * 1024;
const SAVINGS_KEEP_LINES: usize = 10_000;

fn maybe_rotate_savings(log: &Path) {
    let Ok(meta) = fs::metadata(log) else { return };
    if meta.len() <= SAVINGS_MAX_BYTES {
        return;
    }
    let Ok(text) = fs::read_to_string(log) else {
        return;
    };
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() <= SAVINGS_KEEP_LINES {
        return;
    }
    let mut tail = lines[lines.len() - SAVINGS_KEEP_LINES..].join("\n");
    tail.push('\n');
    if let Some(parent) = log.parent() {
        let pid = std::process::id();
        let ts = (now_secs() * 1_000_000.0) as u64;
        let tmp = parent.join(format!(".sav.{pid}.{ts}.tmp"));
        if fs::write(&tmp, tail.as_bytes()).is_ok() {
            if fs::rename(&tmp, log).is_err() {
                let _ = fs::remove_file(log);
                if fs::rename(&tmp, log).is_err() {
                    let _ = fs::remove_file(&tmp);
                }
            }
        } else {
            let _ = fs::remove_file(&tmp);
        }
    }
}

/// Append a funnel tier event (`served` / `retry`) to `~/.terser/funnel.jsonl` (Python parity).
/// Tiers Python's `compute_tier_backoff` switched off for `host` because most of their short
/// answers were retried. The file is written by Python; data older than a day is ignored.
pub fn tier_backed_off(tier: &str, host: &str) -> bool {
    const MAX_AGE_S: f64 = 86400.0;
    let Ok(text) = fs::read_to_string(state_home().join("funnel-backoff.json")) else {
        return false;
    };
    let Ok(Value::Object(root)) = serde_json::from_str::<Value>(&text) else {
        return false;
    };
    let fresh = root
        .get("at")
        .and_then(Value::as_f64)
        .map(|at| {
            let age = now_secs() - at;
            (0.0..MAX_AGE_S).contains(&age)
        })
        .unwrap_or(false);
    fresh
        && root
            .get("disabled")
            .and_then(|d| d.get(host))
            .and_then(Value::as_array)
            .is_some_and(|tiers| tiers.iter().any(|t| t.as_str() == Some(tier)))
}

pub fn record_funnel_event(tier: &str, event: &str, host: &str) {
    let log = state_home().join("funnel.jsonl");
    if let Some(parent) = log.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if fs::metadata(&log)
        .map(|m| m.len() > 4 * 1024 * 1024)
        .unwrap_or(false)
    {
        let _ = fs::rename(&log, log.with_extension("jsonl.1"));
    }
    let ts = (now_secs() * 10.0).round() / 10.0;
    let tier: String = tier.chars().take(32).collect();
    let event: String = event.chars().take(16).collect();
    let entry = json!({"ts": ts, "host": host, "tier": tier, "ev": event});
    if let Ok(line) = serde_json::to_string(&entry) {
        if let Ok(mut fh) = OpenOptions::new().create(true).append(true).open(&log) {
            let _ = writeln!(fh, "{line}");
        }
    }
}

pub fn record_savings(
    category: &str,
    raw_chars: usize,
    served_chars: usize,
    host: &str,
    detail: &str,
) {
    let saved_chars = raw_chars.saturating_sub(served_chars);
    if saved_chars == 0 {
        return;
    }
    let log = state_home().join("savings.jsonl");
    if let Some(parent) = log.parent() {
        let _ = fs::create_dir_all(parent);
    }
    maybe_rotate_savings(&log);
    let trunc_detail: String = detail.chars().take(100).collect();
    let ts = (now_secs() * 10.0).round() / 10.0;
    let entry = json!({
        "ts": ts,
        "host": host,
        "cat": category,
        "raw_tok": (raw_chars / 4).max(1),
        "served_tok": served_chars / 4,
        "saved_tok": (saved_chars / 4).max(1),
        "detail": trunc_detail,
    });
    if let Ok(line) = serde_json::to_string(&entry) {
        if let Ok(mut fh) = OpenOptions::new().create(true).append(true).open(&log) {
            let _ = writeln!(fh, "{line}");
        }
    }
}

pub fn valid_sid(sid: &str) -> bool {
    !sid.is_empty()
        && sid.len() <= 96
        && sid
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}

pub fn safe_sid_filename(sid: &str) -> String {
    sid.chars()
        .take(80)
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

pub fn canonical_json(val: &Value) -> String {
    match val {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            let mut out = String::from("{");
            for (i, k) in keys.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&serde_json::to_string(k).unwrap_or_default());
                out.push(':');
                out.push_str(&canonical_json(&map[*k]));
            }
            out.push('}');
            out
        }
        Value::Array(arr) => {
            let mut out = String::from("[");
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&canonical_json(item));
            }
            out.push(']');
            out
        }
        _ => serde_json::to_string(val).unwrap_or_else(|_| "null".to_string()),
    }
}

#[allow(dead_code)]
pub fn _unused(_m: &HashMap<String, String>) {}
