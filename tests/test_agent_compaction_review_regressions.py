"""Independent acceptance checks for calibrated capacity and activity identity."""
import json

import pytest
from test_agent_context_compaction_policy import WindowCatalog, exchange, pressure, seed
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import budget, context_compaction
from rynmesh.agent_runtime.store import encoded


def test_rejected_summary_does_not_dispatch_above_calibrated_input_capacity(tmp_path):
    truncated = assistant("Goal: inspect")
    truncated["finish_reason"] = "length"
    runtime, model, _, _ = make_runtime(tmp_path, truncated, assistant("Done."))
    runtime.catalog = WindowCatalog(32_000, 4096)
    task = runtime.create({"message": "Inspect the Chinese material without changing it."})
    row = runtime.store.read()["tasks"][task["id"]]
    limit = runtime._context_limit(row)
    for index in range(40):
        if runtime._request_context_bytes(row, specs=runtime._specs(row)) >= limit * 0.7:
            break
        row = seed(runtime, task["id"], exchange(f"doc_{index}", "中文段落。" * 300))
    size = runtime._request_context_bytes(row, specs=runtime._specs(row))

    def observed_usage(data):
        state = budget.initialize(data["tasks"][task["id"]])
        state["records"].append({
            "id": "observed-density", "kind": "model", "stage": "model", "round": state["round"],
            "status": "settled", "input_tokens": int(size / 1.8), "output_tokens": 20,
            "charged_tokens": 100, "charged_seconds": 0.0, "elapsed_seconds": 0.0,
            "input_reservation": 0, "output_reservation": 0,
            "output_policy": {"projected_context_utf8_bytes": size, "estimated_image_tokens": 0}})

    runtime.store.transaction(observed_usage)
    decision = pressure(runtime, runtime.store.read()["tasks"][task["id"]])
    assert decision["compact"] and decision["request_bytes"] > decision["capacity_bytes"]
    advance(runtime)
    assert any(not specs for _, _, specs in model.calls), "No summary was attempted"
    main_sizes = [len(encoded(messages)) + len(encoded(specs))
                  for _, messages, specs in model.calls if specs]
    assert all(size <= decision["capacity_bytes"] for size in main_sizes), {
        "main_request_bytes": main_sizes, "calibrated_capacity_bytes": decision["capacity_bytes"],
        "uncalibrated_hard_limit_bytes": limit}


@pytest.mark.parametrize("kind", ["long_path", "equal_length_code"])
def test_activity_repeat_counts_use_full_arguments_not_truncated_labels(kind):
    prefix = "folder/" + "a" * 135
    paths = [prefix + "/first.txt", prefix + "/second.txt"]
    tool = "ryn_file_read" if kind == "long_path" else "ryn_code_exec"
    arguments = ([{"path": path} for path in paths] if kind == "long_path" else
                 [{"code": "text('first')"}, {"code": "text('other')"}])
    history = []
    for index, args in enumerate(arguments):
        call_id = str(index)
        history.extend([
            {"role": "assistant", "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": tool, "arguments": json.dumps(args)}}]},
            {"role": "tool", "tool_call_id": call_id, "content": json.dumps({"text": "file contents"})},
        ])
    ledger = context_compaction.activity_ledger(history, len(history), 3000)
    rows = json.loads(ledger.split(":\n", 1)[1])
    assert len(rows) == 2 and all(row.get("repeated", 1) == 1 for row in rows), rows
