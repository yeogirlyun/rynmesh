"""The Ryn app starts in full access ("自动处理"); owner choices are never overridden."""
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from test_agent_runtime import Catalog, Model, Tools, advance, assistant

from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import (
    PERMISSION_MODE_SOURCE,
    SHIPPED_PERMISSION_MODE,
    AgentStore,
)


def runtime_at(path, key, *responses, permission_mode=SHIPPED_PERMISSION_MODE):
    model, tools = Model(*responses), Tools()
    runtime = AgentRuntime(AgentStore(path, key, permission_mode=permission_mode), model, tools, Catalog())
    return runtime, model, tools


def legacy_install(path, key, *, mode=None):
    """An install written by a build that saved settings without a mode source."""
    runtime, _, _ = runtime_at(path, key, permission_mode=None)
    runtime.update_settings({"enabled": True, "model_id": "local:test", **({"permission_mode": mode} if mode else {})})
    runtime.store.transaction(lambda data: data.pop(PERMISSION_MODE_SOURCE, None))
    return runtime


def saved(runtime):
    return runtime.store.inspect(lambda data: (data["settings"]["permission_mode"], data.get(PERMISSION_MODE_SOURCE),
                                               data.get("settings_revision", 0)))


def test_shipped_mode_is_full_access():
    assert SHIPPED_PERMISSION_MODE == "automatic"


def test_new_install_starts_in_full_access_and_runs_changes_without_asking(tmp_path):
    key = X25519PrivateKey.generate()
    runtime, _, tools = runtime_at(tmp_path, key,
                                   assistant(name="share_service", args={"service_id": "svc1"}), assistant("Shared."))
    assert runtime.adopt_shipped_permission_mode(SHIPPED_PERMISSION_MODE) is False
    assert not runtime.store.path.exists(), "startup must not create Agent state on a fresh install"
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    assert saved(runtime)[:2] == ("automatic", "shipped_default")
    task = runtime.create({"message": "Share svc1."})
    advance(runtime, 4)
    row = runtime.get(task["id"])
    assert row["permission_mode"] == "automatic"
    assert row["pending_approval"] is None and row["status"] != "waiting_approval"
    assert [name for name, *_ in tools.calls] == ["share_service"]


def test_old_install_on_the_old_default_moves_once(tmp_path):
    key = X25519PrivateKey.generate()
    old = legacy_install(tmp_path, key)
    before = saved(old)
    assert before[:2] == ("workspace", None)
    runtime, _, _ = runtime_at(tmp_path, key)
    assert runtime.adopt_shipped_permission_mode(SHIPPED_PERMISSION_MODE) is True
    assert saved(runtime) == ("automatic", "shipped_default", before[2] + 1)
    assert runtime.adopt_shipped_permission_mode(SHIPPED_PERMISSION_MODE) is False
    assert saved(runtime) == ("automatic", "shipped_default", before[2] + 1)


def test_old_install_with_a_picked_mode_keeps_it(tmp_path):
    key = X25519PrivateKey.generate()
    legacy_install(tmp_path, key, mode="confirm_changes")
    runtime, _, _ = runtime_at(tmp_path, key)
    revision = saved(runtime)[2]
    assert runtime.adopt_shipped_permission_mode(SHIPPED_PERMISSION_MODE) is False
    assert saved(runtime) == ("confirm_changes", "legacy", revision)


def test_owner_choice_survives_later_startups(tmp_path):
    key = X25519PrivateKey.generate()
    runtime, _, _ = runtime_at(tmp_path, key)
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    runtime.update_settings({"permission_mode": "workspace"})
    assert saved(runtime)[:2] == ("workspace", "owner")
    restarted, _, _ = runtime_at(tmp_path, key)
    assert restarted.adopt_shipped_permission_mode(SHIPPED_PERMISSION_MODE) is False
    assert saved(restarted)[:2] == ("workspace", "owner")


def test_existing_tasks_keep_their_own_mode(tmp_path):
    key = X25519PrivateKey.generate()
    old = legacy_install(tmp_path, key)
    task = old.create({"message": "Inspect svc1."})
    runtime, _, _ = runtime_at(tmp_path, key)
    runtime.adopt_shipped_permission_mode(SHIPPED_PERMISSION_MODE)
    assert runtime.get(task["id"])["permission_mode"] == "workspace"
    assert runtime.create({"message": "Inspect svc2."})["permission_mode"] == "automatic"


def test_bare_store_keeps_the_conservative_library_default(tmp_path):
    runtime, _, _ = runtime_at(tmp_path, X25519PrivateKey.generate(), permission_mode=None)
    runtime.update_settings({"enabled": True, "model_id": "local:test"})
    assert saved(runtime)[:2] == ("workspace", None)
