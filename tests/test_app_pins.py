from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rynmesh.app_pins import AppPinsStore
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore


ROUTE = "/api/local/apps/pins"
TOKEN_HEADERS = {"x-ryn-local-token": "test-pins-token"}
LOCAL = {
    "key": "local:images",
    "kind": "local",
    "app_id": "images",
    "title": "Images",
    "open_path": "/images?tab=recent",
}
SHARED = {
    "key": "shared:team|node-1|workspace",
    "kind": "shared",
    "app_id": "workspace",
    "title": "Workspace",
    "provider": {"resource": "team", "peer_id": "node-1", "member_name": "Example"},
}


def test_store_persists_order_and_seeded_empty_list(tmp_path):
    store = AppPinsStore(tmp_path)
    assert store.read() == {"seeded": False, "pins": []}
    assert not store.path.exists()

    expected = {"seeded": True, "pins": [SHARED, LOCAL]}
    assert store.replace([SHARED, LOCAL]) == expected
    assert (tmp_path / "apps" / "pins.json").is_file()
    assert store.read() == AppPinsStore(tmp_path).read() == expected

    store.replace([])
    assert AppPinsStore(tmp_path).read() == {"seeded": True, "pins": []}


@pytest.mark.parametrize(
    "pins",
    [
        None,
        [{"bad": "shape"}],
        [{**LOCAL, "kind": "invalid"}],
        [{**LOCAL, "key": "local:another"}],
        [{**SHARED, "key": "shared:team|node-2|workspace"}],
        [{**LOCAL, "title": "x" * 81}],
        [{**LOCAL, "open_path": "/" + "x" * 512}],
        [{**LOCAL, "open_path": "//example.test/"}],
        [{**LOCAL, "open_path": "/\\example.test/"}],
        [{key: val for key, val in LOCAL.items() if key != "open_path"}],
        [{key: val for key, val in SHARED.items() if key != "provider"}],
        [{**SHARED, "provider": {"resource": "team", "peer_id": "node-1"}}],
        [LOCAL, LOCAL],
        [{**LOCAL, "key": f"local:{i}", "app_id": str(i)} for i in range(31)],
    ],
)
def test_invalid_replace_leaves_existing_pins(tmp_path, pins):
    store = AppPinsStore(tmp_path)
    previous = store.replace([LOCAL])
    with pytest.raises(ValueError):
        store.replace(pins)
    assert store.read() == previous


def test_local_http_routes_require_authorization_and_validate_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path))
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", TOKEN_HEADERS["x-ryn-local-token"])
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    client = TestClient(create_app(RynmeshStore()))

    assert client.get(ROUTE).status_code == 403
    assert client.put(ROUTE, json={"pins": []}).status_code == 403
    assert client.get(ROUTE, headers=TOKEN_HEADERS).json() == {"seeded": False, "pins": []}

    expected = {"seeded": True, "pins": [LOCAL, SHARED]}
    response = client.put(ROUTE, headers=TOKEN_HEADERS, json={"pins": expected["pins"]})
    assert response.status_code == 200
    assert response.json() == expected
    assert client.put(ROUTE, headers=TOKEN_HEADERS, json={"pins": [LOCAL, LOCAL]}).status_code == 400
    assert client.put(ROUTE, headers=TOKEN_HEADERS, content="{").status_code == 400
    assert client.get(ROUTE, headers=TOKEN_HEADERS).json() == expected
