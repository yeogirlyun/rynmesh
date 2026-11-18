"""S06-I29 saved web policy reaches real tool dispatch and never widens tasks."""
import os
import time

import pytest
from test_agent_runtime import make_runtime, assistant, advance, Catalog
from test_agent_local_tools import local, execute  # noqa: F401
from rynmesh.agent_runtime.runtime import AgentRuntime
from test_agent_browser_paging import _RuntimeLocalTools
from tests.test_agent_automations import _build_test_env
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected, DEFAULT_SETTINGS
from rynmesh.agent_runtime.web_search import WebSearchEngine


def bind_real_web(runtime, tmp_path):
    seen = {"search": [], "fetch": []}
    def search(query, maximum, domains):
        seen["search"].append(list(domains))
        return [{"url": "https://docs.example.com/page", "title": "Docs", "snippet": "allowed docs"},
                {"url": "https://other.org/page", "title": "Other", "snippet": "outside policy"}]
    def fetch(url, domains):
        seen["fetch"].append(url)
        return b"<article><p>Public documentation content for a readable test page.</p></article>", url, "text/html"
    runtime.tools = _RuntimeLocalTools(runtime.store, tmp_path / "home")
    runtime.tools.local._web_search = WebSearchEngine(tmp_path / "cache", search_provider=search,
        http_fetcher=fetch, dns_resolver=lambda host: ["93.184.216.34"])
    return seen


def test_T1_disabled_settings_reach_real_model_tool_and_explain_settings(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "state", assistant(name="ryn_web_search", args={"query": "docs"}), assistant("Blocked."))
    seen = bind_real_web(runtime, tmp_path)
    runtime.update_settings({"web_search_mode": "disabled"})
    task = runtime.create({"message": "Search the public documentation."})
    assert task["local_execution"]["web_search_mode"] == "disabled"
    advance(runtime, 3)
    messages = runtime.store.read()["tasks"][task["id"]]["messages"]
    errors = [m["content"] for m in messages if m["role"] == "tool"]
    assert any("agent_web_search_disabled" in s and "联网被设置禁用" in s for s in errors)
    assert seen == {"search": [], "fetch": []}


def test_T2_saved_allowlist_intersects_model_domains_and_blocks_open(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "state", assistant(name="ryn_web_search", args={"query": "docs", "domains": ["docs.example.com"]}), assistant("Done."))
    seen = bind_real_web(runtime, tmp_path)
    runtime.update_settings({"web_allowed_domains": ["https://Example.COM/path"]})
    task = runtime.create({"message": "Find the allowed documentation."})
    assert task["local_execution"]["web_allowed_domains"] == ["example.com"]
    advance(runtime, 3)
    assert seen["search"] == [["docs.example.com"]]
    row = runtime.store.read()["tasks"][task["id"]]
    ctx = runtime._execution_context(row)
    with pytest.raises(AgentToolRejected, match="agent_web_search_domain_not_allowed"):
        runtime.tools.local.perform("ryn_web_search", {"open": "https://other.org/page"}, ctx, lambda: False)
    with pytest.raises(AgentToolRejected, match="agent_web_search_domain_not_allowed"):
        runtime.tools.local.perform("ryn_web_search", {"query": "docs", "domains": ["other.org"]}, ctx, lambda: False)
    assert seen["fetch"] == []


@pytest.mark.parametrize("values", [
    {"web_search_mode": "live"}, {"web_search_mode": None}, {"web_search_mode": []},
    {"web_search_mode": True}, {"web_allowed_domains": "example.com"},
    {"web_allowed_domains": ["example.com"] * 33}, {"web_allowed_domains": [None]},
    {"web_allowed_domains": [""]}, {"web_allowed_domains": ["   "]},
    {"web_allowed_domains": ["localhost"]}, {"web_allowed_domains": ["bad domain.com"]},
    {"web_allowed_domains": ["a" * 250 + ".com"]},
])
def test_T3_invalid_settings_are_atomic_after_valid_normalization(tmp_path, values):
    runtime, _, _, _ = make_runtime(tmp_path)
    saved = runtime.update_settings({"web_allowed_domains": ["*.EXAMPLE.com.", "https://docs.example.com/p"], "web_search_mode": "cached"})
    assert saved["web_allowed_domains"] == ["example.com", "docs.example.com"]
    with pytest.raises(AgentError, match="^agent_invalid_settings$"):
        runtime.update_settings(values)
    assert runtime.settings()["web_allowed_domains"] == saved["web_allowed_domains"]
    assert runtime.settings()["web_search_mode"] == "cached"


def test_T4_existing_binding_only_tightens_and_disjoint_domains_disable(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.update_settings({"web_search_mode": "cached", "web_allowed_domains": ["example.com"]})
    old = runtime.create({"message": "Keep original policy."})
    runtime.update_settings({"web_search_mode": "", "web_allowed_domains": []})
    assert runtime.get(old["id"])["local_execution"]["web_search_mode"] == "cached"
    assert runtime.get(old["id"])["local_execution"]["web_allowed_domains"] == ["example.com"]
    fresh = runtime.create({"message": "Use current default policy."})
    assert fresh["local_execution"]["web_search_mode"] == ""
    assert fresh["local_execution"]["web_allowed_domains"] == []
    runtime.update_settings({"web_allowed_domains": ["docs.example.com", "other.org"]})
    assert runtime.get(old["id"])["local_execution"]["web_allowed_domains"] == ["docs.example.com"]
    runtime.update_settings({"web_allowed_domains": ["outside.net"]})
    assert runtime.get(old["id"])["local_execution"]["web_search_mode"] == "disabled"
    runtime.update_settings({"web_search_mode": "", "web_allowed_domains": []})
    assert runtime.get(old["id"])["local_execution"]["web_search_mode"] == "disabled"


def test_legacy_tasks_defaults_and_archived_binding_survive_restore(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    assert DEFAULT_SETTINGS["web_search_mode"] == "" and DEFAULT_SETTINGS["web_allowed_domains"] == []
    runtime.update_settings({"web_search_mode": "cached", "web_allowed_domains": ["example.com"]})
    task = runtime.create({"message": "Remember policy over restore."})
    advance(runtime, 1)
    archived = runtime.lifecycle(task["id"], {"expected_control_revision": runtime.get(task["id"])["control_revision"]}, "archive")
    runtime.update_settings({"web_search_mode": "", "web_allowed_domains": []})
    restored = runtime.lifecycle(task["id"], {"expected_control_revision": archived["control_revision"]}, "restore")
    assert restored["local_execution"]["web_search_mode"] == "cached"
    assert restored["local_execution"]["web_allowed_domains"] == ["example.com"]
    runtime.store.transaction(lambda d: [d["tasks"][task["id"]]["local_execution"].pop(k, None) for k in ("web_search_mode", "web_allowed_domains")])
    ctx = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
    assert ctx["local_execution"].get("web_search_mode", "") == ""
    assert ctx["local_execution"].get("web_allowed_domains", []) == []


@pytest.mark.parametrize("kind", ["cron", "heartbeat"])
def test_scheduled_tasks_bind_current_web_settings(tmp_path, kind):
    _, runtime, _, client = _build_test_env(tmp_path)
    try:
        runtime.update_settings({"web_search_mode": "disabled", "web_allowed_domains": ["example.com"]})
        created = client.post("/api/local/agent/automations", json={"name": "Policy test", "prompt": "Inspect docs", "kind": kind,
            "repeat": "once", "delay_minutes": 1}).json()["automation"]
        run = client.post(f"/api/local/agent/automations/{created['id']}/run-now").json()
        assert run["triggered"] is True
        row = runtime.get(run["task_id"])
        assert row["local_execution"]["web_search_mode"] == "disabled"
        assert row["local_execution"]["web_allowed_domains"] == ["example.com"]
    finally:
        client.close()


def test_cached_settings_prevent_model_from_forcing_live_network(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path / "state")
    seen = bind_real_web(runtime, tmp_path)
    runtime.update_settings({"web_search_mode": "cached"})
    task = runtime.create({"message": "Use only cached documentation."})
    ctx = runtime._execution_context(runtime.store.read()["tasks"][task["id"]])
    with pytest.raises(AgentToolRejected, match="agent_web_search_cache_miss"):
        runtime.tools.local.perform("ryn_web_search", {"open": "https://docs.example.com/page", "mode": "live"}, ctx, lambda: False)
    assert seen == {"search": [], "fetch": []}


def test_continuation_keeps_stricter_policy_after_settings_relax(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path, assistant("Done."))
    runtime.update_settings({"web_search_mode": "cached", "web_allowed_domains": ["example.com"]})
    task = runtime.create({"message": "Original work."})
    advance(runtime, 1)
    runtime.update_settings({"web_search_mode": "", "web_allowed_domains": []})
    child = runtime._continue_in_new_task(task["id"], {}, "Continue original work.")
    assert child["local_execution"]["web_search_mode"] == "cached"
    assert child["local_execution"]["web_allowed_domains"] == ["example.com"]


def test_empty_and_maximum_domain_lists_are_valid(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    domains = [f"site{i}.example.com" for i in range(32)]
    assert runtime.update_settings({"web_allowed_domains": domains})["web_allowed_domains"] == domains
    assert runtime.update_settings({"web_allowed_domains": []})["web_allowed_domains"] == []


def test_web_policy_change_does_not_discard_completed_shell_file_receipts(local):
    tools, context, workspace, state = local
    # Use the real production binding (including the newly added web keys).
    context["local_execution"] = dict(state.read()["tasks"]["task"]["local_execution"])
    command = ("Start-Sleep -Milliseconds 300; Set-Content policy-output.txt ready" if os.name == "nt"
               else "sleep 0.3; echo ready >policy-output.txt")
    result = execute(tools, context, "ryn_shell_execute", {"command": command, "background": True}, "web-policy-file")
    # A web-only restriction does not revoke the command's file authority.
    runtime = AgentRuntime(state, object(), object(), Catalog())
    runtime.update_settings({"web_search_mode": "disabled", "web_allowed_domains": ["example.com"]})
    tools.processes.get(result["process_id"], "task")["process"].wait(timeout=10)
    deadline = time.monotonic() + 5
    while "web-policy-file" not in state.read()["tasks"]["task"].get("workspace_file_receipts", {}):
        tools.collect_command_artifacts("task")
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert (workspace / "policy-output.txt").read_text().strip() == "ready"
