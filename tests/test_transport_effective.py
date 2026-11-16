"""Tests for S13-I16: effective transport visibility, requested_unknown, config_error, and native_p2p status (T1-T3)."""
from __future__ import annotations

import logging
import ssl
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rynmesh import native_transport, transport
from rynmesh import transport_plugins as _transport_plugins  # noqa: F401
from rynmesh.peer_http import create_app
from rynmesh.store import RynmeshStore
from rynmesh.transport import TransportError, get_transport, reset_transport_cache, resolve_profile


@pytest.fixture(autouse=True)
def _clean_transport_state(monkeypatch):
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "s13-i16-secret")
    monkeypatch.delenv("RYNMESH_LLM_SERVICE_MANIFEST", raising=False)
    monkeypatch.delenv("RYNMESH_TLS_SNI", raising=False)
    monkeypatch.delenv("RYNMESH_CONNECT_HOST", raising=False)
    monkeypatch.delenv("RYNMESH_ECH_CONFIGS", raising=False)
    monkeypatch.setenv("RYNMESH_P2P_NATIVE", "auto")
    reset_transport_cache()
    if hasattr(native_transport, "_reset_state"):
        native_transport._reset_state()
    yield
    reset_transport_cache()
    if hasattr(native_transport, "_reset_state"):
        native_transport._reset_state()


def _auth_headers() -> dict[str, str]:
    return {"x-ryn-local-token": "s13-i16-secret"}


def test_t1_ech_falls_back_to_sni_fronting_and_records_last_effective_and_node_status(
    tmp_path, monkeypatch
) -> None:
    """T1 (covers Steps 1, 3): RYNMESH_TRANSPORT=ech without ECH runtime support sets effective='sni_fronting' in last_effective() and /api/local/node/status; non-factory profiles record stdlib_https / fronted_https."""
    if hasattr(ssl.SSLContext, "set_ech_config_list"):
        monkeypatch.delattr(ssl.SSLContext, "set_ech_config_list", raising=False)

    # Non-factory stdlib_https
    monkeypatch.setenv("RYNMESH_TRANSPORT", "camouflage")
    reset_transport_cache()
    get_transport()
    last_camo = transport.last_effective()
    assert last_camo is not None
    assert last_camo["requested"] == "camouflage"
    assert last_camo["effective"] == "stdlib_https"
    assert last_camo["degraded_reason"] is None

    # Non-factory fronted_https
    monkeypatch.setenv("RYNMESH_TRANSPORT", "hardened")
    monkeypatch.setenv("RYNMESH_TLS_SNI", "cdn.example.com")
    reset_transport_cache()
    get_transport()
    last_fronted = transport.last_effective()
    assert last_fronted is not None
    assert last_fronted["requested"] == "hardened"
    assert last_fronted["effective"] == "fronted_https"
    assert last_fronted["degraded_reason"] is None

    # ECH fallback -> effective='sni_fronting'
    monkeypatch.delenv("RYNMESH_TLS_SNI", raising=False)
    monkeypatch.setenv("RYNMESH_TRANSPORT", "ech")
    reset_transport_cache()
    get_transport()
    last_ech = transport.last_effective()
    assert last_ech is not None
    assert last_ech["requested"] == "ech"
    assert last_ech["effective"] == "sni_fronting"
    assert last_ech["degraded_reason"] == "ech_unavailable"

    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net"))
    with TestClient(app) as client:
        resp = client.get("/api/local/node/status", headers=_auth_headers())
        assert resp.status_code == 200, resp.text
        t_info = resp.json()["transport"]
        assert t_info["requested"] == "ech"
        assert t_info["effective"] == "sni_fronting"
        assert t_info["degraded_reason"] == "ech_unavailable"


def test_t2_unknown_transport_name_sets_requested_unknown(tmp_path, monkeypatch) -> None:
    """T2 (covers Steps 1, 2, 3): Unknown RYNMESH_TRANSPORT name falls back to camouflage with requested_unknown=True in profile, last_effective(), and /api/local/node/status."""
    monkeypatch.setenv("RYNMESH_TRANSPORT", "camouflage")
    assert resolve_profile().requested_unknown is False

    monkeypatch.setenv("RYNMESH_TRANSPORT", "nonexistent-obfs99")
    reset_transport_cache()
    prof = resolve_profile()
    assert prof.name == "camouflage"
    assert prof.requested_unknown is True

    get_transport(prof)
    eff = transport.last_effective()
    assert eff is not None
    assert eff["requested"] == "camouflage"
    assert eff["effective"] == "stdlib_https"
    assert eff["requested_unknown"] is True
    assert eff["degraded_reason"] == "unknown_transport"

    reset_transport_cache()
    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net"))
    with TestClient(app) as client:
        resp = client.get("/api/local/node/status", headers=_auth_headers())
        assert resp.status_code == 200, resp.text
        t_info = resp.json()["transport"]
        assert t_info["requested"] == "camouflage"
        assert t_info["effective"] == "stdlib_https"
        assert t_info["requested_unknown"] is True
        assert t_info["degraded_reason"] == "unknown_transport"


def test_t3_reality_config_error_and_native_p2p_import_error_and_protocol_mismatch_warn_once(
    tmp_path, monkeypatch, caplog
) -> None:
    """T3 (covers Steps 1, 3, 4): reality without curl_cffi surfaces degraded_reason='config_error'; native_transport ImportError and PROTOCOL mismatch surface exact reasons in node/status and log warning once."""
    monkeypatch.setitem(sys.modules, "curl_cffi", None)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", None)
    monkeypatch.setenv("RYNMESH_TRANSPORT", "reality")
    reset_transport_cache()

    with pytest.raises(TransportError) as exc_info:
        get_transport()
    assert exc_info.value.reason == "config_error"
    eff = transport.last_effective()
    assert eff is not None
    assert eff["requested"] == "reality"
    assert eff["effective"] is None
    assert eff["degraded_reason"] == "config_error"

    app = create_app(RynmeshStore(home=tmp_path / "node", network_dir=tmp_path / "net"))
    with TestClient(app) as client:
        # 1) Simulate ImportError for ryn_transport + fresh transport cache
        reset_transport_cache()
        native_transport._reset_state()
        monkeypatch.setitem(sys.modules, "ryn_transport", None)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rynmesh.native_transport"):
            r1 = client.get("/api/local/node/status", headers=_auth_headers())
            r2 = client.get("/api/local/node/status", headers=_auth_headers())
            assert native_transport.enabled() is False

        assert r1.status_code == 200 and r2.status_code == 200
        t1 = r1.json()["transport"]
        assert t1["requested"] == "reality"
        assert t1["effective"] is None
        assert t1["degraded_reason"] == "config_error"
        assert t1["native_p2p"] == {"enabled": False, "reason": "import_error"}
        assert r2.json()["transport"]["native_p2p"] == {"enabled": False, "reason": "import_error"}
        import_warnings = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
        assert len(import_warnings) == 1
        assert "import_error" in import_warnings[0].getMessage()

        # 2) Simulate PROTOCOL mismatch on ryn_transport -> protocol_mismatch:<actual>, warned once
        native_transport._reset_state()
        monkeypatch.setitem(
            sys.modules,
            "ryn_transport",
            SimpleNamespace(PROTOCOL="ryn-native-p2p/0-old"),
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rynmesh.native_transport"):
            r3 = client.get("/api/local/node/status", headers=_auth_headers())
            r4 = client.get("/api/local/node/status", headers=_auth_headers())
            assert native_transport.enabled() is False

        assert r3.status_code == 200 and r4.status_code == 200
        assert r3.json()["transport"]["native_p2p"] == {
            "enabled": False,
            "reason": "protocol_mismatch:ryn-native-p2p/0-old",
        }
        assert r4.json()["transport"]["native_p2p"] == {
            "enabled": False,
            "reason": "protocol_mismatch:ryn-native-p2p/0-old",
        }
        mismatch_warnings = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
        assert len(mismatch_warnings) == 1
        assert "protocol_mismatch:ryn-native-p2p/0-old" in mismatch_warnings[0].getMessage()

        # 3) Matching PROTOCOL -> enabled=True, reason=None, zero warnings
        native_transport._reset_state()
        monkeypatch.setitem(
            sys.modules,
            "ryn_transport",
            SimpleNamespace(PROTOCOL=native_transport.PROTOCOL),
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="rynmesh.native_transport"):
            r5 = client.get("/api/local/node/status", headers=_auth_headers())
        assert r5.status_code == 200
        assert r5.json()["transport"]["native_p2p"] == {"enabled": True, "reason": None}
        assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []
