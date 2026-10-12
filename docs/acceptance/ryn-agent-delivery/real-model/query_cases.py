"""Q01-Q03/Q06 fixed local business world; no model answers are fabricated.

Business setup uses real encrypted sources, local HTTP capability probes,
self-signed registry records and the production presence classifier. The clock
is the frozen development clock; registry retention is disabled for these fixed
records only. Loopback responders are fixtures, never independent AI providers.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import socket
import threading
from contextlib import ExitStack
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rynmesh.llm_package.adapters import AdapterError
from rynmesh.registry import FilePeerRegistry, PeerRecord, sign_peer_record
from rynmesh.services.peer_health import PeerHealthProbe
from rynmesh.store import RynmeshStore

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests/fixtures/agent_acceptance"
SUPPORTED = {"Q01", "Q02", "Q03", "Q06"}


def load_case(case_id):
    if case_id not in SUPPORTED:
        raise ValueError("development_query_case_unsupported")
    suite = json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))
    if suite.get("partition") != "development":
        raise ValueError("development_partition_required")
    return copy.deepcopy(next(item for item in suite["cases"] if item["id"] == case_id))


class QueryWorld:
    def __init__(self, home, case_id):
        self.home, self.case, self.stack = Path(home), load_case(case_id), ExitStack()
        self.world = json.loads((FIXTURES / "world.json").read_text(encoding="utf-8"))
        self.bindings, self.probes, self.health_requests = {}, [], []
        self.planner_policy, self.planner_policy_mapping = None, []
        self.access_query = None

    def __enter__(self):
        spec = importlib.util.spec_from_file_location("development_query_source", FIXTURES / "source_fixture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.upstream = self.stack.enter_context(module.FixtureSource(tool_models=("alpha",)))
        self.closed_port = socket.socket()
        self.closed_port.bind(("127.0.0.1", 0))  # Bound but not listening: deterministic refusal, no port reuse.
        self.stack.callback(self.closed_port.close)
        requests = self.health_requests
        class Health(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                requests.append({"path": self.path, "status": 200 if self.path == "/health" else 404})
                self.send_response(requests[-1]["status"])
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.health = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        thread = threading.Thread(target=self.health.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(thread.join, 3)
        self.stack.callback(self.health.server_close)
        self.stack.callback(self.health.shutdown)
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def registry_store(self, label, store):
        return RynmeshStore(home=self.home / label, network_dir=self.home / "fixed-network")

    def configure_access(self, app, store):
        if self.case["id"] == "Q06":
            from access_query import AccessQuery
            self.access_query = AccessQuery(self, app, store)

    def select_model(self):
        return self.access_query.consumer.selected if self.access_query else self.bindings["model-a"]

    def configure(self, app, store):
        self.app, self.store = app, store
        sources = self.sources = app.state.llm_agent_services["source_store"]
        store.personal_space.create("Frozen development model world")
        store.personal_space.set_policy("space")
        for label, models in (("source-alpha", ["alpha", "beta"]), ("source-other", ["alpha"]), ("source-broken", ["offline"])):
            broken = label == "source-broken"
            endpoint = ("http://127.0.0.1:" + str(self.closed_port.getsockname()[1]) + "/v1") if broken else self.upstream.url
            record = sources.prepare({"name": label, "kind": "local", "base_url": endpoint,
                                      "api_key": self.upstream.secret, "models": models})
            if broken:
                try:
                    sources.probe(record)
                except (AdapterError, OSError) as exc:
                    chain, current = [], exc
                    for _ in range(8):
                        if current is None:
                            break
                        chain.append(type(current).__name__)
                        current = current.__cause__ or current.__context__
                    if "ConnectionRefusedError" not in chain:
                        raise RuntimeError("fixture_connection_refusal_not_observed") from None
                    self.probes.append({"source": label, "error_code": "connection_refused", "cause_types": chain})
                else:
                    raise RuntimeError("fixture_port_unexpectedly_reachable")
                record["status"] = "failed"
                record["model_settings"]["offline"]["evidence"] = {"connection": {"status": "failed",
                    "error_code": "connection_refused", "checked_at": self.world["clock"], "method": "actual_loopback_socket_probe"}}
            else:
                sources.probe(record)
                record["status"] = "ready"
                record["tool_support"] = {}
                for model in models:
                    try:
                        sources.probe_model_tools(record, model)
                    except ValueError:
                        record["tool_support"][model] = "unsupported"
                    else:
                        record["tool_support"][model] = "supported"
                self.probes.append({"source": label, "chat": "passed", "tools": copy.deepcopy(record["tool_support"])})
            record["shared_models"] = ["alpha"] if label == "source-alpha" else []
            if label == "source-alpha" and self.planner_policy:
                from planner_policy import map_fixture
                self.planner_policy_mapping.append(map_fixture(record, "alpha", self.planner_policy))
            sources.save(record)
            self.bindings[label] = record["id"]
            for logical, expected in self.world["models"].items():
                if expected["source_id"] == label:
                    self.bindings[logical] = "local/" + sources.model_service_id(record, expected["upstream_id"])
        registry = FilePeerRegistry(self.home / "fixed-registry")
        for label in ("node-a", "node-c", "node-d"):
            node = self.registry_store(label, store)
            facts = self.world["devices"][label]
            endpoints = (f"http://127.0.0.1:{self.health.server_port}",) if label == "node-a" else ()
            record = PeerRecord(peer_id=node.peer_id, node_name=label, endpoints=endpoints,
                updated_at=facts.get("last_seen", facts.get("checked_at")) or "")
            registry.publish(sign_peer_record(record, private_key_bytes=node.private_key_bytes))
            self.bindings[label] = node.peer_id
        class FixedClockRegistry:
            def list_peers(self, *, network_id, max_age_hours=None):
                return registry.list_peers(network_id=network_id, max_age_hours=None)
            def __getattr__(self, name):
                return getattr(registry, name)
        store.registry = FixedClockRegistry()
        self.configure_access(app, store)
        self.sources_before = hashlib.sha256(sources.path.read_bytes()).hexdigest()
        self.selected = self.select_model()
        return self.selected

    def attach(self, tools):
        tools.peer_health = PeerHealthProbe(now=lambda: datetime.fromisoformat(self.world["clock"].replace("Z", "+00:00")))
        # Frozen prompts name model-a/model-b while real encrypted source IDs
        # are random. Expose only that neutral identity mapping with the actual
        # model records. Never attach expected availability/sharing/answers.
        # Reference controllers must use this same wrapper, not a separate map
        # embedded in their prompt. Production tool results are otherwise intact.
        if not getattr(tools, "_query_identity_labels_installed", False):
            execute = tools.execute
            labels = {self.bindings[name]: name for name in self.world["models"]}
            if self.access_query and not self.access_query.owner_execution:
                labels = {self.access_query.consumer.selected: "model-a"}
            def identified(name, *args, **kwargs):
                result = execute(name, *args, **kwargs)
                if name == "ryn_list_models" and isinstance(result, dict):
                    result = copy.deepcopy(result)
                    for row in result.get("models", []):
                        if row.get("id") in labels:
                            row["fixture_identity"] = labels[row["id"]]
                return result
            tools.execute = identified
            tools._query_identity_labels_installed = True
        if self.access_query:
            self.access_query.attach(tools)

    def source_records(self):
        return self.sources.read()

    def collect(self):
        records = self.source_records()
        classification = {}
        for logical, expected in self.world["models"].items():
            source = records[self.bindings[expected["source_id"]]]
            model = expected["upstream_id"]
            classification[logical] = {"id": self.bindings[logical], "chat": source.get("status") == "ready",
                "agent": source.get("status") == "ready" and source.get("tool_support", {}).get(model) == "supported",
                "shared": model in self.sources.shared_models(source)}
        signed = self.store.registry.list_peers(network_id="rynmesh-main")
        return {"bindings": copy.deepcopy(self.bindings), "source_readback": classification,
                "identity_mapping_policy": "ryn_list_models adds fixture_identity only; frozen prompt, production facts, IDs, permissions and verification are unchanged. Reference must use the same wrapper.",
                "source_configuration_unchanged": hashlib.sha256(self.sources.path.read_bytes()).hexdigest() == self.sources_before,
                "setup_probes": copy.deepcopy(self.probes), "http_health_audit": copy.deepcopy(self.health_requests),
                "signed_registry_evidence": [record.to_dict() for record in signed],
                "semantic_review": "pending independent review; fixed loopback fixture is not an independent AI provider",
                **(self.access_query.collect() if self.access_query else {})}

    def execute(self, runtime):
        """Start the original prompt once; no follow-up hints or config writes."""
        self.attach(runtime.tools)
        if self.access_query:
            if runtime.consumer_node_id != self.bindings["node-b"]:
                raise RuntimeError("query_consumer_identity_mismatch")
            runtime.update_settings({"permission_mode": "read_only"})
        task = runtime.create({"message": self.case["prompt"], "model_id": self.selected})
        self.task_id = task["id"]
        if getattr(self, "on_task_created", None):
            self.on_task_created(task)
        for _ in range(160):
            task = runtime.get(task["id"])
            if task["status"] == "waiting_approval":
                runtime.action(task["id"], {"action": "deny", "approval_id": task["pending_approval"]["id"],
                                           "expected_control_revision": task["control_revision"]})
            elif task["status"] not in {"queued", "running"}:
                break
            else:
                runtime.run_once()
        return {"case_id": self.case["id"], "prompt": self.case["prompt"], "task": runtime.get(task["id"]),
                "formal_case_score": False, **self.collect()}
