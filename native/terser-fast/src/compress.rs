/// Lossless / error-preserving native output compression stages:
///   1. ANSI & OSC-8 escape + carriage-return spinner stripping
///   2. Git lockfile / minified-asset diff elision
///   3. Stream filters (Cargo, Go, pytest, npm/pnpm/uv/pip)
///   4. Consecutive duplicate-line collapsing (never touches errors/warnings/diffs)
///
/// Entry point for stage 1:
pub fn strip_ansi_and_progress(text: &str) -> String {
    if text.is_empty() {
        return String::new();
    }
    let bytes = text.as_bytes();
    let n = bytes.len();
    let mut out = String::with_capacity(n);
    let mut i = 0;

    while i < n {
        if bytes[i] == 0x1b && i + 1 < n {
            match bytes[i + 1] {
                b'[' => {
                    // CSI sequence: ESC [ [0-?]* [ -/]* [@-~]
                    i += 2;
                    while i < n && !(0x40..=0x7e).contains(&bytes[i]) {
                        i += 1;
                    }
                    if i < n {
                        i += 1;
                    }
                    continue;
                }
                b']' => {
                    // OSC sequence (including OSC 8 hyperlinks: preserve visible text after BEL)
                    i += 2;
                    while i < n {
                        if bytes[i] == 0x07 {
                            i += 1;
                            break;
                        }
                        if bytes[i] == 0x1b && i + 1 < n && bytes[i + 1] == b'\\' {
                            i += 2;
                            break;
                        }
                        i += 1;
                    }
                    continue;
                }
                _ => {
                    i += 2;
                    continue;
                }
            }
        }
        // Safe UTF-8 char step
        let ch = text[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }

    if !out.contains('\r') {
        return out;
    }

    let normalized = out.replace("\r\n", "\n");
    let mut lines: Vec<&str> = Vec::new();
    for line in normalized.split('\n') {
        if line.contains('\r') {
            let last = line
                .split('\r')
                .rev()
                .find(|seg| !seg.trim().is_empty())
                .unwrap_or("");
            lines.push(last);
        } else {
            lines.push(line);
        }
    }
    lines.join("\n")
}

pub fn is_signal_line(line: &str) -> bool {
    let trimmed = line.trim_start();
    if trimmed.starts_with("FAILED ")
        || trimmed.starts_with("ERROR ")
        || trimmed.starts_with("E   ")
        || trimmed.starts_with("File \"")
        || trimmed.starts_with("@@ ")
        || trimmed.starts_with("--- ")
        || trimmed.starts_with("+++ ")
        || trimmed.starts_with("diff --git ")
    {
        return true;
    }
    let low = line.to_ascii_lowercase();
    for kw in [
        "error",
        "failed",
        "fail:",
        "exception",
        "traceback",
        "panic",
        "assertion",
        "fatal",
        "warning",
        "warn:",
        " passed",
        " failed",
    ] {
        if low.contains(kw) {
            return true;
        }
    }
    false
}

fn is_lockfile_path(diff_header: &str) -> bool {
    let low = diff_header.to_ascii_lowercase();
    for name in [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
        ".min.js",
        ".min.css",
        ".map",
        ".pb.go",
        "_pb2.py",
        ".snap",
        ".svg",
        "/dist/",
        "a/dist/",
    ] {
        if low.contains(name) {
            return true;
        }
    }
    false
}

pub fn elide_lockfile_diffs(text: &str) -> String {
    if !text.contains("diff --git ") {
        return text.to_string();
    }
    let mut out: Vec<String> = Vec::new();
    let mut in_lockfile = false;
    let mut elided_count: usize = 0;

    for line in text.lines() {
        if line.starts_with("diff --git ") {
            if in_lockfile && elided_count > 0 {
                out.push(format!(
                    "  [... elided lockfile/generated diff ({elided_count} lines) ...]"
                ));
            }
            elided_count = 0;
            in_lockfile = is_lockfile_path(line);
            out.push(line.to_string());
        } else if in_lockfile {
            elided_count += 1;
        } else {
            out.push(line.to_string());
        }
    }
    if in_lockfile && elided_count > 0 {
        out.push(format!(
            "  [... elided lockfile/generated diff ({elided_count} lines) ...]"
        ));
    }
    out.join("\n")
}

pub fn compact_diff_headers(text: &str) -> String {
    if !text.contains("diff --git ") {
        return text.to_string();
    }
    let lines: Vec<&str> = text.lines().collect();
    let mut out: Vec<String> = Vec::with_capacity(lines.len());
    let mut i = 0;
    while i < lines.len() {
        let line = lines[i];
        if let Some(rest) = line.strip_prefix("diff --git a/") {
            if let Some((path_a, path_b)) = rest.split_once(" b/") {
                let mut j = i + 1;
                let mut mode_tag = "";
                while j < lines.len() {
                    let ln = lines[j];
                    if ln.starts_with("index ") && ln.contains("..") {
                        j += 1;
                    } else if ln.starts_with("new file mode ") {
                        mode_tag = " (new)";
                        j += 1;
                    } else if ln.starts_with("deleted file mode ") {
                        mode_tag = " (deleted)";
                        j += 1;
                    } else if ln == format!("--- a/{path_a}") || ln == "--- /dev/null" {
                        if ln == "--- /dev/null" {
                            mode_tag = " (new)";
                        }
                        j += 1;
                    } else if ln == format!("+++ b/{path_b}") || ln == "+++ /dev/null" {
                        if ln == "+++ /dev/null" {
                            mode_tag = " (deleted)";
                        }
                        j += 1;
                    } else {
                        break;
                    }
                }
                if j > i + 1 {
                    let hdr = if path_a == path_b {
                        format!("=== {path_b}{mode_tag}")
                    } else {
                        format!("=== {path_a} -> {path_b}{mode_tag}")
                    };
                    out.push(hdr);
                    i = j;
                    continue;
                }
            }
        }
        out.push(line.to_string());
        i += 1;
    }
    out.join("\n")
}

pub fn apply_stream_filters(text: &str, command: &str) -> String {
    let cmd_low = command.to_ascii_lowercase();
    let is_cargo = cmd_low.contains("cargo");
    let is_go = cmd_low.starts_with("go ") || cmd_low.contains(" go ");
    let is_test = cmd_low.contains("pytest")
        || cmd_low.contains("unittest")
        || cmd_low.contains("jest")
        || cmd_low.contains("vitest");
    let is_pkg = cmd_low.contains("npm ")
        || cmd_low.contains("pnpm ")
        || cmd_low.contains("yarn ")
        || cmd_low.contains("pip ")
        || cmd_low.contains("uv ");
    let is_infra = cmd_low.contains("docker")
        || cmd_low.contains("terraform")
        || cmd_low.contains("gradle")
        || cmd_low.contains("dotnet");

    if !is_cargo && !is_go && !is_test && !is_pkg && !is_infra {
        return text.to_string();
    }

    let mut out: Vec<&str> = Vec::new();
    let mut dropped: usize = 0;

    for line in text.lines() {
        let t = line.trim_start();
        let test_pass = is_test
            && ((t.contains("::")
                && (t.ends_with(" PASSED") || t.contains(" PASSED [") || t.ends_with(" SKIPPED")))
                || (t.starts_with("test_") && t.ends_with("... ok"))
                || t.starts_with("✓ ")
                || t.starts_with("PASS "));
        if !test_pass && is_signal_line(line) {
            out.push(line);
            continue;
        }
        let noise = test_pass
            || (is_cargo
                && (t.starts_with("Compiling ")
                    || t.starts_with("Downloading ")
                    || t.starts_with("Downloaded ")
                    || t.starts_with("Updating ")
                    || t.starts_with("Fresh ")
                    || t.starts_with("Checking ")
                    || t.starts_with("Blocking waiting for file lock")))
            || (is_go
                && (t.starts_with("=== RUN")
                    || t.starts_with("--- PASS:")
                    || (t.starts_with("? ") && t.contains("[no test files]"))))
            || (is_test
                && (t.starts_with("platform ")
                    || t.starts_with("rootdir:")
                    || t.starts_with("plugins:")
                    || t.starts_with("cachedir:")
                    || t.starts_with("collecting ...")))
            || (is_pkg
                && (t.starts_with("Downloading ")
                    || t.starts_with("Using cached ")
                    || t.starts_with("Requirement already satisfied")
                    || t.starts_with("Progress:")
                    || t.starts_with("Resolving ")))
            || (is_infra
                && (t.contains("Already exists")
                    || t.contains("Pulling fs layer")
                    || t.contains("Refreshing state...")
                    || t.contains("UP-TO-DATE")));

        if noise {
            dropped += 1;
        } else {
            out.push(line);
        }
    }

    if dropped == 0 {
        return text.to_string();
    }
    let mut joined = out.join("\n");
    if !joined.is_empty() {
        joined.push('\n');
    }
    joined.push_str(&format!(
        "[Terser: filtered {dropped} noisy build/progress lines]"
    ));
    joined
}

fn normalize_for_dedup(line: &str) -> String {
    // Strip digits so timestamped/numbered identical log lines collapse cleanly
    line.trim()
        .chars()
        .map(|c| if c.is_ascii_digit() { '#' } else { c })
        .collect()
}

pub fn collapse_duplicate_lines(text: &str, min_run: usize) -> String {
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() < min_run {
        return text.to_string();
    }
    let mut out: Vec<String> = Vec::with_capacity(lines.len());
    let mut i = 0;
    let n = lines.len();

    while i < n {
        let line = lines[i];
        if is_signal_line(line) {
            out.push(line.to_string());
            i += 1;
            continue;
        }
        if line.trim().is_empty() {
            let mut j = i + 1;
            while j < n && lines[j].trim().is_empty() {
                j += 1;
            }
            out.push(String::new());
            i = j;
            continue;
        }
        let norm = normalize_for_dedup(line);
        let mut j = i + 1;
        while j < n && !is_signal_line(lines[j]) && normalize_for_dedup(lines[j]) == norm {
            j += 1;
        }
        let run_len = j - i;
        if run_len >= min_run {
            out.push(line.to_string());
            out.push(format!("  [... repeated {}x more ...]", run_len - 1));
        } else {
            out.extend(lines[i..j].iter().map(|l| l.to_string()));
        }
        i = j;
    }
    out.join("\n")
}

pub fn dedup_compiler_notes(text: &str) -> String {
    if text.is_empty() || (!text.contains("note:") && !text.contains("help:")) {
        return text.to_string();
    }
    let mut seen: std::collections::HashMap<String, usize> = std::collections::HashMap::new();
    let mut out: Vec<&str> = Vec::new();
    let mut omitted: usize = 0;

    for ln in text.lines() {
        let mut s = ln.trim_start();
        if let Some(rest) = s.strip_prefix('=') {
            s = rest.trim_start();
        }
        let payload = s
            .strip_prefix("note:")
            .or_else(|| s.strip_prefix("help:"))
            .map(str::trim);
        if let Some(p) = payload {
            let norm: String = p.split_whitespace().collect::<Vec<_>>().join(" ");
            if norm.len() >= 24 {
                let cnt = seen.entry(norm).or_insert(0);
                *cnt += 1;
                if *cnt >= 2 {
                    omitted += 1;
                    continue;
                }
            }
        }
        out.push(ln);
    }

    if omitted >= 2 {
        let mut joined = out.join("\n");
        if !joined.is_empty() {
            joined.push('\n');
        }
        joined.push_str(&format!(
            "[Terser: {omitted} repeated compiler note/help lines deduplicated]"
        ));
        return joined;
    }
    text.to_string()
}

fn is_framework_frame(line: &str) -> bool {
    const MARKERS: &[&str] = &[
        "site-packages/",
        "site-packages\\",
        "dist-packages/",
        "dist-packages\\",
        "node_modules/",
        "node_modules\\",
        "node:internal/",
        "Lib/unittest/",
        "Lib\\unittest\\",
        "Lib/asyncio/",
        "Lib\\asyncio\\",
        "Lib/importlib/",
        "Lib\\importlib\\",
        "Lib/runpy/",
        "Lib\\runpy\\",
        "Lib/threading/",
        "Lib\\threading\\",
        "Lib/concurrent/",
        "Lib\\concurrent\\",
        "/usr/lib/python",
        "/Library/Frameworks/Python",
    ];
    MARKERS.iter().any(|m| line.contains(m))
}

pub fn fold_framework_stack_frames(text: &str, min_run: usize) -> String {
    if !text.contains("Traceback") && !text.contains("    at ") {
        return text.to_string();
    }
    let lines: Vec<&str> = text.lines().collect();
    let n = lines.len();
    let mut out: Vec<String> = Vec::with_capacity(n);
    let mut i = 0;

    while i < n {
        let ln = lines[i];
        let trim = ln.trim_start();
        let is_py_fw = trim.starts_with("File \"") && is_framework_frame(ln);
        let is_js_fw = trim.starts_with("at ") && is_framework_frame(ln);
        if is_py_fw || is_js_fw {
            let start = i;
            let mut frames = 0;
            while i < n {
                let cur = lines[i];
                let ct = cur.trim_start();
                if ct.starts_with("File \"") && is_framework_frame(cur) {
                    frames += 1;
                    i += 1;
                    if i < n
                        && lines[i].starts_with("    ")
                        && !lines[i].trim_start().starts_with("File \"")
                    {
                        i += 1;
                    }
                } else if ct.starts_with("at ") && is_framework_frame(cur) {
                    frames += 1;
                    i += 1;
                } else {
                    break;
                }
            }
            if frames >= min_run {
                out.push(format!(
                    "  [... {frames} framework/stdlib stack frames omitted ...]"
                ));
            } else {
                out.extend(lines[start..i].iter().map(|l| l.to_string()));
            }
        } else {
            out.push(ln.to_string());
            i += 1;
        }
    }
    out.join("\n")
}

pub fn elide_workspace_paths(text: &str, workspace: Option<&str>) -> String {
    if text.len() < 400 {
        return text.to_string();
    }
    let ws_raw = match workspace {
        Some(w) => w.trim_end_matches(['/', '\\']),
        None => return text.to_string(),
    };
    if ws_raw.len() < 12 {
        return text.to_string();
    }
    let slash = format!("{}/", ws_raw.replace('\\', "/"));
    let bslash = format!("{}\\", ws_raw.replace('/', "\\"));
    let mut count = text.matches(&slash).count();
    if bslash != slash {
        count += text.matches(&bslash).count();
    }
    if count < 4 {
        return text.to_string();
    }
    let mut replaced = text.replace(&slash, "./");
    if bslash != slash {
        replaced = replaced.replace(&bslash, "./");
    }
    if replaced.len() + ws_raw.len() + 12 < text.len() {
        format!("[root: {ws_raw}]\n{replaced}")
    } else {
        text.to_string()
    }
}

fn is_aligned_table_row(line: &str) -> bool {
    let s = line.trim();
    if s.is_empty() {
        return false;
    }
    if matches!(s.as_bytes()[0], b'{' | b'[' | b'}' | b']' | b'#' | b'<') {
        return false;
    }
    let bytes = s.as_bytes();
    let mut runs = 0usize;
    let mut i = 0usize;
    while i < bytes.len() {
        if bytes[i] == b' ' {
            let start = i;
            while i < bytes.len() && bytes[i] == b' ' {
                i += 1;
            }
            if i - start >= 2 {
                runs += 1;
                if runs >= 2 {
                    return true;
                }
            }
        } else {
            i += 1;
        }
    }
    false
}

fn collapse_multi_spaces_to_tab(s: &str) -> String {
    let trimmed = s.trim();
    let mut out = String::with_capacity(trimmed.len());
    let mut space_run = 0usize;
    for ch in trimmed.chars() {
        if ch == ' ' {
            space_run += 1;
        } else {
            if space_run >= 2 {
                out.push('\t');
            } else if space_run == 1 {
                out.push(' ');
            }
            space_run = 0;
            out.push(ch);
        }
    }
    out
}

pub fn compact_aligned_tables(text: &str) -> String {
    if text.len() < 300 || text.contains("diff --git ") || text.contains("Traceback ") {
        return text.to_string();
    }
    let lines: Vec<&str> = text.lines().collect();
    if lines.len() < 4 {
        return text.to_string();
    }
    let mut out: Vec<String> = Vec::with_capacity(lines.len());
    let mut i = 0;
    let n = lines.len();
    let mut changed = false;

    while i < n {
        if is_aligned_table_row(lines[i]) {
            let mut j = i + 1;
            while j < n && is_aligned_table_row(lines[j]) {
                j += 1;
            }
            if j - i >= 4 {
                let raw_len: usize = lines[i..j].iter().map(|l| l.len() + 1).sum();
                let tsv_rows: Vec<String> = lines[i..j]
                    .iter()
                    .map(|l| collapse_multi_spaces_to_tab(l))
                    .collect();
                let tsv_len: usize = tsv_rows.iter().map(|l| l.len() + 1).sum();
                if tsv_len < (raw_len * 85) / 100 {
                    out.extend(tsv_rows);
                    changed = true;
                    i = j;
                    continue;
                }
            }
            out.extend(lines[i..j].iter().map(|l| l.to_string()));
            i = j;
        } else {
            out.push(lines[i].to_string());
            i += 1;
        }
    }
    if changed {
        out.join("\n")
    } else {
        text.to_string()
    }
}

pub fn error_pinned_window(text: &str, max_chars: usize) -> String {
    if text.len() <= max_chars {
        return text.to_string();
    }
    let lines: Vec<&str> = text.lines().collect();
    let head_n = 30.min(lines.len());
    let tail_n = 40.min(lines.len().saturating_sub(head_n));
    let head = &lines[..head_n];
    let mid_end = lines.len() - tail_n;
    let mid = &lines[head_n..mid_end];
    let tail = &lines[mid_end..];

    let mut pinned: Vec<String> = Vec::new();
    let pinned_budget = (max_chars / 2).max(2000);
    let mut used = 0usize;
    for (offset, ln) in mid.iter().enumerate() {
        if is_signal_line(ln) {
            let entry = format!("L{}: {ln}", head_n + offset + 1);
            if used + entry.len() < pinned_budget {
                used += entry.len() + 1;
                pinned.push(entry);
            }
        }
    }
    let omitted = mid.len().saturating_sub(pinned.len());
    let mut parts: Vec<String> = head.iter().map(|s| (*s).to_string()).collect();
    if omitted > 0 {
        parts.push(format!(
            "... [{omitted} routine middle lines omitted; {} signal lines preserved] ...",
            pinned.len()
        ));
    }
    parts.extend(pinned);
    if !tail.is_empty() {
        parts.push("--- tail ---".to_string());
        parts.extend(tail.iter().map(|s| (*s).to_string()));
    }
    let res = parts.join("\n");
    if res.len() > max_chars {
        let half = ((max_chars.saturating_sub(80)) / 2).max(500);
        let prefix: String = res.chars().take(half).collect();
        let suffix: String = res
            .chars()
            .rev()
            .take(half)
            .collect::<String>()
            .chars()
            .rev()
            .collect();
        return format!("{prefix}\n... [truncated] ...\n{suffix}");
    }
    res
}

pub fn compress_output_with_ws(text: &str, command: &str, workspace: Option<&str>) -> String {
    let s1 = strip_ansi_and_progress(text);
    let s2 = elide_lockfile_diffs(&s1);
    let s2 = compact_diff_headers(&s2);
    let s3 = fold_framework_stack_frames(&s2, 3);
    let s4 = dedup_compiler_notes(&s3);
    let s5 = compact_aligned_tables(&s4);
    let s6 = apply_stream_filters(&s5, command);
    let s7 = elide_workspace_paths(&s6, workspace);
    collapse_duplicate_lines(&s7, 3)
}

/// Size above which even an explicitly scoped read gets the error-pinned window.
pub const SCOPED_READ_MAX: usize = 40_000;

/// True when the command already selects a line range (`sed -n A,Bp`, `head -n N`,
/// `tail -n N`, `awk 'NR..'`, PowerShell `-TotalCount`/`-Tail`/`Select-Object -First`).
/// The agent asked for exactly those lines, so they are not elided mid-output.
pub fn is_scoped_read(command: &str) -> bool {
    let low = command.to_ascii_lowercase();
    let toks: Vec<&str> = low.split_whitespace().collect();
    for (i, t) in toks.iter().enumerate() {
        let next = toks.get(i + 1).copied().unwrap_or("");
        let bare = t.trim_start_matches(['|', '(']);
        match bare {
            "sed" if next == "-n" => {
                let arg = toks
                    .get(i + 2)
                    .copied()
                    .unwrap_or("")
                    .trim_matches(|c| c == '\'' || c == '"');
                if arg.ends_with('p') && arg[..arg.len() - 1].chars().any(|c| c.is_ascii_digit()) {
                    return true;
                }
            }
            "head" | "tail" => {
                if next == "-n"
                    || next.starts_with("-n")
                    || (next.len() > 1
                        && next[1..].chars().all(|c| c.is_ascii_digit())
                        && next.starts_with('-'))
                {
                    return true;
                }
            }
            "awk" if next.trim_start_matches(['\'', '"']).starts_with("nr") => return true,
            "-totalcount" | "-tail" | "-head" | "-first" | "-last" | "-index" => return true,
            _ => {}
        }
    }
    false
}

/// The window cap for a command's output: raised for explicitly scoped reads.
pub fn window_limit(command: &str, default: usize) -> usize {
    if is_scoped_read(command) {
        default.max(SCOPED_READ_MAX)
    } else {
        default
    }
}

pub fn compress_output(text: &str, command: &str) -> String {
    compress_output_with_ws(text, command, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scoped_reads_are_detected() {
        assert!(is_scoped_read("sed -n '10,50p' README.md"));
        assert!(is_scoped_read("cat a.log | head -n 40"));
        assert!(is_scoped_read("tail -200 build.log"));
        assert!(is_scoped_read("Get-Content x.txt -TotalCount 30"));
        assert!(!is_scoped_read("cargo build"));
        assert!(!is_scoped_read("sed -i s/a/b/ f"));
        assert_eq!(window_limit("cargo test", 12000), 12000);
        assert_eq!(window_limit("head -n 5 f", 12000), SCOPED_READ_MAX);
    }

    #[test]
    fn strips_ansi_and_preserves_osc8_label() {
        let raw = "\x1b[32mOK\x1b[0m \x1b]8;;https://example.com\x07docs\x1b]8;;\x07\nstep 1\rstep 2\rstep 3";
        let out = strip_ansi_and_progress(raw);
        assert_eq!(out, "OK docs\nstep 3");
    }

    #[test]
    fn cargo_filter_preserves_errors() {
        let raw = "   Compiling a v0.1.0\n   Compiling b v0.1.0\n   Compiling c v0.1.0\nerror[E0308]: mismatched types\n    Finished dev";
        let out = apply_stream_filters(raw, "cargo build");
        assert!(out.contains("error[E0308]"));
        assert!(out.contains("Finished dev"));
        assert!(!out.contains("Compiling a"));
    }

    #[test]
    fn elides_lockfile_diffs_only() {
        let raw = "diff --git a/src/lib.rs b/src/lib.rs\n+fn hi() {}\ndiff --git a/Cargo.lock b/Cargo.lock\n+pkg1\n+pkg2\n+pkg3";
        let out = elide_lockfile_diffs(raw);
        assert!(out.contains("+fn hi() {}"));
        assert!(out.contains("elided lockfile/generated diff (3 lines)"));
        assert!(!out.contains("+pkg2"));
    }

    #[test]
    fn collapses_duplicate_non_signal_lines() {
        let raw = "2026-11-26 10:00:01 info tick\n2026-11-26 10:00:02 info tick\n2026-11-26 10:00:03 info tick\n2026-11-26 10:00:04 info tick\nERROR: keep me";
        let out = collapse_duplicate_lines(raw, 3);
        assert!(out.contains("repeated 3x more"));
        assert!(out.contains("ERROR: keep me"));
    }

    #[test]
    fn dedups_compiler_notes_and_folds_framework_frames() {
        let notes = "error: foo\n  = note: required by a bound in `std::convert::From`\nerror: bar\n  = note: required by a bound in `std::convert::From`\nerror: baz\n  = note: required by a bound in `std::convert::From`";
        let out = dedup_compiler_notes(notes);
        assert!(out.contains("2 repeated compiler note/help lines deduplicated"));
        assert!(out.contains("error: baz"));

        let tb = "Traceback (most recent call last):\n  File \"/app/main.py\", line 10, in run\n    go()\n  File \"/venv/site-packages/a.py\", line 1, in f1\n    x()\n  File \"/venv/site-packages/b.py\", line 2, in f2\n    y()\n  File \"/venv/site-packages/c.py\", line 3, in f3\n    z()\nValueError: bad";
        let folded = fold_framework_stack_frames(tb, 3);
        assert!(folded.contains("3 framework/stdlib stack frames omitted"));
        assert!(folded.contains("/app/main.py"));
        assert!(folded.contains("ValueError: bad"));
    }

    #[test]
    fn compacts_aligned_tables_and_elides_workspace() {
        let table = "CONTAINER ID        IMAGE               COMMAND             STATUS\n\
                     a1b2c3d4e5f6        postgres:16         \"docker-entry\"      Up 2 hours\n\
                     b2c3d4e5f6a1        redis:7             \"redis-server\"      Up 2 hours\n\
                     c3d4e5f6a1b2        nginx:latest        \"nginx -g\"          Up 2 hours\n\
                     d4e5f6a1b2c3        worker:dev          \"python app.py\"     Up 2 hours";
        let tsv = compact_aligned_tables(table);
        assert!(tsv.contains('\t'));
        assert!(tsv.len() < table.len());

        let ws = "/Users/dev/my-long-workspace-root";
        let paths = format!(
            "{ws}/src/a.rs:10: fn a\n{ws}/src/b.rs:20: fn b\n{ws}/src/c.rs:30: fn c\n{ws}/src/d.rs:40: fn d\n{}",
            "x".repeat(300)
        );
        let elided = elide_workspace_paths(&paths, Some(ws));
        assert!(elided.starts_with("[root: /Users/dev/my-long-workspace-root]\n"));
        assert!(elided.contains("./src/a.rs:10"));
    }

    #[test]
    fn compacts_git_diff_headers_losslessly() {
        let diff = "diff --git a/src/a.rs b/src/a.rs\nindex 111111..222222 100644\n--- a/src/a.rs\n+++ b/src/a.rs\n@@ -1,2 +1,2 @@\n-let x = 1;\n+let x = 2;\n";
        let out = compact_diff_headers(diff);
        assert!(out.contains("=== src/a.rs"));
        assert!(!out.contains("index 111111"));
        assert!(out.contains("@@ -1,2 +1,2 @@"));
        assert!(out.contains("+let x = 2;"));
    }
}
