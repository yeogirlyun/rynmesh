"""Exact continuation values survive lossy summaries without becoming authority."""
import copy
import json

import pytest
from test_agent_resources import file_ref, invoke, resources  # noqa: F401
from test_agent_runtime import advance, assistant, make_runtime
from test_agent_tool_menu import ManyTools

from rynmesh.agent_runtime import checkpoint_refs
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, AgentToolRejected, encoded


def exchange(reference, name="ryn_list_resources", content=None):
    return [assistant(name=name, args={"resource_ref": reference}, call_id="read")["message"],
            {"role": "tool", "tool_call_id": "read", "content": json.dumps(content or {
                "entries": [{"name": "notes.md", "resource_ref": reference}], "next_cursor": ""})}]


def test_projection_is_lossless_data_only_and_short_label_cannot_read(resources):  # noqa: F811
    tools, _, context, state = resources
    reference = file_ref(tools, context)
    original = [{"role": "user", "content": reference}, *exchange(reference,
        content={"resource_ref": reference, "text": json.dumps({"resource_ref": reference}), "next_cursor": ""})]
    before = copy.deepcopy(original)
    messages, refs = checkpoint_refs.project(original)
    short = checkpoint_refs.label(reference)
    assert refs == {short: reference} and original == before
    assert messages[0] == original[0]  # Owner text is never rewritten.
    result = json.loads(messages[-1]["content"])
    assert result["resource_ref"] == short and result["text"] == json.loads(original[-1]["content"])["text"]
    assert result["next_cursor"] == ""
    with pytest.raises(AgentToolRejected):
        invoke(tools, context, "ryn_read_resource", short)
    assert not state["reads"]
    assert invoke(tools, context, "ryn_read_resource", refs[short])["text"]
    untouched, absent = checkpoint_refs.project(exchange(reference, name="untrusted_other_tool"))
    assert untouched == exchange(reference, name="untrusted_other_tool") and not absent


def test_runtime_keeps_exact_refs_outside_summary_and_across_encrypted_restart(tmp_path):
    reference = "gAAAA" + "R" * 700
    short = checkpoint_refs.label(reference)
    runtime, model, _, key = make_runtime(tmp_path, assistant(f"Read {short}; no writes."), assistant("Observed only."))
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    row = runtime.create({"message": "Inspect only this document. Never modify anything."})
    history = exchange(reference, content={"resource_ref": reference, "text": "e" * 8100})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["messages"].extend(history))
    original = runtime.store.read()["tasks"][row["id"]]["messages"]
    advance(runtime)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["context_checkpoint"]["references"] == {short: reference}
    assert reference not in json.dumps(model.calls[0][1])
    assert reference in json.dumps(model.calls[-1][1])
    assert saved["messages"][:len(original)] == original
    assert all(len(encoded(call[1])) + len(encoded(call[2])) <= 12000 for call in model.calls)
    restored = AgentRuntime(AgentStore(tmp_path, key), model, runtime.tools, runtime.catalog)
    assert reference in json.dumps(restored._context(restored.store.read()["tasks"][row["id"]]))
    assert reference not in (tmp_path / "sessions.json").read_text(encoding="utf-8")


def test_reference_map_cannot_grow_without_limit_or_silently_lose_values():
    references = {checkpoint_refs.label("gAAAA" + str(i).zfill(80)): "gAAAA" + str(i).zfill(80) for i in range(128)}
    with pytest.raises(AgentError, match="agent_context_limit"):
        checkpoint_refs.project(exchange("gAAAA" + "N" * 80), references)
    assert len(references) == 128
    for malformed in ([], {"wrong-label": "gAAAA" + "R" * 80}):
        with pytest.raises(AgentError, match="agent_checkpoint_references_invalid"):
            checkpoint_refs.project([], malformed)


def test_pinned_references_count_toward_hard_context_limit_before_inference(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    row = runtime.create({"message": "Inspect the documents without changing them."})
    history = exchange("gAAAA" + "A" * 4000, content={"entries": [
        {"resource_ref": "gAAAA" + character * 4000} for character in "ABCD"]})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["messages"].extend(history))
    advance(runtime)
    assert runtime.get(row["id"])["error"] == "agent_context_limit"
    assert model.calls == []
    assert runtime.store.read()["tasks"][row["id"]]["messages"][1:] == history


def test_schema_cache_yields_to_exact_references_without_dropping_evidence(tmp_path):
    tools = ManyTools()
    names = [spec["function"]["name"] for spec in tools.specs()][:8]
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant(name="ryn_discover_tools", args={"load": names}),
        assistant("Three authorized resources were observed; no mutation occurred."), assistant("Inspection recorded."))
    runtime.tools = tools
    runtime.update_settings({"max_context_chars": 12000})
    row = runtime.create({"message": "Inspect the documents. Never share or modify them."})
    advance(runtime, 2)
    # The credential-input contract also occupies the fixed 12 KB context.
    # Leave room for that immutable schema while still forcing cache release.
    references = ["gAAAA" + character * 1400 for character in "ABC"]
    history = exchange(references[0], content={"entries": [{"resource_ref": ref} for ref in references], "text": "e" * 1400})
    runtime.store.transaction(lambda data: data["tasks"][row["id"]]["messages"].extend(history))
    advance(runtime)
    saved = runtime.store.read()["tasks"][row["id"]]
    assert saved["status"] == "completed", saved.get("error")
    assert saved["tool_menu"]["loaded_names"] == []
    assert set(saved["context_checkpoint"]["references"].values()) == set(references)
    assert all(ref in json.dumps(model.calls[-1][1]) for ref in references)
    assert saved["messages"][-3:-1] == history
    assert any(event["type"] == "tool_menu_changed" and event.get("reason") == "context_space" for event in saved["events"])
    assert not runtime.tools.calls
    assert all(len(encoded(call[1])) + len(encoded(call[2])) <= 12000 for call in model.calls)
