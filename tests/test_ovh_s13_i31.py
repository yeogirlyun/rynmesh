"""S13-I31 local-control credentials, real handler gates and token storage."""
import asyncio
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from rynmesh import node_auth
from rynmesh.agent_runtime import prompt
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore
from test_agent_local_tools import local
from test_agent_runtime import make_runtime

ORIGINAL_LOOPBACK = set(node_auth._LOOPBACK)
ORIGINAL_HOSTS = set(node_auth._LOCAL_HOST_NAMES)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "launch-secret-for-test")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_DISABLE_DISCOVERY", "1")
    store = RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")
    return create_app(store)


def test_t1_loopback_without_launch_token_is_forbidden(app):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000))
    assert client.get("/api/local/node/status").status_code == 403


def test_t2_launch_token_and_browser_cookie_are_accepted(app, monkeypatch):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000))
    assert client.get("/api/local/node/status", headers={"X-Ryn-Local-Token": "launch-secret-for-test"}).status_code == 200
    token = node_auth.NodeAuth(Path(os.environ["RYNMESH_HOME"])).token()
    assert client.post("/api/local/auth/unlock", json={"token": token}).status_code == 200
    assert client.get("/api/local/node/status").status_code == 200


def test_t2_webview_preflight_allows_launch_header_without_opening_control(app):
    client = TestClient(app)
    response = client.options("/api/local/node/status", headers={
        "Origin": "http://tauri.localhost", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Ryn-Local-Token"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert client.get("/api/local/node/status", headers={"Origin": "http://tauri.localhost"}).status_code == 403


@pytest.mark.parametrize("mode", ["automatic", "confirm_changes", "workspace"])
def test_t3_real_shell_receives_token_only_in_automatic(local, monkeypatch, mode):
    tools, context, workspace, state = local
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "launch-secret-for-test")
    monkeypatch.setenv("RYN_LOCAL_CONTROL_TOKEN", "inherited-secret-must-be-removed")
    state.transaction(lambda data: data["tasks"]["task"].update(permission_mode=mode))
    (workspace / "probe.py").write_text("import os\nprint('control=[' + os.environ.get('RYN_LOCAL_CONTROL_TOKEN', '') + '] node=[' + os.environ.get('RYNMESH_LOCAL_TOKEN', '') + ']')\n", encoding="utf-8")
    command = f'{sys.executable} probe.py' if os.name == "nt" else f'"{sys.executable}" probe.py'
    # The host already authorizes this literal read. Exercise LocalTools -> Processes -> child.
    result = tools.perform("ryn_shell_execute", {"command": command, "shell": "cmd" if os.name == "nt" else "sh", "yield_seconds": 10}, context, lambda: False)
    assert ("launch-secret-for-test" in result["output"]) == (mode == "automatic")
    assert "inherited-secret-must-be-removed" not in result["output"]
    assert "node=[]" in result["output"]


def test_t3_prompt_only_exposes_control_api_to_automatic(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_PEER_PORT", "8791")
    runtime, *_ = make_runtime(tmp_path)
    task = runtime.create({"message": "test"})
    row = runtime.store.read()["tasks"][task["id"]]
    for mode in ["workspace", "confirm_changes", "read_only", "automatic"]:
        row["permission_mode"] = mode
        text = prompt.authority(row)
        control = json.JSONDecoder().raw_decode(text.split("Server control state: ", 1)[1])[0]
        assert ("local_control_api" in control) == (mode == "automatic")
        if mode == "automatic":
            assert control["local_control_api"]["token_header"] == "X-Ryn-Local-Token"
            assert "RYN_LOCAL_CONTROL_TOKEN" in text


@pytest.mark.parametrize("path,kwargs", [
    ("/api/local/agent/extensions/mcp/restart", {}),
    ("/api/local/agent/extensions/mcp/apps/clear-data", {}),
    ("/api/local/messages/send", {}),
    ("/api/local/messages/stream", {}),
    ("/api/local/messages/attachment/{msg_id}", {"msg_id": "missing", "peer_id": "peer"}),
    ("/api/local/messages", {"peer_id": "peer"}),
])
def test_t4_handlers_reject_without_middleware(app, path, kwargs, monkeypatch):
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    route = next(route for route in app.routes if getattr(route, "path", "") == path)
    request = Request({"type": "http", "method": "GET", "path": path, "headers": [], "client": ("127.0.0.1", 30000), "server": ("localhost", 80), "query_string": b""})
    import inspect
    with pytest.raises(HTTPException) as exc:
        value = route.endpoint(request=request, **kwargs)
        if inspect.isawaitable(value):
            asyncio.run(value)
    assert exc.value.status_code == 403


def test_t5_spoofed_forwarded_addresses_do_not_reset_unlock_limit(app):
    client = TestClient(app, base_url="http://localhost", client=("127.0.0.1", 30000))
    for index in range(9):
        response = client.post("/api/local/auth/unlock", json={"token": "wrong"}, headers={"x-forwarded-for": f"192.0.2.{index}", "cf-access-jwt-assertion": "forged"})
    assert response.status_code == 429


def test_t5_unreadable_token_is_preserved_and_reported(tmp_path, monkeypatch, app):
    path = Path(os.environ["RYNMESH_HOME"]) / "control_token"
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    existing = path.read_bytes()
    real_read = Path.read_text
    def read(file, *args, **kwargs):
        if file == path:
            raise PermissionError("locked")
        return real_read(file, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(PermissionError):
        node_auth.NodeAuth(path.parent).token()
    assert path.read_bytes() == existing
    response = TestClient(app).get("/api/local/auth/status")
    assert response.status_code == 503
    assert response.json()["detail"] == "device_token_unreadable"
    assert path.read_bytes() == existing


def signed_jwt(key, *, kid="k1", issuer="https://team.example.test", nbf=None):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    payload = {"aud": "aud", "exp": time.time() + 300, "iss": issuer}
    if nbf is not None:
        payload["nbf"] = nbf
    content = encode({"alg": "RS256", "kid": kid}) + "." + encode(payload)
    signature = key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    return content + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")


@pytest.fixture
def perimeter(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    def encode(number):
        return base64.urlsafe_b64encode(number.to_bytes((number.bit_length() + 7) // 8, "big")).decode().rstrip("=")
    auth = node_auth.NodeAuth(tmp_path, access_team_domain="team.example.test", access_audience="aud")
    auth._jwks = {"keys": [{"kid": "k1", "e": encode(public.e), "n": encode(public.n)}]}
    auth._jwks_fetched_at = time.time()
    return auth, key


def test_t6_unknown_kid_refreshes_in_background_only(perimeter, monkeypatch):
    auth, key = perimeter
    gate = threading.Event()
    entered = threading.Event()
    caller = threading.get_ident()
    seen = []
    def warm(*args, **kwargs):
        seen.append(threading.get_ident())
        entered.set()
        gate.wait(3)
    monkeypatch.setattr(auth, "warm_access_keys", warm)
    monkeypatch.setattr(node_auth.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("request path must not fetch JWKS"))
    try:
        assert not auth.verify_access_jwt(signed_jwt(key, kid="unknown"))
        assert entered.wait(1)
        assert seen == [seen[0]] and seen[0] != caller
    finally:
        gate.set()


@pytest.mark.parametrize("issuer,nbf,expected", [
    ("https://wrong.example.test", None, False),
    ("https://team.example.test", 120, False),
    ("https://team.example.test", 30, True),
    ("https://team.example.test", None, True),
])
def test_t7_issuer_and_not_before_are_verified(perimeter, issuer, nbf, expected):
    auth, key = perimeter
    assert auth.verify_access_jwt(signed_jwt(key, issuer=issuer, nbf=None if nbf is None else time.time() + nbf)) is expected


def test_t8_production_does_not_trust_framework_hosts():
    assert "testclient" not in ORIGINAL_LOOPBACK
    assert "testserver" not in ORIGINAL_HOSTS


def test_token_cache_invalidates_on_stat_change_and_rotation(tmp_path, monkeypatch):
    auth = node_auth.NodeAuth(tmp_path)
    first = auth.token()
    original = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    assert auth.token() == first
    assert auth.token() == first
    assert reads == []
    auth.token_path.write_text("replacement-longer", encoding="utf-8")
    assert auth.token() == "replacement-longer"
    assert auth.rotate_token() != "replacement-longer"


def test_empty_token_is_recreated_with_private_acl(tmp_path, monkeypatch):
    from rynmesh import private_permissions
    path = tmp_path / "control_token"
    path.touch()
    calls = []
    original = private_permissions.restrict_windows_acl
    def restrict(file, *args, **kwargs):
        calls.append(Path(file))
        return original(file, *args, **kwargs)
    monkeypatch.setattr(private_permissions, "restrict_windows_acl", restrict)
    assert node_auth.NodeAuth(tmp_path).token()
    if os.name == "nt":
        assert path in calls


def test_concurrent_token_creation_rereads_winner_without_truncation(tmp_path, monkeypatch):
    auth = node_auth.NodeAuth(tmp_path)
    original = os.open
    def create(path, flags, *args, **kwargs):
        if Path(path) == auth.token_path and flags & os.O_EXCL:
            auth.token_path.write_text("winner-token", encoding="utf-8")
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", create)
    assert auth.token() == "winner-token"
    assert auth.token_path.read_text() == "winner-token"


def test_deprecated_remote_control_flag_warns_without_bypassing_gate(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "launch-token")
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    app = create_app(RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network"))
    assert "RYNMESH_ALLOW_REMOTE_CONTROL 已不支持" in caplog.text
    assert TestClient(app).get("/api/local/node/status").status_code == 403


def test_valid_perimeter_assertion_uses_forwarded_caller_for_limit(tmp_path, monkeypatch, perimeter):
    auth, key = perimeter
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RYNMESH_ACCESS_TEAM_DOMAIN", auth.access_team_domain)
    monkeypatch.setenv("RYNMESH_ACCESS_AUD", auth.access_audience)
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    def warm(instance):
        instance._jwks = auth._jwks
        instance._jwks_fetched_at = time.time()
    monkeypatch.setattr(node_auth.NodeAuth, "warm_access_keys", warm)
    client = TestClient(create_app(RynmeshStore(home=tmp_path / "home", network_dir=tmp_path / "network")))
    assertion = signed_jwt(key)
    for index in range(9):
        response = client.post("/api/local/auth/unlock", json={"token": "wrong"}, headers={
            "cf-access-jwt-assertion": assertion, "cf-connecting-ip": f"192.0.2.{index}"})
        assert response.status_code == 401

