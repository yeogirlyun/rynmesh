"""Actual headless browser observation beyond the original first-page limits."""
import time

import pytest
from test_agent_local_tools import execute
from test_agent_local_tools import local as local

from rynmesh.agent_runtime.local_browser import Browser, find_browser


def test_page_controls_and_text_are_reachable_beyond_first_page(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)
    buttons = "".join(f'<button>Control {i}</button>' for i in range(95))
    (root / "page.html").write_text(buttons + "<p>" + "long text " * 900 + "END_OF_PAGE</p>", encoding="utf-8")
    page = execute(tools, context, "ryn_browser_open", {"url": (root / "page.html").as_uri()})["page_id"]
    until = time.monotonic() + 5
    while time.monotonic() < until:
        first = tools.perform("ryn_browser_read", {"page_id": page}, context, lambda: False)
        if first["elements"]:
            break
        time.sleep(0.05)
    assert first["element_count"] == 95 and first["next_element_offset"] == 40
    second = tools.perform("ryn_browser_read", {"page_id": page, "element_offset": 40}, context, lambda: False)
    assert second["elements"][0]["name"] == "Control 40" and second["next_element_offset"] == 80
    target = tools.perform("ryn_browser_read", {"page_id": page, "query": "Control 94"}, context, lambda: False)
    assert len(target["elements"]) == 1 and target["elements"][0]["name"] == "Control 94"
    tail = tools.perform("ryn_browser_read", {"page_id": page, "text_offset": first["text_length"] - 50}, context, lambda: False)
    assert "END_OF_PAGE" in tail["text"] and tail["next_text_offset"] is None


def _all_local_specs():
    from rynmesh.agent_runtime.local_specs import SPECS
    return [
        {"type": "function", "function": {"name": name, "description": SPECS[name][0], "parameters": SPECS[name][3]}}
        for name in SPECS
    ]


class _RuntimeLocalTools:
    def __init__(self, store, home):
        from types import SimpleNamespace
        from rynmesh.agent_runtime.local_tools import LocalTools
        from rynmesh.agent_runtime.operation_ledger import OperationLedger

        ledger = OperationLedger(home)
        self.local = LocalTools(SimpleNamespace(home=home), lambda: ledger)
        self.local.store = store

    def specs(self):
        return _all_local_specs()

    def metadata(self, name):
        from rynmesh.agent_runtime.local_specs import SPECS

        effect = SPECS[name][1]
        return {"effect": effect, "scope_context_supported": True, "preview_precondition": effect == "write"}

    def preview(self, name, args, *, context):
        return self.local.preview(name, args, context)

    def execute(self, name, args, *, operation_id, cancelled, context, expected_preview=None):
        return self.local.execute(
            name,
            args,
            operation_id=operation_id,
            cancelled=cancelled,
            context=context,
            expected_preview=expected_preview,
        )


def test_existing_task_enabling_desktop_access_exposes_browser_tools_on_next_model_turn(tmp_path):
    from test_agent_runtime import advance, assistant, make_runtime
    from rynmesh.agent_runtime import tool_menu

    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, model, _, _ = make_runtime(
        tmp_path / "agent",
        assistant(name="ryn_file_list", args={"path": "."}),
        assistant("Browser tools are now directly available."),
    )
    runtime.tools = _RuntimeLocalTools(runtime.store, tmp_path / "home")
    runtime.update_settings({"workspace_root": str(workspace), "desktop_access": False})
    task = runtime.create({"message": "List workspace files then open browser."})

    # Run model turn 1 + tool execution so task remains active (queued for turn 2).
    advance(runtime, 2)
    first_turn_tools = {spec["function"]["name"] for spec in model.calls[0][2]}
    assert not (set(tool_menu.BROWSER_TOOLS) & first_turn_tools)

    before = runtime.store.read()["tasks"][task["id"]]
    before_ctrl = before["control_revision"]

    # Enable desktop_access on the existing active task without creating a new task.
    runtime.update_settings({"desktop_access": True})
    updated = runtime.store.read()["tasks"][task["id"]]
    assert updated["local_execution"]["desktop_access"] is True
    assert updated["control_revision"] == before_ctrl + 1
    perm_events = [e for e in updated["events"] if e["type"] == "permissions_changed"]
    assert len(perm_events) == 1

    # Next model request must directly contain all 3 browser tools.
    advance(runtime, 1)
    second_turn_tools = {spec["function"]["name"] for spec in model.calls[1][2]}
    assert set(tool_menu.BROWSER_TOOLS) <= second_turn_tools


def test_disabling_desktop_access_rejects_calls_and_aborts_inflight_wait(local):
    import threading
    from rynmesh.agent_runtime.runtime import write_permission_revoked
    from rynmesh.agent_runtime.store import AgentToolRejected

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = False
    state.transaction(lambda data: data["settings"].update(desktop_access=False))

    # Calling browser tools when desktop_access=False is rejected before dispatch.
    with pytest.raises(AgentToolRejected, match="agent_desktop_access_required"):
        tools.execute(
            "ryn_browser_open",
            {"url": "https://example.com"},
            operation_id="op_disabled_open",
            context=context,
            expected_preview=None,
            cancelled=lambda: False,
        )
    assert tools.ledger_factory().get("op_disabled_open") is None

    # write_permission_revoked must detect revocation for all in-flight browser/computer tools.
    for tool_name in ("ryn_browser_open", "ryn_browser_read", "ryn_browser_action", "ryn_computer_action"):
        assert write_permission_revoked({"in_flight": {"name": tool_name, "effect": "read"}}, {"desktop_access": False}) is True
        assert write_permission_revoked({"in_flight": {"name": tool_name, "effect": "read"}}, {"desktop_access": True}) is False

    # Enable desktop_access, then revoke mid-wait and verify the wait aborts promptly.
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    if find_browser():
        tools._browser = Browser(tools.home, tools.processes, headless=True)
        (root / "wait.html").write_text("<p>Waiting page</p>", encoding="utf-8")
        opened = execute(tools, context, "ryn_browser_open", {"url": (root / "wait.html").as_uri()}, "op_open_wait")
        page_id = opened["page_id"]

        wait_args = {"page_id": page_id, "action": "wait", "selector": "#never-present", "wait_seconds": 5}
        wait_preview = tools.preview("ryn_browser_action", wait_args, context)

        def revoke_soon():
            time.sleep(0.35)
            state.transaction(lambda data: data["settings"].update(desktop_access=False))

        timer = threading.Thread(target=revoke_soon, daemon=True)
        timer.start()
        started = time.monotonic()
        with pytest.raises(AgentToolRejected, match="agent_desktop_access_required"):
            tools.execute(
                "ryn_browser_action",
                wait_args,
                operation_id="op_wait_revoked",
                context=context,
                expected_preview=wait_preview,
                cancelled=lambda: False,
            )
        elapsed = time.monotonic() - started
        timer.join(timeout=1.0)
        assert elapsed < 2.5
        assert tools.ledger_factory().get("op_wait_revoked")["status"] == "not_executed"

        # Subsequent read after revocation is also rejected.
        with pytest.raises(AgentToolRejected, match="agent_desktop_access_required"):
            tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)


def test_completed_task_followup_and_archived_restore_sync_desktop_access(tmp_path):
    from test_agent_runtime import advance, assistant, make_runtime
    from rynmesh.agent_runtime import tool_menu

    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime, model, _, _ = make_runtime(
        tmp_path / "agent",
        assistant("Initial task 1 done."),
        assistant("Initial task 2 done."),
        assistant("Follow-up on completed task sees browser tools."),
    )
    runtime.tools = _RuntimeLocalTools(runtime.store, tmp_path / "home")
    runtime.update_settings({"workspace_root": str(workspace), "desktop_access": False})

    task1 = runtime.create({"message": "Finish task 1."})
    advance(runtime, 1)
    assert runtime.get(task1["id"])["status"] == "completed"

    task2 = runtime.create({"message": "Finish task 2."})
    advance(runtime, 1)
    t2_state = runtime.get(task2["id"])
    assert t2_state["status"] == "completed"

    # Archive task2 while desktop_access is False.
    archived_t2 = runtime.lifecycle(
        task2["id"],
        {"expected_control_revision": runtime.store.read()["tasks"][task2["id"]]["control_revision"]},
        "archive",
    )

    # Enable desktop_access while task1 is completed and task2 is archived.
    runtime.update_settings({"desktop_access": True})

    # Completed task1 must have local_execution.desktop_access synced to True, and continuing chat exposes browser tools.
    t1_row = runtime.store.read()["tasks"][task1["id"]]
    assert t1_row["local_execution"]["desktop_access"] is True
    runtime.add_message(
        task1["id"],
        {
            "message": "Continue task 1 with browser access.",
            "expected_control_revision": t1_row["control_revision"],
            "expected_goal_revision": t1_row["goal"]["revision"],
        },
    )
    advance(runtime, 1)
    followup_tools = {spec["function"]["name"] for spec in model.calls[-1][2]}
    assert set(tool_menu.BROWSER_TOOLS) <= followup_tools

    # Restoring archived task2 must synchronize its local_execution.desktop_access to current settings (True).
    restored = runtime.lifecycle(task2["id"], {"expected_control_revision": archived_t2["control_revision"]}, "restore")
    assert restored["local_execution"]["desktop_access"] is True
    assert runtime.store.read()["tasks"][task2["id"]]["local_execution"]["desktop_access"] is True

    # Archive again, turn desktop_access off, and restore: must sync to False and clear stale approvals.
    t2_ctrl = runtime.store.read()["tasks"][task2["id"]]["control_revision"]
    archived_again = runtime.lifecycle(task2["id"], {"expected_control_revision": t2_ctrl}, "archive")
    runtime.update_settings({"desktop_access": False})
    restored_off = runtime.lifecycle(task2["id"], {"expected_control_revision": archived_again["control_revision"]}, "restore")
    assert restored_off["local_execution"]["desktop_access"] is False


def test_window_budgets_desktop_off_preservation_and_pins_released_lifecycle():
    from rynmesh.agent_runtime import tool_menu
    from rynmesh.agent_runtime.prompt import INTERNAL_SPECS
    from rynmesh.agent_runtime.store import encoded

    business = _all_local_specs()
    base_pins = tool_menu.PLUGIN_DISCOVERY_TOOLS + ("ryn_project_inspect",) + tool_menu.CORE_TOOLS
    desktop_pins = base_pins + tool_menu.BROWSER_TOOLS

    # 1. desktop_access=False preserves core tools without browser tools.
    exposed_off, state_off = tool_menu.project(
        business,
        INTERNAL_SPECS,
        {},
        max_context_bytes=60000,
        authority_bytes=4500,
        epoch=0,
        goal_revision=1,
        pinned_names=base_pins,
    )
    names_off = {s["function"]["name"] for s in exposed_off}
    assert set(tool_menu.CORE_TOOLS) <= names_off
    assert not (set(tool_menu.BROWSER_TOOLS) & names_off)

    # 2. Small window (12 KB) with desktop_access=True respects hard budget and degrades to tool discovery.
    exposed_small, state_small = tool_menu.project(
        business,
        INTERNAL_SPECS,
        {},
        max_context_bytes=12000,
        authority_bytes=1000,
        epoch=0,
        goal_revision=1,
        pinned_names=desktop_pins,
        initial_names=("ryn_shell_execute", "ryn_file_read", "ryn_file_search") + tool_menu.BROWSER_TOOLS,
    )
    assert len(encoded(exposed_small)) <= tool_menu.capacity(12000, 1000)
    assert not state_small.get("pinned_names")
    assert tool_menu.NAME in {s["function"]["name"] for s in exposed_small}

    # 3. Moderate (60 KB) and large (120 KB) windows with desktop_access=True keep CORE_TOOLS + BROWSER_TOOLS pinned.
    for window_bytes in (60000, 120000):
        exposed_on, state_on = tool_menu.project(
            business,
            INTERNAL_SPECS,
            {},
            max_context_bytes=window_bytes,
            authority_bytes=4500,
            epoch=0,
            goal_revision=1,
            pinned_names=desktop_pins,
        )
        names_on = {s["function"]["name"] for s in exposed_on}
        assert set(tool_menu.CORE_TOOLS) | set(tool_menu.BROWSER_TOOLS) <= names_on
        assert set(tool_menu.BROWSER_TOOLS) <= set(state_on["pinned_names"])

    # 4. Mid-task permission enable after pins_released re-evaluates pins, while still allowing release under tight context.
    released_state = tool_menu.release(state_off)
    assert released_state["pins_released"] is True
    exposed_reenabled, state_reenabled = tool_menu.project(
        business,
        INTERNAL_SPECS,
        released_state,
        max_context_bytes=60000,
        authority_bytes=4500,
        epoch=0,
        goal_revision=1,
        pinned_names=desktop_pins,
    )
    assert not state_reenabled.get("pins_released")
    assert set(tool_menu.BROWSER_TOOLS) <= {s["function"]["name"] for s in exposed_reenabled}

    # Releasing pins again when context becomes tight drops pinned schemas back to discovery menu.
    tight_released = tool_menu.release(state_reenabled)
    exposed_after_release, state_after_release = tool_menu.project(
        business,
        INTERNAL_SPECS,
        tight_released,
        max_context_bytes=60000,
        authority_bytes=4500,
        epoch=0,
        goal_revision=1,
        pinned_names=desktop_pins,
    )
    assert not state_after_release.get("pinned_names")
    after_release_names = {s["function"]["name"] for s in exposed_after_release}
    assert after_release_names == {s["function"]["name"] for s in INTERNAL_SPECS}
    assert not (set(tool_menu.BROWSER_TOOLS) & after_release_names)

    # Turning desktop_access back off removes browser tools from loaded/pinned state.
    _, state_disabled_again = tool_menu.project(
        business,
        INTERNAL_SPECS,
        state_reenabled,
        max_context_bytes=60000,
        authority_bytes=4500,
        epoch=0,
        goal_revision=1,
        pinned_names=base_pins,
    )
    assert not (set(tool_menu.BROWSER_TOOLS) & set(state_disabled_again.get("pinned_names") or ()))
    assert not (set(tool_menu.BROWSER_TOOLS) & set(state_disabled_again.get("loaded_names") or ()))


def test_open_then_immediate_read_without_polling_and_navigation_timeout_status(local, monkeypatch):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    import http.server
    import socketserver
    import threading
    from rynmesh.agent_runtime import local_browser

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "instant.html").write_text(
        "<!doctype html><html><head><title>Instant Ready</title></head>"
        "<body><button id='ready-btn'>Launch Action</button><p>Immediate DOM content</p></body></html>",
        encoding="utf-8",
    )
    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "instant.html").as_uri()}, "op_open_instant")
    assert opened["ready"] in ("interactive", "complete")
    assert opened["dom_ready"] is True
    assert opened["timed_out"] is False
    assert opened["title"] == "Instant Ready"

    # Immediate read with zero polling loop in the test itself.
    immediate = tools.perform("ryn_browser_read", {"page_id": opened["page_id"]}, context, lambda: False)
    assert immediate["title"] == "Instant Ready"
    assert "Immediate DOM content" in immediate["text"]
    assert any(el["name"] == "Launch Action" for el in immediate["elements"])

    # Slow navigation where head script blocks body parsing past NAVIGATE_WAIT_SECONDS must report dom_ready=False, timed_out=True.
    class SlowHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/block.js":
                time.sleep(0.6)
                body = b"window.loadedSlow = true;"
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = b"<!doctype html><html><head><title>Slow Page</title><script src='/block.js'></script></head><body><button>Slow</button></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    with ThreadedTCPServer(("127.0.0.1", 0), SlowHandler) as httpd:
        port = httpd.server_address[1]
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        try:
            monkeypatch.setattr(local_browser, "NAVIGATE_WAIT_SECONDS", 0.15)
            slow_open = execute(
                tools,
                context,
                "ryn_browser_open",
                {"url": f"http://127.0.0.1:{port}/slow"},
                "op_open_slow",
            )
            assert slow_open["dom_ready"] is False
            assert slow_open["timed_out"] is True
        finally:
            httpd.shutdown()


def test_hover_async_wait_clear_expanded_press_and_multi_tab_management(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    from rynmesh.agent_runtime.store import AgentToolRejected

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "tab2.html").write_text(
        "<!doctype html><html><head><title>Second Tab</title></head>"
        "<body><button id='tab2-btn' onclick=\"document.body.dataset.clicked='yes'\">Tab2 Action</button>"
        "<button id='close-self' onclick='window.close()'>Close Tab</button></body></html>",
        encoding="utf-8",
    )
    (root / "interactive.html").write_text(
        """<!doctype html><html><head><title>Interactive Suite</title></head><body>
        <div id="hover-target" role="button" tabindex="0" style="width:120px;height:40px;background:#ccc">Hover Me</div>
        <div id="async-container"></div>
        <input id="ctrl-input" value="initial-value">
        <input id="ro-input" value="locked" readonly>
        <input id="dis-input" value="disabled" disabled>
        <div id="ce-input" contenteditable="true">editable text</div>
        <input id="key-input" value="abcd">
        <input id="next-input" value="">
        <button id="open-tab" onclick="window.open('tab2.html', '_blank')">Open New Tab</button>
        <script>
          window.hoverEnterCount = 0;
          window.hoverMoveCount = 0;
          const ht = document.getElementById('hover-target');
          ht.addEventListener('mouseenter', () => {
            window.hoverEnterCount += 1;
            setTimeout(() => {
              const btn = document.createElement('button');
              btn.id = 'async-btn';
              btn.textContent = 'Async Appeared';
              document.getElementById('async-container').appendChild(btn);
            }, 180);
          });
          ht.addEventListener('mousemove', () => { window.hoverMoveCount += 1; });

          window.inputEvents = [];
          const ci = document.getElementById('ctrl-input');
          ci.addEventListener('input', () => window.inputEvents.push('input:' + ci.value));
          ci.addEventListener('change', () => window.inputEvents.push('change:' + ci.value));

          window.keyEvents = [];
          const ki = document.getElementById('key-input');
          ki.addEventListener('keydown', (e) => window.keyEvents.push(e.key + ':' + e.code + ':' + e.keyCode));
        </script>
        </body></html>""",
        encoding="utf-8",
    )

    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "interactive.html").as_uri()}, "op_open_suite")
    page_id = opened["page_id"]

    # 1. Hover uses CDP mouseMoved and triggers native mouseenter exactly once (no duplicate synthetic dispatch).
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "hover", "selector": "#hover-target"}, "op_hover")
    assert tools._browser.evaluate(page_id, "task", "window.hoverEnterCount") == 1
    assert tools._browser.evaluate(page_id, "task", "window.hoverMoveCount") >= 1

    # 2. Wait for async selector to become visible.
    wait_res = execute(
        tools,
        context,
        "ryn_browser_action",
        {"page_id": page_id, "action": "wait", "selector": "#async-btn", "wait_seconds": 3},
        "op_wait_async",
    )
    assert wait_res["ready"] is True and wait_res["timed_out"] is False

    # Bounded delay wait without selector does not claim rendering readiness.
    delay_res = execute(
        tools,
        context,
        "ryn_browser_action",
        {"page_id": page_id, "action": "wait", "wait_seconds": 0.12},
        "op_wait_delay",
    )
    assert "ready" not in delay_res and delay_res["delay_completed"] is True and delay_res["waited_seconds"] >= 0.1

    # 3. Clear updates controlled input with native setter + input/change events, handles contenteditable, and rejects readonly/disabled.
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "clear", "selector": "#ctrl-input"}, "op_clear_ctrl")
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('ctrl-input').value") == ""
    assert tools._browser.evaluate(page_id, "task", "window.inputEvents") == ["input:", "change:"]

    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "clear", "selector": "#ce-input"}, "op_clear_ce")
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('ce-input').textContent") == ""

    with pytest.raises(AgentToolRejected, match="agent_browser_element_readonly"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "clear", "selector": "#ro-input"}, "op_clear_ro")
    with pytest.raises(AgentToolRejected, match="agent_browser_element_disabled"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "clear", "selector": "#dis-input"}, "op_clear_dis")

    # 4. Expanded press with optional selector focus and real editing/navigation behavior.
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "selector": "#key-input", "key": "End"}, "op_k_end")
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "key": "ArrowLeft"}, "op_k_left")
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "key": "Backspace"}, "op_k_bs")
    # "abcd" with cursor before 'd' -> Backspace removes 'c' -> "abd"
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('key-input').value") == "abd"
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "key": "Delete"}, "op_k_del")
    # Delete removes 'd' -> "ab"
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('key-input').value") == "ab"
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "key": "Home"}, "op_k_home")
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "key": "Space"}, "op_k_space")
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('key-input').value") == " ab"

    for extra_key in ("ArrowRight", "ArrowUp", "ArrowDown", "PageUp", "PageDown", "Escape", "Enter"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "key": extra_key}, f"op_k_{extra_key}")
    key_log = tools._browser.evaluate(page_id, "task", "window.keyEvents")
    assert any(item.startswith("Enter:Enter:13") for item in key_log)
    assert any(item.startswith("Escape:Escape:27") for item in key_log)
    assert any(item.startswith("ArrowDown:ArrowDown:40") for item in key_log)

    # Tab moves focus to #next-input.
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "press", "selector": "#key-input", "key": "Tab"}, "op_k_tab")
    assert tools._browser.evaluate(page_id, "task", "document.activeElement && document.activeElement.id") == "next-input"

    # 5. Multi-tab discovery, operating on new tab by page_id, and cleanup on close.
    click_open = execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#open-tab"}, "op_open_tab2")
    tabs = click_open["tabs"]
    if len(tabs) < 2:
        read_after = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
        tabs = read_after["tabs"]
    assert len(tabs) == 2
    tab2_id = next(t["page_id"] for t in tabs if t["page_id"] != page_id)

    tab2_read = tools.perform("ryn_browser_read", {"page_id": tab2_id}, context, lambda: False)
    assert tab2_read["title"] == "Second Tab"
    assert any(e["name"] == "Tab2 Action" for e in tab2_read["elements"])

    execute(tools, context, "ryn_browser_action", {"page_id": tab2_id, "action": "click", "selector": "#tab2-btn"}, "op_click_tab2")
    assert tools._browser.evaluate(tab2_id, "task", "document.body.dataset.clicked") == "yes"

    # Close tab2 via JS and verify sync_tabs cleans up its page record and connection.
    tools._browser.evaluate(tab2_id, "task", "window.close()")
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        remaining = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)["tabs"]
        if len(remaining) == 1:
            break
        time.sleep(0.05)
    assert [t["page_id"] for t in remaining] == [page_id]
    assert tab2_id not in tools._browser.pages


def test_state_fields_iframe_click_occlusion_and_disabled_element_checks(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    from rynmesh.agent_runtime.store import AgentToolRejected

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "states.html").write_text(
        """<!doctype html><html><head><title>States and HitTest</title></head><body>
        <a id="doc-link" href="https://example.com/guide">Guide Link</a>
        <input id="native-chk" type="checkbox" checked aria-label="Native Check">
        <div id="aria-chk" role="checkbox" aria-checked="mixed" tabindex="0">Mixed Check</div>
        <button id="exp-btn" aria-expanded="false" onclick="this.setAttribute('aria-expanded', 'true')">Accordion</button>
        <button id="dis-btn" disabled onclick="window.disClicked=true">Disabled Button</button>
        <div id="aria-dis-btn" role="button" aria-disabled="true" tabindex="0" onclick="window.ariaDisClicked=true">Aria Disabled</div>
        <div style="position:relative;width:140px;height:40px">
          <button id="covered-btn" style="width:140px;height:40px" onclick="window.coveredClicked=true">Covered Button</button>
          <div id="blocker" style="position:absolute;inset:0;background:rgba(0,0,0,0.5)">Blocking Overlay</div>
        </div>
        <button id="zero-btn" style="width:0;height:0;padding:0;border:0;overflow:hidden" onclick="window.zeroClicked=true">Zero</button>
        <iframe id="my-frame" srcdoc="<button id='frame-btn' onclick=&quot;this.textContent='Frame Clicked'&quot;>Frame Button</button>" style="width:240px;height:80px;border:1px solid #000"></iframe>
        </body></html>""",
        encoding="utf-8",
    )

    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "states.html").as_uri()}, "op_open_states")
    page_id = opened["page_id"]
    snap = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
    by_name = {el["name"]: el for el in snap["elements"]}

    # Verify extracted state fields (href, checked, disabled, expanded) for native and aria-* attributes.
    assert by_name["Guide Link"]["href"] == "https://example.com/guide"
    assert by_name["Native Check"]["checked"] is True
    assert by_name["Mixed Check"]["checked"] == "mixed"
    assert by_name["Accordion"]["expanded"] is False
    assert by_name["Disabled Button"]["disabled"] is True
    assert by_name["Aria Disabled"]["disabled"] is True

    # Clicking Accordion updates aria-expanded to True.
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#exp-btn"}, "op_click_exp")
    snap2 = tools.perform("ryn_browser_read", {"page_id": page_id, "query": "Accordion"}, context, lambda: False)
    assert snap2["elements"][0]["expanded"] is True

    # Clicking disabled or aria-disabled elements must be rejected without firing onclick.
    with pytest.raises(AgentToolRejected, match="agent_browser_element_disabled"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#dis-btn"}, "op_click_dis")
    with pytest.raises(AgentToolRejected, match="agent_browser_element_disabled"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#aria-dis-btn"}, "op_click_ariadis")
    assert tools._browser.evaluate(page_id, "task", "Boolean(window.disClicked || window.ariaDisClicked)") is False

    # Clicking occluded or zero-area elements must be rejected without silent fallback to e.click().
    with pytest.raises(AgentToolRejected, match="agent_browser_element_occluded"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#covered-btn"}, "op_click_cov")
    with pytest.raises(AgentToolRejected, match="agent_browser_element_not_visible"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#zero-btn"}, "op_click_zero")
    assert tools._browser.evaluate(page_id, "task", "Boolean(window.coveredClicked || window.zeroClicked)") is False

    # Clicking inside iframe with frame_selector translates coordinates and triggers real CDP click.
    frame_snap = tools.perform("ryn_browser_read", {"page_id": page_id, "frame_selector": "#my-frame"}, context, lambda: False)
    frame_btn_sel = frame_snap["elements"][0]["selector"]
    execute(
        tools,
        context,
        "ryn_browser_action",
        {"page_id": page_id, "action": "click", "frame_selector": "#my-frame", "selector": frame_btn_sel},
        "op_click_frame",
    )
    frame_snap_after = tools.perform("ryn_browser_read", {"page_id": page_id, "frame_selector": "#my-frame"}, context, lambda: False)
    assert frame_snap_after["elements"][0]["name"] == "Frame Clicked"


def test_stable_element_refs_across_spa_insertions_replacements_and_scoped_reads(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    from rynmesh.agent_runtime.store import AgentToolRejected

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "spa.html").write_text(
        """<!doctype html><html><head><title>SPA Stability</title></head><body>
        <div id="list">
          <button id="orig-target" onclick="window.lastClicked = 'orig-target'">Target Action</button>
        </div>
        <div id="scoped-area">
          <button id="scoped-btn" onclick="window.lastClicked = 'scoped-btn'">Scoped Action</button>
        </div>
        </body></html>""",
        encoding="utf-8",
    )
    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "spa.html").as_uri()}, "op_open_spa")
    page_id = opened["page_id"]

    full_read = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
    by_name = {el["name"]: el["selector"] for el in full_read["elements"]}
    target_sel = by_name["Target Action"]
    scoped_sel = by_name["Scoped Action"]
    assert target_sel != scoped_sel

    # Scoped read and query read must preserve stable IDs and never collide with other elements.
    scoped_read = tools.perform("ryn_browser_read", {"page_id": page_id, "scope_selector": "#scoped-area"}, context, lambda: False)
    assert scoped_read["elements"][0]["selector"] == scoped_sel
    query_read = tools.perform("ryn_browser_read", {"page_id": page_id, "query": "Target Action"}, context, lambda: False)
    assert query_read["elements"][0]["selector"] == target_sel

    # SPA inserts new buttons BEFORE #orig-target without re-reading the page.
    tools._browser.evaluate(
        page_id,
        "task",
        """(() => {
            const list = document.getElementById('list');
            for (let i = 0; i < 3; i++) {
                const b = document.createElement('button');
                b.textContent = 'Inserted ' + i;
                b.onclick = () => { window.lastClicked = 'inserted-' + i; };
                list.insertBefore(b, list.firstChild);
            }
        })()""",
    )
    # Clicking the old target_sel must still click #orig-target, never an inserted button.
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": target_sel}, "op_click_stable")
    assert tools._browser.evaluate(page_id, "task", "window.lastClicked") == "orig-target"

    # SPA replaces #orig-target with a new button node: old target_sel must report stale.
    tools._browser.evaluate(
        page_id,
        "task",
        """(() => {
            const old = document.getElementById('orig-target');
            const replacement = document.createElement('button');
            replacement.id = 'orig-target';
            replacement.textContent = 'Target Action';
            replacement.onclick = () => { window.lastClicked = 'replacement'; };
            old.replaceWith(replacement);
        })()""",
    )
    with pytest.raises(AgentToolRejected, match="agent_browser_element_stale_read_again"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": target_sel}, "op_click_stale_replaced")
    assert tools._browser.evaluate(page_id, "task", "window.lastClicked") == "orig-target"

    # Clicking a dynamically inserted button via plain CSS selector (#dynamic) before reading a new snapshot
    # must not pollute snapshot registration; the subsequent snapshot selector must click the same button cleanly.
    tools._browser.evaluate(
        page_id,
        "task",
        """(() => {
            const dyn = document.createElement('button');
            dyn.id = 'dynamic';
            dyn.textContent = 'Dynamic Button';
            dyn.onclick = () => { window.dynamicClicks = (window.dynamicClicks || 0) + 1; };
            document.getElementById('list').appendChild(dyn);
        })()""",
    )
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#dynamic"}, "op_click_dyn_css")
    assert tools._browser.evaluate(page_id, "task", "window.dynamicClicks") == 1
    dyn_read = tools.perform("ryn_browser_read", {"page_id": page_id, "query": "Dynamic Button"}, context, lambda: False)
    dyn_sel = dyn_read["elements"][0]["selector"]
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": dyn_sel}, "op_click_dyn_ref")
    assert tools._browser.evaluate(page_id, "task", "window.dynamicClicks") == 2

    # Reloading/navigating the page also invalidates old document element references with stale error.
    execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "reload"}, "op_reload_spa")
    with pytest.raises(AgentToolRejected, match="agent_browser_element_stale_read_again"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": scoped_sel}, "op_click_stale_nav")


def test_websocket_disconnect_reconnects_reads_but_never_replays_mutating_actions(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    from rynmesh.agent_runtime.store import AgentError

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "Counter.html").write_text(
        "<!doctype html><html><head><title>Disconnect Safety</title></head><body>"
        "<button id='inc' onclick='window.count = (window.count || 0) + 1; this.textContent = \"Count: \" + window.count'>Count: 0</button>"
        "</body></html>",
        encoding="utf-8",
    )
    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "Counter.html").as_uri()}, "op_open_disc")
    page_id = opened["page_id"]

    # 1. Read operation when WebSocket is closed automatically reconnects and succeeds.
    tools._browser.connections[page_id].close()
    read_after_drop = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
    assert read_after_drop["title"] == "Disconnect Safety"
    assert any(el["name"] == "Count: 0" for el in read_after_drop["elements"])

    # 2. Simulate WebSocket dropping AFTER the mutating mouseReleased command is sent (so the click executed,
    #    but the CDP response was lost). It must NOT replay the click!
    real_sock = tools._browser.connections[page_id]

    class DropOnMouseReleasedSocket:
        def __init__(self, inner):
            self.inner = inner
            self.drop_next_recv = False

        def send(self, payload):
            res = self.inner.send(payload)
            if '"Input.dispatchMouseEvent"' in payload and '"mouseReleased"' in payload:
                self.drop_next_recv = True
            return res

        def recv(self, timeout=None):
            if self.drop_next_recv:
                self.drop_next_recv = False
                self.inner.close()
                raise ConnectionResetError("Simulated socket drop after mutating command was sent")
            return self.inner.recv(timeout=timeout)

        def close(self):
            return self.inner.close()

    tools._browser.connections[page_id] = DropOnMouseReleasedSocket(real_sock)

    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed"):
        tools.execute(
            "ryn_browser_action",
            {"page_id": page_id, "action": "click", "selector": "#inc"},
            operation_id="op_click_once",
            context=context,
            expected_preview=tools.preview("ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#inc"}, context),
            cancelled=lambda: False,
        )

    # Ledger must keep op_click_once in 'dispatched' status and refuse duplicate execution of the same operation_id.
    assert tools.ledger_factory().get("op_click_once")["status"] == "dispatched"
    with pytest.raises(AgentError, match="agent_write_outcome_unknown"):
        tools.execute(
            "ryn_browser_action",
            {"page_id": page_id, "action": "click", "selector": "#inc"},
            operation_id="op_click_once",
            context=context,
            expected_preview=tools.preview("ryn_browser_action", {"page_id": page_id, "action": "click", "selector": "#inc"}, context),
            cancelled=lambda: False,
        )

    # Subsequent read reconnects cleanly and confirms the click happened exactly once (count == 1, never 2).
    recovered = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
    assert any(el["name"] == "Count: 1" for el in recovered["elements"])
    assert tools._browser.evaluate(page_id, "task", "window.count") == 1


def test_inflight_inference_permission_change_reschedules_cleanly_and_preserves_explicit_controls(tmp_path):
    from test_agent_runtime import advance, assistant, make_runtime
    from rynmesh.agent_runtime import tool_menu
    from rynmesh.agent_runtime.store import AgentError

    workspace = tmp_path / "ws"
    workspace.mkdir()

    runtime_ref = {}

    def _wrap_callable_model(model):
        orig = model.complete

        def complete(model_id, messages, tool_specs, cancelled, on_event):
            item = orig(model_id, messages, tool_specs, cancelled, on_event)
            return item(messages, tool_specs, cancelled) if callable(item) else item

        model.complete = complete

    def toggle_on_and_abort(messages, tools, cancelled):
        runtime_ref["rt"].update_settings({"desktop_access": True})
        assert cancelled() is True
        raise AgentError("agent_owner_changed")

    def toggle_off_and_return_stale(messages, tools, cancelled):
        runtime_ref["rt"].update_settings({"desktop_access": False})
        assert cancelled() is True
        return assistant("Stale response that must be discarded")

    runtime, model, _, _ = make_runtime(
        tmp_path / "agent",
        toggle_on_and_abort,
        toggle_off_and_return_stale,
        assistant("Completed cleanly after permission switches."),
    )
    _wrap_callable_model(model)
    runtime_ref["rt"] = runtime
    runtime.tools = _RuntimeLocalTools(runtime.store, tmp_path / "home")
    runtime.update_settings({"workspace_root": str(workspace), "desktop_access": False})

    task = runtime.create({"message": "Run while toggling permissions mid-inference."})

    # Step 1: in-flight model.complete() triggers desktop_access=True and aborts with agent_owner_changed.
    # Task must NOT fail; it must settle back to 'queued'.
    advance(runtime, 1)
    row_after_turn1 = runtime.store.read()["tasks"][task["id"]]
    assert row_after_turn1["status"] == "queued"
    assert row_after_turn1["error"] is None
    assert row_after_turn1["local_execution"]["desktop_access"] is True
    assert not (set(tool_menu.BROWSER_TOOLS) & {s["function"]["name"] for s in model.calls[0][2]})

    # Step 2: next model turn sees BROWSER_TOOLS directly; mid-inference it toggles desktop_access=False
    # and returns a stale assistant response, which must also be discarded and rescheduled as 'queued'.
    advance(runtime, 1)
    assert set(tool_menu.BROWSER_TOOLS) <= {s["function"]["name"] for s in model.calls[1][2]}
    row_after_turn2 = runtime.store.read()["tasks"][task["id"]]
    assert row_after_turn2["status"] == "queued"
    assert row_after_turn2["local_execution"]["desktop_access"] is False

    # Step 3: third turn has BROWSER_TOOLS removed again and completes normally.
    advance(runtime, 1)
    assert not (set(tool_menu.BROWSER_TOOLS) & {s["function"]["name"] for s in model.calls[2][2]})
    assert runtime.get(task["id"])["status"] == "completed"

    # Explicit pause/cancel during in-flight inference must NOT be revived into queued by update_settings.
    def pause_then_perm_change(messages, tools, cancelled):
        rt = runtime_ref["rt2"]
        tid = runtime_ref["tid2"]
        rt.action(tid, {"action": "pause"})
        rt.update_settings({"desktop_access": True})
        raise AgentError("agent_owner_changed")

    runtime2, model2, _, _ = make_runtime(tmp_path / "agent2", pause_then_perm_change, assistant("Should not run while paused"))
    _wrap_callable_model(model2)
    runtime_ref["rt2"] = runtime2
    runtime2.tools = _RuntimeLocalTools(runtime2.store, tmp_path / "home2")
    runtime2.update_settings({"workspace_root": str(workspace), "desktop_access": False})
    task2 = runtime2.create({"message": "Pause precedence check."})
    runtime_ref["tid2"] = task2["id"]
    advance(runtime2, 1)
    assert runtime2.get(task2["id"])["status"] == "paused"


def test_send_exception_on_mutating_command_and_post_side_effect_rejection_ledger(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    from rynmesh.agent_runtime.store import AgentError

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "send_and_ledger.html").write_text(
        """<!doctype html><html><head><title>Send and Ledger</title></head><body>
        <input id="space-input" value="">
        <button id="inc-btn" onclick="window.clicks = (window.clicks || 0) + 1">Increment</button>
        </body></html>""",
        encoding="utf-8",
    )
    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "send_and_ledger.html").as_uri()}, "op_open_sl")
    page_id = opened["page_id"]

    # Issue 2: connection.send() transmits the mutating keyDown command, then raises an exception inside send().
    # Must NOT reconnect and replay keyDown (which would type two spaces).
    real_sock = tools._browser.connections[page_id]

    class FailAfterMutatingSendSocket:
        def __init__(self, inner):
            self.inner = inner

        def send(self, payload):
            res = self.inner.send(payload)
            if '"Input.dispatchKeyEvent"' in payload and '"keyDown"' in payload:
                self.inner.close()
                raise OSError("Simulated socket failure after send transmitted payload")
            return res

        def recv(self, timeout=None):
            return self.inner.recv(timeout=timeout)

        def close(self):
            return self.inner.close()

    tools._browser.connections[page_id] = FailAfterMutatingSendSocket(real_sock)
    press_args = {"page_id": page_id, "action": "press", "selector": "#space-input", "key": "Space"}
    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed"):
        tools.execute(
            "ryn_browser_action",
            press_args,
            operation_id="op_space_send_err",
            context=context,
            expected_preview=tools.preview("ryn_browser_action", press_args, context),
            cancelled=lambda: False,
        )
    assert tools.ledger_factory().get("op_space_send_err")["status"] == "dispatched"
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('space-input').value") == " "

    # Issue 3a: Click takes effect (window.clicks becomes 1), then task is cancelled during post-click settle.
    # Ledger must remain 'dispatched' (uncertain/unconfirmed), NEVER 'not_executed'.
    click_args = {"page_id": page_id, "action": "click", "selector": "#inc-btn"}
    click_preview = tools.preview("ryn_browser_action", click_args, context)
    cancel_flag = {"cancelled": False}
    real_sock2 = tools._browser.connections[page_id]

    class CancelAfterMouseReleasedSocket:
        def __init__(self, inner):
            self.inner = inner

        def send(self, payload):
            res = self.inner.send(payload)
            if '"Input.dispatchMouseEvent"' in payload and '"mouseReleased"' in payload:
                cancel_flag["cancelled"] = True
            return res

        def recv(self, timeout=None):
            return self.inner.recv(timeout=timeout)

        def close(self):
            return self.inner.close()

    tools._browser.connections[page_id] = CancelAfterMouseReleasedSocket(real_sock2)
    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed"):
        tools.execute(
            "ryn_browser_action",
            click_args,
            operation_id="op_click_then_cancel",
            context=context,
            expected_preview=click_preview,
            cancelled=lambda: cancel_flag["cancelled"],
        )
    assert tools.ledger_factory().get("op_click_then_cancel")["status"] == "dispatched"
    assert tools._browser.evaluate(page_id, "task", "window.clicks") == 1

    # Issue 3b: Click takes effect (window.clicks becomes 2), then desktop_access is revoked during post-click settle.
    # Ledger must also remain 'dispatched', NEVER 'not_executed'.
    click_preview2 = tools.preview("ryn_browser_action", click_args, context)
    real_sock3 = tools._browser.connections[page_id]

    class RevokeAfterMouseReleasedSocket:
        def __init__(self, inner):
            self.inner = inner

        def send(self, payload):
            if "window.__rynPreNav" in payload:
                state.transaction(lambda data: data["settings"].update(desktop_access=False))
            return self.inner.send(payload)

        def recv(self, timeout=None):
            return self.inner.recv(timeout=timeout)

        def close(self):
            return self.inner.close()

    tools._browser.connections[page_id] = RevokeAfterMouseReleasedSocket(real_sock3)
    with pytest.raises(AgentError, match="agent_local_operation_unconfirmed"):
        tools.execute(
            "ryn_browser_action",
            click_args,
            operation_id="op_click_then_revoke",
            context=context,
            expected_preview=click_preview2,
            cancelled=lambda: False,
        )
    assert tools.ledger_factory().get("op_click_then_revoke")["status"] == "dispatched"


def test_click_revalidates_after_mouse_moved_press_verifies_focus_and_ancestor_scaled_iframe_rejected(local):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    from rynmesh.agent_runtime.store import AgentToolRejected

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    (root / "guards.html").write_text(
        """<!doctype html><html><head><title>Guards</title></head><body>
        <div id="swap-Slot">
          <button id="orig-hover-swap" style="width:120px;height:40px">Original Swap Button</button>
        </div>
        <input id="old-focused-input" value="keep">
        <div id="nonfocusable-role-btn" role="button" style="width:100px;height:30px">No TabIndex Div</div>
        <div style="transform: scale(0.5); transform-origin: 0 0; width: 300px; height: 120px">
          <iframe id="ancestor-scaled-frame" srcdoc="<button id='scaled-btn' onclick='window.scaledClicked=true' style='width:200px;height:60px'>Scaled Frame Btn</button>" style="width:240px;height:80px;border:0"></iframe>
        </div>
        <script>
          const orig = document.getElementById('orig-hover-swap');
          orig.addEventListener('mouseenter', () => {
            const rep = document.createElement('button');
            rep.id = 'orig-hover-swap';
            rep.style.width = '120px';
            rep.style.height = '40px';
            rep.textContent = 'Replacement Button';
            rep.onclick = () => { window.replacementClicked = true; };
            orig.replaceWith(rep);
          });
        </script>
        </body></html>""",
        encoding="utf-8",
    )
    opened = execute(tools, context, "ryn_browser_open", {"url": (root / "guards.html").as_uri()}, "op_open_guards")
    page_id = opened["page_id"]

    # Issue 4: mouseMoved triggers mouseenter which replaces the target node before mousePressed.
    # Both stable element reference and CSS selector must detect the replacement and refuse to click the replacement node.
    snap = tools.perform("ryn_browser_read", {"page_id": page_id}, context, lambda: False)
    swap_sel = next(el["selector"] for el in snap["elements"] if el["name"] == "Original Swap Button")
    with pytest.raises(AgentToolRejected, match="agent_browser_element_stale_read_again"):
        execute(tools, context, "ryn_browser_action", {"page_id": page_id, "action": "click", "selector": swap_sel}, "op_click_swap")
    assert tools._browser.evaluate(page_id, "task", "Boolean(window.replacementClicked)") is False

    # Issue 5: Old input keeps focus; pressing Space on a non-focusable div[role=button] without tabindex
    # must reject with agent_browser_element_not_focusable and leave the old input value untouched.
    tools._browser.evaluate(page_id, "task", "document.getElementById('old-focused-input').focus()")
    with pytest.raises(AgentToolRejected, match="agent_browser_element_not_focusable"):
        execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": page_id, "action": "press", "selector": "#nonfocusable-role-btn", "key": "Space"},
            "op_press_unfocusable",
        )
    assert tools._browser.evaluate(page_id, "task", "document.getElementById('old-focused-input').value") == "keep"

    # Issue 8: Iframe inside a CSS-scaled ancestor container must be rejected with agent_browser_frame_action_unsupported.
    frame_snap = tools.perform("ryn_browser_read", {"page_id": page_id, "frame_selector": "#ancestor-scaled-frame"}, context, lambda: False)
    scaled_btn_sel = frame_snap["elements"][0]["selector"]
    with pytest.raises(AgentToolRejected, match="agent_browser_frame_action_unsupported"):
        execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": page_id, "action": "click", "frame_selector": "#ancestor-scaled-frame", "selector": scaled_btn_sel},
            "op_click_scaled_ancestor_frame",
        )


def test_slow_main_document_headers_budget_and_selector_wait_recovers_after_main_thread_block(local, monkeypatch):
    if not find_browser():
        pytest.skip("Chromium unavailable")
    import http.server
    import socketserver
    import threading
    from rynmesh.agent_runtime import local_browser
    from rynmesh.agent_runtime.store import AgentToolRejected

    tools, context, root, state = local
    context["local_execution"]["desktop_access"] = True
    state.transaction(lambda data: data["settings"].update(desktop_access=True))
    tools._browser = Browser(tools.home, tools.processes, headless=True)

    # Ensure browser process is already started before measuring navigation budget.
    (root / "warmup.html").write_text("<!doctype html><title>Warmup</title><body>Warm</body>", encoding="utf-8")
    execute(tools, context, "ryn_browser_open", {"url": (root / "warmup.html").as_uri()}, "op_warmup")

    request_count = {"slow_headers": 0}

    class SlowHeaderHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/slow-headers":
                request_count["slow_headers"] += 1
                time.sleep(0.55)
                body = b"<!doctype html><html><head><title>Delayed Headers Ready</title></head><body><button id='dh-btn'>Header Ready</button></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True

    # Issue 6: Unified navigation wait budget covers Page.navigate when main document HTTP headers are delayed.
    with ThreadedTCPServer(("127.0.0.1", 0), SlowHeaderHandler) as httpd:
        port = httpd.server_address[1]
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        try:
            monkeypatch.setattr(local_browser, "NAVIGATE_WAIT_SECONDS", 0.2)
            t0 = time.monotonic()
            slow_hdr_open = execute(
                tools,
                context,
                "ryn_browser_open",
                {"url": f"http://127.0.0.1:{port}/slow-headers"},
                "op_open_slow_headers",
            )
            elapsed = time.monotonic() - t0
            assert elapsed < 1.5
            assert slow_hdr_open["page_id"]
            assert slow_hdr_open["dom_ready"] is False
            assert slow_hdr_open["timed_out"] is True
            assert slow_hdr_open["ready"] == "loading"
            assert request_count["slow_headers"] == 1

            # Once the delayed headers finish arriving, reading the returned page_id observes the loaded page without re-navigating.
            monkeypatch.setattr(local_browser, "NAVIGATE_WAIT_SECONDS", 2.0)
            read_after = tools.perform("ryn_browser_read", {"page_id": slow_hdr_open["page_id"]}, context, lambda: False)
            assert read_after["title"] == "Delayed Headers Ready"
            assert any(el["name"] == "Header Ready" for el in read_after["elements"])
            assert request_count["slow_headers"] == 1
        finally:
            httpd.shutdown()

    # Issue 7: Selector wait continues across a single evaluate timeout (1.5s) when the main thread is briefly blocked (~1.8s),
    # succeeding within the total wait_seconds budget (3.5s) while still rejecting invalid selectors immediately.
    (root / "busy_wait.html").write_text(
        """<!doctype html><html><head><title>Busy Wait</title></head><body>
        <div id="busy-root"></div>
        <script>
          setTimeout(() => {
            const until = Date.now() + 1800;
            while (Date.now() < until) {}
            const b = document.createElement('button');
            b.id = 'after-busy-btn';
            b.textContent = 'Appeared After Block';
            document.getElementById('busy-root').appendChild(b);
          }, 20);
        </script>
        </body></html>""",
        encoding="utf-8",
    )
    busy_open = execute(tools, context, "ryn_browser_open", {"url": (root / "busy_wait.html").as_uri()}, "op_open_busy")
    busy_page_id = busy_open["page_id"]

    wait_ok = execute(
        tools,
        context,
        "ryn_browser_action",
        {"page_id": busy_page_id, "action": "wait", "selector": "#after-busy-btn", "wait_seconds": 3.8},
        "op_wait_busy_ok",
    )
    assert wait_ok["matched"] is True
    assert wait_ok["ready"] is True
    assert wait_ok["timed_out"] is False

    with pytest.raises(AgentToolRejected, match="agent_browser_element_changed_or_script_failed"):
        execute(
            tools,
            context,
            "ryn_browser_action",
            {"page_id": busy_page_id, "action": "wait", "selector": "[[[invalid", "wait_seconds": 1.0},
            "op_wait_invalid_sel",
        )



