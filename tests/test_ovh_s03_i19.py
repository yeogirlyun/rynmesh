"""S03-I19: budgeted resource pages and real approved UTF-8 creates."""
import hashlib
from types import SimpleNamespace

import pytest
from jsonschema import ValidationError, validate

from agent_tool_fixtures import expose
from test_agent_resource_writes import writable as writable_fixture, execute
from test_agent_runtime import advance, approve, assistant
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected, encoded
from rynmesh.services import peer_box

writable = writable_fixture


def _runtime(writable, budget=20000):
    tools, _, _, _, source, _, _, _, store = writable

    class Model:
        supports_destination_binding = True

        def __init__(self):
            self.responses = []

        def complete(self, model_id, messages, specs, cancelled, on_event, **options):
            return self.responses.pop(0) if self.responses else assistant("Done")

    model = Model()
    catalog = SimpleNamespace(list_models=lambda: [{"id": "fixture", "source": "local_api", "available": True,
        "agent_supported": True, "peer_id": "local", "source_id": "fixture", "destination_revision": "rev1",
        "context_window_tokens": 128000}])
    key = peer_box.load_or_create_messaging_key(store.home / "messaging.x25519")
    runtime = AgentRuntime(AgentStore(store.home / "agent", key), model, tools, catalog, consumer_node_id="local")
    runtime.update_settings({"enabled": True, "model_id": "fixture", "max_output_chars": budget})
    parent = runtime.create({"message": "Read or create the requested files", "scope": {"resources": [
        {"connection_id": source["id"], "root_path": "allowed", "recursive": True,
         "actions": ["list", "read_ai", "create_file"]}], "allow_model_data": True},
        "expected_destination_revision": "rev1"})
    row = runtime.store.read()["tasks"][parent["id"]]
    return runtime, model, row, runtime._execution_context(row)


def _call(tools, context, name, **args):
    return tools.execute(name, args, operation_id="read-page", context=context, cancelled=lambda: False)


def _assert_text(actual, expected):
    assert len(actual) == len(expected)
    assert hashlib.sha256(actual.encode()).digest() == hashlib.sha256(expected.encode()).digest()


def _data(writable):
    root = writable[6] / "allowed"
    text = "前言资料\n" * 1400 + ("needle " + "中文证据" * 100 + "\n") * 30
    (root / "000-source.md").write_bytes(text.encode("utf-8"))
    for index in range(60):
        (root / f"entry-{index:02d}.md").write_text(str(index), encoding="utf-8")
    return text


@pytest.mark.parametrize("budget,entries,matches,characters", [
    (20000, 50, 16, 5000), (4000, 10, 6, 2000), (40000, 50, 20, 10000),
])
def test_t1_t2_real_context_controls_all_three_page_sizes(writable, budget, entries, matches, characters):
    tools = writable[0]
    expected = _data(writable)
    runtime, _, row, context = _runtime(writable, budget)
    assert context["result_budget_bytes"] == budget
    assert runtime._execution_context(row, cheap_only=True)["result_budget_bytes"] == budget
    ref = context["resource_grants"][0]["root_resource_ref"]
    listing = _call(tools, context, "ryn_list_resources", resource_ref=ref)
    assert len(listing["entries"]) == entries and listing["next_cursor"]
    source = next(e["resource_ref"] for e in listing["entries"] if e["name"] == "000-source.md")
    read = _call(tools, context, "ryn_read_resource", resource_ref=source)
    assert len(read["text"]) == characters
    _assert_text(read["text"], expected[:characters])
    assert read["characters_returned"] == characters and read["next_cursor"]
    found = _call(tools, context, "ryn_search_resource", resource_ref=source, query="needle")
    assert len(found["matches"]) == matches and found["next_cursor"]
    assert found["matches"][0]["match_offset"] == expected.index("needle")
    if budget == 20000:
        assert len(encoded(read)) < budget


def test_t3_old_listing_read_and_search_cursors_continue_under_new_budget(writable):
    tools, old_context, args = writable[:3]
    text = _data(writable)
    root = args["directory_ref"]
    # No budget is the pre-change execution-context shape. Preserve its paging
    # and issue genuine encrypted cursors before introducing the new budget.
    old_listing = _call(tools, old_context, "ryn_list_resources", resource_ref=root)
    source = next(e["resource_ref"] for e in old_listing["entries"] if e["name"] == "000-source.md")
    old_read = _call(tools, old_context, "ryn_read_resource", resource_ref=source)
    old_search = _call(tools, old_context, "ryn_search_resource", resource_ref=source, query="needle")
    old_characters = old_read["characters_returned"]
    assert 0 < old_characters <= 8000
    _assert_text(old_read["text"], text[:old_characters])
    assert len(old_search["matches"]) == 6
    context = {**old_context, "result_budget_bytes": 20000}
    listing = _call(tools, context, "ryn_list_resources", resource_ref=root, cursor=old_listing["next_cursor"])
    assert [e["name"] for e in old_listing["entries"] + listing["entries"]] == [
        "000-source.md", *[f"entry-{i:02d}.md" for i in range(60)]]
    read = _call(tools, context, "ryn_read_resource", resource_ref=source, cursor=old_read["next_cursor"])
    assert read["character_offset"] == old_characters
    assert len(read["text"]) == 5000
    _assert_text(read["text"], text[old_characters:old_characters + 5000])
    found = _call(tools, context, "ryn_search_resource", resource_ref=source, query="needle", cursor=old_search["next_cursor"])
    assert len(found["matches"]) == 16
    assert found["matches"][0]["match_offset"] > old_search["matches"][-1]["match_offset"]


def test_t4_64_kib_csv_real_model_approval_preserves_full_preview_and_writes_once(writable):
    tools, _, _, _, _, server, root = writable[:7]
    runtime, model, row, context = _runtime(writable)
    body = "a,b\n" * (64 * 1024 // 4)
    args = {"directory_ref": context["resource_grants"][0]["root_resource_ref"], "name": "approved.csv", "content": body}
    expose(runtime, row["id"], ["ryn_create_text_file"])
    model.responses.append(assistant(name="ryn_create_text_file", args=args))
    advance(runtime, 2)
    waiting = runtime.get(row["id"])
    assert waiting["status"] == "waiting_approval"
    _assert_text(waiting["pending_approval"]["preview"]["proposed_content"], body)
    assert waiting["pending_approval"]["preview"]["bytes"] == 65536
    assert not (root / "allowed/approved.csv").exists()
    approve(runtime, row["id"])
    advance(runtime)
    assert (root / "allowed/approved.csv").read_bytes() == body.encode()
    assert len([event for event in server.audit if event["method"] == "PUT"]) == 1
    receipt = next(r for r in runtime.store.read()["tasks"][row["id"]]["receipts"] if r["tool"] == "ryn_create_text_file")
    assert receipt["result"]["content_sha256"] == hashlib.sha256(body.encode()).hexdigest()


@pytest.mark.parametrize("name,content,error", [
    ("over.csv", "x" * 65537, "agent_tool_arguments_invalid"),
    ("binary.exe", "text", "agent_resource_format_unsupported"),
    ("wide.csv", "中" * 21846, "agent_resource_size_limit"),
], ids=["over-64k", "binary-suffix", "utf8-byte-limit"])
def test_t4_rejected_size_is_utf8_bytes_and_binary_suffix_still_rejected(writable, name, content, error):
    tools, context, args, _, _, server = writable[:6]
    with pytest.raises(AgentToolRejected, match=error):
        execute(tools, context, {**args, "name": name, "content": content})
    with pytest.raises(AgentToolRejected, match=("agent_resource_format_unsupported" if name.endswith(".exe")
                                               else "agent_resource_size_limit")):
        tools.resource_writes.target("ryn_create_text_file", {**args, "name": name, "content": content}, context)
    assert not any(event["mutation"] for event in server.audit)


@pytest.mark.parametrize("suffix", [".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".log", ".html"])
def test_t4_all_inline_text_suffixes_are_accepted(writable, suffix):
    tools, context, args = writable[:3]
    result = execute(tools, context, {**args, "name": "document" + suffix, "content": "中文文本"})
    assert result["bytes"] == len("中文文本".encode())


def test_registry_schema_descriptions_and_large_page_return_path(writable):
    tools, context, args = writable[:3]
    by_name = {s["function"]["name"]: s["function"] for s in tools.specs()}
    schema = by_name["ryn_create_text_file"]["parameters"]
    assert schema["properties"]["content"]["maxLength"] == 65536
    validate({**args, "name": "limit.csv", "content": "x" * 65536}, schema)
    with pytest.raises(ValidationError):
        validate({**args, "content": "x" * 65537}, schema)
    description = by_name["ryn_create_text_file"]["description"]
    assert "64 KiB" in description and "workspace_path" in description and ".csv" in description
    for name in ("ryn_list_resources", "ryn_read_resource", "ryn_search_resource"):
        description = by_name[name]["description"]
        assert "output limit" in description and "next_cursor" in description
    text = "😀" * 24000
    (writable[6] / "allowed/wide.md").write_text(text, encoding="utf-8")
    listed = _call(tools, context, "ryn_list_resources", resource_ref=args["directory_ref"])
    source = next(e["resource_ref"] for e in listed["entries"] if e["name"] == "wide.md")
    large = {**context, "result_budget_bytes": 80000}
    first = _call(tools, large, "ryn_read_resource", resource_ref=source)
    _assert_text(first["text"], text[:20000])
    assert first["characters_returned"] == 20000
    second = _call(tools, large, "ryn_read_resource", resource_ref=source, cursor=first["next_cursor"])
    _assert_text(second["text"], text[20000:])
    assert second["character_offset"] == 20000
    _assert_text(first["text"] + second["text"], text)
