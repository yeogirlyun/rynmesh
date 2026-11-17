"""S12-I4 reporting rules in the actual provider-visible instruction path."""
import copy
import hashlib
import json
import re

from test_agent_runtime import advance, assistant, make_runtime
from rynmesh.agent_runtime import prompt


FINAL = "Final reply: outcome first, then what changed, how it was checked and what is open. Link files instead of pasting; restate key command output, which the owner cannot see. No flattery."
PLAIN = "Describe what was done in plain words; leave internal tool names and ids out unless the owner asks."
BLOCKING = "Put blocking questions in ryn_request_input or the final reply, not in mid-work text."
VERIFY = "Before saying done, check the result the owner will use: run the relevant test or command, read back the written file or page, and compare with each stated requirement; say what you could not check."
PROGRESS = "On long work, give a one-line progress note when a stage finishes (what was learned, what is next); do not narrate every call."
PREAMBLE = "Before a batch of tool calls, say in one sentence what you will do and why; skip this for a single trivial read."


def row_fixture(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Done."))
    task = runtime.create({"message": "Summarize verified work"})
    return runtime, model, runtime.store.read()["tasks"][task["id"]]


def test_t1_report_rules_order_and_bounded_byte_cost(tmp_path):
    runtime, _, row = row_fixture(tmp_path)
    text = prompt.authority(row)
    assert 0 < len(prompt.REPORT_RULES.encode()) <= 900
    assert len((prompt.REPORT_RULES + " " + prompt.PREAMBLE_RULE).encode()) <= 900
    assert text.index("Claims need matching subject/scope/time:") < text.index(prompt.REPORT_RULES) < text.index(prompt.INFERENCE_GUIDANCE)
    assert text.count(prompt.REPORT_RULES) == 1
    assert hashlib.sha256(prompt.SYSTEM.encode()).hexdigest() == "6ebd3c3555817db16ca52f0c78bbbfd7a2f351169ace8ab67320d6daac78b60d"


def test_t2_reporting_verification_progress_and_blocking_rules():
    for sentence in (FINAL, PLAIN, BLOCKING, VERIFY, PROGRESS):
        assert sentence in prompt.REPORT_RULES
    assert prompt.PREAMBLE_RULE == PREAMBLE
    assert not re.search(r"\b\d+\s+lines\b", prompt.REPORT_RULES + prompt.PREAMBLE_RULE)


def test_t3_file_policy_has_single_line_links_and_tables(tmp_path):
    _, _, row = row_fixture(tmp_path)
    first = prompt.authority(row).splitlines()[1]
    context = json.loads(first.removeprefix("Server control state: "))
    policy = context["file_output_policy"]
    assert "a link may end with :line" in policy
    assert "never use file:// URIs or line ranges" in policy
    assert "use a table for mappings and comparisons" in policy


def test_t3_real_runtime_model_request_receives_report_rules(tmp_path):
    runtime, model, row = row_fixture(tmp_path)
    advance(runtime)
    request = "\n".join(m["content"] for m in model.calls[-1][1] if m["role"] in {"system", "developer"})
    assert prompt.REPORT_RULES in request and VERIFY in request
    assert "never use file:// URIs or line ranges" in request
    assert runtime.get(row["id"])["status"] == "completed"


def test_t1_preamble_toggle_is_live_and_defaults_off_until_preview_validation(tmp_path, monkeypatch):
    runtime, _, row = row_fixture(tmp_path)
    assert prompt.PREAMBLE_ENABLED is False
    before = copy.deepcopy(runtime.store.read())
    baseline = runtime._request_context_bytes(row, specs=runtime._specs(row))
    assert prompt.PREAMBLE_RULE not in prompt.authority(row)
    monkeypatch.setattr(prompt, "PREAMBLE_ENABLED", True)
    enabled = runtime._request_context_bytes(row, specs=runtime._specs(row))
    assert enabled - baseline == len(prompt.PREAMBLE_RULE.encode()) + 1
    actual = "\n".join(m["content"] for m in runtime._context(row))
    assert prompt.PREAMBLE_RULE in actual
    monkeypatch.setattr(prompt, "PREAMBLE_ENABLED", False)
    assert prompt.PREAMBLE_RULE not in prompt.authority(row)
    assert runtime.store.read() == before


def test_t1_reporting_rules_leave_twelve_k_tool_discovery_callable(tmp_path):
    from test_agent_tool_menu import ManyTools
    from rynmesh.agent_runtime import tool_menu
    runtime, model, _, _ = make_runtime(tmp_path,
        assistant(name=tool_menu.NAME, args={"load": ["inspect_service"]}),
        assistant(name="inspect_service", args={"service_id": "svc1"}),
        assistant("Read confirmed."))
    runtime.tools = ManyTools()
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect the one service."})
    advance(runtime, 5)
    assert runtime.get(task["id"])["status"] == "completed"
    assert [call[0] for call in runtime.tools.calls] == ["inspect_service"]
    assert prompt.REPORT_RULES in "\n".join(m["content"] for m in model.calls[0][1] if m["role"] == "developer")
