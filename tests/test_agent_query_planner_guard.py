"""Exercise the query controller's common dispatch gate without model/network calls."""
import ast
import copy
import hashlib
import importlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_planner_policy import planner  # noqa: F401

from rynmesh.llm_package.model_dispatch import ModelDispatchAdapter
from rynmesh.llm_package.sources import SourceStore

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "docs/acceptance/ryn-agent-delivery/real-model/run_queries.py"


class CountingAdapter:
    def __init__(self):
        self.calls = []

    def chat(self, body, **kwargs):
        self.calls.append((body, kwargs))
        return {"message": {"role": "assistant", "content": "synthetic"}}


def dispatch_gate(namespace):
    tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
    main = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "main")
    gate = next(item for item in ast.walk(main) if isinstance(item, ast.FunctionDef) and item.name == "provider_for")
    # This exact definition installs before either cohort execution branch.
    installation = next(item for item in ast.walk(main) if isinstance(item, ast.Assign)
                        and ast.unparse(item) == "services['provider_for'] = provider_for")
    cohort_branch = next(item for item in ast.walk(main) if isinstance(item, ast.If)
                         and ast.unparse(item.test) == "args.cohort == 'ryn'")
    assert gate.lineno < installation.lineno < cohort_branch.lineno
    exec(compile(ast.Module(body=[gate], type_ignores=[]), str(CONTROLLER), "exec"), namespace)
    return namespace["provider_for"]


@pytest.fixture
def guarded_provider(tmp_path, planner, monkeypatch):  # noqa: F811
    helper, policy, _, _ = planner
    # Even an accidental use of the original source transport must stay offline.
    def forbidden_network(*args, **kwargs):
        pytest.fail("query planner guard test attempted network dispatch")
    monkeypatch.setattr("urllib.request.urlopen", forbidden_network)
    sources = SourceStore(tmp_path / "consumer", b"T" * 32, migrate=False)
    fixture = sources.prepare({"name": "Synthetic query gate", "kind": "local",
        "base_url": "http://127.0.0.1:9999/v1", "models": [helper.MODEL_ID]})
    fixture.update(status="ready", tool_support={helper.MODEL_ID: "supported"})
    helper.map_fixture(fixture, helper.MODEL_ID, policy)
    sources.save(fixture)
    selected = "local/" + sources.model_service_id(fixture, helper.MODEL_ID)
    outer = ModelDispatchAdapter(sources, fixture, helper.MODEL_ID, CountingAdapter())
    manager = SimpleNamespace(adapter=outer)
    owner_path = tmp_path / "synthetic-owner/llm/sources.enc"
    before = owner_path.read_bytes()
    inner = CountingAdapter()
    mapping_calls = []
    def mapping(*args):
        mapping_calls.append(args)
        return helper.assert_mapping(*args)
    namespace = {"original": lambda _: manager, "adapter": inner, "selected": selected, "provider_selected": selected,
        "args": SimpleNamespace(fixture_only=False, case="Q01"),
        "controller": SimpleNamespace(sources=sources), "planner_policy": policy, "assert_mapping": mapping,
        "ModelDispatchAdapter": ModelDispatchAdapter, "owner_path": owner_path,
        "owner_hash": hashlib.sha256(before).hexdigest(), "hashlib": hashlib}
    provider = dispatch_gate(namespace)
    return SimpleNamespace(provider=provider, namespace=namespace, selected=selected, manager=manager,
                           outer=outer, owner_path=owner_path, before=before, inner=inner,
                           mapping_calls=mapping_calls)


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
def test_common_query_gate_dispatches_normally_without_replacing_outer_gate(guarded_provider, cohort):
    guard = guarded_provider
    guard.namespace["args"] = SimpleNamespace(cohort=cohort)
    manager = guard.provider(guard.selected.removeprefix("local/"))
    assert manager is guard.manager and manager.adapter is guard.outer
    assert isinstance(manager.adapter, ModelDispatchAdapter)
    assert manager.adapter.inner is guard.inner and len(guard.mapping_calls) == 1
    manager.adapter.chat({"messages": [{"role": "user", "content": "synthetic guard exercise"}]})
    assert len(guard.inner.calls) == 1
    assert guard.owner_path.read_bytes() == guard.before


@pytest.mark.parametrize("cohort", ["ryn", "reference"])
@pytest.mark.parametrize(("change", "error"), [
    ("contents", "owner_source_changed_before_dispatch"),
    ("delete", "owner_source_unavailable_before_dispatch"),
])
def test_common_query_gate_refuses_owner_drift_before_any_inner_dispatch(guarded_provider, cohort, change, error):
    guard = guarded_provider
    guard.namespace["args"] = SimpleNamespace(cohort=cohort)
    if change == "contents":
        guard.owner_path.write_bytes(guard.before + b"synthetic drift")
    else:
        guard.owner_path.unlink()
    with pytest.raises(RuntimeError, match=error):
        guard.provider(guard.selected.removeprefix("local/")).adapter.chat({"messages": []})
    assert guard.inner.calls == [] and guard.mapping_calls == []
    assert guard.manager.adapter is guard.outer and guard.outer.inner is not guard.inner
    assert guard.outer.inner.calls == []


def test_query_gate_rechecks_owner_between_dispatches_not_only_at_final_record(guarded_provider):
    guard = guarded_provider
    guard.provider(guard.selected.removeprefix("local/")).adapter.chat({"messages": []})
    assert len(guard.inner.calls) == 1
    guard.owner_path.write_bytes(guard.before + b"later drift")
    with pytest.raises(RuntimeError, match="owner_source_changed_before_dispatch"):
        guard.provider(guard.selected.removeprefix("local/")).adapter.chat({"messages": []})
    assert len(guard.inner.calls) == 1 and len(guard.mapping_calls) == 1


def test_fixture_query_gate_does_not_require_an_owner_snapshot(guarded_provider):
    guard = guarded_provider
    guard.namespace["adapter"] = None
    guard.owner_path.unlink()
    assert guard.provider(guard.selected.removeprefix("local/")) is guard.manager
    assert guard.mapping_calls == [] and guard.inner.calls == []


@pytest.mark.parametrize(("change", "error"), [
    ("normal", None),
    ("contents", "owner_source_changed_before_dispatch"),
    ("delete", "owner_source_unavailable_before_dispatch"),
    ("unreadable", "owner_source_unavailable_before_dispatch"),
])
def test_query_cli_saves_actual_owner_hashes_and_read_status(tmp_path, planner, monkeypatch, change, error):  # noqa: F811
    # CLI orchestration only: inject the failure before running either model engine.
    runner = importlib.import_module("run_queries")
    helper, policy, source, _ = planner
    owner_home = tmp_path / "synthetic-owner"
    owner_path = owner_home / "llm/sources.enc"
    before = owner_path.read_bytes()
    blocked = False
    original_read = Path.read_bytes
    def read_bytes(path):
        if path == owner_path and blocked:
            raise PermissionError(13, "PRIVATE_OWNER_KEY synthetic unreadable path")
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    inner = CountingAdapter()
    monkeypatch.setenv("RYNMESH_HOME", str(owner_home))
    monkeypatch.setattr(runner, "read_planner", lambda *_: (source, inner, policy))
    monkeypatch.setattr("sys.argv", ["run_queries.py", "--live", "--case", "Q01", "--source-id", helper.SOURCE_ID])
    output = tmp_path / "cli-Q01-ryn"
    class Timestamp:
        def strftime(self, _):
            return str(tmp_path / "cli-")

        def isoformat(self):
            return datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(runner, "datetime", SimpleNamespace(now=lambda _: Timestamp()))
    class RefusingWorld(runner.QueryWorld):
        def configure(self, app, store):
            self.app = app
            self.selected = super().configure(app, store)
            return self.selected

        def execute(self, runtime):
            nonlocal blocked
            initialized = json.loads((output / "run.json").read_bytes())
            assert initialized["owner_source_sha256_before"] == hashlib.sha256(before).hexdigest()
            assert initialized["owner_source_sha256_after"] is None
            assert initialized["owner_source_after_read_status"] == "pending"
            if change == "contents":
                owner_path.write_bytes(owner_path.read_bytes() + b"synthetic drift")
            elif change == "delete":
                owner_path.unlink()
            elif change == "unreadable":
                blocked = True
            self.app.state.llm_agent_services["provider_for"](self.selected.removeprefix("local/"))
            if change == "normal":
                return {}
            pytest.fail("query controller admitted an owner source failure")
    monkeypatch.setattr(runner, "QueryWorld", RefusingWorld)
    runner.main()
    record = json.loads((output / "run.json").read_bytes())
    assert record["owner_source_sha256_before"] == hashlib.sha256(before).hexdigest()
    assert record["owner_source_hash_basis"] == "sources.enc_ciphertext_bytes"
    assert record["owner_source_unchanged"] is (change == "normal")
    if error is not None:
        assert record["error_type"] == "RuntimeError" and record["error_code"] == error
        assert str(owner_path) not in record["error_code"]
    else:
        assert "error_type" not in record and "error_code" not in record
    if change in {"delete", "unreadable"}:
        assert record["owner_source_sha256_after"] is None and record["owner_source_after_read_status"] == "unknown"
    else:
        assert record["owner_source_sha256_after"] == hashlib.sha256(owner_path.read_bytes()).hexdigest()
        assert record["owner_source_after_read_status"] == "read"
        assert (record["owner_source_sha256_before"] == record["owner_source_sha256_after"]) is (change == "normal")
    assert record["model_calls"] == [] and inner.calls == []
    assert record["formal_case_score"] is False
    assert "PRIVATE_OWNER_KEY" not in json.dumps(record)


def test_fixture_cli_owner_hashes_are_explicitly_not_applicable(tmp_path, planner, monkeypatch):  # noqa: F811
    runner = importlib.import_module("run_queries")
    owner_home = tmp_path / "synthetic-owner"
    owner_path = owner_home / "llm/sources.enc"
    original_read = Path.read_bytes
    def read_bytes(path):
        if path == owner_path:
            pytest.fail("fixture-only CLI read owner ciphertext")
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setenv("RYNMESH_HOME", str(owner_home))
    monkeypatch.setattr("sys.argv", ["run_queries.py", "--fixture-only", "--case", "Q01"])
    output = tmp_path / "fixture-Q01-ryn-fixture"
    class Timestamp:
        def strftime(self, _):
            return str(tmp_path / "fixture-")

        def isoformat(self):
            return datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(runner, "datetime", SimpleNamespace(now=lambda _: Timestamp()))
    class CollectorWorld(runner.QueryWorld):
        def execute(self, runtime):
            initialized = json.loads((output / "run.json").read_bytes())
            assert initialized["owner_source_sha256_before"] is None
            assert initialized["owner_source_sha256_after"] is None
            assert initialized["owner_source_after_read_status"] == "not_applicable"
            return {}
    monkeypatch.setattr(runner, "QueryWorld", CollectorWorld)
    runner.main()
    record = json.loads((output / "run.json").read_bytes())
    assert record["owner_source_sha256_before"] is None and record["owner_source_sha256_after"] is None
    assert record["owner_source_after_read_status"] == record["owner_source_hash_basis"] == "not_applicable"
    assert record["owner_source_unchanged"] is True and "error_type" not in record


@pytest.mark.parametrize("error", ["owner_source_changed_before_dispatch", "owner_source_unavailable_before_dispatch",
                                    "arbitrary exception with private information"])
def test_query_trace_records_only_whitelisted_owner_error_codes(error):
    tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
    traced = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == "traced")
    record = {"model_calls": []}
    def refused(*args, **kwargs):
        raise RuntimeError(error)
    namespace = {"source": {}, "controller": SimpleNamespace(upstream=SimpleNamespace(secret="fixture-secret")),
        "copy": copy, "json": json, "time": time, "args": SimpleNamespace(live=False), "record": record,
        "original_complete": refused, "dispatch_collector": None}
    exec(compile(ast.Module(body=[traced], type_ignores=[]), str(CONTROLLER), "exec"), namespace)
    with pytest.raises(RuntimeError):
        namespace["traced"]("model-a", [{"role": "user", "content": "synthetic"}], [], lambda: False)
    call = record["model_calls"][0]
    assert call["error_type"] == "RuntimeError"
    if error.startswith("owner_source_"):
        assert call["error_code"] == error
    else:
        assert "error_code" not in call and error not in json.dumps(record)
