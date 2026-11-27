//! Client for the warm Python hook worker (`unified_hook.py --serve-worker`).
//!
//! A delegated hook normally pays for a fresh interpreter plus module imports. When a worker
//! for this plugin checkout is listening, terser-fast hands it the exact same inputs (argv,
//! environment, cwd and stdin) and prints its reply. The worker runs the unchanged
//! `dispatch()` code, so output is identical to a cold run.
//!
//! Safety of the fallback: the worker answers `1` once it has *accepted* a request and `0`
//! when it refuses one (bad token, code changed on disk). Only a refusal or a failure before
//! the acknowledgement falls back to a cold run; a request that was accepted is never run a
//! second time, so stateful handlers cannot double-count. Each request also carries its
//! byte length and an acknowledgement deadline the worker enforces, so a truncated or
//! queued-too-long request is refused rather than run alongside a cold fallback.

use crate::config::{now_secs, state_home};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::{Duration, UNIX_EPOCH};

const CONNECT_TIMEOUT: Duration = Duration::from_millis(150);
/// How long a request may wait in the worker's queue. The worker refuses (never runs) a
/// request it picks up after `deadline - ACK_MARGIN_S`, so a cold fallback after this wait
/// can never overlap with a late worker run.
const ACK_WAIT_S: f64 = 1.5;
const WRITE_TIMEOUT: Duration = Duration::from_millis(1500);
const SPAWN_LEASE_SECONDS: f64 = 15.0;
const MAX_REPLY_BYTES: u64 = 8 * 1024 * 1024;

pub enum Outcome {
    /// The worker accepted the request; this is its complete stdout (possibly empty).
    Handled(Vec<u8>),
    /// The worker accepted the request but the reply was lost: fail open, print nothing.
    Lost,
    /// No worker, or it refused before running anything: safe to run cold.
    Unavailable,
}

pub fn worker_key(script: &str) -> String {
    let digest = Sha256::digest(script.as_bytes());
    digest.iter().take(6).map(|b| format!("{b:02x}")).collect()
}

fn info_path(key: &str) -> PathBuf {
    state_home().join(format!("hook-worker-{key}.json"))
}

/// Stay under each event's timeout in hooks/hooks.json, leaving room for process exit.
fn reply_timeout(event: &str) -> Duration {
    let secs = match event {
        "SubagentStop" => 3.5,
        "PreToolUse" | "PostToolUse" | "PostToolUseFailure" | "SubagentStart" | "CwdChanged" => 8.0,
        "StopFailure" => 10.0,
        _ => 13.0,
    };
    Duration::from_secs_f64(secs)
}

pub fn call(key: &str, host: Option<&str>, event: &str, raw_stdin: &[u8]) -> Outcome {
    let Ok(text) = fs::read_to_string(info_path(key)) else {
        return Outcome::Unavailable;
    };
    let Ok(Value::Object(info)) = serde_json::from_str::<Value>(&text) else {
        return Outcome::Unavailable;
    };
    let (Some(port), Some(token)) = (
        info.get("port").and_then(Value::as_u64),
        info.get("token").and_then(Value::as_str),
    ) else {
        return Outcome::Unavailable;
    };
    let Ok(port) = u16::try_from(port) else {
        return Outcome::Unavailable;
    };
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT) else {
        return Outcome::Unavailable;
    };
    let _ = stream.set_nodelay(true);
    let _ = stream.set_write_timeout(Some(WRITE_TIMEOUT));

    let env_map: Map<String, Value> = env::vars_os()
        .map(|(k, v)| {
            (
                k.to_string_lossy().into_owned(),
                Value::String(v.to_string_lossy().into_owned()),
            )
        })
        .collect();
    let cwd = env::current_dir()
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_default();
    let header = json!({
        "token": token,
        "host": host,
        "event": event,
        "cwd": cwd,
        "env": env_map,
        "len": raw_stdin.len(),
        "deadline": now_secs() + ACK_WAIT_S,
    });
    let Ok(mut msg) = serde_json::to_vec(&header) else {
        return Outcome::Unavailable;
    };
    msg.push(b'\n');
    msg.extend_from_slice(raw_stdin);
    if stream.write_all(&msg).is_err() || stream.shutdown(Shutdown::Write).is_err() {
        // The worker only acts after reading the full request up to EOF, so nothing ran.
        return Outcome::Unavailable;
    }

    let _ = stream.set_read_timeout(Some(Duration::from_secs_f64(ACK_WAIT_S)));
    let mut ack = [0u8; 1];
    match stream.read_exact(&mut ack) {
        Ok(()) if ack[0] == b'1' => {}
        _ => return Outcome::Unavailable,
    }
    let _ = stream.set_read_timeout(Some(reply_timeout(event)));
    let mut reply = Vec::new();
    match stream.take(MAX_REPLY_BYTES).read_to_end(&mut reply) {
        Ok(_) => Outcome::Handled(reply),
        Err(_) => Outcome::Lost,
    }
}

#[cfg(windows)]
fn disinherit_std_handles() {
    use std::os::windows::io::AsRawHandle;

    #[link(name = "kernel32")]
    extern "system" {
        fn SetHandleInformation(handle: *mut std::ffi::c_void, mask: u32, flags: u32) -> i32;
    }
    const HANDLE_FLAG_INHERIT: u32 = 0x1;
    let handles = [
        std::io::stdin().as_raw_handle(),
        std::io::stdout().as_raw_handle(),
        std::io::stderr().as_raw_handle(),
    ];
    for handle in handles {
        if !handle.is_null() {
            // SAFETY: `handle` is this process's own std handle, valid for the process
            // lifetime; clearing its inherit flag does not close or alias it.
            unsafe {
                SetHandleInformation(handle, HANDLE_FLAG_INHERIT, 0);
            }
        }
    }
}

/// Start a worker in the background (at most one attempt per lease window).
pub fn spawn(key: &str, python: &str, prefix_args: &[&str], script: &str) {
    let lease = state_home().join(format!("hook-worker-{key}.lease"));
    let recent = fs::metadata(&lease)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
        .map(|d| now_secs() - d.as_secs_f64() < SPAWN_LEASE_SECONDS)
        .unwrap_or(false);
    if recent {
        return;
    }
    if let Some(parent) = lease.parent() {
        let _ = fs::create_dir_all(parent);
    }
    if fs::write(&lease, b"").is_err() {
        return;
    }

    let build = || {
        let mut cmd = Command::new(python);
        cmd.args(prefix_args)
            .args([
                "-E",
                "-s",
                "-X",
                "utf8",
                script,
                "--serve-worker",
                "--worker-key",
                key,
            ])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        cmd
    };
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // Windows children inherit every inheritable handle, and the stdio pipes the host gave
        // this hook are inheritable. A long-lived worker holding them would keep the host
        // waiting for EOF on our stdout until the hook timed out. Python's `close_fds=True`
        // avoids this for its detached workers; here the handles are made non-inheritable
        // (later `Stdio::inherit` spawns duplicate them as inheritable, so they are unaffected).
        disinherit_std_handles();
        // Same flags as runtime/config.py::detached_spawn_kwargs (hidden console, own group).
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
        const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
        let mut cmd = build();
        cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB);
        if cmd.spawn().is_err() {
            let mut cmd = build();
            cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
            let _ = cmd.spawn();
        }
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        let mut cmd = build();
        cmd.process_group(0);
        let _ = cmd.spawn();
    }
    #[cfg(not(any(windows, unix)))]
    {
        let _ = build().spawn();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_key_is_stable_and_short() {
        assert_eq!(worker_key("a/b.py"), worker_key("a/b.py"));
        assert_ne!(worker_key("a/b.py"), worker_key("c/b.py"));
        assert_eq!(worker_key("x").len(), 12);
    }

    #[test]
    fn missing_worker_is_unavailable() {
        assert!(matches!(
            call(
                "no-such-worker-key-for-tests",
                Some("claude"),
                "Stop",
                b"{}"
            ),
            Outcome::Unavailable
        ));
    }
}
