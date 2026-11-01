"""Independent regressions for provider lifecycle authority boundaries."""
import asyncio
import json
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from test_peer_providers import MANIFEST, RESOURCE, FakeServer, login, model, node
from test_peer_providers import no_network as no_network  # noqa: F401

from rynmesh.agent_runtime.extensions import ExtensionImportManager, ExtensionStore
from rynmesh.peer_providers import PeerProviderError


def test_disable_enable_between_provider_polls_invalidates_prior_grant(tmp_path):
    extensions = ExtensionStore(tmp_path / "extensions", X25519PrivateKey.generate())
    package = tmp_path / "package"
    package.mkdir()
    (package / "plugin.json").write_text(json.dumps({
        "id": "test", "name": "Review fixture", "version": "1",
        "ryn": {"peerProvider": MANIFEST},
    }), encoding="utf-8")
    manager = ExtensionImportManager(extensions)
    preview = manager.preview({"kind": "plugin", "path": str(package)})
    manager.commit(preview["preview_id"], expected_revision=preview["expected_revision"])
    extensions.set_plugin_enabled("test", True)
    server = FakeServer()
    service = node(tmp_path, server, extension_source=extensions.list_peer_providers_internal)

    async def scenario():
        await login(service, server)
        await service.publish("test", ["model-a"])
        await service.directory("test")
        before = extensions.list_peer_providers_internal()[0]
        # The UI can perform both mutations before the periodic provider poll.
        extensions.set_plugin_enabled("test", False)
        extensions.set_plugin_enabled("test", True)
        after = extensions.list_peer_providers_internal()[0]
        assert after["instance_id"] == before["instance_id"]
        assert after["revision"] != before["revision"]
        status = service.status("test")
        assert status["status"] == "signed_out"
        assert not service.cached_peers()
        assert not status["selected_service_ids"]

    asyncio.run(scenario())


def test_older_publish_cannot_restore_selection_after_failed_deselect(tmp_path):
    server = FakeServer()
    service = node(tmp_path, server)
    asyncio.run(login(service, server))
    ready, release = threading.Event(), threading.Event()
    failures = []
    calls = 0

    def model_status():
        nonlocal calls
        calls += 1
        if calls == 2:
            # Pause the old publication after its successful remote PUT but
            # before it writes the local authorization selection.
            ready.set()
            assert release.wait(5)
        return [model()]

    service.model_status = model_status

    def publish_old():
        try:
            asyncio.run(service.publish("test", ["model-a"]))
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=publish_old)
    thread.start()
    try:
        assert ready.wait(5)
        server.fail = lambda target: target.endswith("/models")
        with pytest.raises(PeerProviderError):
            asyncio.run(service.publish("test", []))
        assert not service.locally_published(RESOURCE, "model-a", model()["model_binding"])
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not failures
    assert not service.locally_published(RESOURCE, "model-a", model()["model_binding"])
