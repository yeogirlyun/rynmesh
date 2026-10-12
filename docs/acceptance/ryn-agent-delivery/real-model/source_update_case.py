"""C02 development world: real probes, encrypted saves and owner conflict.

Logical versions 7/8/9 label actual opaque source revisions, never replace them.
No held-out fixtures or expected answers are exposed to the planner.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from contextlib import ExitStack
from pathlib import Path

from rynmesh.agent_runtime.store import encoded

ROOT = Path(__file__).resolve().parents[4]


class UpdateFixturePlanner:
    """Deterministic controller check only, never model effectiveness evidence."""
    def __init__(self, world):
        self.world, self.count = world, 0

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        message = {"role": "assistant", "content": "Requested candidate is unavailable; inspect actual source state."}
        if not specs:
            data = json.loads(messages[-1]["content"])
            message["content"] = json.dumps({"status": "failed", "summary": "Candidate probe failed.",
                "uncovered_requirements": ["Requested endpoint cannot be verified."],
                "evidence_refs": [e["id"] for e in data["evidence"]]})
        else:
            self.count += 1
            name, args = None, {}
            if self.count == 1 or self.count == 6 or self.count == 8:
                name = "ryn_list_source_configurations"
            elif self.count == 2:
                name, args = "ryn_request_input", {"goal_revision": 1, "kind": "credential_ref", "prompt": "Use secure local form.",
                    "fields": [{"id": "source", "label": "Source credential", "type": "credential_ref", "required": True}]}
            elif self.count == 3:
                name, args = "ryn_prepare_source_draft", {"source_id": self.world.source_id,
                    "configuration": {"name": "source-alpha", "kind": "local", "base_url": self.world.candidate.url,
                                      "models": ["alpha", "beta", "gamma"]}}
            elif self.count == 4 or (self.count == 5 and self.world.scenario == "post_save_conflict"):
                draft = json.loads(next(m["content"] for m in reversed(messages)
                    if m.get("role") == "tool" and "draft_id" in m.get("content", "")))
                name = "ryn_test_source_draft" if self.count == 4 else "ryn_commit_source_draft"
                args = {"draft_id": draft["draft_id"], "expected_draft_version": draft["version"]}
            if name:
                message.update(content=None, tool_calls=[{"id": "fixture-update-" + str(self.count), "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)}}])
        return {"message": message, "usage": {"input_tokens": 8, "output_tokens": 4}}


def load_case(scenario):
    suite = json.loads((ROOT / "docs/acceptance/ryn-agent-delivery/cases-v1.json").read_text(encoding="utf-8"))
    if suite.get("partition") != "development":
        raise ValueError("development_partition_required")
    case = next(c for c in suite["cases"] if c["id"] == "C02")
    return copy.deepcopy(next(c for c in case["setup"]["subscenarios"] if c["id"] == scenario))


class SourceUpdateWorld:
    def __init__(self, sources, scenario, *, initial_source=None):
        self.initial_source = copy.deepcopy(initial_source)
        self.sources, self.scenario, self.case = sources, scenario, load_case(scenario)
        self.stack, self.saves, self.interventions = ExitStack(), [], []
        self.logical_versions, self.source_id = {}, None

    def __enter__(self):
        spec = importlib.util.spec_from_file_location("update_source_fixture", ROOT / "tests/fixtures/agent_acceptance/source_fixture.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.original = self.stack.enter_context(module.FixtureSource())
        self.candidate = self.stack.enter_context(module.FixtureSource(
            models=("alpha", "beta", "gamma"), saved=lambda: bool(self.saves),
            failure_status=lambda: 503 if self.scenario == "pre_save_failure" or self.saves else None))
        self.newer = self.stack.enter_context(module.FixtureSource(models=("alpha", "beta", "gamma")))
        configuration = {"name": "source-alpha", "kind": "local", "base_url": self.original.url,
                         "api_key": self.original.secret, "models": ["alpha", "beta"]}
        if self.initial_source is None:
            saved = self.sources.save(self.sources.prepare(configuration))
        else:
            if (self.sources.read().get(self.initial_source["id"]) != self.initial_source
                    or self.initial_source.get("name") != "source-alpha" or self.initial_source.get("models") != ["alpha", "beta"]):
                raise ValueError("registered_source_initial_state_mismatch")
            # Explicit registered setup retains the original role-world identity
            # and sharing/grants, while realizing C02's controlled version 7 URL.
            saved = self.sources.save({**self.initial_source, **configuration})
        self.source_id = saved["id"]
        self.logical_versions[saved["source_revision"]] = 7
        self.initial = self.sources.read()[self.source_id]
        self.original_save = self.sources.save

        def tracked_save(record):
            value = self.original_save(record)
            if value["id"] == self.source_id:
                ordinal = 8 + len(self.saves)
                self.logical_versions[value["source_revision"]] = ordinal
                self.saves.append({"source_revision": value["source_revision"], "logical_version": ordinal,
                    "endpoint": self.endpoint_label(value["base_url"]), "models": value["models"],
                    "actor": "owner" if getattr(self, "owner_writing", False) else "agent"})
            return value
        self.sources.save = tracked_save
        self.stack.callback(setattr, self.sources, "save", self.original_save)
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def endpoint_label(self, url):
        return {self.original.url: "endpoint-original", self.candidate.url: "endpoint-candidate",
                self.newer.url: "endpoint-owner-newer"}.get(url, "unrecognized")

    def attach(self, tools):
        original_list = tools.source_drafts.list_sources

        def listed():
            result = original_list()
            for row in result["sources"]:
                if row["source_id"] == self.source_id:
                    row["fixture_identity"] = {"name": "source-alpha",
                        "source_version": self.logical_versions.get(row["source_revision"]),
                        "endpoint": self.endpoint_label(row["base_url"])}
            return result
        tools.source_drafts.list_sources = listed
        original_verify = tools.verify

        def verify(receipt, criteria, *, context, cancelled):
            result = original_verify(receipt, criteria, context=context, cancelled=cancelled)
            evidence = result.get("evidence", {})
            if (self.scenario == "post_save_conflict" and not self.interventions
                    and receipt.get("tool") == "ryn_commit_source_draft"
                    and evidence.get("source_id") == self.source_id
                    and evidence.get("post_save_retest", {}).get("chat_passed") is False):
                before = self.sources.read()[self.source_id]
                self.owner_writing = True
                try:
                    saved = self.sources.save(self.sources.prepare({"base_url": self.newer.url,
                        "api_key": self.newer.secret}, self.source_id))
                finally:
                    self.owner_writing = False
                self.interventions.append({"actor": "owner", "point": "after retest failure before recovery",
                    "before_revision": before["source_revision"], "after_revision": saved["source_revision"],
                    "endpoint": "endpoint-owner-newer", "logical_version": self.logical_versions[saved["source_revision"]]})
            return result
        tools.verify = verify

    def approve(self, approval):
        preview = approval.get("preview", {})
        config = preview.get("configuration", {})
        current = self.sources.read()[self.source_id]
        expected = self.sources.prepare({"base_url": self.candidate.url, "api_key": self.candidate.secret,
            "models": ["alpha", "beta", "gamma"]}, self.source_id)
        from rynmesh.agent_runtime.source_drafts import CONFIG_FIELDS
        return (not self.saves and self.scenario == "post_save_conflict"
            and approval.get("tool") == "ryn_commit_source_draft" and preview.get("source_id") == self.source_id
            and preview.get("expected_source_revision") == current["source_revision"]
            and config == {k: expected[k] for k in CONFIG_FIELDS}
            and preview.get("test", {}).get("chat_passed") is True)

    def evidence(self):
        current = self.sources.read()[self.source_id]
        version = self.logical_versions.get(current["source_revision"])
        return {"source_id": self.source_id, "source_version": version, "source_revision": current["source_revision"],
            "endpoint": self.endpoint_label(current["base_url"]), "models": current["models"],
            "shared_models": current["shared_models"], "original_unchanged": encoded(current) == encoded(self.initial),
            "config_saves": len([s for s in self.saves if s["actor"] == "agent"]),
            "saves": copy.deepcopy(self.saves), "owner_interventions": copy.deepcopy(self.interventions),
            "candidate_audit": copy.deepcopy(self.candidate.audit),
            "old_destination_generation_invalidated": current["destination_revision"] != self.initial["destination_revision"],
            "rollback_conflict_rejected": None}
