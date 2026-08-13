from rynmesh.settings_store import SettingsStore


def test_settings_api_applies_runtime_policy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from rynmesh.peer_http import create_app
    from rynmesh.store import RynmeshStore

    monkeypatch.setenv("RYNMESH_HOME", str(tmp_path / "node"))
    monkeypatch.setenv("RYNMESH_AUTO_REGISTER", "0")
    monkeypatch.setenv("RYNMESH_ALLOW_REMOTE_CONTROL", "1")
    store = RynmeshStore()
    client = TestClient(create_app(store))

    response = client.patch(
        "/api/local/settings",
        json={
            "node_name": "Personal Ryn",
            "safety_policy": "strict",
            "rank_default": "novelty",
            "publish_visibility": "trusted",
            "fetch_timeout_s": 33,
            "onboarding_version": 1,
        },
    )
    assert response.status_code == 200
    settings = response.json()
    assert settings["node_name"] == "Personal Ryn"
    assert settings["rank_default"] == "novelty"
    assert settings["publish_visibility"] == "trusted"
    assert settings["fetch_timeout_s"] == 33
    assert settings["onboarding_version"] == 1
    assert store.policy.allow_warnings is False
    assert store.policy.min_pass_receipts == 1


def test_default_auto_update_true(tmp_path):
    s = SettingsStore(tmp_path / "settings.json")
    assert s.get()["auto_update"] is True
    assert s.get()["onboarding_version"] == 0


def test_patch_persists_whitelisted(tmp_path):
    p = tmp_path / "settings.json"
    SettingsStore(p).patch({"auto_update": False, "ignored": 1})
    s2 = SettingsStore(p)
    assert s2.get()["auto_update"] is False
    assert "ignored" not in s2.get()


def test_patch_persists_and_validates_desktop_policy(tmp_path):
    p = tmp_path / "settings.json"
    settings = SettingsStore(p).patch(
        {
            "node_name": "My Ryn Node",
            "rank_default": "novelty",
            "publish_visibility": "trusted",
            "fetch_timeout_s": 45,
            "onboarding_version": 1,
            "safety_policy": "invalid",
        }
    )
    assert settings["node_name"] == "My Ryn Node"
    assert settings["rank_default"] == "novelty"
    assert settings["publish_visibility"] == "trusted"
    assert settings["fetch_timeout_s"] == 45
    assert settings["onboarding_version"] == 1
    assert settings["safety_policy"] == "standard"


def test_onboarding_version_accepts_zero_but_not_negative(tmp_path):
    p = tmp_path / "settings.json"
    store = SettingsStore(p)
    assert store.patch({"onboarding_version": 2})["onboarding_version"] == 2
    assert store.patch({"onboarding_version": -1})["onboarding_version"] == 2
    assert store.patch({"onboarding_version": 0})["onboarding_version"] == 0
