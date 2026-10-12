"""Delegated coding work in an explicitly selected workspace (argv-only).

Architecture:
    Gemini -> run_coding_task(task, workspace) [tools.py, voice approval]
    -> WorkerRequest (validated, workspace-contained)
    -> WorkerProvider.run() -> WorkerResult (structured, rendered)

Chibi stays the orchestrator: this module never holds conversation,
never routes desktop tools, never decides what the user meant. The
model decides delegation; the worker only executes inside the chosen
workspace and reports back. `WorkerResult` is the source of truth -
never model assumption.

Security: argv lists only (never shell=True), bounded timeout and
output, process-group cleanup on timeout, realpath workspace
containment, protected runtime paths denied everywhere, files reported
outside the workspace are dropped from the result, failures are
honest strings. No credentials are added; network is not assumed.

Sandbox honesty: the default CLI offers NO filesystem sandbox flags
(verified against installed `opencode run --help`: only `--dir`
project scoping exists, which is passed explicitly alongside cwd, and
`--auto` auto-approve is deliberately never passed). Containment is
therefore pre-execution validation + process scoping + approval, NOT
an OS sandbox: a compromised worker runs as the user and could reach
anything the user can. Do not claim otherwise.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from cat_talker.logging_config import get_logger

logger = get_logger("cat_talker.coding_worker")

# Bounded external execution: never an unbounded process lifetime.
WORKER_TIMEOUT_DEFAULT = 300
WORKER_TIMEOUT_MAX = 900
OUTPUT_MAX_BYTES = 32 * 1024
TRUNCATION_NOTE = "\n...[TRUNCATED]"
# Independent-verification output kept for the voice report: concise and
# bounded separately from the worker's own (potentially large) output.
VERIFY_SUMMARY_MAX_CHARS = 2000
VERIFY_TRUNCATION_NOTE = "\n...[verification output truncated]"

# First CLI wrapped. Overridable without code changes via
# CAT_TALKER_CODER_BIN (bare binary name or absolute path; it becomes
# argv[0], never a shell string).
DEFAULT_CLI_BIN = "opencode"
DEFAULT_CLI_ARGS = ("run",)

# Explicit backend selection:
# CAT_TALKER_CODER_BACKEND=opencode|goose|goose-lite.
# OpenCode is always the default; Goose variants run only when explicitly
# requested. Anything else fails honestly (never a silent fallback).
BACKEND_OPENCODE = "opencode"
BACKEND_GOOSE = "goose"
BACKEND_GOOSE_LITE = "goose-lite"
BACKENDS = (BACKEND_OPENCODE, BACKEND_GOOSE, BACKEND_GOOSE_LITE)
GOOSE_CLI_BIN = "goose"
GOOSE_PROVIDER = "google"
# Validated Goose cloud models. Single source:
# CAT_TALKER_GOOSE_MODEL overrides the selected backend's default.
GOOSE_DEFAULT_MODEL = "gemini-3.8-flash"
GOOSE_FLASH_LITE_MODEL = "gemini-3.5-flash-lite"
# Bounded agent iterations per task (verified `goose run --help` flag;
# the subprocess timeout below remains the hard wall).
GOOSE_MAX_TURNS = 25


def resolve_goose_model(default: str = GOOSE_DEFAULT_MODEL) -> str:
    """Configured Goose model, falling back to the selected backend default."""
    configured = os.environ.get("CAT_TALKER_GOOSE_MODEL", "").strip()
    return configured or default


def _protected_roots() -> list:
    """Runtime paths the worker may never touch, in any workspace."""
    home = os.path.realpath(os.path.expanduser("~"))
    return [
        os.path.join(home, ".config", "cat-talker"),
        os.path.join(home, ".cat_talker_history.txt"),
    ]


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def resolve_workspace(path: str) -> str:
    """Validate an explicitly selected workspace. Returns real path.

    Raises ValueError (missing/empty/not-a-directory) or
    PermissionError (protected or escaping). No implicit default:
    an empty path is an error asking the user which repository to use.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError(
            "Coding error: no workspace given - "
            "ask which repository to work in.")
    real = _real(path)
    if not os.path.isdir(real):
        raise ValueError(f"Coding error: workspace is not a directory: {path}")
    for protected in _protected_roots():
        if real == protected or real.startswith(protected + os.sep):
            raise PermissionError(
                "Coding error: workspace is a protected assistant path.")
        if protected.startswith(real + os.sep):
            raise PermissionError(
                "Coding error: workspace contains protected assistant paths.")
    return real


@dataclass
class WorkerRequest:
    workspace: str
    task: str
    constraints: str = ""
    timeout: int = WORKER_TIMEOUT_DEFAULT
    # Optional explicit local verification command (argv list, no shell),
    # run inside the workspace AFTER the worker finishes. Never invented:
    # only run what the caller explicitly supplied (e.g. this repo's own
    # "./.venv/bin/python -m pytest -q"). None means no command check.
    verify_command: object = None
    # Ordinary-tool hygiene only: when True and no explicit
    # verify_command was supplied, delegate() may run a conservatively
    # auto-detected project check (never invented, fixed argv). An
    # explicit verify_command always wins; False disables detection.
    auto_verify: bool = False

    def validated(self) -> "WorkerRequest":
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("Coding error: no coding task given.")
        workspace = resolve_workspace(self.workspace)
        try:
            timeout = int(self.timeout)
        except (TypeError, ValueError):
            timeout = WORKER_TIMEOUT_DEFAULT
        timeout = max(1, min(WORKER_TIMEOUT_MAX, timeout))
        constraints = self.constraints if isinstance(
            self.constraints, str) else ""
        verify = self.verify_command
        if verify is not None:
            if (not isinstance(verify, (list, tuple)) or not verify
                    or not all(isinstance(a, str) and a.strip()
                               for a in verify)):
                raise ValueError(
                    "Coding error: verify_command must be a non-empty "
                    "argv list of strings.")
            verify = tuple(verify)
        auto = self.auto_verify
        if not isinstance(auto, bool):
            raise ValueError(
                "Coding error: auto_verify must be a boolean.")
        return WorkerRequest(workspace=workspace,
                             task=self.task.strip(),
                             constraints=constraints.strip(),
                             timeout=timeout,
                             verify_command=verify,
                             auto_verify=auto)


def build_request(workspace: str, task: str, constraints: str = "",
                  timeout: int = WORKER_TIMEOUT_DEFAULT,
                  verify_command=None,
                  auto_verify: bool = False) -> WorkerRequest:
    """Construct and validate a worker request. Raises on any problem."""
    return WorkerRequest(workspace=workspace, task=task,
                         constraints=constraints,
                         timeout=timeout,
                         verify_command=verify_command,
                         auto_verify=auto_verify).validated()


@dataclass
class WorkerResult:
    ok: bool
    summary: str = ""
    files_changed: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    # WORKER CLAIM ONLY: what the worker said about tests (True/False/
    # None unknown). Never treat as verified fact; see `verification`.
    tests_passed: object = None  # True / False / None (unknown)
    error: str = ""
    truncated: bool = False
    # Independent verification state. Only "independently_verified_*"
    # may be reported as verified fact. Anything else must be worded
    # as unverified (see render()).
    # One of: not_run, worker_reported_pass, worker_reported_fail,
    # independently_verified_pass, independently_verified_fail, unknown.
    verification: str = "not_run"
    # Actual validated workspace path the work ran in (realpath, set by
    # the provider/verify stages - never an unvalidated user string).
    workspace: str = ""
    # Independent verification command outcome: exit status (None when no
    # command ran) and bounded output, kept separate from worker output.
    verify_exit: object = None  # int / None (no command ran)
    verify_output: str = ""
    # Resolved runtime facts, set by delegate() from the actual
    # execution path (never from configuration alone). Rendered so
    # Chibi can report them; empty when unset (older constructions).
    backend: str = ""
    sandbox: str = ""

    def render(self) -> str:
        """Stable compact report: claim, verified fact, and unknown stay
        in separate labeled fields. Only independent verification may be
        worded as verified fact; everything else is an explicit claim
        or an explicit absence of verification. Failure never claims
        completion, and partial results (files changed but verification
        failed, truncated output, missing verification) stay visible."""
        lines = []
        if self.workspace:
            workspace_line = f"Workspace: {self.workspace}"
        else:
            workspace_line = ""
        if not self.ok:
            lines.append("Coding task FAILED.")
            if workspace_line:
                lines.append(workspace_line)
            lines.extend(_runtime_block(self.backend, self.sandbox))
            if self.error:
                lines.append(f"Reason: {self.error}")
            elif self.summary:
                lines.append(f"Detail: {self.summary}")
            lines.extend(_files_block(self.files_changed))
            lines.append(f"Verification: {self.verification}")
            if self.truncated:
                lines.append("(Worker output was truncated.)")
            return "\n".join(lines)
        if self.verification == "independently_verified_pass":
            lines.append("Coding task succeeded.")
        elif self.verification == "independently_verified_fail":
            lines.append("Worker completed; independent verification FAILED.")
        else:
            lines.append("Worker completed; verification was not available.")
        if workspace_line:
            lines.append(workspace_line)
        lines.append(f"Verification: {self.verification}")
        lines.extend(_runtime_block(self.backend, self.sandbox))
        if self.summary:
            if self.verification == "independently_verified_pass":
                lines.append(f"Summary: {self.summary}")
            else:
                lines.append("Worker reports (UNVERIFIED - do not present "
                             f"as verified fact): {self.summary}")
        lines.extend(_files_block(self.files_changed))
        if self.commands:
            shown = "; ".join(str(c) for c in self.commands[:5])
            lines.append(f"Commands run: {shown}")
        if self.verify_output or isinstance(self.verify_exit, int):
            lines.append(_verify_block(self.verify_exit, self.verify_output))
        if self.verification == "independently_verified_pass":
            lines.append("Tests: independently verified.")
        elif self.tests_passed is True:
            lines.append("Worker tests: reported pass "
                         "(NOT independently verified).")
        elif self.tests_passed is False:
            lines.append("Worker tests: reported FAILED.")
        else:
            lines.append("Worker tests: unknown (NOT independently verified).")
        if self.error and self.verification != "independently_verified_pass":
            lines.append(f"Reason: {self.error}")
        if self.truncated:
            lines.append("(Worker output was truncated.)")
        return "\n".join(lines)


def _clip(text: str) -> tuple:
    """Bound one output stream. Returns (text, truncated)."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if len(text.encode("utf-8", errors="replace")) <= OUTPUT_MAX_BYTES:
        return text, False
    raw = text.encode("utf-8", errors="replace")[:OUTPUT_MAX_BYTES]
    return raw.decode("utf-8", errors="replace") + TRUNCATION_NOTE, True


def _files_block(files_changed) -> list:
    """Render the file list as a labeled bullet block (empty when none)."""
    files = [f for f in (files_changed or []) if isinstance(f, str) and f]
    if not files:
        return []
    return (["Files changed:"]
            + [f"* {f}" for f in files[:20]])


def _verify_summary(out_text: str, err_text: str) -> tuple:
    """Concise bounded verification output, stdout preferred.

    Returns (text, was_cut). Test-runner output normally lands on stdout;
    stderr is the fallback so diagnostics are never silently dropped.
    """
    text = out_text if isinstance(out_text, str) and out_text.strip() \
        else (err_text or "")
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= VERIFY_SUMMARY_MAX_CHARS:
        return text, False
    return (text[:VERIFY_SUMMARY_MAX_CHARS] + VERIFY_TRUNCATION_NOTE, True)


def _verify_block(verify_exit, verify_output: str) -> str:
    """Labeled verification-output block for render()."""
    if isinstance(verify_exit, int):
        head = f"Verification output (exit {verify_exit}):"
    else:
        head = "Verification output:"
    body = verify_output if isinstance(verify_output, str) else ""
    return head + (("\n" + body) if body else " (none)")


def _runtime_block(backend: str, sandbox: str) -> list:
    """Resolved-runtime lines for render() (empty when unset).

    Only ever the backend/boundary delegate() confirmed from the
    actual execution path - never configuration claims.
    """
    lines = []
    if isinstance(backend, str) and backend:
        lines.append(f"Backend: {backend}")
    if isinstance(sandbox, str) and sandbox:
        lines.append(f"Sandbox: {sandbox}")
    return lines


def _claim_state(tests) -> str:
    """Map a worker tests claim to a verification state (still a claim)."""
    if tests is True:
        return "worker_reported_pass"
    if tests is False:
        return "worker_reported_fail"
    return "unknown"


def _files_inside_workspace(files, workspace: str) -> tuple:
    """Keep only paths resolving inside the workspace (drop escapes)."""
    kept, dropped = [], 0
    if not isinstance(files, list):
        return kept, dropped
    for entry in files:
        if not isinstance(entry, str) or not entry.strip():
            dropped += 1
            continue
        abs_entry = os.path.realpath(os.path.join(workspace, entry.strip()))
        if abs_entry == workspace or abs_entry.startswith(workspace + os.sep):
            kept.append(entry.strip())
        else:
            dropped += 1
    return kept, dropped


def result_from_process(returncode: int, stdout: str, stderr: str,
                        truncated: bool, workspace: str) -> WorkerResult:
    """Deterministic result from raw process output. Never raises."""
    try:
        envelope = None
        if isinstance(stdout, str) and stdout.strip().startswith("{"):
            try:
                envelope = json.loads(stdout)
            except (ValueError, TypeError):
                envelope = None
        if isinstance(envelope, dict):
            files, dropped = _files_inside_workspace(
                envelope.get("files_changed", []), workspace)
            commands = envelope.get("commands", [])
            if not isinstance(commands, list):
                commands = []
            commands = [str(c)[:200] for c in commands[:20]]
            tests = envelope.get("tests_passed", None)
            tests = tests if isinstance(tests, bool) else None
            summary = str(envelope.get("summary", ""))[:2000]
            note = (f" ({dropped} reported path(s) outside the workspace "
                    f"were excluded)" if dropped else "")
            if returncode == 0:
                return WorkerResult(ok=True, summary=summary + note,
                                    files_changed=files, commands=commands,
                                    tests_passed=tests, truncated=truncated,
                                    verification=_claim_state(tests),
                                    workspace=workspace)
            return WorkerResult(
                ok=False, summary=summary + note, files_changed=files,
                commands=commands, tests_passed=False,
                error=(str(envelope.get("error", ""))[:1000]
                       or f"worker exited with code {returncode}"),
                truncated=truncated,
                verification=_claim_state(False),
                workspace=workspace)
        clipped, _ = _clip(stdout or "")
        err_clipped, _ = _clip(stderr or "")
        if returncode == 0:
            return WorkerResult(ok=True, summary=clipped or "done.",
                                truncated=truncated,
                                verification="unknown",
                                workspace=workspace)
        return WorkerResult(
            ok=False, summary=clipped,
            error=err_clipped or f"worker exited with code {returncode}",
            truncated=truncated,
            verification="unknown",
            workspace=workspace)
    except Exception as e:
        return WorkerResult(ok=False,
                            error=f"Worker result handling failed: {e}",
                            verification="unknown")


# ─── conservative Python/pytest auto-detection ────────────────
#
# Ordinary tasks only: fixed argv, never parsed from project files.
# A candidate needs a STRONG marker (pytest.ini, conftest.py) or a
# recognizable test file (test_*.py / *_test.py) in the workspace
# root, tests/, or test/. Generic config (pyproject.toml, setup.cfg,
# tox.ini) or a bare tests/ directory alone is NOT proof of pytest.
# Runner suitability is probed inside the verification execution
# boundary (isolated mode + find_spec, never a real import), never
# assumed; nothing is installed. Returns (argv|None, status, detail)
# with status one of "ready" | "unavailable" | "absent".

AUTO_CHECK_PROBE_TIMEOUT = 30
AUTO_CHECK_TEST_DIRS = ("tests", "test")

# Isolated availability probe: -I drops cwd, PYTHONPATH, and user
# site; find_spec locates pytest WITHOUT importing it, so a
# workspace-controlled pytest.py can never execute during detection.
# Distinctive exits (10 found / 11 missing) keep the verdict
# unambiguous: launcher failures (bwrap exec errors exit 1, missing
# binaries 127, timeouts) can never masquerade as either verdict.
_PYTEST_PROBE_FOUND = 10
_PYTEST_PROBE_MISSING = 11
_PYTEST_PROBE_CODE = (
    "import importlib.util,sys;"
    "sys.exit(10 if importlib.util.find_spec('pytest') else 11)")

# Explicit user opt-outs: when the task or constraints contain one of
# these, automatic checks are skipped (explicit verify_command still
# runs - it was directly requested). Plain substring phrases only, no
# parsing: every entry pairs an opt-out cue (not/n't/never/avoid/
# skip/without/stop) with running or executing tests, pytest, or
# checks, so routine wording like "run the tests and fix failures"
# never matches.
_NO_AUTO_CHECK_PHRASES = (
    "do not run tests", "don't run tests",
    "do not run the tests", "don't run the tests",
    "do not run any tests", "don't run any tests",
    "do not run pytest", "don't run pytest",
    "do not run checks", "don't run checks",
    "do not run any checks", "don't run any checks",
    "do not execute tests", "don't execute tests",
    "do not execute any tests", "don't execute any tests",
    "do not execute pytest", "don't execute pytest",
    "do not execute checks", "don't execute checks",
    "do not execute any checks", "don't execute any checks",
    "do not run all tests", "don't run all tests",
    "never run tests", "never run pytest", "never run checks",
    "never execute tests",
    "avoid running tests", "avoid running pytest",
    "avoid running checks",
    "avoid executing tests", "avoid executing pytest",
    "skip tests", "skip the tests", "skip testing", "skip checks",
    "skip all tests", "skip all checks",
    "skip pytest", "skip any tests",
    "without running tests", "without running checks",
    "without executing tests",
    "stop running tests",
    "do not verify", "don't verify",
    "no verification", "skip verification",
)

# Opposite instructions: the user affirmatively asks NOT to skip
# checks ("do not skip tests"). These contain an opt-out phrase as a
# substring ("skip tests") but mean the reverse. The resolution
# algorithm collects all opt-out phrases present in the request,
# then any matching opposite phrase negates only its corresponding
# opt-out (e.g., "do not skip tests" negates opt-outs containing
# "skip tests"). After negation, if any opt-out remains un-negated,
# automatic checks are disabled. This conservative rule ensures
# contradictory instructions never enable execution by accident.
_NOT_AN_OPTOUT_PHRASES = (
    "do not skip tests", "don't skip tests",
    "do not skip pytest", "don't skip pytest",
    "do not skip checks", "don't skip checks",
    "should not skip tests", "should not skip pytest",
    "should not skip checks",
    "never skip tests", "never skip checks",
)


def _looks_like_pytest_file(name: str) -> bool:
    if not isinstance(name, str) or not name.endswith(".py"):
        return False
    base = name[:-3]
    return base.startswith("test_") or base.endswith("_test")


def _auto_checks_allowed(request: WorkerRequest) -> bool:
    text = " ".join(s for s in (request.task, request.constraints)
                    if isinstance(s, str)).lower()

    # Find all opt-out phrases present in the text
    present_opt_outs = {p for p in _NO_AUTO_CHECK_PHRASES if p in text}
    if not present_opt_outs:
        return True

    # Find all opposite phrases present
    present_opposites = {p for p in _NOT_AN_OPTOUT_PHRASES if p in text}
    if not present_opposites:
        return False  # genuine opt-outs present, no opposites to negate them

    # Each opposite phrase only negates its specific matching opt-out.
    # An opposite "do not skip X" negates opt-outs containing "skip X".
    # It does NOT negate opt-outs about running/executing/avoiding.
    negated_opt_outs = set()
    for opp in present_opposites:
        # Extract the activity being negated: "do not skip X" → "skip X"
        if opp.startswith("do not skip "):
            activity = "skip " + opp[len("do not skip "):]
        elif opp.startswith("don't skip "):
            activity = "skip " + opp[len("don't skip "):]
        elif opp.startswith("should not skip "):
            activity = "skip " + opp[len("should not skip "):]
        elif opp.startswith("never skip "):
            activity = "skip " + opp[len("never skip "):]
        else:
            continue
        # Negate any opt-out that contains this activity
        for opt_out in present_opt_outs:
            if activity in opt_out:
                negated_opt_outs.add(opt_out)

    # Remaining opt-outs after negation
    remaining_opt_outs = present_opt_outs - negated_opt_outs
    return len(remaining_opt_outs) == 0


def _interpreter_has_pytest(python: str, workspace: str,
                            sandbox=None):
    """Probe-only: is pytest resolvable for this interpreter?

    Runs inside the SAME execution boundary as the later check (see
    _prepare_exec): isolated mode plus find_spec, so no
    workspace-controlled code ever executes here. Uses the same
    filesystem restrictions as verification (bind_binary_dir=False):
    a detected interpreter must already be reachable through the
    workspace or system bindings - the probe never grants broader
    access than the eventual check would have. Returns True
    (pytest found), False (find_spec confirms it is missing), or None
    (availability NOT established: timeout, setup failure, launch
    error, or any other exit - launcher failures use different codes
    and can never read as a verdict). None is never a
    license for an unsandboxed run - callers must report it honestly.
    Bounded by AUTO_CHECK_PROBE_TIMEOUT with cleanup. No installs.
    """
    argv, env, cleanup, error = _prepare_exec(
        [python, "-I", "-c", _PYTEST_PROBE_CODE], workspace, sandbox,
        bind_binary_dir=False)
    if error is not None:
        logger.warning(
            "coding pytest probe setup failed: sandbox=%s workspace=%s "
            "error=%s (not executed)", _sandbox_label(sandbox),
            workspace, error)
        return None
    try:
        try:
            returncode, _, _ = _run_bounded(
                argv, workspace, env, AUTO_CHECK_PROBE_TIMEOUT)
        except (FileNotFoundError, OSError):
            return None
        except Exception:
            return None
        if returncode == _PYTEST_PROBE_FOUND:
            return True
        if returncode == _PYTEST_PROBE_MISSING:
            return False
        return None
    finally:
        if cleanup is not None:
            cleanup()


def detect_python_checks(workspace: str, sandbox=None) -> tuple:
    """Conservative pytest detection. Pure except for probe subprocesses.

    Probes run inside the given verification execution boundary
    (None = explicit unsandboxed compat mode, "bwrap" = bubblewrap).
    Returns (argv|None, status, detail). argv is always a fixed list
    (never shell, never parsed from files). "ready" means markers plus
    a proven runner; "unavailable" means markers but no runner;
    "absent" means no confident identification.
    """
    try:
        top = set(os.listdir(workspace))
    except OSError as e:
        return None, "absent", f"workspace not listable: {e}"
    strong = "pytest.ini" in top or "conftest.py" in top
    found = any(_looks_like_pytest_file(n) for n in top)
    if not (strong or found):
        for sub in AUTO_CHECK_TEST_DIRS:
            subdir = os.path.join(workspace, sub)
            try:
                names = os.listdir(subdir)
            except OSError:
                continue
            if not os.path.isdir(subdir):
                continue
            if any(_looks_like_pytest_file(n) for n in names):
                found = True
                break
    if not (strong or found):
        return (None, "absent",
                "no recognized Python/pytest markers (pytest.ini, "
                "conftest.py, or test_*.py/*_test.py in the workspace "
                "root, tests/, or test/)")
    venv_python = os.path.join(workspace, ".venv", "bin", "python")
    if os.path.isfile(venv_python) and os.access(venv_python, os.X_OK):
        venv_state = _interpreter_has_pytest(venv_python, workspace,
                                             sandbox)
    else:
        venv_state = "skipped"
    if venv_state is True:
        return ([venv_python, "-m", "pytest", "-q"], "ready",
                ".venv/bin/python -m pytest -q")
    python3 = shutil.which("python3")
    py3_state = (_interpreter_has_pytest(python3, workspace, sandbox)
                 if python3 else "skipped")
    if py3_state is True:
        return ([python3, "-m", "pytest", "-q"], "ready",
                "python3 -m pytest -q")
    # Never claim absence from an unestablished probe: any None means
    # availability is unknown, not missing.
    if venv_state == "skipped" and py3_state == "skipped":
        return (None, "unavailable",
                "recognized Python/pytest markers present but no usable "
                "Python interpreter was found (no .venv/bin/python, no "
                "python3 on PATH)")
    if venv_state is not None and py3_state is not None:
        return (None, "unavailable",
                "recognized Python/pytest markers present but pytest is "
                "available in neither .venv/bin/python nor python3; "
                "not installing dependencies")
    return (None, "unavailable",
            "recognized Python/pytest markers present but pytest "
            "availability could not be established (probe or sandbox "
            "setup failed); not assuming pytest is absent and not "
            "installing dependencies")


def verify_result(request: WorkerRequest, result: WorkerResult,
                  sandbox=None, auto_source: str | None = None
                  ) -> WorkerResult:
    """Independently check a worker result against the workspace.

    Never raises. Only ever upgrades information, never success:
    - failed results keep their state (no local run on failure);
    - reported files that do not exist -> independently_verified_fail;
    - an explicit request.verify_command (argv, bounded, cwd=workspace)
      decides independently_verified_pass/fail;
    - otherwise the worker claim stands, explicitly unverified.

    auto_source names an auto-detected project check (e.g.
    "python3 -m pytest -q") so the bounded output is labeled as
    routine hygiene, never as a user-supplied command.

    sandbox selects the verification execution boundary: None runs
    the command directly (historic behavior); "bwrap" runs it inside
    the same bubblewrap confinement as a sandboxed worker, so an
    untrusted modified workspace cannot use verify_command to escape.
    Anything else fails closed (unknown state, never an unsandboxed
    run passed off as sandboxed).
    """
    try:
        if not isinstance(result, WorkerResult) or not result.ok:
            return result
        workspace = request.validated().workspace
        for entry in (result.files_changed or [])[:20]:
            candidate = os.path.realpath(os.path.join(workspace, entry))
            if not os.path.exists(candidate):
                result.verification = "independently_verified_fail"
                result.error = (
                    f"Verification failed: reported file '{entry}' "
                    f"does not exist in the workspace.")
                logger.warning(
                    "coding verification: sandbox=%s workspace=%s "
                    "outcome=independently_verified_fail "
                    "reason=missing-reported-file",
                    _sandbox_label(sandbox), workspace)
                return result
        command = request.validated().verify_command
        if not command:
            return result
        cmd_list = (list(command) if isinstance(command, (list, tuple))
                    else [])
        if not cmd_list:
            return result
        vsandbox = _sandbox_label(sandbox)
        # Verification commands must already live in /usr or the
        # workspace (bind_binary_dir=False): an absolute outside path
        # must fail honestly instead of punching its own directory
        # into the sandbox (see _bwrap_wrap).
        argv, env, cleanup, prep_error = _prepare_exec(
            cmd_list, workspace, sandbox, bind_binary_dir=False)
        if prep_error is not None:
            if sandbox == "bwrap":
                result.verification = "unknown"
                result.error = f"Verification could not run: {prep_error}"
                logger.error(
                    "coding verification sandbox setup failed: "
                    "sandbox=bwrap workspace=%s error=%s "
                    "(not executed; never reported as sandboxed)",
                    workspace, prep_error)
                return result
            result.verification = "unknown"
            result.error = (f"Verification misconfigured: unknown "
                            f"sandbox '{sandbox}'.")
            logger.warning(
                "coding verification misconfigured: sandbox=%s "
                "workspace=%s (not executed)", sandbox, workspace)
            return result
        try:
            try:
                returncode, out_text, err_text = _run_bounded(
                    argv, workspace, env, request.timeout)
            except (FileNotFoundError, OSError) as e:
                result.verification = "unknown"
                result.error = f"Verification could not run: {e}"
                logger.warning(
                    "coding verification launch failed: sandbox=%s "
                    "workspace=%s error=%s", vsandbox, workspace, e)
                return result
            if returncode is None:
                result.verification = "unknown"
                result.verify_exit = None
                result.verify_output = ""
                result.error = "Verification timed out."
                logger.warning(
                    "coding verification timed out: sandbox=%s "
                    "workspace=%s", vsandbox, workspace)
                return result
        finally:
            if cleanup is not None:
                cleanup()
        summary, _ = _verify_summary(out_text or "", err_text or "")
        if auto_source:
            tagged = (f"[auto-detected project check: {auto_source}]"
                      + ("\n" + summary if summary else ""))
            if len(tagged) <= VERIFY_SUMMARY_MAX_CHARS:
                summary = tagged
            else:
                summary = (tagged[:VERIFY_SUMMARY_MAX_CHARS]
                           + VERIFY_TRUNCATION_NOTE)
        result.verify_exit = returncode
        result.verify_output = summary
        if returncode == 0:
            result.verification = "independently_verified_pass"
        else:
            result.verification = "independently_verified_fail"
            clipped, _ = _clip(err_text or out_text or "")
            result.error = (
                f"Verification command failed "
                f"(exit {returncode}): {clipped[:500]}")
        logger.info(
            "coding verification: sandbox=%s workspace=%s outcome=%s "
            "exit=%s source=%s", vsandbox, workspace, result.verification,
            returncode,
            ("auto:" + auto_source) if auto_source else "explicit")
        return result
    except Exception as e:
        try:
            result.verification = "unknown"
            result.error = f"Verification error: {e}"
        except Exception:
            pass
        return result


class WorkerProvider:
    """Boundary: run a validated request, return a WorkerResult.

    Subclasses implement run(). Chibi-visible behavior depends only on
    this interface, so the coding backend can change without touching
    the assistant.
    """

    name = "base"

    def run(self, request: WorkerRequest) -> WorkerResult:
        raise NotImplementedError


# ─── bubblewrap sandbox (default; explicit escape hatch) ─────────
#
# Boundary: the worker sees ONLY the validated workspace (writable)
# plus a throwaway HOME/XDG area and a private /tmp. The real home
# directory (assistant config, SSH keys, browser profiles, sibling
# repos), host /tmp, and host IPC (Wayland/X11, D-Bus, ssh-agent)
# are never bound. System runtime (/usr, loader symlinks, /etc
# read-only, /dev, /proc) stays available so stock CLIs execute.
#
# Deliberately NOT isolated: the network namespace is shared with the
# host. Cloud coding CLIs (and a host Ollama at localhost) require
# network during normal execution; pretending otherwise would
# silently break them. There is no proxy/allowlist layer here.
#
# Identity: --uid/--gid map the sandbox to the invoking user (no root
# inside; getuid() reports the real uid). This is filesystem/process
# confinement, NOT protection against kernel exploits.

SANDBOX_NONE = "none"
SANDBOX_BWRAP = "bwrap"
SANDBOX_MODES = (SANDBOX_NONE, SANDBOX_BWRAP)
BWRAP_BIN = "bwrap"

# Minimal child environment: everything else (DISPLAY, WAYLAND_*,
# DBUS_*, SSH_AUTH_SOCK, XDG_RUNTIME_DIR, GEMINI_API_KEY, secrets)
# is dropped. GOOGLE_API_KEY passes only because Goose puts it there
# via its own _child_env (OpenCode inherits no credential at all).
_SANDBOX_ENV_KEEP = frozenset({
    "PATH", "LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE", "TERM",
    "USER", "LOGNAME", "SHELL", "NO_COLOR",
    "GOOSE_MODEL", "GOOSE_PROVIDER", "GOOSE_MODE",
    "GOOGLE_API_KEY",
})


def _make_sandbox_home() -> str:
    """Create a throwaway HOME with XDG subdirs. Caller removes it."""
    import tempfile
    home = tempfile.mkdtemp(prefix="cat-talker-sandbox-home-")
    for sub in ("config", "cache", "share"):
        os.makedirs(os.path.join(home, sub), exist_ok=True)
    return home


def _sandbox_env(base_env) -> dict:
    """Minimal sandbox environment (names only, values from base).

    base_env None means the current process environment. Anything not
    in the allowlist is dropped — notably GEMINI_API_KEY, display
    servers, D-Bus, ssh-agent, and the host XDG runtime dir.
    """
    base = os.environ if base_env is None else base_env
    get = (base.get if hasattr(base, "get")
           else lambda k, d=None: base[k] if k in base else d)
    env = {}
    for name in sorted(_SANDBOX_ENV_KEEP):
        try:
            value = get(name)
        except Exception:
            value = None
        if isinstance(value, str) and value:
            env[name] = value
    return env


def _bwrap_wrap(inner_argv, workspace: str, home: str,
                base_env, bind_binary_dir: bool = True) -> tuple:
    """Wrap inner_argv (list, after --) for bubblewrap confinement.

    Returns (argv, env). Pure construction (no execution) so tests
    assert the exact boundary. Raises ValueError when bwrap is
    missing (fail closed: the caller must not run unsandboxed).

    bind_binary_dir ro-binds argv[0]'s directory so the process can
    execute. True for the Chibi-chosen worker binary; False for
    verification commands, which must live in /usr or the workspace —
    otherwise an absolute outside path would punch its own directory
    into the sandbox.
    """
    bwrap = shutil.which(BWRAP_BIN)
    if not bwrap:
        raise ValueError(
            "Coding error: sandbox 'bwrap' requested but the 'bwrap' "
            "executable was not found on PATH.")
    uid, gid = os.geteuid(), os.getegid()
    argv = [
        bwrap,
        "--unshare-user", "--unshare-pid", "--unshare-ipc",
        "--unshare-uts",
        # Network namespace deliberately NOT unshared: cloud CLIs and
        # host-local providers need real network (see module notes).
        "--uid", str(uid), "--gid", str(gid),
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib", "/lib64",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", workspace, workspace,
        "--bind", home, home,
    ]
    # The worker binary itself must be visible (bind_binary_dir=True
    # only): CLIs outside /usr (e.g. ~/.local/bin) get their
    # containing directory bound read-only. Relative commands resolve
    # inside the workspace. Verification passes False (see below).
    binary = inner_argv[0] if inner_argv else ""
    if bind_binary_dir:
        if os.path.isabs(binary):
            bindir = os.path.dirname(binary)
            if bindir and bindir != "/usr/bin":
                argv += ["--ro-bind", bindir, bindir]
        elif os.path.basename(binary) != binary:
            pass  # relative: resolved against cwd=workspace (bound)
        else:
            located = shutil.which(binary)
            if located:
                bindir = os.path.dirname(located)
                if bindir != "/usr/bin":
                    argv += ["--ro-bind", bindir, bindir]
            # else: spawn fails honestly inside the sandbox (exit 127).
    argv += [
        "--chdir", workspace,
        "--die-with-parent",
        "--",
    ] + list(inner_argv)
    env = _sandbox_env(base_env)
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = os.path.join(home, "config")
    env["XDG_CACHE_HOME"] = os.path.join(home, "cache")
    env["XDG_DATA_HOME"] = os.path.join(home, "share")
    env["TMPDIR"] = "/tmp"
    return argv, env


class SubprocessWorkerProvider(WorkerProvider):
    """Run an external coding CLI as a bounded argv-only subprocess."""

    name = "subprocess-cli"

    def __init__(self, binary: str | None = None,
                 extra_args: tuple = DEFAULT_CLI_ARGS,
                 timeout: int = WORKER_TIMEOUT_DEFAULT):
        configured = (binary if isinstance(binary, str) and binary.strip()
                      else os.environ.get("CAT_TALKER_CODER_BIN", "").strip()
                      or DEFAULT_CLI_BIN)
        self.binary = configured
        self.extra_args = tuple(extra_args) if extra_args else ()
        try:
            self.timeout = max(1, min(WORKER_TIMEOUT_MAX, int(timeout)))
        except (TypeError, ValueError):
            self.timeout = WORKER_TIMEOUT_DEFAULT

    def _build_argv(self, resolved: str, req: WorkerRequest) -> list:
        """Argv for the worker process. Subclasses override ONLY this:
        validation, spawning, timeout, cleanup, output bounds, and
        result parsing stay shared below."""
        prompt = req.task
        if req.constraints:
            prompt += "\nConstraints: " + req.constraints
        # Explicit project scoping alongside cwd (--dir is advisory to
        # the agent, not a sandbox; see module docstring).
        return ([resolved] + list(self.extra_args)
                + ["--dir", req.workspace, prompt])

    def _child_env(self, req: WorkerRequest) -> dict | None:
        """Environment for the worker process. None inherits the
        current environment (OpenCode default). Subclasses override
        to isolate credentials without touching shared logic."""
        return None

    def run(self, request: WorkerRequest) -> WorkerResult:
        req = request.validated()
        resolved = shutil.which(self.binary)
        if not resolved:
            return WorkerResult(
                ok=False,
                error=f"Coding worker not installed: '{self.binary}' "
                      f"not found on PATH.",
                workspace=req.workspace)
        argv = self._build_argv(resolved, req)
        timeout = min(req.timeout, self.timeout)
        logger.info("coding worker launch: backend=%s sandbox=none "
                    "workspace=%s (direct unsandboxed invocation)",
                    _backend_label(self), req.workspace)
        try:
            returncode, out_text, err_text = _run_bounded(
                argv, req.workspace, self._child_env(req), timeout)
        except FileNotFoundError as e:
            return WorkerResult(ok=False, error=f"Coding worker missing: {e}")
        except OSError as e:
            return WorkerResult(ok=False, error=f"Coding worker failed: {e}")
        except Exception as e:
            return WorkerResult(ok=False, error=f"Coding worker failed: {e}")
        if returncode is None:
            return WorkerResult(
                ok=False,
                error=f"Coding worker timed out after {timeout}s; stopped.",
                workspace=req.workspace)
        out, out_cut = _clip(out_text or "")
        err, err_cut = _clip(err_text or "")
        return result_from_process(returncode, out, err,
                                   out_cut or err_cut, req.workspace)


class GooseWorkerProvider(SubprocessWorkerProvider):
    """Run Goose (explicit opt-in backend) as a bounded argv-only process.

    Uses the locally verified headless shape (`goose run --no-session
    -q --max-turns N -t prompt`, cwd=workspace). Plain-text stdout is
    consumed by the shared result path; `--output-format json` is a
    future follow-up once a real coding-task JSON result is captured.
    Goose is never configured from here: a missing binary or missing
    provider setup fails honestly. Same honesty as the default CLI:
    argv-only, cwd-scoped, timeout-bound, no OS sandbox — Goose runs
    with the user's privileges inside the validated workspace.
    """

    name = "goose-cli"

    def __init__(self, binary: str | None = None,
                 max_turns: int = GOOSE_MAX_TURNS,
                 timeout: int = WORKER_TIMEOUT_DEFAULT,
                 model_default: str = GOOSE_DEFAULT_MODEL):
        super().__init__(binary=(binary if isinstance(binary, str)
                                 and binary.strip() else GOOSE_CLI_BIN),
                         extra_args=(), timeout=timeout)
        try:
            self.max_turns = max(1, min(100, int(max_turns)))
        except (TypeError, ValueError):
            self.max_turns = GOOSE_MAX_TURNS
        if not isinstance(model_default, str) or not model_default.strip():
            model_default = GOOSE_DEFAULT_MODEL
        self.goose_model = resolve_goose_model(model_default.strip())

    def _build_argv(self, resolved: str, req: WorkerRequest) -> list:
        prompt = req.task
        if req.constraints:
            prompt += "\nConstraints: " + req.constraints
        # No --dir equivalent exists (verified `goose run --help`):
        # cwd=workspace (set by the shared run path) is the scope.
        # Credentials never appear here (see _child_env).
        return [resolved, "run", "--no-session", "-q",
                "--max-turns", str(self.max_turns),
                "--provider", GOOSE_PROVIDER,
                "--model", self.goose_model,
                "-t", prompt]

    def _child_env(self, req: WorkerRequest) -> dict:
        """Hand Chibi's secure Gemini key to Goose as GOOGLE_API_KEY.

        Reads the established credential path (secure store, then
        legacy, then env) and rebuilds the child environment: the
        Chibi-side GEMINI_API_KEY name is removed so only the
        Google-provider name reaches Goose. Never logged, never in
        argv, never persisted. Raises ValueError (honest failure
        before launch) when no key exists — no fallback, no Ollama.
        """
        from cat_talker import config as config_mod
        key = config_mod.get_api_key()
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                "Coding error: no Gemini API key available for the "
                "Goose backend (Google provider needs GOOGLE_API_KEY). "
                "Add a key via Settings or GEMINI_API_KEY; "
                "Goose is not configured automatically.")
        child = dict(os.environ)
        child.pop("GEMINI_API_KEY", None)
        child["GOOGLE_API_KEY"] = key.strip()
        return child


def _kill_process_group(proc):
    """Best-effort process-group cleanup (used on timeout paths)."""
    try:
        import signal as _signal
        os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_bounded(argv, cwd, env, timeout):
    """Spawn argv (list, never shell) bounded by timeout seconds.

    Shared by worker execution and independent verification so both
    get identical timeout/process-group semantics. Returns
    (returncode, stdout, stderr); returncode None means the timeout
    fired (the whole process group is already killed). Popen-time
    errors propagate for the caller to report honestly.
    """
    proc = subprocess.Popen(
        argv, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=env)
    try:
        out_text, err_text = proc.communicate(timeout=timeout)
        return proc.returncode, out_text, err_text
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        return None, "", ""


class BubblewrapWorkerProvider(WorkerProvider):
    """Confine any SubprocessWorkerProvider backend in bubblewrap.

    Decorator: validation, argv, credentials, timeout, output bounds,
    result parsing, and verification stay exactly the inner
    provider's — only the execution boundary changes (bwrap prefix,
    throwaway HOME, minimal env). Works for OpenCode, Goose, and
    Goose-Lite without duplicating their behavior. Exposes
    verify_sandbox so delegate() routes independent verification
    through the same boundary.
    """

    name = "bwrap-sandbox"
    verify_sandbox = "bwrap"

    def __init__(self, inner: WorkerProvider):
        if not isinstance(inner, SubprocessWorkerProvider):
            raise ValueError(
                "Coding error: bubblewrap sandbox wraps a "
                "SubprocessWorkerProvider backend only.")
        self.inner = inner

    def run(self, request: WorkerRequest) -> WorkerResult:
        req = request.validated()
        inner = self.inner
        resolved = shutil.which(inner.binary)
        if not resolved:
            return WorkerResult(
                ok=False,
                error=f"Coding worker not installed: '{inner.binary}' "
                      f"not found on PATH.",
                workspace=req.workspace)
        try:
            inner_argv = inner._build_argv(resolved, req)
            inner_env = inner._child_env(req)
        except (ValueError, PermissionError) as e:
            return WorkerResult(ok=False, error=str(e),
                                workspace=req.workspace)
        home = _make_sandbox_home()
        try:
            try:
                argv, env = _bwrap_wrap(inner_argv, req.workspace, home,
                                        inner_env)
            except ValueError as e:
                logger.error(
                    "coding worker sandbox setup failed: backend=%s "
                    "workspace=%s error=%s "
                    "(not executed; never reported as sandboxed)",
                    _backend_label(self), req.workspace, e)
                return WorkerResult(ok=False, error=str(e),
                                    workspace=req.workspace)
            logger.info("coding worker launch: backend=%s sandbox=bwrap "
                        "workspace=%s (bubblewrap invocation constructed)",
                        _backend_label(self), req.workspace)
            timeout = min(req.timeout, inner.timeout)
            try:
                returncode, out_text, err_text = _run_bounded(
                    argv, req.workspace, env, timeout)
            except (FileNotFoundError, OSError) as e:
                return WorkerResult(
                    ok=False,
                    error=f"Coding worker sandbox failed: {e}",
                    workspace=req.workspace)
            except Exception as e:
                return WorkerResult(
                    ok=False,
                    error=f"Coding worker sandbox failed: {e}",
                    workspace=req.workspace)
            if returncode is None:
                return WorkerResult(
                    ok=False,
                    error=f"Coding worker timed out after {timeout}s; "
                          f"stopped.",
                    workspace=req.workspace)
            out, out_cut = _clip(out_text or "")
            err, err_cut = _clip(err_text or "")
            return result_from_process(returncode, out, err,
                                       out_cut or err_cut, req.workspace)
        finally:
            shutil.rmtree(home, ignore_errors=True)


def select_sandbox(name: str | None = None) -> str:
    """Sandbox mode: 'bwrap' default, 'none' explicit compatibility
    escape hatch. Raises ValueError on anything else (fail closed,
    never silent)."""
    cleaned = (name if isinstance(name, str) and name.strip()
               else os.environ.get("CAT_TALKER_CODER_SANDBOX", "")
               ).strip().lower()
    if not cleaned:
        cleaned = SANDBOX_BWRAP
    if cleaned not in SANDBOX_MODES:
        raise ValueError(
            f"Coding error: unknown coder sandbox '{cleaned}'. "
            f"Allowed: {', '.join(SANDBOX_MODES)}.")
    return cleaned


def _sandbox_label(sandbox) -> str:
    """Effective verification boundary from the resolved sandbox arg."""
    if sandbox is None:
        return SANDBOX_NONE
    if sandbox == SANDBOX_BWRAP:
        return SANDBOX_BWRAP
    return "unknown"


def _prepare_exec(cmd_list, workspace: str, sandbox,
                  bind_binary_dir: bool):
    """Build the real execution triple for a workspace command.

    Returns (argv, env, cleanup, error). error None means argv may be
    executed exactly once within the stated boundary; otherwise
    nothing may run (setup failure or unknown sandbox - never fall
    back to an unsandboxed execution). cleanup removes the throwaway
    sandbox home (None when there is none).
    """
    if sandbox is None:
        return list(cmd_list), None, None, None
    if sandbox == SANDBOX_BWRAP:
        try:
            home = _make_sandbox_home()
        except OSError as e:
            return None, None, None, f"could not create sandbox: {e}"
        try:
            argv, env = _bwrap_wrap(cmd_list, workspace, home, None,
                                    bind_binary_dir=bind_binary_dir)
        except ValueError as e:
            shutil.rmtree(home, ignore_errors=True)
            return None, None, None, str(e)
        return (argv, env,
                lambda: shutil.rmtree(home, ignore_errors=True), None)
    return None, None, None, f"unknown sandbox '{sandbox}'"


def _backend_label(provider: WorkerProvider) -> str:
    """Honest backend label from the resolved provider (never config).

    Unwraps the bubblewrap decorator to the real backend. Goose
    variants are distinguished by their resolved model; a non-default
    worker binary is labeled custom (binary basename only, no args).
    Anything else falls back to the provider class name.
    """
    inner = (provider.inner
             if isinstance(provider, BubblewrapWorkerProvider)
             and isinstance(provider.inner, SubprocessWorkerProvider)
             else provider)
    if isinstance(inner, GooseWorkerProvider):
        model = getattr(inner, "goose_model", "") or ""
        tag = (BACKEND_GOOSE_LITE
               if model == GOOSE_FLASH_LITE_MODEL else BACKEND_GOOSE)
        return f"{tag}:{model}" if model else tag
    if isinstance(inner, SubprocessWorkerProvider):
        binary = getattr(inner, "binary", "") or ""
        if binary == DEFAULT_CLI_BIN:
            return BACKEND_OPENCODE
        return f"custom-cli:{os.path.basename(binary)}"
    name = type(provider).__name__ or "unknown"
    return name


def _execution_boundary(engine: WorkerProvider) -> str:
    """Effective coding boundary from the resolved engine (never env).

    Only a successfully constructed BubblewrapWorkerProvider reports
    'bwrap'; a direct subprocess engine reports 'none'; anything else
    is honestly 'unknown'.
    """
    if isinstance(engine, BubblewrapWorkerProvider):
        return SANDBOX_BWRAP
    if isinstance(engine, SubprocessWorkerProvider):
        return SANDBOX_NONE
    return "unknown"


def maybe_sandbox(provider: WorkerProvider,
                  mode: str | None = None) -> WorkerProvider:
    """Wrap a subprocess backend in bubblewrap unless explicitly
    disabled. Custom (non-subprocess) providers are left alone."""
    if select_sandbox(mode) == SANDBOX_BWRAP:
        if isinstance(provider, BubblewrapWorkerProvider):
            return provider
        if isinstance(provider, SubprocessWorkerProvider):
            return BubblewrapWorkerProvider(provider)
    return provider


_DEFAULT_PROVIDER = SubprocessWorkerProvider()


def select_backend(name: str | None = None) -> WorkerProvider:
    """Choose the coding backend explicitly. OpenCode is always the
    default; Goose runs only on explicit request. Raises ValueError
    on unknown names (never a silent fallback)."""
    cleaned = (name if isinstance(name, str) and name.strip()
               else os.environ.get("CAT_TALKER_CODER_BACKEND", "")).strip().lower()
    if not cleaned:
        cleaned = BACKEND_OPENCODE
    if cleaned == BACKEND_GOOSE:
        return GooseWorkerProvider()
    if cleaned == BACKEND_GOOSE_LITE:
        return GooseWorkerProvider(model_default=GOOSE_FLASH_LITE_MODEL)
    if cleaned == BACKEND_OPENCODE:
        return SubprocessWorkerProvider()
    raise ValueError(
        f"Coding error: unknown coder backend '{cleaned}'. "
        f"Allowed: {', '.join(BACKENDS)}.")


def get_default_provider() -> WorkerProvider:
    return _DEFAULT_PROVIDER


def delegate(request: WorkerRequest | dict,
             provider: WorkerProvider | None = None) -> str:
    """Validate, execute via provider, verify, render. Never raises."""
    try:
        req = request.validated() if isinstance(
            request, WorkerRequest) else build_request(**request)
        engine = maybe_sandbox(
            provider if provider is not None else select_backend())
        backend = _backend_label(engine)
        boundary = _execution_boundary(engine)
        verify_requested = bool(req.verify_command)
        logger.info("coding task start: backend=%s sandbox=%s "
                    "workspace=%s verify_requested=%s",
                    backend, boundary, req.workspace, verify_requested)
        try:
            result = engine.run(req)
        except NotImplementedError as e:
            logger.warning("coding task launch failed: backend=%s "
                           "sandbox=%s workspace=%s error=%s",
                           backend, boundary, req.workspace, e)
            return f"Coding error: {e}"
        except Exception as e:
            logger.warning("coding task launch failed: backend=%s "
                           "sandbox=%s workspace=%s error=%s",
                           backend, boundary, req.workspace, e)
            return f"Coding task FAILED.\nError: {e}"
        if not isinstance(result, WorkerResult):
            logger.warning("coding task produced no result: backend=%s "
                           "sandbox=%s workspace=%s",
                           backend, boundary, req.workspace)
            return "Coding task FAILED.\nError: worker returned no result."
        if not result.ok:
            logger.warning("coding task failed: backend=%s sandbox=%s "
                           "workspace=%s", backend, boundary, req.workspace)
        result.backend = backend
        result.sandbox = boundary
        sandbox = getattr(engine, "verify_sandbox", None)
        auto_source = None
        if (req.auto_verify and result.ok
                and req.validated().verify_command is None):
            if not _auto_checks_allowed(req):
                logger.info("coding auto check skipped: workspace=%s "
                            "reason=user opted out", req.workspace)
            else:
                detected, status, detail = detect_python_checks(
                    req.workspace, sandbox)
                if status == "ready" and detected:
                    from dataclasses import replace as _replace
                    req = _replace(req,
                                   verify_command=tuple(detected))
                    auto_source = detail
                    logger.info(
                        "coding auto check selected: backend=%s "
                        "sandbox=%s workspace=%s command=%s",
                        backend, _sandbox_label(sandbox),
                        req.workspace, detail)
                elif status == "unavailable":
                    result.verification = "verification_unavailable"
                    result.verify_output = detail
                    result.error = detail
                    logger.warning(
                        "coding auto check unavailable: backend=%s "
                        "sandbox=%s workspace=%s reason=%s",
                        backend, _sandbox_label(sandbox),
                        req.workspace, detail)
                    return result.render()
                else:
                    logger.debug("coding auto check absent: workspace=%s",
                                 req.workspace)
        return verify_result(req, result, sandbox=sandbox,
                             auto_source=auto_source).render()
    except (ValueError, PermissionError) as e:
        return str(e)
    except Exception as e:
        return f"Coding task FAILED.\nError: {e}"
