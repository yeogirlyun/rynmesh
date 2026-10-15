"""A separate task-owned Chromium profile, controlled through loopback CDP."""
from __future__ import annotations

import base64
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .store import AgentToolRejected


def find_browser():
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        if executable := shutil.which(name):
            return executable
    if os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""), os.environ.get("LOCALAPPDATA", "")):
            for relative in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
                path = Path(base) / relative
                if path.is_file():
                    return str(path)
    return None


SNAPSHOT = """(options => {
 let doc=document;
 if(options.frame_selector) {doc=document.querySelector(options.frame_selector)?.contentDocument; if(!doc) throw Error('frame unavailable');}
 const scope=options.scope_selector?doc.querySelector(options.scope_selector):doc;
 if(!scope) throw Error('scope unavailable');
 const els=[...scope.querySelectorAll('a,button,input,textarea,select,iframe,[role="button"],[contenteditable="true"]')];
 const visible=els.filter(e=>e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
 visible.forEach((e,i)=>e.setAttribute('data-ryn-element',String(i)));
 const filtered=options.query?visible.filter(e=>(e.getAttribute('aria-label')||e.innerText||e.placeholder||e.name||'').toLowerCase().includes(options.query.toLowerCase())):visible;
 const offset=options.element_offset||0, textOffset=options.text_offset||0;
 const text=(scope===doc?doc.body:scope)?.innerText||'';
 if(offset>filtered.length || textOffset>text.length) throw Error('offset unavailable');
 const items=filtered.slice(offset,offset+40).map(e=>{
   return {selector:'[data-ryn-element="'+e.getAttribute('data-ryn-element')+'"]',tag:e.tagName.toLowerCase(),type:e.type||'',
     name:(e.getAttribute('aria-label')||e.innerText||e.placeholder||e.name||'').slice(0,160),
     value:e.type==='password'?'[password]':String(e.value||'').slice(0,160)};
 });
 if (!window.__rynErrors) {window.__rynErrors=[]; addEventListener('error',e=>window.__rynErrors.push(String(e.message).slice(0,200)));}
 return {url:location.href,title:document.title,ready:document.readyState,
 text:text.slice(textOffset,textOffset+4000),elements:items,errors:window.__rynErrors.slice(-8),
 element_count:filtered.length,text_length:text.length,element_offset:offset,text_offset:textOffset,
 next_element_offset:offset+40<filtered.length?offset+40:null,next_text_offset:textOffset+4000<text.length?textOffset+4000:null,
 incomplete:offset+40<filtered.length||textOffset+4000<text.length,
 frame_selector:options.frame_selector||null};
})"""


class Browser:
    def __init__(self, home, processes, *, headless=False):
        self.home, self.processes = Path(home), processes
        self.sessions = {}
        self.pages = {}
        self.headless = headless
        self.connections = {}
        self.sequence = 0
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
            if port_file.exists():
                lines = port_file.read_text().splitlines()
                if lines and lines[0].isdigit():
                    current = {"port": int(lines[0]), "process_id": ident}
                    self.sessions[task_id] = current
                    return current
            if self.processes.get(ident, task_id)["process"].poll() is not None:
                break
            time.sleep(0.1)
        self.processes.stop(ident, task_id)
        raise AgentToolRejected("agent_browser_start_failed")

    @staticmethod
    def request(session, route, method="GET"):
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.request(method, f"http://127.0.0.1:{session['port']}{route}")
            response.raise_for_status()
            return response.json()

    def page(self, ident, task_id):
        record = self.pages.get(ident)
        if not record or record["task_id"] != task_id:
            raise AgentToolRejected("agent_browser_page_not_found")
        session = self.sessions[task_id]
        if self.processes.get(session["process_id"], task_id)["process"].poll() is not None:
            raise AgentToolRejected("agent_browser_session_ended")
        rows = self.request(session, "/json/list")
        target = next((r for r in rows if r.get("id") == ident and r.get("type") == "page"), None)
        if target is None:
            raise AgentToolRejected("agent_browser_page_not_found")
        return target

    def cdp(self, ident, task_id, method, params=None):
        from websockets.sync.client import connect
        target = self.page(ident, task_id)
        uri = target["webSocketDebuggerUrl"]
        parsed = urlparse(uri)
        if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != self.sessions[task_id]["port"]:
            raise AgentToolRejected("agent_browser_debug_endpoint_invalid")
        with self.lock:
            connection = self.connections.get(ident)
            if connection is None:
                connection = connect(uri, open_timeout=5, close_timeout=1, max_size=8 * 1024 * 1024, proxy=None)
                self.connections[ident] = connection
            self.sequence += 1
            sequence = self.sequence
            connection.send(json.dumps({"id": sequence, "method": method, "params": params or {}}))
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                response = json.loads(connection.recv(timeout=max(0.1, deadline - time.monotonic())))
                if response.get("id") == sequence:
                    if response.get("error"):
                        raise AgentToolRejected("agent_browser_protocol_error")
                    return response.get("result", {})
        raise AgentToolRejected("agent_browser_timeout")

    def evaluate(self, ident, task_id, expression):
        result = self.cdp(ident, task_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
        if result.get("exceptionDetails"):
            raise AgentToolRejected("agent_browser_element_changed_or_script_failed")
        return result.get("result", {}).get("value")

    @staticmethod
    def document(args):
        frame = args.get("frame_selector")
        return "document.querySelector(" + json.dumps(frame) + ").contentDocument" if frame else "document"

    def preview(self, args, task_id):
        target = self.page(args["page_id"], task_id)
        result = {"page_url": target.get("url"), "page_title": target.get("title")}
        action = args["action"]
        if action in {"click", "fill", "select", "upload"}:
            selector = args.get("selector")
            if not selector:
                raise AgentToolRejected("agent_browser_selector_required")
            element = self.evaluate(args["page_id"], task_id, "(() => {const es=" + self.document(args) + ".querySelectorAll(" + json.dumps(selector) + "); if(es.length!==1) throw Error('unique selector required'); const e=es[0]; return {tag:e.tagName,type:e.type||'',name:(e.getAttribute('aria-label')||e.innerText||e.placeholder||'').slice(0,160)};})()")
            result["element"] = element
            if action in {"fill", "select"} and "text" not in args:
                raise AgentToolRejected("agent_browser_text_required")
        if action == "press" and "key" not in args:
            raise AgentToolRejected("agent_browser_key_required")
        if args.get("frame_selector") and action not in {"click", "fill", "select"}:
            raise AgentToolRejected("agent_browser_frame_action_unsupported")
        return result

    def execute(self, name, args, task_id, resolve, allowed, cancelled):
        if name == "ryn_browser_open":
            url = self.validate_url(args["url"], resolve)
            session = self.session(task_id, resolve, allowed, cancelled)
            target = self.request(session, "/json/new?about:blank", "PUT")
            ident = target["id"]
            self.pages[ident] = {"task_id": task_id}
            self.cdp(ident, task_id, "Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(resolve("."))})
            result = self.cdp(ident, task_id, "Page.navigate", {"url": url})
            if result.get("errorText"):
                return {"page_id": ident, "navigation_error": result["errorText"][:200]}
            return {"page_id": ident, "navigation_requested": True, "url": url, "next_step": "Read the page to observe loading and results."}
        ident = args["page_id"]
        self.page(ident, task_id)
        if name == "ryn_browser_read":
            options = {key: args[key] for key in ("element_offset", "text_offset", "query", "scope_selector", "frame_selector") if key in args}
            result = self.evaluate(ident, task_id, SNAPSHOT + "(" + json.dumps(options) + ")")
            if args.get("screenshot_path"):
                path = resolve(args["screenshot_path"])
                if path.suffix.lower() != ".png" or path.exists():
                    raise AgentToolRejected("agent_screenshot_requires_new_png")
                result_png = self.cdp(ident, task_id, "Page.captureScreenshot", {"format": "png"})
                with path.open("xb") as stream:
                    stream.write(base64.b64decode(result_png["data"]))
                result["screenshot_path"] = str(path)
            return {"page_id": ident, **result}
        action = args["action"]
        self.preview(args, task_id)
        if action in {"click", "fill", "select"}:
            selector = json.dumps(args["selector"])
            text = json.dumps(args.get("text", ""))
            operation = "e.click()" if action == "click" else (
                "e.value=" + text + "; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}))" if action == "select" else
                "e.focus(); const setter=Object.getOwnPropertyDescriptor(e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value')?.set; if(setter) setter.call(e," + text + "); else e.textContent=" + text + "; e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true}))")
            self.evaluate(ident, task_id, "(()=>{const es=" + self.document(args) + ".querySelectorAll(" + selector + "); if(es.length!==1) throw Error('changed'); const e=es[0]; " + operation + "; return true;})()")
        elif action == "press":
            key = args["key"]
            codes = {"Enter": 13, "Tab": 9, "Escape": 27, "ArrowDown": 40, "ArrowUp": 38}
            self.cdp(ident, task_id, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": key, "windowsVirtualKeyCode": codes[key]})
            self.cdp(ident, task_id, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": key, "windowsVirtualKeyCode": codes[key]})
        elif action == "scroll":
            self.evaluate(ident, task_id, f"window.scrollBy(0,{args.get('delta_y', 600)})")
        elif action == "back":
            self.evaluate(ident, task_id, "history.back()")
        elif action == "reload":
            self.cdp(ident, task_id, "Page.reload")
        elif action == "upload":
            document = self.cdp(ident, task_id, "DOM.getDocument")
            node = self.cdp(ident, task_id, "DOM.querySelector", {"nodeId": document["root"]["nodeId"], "selector": args["selector"]})
            self.cdp(ident, task_id, "DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(resolve(p)) for p in args["files"]]})
        return {"page_id": ident, "action_delivered": action, "next_step": "Read page again to verify the actual effect."}
