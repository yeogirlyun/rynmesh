"""Durable, authenticated host-to-company authorization notifications.

The journal contains cumulative revocation watermarks, not bearer credentials.
A receiver persists the watermarks before ACK, so duplicate/out-of-order delivery
and a crash on either side cannot resurrect an older browser authorization.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
from urllib.parse import urlsplit

import httpx

from ..store import AgentError
from ...file_transactions import file_transaction

COMPANY_PLUGINS = frozenset({'company-workspace', 'company-dashboard', 'company-affiliate'})
STATE_KEY = 'company_authorization'


def _state(document):
    return document.setdefault(STATE_KEY, {'sequence': 0, 'revocations': {}, 'acknowledged': -1})


def record_revocation(document, plugins, *, reason):
    """Call inside the same credential transaction that removes the grant."""
    state = _state(document)
    scopes = sorted(set(plugins))
    if not scopes or any(scope not in COMPANY_PLUGINS | {'*'} for scope in scopes):
        return None
    state['sequence'] += 1
    for scope in scopes:
        state['revocations'][scope] = state['sequence']
    state['reason'] = reason
    # Every outstanding ticket predates this change; never bootstrap a prior user.
    state.pop('bootstrap', None)
    return state['sequence']


def authorization_binding(server):
    """Only identity/security changes invalidate browser grants, not discovery."""
    if not server or server.get('plugin_id') not in COMPANY_PLUGINS:
        return None
    binding = {key: copy.deepcopy(server.get(key)) for key in (
        'plugin_id', 'instance_id', 'url', 'auth', 'identity_group_id',
        'bearer_token_env', 'env_http_headers', 'oauth_client_registration')}
    headers = {key.lower(): value for key, value in (server.get('secret_headers') or {}).items()
               if key.lower() not in {'x-ryn-service-token', 'x-ryn-service-generation'}}
    binding['credentialFingerprint'] = hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).hexdigest()
    return binding


class CompanyAuthorizationChannel:
    def __init__(self, oauth, *, sender=None):
        self.oauth, self.credentials = oauth, oauth.credentials
        self.sender = sender or self._post
        self._start_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._bootstrap_lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = threading.Event()
        self._thread = None
        self._renewing = False
        self._renew_lock = threading.Lock()

    def start(self):
        with self._start_lock:
            if self._thread is None or self._closed.is_set():
                self._closed = threading.Event()
                self._thread = threading.Thread(target=self._worker, args=(self._closed,),
                                                name='company-authorization', daemon=True)
                self._thread.start()

    def close(self):
        self._closed.set()
        self._wake.set()

    def _worker(self, stopped):
        while not stopped.is_set():
            try:
                self.sync_bindings()
                self.flush()
                self._start_renewal()
            except Exception:
                # Journal is retained; only an exact authenticated ACK advances it.
                pass
            self._wake.wait(1)
            self._wake.clear()

    def sync_bindings(self):
        # Covers a crash after extension storage commits but before a route can
        # deliver its notification, as well as changes through non-HTTP import.
        current = {}
        for server in self.oauth.store.list_mcp_servers_internal(include_tools=False):
            binding = authorization_binding(server)
            if binding:
                current[server['id']] = {**binding, 'enabled': bool(server.get('effective_enabled'))}
        with self.credentials.transaction(document=True) as document:
            state = _state(document)
            previous = state.get('bindings')
            if previous is not None:
                scopes = {binding['plugin_id'] for sid, binding in previous.items()
                          if sid not in current or current[sid] != binding}
                if scopes:
                    record_revocation(document, scopes, reason='configuration_changed')
            state['bindings'] = current

    def register(self, *, service_id, plugin_ids, server_ids, port, credential, generation):
        # Called only after the native owner prepare handshake checked the actual
        # installed server bindings. Never use a user supplied arbitrary endpoint.
        if service_id != 'company' or not set(plugin_ids).issubset(COMPANY_PLUGINS):
            return
        if not server_ids:
            prior = self.credentials.read().get(STATE_KEY, {}).get('service') or {}
            if prior.get('id') != service_id or prior.get('port') != port:
                raise AgentError('agent_local_service_binding_invalid')
        for sid in server_ids:
            server = self.oauth.store.get_mcp_server_internal(sid, require_enabled=False)
            parsed = urlsplit(server.get('url') or '')
            if (server.get('plugin_id') not in COMPANY_PLUGINS or parsed.scheme != 'http'
                    or parsed.hostname != '127.0.0.1' or parsed.port != port
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise AgentError('agent_local_service_binding_invalid')
        self.sync_bindings()
        with self.credentials.transaction(document=True) as document:
            state = _state(document)
            state['service'] = {'id': service_id, 'port': port, 'credential': credential,
                                'generation': generation, 'plugin_ids': list(plugin_ids)}
            state['acknowledged'] = -1  # A new process must replay even an already ACKed state.
        self.start()
        self._wake.set()

    def authenticate(self, token, generation):
        state = self.credentials.read().get(STATE_KEY, {})
        service = state.get('service') or {}
        if (not service.get('credential') or not service.get('generation')
                or not hmac.compare_digest(str(token), service['credential'])
                or not hmac.compare_digest(str(generation), service['generation'])):
            raise AgentError('agent_local_service_denied')
        return state

    @staticmethod
    def payload(state):
        value = {key: copy.deepcopy(state.get(key, {} if key == 'revocations' else 0))
                 for key in ('sequence', 'revocations')}
        if state.get('bootstrap'):
            value['bootstrap'] = copy.deepcopy(state['bootstrap'])
        return value

    def _authorized_scopes(self):
        states = self.oauth.company_connections()
        servers = self.oauth.store.list_mcp_servers_internal(include_tools=False)
        scopes = {s['plugin_id'] for s in servers
                  if s.get('plugin_id') in COMPANY_PLUGINS and states.get(s['id'])}
        if scopes:
            # Workbench and affiliate reuse the verified company member; their
            # own backend still checks current member/admin permissions. Only
            # Dashboard requires its additional upstream OAuth grant.
            scopes.update(s['plugin_id'] for s in servers
                          if s.get('plugin_id') in {'company-workspace', 'company-affiliate'}
                          and s.get('effective_enabled'))
        document = self.credentials.read()
        login_sequence = (document.get('company_action') or {}).get('authorizationSequence', 0)
        state = document.get(STATE_KEY, {})
        revoked, granted = state.get('revocations', {}), state.get('scope_grants', {})
        return sorted(scope for scope in scopes if revoked.get(scope, 0) <= max(login_sequence, granted.get(scope, 0)))

    def _leases(self):
        document = self.credentials.read()
        action = document.get('company_action') or {}
        if action.get('action') != 'login':
            return {}
        source = next((s for s in self.oauth.store.list_mcp_servers_internal(include_tools=False)
                       if s['id'] == action.get('serverId') and s.get('effective_enabled')
                       and s.get('instance_id') == action.get('instanceId')), None)
        row = document['servers'].get((source or {}).get('instance_id'), {})
        tokens = row.get('tokens') or {}
        if not source or row.get('error') or not tokens or row.get('pending'):
            return {}
        return {source['id']: {'expiresAt': tokens.get('expires_at', 0), 'actionId': action['id'],
                               'revision': row.get('lease_revision', 0),
                               'authorizedScopes': self._authorized_scopes()}}

    def _start_renewal(self):
        # A rotating authorization-server refresh can be slow. Keep it on a
        # separate worker, never behind the local revocation delivery lock.
        if not self._renew_lock.acquire(blocking=False):
            return
        try:
            if self._renewing:
                return
            document = self.credentials.read()
            action = document.get('company_action') or {}
            row = document['servers'].get(action.get('instanceId'), {})
            tokens = row.get('tokens') or {}
            if (action.get('action') != 'login' or row.get('error') or row.get('pending')
                    or not tokens.get('refresh_token') or tokens.get('expires_at', 0) > self.oauth.clock() + 60):
                return
            self._renewing = True
        finally:
            self._renew_lock.release()
        def renew():
            try:
                server = self.oauth.store.get_mcp_server_internal(action['serverId'], require_enabled=True)
                self.oauth.access_token(server)
            except AgentError:
                pass
            finally:
                self._renewing = False
        threading.Thread(target=renew, name='company-authorization-renewal', daemon=True).start()

    def snapshot(self, token, generation):
        value = self.payload(self.authenticate(token, generation))
        # restart_all() closes transports but reuses this OAuth manager.
        self.start()
        value['leases'] = self._leases()
        return value

    def apply_service_logouts(self, token, generation, events):
        self.authenticate(token, generation)
        if not isinstance(events, list) or len(events) > 32:
            raise AgentError('agent_local_service_invalid')
        acknowledgements, instances = [], set()
        with self.oauth._lock, file_transaction(self.oauth.store.lock):
            servers = self.oauth.store.list_mcp_servers_internal(include_tools=False)
            with self.credentials.transaction(document=True) as document:
                state = _state(document)
                service = state.get('service') or {}
                if (not hmac.compare_digest(str(token), service.get('credential', ''))
                        or not hmac.compare_digest(str(generation), service.get('generation', ''))):
                    raise AgentError('agent_local_service_denied')
                seen = state.setdefault('logout_events', {})
                for event in events:
                    if (not isinstance(event, dict)
                            or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', str(event.get('eventId', '')))
                            or not isinstance(event.get('expectedActionId'), str)
                            or type(event.get('observedSequence')) is not int
                            or not 0 <= event['observedSequence'] <= state['sequence']):
                        raise AgentError('agent_local_service_invalid')
                    event_id, sid = event['eventId'], event.get('serverId')
                    if sid is not None and not isinstance(sid, str):
                        raise AgentError('agent_local_service_invalid')
                    if event_id in seen:
                        acknowledgements.append({'eventId': event_id, 'applied': seen[event_id]['applied']})
                        continue
                    targets = [s for s in servers if s.get('plugin_id') in COMPANY_PLUGINS
                               and (sid is None or s['id'] == sid)]
                    if sid is not None and not targets:
                        # Deleted connections are already unusable. Never let a
                        # company service act on an unrelated installed plugin.
                        if any(s['id'] == sid for s in servers) or not sid.startswith('company-'):
                            raise AgentError('agent_local_service_denied')
                    scopes = {'*'} if sid is None else {s['plugin_id'] for s in targets}
                    action = document.get('company_action') or {}
                    applied = bool(scopes and action.get('id') == event['expectedActionId']
                                   and action.get('action') == 'login')
                    if sid is not None and any(state.get('scope_grants', {}).get(scope, 0) > event['observedSequence']
                                               for scope in scopes):
                        applied = False
                    if any(state.get('revocations', {}).get(scope, 0) > event['observedSequence'] for scope in scopes):
                        applied = False
                    if applied:
                        for server in targets:
                            instance = server['instance_id']
                            instances.add(instance)
                            row = document['servers'].get(instance)
                            if row:
                                row.pop('tokens', None)
                                row.pop('pending', None)
                                row.pop('error', None)
                                row.update(local_logged_out=True, epoch=secrets.token_hex(16),
                                           lease_revision=row.get('lease_revision', 0) + 1)
                        if sid is None:
                            document['company_action'] = {'id': secrets.token_hex(16), 'action': 'logout'}
                        record_revocation(document, scopes, reason='service_logout')
                    # Deduplication is committed atomically with credential
                    # removal, before any response/ACK can be lost.
                    seen[event_id] = {'applied': applied, 'sequence': state['sequence']}
                    acknowledgements.append({'eventId': event_id, 'applied': applied})
                while len(seen) > 256:
                    del seen[next(iter(seen))]
            flows = [(flow['server']['id'], ident) for ident, flow in self.oauth._flows.items()
                     if flow['server']['instance_id'] in instances]
        for sid, ident in flows:
            self.oauth.cancel_login(sid, ident)
        self._wake.set()
        return acknowledgements

    def acknowledge(self, token, generation, sequence):
        self.authenticate(token, generation)
        with self.credentials.transaction(document=True) as document:
            state = _state(document)
            service = state.get('service') or {}
            if (not hmac.compare_digest(str(token), service.get('credential', ''))
                    or not hmac.compare_digest(str(generation), service.get('generation', ''))):
                raise AgentError('agent_local_service_denied')
            if type(sequence) is not int or sequence < 0 or sequence > state['sequence']:
                raise AgentError('agent_local_service_invalid')
            state['acknowledged'] = max(state.get('acknowledged', -1), sequence)
        return {'sequence': sequence}

    @staticmethod
    def _post(url, payload, headers):
        with httpx.Client(timeout=httpx.Timeout(2, connect=.5), trust_env=False, follow_redirects=False) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def flush(self):
        # One bounded network request, outside every file/OAuth lock.
        if not self._send_lock.acquire(timeout=2.5):
            return False
        try:
            state = self.credentials.read().get(STATE_KEY, {})
            service = state.get('service')
            if not service:
                return False
            if state.get('acknowledged', -1) >= state.get('sequence', 0):
                return True
            payload = self.payload(state)
            headers = {'X-Ryn-Service-Token': service['credential'],
                       'X-Ryn-Service-Generation': service['generation']}
            try:
                result = self.sender(f"http://127.0.0.1:{service['port']}/api/local/runtime/authorization-events", payload, headers)
                if result != {'sequence': payload['sequence']}:
                    return False
                self.acknowledge(service['credential'], service['generation'], payload['sequence'])
                return True
            except (httpx.HTTPError, ValueError, AgentError, OSError):
                return False
        finally:
            self._send_lock.release()

    def account_target(self):
        service = self.credentials.read().get(STATE_KEY, {}).get('service')
        if not service:
            raise AgentError('agent_local_service_unavailable')
        return (f"http://127.0.0.1:{service['port']}/api/local/runtime/company-account",
                {'X-Ryn-Service-Token': service['credential'],
                 'X-Ryn-Service-Generation': service['generation']})

    def delivery_status(self):
        state = self.credentials.read().get(STATE_KEY, {})
        sequence = state.get('sequence', 0)
        acknowledged = state.get('acknowledged', -1) >= sequence or (sequence == 0 and not state.get('service'))
        return {'sequence': sequence, 'acknowledged': acknowledged, 'pending': not acknowledged}

    def changed(self):
        if self.credentials.read().get(STATE_KEY, {}).get('service'):
            self.start()
            self._wake.set()
        self.flush()
        return self.delivery_status()

    def revoke(self, plugins, reason):
        with self.credentials.transaction(document=True) as document:
            record_revocation(document, plugins, reason=reason)
        return self.changed()

    def changed_server(self, previous, current):
        before, after = authorization_binding(previous), authorization_binding(current)
        if before and (before != after or (previous.get('effective_enabled') and not current.get('effective_enabled'))):
            return self.revoke([previous['plugin_id']], 'configuration_changed')
        return None

    def bootstrap(self, plugin_id):
        with self._bootstrap_lock:
            return self._bootstrap_once(plugin_id)

    def _bootstrap_once(self, plugin_id):
        if plugin_id not in COMPANY_PLUGINS:
            raise AgentError('agent_local_service_denied')
        servers = self.oauth.store.list_mcp_servers_internal(include_tools=False)
        if not any(s.get('plugin_id') == plugin_id and s.get('effective_enabled') for s in servers):
            raise AgentError('agent_mcp_auth_required')
        action = self.oauth.company_action()
        if action.get('action') != 'login':
            raise AgentError('agent_mcp_auth_required')
        source = next((s for s in servers if s['id'] == action.get('serverId')
                       and s.get('instance_id') == action.get('instanceId')
                       and s.get('effective_enabled')), None)
        if not source or not self.oauth.access_token(source):
            raise AgentError('agent_mcp_auth_required')
        row = self.credentials.read()['servers'].get(source['instance_id'], {})
        # Browser lifetime cannot exceed the current authority's access lease.
        expiry = (row.get('tokens') or {}).get('expires_at', 0)
        if expiry <= self.oauth.clock():
            raise AgentError('agent_mcp_auth_required')
        ticket = secrets.token_urlsafe(32)
        scopes = self._authorized_scopes()
        with file_transaction(self.oauth.store.lock):
            self.oauth._current(source)
            with self.credentials.transaction(document=True) as document:
                current_row = document['servers'].get(source['instance_id'], {})
                if (document.get('company_action') != action or not current_row.get('tokens')
                        or current_row.get('error') or current_row.get('epoch') != row.get('epoch')):
                    raise AgentError('agent_mcp_oauth_configuration_changed')
                state = _state(document)
                if not state.get('service') or plugin_id not in state['service']['plugin_ids']:
                    raise AgentError('agent_local_service_denied')
                login_sequence = action.get('authorizationSequence', 0)
                scopes = [scope for scope in scopes if state['revocations'].get(scope, 0) <=
                          max(login_sequence, state.get('scope_grants', {}).get(scope, 0))]
                state['sequence'] += 1
                state['bootstrap'] = {'ticketHash': hashlib.sha256(ticket.encode()).hexdigest(),
                    'expiresAt': self.oauth.clock() + 60, 'actionId': action['id'],
                    'source': source['id'], 'pluginId': plugin_id, 'sessionExpiresAt': expiry,
                    'authorizedScopes': scopes}
        if not self.changed()['acknowledged']:
            raise AgentError('agent_local_service_unavailable')
        service = self.credentials.read()[STATE_KEY]['service']
        return {'ticket': ticket, 'origin': f"http://127.0.0.1:{service['port']}"}
