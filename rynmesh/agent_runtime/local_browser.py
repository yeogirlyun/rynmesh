"""A separate task-owned Chromium profile, controlled through loopback CDP."""
from __future__ import annotations

import base64
import json
import os
import shutil
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .store import AgentError, AgentToolRejected


class AgentBrowserOperationUnconfirmed(AgentError):
    """Raised when a mutating CDP command was sent over WebSocket but its response was lost."""


NAVIGATE_WAIT_SECONDS = 3.0
_SSL_CONTEXT_LOCK = threading.Lock()
_LOOPBACK_SSL_CONTEXT: ssl.SSLContext | None = None


def _loopback_ssl_context() -> ssl.SSLContext:
    """Return a verified SSLContext so httpx does not reload certifi CA bundles per client."""
    global _LOOPBACK_SSL_CONTEXT
    ctx = _LOOPBACK_SSL_CONTEXT
    if ctx is not None:
        return ctx
    with _SSL_CONTEXT_LOCK:
        if _LOOPBACK_SSL_CONTEXT is None:
            import certifi

            created = ssl.create_default_context(cafile=certifi.where())
            created.verify_mode = ssl.CERT_REQUIRED
            created.check_hostname = True
            _LOOPBACK_SSL_CONTEXT = created
        return _LOOPBACK_SSL_CONTEXT


def _create_loopback_client(*, timeout: float = 5.0) -> httpx.Client:
    return httpx.Client(
        trust_env=False,
        follow_redirects=False,
        verify=_loopback_ssl_context(),
        timeout=max(0.1, float(timeout)),
        headers={"Connection": "close"},
    )



KEY_DEFINITIONS = {
    "Enter": {"key": "Enter", "code": "Enter", "vk": 13, "text": "\r"},
    "Tab": {"key": "Tab", "code": "Tab", "vk": 9},
    "Escape": {"key": "Escape", "code": "Escape", "vk": 27},
    "Backspace": {"key": "Backspace", "code": "Backspace", "vk": 8},
    "Delete": {"key": "Delete", "code": "Delete", "vk": 46},
    "Space": {"key": " ", "code": "Space", "vk": 32, "text": " "},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "vk": 40},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "vk": 38},
    "ArrowLeft": {"key": "ArrowLeft", "code": "ArrowLeft", "vk": 37},
    "ArrowRight": {"key": "ArrowRight", "code": "ArrowRight", "vk": 39},
    "Home": {"key": "Home", "code": "Home", "vk": 36},
    "End": {"key": "End", "code": "End", "vk": 35},
    "PageUp": {"key": "PageUp", "code": "PageUp", "vk": 33},
    "PageDown": {"key": "PageDown", "code": "PageDown", "vk": 34},
}


def find_browser():
    for name in ("chrome", "google-chrome", "chromium", "chromium-browser", "msedge"):
        if executable := shutil.which(name):
            return executable
    if os.name == "nt":
        for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
            for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")):
                if not base:
                    continue
                path = Path(base) / relative
                if path.is_file():
                    return str(path)
    return None


SNAPSHOT = """(options => {
 let doc = document;
 if (options.frame_selector) {
   const frames = document.querySelectorAll(options.frame_selector);
   if (frames.length !== 1) throw Error('FRAME_UNAVAILABLE:frame unavailable');
   try { doc = frames[0].contentDocument; } catch (_) { doc = null; }
   if (!doc) throw Error('FRAME_UNAVAILABLE:frame unavailable');
 }
 const win = doc.defaultView || window;
 if (!win.__rynDocId) {
   win.__rynDocId = Math.random().toString(36).slice(2, 8);
   win.__rynSeq = 0;
   win.__rynNodes = new Map();
   win.__rynElemIds = new WeakMap();
 }
 const scope = options.scope_selector ? doc.querySelector(options.scope_selector) : doc;
 if (!scope) throw Error('SCOPE_UNAVAILABLE:scope unavailable');
 const selectorList = [
   'a', 'button', 'input', 'textarea', 'select', 'iframe', 'summary',
   '[role="button"]', '[role="link"]', '[role="menuitem"]', '[role="tab"]',
   '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="combobox"]',
   '[role="option"]', '[contenteditable="true"]', '[contenteditable=""]',
   '[onclick]', '[tabindex]:not([tabindex="-1"])'
 ].join(',');
 const els = [...scope.querySelectorAll(selectorList)];
 const visible = els.filter(e => {
   if (e.hidden) return false;
   const style = win.getComputedStyle(e);
   if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
   const rects = e.getClientRects();
   return rects.length > 0 && [...rects].some(r => r.width > 0 && r.height > 0);
 });
 visible.forEach(e => {
   let id = win.__rynElemIds.get(e);
   if (!id) {
     win.__rynSeq += 1;
     id = win.__rynDocId + '-' + String(win.__rynSeq);
     win.__rynElemIds.set(e, id);
     win.__rynNodes.set(id, e);
   }
   if (e.getAttribute('data-ryn-element') !== id) {
     e.setAttribute('data-ryn-element', id);
   }
   for (const dup of doc.querySelectorAll('[data-ryn-element="' + id + '"]')) {
     if (dup !== e) dup.removeAttribute('data-ryn-element');
   }
 });
 const getLabel = e => String(e.getAttribute('aria-label') || e.innerText || e.placeholder || e.name || e.title || '').slice(0, 160);
 const filtered = options.query
   ? visible.filter(e => getLabel(e).toLowerCase().includes(options.query.toLowerCase()))
   : visible;
 const offset = options.element_offset || 0, textOffset = options.text_offset || 0;
 const text = (scope === doc ? doc.body : scope)?.innerText || '';
 if (offset > filtered.length || textOffset > text.length) throw Error('OFFSET_INVALID:offset unavailable');
 const items = filtered.slice(offset, offset + 40).map(e => {
   const tag = e.tagName.toLowerCase();
   const disabled = Boolean(e.disabled || (e.matches && e.matches(':disabled')) || e.getAttribute('aria-disabled') === 'true');
   let checked = null;
   if (tag === 'input' && (e.type === 'checkbox' || e.type === 'radio')) checked = Boolean(e.checked);
   else if (e.getAttribute('aria-checked') === 'true') checked = true;
   else if (e.getAttribute('aria-checked') === 'false') checked = false;
   else if (e.getAttribute('aria-checked') === 'mixed') checked = 'mixed';
   else if (tag === 'option') checked = Boolean(e.selected);
   let expanded = null;
   if (e.getAttribute('aria-expanded') === 'true') expanded = true;
   else if (e.getAttribute('aria-expanded') === 'false') expanded = false;
   else if (tag === 'summary' && e.parentElement && e.parentElement.tagName === 'DETAILS') expanded = Boolean(e.parentElement.open);
   else if (tag === 'details') expanded = Boolean(e.open);
   const href = (tag === 'a' && e.getAttribute('href') !== null) ? String(e.href || e.getAttribute('href')).slice(0, 500) : null;
   const rawVal = e.type === 'password'
     ? '[password]'
     : (e.value !== undefined && e.value !== null && tag !== 'button' && tag !== 'a'
        ? String(e.value)
        : (e.isContentEditable ? String(e.innerText || '') : ''));
   return {
     selector: '[data-ryn-element="' + e.getAttribute('data-ryn-element') + '"]',
     tag,
     type: String(e.type || e.getAttribute('role') || ''),
     name: getLabel(e),
     value: rawVal.slice(0, 160),
     href,
     disabled,
     checked,
     expanded
   };
 });
 if (!window.__rynErrors) {
   window.__rynErrors = [];
   addEventListener('error', e => window.__rynErrors.push(String(e.message).slice(0, 200)));
 }
 return {
   url: location.href,
   title: document.title,
   ready: document.readyState,
   doc_id: win.__rynDocId,
   text: text.slice(textOffset, textOffset + 4000),
   elements: items,
   errors: window.__rynErrors.slice(-8),
   element_count: filtered.length,
   text_length: text.length,
   element_offset: offset,
   text_offset: textOffset,
   next_element_offset: offset + 40 < filtered.length ? offset + 40 : null,
   next_text_offset: textOffset + 4000 < text.length ? textOffset + 4000 : null,
   incomplete: offset + 40 < filtered.length || textOffset + 4000 < text.length,
   frame_selector: options.frame_selector || null
 };
})"""


RESOLVE_HELPER_JS = """
function resolveTarget(selector, frameSelector) {
  let doc = document;
  let frameEl = null;
  if (frameSelector) {
    const frames = document.querySelectorAll(frameSelector);
    if (frames.length !== 1) throw Error('FRAME_UNAVAILABLE:frame_selector must match a unique iframe');
    frameEl = frames[0];
    try { doc = frameEl.contentDocument; } catch (_) { doc = null; }
    if (!doc) throw Error('FRAME_UNAVAILABLE:iframe contentDocument unavailable or cross-origin');
  }
  const win = doc.defaultView || window;
  const rynMatch = /^\\[data-ryn-element=["']([^"']+)["']\\]$/.exec((selector || '').trim());
  if (rynMatch) {
    const rynId = rynMatch[1];
    if (!win.__rynDocId || !rynId.startsWith(win.__rynDocId + '-')) {
      throw Error('STALE_ELEMENT:Document changed since snapshot; read the page again');
    }
    const registered = win.__rynNodes ? win.__rynNodes.get(rynId) : null;
    if (!registered || !registered.isConnected || registered.ownerDocument !== doc) {
      throw Error('STALE_ELEMENT:Element was removed or replaced in DOM; read the page again');
    }
    for (const dup of doc.querySelectorAll(selector)) {
      if (dup !== registered) dup.removeAttribute('data-ryn-element');
    }
    if (registered.getAttribute('data-ryn-element') !== rynId) {
      registered.setAttribute('data-ryn-element', rynId);
    }
    return { doc, win, frameEl, element: registered };
  }
  let es;
  try {
    es = doc.querySelectorAll(selector);
  } catch (_) {
    throw Error('INVALID_SELECTOR:Invalid CSS selector');
  }
  if (es.length !== 1) {
    throw Error('NOT_UNIQUE:Selector matched ' + es.length + ' elements');
  }
  return { doc, win, frameEl, element: es[0] };
}
"""


PREPARE_POINTER_JS = """(async options => {
""" + RESOLVE_HELPER_JS + """
  const { doc, win, frameEl, element: e } = resolveTarget(options.selector, options.frame_selector);
  if (!win.__rynPointerIds) win.__rynPointerIds = new WeakMap();
  if (typeof win.__rynPointerSeq !== 'number') win.__rynPointerSeq = 1;
  let elemId = win.__rynPointerIds.get(e);
  if (!elemId) {
    elemId = 'p' + (win.__rynPointerSeq++);
    win.__rynPointerIds.set(e, elemId);
  }
  if (options.expected_ryn_id && elemId !== options.expected_ryn_id) {
    throw Error('STALE_ELEMENT:Target element changed after pointer move');
  }
  if (options.check_disabled) {
    const isDisabled = Boolean(
      e.disabled ||
      (e.matches && e.matches(':disabled')) ||
      e.getAttribute('aria-disabled') === 'true' ||
      (e.closest && e.closest('[disabled], [aria-disabled="true"]'))
    );
    if (isDisabled) throw Error('DISABLED:Target element is disabled');
  }
  if (!options.verify_only) {
    if (frameEl) {
      frameEl.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    }
    e.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
  }
  const checkVisibleAndRect = (el, w) => {
    if (el.hidden) throw Error('NOT_VISIBLE:Element is hidden');
    const style = w.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse' || parseFloat(style.opacity) === 0) {
      throw Error('NOT_VISIBLE:Element is not visible');
    }
    if (style.pointerEvents === 'none') {
      throw Error('NOT_VISIBLE:Element has pointer-events: none');
    }
    const r = el.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) {
      throw Error('NOT_VISIBLE:Element has zero area');
    }
    return r;
  };
  let r2 = checkVisibleAndRect(e, win);
  if (!options.verify_only) {
    const r1 = r2;
    await new Promise(resolve => setTimeout(resolve, 20));
    r2 = checkVisibleAndRect(e, win);
    if (Math.abs(r2.left - r1.left) > 2 || Math.abs(r2.top - r1.top) > 2 || Math.abs(r2.width - r1.width) > 2 || Math.abs(r2.height - r1.height) > 2) {
      await new Promise(resolve => setTimeout(resolve, 40));
      const r3 = checkVisibleAndRect(e, win);
      if (Math.abs(r3.left - r2.left) > 2 || Math.abs(r3.top - r2.top) > 2 || Math.abs(r3.width - r2.width) > 2 || Math.abs(r3.height - r2.height) > 2) {
        throw Error('UNSTABLE:Element position is still moving');
      }
      r2 = r3;
    }
  }
  const vw = win.innerWidth || doc.documentElement.clientWidth || 800;
  const vh = win.innerHeight || doc.documentElement.clientHeight || 600;
  const left = Math.max(0, r2.left), top = Math.max(0, r2.top);
  const right = Math.min(vw, r2.right), bottom = Math.min(vh, r2.bottom);
  if (right <= left || bottom <= top) {
    throw Error('NOT_VISIBLE:Element is outside the viewport');
  }
  const cx = (left + right) / 2;
  const cy = (top + bottom) / 2;
  const hit = doc.elementFromPoint(cx, cy);
  const validHit = Boolean(
    hit && (
      hit === e ||
      e.contains(hit) ||
      (hit.tagName === 'LABEL' && hit.control === e)
    )
  );
  if (!validHit) {
    const blocker = hit ? (hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '')) : 'none';
    throw Error('OCCLUDED:Element click point is occluded by ' + blocker);
  }
  let mainX = cx, mainY = cy;
  if (frameEl) {
    let cur = frameEl;
    while (cur && cur.nodeType === 1) {
      const cs = window.getComputedStyle(cur);
      if (
        (cs.transform && cs.transform !== 'none') ||
        (cs.scale && cs.scale !== 'none' && cs.scale !== '1') ||
        (cs.rotate && cs.rotate !== 'none' && cs.rotate !== '0deg') ||
        (cs.translate && cs.translate !== 'none' && cs.translate !== '0px') ||
        (cs.zoom && cs.zoom !== 'normal' && cs.zoom !== '1' && cs.zoom !== '100%')
      ) {
        throw Error('FRAME_UNSUPPORTED:CSS-transformed or scaled iframe ancestor coordinate conversion is unsupported');
      }
      cur = cur.parentElement;
    }
    const fStyle = window.getComputedStyle(frameEl);
    const fRect = checkVisibleAndRect(frameEl, window);
    if (
      (frameEl.offsetWidth > 0 && Math.abs(fRect.width - frameEl.offsetWidth) > 2) ||
      (frameEl.offsetHeight > 0 && Math.abs(fRect.height - frameEl.offsetHeight) > 2)
    ) {
      throw Error('FRAME_UNSUPPORTED:Scaled iframe coordinate conversion is unsupported');
    }
    const borderLeft = parseFloat(fStyle.borderLeftWidth) || 0;
    const borderTop = parseFloat(fStyle.borderTopWidth) || 0;
    const padLeft = parseFloat(fStyle.paddingLeft) || 0;
    const padTop = parseFloat(fStyle.paddingTop) || 0;
    mainX = fRect.left + borderLeft + padLeft + cx;
    mainY = fRect.top + borderTop + padTop + cy;
    const mvw = window.innerWidth || document.documentElement.clientWidth || 800;
    const mvh = window.innerHeight || document.documentElement.clientHeight || 600;
    if (mainX < 0 || mainX >= mvw || mainY < 0 || mainY >= mvh) {
      throw Error('NOT_VISIBLE:Translated iframe coordinates are outside main viewport');
    }
    const mainHit = document.elementFromPoint(mainX, mainY);
    if (mainHit !== frameEl && !frameEl.contains(mainHit)) {
      const blocker = mainHit ? (mainHit.tagName.toLowerCase() + (mainHit.id ? '#' + mainHit.id : '')) : 'none';
      throw Error('OCCLUDED:Iframe is occluded at target point by ' + blocker);
    }
  }
  const rx = Math.round(mainX * 100) / 100;
  const ry = Math.round(mainY * 100) / 100;
  if (options.verify_only && typeof options.expected_x === 'number' && typeof options.expected_y === 'number') {
    if (Math.abs(rx - options.expected_x) > 2 || Math.abs(ry - options.expected_y) > 2) {
      throw Error('UNSTABLE:Element position shifted after pointer move');
    }
  }
  return { x: rx, y: ry, ryn_id: elemId };
})"""


class Browser:
    def __init__(self, home, processes, *, headless=False):
        self.home, self.processes = Path(home), processes
        self.sessions = {}
        self.pages = {}
        self.headless = headless
        self.connections = {}
        self.sequence = 0
        self.tab_sequence = 0
        self.lock = threading.RLock()

    @staticmethod
    def validate_url(url, resolve):
        parsed = urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return url
        if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
            value = unquote(parsed.path)
            if os.name == "nt" and value.startswith("/"):
                value = value[1:]
            path = resolve(value)
            if path.is_file():
                return path.as_uri()
        raise AgentToolRejected("agent_browser_url_invalid")

    def session(self, task_id, resolve, allowed, cancelled):
        current = self.sessions.get(task_id)
        if current and self.processes.get(current["process_id"], task_id)["process"].poll() is None:
            return current
        executable = find_browser()
        if not executable:
            raise AgentToolRejected("agent_browser_not_installed")
        profile = self.home / "agent" / "browser" / uuid.uuid4().hex
        profile.mkdir(parents=True)
        command = [executable, "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                   "--user-data-dir=" + str(profile), "--no-first-run", "--no-default-browser-check", "about:blank"]
        if self.headless:
            command.append("--headless=new")
        ident = self.processes.start(command, resolve("."), task_id, 600, allowed, cancelled)
        deadline = time.monotonic() + 12
        port_file = profile / "DevToolsActivePort"
        while time.monotonic() < deadline:
            if cancelled():
                self.processes.stop(ident, task_id)
                raise AgentToolRejected("agent_cancelled")
            if not allowed():
                self.processes.stop(ident, task_id)
                raise AgentToolRejected("agent_desktop_access_required")
            if port_file.exists():
                try:
                    lines = port_file.read_text().splitlines()
                except OSError:
                    lines = []
                if lines and lines[0].isdigit():
                    current = {"port": int(lines[0]), "process_id": ident, "initial_blank_ids": set()}
                    try:
                        initial_rows = self.request(current, "/json/list", timeout=2.0)
                        current["initial_blank_ids"] = {
                            r["id"] for r in initial_rows
                            if isinstance(r, dict) and r.get("type") == "page" and r.get("id") and r.get("url") in ("", "about:blank")
                        }
                    except Exception:
                        pass
                    self.sessions[task_id] = current
                    return current
            if self.processes.get(ident, task_id)["process"].poll() is not None:
                break
            time.sleep(0.1)
        self.processes.stop(ident, task_id)
        raise AgentToolRejected("agent_browser_start_failed")

    @staticmethod
    def request(session, route, method="GET", *, timeout=5.0):
        if (
            not isinstance(session, dict)
            or type(session.get("port")) is not int
            or not (1 <= session["port"] <= 65535)
            or not isinstance(route, str)
            or not route.startswith("/")
            or route.startswith("//")
            or any(ch in route for ch in ("\r", "\n", "\x00"))
        ):
            raise AgentToolRejected("agent_browser_connection_failed")
        port = session["port"]
        method_upper = str(method or "GET").upper()
        budget = max(0.1, float(timeout))
        deadline = time.monotonic() + budget
        attempts = 2 if method_upper == "GET" else 1
        url = f"http://127.0.0.1:{port}{route}"
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            attempt_timeout = budget if attempt == 0 else max(0.05, remaining)
            try:
                with _create_loopback_client(timeout=attempt_timeout) as client:
                    response = client.request(method_upper, url, timeout=attempt_timeout)
                    response.raise_for_status()
                    return response.json()
            except Exception:
                if attempt + 1 < attempts:
                    rem_after = deadline - time.monotonic()
                    if rem_after > 0.05:
                        time.sleep(min(0.03, rem_after - 0.05))
                        continue
                raise AgentToolRejected("agent_browser_connection_failed") from None

    def _close_connection(self, ident):
        with self.lock:
            conn = self.connections.pop(ident, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def sync_tabs(self, task_id, *, timeout=5.0):
        session = self.sessions.get(task_id)
        if not session:
            return []
        if self.processes.get(session["process_id"], task_id)["process"].poll() is not None:
            raise AgentToolRejected("agent_browser_session_ended")
        rows = self.request(session, "/json/list", timeout=timeout)
        page_rows = [r for r in rows if isinstance(r, dict) and r.get("type") == "page" and r.get("id")]
        live_by_id = {r["id"]: r for r in page_rows}
        initial_blanks = session.get("initial_blank_ids") or set()
        with self.lock:
            for pid, meta in list(self.pages.items()):
                if meta.get("task_id") == task_id and pid not in live_by_id:
                    self.pages.pop(pid, None)
                    self._close_connection(pid)
            for r in page_rows:
                pid = r["id"]
                raw_url = str(r.get("url") or "")
                if pid in initial_blanks and pid not in self.pages and raw_url in ("", "about:blank"):
                    continue
                if pid not in self.pages and raw_url.startswith(("edge://", "chrome://", "devtools://", "chrome-extension://", "extension://")):
                    continue
                existing = self.pages.get(pid)
                if existing is None:
                    self.tab_sequence += 1
                    self.pages[pid] = {"task_id": task_id, "seq": self.tab_sequence, "ready_checked": False, "target": r}
                elif existing.get("task_id") == task_id:
                    existing["target"] = r
            task_pids = [
                pid for pid, meta in self.pages.items()
                if meta.get("task_id") == task_id and pid in live_by_id
            ]
            task_pids.sort(key=lambda pid: self.pages[pid].get("seq", 0))
        return [
            {
                "page_id": pid,
                "title": str(live_by_id[pid].get("title") or "")[:300],
                "url": str(live_by_id[pid].get("url") or "")[:2048],
            }
            for pid in task_pids
        ]

    def page(self, ident, task_id, *, timeout=5.0, refresh=False):
        existing = self.pages.get(ident)
        if existing is not None and existing.get("task_id") != task_id:
            raise AgentToolRejected("agent_browser_page_not_found")
        session = self.sessions.get(task_id)
        if not session:
            raise AgentToolRejected("agent_browser_page_not_found")
        if self.processes.get(session["process_id"], task_id)["process"].poll() is not None:
            raise AgentToolRejected("agent_browser_session_ended")
        if refresh or existing is None or not existing.get("target"):
            self.sync_tabs(task_id, timeout=timeout)
        record = self.pages.get(ident)
        if not record or record.get("task_id") != task_id or not record.get("target"):
            raise AgentToolRejected("agent_browser_page_not_found")
        return record["target"]

    def _connect_ws(self, ident, task_id, *, timeout=5.0):
        from websockets.sync.client import connect
        target = self.page(ident, task_id, timeout=timeout, refresh=False)
        if not target.get("webSocketDebuggerUrl"):
            target = self.page(ident, task_id, timeout=timeout, refresh=True)
        uri = target["webSocketDebuggerUrl"]
        parsed = urlparse(uri)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != self.sessions[task_id]["port"]:
            raise AgentToolRejected("agent_browser_debug_endpoint_invalid")
        connection = connect(uri, open_timeout=max(0.2, min(5.0, float(timeout))), close_timeout=1, max_size=8 * 1024 * 1024, proxy=None)
        self.connections[ident] = connection
        return connection

    def cdp(self, ident, task_id, method, params=None, *, timeout=8.0, mutating=False, allow_response_timeout=False, guard=None):
        budget_seconds = max(0.1, float(timeout))
        with self.lock:
            for attempt in range(2):
                connection = self.connections.get(ident)
                had_cached = connection is not None
                if connection is None:
                    try:
                        connection = self._connect_ws(ident, task_id, timeout=budget_seconds)
                    except AgentToolRejected:
                        raise
                    except Exception:
                        raise AgentToolRejected("agent_browser_connection_failed") from None
                self.sequence += 1
                sequence = self.sequence
                payload = json.dumps({"id": sequence, "method": method, "params": params or {}})
                try:
                    connection.send(payload)
                except Exception as exc:
                    self._close_connection(ident)
                    if mutating:
                        raise AgentBrowserOperationUnconfirmed("agent_browser_action_unconfirmed") from exc
                    if had_cached and attempt == 0:
                        continue
                    raise AgentToolRejected("agent_browser_connection_failed") from None
                deadline = time.monotonic() + budget_seconds
                try:
                    while True:
                        if guard is not None:
                            guard()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            recv_timeout = min(0.1, max(0.02, remaining)) if guard is not None else max(0.05, remaining)
                            raw = connection.recv(timeout=recv_timeout)
                        except TimeoutError:
                            if time.monotonic() < deadline:
                                continue
                            raise
                        response = json.loads(raw)
                        if response.get("id") == sequence:
                            if response.get("error"):
                                raise AgentToolRejected("agent_browser_protocol_error")
                            return response.get("result", {})
                except AgentToolRejected:
                    raise
                except TimeoutError:
                    if allow_response_timeout:
                        return {"_timed_out": True}
                    if mutating:
                        self._close_connection(ident)
                        raise AgentBrowserOperationUnconfirmed("agent_browser_action_unconfirmed") from None
                    raise AgentToolRejected("agent_browser_timeout") from None
                except Exception as exc:
                    self._close_connection(ident)
                    if mutating:
                        raise AgentBrowserOperationUnconfirmed("agent_browser_action_unconfirmed") from exc
                    if attempt == 0:
                        continue
                    raise AgentToolRejected("agent_browser_connection_failed") from None
                if allow_response_timeout:
                    return {"_timed_out": True}
                if mutating:
                    self._close_connection(ident)
                    raise AgentBrowserOperationUnconfirmed("agent_browser_action_unconfirmed")
                raise AgentToolRejected("agent_browser_timeout")
        raise AgentToolRejected("agent_browser_connection_failed")

    @staticmethod
    def _raise_for_script_exception(details, selector=None):
        text = json.dumps(details, ensure_ascii=False)
        if "STALE_ELEMENT:" in text:
            raise AgentToolRejected(
                "agent_browser_element_stale_read_again",
                correction="Target element or document changed since the last read. Call ryn_browser_read to obtain fresh selectors.",
                details={"selector": selector} if selector else None,
            )
        if "DISABLED:" in text:
            raise AgentToolRejected(
                "agent_browser_element_disabled",
                correction="Target element is disabled and cannot be operated.",
                details={"selector": selector} if selector else None,
            )
        if "READONLY:" in text:
            raise AgentToolRejected(
                "agent_browser_element_readonly",
                correction="Target element is read-only and cannot be modified.",
                details={"selector": selector} if selector else None,
            )
        if "NOT_EDITABLE:" in text:
            raise AgentToolRejected(
                "agent_browser_element_not_editable",
                correction="Target element is not an editable input, textarea or contenteditable element.",
                details={"selector": selector} if selector else None,
            )
        if "FOCUS_FAILED:" in text:
            raise AgentToolRejected(
                "agent_browser_element_not_focusable",
                correction="Target element could not receive keyboard focus. Use click for non-focusable controls or select a focusable element.",
                details={"selector": selector} if selector else None,
            )
        if "OCCLUDED:" in text:
            raise AgentToolRejected(
                "agent_browser_element_occluded",
                correction="Target element click point is covered by another element. Scroll, dismiss overlays, or read the page again.",
                details={"selector": selector} if selector else None,
            )
        if "NOT_VISIBLE:" in text:
            raise AgentToolRejected(
                "agent_browser_element_not_visible",
                correction="Target element is hidden, has zero area, or is outside the viewport.",
                details={"selector": selector} if selector else None,
            )
        if "UNSTABLE:" in text:
            raise AgentToolRejected(
                "agent_browser_element_not_stable",
                correction="Target element position is still animating or moving; wait briefly and retry.",
                details={"selector": selector} if selector else None,
            )
        if "FRAME_UNSUPPORTED:" in text:
            raise AgentToolRejected(
                "agent_browser_frame_action_unsupported",
                correction="Target iframe has a CSS transform or layout that prevents safe coordinate translation.",
            )
        if "FRAME_UNAVAILABLE:" in text:
            raise AgentToolRejected(
                "agent_browser_frame_unavailable",
                correction="The specified frame_selector did not match an accessible same-origin iframe.",
            )
        if "NOT_UNIQUE:" in text or "INVALID_SELECTOR:" in text:
            raise AgentToolRejected(
                "agent_browser_element_changed_or_script_failed",
                correction="Selector did not match a single element. Call ryn_browser_read to obtain a unique selector.",
                details={"selector": selector} if selector else None,
            )
        raise AgentToolRejected("agent_browser_element_changed_or_script_failed")

    def evaluate(self, ident, task_id, expression, *, timeout=8.0, mutating=False, selector=None, guard=None):
        result = self.cdp(
            ident,
            task_id,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
            mutating=mutating,
            guard=guard,
        )
        if result.get("exceptionDetails"):
            self._raise_for_script_exception(result["exceptionDetails"], selector=selector)
        return result.get("result", {}).get("value")

    @staticmethod
    def document(args):
        frame = args.get("frame_selector")
        return "document.querySelector(" + json.dumps(frame) + ").contentDocument" if frame else "document"

    def _check_guards(self, allowed, cancelled):
        if cancelled and cancelled():
            raise AgentToolRejected("agent_cancelled")
        if allowed and not allowed():
            raise AgentToolRejected("agent_desktop_access_required")

    def _wait_for_navigation_ready(self, ident, task_id, allowed, cancelled, *, timeout=3.0, expect_new_doc=True):
        deadline = time.monotonic() + max(0.0, float(timeout))
        last_state = {"url": "", "title": "", "ready": "loading", "has_body": False, "pre_nav": expect_new_doc}
        def guard():
            return self._check_guards(allowed, cancelled)
        while True:
            self._check_guards(allowed, cancelled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                state = self.evaluate(
                    ident,
                    task_id,
                    "(() => ({url: String(location.href || ''), title: String(document.title || ''), "
                    "ready: String(document.readyState || ''), has_body: Boolean(document.body), "
                    "pre_nav: Boolean(window.__rynPreNav)}))()",
                    timeout=min(1.5, max(0.1, remaining)),
                    mutating=False,
                    guard=guard,
                )
                if isinstance(state, dict):
                    last_state = state
                    is_navigated = (not state.get("pre_nav")) if expect_new_doc else True
                    if (
                        is_navigated
                        and state.get("url") not in ("", "about:blank")
                        and state.get("ready") in ("interactive", "complete")
                        and state.get("has_body")
                    ):
                        with self.lock:
                            if ident in self.pages:
                                self.pages[ident]["ready_checked"] = True
                        return {
                            "url": state["url"],
                            "title": state["title"],
                            "ready": state["ready"],
                            "dom_ready": True,
                            "timed_out": False,
                        }
            except AgentToolRejected as exc:
                if str(exc) in {"agent_cancelled", "agent_desktop_access_required", "agent_browser_session_ended", "agent_browser_page_not_found"}:
                    raise
            self._check_guards(allowed, cancelled)
            sleep_for = min(0.05, max(0.0, deadline - time.monotonic()))
            if sleep_for <= 0:
                break
            time.sleep(sleep_for)
        self._check_guards(allowed, cancelled)
        return {
            "url": str(last_state.get("url") or ""),
            "title": str(last_state.get("title") or ""),
            "ready": str(last_state.get("ready") or "loading"),
            "dom_ready": False,
            "timed_out": True,
        }

    def _settle_after_interaction(self, ident, task_id, allowed, cancelled, *, timeout=1.5):
        self._check_guards(allowed, cancelled)
        time.sleep(0.03)
        self._check_guards(allowed, cancelled)
        try:
            state = self.evaluate(
                ident,
                task_id,
                "(() => ({url: String(location.href || ''), ready: String(document.readyState || ''), has_body: Boolean(document.body), pre_nav: Boolean(window.__rynPreNav)}))()",
                timeout=0.5,
                mutating=False,
                guard=lambda: self._check_guards(allowed, cancelled),
            )
        except AgentToolRejected as exc:
            if str(exc) in {"agent_cancelled", "agent_desktop_access_required"}:
                raise
            return
        except Exception:
            return
        self._check_guards(allowed, cancelled)
        if isinstance(state, dict) and (state.get("ready") == "loading" or not state.get("has_body") or state.get("pre_nav")):
            try:
                self._wait_for_navigation_ready(
                    ident,
                    task_id,
                    allowed,
                    cancelled,
                    timeout=timeout,
                    expect_new_doc=bool(state.get("pre_nav")),
                )
            except AgentToolRejected as exc:
                if str(exc) in {"agent_cancelled", "agent_desktop_access_required"}:
                    raise

    def preview(self, args, task_id):
        target = self.page(args["page_id"], task_id)
        result = {"page_url": target.get("url"), "page_title": target.get("title")}
        action = args["action"]
        if args.get("frame_selector") and action not in {"click", "hover", "fill", "clear", "select", "press", "wait"}:
            raise AgentToolRejected("agent_browser_frame_action_unsupported")
        if action in {"click", "hover", "fill", "clear", "select", "upload"}:
            selector = args.get("selector")
            if not selector:
                raise AgentToolRejected("agent_browser_selector_required")
            element = self.evaluate(
                args["page_id"],
                task_id,
                "(() => {"
                + RESOLVE_HELPER_JS
                + "const { element: e } = resolveTarget("
                + json.dumps(selector)
                + ", "
                + json.dumps(args.get("frame_selector"))
                + "); return {tag: e.tagName, type: e.type || '', name: (e.getAttribute('aria-label') || e.innerText || e.placeholder || '').slice(0, 160)};})()",
                selector=selector,
            )
            result["element"] = element
            if action in {"fill", "select"} and "text" not in args:
                raise AgentToolRejected("agent_browser_text_required")
        elif action == "press":
            key = args.get("key")
            if not key or key not in KEY_DEFINITIONS:
                raise AgentToolRejected("agent_browser_key_required")
            if args.get("selector"):
                selector = args["selector"]
                element = self.evaluate(
                    args["page_id"],
                    task_id,
                    "(() => {"
                    + RESOLVE_HELPER_JS
                    + "const { element: e } = resolveTarget("
                    + json.dumps(selector)
                    + ", "
                    + json.dumps(args.get("frame_selector"))
                    + "); return {tag: e.tagName, type: e.type || '', name: (e.getAttribute('aria-label') || e.innerText || e.placeholder || '').slice(0, 160)};})()",
                    selector=selector,
                )
                result["element"] = element
        elif action == "wait":
            wait_seconds = args.get("wait_seconds", 3.0)
            if type(wait_seconds) not in (int, float) or not (0.1 <= float(wait_seconds) <= 10.0):
                raise AgentToolRejected("agent_browser_wait_invalid")
        return result

    def execute(self, name, args, task_id, resolve, allowed, cancelled):
        state = {"side_effect_started": False}
        try:
            return self._execute_inner(name, args, task_id, resolve, allowed, cancelled, state)
        except AgentToolRejected as exc:
            try:
                self._check_guards(allowed, cancelled)
            except AgentToolRejected as guard_exc:
                if state["side_effect_started"]:
                    guard_exc.after_side_effect = True
                raise guard_exc from None
            if state["side_effect_started"]:
                exc.after_side_effect = True
            raise

    def _execute_inner(self, name, args, task_id, resolve, allowed, cancelled, state):
        self._check_guards(allowed, cancelled)
        def guard():
            return self._check_guards(allowed, cancelled)
        if name == "ryn_browser_open":
            url = self.validate_url(args["url"], resolve)
            session = self.session(task_id, resolve, allowed, cancelled)
            self._check_guards(allowed, cancelled)
            state["side_effect_started"] = True
            target = self.request(session, "/json/new?about:blank", "PUT")
            ident = target["id"]
            with self.lock:
                self.tab_sequence += 1
                self.pages[ident] = {"task_id": task_id, "seq": self.tab_sequence, "ready_checked": False, "target": target}
            self.cdp(ident, task_id, "Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(resolve("."))})
            try:
                self.evaluate(ident, task_id, "window.__rynPreNav = true", timeout=2.0, mutating=False)
            except Exception:
                pass
            self._check_guards(allowed, cancelled)
            nav_budget = max(0.1, float(NAVIGATE_WAIT_SECONDS))
            nav_deadline = time.monotonic() + nav_budget
            result = self.cdp(
                ident,
                task_id,
                "Page.navigate",
                {"url": url},
                timeout=max(0.1, nav_deadline - time.monotonic()),
                mutating=True,
                allow_response_timeout=True,
                guard=guard,
            )
            if result.get("errorText"):
                tabs = self.sync_tabs(task_id)
                return {"page_id": ident, "navigation_error": result["errorText"][:200], "tabs": tabs}
            remaining_nav = nav_deadline - time.monotonic()
            if result.get("_timed_out") or remaining_nav <= 0:
                self._check_guards(allowed, cancelled)
                nav_state = {"url": "", "title": "", "ready": "loading", "dom_ready": False, "timed_out": True}
            else:
                nav_state = self._wait_for_navigation_ready(
                    ident,
                    task_id,
                    allowed,
                    cancelled,
                    timeout=remaining_nav,
                    expect_new_doc=True,
                )
            tabs = self.sync_tabs(task_id)
            return {
                "page_id": ident,
                "navigation_requested": True,
                "url": nav_state["url"] or url,
                "title": nav_state["title"],
                "ready": nav_state["ready"],
                "dom_ready": nav_state["dom_ready"],
                "timed_out": nav_state["timed_out"],
                "tabs": tabs,
                "next_step": (
                    "Navigation wait timed out before DOM ready; use ryn_browser_action wait or ryn_browser_read to check progress."
                    if nav_state["timed_out"]
                    else "Read the page to observe loading and results."
                ),
            }
        ident = args["page_id"]
        self.page(ident, task_id)
        if name == "ryn_browser_read":
            meta = self.pages.get(ident) or {}
            if not meta.get("ready_checked"):
                try:
                    cur = self.evaluate(
                        ident,
                        task_id,
                        "(() => ({url: String(location.href || ''), ready: String(document.readyState || ''), has_body: Boolean(document.body), pre_nav: Boolean(window.__rynPreNav)}))()",
                        timeout=1.0,
                        mutating=False,
                        guard=guard,
                    )
                    if isinstance(cur, dict) and (cur.get("pre_nav") or cur.get("url") in ("", "about:blank") or cur.get("ready") not in ("interactive", "complete") or not cur.get("has_body")):
                        self._wait_for_navigation_ready(
                            ident,
                            task_id,
                            allowed,
                            cancelled,
                            timeout=NAVIGATE_WAIT_SECONDS,
                            expect_new_doc=bool(cur.get("pre_nav")),
                        )
                    else:
                        with self.lock:
                            if ident in self.pages:
                                self.pages[ident]["ready_checked"] = True
                except AgentToolRejected as exc:
                    if str(exc) in {"agent_cancelled", "agent_desktop_access_required"}:
                        raise
            self._check_guards(allowed, cancelled)
            options = {key: args[key] for key in ("element_offset", "text_offset", "query", "scope_selector", "frame_selector") if key in args}
            result = self.evaluate(ident, task_id, SNAPSHOT + "(" + json.dumps(options) + ")", mutating=False, guard=guard)
            if args.get("screenshot_path"):
                path = resolve(args["screenshot_path"])
                if path.suffix.lower() != ".png" or path.exists():
                    raise AgentToolRejected("agent_screenshot_requires_new_png")
                result_png = self.cdp(ident, task_id, "Page.captureScreenshot", {"format": "png"}, mutating=False, guard=guard)
                state["side_effect_started"] = True
                with path.open("xb") as stream:
                    stream.write(base64.b64decode(result_png["data"]))
                result["screenshot_path"] = str(path)
            tabs = self.sync_tabs(task_id)
            return {"page_id": ident, **result, "tabs": tabs}
        action = args["action"]
        self.preview(args, task_id)
        self._check_guards(allowed, cancelled)
        if action in {"click", "hover"}:
            selector = args["selector"]
            coords = self.evaluate(
                ident,
                task_id,
                PREPARE_POINTER_JS
                + "("
                + json.dumps({
                    "selector": selector,
                    "frame_selector": args.get("frame_selector"),
                    "check_disabled": action == "click",
                })
                + ")",
                mutating=False,
                selector=selector,
            )
            self._check_guards(allowed, cancelled)
            x, y = float(coords["x"]), float(coords["y"])
            if action == "hover":
                state["side_effect_started"] = True
                self.cdp(
                    ident,
                    task_id,
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0},
                    mutating=True,
                )
            else:
                self.cdp(
                    ident,
                    task_id,
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0},
                    mutating=False,
                )
                self._check_guards(allowed, cancelled)
                verified = self.evaluate(
                    ident,
                    task_id,
                    PREPARE_POINTER_JS
                    + "("
                    + json.dumps({
                        "selector": selector,
                        "frame_selector": args.get("frame_selector"),
                        "check_disabled": True,
                        "verify_only": True,
                        "expected_ryn_id": coords.get("ryn_id"),
                        "expected_x": x,
                        "expected_y": y,
                    })
                    + ")",
                    mutating=False,
                    selector=selector,
                )
                x, y = float(verified["x"]), float(verified["y"])
                self._check_guards(allowed, cancelled)
                state["side_effect_started"] = True
                self.cdp(
                    ident,
                    task_id,
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": x, "y": y, "button": "left", "buttons": 1, "clickCount": 1},
                    mutating=True,
                )
                self.cdp(
                    ident,
                    task_id,
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1},
                    mutating=True,
                )
                self._settle_after_interaction(ident, task_id, allowed, cancelled, timeout=1.5)
        elif action in {"fill", "clear", "select"}:
            selector = args["selector"]
            text = json.dumps("" if action == "clear" else args.get("text", ""))
            # First validate element state before mutating
            self.evaluate(
                ident,
                task_id,
                "(() => {"
                + RESOLVE_HELPER_JS
                + "const { win, element: e } = resolveTarget("
                + json.dumps(selector)
                + ", "
                + json.dumps(args.get("frame_selector"))
                + ");"
                "if (e.disabled || (e.matches && e.matches(':disabled')) || e.getAttribute('aria-disabled') === 'true') throw Error('DISABLED:Element is disabled');"
                "if (e.readOnly || e.getAttribute('aria-readonly') === 'true') throw Error('READONLY:Element is read-only');"
                + (
                    "if (e.tagName !== 'SELECT') throw Error('NOT_EDITABLE:Element is not a select');"
                    if action == "select"
                    else "if (e.tagName !== 'INPUT' && e.tagName !== 'TEXTAREA' && !e.isContentEditable && e.getAttribute('contenteditable') !== 'true') throw Error('NOT_EDITABLE:Element is not editable');"
                )
                + "return true;})()",
                mutating=False,
                selector=selector,
            )
            self._check_guards(allowed, cancelled)
            if action == "select":
                mutation = (
                    "e.value = " + text + "; "
                    "e.dispatchEvent(new win.Event('input', {bubbles: true})); "
                    "e.dispatchEvent(new win.Event('change', {bubbles: true}));"
                )
            else:
                mutation = (
                    "e.focus(); "
                    "if (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') { "
                    "  const proto = e.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype; "
                    "  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set; "
                    "  if (setter) setter.call(e, " + text + "); else e.value = " + text + "; "
                    "} else { "
                    "  e.textContent = " + text + "; "
                    "} "
                    "e.dispatchEvent(new win.InputEvent('input', {bubbles: true, data: " + ("null" if action == "clear" else text) + ", inputType: " + ("'deleteContentBackward'" if action == "clear" else "'insertText'") + "})); "
                    "e.dispatchEvent(new win.Event('change', {bubbles: true})); "
                    "const actual = (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') ? String(e.value || '') : String(e.textContent || ''); "
                    + ("if (actual !== '') throw Error('CLEAR_FAILED:Controlled input did not clear');" if action == "clear" else "")
                )
            state["side_effect_started"] = True
            self.evaluate(
                ident,
                task_id,
                "(() => {"
                + RESOLVE_HELPER_JS
                + "const { win, element: e } = resolveTarget("
                + json.dumps(selector)
                + ", "
                + json.dumps(args.get("frame_selector"))
                + "); "
                + mutation
                + " return true;})()",
                mutating=True,
                selector=selector,
            )
        elif action == "press":
            key_name = args["key"]
            spec = KEY_DEFINITIONS[key_name]
            if args.get("selector"):
                selector = args["selector"]
                self.evaluate(
                    ident,
                    task_id,
                    "(() => {"
                    + RESOLVE_HELPER_JS
                    + "const { doc, win, frameEl, element: e } = resolveTarget("
                    + json.dumps(selector)
                    + ", "
                    + json.dumps(args.get("frame_selector"))
                    + "); "
                    "if (e.disabled || (e.matches && e.matches(':disabled')) || e.getAttribute('aria-disabled') === 'true') throw Error('DISABLED:Element is disabled'); "
                    "if (e.hidden) throw Error('NOT_VISIBLE:Element is hidden'); "
                    "const style = win.getComputedStyle(e); "
                    "if (style.display === 'none' || style.visibility === 'hidden') throw Error('NOT_VISIBLE:Element is not visible'); "
                    "if (frameEl) frameEl.focus(); "
                    "e.focus(); "
                    "if (doc.activeElement !== e || (frameEl && document.activeElement !== frameEl)) { "
                    "  throw Error('FOCUS_FAILED:Target element did not receive keyboard focus'); "
                    "} "
                    "return true;})()",
                    mutating=False,
                    selector=selector,
                )
            self._check_guards(allowed, cancelled)
            down_params = {
                "type": "keyDown" if "text" in spec else "rawKeyDown",
                "key": spec["key"],
                "code": spec["code"],
                "windowsVirtualKeyCode": spec["vk"],
                "nativeVirtualKeyCode": spec["vk"],
            }
            if "text" in spec:
                down_params["text"] = spec["text"]
                down_params["unmodifiedText"] = spec["text"]
            up_params = {
                "type": "keyUp",
                "key": spec["key"],
                "code": spec["code"],
                "windowsVirtualKeyCode": spec["vk"],
                "nativeVirtualKeyCode": spec["vk"],
            }
            state["side_effect_started"] = True
            self.cdp(ident, task_id, "Input.dispatchKeyEvent", down_params, mutating=True)
            self.cdp(ident, task_id, "Input.dispatchKeyEvent", up_params, mutating=True)
            self._settle_after_interaction(ident, task_id, allowed, cancelled, timeout=1.5)
        elif action == "wait":
            wait_seconds = float(args.get("wait_seconds", 3.0))
            started = time.monotonic()
            deadline = started + wait_seconds
            selector = args.get("selector")
            if selector:
                while True:
                    self._check_guards(allowed, cancelled)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        matched = self.evaluate(
                            ident,
                            task_id,
                            "(() => {"
                            "let doc = document;"
                            + (
                                "const fs = document.querySelectorAll(" + json.dumps(args["frame_selector"]) + ");"
                                "if (fs.length !== 1) return false;"
                                "try { doc = fs[0].contentDocument; } catch (_) { return false; }"
                                "if (!doc) return false;"
                                if args.get("frame_selector")
                                else ""
                            )
                            + "const win = doc.defaultView || window;"
                            "let els; try { els = [...doc.querySelectorAll(" + json.dumps(selector) + ")]; } catch (_) { throw Error('INVALID_SELECTOR:Invalid selector'); }"
                            "return els.some(e => {"
                            "  if (e.hidden) return false;"
                            "  const s = win.getComputedStyle(e);"
                            "  if (s.display === 'none' || s.visibility === 'hidden' || s.visibility === 'collapse') return false;"
                            "  const rs = e.getClientRects();"
                            "  return rs.length > 0 && [...rs].some(r => r.width > 0 && r.height > 0);"
                            "});"
                            "})()",
                            timeout=min(1.5, max(0.1, remaining)),
                            mutating=False,
                            selector=selector,
                            guard=guard,
                        )
                    except AgentToolRejected as exc:
                        if str(exc) == "agent_browser_timeout":
                            self._check_guards(allowed, cancelled)
                            self.page(ident, task_id, timeout=min(1.0, max(0.1, deadline - time.monotonic())), refresh=True)
                            if time.monotonic() < deadline:
                                continue
                            break
                        raise
                    if matched:
                        tabs = self.sync_tabs(task_id)
                        return {
                            "page_id": ident,
                            "action_delivered": "wait",
                            "selector": selector,
                            "matched": True,
                            "ready": True,
                            "timed_out": False,
                            "waited_seconds": round(time.monotonic() - started, 3),
                            "tabs": tabs,
                            "next_step": "Selector is visible; read page again to inspect current elements.",
                        }
                    self._check_guards(allowed, cancelled)
                    sleep_for = min(0.05, max(0.0, deadline - time.monotonic()))
                    if sleep_for <= 0:
                        break
                    time.sleep(sleep_for)
                self._check_guards(allowed, cancelled)
                tabs = self.sync_tabs(task_id)
                return {
                    "page_id": ident,
                    "action_delivered": "wait",
                    "selector": selector,
                    "matched": False,
                    "ready": False,
                    "timed_out": True,
                    "waited_seconds": round(time.monotonic() - started, 3),
                    "tabs": tabs,
                    "next_step": "Selector wait timed out before target became visible; read page to inspect actual DOM state.",
                }
            while True:
                self._check_guards(allowed, cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
            self._check_guards(allowed, cancelled)
            tabs = self.sync_tabs(task_id)
            return {
                "page_id": ident,
                "action_delivered": "wait",
                "delay_completed": True,
                "timed_out": False,
                "waited_seconds": round(time.monotonic() - started, 3),
                "note": "Bounded delay completed; elapsed time alone does not confirm asynchronous rendering is finished.",
                "tabs": tabs,
                "next_step": "Read page again to verify the actual effect.",
            }
        elif action == "scroll":
            state["side_effect_started"] = True
            self.evaluate(ident, task_id, f"window.scrollBy(0,{args.get('delta_y', 600)})", mutating=True)
        elif action == "back":
            state["side_effect_started"] = True
            self.evaluate(ident, task_id, "history.back()", mutating=True)
            self._settle_after_interaction(ident, task_id, allowed, cancelled, timeout=1.5)
        elif action == "reload":
            try:
                self.evaluate(ident, task_id, "window.__rynPreNav = true", timeout=1.0, mutating=False)
            except Exception:
                pass
            state["side_effect_started"] = True
            self.cdp(ident, task_id, "Page.reload", mutating=True)
            self._wait_for_navigation_ready(ident, task_id, allowed, cancelled, timeout=3.0, expect_new_doc=True)
        elif action == "upload":
            selector = args["selector"]
            self.evaluate(
                ident,
                task_id,
                "(() => {"
                + RESOLVE_HELPER_JS
                + "const { element: e } = resolveTarget(" + json.dumps(selector) + ", null);"
                "if (e.disabled || (e.matches && e.matches(':disabled')) || e.getAttribute('aria-disabled') === 'true') throw Error('DISABLED:Element is disabled');"
                "return true;})()",
                mutating=False,
                selector=selector,
            )
            document = self.cdp(ident, task_id, "DOM.getDocument", mutating=False)
            node = self.cdp(ident, task_id, "DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": selector}, mutating=False)
            if not node.get("nodeId"):
                raise AgentToolRejected("agent_browser_element_changed_or_script_failed")
            self._check_guards(allowed, cancelled)
            state["side_effect_started"] = True
            self.cdp(ident, task_id, "DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(resolve(p)) for p in args["files"]]}, mutating=True)
        tabs = self.sync_tabs(task_id)
        return {"page_id": ident, "action_delivered": action, "tabs": tabs, "next_step": "Read page again to verify the actual effect."}
