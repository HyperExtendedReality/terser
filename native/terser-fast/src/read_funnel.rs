use crate::config::{
    atomic_write_json, canonical_json, now_secs, record_savings, safe_sid_filename, state_home,
    valid_sid, Config,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_SNAPSHOT_BYTES: usize = 128 * 1024;
const MAX_STORE_ENTRIES: usize = 160;
const SOFT_DENY_CAP: i64 = 1;

const MUTATING_OR_LIVE_MCP_TOKENS: &[&str] = &[
    "write", "edit", "create", "delete", "remove", "drop", "update", "patch", "put", "post",
    "send", "submit", "publish", "deploy", "run", "exec", "execute", "invoke", "trigger", "start",
    "stop", "kill", "cancel", "status", "poll", "wait", "watch", "tail", "logs", "time", "now",
    "clock", "health", "ping", "refresh", "invalidate", "register", "index",
];

pub enum FunnelOutcome {
    Allow,
    Deny(String),
    Delegate,
}

fn cache_path(sid: &str) -> PathBuf {
    state_home()
        .join("read-cache")
        .join(format!("{}.json", safe_sid_filename(sid)))
}

fn norm_path(file_path: &str) -> String {
    let p = Path::new(file_path);
    let abs = fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    let mut s = abs.to_string_lossy().to_string();
    // Strip Windows verbatim prefix \\?\ so keys match Python os.path.abspath()
    if let Some(stripped) = s.strip_prefix(r"\\?\") {
        s = stripped.to_string();
    }
    if cfg!(windows) {
        s.to_ascii_lowercase()
    } else {
        s
    }
}

fn load_store(sid: &str) -> Map<String, Value> {
    let p = cache_path(sid);
    let mut root = Map::new();
    if let Ok(text) = fs::read_to_string(&p) {
        if let Ok(Value::Object(m)) = serde_json::from_str::<Value>(&text) {
            root = m;
        }
    }
    if !matches!(root.get("reads"), Some(Value::Object(_))) {
        root.insert("reads".into(), Value::Object(Map::new()));
    }
    if !matches!(root.get("mcp"), Some(Value::Object(_))) {
        root.insert("mcp".into(), Value::Object(Map::new()));
    }
    if !matches!(root.get("meta"), Some(Value::Object(_))) {
        root.insert("meta".into(), Value::Object(Map::new()));
    }
    root
}

fn save_store(sid: &str, store: &mut Map<String, Value>) {
    for bucket_key in ["reads", "mcp"] {
        if let Some(Value::Object(bucket)) = store.get_mut(bucket_key) {
            if bucket.len() > MAX_STORE_ENTRIES {
                let mut items: Vec<(String, f64)> = bucket
                    .iter()
                    .map(|(k, v)| {
                        let ts = v.get("ts").and_then(|t| t.as_f64()).unwrap_or(0.0);
                        (k.clone(), ts)
                    })
                    .collect();
                items.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
                let keep: std::collections::HashSet<String> = items
                    .into_iter()
                    .take(MAX_STORE_ENTRIES)
                    .map(|(k, _)| k)
                    .collect();
                bucket.retain(|k, _| keep.contains(k));
            }
        }
    }
    atomic_write_json(&cache_path(sid), &Value::Object(store.clone()));
}

fn range_covered(
    prev_start: Option<i64>,
    prev_end: Option<i64>,
    start: Option<i64>,
    end: Option<i64>,
) -> bool {
    let ps = prev_start.unwrap_or(1);
    let s = start.unwrap_or(1);
    if s < ps {
        return false;
    }
    if let Some(pe) = prev_end {
        if end.is_none() || end.unwrap() > pe {
            return false;
        }
    }
    true
}

fn intervals_cover(prev: &Map<String, Value>, start: Option<i64>, end: Option<i64>) -> bool {
    if let Some(Value::Array(ivs)) = prev.get("intervals") {
        if !ivs.is_empty() {
            for iv in ivs {
                if let Value::Array(pair) = iv {
                    if pair.len() == 2 {
                        let ps = opt_i64(pair.first());
                        let pe = opt_i64(pair.get(1));
                        if range_covered(ps, pe, start, end) {
                            return true;
                        }
                    }
                }
            }
            return false;
        }
    }
    range_covered(opt_i64(prev.get("start")), opt_i64(prev.get("end")), start, end)
}

fn merge_intervals(
    existing: Option<&Vec<Value>>,
    new_start: Option<i64>,
    new_end: Option<i64>,
) -> Vec<(i64, Option<i64>)> {
    let mut items: Vec<(i64, Option<i64>)> = Vec::new();
    if let Some(arr) = existing {
        for iv in arr {
            if let Value::Array(pair) = iv {
                if pair.len() == 2 {
                    let s = opt_i64(pair.first()).unwrap_or(1).max(1);
                    let e = opt_i64(pair.get(1));
                    items.push((s, e));
                }
            }
        }
    }
    items.push((new_start.unwrap_or(1).max(1), new_end));
    items.sort_by(|a, b| {
        a.0.cmp(&b.0).then_with(|| match (a.1, b.1) {
            (None, None) => std::cmp::Ordering::Equal,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (Some(_), None) => std::cmp::Ordering::Less,
            (Some(x), Some(y)) => x.cmp(&y),
        })
    });
    let mut merged: Vec<(i64, Option<i64>)> = Vec::new();
    for (s, e) in items {
        if let Some(last) = merged.last_mut() {
            match last.1 {
                None => continue,
                Some(pe) if s <= pe + 1 => {
                    last.1 = match e {
                        None => None,
                        Some(ne) => Some(pe.max(ne)),
                    };
                }
                _ => merged.push((s, e)),
            }
        } else {
            merged.push((s, e));
        }
    }
    merged
}

fn intervals_to_json(ivs: &[(i64, Option<i64>)]) -> Value {
    Value::Array(
        ivs.iter()
            .map(|(s, e)| json!([s, e]))
            .collect(),
    )
}

fn is_lockfile_or_minified(basename: &str) -> bool {
    let low = basename.to_ascii_lowercase();
    matches!(
        low.as_str(),
        "package-lock.json"
            | "pnpm-lock.yaml"
            | "yarn.lock"
            | "cargo.lock"
            | "poetry.lock"
            | "uv.lock"
            | "pipfile.lock"
            | "composer.lock"
            | "gemfile.lock"
            | "go.sum"
    ) || low.ends_with(".min.js")
        || low.ends_with(".min.css")
        || low.ends_with(".snap")
        || low.ends_with(".map")
}

fn format_range(start: Option<i64>, end: Option<i64>) -> String {
    match (start, end) {
        (None, None) => "full file".to_string(),
        (Some(s), Some(e)) => format!("L{s}-L{e}"),
        (Some(s), None) => format!("from L{s}"),
        (None, Some(e)) => format!("L1-L{e}"),
    }
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    let mut out = String::with_capacity(64);
    for b in digest {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

fn opt_i64(val: Option<&Value>) -> Option<i64> {
    match val? {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    }
}

fn meta_mtime_ns(meta: &fs::Metadata) -> Option<i64> {
    meta.modified()
        .ok()?
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|d| d.as_nanos() as i64)
}

pub fn check_read(
    sid: &str,
    file_path: &str,
    start_line: Option<i64>,
    end_line: Option<i64>,
    byte_offset: Option<i64>,
    cfg: &Config,
    host: &str,
) -> FunnelOutcome {
    if !cfg.read_cache || sid.is_empty() || !valid_sid(sid) || file_path.is_empty() {
        return FunnelOutcome::Allow;
    }
    if byte_offset.unwrap_or(0) > 0 {
        return FunnelOutcome::Allow;
    }

    let target = Path::new(file_path);
    let meta = match fs::metadata(target) {
        Ok(m) if m.is_file() && m.len() <= MAX_FILE_BYTES && m.len() > 0 => m,
        _ => return FunnelOutcome::Allow,
    };

    let now = now_secs();
    let mtime_ns = meta_mtime_ns(&meta);
    let file_size = meta.len() as i64;
    let key = norm_path(file_path);
    let basename = target
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| file_path.to_string());

    let mut store = load_store(sid);
    let prev_opt = store
        .get("reads")
        .and_then(|v| v.as_object())
        .and_then(|m| m.get(&key))
        .and_then(|v| v.as_object())
        .cloned();

    // Stat fastpath: avoid reading & SHA-256 hashing unchanged files on T0 hits
    if let Some(mut prev) = prev_opt.clone() {
        let outline_only = prev
            .get("outline_only")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let has_sha = prev
            .get("sha")
            .and_then(|v| v.as_str())
            .map(|s| !s.is_empty())
            .unwrap_or(false);
        let prev_mtime = opt_i64(prev.get("mtime_ns"));
        let prev_size = opt_i64(prev.get("size"));
        if !outline_only
            && has_sha
            && mtime_ns.is_some()
            && prev_mtime == mtime_ns
            && prev_size == Some(file_size)
            && intervals_cover(&prev, start_line, end_line)
        {
            let soft_denies = opt_i64(prev.get("soft_denies")).unwrap_or(0);
            if soft_denies < SOFT_DENY_CAP && !cfg.shadow {
                prev.insert("soft_denies".into(), json!(soft_denies + 1));
                prev.insert("ts".into(), json!(now));
                if let Some(Value::Object(reads)) = store.get_mut("reads") {
                    reads.insert(key, Value::Object(prev));
                }
                save_store(sid, &mut store);
                let rng = format_range(start_line, end_line);
                let reason = format!(
                    "[Terser] `{basename}` ({rng}) is unchanged since your earlier read. \
                     Reuse the content already in context, or retry to refresh (e.g. after /compact)."
                );
                record_savings("read_dedup", meta.len() as usize, reason.len(), host, &basename);
                return FunnelOutcome::Deny(reason);
            }
            prev.insert("soft_denies".into(), json!(0));
            prev.insert("ts".into(), json!(now));
            if let Some(Value::Object(reads)) = store.get_mut("reads") {
                reads.insert(key, Value::Object(prev));
            }
            save_store(sid, &mut store);
            return FunnelOutcome::Allow;
        }
    }

    let raw_bytes = match fs::read(target) {
        Ok(b) => b,
        Err(_) => return FunnelOutcome::Allow,
    };
    let probe_len = raw_bytes.len().min(4096);
    if raw_bytes[..probe_len].contains(&0u8) {
        return FunnelOutcome::Allow;
    }

    let sha = sha256_hex(&raw_bytes);
    let text_snapshot: Option<String> = if raw_bytes.len() <= MAX_SNAPSHOT_BYTES {
        Some(String::from_utf8_lossy(&raw_bytes).into_owned())
    } else {
        None
    };

    if let Some(mut prev) = prev_opt {
        let prev_sha = prev.get("sha").and_then(|v| v.as_str()).unwrap_or("");
        let soft_denies = opt_i64(prev.get("soft_denies")).unwrap_or(0);
        let outline_only = prev
            .get("outline_only")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        // Retry after outline-only -> allow full file & record override
        if outline_only {
            let ivs = merge_intervals(None, start_line, end_line);
            prev.insert("outline_only".into(), Value::Bool(false));
            prev.insert("sha".into(), Value::String(sha));
            if let Some(m) = mtime_ns {
                prev.insert("mtime_ns".into(), json!(m));
            }
            prev.insert("size".into(), json!(file_size));
            prev.insert("start".into(), start_line.map(Into::into).unwrap_or(Value::Null));
            prev.insert("end".into(), end_line.map(Into::into).unwrap_or(Value::Null));
            prev.insert("intervals".into(), intervals_to_json(&ivs));
            prev.insert("soft_denies".into(), json!(0));
            prev.insert("ts".into(), json!(now));
            if let Some(snap) = text_snapshot {
                prev.insert("snapshot".into(), Value::String(snap));
            }
            if let Some(Value::Object(reads)) = store.get_mut("reads") {
                reads.insert(key, Value::Object(prev));
            }
            let total_lines_retry = (raw_bytes.iter().filter(|&&b| b == b'\n').count() + 1) as i64;
            let rs_r = start_line.unwrap_or(1).max(1);
            let re_r = end_line.unwrap_or(total_lines_retry).min(total_lines_retry);
            if (start_line.is_none() && end_line.is_none()) || (re_r - rs_r + 1) >= 400 {
                if let Some(Value::Object(meta_obj)) = store.get_mut("meta") {
                    let cur = opt_i64(meta_obj.get("outline_overrides")).unwrap_or(0);
                    meta_obj.insert("outline_overrides".into(), json!(cur + 1));
                }
            }
            save_store(sid, &mut store);
            return FunnelOutcome::Allow;
        }

        // Tier 0: Unchanged re-read
        if prev_sha == sha && intervals_cover(&prev, start_line, end_line) {
            if soft_denies < SOFT_DENY_CAP && !cfg.shadow {
                prev.insert("soft_denies".into(), json!(soft_denies + 1));
                prev.insert("ts".into(), json!(now));
                if let Some(Value::Object(reads)) = store.get_mut("reads") {
                    reads.insert(key, Value::Object(prev));
                }
                save_store(sid, &mut store);
                let rng = format_range(start_line, end_line);
                let reason = format!(
                    "[Terser] `{basename}` ({rng}) is unchanged since your earlier read. \
                     Reuse the content already in context, or retry to refresh (e.g. after /compact)."
                );
                record_savings("read_dedup", raw_bytes.len(), reason.len(), host, &basename);
                return FunnelOutcome::Deny(reason);
            }
            prev.insert("soft_denies".into(), json!(0));
            prev.insert("ts".into(), json!(now));
            if let Some(Value::Object(reads)) = store.get_mut("reads") {
                reads.insert(key, Value::Object(prev));
            }
            save_store(sid, &mut store);
            return FunnelOutcome::Allow;
        }

        // Tier 0b: Partial-overlap range trimming -> delegate to Python when >=40 lines overlap
        if prev_sha == sha
            && soft_denies < SOFT_DENY_CAP
            && !cfg.shadow
            && (start_line.is_some() || end_line.is_some())
        {
            let total_lines = (raw_bytes.iter().filter(|&&b| b == b'\n').count() + 1) as i64;
            let rs = start_line.unwrap_or(1).max(1);
            let re = end_line.unwrap_or(total_lines).min(total_lines);
            let mut has_large_overlap = false;
            if let Some(Value::Array(ivs)) = prev.get("intervals") {
                for iv in ivs {
                    if let Value::Array(pair) = iv {
                        if pair.len() == 2 {
                            let s = opt_i64(pair.first()).unwrap_or(1).max(rs);
                            let e = opt_i64(pair.get(1)).unwrap_or(total_lines).min(re);
                            if e >= s && (e - s + 1) >= 40 {
                                has_large_overlap = true;
                                break;
                            }
                        }
                    }
                }
            }
            if has_large_overlap {
                return FunnelOutcome::Delegate;
            }
        }

        // Tier 1: File changed slightly & snapshot exists -> delegate to Python unified diff
        if cfg.delta_diff
            && prev_sha != sha
            && soft_denies < SOFT_DENY_CAP
            && !cfg.shadow
            && prev.get("snapshot").and_then(|v| v.as_str()).is_some()
            && text_snapshot.is_some()
            && intervals_cover(&prev, start_line, end_line)
        {
            return FunnelOutcome::Delegate;
        }
    } else {
        // Tier 2: Large unranged first read (or huge first-time ranged slice >= 400L) -> delegate to Python
        let outline_overrides = store
            .get("meta")
            .and_then(|v| v.as_object())
            .and_then(|m| opt_i64(m.get("outline_overrides")))
            .unwrap_or(0);
        let is_unranged = start_line.is_none() && end_line.is_none();
        let min_bytes = if is_unranged {
            cfg.outline_min_bytes
        } else {
            cfg.outline_min_bytes.max(12288)
        };
        if cfg.outline_first
            && (cfg.outline_backoff_limit <= 0 || outline_overrides < cfg.outline_backoff_limit)
            && !cfg.shadow
            && meta.len() >= min_bytes
        {
            let line_count = raw_bytes.iter().filter(|&&b| b == b'\n').count() + 1;
            let total_i64 = line_count as i64;
            let rs_t2 = start_line.unwrap_or(1).max(1);
            let re_t2 = end_line.unwrap_or(total_i64).min(total_i64);
            let req_span = (re_t2 - rs_t2 + 1).max(0);
            let qualifies = if is_unranged {
                is_lockfile_or_minified(&basename) || line_count >= cfg.outline_min_lines
            } else {
                req_span >= 400
                    && (is_lockfile_or_minified(&basename) || line_count >= cfg.outline_min_lines)
            };
            if qualifies {
                // Fast-path: serve cached Tier 2 outline directly in Rust (~5ms, zero Python spawn)
                let outline_path = state_home()
                    .join("outline-cache")
                    .join(format!("{}.txt", &sha[..24]));
                if let Ok(cached_outline) = fs::read_to_string(&outline_path) {
                    let trimmed = cached_outline.trim();
                    if !trimmed.is_empty() {
                        let mut entry = Map::new();
                        entry.insert("sha".into(), Value::String(sha));
                        entry.insert("start".into(), Value::Null);
                        entry.insert("end".into(), Value::Null);
                        entry.insert("intervals".into(), json!([]));
                        entry.insert("soft_denies".into(), json!(1));
                        entry.insert("outline_only".into(), Value::Bool(true));
                        entry.insert("ts".into(), json!(now));
                        if let Some(Value::Object(reads)) = store.get_mut("reads") {
                            reads.insert(key, Value::Object(entry));
                        }
                        save_store(sid, &mut store);
                        let range_hint = if host == "antigravity" {
                            "StartLine/EndLine"
                        } else {
                            "offset/limit"
                        };
                        let slice_note = if is_unranged {
                            String::new()
                        } else {
                            format!(" (requested {req_span} lines: L{rs_t2}-L{re_t2})")
                        };
                        let reason = format!(
                            "[Terser] `{basename}` is {line_count} lines ({} B){slice_note}. \
                             Outline (exact line spans):\n{trimmed}\n\n\
                             Read only the needed span with {range_hint} (or `terser-index` `get_symbol_source`), \
                             or retry this read immediately to load the full requested span.",
                            meta.len()
                        );
                        record_savings("read_outline", raw_bytes.len(), reason.len(), host, &basename);
                        return FunnelOutcome::Deny(reason);
                    }
                }
                return FunnelOutcome::Delegate;
            }
        }
    }

    // Tier 3: First read / ranged read / wider range / large change -> record disjoint intervals & allow
    let existing_ivs = store
        .get("reads")
        .and_then(|v| v.as_object())
        .and_then(|reads| reads.get(&key))
        .and_then(|v| v.as_object())
        .and_then(|prev| {
            if prev.get("sha").and_then(|v| v.as_str()) == Some(sha.as_str()) {
                if let Some(Value::Array(arr)) = prev.get("intervals") {
                    if !arr.is_empty() {
                        return Some(arr.clone());
                    }
                }
                let ps = opt_i64(prev.get("start")).unwrap_or(1);
                let pe = opt_i64(prev.get("end"));
                return Some(vec![json!([ps, pe])]);
            }
            None
        });
    let merged_ivs = merge_intervals(existing_ivs.as_ref(), start_line, end_line);
    let (legacy_start, legacy_end) = if merged_ivs.len() == 1 {
        let (s, e) = merged_ivs[0];
        if s == 1 && e.is_none() {
            (None, None)
        } else {
            (Some(s), e)
        }
    } else {
        (start_line, end_line)
    };

    let mut entry = Map::new();
    entry.insert("sha".into(), Value::String(sha));
    if let Some(m) = mtime_ns {
        entry.insert("mtime_ns".into(), json!(m));
    }
    entry.insert("size".into(), json!(file_size));
    entry.insert("start".into(), legacy_start.map(Into::into).unwrap_or(Value::Null));
    entry.insert("end".into(), legacy_end.map(Into::into).unwrap_or(Value::Null));
    entry.insert("intervals".into(), intervals_to_json(&merged_ivs));
    entry.insert("soft_denies".into(), json!(0));
    entry.insert("outline_only".into(), Value::Bool(false));
    entry.insert("ts".into(), json!(now));
    if let Some(snap) = text_snapshot {
        entry.insert("snapshot".into(), Value::String(snap));
    }
    if let Some(Value::Object(reads)) = store.get_mut("reads") {
        reads.insert(key, Value::Object(entry));
    }
    save_store(sid, &mut store);
    FunnelOutcome::Allow
}

fn is_mutating_or_live_mcp(name: &str) -> bool {
    let tool_part = name.rsplit(':').next().unwrap_or(name);
    let tool_part = if tool_part.contains("__") {
        tool_part.rsplit("__").next().unwrap_or(tool_part)
    } else {
        tool_part
    };
    let lower = tool_part.to_ascii_lowercase();
    lower
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|t| !t.is_empty())
        .any(|t| MUTATING_OR_LIVE_MCP_TOKENS.contains(&t))
}

pub fn check_mcp_refetch(
    sid: &str,
    display_name: &str,
    inner_args: &Value,
    cfg: &Config,
    host: &str,
) -> FunnelOutcome {
    if !cfg.mcp_refetch_guard || cfg.shadow || sid.is_empty() || !valid_sid(sid) {
        return FunnelOutcome::Allow;
    }
    if display_name.is_empty() || is_mutating_or_live_mcp(display_name) {
        return FunnelOutcome::Allow;
    }

    let now = now_secs();
    let payload = json!({"tool": display_name, "args": inner_args});
    let canon = canonical_json(&payload);
    let fp = sha256_hex(canon.as_bytes())[..24].to_string();

    let mut store = load_store(sid);
    let prev_opt = store
        .get("mcp")
        .and_then(|v| v.as_object())
        .and_then(|m| m.get(&fp))
        .and_then(|v| v.as_object())
        .cloned();

    if let Some(mut prev) = prev_opt {
        let elapsed = now - prev.get("ts").and_then(|v| v.as_f64()).unwrap_or(0.0);
        let soft_denies = opt_i64(prev.get("soft_denies")).unwrap_or(0);
        if elapsed >= 0.0 && elapsed <= cfg.mcp_dedup_window_s && soft_denies < SOFT_DENY_CAP {
            let cached_resp_bytes = prev
                .get("resp_bytes")
                .and_then(|v| v.as_u64())
                .map(|n| n as usize);
            prev.insert("soft_denies".into(), json!(soft_denies + 1));
            if let Some(Value::Object(mcp)) = store.get_mut("mcp") {
                mcp.insert(fp, Value::Object(prev));
            }
            save_store(sid, &mut store);
            let secs = (elapsed as i64).max(1);
            let reason = format!(
                "[Terser] Identical read-only MCP call `{display_name}` ran {secs}s ago. \
                 Reuse the earlier result, or retry if live state changed."
            );
            let raw_est = cached_resp_bytes.unwrap_or(2000).max(reason.len() + 1);
            record_savings("mcp_dedup", raw_est, reason.len(), host, display_name);
            return FunnelOutcome::Deny(reason);
        }
    }

    if let Some(Value::Object(mcp)) = store.get_mut("mcp") {
        mcp.insert(fp, json!({"tool": display_name, "ts": now, "soft_denies": 0}));
    }
    save_store(sid, &mut store);
    FunnelOutcome::Allow
}

pub fn record_mcp_response_size(sid: &str, display_name: &str, inner_args: &Value, resp_bytes: usize) {
    if sid.is_empty() || !valid_sid(sid) || display_name.is_empty() || resp_bytes == 0 {
        return;
    }
    let payload = json!({"tool": display_name, "args": inner_args});
    let canon = canonical_json(&payload);
    let fp = sha256_hex(canon.as_bytes())[..24].to_string();
    let mut store = load_store(sid);
    let updated = if let Some(Value::Object(mcp)) = store.get_mut("mcp") {
        if let Some(Value::Object(prev)) = mcp.get_mut(&fp) {
            prev.insert("resp_bytes".into(), json!(resp_bytes));
            true
        } else {
            false
        }
    } else {
        false
    };
    if updated {
        save_store(sid, &mut store);
    }
}
