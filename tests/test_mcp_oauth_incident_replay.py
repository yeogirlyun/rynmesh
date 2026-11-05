"""Replays of the 2026-11-05 workspace authorization incident against the owned
loopback authority (no production login, no external network)."""
import logging
import re
import time
from urllib.parse import parse_qs, urlsplit

import httpx

from rynmesh.agent_runtime.diagnostic_log import failure
from rynmesh.agent_runtime.extensions.mcp_oauth import McpOAuthError
from rynmesh.agent_runtime.extensions.mcp_transport import McpSessionManager

from test_agent_extensions_oauth import LocalAuthority, authority, begin, no_network, server, setup  # noqa: F401

LOGGER = "rynmesh.agent_runtime.diagnostic_log"


def _events(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.getMessage().startswith("agent_oauth ")]


def _stages(caplog):
    return [re.search(r" stage=(\S+) ", m).group(1) for m in _events(caplog)]


def _assert_no_secret(caplog, *values):
    text = "\n".join(r.getMessage() for r in caplog.records)
    for value in values:
        assert value and value not in text


def test_authority_invalid_target_is_reported_and_does_not_poison_the_next_attempt(tmp_path, authority, caplog):
    """12:22: central rejected an unsupported resource with error=invalid_target.
    It used to be stored as agent_mcp_oauth_login_denied and was still shown as the
    status of the later, unrelated 12:35 attempt."""
    caplog.set_level(logging.WARNING, logger=LOGGER)
    store, manager = setup(tmp_path, authority)
    instance = server(store)["instance_id"]
    try:
        first = begin(store, manager)
        params = parse_qs(urlsplit(first["authorization_url"]).query)
        rejected = params["redirect_uri"][0] + "?" + httpx.QueryParams(
            {"error": "invalid_target", "iss": authority.issuer, "state": params["state"][0]}).__str__()
        assert httpx.get(rejected, trust_env=False).status_code == 400
        assert manager.login_status("remote", first["auth_id"]) == {"state": "failed", "error": "agent_mcp_oauth_invalid_target"}
        assert manager.public_status(instance)["error"] == "agent_mcp_oauth_invalid_target"
        assert not any(row[1] == "/token" for row in authority.calls)

        second = begin(store, manager)
        pending = manager.public_status(instance)
        assert pending["state"] == "pending" and pending["error"] is None

        # The second attempt expires unused (no callback ever reaches the host).
        now = manager.clock()
        manager.clock = lambda: now + 601
        assert manager.login_status("remote", second["auth_id"])["state"] == "expired"
        assert manager.public_status(instance) | {"session_revision": None} == {
            "state": "logged_out", "session_revision": None, "expires_at": None, "scopes": [],
            "issuer_host": "127.0.0.1", "error": None}
        manager.clock = time.time

        third = begin(store, manager)
        target = authority.callback(third["authorization_url"])
        assert httpx.get(target, trust_env=False).status_code == 200
        assert manager.public_status(instance)["state"] == "logged_in"
        stages = _stages(caplog)
        for stage in ("login_started", "callback_accepted", "authorization_error", "login_failed",
                      "login_expired", "token_exchange_started", "login_connected"):
            assert stage in stages, stage
        assert any("oauth_error': 'invalid_target'" in m for m in _events(caplog))
        code = parse_qs(urlsplit(target).query)["code"][0]
        _assert_no_secret(caplog, params["state"][0], code, third["auth_id"], first["auth_id"])
    finally:
        manager.close()


def test_late_or_foreign_callbacks_are_traced_with_a_reason(tmp_path, authority, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    store, manager = setup(tmp_path, authority)
    try:
        started = begin(store, manager)
        target = authority.callback(started["authorization_url"])
        assert httpx.get(target, headers={"Origin": "https://example.test"}, trust_env=False).status_code == 400
        assert httpx.get(authority.callback(started["authorization_url"], state="wrong"), trust_env=False).status_code == 400
        assert httpx.get(target.split("/callback")[0] + "/favicon.ico", trust_env=False).status_code == 400
        now = manager.clock()
        manager.clock = lambda: now + 601
        assert httpx.get(target, trust_env=False).status_code == 400
        reasons = re.findall(r"'reason': '([a-z_]+)'", "\n".join(_events(caplog)))
        assert reasons == ["proxy_or_origin_header", "state_mismatch", "flow_expired"]
        assert not any(row[1] == "/token" for row in authority.calls)
        _assert_no_secret(caplog, parse_qs(urlsplit(target).query)["code"][0], started["auth_id"])
    finally:
        manager.close()


def test_token_endpoint_rejection_is_traced_without_credentials(tmp_path, authority, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    store, manager = setup(tmp_path, authority)
    try:
        started = begin(store, manager)
        target = authority.callback(started["authorization_url"])
        authority.codes.clear()  # e.g. code already expired at the authority
        assert httpx.get(target, trust_env=False).status_code == 400
        status = manager.login_status("remote", started["auth_id"])
        assert status == {"state": "failed", "error": "agent_mcp_oauth_request_failed"}
        failed = [m for m in _events(caplog) if " stage=token_exchange_failed " in m]
        assert len(failed) == 1 and "'http_status': 400" in failed[0] and "'oauth_error': 'invalid_grant'" in failed[0]
        _assert_no_secret(caplog, parse_qs(urlsplit(target).query)["code"][0])
    finally:
        manager.close()


def test_discovery_after_authorization_is_traced(tmp_path, authority, caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    store, _ = setup(tmp_path, authority)
    sessions = McpSessionManager(store)
    try:
        started = begin(store, sessions.oauth)
        assert httpx.get(authority.callback(started["authorization_url"]), trust_env=False).status_code == 200
        deadline = time.monotonic() + 5
        while sessions.oauth.login_status("remote", started["auth_id"])["state"] != "connected" and time.monotonic() < deadline:
            time.sleep(.01)
        finished = [m for m in _events(caplog) if " stage=discovery_finished " in m]
        assert finished and "'ok': True" in finished[0] and "'tools': 1" in finished[0]
    finally:
        sessions.close()


def test_oauth_failure_codes_reach_the_failure_log(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    failure("api_ext_company_workspace_member", McpOAuthError("agent_mcp_auth_required"))
    assert "'code': 'agent_mcp_auth_required'" in caplog.records[-1].getMessage()
