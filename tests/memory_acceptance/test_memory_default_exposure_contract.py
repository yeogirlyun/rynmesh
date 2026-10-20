"""Owner chat writes must obey configured recipients and preserve old policies.

Uses actual encrypted Store, owner HTTP, Runtime and outgoing provider messages.
The two tool cases call the real grounded tool handler with a genuine API-created
owner message before interactive execution; they do not manufacture provenance.
"""
import copy
import importlib
import json

import pytest

from test_memory_integration_contract import (
    BASE, CORRECT, SAVE, finish, front_requests, integrated, prohibit_network,
)


def text(value):
    return json.dumps(value, ensure_ascii=False)


def two_destinations(env, monkeypatch):
    first = copy.deepcopy(env.catalog.row)
    second = {**first, 'id': 'local:memory-other', 'peer_id': 'other-provider',
              'source_id': 'other-source', 'destination_revision': 'other-rev1'}
    rows = {first['id']: first, second['id']: second}
    monkeypatch.setattr(env.catalog, 'list_models', lambda **_kw: copy.deepcopy(list(rows.values())))
    monkeypatch.setattr(env.catalog, 'resolve', lambda model_id: copy.deepcopy(rows[model_id]))
    return first, second, env.catalog.destination(first), env.catalog.destination(second)


@pytest.mark.parametrize('mode', ['selected_models', 'destination'])
def test_new_explicit_facts_only_reach_the_configured_recipient(integrated, monkeypatch, mode):
    env = integrated
    first, second, allowed, _other = two_destinations(env, monkeypatch)
    env.api.write('PUT', '/settings', {'default_exposure': {
        'mode': mode, 'allowed_destinations': [allowed]}})
    finish(env, env.api.create_task(SAVE))
    entries = env.api.snapshot()['entries']
    assert len(entries) == 3
    assert all(entry['exposure_policy']['mode'] == 'destinations'
               and entry['exposure_policy']['allowed_destinations'] == [allowed] for entry in entries)
    finish(env, env.api.create_task('我目前使用什么摄影机身和镜头？'))
    assert 'X-T5' in text(front_requests(env)[-1]['messages'])
    env.api.request('PUT', BASE + '/settings', {'model_id': second['id']})
    finish(env, env.api.create_task('我目前使用什么摄影机身和镜头？'))
    outbound = text(front_requests(env)[-1]['messages'])
    assert 'X-T5' not in outbound and 'XF16-55' not in outbound
    assert {entry['id']: entry['exposure_policy'] for entry in env.api.snapshot()['entries']} == {
        entry['id']: entry['exposure_policy'] for entry in entries
    }, 'Changing model must not silently change authorization'


@pytest.mark.parametrize('mode', ['selected_models', 'restricted'])
def test_no_allowed_recipient_stores_explicit_fact_locally_without_disclosure(integrated, mode):
    env = integrated
    env.api.write('PUT', '/settings', {'default_exposure': {'mode': mode, 'allowed_destinations': []}})
    finish(env, env.api.create_task(SAVE))
    entries = env.api.snapshot()['entries']
    assert len(entries) == 3
    assert all(entry['exposure_policy']['mode'] == 'local_only'
               and not entry['exposure_policy']['allowed_destinations'] for entry in entries)
    finish(env, env.api.create_task('我目前使用什么摄影机身和镜头？'))
    outbound = text(front_requests(env)[-1]['messages'])
    assert 'X-T5' not in outbound and 'XF16-55' not in outbound


def test_content_correction_cannot_replace_old_recipient_with_new_default(integrated, monkeypatch):
    env = integrated
    _first, second, allowed, other = two_destinations(env, monkeypatch)
    env.api.write('PUT', '/settings', {'default_exposure': {
        'mode': 'selected_models', 'allowed_destinations': [allowed]}})
    finish(env, env.api.create_task(SAVE))
    before = {entry['fact_key']: entry for entry in env.api.snapshot()['entries']}
    env.api.write('PUT', '/settings', {'default_exposure': {
        'mode': 'selected_models', 'allowed_destinations': [other]}})
    finish(env, env.api.create_task(CORRECT))
    after = env.api.snapshot()['entries']
    assert len(after) == 3 and 'X-H2' in text([entry['text'] for entry in after])
    assert 'X-T5' not in text([entry['text'] for entry in after])
    for entry in after:
        original = before[entry['fact_key']]
        assert entry['id'] == original['id'] and entry['scope'] == original['scope']
        assert entry['exposure_policy'] == original['exposure_policy']
    env.api.request('PUT', BASE + '/settings', {'model_id': second['id']})
    finish(env, env.api.create_task('我目前使用什么摄影机身和镜头？'))
    assert 'X-H2' not in text(front_requests(env)[-1]['messages'])


@pytest.mark.parametrize('has_recipient', [True, False])
def test_grounded_model_tool_obeys_the_same_recipient_rule(integrated, has_recipient):
    env = integrated
    destination = env.catalog.destination(env.catalog.row)
    env.api.write('PUT', '/settings', {'default_exposure': {
        'mode': 'selected_models', 'allowed_destinations': [destination] if has_recipient else []}})
    task_id = env.api.create_task('请记住我的机身是富士 X-T5。')
    assert not env.api.snapshot()['entries'], 'Tool fixture must reach a genuinely new fact before host processing'
    helper = importlib.import_module('rynmesh.agent_runtime.memory_runtime')

    def propose(data):
        return helper.execute_memory_tool_call_in_tx(data, data['tasks'][task_id],
            helper.MEMORY_PROPOSE_TOOL, {'operation': 'remember', 'scope_kind': 'personal',
                'category': 'equipment', 'fact_key': 'personal.gear.camera_body', 'text': '机身是富士 X-T5'})

    receipt = env.runtime.store.transaction(propose)
    assert receipt['applied'] is True and receipt['saved'] is True
    entries = env.api.snapshot()['entries']
    assert len(entries) == 1 and entries[0]['sources'][0]['task_id'] == task_id
    policy = entries[0]['exposure_policy']
    assert policy['mode'] == ('destinations' if has_recipient else 'local_only')
    assert policy['allowed_destinations'] == ([destination] if has_recipient else [])
