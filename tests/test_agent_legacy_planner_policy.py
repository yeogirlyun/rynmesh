"""Legacy collector policy migration with synthetic credentials and transport."""
import base64
import copy
import hashlib
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_integrations import fake_upstream
from test_agent_webdav_fixture import FixtureDAV

from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.sources import SourceStore

FOLDER = Path(__file__).resolve().parents[1] / "docs/acceptance/ryn-agent-delivery/real-model"
CONTROLLERS = ("run_baseline", "run_documents", "run_default_model", "run_diagnostics", "run_recovery", "run_sources")


@pytest.fixture
def recovery_dav(tmp_path):
    root = tmp_path / "dav"
    (root / "reports/acceptance").mkdir(parents=True)
    (root / ".ryn-acceptance-isolated").touch()
    with FixtureDAV(root) as dav:
        yield dav


@pytest.fixture
def synthetic_owner(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(FOLDER))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_LAN_DISCOVERY", "0")
    policy = importlib.import_module("planner_policy")
    home = tmp_path / "owner"
    home.mkdir()
    key = b"synthetic-legacy-controller-key"
    (home / "identity.ed25519").write_text(base64.b64encode(key).decode())
    sources = SourceStore(home, key, migrate=False)
    row = sources.prepare({"name": "Synthetic owner", "kind": "api", "base_url": "https://127.0.0.1:9999/v1",
        "api_key": "SYNTHETIC_OWNER_CREDENTIAL", "models": ["sentinel-first", policy.MODEL_ID]})
    row.update(id=policy.SOURCE_ID, status="ready")
    row["model_settings"][policy.MODEL_ID]["overrides"] = {"context_window": 1000000, "max_output_tokens": 128000,
        "request_defaults": {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}}
    sources.save(row)
    before = sources.path.read_bytes()
    monkeypatch.setenv("RYNMESH_HOME", str(home))
    _, _, generation = policy.read_planner(home, policy.SOURCE_ID)
    return policy, sources, before, generation


def mocked_planner_transport(monkeypatch):
    import urllib.request
    real_urlopen = urllib.request.urlopen

    def reply(request):
        if not request.get("tools"):
            try:
                payload = json.loads(request["messages"][-1]["content"])
            except (ValueError, TypeError):
                payload = {}
            if isinstance(payload, dict) and "evidence" in payload:
                return {"role": "assistant", "content": json.dumps({"status": "unknown", "summary": "Synthetic policy check.",
                    "uncovered_requirements": ["real task execution"], "evidence_refs": [row["id"] for row in payload["evidence"]]})}
        return {"role": "assistant", "content": "Synthetic policy collection; no task effectiveness claim."}

    requests = fake_upstream(monkeypatch, [reply] * 80, json_only=True)
    mocked = urllib.request.urlopen

    def local_only(request, **kwargs):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.startswith("https://127.0.0.1:9999/"):
            return mocked(request, **kwargs)
        assert url.startswith(("http://127.0.0.1:", "http://localhost:")), url
        return real_urlopen(request, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", local_only)
    return requests


@pytest.mark.parametrize("name", CONTROLLERS)
def test_legacy_controller_rejects_other_source_before_owner_access(tmp_path, monkeypatch, name):
    monkeypatch.syspath_prepend(str(FOLDER))
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "missing-owner"))
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "read_planner", lambda *_: pytest.fail("invalid source reached owner reader"))
    if name == "run_recovery":
        with pytest.raises(ValueError, match="configured_glm_source_required"):
            module.run(SimpleNamespace(source_id="unrelated-source"))
    else:
        monkeypatch.setattr(sys, "argv", [name, "--live", "--source-id", "unrelated-source"])
        with pytest.raises(SystemExit) as error:
            module.main()
        assert error.value.code == 2


def test_baseline_rejects_other_model_even_on_the_allowed_source(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(FOLDER))
    policy = importlib.import_module("planner_policy")
    module = importlib.import_module("run_baseline")
    monkeypatch.setattr(module, "read_planner", lambda *_: pytest.fail("invalid model reached owner reader"))
    monkeypatch.setattr(sys, "argv", ["run_baseline", "--live", "--source-id", policy.SOURCE_ID, "--model", "sentinel-first"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


@pytest.mark.parametrize("name,cohort", [(name, "ryn") for name in CONTROLLERS if name != "run_recovery"]
    + [("run_documents", "reference"), ("run_diagnostics", "reference")])
def test_legacy_live_setup_maps_effective_policy_before_business_snapshot(tmp_path, monkeypatch, synthetic_owner, name, cohort):
    import rynmesh.agent_runtime.bridge as bridge_module
    policy, owner, before, generation = synthetic_owner
    module = importlib.import_module(name)
    requests = mocked_planner_transport(monkeypatch)
    real_build = bridge_module.build_agent_integrations
    observations, apps = [], []

    def checked_build(app, store, home):
        apps.append(app)
        bridge, tools, catalog = real_build(app, store, home)
        sources = app.state.llm_agent_services["source_store"]
        for record in sources.read().values():
            if policy.MODEL_ID not in record["models"]:
                continue
            selected = "local/" + sources.model_service_id(record, policy.MODEL_ID)
            policy.assert_mapping(sources, selected, generation)
            row = catalog.resolve(selected)
            assert row["max_output_tokens"] == 128000 and row["context_window"] == 1000000
            manager = app.state.llm_agent_services["provider_for"](selected[6:])
            assert isinstance(manager.adapter, ModelDispatchAdapter)
            assert manager.adapter.inner.model == policy.MODEL_ID
            owner.path.write_bytes(b"synthetic drift")
            try:
                with pytest.raises(RuntimeError, match="owner_source_changed_before_dispatch"):
                    app.state.llm_agent_services["provider_for"](selected[6:])
            finally:
                owner.path.write_bytes(before)
            fixture_before = sources.path.read_bytes()
            changed = sources.read()
            changed[record["id"]]["model_settings"][policy.MODEL_ID]["overrides"]["max_output_tokens"] = 9000
            sources._write(changed)
            try:
                with pytest.raises(RuntimeError, match="planner_fixture_policy_changed"):
                    app.state.llm_agent_services["provider_for"](selected[6:])
            finally:
                sources.path.write_bytes(fixture_before)
            observations.append(selected)
        return bridge, tools, catalog

    monkeypatch.setattr(bridge_module, "build_agent_integrations", checked_build)
    def capability_probe(adapter, cap):
        assert adapter.model == policy.MODEL_ID and cap == 4096
    monkeypatch.setattr(SourceStore, "probe_adapter_tools", staticmethod(capability_probe))
    real_mkdtemp = module.tempfile.mkdtemp
    monkeypatch.setattr(module.tempfile, "mkdtemp", lambda **kwargs: real_mkdtemp(dir=tmp_path, **kwargs))

    class Stamp:
        def strftime(self, _):
            return str(tmp_path / "output")

        def isoformat(self):
            return datetime.now(timezone.utc).isoformat()

    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda *_: Stamp()))
    args = [name, "--live", "--source-id", policy.SOURCE_ID]
    if name == "run_baseline":
        args += ["--runs", "1"]
    if name in ("run_documents", "run_diagnostics"):
        args += ["--cohort", cohort]
    if cohort == "reference":
        def reference_execute(**kwargs):
            kwargs["bridge"].complete(kwargs["selected"], [{"role": "user", "content": "Synthetic reference."}], [], lambda: False)
            kwargs["record"]["reference"] = {"synthetic": True}
        monkeypatch.setitem(sys.modules, "documents", SimpleNamespace(execute=reference_execute))
        args += ["--node", str(tmp_path), "--upstream", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", args)
    try:
        module.main()
    finally:
        for app in apps:
            app.state.llm_shutdown()
    assert observations and requests
    assert all(request["model"] == policy.MODEL_ID and request["max_tokens"] == 128000 for request in requests)
    assert all(request["thinking"] == {"type": "enabled"} and request["reasoning_effort"] == "max" for request in requests)
    assert owner.path.read_bytes() == before
    evidence = next(tmp_path.glob("output*/run-1.json")) if name == "run_baseline" else next(tmp_path.glob("output*/run.json"))
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["planner_policy"] == generation and record["planner_policy_mapping"]
    assert all(mapping["after"] == generation["generation"] for mapping in record["planner_policy_mapping"])
    assert "SYNTHETIC_OWNER_CREDENTIAL" not in json.dumps(record["planner_policy_mapping"])
    assert "127.0.0.1" not in json.dumps(record["planner_policy"])


def test_recovery_maps_once_revalidates_without_rewriting_and_blocks_drift(tmp_path, monkeypatch, synthetic_owner, recovery_dav):
    policy, owner, before, generation = synthetic_owner
    module = importlib.import_module("run_recovery")
    requests = mocked_planner_transport(monkeypatch)
    home = tmp_path / "consumer"
    home.mkdir()
    config = {"home": str(home), "owner_home": str(owner.path.parent.parent), "source_id": policy.SOURCE_ID,
        "owner_source_sha256": hashlib.sha256(before).hexdigest(), "planner_policy": generation,
        "dav_url": recovery_dav.url, "prompt": "Synthetic parameter mapping only.", "phase": "crash"}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    module.child(config_path)
    mapping = (home / "planner-policy-mapping.json").read_bytes()
    snapshot = (home / "llm/sources.enc").read_bytes()
    config["phase"] = "recover"
    config_path.write_text(json.dumps(config))
    module.child(config_path)
    assert (home / "planner-policy-mapping.json").read_bytes() == mapping
    assert (home / "llm/sources.enc").read_bytes() == snapshot
    assert owner.path.read_bytes() == before
    assert requests and all(request["model"] == policy.MODEL_ID and request["max_tokens"] == 128000 for request in requests)
    count = len(requests)
    changed_mapping = json.loads(mapping)
    changed_mapping[0]["fixture_source_id"] = "unrelated-fixture-same-model"
    (home / "planner-policy-mapping.json").write_text(json.dumps(changed_mapping))
    with pytest.raises(RuntimeError, match="planner_recovery_fixture_identity_changed"):
        module.child(config_path)
    (home / "planner-policy-mapping.json").write_bytes(mapping)
    sources = SourceStore(home, base64.b64decode((home / "identity.ed25519").read_text().strip()), migrate=False)
    row = next(iter(sources.read().values()))
    row["model_settings"][policy.MODEL_ID]["overrides"]["max_output_tokens"] = 9000
    sources.save(row)
    changed_bytes = sources.path.read_bytes()
    with pytest.raises(RuntimeError, match="planner_fixture_policy_changed"):
        module.child(config_path)
    assert sources.path.read_bytes() == changed_bytes
    row.pop("model_settings")
    sources._write({row["id"]: row})
    damaged_bytes = sources.path.read_bytes()
    with pytest.raises(RuntimeError, match="planner_recovery_fixture_structure_invalid"):
        module.child(config_path)
    assert sources.path.read_bytes() == damaged_bytes  # No automatic historical migration.
    (home / "llm/sources.enc").write_bytes(snapshot)
    config["planner_policy"] = copy.deepcopy(generation)
    config["planner_policy"]["generation"]["max_output_tokens"] = 9000
    config_path.write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="planner_owner_policy_changed"):
        module.child(config_path)
    config["planner_policy"] = generation
    config["owner_source_sha256"] = "invalid-owner-hash"
    config_path.write_text(json.dumps(config))
    with pytest.raises(RuntimeError, match="owner_source_changed_before_dispatch"):
        module.child(config_path)
    assert len(requests) == count


@pytest.fixture
def historical_recovery_store(tmp_path, synthetic_owner):
    policy, owner, before, generation = synthetic_owner
    home = tmp_path / "historical-consumer"
    home.mkdir()
    key = b"R" * 32
    (home / "identity.ed25519").write_text(base64.b64encode(key).decode())
    sources = SourceStore(home, key, migrate=False)
    row = sources.prepare({"name": "Isolated recovery inference", "kind": "api", "base_url": "https://127.0.0.1:9999/v1",
        "api_key": "in-memory-adapter-placeholder", "models": [policy.MODEL_ID]})
    row.update(status="ready", tool_support={policy.MODEL_ID: "supported"})
    mapping = policy.map_fixture(row, policy.MODEL_ID, generation)
    sources.save(row)
    mapping_path = home / "planner-policy-mapping.json"
    mapping_path.write_text(json.dumps([mapping]), encoding="utf-8")
    config = {"home": str(home), "owner_home": str(owner.path.parent.parent), "source_id": policy.SOURCE_ID,
        "owner_source_sha256": hashlib.sha256(before).hexdigest(), "planner_policy": generation,
        "dav_url": "http://127.0.0.1:9", "prompt": "Synthetic structural preflight only.", "phase": "recover"}
    config_path = tmp_path / "historical-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return sources, row, key, config_path, generation


@pytest.mark.parametrize("damage", [
    "orphan_settings", "missing_settings", "missing_settings_container", "invalid_settings_row",
    "missing_source_revision", "invalid_source_revision", "missing_source_destination", "invalid_source_destination",
    "missing_model_rule", "invalid_model_rule", "missing_model_destination", "invalid_model_destination",
])
def test_recovery_rejects_damaged_encrypted_structure_before_routes_without_writing(
        monkeypatch, historical_recovery_store, damage):
    import rynmesh.llm_package.routes as routes
    module = importlib.import_module("run_recovery")
    sources, row, _, config_path, generation = historical_recovery_store
    model = generation["model_id"]
    if damage == "orphan_settings":
        row["model_settings"]["orphan-model"] = copy.deepcopy(row["model_settings"][model])
    elif damage == "missing_settings":
        row["model_settings"].pop(model)
    elif damage == "missing_settings_container":
        row.pop("model_settings")
    elif damage == "invalid_settings_row":
        row["model_settings"][model] = []
    else:
        fields = {"source_revision": (row, "source_revision"), "source_destination": (row, "destination_revision"),
                  "model_rule": (row["model_settings"][model], "rule_revision"),
                  "model_destination": (row["model_settings"][model], "destination_revision")}
        action, field = damage.split("_", 1)
        target, key = fields[field]
        if action == "missing":
            target.pop(key)
        else:
            target[key] = {"source_revision": 3, "source_destination": "not-a-revision",
                           "model_rule": "g" * 32, "model_destination": ""}[field]
    # Bypass save/reconcile deliberately to model an already encrypted damaged
    # history. The recovery controller must never repair these bytes implicitly.
    sources._write({row["id"]: row})
    before_hash = hashlib.sha256(sources.path.read_bytes()).hexdigest()
    mapping_path = config_path.parent / "historical-consumer/planner-policy-mapping.json"
    mapping_before = mapping_path.read_bytes()
    monkeypatch.setattr(routes, "install_llm_routes", lambda **_: pytest.fail("damaged history reached route setup"))
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: pytest.fail("unexpected model transport"))
    with pytest.raises(RuntimeError, match="planner_recovery_fixture_structure_invalid"):
        module.child(config_path)
    assert hashlib.sha256(sources.path.read_bytes()).hexdigest() == before_hash
    assert sources.read()[row["id"]] == row
    assert mapping_path.read_bytes() == mapping_before


def test_valid_recovery_preflight_and_default_store_startup_preserve_encrypted_bytes(historical_recovery_store):
    sources, row, key, config_path, generation = historical_recovery_store
    module = importlib.import_module("run_recovery")
    home = config_path.parent / "historical-consumer"
    before_hash = hashlib.sha256(sources.path.read_bytes()).hexdigest()
    fixture, selected = module.checked_recovery_fixture(SourceStore(home, key, migrate=False), home, generation)
    assert fixture == row and selected == "local/" + sources.model_service_id(row, generation["model_id"])
    # Exercise the same default migration boundary route setup will cross.
    assert SourceStore(home, key).read()[row["id"]] == row
    assert hashlib.sha256(sources.path.read_bytes()).hexdigest() == before_hash
