"""Focused tests for the external coding worker boundary.

The coding CLI is always mocked (fake Popen / fake provider): no real
model, API, network, or workspace mutation. A socket guard fails any
test that reaches for real sockets.
"""

import json
import os
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cat_talker.agent import SIDE_EFFECT_TOOLS, build_system_instructions
from cat_talker.tools import ALL_TOOLS
import cat_talker.coding_worker as cw
from cat_talker.coding_worker import (
    SubprocessWorkerProvider,
    WorkerRequest,
    WorkerResult,
    build_request,
    delegate,
    resolve_workspace,
    result_from_process,
)


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("real network access blocked in tests")
    monkeypatch.setattr(socket, "create_connection", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


@pytest.fixture(autouse=True)
def _fake_chibi_key(monkeypatch):
    # No test may touch the real secure credential store: the Goose
    # handoff always reads this fake key instead.
    monkeypatch.setattr("cat_talker.config.get_api_key",
                        lambda: "fake-test-key")


@pytest.fixture
def _clean_pending():
    import cat_talker.tools as tools_mod
    saved = tools_mod.PENDING_RISKY_ACTION
    tools_mod.PENDING_RISKY_ACTION = None
    yield tools_mod
    tools_mod.PENDING_RISKY_ACTION = saved


class _FakePopen:
    def __init__(self, out="done", err="", code=0, pid=4242):
        self._out, self._err, self.returncode, self.pid = out, err, code, pid
        self.killed = False
        self.timeout_used = None

    def communicate(self, timeout=5):
        self.timeout_used = timeout
        return self._out, self._err

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


def _mock_popen(monkeypatch, popen=None, binary="/usr/bin/opencode"):
    seen = {}
    monkeypatch.setattr(cw.shutil, "which", lambda _: binary)

    def _fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        proc = popen if popen is not None else _FakePopen()
        seen["proc"] = proc
        return proc

    monkeypatch.setattr(cw.subprocess, "Popen", _fake_popen)
    return seen


# ─── request construction / workspace boundary ────────────────────

def test_build_request_ok(tmp_path):
    req = build_request(str(tmp_path), "add a test", timeout=60)
    assert req.workspace == os.path.realpath(str(tmp_path))
    assert req.task == "add a test"
    assert req.timeout == 60


def test_build_request_rejects_empty(tmp_path):
    with pytest.raises(ValueError, match="no coding task"):
        build_request(str(tmp_path), "   ")
    with pytest.raises(ValueError, match="no workspace"):
        build_request("", "do it")
    with pytest.raises(ValueError, match="not a directory"):
        build_request(str(tmp_path / "missing"), "do it")


def test_workspace_inside_protected_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_protected_roots",
                        lambda: [os.path.realpath(str(tmp_path / "safe"))])
    target = tmp_path / "safe" / "sub"
    target.mkdir(parents=True)
    with pytest.raises(PermissionError, match="protected"):
        resolve_workspace(str(target))


def test_workspace_containing_protected_denied(tmp_path, monkeypatch):
    secret = tmp_path / "vault"
    secret.mkdir()
    monkeypatch.setattr(cw, "_protected_roots",
                        lambda: [os.path.realpath(str(secret))])
    with pytest.raises(PermissionError, match="protected"):
        resolve_workspace(str(tmp_path))


def test_timeout_clamped():
    ws = os.path.realpath("/tmp")
    assert build_request(ws, "t", timeout=99999).timeout == cw.WORKER_TIMEOUT_MAX
    assert build_request(ws, "t", timeout=0).timeout >= 1


# ─── argv execution ───────────────────────────────────────────────

def test_argv_invocation_no_shell(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch)
    provider = SubprocessWorkerProvider()
    result = provider.run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert seen["argv"][:2] == ["/usr/bin/opencode", "run"]
    assert seen["argv"][-1] == "fix bug"
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["cwd"] == os.path.realpath(str(tmp_path))
    assert seen["proc"].timeout_used == 300
    assert seen["kwargs"]["start_new_session"] is True


def test_missing_binary_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which", lambda _: None)
    result = SubprocessWorkerProvider().run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is False
    assert "not installed" in result.error


def test_timeout_kills_process_group(tmp_path, monkeypatch):
    _mock_popen(monkeypatch)
    real_popen = cw.subprocess.Popen
    killed = []

    class _Hanging(_FakePopen):
        def communicate(self, timeout=5):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _Hanging())
    monkeypatch.setattr(cw.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(cw.os, "getpgid", lambda pid: 777)
    provider = SubprocessWorkerProvider(timeout=5)
    result = provider.run(build_request(str(tmp_path), "slow", timeout=5))
    assert result.ok is False
    assert "timed out" in result.error
    assert killed, "process group was not cleaned up"
    assert real_popen is not None


def test_output_bounded(tmp_path, monkeypatch):
    big = "x" * (cw.OUTPUT_MAX_BYTES * 3)
    _mock_popen(monkeypatch, popen=_FakePopen(out=big))
    result = SubprocessWorkerProvider().run(
        build_request(str(tmp_path), "t"))
    assert result.ok is True
    assert result.truncated is True
    assert len(result.summary.encode()) <= cw.OUTPUT_MAX_BYTES + 64
    assert "TRUNCATED" in result.summary


def test_nonzero_exit_is_failure(tmp_path, monkeypatch):
    _mock_popen(monkeypatch, popen=_FakePopen(out="", err="boom", code=2))
    result = SubprocessWorkerProvider().run(
        build_request(str(tmp_path), "t"))
    assert result.ok is False
    assert "boom" in result.error


# ─── result schema ────────────────────────────────────────────────

def test_malformed_output_still_deterministic(tmp_path):
    result = result_from_process(0, "just some text", "", False,
                                 str(tmp_path))
    assert result.ok is True
    assert result.summary == "just some text"


def test_json_envelope_parsed(tmp_path):
    envelope = json.dumps({
        "summary": "fixed",
        "files_changed": ["a.py"],
        "commands": ["pytest -q"],
        "tests_passed": True,
    })
    result = result_from_process(0, envelope, "", False, str(tmp_path))
    assert result.ok is True
    assert result.files_changed == ["a.py"]
    assert result.tests_passed is True


def test_files_outside_workspace_dropped(tmp_path):
    envelope = json.dumps({
        "summary": "x",
        "files_changed": ["ok.py", "/etc/passwd", "../../escape.py"],
    })
    result = result_from_process(0, envelope, "", False, str(tmp_path))
    assert result.files_changed == ["ok.py"]
    assert "excluded" in result.summary


def test_no_completion_claim_on_failure():
    result = WorkerResult(ok=False, error="it broke")
    rendered = result.render()
    assert "FAILED" in rendered
    assert "complet" not in rendered.lower()
    assert "succeed" not in rendered.lower()


def test_success_render_reports_truth(tmp_path):
    result = WorkerResult(ok=True, summary="fixed", files_changed=["a.py"],
                          commands=["pytest -q"], tests_passed=True)
    rendered = result.render()
    # Worker claim only: reported as unverified, never as verified fact.
    assert "verification was not available" in rendered
    assert "NOT independently verified" in rendered
    assert "a.py" in rendered
    assert "All tests passed" not in rendered


# ─── tool surface + approval ──────────────────────────────────────

def test_tool_registered_exactly_once():
    names = [f.__name__ for f in ALL_TOOLS]
    assert names.count("run_coding_task") == 1


def test_tool_is_a_side_effect():
    # File writes + commands: same class as click/type/open.
    assert "run_coding_task" in SIDE_EFFECT_TOOLS


def test_first_call_pauses_for_approval(tmp_path, _clean_pending):
    from cat_talker.tools import run_coding_task
    out = run_coding_task("add logging", str(tmp_path))
    assert "PAUSED FOR SAFETY" in out
    assert "confirm" in out.lower()
    assert _clean_pending.PENDING_RISKY_ACTION is not None


def test_confirm_executes_and_reports_truth(tmp_path, monkeypatch,
                                            _clean_pending):
    from cat_talker.tools import confirm_action, run_coding_task
    (tmp_path / "app.py").write_text("# worker made this\n")
    # Explicit unsandboxed mode (see test_confirm_reports_failure_honestly).
    monkeypatch.setenv("CAT_TALKER_CODER_SANDBOX", "none")
    monkeypatch.setattr(
        SubprocessWorkerProvider, "run",
        lambda self, req: WorkerResult(ok=True, summary="added logging",
                                       files_changed=["app.py"],
                                       tests_passed=True,
                                       verification="worker_reported_pass"))
    run_coding_task("add logging", str(tmp_path))
    out = confirm_action()
    assert "verification was not available" in out
    assert "app.py" in out
    assert "NOT independently verified" in out
    assert "All tests passed" not in out


def test_confirm_reports_failure_honestly(tmp_path, monkeypatch,
                                          _clean_pending):
    from cat_talker.tools import confirm_action, run_coding_task
    # Explicit unsandboxed mode: this test stubs SubprocessWorkerProvider
    # .run, which a sandbox wrapper legitimately bypasses (it
    # reimplements spawning around inner argv, by design).
    monkeypatch.setenv("CAT_TALKER_CODER_SANDBOX", "none")
    monkeypatch.setattr(
        SubprocessWorkerProvider, "run",
        lambda self, req: WorkerResult(ok=False, error="tests red"))
    run_coding_task("fix bug", str(tmp_path))
    out = confirm_action()
    assert "FAILED" in out
    assert "tests red" in out
    assert "complet" not in out.lower()


def test_tool_rejects_bad_workspace_without_pausing(tmp_path,
                                                     _clean_pending):
    from cat_talker.tools import run_coding_task
    out = run_coding_task("do it", "")
    assert "no workspace" in out.lower()
    assert _clean_pending.PENDING_RISKY_ACTION is None
    out = run_coding_task("   ", str(tmp_path))
    assert "no coding task" in out.lower()
    assert _clean_pending.PENDING_RISKY_ACTION is None


def test_tool_denies_protected_workspace(tmp_path, monkeypatch,
                                         _clean_pending):
    from cat_talker.tools import run_coding_task
    monkeypatch.setattr(cw, "_protected_roots",
                        lambda: [os.path.realpath(str(tmp_path))])
    out = run_coding_task("do it", str(tmp_path))
    assert "protected" in out.lower()
    assert _clean_pending.PENDING_RISKY_ACTION is None


# ─── routing stays with Chibi ─────────────────────────────────────

def test_guidance_restricts_worker_to_coding():
    text = build_system_instructions()
    assert "run_coding_task" in text
    for topic in ("weather", "web/news", "media", "conversation"):
        assert topic in text
    assert "never claim" in text.lower()
    # The worker result must never be framed as the source of truth:
    # worker claims stay claims until independently verified.
    assert "source of truth" not in text
    assert "never present worker-reported" in text.lower()


def test_run_coding_task_docstring_separates_claims():
    from cat_talker.tools import run_coding_task
    doc = run_coding_task.__doc__ or ""
    assert "source of truth" not in doc
    assert "never present worker-reported" in doc.lower()


def test_unverified_render_marks_worker_prose_unverified():
    # Exact reported failure: worker says "all N tests passed",
    # verification == unknown. Our rendered result must attribute
    # the claim every time and never assert it as verified fact.
    record = cw.WorkerResult(
        ok=True,
        summary="Ran 6 tests, all 6 tests passed.",
        files_changed=["a.py"],
        tests_passed=True,
        verification="unknown",
        workspace="/tmp/ws")
    rendered = record.render()
    assert "verification was not available" in rendered
    assert "UNVERIFIED" in rendered
    assert "NOT independently verified" in rendered
    assert "Tests: independently verified." not in rendered
    assert "independently_verified_pass" not in rendered
    # Attribution must wrap the worker's own success prose.
    marker = rendered.index("UNVERIFIED")
    assert "all 6 tests passed" in rendered[marker:]


def test_not_run_render_says_not_run():
    record = cw.WorkerResult(
        ok=True, summary="did stuff", verification="not_run",
        workspace="/tmp/ws")
    rendered = record.render()
    assert "verification was not run" in rendered.lower() \
        or "verification was not available" in rendered
    assert "Tests: independently verified." not in rendered


def test_provider_boundary_is_replaceable(tmp_path):
    class _FakeProvider(cw.WorkerProvider):
        name = "fake"

        def run(self, request):
            return WorkerResult(ok=True, summary="fake did it")

    out = delegate(build_request(str(tmp_path), "t"), provider=_FakeProvider())
    assert "fake did it" in out


# ─── trust states (claim vs verified fact) ────────────────────────

def test_claim_states_from_envelope(tmp_path):
    ws = str(tmp_path)
    ok_claim = result_from_process(
        0, '{"summary": "s", "tests_passed": true}', "", False, ws)
    assert ok_claim.verification == "worker_reported_pass"
    assert ok_claim.tests_passed is True
    fail_claim = result_from_process(
        0, '{"summary": "s", "tests_passed": false}', "", False, ws)
    assert fail_claim.verification == "worker_reported_fail"
    no_claim = result_from_process(0, "freeform output", "", False, ws)
    assert no_claim.verification == "unknown"
    assert no_claim.tests_passed is None


def test_render_never_says_all_tests_passed_unverified():
    for verification in ("not_run", "worker_reported_pass",
                         "worker_reported_fail", "unknown"):
        result = WorkerResult(ok=True, summary="s", tests_passed=True,
                              verification=verification)
        rendered = result.render()
        assert "All tests passed" not in rendered
        assert "succeeded (independently verified)" not in rendered


def test_render_verified_pass_wording():
    result = WorkerResult(ok=True, summary="s", files_changed=["a.py"],
                          tests_passed=True,
                          verification="independently_verified_pass")
    rendered = result.render()
    assert "independently verified" in rendered


def test_render_verified_fail_wording():
    result = WorkerResult(ok=True, summary="s",
                          error="t red",
                          verification="independently_verified_fail")
    rendered = result.render()
    assert "verification FAILED" in rendered
    assert "succeeded" not in rendered.lower()


# ─── independent verification ─────────────────────────────────────

def test_verify_missing_reported_file_fails(tmp_path):
    req = build_request(str(tmp_path), "t")
    result = WorkerResult(ok=True, summary="s",
                          files_changed=["ghost.py"],
                          tests_passed=True,
                          verification="worker_reported_pass")
    checked = cw.verify_result(req, result)
    assert checked.verification == "independently_verified_fail"
    assert "ghost.py" in checked.error
    assert "verification FAILED" in checked.render()


def test_verify_existing_files_keep_claim(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    req = build_request(str(tmp_path), "t")
    result = WorkerResult(ok=True, summary="s", files_changed=["a.py"],
                          tests_passed=True,
                          verification="worker_reported_pass")
    checked = cw.verify_result(req, result)
    assert checked.verification == "worker_reported_pass"
    assert "verification was not available" in checked.render()


def test_verify_command_pass_and_fail(tmp_path):
    import sys as _sys
    req_ok = build_request(
        str(tmp_path), "t",
        verify_command=[_sys.executable, "-c", "pass"])
    good = cw.verify_result(
        req_ok, WorkerResult(ok=True, summary="s",
                             verification="worker_reported_pass"))
    assert good.verification == "independently_verified_pass"
    assert "independently verified" in good.render()

    req_bad = build_request(
        str(tmp_path), "t",
        verify_command=[_sys.executable, "-c", "import sys; sys.exit(3)"])
    bad = cw.verify_result(
        req_bad, WorkerResult(ok=True, summary="s",
                              verification="worker_reported_pass"))
    assert bad.verification == "independently_verified_fail"
    assert "exit 3" in bad.error


def test_verify_command_rejects_non_argv():
    ws = os.path.realpath("/tmp")
    with pytest.raises(ValueError, match="verify_command"):
        build_request(ws, "t", verify_command="pytest -q")
    with pytest.raises(ValueError, match="verify_command"):
        build_request(ws, "t", verify_command=[])


def test_failed_result_gets_no_local_run(tmp_path, monkeypatch):
    req = build_request(str(tmp_path), "t",
                        verify_command=["definitely-missing-bin-xyz"])
    result = WorkerResult(ok=False, error="worker blew up",
                          verification="worker_reported_fail")
    checked = cw.verify_result(req, result)
    assert checked.verification == "worker_reported_fail"
    assert "worker blew up" in checked.render()


# ─── adversarial: blocked before access, not hidden after ─────────

def test_dotdot_and_absolute_paths_dropped(tmp_path):
    ws = str(tmp_path)
    envelope = (
        '{"summary": "s", "files_changed": '
        '["../outside.txt", "/etc/passwd", "sub/../../escape.py", "ok.py"]}')
    result = result_from_process(0, envelope, "", False, ws)
    assert result.files_changed == ["ok.py"]
    assert "excluded" in result.summary


def test_symlink_escape_dropped(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    link = tmp_path / "ws" / "link.py"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported here")
    envelope = '{"summary": "s", "files_changed": ["link.py"]}'
    result = result_from_process(
        0, envelope, "", False, str(link.parent))
    assert result.files_changed == []
    assert "excluded" in result.summary


def test_sibling_workspace_not_reachable(tmp_path):
    ws_a = tmp_path / "repo-a"
    ws_b = tmp_path / "repo-b"
    ws_a.mkdir()
    (ws_b / "other.py").parent.mkdir(parents=True, exist_ok=True)
    (ws_b / "other.py").write_text("x")
    envelope = '{"summary": "s", "files_changed": ["../repo-b/other.py"]}'
    result = result_from_process(0, envelope, "", False, str(ws_a))
    assert result.files_changed == []
    # …but explicitly selecting the sibling is honest, permitted work:
    req = build_request(str(ws_b), "t")
    assert req.workspace == os.path.realpath(str(ws_b))


def test_delegate_blocks_before_provider_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_protected_roots",
                        lambda: [os.path.realpath(str(tmp_path))])
    calls = []

    class _SpyProvider(cw.WorkerProvider):
        def run(self, request):
            calls.append(1)
            return WorkerResult(ok=True, summary="should never happen")

    out = delegate(WorkerRequest(workspace=str(tmp_path), task="t"),
                   provider=_SpyProvider())
    assert "protected" in out.lower()
    assert calls == [], "provider executed despite blocked workspace"


# ─── real local integration (script CLI, no AI) ───────────────────

def test_real_subprocess_provider_end_to_end(tmp_path):
    """Real Popen/timeout/cwd/containment against a harmless script CLI."""
    script = tmp_path / "fakecli"
    script.write_text("\n".join([
        "#!/usr/bin/env python3",
        "import json, os, sys",
        "task = sys.argv[-1]",
        "ws = os.getcwd()",
        r"open(os.path.join(ws, 'made.txt'), 'w').write('task:' + task + chr(10))",
        "print(json.dumps({'summary': 'made file', "
        "'files_changed': ['made.txt'], 'tests_passed': True}))",
        ""]))
    script.chmod(0o755)
    provider = SubprocessWorkerProvider(binary=str(script), extra_args=())
    req = build_request(str(tmp_path), "create made.txt")
    result = provider.run(req)
    assert result.ok is True
    assert result.verification == "worker_reported_pass"
    made = tmp_path / "made.txt"
    assert made.exists()
    assert made.read_text() == "task:create made.txt\n"
    checked = cw.verify_result(req, result)
    assert checked.verification == "worker_reported_pass"
    stray = [p for p in tmp_path.iterdir()
             if p.name not in ("fakecli", "made.txt")]
    assert stray == [], stray


# ─── structured trustworthy reporting ─────────────────────────────

def test_verified_pass_report_has_all_fields(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    (tmp_path / "foo.py").write_text("x = 1\n")
    req = build_request(ws, "t",
                        verify_command=[sys.executable, "-c",
                                        "print('3 passed')"])
    result = cw.verify_result(
        req, WorkerResult(ok=True, summary="Added foo.",
                          files_changed=["foo.py"],
                          commands=["pytest -q"],
                          tests_passed=True,
                          verification="worker_reported_pass",
                          workspace=ws))
    assert result.verification == "independently_verified_pass"
    assert result.verify_exit == 0
    assert "3 passed" in result.verify_output
    rendered = result.render()
    assert rendered.startswith("Coding task succeeded.")
    assert f"Workspace: {ws}" in rendered
    assert "Verification: independently_verified_pass" in rendered
    assert "Summary: Added foo." in rendered
    assert "* foo.py" in rendered
    assert "Commands run: pytest -q" in rendered
    assert "Verification output (exit 0):" in rendered
    assert "3 passed" in rendered
    assert "Tests: independently verified." in rendered
    assert "NOT independently verified" not in rendered


def test_worker_success_without_verification(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    (tmp_path / "a.py").write_text("x = 1\n")
    req = build_request(ws, "t")  # no verify_command
    result = cw.verify_result(
        req, WorkerResult(ok=True, summary="Did it.",
                          files_changed=["a.py"], tests_passed=True,
                          verification="worker_reported_pass",
                          workspace=ws))
    assert result.verification == "worker_reported_pass"
    assert result.verify_exit is None
    rendered = result.render()
    assert "Worker completed; verification was not available." in rendered
    assert f"Workspace: {ws}" in rendered
    assert "Verification: worker_reported_pass" in rendered
    assert "Worker tests: reported pass (NOT independently verified)." \
        in rendered


def test_claim_fail_wording_stays_claim(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    result = WorkerResult(ok=True, summary="s", tests_passed=False,
                          verification="worker_reported_fail",
                          workspace=ws)
    rendered = result.render()
    assert "Worker tests: reported FAILED." in rendered
    assert "independently verified pass" not in rendered
    assert "All tests passed" not in rendered


def test_partial_files_with_failed_verification(tmp_path):
    """Worker changed files but verification failed: both stay visible,
    success is never claimed."""
    ws = os.path.realpath(str(tmp_path))
    (tmp_path / "foo.py").write_text("x = 1\n")
    req = build_request(ws, "t",
                        verify_command=[sys.executable, "-c",
                                        "import sys; sys.exit(2)"])
    result = cw.verify_result(
        req, WorkerResult(ok=True, summary="Changed foo.",
                          files_changed=["foo.py"], tests_passed=True,
                          verification="worker_reported_pass",
                          workspace=ws))
    assert result.verification == "independently_verified_fail"
    assert result.verify_exit == 2
    rendered = result.render()
    assert "independent verification FAILED" in rendered
    assert "* foo.py" in rendered
    assert "Verification: independently_verified_fail" in rendered
    assert "succeeded" not in rendered.lower()


def test_verify_command_missing_is_unknown(tmp_path, monkeypatch):
    ws = os.path.realpath(str(tmp_path))
    req = build_request(ws, "t",
                        verify_command=["definitely-not-a-real-binary-xyz"])
    result = cw.verify_result(
        req, WorkerResult(ok=True, summary="s", workspace=ws,
                          verification="worker_reported_pass"))
    assert result.verification == "unknown"
    assert "could not run" in result.error
    rendered = result.render()
    assert "verification was not available" in rendered
    assert f"Workspace: {ws}" in rendered


def test_verify_timeout_is_unknown(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    req = build_request(ws, "t", timeout=1,
                        verify_command=[sys.executable, "-c",
                                        "import time; time.sleep(30)"])
    result = cw.verify_result(
        req, WorkerResult(ok=True, summary="s", workspace=ws))
    assert result.verification == "unknown"
    assert "timed out" in result.error
    assert "succeeded" not in result.render().lower()


def test_verify_output_bounded(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    big = "x" * (cw.VERIFY_SUMMARY_MAX_CHARS + 500)
    req = build_request(ws, "t",
                        verify_command=[sys.executable, "-c",
                                        f"print({big!r})"])
    result = cw.verify_result(
        req, WorkerResult(ok=True, summary="s", workspace=ws))
    assert result.verification == "independently_verified_pass"
    assert len(result.verify_output) <= cw.VERIFY_SUMMARY_MAX_CHARS + 100
    assert "truncated" in result.render()


def test_rejected_workspace_never_reported_as_validated(tmp_path):
    out = delegate({"workspace": "/no/such/dir-xyz", "task": "t"})
    assert "Workspace:" not in out
    assert "not a directory" in out


def test_failure_report_has_workspace_and_reason(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    result = WorkerResult(ok=False, error="boom exploded",
                          files_changed=["half.py"], workspace=ws,
                          verification="unknown")
    rendered = result.render()
    assert rendered.startswith("Coding task FAILED.")
    assert f"Workspace: {ws}" in rendered
    assert "Reason: boom exploded" in rendered
    assert "* half.py" in rendered
    assert "Verification: unknown" in rendered
    assert "succeed" not in rendered.lower()


def test_reported_test_failure_without_verification(tmp_path):
    ws = os.path.realpath(str(tmp_path))
    result = result_from_process(
        0, json.dumps({"summary": "tried", "tests_passed": False,
                       "files_changed": []}), "", False, ws)
    assert result.verification == "worker_reported_fail"
    rendered = result.render()
    assert "Worker tests: reported FAILED." in rendered
    assert "verification was not available" in rendered


def test_approval_still_gates_coding_task(tmp_path, _clean_pending,
                                          monkeypatch):
    from cat_talker.tools import run_coding_task
    ws = os.path.realpath(str(tmp_path))
    paused = run_coding_task("add a file", ws)
    assert "PAUSED FOR SAFETY" in paused
    assert "Do you confirm" in paused


# ─── Goose backend (explicit opt-in; OpenCode stays default) ─────────

from cat_talker.coding_worker import (
    BACKEND_GOOSE,
    BACKEND_GOOSE_LITE,
    BACKEND_OPENCODE,
    GOOSE_FLASH_LITE_MODEL,
    GooseWorkerProvider,
    select_backend,
)


def test_opencode_is_default_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("CAT_TALKER_CODER_BACKEND", raising=False)
    provider = select_backend()
    assert isinstance(provider, SubprocessWorkerProvider)
    assert not isinstance(provider, GooseWorkerProvider)
    seen = _mock_popen(monkeypatch)
    result = provider.run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert seen["argv"][0] == "/usr/bin/opencode"


def test_explicit_backend_selection_chooses_goose(monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", "goose")
    provider = select_backend()
    assert isinstance(provider, GooseWorkerProvider)
    monkeypatch.delenv("CAT_TALKER_CODER_BACKEND", raising=False)
    assert isinstance(select_backend(), SubprocessWorkerProvider)


def test_explicit_goose_lite_backend_uses_flash_lite(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", BACKEND_GOOSE_LITE)
    monkeypatch.delenv("CAT_TALKER_GOOSE_MODEL", raising=False)
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    provider = select_backend()
    assert isinstance(provider, GooseWorkerProvider)
    result = provider.run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert (seen["argv"][seen["argv"].index("--model") + 1]
            == GOOSE_FLASH_LITE_MODEL)


def test_goose_lite_backend_honors_explicit_model_override(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", BACKEND_GOOSE_LITE)
    monkeypatch.setenv("CAT_TALKER_GOOSE_MODEL", "gemini-custom-model")
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    provider = select_backend()
    result = provider.run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert (seen["argv"][seen["argv"].index("--model") + 1]
            == "gemini-custom-model")


def test_invalid_backend_fails_honestly(monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", "clippy")
    with pytest.raises(ValueError, match="unknown coder backend"):
        select_backend()


def test_goose_argv_exact_shape(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    provider = GooseWorkerProvider()
    result = provider.run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert seen["argv"] == ["/home/guru/.local/bin/goose", "run",
                            "--no-session", "-q", "--max-turns", "25",
                            "--provider", "google",
                            "--model", "gemini-3.8-flash",
                            "-t", "fix bug"]
    assert "shell" not in seen["kwargs"]
    assert seen["kwargs"]["start_new_session"] is True


def test_goose_argv_carries_constraints(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    GooseWorkerProvider().run(build_request(
        str(tmp_path), "fix bug", constraints="no new deps"))
    assert seen["argv"][-1] == "fix bug\nConstraints: no new deps"


def test_goose_cwd_is_validated_workspace(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    GooseWorkerProvider().run(build_request(str(tmp_path), "fix bug"))
    assert seen["kwargs"]["cwd"] == os.path.realpath(str(tmp_path))


def test_goose_rejects_protected_workspace_before_start(
        tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(cw.shutil, "which",
                        lambda _: "/home/guru/.local/bin/goose")
    real_popen = cw.subprocess.Popen
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: started.append((a, k)))
    protected = os.path.realpath(os.path.expanduser("~/.config/cat-talker"))
    os.makedirs(protected, exist_ok=True)
    with pytest.raises(PermissionError):
        GooseWorkerProvider().run(build_request(protected, "fix bug"))
    assert started == [], "worker process started despite denial"
    assert real_popen is not None


def test_goose_timeout_kills_process_group(tmp_path, monkeypatch):
    _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    killed = []

    class _Hanging(_FakePopen):
        def communicate(self, timeout=5):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _Hanging())
    monkeypatch.setattr(cw.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(cw.os, "getpgid", lambda pid: 777)
    result = GooseWorkerProvider(timeout=5).run(
        build_request(str(tmp_path), "slow", timeout=5))
    assert result.ok is False
    assert "timed out" in result.error
    assert killed, "goose process group was not cleaned up"


def test_goose_stdout_bounded(tmp_path, monkeypatch):
    big = "x" * (cw.OUTPUT_MAX_BYTES + 1000)
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose",
                       popen=_FakePopen(out=big))
    result = GooseWorkerProvider().run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert result.truncated is True
    assert len(result.summary.encode("utf-8")) <= cw.OUTPUT_MAX_BYTES + 64
    assert seen["proc"].timeout_used == 300


def test_goose_nonzero_exit_is_honest_failure(tmp_path, monkeypatch):
    _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose",
                popen=_FakePopen(out="", err="boom", code=2))
    result = GooseWorkerProvider().run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is False
    assert "boom" in result.error


def test_goose_missing_binary_is_honest(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which", lambda _: None)
    result = GooseWorkerProvider().run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is False
    assert "not installed" in result.error
    assert "goose" in result.error


def test_goose_result_verifies_independently(tmp_path, monkeypatch):
    target = os.path.join(str(tmp_path), "fixed.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=["true"])
    _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose",
                popen=_FakePopen(
                    out='{"summary": "fixed", "files_changed": ["fixed.py"],'
                        ' "tests_passed": true}'))
    monkeypatch.setattr(cw.subprocess, "Popen",
                        _recording_popen(monkeypatch))
    result = GooseWorkerProvider().run(req)
    assert result.ok is True
    verified = cw.verify_result(req, result)
    assert verified.verification == "independently_verified_pass"
    assert "independently verified" in verified.render()


def _recording_popen(monkeypatch):
    import subprocess as _sp

    def _fake(argv, **kwargs):
        if argv and argv[0] == "true":
            return _FakePopen(out="ok", code=0)
        return _FakePopen(out="goose did work", code=0)
    return _fake


def test_goose_approval_behavior_unchanged(tmp_path, _clean_pending,
                                          monkeypatch):
    from cat_talker.tools import run_coding_task
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", "goose")
    out = run_coding_task("add logging", str(tmp_path))
    assert "PAUSED FOR SAFETY" in out
    assert _clean_pending.PENDING_RISKY_ACTION is not None


def test_opencode_argv_unchanged_regression(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch)
    result = SubprocessWorkerProvider().run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert seen["argv"] == ["/usr/bin/opencode", "run",
                            "--dir", os.path.realpath(str(tmp_path)),
                            "fix bug"]


# ─── Goose credential handoff (secure store → GOOGLE_API_KEY only) ──

def test_goose_model_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_TALKER_GOOSE_MODEL", "gemini-9.9-ultra")
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    GooseWorkerProvider().run(build_request(str(tmp_path), "fix bug"))
    assert "--model" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == \
        "gemini-9.9-ultra"


def test_goose_default_model_is_validated_flash(tmp_path, monkeypatch):
    monkeypatch.delenv("CAT_TALKER_GOOSE_MODEL", raising=False)
    assert cw.resolve_goose_model() == "gemini-3.8-flash"


def test_goose_flash_lite_default_is_explicit():
    assert cw.GOOSE_FLASH_LITE_MODEL == "gemini-3.5-flash-lite"
    assert (cw.resolve_goose_model(cw.GOOSE_FLASH_LITE_MODEL)
            == "gemini-3.5-flash-lite")


def test_goose_child_env_isolates_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "chibi-real-key-must-not-pass")
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    GooseWorkerProvider().run(build_request(str(tmp_path), "fix bug"))
    child = seen["kwargs"]["env"]
    assert isinstance(child, dict)
    assert child["GOOGLE_API_KEY"] == "fake-test-key"
    assert "GEMINI_API_KEY" not in child
    # Non-secret runtime context is preserved.
    assert child["PATH"] == os.environ["PATH"]


def test_goose_key_never_in_argv(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch, binary="/home/guru/.local/bin/goose")
    GooseWorkerProvider().run(build_request(str(tmp_path), "fix bug"))
    for part in seen["argv"]:
        assert "fake-test-key" not in part
    assert "--provider" in seen["argv"] and "--model" in seen["argv"]


def test_goose_missing_key_fails_before_launch(tmp_path, monkeypatch):
    import cat_talker.config as config_mod
    monkeypatch.setattr(config_mod, "get_api_key", lambda: "")
    started = []
    monkeypatch.setattr(cw.shutil, "which",
                        lambda _: "/home/guru/.local/bin/goose")
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: started.append((a, k)))
    import cat_talker.coding_worker as cw_mod
    result = cw_mod.delegate(
        build_request(str(tmp_path), "fix bug"),
        provider=GooseWorkerProvider())
    assert "Coding task FAILED" in result
    assert "no Gemini API key" in result
    assert started == [], "goose launched without a credential"


def test_opencode_env_passthrough_unchanged(tmp_path, monkeypatch):
    seen = _mock_popen(monkeypatch)
    result = SubprocessWorkerProvider().run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert seen["kwargs"]["env"] is None
    assert "--provider" not in seen["argv"]
    assert "--model" not in seen["argv"]


# ─── bubblewrap sandbox (explicit opt-in) ────────────────────────────

from cat_talker.coding_worker import (
    BubblewrapWorkerProvider,
    maybe_sandbox,
    select_sandbox,
)


def _sandboxed(opencode=True, **kwargs):
    inner = (SubprocessWorkerProvider(**kwargs) if opencode
             else GooseWorkerProvider(**kwargs))
    return BubblewrapWorkerProvider(inner)


# A: sandbox=none keeps exact existing behavior -------------------------
def test_sandbox_none_is_explicit_passthrough(tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_SANDBOX", "none")
    assert select_sandbox() == "none"
    engine = maybe_sandbox(SubprocessWorkerProvider())
    assert type(engine) is SubprocessWorkerProvider
    seen = _mock_popen(monkeypatch)
    result = engine.run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    assert seen["argv"][0] == "/usr/bin/opencode"
    assert "bwrap" not in seen["argv"][0]


def test_sandbox_default_is_bwrap(tmp_path, monkeypatch):
    monkeypatch.delenv("CAT_TALKER_CODER_SANDBOX", raising=False)
    assert select_sandbox() == "bwrap"
    engine = maybe_sandbox(SubprocessWorkerProvider())
    assert isinstance(engine, BubblewrapWorkerProvider)


def test_sandbox_explicit_bwrap_selects_bwrap(monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_SANDBOX", "bwrap")
    assert select_sandbox() == "bwrap"
    engine = maybe_sandbox(SubprocessWorkerProvider())
    assert isinstance(engine, BubblewrapWorkerProvider)


def test_sandbox_invalid_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("CAT_TALKER_CODER_SANDBOX", "docker")
    with pytest.raises(ValueError, match="unknown coder sandbox"):
        maybe_sandbox(SubprocessWorkerProvider())


# B: bwrap argv shape ----------------------------------------------------
def test_bwrap_argv_isolation_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: "/usr/bin/bwrap"
                        if name == "bwrap" else "/usr/bin/opencode")
    captured = {}

    class _NoExecPopen(_FakePopen):
        pass

    orig_popen = cw.subprocess.Popen

    def _capture(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakePopen(out="done")

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    result = _sandboxed().run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    argv = captured["argv"]
    assert argv[0] == "/usr/bin/bwrap"
    assert "--unshare-user" in argv and "--die-with-parent" in argv
    assert "--tmpfs" in argv and "/tmp" in argv
    sep = argv.index("--")
    inner = argv[sep + 1:]
    assert inner[0] == "/usr/bin/opencode"
    assert "--dir" in inner
    # Confined mounts: workspace bound, home is a throwaway dir.
    ws = os.path.realpath(str(tmp_path))
    assert ws in argv
    home_idx = argv.index(ws) + 1  # placeholder check below
    assert captured["kwargs"]["cwd"] == ws
    assert captured["kwargs"]["env"]["HOME"] != os.path.expanduser("~")
    assert "shell" not in captured["kwargs"]
    assert orig_popen is not None


def test_bwrap_no_network_namespace_flag(tmp_path, monkeypatch):
    # Documented policy: network is shared so cloud CLIs keep working.
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: "/usr/bin/bwrap"
                        if name == "bwrap" else "/usr/bin/opencode")
    captured = {}

    def _capture(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _FakePopen(out="done")

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    _sandboxed().run(build_request(str(tmp_path), "fix bug"))
    assert "--unshare-net" not in captured["argv"]
    assert "--unshare-all" not in captured["argv"]


# C: missing bwrap fails closed, provider never runs ----------------------
def test_bwrap_missing_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: None if name == "bwrap"
                        else "/usr/bin/opencode")
    started = []
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: started.append((a, k)))
    result = _sandboxed().run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is False
    assert "bwrap" in result.error
    assert started == [], "worker ran without the requested sandbox"


def test_bwrap_home_cleanup_on_failure(tmp_path, monkeypatch):
    made = []
    real_make_home = cw._make_sandbox_home

    def _track():
        path = real_make_home()
        made.append(path)
        return path

    monkeypatch.setattr(cw, "_make_sandbox_home", _track)
    monkeypatch.setattr(cw.shutil, "which", lambda _: None)
    result = _sandboxed().run(build_request(str(tmp_path), "fix bug"))
    assert result.ok is False
    assert "not installed" in result.error
    # Fail-closed before allocating anything: no home created, none leaked.
    assert made == []
    for path in made:
        assert not os.path.exists(path), f"sandbox home leaked: {path}"


# H: environment isolation -------------------------------------------------
def test_bwrap_env_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-chibi-key-must-not-pass")
    monkeypatch.setenv("DISPLAY", ":9")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-9")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-agent.sock")
    captured = {}
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: "/usr/bin/bwrap"
                        if name == "bwrap" else "/usr/bin/opencode")

    def _capture(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakePopen(out="done")

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    _sandboxed().run(build_request(str(tmp_path), "fix bug"))
    env = captured["env"]
    assert "GEMINI_API_KEY" not in env
    assert "DISPLAY" not in env and "WAYLAND_DISPLAY" not in env
    assert "SSH_AUTH_SOCK" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" not in env
    assert env["HOME"] != os.path.expanduser("~")
    assert env["TMPDIR"] == "/tmp"
    assert env["XDG_CONFIG_HOME"].startswith(env["HOME"])
    assert "PATH" in env


def test_bwrap_goose_credential_only_google_key(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-chibi-key-must-not-pass")
    captured = {}
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: "/usr/bin/bwrap"
                        if name == "bwrap" else "/home/guru/.local/bin/goose")

    def _capture(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakePopen(out="done")

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    result = _sandboxed(opencode=False).run(
        build_request(str(tmp_path), "fix bug"))
    assert result.ok is True
    env = captured["env"]
    assert env["GOOGLE_API_KEY"] == "fake-test-key"
    assert "GEMINI_API_KEY" not in env


# I: timeout kills the wrapped tree -----------------------------------------
def test_bwrap_timeout_kills_tree(tmp_path, monkeypatch):
    _mock_popen(monkeypatch, binary="/usr/bin/opencode")
    killed = []

    class _Hanging(_FakePopen):
        def communicate(self, timeout=5):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _Hanging())
    monkeypatch.setattr(cw.os, "killpg",
                        lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(cw.os, "getpgid", lambda pid: 777)
    result = _sandboxed().run(
        build_request(str(tmp_path), "slow", timeout=5))
    assert result.ok is False
    assert "timed out" in result.error
    assert killed, "sandboxed process group was not cleaned up"


# J: verification uses the same boundary --------------------------------------
def test_verify_through_bwrap_boundary(tmp_path, monkeypatch):
    target = os.path.join(str(tmp_path), "fixed.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    seen = {}

    def _capture(argv, **kwargs):
        seen["argv"] = argv
        return _FakePopen(out="ok", code=0)

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=["true"])
    record = cw.WorkerResult(ok=True, summary="fixed",
                             files_changed=["fixed.py"],
                             tests_passed=True)
    verified = cw.verify_result(req, record, sandbox="bwrap")
    assert verified.verification == "independently_verified_pass"
    assert seen["argv"][0] == "/usr/bin/bwrap"
    assert "--" in seen["argv"]
    assert seen["argv"][-1] == "true"


def test_verify_unknown_sandbox_fails_closed(tmp_path):
    target = os.path.join(str(tmp_path), "fixed.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=["true"])
    record = cw.WorkerResult(ok=True, summary="fixed",
                             files_changed=["fixed.py"])
    verified = cw.verify_result(req, record, sandbox="lxc")
    assert verified.verification == "unknown"
    assert "misconfigured" in verified.error


def test_delegate_routes_verify_through_sandbox(tmp_path, monkeypatch):
    target = os.path.join(str(tmp_path), "fixed.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    calls = []
    real_verify = cw.verify_result

    def _spy(req, result, sandbox=None, **kwargs):
        calls.append(sandbox)
        return real_verify(req, result, sandbox=sandbox, **kwargs)

    monkeypatch.setattr(cw, "verify_result", _spy)
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _FakePopen(out="ok", code=0))
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=["true"])
    out = cw.delegate(
        req, provider=BubblewrapWorkerProvider(
            SubprocessWorkerProvider()))
    assert calls == ["bwrap"]
    assert "independently verified" in out


# K: backends still select with sandbox off --------------------------------------
def test_backends_select_with_sandbox_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CAT_TALKER_CODER_SANDBOX", raising=False)
    monkeypatch.delenv("CAT_TALKER_CODER_BACKEND", raising=False)
    assert isinstance(cw.select_backend(), SubprocessWorkerProvider)
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", "goose")
    assert isinstance(cw.select_backend(), cw.GooseWorkerProvider)
    monkeypatch.setenv("CAT_TALKER_CODER_BACKEND", "goose-lite")
    assert cw.select_backend().goose_model == "gemini-3.5-flash-lite"


# L: guarded live-bwrap probes ---------------------------------------------------
_bwrap_present = __import__("shutil").which("bwrap") is not None
needs_bwrap = pytest.mark.skipif(not _bwrap_present,
                                 reason="bwrap unavailable on this machine")


@needs_bwrap
def test_live_home_inaccessible(tmp_path):
    home = cw._make_sandbox_home()
    try:
        argv, env = cw._bwrap_wrap(
            ["/usr/bin/test", "!", "-e",
             os.path.expanduser("~/.config/cat-talker/config.json")],
            os.path.realpath(str(tmp_path)), home, None)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0, \
            f"host assistant config visible inside sandbox: {out}"
    finally:
        __import__("shutil").rmtree(home, ignore_errors=True)


@needs_bwrap
def test_live_host_tmp_inaccessible_workspace_writable(tmp_path):
    import pathlib
    sentinel = "/tmp/cat-talker-sandbox-probe-sentinel"
    pathlib.Path(sentinel).write_text("host", encoding="utf-8")
    home = cw._make_sandbox_home()
    try:
        argv, env = cw._bwrap_wrap(
            ["/usr/bin/sh", "-c",
             "test ! -e /tmp/cat-talker-sandbox-probe-sentinel && "
             "echo sealed-ok && echo data > ./probe-write.txt && "
             "test \"$(cat ./probe-write.txt)\" = data && echo rw-ok"],
            os.path.realpath(str(tmp_path)), home, None)
        merged = dict(env)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                cwd=os.path.realpath(str(tmp_path)),
                                env=merged)
        out, _ = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"sandbox probe failed: {out}"
        assert "sealed-ok" in out and "rw-ok" in out
        assert (pathlib.Path(str(tmp_path)) / "probe-write.txt"
                ).read_text(encoding="utf-8").strip() == "data"
    finally:
        __import__("shutil").rmtree(home, ignore_errors=True)
        try:
            os.unlink(sentinel)
        except OSError:
            pass
        try:
            os.unlink(os.path.join(str(tmp_path), "probe-write.txt"))
        except OSError:
            pass


@needs_bwrap
def test_live_workspace_file_roundtrip_via_provider(tmp_path, monkeypatch):
    seen = {}
    real_which = cw.shutil.which
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: ("/usr/bin/bwrap" if name == "bwrap"
                                      else real_which(name)))

    class _CatProvider(SubprocessWorkerProvider):
        def _build_argv(self, resolved, req):
            return ["/usr/bin/cat", "hello.txt"]

    with open(os.path.join(str(tmp_path), "hello.txt"), "w",
              encoding="utf-8") as f:
        f.write("sandbox-hello\n")
    result = BubblewrapWorkerProvider(_CatProvider()).run(
        build_request(str(tmp_path), "read the file"))
    assert result.ok is True
    assert "sandbox-hello" in result.summary


def test_verify_absolute_outside_binary_fails_without_binding(
        tmp_path, monkeypatch):
    import pathlib
    outside = os.path.join(str(tmp_path), "..",
                           "rt-host-only-verify-test")
    outside = os.path.realpath(outside)
    os.makedirs(outside, exist_ok=True)
    script = os.path.join(outside, "probe.sh")
    pathlib.Path(script).write_text("#!/bin/sh\necho HOST-ESCAPE\n",
                                    encoding="utf-8")
    seen = {}

    def _capture(argv, **kwargs):
        seen["argv"] = argv
        return _FakePopen(out="should-not-run", code=0)

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=[script])
    record = cw.WorkerResult(ok=True, summary="fixed",
                             files_changed=[])
    home = cw._make_sandbox_home()
    try:
        argv, _ = cw._bwrap_wrap([script],
                                 os.path.realpath(str(tmp_path)),
                                 home, None, bind_binary_dir=False)
        assert outside not in argv, \
            "verify binding punches outside dir into sandbox"
    finally:
        __import__("shutil").rmtree(home, ignore_errors=True)


def test_worker_argv_still_binds_own_binary_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: "/usr/bin/bwrap"
                        if name == "bwrap" else "/home/guru/.local/bin/goose")
    captured = {}

    def _capture(argv, **kwargs):
        captured["argv"] = argv
        return _FakePopen(out="done")

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    BubblewrapWorkerProvider(GooseWorkerProvider()).run(
        build_request(str(tmp_path), "fix bug"))
    argv = captured["argv"]
    assert "--ro-bind" in argv
    idx = argv.index("--ro-bind")
    pairs = [(argv[i + 1], argv[i + 2])
             for i, a in enumerate(argv[:-2])
             if a == "--ro-bind"]
    assert ("/home/guru/.local/bin", "/home/guru/.local/bin") in pairs


# ─── run_coding_task verify_command contract ─────────────────────────

def test_tool_schema_exposes_verify_command():
    import inspect
    from cat_talker.tools import (run_coding_task,
                                  run_coding_task_with_verification)
    # Ordinary tool: no verification slot; verification tool owns it.
    ordinary = inspect.signature(run_coding_task).parameters
    assert "verify_command" not in ordinary
    params = inspect.signature(
        run_coding_task_with_verification).parameters
    assert "verify_command" in params
    assert params["verify_command"].default is inspect.Parameter.empty
    assert params["verify_command"].annotation == list[str]
    doc = run_coding_task_with_verification.__doc__ or ""
    assert "verify_command" in doc
    assert "never invent" in doc.lower()
    plain_doc = run_coding_task.__doc__ or ""
    assert "run_coding_task_with_verification" in plain_doc


def test_explicit_verify_command_reaches_request(tmp_path, monkeypatch):
    seen = {}
    real_build = cw.build_request

    def _spy(workspace, task, **kwargs):
        seen["verify_command"] = kwargs.get("verify_command")
        return real_build(workspace, task, **kwargs)

    monkeypatch.setattr(cw, "build_request", _spy)
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _FakePopen(out="done"))
    req = cw.build_request(str(tmp_path), "fix bug",
                           verify_command=["python3", "test_calc.py"])
    assert req.verify_command == ("python3", "test_calc.py")
    out = cw.delegate(req)
    assert isinstance(out, str)


def test_verification_tool_passes_exact_command_unchanged(tmp_path,
                                                          monkeypatch):
    import cat_talker.tools as tools_mod
    from cat_talker.tools import (confirm_action,
                                  run_coding_task_with_verification)
    seen = {}
    real_build = cw.build_request

    def _spy(workspace, task, **kwargs):
        seen["verify_command"] = kwargs.get("verify_command")
        return real_build(workspace, task, **kwargs)

    monkeypatch.setattr(cw, "build_request", _spy)
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _FakePopen(out="done"))
    saved = tools_mod.PENDING_RISKY_ACTION
    tools_mod.PENDING_RISKY_ACTION = None
    try:
        paused = run_coding_task_with_verification(
            "fix bug", ["python3", "test_calc.py"], str(tmp_path))
        assert "PAUSED FOR SAFETY" in paused
        out = confirm_action()
        assert isinstance(out, str)
        assert seen["verify_command"] == ["python3", "test_calc.py"]
    finally:
        tools_mod.PENDING_RISKY_ACTION = saved


def test_verification_tool_rejects_empty_and_blank(tmp_path, monkeypatch):
    import cat_talker.tools as tools_mod
    from cat_talker.tools import (confirm_action,
                                  run_coding_task_with_verification)
    saved = tools_mod.PENDING_RISKY_ACTION
    try:
        for bad in ([], ["python3", "  "], "pytest -q"):
            tools_mod.PENDING_RISKY_ACTION = None
            paused = run_coding_task_with_verification(
                "fix bug", bad, str(tmp_path))  # type: ignore[arg-type]
            assert "PAUSED FOR SAFETY" in paused
            out = confirm_action()
            assert "FAILED" in out
            assert "verify_command" in out
    finally:
        tools_mod.PENDING_RISKY_ACTION = saved


def test_verify_command_accepted_form(tmp_path):
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=["python3", "test_calc.py"])
    assert req.verify_command == ("python3", "test_calc.py")


def test_verify_command_empty_argv_rejected(tmp_path):
    with pytest.raises(ValueError, match="verify_command"):
        build_request(str(tmp_path), "fix bug", verify_command=[])


def test_verify_command_empty_elements_rejected(tmp_path):
    with pytest.raises(ValueError, match="verify_command"):
        build_request(str(tmp_path), "fix bug",
                      verify_command=["python3", "  "])


def test_no_verify_command_preserves_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _FakePopen(
                            out="Ran 6 tests, all passed", code=0))
    req = build_request(str(tmp_path), "fix bug")
    assert req.verify_command is None
    result = SubprocessWorkerProvider().run(req)
    assert result.ok is True
    verified = cw.verify_result(req, result)
    assert verified.verification == "unknown"
    assert "NOT independently verified" in verified.render()


def test_guidance_says_ask_rather_than_infer():
    text = build_system_instructions()
    assert "ask which exact command to run" in text
    assert "never invent, infer, guess" in text.lower()


def test_worker_tests_never_promoted_without_command(tmp_path, monkeypatch):
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cw.subprocess, "Popen",
                        lambda *a, **k: _FakePopen(
                            out="Ran 6 tests, all 6 tests passed", code=0))
    from cat_talker.tools import confirm_action, run_coding_task
    import cat_talker.tools as tools_mod
    saved = tools_mod.PENDING_RISKY_ACTION
    tools_mod.PENDING_RISKY_ACTION = None
    try:
        paused = run_coding_task("fix bug", str(tmp_path))
        assert "PAUSED FOR SAFETY" in paused
        out = confirm_action()
        assert "verification was not available" in out
        assert "Tests: independently verified." not in out
        assert "UNVERIFIED" in out
    finally:
        tools_mod.PENDING_RISKY_ACTION = saved


def test_sandboxed_verify_uses_bwrap_boundary_with_command(tmp_path,
                                                           monkeypatch):
    target = os.path.join(str(tmp_path), "a.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    seen = {}
    real_which = cw.shutil.which

    def _capture(argv, **kwargs):
        seen["argv"] = argv
        return _FakePopen(out="ok", code=0)

    monkeypatch.setattr(cw.subprocess, "Popen", _capture)
    req = build_request(str(tmp_path), "fix bug",
                        verify_command=["true"])
    record = cw.WorkerResult(ok=True, summary="fixed",
                             files_changed=["a.py"])
    verified = cw.verify_result(req, record, sandbox="bwrap")
    assert verified.verification == "independently_verified_pass"
    assert seen["argv"][0] == real_which("bwrap")
    assert "--" in seen["argv"]
    assert seen["argv"][-1] == "true"


# ─── Live SDK schema compatibility (regression) ─────────────────────
# The untyped verify_command=None default once broke Gemini Live
# session startup (Pydantic IsInstanceSchema over inspect._empty).
# These tests run the installed SDK's own declaration parser over
# the real model-facing functions.

def test_live_schema_generation_for_run_coding_task():
    afc = pytest.importorskip(
        "google.genai._automatic_function_calling_util")
    from cat_talker.tools import (run_coding_task,
                                  run_coding_task_with_verification)
    plain = afc.parse_function_declaration_json_schema(
        run_coding_task, None)
    plain_props = (plain.parameters_json_schema or {}).get(
        "properties", {})
    assert "verify_command" not in plain_props
    decl = afc.parse_function_declaration_json_schema(
        run_coding_task_with_verification, None)
    schema = decl.parameters_json_schema or {}
    props = schema.get("properties", {})
    assert "verify_command" in props
    text = json.dumps(props["verify_command"])
    assert '"array"' in text and '"string"' in text
    assert "null" not in text
    required = schema.get("required") or []
    assert "task" in required
    assert "verify_command" in required


def test_live_schema_generation_for_all_tools():
    afc = pytest.importorskip(
        "google.genai._automatic_function_calling_util")
    from cat_talker.tools import ALL_TOOLS
    for func in ALL_TOOLS:
        afc.parse_function_declaration_json_schema(func, None)


def test_verify_command_trigger_wording_in_model_facing_description():
    afc = pytest.importorskip(
        "google.genai._automatic_function_calling_util")
    from cat_talker.tools import (run_coding_task,
                                  run_coding_task_with_verification)
    decl = afc.parse_function_declaration_json_schema(
        run_coding_task_with_verification, None)
    # The model-facing artifact is the declaration description, built
    # from the real docstring: the required slot must carry the
    # explicit-request rule with a concrete example.
    text = " ".join((decl.description or "").split())
    assert "MUST come from the user's explicit request" in text
    assert '["python3", "test_calc.py"]' in text
    assert "ask which exact command to run" in text
    # Schema shape: required string array, not nullable.
    schema = decl.parameters_json_schema or {}
    props = schema.get("properties", {})
    assert "verify_command" in props
    assert "verify_command" in (schema.get("required") or [])
    # Ordinary tool points away from verification requests.
    plain = afc.parse_function_declaration_json_schema(
        run_coding_task, None)
    plain_text = " ".join((plain.description or "").split())
    assert "run_coding_task_with_verification" in plain_text


def test_guidance_reinforces_verify_command_placement():
    text = build_system_instructions()
    assert "never fold it into the task prose" in text
    assert "run_coding_task_with_verification" in text
    assert "ask which exact command to run" in text


# ─── Runtime backend/sandbox audit logging ──────────────────────────
# Logs (never config) prove which backend and boundary really ran.

def _start_records(caplog):
    return [r for r in caplog.records
            if "coding task start" in r.getMessage()]


def test_audit_default_backend_is_opencode(monkeypatch):
    from cat_talker.coding_worker import (maybe_sandbox, select_backend,
                                          _backend_label,
                                          _execution_boundary)
    monkeypatch.delenv("CAT_TALKER_CODER_BACKEND", raising=False)
    monkeypatch.delenv("CAT_TALKER_CODER_SANDBOX", raising=False)
    engine = maybe_sandbox(select_backend())
    assert _backend_label(engine) == "opencode"
    assert _execution_boundary(engine) == "bwrap"


def test_audit_explicit_goose_stays_opt_in():
    from cat_talker.coding_worker import (select_backend, _backend_label)
    assert _backend_label(select_backend("goose")).startswith("goose:")
    lite = _backend_label(select_backend("goose-lite"))
    assert lite.startswith("goose-lite:")


def test_audit_bwrap_launch_logged_for_verified_run(tmp_path, monkeypatch,
                                                   caplog):
    import logging
    from cat_talker.coding_worker import (SubprocessWorkerProvider,
                                          delegate)
    monkeypatch.delenv("CAT_TALKER_CODER_SANDBOX", raising=False)
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        out = delegate({"workspace": str(tmp_path), "task": "noop",
                        "verify_command": ["true"]},
                       provider=SubprocessWorkerProvider(binary="true"))
    assert "Backend: custom-cli:true" in out
    assert "Sandbox: bwrap" in out
    assert "Verification: independently_verified_pass" in out
    messages = [r.getMessage() for r in caplog.records]
    assert any("coding task start" in m and "backend=custom-cli:true" in m
               and "sandbox=bwrap" in m and "verify_requested=True" in m
               for m in messages)
    assert any("coding worker launch" in m and "sandbox=bwrap" in m
               for m in messages)
    assert any("coding verification" in m and "sandbox=bwrap" in m
               and "outcome=independently_verified_pass" in m
               and "exit=0" in m for m in messages)


def test_audit_explicit_none_reports_unsandboxed(tmp_path, monkeypatch,
                                                caplog):
    import logging
    from cat_talker.coding_worker import (SubprocessWorkerProvider,
                                          delegate)
    monkeypatch.setenv("CAT_TALKER_CODER_SANDBOX", "none")
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        out = delegate({"workspace": str(tmp_path), "task": "noop"},
                       provider=SubprocessWorkerProvider(binary="true"))
    assert "Sandbox: none" in out
    assert "Verification: unknown" in out
    messages = [r.getMessage() for r in caplog.records]
    assert any("sandbox=none" in m and "direct unsandboxed" in m
               for m in messages)
    assert not any("sandbox=bwrap" in m for m in messages)


def test_audit_verifier_boundary_independent_of_worker(tmp_path,
                                                      monkeypatch, caplog):
    import logging
    import cat_talker.coding_worker as cwmod
    req = build_request(str(tmp_path), "noop")
    record = cwmod.WorkerResult(ok=True, summary="done")
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        verified = cwmod.verify_result(req, record, sandbox="bwrap")
    assert verified.verification == "not_run"  # no command configured
    req2 = build_request(str(tmp_path), "noop",
                         verify_command=["true"])
    record2 = cwmod.WorkerResult(ok=True, summary="done")
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        verified2 = cwmod.verify_result(req2, record2, sandbox=None)
    assert verified2.verification == "independently_verified_pass"
    worker_msgs = [r.getMessage() for r in caplog.records
                   if "coding verification" in r.getMessage()]
    assert any("sandbox=none" in m and "exit=0" in m
               for m in worker_msgs)


def test_audit_failed_verification_logs_nonzero_exit(tmp_path, caplog):
    import logging
    import cat_talker.coding_worker as cwmod
    req = build_request(str(tmp_path), "noop",
                        verify_command=["false"])
    record = cwmod.WorkerResult(ok=True, summary="worker claims pass",
                                tests_passed=True)
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        verified = cwmod.verify_result(req, record, sandbox=None)
    assert verified.verification == "independently_verified_fail"
    assert "independently verified." not in verified.render()
    assert any("outcome=independently_verified_fail" in r.getMessage()
               and "exit=1" in r.getMessage() for r in caplog.records)


def test_audit_failed_sandbox_setup_never_reports_sandboxed(
        tmp_path, monkeypatch, caplog):
    import logging
    import cat_talker.coding_worker as cwmod
    inner = cwmod.SubprocessWorkerProvider(binary="true")
    engine = cwmod.BubblewrapWorkerProvider(inner)

    def _boom(argv, workspace, home, base_env, bind_binary_dir=True):
        raise ValueError("Coding error: sandbox 'bwrap' requested but "
                         "the 'bwrap' executable was not found on PATH.")

    monkeypatch.setattr(cwmod, "_bwrap_wrap", _boom)
    req = build_request(str(tmp_path), "noop")
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        result = engine.run(req)
    assert result.ok is False
    assert "bwrap" in result.error
    messages = [r.getMessage() for r in caplog.records]
    assert any("sandbox setup failed" in m for m in messages)
    assert not any("sandbox=bwrap workspace" in m
                   and "launch" in m for m in messages)


# ─── Conservative Python auto-verification ──────────────────────────
# Ordinary tasks may run a fixed pytest argv after a successful
# worker result. Explicit commands always win; nothing is invented.

def _make_marker_ws(path, *names):
    for name in names:
        full = os.path.join(str(path), name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write("# marker\n")
    return str(path)


def _auto_popen(monkeypatch, seen, probe_ok=True, pytest_code=0,
                venv_probe_ok=None):
    """Fake Popen dispatcher: worker, probe, and pytest runs."""
    def _fake(argv, **kwargs):
        seen.append(list(argv))
        if "-c" in argv:
            ok = (venv_probe_ok if ".venv" in str(argv[0])
                  else probe_ok)
            # Distinctive isolated-probe verdicts (10 found / 11
            # missing); anything else means "not established".
            return _FakePopen(out="", code=10 if ok else 11)
        if "-m" in argv:
            return _FakePopen(out="1 passed", code=pytest_code)
        return _FakePopen(out="done", code=0)
    monkeypatch.setattr(cw.subprocess, "Popen", _fake)
    monkeypatch.setattr(cw.shutil, "which",
                        lambda name: f"/usr/bin/{name}")


def test_detect_strong_markers_and_test_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "_interpreter_has_pytest",
                        lambda *a: True)
    cases = (("pytest.ini",), ("conftest.py",),
             ("test_calc.py",), ("calc_test.py",),
             ("tests/test_calc.py",), ("test/test_calc.py",))
    for i, names in enumerate(cases):
        ws = tmp_path / ("ws%d" % i)
        ws.mkdir()
        _make_marker_ws(ws, *names)
        argv, status, detail = cw.detect_python_checks(str(ws))
        assert status == "ready", names
        assert argv[-3:] == ["-m", "pytest", "-q"], names
        assert "pytest -q" in detail, names


def test_detect_ignores_generic_config_alone(tmp_path):
    ws = tmp_path / "generic"
    ws.mkdir()
    _make_marker_ws(ws, "pyproject.toml", "setup.cfg", "tox.ini",
                    "tests/helpers.py", "README.md")
    argv, status, detail = cw.detect_python_checks(str(ws))
    assert argv is None
    assert status == "absent"
    assert "no recognized" in detail
    argv2, status2, _ = cw.detect_python_checks(
        str(tmp_path / "missing-dir-xyz"))
    assert argv2 is None and status2 == "absent"


def test_detect_prefers_venv_python_over_python3(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    bindir = ws / ".venv" / "bin"
    bindir.mkdir(parents=True)
    venv_python = bindir / "python"
    venv_python.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(venv_python), 0o755)
    seen = []
    _auto_popen(monkeypatch, seen, venv_probe_ok=True)
    argv, status, detail = cw.detect_python_checks(str(ws))
    assert status == "ready"
    assert argv[0] == str(venv_python)
    assert detail == ".venv/bin/python -m pytest -q"
    # Venv without pytest falls back to python3 when it has it.
    _auto_popen(monkeypatch, seen, probe_ok=True, venv_probe_ok=False)
    argv2, status2, detail2 = cw.detect_python_checks(str(ws))
    assert status2 == "ready"
    assert argv2[0] == "/usr/bin/python3"
    assert detail2 == "python3 -m pytest -q"


def test_detect_unavailable_runner_is_not_code_failure(
        tmp_path, monkeypatch, caplog):
    import logging
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "pytest.ini", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen, probe_ok=False, venv_probe_ok=False)
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        out = delegate({"workspace": str(ws), "task": "fix bug",
                        "auto_verify": True},
                       provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: verification_unavailable" in out
    assert "pytest" in out
    assert "independently verified." not in out
    assert "auto-detected project check" not in out
    assert not any("-m" in a for a in seen)
    assert any("unavailable" in r.getMessage() for r in caplog.records)


def test_auto_check_pass_and_fail(tmp_path, monkeypatch):
    for code, state in ((0, "independently_verified_pass"),
                        (2, "independently_verified_fail")):
        ws = tmp_path / ("run%d" % code)
        ws.mkdir()
        _make_marker_ws(ws, "tests/test_a.py")
        seen = []
        _auto_popen(monkeypatch, seen, pytest_code=code)
        out = delegate({"workspace": str(ws), "task": "fix bug",
                        "auto_verify": True},
                       provider=SubprocessWorkerProvider(binary="true"))
        assert f"Verification: {state}" in out, code
        assert "auto-detected project check" in out, code
        assert f"exit {code}" in out, code
        pytest_runs = [a for a in seen if "-m" in a]
        assert len(pytest_runs) == 1, code
        assert pytest_runs[0][-3:] == ["-m", "pytest", "-q"], code


def test_auto_check_absent_without_markers(tmp_path, monkeypatch):
    ws = tmp_path / "plain"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello\n")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": "fix bug",
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: unknown" in out
    assert "auto-detected project check" not in out
    assert not any("-m" in a for a in seen)


def test_explicit_command_skips_detection(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "pytest.ini", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    calls = []
    real_detect = cw.detect_python_checks
    monkeypatch.setattr(cw, "detect_python_checks",
                        lambda ws_: (calls.append(ws_), (None, "absent",
                                                         "spy"))[1])
    out = delegate({"workspace": str(ws), "task": "fix bug",
                    "verify_command": ["true"], "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert calls == []
    assert "Verification: independently_verified_pass" in out
    assert "auto-detected project check" not in out
    assert not any("-m" in a for a in seen)
    assert real_detect is not None


def test_guidance_keeps_ask_first_and_limits_auto_checks():
    text = build_system_instructions()
    assert "ask which exact command to run" in text
    assert "routine auto-detected Python check" in text
    assert ("never satisfies an explicit request for independent "
            "verification" in text)
    assert "instruction not to run tests disables it" in text


def test_no_test_instruction_disables_auto_checks(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate(
        {"workspace": str(ws),
         "task": "fix bug but do not run tests", "auto_verify": True},
        provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: unknown" in out
    assert "auto-detected project check" not in out
    assert not any("-m" in a for a in seen)


def test_auto_check_runs_inside_bwrap_boundary(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    monkeypatch.delenv("CAT_TALKER_CODER_SANDBOX", raising=False)
    out = delegate({"workspace": str(ws), "task": "fix bug",
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: independently_verified_pass" in out
    pytest_runs = [a for a in seen if "-m" in a]
    assert len(pytest_runs) == 1
    assert pytest_runs[0][0] == "/usr/bin/bwrap"
    assert "--" in pytest_runs[0]


def test_ordinary_tool_approval_mentions_routine_checks(
        tmp_path, _clean_pending):
    from cat_talker.tools import confirm_action, run_coding_task
    tools_mod = _clean_pending
    paused = run_coding_task("fix bug", str(tmp_path))
    assert "PAUSED FOR SAFETY" in paused
    assert "routine auto-detected project checks may also run" in paused
    pending = tools_mod.PENDING_RISKY_ACTION
    assert pending["args"].get("auto_verify") is True
    tools_mod.PENDING_RISKY_ACTION = None


def test_no_auto_check_after_failed_worker(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "pytest.ini", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)

    def _failing(argv, **kwargs):
        seen.append(list(argv))
        if "-c" in argv:
            return _FakePopen(out="", code=0)
        return _FakePopen(out="boom", code=1)
    monkeypatch.setattr(cw.subprocess, "Popen", _failing)
    out = delegate({"workspace": str(ws), "task": "fix bug",
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "independently_verified_pass" not in out
    assert "auto-detected project check" not in out
    assert not any("-m" in a for a in seen)


# ─── Review blockers: sandboxed probes, ask-first guard, flag ────

def test_probe_uses_configured_boundary(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    argv, status, _ = cw.detect_python_checks(str(ws), sandbox="bwrap")
    assert status == "ready"
    probes = [a for a in seen if "-c" in a]
    assert len(probes) == 1
    probe = probes[0]
    assert probe[0] == "/usr/bin/bwrap"
    assert "-I" in probe
    code = probe[probe.index("-c") + 1]
    assert "find_spec" in code
    assert "import pytest" not in code


def test_workspace_interpreter_never_runs_outside_boundary(
        tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "tests/test_a.py")
    bindir = ws / ".venv" / "bin"
    bindir.mkdir(parents=True)
    venv_python = bindir / "python"
    venv_python.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(str(venv_python), 0o755)
    seen = []
    _auto_popen(monkeypatch, seen, venv_probe_ok=True)
    argv, status, _ = cw.detect_python_checks(str(ws), sandbox="bwrap")
    assert status == "ready"
    assert argv[0] == str(venv_python)
    probes = [a for a in seen if "-c" in a]
    assert len(probes) == 1
    # Workspace binary only ever appears after the sandbox separator,
    # with the same restrictions as verification (no --ro-bind of the
    # interpreter's own directory).
    assert probes[0][0] == "/usr/bin/bwrap"
    assert "--" in probes[0]
    assert probes[0].index(str(venv_python)) > probes[0].index("--")
    sep = probes[0].index("--")
    binds = probes[0][:sep]
    assert str(bindir) not in binds


def _bwrap_executes(workspace: str) -> bool:
    """Prove the sandbox can really execute (not just be installed)."""
    import shutil as _shutil
    if _shutil.which("bwrap") is None:
        return False
    true_bin = _shutil.which("true")
    if not true_bin:
        return False
    argv, env, cleanup, error = cw._prepare_exec(
        [true_bin], workspace, "bwrap", bind_binary_dir=False)
    if error is not None:
        return False
    try:
        returncode, _, _ = cw._run_bounded(argv, workspace, env, 30)
    except Exception:
        return False
    finally:
        if cleanup is not None:
            try:
                cleanup()
            except Exception:
                pass
    return returncode == 0


def test_evil_workspace_pytest_never_imported(tmp_path):
    import shutil as _shutil
    if _shutil.which("python3") is None:
        pytest.skip("no system python3 for real-boundary probe")
    ws = tmp_path / "evil"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py")
    if not _bwrap_executes(str(ws)):
        pytest.skip("bubblewrap sandbox execution unavailable; "
                    "a failed setup must not pass as isolation evidence")
    marker = ws / "PWNED"
    (ws / "pytest.py").write_text(
        "import os\n"
        f"os.system('touch {marker}')\n")
    # Sandboxed probe: real boundary, workspace code must not run.
    # Ready (system-wide pytest) and established-unavailable (both
    # probes returned the missing verdict) are both valid; anything
    # else means execution was not proven and must fail loudly.
    argv, status, detail = cw.detect_python_checks(str(ws),
                                                   sandbox="bwrap")
    assert not marker.exists()
    assert status in ("ready", "unavailable"), detail
    if status == "ready":
        assert argv[-3:] == ["-m", "pytest", "-q"]
        assert "pytest.py" not in " ".join(argv)
    else:
        assert "available in neither" in detail, detail
    # Isolated probe without sandbox: -I still excludes the workspace.
    argv2, status2, detail2 = cw.detect_python_checks(str(ws),
                                                      sandbox=None)
    assert not marker.exists()
    assert status2 in ("ready", "unavailable"), detail2
    if status2 == "unavailable":
        assert "available in neither" in detail2, detail2


def test_outside_workspace_interpreter_never_declared_ready(
        tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py")
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    ran = bindir / "RAN"
    fake = bindir / "python3"
    fake.write_text("#!/bin/sh\n"
                    f"touch {ran}\n"
                    "exit 0\n")
    os.chmod(str(fake), 0o755)
    if not _bwrap_executes(str(ws)):
        pytest.skip("bubblewrap sandbox execution unavailable")
    import shutil as _shutil
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    assert _shutil.which("python3") == str(fake)
    argv, status, detail = cw.detect_python_checks(str(ws),
                                                   sandbox="bwrap")
    # The fake exits 0, yet it must never read as pytest found: it is
    # invisible under verification's filesystem restrictions, and it
    # must never have executed (here or anywhere outside them).
    assert status == "unavailable", (status, detail)
    assert "could not be established" in detail, detail
    assert not ran.exists()


def test_probe_setup_failure_never_falls_back(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "pytest.ini")
    seen = []
    _auto_popen(monkeypatch, seen)

    def _boom(*a, **k):
        raise ValueError("Coding error: sandbox 'bwrap' requested but "
                         "the 'bwrap' executable was not found on PATH.")
    monkeypatch.setattr(cw, "_bwrap_wrap", _boom)
    argv, status, detail = cw.detect_python_checks(str(ws),
                                                   sandbox="bwrap")
    assert argv is None
    assert status == "unavailable"
    assert seen == []


def test_ask_first_guard_blocks_worker_without_command(
        tmp_path, monkeypatch, _clean_pending):
    from cat_talker.tools import run_coding_task
    seen = []
    _auto_popen(monkeypatch, seen)
    tools_mod = _clean_pending
    out = run_coding_task(
        "fix the login bug and verify it independently", str(tmp_path))
    assert "exact verification command" in out
    assert "Do not start the coding task yet" in out
    assert tools_mod.PENDING_RISKY_ACTION is None
    assert seen == []


def test_ask_first_guard_precise_phrasing(tmp_path, monkeypatch,
                                          _clean_pending):
    from cat_talker.tools import run_coding_task
    seen = []
    _auto_popen(monkeypatch, seen)
    tools_mod = _clean_pending
    # Explicit independent-verification demands trigger the guard.
    for task in ("add square() with independent verification",
                 "fix it; I want you to independently verify the fix"):
        tools_mod.PENDING_RISKY_ACTION = None
        out = run_coding_task(task, str(tmp_path))
        assert "exact verification command" in out, task
        assert tools_mod.PENDING_RISKY_ACTION is None, task
    # Routine wording stays an ordinary task (approval pause, no ask).
    for task in ("run the tests and repair failures",
                 "add regression tests for square()",
                 "verify the build passes",
                 "check whether the tests pass"):
        tools_mod.PENDING_RISKY_ACTION = None
        out = run_coding_task(task, str(tmp_path))
        assert "PAUSED FOR SAFETY" in out, task
    tools_mod.PENDING_RISKY_ACTION = None


def test_ask_first_guard_bypassed_by_explicit_command(
        tmp_path, monkeypatch, _clean_pending):
    from cat_talker.tools import run_coding_task_with_verification
    seen = []
    _auto_popen(monkeypatch, seen)
    tools_mod = _clean_pending
    out = run_coding_task_with_verification(
        "fix it and verify it independently", ["true"], str(tmp_path))
    assert "PAUSED FOR SAFETY" in out
    pending = tools_mod.PENDING_RISKY_ACTION
    assert pending["args"].get("verify_command") == ["true"]
    tools_mod.PENDING_RISKY_ACTION = None


def test_auto_verify_rejects_non_boolean(tmp_path):
    with pytest.raises(ValueError, match="auto_verify"):
        build_request(str(tmp_path), "fix bug",
                      auto_verify="false")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="auto_verify"):
        build_request(str(tmp_path), "fix bug",
                      auto_verify=1)  # type: ignore[arg-type]
    assert build_request(str(tmp_path), "fix bug",
                         auto_verify=True).auto_verify is True
    assert build_request(str(tmp_path), "fix bug").auto_verify is False


# ─── Final review: negation, tri-state probes, portability ──────

def test_negated_verification_requests_skip_guard(tmp_path, monkeypatch,
                                                 _clean_pending):
    from cat_talker.tools import run_coding_task
    seen = []
    _auto_popen(monkeypatch, seen)
    tools_mod = _clean_pending
    for task in ("fix the bug; no independent verification is needed",
                 "do not perform independent verification",
                 "fix it without independent verification, just do it"):
        tools_mod.PENDING_RISKY_ACTION = None
        out = run_coding_task(task, str(tmp_path))
        assert "PAUSED FOR SAFETY" in out, task
        assert "exact verification command" not in out, task
    tools_mod.PENDING_RISKY_ACTION = None


def test_probe_indeterminate_reports_unavailable_honestly(
        tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "pytest.ini")
    seen = []
    _auto_popen(monkeypatch, seen)
    # Probe can establish nothing (e.g. timeout): None, not False.
    monkeypatch.setattr(cw, "_interpreter_has_pytest", lambda *a: None)
    argv, status, detail = cw.detect_python_checks(str(ws),
                                                   sandbox="bwrap")
    assert argv is None
    assert status == "unavailable"
    assert "could not be established" in detail
    assert "not assuming pytest is absent" in detail
    # Confirmed absence keeps the distinct, stronger message.
    monkeypatch.setattr(cw, "_interpreter_has_pytest", lambda *a: False)
    argv2, status2, detail2 = cw.detect_python_checks(str(ws),
                                                      sandbox="bwrap")
    assert status2 == "unavailable"
    assert "available in neither" in detail2
    assert "could not be established" not in detail2


def test_indeterminate_probe_never_claims_absence(tmp_path, monkeypatch,
                                                 caplog):
    import logging
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    monkeypatch.setattr(cw, "_interpreter_has_pytest", lambda *a: None)
    with caplog.at_level(logging.INFO,
                         logger="cat_talker.coding_worker"):
        out = delegate({"workspace": str(ws), "task": "fix bug",
                        "auto_verify": True},
                       provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: verification_unavailable" in out
    assert "could not be established" in out
    assert "independently verified." not in out
    assert not any("-m" in a for a in seen)


def test_confirmed_absent_probe_message_preserved(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py")
    seen = []
    _auto_popen(monkeypatch, seen, probe_ok=False, venv_probe_ok=False)
    out = delegate({"workspace": str(ws), "task": "fix bug",
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: verification_unavailable" in out
    assert "available in neither" in out


# ─── Opt-out variations disable automatic checks ────────────────

@pytest.mark.parametrize("task", [
    "fix the bug; do not run any tests",
    "fix the bug; don't run any tests",
    "fix it but don't run pytest",
    "fix it but do not run pytest",
    "avoid running tests for now, just fix it",
    "avoid running pytest, the env is broken",
    "skip all checks and only make the change",
    "skip all tests, I will run them myself",
    "never run checks on this workspace",
    "do not execute tests after the fix",
    "without executing tests, apply the patch",
    "stop running tests here",
])
def test_opt_out_variations_disable_auto_checks(tmp_path, monkeypatch,
                                                task):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": task,
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: unknown" in out, task
    assert "auto-detected project check" not in out, task
    assert not any("-m" in a for a in seen), task


def test_opt_out_preserves_explicit_command(tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws),
                    "task": "fix the bug; do not run any tests",
                    "verify_command": ["true"], "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: independently_verified_pass" in out
    assert "auto-detected project check" not in out
    assert not any("-m" in a for a in seen)


# ─── Opposite instructions are not opt-outs ─────────────────────

@pytest.mark.parametrize("task", [
    "fix the bug; do not skip tests",
    "fix the bug; don't skip tests",
    "fix it; do not skip pytest",
    "fix it; don't skip pytest",
    "we should not skip tests for this change",
])
def test_opposite_instructions_keep_auto_checks(tmp_path, monkeypatch,
                                                task):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": task,
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: independently_verified_pass" in out, task
    assert "auto-detected project check" in out, task
    assert any("-m" in a for a in seen), task


@pytest.mark.parametrize("task", [
    "fix the bug; do not run any checks",
    "fix the bug; don't run any checks",
    "fix it; do not execute any checks",
    "fix it; don't execute any checks",
    "fix the bug; do not run any tests",
    "fix it; avoid running tests",
    "skip all checks and only make the change",
])
def test_genuine_opt_outs_suppress_auto_checks(tmp_path, monkeypatch,
                                               task):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": task,
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: unknown" in out, task
    assert "auto-detected project check" not in out, task
    assert not any("-m" in a for a in seen), task


# ─── Opt-out precedence: contradictory instructions ──────────────

@pytest.mark.parametrize("task,should_allow", [
    # Genuine opt-outs → block
    ("fix the bug; do not run tests", False),
    ("fix the bug; do not run any tests", False),
    ("fix the bug; don't run pytest", False),
    ("fix it; avoid running tests", False),
    ("fix it; do not run any checks", False),
    ("fix it; don't execute any checks", False),
    # Opposite phrases → allow
    ("fix the bug; do not skip tests", True),
    ("fix the bug; don't skip tests", True),
    ("fix it; do not skip pytest", True),
    ("fix it; don't skip pytest", True),
    ("we should not skip tests for this change", True),
])
def test_opt_out_precedence_single_instruction(tmp_path, monkeypatch,
                                                task, should_allow):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": task,
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    if should_allow:
        assert "Verification: independently_verified_pass" in out, task
        assert "auto-detected project check" in out, task
        assert any("-m" in a for a in seen), task
    else:
        assert "Verification: unknown" in out, task
        assert "auto-detected project check" not in out, task
        assert not any("-m" in a for a in seen), task


@pytest.mark.parametrize("task,should_allow", [
    # Contradictory both orders → block (conservative)
    ("do not run tests but do not skip tests", False),
    ("do not skip tests but do not run tests", False),
    ("don't run any tests; don't skip tests either", False),
    ("avoid running tests, also do not skip tests", False),
    ("do not run any checks, we should not skip tests", False),
    ("don't skip tests but do not run pytest", False),
])
def test_contradictory_instructions_block(tmp_path, monkeypatch,
                                          task, should_allow):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": task,
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: unknown" in out, task
    assert "auto-detected project check" not in out, task
    assert not any("-m" in a for a in seen), task


@pytest.mark.parametrize("task", [
    "fix the bug; do not skip tests",
    "fix the bug; don't skip tests",
    "fix it; do not skip pytest",
    "fix it; don't skip pytest",
    "we should not skip tests for this change",
])
def test_opposite_only_allows(tmp_path, monkeypatch, task):
    ws = tmp_path / "proj"
    ws.mkdir()
    _make_marker_ws(ws, "conftest.py", "tests/test_a.py")
    seen = []
    _auto_popen(monkeypatch, seen)
    out = delegate({"workspace": str(ws), "task": task,
                    "auto_verify": True},
                   provider=SubprocessWorkerProvider(binary="true"))
    assert "Verification: independently_verified_pass" in out, task
    assert "auto-detected project check" in out, task
    assert any("-m" in a for a in seen), task
