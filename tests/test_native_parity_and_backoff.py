"""PowerShell coverage, content-addressed read snapshots, native T1 parity, and tier backoff."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import config, loop_guard, read_funnel, unified_hook  # noqa: E402

os.environ.setdefault("TERSER_PYTHON_WORKER", "0")
_FAST_BIN = ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def _noisy_output() -> str:
    return "".join(f"   Compiling dep v0.1.{i % 3}\n" if i % 2 else "warning: unused import\n" for i in range(4)) + (
        "   Compiling same-crate v1.0.0 (/w/same)\n" * 80
    ) + "error[E0425]: cannot find value `x` in this scope\n"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = {"TERSER_HOME": str(self.tmp / "state")}
        self.cfg = config.load_config(self.env)

    def tearDown(self) -> None:
        self._td.cleanup()

    def _source(self, name: str = "mod.py", n: int = 30) -> Path:
        p = self.tmp / name
        p.write_text("".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(n // 2)), encoding="utf-8")
        return p

    def _store(self, sid: str) -> dict:
        return json.loads(read_funnel._cache_path(sid, self.env).read_text(encoding="utf-8"))


class PowerShellCompressionTests(_Base):
    def _post(self, tool: str) -> dict:
        payload = {
            "session_id": "ps-1",
            "cwd": str(self.tmp),
            "tool_name": tool,
            "tool_input": {"command": "cargo build"},
            "tool_response": {"stdout": _noisy_output(), "stderr": ""},
        }
        return unified_hook.handle_post_tool_use("claude", payload, self.cfg, self.env)

    def test_powershell_output_gets_the_same_lossless_compression_as_bash(self) -> None:
        ps = self._post("PowerShell")
        bash = self._post("Bash")
        self.assertIn("updatedToolOutput", ps.get("hookSpecificOutput", {}))
        ps_out = ps["hookSpecificOutput"]["updatedToolOutput"]["stdout"]
        self.assertLess(len(ps_out), len(_noisy_output()))
        self.assertIn("error[E0425]", ps_out)
        self.assertEqual(ps_out, bash["hookSpecificOutput"]["updatedToolOutput"]["stdout"])

    def test_powershell_form_repair_only_swaps_plain_executable_names(self) -> None:
        self.assertTrue(loop_guard._is_plain_executable("python3"))
        self.assertTrue(loop_guard._is_plain_executable("py.exe"))
        for posix_only in ("source .venv/bin/activate", "./gradlew", "VAR=1 make", "a|b", "~/bin/x"):
            self.assertFalse(loop_guard._is_plain_executable(posix_only), posix_only)


class SnapshotBlobTests(_Base):
    def test_snapshots_live_in_shared_blobs_and_still_drive_exact_t1_diffs(self) -> None:
        f = self._source()
        self.assertIsNone(read_funnel.check_read("blob-1", str(f), cfg=self.cfg, host="claude", env=self.env))
        entry = next(iter(self._store("blob-1")["reads"].values()))
        self.assertNotIn("snapshot", entry)
        blob = read_funnel._blob_path(entry["snapshot_sha"], self.env)
        self.assertTrue(blob.is_file())
        self.assertEqual(blob.read_text(encoding="utf-8"), f.read_text(encoding="utf-8"))

        f.write_text(f.read_text(encoding="utf-8").replace("return x + 3\n", "return x * 3\n"), encoding="utf-8")
        _bump_mtime(f)
        res = read_funnel.check_read("blob-1", str(f), cfg=self.cfg, host="claude", env=self.env)
        self.assertEqual(res and res["action"], "deny")
        self.assertIn("-    return x + 3", res["reason"])
        self.assertIn("+    return x * 3", res["reason"])

    def test_corrupt_blob_is_never_diffed_against(self) -> None:
        f = self._source()
        read_funnel.check_read("blob-2", str(f), cfg=self.cfg, host="claude", env=self.env)
        entry = next(iter(self._store("blob-2")["reads"].values()))
        read_funnel._blob_path(entry["snapshot_sha"], self.env).write_text("tampered\n", encoding="utf-8")
        f.write_text(f.read_text(encoding="utf-8").replace("return x + 3\n", "return x * 3\n"), encoding="utf-8")
        _bump_mtime(f)
        self.assertIsNone(read_funnel.check_read("blob-2", str(f), cfg=self.cfg, host="claude", env=self.env))

    def test_identical_content_shares_one_blob_across_sessions(self) -> None:
        f = self._source()
        read_funnel.check_read("share-a", str(f), cfg=self.cfg, host="claude", env=self.env)
        read_funnel.check_read("share-b", str(f), cfg=self.cfg, host="claude", env=self.env)
        blobs = list((read_funnel._cache_dir(self.env) / "blobs").glob("*.txt"))
        self.assertEqual(len(blobs), 1)

    def test_sweep_is_throttled_and_removes_idle_blobs(self) -> None:
        cdir = read_funnel._cache_dir(self.env)
        old_blob = cdir / "blobs" / ("0" * 64 + ".txt")
        old_blob.parent.mkdir(parents=True)
        old_blob.write_text("old", encoding="utf-8")
        stale = time.time() - read_funnel._STORE_TTL_SECONDS - 60
        os.utime(old_blob, (stale, stale))
        read_funnel._sweep_stale(cdir, time.time())
        self.assertFalse(old_blob.exists())
        old_blob.write_text("old", encoding="utf-8")
        os.utime(old_blob, (stale, stale))
        read_funnel._sweep_stale(cdir, time.time())  # within the hourly throttle window
        self.assertTrue(old_blob.exists())


class TierBackoffTests(_Base):
    def _log(self, tier: str, served: int, retry: int, host: str = "claude") -> None:
        log = config.state_home(self.env) / "funnel.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        now = round(time.time(), 1)
        rows = [{"ts": now, "host": host, "tier": tier, "ev": "served"}] * served
        rows += [{"ts": now, "host": host, "tier": tier, "ev": "retry"}] * retry
        log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_mostly_retried_tier_backs_off_per_host(self) -> None:
        self._log("t0", served=12, retry=8)
        self.assertEqual(config.compute_tier_backoff(self.env), {"claude": ["t0"]})
        self.assertTrue(config.tier_backed_off("t0", "claude", self.env))
        self.assertFalse(config.tier_backed_off("t0", "codex", self.env))
        self.assertFalse(config.tier_backed_off("t1", "claude", self.env))

    def test_small_samples_and_healthy_ratios_never_back_off(self) -> None:
        self._log("t2", served=11, retry=11)
        self.assertEqual(config.compute_tier_backoff(self.env), {})
        self._log("t2", served=40, retry=20)
        self.assertEqual(config.compute_tier_backoff(self.env), {})

    def test_backed_off_t0_passes_the_full_read_through(self) -> None:
        self._log("t0", served=12, retry=12)
        f = self._source()
        read_funnel.check_read("bo-1", str(f), cfg=self.cfg, host="claude", env=self.env)
        self.assertIsNone(read_funnel.check_read("bo-1", str(f), cfg=self.cfg, host="claude", env=self.env))

    def test_old_events_age_out(self) -> None:
        log = config.state_home(self.env) / "funnel.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        old = time.time() - 8 * 86400
        rows = [{"ts": old, "host": "claude", "tier": "t0", "ev": e} for e in ["served"] * 12 + ["retry"] * 12]
        log.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.assertEqual(config.compute_tier_backoff(self.env), {})


@unittest.skipUnless(_FAST_BIN.is_file(), "terser-fast binary not built")
class NativeParityTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.marker = self.tmp / "delegated.txt"
        self.probe = self.tmp / "probe_hook.py"
        self.probe.write_text("open(%r, 'a').write('x')\n" % str(self.marker), encoding="utf-8")
        self.native_env = dict(
            os.environ,
            TERSER_HOME=str(self.tmp / "native-state"),
            TOKENECONOMIZER_HOME=str(self.tmp / "te"),
            TERSER_PYTHON_WORKER="0",
        )
        self.native_env.pop("TERSER_DISABLE", None)

    def _run(self, event: str, payload: dict) -> dict:
        proc = subprocess.run(
            [str(_FAST_BIN), "--python", sys.executable, "--script", str(self.probe), "--host", "claude", "--event", event],
            input=json.dumps(payload), capture_output=True, text=True, env=self.native_env, timeout=30,
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def _delegated(self) -> bool:
        hit = self.marker.exists()
        if hit:
            self.marker.unlink()
        return hit

    def test_native_t1_diff_is_byte_identical_to_python(self) -> None:
        f = self._source(n=60)
        read = {"session_id": "par-1", "cwd": str(self.tmp), "tool_name": "Read", "tool_input": {"file_path": str(f)}}
        read_funnel.check_read("par-1", str(f), cfg=self.cfg, host="claude", env=self.env)
        self._run("PreToolUse", read)
        self.assertFalse(self._delegated())

        text = f.read_text(encoding="utf-8")
        f.write_text(text.replace("return x + 4\n", "return x - 4\n").replace("def f20(x):\n", "def f20(x, y=0):\n"), encoding="utf-8")
        _bump_mtime(f)
        py = read_funnel.check_read("par-1", str(f), cfg=self.cfg, host="claude", env=self.env)
        native = self._run("PreToolUse", read)
        self.assertFalse(self._delegated(), "small change must be served natively")
        self.assertEqual(native["hookSpecificOutput"]["permissionDecisionReason"], py["reason"])

    def test_native_powershell_output_matches_bash(self) -> None:
        def post(tool: str) -> dict:
            return self._run("PostToolUse", {
                "session_id": "par-2", "cwd": str(self.tmp), "tool_name": tool,
                "tool_input": {"command": "cargo build"},
                "tool_response": {"stdout": _noisy_output(), "stderr": ""},
            })

        ps, bash = post("PowerShell"), post("Bash")
        self.assertIn("updatedToolOutput", ps.get("hookSpecificOutput", {}))
        self.assertEqual(ps["hookSpecificOutput"]["updatedToolOutput"], bash["hookSpecificOutput"]["updatedToolOutput"])

    def test_native_honors_python_tier_backoff(self) -> None:
        state = Path(self.native_env["TERSER_HOME"])
        state.mkdir(parents=True, exist_ok=True)
        f = self._source()
        read = {"session_id": "par-3", "cwd": str(self.tmp), "tool_name": "Read", "tool_input": {"file_path": str(f)}}
        self._run("PreToolUse", read)
        (state / "funnel-backoff.json").write_text(
            json.dumps({"at": time.time(), "disabled": {"claude": ["t0"]}}), encoding="utf-8",
        )
        self.assertEqual(self._run("PreToolUse", read), {})
        (state / "funnel-backoff.json").write_text(json.dumps({"at": time.time(), "disabled": {}}), encoding="utf-8")
        out = self._run("PreToolUse", read)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
