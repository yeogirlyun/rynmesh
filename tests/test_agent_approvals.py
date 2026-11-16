"""Tests for S02-I41: stable browser approval preview binding vs volatile display fields."""
from __future__ import annotations

import copy
import http.server
import json
import socketserver
import threading
import time

from test_agent_integrations import add_source, call, fake_upstream
from test_agent_integrations import node as node  # noqa: F401
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import protocol
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.local_browser import Browser
from rynmesh.agent_runtime.operation_permissions import fingerprint, projection
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.services import peer_box


def _start_html_server(html_bytes: bytes):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_bytes)))
            self.end_headers()
            self.wfile.write(html_bytes)

        def log_message(self, format, *args):
            return

    class _TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    server = _TCPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_until(runtime: AgentRuntime, task_id: str, statuses: set[str], max_steps: int = 20) -> dict:
    current = runtime.get(task_id)
    for _ in range(max_steps):
        if current["status"] in statuses:
            return current
        runtime.run_once()
        current = runtime.get(task_id)
    return current


def test_s02_i41_t2_confirm_mode_approval_executes_after_title_and_element_text_change(node, monkeypatch, tmp_path):
    assert hasattr(protocol, "bound_preview"), "protocol.bound_preview must exist"
    bound_preview = protocol.bound_preview
    digest = protocol.digest

    page_html = (
        b"<!doctype html><html><head><title>Countdown 10s</title></head><body>"
        b"<button id='pay' type='submit' onclick=\"document.body.dataset.paid='1'\">Pay (10s)</button>"
        b"</body></html>"
    )
    server = _start_html_server(page_html)
    port = server.server_address[1]
    initial_url = f"http://127.0.0.1:{port}/checkout/step?token=abc&ts=10#summary"

    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    browser = Browser(tools.local.home, tools.local.processes, headless=True)
    tools.local._browser = browser
    runtime = AgentRuntime(
        AgentStore(
            owner_store.home / "s02-i41-t2-agent",
            peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519"),
        ),
        bridge,
        tools,
        catalog,
    )
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": model_id,
            "workspace_root": str(tmp_path),
            "desktop_access": True,
            "permission_mode": "confirm_changes",
            "verify_text_responses": False,
        }
    )

    page_holder: dict[str, str] = {}

    def click_after_open(request):
        tool_msg = next(m for m in reversed(request["messages"]) if m.get("role") == "tool")
        open_result = json.loads(tool_msg["content"])
        page_holder["page_id"] = open_result["page_id"]
        return call(
            "ryn_browser_action",
            json.dumps({"page_id": open_result["page_id"], "action": "click", "selector": "#pay"}),
            "click_pay",
        )

    fake_upstream(
        monkeypatch,
        [
            call("ryn_discover_tools", json.dumps({"load": ["ryn_browser_open", "ryn_browser_action"]}), "discover"),
            call("ryn_browser_open", json.dumps({"url": initial_url}), "open_page"),
            click_after_open,
            {"role": "assistant", "content": "Clicked Pay button."},
        ],
    )

    try:
        task = runtime.create({"message": "Open checkout and click Pay after my approval."})
        waiting_open = _run_until(runtime, task["id"], {"waiting_approval", "failed"})
        assert waiting_open["status"] == "waiting_approval", waiting_open.get("error")
        assert waiting_open["pending_approval"]["tool"] == "ryn_browser_open"
        runtime.action(
            task["id"],
            {
                "action": "approve",
                "expected_revision": waiting_open["revision"],
                "approval_id": waiting_open["pending_approval"]["id"],
            },
        )
        runtime.run_once()

        waiting_click = _run_until(runtime, task["id"], {"waiting_approval", "failed"})
        assert waiting_click["status"] == "waiting_approval", waiting_click.get("error")
        pending = waiting_click["pending_approval"]
        assert pending["tool"] == "ryn_browser_action"
        assert pending["preview"]["page_url"] == f"http://127.0.0.1:{port}/checkout/step"
        assert pending["preview"]["element"] == {"tag": "BUTTON", "type": "submit"}
        assert pending["preview"]["display"] == {
            "page_title": "Countdown 10s",
            "page_url": initial_url,
            "element": {"name": "Pay (10s)"},
        }
        assert pending["preview_sha256"] == digest(bound_preview(pending["preview"]))

        browser.evaluate(
            page_holder["page_id"],
            task["id"],
            "(() => {"
            "document.title = 'Countdown 01s';"
            "document.getElementById('pay').innerText = 'Pay (1s)';"
            "history.replaceState(null, '', '/checkout/step?token=xyz&ts=1#ready');"
            "return true;"
            "})()",
        )

        runtime.action(
            task["id"],
            {
                "action": "approve",
                "expected_revision": waiting_click["revision"],
                "approval_id": pending["id"],
            },
        )
        done = _run_until(runtime, task["id"], {"completed", "failed"})
        assert done["status"] == "completed", done.get("error")
        assert browser.evaluate(page_holder["page_id"], task["id"], "document.body.dataset.paid") == "1"

        raw_row = runtime.store.read()["tasks"][task["id"]]
        facts = projection(raw_row)
        assert any(
            f["tool"] == "ryn_browser_action"
            and f["permission_basis"] == "owner_approved"
            and f["approval_id"] == pending["id"]
            for f in facts
        )
    finally:
        server.shutdown()
        server.server_close()
        browser.close()
        tools.local.processes.close()


def test_s02_i41_t3_origin_selector_or_action_param_change_still_invalidates_approval(node, monkeypatch, tmp_path):
    assert hasattr(protocol, "bound_preview"), "protocol.bound_preview must exist"

    page_html = (
        b"<!doctype html><html><head><title>Origin Check</title></head><body>"
        b"<button id='pay' type='submit' onclick=\"document.body.dataset.paid='1'\">Pay</button>"
        b"</body></html>"
    )
    server_a = _start_html_server(page_html)
    server_b = _start_html_server(page_html)
    port_a = server_a.server_address[1]
    port_b = server_b.server_address[1]

    app, owner_store = node
    _, model_id = add_source(app)
    bridge, tools, catalog = build_agent_integrations(app, owner_store, owner_store.home)
    browser = Browser(tools.local.home, tools.local.processes, headless=True)
    tools.local._browser = browser
    runtime = AgentRuntime(
        AgentStore(
            owner_store.home / "s02-i41-t3-agent",
            peer_box.load_or_create_messaging_key(owner_store.home / "messaging.x25519"),
        ),
        bridge,
        tools,
        catalog,
    )
    runtime.update_settings(
        {
            "enabled": True,
            "model_id": model_id,
            "workspace_root": str(tmp_path),
            "desktop_access": True,
            "permission_mode": "confirm_changes",
            "verify_text_responses": False,
        }
    )

    page_holder: dict[str, str] = {}

    def click_after_open(request):
        tool_msg = next(m for m in reversed(request["messages"]) if m.get("role") == "tool")
        open_result = json.loads(tool_msg["content"])
        page_holder["page_id"] = open_result["page_id"]
        return call(
            "ryn_browser_action",
            json.dumps({"page_id": open_result["page_id"], "action": "click", "selector": "#pay"}),
            "click_pay",
        )

    fake_upstream(
        monkeypatch,
        [
            call("ryn_discover_tools", json.dumps({"load": ["ryn_browser_open", "ryn_browser_action"]}), "discover"),
            call("ryn_browser_open", json.dumps({"url": f"http://127.0.0.1:{port_a}/checkout/step"}), "open_page"),
            click_after_open,
            {"role": "assistant", "content": "Done."},
        ],
    )

    try:
        # 1) Page origin change during waiting_approval -> agent_approval_preview_changed
        task = runtime.create({"message": "Open checkout on server A and click Pay."})
        waiting_open = _run_until(runtime, task["id"], {"waiting_approval", "failed"})
        assert waiting_open["status"] == "waiting_approval"
        runtime.action(
            task["id"],
            {
                "action": "approve",
                "expected_revision": waiting_open["revision"],
                "approval_id": waiting_open["pending_approval"]["id"],
            },
        )
        runtime.run_once()

        waiting_click = _run_until(runtime, task["id"], {"waiting_approval", "failed"})
        assert waiting_click["status"] == "waiting_approval"
        # Navigate same tab to different origin (port_b) with identical path and DOM
        target_b = f"http://127.0.0.1:{port_b}/checkout/step"
        browser.evaluate(
            page_holder["page_id"],
            task["id"],
            f"(() => {{ window.location.href = {json.dumps(target_b)}; return true; }})()",
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            tabs = browser.sync_tabs(task["id"])
            if any(t["page_id"] == page_holder["page_id"] and f":{port_b}/" in t["url"] for t in tabs):
                break
            time.sleep(0.05)

        runtime.action(
            task["id"],
            {
                "action": "approve",
                "expected_revision": waiting_click["revision"],
                "approval_id": waiting_click["pending_approval"]["id"],
            },
        )
        runtime.run_once()
        after_origin_change = runtime.get(task["id"])
        assert any(
            e.get("type") == "tool_result" and (e.get("result") or {}).get("error") == "agent_approval_preview_changed"
            for e in after_origin_change["events"]
        )
        assert runtime.store.read()["tasks"][task["id"]]["approved"] is None
        assert browser.evaluate(page_holder["page_id"], task["id"], "document.body.dataset.paid || ''") == ""
    finally:
        server_a.shutdown()
        server_a.server_close()
        server_b.shutdown()
        server_b.server_close()
        browser.close()
        tools.local.processes.close()

    # 2) Selector change in bound preview -> agent_approval_preview_changed
    rt_sel, _, tools_sel, _ = make_runtime(
        tmp_path / "sel_case",
        assistant(name="share_service", args={"service_id": "A"}),
        assistant("Done"),
    )
    current_selector = {"value": "#pay"}
    tools_sel.preview = lambda name, arguments, **kw: {
        "page_url": "http://127.0.0.1:8080/checkout",
        "action": "click",
        "selector": current_selector["value"],
        "element": {"tag": "BUTTON", "type": "submit"},
        "display": {"page_title": "Checkout", "page_url": "http://127.0.0.1:8080/checkout", "element": {"name": "Pay"}},
    }
    task_sel = rt_sel.create({"message": "Selector binding check"})
    advance(rt_sel, 2)
    assert rt_sel.get(task_sel["id"])["status"] == "waiting_approval"
    current_selector["value"] = "#delete-account"
    approve(rt_sel, task_sel["id"])
    rt_sel.run_once()
    assert rt_sel.get(task_sel["id"])["events"][-1]["result"]["error"] == "agent_approval_preview_changed"
    assert rt_sel.store.read()["tasks"][task_sel["id"]]["approved"] is None
    assert tools_sel.calls == []

    # 3) Action parameter change in bound preview -> agent_approval_preview_changed
    rt_param, _, tools_param, _ = make_runtime(
        tmp_path / "param_case",
        assistant(name="share_service", args={"service_id": "A"}),
        assistant("Done"),
    )
    current_text = {"value": "100"}
    tools_param.preview = lambda name, arguments, **kw: {
        "page_url": "http://127.0.0.1:8080/transfer",
        "action": "fill",
        "selector": "#amount",
        "text": current_text["value"],
        "element": {"tag": "INPUT", "type": "text"},
        "display": {"page_title": "Transfer", "page_url": "http://127.0.0.1:8080/transfer", "element": {"name": "Amount"}},
    }
    task_param = rt_param.create({"message": "Action param binding check"})
    advance(rt_param, 2)
    assert rt_param.get(task_param["id"])["status"] == "waiting_approval"
    current_text["value"] = "99999"
    approve(rt_param, task_param["id"])
    rt_param.run_once()
    assert rt_param.get(task_param["id"])["events"][-1]["result"]["error"] == "agent_approval_preview_changed"
    assert rt_param.store.read()["tasks"][task_param["id"]]["approved"] is None
    assert tools_param.calls == []


def test_s02_i41_t4_legacy_preview_without_display_remains_approvable(tmp_path):
    assert hasattr(protocol, "bound_preview"), "protocol.bound_preview must exist"
    bound_preview = protocol.bound_preview
    digest = protocol.digest

    runtime, _, tools, _ = make_runtime(
        tmp_path / "legacy_case",
        assistant(name="share_service", args={"service_id": "A"}),
        assistant("Approved legacy preview executed."),
    )
    legacy_preview = {
        "page_url": "http://127.0.0.1:8080/legacy?step=1",
        "page_title": "Legacy Flat Preview",
        "element": {"tag": "BUTTON", "type": "submit", "name": "Confirm"},
    }
    assert bound_preview(legacy_preview) == legacy_preview
    assert digest(bound_preview(legacy_preview)) == digest(legacy_preview)

    tools.preview = lambda name, arguments, **kw: copy.deepcopy(legacy_preview)
    task = runtime.create({"message": "Approve legacy preview without display key."})
    advance(runtime, 2)
    pending = runtime.get(task["id"])["pending_approval"]
    assert "display" not in pending["preview"]
    assert pending["preview_sha256"] == digest(legacy_preview)

    approve(runtime, task["id"])
    advance(runtime, 3)
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert len(tools.calls) == 1
    raw_row = runtime.store.read()["tasks"][task["id"]]
    assert raw_row["receipts"][0]["permission_record"]["preview_sha256"] == digest(legacy_preview)


def test_s02_i41_t5_operation_permissions_verifies_new_and_legacy_receipt_preview_sha256(tmp_path):
    assert hasattr(protocol, "bound_preview"), "protocol.bound_preview must exist"
    bound_preview = protocol.bound_preview

    runtime, _, tools, _ = make_runtime(
        tmp_path / "perm_case",
        assistant(name="share_service", args={"service_id": "A"}),
        assistant(name="share_service", args={"service_id": "B"}),
        assistant("Both operations executed."),
    )
    previews_by_service = {
        "A": {
            "page_url": "http://127.0.0.1:8080/checkout",
            "action": "click",
            "selector": "#pay",
            "element": {"tag": "BUTTON", "type": "submit"},
            "display": {
                "page_title": "Countdown 10s",
                "page_url": "http://127.0.0.1:8080/checkout?ts=10#top",
                "element": {"name": "Pay (10s)"},
            },
        },
        "B": {
            "before": {"shared": False},
            "after": {"shared": True},
        },
    }
    tools.preview = lambda name, arguments, **kw: copy.deepcopy(previews_by_service[arguments["service_id"]])

    task = runtime.create({"message": "Verify new display receipt and legacy receipt."})
    _run_until(runtime, task["id"], {"waiting_approval"})
    approve(runtime, task["id"])
    runtime.run_once()
    _run_until(runtime, task["id"], {"waiting_approval"})
    approve(runtime, task["id"])
    _run_until(runtime, task["id"], {"completed", "failed"})

    raw_row = runtime.store.read()["tasks"][task["id"]]
    receipt_new, receipt_legacy = raw_row["receipts"][0], raw_row["receipts"][1]

    # New receipt stores full preview (including display), while preview_sha256 binds bound_preview(preview)
    assert "display" in receipt_new["preview"]
    assert receipt_new["permission_record"]["preview_sha256"] == fingerprint(bound_preview(receipt_new["preview"]))
    assert receipt_new["permission_record"]["preview_sha256"] != fingerprint(receipt_new["preview"])

    # Legacy receipt has no display key; preview_sha256 equals fingerprint(preview) directly
    assert "display" not in receipt_legacy["preview"]
    assert receipt_legacy["permission_record"]["preview_sha256"] == fingerprint(receipt_legacy["preview"])

    facts = projection(raw_row)
    assert len(facts) == 2
    assert facts[0]["permission_basis"] == "owner_approved"
    assert facts[1]["permission_basis"] == "owner_approved"

    # Volatile display change on stored receipt does not break verification, whereas stable field tamper fails closed
    mutated_display_row = copy.deepcopy(raw_row)
    mutated_display_row["receipts"][0]["preview"]["display"]["page_title"] = "Countdown 00s"
    assert projection(mutated_display_row)[0]["permission_basis"] == "owner_approved"

    tampered_stable_row = copy.deepcopy(raw_row)
    tampered_stable_row["receipts"][0]["preview"]["page_url"] = "https://attacker.example/checkout"
    assert projection(tampered_stable_row)[0]["permission_basis"] == "unknown"
