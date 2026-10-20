"""Tests for read-only Computer Use status endpoint and projection."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from rynmesh.agent_runtime import image_capabilities, verification
from rynmesh.agent_runtime.computer.coordinator import NativeBridge
from rynmesh.agent_runtime.computer.protocol import EXPECTED_WORKER_NAME, PROTOCOL_SEMVER
from rynmesh.agent_runtime.extensions.adapter import ExtensionToolAdapter
from rynmesh.agent_runtime.extensions.mcp_transport import StdioMcpSession
from rynmesh.agent_runtime.extensions.store import (
    EXTENSIONS_CHANNEL,
    EXTENSIONS_VERSION,
    ExtensionStore,
)
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.agent_runtime.routes import install_agent
from rynmesh.agent_runtime.state import initialize
from rynmesh.agent_runtime.store import AgentStore, encoded
from rynmesh.atomic_io import atomic_write_json
from rynmesh.background_workers import BackgroundWorkerRegistry
from rynmesh.llm_package.sources import SourceStore
from rynmesh.services import peer_box


CANARY_SECRET = "sk-proj-canary9876543210secretvalue"
CANARY_WIN_PATH = r"C:\Users\Administrator\AppData\Local\Ryn\ryn-computer-use.exe"
CANARY_UNIX_PATH = "/Users/alice/.ryn/mcp/task_1/op_1/image_0.png"
CANARY_URL = "https://internal.example.local/v1/chat/completions?key=secret"
CANARY_TRACEBACK = 'Traceback (most recent call last): File "worker.py", line 42, in main'

CONTRACT_EVIDENCE_SOURCES = {
    "none",
    "extension_store",
    "agent_store",
    "native_probe_cache",
    "live_session",
    "model_route_contract",
    "model_image_probe",
    "runtime_receipt",
    "client_render_receipt",
}

CONTRACT_REASON_CODES = {
    None,
    "plugin_missing",
    "plugin_disabled",
    "agent_disabled",
    "native_not_verified",
    "native_missing",
    "native_platform_unsupported",
    "native_protocol_incompatible",
    "native_evidence_stale",
    "native_failed",
    "global_desktop_access_disabled",
    "task_local_execution_disabled",
    "task_desktop_access_disabled",
    "session_not_started",
    "session_lost",
    "model_unavailable",
    "image_route_unsupported",
    "image_capability_unknown",
    "image_verification_pending",
    "image_verification_failed",
    "image_evidence_stale",
    "status_unavailable",
}

CONTRACT_PERMISSION_MODES = {
    None,
    "workspace",
    "read_only",
    "confirm_changes",
    "automatic",
    "unknown",
}

CONTRACT_SESSION_STATES = {
    "not_applicable",
    "none",
    "starting",
    "connected",
    "waiting_approval",
    "busy",
    "cancelled",
    "lost",
    "unknown",
}

CONTRACT_ADAPTER_KINDS = {
    None,
    "codex_cli",
    "antigravity_cli",
    "openai_chat",
    "openai_responses",
    "anthropic_messages",
    "other",
    "unknown",
}

CONTRACT_IMAGE_STATUSES = {
    "unsupported",
    "carrier_available",
    "verified",
    "unknown",
}

CONTRACT_IMAGE_CARRIERS = {
    "none",
    "codex_turn_input",
    "api_image_blocks",
    "unknown",
}

CONTRACT_STAGE_STATES = {
    "recorded",
    "not_recorded",
    "failed",
    "unknown",
}


class ForbiddenModelClient:
    supports_image_input = True

    def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Status endpoint must never invoke model completion")


class ForbiddenTools:
    def __init__(self, extensions: ExtensionToolAdapter | None = None) -> None:
        self.extensions = extensions
        self.craft = None

    @property
    def native_bridge(self) -> Any:
        raise AssertionError("Status endpoint must never touch lazy tools.native_bridge property")

    def execute(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Status endpoint must never execute tools")


def _assert_utc_iso(value: Any) -> None:
    assert isinstance(value, str)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0


def _assert_valid_evidence(ev: dict[str, Any]) -> None:
    assert isinstance(ev, dict)
    assert set(ev.keys()) == {"source", "observed_at"}
    assert ev["source"] in CONTRACT_EVIDENCE_SOURCES
    if ev["source"] == "none":
        assert ev["observed_at"] is None
    if ev["observed_at"] is not None:
        _assert_utc_iso(ev["observed_at"])


def assert_valid_computer_status_dto(payload: dict[str, Any], expected_task_id: str | None = None) -> None:
    assert set(payload.keys()) == {
        "schema_version",
        "snapshot_id",
        "generated_at",
        "context",
        "plugin",
        "native",
        "permissions",
        "session",
        "model",
        "latest_observation",
    }
    assert type(payload["schema_version"]) is int and payload["schema_version"] == 1
    assert isinstance(payload["snapshot_id"], str) and 0 < len(payload["snapshot_id"]) <= 160
    _assert_utc_iso(payload["generated_at"])

    ctx = payload["context"]
    assert set(ctx.keys()) == {
        "task_id",
        "task_revision",
        "settings_revision",
        "selection_source",
        "model_id",
        "selection_epoch",
    }
    assert ctx["task_id"] == expected_task_id
    assert ctx["selection_source"] == ("task" if expected_task_id else "settings")
    assert isinstance(ctx["selection_epoch"], str) and 0 < len(ctx["selection_epoch"]) <= 160
    for rev_field in ("task_revision", "settings_revision"):
        assert ctx[rev_field] is None or (type(ctx[rev_field]) is int and ctx[rev_field] >= 0)
    assert ctx["model_id"] is None or (isinstance(ctx["model_id"], str) and len(ctx["model_id"]) <= 1024)

    plugin = payload["plugin"]
    assert set(plugin.keys()) == {"installed", "enabled", "revision", "evidence", "reason_code"}
    assert plugin["installed"] is None or type(plugin["installed"]) is bool
    assert plugin["enabled"] is None or type(plugin["enabled"]) is bool
    assert plugin["revision"] is None or (type(plugin["revision"]) is int and plugin["revision"] >= 0)
    assert plugin["reason_code"] in CONTRACT_REASON_CODES
    _assert_valid_evidence(plugin["evidence"])

    native = payload["native"]
    assert set(native.keys()) == {
        "platform",
        "executable_presence",
        "status",
        "version",
        "protocol_version",
        "evidence",
        "reason_code",
    }
    assert native["platform"] in {"windows", "unsupported", "unknown"}
    assert native["executable_presence"] in {"present", "missing", "unknown"}
    assert native["status"] in {"unknown", "missing", "unverified", "ready", "incompatible", "error"}
    assert native["reason_code"] in CONTRACT_REASON_CODES
    _assert_valid_evidence(native["evidence"])

    perms = payload["permissions"]
    assert set(perms.keys()) == {
        "agent_enabled",
        "global_desktop_access",
        "task_scope",
        "task_local_execution",
        "task_desktop_access",
        "global_permission_mode",
        "task_permission_mode",
        "evidence",
    }
    assert "reason_code" not in perms
    for b_field in ("agent_enabled", "global_desktop_access", "task_local_execution", "task_desktop_access"):
        assert perms[b_field] is None or type(perms[b_field]) is bool
    assert perms["task_scope"] == ("present" if expected_task_id else "not_requested")
    assert perms["global_permission_mode"] in CONTRACT_PERMISSION_MODES
    assert perms["task_permission_mode"] in CONTRACT_PERMISSION_MODES
    _assert_valid_evidence(perms["evidence"])

    sess = payload["session"]
    assert set(sess.keys()) == {"state", "generation", "evidence", "reason_code"}
    assert sess["state"] in CONTRACT_SESSION_STATES
    assert sess["generation"] is None or (type(sess["generation"]) is int and sess["generation"] >= 0)
    assert sess["reason_code"] in CONTRACT_REASON_CODES
    _assert_valid_evidence(sess["evidence"])

    model = payload["model"]
    assert set(model.keys()) == {"id", "display_name", "adapter", "availability", "image_input"}
    assert model["id"] == ctx["model_id"]
    assert model["display_name"] is None or (isinstance(model["display_name"], str) and len(model["display_name"]) <= 160)
    assert model["adapter"] in CONTRACT_ADAPTER_KINDS
    assert model["availability"] in {"available", "unavailable", "unknown"}

    img = model["image_input"]
    assert set(img.keys()) == {"status", "carrier", "evidence", "reason_code"}
    assert img["status"] in CONTRACT_IMAGE_STATUSES
    assert img["carrier"] in CONTRACT_IMAGE_CARRIERS
    assert img["reason_code"] in CONTRACT_REASON_CODES
    _assert_valid_evidence(img["evidence"])
    if img["status"] == "verified":
        assert img["evidence"]["source"] == "model_image_probe"
        assert img["evidence"]["observed_at"] is not None

    obs = payload["latest_observation"]
    if obs is not None:
        assert set(obs.keys()) == {
            "id",
            "task_revision",
            "selection_epoch",
            "model_id",
            "matches_current_selection",
            "width",
            "height",
            "stages",
        }
        assert type(obs["matches_current_selection"]) is bool
        if obs["matches_current_selection"]:
            assert obs["selection_epoch"] == ctx["selection_epoch"]
            assert obs["model_id"] == ctx["model_id"]
            assert obs["task_revision"] == ctx["task_revision"]
        stages = obs["stages"]
        assert set(stages.keys()) == {"saved", "displayed", "submitted", "semantic_observed"}
        for stage_val in stages.values():
            assert set(stage_val.keys()) == {"state", "evidence"}
            assert stage_val["state"] in CONTRACT_STAGE_STATES
            _assert_valid_evidence(stage_val["evidence"])

    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (CANARY_SECRET, CANARY_WIN_PATH, CANARY_UNIX_PATH, CANARY_URL, CANARY_TRACEBACK):
        assert forbidden not in serialized


def make_test_app(
    tmp_path: Path,
    *,
    allow_local: bool = True,
    initialize_extension_store: bool = False,
) -> tuple[FastAPI, Any, ExtensionToolAdapter, SourceStore, Any]:
    app = FastAPI()
    key = X25519PrivateKey.generate()
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)

    ext_store = ExtensionStore(home / "agent", key) if initialize_extension_store else None
    ext_adapter = ExtensionToolAdapter(SimpleNamespace(store=None), lambda: None, extension_store=ext_store)
    tools = ForbiddenTools(extensions=ext_adapter)

    source_store = SourceStore(home, b"test-status-source-key-material")
    services: dict[str, Any] = {"source_store": source_store}
    catalog = ModelCatalog(services, SimpleNamespace(peer_id="node-local", get_shared_model=lambda _: None))

    def local_control(request: Request) -> None:
        if not allow_local or request.headers.get("x-deny-local") == "1":
            raise HTTPException(status_code=403, detail="local_owner_only")

    runtime = install_agent(
        app,
        store=SimpleNamespace(home=home, peer_id="node-local"),
        home=home,
        workers=BackgroundWorkerRegistry(),
        local_control=local_control,
        messaging_key=key,
        integrations=(ForbiddenModelClient(), tools, catalog),
    )

    if not initialize_extension_store:
        # install_agent -> AgentRuntime.__init__ calls bind_agent_store; reset to uninitialized
        # so tests can verify GET /api/local/agent/computer/status does not lazily create it.
        if ext_adapter._manager is not None:
            ext_adapter._manager.close()
        ext_adapter._store = None
        ext_adapter._manager = None
        ext_file = home / "agent" / "extensions.json"
        ext_lock = home / "agent" / ".extensions.lock"
        ext_pkgs = home / "agent" / "extensions_pkgs"
        if ext_file.exists():
            ext_file.unlink()
        if ext_lock.exists():
            ext_lock.unlink()
        if ext_pkgs.exists():
            shutil.rmtree(ext_pkgs, ignore_errors=True)

    return app, runtime, ext_adapter, source_store, key


def seed_task_direct(
    runtime: Any,
    task_id: str,
    *,
    model_id: str | None = None,
    status: str = "completed",
    revision: int = 12,
    control_revision: int = 8,
    scope_revision: int = 1,
    goal_revision: int = 1,
    context_epoch: int = 0,
    permission_mode: str = "workspace",
    local_execution: dict[str, Any] | None = None,
    model_destination: dict[str, Any] | None = None,
    pending_approval: dict[str, Any] | None = None,
    in_flight: dict[str, Any] | None = None,
    receipts: list[dict[str, Any]] | None = None,
    image_request_links: list[dict[str, Any]] | None = None,
    client_render_receipts: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    updated_at: str = "2026-10-19T08:00:00Z",
) -> None:
    def mutator(data: dict[str, Any]) -> None:
        row: dict[str, Any] = {
            "id": task_id,
            "status": status,
            "revision": revision,
            "model_id": model_id if model_id is not None else "",
            "created_at": "2026-10-19T07:55:00Z",
            "updated_at": updated_at,
            "messages": [{"role": "user", "content": CANARY_SECRET}],
            "events": events if events is not None else [],
            "title": CANARY_SECRET,
            "permission_mode": permission_mode,
            "local_execution": local_execution if local_execution is not None else {
                "workspace_root": str(runtime.store.root / "files"),
                "desktop_access": True,
            },
            "model_destination": model_destination if model_destination is not None else {
                "catalog_model_id": model_id or "",
                "destination_revision": "dest_rev_1",
            },
            "receipts": receipts if receipts is not None else [],
            "image_request_links": image_request_links if image_request_links is not None else [],
            "client_render_receipts": client_render_receipts if client_render_receipts is not None else [],
        }
        initialize(row)
        row["control_revision"] = control_revision
        row["scope_revision"] = scope_revision
        row["goal"]["revision"] = goal_revision
        row["context_epoch"] = context_epoch
        if pending_approval is not None:
            row["pending_approval"] = pending_approval
        if in_flight is not None:
            row["in_flight"] = in_flight
        data["tasks"][task_id] = row

    runtime.store.transaction(mutator)


def test_auth_query_validation_and_error_codes(tmp_path: Path) -> None:
    app, runtime, _, _, _ = make_test_app(tmp_path)
    seed_task_direct(runtime, "task_valid_1", model_id="local/openai/gpt-4.1")

    def mark_deleted(data: dict[str, Any]) -> None:
        data.setdefault("deleted_tasks", {})["task_deleted_1"] = {"deleted_at": "2026-10-19T08:10:00Z"}

    runtime.store.transaction(mark_deleted)

    with TestClient(app) as client:
        # 1. Local owner auth check runs first
        denied = client.get("/api/local/agent/computer/status", headers={"x-deny-local": "1"})
        assert denied.status_code == 403
        assert denied.headers.get("Cache-Control") == "no-store"

        # 2. Unknown query params rejected with 400 agent_invalid_request
        bad_param = client.get("/api/local/agent/computer/status?foo=bar")
        assert bad_param.status_code == 400
        assert bad_param.json() == {"detail": "agent_invalid_request"}
        assert bad_param.headers.get("Cache-Control") == "no-store"

        # 3. Repeated task_id rejected with 400 agent_invalid_request
        repeated = client.get("/api/local/agent/computer/status?task_id=task_valid_1&task_id=task_valid_1")
        assert repeated.status_code == 400
        assert repeated.json() == {"detail": "agent_invalid_request"}

        # 4. Invalid/unsafe task_id rejected with 400 agent_invalid_task_id
        for bad_task_id in (
            "",
            "   ",
            "../private",
            "C:\\Windows\\System32\\cmd.exe",
            "/Users/alice/.ssh/id_rsa",
            "task_with_secret_sk-1234567890abcdef",
            "task.exe",
            "x" * 165,
        ):
            res = client.get("/api/local/agent/computer/status", params={"task_id": bad_task_id})
            assert res.status_code == 400, f"Expected 400 for {bad_task_id!r}, got {res.status_code}"
            assert res.json() == {"detail": "agent_invalid_task_id"}
            assert res.headers.get("Cache-Control") == "no-store"

        # 5. Missing task -> 404 agent_task_not_found
        not_found = client.get("/api/local/agent/computer/status", params={"task_id": "task_missing_999"})
        assert not_found.status_code == 404
        assert not_found.json() == {"detail": "agent_task_not_found"}
        assert not_found.headers.get("Cache-Control") == "no-store"

        # 6. Deleted task -> 410 agent_task_deleted
        deleted = client.get("/api/local/agent/computer/status", params={"task_id": "task_deleted_1"})
        assert deleted.status_code == 410
        assert deleted.json() == {"detail": "agent_task_deleted"}
        assert deleted.headers.get("Cache-Control") == "no-store"

        # 7. Corrupt encrypted state -> 503 agent_computer_status_unavailable
        runtime.store.path.write_bytes(b"corrupted-ciphertext")
        runtime.store._cached_stamp = None
        unreadable = client.get("/api/local/agent/computer/status")
        assert unreadable.status_code == 503
        assert unreadable.json() == {"detail": "agent_computer_status_unavailable"}
        assert unreadable.headers.get("Cache-Control") == "no-store"


def test_strictly_readonly_no_spawns_no_lazy_init_and_unchanged_revisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, runtime, ext_adapter, _, _ = make_test_app(tmp_path, initialize_extension_store=False)
    seed_task_direct(runtime, "task_ro", model_id="local/unknown/model")

    # Tripwires on subprocess, model catalog live discovery, store writes, and lazy properties
    def forbidden_popen(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.Popen must never be called by status endpoint")

    def forbidden_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must never be called by status endpoint")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(subprocess, "run", forbidden_run)
    monkeypatch.setattr(
        runtime.catalog,
        "list_models",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("list_models forbidden")),
    )
    monkeypatch.setattr(
        runtime.catalog,
        "resolve",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("resolve forbidden")),
    )
    monkeypatch.setattr(
        AgentStore,
        "_write",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("AgentStore._write forbidden")),
    )
    monkeypatch.setattr(
        AgentStore,
        "transaction",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("AgentStore.transaction forbidden")),
    )
    monkeypatch.setattr(
        ExtensionStore,
        "_write",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ExtensionStore._write forbidden")),
    )

    class RuntimeWithLazyBridgeProperty(type(runtime)):
        @property
        def native_bridge(self) -> Any:
            raise AssertionError("runtime.native_bridge property must never be accessed")

    runtime.__class__ = RuntimeWithLazyBridgeProperty

    ext_file = runtime.store.root / "extensions.json"
    assert not ext_file.exists()
    assert ext_adapter._store is None
    assert ext_adapter._manager is None

    before_bytes = runtime.store.path.read_bytes()

    with TestClient(app) as client:
        resp1 = client.get("/api/local/agent/computer/status")
        assert resp1.status_code == 200
        assert resp1.headers.get("Cache-Control") == "no-store"
        dto1 = resp1.json()
        assert_valid_computer_status_dto(dto1, None)

        resp2 = client.get("/api/local/agent/computer/status", params={"task_id": "task_ro"})
        assert resp2.status_code == 200
        dto2 = resp2.json()
        assert_valid_computer_status_dto(dto2, "task_ro")

    after_bytes = runtime.store.path.read_bytes()
    assert before_bytes == after_bytes
    assert not ext_file.exists()
    assert ext_adapter._store is None
    assert ext_adapter._manager is None
    assert dto1["plugin"]["installed"] is None
    assert dto1["plugin"]["enabled"] is None
    assert dto1["plugin"]["evidence"] == {"source": "none", "observed_at": None}
    assert dto1["context"]["settings_revision"] == dto2["context"]["settings_revision"]


def test_plugin_projection_and_unknown_enabled_neutrality(tmp_path: Path) -> None:
    app, runtime, ext_adapter, _, key = make_test_app(tmp_path, initialize_extension_store=True)

    with TestClient(app) as client:
        # Builtin computer-use starts installed=True, enabled=False
        r1 = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r1, None)
        assert r1["plugin"]["installed"] is True
        assert r1["plugin"]["enabled"] is False
        assert r1["plugin"]["reason_code"] == "plugin_disabled"
        assert r1["plugin"]["evidence"]["source"] == "extension_store"

        # Enable computer-use plugin
        ext_adapter._store.set_plugin_enabled("computer-use", True)
        r2 = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r2, None)
        assert r2["plugin"]["installed"] is True
        assert r2["plugin"]["enabled"] is True
        assert r2["plugin"]["reason_code"] is None

        # Write a sealed extension record without 'enabled' key -> must remain None (unknown), never default to True
        raw_ext = {
            "version": EXTENSIONS_VERSION,
            "revision": 7,
            "updated_at": "2026-10-19T16:00:00+08:00",
            "plugins": {"computer-use": {"id": "computer-use", "installed": True}},
            "mcp_servers": {},
        }
        nonce, ciphertext = peer_box.seal(
            key,
            ext_adapter._store.pub,
            encoded(raw_ext),
            info=EXTENSIONS_CHANNEL,
        )
        atomic_write_json(
            ext_adapter._store.path,
            {"version": EXTENSIONS_VERSION, "nonce": nonce, "ciphertext": ciphertext},
        )
        r3 = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r3, None)
        assert r3["plugin"]["installed"] is True
        assert r3["plugin"]["enabled"] is None
        assert r3["plugin"]["reason_code"] is None
        assert r3["plugin"]["evidence"] == {
            "source": "extension_store",
            "observed_at": "2026-10-19T08:00:00Z",
        }


def test_native_bridge_frozen_hook_and_unbound_cache_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rynmesh.agent_runtime.computer_status.sys.platform", "win32")
    app, runtime, ext_adapter, _, _ = make_test_app(tmp_path, initialize_extension_store=True)
    ext_adapter._store.set_plugin_enabled("computer-use", True)

    def enable_settings(data: dict[str, Any]) -> None:
        data["settings"].update(enabled=True, desktop_access=True, permission_mode="confirm_changes")
        data["settings_revision"] = data.get("settings_revision", 0) + 1

    runtime.store.transaction(enable_settings)

    # 1. Poison arbitrary loose native cache hooks on runtime, store, and tools
    for holder in (runtime, runtime.store, runtime.tools):
        for attr in ("computer_status_native_snapshot", "native_probe_cache", "_native_probe_cache", "native_status"):
            setattr(
                holder,
                attr,
                {
                    "platform": "windows",
                    "status": "ready",
                    "executable_presence": "present",
                    "version": "9.9.9",
                    "protocol_version": PROTOCOL_SEMVER,
                    "observed_at": "2026-10-19T08:00:00Z",
                    "source": "native_probe_cache",
                },
            )

    with TestClient(app) as client:
        r_unbound = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r_unbound, None)
        assert r_unbound["native"]["status"] == "unknown"
        assert r_unbound["native"]["executable_presence"] == "unknown"
        assert r_unbound["native"]["reason_code"] == "native_not_verified"

        # 2. Mount genuine NativeBridge on runtime.store._ryn_native_bridge (construct-only, unprobed)
        bridge = NativeBridge(owner=runtime.store, bundled_executable=CANARY_WIN_PATH)
        runtime.store._ryn_native_bridge = bridge
        r_empty_bridge = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r_empty_bridge, None)
        assert r_empty_bridge["native"]["status"] == "unknown"
        assert r_empty_bridge["native"]["executable_presence"] == "unknown"
        assert r_empty_bridge["native"]["reason_code"] == "native_not_verified"
        assert r_empty_bridge["native"]["evidence"] == {"source": "none", "observed_at": None}

        # 3. Simulate NativeBridge.cache_status() returning a live cached handshake without spawning:
        # until authoritative binary/config verification binding is integrated, status must be "unverified" (never "ready").
        cached_payload: dict[str, Any] = {
            "status": "ready",
            "cached": True,
            "live": True,
            "handshakeTimestamp": "2026-10-19T08:15:00Z",
            "workerGeneration": 1,
            "workerInstance": "inst_1",
            "worker": {"name": EXPECTED_WORKER_NAME, "version": "0.1.0"},
            "protocolVersion": PROTOCOL_SEMVER,
            "capabilities": {"desktop": True},
            "limits": {},
        }
        monkeypatch.setattr(bridge, "cache_status", lambda: dict(cached_payload))

        r_live_bridge = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r_live_bridge, None)
        assert r_live_bridge["native"]["status"] == "unverified"
        assert r_live_bridge["native"]["executable_presence"] == "present"
        assert r_live_bridge["native"]["version"] == "0.1.0"
        assert r_live_bridge["native"]["protocol_version"] == PROTOCOL_SEMVER
        assert r_live_bridge["native"]["reason_code"] == "native_not_verified"
        assert r_live_bridge["native"]["evidence"] == {
            "source": "live_session",
            "observed_at": "2026-10-19T08:15:00Z",
        }

        # 4. Dead worker -> stale cached evidence
        cached_payload["live"] = False
        cached_payload["status"] = "stale"
        r_stale_bridge = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r_stale_bridge, None)
        assert r_stale_bridge["native"]["status"] == "unverified"
        assert r_stale_bridge["native"]["reason_code"] == "native_evidence_stale"
        assert r_stale_bridge["native"]["evidence"] == {
            "source": "native_probe_cache",
            "observed_at": "2026-10-19T08:15:00Z",
        }

        # 5. Incompatible protocol version -> status="incompatible"
        cached_payload["live"] = True
        cached_payload["status"] = "ready"
        cached_payload["protocolVersion"] = "99.0.0"
        r_incompat = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r_incompat, None)
        assert r_incompat["native"]["status"] == "incompatible"
        assert r_incompat["native"]["reason_code"] == "native_protocol_incompatible"


def test_task_session_isolation_and_permissions_projection(tmp_path: Path) -> None:
    app, runtime, ext_adapter, _, _ = make_test_app(tmp_path, initialize_extension_store=True)

    def set_global(data: dict[str, Any]) -> None:
        data["settings"].update(enabled=True, desktop_access=True, permission_mode="confirm_changes")
        data["settings_revision"] = 5

    runtime.store.transaction(set_global)

    seed_task_direct(
        runtime,
        "task_a",
        model_id="local/openai/gpt-4.1",
        permission_mode="workspace",
        local_execution={"workspace_root": str(tmp_path / "ws"), "desktop_access": False},
    )
    seed_task_direct(
        runtime,
        "task_b",
        model_id="local/openai/gpt-4.1",
        permission_mode="read_only",
        local_execution={},
    )

    poll_calls = 0

    def _forbidden_poll() -> int | None:
        nonlocal poll_calls
        poll_calls += 1
        raise AssertionError("read-only computer status GET must never call process.poll()")

    ext_adapter._manager = SimpleNamespace(
        _lock=None,
        _sessions={
            ("computer-use", "task_b"): SimpleNamespace(
                generation=4,
                started_at="2026-10-19T08:20:00Z",
                closed=False,
                proc=SimpleNamespace(returncode=None, poll=_forbidden_poll),
            )
        },
        _pending={},
        close=lambda: None,
    )

    with TestClient(app) as client:
        # Global query without task_id -> session.state == "not_applicable", task_scope == "not_requested"
        r_global = client.get("/api/local/agent/computer/status").json()
        assert_valid_computer_status_dto(r_global, None)
        assert r_global["session"]["state"] == "not_applicable"
        assert r_global["session"]["generation"] is None
        assert r_global["permissions"]["task_scope"] == "not_requested"
        assert r_global["permissions"]["global_permission_mode"] == "confirm_changes"
        assert r_global["permissions"]["task_permission_mode"] is None

        # Querying task_a must NOT see task_b's connected session
        r_a = client.get("/api/local/agent/computer/status", params={"task_id": "task_a"}).json()
        assert_valid_computer_status_dto(r_a, "task_a")
        assert r_a["session"]["state"] == "none"
        assert r_a["session"]["reason_code"] == "session_not_started"
        assert r_a["permissions"]["task_scope"] == "present"
        assert r_a["permissions"]["global_desktop_access"] is True
        assert r_a["permissions"]["task_local_execution"] is True
        assert r_a["permissions"]["task_desktop_access"] is False
        assert r_a["permissions"]["task_permission_mode"] == "workspace"

        # Querying task_b sees its own isolated session entry (not task_a's "none"),
        # refuses fake legacy generation/started_at without calling poll(), and projects empty local_execution -> False
        r_b = client.get("/api/local/agent/computer/status", params={"task_id": "task_b"}).json()
        assert_valid_computer_status_dto(r_b, "task_b")
        assert poll_calls == 0
        assert r_b["session"] == {
            "state": "unknown",
            "generation": None,
            "evidence": {
                "source": "none",
                "observed_at": None,
            },
            "reason_code": None,
        }
        assert r_b["permissions"]["task_local_execution"] is False
        assert r_b["permissions"]["task_desktop_access"] is False
        assert r_b["permissions"]["task_permission_mode"] == "read_only"

        # Real StdioMcpSession.__new__ constructor field shape (without spawning a subprocess):
        # process (not proc), closed=threading.Event() (not bool), config_generation/revision (not generation),
        # and no started_at/handshake_at producer fields.
        real_stdio_sess = StdioMcpSession.__new__(StdioMcpSession)
        real_stdio_sess.server_id = "computer-use"
        real_stdio_sess.instance_id = "inst-stdio-1"
        real_stdio_sess.revision = 3
        real_stdio_sess.config_generation = 7
        real_stdio_sess.initialized = True
        real_stdio_sess.server_info = {"name": EXPECTED_WORKER_NAME, "version": PROTOCOL_SEMVER}
        real_stdio_sess.failure = None
        real_stdio_sess.closed = threading.Event()
        real_stdio_sess.process = SimpleNamespace(returncode=None, poll=_forbidden_poll)

        ext_adapter._manager._sessions = {
            ("computer-use", "task_b"): real_stdio_sess,
            ("other-server", "task_a"): real_stdio_sess,
        }

        # Open threading.Event (is_set() == False) must NOT be treated as bool(Event) == True ("lost"),
        # must NOT fabricate task.updated_at as live_session evidence, and must NOT map config_generation to generation.
        r_b_real_open = client.get("/api/local/agent/computer/status", params={"task_id": "task_b"}).json()
        assert_valid_computer_status_dto(r_b_real_open, "task_b")
        assert poll_calls == 0
        assert r_b_real_open["session"] == {
            "state": "unknown",
            "generation": None,
            "evidence": {"source": "none", "observed_at": None},
            "reason_code": None,
        }

        # Foreign server key ("other-server", "task_a") is ignored by task_a
        r_a_foreign = client.get("/api/local/agent/computer/status", params={"task_id": "task_a"}).json()
        assert_valid_computer_status_dto(r_a_foreign, "task_a")
        assert r_a_foreign["session"]["state"] == "none"
        assert r_a_foreign["session"]["reason_code"] == "session_not_started"

        # Setting closed Event -> state="lost", reason_code="session_lost", zero poll() calls, no fake task_updated_at
        real_stdio_sess.closed.set()
        r_b_real_closed = client.get("/api/local/agent/computer/status", params={"task_id": "task_b"}).json()
        assert_valid_computer_status_dto(r_b_real_closed, "task_b")
        assert poll_calls == 0
        assert r_b_real_closed["session"] == {
            "state": "lost",
            "generation": None,
            "evidence": {"source": "none", "observed_at": None},
            "reason_code": "session_lost",
        }

        # Cached failure string on open Event -> state="lost", reason_code="session_lost"
        real_stdio_sess.closed.clear()
        real_stdio_sess.failure = "agent_mcp_protocol_invalid"
        r_b_real_failed = client.get("/api/local/agent/computer/status", params={"task_id": "task_b"}).json()
        assert_valid_computer_status_dto(r_b_real_failed, "task_b")
        assert poll_calls == 0
        assert r_b_real_failed["session"]["state"] == "lost"
        assert r_b_real_failed["session"]["reason_code"] == "session_lost"

        # Cached process.returncode != None (memory-only __dict__ inspection without calling poll()) -> state="lost"
        real_stdio_sess.failure = None
        real_stdio_sess.process.returncode = 1
        r_b_real_exited = client.get("/api/local/agent/computer/status", params={"task_id": "task_b"}).json()
        assert_valid_computer_status_dto(r_b_real_exited, "task_b")
        assert poll_calls == 0
        assert r_b_real_exited["session"]["state"] == "lost"
        assert r_b_real_exited["session"]["reason_code"] == "session_lost"

        # Historical / interrupted / unknown computer-use flight in real encrypted AgentStore with 0 live sessions:
        # Must project state="unknown" with agent_store evidence and reason_code=None (NEVER "busy"),
        # and must preserve store ciphertext/revisions/reconciliation/receipts without replay or mutation.
        ext_adapter._manager._sessions = {}

        def seed_interrupted_unknown_task(data: dict[str, Any]) -> None:
            flight = {
                "kind": "tool",
                "call_id": "call_click_1",
                "operation_id": "op_click_1",
                "name": "mcp__plugin_computer-use_computer-use__desktop_click",
                "tool": "mcp__plugin_computer-use_computer-use__desktop_click",
                "arguments": {"x": 100, "y": 100},
                "effect": "write",
                "computer_access": True,
                "dispatched": True,
                "context_epoch": 0,
                "goal_revision": 1,
                "scope_revision": 1,
                "started_at": "2026-10-19T08:15:00Z",
                "generation": 2,
                "extension_binding": {
                    "plugin_id": "computer-use@ryn-extensions",
                    "server_id": "computer-use",
                    "computer_access": True,
                },
            }
            row: dict[str, Any] = {
                "id": "task_interrupted",
                "status": "interrupted",
                "revision": 39,
                "created_at": "2026-10-19T08:10:00Z",
                "updated_at": "2026-10-19T08:15:05Z",
                "model_id": "local/openai/gpt-4.1",
                "messages": [{"role": "user", "content": "Click target on desktop"}],
                "events": [],
                "receipts": [],
                "permission_mode": "confirm_changes",
                "local_execution": {"workspace_root": str(tmp_path / "ws"), "desktop_access": True},
                "model_destination": {
                    "catalog_model_id": "local/openai/gpt-4.1",
                    "destination_revision": "dest_rev_1",
                },
                "in_flight": dict(flight),
                "error": "agent_write_outcome_unknown",
                "pending_control": {"kind": "pause"},
            }
            initialize(row)
            row["control_revision"] = 4
            verification.record(
                row,
                flight,
                "unknown",
                "2026-10-19T08:15:05Z",
                flight=flight,
            )
            row["reconciliation"] = {
                "state": "unknown",
                "operation_id": "op_click_1",
                "attempts": 1,
            }
            row["session_generation"] = 2
            data["tasks"]["task_interrupted"] = row

        runtime.store.transaction(seed_interrupted_unknown_task)
        before_bytes = runtime.store.path.read_bytes()
        before_snapshot = runtime.store.read()["tasks"]["task_interrupted"]

        r_interrupted = client.get(
            "/api/local/agent/computer/status",
            params={"task_id": "task_interrupted"},
        ).json()
        assert_valid_computer_status_dto(r_interrupted, "task_interrupted")
        assert r_interrupted["session"] == {
            "state": "unknown",
            "generation": 2,
            "evidence": {
                "source": "agent_store",
                "observed_at": "2026-10-19T08:15:00Z",
            },
            "reason_code": None,
        }
        assert runtime.store.path.read_bytes() == before_bytes
        after_snapshot = runtime.store.read()["tasks"]["task_interrupted"]
        assert after_snapshot["revision"] == before_snapshot["revision"]
        assert after_snapshot["control_revision"] == before_snapshot["control_revision"]
        assert after_snapshot["status"] == "interrupted"
        assert after_snapshot["run_state"] == "pausing"
        assert after_snapshot["in_flight"] == before_snapshot["in_flight"]
        assert after_snapshot["reconciliation"] == {"state": "unknown", "operation_id": "op_click_1", "attempts": 1}
        assert len(after_snapshot["receipts"]) == 1
        assert after_snapshot["receipts"][0]["status"] == "unknown"

        # Injecting an unsupported legacy compat dict into _sessions for an interrupted/unknown task
        # must NOT poll and must NOT upgrade unknown flight to "busy".
        ext_adapter._manager._sessions = {
            ("computer-use", "task_interrupted"): {
                "closed": False,
                "proc": SimpleNamespace(returncode=None, poll=_forbidden_poll),
                "generation": 2,
                "started_at": "2026-10-19T08:15:00Z",
            }
        }
        r_legacy_interrupted = client.get(
            "/api/local/agent/computer/status",
            params={"task_id": "task_interrupted"},
        ).json()
        assert_valid_computer_status_dto(r_legacy_interrupted, "task_interrupted")
        assert poll_calls == 0
        assert r_legacy_interrupted["session"]["state"] == "unknown"
        assert r_legacy_interrupted["session"]["reason_code"] is None
        ext_adapter._manager._sessions = {}

        # Even if task.status is "running" with retained in_flight and 0 live MCP sessions,
        # task.status alone cannot prove live session "busy".
        def set_running_without_live_session(data: dict[str, Any]) -> None:
            data["tasks"]["task_interrupted"]["status"] = "running"
            data["tasks"]["task_interrupted"]["run_state"] = "running"

        runtime.store.transaction(set_running_without_live_session)
        r_running_no_live = client.get(
            "/api/local/agent/computer/status",
            params={"task_id": "task_interrupted"},
        ).json()
        assert_valid_computer_status_dto(r_running_no_live, "task_interrupted")
        assert r_running_no_live["session"]["state"] == "unknown"
        assert r_running_no_live["session"]["evidence"]["source"] == "agent_store"
        assert r_running_no_live["session"]["reason_code"] is None


def test_model_routes_image_capabilities_and_negative_source_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, runtime, _, source_store, _ = make_test_app(tmp_path, initialize_extension_store=True)

    # 1. Seed real _cli_metadata with local/<service_id>/model/<id>
    runtime.catalog._cli_metadata = {
        "local/codex-service": {
            "manager": SimpleNamespace(
                manifest={"name": "Codex Local", "adapter": "codex_cli"},
                adapter=SimpleNamespace(kind="codex_cli"),
            ),
            "models": [
                {"id": "gpt-5-codex", "name": "GPT-5 Codex", "default": True, "supports_image_input": True},
            ],
        },
        "local/agy-service": {
            "manager": SimpleNamespace(
                manifest={"name": "Antigravity Local", "adapter": "antigravity_cli"},
                adapter=SimpleNamespace(kind="antigravity_cli"),
            ),
            "models": [
                {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro", "default": True},
            ],
        },
    }

    # 2. Seed real SourceStore with two sources that share an upstream model name ("owned-model"):
    #    - rec_vision has supports_image_input=True
    #    - rec_disabled has supports_image_input=False
    rec_vision = source_store.prepare({
        "name": "OpenAI Vision Source",
        "kind": "api",
        "protocol": "openai_chat",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test-1234567890",
        "models": ["gpt-4.1-mini", "owned-model"],
    })
    rec_vision["supports_image_input"] = True
    source_store.save(rec_vision)
    sid_vision = f"local/{source_store.model_service_id(rec_vision, 'gpt-4.1-mini')}"
    sid_vision_shared = f"local/{source_store.model_service_id(rec_vision, 'owned-model')}"

    rec_disabled = source_store.prepare({
        "name": "Disabled Image Source",
        "kind": "api",
        "protocol": "openai_chat",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test-1234567890",
        "models": ["owned-model"],
    })
    rec_disabled["supports_image_input"] = False
    source_store.save(rec_disabled)
    sid_disabled = f"local/{source_store.model_service_id(rec_disabled, 'owned-model')}"

    seed_task_direct(runtime, "task_codex", model_id="local/codex-service/model/gpt-5-codex")
    seed_task_direct(runtime, "task_agy", model_id="local/agy-service/model/gemini-2.5-pro")
    seed_task_direct(runtime, "task_api_vision", model_id=sid_vision)
    seed_task_direct(runtime, "task_api_vision_shared", model_id=sid_vision_shared)
    seed_task_direct(runtime, "task_api_disabled", model_id=sid_disabled)
    seed_task_direct(runtime, "task_no_model", model_id="")

    calls: list[dict[str, Any]] = []
    real_eval = image_capabilities.evaluate_image_capability

    def spy_eval(model_row: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({
            "kwargs": set(kwargs.keys()),
            "has_source_record": kwargs.get("source_record") is not None,
            "source_id": (kwargs.get("source_record") or {}).get("id"),
        })
        return real_eval(model_row, **kwargs)

    monkeypatch.setattr(image_capabilities, "evaluate_image_capability", spy_eval)

    with TestClient(app) as client:
        # Codex CLI -> codex_turn_input + carrier_available
        r_codex = client.get("/api/local/agent/computer/status", params={"task_id": "task_codex"}).json()
        assert_valid_computer_status_dto(r_codex, "task_codex")
        assert r_codex["model"]["adapter"] == "codex_cli"
        assert r_codex["model"]["image_input"]["carrier"] == "codex_turn_input"
        assert r_codex["model"]["image_input"]["status"] == "carrier_available"
        assert r_codex["model"]["image_input"]["reason_code"] == "image_verification_pending"

        # Antigravity CLI -> unsupported + carrier="none" + image_route_unsupported
        r_agy = client.get("/api/local/agent/computer/status", params={"task_id": "task_agy"}).json()
        assert_valid_computer_status_dto(r_agy, "task_agy")
        assert r_agy["model"]["adapter"] == "antigravity_cli"
        assert r_agy["model"]["image_input"]["status"] == "unsupported"
        assert r_agy["model"]["image_input"]["carrier"] == "none"
        assert r_agy["model"]["image_input"]["reason_code"] == "image_route_unsupported"

        # OpenAI chat vision -> adapter="openai_chat", carrier="api_image_blocks", status="carrier_available"
        r_api = client.get("/api/local/agent/computer/status", params={"task_id": "task_api_vision"}).json()
        assert_valid_computer_status_dto(r_api, "task_api_vision")
        assert r_api["model"]["adapter"] == "openai_chat"
        assert r_api["model"]["image_input"]["carrier"] == "api_image_blocks"
        assert r_api["model"]["image_input"]["status"] == "carrier_available"

        # Loose hook / runtime.computer_status_model_snapshot self-asserting verified + full probe fields
        # MUST NOT promote model.image_input.status to "verified".
        dest_rev_vision = runtime.catalog.source_destination_revision(rec_vision, "gpt-4.1-mini")
        runtime.computer_status_model_snapshot = {
            sid_vision: {
                "id": sid_vision,
                "name": "OpenAI Vision Source / gpt-4.1-mini",
                "source": "local_api",
                "adapter": "openai_chat",
                "available": True,
                "model_id": "gpt-4.1-mini",
                "source_id": rec_vision["id"],
                "peer_id": "node-local",
                "provider_peer_id": "node-local",
                "destination_revision": dest_rev_vision,
                "source_revision": rec_vision["source_revision"],
                "supports_image_input": True,
                "image_input": "verified",
                "pixel_recognition_verified": True,
                "image_probe": {
                    "source": "model_image_probe",
                    "status": "verified",
                    "verified": True,
                    "task_id": "task_api_vision",
                    "model_id": sid_vision,
                    "source_id": rec_vision["id"],
                    "provider_peer_id": "node-local",
                    "adapter": "openai_chat",
                    "carrier": "api_image_blocks",
                    "image_transport": "api_multimodal_content",
                    "destination_revision": dest_rev_vision,
                    "source_revision": rec_vision["source_revision"],
                    "selection_epoch": r_api["context"]["selection_epoch"],
                    "request_id": "req_probe_1",
                    "sha256": "a" * 64,
                    "observed_at": "2026-10-19T08:05:00Z",
                },
            },
            sid_disabled: {
                "id": sid_disabled,
                "name": "Owned model",
                "source": "local_api",
                "adapter": "openai_compatible",
                "available": True,
                "model_id": "owned-model",
                "supports_image_input": True,
                "image_input": "verified",
                "pixel_recognition_verified": True,
            },
            sid_vision_shared: {
                "id": sid_vision_shared,
                "name": "Shared Owned model",
                "source": "local_api",
                "adapter": "openai_compatible",
                "available": True,
                "model_id": "owned-model",
            },
        }
        r_no_false_verified = client.get("/api/local/agent/computer/status", params={"task_id": "task_api_vision"}).json()
        assert_valid_computer_status_dto(r_no_false_verified, "task_api_vision")
        assert r_no_false_verified["model"]["image_input"]["status"] == "carrier_available"
        assert r_no_false_verified["model"]["image_input"]["reason_code"] == "image_verification_pending"

        # Negative source_record precedence & exact service_id matching across two sources sharing "owned-model"
        calls.clear()
        r_neg = client.get("/api/local/agent/computer/status", params={"task_id": "task_api_disabled"}).json()
        assert_valid_computer_status_dto(r_neg, "task_api_disabled")
        assert len(calls) == 1
        assert calls[0]["has_source_record"] is True
        assert calls[0]["source_id"] == rec_disabled["id"]
        assert "model_id" not in calls[0]["kwargs"]
        assert r_neg["model"]["adapter"] == "openai_chat"
        assert r_neg["model"]["image_input"]["status"] == "unsupported"
        assert r_neg["model"]["image_input"]["reason_code"] == "image_route_unsupported"

        calls.clear()
        r_shared_pos = client.get("/api/local/agent/computer/status", params={"task_id": "task_api_vision_shared"}).json()
        assert_valid_computer_status_dto(r_shared_pos, "task_api_vision_shared")
        assert len(calls) == 1
        assert calls[0]["source_id"] == rec_vision["id"]
        assert r_shared_pos["model"]["image_input"]["status"] == "carrier_available"
        assert r_shared_pos["context"]["selection_epoch"] != r_neg["context"]["selection_epoch"]

        # Task without model_id -> does NOT fall back to global settings.model_id
        def set_default_model(data: dict[str, Any]) -> None:
            data["settings"]["model_id"] = sid_vision
            data["settings_revision"] = data.get("settings_revision", 0) + 1

        runtime.store.transaction(set_default_model)
        r_none = client.get("/api/local/agent/computer/status", params={"task_id": "task_no_model"}).json()
        assert_valid_computer_status_dto(r_none, "task_no_model")
        assert r_none["context"]["model_id"] is None
        assert r_none["model"]["id"] is None
        assert r_none["model"]["adapter"] is None
        assert r_none["model"]["availability"] == "unknown"
        assert r_none["model"]["image_input"]["status"] == "unknown"


def test_observation_requires_correlated_receipts_and_tracks_selection_epoch(tmp_path: Path) -> None:
    app, runtime, _, source_store, _ = make_test_app(tmp_path, initialize_extension_store=True)

    rec = source_store.prepare({
        "name": "Vision API",
        "kind": "api",
        "protocol": "openai_chat",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test-1234567890",
        "models": ["gpt-4.1", "gpt-4.1-mini"],
    })
    rec["supports_image_input"] = True
    source_store.save(rec)
    model_a = f"local/{source_store.model_service_id(rec, 'gpt-4.1')}"
    model_b = f"local/{source_store.model_service_id(rec, 'gpt-4.1-mini')}"
    dest_rev_a = runtime.catalog.source_destination_revision(rec, "gpt-4.1")
    dest_rev_b = runtime.catalog.source_destination_revision(rec, "gpt-4.1-mini")

    rec_other_source = source_store.prepare({
        "name": "Second Vision API",
        "kind": "api",
        "protocol": "openai_chat",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-test-0987654321",
        "models": ["gpt-4.1"],
    })
    rec_other_source["supports_image_input"] = True
    source_store.save(rec_other_source)
    model_a_other_source = f"local/{source_store.model_service_id(rec_other_source, 'gpt-4.1')}"
    dest_rev_other_a = runtime.catalog.source_destination_revision(rec_other_source, "gpt-4.1")

    # 1. Unbound observation hook & unrecognized receipt (or receipt with invented 'at' only) must NOT be promoted
    sha = hashlib.sha256(b"fake-png-bytes").hexdigest()
    seed_task_direct(
        runtime,
        "task_unrecognized",
        model_id=model_a,
        receipts=[
            {
                "saved": True,
                "displayed": True,
                "submitted": True,
                "semantic_observed": True,
                "path": CANARY_WIN_PATH,
                "model_id": "earlier-model",
                "selection_epoch": "earlier-epoch",
            },
            {
                "operation_id": "op_invented_at",
                "status": "confirmed",
                "goal_revision": 1,
                "scope_revision": 1,
                "context_epoch": 0,
                "at": "2026-10-19T08:01:00Z",
                "result": {
                    "image_artifacts": [
                        {
                            "id": "obs_invented_at",
                            "path": ".ryn/mcp/task_unrecognized/op_invented_at/image_0.png",
                            "mime_type": "image/png",
                            "bytes": 1024,
                            "sha256": sha,
                            "width": 1280,
                            "height": 720,
                        }
                    ]
                },
            },
        ],
    )
    runtime.computer_status_observation_snapshot = {
        "task_unrecognized": {
            "id": "unbound-obs",
            "width": 100,
            "height": 100,
            "stages": {
                "semantic_observed": {
                    "state": "recorded",
                    "evidence": {"source": "runtime_receipt", "observed_at": "2026-10-19T08:00:00Z"},
                }
            },
        }
    }

    # 2. Genuine confirmed receipt produced via verification.record + linkage via build_image_request_linkage
    dest_a = {
        "catalog_model_id": model_a,
        "provider_peer_id": "node-local",
        "source_id": rec["id"],
        "destination_revision": dest_rev_a,
    }
    seed_task_direct(
        runtime,
        "task_correlated",
        model_id=model_a,
        revision=12,
        control_revision=8,
        scope_revision=1,
        goal_revision=1,
        context_epoch=0,
        model_destination=dest_a,
        client_render_receipts=[
            # Negative case 1: same operation_id, wrong sha256
            {
                "task_id": "task_correlated",
                "operation_id": "op_shot_1",
                "sha256": "b" * 64,
                "displayed_at": "2026-10-19T08:01:01Z",
            },
            # Negative case 2: same sha256, wrong operation_id
            {
                "task_id": "task_correlated",
                "operation_id": "other_op",
                "sha256": sha,
                "displayed_at": "2026-10-19T08:01:01Z",
            },
            # Negative case 3: missing task_id / no explicit success ack
            {
                "operation_id": "op_shot_1",
                "sha256": sha,
                "observed_at": "2026-10-19T08:01:01Z",
            },
            # Negative case 4: self-asserted displayed dict with no canonical producer
            {
                "task_id": "task_correlated",
                "operation_id": "op_shot_1",
                "sha256": sha,
                "displayed": True,
                "status": "displayed",
                "displayed_at": "2026-10-19T08:01:01Z",
            },
        ],
    )

    art_rel_path = ".ryn/mcp/task_correlated/op_shot_1/image_0.png"
    art_entry = {
        "id": "obs_shot_1",
        "path": art_rel_path,
        "mime_type": "image/png",
        "bytes": 15,
        "sha256": sha,
        "width": 1280,
        "height": 720,
    }
    cap_a = image_capabilities.evaluate_image_capability(
        {
            "id": model_a,
            "source": "local_api",
            "adapter": "openai_compatible",
            "model_id": "gpt-4.1",
        },
        source_record=rec,
    )
    data_Url = "data:image/png;base64,ZmFrZS1wbmctYnl0ZXM="  # base64("fake-png-bytes")

    def record_genuine_receipt_and_link(data: dict[str, Any]) -> None:
        t = data["tasks"]["task_correlated"]
        call = {
            "id": "call_shot_1",
            "operation_id": "op_shot_1",
            "name": "mcp__computer_use__screenshot",
            "arguments": {},
            "effect": "read_only_observation",
            "extension_binding": {"server_id": "computer-use", "computer_access": True},
        }
        verification.record(
            t,
            call,
            "confirmed",
            "2026-10-19T08:01:00Z",
            result={"image_artifacts": [art_entry]},
        )
        link = image_capabilities.build_image_request_linkage(
            request_id="req_101",
            model_id=model_a,
            capability=cap_a,
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_Url}}]}],
            stage="image_inspection",
            operation_id="op_shot_1",
            source_artifacts=[
                {
                    "operation_id": "op_shot_1",
                    "path": art_rel_path,
                    "sha256": sha,
                    "payload_sha256": sha,
                    "mime_type": "image/png",
                    "width": 1280,
                    "height": 720,
                    "bytes": 15,
                }
            ],
            task_row=t,
            dispatch_status="submitted",
            semantic_response_received=True,
        )
        link["binding_current"] = True
        link["submitted_at"] = "2026-10-19T08:01:02Z"
        link["completed_at"] = "2026-10-19T08:01:05Z"
        t["image_request_links"] = [link]

    runtime.store.transaction(record_genuine_receipt_and_link)

    with TestClient(app) as client:
        r_unrec = client.get("/api/local/agent/computer/status", params={"task_id": "task_unrecognized"}).json()
        assert_valid_computer_status_dto(r_unrec, "task_unrecognized")
        assert r_unrec["latest_observation"] is None

        r_corr = client.get("/api/local/agent/computer/status", params={"task_id": "task_correlated"}).json()
        assert_valid_computer_status_dto(r_corr, "task_correlated")
        obs = r_corr["latest_observation"]
        assert obs is not None
        assert obs["id"] == "obs_shot_1"
        assert obs["width"] == 1280
        assert obs["height"] == 720
        assert obs["matches_current_selection"] is True
        orig_obs_epoch = obs["selection_epoch"]
        assert orig_obs_epoch == r_corr["context"]["selection_epoch"]
        assert obs["task_revision"] == r_corr["context"]["task_revision"]
        assert obs["stages"]["saved"] == {
            "state": "recorded",
            "evidence": {"source": "runtime_receipt", "observed_at": "2026-10-19T08:01:00Z"},
        }
        assert obs["stages"]["displayed"] == {"state": "unknown", "evidence": {"source": "none", "observed_at": None}}
        assert obs["stages"]["submitted"] == {
            "state": "recorded",
            "evidence": {"source": "runtime_receipt", "observed_at": "2026-10-19T08:01:02Z"},
        }
        assert obs["stages"]["semantic_observed"] == {
            "state": "recorded",
            "evidence": {"source": "runtime_receipt", "observed_at": "2026-10-19T08:01:05Z"},
        }
        # Semantic observation does NOT promote model.image_input.status to "verified"
        assert r_corr["model"]["image_input"]["status"] == "carrier_available"

        # 3. Dynamic negative cases on correlated linkage / destination / revisions
        def mutate_task_links(mut_fn: Any) -> dict[str, Any]:
            snapshot_Holder: dict[str, Any] = {}

            def save_orig(data: dict[str, Any]) -> None:
                snapshot_Holder["orig"] = json.loads(json.dumps(data["tasks"]["task_correlated"]))
                mut_fn(data["tasks"]["task_correlated"])

            def restore_orig(data: dict[str, Any]) -> None:
                data["tasks"]["task_correlated"] = snapshot_Holder["orig"]

            runtime.store.transaction(save_orig)
            try:
                res = client.get("/api/local/agent/computer/status", params={"task_id": "task_correlated"}).json()
                assert_valid_computer_status_dto(res, "task_correlated")
                return res
            finally:
                runtime.store.transaction(restore_orig)

        # 3a. binding_current=False -> rejected link, submitted/semantic not_recorded, matches_current_selection=False
        r_not_current = mutate_task_links(
            lambda t: t["image_request_links"][0].__setitem__("binding_current", False)
        )
        assert r_not_current["latest_observation"]["matches_current_selection"] is False
        assert r_not_current["latest_observation"]["stages"]["submitted"]["state"] == "not_recorded"
        assert r_not_current["latest_observation"]["stages"]["semantic_observed"]["state"] == "not_recorded"

        # 3b. Receipt vs link revision mismatch -> rejected link, never overwrites historical receipt revision
        r_rev_mismatch = mutate_task_links(
            lambda t: t["receipts"][0].__setitem__("goal_revision", 0)
        )
        assert r_rev_mismatch["latest_observation"]["matches_current_selection"] is False
        assert r_rev_mismatch["latest_observation"]["stages"]["submitted"]["state"] == "not_recorded"

        # 3c. Payload sha256 mismatch -> submitted and semantic_observed stay not_recorded
        r_bad_payload = mutate_task_links(
            lambda t: t["image_request_links"][0]["submitted_images"][0].__setitem__("payload_sha256", "c" * 64)
        )
        assert r_bad_payload["latest_observation"]["stages"]["submitted"]["state"] == "not_recorded"
        assert r_bad_payload["latest_observation"]["stages"]["semantic_observed"]["state"] == "not_recorded"

        # 3d. Missing or wrong destination_revision (including whole-source vs per-model), source_id, or provider_peer_id
        for field_name, bad_val in (
            ("destination_revision", ""),
            ("destination_revision", "stale_dest_rev"),
            ("destination_revision", rec["destination_revision"]),
            ("source_id", ""),
            ("source_id", rec_other_source["id"]),
            ("provider_peer_id", ""),
            ("provider_peer_id", "node-other-peer"),
        ):
            r_bad_dest = mutate_task_links(
                lambda t, fn=field_name, bv=bad_val: t["model_destination"].__setitem__(fn, bv)
            )
            assert r_bad_dest["latest_observation"]["matches_current_selection"] is False, field_name
            assert r_bad_dest["latest_observation"]["selection_epoch"] != r_bad_dest["context"]["selection_epoch"]

        # 3e. Old goal_revision, scope_revision, control_revision, context_epoch on task -> matches_current_selection=False
        for rev_mutation in (
            lambda t: t["goal"].__setitem__("revision", 2),
            lambda t: t.__setitem__("scope_revision", 2),
            lambda t: t.__setitem__("control_revision", 9),
            lambda t: t.__setitem__("context_epoch", 1),
        ):
            r_old_rev = mutate_task_links(rev_mutation)
            assert r_old_rev["latest_observation"]["matches_current_selection"] is False

        # 3f. Same upstream model name ("gpt-4.1") on a different SourceStore source -> matches_current_selection=False
        def switch_to_other_source(t: dict[str, Any]) -> None:
            t["model_id"] = model_a_other_source
            t["model_destination"] = {
                "catalog_model_id": model_a_other_source,
                "provider_peer_id": "node-local",
                "source_id": rec_other_source["id"],
                "destination_revision": dest_rev_other_a,
            }

        r_other_src = mutate_task_links(switch_to_other_source)
        assert r_other_src["latest_observation"]["matches_current_selection"] is False
        assert r_other_src["latest_observation"]["selection_epoch"] != r_other_src["context"]["selection_epoch"]

        # 4. Switching task model invalidates matches_current_selection while preserving original observation epoch
        def switch_model(data: dict[str, Any]) -> None:
            t = data["tasks"]["task_correlated"]
            t["model_id"] = model_b
            t["control_revision"] = 9
            t["revision"] = 13
            t["model_destination"] = {
                "catalog_model_id": model_b,
                "provider_peer_id": "node-local",
                "source_id": rec["id"],
                "destination_revision": dest_rev_b,
            }

        runtime.store.transaction(switch_model)
        r_switched = client.get("/api/local/agent/computer/status", params={"task_id": "task_correlated"}).json()
        assert_valid_computer_status_dto(r_switched, "task_correlated")
        obs_after = r_switched["latest_observation"]
        assert obs_after is not None
        assert obs_after["model_id"] == model_a
        assert obs_after["selection_epoch"] == orig_obs_epoch
        assert obs_after["matches_current_selection"] is False
        assert obs_after["selection_epoch"] != r_switched["context"]["selection_epoch"]

