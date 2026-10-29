"""Tests for Codex-aligned Chrome & Browser control (`oai_browser_use`) in Ryn (`ryn_code_exec`)."""
from __future__ import annotations

import copy
import http.server
import os
import socketserver
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import code_mode, local_browser
from rynmesh.agent_runtime.chrome_runtime import external_runtime_available
from rynmesh.agent_runtime.code_mode import SPEC as CODE_MODE_SPEC
from rynmesh.agent_runtime.local_specs import SPECS as LOCAL_SPECS
from rynmesh.agent_runtime.local_tools import LocalTools
from rynmesh.agent_runtime.operation_ledger import OperationLedger
from rynmesh.agent_runtime.prompt import SYSTEM
from rynmesh.agent_runtime.store import AgentStore, AgentToolRejected


def _make_host_tools(tmp_path: Path, *, headless: bool = True):
    home = tmp_path / "home"
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    agent_store = AgentStore(home / "agent", X25519PrivateKey.generate())
    agent_store.transaction(lambda data: data["settings"].update(enabled=True, workspace_root=str(ws)))
    ledger = OperationLedger(home)
    node_store = SimpleNamespace(home=home)
    local = LocalTools(node_store, lambda: ledger)
    local.store = agent_store
    local._browser = local_browser.Browser(local.home, local.processes, headless=headless)

    class _HostTools:
        def __init__(self):
            self.local = local
            self.runner = code_mode.CodeModeRunner(self)

        def specs(self):
            return [
                {"type": "function", "function": {"name": k, "description": v[0], "parameters": copy.deepcopy(v[3])}}
                for k, v in LOCAL_SPECS.items()
            ] + [copy.deepcopy(CODE_MODE_SPEC)]

        def metadata(self, name: str):
            if name == code_mode.NAME:
                return {"effect": "read", "title": "Execute CodeMode JS", "scope_context_supported": True}
            desc, eff, risk, _ = LOCAL_SPECS[name]
            return {"effect": eff, "title": desc, "risk": risk, "scope_context_supported": True}

        def execute(self, name: str, arguments: dict, *, operation_id: str, cancelled, context=None, expected_preview=None):
            if name == code_mode.NAME:
                return self.runner.execute(arguments, operation_id=operation_id, cancelled=cancelled, context=context)
            return self.local.execute(
                name,
                arguments,
                operation_id=operation_id,
                context=context,
                expected_preview=expected_preview,
                cancelled=cancelled,
            )

    return _HostTools(), ws


def test_find_browser_prioritizes_chrome_over_edge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pf_x86 = tmp_path / "pf_x86"
    pf = tmp_path / "pf"
    edge_exe = pf_x86 / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    chrome_exe = pf / "Google" / "Chrome" / "Application" / "chrome.exe"
    edge_exe.parent.mkdir(parents=True, exist_ok=True)
    chrome_exe.parent.mkdir(parents=True, exist_ok=True)
    edge_exe.write_bytes(b"MZ")
    chrome_exe.write_bytes(b"MZ")

    monkeypatch.setattr(local_browser.shutil, "which", lambda _name: None)
    monkeypatch.setattr(local_browser, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(pf_x86))
    monkeypatch.setenv("PROGRAMFILES", str(pf))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))

    found = local_browser.find_browser()
    assert found == str(chrome_exe)


def test_system_prompt_instructs_codex_chrome_control_not_uia():
    assert "setupBrowserRuntime()" in SYSTEM
    assert "agent.browsers.get('chrome')" in SYSTEM
    assert "WeChat, WeCom/WXWork, Chrome, Zoom" not in SYSTEM


def test_code_exec_browser_runtime_requires_desktop_access(tmp_path: Path):
    host, ws = _make_host_tools(tmp_path)
    try:
        ctx = {
            "task_id": "task_no_desktop",
            "context_epoch": 0,
            "scope_revision": 1,
            "local_execution": {"workspace_root": str(ws), "desktop_access": False},
        }
        with pytest.raises(AgentToolRejected) as exc_info:
            host.execute(
                "ryn_code_exec",
                {"code": "await setupBrowserRuntime();"},
                operation_id="op_no_desktop_1",
                cancelled=lambda: False,
                context=ctx,
            )
        assert str(exc_info.value) == "agent_desktop_access_required"
    finally:
        host.runner.close()


@pytest.mark.skipif(os.environ.get("RYNMESH_TEST_EXTERNAL_BROWSER_RUNTIME") != "1" or not external_runtime_available(),
                    reason="requires explicit external-adapter/browser acceptance")
def test_codex_chrome_control_multi_turn_workflow_via_cdp_pipe_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    if not local_browser.find_browser():
        pytest.skip("No local Chrome/Chromium browser installed on this host")

    monkeypatch.setenv("RYN_CHROME_FORCE_CDP_FALLBACK", "1")
    monkeypatch.setenv("RYN_CHROME_HEADLESS", "1")

    html_body = """<!doctype html>
<html>
<head><title>Ryn Codex Browser Runtime Test</title></head>
<body>
  <h1>Ryn Codex Chrome Control</h1>
  <button id="action-btn" onclick="document.body.dataset.clicked = 'clicked-ok'; this.textContent = 'Done!'">Submit Order</button>
  <input id="query-input" aria-label="Order Note" value="initial-note" />
</body>
</html>
"""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = html_body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as httpd:
        port = httpd.server_address[1]
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        test_url = f"http://127.0.0.1:{port}/"

        host, ws = _make_host_tools(tmp_path, headless=True)
        ctx = {
            "task_id": "task_codex_chrome_e2e",
            "context_epoch": 0,
            "scope_revision": 1,
            "local_execution": {"workspace_root": str(ws), "desktop_access": True},
            "_deferred_observed_files": {},
            "_observed_files_snapshot": {},
        }
        try:
            # Turn 1: setupBrowserRuntime(), agent.browsers.get("chrome"), documentation, openTabs()
            cell_1 = """// @exec: {"yield_time_ms": 25000}
await setupBrowserRuntime();
const chrome = await agent.browsers.get("chrome");
const doc = await chrome.documentation();
const axDoc = await _h.read("accessibility");
const openTabs = await chrome.user.openTabs();
return {
  hasDoc: doc.includes("chrome"),
  hasAxDoc: axDoc.includes("tab.ax"),
  openCount: openTabs.length,
};
"""
            res1 = host.execute(
                "ryn_code_exec",
                {"code": cell_1},
                operation_id="op_chrome_turn_1",
                cancelled=lambda: False,
                context=ctx,
            )
            assert res1["status"] == "completed", res1
            assert res1["return_value"]["hasDoc"] is True
            assert res1["return_value"]["hasAxDoc"] is True
            assert res1["return_value"]["openCount"] >= 1

            # Turn 2: Reuses `chrome` and `openTabs` from Turn 1, claims tab, navigates, writes AX state
            cell_2 = f"""// @exec: {{"yield_time_ms": 20000}}
const tab = await chrome.user.claimTab(openTabs[0].id);
await tab.goto({test_url!r});
await tab.ax.write("state");
return {{
  title: await tab.title(),
  url: await tab.url(),
}};
"""
            res2 = host.execute(
                "ryn_code_exec",
                {"code": cell_2},
                operation_id="op_chrome_turn_2",
                cancelled=lambda: False,
                context=ctx,
            )
            assert res2["status"] == "completed", res2
            assert res2["return_value"]["title"] == "Ryn Codex Browser Runtime Test"
            assert "Submit Order" in res2["output"]
            assert "Order Note" in res2["output"]

            # Turn 3: Reuses `tab` from Turn 2, interacts via tab.ax.click & tab.ax.setValue,
            # verifies via tab.playwright.evaluate & tab.capabilities.get("cdp"), and emits screenshot
            cell_3 = """// @exec: {"yield_time_ms": 20000}
const stateText = await tab.ax.get("state", { disableDiffing: true });
const btnMatch = /\\b(\\d+)\\s+button\\s+Submit Order/i.exec(stateText);
const inpMatch = /\\b(\\d+)\\s+text field[^\\n]*Order Note/i.exec(stateText);
if (!btnMatch || !inpMatch) {
  throw new Error("Could not find button/input indices in AX state:\\n" + stateText);
}
await tab.ax.click(Number(btnMatch[1]));
await tab.ax.setValue(Number(inpMatch[1]), "updated-via-codex-ax");
await tab.ax.write("screenshot");

const clickedAttr = await tab.playwright.evaluate("document.body.dataset.clicked");
const inputVal = await tab.playwright.evaluate("document.getElementById('query-input').value");
const cdp = await tab.capabilities.get("cdp");
await cdp.documentation();
const cdpEval = await cdp.send("Runtime.evaluate", {
  expression: "document.getElementById('action-btn').textContent",
  returnByValue: true,
});
return {
  clickedAttr,
  inputVal,
  btnText: cdpEval.result.value,
};
"""
            res3 = host.execute(
                "ryn_code_exec",
                {"code": cell_3},
                operation_id="op_chrome_turn_3",
                cancelled=lambda: False,
                context=ctx,
            )
            assert res3["status"] == "completed", res3
            assert res3["return_value"] == {
                "clickedAttr": "clicked-ok",
                "inputVal": "updated-via-codex-ax",
                "btnText": "Done!",
            }
            assert len(res3.get("image_artifacts") or []) == 1
            art = res3["image_artifacts"][0]
            assert art["mime_type"] in {"image/png", "image/jpeg"}
            assert (ws / art["path"]).is_file()
        finally:
            host.runner.close()
            httpd.shutdown()


@pytest.mark.skipif(os.environ.get("RYNMESH_TEST_EXTERNAL_BROWSER_RUNTIME") != "1" or not external_runtime_available(),
                    reason="requires explicit external-adapter/browser acceptance")
def test_codex_chrome_control_live_extension_or_auto_fallback_discovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    if not local_browser.find_browser():
        pytest.skip("No local Chrome/Chromium browser installed on this host")

    monkeypatch.delenv("RYN_CHROME_FORCE_CDP_FALLBACK", raising=False)
    monkeypatch.setenv("RYN_CHROME_HEADLESS", "1")

    host, ws = _make_host_tools(tmp_path, headless=True)
    ctx = {
        "task_id": "task_codex_chrome_auto",
        "context_epoch": 0,
        "scope_revision": 1,
        "local_execution": {"workspace_root": str(ws), "desktop_access": True},
    }
    try:
        res = host.execute(
            "ryn_code_exec",
            {
                "code": """// @exec: {"yield_time_ms": 25000}
await setupBrowserRuntime();
const browsers = await agent.browsers.list();
const chrome = await agent.browsers.get("chrome");
await chrome.documentation();
const tabs = await chrome.user.openTabs();
return {
  browserCount: browsers.length,
  family: browsers[0].family,
  type: browsers[0].type,
  tabCount: tabs.length,
};
"""
            },
            operation_id="op_chrome_auto_1",
            cancelled=lambda: False,
            context=ctx,
        )
        assert res["status"] == "completed", res
        assert res["return_value"]["browserCount"] >= 1
        assert res["return_value"]["family"] == "chrome"
        assert res["return_value"]["type"] in {"extension", "cdp"}
        assert res["return_value"]["tabCount"] >= 1
    finally:
        host.runner.close()


def test_missing_external_browser_adapter_preserves_code_and_tools(monkeypatch, tmp_path):
    monkeypatch.delenv("RYN_CHROME_RUNTIME_DIR", raising=False)
    host, ws = _make_host_tools(tmp_path)
    ctx = {"task_id": "optional-adapter", "context_epoch": 0, "scope_revision": 1,
           "local_execution": {"workspace_root": str(ws), "desktop_access": True}}
    try:
        with pytest.raises(AgentToolRejected, match="agent_browser_adapter_unavailable"):
            host.execute("ryn_code_exec", {"code": "await setupBrowserRuntime();"},
                         operation_id="missing-adapter", cancelled=lambda: False, context=ctx)
        result = host.execute("ryn_code_exec", {"code": "return {answer: 2 + 3, tools: ALL_TOOLS.length};"},
                              operation_id="normal-code", cancelled=lambda: False, context=ctx)
        assert result["status"] == "completed"
        assert result["return_value"]["answer"] == 5
        assert result["return_value"]["tools"] > 0
    finally:
        host.runner.close()
