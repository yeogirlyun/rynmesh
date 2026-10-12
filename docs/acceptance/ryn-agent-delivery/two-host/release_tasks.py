"""Explicit fresh acceptance task creation, serialized with the real scheduler."""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(Path(__file__).parent.parent / "real-model"))
from release_collection import ReleaseCollection, registry, stamp  # noqa: E402
from release_observation import verify_observation  # noqa: E402


def fixture_bindings(body, configured, *, plugin, store, execution_label):
    """Resolve P05's freshly created fixtures from the actual local NAS store."""
    bindings = copy.deepcopy(configured)
    if "fixture_bindings" not in body:
        return bindings
    supplied = body["fixture_bindings"]
    labels = {"docs-alpha", "docs-beta"}
    if (body["case_id"] != "P05" or not isinstance(supplied, dict) or set(supplied) != labels
            or execution_label != "node-b" or plugin is None
            or plugin.directory.resolve() != (store.home / "plugins/nas").resolve()
            or any(not isinstance(value, str) or not value for value in supplied.values())
            or len(set(supplied.values())) != len(labels)):
        raise ValueError("release_fixture_binding_invalid")
    with plugin.lock:
        if plugin.state.get("enabled") is not True:
            raise ValueError("release_fixture_connector_disabled")
        for label, identity in supplied.items():
            rows = [row for row in plugin.state["sources"] if row["id"] == identity]
            if (len(rows) != 1 or rows[0]["name"] != "P05-" + label
                    or rows[0].get("writable") is not False or rows[0].get("allow_ai") is not True):
                raise ValueError("release_fixture_connector_mismatch")
            if label in bindings and bindings[label] != identity:
                raise ValueError("release_fixture_binding_changed")
            bindings[label] = identity
    return bindings


class RegisteredTasks:
    def __init__(self, runtime, factory):
        self.runtime, self.factory = runtime, factory
        self.collector = None
        self.collectors = {}
        self.p04_trial = None
        self.original_create = runtime.create
        with runtime._run_lock:
            if runtime.store.read()["tasks"]:
                raise ValueError("release_fresh_runtime_required")
            runtime.owner_id = "owner-test"
            def blocked(_value):
                raise ValueError("release_registered_create_required")
            runtime.create = blocked

    def create(self, body):
        # run_once acquires this same existing lock before claiming any queued
        # task. Registration and hook installation complete before releasing it.
        with self.runtime._run_lock:
            same_p04 = (body.get("case_id") == "P04" and self.p04_trial == body.get("trial")
                        and body.get("subscenario") in registry.subscenarios("P04")
                        and set(self.runtime.store.read()["tasks"]) == set(self.collectors)
                        and all(c.subscenario != body["subscenario"] for c in self.collectors.values()))
            if (self.collector is not None or self.runtime.store.read()["tasks"]) and not same_p04:
                raise ValueError("release_fresh_task_slot_required")
            task = self.original_create(body["task"])
            try:
                collector = self.factory(body, task)
                collector.register()
                collector.attach_candidate()
                self.collector = collector
                self.collectors[task["id"]] = collector
                if body.get("case_id") == "P04":
                    self.p04_trial = body["trial"]
                registered = registry.read(collector.registration_path)
                return {"task": self.runtime.get(task["id"]), "registration": registry.binding(collector.registration_path),
                        "registration_binding": registered["binding"],
                        "registration_observation_task_id": registered["observation"]["task_id"]}
            except BaseException:
                # A failed observer/registration must not leave a queued task
                # available when the scheduler's real lock is released.
                self.runtime.action(task["id"], {"action": "cancel"})
                raise


def install_registered_routes(app, store, config_path):
    config = registry.read(config_path)
    activation = Path(config["activation"])
    _active, manifest = registry._active(activation)
    runtime = app.state.agent_runtime
    collectors = {}
    calls = []
    original_complete = runtime.model.complete

    def traced(model_id, messages, specs, cancelled, on_event=None, **options):
        active = [item for item in manager.collectors.values() if getattr(item.pending, "request", None)]
        if len(active) != 1:
            raise ValueError("release_registered_dispatch_required")
        collector = active[0]
        entry = {"requested_model_id": model_id, "expected_destination": copy.deepcopy(options.get("expected_destination")),
                 "request_messages": [{k: copy.deepcopy(v) for k, v in m.items() if k != "reasoning_content"} for m in messages],
                 "tools": copy.deepcopy(specs), "dispatch_provenance": collector.model_request()}
        calls.append(entry)
        try:
            result = original_complete(model_id, messages, specs, cancelled, on_event, **options)
            entry["response"] = {"message": {k: v for k, v in result["message"].items() if k != "reasoning_content"},
                                 "usage": result.get("usage"), "finish_reason": result.get("finish_reason")}
            return result
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
            raise

    def factory(body, task):
        case_id, trial = body["case_id"], body["trial"]
        case = registry.composer().load_effective_case(case_id)["effective_case"]
        scenario = body.get("subscenario")
        scenarios = registry.subscenarios(case_id)
        if (scenarios and scenario not in scenarios) or (not scenarios and scenario is not None):
            raise ValueError("release_original_subscenario_required")
        expected = scenarios.get(scenario, {}).get("prompt", case["prompt"])
        if body["task"].get("message") != expected:
            raise ValueError("release_frozen_prompt_mismatch")
        from rynmesh.store import RynmeshStore
        nodes = {}
        for label, home in config["node_homes"].items():
            home = Path(home).resolve()
            if not (home / "identity.ed25519").is_file():
                raise ValueError("release_existing_node_identity_required")
            nodes[label] = store if home == store.home.resolve() else RynmeshStore(home=home, network_dir=home / "network")
        execution_label = next((label for label, node in nodes.items() if node is store), None)
        if execution_label is None:
            raise ValueError("release_actual_execution_store_required")
        bindings = fixture_bindings(body, config["bindings"], plugin=getattr(app.state, "nas_plugin", None),
                                    store=store, execution_label=execution_label)
        remotes = {}
        for label, remote in config.get("remote_nodes", {}).items():
            url = remote["url"]
            if urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("release_observer_requires_local_authenticated_tunnel")
            def observe(challenge, selected=remote):
                token = Path(selected["token_path"]).read_text(encoding="utf-8").strip()
                with httpx.Client(trust_env=False, timeout=15) as client:
                    response = client.post(selected["url"], json=challenge, headers={"X-Ryn-Local-Token": token})
                    response.raise_for_status()
                    return response.json()
            remotes[label] = {"peer_id": remote["peer_id"], "observe": observe, "verify": verify_observation}
        output = Path(config["output_root"]) / f"ryn-{case_id}-{trial}" / (scenario or "main")
        collector = ReleaseCollection(activation, output, case_id, "ryn", trial, runtime=runtime, task_id=task["id"],
            node_stores=nodes, source_stores={execution_label: app.state.llm_agent_services["source_store"]},
            connectors={execution_label: app.state.nas_plugin} if hasattr(app.state, "nas_plugin") else {},
            history_stores={execution_label: runtime.store}, bindings=bindings,
            planner_policy=config["planner_policy"], remote_observers=remotes, subscenario=scenario)
        collectors[task["id"]] = collector
        return collector

    manager = RegisteredTasks(runtime, factory)
    runtime.model.complete = traced
    app.state.release_tasks = manager

    @app.post("/api/local/two-host/registered-task")
    async def create(request: Request):
        app.state.friends.local_control(request)
        body = await request.json()
        try:
            if set(body) - {"case_id", "trial", "subscenario", "task", "fixture_bindings"} or not {"case_id", "trial", "task"} <= set(body):
                raise ValueError("release_create_fields_invalid")
            return manager.create(body)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/local/two-host/registered-task/{task_id}/finalize")
    async def finalize(task_id: str, request: Request):
        app.state.friends.local_control(request)
        with runtime._run_lock:
            collector = collectors.get(task_id)
            task = runtime.get(task_id)
            if collector is None or task["status"] not in {"completed", "failed", "cancelled", "interrupted"}:
                raise HTTPException(409, "release_task_not_terminal")
            bound = registry.read(collector.registration_path)["binding"]
            case = registry.composer().load_effective_case(bound["case_id"])["effective_case"]
            prompt = registry.subscenarios(bound["case_id"]).get(bound.get("subscenario"), {}).get("prompt", case["prompt"])
            return {**bound, "task": task, "prompt": case["prompt"] if bound["case_id"] == "P04" else prompt,
                    **({"session_prompt": prompt, "session_projection": manifest["artifacts"]["projection:P04"]}
                       if bound["case_id"] == "P04" else {}),
                    "release_collection": collector.finalize(), "model_calls": copy.deepcopy(
                        [call for call in calls if call["dispatch_provenance"]["task_id"] == task_id]),
                    "bindings": copy.deepcopy(collector.bindings), "started_at": collector.initial["at"], "ended_at": stamp(),
                    "isolated_run_root": str(store.home), "manifest_sha256": _active["manifest"]["sha256"],
                    "real_model": True, "planner_model": config["planner_policy"]["model_id"],
                    "provider": config.get("provider", ""), "output": str(collector.output),
                    "code_snapshot": {Path(p["path"]).relative_to(ROOT).as_posix(): p["sha256"]
                        for p in registry.read(manifest["artifacts"]["candidate_snapshot"]["path"])["files"]
                        if Path(p["path"]).is_relative_to(ROOT)}}
