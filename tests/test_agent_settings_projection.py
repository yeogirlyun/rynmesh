"""Settings display work must not block other state readers or probe providers."""
import threading

from test_agent_runtime import make_runtime


def test_settings_projection_runs_after_commit_without_holding_state_lock(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    readers = []
    observed = []

    def model():
        read = threading.Event()

        def inspect():
            observed.append(runtime.store.inspect(lambda state: state["settings"]["max_steps"]))
            read.set()

        reader = threading.Thread(target=inspect)
        readers.append(reader)
        reader.start()
        assert read.wait(1), "model metadata lookup held the Agent state lock"
        return {"id": "local:test", "available": True, "agent_supported": True,
                "context_window_tokens": 128000}

    runtime.catalog.list_models = lambda: [model()]
    runtime.catalog.display_model = lambda _: model()
    try:
        saved = runtime.update_settings({"max_steps": 19})
        assert saved["max_steps"] == 19
        assert saved["context_budget_policy"]["effective_max_context_bytes"] > 0
        assert observed == [19]
    finally:
        for reader in readers:
            reader.join(2)


def test_settings_display_uses_cached_metadata_without_catalog_refresh(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    calls = []
    runtime.catalog.display_model = lambda model_id: calls.append(model_id) or {
        "id": model_id, "context_window_tokens": 32000}

    def refresh():
        raise AssertionError("displaying saved settings must not refresh model discovery")

    runtime.catalog.list_models = refresh
    saved = runtime.update_settings({"max_seconds": 120})
    assert saved["max_seconds"] == 120
    assert calls == ["local:test"]
