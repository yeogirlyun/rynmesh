"""Actual process death after business ACK; synthetic model, never a real score."""
import json
import subprocess
import sys
import time
from pathlib import Path

from test_agent_webdav_fixture import FixtureDAV

from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


def test_process_killed_after_business_commit_queries_original_receipt_once(tmp_path):
    root, home = tmp_path / "dav", tmp_path / "consumer"
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    home.mkdir()
    worker = Path(__file__).parent / "fixtures/agent_runtime/crash_consumer.py"
    config_path = tmp_path / "consumer-config.json"
    with FixtureDAV(root) as dav:
        config = {"home": str(home), "dav_url": dav.url, "phase": "crash"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        child = subprocess.Popen([sys.executable, str(worker), str(config_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not (home / "crash-ready.json").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (home / "crash-ready.json").exists(), child.communicate(timeout=2)
            key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
            stored = next(iter(AgentStore(home / "agent", key).read()["tasks"].values()))
            assert stored["in_flight"]["dispatched"] is True
            assert not any(r["effect"] == "write" and r["status"] == "confirmed" for r in stored.get("receipts", []))
            assert (root / "reports/acceptance/recovery.md").read_text() == "RECOVERY_ONCE_731"
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        config["phase"] = "recover"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        restarted = subprocess.run([sys.executable, str(worker), str(config_path)], capture_output=True, timeout=20)
        assert restarted.returncode == 0, restarted.stderr.decode(errors="replace")
        recovered = json.loads((home / "consumer-result.json").read_text(encoding="utf-8"))
        assert recovered["reconciliation"]["state"] == "executed"
        assert recovered["status"] == "completed" and recovered["outcome"]["status"] == "verified"
        assert len([e for e in dav.audit if e["method"] == "PUT"]) == 1
        assert len(list((root / "reports/acceptance").iterdir())) == 1
        assert any(e["type"] == "reconciliation_started" for e in recovered["events"])


def test_waiting_approval_survives_process_kill_without_dispatch_or_new_identity(tmp_path):
    """Recovery infrastructure only; frozen R02 friend sharing still needs its controller."""
    root, home = tmp_path / "dav", tmp_path / "consumer"
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    home.mkdir()
    worker = Path(__file__).parent / "fixtures/agent_runtime/crash_consumer.py"
    config_path = tmp_path / "consumer-config.json"
    with FixtureDAV(root) as dav:
        config = {"home": str(home), "dav_url": dav.url, "phase": "crash", "boundary": "approval"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        child = subprocess.Popen([sys.executable, str(worker), str(config_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not (home / "approval-ready.json").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (home / "approval-ready.json").exists(), "Consumer did not reach persisted approval"
            original = json.loads((home / "approval-ready.json").read_text(encoding="utf-8"))
            key = peer_box.load_or_create_messaging_key(home / "messaging.x25519")
            stored = AgentStore(home / "agent", key).read()["tasks"][original["id"]]
            assert stored["status"] == "waiting_approval" and not stored.get("in_flight")
            assert stored["pending_approval"] == original["pending_approval"]
            assert not (root / "reports/acceptance/recovery.md").exists()
            assert not any(e["mutation"] for e in dav.audit)
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)
        config["phase"] = "recover"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        restarted = subprocess.run([sys.executable, str(worker), str(config_path)], capture_output=True, timeout=20)
        assert restarted.returncode == 0, restarted.stderr.decode(errors="replace")
        reloaded = json.loads((home / "approval-reloaded.json").read_text(encoding="utf-8"))
        assert reloaded["status"] == "waiting_approval"
        assert reloaded["pending_approval"] == original["pending_approval"]
        assert reloaded["control_revision"] == original["control_revision"]
        recovered = json.loads((home / "consumer-result.json").read_text(encoding="utf-8"))
        approved = [e for e in recovered["events"] if e["type"] == "approved"]
        writes = [e for e in recovered["events"] if e["type"] == "tool_started" and e.get("effect") == "write"]
        assert len(approved) == len(writes) == 1 and approved[0]["seq"] < writes[0]["seq"]
        assert writes[0]["call_id"] == original["pending_approval"]["call_id"]
        assert recovered["status"] == "completed" and recovered["outcome"]["status"] == "verified"
        assert len([e for e in dav.audit if e["method"] == "PUT"]) == 1
        assert (root / "reports/acceptance/recovery.md").read_text() == "RECOVERY_ONCE_731"
