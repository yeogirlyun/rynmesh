"""Large declared model windows must survive the actual inference entry point."""
import json
import struct
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import p2p
from rynmesh.llm_package.api import install_inference_api
from rynmesh.llm_package.chat import validate_chat
from rynmesh.llm_package.manifest import LLMPackageManifest
from rynmesh.llm_package.safety import MAX_REQUEST


@pytest.mark.parametrize('text', ['x' * 1150000, '销售' * 200000], ids=['ascii_over_1mib', 'utf8_over_1mib'])
def test_large_request_reaches_real_authenticated_api_adapter(tmp_path, text):
    calls = []

    class Adapter:
        def health(self):
            return {'ok': True}

        def chat(self, body, **kwargs):
            calls.append(body)
            return {'text': 'received', 'message': {'role': 'assistant', 'content': 'received'},
                    'finish_reason': 'stop', 'input_tokens': len(text) // 4, 'output_tokens': 2, 'duration_ms': 1}

    manager = SimpleNamespace(adapter=Adapter(), manifest=LLMPackageManifest(
        package_id='large', mode='openai_compatible', public_model_alias='large-context',
        base_url='http://127.0.0.1:1', context_window=1048576, max_output_tokens=512),
        _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    app = FastAPI()
    install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id='self'),
                          active_manager=lambda: manager, discover=lambda _: [],
                          execute_order=None, cancel_order=None)
    with TestClient(app) as client:
        key = client.post('/api/local/llm/api-keys', json={'name': 'large-context-check'}).json()['key']
        payload = {'model': 'local/large', 'messages': [{'role': 'user', 'content': text}], 'max_tokens': 8}
        assert len(json.dumps(payload, ensure_ascii=False).encode()) > 1024 * 1024
        result = client.post('/v1/chat/completions', json=payload, headers={'Authorization': 'Bearer ' + key})
        assert result.status_code == 200, result.text
        assert result.json()['choices'][0]['message']['content'] == 'received'
        assert len(calls) == 1 and calls[0]['messages'][-1]['content'] == text
        rejected = client.post('/v1/chat/completions', content=b'x' * (MAX_REQUEST + 1),
                               headers={'Authorization': 'Bearer ' + key})
        assert rejected.status_code == 413
        assert len(calls) == 1


def test_large_transport_retains_a_finite_validated_request_limit():
    with pytest.raises(ValueError, match='request byte limit'):
        validate_chat({'messages': [{'role': 'user', 'content': 'x' * MAX_REQUEST}], 'max_tokens': 8})


def test_encrypted_transport_capacity_fits_large_context_and_chunk_header():
    # A maximum JSON request plus encryption/base64 overhead fits an envelope;
    # the real wire header can represent its complete chunk count.
    assert p2p._MAX_MESSAGE_BYTES >= MAX_REQUEST * 4 // 3 + 1024
    packet = p2p._HEADER.pack(p2p._MAGIC, p2p._DATA, b'x' * 16,
                              p2p._MAX_CHUNKS - 1, p2p._MAX_CHUNKS, b'x' * 32)
    assert p2p._HEADER.unpack(packet)[4] == p2p._MAX_CHUNKS
    with pytest.raises(struct.error):
        p2p._HEADER.pack(p2p._MAGIC, p2p._DATA, b'x' * 16, 0, 65536, b'x' * 32)
