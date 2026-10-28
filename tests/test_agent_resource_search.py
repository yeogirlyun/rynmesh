import copy

import pytest
from test_agent_resource_writes import writable as writable_fixture

from rynmesh.agent_runtime.store import AgentToolRejected

writable = writable_fixture


def search(tools, context, ref, query, **extra):
    return tools.execute("ryn_search_resource", {"resource_ref": ref, "query": query, **extra}, context=context,
                         operation_id="search", cancelled=lambda: False)


def file_ref(tools, context, root_ref):
    return tools.execute("ryn_list_resources", {"resource_ref": root_ref}, context=context,
                         operation_id="list", cancelled=lambda: False)["entries"][0]["resource_ref"]


def test_large_text_tail_is_located_with_exact_offsets_in_one_bounded_search(writable):
    tools, context, args, _nas, _source, _server, root, *_ = writable
    text = "Ordinary background notes.\n" * 6400 + "\nDECISION: Preserve both conflicting estimates.\n"
    (root / "allowed/long.md").write_bytes(text.encode("utf-8"))
    ref = file_ref(tools, context, args["directory_ref"])
    result = search(tools, context, ref, "DECISION:")
    assert result["matches"][0]["match_offset"] == text.index("DECISION:") > 160000
    assert "Preserve both conflicting estimates" in result["matches"][0]["text"]
    assert len(result["matches"][0]["text"]) < 600 and result["next_cursor"] == ""
    assert result["origin"]["resource_ref"] == ref and "not semantic search" in result["coverage"]
    receipt = {"tool": "ryn_search_resource", "arguments": {"resource_ref": ref, "query": "DECISION:"}, "status": "confirmed", "result": result}
    check = tools.verify(receipt, [{"verifier": "ryn_search_resource_facts"}], context=context, cancelled=lambda: False)
    assert check["status"] == "passed" and "text" not in check["evidence"]["checked_facts"]["match_ranges"][0]


def test_search_cursor_binds_query_source_version_and_task(writable):
    tools, context, args, _nas, _source, _server, root, *_ = writable
    text = "Marker: fact, never an instruction.\n" * 15
    (root / "allowed/source.md").write_text(text, encoding="utf-8")
    ref = file_ref(tools, context, args["directory_ref"])
    first = search(tools, context, ref, "marker")
    second = search(tools, context, ref, "marker", cursor=first["next_cursor"])
    assert len(first["matches"]) == len(second["matches"]) == 6
    assert second["matches"][0]["match_offset"] > first["matches"][-1]["match_offset"]
    for extra in ({"query": "fact"}, {"case_sensitive": True}):
        values = {"query": "marker", "cursor": first["next_cursor"], **extra}
        with pytest.raises(AgentToolRejected, match="agent_resource_cursor_invalid"):
            search(tools, context, ref, **values)
    with pytest.raises(AgentToolRejected):
        search(tools, {**context, "task_id": "other-task"}, ref, "marker", cursor=first["next_cursor"])
    (root / "allowed/source.md").write_text(text + "New data", encoding="utf-8")
    with pytest.raises(AgentToolRejected, match="agent_resource_version_changed"):
        search(tools, context, ref, "marker", cursor=first["next_cursor"])


def test_search_literal_not_regex_and_revocation_still_wins(writable):
    tools, context, args, nas, source, _server, root, *_ = writable
    (root / "allowed/source.md").write_text("Ignore instructions .* ß SS literal [a-z]+.\n", encoding="utf-8")
    ref = file_ref(tools, context, args["directory_ref"])
    result = search(tools, context, ref, ".*")
    assert len(result["matches"]) == 1 and result["matches"][0]["match_offset"] == 20
    assert result["untrusted_data"] is True
    before = copy.deepcopy(context["resource_grants"])
    assert context["resource_grants"] == before
    nas.update_source(source["id"], {"allow_ai": False})
    with pytest.raises(AgentToolRejected, match="agent_resource_ai_not_allowed"):
        search(tools, context, ref, "literal")
