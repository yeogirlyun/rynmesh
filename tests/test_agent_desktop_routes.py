import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime.desktop_routes import check, published_routes
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected


def test_publisher_route_reminds_without_locking_uncovered_ui_and_blocks_business_replay(tmp_path):
    store = AgentStore(tmp_path / 'agent', X25519PrivateKey.generate())
    context = {'task_id': 'task', 'goal_revision': 1}
    store.transaction(lambda data: data['tasks'].update({'task': {'id': 'task', 'status': 'queued', 'events': [], 'messages': [], 'receipts': []}}))
    declaration = {'process_names': ['Synthetic.exe'], 'tools': ['send', 'result'], 'capabilities': ['send message']}
    state = {'plugins': {'p': {'enabled': True, 'desktop_routes': [declaration]}}}
    assert published_routes(state) == []  # Arbitrary imported declarations have no routing authority.
    state['release_defaults'] = {'plugins': {'p': {'desktop_routes': [declaration]}}}
    routes = published_routes(state)
    check(store, context, 'unrelated.exe', routes)
    with pytest.raises(AgentToolRejected, match='agent_plugin_route_preferred'):
        check(store, context, 'Synthetic.exe', routes)
    check(store, context, 'Synthetic.exe', routes)  # Explicit/uncovered UI continues under existing permission.
    for result, expected in [({'error': {'code': 'PERMISSION_DENIED'}}, 'agent_plugin_ui_bypass_denied'),
                             ({'data': {'status': 'unknown'}}, 'agent_plugin_ui_replay_denied'),
                             ({'data': {'status': 'confirmed'}}, 'agent_plugin_ui_replay_denied')]:
        receipt = {'goal_revision': 1, 'effect': 'write', 'extension_binding': {'plugin_id': 'p'},
                   'result': {'structuredContent': result}}
        store.transaction(lambda data, receipt=receipt: data['tasks']['task'].update(receipts=[receipt]))
        with pytest.raises(AgentToolRejected, match=expected):
            check(store, context, 'Synthetic.exe', routes)
    state['plugins']['p']['enabled'] = False
    check(store, context, 'Synthetic.exe', published_routes(state))
    check(store, context, 'unrelated.exe', routes)
