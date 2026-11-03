"""Publisher-verified routing reminders, not a desktop intent classifier."""
from __future__ import annotations

import re

from .store import AgentToolRejected


def check(store, context, process_name, routes):
    """Read-only observation remains available; only a matching desktop write enters here."""
    process = str(process_name or '').replace('\\', '/').rsplit('/', 1)[-1].casefold()
    matching = [route for route in routes if process in route['process_names']]
    if not matching:
        return
    row = store.read()['tasks'][context['task_id']]
    plugin_ids = {route['plugin_id'] for route in matching}
    # A denied or uncertain business operation cannot become a UI retry in the
    # same goal. A new owner goal is independently assessed, not a permanent lock.
    for receipt in row.get('receipts', []):
        binding = receipt.get('extension_binding') or (receipt.get('preview') or {}).get('extension_binding') or {}
        if binding.get('plugin_id') not in plugin_ids or receipt.get('goal_revision') != context['goal_revision']:
            continue
        result = receipt.get('result') or {}
        envelope = result.get('structuredContent') or {}
        code = (envelope.get('error') or {}).get('code')
        state = (envelope.get('data') or {}).get('status') if isinstance(envelope.get('data'), dict) else None
        if code == 'PERMISSION_DENIED':
            raise AgentToolRejected('agent_plugin_ui_bypass_denied', correction='The plugin denied this goal. Desktop access cannot override business permissions.')
        if receipt.get('effect') == 'write' and (receipt.get('status') == 'unknown' or state in {'queued', 'submitting', 'accepted', 'unknown', 'partial', 'confirmed'}):
            raise AgentToolRejected('agent_plugin_ui_replay_denied', correction='Use the plugin result tool with the original operation ID. Do not repeat this business operation through the UI.')
    key = f"{context['goal_revision']}:{process}"
    notified = False
    def mark(data):
        nonlocal notified
        current = data['tasks'][context['task_id']]
        notices = current.setdefault('desktop_route_notices', [])
        if key not in notices:
            notices.append(key)
            del notices[:-32]
            notified = True
    store.transaction(mark)
    if notified:
        alternatives = [{'plugin_id': route['plugin_id'], 'tools': route['tools'], 'capabilities': route['capabilities']} for route in matching]
        conflict = len(plugin_ids) > 1
        raise AgentToolRejected('agent_plugin_route_conflict' if conflict else 'agent_plugin_route_preferred', correction=(
            f'Plugin routes for this observed process: {alternatives}. Discover these tools with ryn_discover_tools before a covered business write. '
            'This is a routing reminder, not proof of action semantics. If the owner explicitly requested UI interaction, '
            'or the operation is genuinely uncovered, explain that reason and retry the UI action under existing permissions. '
            'Tool denial, expired login or uncertain outcomes are not missing coverage. Do not bypass them.'))


def published_routes(state):
    result = []
    for plugin_id, defaults in state.get('release_defaults', {}).get('plugins', {}).items():
        plugin = state.get('plugins', {}).get(plugin_id)
        if not plugin or not plugin.get('enabled'):
            continue
        for route in (defaults.get('desktop_routes') or [])[:4]:
            names, tools, capabilities = route.get('process_names'), route.get('tools'), route.get('capabilities')
            if (not isinstance(names, list) or not 1 <= len(names) <= 4
                    or not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_. -]{1,80}\.exe', name) for name in names)
                    or not isinstance(tools, list) or not 1 <= len(tools) <= 8
                    or not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) for name in tools)
                    or not isinstance(capabilities, list) or not 1 <= len(capabilities) <= 8
                    or not all(isinstance(value, str) and len(value) <= 100 for value in capabilities)):
                continue
            result.append({'plugin_id': plugin_id, 'process_names': [name.casefold() for name in names],
                           'tools': list(tools), 'capabilities': list(capabilities)})
    return result
