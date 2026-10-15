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
