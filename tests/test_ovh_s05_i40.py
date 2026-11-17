"""S05-I40: scoped history validation, metadata hashing and independent NAS IO."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest

from test_agent_cloud_resources import cloud as cloud_fixture, read_ref
from test_agent_file_scope import file_scope
from test_agent_resources import resources as resources_fixture
from test_agent_resource_writes import writable as writable_fixture, execute
from rynmesh.agent_runtime import resources as resource_module
from rynmesh.agent_runtime.cloud_resources import CloudAncestry
from rynmesh.agent_runtime.store import AgentToolRejected
from rynmesh.nas import SMBConnector, WebDAVConnector

cloud = cloud_fixture
resources = resources_fixture
writable = writable_fixture


def _history(cloud, count):
    tools, context, state = cloud
    grant = context["resource_grants"][0]
    root = tools._open(grant["root_resource_ref"])
    refs = []
    for index in range(count):
        key = f"History{index:02d}"
        state["items"][key] = {"id": key, "name": key + ".md", "parent": "FolderA",
                               "folder": False, "version": "history-v1"}
        ref = tools._seal({"path": "FolderA/" + key, "directory": False,
            **{k: root[k] for k in ("grant_id", "owner_id", "consumer_node_id")}, "task_id": context["task_id"]})
        refs.append(ref)
        context["resource_origins"].append({"grant_id": grant["grant_id"], "resource_ref": ref,
            "resource_version": "history-v1", "scope_revision": context["scope_revision"],
            "connection_access_generation": grant["connection_access_generation"]})
    return refs


def _count_checks(monkeypatch):
    calls = Counter()
    original = CloudAncestry.check

    def check(self, path, **options):
        calls[path] += 1
        return original(self, path, **options)

    monkeypatch.setattr(CloudAncestry, "check", check)
    return calls


def _read(tools, context, ref):
    return tools.execute("ryn_read_resource", {"resource_ref": ref}, context=context, cancelled=lambda: False)


def test_t1_real_read_validates_each_history_origin_at_most_once(cloud, monkeypatch):
    tools, context, _ = cloud
    ref = read_ref(tools, context)
    _history(cloud, 1)
    calls = _count_checks(monkeypatch)
    result = _read(tools, context, ref)
    assert result["text"] == "Verified cloud source text"
    assert calls["FolderA/History00"] == 1
    # Download's own before/after ancestry checks remain independent of history
    # validation and must survive this optimization.
    assert calls["FolderA/FileF"] == 2


def test_t2_fifty_origins_cache_for_60_seconds_but_current_ref_is_fresh(cloud, monkeypatch):
    tools, context, _ = cloud
    ref = read_ref(tools, context)
    refs = _history(cloud, 50)
    clock = [1.0]
    monkeypatch.setattr(resource_module, "monotonic", lambda: clock[0], raising=False)
    calls = _count_checks(monkeypatch)
    _read(tools, context, ref)
    assert [calls[f"FolderA/History{i:02d}"] for i in range(50)] == [1] * 50
    clock[0] = 30.0
    _read(tools, context, ref)
    assert [calls[f"FolderA/History{i:02d}"] for i in range(50)] == [1] * 50
    _read(tools, context, refs[0])
    assert calls["FolderA/History00"] == 4  # one fresh origin validation + two download checks
    clock[0] = 92.0
    _read(tools, context, ref)
    assert calls["FolderA/History01"] == 2
    cache_key = (context["resource_grants"][0]["grant_id"],
                 resource_module.hashlib.sha256(refs[1].encode()).hexdigest(), context["scope_revision"])
    assert tools._last_checked[cache_key] == 92.0


def test_t3_single_file_reads_still_fetch_content_but_hash_is_reused(resources):
    tools, _, context, state = resources
    state["body"] = b"hash-cache-only-this-exact-body"
    context = file_scope(tools, context, "docs", "allowed/notes.md")
    ref = context["resource_grants"][0]["root_resource_ref"]
    first = _read(tools, context, ref)
    context["resource_origins"] = [first["origin"]]
    second = _read(tools, context, ref)
    tools.validate_context(context)
    tools.validate_context(context)
    assert second["text"] == first["text"] == state["body"].decode()
    assert state["reads"] == ["allowed/notes.md"] * 2
    assert list(tools._file_hashes.values()) == [first["origin"]["resource_version"]]
    fresh = resource_module.ResourceTools(tools.app)
    fresh.validate_context(context)
    fresh.validate_context(context)
    assert state["reads"] == ["allowed/notes.md"] * 3


def test_t2_historical_listing_child_metadata_uses_same_ttl(cloud, monkeypatch):
    tools, context, _ = cloud
    root = context["resource_grants"][0]["root_resource_ref"]
    listing = tools.execute("ryn_list_resources", {"resource_ref": root}, context=context, cancelled=lambda: False)
    context["resource_origins"] = [listing["origin"]]
    ref = listing["entries"][0]["resource_ref"]
    clock = [1.0]
    monkeypatch.setattr(resource_module, "monotonic", lambda: clock[0], raising=False)
    calls = Counter()
    original = CloudAncestry.metadata

    def metadata(self, path):
        calls[path] += 1
        return original(self, path)

    monkeypatch.setattr(CloudAncestry, "metadata", metadata)
    _read(tools, context, ref)
    assert calls["FolderA/FileF"] == 1
    clock[0] = 30.0
    _read(tools, context, ref)
    assert calls["FolderA/FileF"] == 1
    clock[0] = 62.0
    _read(tools, context, ref)
    assert calls["FolderA/FileF"] == 2


def test_t4_generation_revocation_is_not_hidden_by_origin_cache(cloud):
    tools, context, _ = cloud
    ref = read_ref(tools, context)
    _history(cloud, 2)
    _read(tools, context, ref)
    with tools.nas.lock:
        tools.nas.state["sources"][0]["access_generation"] = "revoked-generation"
        tools.nas.save()
    with pytest.raises(AgentToolRejected, match="agent_resource_permission_changed"):
        tools.validate_context(context, cheap_only=True)
    with pytest.raises(AgentToolRejected, match="agent_resource_permission_changed"):
        _read(tools, context, ref)


def test_t5_live_write_does_not_hold_catalog_or_other_connection_read(writable):
    tools, context, args, nas, _, server, root = writable[:7]
    (root / "allowed/parallel.md").write_bytes(b"Independent connection read")
    other = nas.configure({"name": "Independent DAV", "system": "generic", "protocol": "webdav", "url": server.url,
                           "username": "fixture", "password": "fixture", "allow_ai": True, "writable": False})
    entered, release = server.arm("PUT", "allowed/report.md", "barrier_before_io")
    with ThreadPoolExecutor(max_workers=3) as workers:
        writer = workers.submit(execute, tools, context, args)
        try:
            assert entered.wait(15)
            catalog = workers.submit(tools.resources.catalog)
            reader = workers.submit(nas.operate, other["id"], "ai", "allowed/parallel.md")
            assert len(catalog.result(timeout=5)["connections"]) == 2
            assert reader.result(timeout=5)["text"] == "Independent connection read"
        finally:
            release.set()
        assert writer.result(timeout=15)["business_acknowledged"] is True


def test_t6_live_write_generation_change_is_confirmed_and_persisted(writable):
    tools, context, args, nas, source, server, root = writable[:7]
    entered, release = server.arm("PUT", "allowed/report.md", "barrier_before_io")
    with ThreadPoolExecutor(max_workers=2) as workers:
        writer = workers.submit(execute, tools, context, args)
        try:
            assert entered.wait(15)
            changed = workers.submit(nas.update_source, source["id"], {"writable": False})
            assert changed.result(timeout=5)["writable"] is False
        finally:
            release.set()
        result = writer.result(timeout=15)
    assert result["generation_changed"] is True
    assert result["business_acknowledged"] is True
    assert (root / "allowed/report.md").read_bytes() == args["content"].encode()
    stored = tools.resource_writes.ledger.get("op-create")
    assert stored["status"] == "confirmed" and stored["result"]["generation_changed"] is True


def test_t7_two_writes_to_same_source_remain_serialized(writable):
    tools, context, args, _, _, server, root = writable[:7]
    first_entered, release_first = server.arm("PUT", "allowed/first.md", "barrier_before_io")
    second_entered, release_second = server.arm("PUT", "allowed/second.md", "barrier_before_io")
    started_second = threading.Event()

    def second():
        started_second.set()
        return execute(tools, context, {**args, "name": "second.md"}, "op-second")

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(execute, tools, context, {**args, "name": "first.md"}, "op-first")
        try:
            assert first_entered.wait(15)
            other = workers.submit(second)
            assert started_second.wait(5)
            assert not second_entered.wait(0.2)
            release_first.set()
            assert first.result(timeout=15)["ok"] is True
            assert second_entered.wait(15)
        finally:
            release_first.set()
            release_second.set()
        assert other.result(timeout=15)["ok"] is True
    assert (root / "allowed/first.md").read_bytes() == (root / "allowed/second.md").read_bytes() == args["content"].encode()


def test_single_file_hash_cache_is_lru_64_and_stat_avoids_parent_listing(resources, monkeypatch):
    tools, plugin, context, _ = resources
    version = [0]
    calls = Counter()

    class Connector:
        def stat(self, path):
            calls["stat"] += 1
            return {"path": path, "directory": False, "size": 7, "modified": version[0]}

        def list(self, path):
            raise AssertionError("stat-capable connectors must not enumerate the parent")

        def read(self, path, limit):
            return b"cached!"

    @contextmanager
    def connection(*args, **options):
        yield Connector()

    monkeypatch.setattr(plugin, "connection", connection)
    grant = context["resource_grants"][0]
    versions = []
    for index in range(64):
        version[0] = index
        digest, _ = tools._path_file(grant, "allowed/notes.md", read=True)
        versions.append(digest)
    version[0] = 0
    tools._path_file(grant, "allowed/notes.md", read=True)
    version[0] = 64
    newest, _ = tools._path_file(grant, "allowed/notes.md", read=True)
    assert len(tools._file_hashes) == 64
    assert (grant["grant_id"], versions[0]) in tools._file_hashes
    assert (grant["grant_id"], versions[1]) not in tools._file_hashes
    assert (grant["grant_id"], newest) in tools._file_hashes
    assert calls["stat"] == 132


def test_webdav_stat_uses_depth_zero_and_returns_exact_metadata(monkeypatch):
    connector = WebDAVConnector.__new__(WebDAVConnector)
    connector.root = "https://dav.example.test/dav/"
    calls = []

    def request(method, path, **options):
        calls.append((method, path, options["headers"]))
        return b'''<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/allowed/notes.md</d:href>
          <d:propstat><d:prop><d:resourcetype/><d:getcontentlength>17</d:getcontentlength>
          <d:getlastmodified>version-1</d:getlastmodified></d:prop></d:propstat></d:response></d:multistatus>'''

    monkeypatch.setattr(connector, "request", request)
    result = connector.stat("allowed/notes.md")
    assert result == {"name": "notes.md", "path": "allowed/notes.md", "directory": False, "size": 17, "modified": "version-1"}
    assert calls == [("PROPFIND", "allowed/notes.md", {"Depth": "0"})]


def test_smb_stat_returns_metadata_without_directory_enumeration():
    connector = SMBConnector.__new__(SMBConnector)
    connector.config = {"host": "nas.example.test", "share": "data", "root": "", "port": 445}
    connector.root = "\\\\nas.example.test\\data"
    connector.cache = {}
    connector.api = SimpleNamespace(stat=lambda *args, **options: SimpleNamespace(
        st_file_attributes=0, st_size=17, st_mtime=123.5))
    assert connector.stat("allowed/notes.md") == {"name": "notes.md", "path": "allowed/notes.md",
                                                "directory": False, "size": 17, "modified": 123.5}


def test_external_resolve_still_validates_by_default(resources):
    tools, plugin, context, _ = resources
    root = context["resource_grants"][0]["root_resource_ref"]
    plugin.state["sources"][0]["access_generation"] = "revoked"
    with pytest.raises(AgentToolRejected, match="agent_resource_permission_changed"):
        tools._resolve(root, context, "list")


def test_actual_read_still_detects_changed_bytes_under_identical_metadata(resources):
    tools, _, context, state = resources
    context = file_scope(tools, context, "docs", "allowed/notes.md")
    ref = context["resource_grants"][0]["root_resource_ref"]
    first = _read(tools, context, ref)
    context["resource_origins"] = [first["origin"]]
    state["body"] = b"X" * len(state["body"])
    with pytest.raises(AgentToolRejected, match="agent_resource_version_changed"):
        _read(tools, context, ref)


def test_same_connection_read_waits_for_inflight_write(writable):
    tools, context, args, nas, source, server, root = writable[:7]
    (root / "allowed/parallel.md").write_bytes(b"Same connection read")
    listing = tools.execute("ryn_list_resources", {"resource_ref": args["directory_ref"]},
                            operation_id="list-before-write", context=context, cancelled=lambda: False)
    ref = listing["entries"][0]["resource_ref"]
    entered, release = server.arm("PUT", "allowed/report.md", "barrier_before_io")
    reading = threading.Event()

    def read():
        reading.set()
        return tools.execute("ryn_read_resource", {"resource_ref": ref}, operation_id="parallel-read",
                             context=context, cancelled=lambda: False)

    with ThreadPoolExecutor(max_workers=2) as workers:
        writer = workers.submit(execute, tools, context, args)
        try:
            assert entered.wait(15)
            reader = workers.submit(read)
            assert reading.wait(5)
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.2)
        finally:
            release.set()
        assert writer.result(timeout=15)["ok"] is True
        assert reader.result(timeout=15)["text"] == "Same connection read"


def test_real_dav_file_read_uses_single_resource_stat_not_parent_listing(writable):
    business, context, _, _, source, server, root = writable[:7]
    (root / "allowed/only.md").write_bytes(b"Exact file body")
    scoped = file_scope(business.resources, context, source["id"], "allowed/only.md")
    with server.lock:
        server.audit.clear()
    ref = scoped["resource_grants"][0]["root_resource_ref"]
    assert _read(business.resources, scoped, ref)["text"] == "Exact file body"
    paths = [entry["path"] for entry in server.audit if entry["method"] == "PROPFIND"]
    assert paths and set(paths) == {"allowed/only.md"}
