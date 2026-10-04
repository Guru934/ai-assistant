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

# First CLI wrapped. Overridable without code changes via
# CAT_TALKER_CODER_BIN (bare binary name or absolute path; it becomes
# argv[0], never a shell string).
DEFAULT_CLI_BIN = "opencode"
DEFAULT_CLI_ARGS = ("run",)


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
        return WorkerRequest(workspace=workspace,
                             task=self.task.strip(),
                             constraints=constraints.strip(),
                             timeout=timeout,
                             verify_command=verify)


def build_request(workspace: str, task: str, constraints: str = "",
                  timeout: int = WORKER_TIMEOUT_DEFAULT,
                  verify_command=None) -> WorkerRequest:
    """Construct and validate a worker request. Raises on any problem."""
    return WorkerRequest(workspace=workspace, task=task,
                         constraints=constraints,
                         timeout=timeout,
                         verify_command=verify_command).validated()


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

    def render(self) -> str:
        """Human-readable report. Only independent verification may be
        worded as verified fact; everything else is an explicit claim
        or an explicit absence of verification. Failure never claims
        completion."""
        if not self.ok:
            lines = ["Coding task FAILED."]
            if self.error:
                lines.append(f"Error: {self.error}")
            elif self.summary:
                lines.append(f"Detail: {self.summary}")
            return "\n".join(lines)
        if self.verification == "independently_verified_pass":
            lines = ["Coding task succeeded (independently verified): "
                     + (self.summary or "done.")]
        elif self.verification == "independently_verified_fail":
            lines = ["Worker completed; "
                     "independent verification FAILED."]
            if self.summary:
                lines.append(f"Worker reports: {self.summary}")
        else:
            lines = ["Worker completed; verification was not available."]
            if self.summary:
                lines.append(f"Worker reports: {self.summary}")
        if self.files_changed:
            lines.append("Files changed: "
                         + ", ".join(self.files_changed))
        if self.commands:
            shown = "; ".join(self.commands[:5])
            lines.append(f"Commands run: {shown}")
        if self.verification == "independently_verified_fail":
            lines.append("Independent verification: FAILED.")
        elif self.tests_passed is True:
            lines.append("Tests: worker reports tests passed "
                         "(NOT independently verified).")
        elif self.tests_passed is False:
            lines.append("Tests: worker reports tests FAILED.")
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
                                    verification=_claim_state(tests))
            return WorkerResult(
                ok=False, summary=summary + note, files_changed=files,
                commands=commands, tests_passed=False,
                error=(str(envelope.get("error", ""))[:1000]
                       or f"worker exited with code {returncode}"),
                truncated=truncated,
                verification=_claim_state(False))
        clipped, _ = _clip(stdout or "")
        err_clipped, _ = _clip(stderr or "")
        if returncode == 0:
            return WorkerResult(ok=True, summary=clipped or "done.",
                                truncated=truncated,
                                verification="unknown")
        return WorkerResult(
            ok=False, summary=clipped,
            error=err_clipped or f"worker exited with code {returncode}",
            truncated=truncated,
            verification="unknown")
    except Exception as e:
        return WorkerResult(ok=False,
                            error=f"Worker result handling failed: {e}",
                            verification="unknown")


def verify_result(request: WorkerRequest, result: WorkerResult) -> WorkerResult:
    """Independently check a worker result against the workspace.

    Never raises. Only ever upgrades information, never success:
    - failed results keep their state (no local run on failure);
    - reported files that do not exist -> independently_verified_fail;
    - an explicit request.verify_command (argv, bounded, cwd=workspace)
      decides independently_verified_pass/fail;
    - otherwise the worker claim stands, explicitly unverified.
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
                return result
        command = request.validated().verify_command
        if not command:
            return result
        cmd_list = (list(command) if isinstance(command, (list, tuple))
                    else [])
        if not cmd_list:
            return result
        try:
            proc = subprocess.Popen(
                cmd_list, cwd=workspace,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
        except (FileNotFoundError, OSError) as e:
            result.verification = "unknown"
            result.error = f"Verification could not run: {e}"
            return result
        try:
            out_text, err_text = proc.communicate(timeout=request.timeout)
            passed = proc.returncode == 0
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            result.verification = "unknown"
            result.error = "Verification timed out."
            return result
        if passed:
            result.verification = "independently_verified_pass"
        else:
            result.verification = "independently_verified_fail"
            clipped, _ = _clip(err_text or out_text or "")
            result.error = (
                f"Verification command failed "
                f"(exit {proc.returncode}): {clipped[:500]}")
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

    def run(self, request: WorkerRequest) -> WorkerResult:
        req = request.validated()
        resolved = shutil.which(self.binary)
        if not resolved:
            return WorkerResult(
                ok=False,
                error=f"Coding worker not installed: '{self.binary}' "
                      f"not found on PATH.")
        prompt = req.task
        if req.constraints:
            prompt += "\nConstraints: " + req.constraints
        # Explicit project scoping alongside cwd (--dir is advisory to
        # the agent, not a sandbox; see module docstring).
        argv = ([resolved] + list(self.extra_args)
                + ["--dir", req.workspace, prompt])
        timeout = min(req.timeout, self.timeout)
        try:
            proc = subprocess.Popen(
                argv, cwd=req.workspace,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, start_new_session=True)
        except FileNotFoundError as e:
            return WorkerResult(ok=False, error=f"Coding worker missing: {e}")
        except OSError as e:
            return WorkerResult(ok=False, error=f"Coding worker failed: {e}")
        except Exception as e:
            return WorkerResult(ok=False, error=f"Coding worker failed: {e}")
        try:
            out_text, err_text = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            return WorkerResult(
                ok=False,
                error=f"Coding worker timed out after {timeout}s; stopped.")
        except Exception as e:
            _kill_process_group(proc)
            return WorkerResult(ok=False, error=f"Coding worker failed: {e}")
        out, out_cut = _clip(out_text or "")
        err, err_cut = _clip(err_text or "")
        return result_from_process(returncode, out, err,
                                   out_cut or err_cut, req.workspace)


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


_DEFAULT_PROVIDER = SubprocessWorkerProvider()


def get_default_provider() -> WorkerProvider:
    return _DEFAULT_PROVIDER


def delegate(request: WorkerRequest,
             provider: WorkerProvider | None = None) -> str:
    """Validate, execute via provider, verify, render. Never raises."""
    try:
        req = request.validated() if isinstance(
            request, WorkerRequest) else build_request(**request)
        engine = provider if provider is not None else _DEFAULT_PROVIDER
        try:
            result = engine.run(req)
        except NotImplementedError as e:
            return f"Coding error: {e}"
        except Exception as e:
            return f"Coding task FAILED.\nError: {e}"
        if not isinstance(result, WorkerResult):
            return "Coding task FAILED.\nError: worker returned no result."
        return verify_result(req, result).render()
    except (ValueError, PermissionError) as e:
        return str(e)
    except Exception as e:
        return f"Coding task FAILED.\nError: {e}"
