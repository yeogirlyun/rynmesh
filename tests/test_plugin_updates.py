import hashlib
import io
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from rynmesh.crypto import public_key_from_private, sign_payload
from rynmesh.node_apps import craft_host, plugin_updates
from rynmesh.node_apps.craft_catalog import CRAFT
from rynmesh.node_apps.plugins import PluginManager, install_plugin_routes

KEY = bytes(range(32))


def archive(text):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        output.writestr("web/index.html", text)
        output.writestr("web/LICENSE", "Retained license")
    return stream.getvalue()


@pytest.fixture
def installed(tmp_path, monkeypatch):
    old = archive("old editor")
    new = archive("new editor")
    monkeypatch.setitem(CRAFT, "wordcraft", {**CRAFT["wordcraft"], "web_sha256": hashlib.sha256(old).hexdigest()})
    manager = PluginManager(tmp_path / "node", "updates-test", cache=tmp_path / "plugins", download=lambda url, path: path.write_bytes(old))
    manager.install("wordcraft")
    source = {"kind": "public", "url": "https://github.com/owner/plugins/releases/latest/download/wordcraft-latest.json",
              "public_key": public_key_from_private(KEY), "auto_update": False}
    payload = {"kind": "ryn.plugin.release", "schema": 1, "id": "wordcraft", "version": "0.8.0",
               "runtime": "static-web", "permissions": [], "min_host_version": "0.7.0", "max_host_version": "0.9.0",
               "url": "https://github.com/owner/plugins/releases/download/v0.8.0/wordcraft.zip",
               "size": len(new), "sha256": hashlib.sha256(new).hexdigest()}
    fixture = SimpleNamespace(manager=manager, source=source, payload=payload, new=new, calls=[])
    def transfer(source, url, destination=None):
        fixture.calls.append(url)
        if destination:
            destination.write_bytes(fixture.new)
        else:
            return json.dumps(sign_payload(fixture.payload, private_key_bytes=KEY).to_dict()).encode()
    monkeypatch.setattr(plugin_updates, "transfer", transfer)
    manager.updates.configure("wordcraft", source)
    yield fixture
    manager.close()


def test_signed_update_survives_restart_and_retains_disabled_state(installed):
    f = installed
    f.manager.set_enabled("wordcraft", False)
    assert f.manager.updates.check("wordcraft")["plugins"][0]["available_version"] == "0.8.0"
    result = f.manager.updates.apply("wordcraft")["plugins"][0]
    assert result["state"] == "updated" and result["can_rollback"]
    assert not f.manager._row("wordcraft")["enabled"]
    assert "new editor" in (f.manager.verify("wordcraft") / "index.html").read_text()
    assert (f.manager.cache / "wordcraft/v0.6.1").is_dir()
    again = PluginManager(f.manager.state_path.parent, f.manager.peer_id, cache=f.manager.cache)
    assert again.version("wordcraft") == "0.8.0"
    assert again.catalog()["plugins"][1]["id"]  # the catalog still loads independently of the pinned version
    again.updates.rollback("wordcraft")
    assert again.version("wordcraft") == CRAFT["wordcraft"]["tag"]
    assert not again._row("wordcraft")["enabled"]
    assert "old editor" in (again.verify("wordcraft") / "index.html").read_text()
    assert again.updates.check("wordcraft")["plugins"][0]["last_error"] == "update_version_quarantined"
    again.close()


def test_update_never_replaces_leased_or_open_editor(installed):
    m = installed.manager
    m.updates.check("wordcraft")
    with m.lease("wordcraft"):
        with pytest.raises(craft_host.CraftError, match="plugin_busy"):
            m.updates.apply("wordcraft")
    m.servers["wordcraft"] = (None, None, None)
    assert m.updates.apply("wordcraft")["plugins"][0]["state"] == "waiting_for_idle"
    assert m.version("wordcraft") == "v0.6.1"
    m.servers.pop("wordcraft")
    assert len(installed.calls) == 1  # only the manifest, no package download
    m.updates.apply("wordcraft")
    assert m.version("wordcraft") == "0.8.0"


def test_corrupt_download_keeps_active_version_and_can_retry(installed):
    m = installed.manager
    m.updates.check("wordcraft")
    installed.new = b"bad"
    with pytest.raises(craft_host.CraftError, match="update_size_mismatch"):
        m.updates.apply("wordcraft")
    assert m.version("wordcraft") == "v0.6.1"
    assert "old editor" in (m.verify("wordcraft") / "index.html").read_text()
    assert not (m.cache / "wordcraft/0.8.0").exists()


def test_startup_restores_old_package_when_active_files_are_corrupt(installed):
    m = installed.manager
    m.updates.check("wordcraft")
    m.updates.apply("wordcraft")
    (m.verify("wordcraft") / "index.html").write_text("tampered")
    again = PluginManager(m.state_path.parent, m.peer_id, cache=m.cache)
    assert again.version("wordcraft") == "v0.6.1"
    assert again.updates.status()["plugins"][0]["state"] == "rolled_back"
    again.close()


def test_host_upgrade_disables_incompatible_plugin_without_breaking_catalog(installed, monkeypatch):
    m = installed.manager
    m.updates.check("wordcraft")
    m.updates.apply("wordcraft")
    state = m._read()
    state["plugins"]["wordcraft"].pop("previous")
    m._write(state)
    monkeypatch.setattr(plugin_updates, "__version__", "1.0.0")
    again = PluginManager(m.state_path.parent, m.peer_id, cache=m.cache)
    row = next(row for row in again.catalog()["plugins"] if row["id"] == "wordcraft")
    assert row["installed"] and not row["enabled"]
    assert again.updates.status()["plugins"][0]["last_error"] == "update_incompatible_host"
    with pytest.raises(craft_host.CraftError, match="update_incompatible_host"):
        again.set_enabled("wordcraft", True)
    again.close()


@pytest.mark.parametrize("change,reason", [
    ({"id": "pdfcraft"}, "update_manifest_invalid"),
    ({"version": "../../evil"}, "update_manifest_invalid"),
    ({"version": "0.9.0-rc.1"}, "update_prerelease_not_supported"),
    ({"permissions": ["filesystem"]}, "update_requires_host_or_permissions"),
    ({"runtime": "python"}, "update_requires_host_or_permissions"),
    ({"min_host_version": "1.0.0", "max_host_version": "2.0.0"}, "update_incompatible_host"),
    ({"min_host_version": "1.0.0", "max_host_version": "0.9.0"}, "update_manifest_invalid"),
    ({"url": "https://github.com/attacker/plugins/releases/download/v1/file.zip"}, "update_download_origin_rejected"),
])
def test_signed_but_incompatible_or_unsafe_manifest_is_rejected(installed, change, reason):
    f = installed
    raw = sign_payload({**f.payload, **change}, private_key_bytes=KEY).to_dict()
    with pytest.raises(craft_host.CraftError, match=reason):
        plugin_updates.verify_release(raw, f.source, "wordcraft")


def test_signature_and_company_origin_are_enforced(installed):
    f = installed
    raw = sign_payload(f.payload, private_key_bytes=b"x" * 32).to_dict()
    with pytest.raises(craft_host.CraftError, match="update_manifest_invalid"):
        plugin_updates.verify_release(raw, f.source, "wordcraft")
    source = {**f.source, "kind": "company", "url": "https://plugins.company.test/api/wordcraft/latest", "token_env": "RYN_PLUGIN_TOKEN_COMPANY"}
    plugin_updates.validate_source(source)
    raw = sign_payload({**f.payload, "url": "https://plugins.company.test/packages/wordcraft.zip"}, private_key_bytes=KEY).to_dict()
    assert plugin_updates.verify_release(raw, source, "wordcraft")["version"] == "0.8.0"
    with pytest.raises(craft_host.CraftError, match="update_download_origin_rejected"):
        plugin_updates.verify_release(sign_payload(f.payload, private_key_bytes=KEY).to_dict(), source, "wordcraft")


def test_failed_check_clears_previous_candidate_and_toggle_preserves_it(installed, monkeypatch):
    m = installed.manager
    m.updates.check("wordcraft")
    m.updates.configure("wordcraft", {**installed.source, "auto_update": True})
    assert m.updates.status()["plugins"][0]["available_version"] == "0.8.0"
    monkeypatch.setattr(plugin_updates, "transfer", lambda *args: b"invalid json")
    assert m.updates.check("wordcraft")["plugins"][0]["state"] == "error"
    with pytest.raises(craft_host.CraftError, match="update_not_available"):
        m.updates.apply("wordcraft")


def test_background_poll_only_updates_explicitly_subscribed_installed_plugins(installed):
    m = installed.manager
    m.updates.poll_once()
    assert installed.calls == []
    m.updates.configure("wordcraft", {**installed.source, "auto_update": True})
    with m.lease("wordcraft"):
        m.updates.poll_once()
        assert len(installed.calls) == 1
        assert m.version("wordcraft") == "v0.6.1"
    m.updates.poll_once()
    assert m.version("wordcraft") == "0.8.0"
    assert len(installed.calls) == 2
    m.updates.poll_once()
    assert len(installed.calls) == 2
    m.uninstall("wordcraft")
    m.updates.data["plugins"]["wordcraft"]["last_check"] = 0
    m.updates.poll_once()
    assert len(installed.calls) == 2


def test_update_routes_require_local_owner_before_parsing_body(tmp_path):
    app = FastAPI()
    def local(request):
        if request.headers.get("x-owner") != "yes":
            raise HTTPException(403)
    install_plugin_routes(app, home=tmp_path, peer_id="update-routes", local_control=local)
    with TestClient(app) as client:
        assert client.get("/api/local/plugin-updates").status_code == 403
        assert client.post("/api/local/plugin-updates/wordcraft/configure", content=b"not-json").status_code == 403
        assert client.get("/api/local/plugin-updates", headers={"x-owner": "yes"}).json()["plugins"] == []
        assert client.post("/api/local/plugin-updates/wordcraft/unknown", json={}, headers={"x-owner": "yes"}).status_code == 400
        headers = {"x-owner": "yes"}
        assert client.get("/api/local/plugin-updates/readiness").status_code == 403
        assert client.get("/api/local/plugin-updates/readiness", headers=headers).json()["ready"]
        app.state.agent_runtime = SimpleNamespace(_active_lock=threading.Lock(), _active_runs={1: {"task_id": "running"}})
        assert client.get("/api/local/plugin-updates/readiness", headers=headers).json()["blockers"] == ["agent_running"]


@pytest.mark.parametrize("url", ["http://company.test/latest", "https://user:password@company.test/latest", "file:///tmp/plugin", "https://company.test:444/latest"])
def test_insecure_sources_rejected(installed, url):
    with pytest.raises(craft_host.CraftError):
        plugin_updates.validate_source({**installed.source, "kind": "company", "url": url})


def test_corrupt_update_settings_do_not_prevent_plugin_manager_startup(installed):
    m = installed.manager
    m.updates.path.write_text("broken json")
    again = PluginManager(m.state_path.parent, m.peer_id, cache=m.cache)
    assert again.updates.status()["last_error"] == "update_state_invalid"
    assert "old editor" in (again.verify("wordcraft") / "index.html").read_text()
    assert again.updates.path.read_text() == "broken json"
    again.close()


def test_windows_install_retries_transient_rename(installed, monkeypatch):
    from pathlib import Path

    from rynmesh.node_apps import craft_host

    if craft_host.os.name != "nt":
        pytest.skip("Windows sharing violation")
    original = Path.rename
    attempts = []

    def rename(path, target):
        if path.name.startswith(".install-"):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("scanner sharing violation")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    installed.manager.updates.check("wordcraft")
    installed.manager.updates.apply("wordcraft")
    assert len(attempts) >= 2
    assert installed.manager.version("wordcraft") == "0.8.0"
