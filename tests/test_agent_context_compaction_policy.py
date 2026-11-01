"""Compaction follows the model window and the complete request, not a fixed history size.

Sizes are serialized UTF-8 request bytes (a CJK character is 3 bytes); model
windows and provider usage are tokens. Deterministic model stand-ins only: these
tests establish runtime behaviour, not the quality of a real model's summary.
"""
from __future__ import annotations

import copy
import json

import pytest
from test_agent_runtime import advance, approve, assistant, make_runtime

from rynmesh.agent_runtime import budget, context_compaction, output_policy
from rynmesh.agent_runtime.store import encoded


class WindowCatalog:
    """A model with a declared token window and output reservation."""

    def __init__(self, window_tokens, output_tokens):
        self.window_tokens, self.output_tokens = window_tokens, output_tokens

    def list_models(self):
        return [{"id": "local:test", "name": "Test", "source": "local_api", "available": True,
                 "agent_supported": True, "context_window": self.window_tokens,
                 "max_output_tokens": self.output_tokens, "context_window_known": True}]


def exchange(call_id, observation, name="inspect_service"):
    call = assistant(name=name, args={"service_id": call_id}, call_id=call_id)["message"]
    result = json.dumps({"service_id": call_id, "observation": observation}, ensure_ascii=False)
    return [{**call, "context_epoch": 0}, {"role": "tool", "tool_call_id": call_id, "content": result, "context_epoch": 0}]


def seed(runtime, task_id, messages):
    runtime.store.transaction(lambda data: data["tasks"][task_id]["messages"].extend(copy.deepcopy(messages)))
    return runtime.store.read()["tasks"][task_id]


def pressure(runtime, row):
    specs = runtime._specs(row)
    limit = runtime._context_limit(row)
    return runtime._compaction_pressure(row, limit, runtime._request_context_bytes(row, specs=specs), specs)


def fill_until_near_limit(runtime, task_id, observation, *, limit):
    """Add closed exchanges while the next one would still fit the hard limit."""
    row = runtime.store.read()["tasks"][task_id]
    index = 0
    while True:
        candidate = exchange(f"svc_{index}", observation)
        if runtime._request_context_bytes(row, specs=runtime._specs(row)) + len(encoded(candidate)) > limit:
            return row, index
        row = seed(runtime, task_id, candidate)
        index += 1


def pairs_intact(messages):
    calls = [call["id"] for message in messages for call in message.get("tool_calls", [])]
    results = [message["tool_call_id"] for message in messages if message["role"] == "tool"]
    return sorted(calls) == sorted(results)


def compaction_events(runtime, task_id, kind="context_compaction_started"):
    return [event for event in runtime.store.read()["tasks"][task_id]["events"] if event["type"] == kind]


# --- Units: bytes, characters and tokens stay separate -----------------------------------------


def test_sizes_are_utf8_bytes_and_tokens_use_the_declared_window():
    text = "状态正常。" * 10
    assert len(text) == 50 and len(text.encode()) == 150
    messages = exchange("a", text)
    assert context_compaction.step_sizes(messages) == [len(encoded(messages[0])) + len(encoded(messages[1]))]
    assert context_compaction.step_growth(messages) == context_compaction.MIN_STEP_BYTES
    large = exchange("b", "测" * 3000)
    assert context_compaction.step_growth([*messages, *large]) >= 9000  # 3000 characters are 9000 bytes

    policy = output_policy.context_budget({"context_window": 32_000, "max_output_tokens": 4096})
    hard = policy["effective_max_context_bytes"]
    assert hard == (32_000 - 4096 - output_policy.FRAMING_RESERVE_TOKENS) * output_policy.ESTIMATED_BYTES_PER_TOKEN
    # Denser tokenization (observed 2 bytes/token) leaves fewer request bytes.
    assert context_compaction.input_capacity(hard, policy, 2.0) == (32_000 - 4096 - 512) * 2
    # Sparser text never raises capacity above the admitted request size.
    assert context_compaction.input_capacity(hard, policy, 4.2) == hard
    # Implausible ratios are bounded; unknown windows keep the hard limit.
    assert context_compaction.input_capacity(hard, policy, 0.2) == int((32_000 - 4096 - 512) * 1.5)
    unknown = output_policy.context_budget({})
    assert context_compaction.input_capacity(unknown["effective_max_context_bytes"], unknown, 2.0) == unknown["effective_max_context_bytes"]


def test_observed_ratio_uses_latest_main_request_only():
    row = {"budget": {"records": [
        {"kind": "model", "stage": "model", "status": "settled", "input_tokens": 10_000,
         "output_policy": {"projected_context_utf8_bytes": 20_000, "estimated_image_tokens": 0}},
        {"kind": "model", "stage": "compaction", "status": "settled", "input_tokens": 1000,
         "output_policy": {"projected_context_utf8_bytes": 90_000, "estimated_image_tokens": 0}},
        {"kind": "model", "stage": "model", "status": "settled", "input_tokens": None,
         "output_policy": {"projected_context_utf8_bytes": 50_000, "estimated_image_tokens": 0}},
        {"kind": "model", "stage": "model", "status": "settled", "input_tokens": 100,
         "output_policy": {"projected_context_utf8_bytes": 2000, "estimated_image_tokens": 0}},
    ]}}
    # Summary requests, unknown usage and tiny framing-dominated requests are skipped.
    assert context_compaction.observed_bytes_per_token(row) == 2.0
    assert context_compaction.observed_bytes_per_token({}) is None


def test_summary_target_scales_with_replaced_history_within_the_hard_maximum():
    assert context_compaction.summary_target(16_000, 300_000) == 12_000
    assert context_compaction.summary_target(16_000, 24_000) == 4000
    assert context_compaction.summary_target(16_000, 3000) == 1024
    assert context_compaction.summary_target(3000, 300_000) == 2250
    assert context_compaction.summary_target(16_000, 300_000, reserve=5000) == 5000
    assert context_compaction.summary_target(800, 300_000) == 600


def test_activity_ledger_records_outcomes_merges_repeats_and_hides_secrets():
    reference = "gAAAA" + "R" * 120
    history = [{"role": "user", "content": "修复登录"}]

    def call(call_id, name, args, result):
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})
        history.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})

    call("a", "ryn_file_search", {"query": "登录 校验", "path": "src"}, {"matches": []})
    call("b", "ryn_file_search", {"query": "登录 校验", "path": "src"}, {"matches": []})
    call("c", "ryn_shell_execute", {"command": "pytest tests/test_auth.py"}, {"exit_code": 1, "output": "FAILED"})
    call("d", "ryn_apply_patch", {"patch": "*** Begin Patch\n" + "x" * 400}, {"error": "agent_patch_context_mismatch", "not_executed": True})
    call("e", "connect", {"api_key": "sk-secret", "resource_ref": reference}, {"connected": True})
    call("f", "ryn_update_plan", {"steps": []}, {"ok": True})
    call("g", "ryn_shell_execute", {"command": "pytest tests/test_auth.py"}, {"exit_code": 0, "output": "1 passed"})
    # An open call after the checkpoint boundary is not reported as done.
    history.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "h", "type": "function", "function": {"name": "ryn_file_read", "arguments": "{}"}}]})

    ledger = context_compaction.activity_ledger(history, len(history), 3000)
    rows = json.loads(ledger.split(":\n", 1)[1])
    assert [row["tool"] for row in rows] == ["ryn_file_search", "ryn_shell_execute", "ryn_apply_patch",
                                             "connect", "ryn_shell_execute"]
    assert rows[0] == {"tool": "ryn_file_search", "args": "path=src query=登录 校验", "outcome": "ok", "repeated": 2}
    assert rows[1]["outcome"] == "exit:1" and rows[-1]["outcome"] == "exit:0"
    assert rows[2]["outcome"] == "not_executed:agent_patch_context_mismatch"
    assert rows[2]["args"].startswith("patch=<") and "x" * 50 not in ledger
    assert "sk-secret" not in ledger and "api_key=[redacted]" in rows[3]["args"]
    assert reference not in ledger and "resource_ref=ref_" in rows[3]["args"]
    assert "ryn_update_plan" not in ledger and "ryn_file_read" not in ledger
    # A tight budget keeps the newest entries.
    tight = context_compaction.activity_ledger(history, len(history), 520)
    kept = json.loads(tight.split(":\n", 1)[1].split("\n(")[0])
    assert kept and kept[-1]["outcome"] == "exit:0" and "older entries omitted" in tight
    assert context_compaction.activity_ledger(history, len(history), 0) == ""


def test_activity_ledger_identity_uses_complete_arguments_not_display_text():
    history = []

    def call(call_id, name, args, result):
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        history.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)})

    prefix = "src/" + "module/" * 40  # longer than the 120-character display limit
    call("a", "ryn_file_read", {"path": prefix + "alpha.py"}, {"text": "a"})
    call("b", "ryn_file_read", {"path": prefix + "beta.py"}, {"text": "b"})
    call("c", "ryn_code_exec", {"code": "print('A')"}, {"status": "completed"})
    call("d", "ryn_code_exec", {"code": "print('B')"}, {"status": "completed"})  # same length, other code
    call("e", "ryn_file_search", {"query": "登录"}, {"matches": []})
    call("f", "ryn_file_search", {"query": "登录"}, {"matches": []})  # really identical
    call("g", "ryn_shell_execute", {"command": "pytest"}, {"exit_code": 1})
    call("h", "ryn_shell_execute", {"command": "pytest"}, {"exit_code": 0})  # same call, other outcome
    rows = json.loads(context_compaction.activity_ledger(history, len(history), 3000).split(":\n", 1)[1])
    assert len(rows) == 7
    reads = [row for row in rows if row["tool"] == "ryn_file_read"]
    assert len(reads) == 2 and reads[0]["args"] == reads[1]["args"]  # identical abbreviated text...
    assert reads[0]["args_id"] != reads[1]["args_id"]  # ...but different calls
    code = [row for row in rows if row["tool"] == "ryn_code_exec"]
    assert len(code) == 2 and code[0]["args"] == code[1]["args"] == "code=<10 bytes>"
    assert code[0]["args_id"] != code[1]["args_id"] and "repeated" not in code[0]
    search = [row for row in rows if row["tool"] == "ryn_file_search"]
    assert search == [{"tool": "ryn_file_search", "args": "query=登录", "outcome": "ok", "repeated": 2}]
    assert [row["outcome"] for row in rows if row["tool"] == "ryn_shell_execute"] == ["exit:1", "exit:0"]


# --- Large windows: ample budget means no summary ----------------------------------------------


def test_large_window_keeps_complete_history_while_budget_is_ample(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("服务 A 到 L 均已检查，未做任何修改。"))
    runtime.catalog = WindowCatalog(200_000, 8192)
    task = runtime.create({"message": "检查服务 A 到 L，不要做任何修改。"})
    history = [m for index in range(12) for m in exchange(f"svc_{index}", "观测结果：" + "状态正常。" * 200)]
    row = seed(runtime, task["id"], history)
    limit = runtime._context_limit(row)
    assert limit == (200_000 - 8192 - 512) * 3
    # The removed rule summarized at 24,000 bytes of multi-step history in any
    # window of at least 240,000 bytes. This history is larger than that.
    assert limit >= 240_000 and len(encoded(history)) > 24_000
    decision = pressure(runtime, row)
    assert decision["compact"] is False and decision["reason"] == "headroom"
    assert decision["capacity_bytes"] == limit and decision["headroom_bytes"] > 10 * decision["need_bytes"]
    advance(runtime)
    assert len(model.calls) == 1 and model.calls[0][2]  # one main request, no summary request
    sent = [m for m in model.calls[0][1] if m["role"] == "tool"]
    assert [m["content"] for m in sent] == [m["content"] for m in history if m["role"] == "tool"]
    assert not compaction_events(runtime, task["id"])
    assert runtime.get(task["id"])["status"] == "completed"


def test_many_steps_in_a_large_window_spend_no_summary_calls(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    runtime.catalog = WindowCatalog(200_000, 8192)
    task = runtime.create({"message": "Inspect services 0-13 one by one; change nothing."})
    padding = "Observed healthy service; metrics nominal. " * 100  # about 4.4 KB per step

    def complete(model_id, messages, specs, cancelled, on_event):
        model.calls.append((model_id, copy.deepcopy(messages), copy.deepcopy(specs)))
        done = sum(1 for m in messages if m["role"] == "tool")
        if done < 14:
            return assistant(name="inspect_service", args={"service_id": f"svc-{done}"}, call_id=f"c{done}")
        return assistant("All 14 services inspected; nothing changed.")

    model.complete = complete
    original = tools.execute
    tools.execute = lambda name, arguments, **kw: {**original(name, arguments, **kw), "detail": padding}
    for _ in range(40):
        advance(runtime)
        if runtime.get(task["id"])["status"] == "completed":
            break
    assert runtime.get(task["id"])["status"] == "completed"
    assert len(tools.calls) == 14
    assert all(specs for _, _, specs in model.calls)  # every model call was a main request
    assert not compaction_events(runtime, task["id"])
    final_tools = [m for m in model.calls[-1][1] if m["role"] == "tool"]
    assert len(final_tools) == 14 and pairs_intact(model.calls[-1][1])


# --- Near the limit: compact once, keep recent evidence, leave room ------------------------------


def test_compacts_before_projected_overflow_and_keeps_room_for_several_steps(tmp_path):
    runtime, model, _, _ = make_runtime(
        tmp_path,
        assistant("Goal: inspect every service.\nFindings: all observed healthy.\nNext: report."),
        assistant("All inspected; nothing changed."))
    runtime.catalog = WindowCatalog(32_000, 4096)
    task = runtime.create({"message": "Inspect every service; never change anything."})
    limit = runtime._context_limit(runtime.store.read()["tasks"][task["id"]])
    row, count = fill_until_near_limit(runtime, task["id"], "x" * 5000, limit=limit)
    decision = pressure(runtime, row)
    assert decision["request_bytes"] <= limit  # it still fits: this is the proactive path
    assert decision["near_limit"] and decision["compact"] and decision["reason"] == "projected_overflow"
    advance(runtime)
    summary, main = model.calls
    assert summary[2] == [] and main[2]
    started = compaction_events(runtime, task["id"])
    assert len(started) == 1 and started[0]["reason"] == "projected_overflow"
    assert started[0]["request_bytes"] == decision["request_bytes"]
    sent = main[1]
    assert pairs_intact(sent)
    kept = [m["tool_call_id"] for m in sent if m["role"] == "tool"]
    assert kept and kept[-1] == f"svc_{count - 1}"  # the latest exchanges stay verbatim
    assert len(kept) < count
    assert [m["content"] for m in sent if m["role"] == "user"] == ["Inspect every service; never change anything."]
    after = len(encoded(sent)) + len(encoded(main[2]))
    assert limit - after >= context_compaction.MIN_STEPS_AFTER_COMPACTION * decision["need_bytes"]
    saved = runtime.store.read()["tasks"][task["id"]]
    assert saved["context_checkpoint"]["through"] == decision["preferred_cut"]
    # Right after a checkpoint the next step has room: no second summary.
    follow_up = pressure(runtime, saved)
    assert follow_up["compact"] is False and follow_up["reason"] == "headroom"
    assert runtime.get(task["id"])["status"] == "completed"


def test_medium_window_long_task_leaves_several_steps_between_summaries(tmp_path):
    runtime, model, tools, _ = make_runtime(tmp_path)
    runtime.catalog = WindowCatalog(24_000, 2048)
    runtime.update_settings({"max_steps": 64})
    task = runtime.create({"message": "检查服务 0-23，逐个记录结论，不做任何修改。"})
    padding = "检查结果：服务运行正常，指标无异常。" * 90  # about 4.6 KB of Chinese per step
    kinds = []

    def complete(model_id, messages, specs, cancelled, on_event):
        if not specs:
            kinds.append("summary")
            source = json.loads(messages[-1]["content"])
            assert pairs_intact(source["history"])
            done = [m["tool_call_id"] for m in source["history"] if m["role"] == "tool"]
            return assistant(f"Goal: inspect 24 services. Findings: {len(done)} more inspected, all normal. Next: continue.")
        kinds.append("main")
        assert pairs_intact(messages)
        done = len(tools.calls)
        if done < 24:
            return assistant(name="inspect_service", args={"service_id": f"svc-{done}"}, call_id=f"c{done}")
        return assistant("24 项服务均已检查，未做修改。")

    model.complete = complete
    original = tools.execute
    tools.execute = lambda name, arguments, **kw: {**original(name, arguments, **kw), "detail": padding}
    for _ in range(80):
        advance(runtime)
        if runtime.get(task["id"])["status"] in {"completed", "failed"}:
            break
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert len(tools.calls) == 24 and len({args["service_id"] for _, args, _ in tools.calls}) == 24
    summaries = [index for index, kind in enumerate(kinds) if kind == "summary"]
    assert summaries  # the history really exceeded this window
    gaps = [later - earlier - 1 for earlier, later in zip(summaries, summaries[1:], strict=False)]
    assert all(gap >= 3 for gap in gaps), kinds  # never summary after summary, never one step apart
    assert len(summaries) * 4 <= kinds.count("main"), kinds


# --- Small windows: never a low-yield proactive summary --------------------------------------


def test_small_window_does_not_pay_for_a_summary_that_cannot_buy_steps(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Observed A; nothing changed."))
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect A; never change anything."})
    row = seed(runtime, task["id"], exchange("a", "a" * 1200))
    limit = runtime._context_limit(row)
    while runtime._request_context_bytes(row, specs=runtime._specs(row)) <= limit - 1500:
        def pad(data):
            data["tasks"][task["id"]]["messages"][0]["content"] += " Never change anything."
        runtime.store.transaction(pad)
        row = runtime.store.read()["tasks"][task["id"]]
    decision = pressure(runtime, row)
    assert decision["near_limit"] and not decision["compact"] and decision["reason"] == "low_yield"
    advance(runtime)
    assert len(model.calls) == 1 and model.calls[0][2]
    assert not compaction_events(runtime, task["id"])
    assert runtime.get(task["id"])["status"] == "completed"


def test_hard_overflow_still_compacts_in_a_small_window(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Goal: inspect A. Findings: A observed. Next: answer."),
                                        assistant("A inspected; nothing changed."))
    runtime.update_settings({"max_context_chars": 12000})
    task = runtime.create({"message": "Inspect A; never change anything."})
    seed(runtime, task["id"], exchange("a", "观测" * 1400))  # 8.4 KB of Chinese: does not fit with the prompt
    row = runtime.store.read()["tasks"][task["id"]]
    assert runtime._request_context_bytes(row, specs=runtime._specs(row)) > runtime._context_limit(row)
    advance(runtime)
    summary, main = model.calls
    assert summary[2] == [] and main[2]
    assert compaction_events(runtime, task["id"])[0]["reason"] == "hard_limit"
    assert len(encoded(main[1])) + len(encoded(main[2])) <= 12000 and pairs_intact(main[1])
    assert runtime.get(task["id"])["status"] == "completed"


# --- Chinese text with provider-reported usage --------------------------------------------------


def test_dense_chinese_usage_compacts_before_the_byte_estimate_overflows(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path, assistant("Goal: 总结资料。Findings: 已读取。Next: 汇总。"),
                                        assistant("已完成总结，未修改资料。"))
    runtime.catalog = WindowCatalog(32_000, 4096)
    task = runtime.create({"message": "总结这些中文资料，不要修改任何内容。"})
    row = runtime.store.read()["tasks"][task["id"]]
    limit = runtime._context_limit(row)
    for index in range(40):
        if runtime._request_context_bytes(row, specs=runtime._specs(row)) >= limit * 0.7:
            break
        row = seed(runtime, task["id"], exchange(f"doc_{index}", "资料内容：" + "中文段落。" * 300))
    before = pressure(runtime, row)
    assert before["capacity_bytes"] == limit and not before["near_limit"]
    size = before["request_bytes"]

    def report_usage(data):
        # The provider counted 1 token per 1.8 request bytes for this Chinese
        # request, denser than the 3 bytes/token admission estimate.
        state = budget.initialize(data["tasks"][task["id"]])
        state["records"].append({
            "id": "usage_observed", "kind": "model", "stage": "model", "round": state["round"], "status": "settled",
            "input_tokens": int(size / 1.8), "output_tokens": 20, "charged_tokens": 100, "charged_seconds": 0.0,
            "elapsed_seconds": 0.0, "input_reservation": 0, "output_reservation": 0,
            "output_policy": {"projected_context_utf8_bytes": size, "estimated_image_tokens": 0}})
    runtime.store.transaction(report_usage)
    row = runtime.store.read()["tasks"][task["id"]]
    after = pressure(runtime, row)
    ratio = size / int(size / 1.8)
    assert after["bytes_per_token"] == pytest.approx(1.8, rel=0.01)
    assert after["capacity_bytes"] == int((32_000 - 4096 - 512) * ratio) < limit
    assert after["request_bytes"] == size < limit  # the byte estimate alone would dispatch it
    assert after["compact"] and after["reason"] == "projected_overflow"
    advance(runtime)
    summary, main = model.calls
    assert summary[2] == [] and main[2] and pairs_intact(main[1])
    assert len(encoded(main[1])) + len(encoded(main[2])) < after["capacity_bytes"]
    assert compaction_events(runtime, task["id"])[0]["reason"] == "capacity_limit"


def test_failed_summary_never_dispatches_a_request_over_the_calibrated_capacity(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    runtime.catalog = WindowCatalog(32_000, 4096)
    task = runtime.create({"message": "总结这些中文资料，不要修改任何内容。"})
    row = runtime.store.read()["tasks"][task["id"]]
    limit = runtime._context_limit(row)
    for index in range(40):
        if runtime._request_context_bytes(row, specs=runtime._specs(row)) >= limit * 0.7:
            break
        row = seed(runtime, task["id"], exchange(f"doc_{index}", "资料内容：" + "中文段落。" * 300))
    size = pressure(runtime, row)["request_bytes"]

    def report_usage(data):
        state = budget.initialize(data["tasks"][task["id"]])
        state["records"].append({
            "id": "usage_observed", "kind": "model", "stage": "model", "round": state["round"], "status": "settled",
            "input_tokens": int(size / 1.8), "output_tokens": 20, "charged_tokens": 100, "charged_seconds": 0.0,
            "elapsed_seconds": 0.0, "input_reservation": 0, "output_reservation": 0,
            "output_policy": {"projected_context_utf8_bytes": size, "estimated_image_tokens": 0}})
    runtime.store.transaction(report_usage)
    row = runtime.store.read()["tasks"][task["id"]]
    decision = pressure(runtime, row)
    capacity = decision["capacity_bytes"]
    assert capacity < decision["request_bytes"] <= limit  # fits the byte limit, not the real window
    kinds, sizes = [], []

    def complete(model_id, messages, specs, cancelled, on_event):
        kinds.append("main" if specs else "summary")
        if specs:
            sizes.append(len(encoded(messages)) + len(encoded(specs)))
            assert runtime.store.read()["tasks"][task["id"]].get("context_checkpoint")
            return assistant("已完成总结，未修改资料。")
        if kinds.count("summary") == 1:
            truncated = assistant("Goal: 总结")
            truncated["finish_reason"] = "length"
            return truncated
        return assistant("Goal: 总结资料。Findings: 已读取。Next: 汇总。")

    model.complete = complete
    for _ in range(6):
        advance(runtime)
        if runtime.get(task["id"])["status"] in {"completed", "failed"}:
            break
    done = runtime.get(task["id"])
    # The truncated summary is retried; the oversized request is never sent.
    assert kinds == ["summary", "summary", "main"], (kinds, done.get("error"))
    assert done["status"] == "completed", done.get("error")
    assert sizes and all(value <= capacity for value in sizes)
    assert any(e["type"] == "context_compaction_rejected" for e in done["events"])


# --- After a checkpoint the task resumes with its state ----------------------------------------


class Workbench:
    """Search, edit and test tools with real effects recorded by the runtime."""

    def __init__(self):
        self.calls = []
        self.tests_run = 0

    def specs(self):
        def spec(name, prop):
            return {"type": "function", "function": {"name": name, "description": name, "parameters": {
                "type": "object", "properties": {key: {"type": "string"} for key in prop},
                "required": list(prop), "additionalProperties": False}}}
        return [spec("search_code", ["query"]), spec("edit_file", ["path", "content"]), spec("run_tests", ["target"])]

    def metadata(self, name):
        if name not in {"search_code", "edit_file", "run_tests"}:
            raise ValueError("unavailable")
        return {"effect": "write" if name == "edit_file" else "read", "title": name}

    def preview(self, name, arguments):
        return {"path": arguments["path"], "after": arguments["content"]}

    def execute(self, name, arguments, *, operation_id, cancelled):
        self.calls.append((name, arguments))
        if name == "search_code":
            return {"matches": [{"path": "auth.py", "line": 42, "text": "登录校验返回 500"}], "notes": "说明" * 2500}
        if name == "edit_file":
            return {"path": arguments["path"], "written": True, "bytes": len(arguments["content"].encode())}
        self.tests_run += 1
        if self.tests_run == 1:
            return {"exit_code": 1, "output": "FAILED tests/test_auth.py::test_login 期望 200 实际 500"}
        return {"exit_code": 0, "output": "1 passed"}


def test_resumes_after_compaction_with_goal_corrections_changes_tests_and_failures(tmp_path):
    runtime, model, _, _ = make_runtime(tmp_path)
    tools = runtime.tools = Workbench()
    goal = "修复登录接口的 500 错误。不要修改数据库迁移。"
    correction = "更正：只改 auth.py，不要动 session.py。"
    task = runtime.create({"message": goal})
    summary_requests, main_requests = [], []
    summary_text = ("Goal: 修复登录 500，只改 auth.py。\nChanged: auth.py 校验逻辑。\n"
                    "Findings: auth.py:42 返回 500。\nVerification: 首次 run_tests 失败，修改后通过。\n"
                    "Failed: 首次测试失败。\nNext: 汇报结果。")

    def complete(model_id, messages, specs, cancelled, on_event):
        if not specs:
            summary_requests.append(copy.deepcopy(messages))
            return assistant(summary_text)
        main_requests.append(copy.deepcopy(messages))
        step = len(main_requests)
        if step == 1:
            return assistant(name="search_code", args={"query": "login 500"}, call_id="s1")
        if step == 2:
            return assistant(name="run_tests", args={"target": "tests/test_auth.py"}, call_id="t1")
        if step == 3:
            return assistant(name="edit_file", args={"path": "auth.py", "content": "fixed = True\n"}, call_id="e1")
        if step == 4:
            return assistant(name="run_tests", args={"target": "tests/test_auth.py"}, call_id="t2")
        return assistant("已修复 auth.py，测试通过。")

    model.complete = complete
    runtime.add_message(task["id"], {"message": correction, "intent": "steer",
                                     "expected_control_revision": task["control_revision"],
                                     "expected_goal_revision": task["goal"]["revision"]})
    for _ in range(16):
        row = runtime.get(task["id"])
        if row["status"] == "waiting_approval":
            approve(runtime, task["id"])
        if len(main_requests) == 4 and not runtime.store.read()["tasks"][task["id"]]["queue"]:
            break
        advance(runtime)
    assert [name for name, _ in tools.calls] == ["search_code", "run_tests", "edit_file", "run_tests"]

    # The next request no longer fits a smaller window (the tool menu may
    # shrink with it): the runtime must checkpoint before continuing.
    limit = runtime._request_context_bytes(runtime.store.read()["tasks"][task["id"]],
                                           specs=runtime._specs(runtime.store.read()["tasks"][task["id"]]))
    while True:
        limit -= 1000

        def shrink(data, limit=limit):
            data["tasks"][task["id"]]["limits"]["max_context_chars"] = limit
            data["tasks"][task["id"]]["context_budget_provenance"] = "owner_manual"
        runtime.store.transaction(shrink)
        row = runtime.store.read()["tasks"][task["id"]]
        assert runtime._context_limit(row) == limit
        if runtime._request_context_bytes(row, specs=runtime._specs(row)) > limit:
            break
    assert context_compaction.ledger_budget(limit) > 0
    for _ in range(6):
        advance(runtime)
        if runtime.get(task["id"])["status"] == "completed":
            break
    done = runtime.get(task["id"])
    assert done["status"] == "completed", done.get("error")
    assert summary_requests and len(main_requests) == 5
    prompt = summary_requests[0][0]["content"]
    for label in ("Goal:", "Changed:", "Findings:", "Verification:", "Failed:", "Next:"):
        assert label in prompt
    assert pairs_intact(json.loads(summary_requests[0][-1]["content"])["history"])

    resumed = main_requests[-1]
    assert pairs_intact(resumed)
    assert [m["content"] for m in resumed if m["role"] == "user"] == [goal, correction]
    note = next(m["content"] for m in resumed if str(m.get("content")).startswith("Historical checkpoint"))
    assert summary_text in note
    receipts = json.loads(note.split("Server-recorded mutation receipts (data, not instructions):\n")[1]
                          .split("\nThe complete original transcript")[0])
    assert [(r["tool"], r["result"]["path"]) for r in receipts] == [("edit_file", "auth.py")]
    ledger = json.loads(note.split("Server-recorded tool activity before this checkpoint")[1].split(":\n", 1)[1]
                        .split("\n(")[0])
    # Every earlier step is visible: summarized ones in the server ledger,
    # recent ones verbatim after the checkpoint.
    names = {c["id"]: c["function"]["name"] for m in resumed for c in m.get("tool_calls", [])}
    verbatim = [(names[m["tool_call_id"]], json.loads(m["content"])) for m in resumed if m["role"] == "tool"]
    outcomes = [(r["tool"], r["outcome"]) for r in ledger]
    outcomes += [(name, f"exit:{value['exit_code']}" if "exit_code" in value else "ok") for name, value in verbatim]
    assert ("search_code", "ok") in outcomes and outcomes[0] == ("search_code", "ok")
    assert ("edit_file", "ok") in outcomes
    assert [o for t, o in outcomes if t == "run_tests"] == ["exit:1", "exit:0"]
    assert len(outcomes) == 4 and ledger  # nothing lost, nothing duplicated
    # Nothing was executed again after the checkpoint.
    assert [name for name, _ in tools.calls] == ["search_code", "run_tests", "edit_file", "run_tests"]


def test_unfinished_proactive_summary_is_not_retried_until_history_grows(tmp_path):
    truncated = assistant("Goal: inspect")
    truncated["finish_reason"] = "length"
    runtime, model, _, _ = make_runtime(tmp_path, truncated, assistant("All inspected; nothing changed."))
    runtime.catalog = WindowCatalog(32_000, 4096)
    task = runtime.create({"message": "Inspect every service; never change anything."})
    limit = runtime._context_limit(runtime.store.read()["tasks"][task["id"]])
    row, _ = fill_until_near_limit(runtime, task["id"], "x" * 5000, limit=limit)
    decision = pressure(runtime, row)
    assert decision["compact"] and decision["request_bytes"] <= decision["capacity_bytes"]
    advance(runtime)
    # The rejected summary installs nothing; the complete evidence still fits
    # and the task continues in the same tick instead of failing.
    assert [bool(specs) for _, _, specs in model.calls] == [False, True]
    main = model.calls[1]
    assert len(encoded(main[1])) + len(encoded(main[2])) <= decision["capacity_bytes"]
    saved = runtime.store.read()["tasks"][task["id"]]
    assert not saved.get("context_checkpoint")
    assert any(e["type"] == "context_compaction_rejected" for e in saved["events"])
    assert runtime.get(task["id"])["status"] == "completed"
    # Without new growth the same paid attempt is not repeated.
    retry = pressure(runtime, {**saved, "messages": saved["messages"][:-1]})
    assert retry["compact"] is False and retry["reason"] == "recent_attempt_unfinished"
