from __future__ import annotations

import copy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.exchange.ledger import Ledger
from rynmesh.exchange.protocol import COVENANT, UNITS, VERSION, ExchangeError, amount
from rynmesh.exchange.service import Exchange


def new_id():
    return uuid.uuid4().hex


class TestModel:
    __test__ = False
    def __init__(self, model, network):
        self.model = model; self.network = network
    def generate(self, prompt, *, system, max_tokens):
        self.network.model_calls.append((self.model, prompt, system))
        return json.dumps({'share_bps': self.network.shares.get(self.model, 10000),
                           'reason_code': 'delivery_matches', 'uncertain': self.network.uncertain})


class Mesh:
    def __init__(self, tmp_path, judges=False):
        self.root = tmp_path; self.nodes = []; self.by_endpoint = {}; self.offline = set()
        self.model_calls = []; self.shares = {}; self.uncertain = False; self.now = 1000000
        count = 12 if judges else 6
        for index in range(count):
            key = bytes([index + 1]) * 32
            node = Exchange(tmp_path / str(index), key, X25519PrivateKey.generate(),
                            post=self.post, clock=lambda: self.now, allow_loopback=True,
                            model_factory=lambda name: TestModel(name, self))
            self.nodes.append(node); self.by_endpoint[self.endpoint(index)] = node
        self.config = {'version': VERSION, 'name': 'Acceptance mesh',
            'validators': [{'peer_id': node.peer_id, 'endpoint': self.endpoint(i)} for i,node in enumerate(self.nodes[:4])],
            'judges': [{'peer_id': node.peer_id, 'endpoint': self.endpoint(i+6),
                        'encryption_key': __import__('rynmesh.services.peer_box', fromlist=['public_key_b64']).public_key_b64(node.messaging_key),
                        'model': f'model-{i}', 'fee': 1000} for i,node in enumerate(self.nodes[6:])] if judges else [],
            'issuance_limit': 100 * UNITS, 'work_reward': UNITS,
            'appeal_window_s': 60, 'covenant': COVENANT}
        for node in self.nodes:
            node.configure(self.config)
            node.options({'registry': True, 'judge': True})
        self.buyer = self.nodes[4]; self.provider = self.nodes[5]
        for node in (self.buyer, self.provider):
            node.action('profile', {'label': 'Acceptance participant'}, new_id())

    @staticmethod
    def endpoint(index): return f'http://127.0.0.1:{18000+index}'

    def post(self, endpoint, path, body):
        if endpoint in self.offline: raise OSError('offline')
        return self.by_endpoint[endpoint].peer(path.rsplit('/', 1)[1], copy.deepcopy(body))

    def reward(self, node=None):
        node = node or self.buyer
        return node.action('reward', {'job_id': new_id(), 'endpoint': next(url for url,n in self.by_endpoint.items() if n is node)}, new_id())

    def order(self, price='0.25'):
        if self.buyer.actor not in self.buyer.ledger.state['rewards'].values(): self.reward()
        self.provider.sync()
        if self.provider.actor not in self.provider.ledger.state['rewards'].values(): self.reward(self.provider)
        listing = new_id(); self.buyer.action('listing', {'kind': 'request', 'category': 'translation',
                    'title': 'Translate a short document', 'description': 'Deliver a checked translation.', 'price': price}, listing)
        proposal_id = new_id(); self.provider.action('propose', {'listing_id': listing, 'scope': 'Translate 100 words.', 'price': price}, proposal_id)
        self.buyer.sync(); proposal = next(p for p in self.buyer.status()['proposals'] if p['id'] == proposal_id)
        order_id = new_id(); self.buyer.action('agree', {'proposal_id': proposal_id, 'terms_hash': proposal['terms_hash']}, order_id)
        self.provider.sync(); return order_id

    def dispute(self):
        order_id = self.order()
        self.provider.action('deliver', {'order_id': order_id, 'body': 'DELIVERY_SECRET_123'}, new_id())
        self.buyer.action('dispute', {'order_id': order_id}, new_id())
        self.buyer.action('evidence', {'order_id': order_id, 'body': 'BUYER_SECRET_123'}, new_id())
        self.provider.action('evidence', {'order_id': order_id, 'body': 'PROVIDER_SECRET_123'}, new_id())
        return order_id


def test_free_entry_and_network_work_issuance_not_dev_balance(tmp_path):
    mesh = Mesh(tmp_path)
    assert mesh.buyer.status()['wallet']['available'] == 0
    assert mesh.buyer.ledger.state['issued'] == 0
    mesh.reward()
    assert mesh.buyer.status()['wallet']['available'] == UNITS
    assert mesh.buyer.status()['wallet']['held'] == 0
    for node in mesh.nodes:
        node.sync()
        assert node.ledger.state == mesh.buyer.ledger.state


def test_complete_offer_request_deliver_accept_and_spend_earnings(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order()
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'PRIVATE_TRANSLATION_ABC'}, new_id())
    mesh.buyer.sync()
    assert mesh.buyer.delivery(order_id)['body'] == 'PRIVATE_TRANSLATION_ABC'
    mesh.buyer.action('accept', {'order_id': order_id, 'delivery_hash': mesh.nodes[0].ledger.state['orders'][order_id]['delivery_hash']}, new_id())
    mesh.provider.sync()
    assert mesh.provider.status()['wallet']['available'] == 1_250_000
    assert mesh.buyer.status()['wallet']['available'] == 750_000
    listing_id = new_id(); mesh.provider.action('listing', {'kind': 'request', 'category': 'editing',
        'title': 'Edit my paragraph', 'description': 'Improve grammar.', 'price': '0.5'}, listing_id)
    proposal_id = new_id(); mesh.buyer.action('propose', {'listing_id': listing_id, 'scope': 'Edit 200 words', 'price': '0.5'}, proposal_id)
    mesh.provider.sync(); proposal = next(p for p in mesh.provider.status()['proposals'] if p['id'] == proposal_id)
    second = new_id(); mesh.provider.action('agree', {'proposal_id': proposal_id, 'terms_hash': proposal['terms_hash']}, second)
    mesh.buyer.action('deliver', {'order_id': second, 'body': 'Edited paragraph'}, new_id())
    mesh.provider.action('accept', {'order_id': second, 'delivery_hash': mesh.nodes[0].ledger.state['orders'][second]['delivery_hash']}, new_id())
    assert mesh.provider.status()['wallet']['available'] == 750_000
    assert sum(a['available'] + a['held'] for a in mesh.provider.ledger.state['accounts'].values()) == 2 * UNITS
    for path in tmp_path.rglob('ledger.sqlite3*'):
        assert b'PRIVATE_TRANSLATION_ABC' not in path.read_bytes()


def test_exactly_once_after_restart_and_lost_commit_response(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order(); op_id = new_id()
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'Private body'}, new_id())
    request = {'order_id': order_id, 'delivery_hash': mesh.nodes[0].ledger.state['orders'][order_id]['delivery_hash']}
    mesh.buyer.action('accept', request, op_id)
    height = mesh.buyer.ledger.height
    key = mesh.buyer.signing_key; msg = mesh.buyer.messaging_key
    mesh.buyer.ledger.close()
    restored = Exchange(tmp_path / '4', key, msg, post=mesh.post, clock=lambda: mesh.now, allow_loopback=True)
    mesh.by_endpoint[mesh.endpoint(4)] = restored
    result = restored.action('accept', request, op_id)
    assert result['committed'] and restored.ledger.height == height
    assert restored.status()['wallet']['available'] == 750_000
    with pytest.raises(ExchangeError, match='operation_changed'):
        restored.action('accept', {'order_id': 'another-order'}, op_id)


def test_quorum_loss_durable_vote_lock_and_resume_after_restart(tmp_path):
    mesh = Mesh(tmp_path); mesh.offline = {mesh.endpoint(0), mesh.endpoint(1)}
    op_id = new_id()
    with pytest.raises(ExchangeError, match='quorum_unavailable'):
        mesh.buyer.action('listing', {'kind': 'offer', 'category': 'editing', 'title': 'Editing',
                          'description': 'Edit a paragraph', 'price': '1'}, op_id)
    assert mesh.buyer.status()['pending'][0]['id'] == op_id
    assert mesh.buyer.status()['wallet']['available'] == 0
    pending = mesh.nodes[2].ledger.pending()[0]
    fork = copy.deepcopy(pending); fork['timestamp'] += 1
    core = {k:v for k,v in fork.items() if k != 'proposer'}
    leader = mesh.nodes[(fork['height'] - 1) % 4]
    fork['proposer'] = leader.signed(core)
    with pytest.raises(ExchangeError, match='height_locked'): mesh.nodes[2].ledger.vote(fork)
    mesh.nodes[2].ledger.close()
    restored = Ledger(tmp_path / '2', mesh.nodes[2].signing_key, clock=lambda: mesh.now)
    with pytest.raises(ExchangeError, match='height_locked'): restored.vote(fork)
    mesh.nodes[2].ledger = restored
    mesh.offline.clear(); mesh.buyer.resume()
    assert op_id in mesh.buyer.ledger.state['listings']
    assert not mesh.buyer.status()['pending']


def test_invalid_signature_duplicate_vote_and_fork_never_change_balance(tmp_path):
    mesh = Mesh(tmp_path); mesh.reward()
    certificate = copy.deepcopy(mesh.buyer.ledger.blocks()['blocks'][0])
    observer = Ledger(tmp_path / 'observer', b'x' * 32, clock=lambda: mesh.now)
    observer.configure(mesh.config)
    duplicate = copy.deepcopy(certificate); duplicate['votes'] = [duplicate['votes'][0]] * 3
    with pytest.raises(ExchangeError, match='duplicate_vote'): observer.commit(duplicate)
    forged = copy.deepcopy(certificate); forged['votes'][0]['signature'] = 'broken'
    with pytest.raises(ExchangeError, match='signature_invalid'): observer.commit(forged)
    assert observer.height == 0 and observer.state['issued'] == 0
    observer.commit(certificate)
    fork = copy.deepcopy(certificate); fork['proposal']['timestamp'] += 1
    with pytest.raises(ExchangeError, match='fork'): observer.commit(fork)


def test_tampered_terms_and_insufficient_funds_create_no_hold(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order(); old = copy.deepcopy(mesh.buyer.ledger.state)
    proposal = next(iter(old['proposals'].values()))
    with pytest.raises(ExchangeError): mesh.buyer.action('agree', {'proposal_id': proposal['id'], 'terms_hash': 'bad'}, new_id())
    assert mesh.buyer.ledger.state == old
    # Existing hold prevents a second purchase spending the same funds.
    with pytest.raises(ExchangeError, match='insufficient_balance'): mesh.order(price='100')
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'working'


def test_mutual_refund_returns_all_reserves_without_commission(tmp_path):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.order()
    mesh.buyer.action('refund', {'order_id': order_id}, new_id())
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'working'
    mesh.provider.action('refund', {'order_id': order_id}, new_id()); mesh.buyer.sync()
    assert mesh.buyer.status()['wallet']['available'] == UNITS
    assert mesh.provider.status()['wallet']['available'] == UNITS
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'refunded'


def test_ai_dispute_fees_appeal_independent_panel_and_finalization(tmp_path):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.dispute()
    mesh.buyer.action('rule', {'order_id': order_id}, new_id())
    assert len(mesh.model_calls) == 3
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'ruling_ready'
    with pytest.raises(ExchangeError, match='appeal_open'): mesh.buyer.action('finalize', {'order_id': order_id}, new_id())
    mesh.buyer.action('appeal', {'order_id': order_id}, new_id())
    mesh.buyer.action('evidence', {'order_id': order_id, 'body': 'Appeal buyer statement'}, new_id())
    mesh.provider.action('evidence', {'order_id': order_id, 'body': 'Appeal provider statement'}, new_id())
    mesh.provider.action('rule', {'order_id': order_id}, new_id())
    assert len(mesh.model_calls) == 6 and len({call[0] for call in mesh.model_calls}) == 6
    mesh.now += 61
    mesh.buyer.action('finalize', {'order_id': order_id}, new_id()); mesh.provider.sync()
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'resolved'
    assert mesh.buyer.status()['wallet']['available'] == 747000
    assert mesh.provider.status()['wallet']['available'] == 1247000
    assert sum(a['available'] + a['held'] for a in mesh.buyer.ledger.state['accounts'].values()) == 2 * UNITS
    for path in tmp_path.rglob('ledger.sqlite3*'):
        for marker in (b'DELIVERY_SECRET_123', b'BUYER_SECRET_123', b'PROVIDER_SECRET_123'):
            assert marker not in path.read_bytes()


@pytest.mark.parametrize('uncertain,shares', [(True, {}), (False, {'model-0': 0})])
def test_uncertain_or_disagreeing_models_hold_funds(tmp_path, uncertain, shares):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.dispute()
    mesh.uncertain = uncertain; mesh.shares = shares
    mesh.buyer.sync()
    before = copy.deepcopy(mesh.buyer.ledger.state)
    with pytest.raises(ExchangeError): mesh.buyer.action('rule', {'order_id': order_id}, new_id())
    assert mesh.buyer.ledger.state == before


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '0.0000001', 'bad'])
def test_integer_precision_rejects_invalid_currency(value):
    with pytest.raises(ExchangeError): amount(value)


def test_duplicate_reward_and_budget_exhaustion(tmp_path):
    mesh = Mesh(tmp_path); job_id = new_id(); value = {'job_id': job_id, 'endpoint': mesh.endpoint(4)}
    mesh.buyer.action('reward', value, new_id())
    with pytest.raises(ExchangeError): mesh.buyer.action('reward', value, new_id())
    assert mesh.buyer.status()['wallet']['available'] == UNITS


def test_delegation_revocation_checked_by_all_validators(tmp_path):
    mesh = Mesh(tmp_path)
    delegate = mesh.nodes[0]
    mesh.buyer.action('device', {'peer_id': delegate.peer_id, 'allowed': True}, new_id())
    delegate.sync(); delegate.options({'acting_for': mesh.buyer.peer_id})
    delegate.action('listing', {'kind': 'offer', 'category': 'editing', 'title': 'Editing',
                               'description': 'A small job', 'price': '0'}, new_id())
    mesh.buyer.action('device', {'peer_id': delegate.peer_id, 'allowed': False}, new_id())
    with pytest.raises(ExchangeError, match='unauthorized'):
        delegate.action('listing', {'kind': 'offer', 'category': 'editing', 'title': 'Editing',
                                    'description': 'Revoked job', 'price': '0'}, new_id())


def test_concurrent_provider_and_seeker_post_without_forks(tmp_path):
    mesh = Mesh(tmp_path)
    ids = [new_id(), new_id()]
    def post(pair):
        node, op_id = pair
        return node.action('listing', {'kind': 'offer', 'category': 'arbitrary lawful work',
                           'title': 'Service', 'description': 'Useful digital value', 'price': '0'}, op_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, zip((mesh.buyer, mesh.provider), ids, strict=True)))
    assert all(r['committed'] for r in results)
    mesh.buyer.sync()
    for node in mesh.nodes:
        node.sync()
        assert set(ids) <= set(node.ledger.state['listings'])
        assert node.ledger.head == mesh.buyer.ledger.head
        assert node.ledger.state == mesh.buyer.ledger.state


def test_status_cannot_mutate_replayed_consensus_state(tmp_path):
    mesh = Mesh(tmp_path)
    before = copy.deepcopy(mesh.nodes[0].ledger.state)
    mesh.nodes[0].status(); mesh.nodes[0].sync()
    assert mesh.nodes[0].ledger.state == before
    assert mesh.nodes[0].peer_id not in mesh.nodes[0].ledger.state['accounts']


def test_registry_requires_optin_and_independent_witnesses(tmp_path):
    mesh = Mesh(tmp_path); mesh.buyer.options({'registry': False})
    with pytest.raises(ExchangeError, match='work_unverified'): mesh.reward()
    assert mesh.buyer.ledger.state['issued'] == 0
    mesh.buyer.options({'registry': True}); mesh.reward()
    with pytest.raises(ExchangeError, match='registry_already_rewarded'): mesh.reward()
    assert mesh.buyer.ledger.state['issued'] == UNITS


def test_actual_issuance_cap_and_network_cannot_be_replaced(tmp_path):
    mesh = Mesh(tmp_path)
    low = copy.deepcopy(mesh.config); low['issuance_limit'] = UNITS
    isolated = Mesh.__new__(Mesh)
    isolated.root = tmp_path / 'low'; isolated.nodes = []; isolated.by_endpoint = {}; isolated.offline = set(); isolated.now = mesh.now
    for index, old in enumerate(mesh.nodes):
        node = Exchange(isolated.root / str(index), old.signing_key, old.messaging_key,
                        post=isolated.post, clock=lambda: isolated.now, allow_loopback=True)
        node.configure(low); node.options({'registry': True})
        isolated.nodes.append(node); isolated.by_endpoint[isolated.endpoint(index)] = node
    isolated.buyer = isolated.nodes[4]; isolated.provider = isolated.nodes[5]
    for node in (isolated.buyer, isolated.provider): node.action('profile', {'label': 'Participant'}, new_id())
    isolated.reward()
    with pytest.raises(ExchangeError, match='issuance_exhausted'): isolated.reward(isolated.provider)
    assert isolated.provider.ledger.state['issued'] == UNITS
    with pytest.raises(ExchangeError, match='network_changed'): isolated.buyer.configure(mesh.config)


def test_judges_verify_delivery_and_cache_same_case_without_extra_model_calls(tmp_path):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.dispute()
    mesh.buyer.sync()
    first = mesh.buyer.prepare('rule', {'order_id': order_id}, new_id())
    second = mesh.buyer.prepare('rule', {'order_id': order_id}, new_id())
    assert first == second and len(mesh.model_calls) == 3
    for _, prompt, _ in mesh.model_calls:
        assert json.loads(prompt)['verified_delivery'] == 'DELIVERY_SECRET_123'


def test_signed_tampered_delivery_cannot_be_accepted(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order()
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'Original'}, new_id())
    mesh.buyer.sync()
    mesh.buyer.ledger.state['orders'][order_id]['delivery']['ciphertext'] = 'tampered'
    before = mesh.buyer.ledger.height
    with pytest.raises(ExchangeError, match='delivery_unavailable'): mesh.buyer.prepare('accept', {'order_id': order_id, 'delivery_hash': mesh.nodes[0].ledger.state['orders'][order_id]['delivery_hash']}, new_id())
    assert mesh.buyer.ledger.height == before


def test_fee_reserves_cover_odd_prices_in_both_rounds():
    from rynmesh.exchange.protocol import fees
    assert fees({'judges': [{'fee': 1001}] * 6}) == (3004, 3002)


def test_exchange_http_owner_gate_bounded_body_and_signed_identity(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from rynmesh.exchange.protocol import signature
    from rynmesh.exchange.routes import install_exchange
    mesh = Mesh(tmp_path); node = mesh.buyer
    store = SimpleNamespace(home=tmp_path / '4', private_key_bytes=node.signing_key, peer_id=node.peer_id)
    app = FastAPI()
    def guard(request):
        if request.headers.get('x-owner') != 'yes': raise HTTPException(403)
    install_exchange(app, store=store, messaging_key=node.messaging_key, local_control=guard)
    app.state.exchange = node
    with TestClient(app) as client:
        assert client.get('/api/local/exchange').status_code == 403
        assert client.post('/api/local/exchange/action', json={'action': 'refresh'}).status_code == 403
        assert client.get('/api/local/exchange', headers={'x-owner': 'yes'}).status_code == 200
        assert client.post('/api/local/exchange/action', headers={'x-owner': 'yes'}, content=b'x' * (512 * 1024 + 1)).status_code == 413
        assert client.post('/api/peer/exchange/vote', json={}).status_code == 409
        assert client.post('/api/peer/exchange/identity', json={'bad': True}).status_code == 400
        scoped = {'action': 'listing', 'value': {'kind': 'offer', 'category': 'editing', 'title': 'Public work', 'description': 'A small service', 'price': '0'}, 'operation_id': new_id(), 'actor': None}
        assert client.post('/api/local/exchange/action', headers={'x-owner': 'yes'}, json=scoped).status_code == 400
        scoped['actor'] = mesh.provider.peer_id
        rejected = client.post('/api/local/exchange/action', headers={'x-owner': 'yes'}, json=scoped)
        assert rejected.status_code == 409 and rejected.json()['detail'] == 'exchange_account_changed'
        scoped['actor'] = node.peer_id
        assert client.post('/api/local/exchange/action', headers={'x-owner': 'yes'}, json=scoped).status_code == 200
        identity = client.post('/api/peer/exchange/identity', json={})
        assert identity.status_code == 200
        assert signature(identity.json()).public_key == node.peer_id
        assert client.post('/api/local/exchange/action', headers={'x-owner': 'yes'}, json={'action': 'profile', 'value': {'label': 'Another name'}, 'operation_id': new_id(), 'extra': True}).status_code == 400


def test_root_delegation_does_not_silently_use_wrong_decryption_key(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order(); delegate = mesh.nodes[0]
    mesh.provider.action('device', {'peer_id': delegate.peer_id, 'allowed': True}, new_id())
    delegate.sync(); delegate.options({'acting_for': mesh.provider.peer_id})
    with pytest.raises(ExchangeError, match='private_key_required'):
        delegate.action('deliver', {'order_id': order_id, 'body': 'Do not strand this content'}, new_id())


def test_partial_price_decision_refunds_remainder_and_unused_appeal_fees(tmp_path):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.dispute()
    mesh.shares = {f'model-{i}': 5000 for i in range(3)}
    mesh.buyer.action('rule', {'order_id': order_id}, new_id())
    mesh.buyer.action('waive', {'order_id': order_id}, new_id())
    mesh.provider.action('waive', {'order_id': order_id}, new_id())
    mesh.buyer.action('finalize', {'order_id': order_id}, new_id()); mesh.provider.sync()
    assert mesh.buyer.status()['wallet']['available'] == 873500
    assert mesh.provider.status()['wallet']['available'] == 1123500
    assert mesh.buyer.ledger.state['orders'][order_id]['paid'] == 125000


def test_direct_transfer_is_final_idempotent_and_cannot_spend_held_coins(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order(); operation = new_id()
    value = {'recipient': mesh.provider.peer_id, 'amount': '0.5'}
    mesh.buyer.action('transfer', value, operation)
    mesh.buyer.action('transfer', value, operation)
    assert mesh.buyer.status()['wallet']['available'] == 250000
    assert mesh.buyer.status()['wallet']['held'] == 250000
    with pytest.raises(ExchangeError, match='insufficient_balance'):
        mesh.buyer.action('transfer', {'recipient': mesh.provider.peer_id, 'amount': '0.3'}, new_id())
    mesh.provider.sync()
    assert mesh.provider.status()['wallet']['available'] == 1500000
    assert mesh.provider.ledger.state['orders'][order_id]['status'] == 'working'


def test_paid_participant_is_excluded_from_both_judge_panels(tmp_path):
    from rynmesh.exchange.protocol import judge_panel
    mesh = Mesh(tmp_path, judges=True)
    config = copy.deepcopy(mesh.config)
    config['judges'] += [dict(config['judges'][0], peer_id=mesh.buyer.peer_id, model='model-buyer'),
                         dict(config['judges'][1], peer_id=mesh.provider.peer_id, model='model-provider')]
    config['judges'] = config['judges'][-2:] + config['judges'][:-2]
    order = {'buyer': mesh.buyer.peer_id, 'provider': mesh.provider.peer_id}
    panels = judge_panel(config, order, 0) + judge_panel(config, order, 1)
    assert len(panels) == 6
    assert not {order['buyer'], order['provider']} & {j['peer_id'] for j in panels}


def test_maximum_private_delivery_and_evidence_fit_bounded_wire(tmp_path):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.order()
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'D' * (32 * 1024)}, new_id())
    mesh.buyer.action('dispute', {'order_id': order_id}, new_id())
    mesh.buyer.action('evidence', {'order_id': order_id, 'body': 'E' * 8192}, new_id())
    mesh.provider.action('evidence', {'order_id': order_id, 'body': 'F' * 8192}, new_id())
    mesh.buyer.action('rule', {'order_id': order_id}, new_id())
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'ruling_ready'
    for certificate in mesh.buyer.ledger.blocks()['blocks']:
        assert len(json.dumps(certificate).encode()) < 512 * 1024


def test_original_submission_recovers_if_response_lost_after_remote_commit(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order()
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'Private completed work'}, new_id())
    normal = mesh.buyer.post
    failed = {'once': True}
    def lost(endpoint, path, body):
        if path.endswith('/vote') and failed['once']:
            # Commit at a remote observer, then make the coordinator see transport loss.
            failed['once'] = False
            mesh.provider._certify(body)
            raise OSError('lost after remote commit')
        return normal(endpoint, path, body)
    mesh.buyer.post = lost; operation = new_id()
    mesh.buyer.action('accept', {'order_id': order_id, 'delivery_hash': mesh.nodes[0].ledger.state['orders'][order_id]['delivery_hash']}, operation)
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'accepted'
    assert mesh.buyer.status()['wallet']['available'] == 750000
    assert mesh.buyer.status()['receipts'][0]['status'] == 'committed'


def test_two_authorized_devices_cannot_double_spend_same_account(tmp_path):
    import threading
    mesh = Mesh(tmp_path); mesh.reward()
    devices = mesh.nodes[:2]
    for device in devices:
        mesh.buyer.action('device', {'peer_id': device.peer_id, 'allowed': True}, new_id())
    barrier = threading.Barrier(2)
    def synchronize(original):
        first = True
        def submitted(command):
            nonlocal first
            if first:
                first = False
                barrier.wait(timeout=5)
            return original(command)
        return submitted
    for device in devices:
        device.sync(); device.options({'acting_for': mesh.buyer.peer_id})
        device.proposal = synchronize(device.proposal)
    def spend(device):
        try:
            device.action('transfer', {'recipient': mesh.provider.peer_id, 'amount': '0.75'}, new_id())
            return 'committed'
        except ExchangeError:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(spend, devices))
    assert sorted(results) == ['committed', 'rejected']
    for device in devices: device.resume()
    mesh.buyer.sync(); mesh.provider.sync()
    assert mesh.buyer.status()['wallet']['available'] == 250000
    assert mesh.provider.status()['wallet']['available'] == 750000
    assert sum(a['available'] + a['held'] for a in mesh.buyer.ledger.state['accounts'].values()) == UNITS
    assert any(r['status'] == 'superseded' for d in devices for r in d.status()['receipts'])


def test_acceptance_binds_reviewed_delivery_and_rejects_later_revision(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order()
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'First reviewed delivery'}, new_id())
    mesh.buyer.sync(); reviewed = mesh.buyer.delivery(order_id)
    mesh.provider.action('deliver', {'order_id': order_id, 'body': 'Later different delivery'}, new_id())
    with pytest.raises(ExchangeError, match='delivery_changed'):
        mesh.buyer.action('accept', {'order_id': order_id, 'delivery_hash': reviewed['hash']}, new_id())
    assert mesh.buyer.status()['wallet']['held'] == 250000
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'delivered'
    latest = mesh.buyer.delivery(order_id)
    mesh.buyer.action('accept', {'order_id': order_id, 'delivery_hash': latest['hash']}, new_id())
    assert mesh.buyer.status()['wallet']['held'] == 0


def test_open_hold_stays_visible_when_later_history_grows(tmp_path):
    mesh = Mesh(tmp_path); order_id = mesh.order()
    original = mesh.buyer.ledger.state['orders'][order_id]
    # A retained history snapshot must never displace an older live hold.
    for i in range(250):
        mesh.buyer.ledger.state['orders'][f'history-{i}'] = dict(original, id=f'history-{i}', status='accepted', paid=0)
    rows = mesh.buyer.status()['orders']
    assert len(rows) == 200
    assert rows[0]['id'] == order_id and rows[0]['status'] == 'working'


@pytest.mark.parametrize('decided', [False, True])
def test_both_parties_can_refund_unsettled_dispute_and_keep_incurred_fees(tmp_path, decided):
    mesh = Mesh(tmp_path, judges=True); order_id = mesh.dispute()
    if decided: mesh.buyer.action('rule', {'order_id': order_id}, new_id())
    mesh.buyer.action('refund', {'order_id': order_id}, new_id())
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] != 'refunded'
    mesh.provider.action('refund', {'order_id': order_id}, new_id()); mesh.buyer.sync()
    assert mesh.buyer.ledger.state['orders'][order_id]['status'] == 'refunded'
    assert mesh.buyer.ledger.state['orders'][order_id]['paid'] == 0
    expected = 998500 if decided else UNITS
    assert mesh.buyer.status()['wallet']['available'] == expected
    assert mesh.provider.status()['wallet']['available'] == expected
    assert mesh.buyer.status()['wallet']['held'] == mesh.provider.status()['wallet']['held'] == 0


def test_account_switch_waits_for_inflight_payment_and_cannot_change_payer(tmp_path):
    import threading
    mesh = Mesh(tmp_path); mesh.reward()
    delegate = mesh.nodes[0]
    delegate.action('profile', {'label': 'Separate account'}, new_id()); mesh.reward(delegate)
    mesh.buyer.action('device', {'peer_id': delegate.peer_id, 'allowed': True}, new_id())
    delegate.sync(); delegate.options({'acting_for': mesh.buyer.peer_id})
    preparing, release, switching = threading.Event(), threading.Event(), threading.Event()
    prepare = delegate.prepare
    def paused(action, value, operation_id):
        if action == 'transfer':
            preparing.set(); assert release.wait(timeout=5)
        return prepare(action, value, operation_id)
    delegate.prepare = paused
    def switch():
        switching.set()
        return delegate.options({'acting_for': delegate.peer_id})
    with ThreadPoolExecutor(max_workers=2) as pool:
        payment = pool.submit(delegate.action, 'transfer', {'recipient': mesh.provider.peer_id, 'amount': '0.5'}, new_id())
        assert preparing.wait(timeout=5)
        changed = pool.submit(switch)
        assert switching.wait(timeout=5)
        try:
            with pytest.raises(TimeoutError): changed.result(timeout=0.05)
        finally: release.set()
        assert payment.result(timeout=5)['committed']
        changed.result(timeout=5)
    mesh.buyer.sync(); mesh.provider.sync()
    assert mesh.buyer.status()['wallet']['available'] == 500000
    assert delegate.status()['wallet']['available'] == UNITS
    assert mesh.provider.status()['wallet']['available'] == 500000


def test_stale_account_review_rejects_before_signing_or_spending(tmp_path):
    mesh = Mesh(tmp_path); mesh.reward()
    with pytest.raises(ExchangeError, match='account_changed'):
        mesh.buyer.action('transfer', {'recipient': mesh.provider.peer_id, 'amount': '0.5'}, new_id(), expected_actor=mesh.provider.peer_id)
    assert mesh.buyer.status()['wallet']['available'] == UNITS
    assert not mesh.buyer.status()['pending']


def test_noncanonical_public_key_cannot_count_as_a_second_identity():
    import base64

    from rynmesh.crypto import public_key_from_private
    from rynmesh.exchange.protocol import key
    canonical = public_key_from_private(b'x' * 32)
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    alias = canonical[:-2] + alphabet[alphabet.index(canonical[-2]) + 1] + '='
    assert base64.b64decode(alias) == base64.b64decode(canonical)
    with pytest.raises(ExchangeError, match='key_invalid'): key(alias)
