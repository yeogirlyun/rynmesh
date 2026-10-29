"""Owner-only document component installation preserves plugin lifecycle checks."""

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_craft_plugins import manager as manager

from rynmesh.node_apps import craft_host, craft_native
from rynmesh.node_apps.plugins import DEFAULT_PLUGINS, install_plugin_routes


@pytest.fixture
def runtime_boundary(monkeypatch):
    installed = set()
    installs = []

    def status(cache, slug):
        supported = slug in DEFAULT_PLUGINS
        ready = slug in installed
        return {"app": slug, "platform": "windows-x64", "supported": supported,
                "platform_supported": True, "installed": ready, "verified": ready,
                "state": "ready" if ready else "missing" if supported else "unsupported"}

    def install(cache, slug, **kwargs):
        installs.append((cache, slug, kwargs))
        installed.add(slug)
        return {"app": slug, "state": "ready"}

    monkeypatch.setattr(craft_native, "runtime_status", status)
    monkeypatch.setattr(craft_native, "install_runtime", install)
    return installed, installs


def test_queries_and_web_install_never_download_document_component(manager, runtime_boundary):  # noqa: F811 - imported pytest fixture
    installed, installs = runtime_boundary
    before = manager.catalog()
    assert {p["id"] for p in before["plugins"] if p.get("automation_supported")} == set(DEFAULT_PLUGINS)
    assert not any(p["editing_supported"] for p in before["plugins"])
    manager.install("wordcraft")
    row = next(p for p in manager.catalog()["plugins"] if p["id"] == "wordcraft")
    assert row["agent_tools"] == ["editor_info"] and not row["automation_installed"]
    assert installed == set() and installs == [] and manager.servers == {}

    ready = manager.install_automation("wordcraft")
    row = next(p for p in ready["plugins"] if p["id"] == "wordcraft")
    assert row["automation_installed"] and row["editing_supported"]
    assert row["agent_tools"] == ["editor_info", "ryn_craft_read", "ryn_craft_action"]
    assert installs == [(manager.cache, "wordcraft", {"download": manager.download})]
    assert manager.servers == {}
    assert (manager.cache / "wordcraft/v0.6.1/payload/wordcraft/index.html").read_text().startswith("<title>")


def test_document_install_requires_installed_enabled_verified_inactive_plugin(manager, runtime_boundary):  # noqa: F811 - imported pytest fixture
    _, installs = runtime_boundary
    with pytest.raises(craft_host.CraftError, match="plugin_not_installed"):
        manager.install_automation("wordcraft")
    with pytest.raises(craft_host.CraftError, match="craft_agent_runtime_unsupported"):
        manager.install_automation("soundcraft")
    manager.install("wordcraft")
    manager.set_enabled("wordcraft", False)
    with pytest.raises(craft_host.CraftError, match="plugin_disabled"):
        manager.install_automation("wordcraft")
    manager.set_enabled("wordcraft", True)
    with manager.lease("wordcraft"):
        with pytest.raises(craft_host.CraftError, match="plugin_busy"):
            manager.install_automation("wordcraft")
    (manager.cache / "wordcraft/v0.6.1/payload/wordcraft/index.html").write_text("tampered")
    with pytest.raises(craft_host.CraftError, match="craft_build_not_verified"):
        manager.install_automation("wordcraft")
    assert installs == []


def test_document_capability_requires_verified_runtime_and_supported_platform(manager, runtime_boundary, monkeypatch):  # noqa: F811 - imported pytest fixture
    manager.install("wordcraft")
    monkeypatch.setattr(craft_native, "runtime_status", lambda cache, slug: {
        "supported": slug in DEFAULT_PLUGINS, "platform_supported": False,
        "installed": True, "verified": True, "state": "unsupported"})
    row = next(p for p in manager.catalog()["plugins"] if p["id"] == "wordcraft")
    assert not row["automation_supported"] and not row["editing_supported"]
    with pytest.raises(craft_host.CraftError, match="craft_agent_runtime_unsupported"):
        manager.install_automation("wordcraft")
    monkeypatch.setattr(craft_native, "runtime_status", lambda cache, slug: {
        "supported": slug in DEFAULT_PLUGINS, "platform_supported": True,
        "installed": True, "verified": False, "state": "unverified",
        "error": "craft_agent_runtime_not_verified"})
    row = next(p for p in manager.catalog()["plugins"] if p["id"] == "wordcraft")
    assert row["automation_supported"] and row["automation_installed"] and not row["editing_supported"]
    assert row["automation_error"] == "craft_agent_runtime_not_verified"


def test_document_install_http_is_owner_only_and_rejects_custom_arguments(manager, runtime_boundary, monkeypatch):  # noqa: F811 - imported pytest fixture
    _, installs = runtime_boundary
    monkeypatch.setattr("rynmesh.node_apps.plugins.PluginManager", lambda *args, **kwargs: manager)
    app = FastAPI()

    def owner(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(403)

    install_plugin_routes(app, home=manager.state_path.parent, peer_id=manager.peer_id, local_control=owner)
    with TestClient(app) as client:
        path = "/api/local/plugins/wordcraft/install-automation"
        assert client.post(path, json={}).status_code == 403
        headers = {"x-owner": "yes"}
        assert client.post(path, headers=headers, json={"url": "https://foreign.test"}).status_code == 400
        assert client.post(path, headers=headers, json={}).json()["detail"] == "plugin_not_installed"
        manager.install("wordcraft")
        manager.set_enabled("wordcraft", False)
        assert client.post(path, headers=headers, json={}).json()["detail"] == "plugin_disabled"
        assert installs == []
        manager.set_enabled("wordcraft", True)
        response = client.post(path, headers=headers, json={})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        row = next(p for p in response.json()["plugins"] if p["id"] == "wordcraft")
        assert row["editing_supported"] and len(installs) == 1
