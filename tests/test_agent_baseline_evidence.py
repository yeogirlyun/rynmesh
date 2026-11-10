"""The acceptance checker must reject misleading counts and reordered receipts."""
import copy
import runpy
from pathlib import Path

check_run = runpy.run_path(str(Path(__file__).parents[1] / "docs/acceptance/ryn-agent-delivery/real-model/check_evidence.py"))["check_run"]


def evidence():
    def read(shared):
        return {"models": [{"model_id": "real-model", "sharing": shared},
                           {"model_id": "private-fixture-model", "sharing": False}]}

    results = [("ryn_list_models", read(False)), ("ryn_list_devices", {}), ("ryn_inspect_services", {}),
               ("ryn_set_model_sharing", {"sharing": True, "model_id": "local/target"}),
               ("ryn_list_models", read(True)),
               ("ryn_set_model_sharing", {"sharing": False, "model_id": "local/target"}),
               ("ryn_list_models", read(False))]
    return {"index": 1, "passed": True, "elapsed_seconds": 1,
            "state_snapshots": [{"default_model": "local/target"}],
            "approvals": [{"approved": True}, {"approved": True}],
            "model_calls": [{"response": {"usage": {"input_tokens": 12, "output_tokens": 3}}}],
            "task": {"events": [{"type": "tool_result", "seq": i, "tool": tool, "result": result}
                                for i, (tool, result) in enumerate(results, 1)]}}


def test_evidence_requires_ordered_post_write_reads():
    row = evidence()
    assert check_run(row, "real-model")["passed"] is True
    row["task"]["events"][4]["seq"] = 3  # Claimed shared read preceded the mutation.
    assert check_run(row, "real-model")["passed"] is False


def test_rejections_do_not_count_as_success_and_unknown_usage_is_not_zero():
    row = evidence()
    row["task"]["events"].append({"type": "tool_result", "seq": 8, "tool": "ryn_set_model_sharing",
                                  "result": {"error": "agent_model_not_shareable", "not_executed": True}})
    row["model_calls"].append({"error_type": "AdapterError", "elapsed_seconds": 120})
    result = check_run(row, "real-model")
    assert result["successful_tool_operations"] == 7
    assert len(result["rejected_not_executed"]) == 1
    assert result["input_tokens"] is None and result["output_tokens"] is None
    assert result["known_input_tokens_subtotal"] == 12


def test_evidence_rejects_different_write_target_or_shared_sibling():
    row = evidence()
    altered = copy.deepcopy(row)
    altered["task"]["events"][3]["result"]["model_id"] = "local/other"
    assert check_run(altered, "real-model")["passed"] is False
    row["task"]["events"][4]["result"]["models"][1]["sharing"] = True
    assert check_run(row, "real-model")["passed"] is False
