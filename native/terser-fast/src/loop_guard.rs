use crate::config::{
    atomic_write_json, canonical_json, now_secs, safe_sid_filename, state_home, valid_sid, Config,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

const STALE_SECONDS: f64 = 1800.0;
const REPEAT_AFTER: i64 = 3;
const LABEL_MAX: usize = 80;

fn state_path(sid: &str) -> PathBuf {
    state_home()
        .join("loop-state")
        .join(format!("{}.json", safe_sid_filename(sid)))
}

fn sha256_prefix(input: &str, len: usize) -> String {
    let mut hasher = Sha256::new();
    hasher.update(input.as_bytes());
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for b in digest {
        out.push_str(&format!("{b:02x}"));
    }
    out[..len.min(64)].to_string()
}

fn clean_label(text: &str) -> String {
    let collapsed: String = text
        .chars()
        .map(|c| if c == ' ' || !c.is_control() { c } else { ' ' })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    if collapsed.chars().count() > LABEL_MAX {
        let mut s: String = collapsed.chars().take(LABEL_MAX - 3).collect();
        s.push_str("...");
        s
    } else {
        collapsed
    }
}

fn first_str<'a>(args: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a str> {
    for k in keys {
        if let Some(s) = args.get(*k).and_then(|v| v.as_str()) {
            if !s.trim().is_empty() {
                return Some(s);
            }
        }
    }
    None
}

fn val_to_py_str(val: Option<&Value>) -> String {
    match val {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Number(n)) => n.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => "False".to_string(),
        Some(other) => other.to_string(),
    }
}

fn tool_signature(tool_name: &str, args: &Map<String, Value>) -> Option<(String, String)> {
    let low = tool_name.to_ascii_lowercase();
    if matches!(
        low.as_str(),
        "run_command" | "bash" | "shell" | "exec_command" | "local_shell" | "powershell"
    ) {
        let cmd_val = args
            .get("CommandLine")
            .or_else(|| args.get("command"))
            .or_else(|| args.get("cmd"))?;
        let cmd_str = match cmd_val {
            Value::String(s) => s.clone(),
            Value::Array(arr) => arr
                .iter()
                .map(|x| {
                    x.as_str()
                        .map(str::to_string)
                        .unwrap_or_else(|| x.to_string())
                })
                .collect::<Vec<_>>()
                .join(" "),
            _ => return None,
        };
        if cmd_str.trim().is_empty() {
            return None;
        }
        let norm = cmd_str.split_whitespace().collect::<Vec<_>>().join(" ");
        let h = sha256_prefix(&format!("shell:{norm}"), 20);
        return Some((h, clean_label(&norm)));
    }

    if matches!(
        low.as_str(),
        "read_file" | "view_file" | "read" | "edit_file" | "write_file" | "edit" | "write"
    ) {
        let raw_path = first_str(args, &["Path", "AbsolutePath", "file_path", "path"])?;
        let norm_p = if cfg!(windows) {
            raw_path.to_ascii_lowercase()
        } else {
            raw_path.to_string()
        };
        let extra = if matches!(low.as_str(), "read_file" | "view_file" | "read") {
            let s = args.get("StartLine").or_else(|| args.get("offset"));
            let e = args.get("EndLine").or_else(|| args.get("limit"));
            format!(":{}:{}", val_to_py_str(s), val_to_py_str(e))
        } else {
            let snippet = first_str(args, &["Target", "old_string", "Content", "content"])
                .unwrap_or("")
                .chars()
                .take(120)
                .collect::<String>();
            format!(":{}", sha256_prefix(&snippet, 12))
        };
        let h = sha256_prefix(&format!("{low}:{norm_p}{extra}"), 20);
        let fname = Path::new(raw_path)
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| raw_path.to_string());
        return Some((h, clean_label(&format!("{tool_name}({fname})"))));
    }

    if matches!(low.as_str(), "grep" | "glob" | "call_mcp_tool") || low.starts_with("mcp") {
        let canon = canonical_json(&json!({"t": low, "a": Value::Object(args.clone())}));
        let h = sha256_prefix(&canon, 20);
        return Some((h, clean_label(tool_name)));
    }

    None
}

fn is_shell_edit(tool_name: &str, args: &Map<String, Value>) -> bool {
    let low = tool_name.to_ascii_lowercase();
    if !matches!(
        low.as_str(),
        "run_command" | "bash" | "shell" | "exec_command" | "local_shell" | "powershell"
    ) {
        return false;
    }
    let Some(cmd_val) = args
        .get("CommandLine")
        .or_else(|| args.get("command"))
        .or_else(|| args.get("cmd"))
    else {
        return false;
    };
    let cmd = match cmd_val {
        Value::String(s) => s.to_ascii_lowercase(),
        Value::Array(arr) => arr
            .iter()
            .filter_map(|v| v.as_str())
            .collect::<Vec<_>>()
            .join(" ")
            .to_ascii_lowercase(),
        _ => return false,
    };
    cmd.contains("sed -i")
        || cmd.contains("sed --in-place")
        || cmd.contains("perl -pi")
        || cmd.contains("git apply")
        || cmd.contains("apply_patch")
        || cmd.contains(" patch ")
        || cmd.starts_with("patch ")
        || cmd.contains(" tee ")
        || cmd.contains(" > ")
        || cmd.contains(" >> ")
}

fn should_fire(count: i64, threshold: i64) -> bool {
    count >= threshold && ((count - threshold) % REPEAT_AFTER == 0)
}

pub fn record_tool_event(
    sid: &str,
    tool_name: &str,
    args: &Map<String, Value>,
    is_error: bool,
    cfg: &Config,
) -> Option<String> {
    if !cfg.loop_guard || cfg.shadow || sid.is_empty() || !valid_sid(sid) {
        return None;
    }
    let (key, label) = tool_signature(tool_name, args)?;
    let now = now_secs();

    let p = state_path(sid);
    let mut state: Map<String, Value> = fs::read_to_string(&p)
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default();

    if let Some(last_ts) = state.get("last_ts").and_then(|v| v.as_f64()) {
        if (now - last_ts) > STALE_SECONDS {
            state.clear();
        }
    }

    let same_streak = if state.get("last_key").and_then(|v| v.as_str()) == Some(key.as_str()) {
        state
            .get("same_streak")
            .and_then(|v| v.as_i64())
            .unwrap_or(0)
            + 1
    } else {
        1
    };
    state.insert("last_key".into(), Value::String(key.clone()));
    state.insert("same_streak".into(), json!(same_streak));
    state.insert("last_ts".into(), json!(now));

    let mut fail_streaks: Map<String, Value> = state
        .get("fail_streaks")
        .and_then(|v| v.as_object())
        .cloned()
        .unwrap_or_default();

    let fail_count = if is_error {
        let c = fail_streaks.get(&key).and_then(|v| v.as_i64()).unwrap_or(0) + 1;
        fail_streaks.insert(key.clone(), json!(c));
        c
    } else {
        let low_tool = tool_name.to_ascii_lowercase();
        if matches!(
            low_tool.as_str(),
            "edit_file" | "write_file" | "edit" | "write" | "multiedit" | "apply_patch"
        ) || is_shell_edit(tool_name, args)
        {
            fail_streaks.clear();
        } else {
            fail_streaks.remove(&key);
        }
        0
    };
    while fail_streaks.len() > 50 {
        if let Some(first_k) = fail_streaks.keys().next().cloned() {
            fail_streaks.remove(&first_k);
        } else {
            break;
        }
    }
    state.insert("fail_streaks".into(), Value::Object(fail_streaks));

    let last_nudge_ts = state
        .get("last_nudge_ts")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let in_cooldown = (now - last_nudge_ts) < cfg.nudge_cooldown_s;

    let mut nudge: Option<String> = None;
    if !in_cooldown {
        if should_fire(fail_count, cfg.fail_streak) {
            nudge = Some(format!(
                "[Terser] `{label}` failed {fail_count}x in a row. \
                 Stop repeating it—diagnose the root error or switch approach."
            ));
        } else if should_fire(same_streak, cfg.thrash_streak) {
            nudge = Some(format!(
                "[Terser] `{label}` ran {same_streak}x identically. \
                 Verify what changed before running it again."
            ));
        }
        if let Some(ref n) = nudge {
            state.insert("pending_nudge".into(), Value::String(n.clone()));
            state.insert("last_nudge_ts".into(), json!(now));
        }
    }

    atomic_write_json(&p, &Value::Object(state));
    nudge
}
