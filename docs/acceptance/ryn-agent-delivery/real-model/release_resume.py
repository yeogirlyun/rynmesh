"""Reattach a collector to original persisted worker objects after process loss.

The handoff is a locator, never an identity proof. ReleaseCollection.restore
rechecks the registration, store key, nodes, task and every prior dispatch.
"""
from __future__ import annotations

import os
from pathlib import Path

from release_collection import ReleaseCollection, registry


def existing_identity(home, *, messaging=True):
    """Fail before constructors can silently generate replacement identities."""
    home = Path(home)
    for name in (("identity.ed25519", "messaging.x25519") if messaging else ("identity.ed25519",)):
        if not (home / name).is_file():
            raise ValueError("release_resume_identity_missing")
    return home


def install(config, *, runtime, task_id, node_stores, source_stores,
            connectors=None, history_stores=None, bindings, native=None):
    if not config.get("release_activation"):
        return None
    output = Path(config["output"]).resolve()
    handoff = output / "release-worker-handoff.json"
    objects = {"runtime": runtime, "task_id": task_id, "node_stores": node_stores,
               "source_stores": source_stores, "connectors": connectors or {},
               "history_stores": history_stores or {}, "bindings": bindings,
               "planner_policy": config["planner_policy"], "native": native}
    first = config["phase"] in {"start", "crash", "before"}
    if first:
        if handoff.exists():
            raise ValueError("release_resume_initial_handoff_exists")
        collector = ReleaseCollection(config["release_activation"], output,
            config["case_id"], config.get("cohort", "ryn"), config["trial"], **objects)
        if native is not None:
            # Registration is only possible when the first real native RPC has
            # persisted its original SQLite session. The caller saves afterwards.
            return collector
        collector.register()
        save(collector)
    else:
        previous = registry.read(handoff)
        if (previous["output"] != str(output) or previous["task_id"] != task_id
                or previous["case_id"] != config["case_id"]
                or previous["cohort"] != config.get("cohort", "ryn")
                or previous["trial"] != config["trial"]
                or previous["activation"] != str(Path(config["release_activation"]).resolve())):
            raise ValueError("release_resume_handoff_mismatch")
        registry._check_file(previous["registration"])
        collector = ReleaseCollection.restore(previous["registration"]["path"], output, **objects)
        if registry.read(collector.registration_path)["binding"] != previous["binding"]:
            raise ValueError("release_resume_binding_mismatch")
    if native is None:
        collector.attach_candidate()
    registry._new(output / ("release-worker-" + config["phase"] + ".json"),
                  {"pid": os.getpid(), "task_id": task_id,
                   "registration": registry.binding(collector.registration_path),
                   "prior_dispatches": len(collector.events)})
    return collector


def save(collector):
    """Persist once after actual registration; restart never reserves another slot."""
    row = registry.read(collector.registration_path)
    registry._new(collector.output / "release-worker-handoff.json", {
        "activation": str(collector.activation.resolve()), "output": str(collector.output.resolve()),
        "case_id": collector.case_id, "cohort": collector.cohort, "trial": collector.trial,
        "task_id": collector.task_id, "registration": registry.binding(collector.registration_path),
        "binding": row["binding"],
        "native_session_id": (row["observation"].get("reference") or {}).get("native_session_id")})


def prepare_documents(stack, config, root, case_id, policy, dav_url):
    """Parent-only fresh role setup, before the original task is created."""
    from release_world import ReleaseWorld

    from rynmesh.nas import NasPlugin

    world = stack.enter_context(ReleaseWorld(Path(root) / "roles", case_id, policy))
    nas = NasPlugin(world.execution_store.home)
    nas.enable(True)
    nas.configure({"name": "docs-alpha", "system": "generic", "protocol": "webdav", "url": dav_url,
                   "username": "fixture", "password": "fixture", "allow_ai": True, "writable": True})
    if config.get("planner_policy", policy) != policy:
        raise ValueError("release_resume_planner_policy_mismatch")
    config.update(home=str(world.execution_store.home), planner_policy=policy, release_world={
        "root": str(world.home), "bindings": {**world.bindings, "docs-alpha": nas.state["sources"][0]["id"]},
        "nodes": {k: str(v.home) for k, v in world.instantiated_nodes.items()},
        "node_ids": {k: v.peer_id for k, v in world.instantiated_nodes.items()}})
    # Only the replacement worker owns catalog polling during the task.
    world.consumer.close()
    return world


def reopen_documents(config, stack):
    """Recreate only in-memory signed transport around existing role stores.

    AccessQuery's setup constructor deliberately is not called: it joins friends,
    creates grants and probes sources. Those mutations must not occur on resume.
    """
    from types import SimpleNamespace

    from fastapi import FastAPI
    from planner_policy import assert_mapping
    from query_consumer import QueryConsumer

    from rynmesh.ai_access.catalog import PATH, FriendAICatalog
    from rynmesh.ai_access.routes import AIAccessState
    from rynmesh.ai_access.store import AIAccessStore
    from rynmesh.crypto import canonical_json
    from rynmesh.friends.service import FriendService
    from rynmesh.llm_package.routes import install_llm_routes
    from rynmesh.llm_package.sources import SourceStore
    from rynmesh.registry import FilePeerRegistry
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    saved = config["release_world"]
    homes = {name: existing_identity(path, messaging=name != "node-d") for name, path in saved["nodes"].items()}
    nodes = {name: RynmeshStore(home=path, network_dir=Path(saved["root"]) / "fixed-network")
             for name, path in homes.items()}
    if {k: v.peer_id for k, v in nodes.items()} != saved["node_ids"] or homes["node-b"] != Path(config["home"]):
        raise ValueError("release_resume_role_identity_mismatch")
    owner = nodes["node-a"]
    assert_mapping(SourceStore(owner.home, owner.private_key_bytes, migrate=False),
                   saved["bindings"]["model-a"], config["planner_policy"])
    disk_registry = FilePeerRegistry(Path(saved["root"]) / "fixed-registry")

    class FixedClockRegistry:
        def list_peers(self, *, network_id, max_age_hours=None):
            return disk_registry.list_peers(network_id=network_id, max_age_hours=None)

        def __getattr__(self, name):
            return getattr(disk_registry, name)

    owner.registry = FixedClockRegistry()
    app = FastAPI()
    install_llm_routes(app, store=owner, home=owner.home,
        messaging_key=peer_box.load_or_create_messaging_key(owner.home / "messaging.x25519"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    stack.callback(app.state.llm_shutdown)
    access = SimpleNamespace(store=owner, receiver_store=nodes["node-b"], services=app.state.llm_agent_services,
        nodes={}, catalog_audit=[])
    world = SimpleNamespace(stack=stack, bindings=saved["bindings"])
    access.world = world

    def post(endpoint, path, body, headers, **_):
        target = access.nodes[endpoint]
        relationship = target.verify_request(path=path, body=canonical_json(body), headers=headers)
        if path != PATH or target is not access.owner:
            raise ValueError("release_resume_unexpected_peer_route")
        access.catalog_audit.append({"peer_id": relationship["peer_id"], "authenticated": True})
        return access.publisher.respond(body, relationship)

    for name, port in (("node-a", 18921), ("node-b", 18922), ("node-c", 18923)):
        store = nodes[name]
        endpoint = "http://127.0.0.1:" + str(port)
        service = FriendService(home=store.home, peer_id=store.peer_id, node_name=name, endpoint=endpoint,
            identity_private=store.private_key_bytes,
            messaging_private=peer_box.load_or_create_messaging_key(store.home / "messaging.x25519"),
            post_json=post, allow_loopback=True)
        access.nodes[endpoint] = service
        setattr(access, {"node-a": "owner", "node-b": "receiver", "node-c": "other"}[name], service)
    sources = access.services["source_store"]
    app.state.friends = SimpleNamespace(service=access.owner)
    grants = AIAccessStore(owner.home, relationship=access.owner.store.relationship, sources=sources)
    app.state.ai_access = AIAccessState(grants, lambda _: None)
    access.publisher = FriendAICatalog(friends=lambda: access.owner, grants=lambda: grants,
        provider=lambda: [p.public_status() for p in access.services["active_source_managers"]().values()])
    access.catalogs = {name: FriendAICatalog(friends=lambda peer=peer: peer, grants=lambda: None, provider=lambda: None)
                      for name, peer in (("node-b", access.receiver), ("node-c", access.other))}
    access.catalogs["node-b"].refresh(owner.peer_id)
    consumer = QueryConsumer(access)
    return SimpleNamespace(execution_store=consumer.store, execution_app=consumer.app,
        owner_app=app, sources=sources, consumer=consumer, selected=consumer.selected,
        provider_selected=saved["bindings"]["model-a"], instantiated_nodes=nodes, bindings=saved["bindings"])


def install_documents(config, world, runtime, task_id, nas, *, native=None):
    return install(config, runtime=runtime, task_id=task_id, node_stores=world.instantiated_nodes,
        source_stores={"node-a": world.sources, "node-b": world.consumer.sources},
        connectors={"node-b": nas}, history_stores={"node-b": runtime.store},
        bindings=world.bindings, native=native)


def native_objects(config, runtime, task_id):
    if config.get("cohort", "ryn") != "reference":
        return None
    row = runtime.store.read()["tasks"][task_id]
    saved = row.get("reference_session") or row.get("reference_control_session") or {}
    return {"home": Path(config["home"]), "node": Path(config["node"]), "upstream": Path(config["upstream"]),
            "session_id": saved.get("session_id"),
            "audit_path": Path(config["output"]) / ("native-rpc-" + config["phase"] + ".jsonl")}


class NativeHandoff:
    """Save the actual first RPC registration before its business callback."""
    def __init__(self, collector):
        self.collector = collector

    def attach_reference_meter(self, meter):
        self.collector.attach_reference_meter(meter)

    def invoke(self, method, params, request_context, invoke):
        def dispatched():
            if not (self.collector.output / "release-worker-handoff.json").exists():
                save(self.collector)
            return invoke()
        return self.collector.invoke(method, params, request_context, dispatched)


def native_options(collector, *, readback=False):
    if collector is None:
        return {}
    provenance = collector.native_config()
    # A cold hydrate can truthfully produce zero RPCs. Keep its empty segment
    # present so final collection distinguishes zero dispatch from missing audit.
    if readback:
        with Path(provenance["audit_path"]).open("x", encoding="utf-8") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    return {"dispatch_observer": NativeHandoff(collector), "dispatch_provenance": provenance}


def native_recovery(config, runtime, task, bridge, tools, catalog, record, permitted, collector):
    """Original R02/R03 interrupted native executor remains a truthful failure."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
    from documents import execute
    from recovery import ReferenceRecovery

    first = config["phase"] in {"start", "crash"}
    phase = ("approval" if config["case_id"] == "R02" else "receipt") if first else "resume"
    record.update(prompt=record.get("prompt") or runtime.store.read()["tasks"][task["id"]]["messages"][0]["content"],
                  approvals=record.get("approvals", []))
    execute(runtime=runtime, tools=tools, catalog=catalog, selected=task["model_id"], task=task,
        bridge=bridge, record=record, home=Path(config["home"]), node=Path(config["node"]),
        upstream=Path(config["upstream"]), allow_write=lambda r: permitted({**r, "tool": r["name"]}),
        real_model=config.get("live", True), native_business_operations=True,
        recovery=ReferenceRecovery(runtime, task["id"], phase), **native_options(collector, readback=phase == "resume"))
    record["task"] = runtime.get(task["id"])
    return record


def native_compact(config, runtime, task, bridge, tools, catalog, permitted, collector):
    """Observe the original R06 compact boundary and resume its stopped session."""
    import copy
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "reference"))
    from documents import execute
    from run_r06 import checkpoint_state, load_case, request_version

    case = load_case()
    evidence = {"prompt": case["prompt"], "approvals": [], "formal_pass": False}
    checkpoint = None

    def compact(metadata):
        nonlocal checkpoint
        if checkpoint is not None:
            return None
        current = runtime.get(task["id"])
        body = {"message": case["interventions"][0]["message"], "intent": "steer",
                "expected_goal_revision": current["goal"]["revision"], **request_version(current)}
        updated = runtime.add_message(task["id"], body)
        checkpoint = {"compaction": copy.deepcopy(metadata), "owner_request": body, "after_correction": updated}
        return "steer"

    resumed = {}
    if config["phase"] == "after":
        saved = runtime.store.read()["tasks"][task["id"]]["reference_control_session"]
        resumed = {"native_session_id": saved["session_id"], "rpc_sequence_start": saved["last_rpc_id"],
                   "resume_action": "owner_turn", "owner_message": case["interventions"][0]["message"]}
    try:
        execute(runtime=runtime, tools=tools, catalog=catalog, selected=task["model_id"], task=task,
            bridge=bridge, record=evidence, home=Path(config["home"]), node=Path(config["node"]),
            upstream=Path(config["upstream"]), allow_write=lambda r: permitted({**r, "tool": r["name"]}),
            real_model=config.get("live", True), native_business_operations=True,
            compact_event=compact if config["phase"] == "before" else None,
            **resumed, **native_options(collector))
    except Exception as exc:
        from rynmesh.agent_runtime.store import AgentError
        if not isinstance(exc, AgentError) and not (isinstance(exc, ValueError) and str(exc).startswith("agent_")):
            raise
        evidence["native_failure"] = str(exc) if str(exc).startswith("agent_") and len(str(exc)) < 100 else type(exc).__name__
    if checkpoint is not None:
        state = checkpoint_state(runtime, task["id"])
        saved = runtime.store.read()["tasks"][task["id"]]["reference_control_session"]
        if not state["safe"] or saved["state"] != "stopped":
            raise ValueError("release_native_compact_checkpoint_not_stopped")
        evidence["safe_checkpoint"] = {"correction": checkpoint, "checkpoint": state, "worker_pid": os.getpid()}
    else:
        evidence["unmet_trigger"] = ("first_compaction_committed_then_safe_correction_checkpoint"
            if config["phase"] == "before" else "native_budget_stop_and_original_owner_continue")
    evidence["task"] = runtime.get(task["id"])
    return evidence


def restore_sharing_parent(config, world, app, store):
    """Postcondition probes stay on their original signed provider path."""
    from release_world import bind_receiver_dispatch

    from rynmesh.agent_runtime.bridge import build_agent_integrations
    from rynmesh.agent_runtime.runtime import AgentRuntime
    from rynmesh.agent_runtime.store import AgentStore
    from rynmesh.services import peer_box

    output = Path(config["output"])
    saved = registry.read(output / "release-worker-handoff.json")
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    runtime = AgentRuntime(AgentStore(store.home / "agent", peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")),
        bridge, tools, catalog, owner_id=world.case["authorization"]["owner_id"], consumer_node_id=store.peer_id)
    native = native_objects(config, runtime, saved["task_id"])
    if native is not None:
        segments = sorted(output.glob("release-native-segment-*.json"))
        native["audit_path"] = Path(registry.read(segments[-1])["audit_path"])
    collector = ReleaseCollection.restore(saved["registration"]["path"], output,
        runtime=runtime, task_id=saved["task_id"], node_stores=world.instantiated_nodes,
        source_stores={"node-a": world.sources}, connectors={}, history_stores={},
        bindings=world.bindings, planner_policy=config["planner_policy"], native=native)
    if native is None:
        collector.attach_candidate()
    bind_receiver_dispatch(world, collector)
    return collector
