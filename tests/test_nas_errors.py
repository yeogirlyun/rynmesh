"""Tests for S05-I19: NAS error classification, credential/host redaction, warning logs (T2, T3), and agent ResourceTools passthrough + REJECTION_CORRECTIONS."""
from __future__ import annotations

import logging
import socket
from types import SimpleNamespace

import httpx
import pytest
from smbprotocol.exceptions import SMBAuthenticationError, SMBOSError
from smbprotocol.header import NtStatus

from rynmesh.agent_runtime.store import REJECTION_CORRECTIONS, AgentToolRejected
from rynmesh.agent_runtime.tools import RynTools
from rynmesh.nas import NasError, NasPlugin, WebDAVConnector

SENSITIVE_HOST = "vault-nas-01.corp.internal"
SENSITIVE_USER = "svc_nas_secret_user"
SENSITIVE_PASS = "TopSecretPass!99"


def _setup_plugin_and_source(tmp_path) -> tuple[NasPlugin, dict]:
    plugin = NasPlugin(tmp_path / "nas_home")

    class _InitConnector:
        def __init__(self, config):
            self.config = config

        def list(self, _path):
            return []

        def list_page(self, _path, _cursor=""):
            return {"entries": [], "next_cursor": "", "skipped": 0}

        def close(self):
            return None

    plugin.connectors["smb"] = _InitConnector
    plugin.enable(True)
    source = plugin.configure(
        {
            "name": "Corp Vault",
            "system": "synology",
            "protocol": "smb",
            "host": SENSITIVE_HOST,
            "share": "finance",
            "username": SENSITIVE_USER,
            "password": SENSITIVE_PASS,
            "allow_ai": True,
            "writable": False,
        }
    )
    return plugin, source


def test_t2_connection_exceptions_map_to_classified_codes_without_leaking_host_or_user(tmp_path) -> None:
    """T2: Auth, timeout, DNS, connection-refused, and generic errors map to exact closed codes without exposing host/user/password, and ResourceTools passes them through with REJECTION_CORRECTIONS."""
    plugin, source = _setup_plugin_and_source(tmp_path)
    secret_detail = f"failed for {SENSITIVE_USER}:{SENSITIVE_PASS}@{SENSITIVE_HOST}:445"

    cases = [
        (SMBAuthenticationError(secret_detail), "nas_auth_failed"),
        (SMBOSError(NtStatus.STATUS_LOGON_FAILURE, secret_detail), "nas_auth_failed"),
        (socket.timeout(secret_detail), "nas_timeout"),
        (TimeoutError(secret_detail), "nas_timeout"),
        (socket.gaierror(8, secret_detail), "nas_dns_failed"),
        (ConnectionRefusedError(secret_detail), "nas_unreachable"),
        (RuntimeError(secret_detail), "nas_connection_failed"),
    ]

    for raised_exc, expected_code in cases:

        class _FailingConnector:
            def __init__(self, _cfg):
                raise raised_exc

            def close(self):
                return None

        plugin.connectors["smb"] = _FailingConnector
        with pytest.raises(NasError) as caught:
            plugin.operate(source["id"], "list", "")
        assert str(caught.value) == expected_code
        for secret in (SENSITIVE_HOST, SENSITIVE_USER, SENSITIVE_PASS):
            assert secret not in str(caught.value)

    # Also verify HTTP 401 and 403 on WebDAV map to nas_auth_failed
    for status_code in (401, 403):
        dav = WebDAVConnector(
            {
                "url": f"http://{SENSITIVE_HOST}/dav",
                "root": "",
                "username": SENSITIVE_USER,
                "password": SENSITIVE_PASS,
            }
        )
        dav.client.close()
        dav.client = httpx.Client(
            transport=httpx.MockTransport(lambda _req, sc=status_code: httpx.Response(sc, text=secret_detail)),
            auth=(SENSITIVE_USER, SENSITIVE_PASS),
            follow_redirects=False,
        )
        try:
            with pytest.raises(NasError) as dav_caught:
                dav.list("")
            assert str(dav_caught.value) == "nas_auth_failed"
            assert SENSITIVE_HOST not in str(dav_caught.value)
            assert SENSITIVE_USER not in str(dav_caught.value)
        finally:
            dav.close()

    # Verify ResourceTools passes these classified NAS codes through to the model and attaches REJECTION_CORRECTIONS
    tools = RynTools(SimpleNamespace(state=SimpleNamespace(nas_plugin=plugin)), SimpleNamespace(), {}, None)
    destination = {
        "catalog_model_id": "m1",
        "provider_peer_id": "p1",
        "source_id": "s1",
        "destination_revision": "r1",
    }

    class _OkConnector:
        def __init__(self, _cfg):
            return None

        def list_page(self, _path, _cursor=""):
            return {"entries": [], "next_cursor": "", "skipped": 0}

        def close(self):
            return None

    plugin.connectors["smb"] = _OkConnector
    scope = tools.prepare_scope(
        {
            "resources": [{"connection_id": source["id"], "root_path": "", "actions": ["list", "read_ai"]}],
            "allow_model_data": True,
        },
        owner_id="owner",
        consumer_node_id="node",
        model_destination=destination,
    )
    ctx = {
        **scope,
        "task_id": "t-errors",
        "scope_revision": 1,
        "owner_id": "owner",
        "consumer_node_id": "node",
        "model_destination": destination,
        "resource_origins": [],
    }
    root_ref = scope["resource_grants"][0]["root_resource_ref"]

    for raised_exc, expected_code in cases:

        class _ErrOnListConnector:
            def __init__(self, _cfg):
                return None

            def list_page(self, _path, _cursor=""):
                raise raised_exc

            def close(self):
                return None

        plugin.connectors["smb"] = _ErrOnListConnector
        with pytest.raises(AgentToolRejected) as tool_err:
            tools.execute(
                "ryn_list_resources",
                {"resource_ref": root_ref},
                operation_id=f"op-{expected_code}",
                context=ctx,
                cancelled=lambda: False,
            )
        assert str(tool_err.value) == expected_code
        assert expected_code in REJECTION_CORRECTIONS
        assert tool_err.value.correction == REJECTION_CORRECTIONS[expected_code]
        assert tool_err.value.correction
        for secret in (SENSITIVE_HOST, SENSITIVE_USER, SENSITIVE_PASS):
            assert secret not in str(tool_err.value)
            assert secret not in (tool_err.value.correction or "")


def test_t3_exception_class_name_logged_without_leaking_host_or_username(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """T3: Each mapped failure logs 'nas operation failed: <ClassName>' on logger 'rynmesh.nas' without logging host, username, or password."""
    plugin, source = _setup_plugin_and_source(tmp_path)
    secret_detail = f"connect https://{SENSITIVE_USER}:{SENSITIVE_PASS}@{SENSITIVE_HOST}/share failed"

    exceptions = [
        SMBAuthenticationError(secret_detail),
        TimeoutError(secret_detail),
        socket.gaierror(8, secret_detail),
        ConnectionRefusedError(secret_detail),
        RuntimeError(secret_detail),
    ]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="rynmesh.nas"):
        for exc in exceptions:

            class _BoomConnector:
                def __init__(self, _cfg, _exc=exc):
                    raise _exc

                def close(self):
                    return None

            plugin.connectors["smb"] = _BoomConnector
            with pytest.raises(NasError):
                plugin.operate(source["id"], "list", "")

    messages = [rec.getMessage() for rec in caplog.records if rec.name == "rynmesh.nas"]
    for expected_cls in (
        "SMBAuthenticationError",
        "TimeoutError",
        "gaierror",
        "ConnectionRefusedError",
        "RuntimeError",
    ):
        assert any(f"nas operation failed: {expected_cls}" in msg for msg in messages), messages

    full_log = "\n".join(messages) + "\n" + caplog.text
    for secret in (SENSITIVE_HOST, SENSITIVE_USER, SENSITIVE_PASS):
        assert secret not in full_log
