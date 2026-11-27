use crate::config::{
    atomic_write_json, canonical_json, now_secs, record_funnel_event, record_savings,
    safe_sid_filename, state_home, tier_backed_off, valid_sid, Config,
};
use crate::difflib::{py_slice, splitlines_keepends, unified_diff};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};

const MAX_FILE_BYTES: u64 = 1024 * 1024;
const MAX_SNAPSHOT_BYTES: usize = 128 * 1024;
const MAX_STORE_ENTRIES: usize = 160;
const SOFT_DENY_CAP: i64 = 1;
const DELTA_MAX_DIFF_LINES: usize = 60;
const DELTA_MAX_RATIO: f64 = 0.40;
const BLOB_DIR: &str = "blobs";

const MUTATING_OR_LIVE_MCP_TOKENS: &[&str] = &[
    "write",
    "edit",
    "create",
    "delete",
    "remove",
    "drop",
    "update",
    "patch",
    "put",
    "post",
    "send",
    "submit",
    "publish",
    "deploy",
    "run",
    "exec",
    "execute",
    "invoke",
    "trigger",
    "start",
    "stop",
    "kill",
    "cancel",
    "status",
    "poll",
    "wait",
    "watch",
    "tail",
    "logs",
    "time",
    "now",
    "clock",
    "health",
    "ping",
    "refresh",
    "invalidate",
    "register",
    "index",
    // UI / browser / device / session tools whose result reflects state other calls change.
    "computer",
    "screenshot",
    "screen",
    "snapshot",
    "capture",
    "page",
    "tab",
    "tabs",
    "navigate",
    "click",
    "scroll",
    "type",
    "key",
    "hover",
    "drag",
    "console",
    "network",
    "javascript",
    "eval",
    "evaluate",
    "messages",
    "inbox",
    "notifications",
    "events",
    "current",
    "latest",
    "recent",
    "live",
    "session",
    "sessions",
    "terminal",
    "clipboard",
    "query",
    "sql",
    "select",
];

/// Server names whose tools observe or drive live state. Mirrors `_LIVE_MCP_SERVER_TOKENS`.
const LIVE_MCP_SERVER_TOKENS: &[&str] = &[
    "browser",
    "chrome",
    "playwright",
    "puppeteer",
    "selenium",
    "computer",
    "desktop",
    "terminal",
    "devtools",
    "screen",
    "android",
    "adb",
    "ios",
    "simulator",
    "emulator",
    "frida",
    "gdb",
    "debug",
    "debugger",
    "rizin",
    "rea",
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

/// Record a `retry` when the model re-requested content right after a short funnel answer.
fn note_retry(prev: &mut Map<String, Value>, soft_denies: i64, host: &str) {
    const RETRY_WINDOW_S: f64 = 180.0;
    if let Some(Value::String(tier)) = prev.remove("last_tier") {
        let ts = prev.get("ts").and_then(|v| v.as_f64()).unwrap_or(0.0);
        if soft_denies >= SOFT_DENY_CAP && now_secs() - ts <= RETRY_WINDOW_S {
            record_funnel_event(&tier, "retry", host);
        }
    }
}

fn save_store(sid: &str, store: &mut Map<String, Value>) {
    for bucket_key in ["reads", "mcp", "searches"] {
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
    externalize_snapshots(store);
    atomic_write_json(&cache_path(sid), &Value::Object(store.clone()));
}

fn blob_path(digest: &str) -> PathBuf {
    state_home()
        .join("read-cache")
        .join(BLOB_DIR)
        .join(format!("{digest}.txt"))
}

/// Store `text` content-addressed by the SHA-256 of its UTF-8 bytes (shared with Python).
fn write_blob(text: &str) -> Option<String> {
    let digest = sha256_hex(text.as_bytes());
    let p = blob_path(&digest);
    if p.is_file() {
        // Refresh mtime so the TTL sweep keeps referenced blobs alive.
        if let Ok(f) = fs::OpenOptions::new().append(true).open(&p) {
            let _ = f.set_modified(std::time::SystemTime::now());
        }
        return Some(digest);
    }
    let dir = p.parent()?;
    fs::create_dir_all(dir).ok()?;
    let tmp = dir.join(format!("{digest}.tmp.{}", std::process::id()));
    fs::write(&tmp, text.as_bytes()).ok()?;
    fs::rename(&tmp, &p).ok()?;
    Some(digest)
}

/// An entry's cached snapshot: inline (legacy) or from its SHA-verified blob.
fn snapshot_text(entry: &Map<String, Value>) -> Option<String> {
    if let Some(Value::String(s)) = entry.get("snapshot") {
        return Some(s.clone());
    }
    let digest = entry.get("snapshot_sha")?.as_str()?;
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    {
        return None;
    }
    let data = fs::read(blob_path(digest)).ok()?;
    if sha256_hex(&data) != digest {
        return None;
    }
    String::from_utf8(data).ok()
}

fn has_snapshot(entry: &Map<String, Value>) -> bool {
    matches!(entry.get("snapshot"), Some(Value::String(_)))
        || matches!(entry.get("snapshot_sha"), Some(Value::String(_)))
}

/// Move inline snapshots into shared blobs so each hook parses and rewrites a small store.
fn externalize_snapshots(store: &mut Map<String, Value>) {
    let Some(Value::Object(reads)) = store.get_mut("reads") else {
        return;
    };
    for entry in reads.values_mut() {
        let Value::Object(e) = entry else { continue };
        let Some(Value::String(snap)) = e.get("snapshot") else {
            continue;
        };
        if let Some(digest) = write_blob(snap) {
            e.remove("snapshot");
            e.insert("snapshot_sha".into(), Value::String(digest));
        }
    }
}

/// Python `str.isspace()` (Unicode White_Space plus the C0 separators U+001C..U+001F).
fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Unified diff of the requested window (byte-identical to Python `difflib`), or `None`
/// when the diff is too long or too dense to be worth sending.
fn compact_unified_diff(
    old_text: &str,
    new_text: &str,
    basename: &str,
    start_line: Option<i64>,
    end_line: Option<i64>,
) -> Option<String> {
    let s = start_line
        .filter(|&v| v != 0)
        .map(|v| (v - 1).max(0))
        .unwrap_or(0);
    let e = end_line.filter(|&v| v != 0);
    let old_lines = splitlines_keepends(old_text);
    let new_lines = splitlines_keepends(new_text);
    let old_slice = py_slice(&old_lines, Some(s), e);
    let new_slice = py_slice(&new_lines, Some(s), e);
    if new_slice.is_empty() {
        return None;
    }
    let diff = unified_diff(
        old_slice,
        new_slice,
        &format!("a/{basename}"),
        &format!("b/{basename}"),
        2,
    );
    let too_long = diff.len() > DELTA_MAX_DIFF_LINES;
    let too_dense =
        new_slice.len() >= 20 && diff.len() as f64 > DELTA_MAX_RATIO * new_slice.len() as f64;
    if diff.is_empty() || too_long || too_dense {
        return None;
    }
    Some(diff.concat().trim_end_matches(py_isspace).to_string())
}

/// True when the requested span lies inside the span already delivered (`None` = open end).
fn range_covered(
    prev_start: Option<i64>,
    prev_end: Option<i64>,
    start: Option<i64>,
    end: Option<i64>,
) -> bool {
    let have = (prev_start.unwrap_or(1), prev_end.unwrap_or(i64::MAX));
    let want = (start.unwrap_or(1), end.unwrap_or(i64::MAX));
    have.0 <= want.0 && want.1 <= have.1
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
    range_covered(
        opt_i64(prev.get("start")),
        opt_i64(prev.get("end")),
        start,
        end,
    )
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
                    last.1 = e.map(|ne| pe.max(ne));
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
    Value::Array(ivs.iter().map(|(s, e)| json!([s, e])).collect())
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
            if soft_denies < SOFT_DENY_CAP && !cfg.shadow && !tier_backed_off("t0", host) {
                prev.insert("soft_denies".into(), json!(soft_denies + 1));
                prev.insert("ts".into(), json!(now));
                prev.insert("last_tier".into(), Value::String("t0".into()));
                record_funnel_event("t0", "served", host);
                if let Some(Value::Object(reads)) = store.get_mut("reads") {
                    reads.insert(key, Value::Object(prev));
                }
                save_store(sid, &mut store);
                let rng = format_range(start_line, end_line);
                let reason = format!(
                    "[Terser] `{basename}` ({rng}) is unchanged since your earlier read. \
                     Reuse the content already in context, or retry to refresh (e.g. after /compact)."
                );
                record_savings(
                    "read_dedup",
                    meta.len() as usize,
                    reason.len(),
                    host,
                    &basename,
                );
                return FunnelOutcome::Deny(reason);
            }
            note_retry(&mut prev, soft_denies, host);
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
            note_retry(&mut prev, soft_denies, host);
            let ivs = merge_intervals(None, start_line, end_line);
            prev.insert("outline_only".into(), Value::Bool(false));
            prev.insert("sha".into(), Value::String(sha));
            if let Some(m) = mtime_ns {
                prev.insert("mtime_ns".into(), json!(m));
            }
            prev.insert("size".into(), json!(file_size));
            prev.insert(
                "start".into(),
                start_line.map(Into::into).unwrap_or(Value::Null),
            );
            prev.insert(
                "end".into(),
                end_line.map(Into::into).unwrap_or(Value::Null),
            );
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
            if soft_denies < SOFT_DENY_CAP && !cfg.shadow && !tier_backed_off("t0", host) {
                prev.insert("soft_denies".into(), json!(soft_denies + 1));
                prev.insert("ts".into(), json!(now));
                prev.insert("last_tier".into(), Value::String("t0".into()));
                record_funnel_event("t0", "served", host);
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
            note_retry(&mut prev, soft_denies, host);
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
            && !tier_backed_off("t0b", host)
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

        // Tier 1: File changed slightly & snapshot exists -> exact unified diff natively;
        // larger changes delegate to Python (symbol-region delta, outline, passthrough).
        if cfg.delta_diff
            && prev_sha != sha
            && soft_denies < SOFT_DENY_CAP
            && !cfg.shadow
            && text_snapshot.is_some()
            && has_snapshot(&prev)
            && intervals_cover(&prev, start_line, end_line)
        {
            let diff = match (text_snapshot.as_deref(), snapshot_text(&prev)) {
                _ if tier_backed_off("t1", host) => None,
                (Some(new_text), Some(old_text)) => {
                    compact_unified_diff(&old_text, new_text, &basename, start_line, end_line)
                }
                _ => None,
            };
            let Some(diff) = diff else {
                return FunnelOutcome::Delegate;
            };
            let reason = format!(
                "[Terser] `{basename}` changed slightly since your last read. \
                 Exact unified diff:\n```diff\n{diff}\n```\n(Retry read to load the full file.)"
            );
            prev.insert("sha".into(), Value::String(sha));
            match mtime_ns {
                Some(m) => prev.insert("mtime_ns".into(), json!(m)),
                None => prev.remove("mtime_ns"),
            };
            prev.insert("size".into(), json!(file_size));
            prev.remove("snapshot_sha");
            if let Some(new_text) = text_snapshot {
                prev.insert("snapshot".into(), Value::String(new_text));
            }
            prev.insert("soft_denies".into(), json!(soft_denies + 1));
            prev.insert("ts".into(), json!(now));
            prev.insert("last_tier".into(), Value::String("t1".into()));
            if let Some(Value::Object(reads)) = store.get_mut("reads") {
                reads.insert(key, Value::Object(prev));
            }
            save_store(sid, &mut store);
            record_savings("read_delta", raw_bytes.len(), reason.len(), host, &basename);
            record_funnel_event("t1", "served", host);
            return FunnelOutcome::Deny(reason);
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
            && !tier_backed_off("t2", host)
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
                        entry.insert("last_tier".into(), Value::String("t2".into()));
                        entry.insert("ts".into(), json!(now));
                        record_funnel_event("t2", "served", host);
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
                        record_savings(
                            "read_outline",
                            raw_bytes.len(),
                            reason.len(),
                            host,
                            &basename,
                        );
                        return FunnelOutcome::Deny(reason);
                    }
                }
                return FunnelOutcome::Delegate;
            }
        }
    }

    // Tier 3: First read / ranged read / wider range / large change -> record disjoint intervals & allow
    if let Some(Value::Object(reads)) = store.get_mut("reads") {
        if let Some(Value::Object(prev)) = reads.get_mut(&key) {
            let sd = opt_i64(prev.get("soft_denies")).unwrap_or(0);
            note_retry(prev, sd, host);
        }
    }
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
    entry.insert(
        "start".into(),
        legacy_start.map(Into::into).unwrap_or(Value::Null),
    );
    entry.insert(
        "end".into(),
        legacy_end.map(Into::into).unwrap_or(Value::Null),
    );
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

/// `(server, tool)` for `server:tool`, `mcp__server__tool` or a bare name. Mirrors `_split_mcp_name`.
fn split_mcp_name(name: &str) -> (String, String) {
    let (mut server, mut tool_part) = match name.rsplit_once(':') {
        Some((s, t)) => (s.to_string(), t.to_string()),
        None => (String::new(), name.to_string()),
    };
    if let Some((head, t)) = tool_part.clone().rsplit_once("__") {
        if server.is_empty() {
            server = head.to_string();
        }
        tool_part = t.to_string();
    }
    if let Some(rest) = server.strip_prefix("mcp__") {
        server = rest.to_string();
    }
    (server, tool_part)
}

fn has_token(text: &str, list: &[&str]) -> bool {
    text.to_ascii_lowercase()
        .split(|c: char| !c.is_ascii_alphanumeric())
        .filter(|t| !t.is_empty())
        .any(|t| list.contains(&t))
}

fn is_mutating_or_live_mcp(name: &str) -> bool {
    let (server, tool_part) = split_mcp_name(name);
    has_token(&tool_part, MUTATING_OR_LIVE_MCP_TOKENS) || has_token(&server, LIVE_MCP_SERVER_TOKENS)
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
    if display_name.is_empty() {
        return FunnelOutcome::Allow;
    }
    if is_mutating_or_live_mcp(display_name) {
        // A mutating or live call can change what any earlier read-only call would return.
        let mut store = load_store(sid);
        let has_entries = store
            .get("mcp")
            .and_then(|v| v.as_object())
            .is_some_and(|m| !m.is_empty());
        if has_entries {
            store.insert("mcp".into(), Value::Object(Map::new()));
            save_store(sid, &mut store);
        }
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
        mcp.insert(
            fp,
            json!({"tool": display_name, "ts": now, "soft_denies": 0}),
        );
    }
    save_store(sid, &mut store);
    FunnelOutcome::Allow
}

pub fn record_mcp_response_size(
    sid: &str,
    display_name: &str,
    inner_args: &Value,
    resp_bytes: usize,
) {
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
