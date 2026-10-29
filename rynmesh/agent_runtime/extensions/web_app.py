"""Declarative remote web application metadata for imported plugins."""
from urllib.parse import urlsplit

from ..store import AgentError

PERMISSIONS = {'models', 'messages', 'peer_provider', 'tools', 'agent'}

def validate_web_app(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'url', 'permissions'}:
        raise AgentError('agent_invalid_web_app')
    url = value.get('url')
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url) or '\\' in url:
        raise AgentError('agent_invalid_web_app')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise AgentError('agent_invalid_web_app') from None
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment or port == 0:
        raise AgentError('agent_invalid_web_app')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
        raise AgentError('agent_invalid_web_app')
    permissions = value.get('permissions', [])
    if not isinstance(permissions, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions):
        raise AgentError('agent_invalid_web_app')
    return {'url': url, 'permissions': sorted(set(permissions))}
