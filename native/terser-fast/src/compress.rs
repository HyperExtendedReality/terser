/// Lossless / error-preserving native output compression stages:
///   1. ANSI & OSC-8 escape + carriage-return spinner stripping
///   2. Git lockfile / minified-asset diff elision
///   3. Stream filters (Cargo, Go, pytest, npm/pnpm/uv/pip)
///   4. Consecutive duplicate-line collapsing (never touches errors/warnings/diffs)

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
                out.push(format!("  [... elided lockfile/generated diff ({elided_count} lines) ...]"));
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
        out.push(format!("  [... elided lockfile/generated diff ({elided_count} lines) ...]"));
    }
    out.join("\n")
}

pub fn apply_stream_filters(text: &str, command: &str) -> String {
    let cmd_low = command.to_ascii_lowercase();
    let is_cargo = cmd_low.contains("cargo");
    let is_go = cmd_low.starts_with("go ") || cmd_low.contains(" go ");
    let is_pytest = cmd_low.contains("pytest");
    let is_pkg = cmd_low.contains("npm ")
        || cmd_low.contains("pnpm ")
        || cmd_low.contains("yarn ")
        || cmd_low.contains("pip ")
        || cmd_low.contains("uv ");

    if !is_cargo && !is_go && !is_pytest && !is_pkg {
        return text.to_string();
    }

    let mut out: Vec<&str> = Vec::new();
    let mut dropped: usize = 0;

    for line in text.lines() {
        if is_signal_line(line) {
            out.push(line);
            continue;
        }
        let t = line.trim_start();
        let noise = (is_cargo
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
            || (is_pytest
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
                    || t.starts_with("Resolving ")));

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
    joined.push_str(&format!("[Terser: filtered {dropped} noisy build/progress lines]"));
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
            for k in i..j {
                out.push(lines[k].to_string());
            }
        }
        i = j;
    }
    out.join("\n")
}

pub fn compress_output(text: &str, command: &str) -> String {
    let s1 = strip_ansi_and_progress(text);
    let s2 = elide_lockfile_diffs(&s1);
    let s3 = apply_stream_filters(&s2, command);
    collapse_duplicate_lines(&s3, 3)
}

#[cfg(test)]
mod tests {
    use super::*;

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
}
