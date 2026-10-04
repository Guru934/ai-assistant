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
    assert "source of truth" in text
    assert "never claim" in text.lower()


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
