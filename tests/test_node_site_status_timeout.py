"""A slow NAT dial must not delay the trusted frame or multiply probe work."""
import threading
import time
from types import SimpleNamespace

import pytest

from rynmesh.node_sites import client as mod
from rynmesh.node_sites.protocol import SiteError
from rynmesh.node_sites.store import SiteStore


def test_status_bounds_slow_probe_and_recovers_without_duplicate_dials(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATUS_TIMEOUT_S", 0.03)
    release = threading.Event()
    started = threading.Event()
    calls = []

    def probe(*args, **kwargs):
        calls.append(args)
        started.set()
        assert release.wait(2)
        return SimpleNamespace(online=True, route="p2p")

    engine = SimpleNamespace(provider_probe=probe, provider_member=lambda *a: {"member_name": "Caller"},
                             provider_directory=lambda *a: SimpleNamespace(last_seen=lambda *a: None))
    store = SiteStore(tmp_path)
    key = store.key_for("resource", "peer", "demo")
    client = mod.SiteClient(engine, store)
    try:
        before = time.monotonic()
        assert client.status(key, fresh=True)["online"] is False
        assert time.monotonic() - before < 0.5
        assert started.is_set()
        assert client.status(key, fresh=True)["online"] is False
        assert len(calls) == 1
        release.set()
        state = client.status(key, fresh=True)
        assert state["online"] is True and state["route"] == "p2p"
        assert len(calls) == 1
    finally:
        release.set()


def test_status_probe_preserves_identity_errors(tmp_path):
    from rynmesh.drop.engine import DropError

    def probe(*args, **kwargs):
        raise DropError("identity_mismatch", 403)

    client = mod.SiteClient(SimpleNamespace(provider_probe=probe), SiteStore(tmp_path))
    with pytest.raises(SiteError) as exc:
        client._status_probe("resource", "peer", fresh=True)
    assert exc.value.code == "site_identity_changed"
    assert not client._status_probes
