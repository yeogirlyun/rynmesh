"""A slow model refresh must not hold the owner's settings/composer hostage."""
import threading
import time

from test_agent_cli_models import cli_metadata_catalog
from test_agent_runtime import make_runtime


def test_display_uses_observed_inventory_while_discovery_is_blocked(cli_metadata_catalog, monkeypatch):
    from rynmesh.llm_package import codex_session
    f = cli_metadata_catalog
    identifier = "local/codex-cli/model/model-a"
    original = f.catalog.get_model(identifier)
    f.expire()
    started, release = threading.Event(), threading.Event()

    def slow(_):
        started.set()
        assert release.wait(5)
        return [dict(m) for m in f.models]

    monkeypatch.setattr(codex_session, "list_models", slow)
    try:
        before = time.monotonic()
        rows = f.catalog.display_models()
        elapsed = time.monotonic() - before
        assert started.wait(1)
        display = next(row for row in rows if row["id"] == identifier)
        assert elapsed < 1
        assert display["context_window"] == original["context_window"]
        assert display["available"] is True
        assert display["local_model_binding"] is None
        assert display["destination_binding_supported"] is False
        # A second owner read cannot queue behind the in-progress discovery.
        before = time.monotonic()
        assert f.catalog.display_model(identifier)["id"] == identifier
        assert time.monotonic() - before < 1
    finally:
        release.set()


def test_settings_uses_display_metadata_without_live_discovery(tmp_path, monkeypatch):
    runtime, _, _, _ = make_runtime(tmp_path)
    monkeypatch.setattr(runtime.catalog, "display_model", lambda _: {"context_window": 400000}, raising=False)
    monkeypatch.setattr(runtime, "_model_metadata", lambda _: (_ for _ in ()).throw(AssertionError("live lookup")))
    assert runtime.settings()["context_budget_policy"]
