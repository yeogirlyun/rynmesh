import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from rynmesh.crypto import public_key_from_private, sign_payload
from rynmesh.node_apps import service_updates as su

KEY = bytes(range(32))
PUBLIC = public_key_from_private(KEY)
OLD, NEW, NEWER = "a" * 40, "b" * 40, "c" * 40
MANIFEST_URL = "https://updates.example.test/ryn/updates/packs/svc/latest.json"
BLOBS = MANIFEST_URL.rsplit("/", 1)[0] + "/blobs/"
TOKEN = ("tok_123", "https://updates.example.test/ryn/connect")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Server:
    """Serves manifests and blobs; records what was requested and with which token."""
    def __init__(self):
        self.files, self.requests, self.redirects = {}, [], {}
        self.status = None

    def opener(self, allowed):
        server = self

        class Opener:
            def open(self, request, timeout):
                url = request.full_url
                server.requests.append((url, request.get_header("Authorization")))
                if server.status:
                    raise su.urllib.error.HTTPError(url, server.status, "denied", {}, None)
                if url in server.redirects:
                    allowed(server.redirects[url])  # what the redirect handler does
                    url = server.redirects[url]
                if url not in server.files:
                    raise su.urllib.error.HTTPError(url, 404, "missing", {}, None)
                return Response(server.files[url])
        return Opener()

    def publish(self, files, revision=NEW, version="1.1.0", key=KEY, **extra):
        payload = {"kind": "ryn.service.release", "schema": 1, "id": "svc", "version": version, "revision": revision,
                   "program": "svc.exe", "files": {p: {"sha256": sha(d), "size": len(d)} for p, d in files.items()},
                   **extra}
        for data in files.values():
            self.files[BLOBS + sha(data)] = data
        self.files[MANIFEST_URL] = json.dumps(sign_payload(payload, private_key_bytes=key).to_dict()).encode()
        return payload


class Store:
    def __init__(self, enabled=True):
        self.enabled, self.migrated = enabled, []

    def overview(self):
        return {"plugins": [{"id": "p1", "name": "Plugin One", "enabled": self.enabled}]}

    def migrate_release_defaults(self, declarations):
        self.migrated.append(declarations)
        return {"conflicts": []}

    def _read(self):
        return {"skills": {}, "release_defaults": {}}

    def trust_installer_connections(self, declarations):
        return []


def write_runtime(directory: Path, revision, version, files):
    directory.mkdir(parents=True, exist_ok=True)
    for path, data in files.items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_bytes(data)
    (directory / "runtime-manifest.json").write_text(json.dumps({"schema": 1, "service": "svc", "source_commit": revision,
        **({"version": version} if version else {}), "files": {p: sha(d) for p, d in files.items()}}))


@pytest.fixture
def root(tmp_path):
    write_runtime(tmp_path / f"svc-{OLD[:16]}", OLD, "1.0.0",
                  {"svc.exe": b"old program", "lib/shared.dat": b"unchanged library"})
    (tmp_path / "services.json").write_text(json.dumps({"version": 1, "services": [{
        "id": "svc", "plugins": ["p1"], "program": f"svc-{OLD[:16]}/svc.exe", "port": 18990,
        "health_path": "/health", "environment": {"APP_HOME": "x"}}]}))
    (tmp_path / "updates.json").write_text(json.dumps({"schema": 1, "services": {"svc": {
        "name": {"zh-CN": "示例插件", "en": "Sample plugins"},
        "plugins": ["p1"],
        "publisher": {"zh-CN": "示例服务器", "en": "the sample server"},
        "url": MANIFEST_URL,
        "public_key": PUBLIC,
        "auth": {"peer_provider": "p1", "label": {"zh-CN": "示例账号", "en": "sample account"}},
    }}}))
    return tmp_path


def make(root, server, *, health=None, store=None, token=TOKEN, activity=None, preferences=None, host="0.9.0"):
    now = {"t": 1000.0}
    updates = su.ServiceUpdates(root, token_source=lambda plugin: token, extensions=lambda: store,
                                health=health or (lambda port, path: (None, {})), opener_factory=server.opener,
                                clock=lambda: now["t"], activity=activity,
                                preferences=preferences or (lambda: {"auto_download": True, "install_on_quit": True}),
                                host_version=host)
    return updates, now


def program(root):
    return json.loads((root / "services.json").read_text())["services"][0]["program"]


def test_stages_a_newer_release_downloading_only_changed_files(root):
    server = Server()
    server.publish({"svc.exe": b"new program", "lib/shared.dat": b"unchanged library"})
    updates, _ = make(root, server, store=Store())
    row = updates.check("svc")
    assert row["state"] == "ready" and row["staged"]["version"] == "1.1.0"
    assert row["staged"]["downloaded_bytes"] == len(b"new program")
    staged = root / f"svc-{NEW[:16]}"
    assert (staged / "svc.exe").read_bytes() == b"new program"
    assert (staged / "lib/shared.dat").read_bytes() == b"unchanged library"
    blobs = [url for url, _ in server.requests if "/blobs/" in url]
    assert blobs == [BLOBS + sha(b"new program")]
    assert all(auth == "Bearer tok_123" for _, auth in server.requests)
    # Staging never touches the running runtime.
    assert program(root) == f"svc-{OLD[:16]}/svc.exe"
    assert json.loads((staged / "runtime-manifest.json").read_text())["source_commit"] == NEW
    status = updates.status()["services"][0]
    assert status["name"] == {"zh-CN": "示例插件", "en": "Sample plugins"} and status["plugin_names"] == ["Plugin One"]
    assert status["publisher"] == {"zh-CN": "示例服务器", "en": "the sample server"}
    assert status["auth_label"] == {"zh-CN": "示例账号", "en": "sample account"}
    assert status["state"] == "ready" and status["available_version"] == "1.1.0"


def test_untrusted_or_tampered_manifests_stage_nothing(root):
    server = Server()
    server.publish({"svc.exe": b"new"}, key=bytes(range(1, 33)))
    updates, _ = make(root, server)
    with pytest.raises(su.ServiceUpdateError, match="untrusted_signer"):
        updates.check("svc")
    server.publish({"svc.exe": b"new"})
    raw = json.loads(server.files[MANIFEST_URL])
    raw["payload"]["version"] = "9.9.9"
    server.files[MANIFEST_URL] = json.dumps(raw).encode()
    with pytest.raises(su.ServiceUpdateError, match="bad_signature"):
        updates.check("svc")
    assert updates._row("svc")["state"] == "error"
    assert not (root / f"svc-{NEW[:16]}").exists()


def test_corrupt_download_fails_and_leaves_no_runtime(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    server.files[BLOBS + sha(b"new program")] = b"evil program"
    updates, _ = make(root, server)
    with pytest.raises(su.ServiceUpdateError, match="download_too_large|checksum"):
        updates.check("svc")
    assert not (root / f"svc-{NEW[:16]}").exists()
    updates.recover()
    assert not list(root.glob(".*.staging"))
    assert program(root) == f"svc-{OLD[:16]}/svc.exe"


def test_cross_origin_redirects_and_urls_are_refused(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    server.redirects[BLOBS + sha(b"new program")] = "https://elsewhere.example.test/blob"
    server.files["https://elsewhere.example.test/blob"] = b"new program"
    updates, _ = make(root, server)
    with pytest.raises(su.ServiceUpdateError, match="download_origin_rejected"):
        updates.check("svc")
    assert not (root / f"svc-{NEW[:16]}").exists()
    for bad in ("http://updates.example.test/latest.json", "https://user:pw@updates.example.test/x",
                "https://updates.example.test/x#frag", "file:///etc/passwd"):
        with pytest.raises(su.ServiceUpdateError):
            su.validate_source("svc", {"url": bad, "public_key": PUBLIC})
    assert su.origin("https://h.test/a") == su.origin("https://H.test:443/b") != su.origin("https://h.test:8443/a")


@pytest.mark.parametrize("path", ["../escape.exe", "C:/x.exe", "a\\b.exe", "data/app.db", "conf/.env",
                                  "x/config.key", "nul.txt", "runtime-manifest.json", "dir/./x"])
def test_unsafe_paths_are_rejected(root, path):
    server = Server()
    server.publish({"svc.exe": b"new", path: b"x"})
    updates, _ = make(root, server)
    with pytest.raises(su.ServiceUpdateError, match="invalid_path|private_file"):
        updates.check("svc")


def test_sign_in_is_only_sent_to_the_issuing_origin(root):
    server = Server()
    server.publish({"svc.exe": b"new"})
    updates, _ = make(root, server, token=("tok", "https://other.example.test/ryn/connect"))
    with pytest.raises(su.ServiceUpdateError, match="auth_origin_mismatch"):
        updates.check("svc")
    assert server.requests == []


def test_without_sign_in_the_state_asks_for_login_and_the_runtime_stays(root):
    server = Server()
    server.publish({"svc.exe": b"new"})

    def no_login(plugin):
        raise RuntimeError("peer_provider_login_required")
    updates = su.ServiceUpdates(root, token_source=no_login, opener_factory=server.opener,
                                preferences=lambda: {"auto_download": True, "install_on_quit": True})
    with pytest.raises(su.ServiceUpdateError, match="login_required"):
        updates.check("svc")
    assert updates.status()["services"][0]["state"] == "login_required" and server.requests == []
    # The server refusing the sign-in reads the same.
    server.status = 401
    updates, _ = make(root, server)
    with pytest.raises(su.ServiceUpdateError, match="login_required"):
        updates.check("svc")
    row = updates._row("svc")
    assert row["state"] == "login_required" and row["next_check"] == 1000.0 + su.RETRY_INTERVAL
    assert program(root) == f"svc-{OLD[:16]}/svc.exe"


def test_release_needing_a_newer_ryn_is_not_downloaded(root):
    server = Server()
    server.publish({"svc.exe": b"new program"}, min_host="0.9.1")
    updates, _ = make(root, server, host="0.9.0")
    row = updates.check("svc")
    assert row["state"] == "requires_host" and row["min_host"] == "0.9.1"
    assert not [url for url, _ in server.requests if "/blobs/" in url]
    assert not (root / f"svc-{NEW[:16]}").exists()
    updates, _ = make(root, server, host="0.9.1")
    assert updates.check("svc")["state"] == "ready"


def test_never_downgrades_the_installed_runtime(root):
    server = Server()
    server.publish({"svc.exe": b"older program"}, version="0.9.0")
    updates, _ = make(root, server)
    assert updates.check("svc")["state"] == "up_to_date"
    assert not (root / f"svc-{NEW[:16]}").exists()
    server.publish({"svc.exe": b"same version, other build"}, version="1.0.0")
    assert updates.check("svc")["state"] == "up_to_date"


def test_switch_migrates_declarations_and_verifies_the_new_revision(root):
    server = Server()
    declarations = {"plugins": [], "skills": [], "mcp_servers": []}
    server.publish({"svc.exe": b"new program", "ryn-declarations.json": json.dumps(declarations).encode()},
                   declarations="ryn-declarations.json", notes="Faster reports")
    store = Store()
    health = {"value": (200, {"revision": OLD})}
    updates, now = make(root, server, store=store, health=lambda port, path: health["value"])
    updates.check("svc")
    row = updates.apply("svc")
    assert row["state"] == "applying" and program(root) == f"svc-{NEW[:16]}/svc.exe"
    assert store.migrated == [declarations]
    assert json.loads((root / "services.json").read_text())["services"][0]["environment"] == {"APP_HOME": "x"}
    # Neither the old revision nor a missing revision counts as healthy.
    now["t"] += 60
    assert updates.verify("svc")["state"] == "applying"
    health["value"] = (200, {})
    assert updates.verify("svc")["state"] == "applying"
    health["value"] = (200, {"revision": NEW})
    assert updates.verify("svc")["state"] == "up_to_date"
    status = updates.status()["services"][0]
    assert status["current_version"] == "1.1.0" and status["updated"]["version"] == "1.1.0"
    assert status["updated"]["notes"] == "Faster reports"
    updates.acknowledge("svc")
    assert updates.status()["services"][0]["updated"] is None
    # The previous runtime is kept for one more release.
    assert (root / f"svc-{OLD[:16]}").is_dir()


def test_busy_plugins_defer_automatic_switch_while_force_switches_immediately(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    busy = {"health": True, "agent": None}
    updates, now = make(root, server, store=Store(),
                        health=lambda port, path: (200, {"revision": OLD, "busy": busy["health"]}),
                        activity=lambda: busy["agent"])
    row = updates.check("svc")
    assert row["state"] == "waiting" and row["waiting_for"] == "plugin_busy"
    status = updates.status()["services"][0]
    assert status["state"] == "waiting" and status["waiting_for"] == "plugin_busy"
    # Automatic switch (apply without force, startup recover, quit) defers while busy.
    assert updates.apply("svc")["state"] == "waiting"
    updates.quit()
    assert program(root) == f"svc-{OLD[:16]}/svc.exe"
    busy.update(health=False, agent="agent_running")
    now["t"] += 5
    updates.poll_once()
    assert updates._row("svc")["waiting_for"] == "agent_running"
    busy["agent"] = None
    now["t"] += 5
    updates.poll_once()  # the remembered request switches once idle
    assert program(root) == f"svc-{NEW[:16]}/svc.exe"
    # Explicit "Update now" (force=True) switches even while busy.
    server.publish({"svc.exe": b"newer"}, revision=NEWER, version="1.2.0")
    updates._put("svc", verify=None)
    updates.check("svc")
    busy["health"] = True
    assert updates.apply("svc", force=True)["state"] == "applying"


def test_unhealthy_release_rolls_back_with_its_declarations_and_is_never_retried(root):
    server = Server()
    old_declarations = {"plugins": [], "skills": [], "mcp_servers": [], "release": "old"}
    (root / f"svc-{OLD[:16]}" / "ryn-declarations.json").write_text(json.dumps(old_declarations))
    new_declarations = {"plugins": [], "skills": [], "mcp_servers": [], "release": "new"}
    server.publish({"svc.exe": b"broken program", "ryn-declarations.json": json.dumps(new_declarations).encode()},
                   declarations="ryn-declarations.json")
    store = Store()
    updates, now = make(root, server, store=store, health=lambda port, path: (None, {}))
    updates.check("svc")
    updates.apply("svc")
    now["t"] += su.VERIFY_TIMEOUT + 1
    row = updates.verify("svc")
    assert row["state"] == "rolled_back" and row["last_error"] == "service_update_unhealthy"
    assert program(root) == f"svc-{OLD[:16]}/svc.exe" and NEW in row["quarantined"]
    assert [d["release"] for d in store.migrated] == ["new", "old"]
    status = updates.status()["services"][0]
    assert status["rolled_back_version"] == "1.1.0" and status["current_version"] == "1.0.0"
    now["t"] += su.CHECK_INTERVAL
    assert updates.check("svc")["state"] == "rolled_back"
    assert updates._row("svc").get("staged") is None
    # Quarantined revisions are never retried, even on manual retry; only a newer revision stages.
    assert updates.retry("svc")["state"] == "rolled_back"
    assert updates._row("svc").get("staged") is None
    server.publish({"svc.exe": b"fixed program"}, revision=NEWER, version="1.1.1")
    assert updates.retry("svc")["state"] == "ready"
    assert updates._row("svc")["staged"]["revision"] == NEWER


def test_runtime_not_in_use_switches_without_waiting_for_health(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    updates, _ = make(root, server, store=Store(enabled=False))
    updates.check("svc")
    updates.apply("svc")
    assert updates.verify("svc")["state"] == "up_to_date"


def test_staged_release_switches_at_start_and_at_quit_when_allowed(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    preferences = {"auto_download": True, "install_on_quit": False}
    updates, _ = make(root, server, preferences=lambda: preferences)
    updates.check("svc")
    updates.recover()
    assert program(root) == f"svc-{NEW[:16]}/svc.exe"
    server.publish({"svc.exe": b"newer program"}, revision=NEWER, version="1.2.0")
    updates._put("svc", verify=None)
    updates.check("svc")
    updates.quit()
    assert program(root) == f"svc-{NEW[:16]}/svc.exe"  # "install when Ryn quits" is off
    preferences["install_on_quit"] = True
    updates.quit()
    assert program(root) == f"svc-{NEWER[:16]}/svc.exe"


def test_background_checks_follow_the_download_switch_and_retry_after_failures(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    preferences = {"auto_download": False, "install_on_quit": True}
    updates, now = make(root, server, preferences=lambda: preferences)
    updates.poll_once()
    assert server.requests == []
    preferences["auto_download"] = True
    server.status = 500
    updates.poll_once()
    assert updates._row("svc")["state"] == "error"
    now["t"] += su.RETRY_INTERVAL - 1
    count = len(server.requests)
    updates.poll_once()
    assert len(server.requests) == count
    now["t"] += 2
    server.status = None
    updates.poll_once()
    assert updates._row("svc")["state"] == "ready"
    assert updates._row("svc")["next_check"] == now["t"] + su.CHECK_INTERVAL


def test_full_install_moving_forward_drops_an_older_staged_release(root):
    server = Server()
    server.publish({"svc.exe": b"new program"})
    updates, _ = make(root, server)
    updates.check("svc")
    manifest = root / f"svc-{OLD[:16]}" / "runtime-manifest.json"
    value = json.loads(manifest.read_text())
    value["version"] = "2.0.0"
    manifest.write_text(json.dumps(value))
    assert updates.apply("svc")["state"] == "up_to_date"
    assert program(root) == f"svc-{OLD[:16]}/svc.exe"


def test_reinstalling_older_full_package_does_not_downgrade_updated_runtime(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "full_install_antidowngrade",
        Path(__file__).resolve().parents[1] / "scripts/full_package_install.py",
    )
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.setattr(installer, "allow_desktop_user", lambda path: None)
    inits = []
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: inits.append(cmd[0]))

    # Build an older full-package payload (v1 = 2026.11.12.100000).
    payload = tmp_path / "payload"
    company = payload / "company"
    company.mkdir(parents=True)
    exe_v1 = company / "company-service.exe"
    exe_v1.write_bytes(b"v1 runtime")
    (company / "runtime-manifest.json").write_text(json.dumps({
        "schema": 1, "service": "company", "source_commit": OLD,
        "version": "2026.11.12.100000", "files": {"company-service.exe": sha(b"v1 runtime")},
    }))
    declarations_v1 = {
        "plugins": [{"id": p, "name": p, "version": "1", "skill_ids": [], "mcp_server_ids": [],
                     "web_app": {"url": "http://127.0.0.1:18140/apps/" + p, "permissions": []}}
                    for p in installer.COMPANY],
        "skills": [], "mcp_servers": [],
    }
    (payload / "company-plugins.json").write_text(json.dumps(declarations_v1))
    (payload / "service-updates.json").write_text(json.dumps({
        "schema": 1, "services": {"company": {
            "name": {"zh-CN": "公司插件", "en": "Company plugins"},
            "plugins": list(installer.COMPANY),
            "url": "https://dashboard-hk-api.gshopper.com.cn/ryn/updates/packs/company/latest.json",
            "public_key": PUBLIC,
            "publisher": {"zh-CN": "公司服务器", "en": "the company server"},
            "auth": {"peer_provider": "company-workspace", "label": {"zh-CN": "公司账号", "en": "company account"}},
        }},
    }))
    files = {p.relative_to(payload).as_posix(): installer.sha(p) for p in payload.rglob("*") if p.is_file()}
    (payload / "full-package.json").write_text(json.dumps({
        "schema": 1, "ryn_commit": OLD, "company_commit": OLD,
        "plugins": list(installer.COMPANY), "files": files,
    }))

    home, dest = tmp_path / "home", tmp_path / "install"
    home.mkdir()
    dest.mkdir()
    (dest / "Ryn.exe").write_bytes(b"core")
    installer.install(payload, dest, home)
    assert (dest / "plugin-services/updates.json").is_file()

    # Simulate background update moving active runtime to v2 = 2026.11.12.120000.
    services_root = dest / "plugin-services"
    v2_dir = services_root / f"company-{NEW[:16]}"
    declarations_v2 = {
        "plugins": [{"id": p, "name": p, "version": "2", "skill_ids": [], "mcp_server_ids": [],
                     "web_app": {"url": "http://127.0.0.1:18140/apps/" + p, "permissions": []}}
                    for p in installer.COMPANY],
        "skills": [], "mcp_servers": [],
        "previous_defaults": [{"plugins": {p["id"]: p for p in declarations_v1["plugins"]}}],
    }
    v2_decl_bytes = json.dumps(declarations_v2).encode("utf-8")
    write_runtime(v2_dir, NEW, "2026.11.12.120000", {
        "company-service.exe": b"v2 runtime",
        "ryn-declarations.json": v2_decl_bytes,
    })
    services_cfg = json.loads((services_root / "services.json").read_text())
    services_cfg["services"][0]["program"] = f"company-{NEW[:16]}/company-service.exe"
    (services_root / "services.json").write_text(json.dumps(services_cfg))

    # Reinstalling the v1 full package must NOT downgrade v2!
    installer.install(payload, dest, home)
    services_after = json.loads((services_root / "services.json").read_text())
    assert services_after["services"][0]["program"] == f"company-{NEW[:16]}/company-service.exe"
    receipt = json.loads((home / "full-package-install.json").read_text())
    assert receipt["kept_newer_runtime"] is True
    assert receipt["company_commit"] == NEW


def test_without_update_sources_nothing_runs(tmp_path):
    updates = su.ServiceUpdates(tmp_path)
    assert updates.status() == {"services": []}
    updates.start()
    updates.close()
    assert su.ServiceUpdates(None).status() == {"services": []}


def test_preferences_follow_the_desktop_switches(tmp_path):
    path = tmp_path / "preferences.json"
    assert su.read_preferences(path) == {"auto_download": True, "install_on_quit": True}
    path.write_text(json.dumps({"autoDownload": False, "installOnQuit": False, "lastVersion": "0.9.0"}))
    assert su.read_preferences(path) == {"auto_download": False, "install_on_quit": False}
    path.write_text("not json")
    assert su.read_preferences(path) == {"auto_download": True, "install_on_quit": True}


def test_routes_are_owner_only_and_safe_without_sources(root, monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    def owner(request):
        if request.headers.get("x-owner") != "1":
            raise HTTPException(403, "owner_only")
    for directory in (None, str(root)):
        if directory:
            monkeypatch.setenv("RYN_PLUGIN_SERVICES_DIR", directory)
        else:
            monkeypatch.delenv("RYN_PLUGIN_SERVICES_DIR", raising=False)
        app = FastAPI()
        su.install_service_update_routes(app, local_control=owner)
        app.state.service_updates.start = lambda: None
        with TestClient(app) as client:
            assert client.get("/api/local/service-updates").status_code == 403
            assert client.post("/api/local/service-updates/svc/apply").status_code == 403
            owner_headers = {"x-owner": "1"}
            status = client.get("/api/local/service-updates", headers=owner_headers).json()
            assert client.post("/api/local/service-updates/quit", headers=owner_headers).status_code == 200
            missing = client.post("/api/local/service-updates/other/apply", headers=owner_headers)
            assert missing.status_code == 404
            if directory:
                assert client.post("/api/local/service-updates/svc/apply", headers=owner_headers).status_code == 409
        assert [row["id"] for row in status["services"]] == ([] if directory is None else ["svc"])


def test_published_pack_round_trips_through_the_updater(root, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import publish_service_pack as publisher
    runtime = tmp_path / "dist" / "svc-runtime"
    write_runtime(runtime, NEW, "1.3.0", {"svc.exe": b"published program", "lib/shared.dat": b"unchanged library"})
    skills = tmp_path / "skills" / "s1"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("Use the reports.")
    key_file = tmp_path / "keys" / "publisher.key"
    public = publisher.generate_key(key_file)
    assert len(su.base64.b64decode(public)) == 32 and len(publisher.read_key(key_file)) == 32
    with pytest.raises(FileExistsError):
        publisher.generate_key(key_file)
    out = tmp_path / "site"
    declarations = {"plugins": [], "skills": [], "mcp_servers": []}
    payload = publisher.publish(runtime, out, KEY, service="svc", program="svc.exe", declarations=declarations,
                                skills_root=tmp_path / "skills", min_host="0.9.0", notes="Notes", previous="1.2.0")
    assert payload["version"] == "1.3.0" and payload["declarations"] == "ryn-declarations.json"
    assert payload["skills"] == "ryn-skills" and "ryn-skills/s1/SKILL.md" in payload["files"]
    assert len(list((out / "blobs").iterdir())) == 4
    with pytest.raises(ValueError, match="not higher"):
        publisher.publish(runtime, tmp_path / "site2", KEY, service="svc", program="svc.exe", previous="1.3.0")
    with pytest.raises(FileExistsError):
        publisher.publish(runtime, out, KEY, service="svc", program="svc.exe")
    server = Server()
    server.files[MANIFEST_URL] = (out / "latest.json").read_bytes()
    for blob in (out / "blobs").iterdir():
        server.files[BLOBS + blob.name] = blob.read_bytes()
    store = Store()
    updates, _ = make(root, server, store=store)
    assert updates.check("svc")["state"] == "ready"
    updates.apply("svc")
    assert store.migrated == [declarations]
    assert (root / f"svc-{NEW[:16]}" / "ryn-skills" / "s1" / "SKILL.md").read_text() == "Use the reports."
    (runtime / "svc.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        publisher.publish(runtime, tmp_path / "site3", KEY, service="svc", program="svc.exe")


def store_at(tmp_path):
    from rynmesh.agent_runtime.extensions.store import ExtensionStore
    from rynmesh.services.peer_box import load_or_create_messaging_key
    return ExtensionStore(tmp_path / "agent", load_or_create_messaging_key(tmp_path / "messaging.x25519"))


def release(version, content, url="https://reports.example.test/mcp"):
    return {
        "plugins": [{"id": "p1", "name": "Reports", "title": "Reports", "version": version,
                     "skill_ids": ["s1"], "mcp_server_ids": ["remote"]}],
        "skills": [{"id": "s1", "name": "s1", "title": "Reports", "version": version, "content": content,
                    "plugin_id": "p1"}],
        "mcp_servers": [{"id": "remote", "name": "Reports", "transport": "streamable_http", "url": url,
                         "plugin_id": "p1", "auth": {"mode": "oauth", "client_id": "ryn-desktop", "scopes": ["r.read"]}}],
    }


def skill_files(root: Path, content, extra):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(content, encoding="utf-8")
    (root / "guide.md").write_text(extra, encoding="utf-8")
    return root.parent


def test_release_declarations_keep_skill_files_and_remote_trust_in_step(tmp_path):
    from rynmesh.node_apps.release_declarations import apply_release_declarations
    store = store_at(tmp_path)
    first = release("1", "Version one")
    store.commit_imported_items(plugins=first["plugins"], skills=first["skills"], mcp_servers=first["mcp_servers"])
    store.set_plugin_enabled("p1", True)
    store.set_skill_enabled("s1", True)
    v1 = skill_files(tmp_path / "v1" / "s1", "Version one", "old guide")
    apply_release_declarations(store, first, skills_root=v1, service_id="svc", port=18990, plugin_ids=["p1"])
    skill = store._read()["skills"]["s1"]
    assert skill["package_dir"] and (Path(skill["package_dir"]) / "guide.md").read_text() == "old guide"
    old_dir = Path(skill["package_dir"])
    second = release("2", "Version two")
    v2 = skill_files(tmp_path / "v2" / "s1", "Version two", "new guide")
    result = apply_release_declarations(store, second, skills_root=v2, service_id="svc", port=18990, plugin_ids=["p1"])
    skill = store._read()["skills"]["s1"]
    assert result["skills_refreshed"] == ["s1"] and skill["content"] == "Version two" and skill["enabled"]
    assert (Path(skill["package_dir"]) / "guide.md").read_text() == "new guide"
    assert not old_dir.exists()
    assert "remote" in result["trusted_connections"]
    # Verify read-only tools discovered after update are auto-trusted with zero prompt candidates!
    row = store.get_mcp_server_internal("remote")
    row = store.record_mcp_discovery(
        "remote", expected_revision=row["revision"], status="ready",
        discovered_tools=[
            {"name": "query_report", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}},
            {"name": "mutate_report", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": False}},
        ],
    )
    assert row["read_only_sources"] == {"query_report": "installer"}
    assert row["server_hint_candidates"] == []
    # A skill the owner edited is kept as it is.
    skill_path = Path(skill["package_dir"]) / "guide.md"
    skill_path.write_text("owner notes")
    third = release("3", "Version three")
    v3 = skill_files(tmp_path / "v3" / "s1", "Version three", "third guide")
    result = apply_release_declarations(store, third, skills_root=v3, service_id="svc", port=18990, plugin_ids=["p1"])
    assert result["skills_refreshed"] == [] and skill_path.read_text() == "owner notes"


def test_generic_updater_modules_contain_no_company_or_wecom_terms():
    repo = Path(__file__).resolve().parents[1]
    generic_files = [
        repo / "rynmesh/node_apps/service_updates.py",
        repo / "rynmesh/node_apps/release_declarations.py",
        repo / "webapp/src-tauri/src/node.rs",
        repo / "webapp/src-tauri/src/updates.rs",
    ]
    forbidden = ("公司", "企业微信", "wecom", "gshopper", "company-workspace", "company-dashboard", "company-affiliate")
    for file_path in generic_files:
        content = file_path.read_text(encoding="utf-8")
        lowered = content.lower()
        for term in forbidden:
            assert term.lower() not in lowered, f"{file_path.name} must remain generic, found {term!r}"
    # UpdatesPanel.tsx: everything before the legacy Craft PluginUpdatesPanel must remain completely generic.
    updates_panel = (repo / "webapp/src/screens/UpdatesPanel.tsx").read_text(encoding="utf-8")
    desktop_panel_section = updates_panel.split("export function PluginUpdatesPanel()", 1)[0]
    for term in forbidden:
        assert term.lower() not in desktop_panel_section.lower(), f"DesktopUpdatesPanel must remain generic, found {term!r}"
