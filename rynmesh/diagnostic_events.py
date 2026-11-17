"""Bounded node diagnostics without exception messages or absolute paths."""
from collections import deque
import logging
from pathlib import Path
from threading import Lock
from time import monotonic
import traceback


_lock = Lock()
_events = deque(maxlen=50)
_last_emitted = {}
_logger = logging.getLogger(__name__)


def record(component: str, code: str, exc: BaseException | None = None) -> None:
    """Record each controlled component/code at most once every five minutes."""
    now = monotonic()
    with _lock:
        previous = _last_emitted.get((component, code))
        if previous is not None and now - previous < 300:
            return
        frame = (traceback.extract_tb(exc.__traceback__)[-1] if exc is not None and exc.__traceback__
                 else traceback.extract_stack(limit=2)[0])
        event = {"component": component, "code": code,
                 "exception_type": type(exc).__name__ if exc is not None else None,
                 "file": Path(frame.filename).name, "line": frame.lineno}
        _last_emitted[(component, code)] = now
        _events.append(event)
    _logger.warning("component=%s code=%s exception_type=%s file=%s line=%s",
                    event["component"], event["code"], event["exception_type"], event["file"], event["line"])


def recent() -> list[dict]:
    """Return an independent snapshot of the latest fifty diagnostic events."""
    with _lock:
        return [dict(event) for event in _events]
