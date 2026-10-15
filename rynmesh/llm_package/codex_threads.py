"""Reusable environment-free Codex threads for client tool loops and windows.

Threads are grouped by (command, model, effort, requester, window). The
provider derives the requester from the authenticated caller (local process,
local API key or signed peer id), never from client fields. The window is a
client-supplied opaque key (a hash of the agent task id) and only labels a
conversation within that requester's scope.

Inside a group, a thread is resumed only when the request's client transcript
exactly extends what the thread received plus the response it returned,
including the random call ids. Only the new messages are sent. With a window
key the thread survives final answers, so the window's next owner message
continues it. Without one (older clients) it only spans a tool loop.

A rejected or failed turn, cancellation, timeout, process exit, compaction,
a model or effort switch in the window, a change in system prose (where client
permission policy lives), LRU eviction, idle expiry or shutdown closes the
thread; that request then sends full history again. The provider never executes
a tool, so no fallback can replay one: it only resends the client's own record.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path

SESSION_IDLE_SECONDS = 1800
# Each kept thread is one idle app-server process (see the acceptance README for
# how to measure its resident memory); RYN_CODEX_SESSION_LIMIT may adjust 1-16.
SESSION_LIMIT = 4
# POSIX workers enforce this CPU budget for the whole process lifetime. A kept
# thread is rebuilt with full history before a turn could cross it.
SESSION_CPU_SECONDS = 7200

_LABELLED_STATE = re.compile(r"([^\n{}\[\]\"]{1,80}: )([\[{].*)")
_KEY = re.compile(r"[0-9a-f]{32,64}")
_POOLS = weakref.WeakSet()


def enabled() -> bool:
    return os.environ.get("RYN_CODEX_SESSION_REUSE", "1") != "0"


def limit() -> int:
    try:
        return min(16, max(1, int(os.environ.get("RYN_CODEX_SESSION_LIMIT", SESSION_LIMIT))))
    except ValueError:
        return SESSION_LIMIT


def window_key(value) -> str | None:
    """Consumer side: an opaque window label that never reveals the task id."""
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(b"ryn-codex-window-v1\0" + value.encode()).hexdigest()[:32]


def scope(requester: str, window=None) -> dict:
    """Provider side: requester is the authenticated caller; window is untrusted."""
    return {"requester": requester, "window": window if isinstance(window, str) and _KEY.fullmatch(window) else None}


def process_cpu_seconds(pid) -> float | None:
    """User+system CPU of a live child, or None when it cannot be read."""
    try:
        if sys.platform.startswith("linux"):
            fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
        if sys.platform == "darwin":
            value = subprocess.run(["/bin/ps", "-o", "time=", "-p", str(int(pid))], stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            days, _, clock = value.rpartition("-")
            seconds = 0.0
            for part in clock.split(":"):
                seconds = seconds * 60 + float(part)
            return seconds + (int(days) * 86400 if days else 0)
    except (OSError, ValueError, IndexError, TypeError, subprocess.SubprocessError):
        return None
    return None


def _canonical(message: dict) -> dict:
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        try:
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        except ValueError:
            pass
        calls.append([call.get("id"), function.get("name"), arguments])
    return {"role": message.get("role"), "content": message.get("content") or "",
            "tool_calls": calls, "tool_call_id": message.get("tool_call_id"), "name": message.get("name")}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def split(messages: list) -> tuple[list, list]:
    """Leading control messages, then the conversation from the first user turn."""
    for index, message in enumerate(messages):
        if message.get("role") == "user":
            return messages[:index], messages[index:]
    return messages, []


def state_lines(old: str, new: str) -> list[str] | None:
    """Changed labelled JSON state lines, or None when instruction prose changed."""
    before, after = old.split("\n"), new.split("\n")
    if len(before) != len(after):
        return None
    changed = []
    for previous, current in zip(before, after):
        if previous == current:
            continue
        a, b = _LABELLED_STATE.fullmatch(previous), _LABELLED_STATE.fullmatch(current)
        if not a or not b or a.group(1) != b.group(1):
            return None
        try:
            json.loads(a.group(2))
            json.loads(b.group(2))
        except ValueError:
            return None
        changed.append(current)
    return changed


class Conversation:
    """One proposal request's view of a reusable thread."""

    def __init__(self, messages: list, functions: dict, render=None, scope=None):
        self.render = render  # continuation update -> provider turn text
        scope = scope or {}
        self.requester, self.window = scope.get("requester"), scope.get("window")
        self.header, self.body = split(messages)
        self.body_digests = [digest(_canonical(message)) for message in self.body]
        self.functions = {name: digest(spec) for name, spec in functions.items()}
        self.specs = functions
        self.session = None
        self.update = None
        self.streamed = False

    @property
    def root(self):
        return self.body_digests[0] if self.body_digests else None

    def finish(self, message: dict):
        session, self.session = self.session, None
        if session is None:
            return
        # Without a window key, a final answer ends the only conversation this
        # thread can recognise; the returned call ids were its capability.
        if not message.get("tool_calls") and self.window is None:
            session.close()
            return
        session.header = [dict(item) for item in self.header]
        session.body = [*self.body_digests, digest(_canonical(message))]
        session.functions = dict(self.functions)
        session.pool.put(session)

    def abandon(self):
        session, self.session = self.session, None
        if session is not None:
            session.close()


class Session:
    def __init__(self, pool, rpc, key):
        self.pool, self.rpc, self.key = pool, rpc, key
        self.thread_id = ""
        self.header, self.body, self.functions = [], [], {}
        self.turns = 0
        self.busy_seconds = 0.0
        self.idle_since = time.monotonic()
        self.closed = False

    def matches(self, conversation: Conversation) -> bool:
        size = len(self.body)
        return 0 < size < len(conversation.body_digests) and conversation.body_digests[:size] == self.body

    def continuation(self, conversation: Conversation) -> dict | None:
        """New client messages plus replaced control state, or None to reseed."""
        update = {}
        if digest(self.header) != digest(conversation.header):
            control = {"system", "developer"}
            before = [item for item in self.header if item.get("role") in control]
            after = [item for item in conversation.header if item.get("role") in control]
            if [item.get("role") for item in before] != [item.get("role") for item in after]:
                return None
            lines = []
            for previous, current in zip(before, after):
                if previous.get("content") == current.get("content"):
                    continue
                if not isinstance(previous.get("content"), str) or not isinstance(current.get("content"), str):
                    return None
                changed = state_lines(previous["content"], current["content"])
                if changed is None:
                    return None
                lines.extend(changed)
            if lines:
                update["system_state_updates"] = lines
            context_before = [item for item in self.header if item.get("role") not in control]
            context_after = [item for item in conversation.header if item.get("role") not in control]
            if digest(context_before) != digest(context_after):
                update["context_messages"] = context_after
        replaced = [conversation.specs[name] for name, value in conversation.functions.items()
                    if self.functions.get(name) != value]
        removed = [name for name in self.functions if name not in conversation.functions]
        if replaced or removed:
            update["functions_update"] = {"added_or_replaced": replaced, "removed": removed}
        update["messages"] = conversation.body[len(self.body):]
        return update

    def usable(self, reserve: float) -> bool:
        """Open, and the next turn cannot cross the process CPU budget."""
        if self.closed:
            return False
        # Measured CPU when readable, else wall time spent in turns (an upper
        # bound for this I/O-bound server); None where no lifetime limit applies.
        used = self.rpc.cpu_seconds(self.busy_seconds)
        return used is None or used + reserve < SESSION_CPU_SECONDS

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.rpc.close()
        except Exception:
            pass


class Pool:
    def __init__(self):
        self.condition = threading.Condition()
        self.idle: list[Session] = []
        self.reaper = None
        self.stats = {"started": 0, "resumed": 0, "fallback": 0, "rebuilt": 0}
        _POOLS.add(self)

    def session(self, rpc, key) -> Session:
        return Session(self, rpc, key)

    def lease(self, key, conversation: Conversation, reserve: float = 0) -> Session | None:
        """Exclusive use of the thread this request continues, if it is still sound."""
        window = key[-2:] if key[-1] is not None else None
        with self.condition:
            same = [item for item in self.idle if item.key == key]
            match = max((item for item in same if item.matches(conversation)),
                        key=lambda item: len(item.body), default=None)
            # Supersession is judged only inside one requester's group: a
            # request that shares a root with an idle thread but does not
            # extend it (retry, rejection, compaction) replaces that thread,
            # and so does a model or effort switch in the same window.
            stale = [item for item in same if item is not match and conversation.root
                     and item.body[:1] == [conversation.root]]
            if window is not None:
                stale += [item for item in self.idle if item.key != key and item.key[-2:] == window]
            for item in [match, *stale]:
                if item is not None:
                    self.idle.remove(item)
        for item in stale:
            item.close()
        if match is None:
            return None
        if not (match.rpc.alive() and match.rpc.settle()):
            match.close()
            return None
        if not match.usable(reserve):
            self.stats["rebuilt"] += 1
            match.close()
            return None
        update = match.continuation(conversation)
        if update is None:
            match.close()
            return None
        conversation.update = update
        return match

    def put(self, session: Session):
        if session.closed:
            return
        evicted = []
        with self.condition:
            session.idle_since = time.monotonic()
            self.idle.append(session)
            while len(self.idle) > limit():
                evicted.append(self.idle.pop(0))
            if self.reaper is None:
                self.reaper = threading.Thread(target=self._reap, name="ryn-codex-thread-reaper", daemon=True)
                self.reaper.start()
            self.condition.notify_all()
        for item in evicted:
            item.close()

    def _reap(self):
        while True:
            with self.condition:
                if not self.idle:
                    self.reaper = None
                    return
                now = time.monotonic()
                expired = [item for item in self.idle if now - item.idle_since >= SESSION_IDLE_SECONDS]
                for item in expired:
                    self.idle.remove(item)
                if not expired:
                    wait = min(SESSION_IDLE_SECONDS - (now - item.idle_since) for item in self.idle)
                    self.condition.wait(max(0.05, min(wait, 30)))
                    continue
            for item in expired:
                item.close()

    def close_all(self):
        with self.condition:
            idle, self.idle = self.idle, []
            self.condition.notify_all()
        for item in idle:
            item.close()

    def __len__(self):
        with self.condition:
            return len(self.idle)


def pool_for(adapter) -> Pool:
    with adapter._lock:
        pool = getattr(adapter, "_codex_threads", None)
        if pool is None:
            pool = adapter._codex_threads = Pool()
        return pool


def close_pool(adapter):
    pool = getattr(adapter, "_codex_threads", None)
    if pool is not None:
        pool.close_all()


@atexit.register
def _close_all_pools():
    for pool in list(_POOLS):
        pool.close_all()
