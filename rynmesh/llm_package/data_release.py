"""Request-local destination checks for restricted model payloads."""
from __future__ import annotations

import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar

_restricted = ContextVar("ryn_model_data_release", default=None)


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
    if scope is None:
        return urllib.request.urlopen(request, **kwargs)
    if scope[0] is not None:
        try:
            scope[0]()
        except Exception as exc:
            code = str(exc)
            if code not in {"agent_model_destination_changed", "agent_extension_model_not_allowed"}:
                code = "agent_model_destination_changed"
            raise DataReleaseError(code) from None
    # A fresh opener avoids the process-wide opener and refuses every redirect.
    return urllib.request.build_opener(_NoRedirect()).open(request, **kwargs)
