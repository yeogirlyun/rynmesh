"""S03-I5 tests: prefix-reuse compaction, reference eviction/bounds, configurable compact_prompt, low effort, and retry limit."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_agent_context_compaction_policy import WindowCatalog, exchange, seed
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import checkpoint_refs, context_compaction, retry_policy
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.store import AgentError, AgentStore, encoded
from rynmesh.services import peer_box


def _fernet_like_ref(index: int, total_len: int = 120) -> str:
    prefix = f"gAAAAABn{index:06d}_"
    fill_len = max(1, total_len - len(prefix))
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    ch = alphabet[index % len(alphabet)]
    return prefix + (ch * fill_len)


def test_t1_32k_window_truncates_oversized_tool_output_and_drops_oldest_history(tmp_path):
    """T1: On a 32k window, oversized tool output is replaced and oldest history dropped so compaction succeeds."""
    captured_compaction_inputs: list[tuple[list[dict], list[dict]]] = []

    def complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        last_text = str(messages[-1].get("content") or "") if messages else ""
        first_text = str(messages[0].get("content") or "") if messages else ""
        if "CONTEXT CHECKPOINT COMPACTION" in first_text or "Server compaction request:" in last_text:
            captured_compaction_inputs.append((copy.deepcopy(messages), copy.deepcopy(specs)))
            return assistant("Goal: inspect logs.\nFindings: oversized output truncated and oldest steps condensed.\nNext: summarize.")
        return assistant("Completed after 32k fallback compaction.")

    runtime, model, _, _ = make_runtime(tmp_path)
    model.complete = complete
    runtime.catalog = WindowCatalog(32768, 4096)
    task = runtime.create({"message": "Analyze all service logs on 32k window."})
    tid = task["id"]

    # Seed large assistant messages + an oversized last tool output (> 32k window capacity ~82 KB)
    big_prose_1 = {"role": "assistant", "content": "Early analysis A: " + ("alpha " * 6000), "context_epoch": 0}
    big_prose_2 = {"role": "assistant", "content": "Early analysis B: " + ("bravo " * 6000), "context_epoch": 0}
    big_prose_3 = {"role": "assistant", "content": "Early analysis C: " + ("charlie " * 5500), "context_epoch": 0}
    huge_exchange = exchange("svc_huge", "HUGE_TOOL_LINE " * 5000)
    seed(runtime, tid, [big_prose_1, big_prose_2, big_prose_3, *huge_exchange])

    for _ in range(4):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            break
        advance(runtime)

    done = runtime.get(tid)
    assert done["status"] == "completed", done.get("error")
    assert captured_compaction_inputs, "expected compaction call"
    compaction_msgs, _ = captured_compaction_inputs[0]
    serialized = encoded(compaction_msgs).decode("utf-8")
    assert "Output exceeded the available model context and was truncated" in serialized
    assert "Early analysis A:" not in serialized


def test_t2_32k_window_oversized_summary_is_truncated_instead_of_size_limit(tmp_path):
    """T2: On a 32k window, an over-maximum summary is UTF-8 truncated to maximum rather than failing with size_limit."""
    def complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        last_text = str(messages[-1].get("content") or "") if messages else ""
        first_text = str(messages[0].get("content") or "") if messages else ""
        if "CONTEXT CHECKPOINT COMPACTION" in first_text or "Server compaction request:" in last_text:
            return assistant("Goal: 保留关键上下文摘要。" + ("超长摘要内容段——" * 2500))
        return assistant("Done after truncated summary.")

    runtime, model, _, _ = make_runtime(tmp_path)
    model.complete = complete
    runtime.catalog = WindowCatalog(32768, 4096)
    task = runtime.create({"message": "Summarize dense logs on 32k window."})
    tid = task["id"]
    for idx in range(25):
        seed(runtime, tid, exchange(f"svc_{idx}", "log_entry " * 400))

    for _ in range(4):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            break
        advance(runtime)

    saved = runtime.store.read()["tasks"][tid]
    assert saved["status"] == "completed", saved.get("error")
    started_ev = next(ev for ev in saved["events"] if ev["type"] == "context_compaction_started")
    max_bytes = started_ev["summary_max_bytes"]
    checkpoint_text = saved["context_checkpoint"]["content"]
    assert 256 < len(checkpoint_text.encode("utf-8")) <= max_bytes
    assert not any(ev["type"] == "context_compaction_rejected" for ev in saved["events"])


def test_t3_summary_cap_scales_across_120k_240k_and_1_2m_windows():
    """T3: summary_cap returns clamp(hard_limit_bytes // 15, SMALL_SUMMARY_CAP, 48000) across 120k / 240k / 1.2M."""
    assert context_compaction.summary_cap(60_000) == 6000
    assert context_compaction.summary_cap(120_000) == 8000
    assert context_compaction.summary_cap(240_000) == 16_000
    assert context_compaction.summary_cap(1_200_000) == 48_000
    assert context_compaction.summary_cap(2_400_000) == 48_000


def test_t4_129th_reference_evicts_oldest_and_emits_dropped_notice():
    """T4: Adding a 129th reference evicts the oldest entry in insertion order and emits the dropped notice in note()."""
    initial_values = [_fernet_like_ref(i, 100) for i in range(128)]
    previous = {checkpoint_refs.label(v): v for v in initial_values}
    newest_val = _fernet_like_ref(128, 100)
    msgs = [
        assistant(name="ryn_list_resources", args={"resource_ref": newest_val}, call_id="c129")["message"],
        {"role": "tool", "tool_call_id": "c129", "content": json.dumps({"entries": [{"resource_ref": newest_val}]})},
    ]
    _, updated_refs = checkpoint_refs.project(msgs, previous)
    assert len(updated_refs) == 128
    assert checkpoint_refs.label(initial_values[0]) not in updated_refs
    assert checkpoint_refs.label(initial_values[1]) in updated_refs
    assert updated_refs[checkpoint_refs.label(newest_val)] == newest_val

    note_text = checkpoint_refs.note(updated_refs)
    assert "1 older ref_ labels were dropped; list the directory again to get fresh references" in note_text

    with pytest.raises(AgentError, match="agent_checkpoint_references_invalid"):
        checkpoint_refs.project([], {"bad_key": newest_val})


def test_t5_reference_note_bounds_128_max_size_refs_under_32kib_with_unlisted_notice():
    """T5: note() on 128 references of 8192 chars lists only the newest entries within 32 KiB and emits the unlisted notice."""
    big_values = [_fernet_like_ref(i, 8192) for i in range(128)]
    assert all(len(v) == 8192 for v in big_values)
    refs = {checkpoint_refs.label(v): v for v in big_values}

    note_text = checkpoint_refs.note(refs)
    note_bytes = len(note_text.encode("utf-8"))
    assert note_bytes <= checkpoint_refs.REF_NOTE_MAX_BYTES == 32768
    assert big_values[-1] in note_text
    assert big_values[0] not in note_text
    assert "older ref_ values are not listed; list the directory again to get fresh references" in note_text


def test_t6_upload_resource_file_result_is_projected_to_ref_label():
    """T6: ryn_upload_resource_file result resource_ref is replaced with ref_ label and stored in references."""
    dir_ref = _fernet_like_ref(501, 140)
    uploaded_ref = _fernet_like_ref(502, 140)
    msgs = [
        assistant(
            name="ryn_upload_resource_file",
            args={"directory_ref": dir_ref, "name": "report.pdf", "workspace_path": "report.pdf"},
            call_id="up_1",
        )["message"],
        {
            "role": "tool",
            "tool_call_id": "up_1",
            "content": json.dumps(
                {
                    "ok": True,
                    "operation_id": "op_up_1",
                    "name": "report.pdf",
                    "resource_ref": uploaded_ref,
                    "created": "file",
                }
            ),
        },
    ]
    projected, refs = checkpoint_refs.project(msgs)
    parsed_result = json.loads(projected[1]["content"])
    parsed_args = json.loads(projected[0]["tool_calls"][0]["function"]["arguments"])
    assert parsed_result["resource_ref"] == checkpoint_refs.label(uploaded_ref)
    assert parsed_args["directory_ref"] == checkpoint_refs.label(dir_ref)
    assert refs[checkpoint_refs.label(uploaded_ref)] == uploaded_ref
    assert refs[checkpoint_refs.label(dir_ref)] == dir_ref


def test_t7_prefix_reuse_compaction_request_matches_previous_normal_request_byte_for_byte(tmp_path):
    """T7: Prefix-reuse compaction request's first N messages match the previous normal request byte-for-byte and pass active specs."""
    recorded: list[tuple[list[dict], list[dict]]] = []

    def complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        recorded.append((copy.deepcopy(messages), copy.deepcopy(specs)))
        last_text = str(messages[-1].get("content") or "") if messages else ""
        if "Server compaction request:" in last_text:
            return assistant("Goal: inspect services.\nFindings: 0..5 checked.\nNext: finish.")
        if len(recorded) == 1:
            return assistant(name="inspect_service", args={"service_id": "svc_last"}, call_id="call_last")
        return assistant("All services inspected.")

    runtime, model, tools, _ = make_runtime(tmp_path)
    model.complete = complete
    runtime.catalog = WindowCatalog(32768, 4096)
    task = runtime.create({"message": "Inspect services with prefix cache reuse."})
    tid = task["id"]

    # Seed enough closed exchanges so step 1 runs normally (~67 KB <= 82 KB), then step 1's inline 14 KB tool output (< max_output_chars) triggers proactive compaction!
    for idx in range(9):
        seed(runtime, tid, exchange(f"svc_{idx}", "observation_payload " * 220))
    orig_exec = tools.execute
    tools.execute = lambda name, arguments, **kw: {**orig_exec(name, arguments, **kw), "extra": "growth " * 2000}

    for _ in range(6):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            break
        advance(runtime)

    assert runtime.get(tid)["status"] == "completed"
    assert len(recorded) >= 3
    normal_msgs, normal_specs = recorded[0]
    compact_msgs, compact_specs = recorded[1]
    assert "Server compaction request:" in compact_msgs[-1]["content"]
    assert compact_msgs[-1]["role"] == "user"
    assert "Do not call tools" in compact_msgs[-1]["content"]
    assert compact_specs == normal_specs and len(compact_specs) > 0
    prefix_n = min(len(normal_msgs), len(compact_msgs) - 1)
    assert prefix_n == len(normal_msgs) or prefix_n > 2
    assert encoded(compact_msgs[:prefix_n]) == encoded(normal_msgs[:prefix_n])


def test_t8_prefix_reuse_summary_output_replaces_full_fernet_refs_with_short_labels(tmp_path):
    """T8: Prefix-reuse post-processes full gAAAA... tokens in summary output into ref_ labels and stores them in references."""
    raw_ref = _fernet_like_ref(808, 180)
    short_label = checkpoint_refs.label(raw_ref)
    seen_compaction_msgs: list[list[dict]] = []

    def complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        last_text = str(messages[-1].get("content") or "") if messages else ""
        if "Server compaction request:" in last_text:
            seen_compaction_msgs.append(copy.deepcopy(messages))
            return assistant(f"Goal: read resource.\nNext: call ryn_read_resource on {raw_ref} next.")
        return assistant("Completed with post-processed ref.")

    runtime, model, _, _ = make_runtime(tmp_path)
    model.complete = complete
    runtime.catalog = WindowCatalog(64000, 4096)
    task = runtime.create({"message": "Track exact resource reference across prefix compaction."})
    tid = task["id"]

    # Seed exchanges containing raw_ref so proactive compaction triggers via prefix-reuse
    seed(
        runtime,
        tid,
        [
            assistant(name="ryn_list_resources", args={"resource_ref": raw_ref}, call_id="r0")["message"],
            {
                "role": "tool",
                "tool_call_id": "r0",
                "content": json.dumps({"entries": [{"name": "doc.txt", "resource_ref": raw_ref}], "pad": "p " * 3000}),
                "context_epoch": 0,
            },
        ],
    )
    for idx in range(1, 22):
        seed(runtime, tid, exchange(f"fill_{idx}", "filler_text " * 550))

    for _ in range(5):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            break
        advance(runtime)

    saved = runtime.store.read()["tasks"][tid]
    assert saved["status"] == "completed"
    assert seen_compaction_msgs, "expected prefix-reuse compaction"
    # Input prefix kept raw_ref untouched (not pre-projected)
    assert raw_ref in encoded(seen_compaction_msgs[0][:-1]).decode("utf-8")
    # Saved checkpoint summary replaced raw_ref with short_label and recorded raw_ref in references
    assert raw_ref not in saved["context_checkpoint"]["content"]
    assert short_label in saved["context_checkpoint"]["content"]
    assert saved["context_checkpoint"]["references"][short_label] == raw_ref


def test_t9_prefix_reuse_falls_back_to_json_summary_path_when_over_capacity(tmp_path):
    """T9: Prefix-reuse is used when within capacity, and falls back to the JSON summary path when prefix + tools exceed capacity."""
    compaction_calls: list[tuple[list[dict], list[dict]]] = []

    def complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        first_text = str(messages[0].get("content") or "") if messages else ""
        last_text = str(messages[-1].get("content") or "") if messages else ""
        if not specs or "CONTEXT CHECKPOINT COMPACTION" in first_text or "Server compaction request:" in last_text:
            compaction_calls.append((copy.deepcopy(messages), copy.deepcopy(specs)))
            return assistant("Goal: inspect.\nFindings: summary recorded.\nNext: answer.")
        return assistant("Answered.")

    # Part A: Spacious window uses prefix-reuse (specs non-empty)
    rt_wide, m_wide, _, _ = make_runtime(tmp_path / "wide")
    m_wide.complete = complete
    rt_wide.catalog = WindowCatalog(32768, 4096)
    t_wide = rt_wide.create({"message": "Spacious window uses prefix reuse."})
    for idx in range(22):
        seed(rt_wide, t_wide["id"], exchange(f"w_{idx}", "obs_wide " * 350))
    for _ in range(4):
        if rt_wide.get(t_wide["id"])["status"] not in {"queued", "running"}:
            break
        advance(rt_wide)
    assert compaction_calls and len(compaction_calls[0][1]) > 0
    assert "Server compaction request:" in compaction_calls[0][0][-1]["content"]

    # Part B: Tight manual window where instructions + specs + 1 large exchange > cap falls back to JSON summary (specs=[])
    compaction_calls.clear()
    runtime, model, _, _ = make_runtime(tmp_path / "tight")
    model.complete = complete
    runtime.update_settings({"max_context_chars": 15000})
    task = runtime.create({"message": "Compact via fallback when prefix + tools exceed window."})
    tid = task["id"]
    seed(runtime, tid, exchange("single_heavy", "obs " * 1400))

    for _ in range(4):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            break
        advance(runtime)

    assert runtime.get(tid)["status"] == "completed"
    assert compaction_calls
    fallback_msgs, fallback_specs = compaction_calls[0]
    assert fallback_specs == []
    assert fallback_msgs[0]["role"] == "system"
    parsed_source = json.loads(fallback_msgs[1]["content"])
    assert "history" in parsed_source


def test_t10_compact_prompt_setting_overrides_body_and_keeps_hard_maximum_clause(tmp_path):
    """T10: compact_prompt setting (<=4000 bytes) overrides built-in instruction body while keeping 'hard maximum is N bytes'."""
    seen_prompts: list[str] = []

    def complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        last_text = str(messages[-1].get("content") or "") if messages else ""
        first_text = str(messages[0].get("content") or "") if messages else ""
        if "CUSTOM_HANDOFF_FORMAT_XYZ" in last_text or "CUSTOM_HANDOFF_FORMAT_XYZ" in first_text:
            seen_prompts.append(last_text if "CUSTOM_HANDOFF_FORMAT_XYZ" in last_text else first_text)
            return assistant("CUSTOM_SUMMARY: concise handoff.")
        return assistant("Completed with custom compact_prompt.")

    runtime, model, _, _ = make_runtime(tmp_path)
    model.complete = complete
    runtime.catalog = WindowCatalog(32768, 4096)

    with pytest.raises(AgentError, match="agent_invalid_settings"):
        runtime.update_settings({"compact_prompt": "x" * 4001})

    updated = runtime.update_settings({"compact_prompt": "CUSTOM_HANDOFF_FORMAT_XYZ: summarize in two lines."})
    assert updated["compact_prompt"] == "CUSTOM_HANDOFF_FORMAT_XYZ: summarize in two lines."
    assert runtime.settings()["compact_prompt"] == "CUSTOM_HANDOFF_FORMAT_XYZ: summarize in two lines."

    task = runtime.create({"message": "Run with custom compact prompt."})
    tid = task["id"]
    for idx in range(22):
        seed(runtime, tid, exchange(f"s_{idx}", "payload_word " * 350))

    for _ in range(5):
        if runtime.get(tid)["status"] not in {"queued", "running"}:
            break
        advance(runtime)

    assert runtime.get(tid)["status"] == "completed"
    assert seen_prompts
    assert "CUSTOM_HANDOFF_FORMAT_XYZ: summarize in two lines." in seen_prompts[0]
    assert "You are performing a CONTEXT CHECKPOINT COMPACTION." not in seen_prompts[0]
    assert "hard maximum is " in seen_prompts[0] and " bytes" in seen_prompts[0]


def test_t11_low_reasoning_override_only_sent_when_model_supports_low(tmp_path):
    """T11: Compaction passes reasoning_effort='low' only when the model supports 'low', else keeps task effort."""
    recorded_efforts: list[tuple[str, str | None]] = []

    class EffortModel:
        supports_reasoning_effort = True

        def complete(self, _model_id, messages, _specs, _cancelled, _on_event=None, **options):
            last_text = str(messages[-1].get("content") or "") if messages else ""
            first_text = str(messages[0].get("content") or "") if messages else ""
            is_compact = "Server compaction request:" in last_text or "CONTEXT CHECKPOINT COMPACTION" in first_text
            recorded_efforts.append(("compact" if is_compact else "main", options.get("reasoning_effort")))
            if is_compact:
                return assistant("Goal: test effort.\nNext: finish.")
            return assistant("Done.")

    def run_case(sub_dir: str, supported_levels: list[str] | None, task_effort: str | None) -> list[tuple[str, str | None]]:
        recorded_efforts.clear()
        case_dir = tmp_path / sub_dir
        case_dir.mkdir()
        key = peer_box.load_or_create_messaging_key(case_dir / "msg.key")
        model_meta: dict = {
            "id": "local/codex",
            "name": "Codex",
            "source": "local_api",
            "adapter": "codex_cli",
            "available": True,
            "agent_supported": True,
            "context_window": 32768,
            "context_window_known": True,
            "max_output_tokens": 4096,
        }
        if supported_levels is not None:
            model_meta["supported_reasoning_efforts"] = supported_levels
            model_meta["default_reasoning_effort"] = supported_levels[-1]
        catalog = SimpleNamespace(list_models=lambda: [copy.deepcopy(model_meta)])
        _, _, tools, _ = make_runtime(case_dir / "dummy")
        rt = AgentRuntime(AgentStore(case_dir / "store", key), EffortModel(), tools, catalog)
        rt.update_settings({"enabled": True, "model_id": "local/codex"})
        create_body: dict = {"message": "Check compaction reasoning effort."}
        if task_effort is not None:
            create_body["reasoning_effort"] = task_effort
        t = rt.create(create_body)
        for idx in range(22):
            seed(rt, t["id"], exchange(f"item_{idx}", "effort_data " * 350))
        for _ in range(5):
            if rt.get(t["id"])["status"] not in {"queued", "running"}:
                break
            advance(rt)
        assert rt.get(t["id"])["status"] == "completed"
        return list(recorded_efforts)

    # Case 1: Model supports 'low' while task requested 'high' -> compact uses 'low', main uses 'high'
    efforts_with_low = run_case("supports_low", ["low", "medium", "high"], "high")
    assert ("compact", "low") in efforts_with_low
    assert ("main", "high") in efforts_with_low

    # Case 2: Model supports only ['medium', 'high'] (no 'low') -> compact keeps task's 'high'
    efforts_without_low = run_case("no_low", ["medium", "high"], "high")
    assert ("compact", "high") in efforts_without_low
    assert ("main", "high") in efforts_without_low

    # Case 3: Model has no supported_reasoning_efforts -> compact sends None
    efforts_unsupported = run_case("none_supported", None, None)
    assert ("compact", None) in efforts_unsupported


def test_t12_compaction_incomplete_retries_twice_before_failing(tmp_path):
    """T12: agent_compaction_incomplete has retry limit 2 (fails only after the 2nd retry, i.e., 3rd total attempt)."""
    assert retry_policy.limit("agent_compaction_incomplete", {}) == 2
    assert retry_policy.limit("agent_compaction_size_limit", {}) == 1
    assert retry_policy.limit("agent_invalid_model_response", {}) == 1

    inc_1 = {**assistant("Partial 1"), "finish_reason": "length"}
    inc_2 = {**assistant("Partial 2"), "finish_reason": "length"}
    inc_3 = {**assistant("Partial 3"), "finish_reason": "length"}

    runtime, model, _, _ = make_runtime(tmp_path, inc_1, inc_2, inc_3)
    runtime.catalog = WindowCatalog(32768, 4096)
    task = runtime.create({"message": "Test incomplete compaction retries."})
    tid = task["id"]
    for idx in range(22):
        seed(runtime, tid, exchange(f"r_{idx}", "retry_payload " * 350))

    # Attempt 1 -> retry 1 scheduled (queued)
    advance(runtime)
    after_1 = runtime.get(tid)
    assert after_1["status"] == "queued" and after_1["error"] is None

    # Attempt 2 (1st retry) -> retry 2 scheduled (queued)
    advance(runtime)
    after_2 = runtime.get(tid)
    assert after_2["status"] == "queued" and after_2["error"] is None

    # Attempt 3 (2nd retry) -> exhausted limit=2 -> failed with agent_compaction_incomplete
    advance(runtime)
    after_3 = runtime.get(tid)
    assert after_3["status"] == "failed"
    assert after_3["error"] == "agent_compaction_incomplete"
    assert len(model.calls) == 3
