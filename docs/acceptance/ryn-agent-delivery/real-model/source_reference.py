"""Offline native source protocol fixture; never used by a live planner."""
import json
import uuid


class NativeSourceFixturePlanner:
    supports_destination_binding = True

    def __init__(self, case_id):
        self.case_id = case_id

    def complete(self, model_id, messages, specs, cancelled, on_event=None, **options):
        from importlib import import_module
        helper = import_module("docs.acceptance.ryn-agent-delivery.real-model.planner_discovery")
        business_history, discover = helper.business_history, helper.discover
        discovery = discover(specs, ['ryn_request_input', 'ryn_list_source_configurations', 'ryn_prepare_source_draft', 'ryn_test_source_draft', 'ryn_commit_source_draft'])
        if discovery:
            return discovery
        messages = business_history(messages)
        def answer(text):
            return {"message": {"role": "assistant", "content": text},
                    "usage": {"input_tokens": 8, "output_tokens": 4}, "finish_reason": "stop"}
        if not specs:
            assert "Verify whether the active session goal" in messages[-1]["content"]
            return answer(json.dumps({"passed": True, "reason": "Synthetic protocol fixture, not a quality judgment.", "nextAction": ""}))
        names = {c["id"]: c["function"]["name"] for m in messages for c in m.get("tool_calls", [])}
        receipts = [(names[m["tool_call_id"]], json.loads(m["content"])) for m in messages if m["role"] == "tool"]
        latest = dict(receipts)
        def call(name, args):
            return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": uuid.uuid4().hex,
                "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
                "usage": {"input_tokens": 8, "output_tokens": 4}, "finish_reason": "tool_calls"}
        if self.case_id == "C02" and "ryn_list_source_configurations" not in latest:
            return call("ryn_list_source_configurations", {})
        if "ryn_request_input" not in latest:
            return call("ryn_request_input", {"goal_revision": 1, "kind": "credential_ref", "prompt": "Use secure local form.",
                "fields": [{"id": "source", "label": "Source credential", "type": "credential_ref", "required": True}]})
        if "ryn_prepare_source_draft" not in latest:
            credential = latest["ryn_request_input"]["credential"]
            configuration = {"name": "source-new" if self.case_id == "C01" else "source-alpha",
                "kind": credential["kind"], "base_url": credential["base_url"],
                "models": ["alpha", "beta"] if self.case_id == "C01" else ["alpha", "beta", "gamma"]}
            args = {"configuration": configuration}
            if self.case_id == "C02":
                args["source_id"] = next(r["source_id"] for r in latest["ryn_list_source_configurations"]["sources"] if r["name"] == "source-alpha")
            return call("ryn_prepare_source_draft", args)
        draft = latest["ryn_prepare_source_draft"]
        locator = {"draft_id": draft["draft_id"], "expected_draft_version": draft["version"]}
        if "ryn_test_source_draft" not in latest:
            return call("ryn_test_source_draft", {**locator, "test_tools": self.case_id == "C01"})
        if latest["ryn_test_source_draft"]["test"]["chat_passed"] and "ryn_commit_source_draft" not in latest:
            return call("ryn_commit_source_draft", locator)
        if self.case_id == "C02" and sum(name == "ryn_list_source_configurations" for name, _ in receipts) < 2:
            return call("ryn_list_source_configurations", {})
        return answer("Synthetic source workflow ended; actual test results and source readbacks determine observed state.")
