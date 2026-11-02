"""Session markers are change detectors, never exported credentials."""
from types import SimpleNamespace
from rynmesh.agent_runtime.extensions.mcp_oauth import McpOAuthManager


def test_session_revision_tracks_credential_replacement_and_logout():
    server = {'instance_id': 'synthetic', 'transport': 'streamable_http', 'auth': {'mode': 'oauth'}}
    row = {'epoch': 'epoch-one', 'tokens': {'access_token': 'synthetic-private-token',
            'refresh_token': 'synthetic-refresh', 'expires_at': 9999999999}}
    manager = object.__new__(McpOAuthManager)
    manager.store = SimpleNamespace(list_mcp_servers_internal=lambda: [server])
    manager._row = lambda _: row
    manager.clock = lambda: 1
    first = manager.public_status('synthetic')
    assert first['state'] == 'logged_in' and len(first['session_revision']) == 64
    assert first == manager.public_status('synthetic')
    assert 'synthetic-private-token' not in str(first) and 'synthetic-refresh' not in str(first)
    row['tokens']['access_token'] = 'new-account-token'
    second = manager.public_status('synthetic')
    assert first['session_revision'] != second['session_revision']
    row['epoch'] = 'epoch-two'
    assert manager.public_status('synthetic')['session_revision'] != second['session_revision']
    row['pending'] = 'login-flow'
    assert manager.public_status('synthetic')['state'] == 'pending'
    row.pop('pending'); row.pop('tokens')
    assert manager.public_status('synthetic')['state'] == 'logged_out'
    assert manager.public_status('synthetic')['session_revision'] is None
