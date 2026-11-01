"""Local subprocess proofs; no network providers or acceptance scores."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

FOLDER = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
spec = importlib.util.spec_from_file_location("consumer_process_proof_test_module", FOLDER / "consumer_process_proof.py")
proof_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof_module)


def sleeper(tmp_path, name="pid.json"):
    marker = tmp_path / name
    executable = Path(__file__).resolve().parents[1] / ".venv/Scripts/python.exe" if os.name == "nt" else Path(sys.executable)
    if not executable.exists():
        pytest.skip("actual Windows venv executable unavailable")
    child = subprocess.Popen([str(executable), "-c",
        "import json,os,sys,time; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()})); time.sleep(60)", str(marker)])
    owned = proof_module.OwnedConsumerProcess(child)
    deadline = time.monotonic() + 10
    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    if not marker.exists():
        owned.terminate()
        owned.close()
        pytest.fail("local subprocess did not publish identity")
    return child, owned, json.loads(marker.read_text())["pid"]


def test_actual_venv_worker_handle_is_retained_until_terminal(tmp_path):
    child, owned, pid = sleeper(tmp_path)
    try:
        owned.bind_worker(pid)
        handle = owned.handles.get(pid)
        if os.name == "nt":
            assert child.pid != pid  # Exercises the actual venv launcher split.
            assert owned.identity_basis == "verified_descendant"
            assert proof_module._wait(handle) == 258
        else:
            assert pid == child.pid
        evidence = owned.terminate()
        assert evidence["actual_worker_pid"] == pid
        assert evidence["launcher_pid"] == child.pid
        assert evidence["confirmed_terminal"]
        assert evidence["actual_worker_terminal_proof"]["confirmed_terminal"]
        if os.name == "nt":
            assert proof_module._wait(handle) == 0
            assert evidence["actual_worker_terminal_proof"]["wait_status"] == 0
            assert evidence["actual_worker_terminal_proof"]["handle_opened_before_termination"]
        assert owned.terminate() is evidence
    finally:
        owned.terminate()
        owned.close()
    assert owned.closed and not owned.handles
    owned.close()
    if os.name == "nt":
        with pytest.raises(proof_module.ProcessIdentityError, match="wait_failed"):
            proof_module._wait(handle)


def test_unrelated_and_self_reported_identity_is_harmless(tmp_path):
    child, owned, pid = sleeper(tmp_path)
    other, unrelated, other_pid = sleeper(tmp_path, "other.json")
    try:
        unrelated.bind_worker(other_pid)
        for invalid in (os.getpid(), other_pid, 0, True, "123", None):
            with pytest.raises(proof_module.ProcessIdentityError):
                owned.bind_worker(invalid)
            assert child.poll() is None and other.poll() is None
            assert owned.worker_pid is None
        owned.bind_worker(pid)
        assert owned.terminate()["confirmed_terminal"]
        assert other.poll() is None
        if os.name == "nt":
            assert proof_module._wait(unrelated.handles[other_pid]) == 258
    finally:
        owned.terminate()
        owned.close()
        unrelated.terminate()
        unrelated.close()


def test_timeout_terminalizes_owned_venv_worker_without_identity_marker(tmp_path):
    child, owned, pid = sleeper(tmp_path)
    handle = proof_module._open(pid) if os.name == "nt" else None
    controller_spec = importlib.util.spec_from_file_location("sharing_recovery_timeout_test", FOLDER / "run_sharing_recovery.py")
    controller = importlib.util.module_from_spec(controller_spec)
    sys.path.insert(0, str(FOLDER))
    try:
        controller_spec.loader.exec_module(controller)
        with pytest.raises(RuntimeError, match="consumer_deadline_exceeded"):
            controller.wait_child(child, tmp_path / "missing-boundary.json", timeout=.01, owned=owned)
        assert child.poll() is not None
        if os.name == "nt":
            assert proof_module._wait(handle) == 0
            assert pid in owned.proof["owned_processes_confirmed_terminal"]
    finally:
        sys.path.remove(str(FOLDER))
        owned.terminate()
        owned.close()
        if handle is not None:
            proof_module.kernel.CloseHandle(handle)


def test_controller_rejects_unowned_boundary_before_replacement(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(FOLDER))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "0")
    controller_spec = importlib.util.spec_from_file_location("sharing_recovery_invalid_test", FOLDER / "run_sharing_recovery.py")
    controller = importlib.util.module_from_spec(controller_spec)
    controller_spec.loader.exec_module(controller)
    original_wait = controller.wait_child
    launches = []
    wrapper_steps = []
    original_popen = controller.subprocess.Popen

    def launched(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        launches.append(child)
        return child

    def read_marker(marker):
        wrapper_steps.append("read_marker")
        return json.loads(marker.read_text(encoding="utf-8"))

    def rewrite_marker(marker, boundary):
        wrapper_steps.append("rewrite_marker")
        marker.write_text(json.dumps(boundary), encoding="utf-8")

    def invalid_boundary(child, marker=None, **kwargs):
        wrapper_steps.append("wait")
        original_wait(child, marker, **kwargs)
        if marker is not None:
            boundary = read_marker(marker)
            boundary["pid"] = os.getpid()
            rewrite_marker(marker, boundary)

    monkeypatch.setattr(controller.subprocess, "Popen", launched)
    monkeypatch.setattr(controller, "wait_child", invalid_boundary)
    result = controller.execute(tmp_path / "evidence", home=tmp_path / "consumer")
    assert result["error_code"] == "consumer_worker_invalid_identity"
    assert wrapper_steps == ["wait", "read_marker", "rewrite_marker"]
    assert result["failure_diagnostic"]["stage"] == "bind_original_worker"
    assert len(launches) == 1 and launches[0].poll() is not None
    assert "replacement_consumer_pid" not in result
    assert result["owner_source_unchanged"] and result["code_unchanged"]


def test_controller_preserves_cleanup_failure_and_closes_every_handle(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(FOLDER))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "0")
    controller_spec = importlib.util.spec_from_file_location("sharing_recovery_cleanup_test", FOLDER / "run_sharing_recovery.py")
    controller = importlib.util.module_from_spec(controller_spec)
    controller_spec.loader.exec_module(controller)
    processes = []
    real_owned = controller.OwnedConsumerProcess

    class FailFirstCleanup(real_owned):
        def __init__(self, child):
            super().__init__(child)
            self.calls = 0
            processes.append(self)

        def terminate(self, **kwargs):
            self.calls += 1
            if self is processes[0] and self.calls > 1:
                raise RuntimeError("consumer_injected_cleanup_failure")
            return super().terminate(**kwargs)

    monkeypatch.setattr(controller, "OwnedConsumerProcess", FailFirstCleanup)
    output = tmp_path / "evidence"
    result = controller.execute(output, home=tmp_path / "consumer")
    assert result["error_code"] == "consumer_cleanup_failed"
    assert result["cleanup_errors"][0]["error_code"] == "consumer_injected_cleanup_failure"
    assert len(processes) == 2 and processes[1].calls == 1
    assert all(process.closed and not process.handles for process in processes)
    assert json.loads((output / "run.json").read_text(encoding="utf-8"))["cleanup_errors"] == result["cleanup_errors"]


def load_controller(monkeypatch):
    monkeypatch.syspath_prepend(str(FOLDER))
    controller_spec = importlib.util.spec_from_file_location("sharing_recovery_diagnostic_test", FOLDER / "run_sharing_recovery.py")
    controller = importlib.util.module_from_spec(controller_spec)
    controller_spec.loader.exec_module(controller)
    return controller


def test_failure_metadata_excludes_exception_text_locals_and_external_paths(monkeypatch):
    controller = load_controller(monkeypatch)
    secret = "PRIVATE-DIAGNOSTIC-CREDENTIAL"
    external = "C:/private/PRIVATE-DIAGNOSTIC-CREDENTIAL/external.py"
    namespace = {"PermissionError": PermissionError}
    exec(compile("def external_failure():\n    raise PermissionError(13, 'PRIVATE-DIAGNOSTIC-CREDENTIAL')\n", external, "exec"), namespace)
    try:
        namespace["external_failure"]()
    except PermissionError as exc:
        exc.winerror = 5
        metadata = controller.failure_diagnostic(exc, "wait_approval_boundary", {
            "tests/test_agent_consumer_process_proof.py"})
    assert metadata["exception_type"] == "PermissionError"
    assert metadata["errno"] == 13 and metadata["winerror"] == 5
    assert metadata["repository_frames"] == [{"file": "tests/test_agent_consumer_process_proof.py",
        "function": "test_failure_metadata_excludes_exception_text_locals_and_external_paths",
        "line": metadata["repository_frames"][0]["line"]}]
    assert type(metadata["repository_frames"][0]["line"]) is int
    assert secret not in json.dumps(metadata) and external not in json.dumps(metadata)
    assert controller.safe_error_code(RuntimeError("consumer_" + secret)) is None
    assert controller.safe_error_code(RuntimeError("consumer_worker_invalid_identity")) == "consumer_worker_invalid_identity"
    invalid = OSError(13, secret)
    invalid.errno, invalid.winerror = "13", True
    metadata = controller.failure_diagnostic(invalid, "read_approval_boundary", set())
    assert "errno" not in metadata and "winerror" not in metadata and metadata["repository_frames"] == []


@pytest.mark.parametrize("fault_phase", ["wait", "read_marker", "rewrite_marker"])
def test_wrapper_permission_failure_remains_failed_and_identifies_substep(tmp_path, monkeypatch, fault_phase):
    controller = load_controller(monkeypatch)
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "0")
    original_wait, original_popen = controller.wait_child, controller.subprocess.Popen
    launches, steps = [], []
    secret = "PRIVATE-FAULT-INJECTION-MESSAGE"

    def injected_failure():
        error = PermissionError(13, secret, "C:/private/" + secret)
        error.winerror = 5
        raise error

    def wait(child, marker, **kwargs):
        steps.append("wait")
        original_wait(child, marker, **kwargs)
        if fault_phase == "wait":
            injected_failure()

    def read_marker(marker):
        steps.append("read_marker")
        if fault_phase == "read_marker":
            injected_failure()
        return json.loads(marker.read_text(encoding="utf-8"))

    def rewrite_marker(marker, boundary):
        steps.append("rewrite_marker")
        if fault_phase == "rewrite_marker":
            injected_failure()
        marker.write_text(json.dumps(boundary), encoding="utf-8")

    def boundary_wrapper(child, marker=None, **kwargs):
        wait(child, marker, **kwargs)
        if marker is not None:
            boundary = read_marker(marker)
            boundary["pid"] = os.getpid()
            rewrite_marker(marker, boundary)

    def launched(*args, **kwargs):
        child = original_popen(*args, **kwargs)
        launches.append(child)
        return child

    monkeypatch.setattr(controller, "wait_child", boundary_wrapper)
    monkeypatch.setattr(controller.subprocess, "Popen", launched)
    output = tmp_path / "evidence"
    result = controller.execute(output, home=tmp_path / "consumer")
    assert result["error_type"] == "PermissionError" and "error_code" not in result
    assert "before_restart" not in result and "replacement_consumer_pid" not in result
    assert len(launches) == 1 and launches[0].poll() is not None
    diagnostic = result["failure_diagnostic"]
    assert diagnostic["stage"] == "wait_approval_boundary"
    assert diagnostic["errno"] == 13 and diagnostic["winerror"] == 5
    functions = [frame["function"] for frame in diagnostic["repository_frames"]]
    assert functions[-2:] == [fault_phase, "injected_failure"]
    assert steps == ["wait", "read_marker", "rewrite_marker"][:["wait", "read_marker", "rewrite_marker"].index(fault_phase) + 1]
    assert all(frame["file"] in {"docs/acceptance/ryn-agent-delivery/real-model/run_sharing_recovery.py",
                                "tests/test_agent_consumer_process_proof.py"} for frame in diagnostic["repository_frames"])
    persisted = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert persisted["failure_diagnostic"] == diagnostic and secret not in json.dumps(persisted)
    assert result["owner_source_unchanged"] and result["code_unchanged"]
