"""S06-I12: resource pagination through the real strict tool registry."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from test_agent_resources import resources, file_ref, invoke
from test_agent_cloud_resources import cloud
from test_agent_cloud_resources import GoogleConnector, OneDriveConnector
from rynmesh.agent_runtime.store import encoded
from rynmesh.agent_runtime.tools import RynTools, bounded_result, _SPECS as SPECS


def registry(resources):
    resource_api, _, context, state = resources
    tools = RynTools(resource_api.app, SimpleNamespace(), {}, None)
    return tools, context, state, file_ref(resource_api, context)


def pages(tools, context, reference):
    offset, cursor = 0, ""
    for i in range(30):
        result = tools.execute("ryn_read_resource", {"resource_ref": reference, "cursor": cursor},
            operation_id=f"read-{i}", context=context, cancelled=lambda: False)
        assert result["character_offset"] == offset
        assert result["characters_returned"] == len(result["text"])
        assert result["untrusted_data"] is True
        assert len(encoded(result)) <= 18*1024
        assert result["text"]
        yield result
        offset += len(result["text"])
        cursor = result["next_cursor"]
        if not cursor:
            return
    pytest.fail("pagination must terminate")


def test_T1_s06_i12_english_8000_and_exact_continuation(resources):
    tools, context, state, ref = registry(resources)
    text = "".join(chr(65+i%26) for i in range(30000))
    state["body"] = text.encode()
    results = list(pages(tools, context, ref))
    assert [len(result["text"]) for result in results] == [8000, 8000, 8000, 6000]
    assert "".join(result["text"] for result in results) == text


@pytest.mark.parametrize("character", ["汉", "😀", '"'])
def test_T2_s06_i12_wide_and_escaped_text_respects_complete_result_budget(resources, character):
    tools, context, state, ref = registry(resources)
    text = character*30000
    state["body"] = text.encode()
    results = list(pages(tools, context, ref))
    assert len(results[0]["text"]) > 2048
    assert "".join(result["text"] for result in results) == text


def test_T3_s06_i12_directory_bytes_and_entries_resume_exactly(resources, monkeypatch):
    api, plugin, context, _ = resources
    expected = [f"file-{i:03}.txt" for i in range(60)]
    class Connector:
        def list(self, path):
            return [{"name": name, "path": path+"/"+name, "directory": False, "size": 1} for name in expected]
    @contextmanager
    def connection(*args, **kwargs):
        yield Connector()
    monkeypatch.setattr(plugin, "connection", connection)
    root = invoke(api, context, "ryn_list_resource_roots")["roots"][0]["resource_ref"]
    tools = RynTools(api.app, SimpleNamespace(), {}, None)
    cursor, names = "", []
    for i in range(20):
        result = tools.execute("ryn_list_resources", {"resource_ref": root, "cursor": cursor},
            operation_id=f"list-{i}", context=context, cancelled=lambda: False)
        assert 0 < len(result["entries"]) <= 50
        assert len(encoded(result["entries"])) <= 16*1024
        assert len(encoded(result)) <= 16*1024
        if i == 0:
            assert len(result["entries"]) > 10
        names.extend(item["name"] for item in result["entries"])
        cursor = result["next_cursor"]
        if not cursor:
            break
    assert names == expected


def test_T4_s06_i12_long_text_option_and_descriptions(resources):
    text = "a"*8000
    result = bounded_result({"text": text, "note": "b"*3000}, long_text_keys={"text": 8192})
    assert result["text"] == text and len(result["note"]) < 2100
    assert "characters per page according to the task output limit" in SPECS["ryn_read_resource"][0]
    assert "entries per page according to the task output limit" in SPECS["ryn_list_resources"][0]


def test_T3_s06_i12_cloud_visible_children_are_all_verified(cloud, monkeypatch):
    api, context, state = cloud
    protocol = api.nas.state["sources"][0]["protocol"]
    klass = GoogleConnector if protocol == "google" else OneDriveConnector
    original = klass.request
    ids = [f"File{i:03}" for i in range(60)]
    state["items"].update({key: {"id": key, "name": key+".txt", "parent": "FolderA", "folder": False} for key in ids})
    checked = []
    def request(connector, method, path, **kwargs):
        if path == "/files" or path.endswith("/children"):
            return {"files" if protocol == "google" else "value": [original(connector, "GET", "/files/"+key) for key in ids]}
        checked.append(path.rsplit("/", 1)[-1])
        return original(connector, method, path, **kwargs)
    monkeypatch.setattr(klass, "request", request)
    root = invoke(api, context, "ryn_list_resource_roots")["roots"][0]["resource_ref"]
    result = invoke(api, context, "ryn_list_resources", root)
    assert len(result["entries"]) > 10
    assert len(encoded(result)) <= 16*1024
    snapshot = api._open(result["origin"]["listing_snapshot"])
    assert len(snapshot["children"]) == len(result["entries"])
    for entry in result["entries"]:
        ident = api._open(entry["resource_ref"])["path"].rsplit("/", 1)[-1]
        assert ident in checked


def test_T1_s06_i12_old_character_cursor_stays_valid(resources):
    api, _, context, state = resources
    state["body"] = b"a"*12000
    ref = file_ref(api, context)
    first = invoke(api, context, "ryn_read_resource", ref)
    cursor = api._open(first["next_cursor"])
    cursor["offset"] = 2000
    old = api._seal(cursor)
    second = api.execute("ryn_read_resource", {"resource_ref": ref, "cursor": old}, context=context, cancelled=lambda: False)
    assert second["character_offset"] == 2000 and len(second["text"]) == 8000
