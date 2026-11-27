"""Check Codex hook parity and compare observed usage from optional matched rollouts."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import config, index_mcp
from scripts.benchmark import _compare_codex_rollouts


def _fixtures(workspace: Path) -> list[tuple[str, dict]]:
    common = {"session_id": "terser-benchmark", "turn_id": "turn-1", "cwd": str(workspace),
              "model": "fixture", "permission_mode": "default", "transcript_path": None}
    shell = {"tool_name": "Bash", "tool_input": {"command": "cargo test"}, "tool_use_id": "shell-1"}
    return [
        ("PreToolUse", dict(common, tool_name="update_plan", tool_input={"plan": []}, tool_use_id="plan-1")),
        ("PreToolUse", dict(common, **shell)),
        ("PostToolUse", dict(common, **shell, tool_response={
            "stdout": "Running tests\n" + "test tests::fixture ... ok\n" * 40,
            "stderr": "error: fixture failure\n", "exit_code": 1,
        })),
        ("PostToolUse", dict(common, tool_name="mcp__plugin_terser_terser-index__search_symbols",
                            tool_input={"query": "fixture"}, tool_use_id="mcp-1",
                            tool_response={"isError": True, "content": [{"type": "text", "text": "Query failed"}]})),
        ("SubagentStart", dict(common, agent_id="agent-1", agent_type="worker")),
    ]


def _run_fixture(command: list[str], event: str, payload: dict, state: str) -> dict:
    # Give each sample a fresh state directory to keep loop-guard results deterministic.
    with tempfile.TemporaryDirectory(dir=state) as work:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TERSER_")}
        env["TERSER_HOME"] = work
        env["TERSER_PYTHON_WORKER"] = "0"  # A fresh state dir per sample: a warm worker never pays off
        run = subprocess.run(
            [*command, "--host", "codex", "--event", event],
            input=json.dumps(dict(payload, hook_event_name=event)), text=True, encoding="utf-8",
            env=env, capture_output=True, timeout=10, check=True,
        )
    if run.stderr:
        raise ValueError(f"{event} wrote unexpected hook stderr")
    return json.loads(run.stdout) if run.stdout.strip() else {}


def _model_visible(event: str, payload: dict, hook: dict) -> tuple:
    output = hook.get("hookSpecificOutput") or {}
    if event == "PostToolUse":
        unsafe = {"updatedToolOutput", "updatedMCPToolOutput", "suppressOutput"}
        if unsafe & (hook.keys() | output.keys()) or hook.get("decision") == "block" or hook.get("continue") is False:
            raise ValueError("PostToolUse attempted to replace the original tool result")
        return payload["tool_response"], output.get("additionalContext")
    return output.get("updatedInput", payload.get("tool_input")), output.get("additionalContext"), output.get("permissionDecision")


def _workload(path: Path) -> tuple[list[bytes], list[tuple[str | None, str | None]]]:
    prompts: list[bytes] = []
    settings: list[tuple[str | None, str | None]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            payload = record.get("payload") or {}
            if record.get("type") == "event_msg" and payload.get("type") == "user_message":
                prompts.append(hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).digest())
            if record.get("type") == "turn_context":
                current = (payload.get("model"), payload.get("effort") or payload.get("reasoning_effort"))
                if any(current) and (not settings or current != settings[-1]):
                    settings.append(current)
    return prompts, settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10, help="samples per event and runtime")
    parser.add_argument("--codex-rollouts", nargs=2, type=Path, metavar=("BASELINE", "CANDIDATE"))
    options = parser.parse_args(argv)
    if options.runs < 1:
        parser.error("use at least one sample")
    if options.codex_rollouts:
        before, after = options.codex_rollouts
        workload = _workload(before)
        if not workload[0] or workload != _workload(after):
            parser.error("Rollouts must contain matching user messages, models, and reasoning effort")

    python = [sys.executable, "-E", "-s", "-X", "utf8", str(ROOT / "runtime" / "unified_hook.py")]
    filename = "terser-fast.exe" if os.name == "nt" else "terser-fast"
    native = ROOT / "bin" / filename
    packaged = native.with_name(filename.replace("terser-fast", "terser-codex"))
    commands = {"Python": python}
    for binary in (packaged, native):
        if binary.is_file():
            probe = subprocess.run([str(binary), "--codex-hook-protocol"], input=b"", capture_output=True, timeout=3)
            if probe.returncode == 0 and probe.stdout.strip() == b"1":
                commands["Rust"] = [str(binary), "--python", sys.executable, "--script", python[-1]]
                break

    with tempfile.TemporaryDirectory() as state:
        workspace = Path(state) / "repo"
        workspace.mkdir()
        fixtures = _fixtures(workspace)
        expected = [_model_visible(event, payload, _run_fixture(python, event, payload, state))
                    for event, payload in fixtures]
        for name, command in commands.items():
            for (event, payload), evidence in zip(fixtures, expected):
                samples = []
                for _ in range(options.runs):
                    started = time.perf_counter()
                    actual = _model_visible(event, payload, _run_fixture(command, event, payload, state))
                    samples.append((time.perf_counter() - started) * 1000)
                    if actual != evidence:
                        raise ValueError(f"{name} diverged from Python for {event}")
                p95 = sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
                print(f"{name:6} {event:14} {payload.get('tool_name', 'subagent'):44} "
                      f"p50 {statistics.median(samples):6.1f} ms | p95 {p95:6.1f} ms")

        lines = ["def fixture():", *[f"    value_{i} = {i}" for i in range(150)], "    return value_87"]
        (workspace / "example.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
        env = {"TERSER_HOME": state}
        repo = index_mcp.index_folder(str(workspace), env=env)["repo"]
        args = {"repo": repo, "symbol_id": "example.py::fixture"}
        full = index_mcp.dispatch_action("get_symbol_source", args, env=env)
        part = index_mcp.dispatch_action("get_symbol_source", dict(args, start_line=89, end_line=89), env=env)
        if part.get("source") != full["source"].splitlines()[88] or part["source"] != "89:     value_87 = 87":
            raise ValueError("The ranged source differs from the original file")
        a, b = (config.estimate_tokens(index_mcp.format_mcp_response("get_symbol_source", result))
                for result in (full, part))
        print(f"Exact source fixture: {a:,} -> {b:,} estimated tokens for a single requested line")
    if options.codex_rollouts:
        return _compare_codex_rollouts(*options.codex_rollouts)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Codex benchmark failed: {exc}", file=sys.stderr)
        sys.exit(1)
