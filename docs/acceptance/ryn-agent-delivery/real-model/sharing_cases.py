"""C03 development controller: original prompt, exact approvals, signed receivers.

Peers/catalog exchange are authenticated in-memory fixtures on one host. Model-b
inference reaches the actual loopback synthetic HTTP source; it is not another
independent LLM. No frozen case or permission is changed to fit the implementation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

from query_cases import ROOT, QueryWorld

from rynmesh.ai_access.catalog import PATH, FriendAICatalog
from rynmesh.ai_access.routes import AIAccessState
from rynmesh.ai_access.store import AIAccessStore
from rynmesh.crypto import canonical_json
from rynmesh.friends.service import FriendService
from rynmesh.llm_package.peer_binding import expected
from rynmesh.llm_package.routes import _expires
from rynmesh.llm_package.task_protocol import TaskProtocolError, open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


class SharingWorld(QueryWorld):
    def __init__(self, home, case_id):
        if case_id not in {"C03", "R02"}:
            raise ValueError("development_sharing_case_unsupported")
        super().__init__(home, "Q01")
        suite = json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))
        if suite.get("partition") != "development":
            raise ValueError("development_partition_required")
        self.case = copy.deepcopy(next(c for c in suite["cases"] if c["id"] == case_id))
        self.approvals, self.peer_probes, self.catalog_audit = [], [], []

    def configure(self, app, store):
        selected = super().configure(app, store)
        if self.case["setup"]["world_overrides"].get("all_models_private"):
            for record in self.sources.read().values():
                record["shared_models"], record["sharing"] = [], False
                self.sources.save(record)
        self.attach_services(app, store)
        self.initial_sources = copy.deepcopy(self.sources.read())
        self.sources_before = hashlib.sha256(self.sources.path.read_bytes()).hexdigest()
        assert not self.grants.list() and not self.receiver_catalog.refresh(store.peer_id)["services"]
        return selected

    def reopen(self, app, store, bindings):
        """Attach a replacement consumer to existing state; never seed or grant."""
        self.app, self.store, self.bindings = app, store, copy.deepcopy(bindings)
        self.sources = app.state.llm_agent_services["source_store"]
        self.attach_services(app, store, reopen=True)

    def attach_services(self, app, store, *, reopen=False):
        self.nodes = {}
        def post(endpoint, path, body, headers, **_):
            target = self.nodes[endpoint]
            if path == "/api/peer/friends/accept":
                return target.accept(body)
            relation = target.verify_request(path=path, body=canonical_json(body), headers=headers)
            if path != PATH or target is not self.owner:
                raise RuntimeError("fixture_unknown_peer_route")
            self.catalog_audit.append({"receiver_peer_id": relation["peer_id"], "authenticated": True})
            return self.publisher.respond(body, relation)
        def node(node_store, name, port):
            key = peer_box.load_or_create_messaging_key(node_store.home / "messaging.x25519")
            endpoint = "http://127.0.0.1:" + str(port)
            service = FriendService(home=node_store.home, peer_id=node_store.peer_id, node_name=name, endpoint=endpoint,
                identity_private=node_store.private_key_bytes, messaging_private=key, post_json=post, allow_loopback=True)
            self.nodes[endpoint] = service
            return service
        self.owner = node(store, "owner-provider", 18911)
        self.receiver_store = RynmeshStore(home=self.home / "receiver", network_dir=self.home / "network")
        self.other_store = RynmeshStore(home=self.home / ("node-c" if getattr(self, "registered_roles", False) else "unauthorized-receiver"), network_dir=self.home / "network")
        self.receiver = node(self.receiver_store, "node-b", 18912)
        self.other = node(self.other_store, "ungranted-peer", 18913)
        if not reopen:
            for receiver in (self.receiver, self.other):
                receiver.join(self.owner.create_invite()["invite_uri"])
        app.state.friends = SimpleNamespace(service=self.owner)
        self.grants = AIAccessStore(store.home, relationship=self.owner.store.relationship, sources=self.sources)
        app.state.ai_access = AIAccessState(self.grants, lambda _: None)
        self.services = app.state.llm_agent_services
        self.publisher = FriendAICatalog(friends=lambda: self.owner, grants=lambda: self.grants,
            provider=lambda: [p.public_status() for p in self.services["active_source_managers"]().values()])
        self.receiver_catalog = FriendAICatalog(friends=lambda: self.receiver, grants=lambda: None, provider=lambda: None)
        self.other_catalog = FriendAICatalog(friends=lambda: self.other, grants=lambda: None, provider=lambda: None)
        if reopen and self.bindings["node-b"] != self.receiver.peer_id:
            raise RuntimeError("fixture_receiver_identity_changed")
        self.bindings["node-b"] = self.receiver.peer_id
        self.rid = self.owner.store.relationship_for_peer(self.receiver.peer_id)["relationship_id"]
        self.sid = self.bindings["model-b"].removeprefix("local/")

    def permitted(self, approval):
        """An exact model/receiver allowlist, never a tool-wide write authorization."""
        name, args, preview = approval["tool"], approval["arguments"], approval["preview"]
        if name == "ryn_share_model_with_friend":
            after = preview.get("after", {})
            recipients = after.get("recipients", [])
            return (args == {"model_id": self.bindings["model-b"], "relationship_id": self.rid}
                    and preview.get("model_id") == self.bindings["model-b"] and preview.get("service_id") == self.sid
                    and after.get("sharing") is True and after.get("scope") == "friends"
                    and len(recipients) == 1 and recipients[0].get("relationship_id") == self.rid
                    and recipients[0].get("peer_id") == self.receiver.peer_id)
        if name == "ryn_set_default_agent_model":
            return self.case["id"] == "C03" and args.get("model_id") == self.bindings["model-a"] and preview.get("applies_to") == "future_tasks_only"
        if name == "ryn_set_friend_model_access":
            return (args.get("service_id") == self.sid and args.get("relationship_id") == self.rid
                    and args.get("allowed") is True and preview.get("peer_id") == self.receiver.peer_id)
        if name == "ryn_set_model_sharing":
            if args.get("model_id") != self.bindings["model-b"] or preview.get("scope") != "friends":
                return False
            grants = preview.get("allowed_friend_grants", [])
            exact = all(g.get("peer_id") == self.receiver.peer_id and g.get("relationship_id") == self.rid for g in grants)
            return exact and (args.get("enabled") is False or
                args.get("enabled") is True and len(grants) == 1 and grants[0].get("effective") is True)
        return False

    def probe_receiver(self, logical, sender, row=None):
        sid = self.bindings[logical].removeprefix("local/")
        request_id = "receiver-probe-" + uuid.uuid4().hex
        grant = self.grants.grant(sid, self.rid)
        permission = {"relationship_id": self.rid, "revision": (grant or {}).get("revision", 0)}
        body = {"task_id": request_id, "service_id": sid, "max_tokens": 10, "max_amount": .01,
            "chat": {"messages": [{"role": "user", "content": "Synthetic receiver authorization probe"}], "max_tokens": 10},
            "ai_permission": permission, "reply_messaging_pub": peer_box.public_key_b64(sender.messaging_private)}
        if row:
            body["expected_model_binding"] = expected(row, sender.peer_id)
        wire = seal_task(body=body, task_id=request_id, kind="llm_request", sender_peer_id=sender.peer_id,
            recipient_peer_id=self.store.peer_id, sender_signing_key=sender.identity_private,
            recipient_messaging_pub=peer_box.public_key_b64(self.owner.messaging_private), expires_at=_expires(300)).to_dict()
        before = len(self.upstream.audit)
        result = {"model": logical, "receiver_peer_id": sender.peer_id, "signed_request": True,
                  "result": "unknown", "upstream_requests": 0}
        try:
            service = self.services["provider_for"](sid)
            if service is None:
                result["error_code"] = "service_unavailable"
            else:
                observer = getattr(self, "receiver_dispatch", None)
                reply = (observer(request_id, wire, lambda: service.handle(wire)) if observer is not None
                         else service.handle(wire))
                _, payload = open_task(reply, recipient_peer_id=sender.peer_id,
                    recipient_messaging_key=sender.messaging_private, expected_kind="llm_response")
                result.update(state=payload.get("state"), error_code=payload.get("error_code"), response_authenticated=True)
                if payload.get("state") == "succeeded":
                    result["result"] = "allowed"
                elif payload.get("state") == "rejected" and payload.get("error_code") in {"ai_permission_denied", "service_paused"}:
                    result["result"] = "denied"
        except TaskProtocolError as exc:
            # This exact production admission rejection is evidence; other failures are unknown.
            if str(exc) == "This device is not allowed to use this personal-space AI service.":
                result.update(result="denied", error_code="personal_space_access_denied")
            else:
                result["error_type"] = type(exc).__name__
        except Exception as exc:
            result["error_type"] = type(exc).__name__
        result["upstream_requests"] = len(self.upstream.audit) - before
        self.peer_probes.append(result)
        return result

    def collect(self):
        evidence = super().collect()
        current = self.sources.read()
        from rynmesh.llm_package.model_settings import effective
        grants = self.grants.list()
        for logical, facts in self.world["models"].items():
            row = evidence["source_readback"][logical]
            source = current[self.bindings[facts["source_id"]]]
            row["share_scope"] = effective(source, facts["upstream_id"])["share_scope"]
            if not row["shared"]:
                row["receivers"] = []
            elif row["share_scope"] == "friends":
                row["receivers"] = sorted("node-b" if g["peer_id"] == self.receiver.peer_id else g["peer_id"]
                    for g in grants if g["service_id"] == row["id"].removeprefix("local/") and g["effective"])
        unchanged = True
        for sid, before in self.initial_sources.items():
            after = current.get(sid, {})
            excluded = {"source_revision", "shared_models", "sharing", "model_settings", "_friend_grants", "_sharing_operations"}
            unchanged &= {k: v for k, v in before.items() if k not in excluded} == {k: v for k, v in after.items() if k not in excluded}
            # The target may gain an atomic receipt/grant; sibling grants and
            # sharing flags must still remain identical, including initial A.
            unchanged &= {k: v for k, v in before.get("_friend_grants", {}).items() if k != self.sid} == {
                k: v for k, v in after.get("_friend_grants", {}).items() if k != self.sid}
            for model, settings in before["model_settings"].items():
                if "local/" + self.sources.model_service_id(before, model) != self.bindings["model-b"]:
                    unchanged &= after.get("model_settings", {}).get(model) == settings
                    unchanged &= (model in self.sources.shared_models(before)) == (model in self.sources.shared_models(after))
        evidence.update(approvals=copy.deepcopy(self.approvals), receiver_probes=copy.deepcopy(self.peer_probes),
            actual_source_http_audit=copy.deepcopy(self.upstream.audit), catalog_authentication=copy.deepcopy(self.catalog_audit),
            friend_grants=grants, sibling_and_source_configuration_unchanged=bool(unchanged),
            physical_hosts=1, initial_all_models_private=all(not r["shared_models"] for r in self.initial_sources.values()))
        return evidence

    def execute(self, runtime):
        if self.case["id"] != "C03":
            raise ValueError("sharing_recovery_requires_process_controller")
        self.attach(runtime.tools)
        settings_before = runtime.settings()
        task = runtime.create({"message": self.case["prompt"], "model_id": self.bindings["model-a"]})
        if callable(getattr(self, "on_task_created", None)):
            self.on_task_created(task)
        self.task_id = task["id"]
        for _ in range(160):
            task = runtime.get(task["id"])
            if task["status"] == "waiting_approval":
                approval = task["pending_approval"]
                allowed = self.permitted(approval)
                self.approvals.append({"approved": allowed, "approval": copy.deepcopy(approval)})
                runtime.action(task["id"], {"action": "approve" if allowed else "deny", "approval_id": approval["id"],
                                           "expected_control_revision": task["control_revision"]})
            elif task["status"] not in {"queued", "running"}:
                break
            else:
                runtime.run_once()
        return self.collect_postconditions(runtime, task["id"], settings_before)

    def collect_postconditions(self, runtime, task_id, settings_before):
        """Same signed probes/readbacks for candidate and native controllers."""
        rows = self.receiver_catalog.refresh(self.store.peer_id)["services"]
        visible = [name for name in self.world["models"] if any(
            r["service"]["package_id"] == self.bindings[name].removeprefix("local/") for r in rows)]
        row_b = next((r for r in rows if r["service"]["package_id"] == self.sid), None)
        self.probe_receiver("model-b", self.receiver, row_b)
        self.probe_receiver("model-a", self.receiver)
        self.probe_receiver("model-b", self.other)  # Known grant copied by the wrong signed identity.
        after = runtime.settings()
        return {"case_id": self.case["id"], "prompt": self.case["prompt"], "task": runtime.get(task_id),
            "formal_case_score": False, "settings_before": settings_before, "settings_after": after,
            "receiver_visible_models": visible, "ungranted_receiver_visible_models": self.other_catalog.refresh(self.store.peer_id)["services"],
            "settings_except_default_unchanged": {k: v for k, v in settings_before.items() if k != "model_id"} ==
                {k: v for k, v in after.items() if k != "model_id"}, **self.collect()}


class SharingFixturePlanner:
    """Only exercises the controller/runtime; contains synthetic scripted choices."""
    supports_destination_binding = True

    def __init__(self, cohort="ryn"):
        self.cohort = cohort

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        from importlib import import_module
        helper = import_module("docs.acceptance.ryn-agent-delivery.real-model.planner_discovery")
        business_history, discover = helper.business_history, helper.discover
        discovery = discover(specs, ['ryn_list_models', 'ryn_get_agent_configuration', 'ryn_list_devices', 'ryn_set_default_agent_model', 'ryn_set_model_sharing', 'ryn_set_friend_model_access'])
        if discovery:
            return discovery
        messages = business_history(messages)
        def answer(text):
            return {"message": {"role": "assistant", "content": text}, "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "stop"}
        if not specs:
            if self.cohort == "reference":
                assert "Verify whether the active session goal" in messages[-1]["content"]
                return answer(json.dumps({"passed": True, "reason": "Synthetic protocol fixture only; not a quality verdict.", "nextAction": ""}))
            evidence = json.loads(messages[-1]["content"])["evidence"]
            return answer(json.dumps({"status": "unknown", "summary": "Synthetic controller only, not quality evidence.",
                "uncovered_requirements": ["independent semantic review"], "evidence_refs": [r["id"] for r in evidence]}))
        names = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls", [])}
        receipts = {names.get(m.get("tool_call_id")): json.loads(m["content"]) for m in messages if m["role"] == "tool"}
        def call(name, args):
            return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": uuid.uuid4().hex,
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
                "usage": {"input_tokens": 1, "output_tokens": 1}, "finish_reason": "tool_calls"}
        for name in ("ryn_list_models", "ryn_get_agent_configuration", "ryn_list_devices"):
            if name not in receipts:
                return call(name, {})
        models = {m["fixture_identity"]: m for m in receipts["ryn_list_models"]["models"]}
        if "ryn_set_default_agent_model" not in receipts:
            return call("ryn_set_default_agent_model", {"model_id": models["model-a"]["id"],
                "expected_settings_revision": receipts["ryn_get_agent_configuration"]["settings_revision"]})
        if "ryn_set_model_sharing" not in receipts:
            return call("ryn_set_model_sharing", {"model_id": models["model-b"]["id"], "enabled": False, "scope": "friends"})
        if "ryn_set_friend_model_access" not in receipts:
            friend = next(f for f in receipts["ryn_list_devices"]["friends"] if f["node_name"] == "node-b")
            return call("ryn_set_friend_model_access", {"service_id": models["model-b"]["id"].removeprefix("local/"),
                "relationship_id": friend["relationship_id"], "allowed": True, "expected_revision": 0})
        if not receipts["ryn_set_model_sharing"].get("sharing"):
            return call("ryn_set_model_sharing", {"model_id": models["model-b"]["id"], "enabled": True, "scope": "friends"})
        for name in ("ryn_list_models", "ryn_get_agent_configuration"):
            if list(names.values()).count(name) < 2:
                return call(name, {})
        return answer("Synthetic control flow finished. Receiver probes are separate evidence, not a real-model success claim.")
