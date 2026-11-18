"""S01-I6: upstream metadata reaches real adapters, retry state and task events."""
import io
import json
from datetime import datetime, timezone
from email.message import Message
from urllib.error import HTTPError

import pytest

from test_agent_integrations import add_source, node as node
from rynmesh.agent_runtime import diagnostic_log, retry_policy
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.llm_package import adapters, protocol_adapter, transport_failure as tf
from rynmesh.llm_package.adapters import OpenAICompatibleAdapter
from rynmesh.llm_package.protocol_adapter import NativeProtocolAdapter
from rynmesh.services import peer_box


def headers(values):
    result = Message()
    for key, value in values.items():
        result[key] = value
    return result


def failure(status=429, body=None, **values):
    return HTTPError('https://vendor.test/v1/chat/completions', status, 'private reason',
                     headers(values), io.BytesIO(json.dumps(body or {}).encode()))


@pytest.mark.parametrize('values,expected', [
    ({'x-ratelimit-reset-requests': '6m0s', 'x-ratelimit-reset-tokens': '1.5s', 'Retry-After': '2'}, 360),
    ({'anthropic-ratelimit-requests-reset': '2026-11-17T00:00:30Z',
      'anthropic-ratelimit-tokens-reset': '2026-11-17T00:01:30+00:00'}, 90),
    ({'x-ratelimit-reset-tokens': '999999m'}, 86400),
])
def test_t1_limit_headers_maximum_and_quota_reset(monkeypatch, values, expected):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(tf.time, 'time', lambda: now)
    info = tf.read_http_failure(failure(body={'error': {'code': 'insufficient_quota'}}, **values))
    assert info['retry_after_seconds'] == expected
    assert info['reset_at'] == int(now + expected)
    assert diagnostic_log.safe_diagnostics(info)['reset_at'] == int(now + expected)


@pytest.mark.parametrize('code', ['insufficient_quota', 'billing_hard_limit_reached',
    'credit_balance_too_low', 'credit_balance_exhausted', 'organization_spend_limit_exceeded',
    'usage_limit_reached', 'quota_exceeded', 'Arrearage', 'insufficient_balance'])
def test_t1_quota_codes_and_402(code):
    assert tf.read_http_failure(failure(body={'error': {'code': code}}))['network_reason'] == 'quota'
    assert tf.read_http_failure(failure(status=402))['network_reason'] == 'quota'


@pytest.mark.parametrize('name', ['x-request-id', 'request-id', 'cf-ray'])
def test_t1_request_id_safe_end_to_end(name):
    info = tf.read_http_failure(failure(**{name: 'req-abc_012:XYZ'}))
    assert info['upstream_request_id'] == 'req-abc_012:XYZ'
    assert diagnostic_log.safe_diagnostics(info)['upstream_request_id'] == 'req-abc_012:XYZ'
    assert 'upstream_request_id' not in diagnostic_log.safe_diagnostics({'upstream_request_id': 'bad\nsecret'})
    assert 'upstream_request_id' not in tf.read_http_failure(failure(**{name: 'bad/secret'}))
    assert len(tf.read_http_failure(failure(**{name: 'a' * 200}))['upstream_request_id']) == 128


@pytest.mark.parametrize('server,body', [
    ('cloudflare', {'error': {'message': 'Request blocked'}}),
    ('nginx', {'error': {'message': 'Cloudflare blocked this request'}}),
])
def test_t1_region_blocked(server, body):
    info = tf.read_http_failure(failure(status=403, body=body, server=server))
    assert info['network_reason'] == 'region_blocked'
    assert diagnostic_log.safe_diagnostics(info)['network_reason'] == 'region_blocked'
    assert tf.read_http_failure(failure(status=403, body={'error': {'message': 'invalid key'}}))['network_reason'] == 'auth_rejected'
    assert 'private' not in json.dumps(info)


@pytest.mark.parametrize('text,seconds', [('try again in 1.2s', 1.2), ('Try again in 1200 ms', 1.2)])
def test_t1_http_and_stream_message_delay(text, seconds):
    envelope = {'error': {'code': 'rate_limit_exceeded', 'message': text + ' PRIVATE_CANARY'}}
    info = tf.read_http_failure(failure(body=envelope))
    assert info['retry_after_seconds'] == seconds
    code, stream = tf.classify_stream_error(envelope)
    assert code == 'upstream_stream_error' and stream['retry_after_seconds'] == seconds
    assert 'PRIVATE_CANARY' not in json.dumps([info, stream])
    assert tf.read_http_failure(failure(body=envelope, **{'Retry-After': '8'}))['retry_after_seconds'] == 8
    assert 'retry_after_seconds' not in tf.read_http_failure(failure(status=400, body=envelope))


class Response(io.BytesIO):
    def __init__(self, body, *, sse=False, values=None):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        super().__init__(raw)
        self.headers = headers({'Content-Type': 'text/event-stream' if sse else 'application/json', **(values or {})})


def completion(model='served'):
    return {'model': model, 'choices': [{'message': {'role': 'assistant', 'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}


@pytest.mark.parametrize('stream,values,model,expected', [
    (False, {'openai-model': 'header-model'}, 'body-model', 'header-model'),
    (True, {'x-model': 'header-model'}, 'chunk-model', 'header-model'),
    (True, {}, 'chunk-model', 'chunk-model'),
    (False, {}, 'body-model', 'body-model'),
    (True, {}, 'a' * 200, 'a' * 160),
])
def test_t3_chat_model_from_headers_and_first_chunk(monkeypatch, stream, values, model, expected):
    first = {'model': model, 'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}
    second = {'model': 'later-untrusted-model', 'choices': [], 'usage': {'prompt_tokens': 2, 'completion_tokens': 1}}
    body = ''.join('data: ' + json.dumps(item) + '\n\n' for item in (first, second)) + 'data: [DONE]\n\n' if stream else completion(model)
    monkeypatch.setattr(adapters, 'model_urlopen', lambda *a, **k: Response(body, sse=stream, values=values))
    result = OpenAICompatibleAdapter(base_url='http://127.0.0.1:1234', model='requested').chat(
        {'messages': [{'role': 'user', 'content': 'hi'}], 'stream': stream}, task_id='t')
    assert result['server_model'] == expected


@pytest.mark.parametrize('stream', [False, True])
def test_t3_inference_entrypoints_preserve_server_model(monkeypatch, stream):
    chunk = {'model': 'chunk-model', 'choices': [{'delta': {'content': 'ok'}, 'finish_reason': 'stop'}]}
    body = 'data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n' if stream else completion()
    monkeypatch.setattr(adapters, 'model_urlopen', lambda *a, **k: Response(body, sse=stream, values={'x-model': 'served-header'}))
    adapter = OpenAICompatibleAdapter(base_url='http://127.0.0.1:1234', model='requested')
    options = {'prompt': 'hi', 'max_tokens': 32, 'task_id': 't', 'timeout_s': 3}
    result = adapter.infer_stream(**options, on_delta=lambda _: None) if stream else adapter.infer(**options)
    assert result['server_model'] == 'served-header'


def test_t1_plaintext_429_delay_reaches_real_chat_adapter(monkeypatch):
    def upstream(*a, **k):
        raise HTTPError('http://127.0.0.1:1234', 429, 'private', headers({}), io.BytesIO(b'try again in 12s PRIVATE'))
    monkeypatch.setattr(adapters, 'model_urlopen', upstream)
    adapter = OpenAICompatibleAdapter(base_url='http://127.0.0.1:1234', model='requested')
    with pytest.raises(adapters.AdapterError) as caught:
        adapter.chat({'messages': [{'role': 'user', 'content': 'hi'}], 'stream': True}, task_id='t')
    assert caught.value.diagnostics['retry_after_seconds'] == 12
    assert 'PRIVATE' not in json.dumps(caught.value.diagnostics)


@pytest.mark.parametrize('protocol', ['openai_responses', 'anthropic_messages'])
@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('header', [False, True])
def test_t3_native_model_from_headers_and_native_envelope(monkeypatch, protocol, stream, header):
    if protocol == 'openai_responses':
        raw = {'id': 'resp_1', 'object': 'response', 'status': 'completed', 'model': 'native-model',
               'output': [{'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'ok'}]}],
               'usage': {'input_tokens': 2, 'output_tokens': 1}}
        events = [{'type': 'response.created', 'response': {**raw, 'status': 'in_progress', 'output': []}},
                  {'type': 'response.completed', 'response': raw}]
    else:
        raw = {'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'model': 'native-model',
               'content': [{'type': 'text', 'text': 'ok'}], 'stop_reason': 'end_turn',
               'usage': {'input_tokens': 2, 'output_tokens': 1}}
        events = [{'type': 'message_start', 'message': {**raw, 'content': [], 'stop_reason': None}},
                  {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': 'ok'}},
                  {'type': 'content_block_stop', 'index': 0},
                  {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 1}},
                  {'type': 'message_stop'}]
    body = ''.join('data: ' + json.dumps(item) + '\n\n' for item in events) if stream else raw
    values = {'openai-model': 'native-header'} if header else {}
    monkeypatch.setattr(protocol_adapter, 'model_urlopen', lambda *a, **k: Response(body, sse=stream, values=values))
    result = NativeProtocolAdapter(protocol=protocol, base_url='http://127.0.0.1:1234', model='requested').chat(
        {'messages': [{'role': 'user', 'content': 'hi'}], 'stream': stream}, task_id='t')
    assert result['server_model'] == ('native-header' if header else 'native-model')


@pytest.fixture
def live(node):
    app, store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, store, store.home)
    key = peer_box.load_or_create_messaging_key(store.home / 'messaging.x25519')
    runtime = AgentRuntime(AgentStore(store.home / 'i6', key), bridge, tools, catalog)
    runtime.update_settings({'enabled': True, 'model_id': model_id})
    yield runtime, app, model_id
    tools.local.processes.close()


def test_t2_body_delay_reaches_runtime_retry_event(live, monkeypatch):
    runtime, _, _ = live
    clock, calls = [1000.0], []
    monkeypatch.setattr(retry_policy, 'now', lambda: clock[0])
    def upstream(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise failure(body={'error': {'code': 'rate_limit_exceeded', 'message': 'try again in 12s'}}, **{'cf-ray': 'ray:123'})
        return Response(completion('model-a'))
    monkeypatch.setattr(adapters, 'model_urlopen', upstream)
    row = runtime.create({'message': 'hi'})
    runtime.run_once()
    current = runtime.get(row['id'])
    assert current['status'] == 'queued'
    recovery = current['connection_recovery']
    assert recovery['diagnostics']['retry_after_seconds'] == 12
    assert recovery['diagnostics']['upstream_request_id'] == 'ray:123'
    assert recovery['next_retry_at'] == pytest.approx(1012)
    event = next(e for e in current['events'] if e['type'] == 'model_retry_scheduled')
    assert event['diagnostics']['retry_after_seconds'] == 12
    runtime.run_once()
    assert len(calls) == 1
    clock[0] += 12
    runtime.run_once()
    assert runtime.get(row['id'])['status'] == 'completed' and len(calls) == 2


def test_t1_quota_header_reset_reaches_real_runtime_stop(live, monkeypatch):
    runtime, _, _ = live
    monkeypatch.setattr(tf.time, 'time', lambda: 1800000000.0)
    def upstream(*a, **k):
        raise failure(status=402, body={'error': {'code': 'insufficient_balance'}},
                      **{'x-ratelimit-reset-requests': '90s', 'x-request-id': 'req_quota:1'})
    monkeypatch.setattr(adapters, 'model_urlopen', upstream)
    row = runtime.create({'message': 'hi'})
    runtime.run_once()
    done = runtime.get(row['id'])
    assert done['status'] == 'paused' and done['error'] is None
    recovery = done['connection_recovery']
    assert recovery['state'] == 'disconnected' and recovery['max_retries'] == 0
    assert recovery['diagnostics']['reset_at'] == 1800000090
    assert recovery['diagnostics']['upstream_request_id'] == 'req_quota:1'


@pytest.mark.parametrize('served,mismatch', [('other-model', True), ('vendor/model-a-2026-11-17', False)])
def test_t4_real_bridge_runtime_mismatch_once_per_task(live, monkeypatch, served, mismatch):
    runtime, app, model_id = live
    monkeypatch.setattr(adapters, 'model_urlopen', lambda *a, **k: Response(completion(served)))
    row = runtime.create({'message': 'hi'})
    runtime.run_once()
    current = runtime.get(row['id'])
    assert len([e for e in current['events'] if e['type'] == 'model_mismatch']) == (1 if mismatch else 0)
    runtime.add_message(row['id'], {'message': 'again', 'expected_revision': current['revision']})
    runtime.run_once()
    done = runtime.get(row['id'])
    assert done['status'] == 'completed'
    notices = [e for e in done['events'] if e['type'] == 'model_mismatch']
    assert len(notices) == (1 if mismatch else 0)
    if mismatch:
        assert notices[0]['requested_model'] == 'model-a'
        assert notices[0]['server_model'] == served
    assert done['model_id'] == model_id
