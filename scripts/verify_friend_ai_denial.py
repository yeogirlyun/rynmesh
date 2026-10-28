"""Real signed HTTP denial checks, after revoking an acceptance fixture friend."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from rynmesh.friends.store import FriendStore
from rynmesh.llm_package.task_protocol import open_task, seal_task
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def main():
    workspace = Path(__file__).resolve().parents[1]
    home = workspace / '.rynmesh/fai-new-b'
    friend = RynmeshStore(home=home, network_dir=home / 'network', node_name='FAI-New-B')
    relation = FriendStore(home).list_relationships()[0]
    stranger_home = workspace / '.rynmesh/fai-stranger'
    stranger = RynmeshStore(home=stranger_home, network_dir=stranger_home / 'network', node_name='FAI-Stranger')
    checks = []
    for label, sender in [('revoked_B', friend), ('never_authorized_C', stranger)]:
        key = peer_box.load_or_create_messaging_key(sender.home / 'messaging.x25519')
        task_id = 'task_' + uuid.uuid4().hex
        body = {'task_id': task_id, 'service_id': 'local-small', 'prompt': 'Denied synthetic acceptance question',
                'max_tokens': 8, 'max_amount': 0, 'reply_messaging_pub': peer_box.public_key_b64(key),
                'ai_permission': {'relationship_id': relation['relationship_id'], 'revision': 1}}
        wire = seal_task(body=body, task_id=task_id, kind='llm_request', sender_peer_id=sender.peer_id,
                         recipient_peer_id=relation['peer_id'], sender_signing_key=sender.private_key_bytes,
                         recipient_messaging_pub=relation['messaging_pub'],
                         expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()).to_dict()
        response = httpx.post(relation['endpoint'] + '/api/peer/llm/tasks', json=wire, timeout=15)
        response.raise_for_status()
        result = open_task(response.json(), recipient_peer_id=sender.peer_id, recipient_messaging_key=key, expected_kind='llm_response')[1]
        assert result['state'] == 'rejected' and result['error_code'] == 'ai_permission_denied'
        checks.append({'sender': label, 'task_id': task_id, 'state': result['state'], 'error_code': result['error_code']})
    client = httpx.Client(base_url='http://127.0.0.1:19044')
    operation = client.get('/api/local/friends/ai-joins').json()['operations'][0]
    resumed = client.post('/api/local/friends/ai-joins/' + operation['operation_id'], json={}).json()
    assert resumed['stage'] != 'complete' and resumed['error'] == 'ai_invite_revoked'
    result = {'scope': 'Real identities, sealed requests and provider HTTP. No inference should run for these denials.',
              'checks': checks, 'resume_after_revocation': {'stage': resumed['stage'], 'error': resumed['error']}}
    target = workspace / 'docs/acceptance/friend-ai-sharing/step2-denials.json'
    target.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
