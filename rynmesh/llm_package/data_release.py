"""Request-local destination checks for restricted model payloads."""
from __future__ import annotations

import re
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar

_restricted = ContextVar("ryn_model_data_release", default=None)
_STDLIB_URLOPEN = urllib.request.urlopen
_KNOWN_REJECTION_CODES = frozenset({"agent_model_destination_changed", "agent_extension_model_not_allowed"})
_AGENT_CODE_RE = re.compile(r"agent_[a-z0-9_]{1,100}\Z")


class DataReleaseError(RuntimeError):
    """Fixed host rejection, distinct from an upstream JSON ValueError."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # Let urllib raise HTTPError; adapters expose only their fixed error code.
        return None


@contextmanager
def model_request_scope(*, restricted=False, before_dispatch=None):
    """Constrain only this inference, including its streaming HTTP requests."""
    token = _restricted.set((before_dispatch,) if restricted else None)
    try:
        yield
    finally:
        _restricted.reset(token)


def urlopen(request, **kwargs):
    scope = _restricted.get()
    if scope is not None and scope[0] is not None:
        try:
            scope[0]()
        except Exception as exc:
            raw = str(exc)
            if not isinstance(exc, OSError) and (raw in _KNOWN_REJECTION_CODES or _AGENT_CODE_RE.fullmatch(raw)):
                code = raw
            else:
                from ..agent_runtime import diagnostic_log
                diagnostic_log.failure("data_release_check", exc)
                code = "agent_data_release_check_failed"
            raise DataReleaseError(code) from None
    if scope is None and urllib.request.urlopen is not _STDLIB_URLOPEN:
        return urllib.request.urlopen(request, **kwargs)
    # A fresh opener avoids the process-wide opener and refuses every redirect.
    context = kwargs.pop("context", None)
    handlers = [_NoRedirect()]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers).open(request, **kwargs)
