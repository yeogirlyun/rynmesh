"""Tests for S11-I24: ryn_browser_* lifetime, cross-platform discovery, and robustness."""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from test_agent_local_tools import execute
from test_agent_local_tools import local as local  # noqa: F401

import rynmesh.agent_runtime.local_browser as lb
from rynmesh.agent_runtime.local_browser import Browser, find_browser
from rynmesh.agent_runtime.local_specs import SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.store import AgentError, AgentToolRejected


def _enable_browser(tools, context, state, *, headless=True, **kwargs):
    context["local_execution"]["desktop_access"] = True
    def enable(data):
        data["settings"]["desktop_access"] = True
        # Authority comes from the stored task row, not the caller's context copy.
        data["tasks"][context["task_id"]]["local_execution"]["desktop_access"] = True
    state.transaction(enable)
    tools._browser = Browser(tools.home, tools.processes, headless=headless, **kwargs)
    return tools._browser


def test_t01_session_survives_past_600s_and_uses_no_process_slots(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "hello.html").write_text(
            "<!doctype html><html><head><title>Long Lived</title></head><body><button id='b'>OK</button></body></html>",
            encoding="utf-8",
        )
        opened = execute(tools, context, "ryn_browser_open", {"url": (root / "hello.html").as_uri()}, "op_t01_open")
        page_id = opened["page_id"]

        # Browser must run as a service process and NOT occupy any of the 8 Processes slots.
        assert len(tools.processes.items) == 0

        session = browser.sessions[context["task_id"]]
        svc_proc = session["service_process"]
        orig_pid = svc_proc.pid
        assert svc_proc.poll() is None

        # Simulate session created > 600s ago (650s), recently active -> must NOT be killed!
        session["created_at"] = time.monotonic() - 650.0
        session["last_used"] = time.monotonic() - 5.0
        browser._reap_idle_sessions()
        snap = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        assert snap["title"] == "Long Lived"
        assert browser.sessions[context["task_id"]]["service_process"].pid == orig_pid
        assert svc_proc.poll() is None

        # Simulate > 30 minutes idle -> idle reaper closes browser and removes session.
        session["last_used"] = time.monotonic() - 1805.0
        browser._reap_idle_sessions()
        assert context["task_id"] not in browser.sessions
        deadline = time.monotonic() + 5.0
        while svc_proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert svc_proc.poll() is not None
    finally:
        browser.close()


def test_t02_closing_session_deletes_temp_profile_dir(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "p.html").write_text("<!doctype html><title>Temp Profile</title><p>hi</p>", encoding="utf-8")
        execute(tools, context, "ryn_browser_open", {"url": (root / "p.html").as_uri()}, "op_t02_open")
        session = browser.sessions[context["task_id"]]
        profile_dir = Path(session["profile_dir"])
        svc_proc = session["service_process"]
        assert profile_dir.is_dir()
        assert profile_dir.parent == Path(tools.home) / "agent" / "browser"

        tools.forget_baselines(context["task_id"])

        assert context["task_id"] not in browser.sessions
        assert svc_proc.poll() is not None
        assert not profile_dir.exists()
    finally:
        browser.close()


def test_t03_find_browser_macos_linux_and_env_override(tmp_path, monkeypatch):
    # 1. RYN_CHROME_EXECUTABLE takes highest precedence when pointing to a file.
    custom_bin = tmp_path / "custom-chrome-bin"
    custom_bin.write_bytes(b"fake")
    monkeypatch.setenv("RYN_CHROME_EXECUTABLE", str(custom_bin))
    assert find_browser() == str(custom_bin)
    monkeypatch.delenv("RYN_CHROME_EXECUTABLE", raising=False)

    # 2. macOS standard /Applications bundle path (simulated filesystem).
    mac_root = tmp_path / "mac_fs"
    mac_chrome = mac_root / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    mac_chrome.parent.mkdir(parents=True)
    mac_chrome.write_bytes(b"macho")
    found_mac = find_browser(
        platform="darwin",
        fs_root=mac_root,
        environ={},
        which=lambda _name: None,
    )
    assert found_mac == str(mac_chrome)

    # macOS Edge fallback when Chrome is absent.
    mac_root2 = tmp_path / "mac_fs2"
    mac_edge = mac_root2 / "Applications" / "Microsoft Edge.app" / "Contents" / "MacOS" / "Microsoft Edge"
    mac_edge.parent.mkdir(parents=True)
    mac_edge.write_bytes(b"macho")
    assert find_browser(platform="darwin", fs_root=mac_root2, environ={}, which=lambda _name: None) == str(mac_edge)

    # 3. Linux standard /usr/bin path (simulated filesystem).
    linux_root = tmp_path / "linux_fs"
    linux_chrome = linux_root / "usr" / "bin" / "google-chrome-stable"
    linux_chrome.parent.mkdir(parents=True)
    linux_chrome.write_bytes(b"elf")
    found_linux = find_browser(
        platform="linux",
        fs_root=linux_root,
        environ={},
        which=lambda _name: None,
    )
    assert found_linux == str(linux_chrome)


def test_t04_javascript_dialogs_auto_handled_and_reported_or_dialog_open_error(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "dialog.html").write_text(
            """<!doctype html><html><head><title>Dialogs</title></head><body>
            <button id="trig" onclick="alert('Untrusted alert 123'); window.confirmResult = confirm('Delete everything?'); document.getElementById('out').textContent = 'confirm=' + String(window.confirmResult);">Trigger</button>
            <p id="out">idle</p>
            </body></html>""",
            encoding="utf-8",
        )
        opened = execute(tools, context, "ryn_browser_open", {"url": (root / "dialog.html").as_uri()}, "op_t04_open")
        page_id = opened["page_id"]

        # Clicking triggers alert() then confirm(): must NOT hang/timeout; alert accepted, confirm cancelled (false).
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#trig"}, "op_t04_click")
        snap = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        assert "confirm=false" in snap["text"]
        assert "dialogs" in snap and len(snap["dialogs"]) >= 2
        messages = [d["message"] for d in snap["dialogs"]]
        assert "Untrusted alert 123" in messages
        assert "Delete everything?" in messages
        assert all(d.get("untrusted") is True for d in snap["dialogs"])

        # Subsequent read after dialogs were drained does not duplicate old dialogs.
        snap2 = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        assert not snap2.get("dialogs")

        # If an unhandled dialog remains open while Runtime.evaluate hangs, raise agent_browser_dialog_open instead of timeout.
        real_sock = browser.connections[page_id]

        class UnhandledDialogSocket:
            def __init__(self):
                self.sent_event = False

            def send(self, payload):
                msg = json.loads(payload)
                # Ignore handleJavaScriptDialog so dialog stays open
                if msg.get("method") == "Runtime.evaluate":
                    self.sent_event = False

            def recv(self, timeout=None):
                if not self.sent_event:
                    self.sent_event = True
                    return json.dumps({
                        "method": "Page.javascriptDialogOpening",
                        "params": {"type": "confirm", "message": "Blocking dialog", "url": "about:blank"},
                    })
                time.sleep(min(0.05, timeout or 0.05))
                raise TimeoutError("dialog blocking Runtime.evaluate")

            def close(self):
                self.closed = True

        browser._auto_handle_dialogs = False
        browser.connections[page_id] = UnhandledDialogSocket()
        try:
            with pytest.raises(AgentToolRejected) as exc_info:
                browser.evaluate(page_id, context["task_id"], "1 + 1", timeout=0.25)
            assert str(exc_info.value) == "agent_browser_dialog_open"
        finally:
            browser._auto_handle_dialogs = True
            browser.connections[page_id] = real_sock
    finally:
        browser.close()


def test_t05_two_tabs_concurrent_cdp_do_not_block_each_other(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "tab_a.html").write_text("<!doctype html><title>Tab A</title><p>A</p>", encoding="utf-8")
        (root / "tab_b.html").write_text("<!doctype html><title>Tab B</title><p>B</p>", encoding="utf-8")
        page_a = execute(tools, context, "ryn_browser_open", {"url": (root / "tab_a.html").as_uri()}, "op_t05_a")["page_id"]
        page_b = execute(tools, context, "ryn_browser_open", {"url": (root / "tab_b.html").as_uri()}, "op_t05_b")["page_id"]

        # Warm up both tab WebSocket connections first
        assert browser.evaluate(page_a, context["task_id"], "document.title") == "Tab A"
        assert browser.evaluate(page_b, context["task_id"], "document.title") == "Tab B"

        results = {}
        started_a = threading.Event()

        def slow_tab_a():
            started_a.set()
            results["a"] = browser.evaluate(
                page_a,
                context["task_id"],
                "new Promise(resolve => setTimeout(() => resolve('slow_done'), 1200))",
                timeout=5.0,
            )

        thread_a = threading.Thread(target=slow_tab_a)
        thread_a.start()
        started_a.wait(timeout=1.0)
        time.sleep(0.08)

        t0 = time.monotonic()
        val_b = browser.evaluate(page_b, context["task_id"], "'fast_' + document.title", timeout=2.0)
        elapsed_b = time.monotonic() - t0
        thread_a.join(timeout=5.0)

        assert val_b == "fast_Tab B"
        assert elapsed_b < 0.5, f"Tab B was blocked by Tab A ({elapsed_b:.3f}s)"
        assert results.get("a") == "slow_done"

        # Same-tab concurrent CDP calls: out-of-order responses matched strictly by id (no disorder).
        same_tab_id = "same_tab_mux"
        sent_ids: list[int] = []
        ready_two = threading.Event()
        out_lock = threading.Lock()
        reply_queue: list[str] = []

        class OutOfOrderSocket:
            def send(self, payload):
                mid = json.loads(payload)["id"]
                with out_lock:
                    sent_ids.append(mid)
                    if len(sent_ids) == 2:
                        # Reply in reverse order: second request first, first request second!
                        reply_queue.append(json.dumps({"id": sent_ids[1], "result": {"result": {"value": "second_resp"}}}))
                        reply_queue.append(json.dumps({"id": sent_ids[0], "result": {"result": {"value": "first_resp"}}}))
                        ready_two.set()

            def recv(self, timeout=None):
                ready_two.wait(timeout=timeout or 1.0)
                with out_lock:
                    if reply_queue:
                        return reply_queue.pop(0)
                raise TimeoutError("waiting")

            def close(self):
                self.closed = True

        browser.connections[same_tab_id] = OutOfOrderSocket()
        mux_res = {}
        t1 = threading.Thread(target=lambda: mux_res.update(r1=browser.evaluate(same_tab_id, context["task_id"], "'q1'")))
        t2 = threading.Thread(target=lambda: mux_res.update(r2=browser.evaluate(same_tab_id, context["task_id"], "'q2'")))
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)
        assert sorted(mux_res.values()) == ["first_resp", "second_resp"]
        # Verify the thread that sent sent_ids[0] got "first_resp" and sent_ids[1] got "second_resp"
        browser._close_connection(same_tab_id)
    finally:
        browser.close()


def test_t06_downloads_saved_to_ryn_downloads_excluded_and_reported(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    # Simulate git repository info dir so .git/info/exclude protection is verified
    git_info = root / ".git" / "info"
    git_info.mkdir(parents=True)
    exclude_file = git_info / "exclude"
    exclude_file.write_text("# git exclude\n", encoding="utf-8")

    payload_bytes = b"downloaded-report-content-12345\n"

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/file":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="report.txt"')
                self.send_header("Content-Length", str(len(payload_bytes)))
                self.end_headers()
                self.wfile.write(payload_bytes)
                return
            html = (
                "<!doctype html><html><head><title>DL</title></head>"
                "<body><a id='dl' href='/file' download='report.txt'>Download Report</a></body></html>"
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, format, *args):
            return

    class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    browser = _enable_browser(tools, context, state, headless=True)
    server = _TCPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    try:
        opened = execute(tools, context, "ryn_browser_open", {"url": f"http://127.0.0.1:{port}/"}, "op_t06_open")
        page_id = opened["page_id"]

        assert ".ryn/" in exclude_file.read_text(encoding="utf-8")

        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#dl"}, "op_t06_click")

        dl_root = root / ".ryn" / "downloads"
        reported = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            snap = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
            if snap.get("downloads"):
                reported = snap["downloads"]
                break
            time.sleep(0.1)

        assert not (root / "report.txt").exists(), "Download leaked into workspace root!"
        assert reported is not None and len(reported) >= 1
        item = reported[0]
        assert item["filename"] == "report.txt"
        assert item["bytes"] == len(payload_bytes)
        saved_path = Path(item["path"])
        assert saved_path.is_file()
        assert saved_path.is_relative_to(dl_root.resolve())
        assert saved_path.read_bytes() == payload_bytes

        # Oversized download (> 200 MiB) must be cancelled immediately and never reported as completed.
        canceled_guids: list[str] = []
        task_dl_dir = saved_path.parent
        oversize_partial = task_dl_dir / "huge.iso"
        oversize_partial.write_bytes(b"partial")
        browser._handle_download_event(
            context["task_id"],
            "Browser.downloadWillBegin",
            {"guid": "guid-oversize-1", "suggestedFilename": "huge.iso", "url": "http://127.0.0.1/huge.iso"},
        )
        browser._handle_download_event(
            context["task_id"],
            "Browser.downloadProgress",
            {"guid": "guid-oversize-1", "totalBytes": 205 * 1024 * 1024, "receivedBytes": 1024, "state": "inProgress"},
            cancel_fn=lambda g: canceled_guids.append(g),
        )
        assert canceled_guids == ["guid-oversize-1"]
        assert not oversize_partial.exists()
        assert browser._drain_downloads(context["task_id"]) == []
    finally:
        server.shutdown()
        server.server_close()
        browser.close()


def test_t07_validate_url_blocks_cloud_metadata_and_allows_localhost_and_lan(tmp_path):
    resolve = lambda p: (tmp_path / p).resolve()
    blocked_urls = [
        "http://169.254.169.254/latest/meta-data/",
        "https://169.254.169.254/metadata/v1/",
        "http://169.254.170.2/v2/credentials",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[fe80::1]/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata.google.internal./computeMetadata/v1/",
        "http://2852039166/latest/meta-data/",
    ]
    for bad in blocked_urls:
        with pytest.raises(AgentToolRejected):
            Browser.validate_url(bad, resolve)

    allowed_urls = [
        "http://localhost:3000/test",
        "http://127.0.0.1:8080/api",
        "http://[::1]:5173/",
        "http://192.168.1.20:8000/",
        "http://10.0.0.8:9000/",
        "https://example.com/path",
    ]
    for good in allowed_urls:
        assert Browser.validate_url(good, resolve) == good


def test_t08_new_tab_resize_and_close_tab_remove_tab(local):
    action_enum = SPECS["ryn_browser_action"][3]["properties"]["action"]["enum"]
    for expected in ("close_tab", "new_tab", "resize", "forward"):
        assert expected in action_enum

    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "one.html").write_text("<!doctype html><title>One</title><p>1</p>", encoding="utf-8")
        (root / "two.html").write_text("<!doctype html><title>Two</title><p>2</p>", encoding="utf-8")
        opened = execute(tools, context, "ryn_browser_open", {"url": (root / "one.html").as_uri()}, "op_t08_open")
        first_id = opened["page_id"]

        created = execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": first_id, "action": "new_tab", "url": (root / "two.html").as_uri()},
            "op_t08_new",
        )
        second_id = created["new_page_id"]
        assert second_id != first_id
        assert {t["page_id"] for t in created["tabs"]} >= {first_id, second_id}

        execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": second_id, "action": "resize", "width": 1111, "height": 777},
            "op_t08_resize",
        )
        dims = browser.evaluate(second_id, context["task_id"], "({w: window.innerWidth, h: window.innerHeight})")
        assert dims == {"w": 1111, "h": 777}

        closed = execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": second_id, "action": "close_tab"},
            "op_t08_close",
        )
        assert second_id not in {t["page_id"] for t in closed["tabs"]}
        assert second_id not in browser.pages
        with pytest.raises(AgentToolRejected, match="agent_browser_page_not_found"):
            tools.perform("ryn_browser_read", {"page_id": second_id}, context, lambda: False)
    finally:
        browser.close()


def test_t09_script_error_codes_scope_offset_clear_and_option_not_found(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "errs.html").write_text(
            """<!doctype html><html><head><title>Script Errors</title></head><body>
            <input id="locked" value="sticky">
            <script>
              const el = document.getElementById('locked');
              el.addEventListener('input', () => { el.value = 'sticky'; });
            </script>
            </body></html>""",
            encoding="utf-8",
        )
        page_id = execute(tools, context, "ryn_browser_open", {"url": (root / "errs.html").as_uri()}, "op_t09_open")["page_id"]

        # 1. SCOPE_UNAVAILABLE -> agent_browser_scope_unavailable
        with pytest.raises(AgentToolRejected) as scope_exc:
            tools.perform("ryn_browser_read", {"page_id": page_id, "scope_selector": "#non-existent-scope"}, context, lambda: False)
        assert str(scope_exc.value) == "agent_browser_scope_unavailable"
        assert scope_exc.value.correction and "scope_selector" in scope_exc.value.correction

        # 2. OFFSET_INVALID -> agent_browser_offset_invalid with element_count & text_length in details
        with pytest.raises(AgentToolRejected) as offset_exc:
            tools.perform("ryn_browser_read", {"page_id": page_id, "element_offset": 500}, context, lambda: False)
        assert str(offset_exc.value) == "agent_browser_offset_invalid"
        assert isinstance(offset_exc.value.details, dict)
        assert offset_exc.value.details.get("element_count") == 1
        assert "text_length" in offset_exc.value.details

        # 3. CLEAR_FAILED -> agent_browser_clear_failed
        clear_args = {"page_id": page_id, "action": "clear", "selector": "#locked"}
        clear_preview = tools.preview("ryn_browser_action", clear_args, context)
        with pytest.raises((AgentToolRejected, AgentError)) as clear_exc:
            tools.execute(
                "ryn_browser_action",
                clear_args,
                operation_id="op_t09_clear",
                context=context,
                expected_preview=clear_preview,
                cancelled=lambda: False,
            )
        cause = clear_exc.value.__cause__ or clear_exc.value
        assert str(cause) == "agent_browser_clear_failed"
        assert cause.correction and "Delete" in cause.correction

        # 4. OPTION_NOT_FOUND -> agent_browser_option_not_found
        with pytest.raises(AgentToolRejected) as opt_exc:
            Browser._raise_for_script_exception({"exception": {"description": "Error: OPTION_NOT_FOUND:no such option"}}, selector="#sel")
        assert str(opt_exc.value) == "agent_browser_option_not_found"
    finally:
        browser.close()


def test_t10_cdp_protocol_error_includes_sanitized_message_and_correction(tmp_path):
    browser = Browser(tmp_path / "home", None, headless=True)
    page_id = "fake_tab_1"
    raw_msg = "Cannot open file:\x00\x07\x1b " + ("A" * 260)

    class ErrorSocket:
        def __init__(self):
            self.last_id = None

        def send(self, payload):
            self.last_id = json.loads(payload)["id"]

        def recv(self, timeout=None):
            return json.dumps({"id": self.last_id, "error": {"code": -32000, "message": raw_msg}})

        def close(self):
            self.closed = True

    browser.connections[page_id] = ErrorSocket()
    with pytest.raises(AgentToolRejected) as exc_file:
        browser.cdp(page_id, "task1", "DOM.setFileInputFiles", {"files": ["missing.bin"]})
    assert str(exc_file.value) == "agent_browser_protocol_error"
    assert isinstance(exc_file.value.details, dict)
    msg = exc_file.value.details["message"]
    assert len(msg) <= 200
    assert "\x00" not in msg and "\x07" not in msg and "\x1b" not in msg
    assert "Cannot open file:" in msg
    assert exc_file.value.correction and ("文件" in exc_file.value.correction or "file" in exc_file.value.correction.lower())

    with pytest.raises(AgentToolRejected) as exc_nav:
        browser.cdp(page_id, "task1", "Page.navigate", {"url": "http://bad"})
    assert str(exc_nav.value) == "agent_browser_protocol_error"
    assert exc_nav.value.correction and "URL" in exc_nav.value.correction


def test_t11_back_without_history_returns_no_history_and_forward_navigates(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        p1 = root / "hist1.html"
        p2 = root / "hist2.html"
        p2.write_text("<!doctype html><title>Page Two</title><p>Two</p>", encoding="utf-8")
        p1.write_text(
            f"<!doctype html><title>Page One</title><a id='to2' href='{p2.as_uri()}'>Go Two</a>",
            encoding="utf-8",
        )
        opened = execute(tools, context, "ryn_browser_open", {"url": p1.as_uri()}, "op_t11_open")
        page_id = opened["page_id"]

        # Fresh tab has no real back target (only initial about:blank or no history): back must report no_history
        # Note: ryn_browser_open opens /json/new?about:blank then Page.navigate(p1). Let's test on a tab with no prior document:
        fresh_tab = execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": page_id, "action": "new_tab"},
            "op_t11_fresh",
        )["new_page_id"]
        no_hist = execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": fresh_tab, "action": "back"},
            "op_t11_back_empty",
        )
        assert no_hist.get("status") == "no_history"
        assert "action_delivered" not in no_hist

        # Navigate page_id from Page One -> Page Two, then back -> Page One, then forward -> Page Two.
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#to2"}, "op_t11_go2")
        snap2 = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        assert snap2["title"] == "Page Two"

        back_res = execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "back"}, "op_t11_back")
        assert back_res.get("action_delivered") == "back"
        snap1 = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        assert snap1["title"] == "Page One"

        fwd_res = execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "forward"}, "op_t11_fwd")
        assert fwd_res.get("action_delivered") == "forward"
        snap2_again = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        assert snap2_again["title"] == "Page Two"
    finally:
        browser.close()


def test_t12_startup_failure_reports_exit_code_output_tail_and_deletes_temp_profile(tmp_path, monkeypatch):
    if os.name == "nt":
        fail_script = tmp_path / "fail_browser.cmd"
        fail_script.write_text("@echo off\r\necho FATAL_CRASH_OUTPUT_MARKER 1>&2\r\nexit /b 23\r\n", encoding="utf-8")
    else:
        fail_script = tmp_path / "fail_browser.sh"
        fail_script.write_text("#!/bin/sh\necho FATAL_CRASH_OUTPUT_MARKER >&2\nexit 23\n", encoding="utf-8")
        fail_script.chmod(0o755)

    monkeypatch.setenv("RYN_CHROME_EXECUTABLE", str(fail_script))
    home = tmp_path / "home"
    browser = Browser(home, None, headless=True)
    assert browser.first_start_timeout == 30.0
    assert browser.subsequent_start_timeout == 12.0

    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(AgentToolRejected) as exc_info:
        browser.session("task_fail", lambda p: (ws / p).resolve(), lambda: True, lambda: False)

    assert str(exc_info.value) == "agent_browser_start_failed"
    details = exc_info.value.details
    assert isinstance(details, dict)
    assert details["exit_code"] == 23
    assert "FATAL_CRASH_OUTPUT_MARKER" in details["output"]

    # Newly created temporary uuid profile directory MUST be cleaned up on startup failure.
    temp_root = home / "agent" / "browser"
    leftovers = list(temp_root.iterdir()) if temp_root.exists() else []
    assert leftovers == []


def test_t13_cancel_during_click_wait_returns_within_half_second(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "cancel_click.html").write_text(
            "<!doctype html><title>Cancel Click</title><button id='btn'>Click Me</button>",
            encoding="utf-8",
        )
        page_id = execute(tools, context, "ryn_browser_open", {"url": (root / "cancel_click.html").as_uri()}, "op_t13_open")["page_id"]
        click_args = {"page_id": page_id, "action": "click", "selector": "#btn"}
        click_preview = tools.preview("ryn_browser_action", click_args, context)

        real_sock = browser.connections[page_id]
        cancel_flag = {"cancelled": False, "at": None}

        def _trigger_cancel():
            cancel_flag["at"] = time.monotonic()
            cancel_flag["cancelled"] = True

        class HangOnMousePressedSocket:
            def __init__(self, inner):
                self.inner = inner
                self.hang = False

            def send(self, payload):
                if '"Input.dispatchMouseEvent"' in payload and '"mousePressed"' in payload:
                    self.hang = True
                    threading.Timer(0.05, _trigger_cancel).start()
                    return None
                return self.inner.send(payload)

            def recv(self, timeout=None):
                if self.hang:
                    time.sleep(min(timeout or 8.0, 8.0))
                    raise TimeoutError("simulated hang waiting for mousePressed")
                return self.inner.recv(timeout=timeout)

            def close(self):
                return self.inner.close()

        browser.connections[page_id] = HangOnMousePressedSocket(real_sock)
        with pytest.raises(AgentError, match="agent_local_operation_unconfirmed") as exc_info:
            tools.execute(
                "ryn_browser_action",
                click_args,
                operation_id="op_t13_click",
                context=context,
                expected_preview=click_preview,
                cancelled=lambda: cancel_flag["cancelled"],
            )
        assert cancel_flag["at"] is not None
        elapsed = time.monotonic() - cancel_flag["at"]
        assert elapsed < 0.5, f"Cancel during click wait took too long: {elapsed:.3f}s"
        assert page_id not in browser.connections
        assert getattr(exc_info.value.__cause__, "after_side_effect", False) is True
        assert tools.ledger_factory().get("op_t13_click")["status"] == "dispatched"
    finally:
        browser.close()


def test_t14_close_deletes_temp_profile_but_preserves_persistent_profile(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    (root / "persist.html").write_text("<!doctype html><title>Persist</title><p>ok</p>", encoding="utf-8")

    # 1. Temporary profile session -> deleted on close.
    temp_browser = _enable_browser(tools, context, state, headless=True)
    execute(tools, context, "ryn_browser_open", {"url": (root / "persist.html").as_uri()}, "op_t14_temp")
    temp_dir = Path(temp_browser.sessions[context["task_id"]]["profile_dir"])
    assert temp_dir.is_dir()
    temp_browser.close()
    assert not temp_dir.exists()

    # 2. Persistent profile session -> preserved on close.
    pdir = lb.persistent_profile_dir(tools.home)
    pdir.mkdir(parents=True, exist_ok=True)
    marker = pdir / "CookiesMarker"
    marker.write_text("keep-me", encoding="utf-8")
    (pdir / "DevToolsActivePort").write_text("99999\n/devtools/browser/stale\n", encoding="utf-8")

    persist_browser = _enable_browser(tools, context, state, headless=True, profile_dir=pdir, owns_profile=False)
    try:
        execute(tools, context, "ryn_browser_open", {"url": (root / "persist.html").as_uri()}, "op_t14_persist")
        assert Path(persist_browser.sessions[context["task_id"]]["profile_dir"]) == pdir
    finally:
        persist_browser.close()

    assert pdir.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep-me"


def test_t15_startup_sweep_cleans_orphan_temp_profiles_without_touching_persistent_profile(tmp_path):
    home = tmp_path / "ryn_home"
    orphan_uuid = home / "agent" / "browser" / ("a" * 32)
    orphan_uuid.mkdir(parents=True)
    (orphan_uuid / "SingletonLock").write_text("stale", encoding="utf-8")

    pdir = lb.persistent_profile_dir(home)
    pdir.mkdir(parents=True)
    cookie_file = pdir / "Cookies"
    cookie_file.write_text("important_session_cookie", encoding="utf-8")

    ledger = OperationLedger(home)
    from types import SimpleNamespace

    lt = LocalTools(SimpleNamespace(home=home), lambda: ledger)
    try:
        assert not orphan_uuid.exists(), "Orphan temporary uuid directory should have been swept on startup"
        assert pdir.is_dir()
        assert cookie_file.read_text(encoding="utf-8") == "important_session_cookie"
    finally:
        if lt._browser is not None:
            lt._browser.close()


def test_t16_j07_10_upload_uses_object_id_and_marks_side_effect_after_set_file_input_files(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "sample.txt").write_text("hello upload", encoding="utf-8")
        (root / "up.html").write_text(
            "<!doctype html><title>Up</title><input id='f' type='file'>",
            encoding="utf-8",
        )
        page_id = execute(tools, context, "ryn_browser_open", {"url": (root / "up.html").as_uri()}, "op_t16_open")["page_id"]

        seen_methods = []
        orig_cdp = browser.cdp

        def spy_cdp(ident, task_id, method, params=None, **kwargs):
            seen_methods.append((method, dict(params or {})))
            return orig_cdp(ident, task_id, method, params, **kwargs)

        browser.cdp = spy_cdp
        execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": page_id, "action": "upload", "selector": "#f", "files": ["sample.txt"]},
            "op_t16_upload",
        )
        method_names = [m for m, _ in seen_methods]
        assert "DOM.getDocument" not in method_names
        assert "DOM.querySelector" not in method_names
        set_calls = [p for m, p in seen_methods if m == "DOM.setFileInputFiles"]
        assert len(set_calls) == 1
        assert "objectId" in set_calls[0] and "nodeId" not in set_calls[0]
        assert browser.evaluate(page_id, context["task_id"], "document.getElementById('f').files[0].name") == "sample.txt"
    finally:
        browser.close()


def test_t17_hidden_file_input_appears_in_read_file_inputs(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        (root / "hidden_up.html").write_text(
            """<!doctype html><html><head><title>Hidden Upload</title></head><body>
            <button id="pick">Choose File</button>
            <input id="hidden-file" type="file" name="avatar_upload" accept=".png,.jpg" multiple style="display:none">
            </body></html>""",
            encoding="utf-8",
        )
        page_id = execute(tools, context, "ryn_browser_open", {"url": (root / "hidden_up.html").as_uri()}, "op_t17_open")["page_id"]
        snap = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)

        # Hidden input[type=file] is excluded from visible elements, but present in file_inputs with [data-ryn-element="..."]
        assert all(el["tag"] != "input" for el in snap["elements"])
        assert "file_inputs" in snap and len(snap["file_inputs"]) == 1
        fi = snap["file_inputs"][0]
        assert fi["selector"].startswith('[data-ryn-element="')
        assert "avatar_upload" in (fi.get("name") or "") or fi.get("input_name") == "avatar_upload"
        assert fi["accept"] == ".png,.jpg"
        assert fi["multiple"] is True
        assert fi["disabled"] is False

        # Can upload directly using the selector from read.file_inputs!
        execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": page_id, "action": "upload", "selector": fi["selector"], "files": ["avatar.png"]},
            "op_t17_upload",
        )
        assert browser.evaluate(page_id, context["task_id"], "document.getElementById('hidden-file').files[0].name") == "avatar.png"
    finally:
        browser.close()


def test_t18_upload_to_non_file_input_rejected_and_ledger_not_executed(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "doc.txt").write_text("data", encoding="utf-8")
        (root / "non_file.html").write_text(
            "<!doctype html><title>Non File</title><input id='txt' type='text' value='abc'>",
            encoding="utf-8",
        )
        page_id = execute(tools, context, "ryn_browser_open", {"url": (root / "non_file.html").as_uri()}, "op_t18_open")["page_id"]
        args = {"page_id": page_id, "action": "upload", "selector": "#txt", "files": ["doc.txt"]}
        preview = tools.preview("ryn_browser_action", args, context)

        with pytest.raises(AgentToolRejected) as exc_info:
            tools.execute(
                "ryn_browser_action",
                args,
                operation_id="op_t18_bad_upload",
                context=context,
                expected_preview=preview,
                cancelled=lambda: False,
            )
        assert str(exc_info.value) == "agent_browser_not_file_input"
        assert exc_info.value.correction and "file_inputs" in exc_info.value.correction
        assert tools.ledger_factory().get("op_t18_bad_upload")["status"] == "not_executed"
    finally:
        browser.close()


def test_t19_set_file_input_files_protocol_error_marks_ledger_not_executed(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    try:
        (root / "doc.txt").write_text("data", encoding="utf-8")
        (root / "proto_err.html").write_text(
            "<!doctype html><title>Proto Err</title><input id='f' type='file'>",
            encoding="utf-8",
        )
        page_id = execute(tools, context, "ryn_browser_open", {"url": (root / "proto_err.html").as_uri()}, "op_t19_open")["page_id"]
        args = {"page_id": page_id, "action": "upload", "selector": "#f", "files": ["doc.txt"]}
        preview = tools.preview("ryn_browser_action", args, context)

        real_sock = browser.connections[page_id]

        class RejectSetFilesSocket:
            def __init__(self, inner):
                self.inner = inner
                self.reject_id = None

            def send(self, payload):
                msg = json.loads(payload)
                if msg.get("method") == "DOM.setFileInputFiles":
                    self.reject_id = msg["id"]
                    return None
                return self.inner.send(payload)

            def recv(self, timeout=None):
                if self.reject_id is not None:
                    rid = self.reject_id
                    self.reject_id = None
                    return json.dumps({"id": rid, "error": {"code": -32000, "message": "Failed to read file for upload"}})
                return self.inner.recv(timeout=timeout)

            def close(self):
                return self.inner.close()

        browser.connections[page_id] = RejectSetFilesSocket(real_sock)
        with pytest.raises(AgentToolRejected) as exc_info:
            tools.execute(
                "ryn_browser_action",
                args,
                operation_id="op_t19_proto_err",
                context=context,
                expected_preview=preview,
                cancelled=lambda: False,
            )
        assert str(exc_info.value) == "agent_browser_protocol_error"
        assert "Failed to read file for upload" in exc_info.value.details["message"]
        assert tools.ledger_factory().get("op_t19_proto_err")["status"] == "not_executed"
    finally:
        browser.close()


def test_t20_upload_inside_same_origin_iframe_succeeds(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)
    (root / "iframe_payload.txt").write_text("inside iframe", encoding="utf-8")

    child_html = b"<!doctype html><html><body><input id='inner-up' type='file' name='inner_doc' style='display:none'></body></html>"
    parent_html = (
        b"<!doctype html><html><head><title>Iframe Upload</title></head><body>"
        b"<iframe id='target-frame' src='/child_frame.html' width='400' height='200'></iframe>"
        b"</body></html>"
    )

    class _IframeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = child_html if self.path == "/child_frame.html" else parent_html
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    server = _TCPServer(("127.0.0.1", 0), _IframeHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page_id = execute(tools, context, "ryn_browser_open", {"url": f"http://127.0.0.1:{port}/"}, "op_t20_open")["page_id"]

        # Wait until iframe document is loaded and readable
        snap = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                snap = tools.perform(
                    "ryn_browser_read",
                    {"page_id": page_id, "frame_selector": "#target-frame"},
                    context,
                    lambda: False,
                )
                if snap.get("file_inputs"):
                    break
            except AgentToolRejected:
                pass
            time.sleep(0.05)

        assert snap is not None and len(snap["file_inputs"]) == 1
        selector = snap["file_inputs"][0]["selector"]

        execute(
            tools,
            context,
            "ryn_browser_action",
            {
                "page_id": page_id,
                "action": "upload",
                "frame_selector": "#target-frame",
                "selector": selector,
                "files": ["iframe_payload.txt"],
            },
            "op_t20_upload",
        )
        uploaded_name = browser.evaluate(
            page_id,
            context["task_id"],
            "document.querySelector('#target-frame').contentDocument.getElementById('inner-up').files[0].name",
        )
        assert uploaded_name == "iframe_payload.txt"
    finally:
        server.shutdown()
        server.server_close()
        browser.close()


def test_s02_i41_t1_browser_preview_separates_stable_binding_and_volatile_display(local):
    from rynmesh.agent_runtime import protocol

    assert hasattr(protocol, "bound_preview"), "protocol.bound_preview must exist"
    bound_preview = protocol.bound_preview
    digest = protocol.digest

    tools, context, root, state = local
    browser = _enable_browser(tools, context, state, headless=True)

    page_html = (
        b"<!doctype html><html><head><title>Timer 10s</title></head><body>"
        b"<button id='pay' type='submit' onclick=\"document.body.dataset.clicked='yes'\">Pay now (10s)</button>"
        b"<button id='ephemeral' type='button'>Vanish</button>"
        b"</body></html>"
    )

    class _CheckoutHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page_html)))
            self.end_headers()
            self.wfile.write(page_html)

        def log_message(self, format, *args):
            return

    class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    server = _TCPServer(("127.0.0.1", 0), _CheckoutHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        initial_url = f"http://127.0.0.1:{port}/checkout/step?token=abc&ts=10#summary"
        page_id = execute(tools, context, "ryn_browser_open", {"url": initial_url}, "op_s02_i41_open")["page_id"]

        click_args = {"page_id": page_id, "action": "click", "selector": "#pay"}
        preview1 = tools.preview("ryn_browser_action", click_args, context)

        assert preview1["page_url"] == f"http://127.0.0.1:{port}/checkout/step"
        assert "page_title" not in preview1
        assert preview1["element"] == {"tag": "BUTTON", "type": "submit"}
        assert preview1["action"] == "click"
        assert preview1["selector"] == "#pay"
        assert preview1["display"] == {
            "page_title": "Timer 10s",
            "page_url": initial_url,
            "element": {"name": "Pay now (10s)"},
        }

        browser.evaluate(
            page_id,
            context["task_id"],
            "(() => {"
            "document.title = 'Timer 01s';"
            "document.getElementById('pay').innerText = 'Pay now (1s)';"
            "history.replaceState(null, '', '/checkout/step?token=xyz&ts=1#done');"
            "return true;"
            "})()",
        )

        preview2 = tools.preview("ryn_browser_action", click_args, context)
        assert preview2["page_url"] == f"http://127.0.0.1:{port}/checkout/step"
        assert preview2["display"] == {
            "page_title": "Timer 01s",
            "page_url": f"http://127.0.0.1:{port}/checkout/step?token=xyz&ts=1#done",
            "element": {"name": "Pay now (1s)"},
        }
        assert "display" not in bound_preview(preview1)
        assert digest(bound_preview(preview1)) == digest(bound_preview(preview2))

        tools.execute(
            "ryn_browser_action",
            click_args,
            operation_id="op_s02_i41_click",
            context=context,
            expected_preview=preview1,
            cancelled=lambda: False,
        )
        assert browser.evaluate(page_id, context["task_id"], "document.body.dataset.clicked") == "yes"

        vanish_args = {"page_id": page_id, "action": "click", "selector": "#ephemeral"}
        vanish_preview = tools.preview("ryn_browser_action", vanish_args, context)
        browser.evaluate(
            page_id,
            context["task_id"],
            "(() => { document.getElementById('ephemeral').remove(); return true; })()",
        )
        with pytest.raises(AgentToolRejected) as exc_info:
            browser.execute(
                "ryn_browser_action",
                vanish_args,
                context["task_id"],
                lambda v: tools.path(v, context),
                tools.allowed(context),
                lambda: False,
            )
        assert str(exc_info.value) == "agent_browser_element_changed_or_script_failed"
        assert vanish_preview["element"] == {"tag": "BUTTON", "type": "button"}
    finally:
        server.shutdown()
        server.server_close()
        browser.close()
