"""Terser native per-command output handlers.

``compress(command, text, returncode=0, stderr="")`` returns a shorter rendering of a
known command's stdout, or ``text`` unchanged. Rules every handler follows:

* Error, warning, failure, and traceback lines are never dropped.
* A failed non-test command is returned untouched (tee-on-failure).
* Test runners keep every failure block verbatim and drop only passing-test noise,
  so a failing run is reduced to its failures plus the summary line.
* Dropped lines are always counted in a ``[terser: ...]`` note so nothing is silent.
"""
from __future__ import annotations

import re
import shlex
from collections import OrderedDict
from typing import Callable

_SIGNAL_RE = re.compile(
    r"(?i)(error|fail|panic|exception|traceback|warning|warn\b|fatal|denied|not found|"
    r"cannot|could not|unable|segfault|assert|conflict|refused|timed? ?out|E\d{3,})"
)


def _note(n: int, what: str) -> str:
    return f"[terser: {n} {what} line(s) omitted]" if n else ""


def _join(lines: list[str], notes: list[str]) -> str:
    notes = [n for n in notes if n]
    return "\n".join(lines + notes).rstrip() + "\n"


def _argv(command: str) -> list[str]:
    cmd = command.strip()
    for sep in ("&&", "||", ";", "|"):
        if sep in cmd:
            cmd = cmd.split(sep)[-1] if sep != "|" else cmd.split(sep)[0]
    try:
        parts = shlex.split(cmd, posix=True)
    except ValueError:
        parts = cmd.split()
    while parts and "=" in parts[0] and not parts[0].startswith("-"):
        parts.pop(0)  # FOO=bar env prefixes
    while parts and parts[0] in ("sudo", "time", "npx", "uv", "poetry", "pipenv", "bunx") and len(parts) > 1:
        if parts[0] in ("uv", "poetry", "pipenv") and parts[1] == "run":
            parts = parts[2:]
        elif parts[0] in ("uv", "poetry", "pipenv"):
            break
        else:
            parts = parts[1:]
    if parts:
        parts[0] = parts[0].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe").lower()
    return parts


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

_GIT_HINT_RE = re.compile(r'^\s*\(use "git [^"]+"[^)]*\)\s*$')
_GIT_STATUS_ENTRY_RE = re.compile(r"^\t(?:(modified|new file|deleted|renamed|typechange|both modified):\s+)?(.+)$")


def _git_status(text: str) -> str:
    out: list[str] = []
    hints = 0
    group: str | None = None
    entries: "OrderedDict[str, list[str]]" = OrderedDict()

    def flush() -> None:
        nonlocal entries
        for state, paths in entries.items():
            out.append(f"  {state} ({len(paths)}): " + ", ".join(paths))
        entries = OrderedDict()

    for line in text.splitlines():
        if _GIT_HINT_RE.match(line):
            hints += 1
            continue
        m = _GIT_STATUS_ENTRY_RE.match(line)
        if m and group is not None:
            state = m.group(1) or "untracked"
            entries.setdefault(state, []).append(m.group(2).strip())
            continue
        if line.endswith(":") and not line.startswith(("\t", " ")):
            flush()
            group = line
            out.append(line)
            continue
        if not line.strip():
            continue
        flush()
        out.append(line)
    flush()
    return _join(out, [_note(hints, "git hint")])


_GIT_LOG_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{7,40})(.*)$")


def _git_log(text: str) -> str:
    """Default ``git log`` → one header line per commit; message bodies kept."""
    out: list[str] = []
    cur: dict[str, str] = {}
    body: list[str] = []

    def flush() -> None:
        if not cur:
            return
        head = f"{cur['sha'][:10]}{cur.get('refs', '')} {cur.get('date', '')} {cur.get('author', '')}".rstrip()
        out.append(head)
        out.extend("    " + b for b in body if b.strip())

    for line in text.splitlines():
        m = _GIT_LOG_COMMIT_RE.match(line)
        if m:
            flush()
            cur, body = {"sha": m.group(1), "refs": m.group(2)}, []
            continue
        if not cur:
            out.append(line)
        elif line.startswith("Author:"):
            cur["author"] = re.sub(r"\s*<[^>]*>", "", line[7:].strip())
        elif line.startswith("Date:"):
            cur["date"] = line[5:].strip()
        elif line.startswith("Merge:"):
            cur["refs"] = cur.get("refs", "") + " (merge)"
        else:
            body.append(line.strip())
    flush()
    return _join(out, [])


_GIT_REMOTE_NOISE_RE = re.compile(
    r"^(?:remote: )?(?:Enumerating|Counting|Compressing|Writing|Receiving|Resolving|Total|Delta compression|Unpacking)\b"
)


def _git_transfer(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _GIT_REMOTE_NOISE_RE.match(line) and not _SIGNAL_RE.search(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "transfer progress")])


# ---------------------------------------------------------------------------
# package managers
# ---------------------------------------------------------------------------

_PIP_NOISE_RE = re.compile(r"^\s*(Requirement already satisfied|Collecting |Downloading |Using cached |Obtaining |  Downloading|Installing build dependencies|Getting requirements|Preparing metadata|Building wheel|Created wheel|Stored in directory)")


def _pip(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _PIP_NOISE_RE.match(line) and not _SIGNAL_RE.search(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "pip resolve/download")])


_NPM_DEPRECATED_RE = re.compile(r"^npm (?:WARN|warn) deprecated (\S+)")
_NPM_NOISE_RE = re.compile(r"^(?:npm (?:http|timing|sill|verb)\b|\s*[⠁-⣿]|Progress: resolved|Packages: \+|\s*\+{5,}|Lockfile is up to date|Already up to date|Done in |\s*$)")


def _node_install(text: str) -> str:
    out, dropped, deprecated = [], 0, []
    for line in text.splitlines():
        m = _NPM_DEPRECATED_RE.match(line)
        if m:
            deprecated.append(m.group(1))
            continue
        if _NPM_NOISE_RE.match(line) and not _SIGNAL_RE.search(line):
            dropped += 1
            continue
        out.append(line)
    if deprecated:
        out.append(f"npm warn deprecated ({len(deprecated)}): " + ", ".join(deprecated))
    return _join(out, [_note(dropped, "install progress")])


# ---------------------------------------------------------------------------
# test runners: failures only
# ---------------------------------------------------------------------------

_PYTEST_PASS_RE = re.compile(r"^(?:\S+::\S+ PASSED|PASSED \S+|\S+\.py [.sxX]+\s*(?:\[\s*\d+%\])?\s*$|[.sxX]+\s*\[\s*\d+%\]\s*$)")


def _pytest(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _PYTEST_PASS_RE.match(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "passing-test")])


_CARGO_TEST_OK_RE = re.compile(r"^test \S+ \.\.\. (?:ok|ignored)$")
_CARGO_NOISE_RE = re.compile(r"^\s*(?:Compiling|Checking|Downloaded|Downloading|Fresh|Blocking|Updating|Locking|Adding|Running unittests|Running tests|Doc-tests)\b")


def _cargo(text: str) -> str:
    out, ok, noise = [], 0, 0
    for line in text.splitlines():
        if _CARGO_TEST_OK_RE.match(line):
            ok += 1
            continue
        if _CARGO_NOISE_RE.match(line) and not _SIGNAL_RE.search(line):
            noise += 1
            continue
        if line.strip() == "running 0 tests":
            noise += 1
            continue
        out.append(line)
    # Drop now-empty "test result: ok. 0 passed" blocks for crates without tests.
    out = [ln for ln in out if not re.match(r"^test result: ok\. 0 passed; 0 failed; 0 ignored", ln)]
    return _join(out, [_note(ok, "passing-test"), _note(noise, "cargo progress")])


_GO_PASS_RE = re.compile(r"^(?:=== (?:RUN|PAUSE|CONT)\s|\s*--- PASS: |PASS$|ok\s+\S+\s+[\d.]+s|\?\s+\S+\s+\[no test files\])")


def _go_test(text: str) -> str:
    out, dropped, ok_pkgs = [], 0, 0
    for line in text.splitlines():
        if _GO_PASS_RE.match(line):
            dropped += 1
            ok_pkgs += line.startswith("ok ")
            continue
        out.append(line)
    if ok_pkgs:
        out.append(f"ok: {ok_pkgs} package(s) passed")
    return _join(out, [_note(dropped, "passing-test")])


_JS_PASS_RE = re.compile(r"^\s*(?:✓|√|✔|PASS\s|ok \d+ )")


def _js_test(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _JS_PASS_RE.match(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "passing-test")])


_GRADLE_NOISE_RE = re.compile(r"^(?:> Task \S+(?: UP-TO-DATE| NO-SOURCE| FROM-CACHE| SKIPPED)?$|\[INFO\] (?:Download|Downloading|Downloaded|---|Building|$)|<-+> \d+%)")


def _jvm_build(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _GRADLE_NOISE_RE.match(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "build task")])


_DOTNET_NOISE_RE = re.compile(r"^\s*(?:Determining projects to restore|All projects are up-to-date|Restored |\S+ -> \S+\.dll$|Passed\s+\S+|Microsoft \(R\)|Copyright \(C\))")


def _dotnet(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _DOTNET_NOISE_RE.match(line) and not _SIGNAL_RE.search(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "dotnet progress")])


_DOCKER_BUILD_NOISE_RE = re.compile(r"^#\d+ (?:\[internal\]|sha256:|extracting|resolve|DONE \d|CACHED|transferring|naming to|exporting|writing image|\d+\.\d+ )")


def _docker(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _DOCKER_BUILD_NOISE_RE.match(line) and not _SIGNAL_RE.search(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "docker build progress")])


_TF_NOISE_RE = re.compile(r"^\S+: (?:Refreshing state|Reading\.\.\.|Read complete|Still (?:reading|creating|modifying|destroying)\.\.\.)")


def _terraform(text: str) -> str:
    out, dropped = [], 0
    for line in text.splitlines():
        if _TF_NOISE_RE.match(line):
            dropped += 1
            continue
        out.append(line)
    return _join(out, [_note(dropped, "terraform refresh")])


# ---------------------------------------------------------------------------
# xcodebuild
# ---------------------------------------------------------------------------

_XC_STEP_RE = re.compile(
    r"^(?:CompileC|CompileSwift|CompileSwiftSources|SwiftCompile|SwiftDriver(?:JobDiscovery)?|SwiftEmitModule|"
    r"EmitSwiftModule|SwiftMergeGeneratedHeaders|Ld|Libtool|CodeSign|ProcessInfoPlistFile|CopySwiftLibs|"
    r"CreateBuildDirectory|MkDir|Touch|PhaseScriptExecution|CpResource|CpHeader|CompileAssetCatalog|"
    r"CompileStoryboard|CompileXIB|LinkStoryboards|ProcessProductPackaging(?:DER)?|RegisterExecutionPolicyException|"
    r"Validate|GenerateDSYMFile|WriteAuxiliaryFile|ExtractAppIntentsMetadata|CopyPlistFile|Copy|Ditto|"
    r"ScanDependencies|ComputeTargetDependencyGraph|CreateUniversalBinary|ProcessPCH|Strip|SymLink|"
    r"RegisterWithLaunchServices|ClangStatCache|PrecompileModule|GenerateAssetSymbols|LinkAssetCatalog|"
    r"ComputePackagePrebuildTargetDependencyGraph|Prepare packages|Resolve Package Graph|"
    r"CreateBuildRequest|SendProjectDescription|CreateBuildOperation|Build description)\b"
)
_XC_PASS_RE = re.compile(
    r"^(?:Test Case '.+' (?:started|passed)|Test Suite '.+' (?:started|passed)|"
    r"\s*[✔◇] Test .+ (?:started|passed)|Testing started|Test session results)"
)


def _xcodebuild(text: str) -> str:
    out, steps, passes, in_step = [], 0, 0, False
    for line in text.splitlines():
        if _SIGNAL_RE.search(line) and not _XC_PASS_RE.match(line):
            in_step = False
            out.append(line)
            continue
        if _XC_STEP_RE.match(line):
            steps += 1
            in_step = True
            continue
        if in_step and (line.startswith("    ") or not line.strip()):
            continue  # the step's `cd`/`export`/tool invocation lines
        in_step = False
        if _XC_PASS_RE.match(line):
            passes += 1
            continue
        out.append(line)
    return _join(out, [_note(steps, "xcodebuild build-step"), _note(passes, "passing test")])


# ---------------------------------------------------------------------------
# adb logcat
# ---------------------------------------------------------------------------

# threadtime: "11-27 10:00:01.123  1234  1250 D Tag: msg"; brief: "D/Tag( 1234): msg"
_LOGCAT_THREADTIME_RE = re.compile(r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+([VDIWEFA])\s+([^:]*?)\s*: ?(.*)$")
_LOGCAT_BRIEF_RE = re.compile(r"^([VDIWEFA])/([^(]*?)\(\s*\d+\): ?(.*)$")
_LOGCAT_QUIET_MIN_LINES = 200


def _logcat(text: str) -> str:
    lines = text.splitlines()
    drop_quiet = len(lines) >= _LOGCAT_QUIET_MIN_LINES
    out: list[str] = []
    quiet: dict[str, int] = OrderedDict()
    last_key: tuple[str, str, str] | None = None
    repeats = 0
    collapsed = 0

    def flush() -> None:
        nonlocal repeats
        if repeats:
            out.append(f"  [x{repeats} more identical]")
        repeats = 0

    for line in lines:
        m = _LOGCAT_THREADTIME_RE.match(line) or _LOGCAT_BRIEF_RE.match(line)
        if not m:
            flush()
            last_key = None
            out.append(line)
            continue
        level, tag, msg = m.group(1), m.group(2).strip(), m.group(3)
        if drop_quiet and level in "VD" and not _SIGNAL_RE.search(msg):
            quiet[tag] = quiet.get(tag, 0) + 1
            continue
        key = (level, tag, msg)
        if key == last_key:
            repeats += 1
            collapsed += 1
            continue
        flush()
        last_key = key
        out.append(line)
    flush()
    notes = []
    if quiet:
        top = sorted(quiet.items(), key=lambda kv: -kv[1])[:8]
        detail = ", ".join(f"{t or '?'}={n}" for t, n in top)
        notes.append(f"[terser: {sum(quiet.values())} verbose/debug logcat line(s) omitted ({detail})]")
    if collapsed:
        notes.append(f"[terser: {collapsed} repeated logcat line(s) collapsed]")
    return _join(out, notes)


# ---------------------------------------------------------------------------
# linters & typecheckers (tsc, eslint, ruff, mypy, pyright, biome, oxlint)
# ---------------------------------------------------------------------------

_DIAG_REPEAT_FOOTER_RE = re.compile(
    r"^\s*(?:help:|note:|info:|For more information|See https?://|https?://\S+\s*$|\[\*\]\s+Fixable\b)",
    re.IGNORECASE,
)


def _linter_diag(text: str) -> str:
    """Losslessly preserve every diagnostic file:line:col and message while deduplicating
    repeated help/note/URL footers and collapsing excessive blank runs."""
    out: list[str] = []
    seen_footers: set[str] = set()
    deduped = 0
    prev_blank = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if not prev_blank:
                out.append("")
                prev_blank = True
            continue
        prev_blank = False
        if _DIAG_REPEAT_FOOTER_RE.match(line):
            key = re.sub(r"\s+", " ", s)
            if key in seen_footers:
                deduped += 1
                continue
            seen_footers.add(key)
        out.append(line)
    return _join(out, [_note(deduped, "repeated linter note/help")])


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_TEST_HANDLERS = {"pytest", "cargo-test", "go-test", "js-test", "dotnet-test", "xcodebuild-test", "linter"}
TEST_HANDLER_NAMES = frozenset(_TEST_HANDLERS)

# Named handlers that `command-filters.toml` may reuse via `handler = "<name>"`.
HANDLERS: dict[str, Callable[[str], str]] = {}


def _pick(argv: list[str]) -> tuple[str, Callable[[str], str]] | None:
    if not argv:
        return None
    exe, rest = argv[0], argv[1:]
    sub = next((a for a in rest if not a.startswith("-")), "")
    if exe == "git":
        # Skip global options such as -C <dir> / -c k=v.
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-C", "-c") else 1
        sub = rest[i] if i < len(rest) else ""
        args = rest[i + 1:]
        if sub == "status" and not any(a in ("-s", "--short", "--porcelain") or a.startswith("--porcelain") for a in args):
            return "git", _git_status
        if sub == "log" and not any(a.startswith(("--oneline", "--format", "--pretty", "-p", "--patch", "--stat")) for a in args):
            return "git", _git_log
        if sub in ("push", "pull", "fetch", "clone"):
            return "git", _git_transfer
        return None
    if exe in ("pip", "pip3") or (exe in ("python", "python3", "py") and rest[:2] == ["-m", "pip"]):
        return "pip", _pip
    if exe in ("npm", "pnpm", "yarn", "bun") and sub in ("install", "i", "ci", "add", "update", "up", ""):
        return "node-install", _node_install
    if exe in ("pytest", "py.test") or (exe in ("python", "python3", "py") and rest[:2] == ["-m", "pytest"]):
        return "pytest", _pytest
    if exe == "cargo" and sub in ("test", "build", "check", "clippy", "nextest", "run"):
        return ("cargo-test" if sub in ("test", "nextest") else "cargo"), _cargo
    if exe == "go" and sub == "test":
        return "go-test", _go_test
    if exe in ("jest", "vitest", "mocha") or (exe in ("npm", "pnpm", "yarn", "bun") and sub == "test"):
        return "js-test", _js_test
    if exe in ("gradle", "gradlew", "mvn", "mvnw"):
        return "jvm", _jvm_build
    if exe == "dotnet" and sub in ("build", "test", "restore", "run"):
        return ("dotnet-test" if sub == "test" else "dotnet"), _dotnet
    if exe == "docker" and sub in ("build", "buildx", "compose"):
        return "docker", _docker
    if exe == "terraform" and sub in ("plan", "apply", "refresh"):
        return "terraform", _terraform
    if exe == "xcodebuild" or (exe == "xcrun" and sub == "xcodebuild"):
        return ("xcodebuild-test" if "test" in rest or "test-without-building" in rest else "xcodebuild"), _xcodebuild
    if exe == "adb" and sub == "logcat":
        return "logcat", _logcat
    if exe in ("tsc", "eslint", "ruff", "mypy", "pyright", "biome", "oxlint") or (
        exe in ("python", "python3", "py") and len(rest) >= 2 and rest[0] == "-m" and rest[1] in ("mypy", "ruff", "pyright")
    ):
        return "linter", _linter_diag
    return None


HANDLERS.update({
    "git-status": _git_status, "git-log": _git_log, "git-transfer": _git_transfer,
    "pip": _pip, "node-install": _node_install, "pytest": _pytest, "cargo": _cargo,
    "cargo-test": _cargo, "go-test": _go_test, "js-test": _js_test, "jvm": _jvm_build,
    "dotnet": _dotnet, "dotnet-test": _dotnet, "docker": _docker, "terraform": _terraform,
    "xcodebuild": _xcodebuild, "xcodebuild-test": _xcodebuild, "logcat": _logcat,
    "linter": _linter_diag,
})


def compress(command: str, text: str, returncode: int = 0, stderr: str = "") -> str:
    if not command or not text:
        return text
    try:
        picked = _pick(_argv(command))
        if not picked:
            return text
        name, fn = picked
        if returncode != 0 and name not in _TEST_HANDLERS:
            return text
        out = fn(text)
        if not out or len(out) >= len(text):
            return text
        return out
    except Exception:
        return text
