"""S03-I17 tests: compacted owner message floor, multimodal omission, and continuation threshold."""
from __future__ import annotations

import base64
import copy
import io

import pytest
from PIL import Image
from test_agent_runtime import advance, assistant, make_runtime

from rynmesh.agent_runtime import image_capabilities, request_context
from rynmesh.agent_runtime.store import AgentError, encoded

PLACEHOLDER = "[image omitted from compacted context]"


def _png_bytes(width: int = 24, height: int = 24, color: tuple[int, int, int] = (20, 80, 160)) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color)
    img.save(buf, format="PNG", compress_level=0)
    return buf.getvalue()


def _png_data_url(seed: int) -> str:
    raw = _png_bytes(48, 48, ((seed * 37) % 256, (seed * 73) % 256, (seed * 109) % 256))
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _png_attachment(name: str, seed: int) -> dict:
    raw = _png_bytes(48, 48, ((seed * 41) % 256, (seed * 83) % 256, (seed * 127) % 256))
    return {
        "name": name,
        "media_type": "image/png",
        "data_base64": base64.b64encode(raw).decode("ascii"),
    }


def _window_model(window: int = 32768, *, output: int = 4096, **extra) -> dict:
    return {
        "id": "local:test",
        "name": "Fixture",
        "source": "local_api",
        "available": True,
        "agent_supported": True,
        "context_window": window,
        "context_window_known": True,
        "context_window_basis": "owner_model_override",
        "max_output_tokens": output,
        **extra,
    }


def _configure(runtime, metadata: dict) -> None:
    runtime.catalog.list_models = lambda: [copy.deepcopy(metadata)]


def _send(runtime, task_id: str, message: str, *, attachments: list[dict] | None = None) -> dict:
    row = runtime.get(task_id)
    body: dict = {
        "message": message,
        "intent": "information",
        "expected_control_revision": row["control_revision"],
        "expected_goal_revision": row["goal"]["revision"],
    }
    if attachments is not None:
        body["attachments"] = attachments
    return runtime.add_message(task_id, body)


def _install_auto_responder(model) -> None:
    def dynamic_complete(_model_id, messages, specs, _cancelled, _on_event=None, **_options):
        model.calls.append((_model_id, copy.deepcopy(messages), copy.deepcopy(specs)))
        first_text = str(messages[0].get("content") or "") if messages else ""
        if "CONTEXT CHECKPOINT COMPACTION" in first_text:
            return assistant(
                "Goal: preserve active requirements from earlier owner messages.\n"
                "Next: answer the latest owner request."
            )
        return assistant(f"Reply #{len(model.calls)}")

    model.complete = dynamic_complete


def _settle_task(runtime, task_id: str, max_ticks: int = 8) -> dict:
    for _ in range(max_ticks):
        current = runtime.get(task_id)
        if current["status"] not in {"queued", "running"}:
            return current
        advance(runtime)
    return runtime.get(task_id)


def test_t1_old_multimodal_owner_message_compacts_images_to_placeholders_and_counts_only_text_bytes(tmp_path):
    """T1: Old owner message with 3 base64 images keeps only text + placeholders after compaction, and byte budget counts only text."""
    old_msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Inspect all three diagrams carefully."},
            {"type": "image_url", "image_url": {"url": _png_data_url(1), "detail": "auto"}},
            {"type": "image_url", "image_url": {"url": _png_data_url(2), "detail": "auto"}},
            {"type": "image_url", "image_url": {"url": _png_data_url(3), "detail": "auto"}},
        ],
    }
    latest_msg = {"role": "user", "content": "Now summarize what changed."}
    exact_text_bytes = len("Inspect all three diagrams carefully.".encode("utf-8")) + len(
        "Now summarize what changed.".encode("utf-8")
    )

    built = request_context.build_context(
        [{"role": "developer", "content": "sys"}],
        [old_msg, {"role": "assistant", "content": "Noted."}, latest_msg],
        checkpoint={"through": 2, "content": "Compacted turn 1."},
        owner_budget=exact_text_bytes,
    )
    compacted_old = built[1]
    assert compacted_old["role"] == "user"
    assert compacted_old["content"] == [
        {"type": "text", "text": "Inspect all three diagrams carefully."},
        {"type": "text", "text": PLACEHOLDER},
        {"type": "text", "text": PLACEHOLDER},
        {"type": "text", "text": PLACEHOLDER},
    ]
    assert not image_capabilities.message_has_image_input([compacted_old])

    # Also verify attachment-backed multimodal owner messages in full AgentRuntime context projection.
    runtime, model, _, _ = make_runtime(tmp_path)
    _install_auto_responder(model)
    _configure(runtime, _window_model(128000, output=4096, supports_image_input=True))
    created = runtime.create(
        {
            "message": "Initial visual review.",
            "attachments": [
                _png_attachment("img1.png", 10),
                _png_attachment("img2.png", 11),
                _png_attachment("img3.png", 12),
            ],
        }
    )
    advance(runtime)
    _send(runtime, created["id"], "Follow-up text turn without images.")
    advance(runtime)

    def install_checkpoint(data):
        t = data["tasks"][created["id"]]
        t["context_checkpoint"] = {"through": 2, "content": "Reviewed initial images."}

    runtime.store.transaction(install_checkpoint)
    stored = runtime.store.read()["tasks"][created["id"]]
    projected = list(runtime._context(stored))
    assert not image_capabilities.message_has_image_input(projected)
    user_lists = [m["content"] for m in projected if m.get("role") == "user" and isinstance(m.get("content"), list)]
    assert len(user_lists) == 1
    placeholder_parts = [p for p in user_lists[0] if p == {"type": "text", "text": PLACEHOLDER}]
    assert len(placeholder_parts) == 3


def test_t2_latest_owner_message_retains_original_images(tmp_path):
    """T2: Latest owner message keeps its original image parts while earlier owner messages strip theirs."""
    old_url = _png_data_url(21)
    latest_url = _png_data_url(22)
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Old screenshot"},
                {"type": "image_url", "image_url": {"url": old_url, "detail": "auto"}},
            ],
        },
        {"role": "assistant", "content": "Checked old screenshot."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Latest screenshot"},
                {"type": "image_url", "image_url": {"url": latest_url, "detail": "auto"}},
            ],
        },
        {"role": "assistant", "content": "Checked latest screenshot."},
    ]
    # Subcase A: checkpoint before latest owner message (through=2)
    built_before = request_context.build_context(
        [{"role": "developer", "content": "sys"}],
        history,
        checkpoint={"through": 2, "content": "Summary"},
        owner_budget=10_000,
    )
    digests_before = image_capabilities.extract_message_image_digests(built_before)
    assert len(digests_before) == 1
    assert latest_url in str(built_before)
    assert old_url not in str(built_before)

    # Subcase B: checkpoint covers entire closed prefix including latest owner message (through=4)
    built_through_all = request_context.build_context(
        [{"role": "developer", "content": "sys"}],
        history,
        checkpoint={"through": 4, "content": "Summary"},
        owner_budget=10_000,
    )
    digests_all = image_capabilities.extract_message_image_digests(built_through_all)
    assert len(digests_all) == 1
    assert latest_url in str(built_through_all)
    assert old_url not in str(built_through_all)


def test_t3_eighty_long_owner_messages_compact_and_continue_in_same_task(tmp_path):
    """T3: 80 long owner messages (total > model capacity) compact and continue in the same task without ContinueInNewTask."""
    runtime, model, _, _ = make_runtime(tmp_path)
    _install_auto_responder(model)

    first = runtime.create({"message": "Turn 000 original objective: " + ("alpha " * 400)})
    _settle_task(runtime, first["id"])
    tid = first["id"]
    total_owner_bytes = len(first["messages"][0]["content"].encode("utf-8"))

    for turn in range(1, 80):
        text = f"Turn {turn:03d} requirement update: " + ("bravo " * 400)
        total_owner_bytes += len(text.encode("utf-8"))
        updated = _send(runtime, tid, text)
        assert updated["id"] == tid, f"unexpected continuation on turn {turn}"
        _settle_task(runtime, tid)

    stored = runtime.store.read()["tasks"][tid]
    capacity = runtime._effective_capacity(stored)[0]
    assert total_owner_bytes > capacity * 1.5
    assert stored.get("continued_to") is None
    assert len(runtime.store.read()["tasks"]) == 1
    assert stored["status"] == "completed"
    assert any(ev["type"] == "context_compacted" for ev in stored["events"])


def test_t4_first_owner_message_is_always_retained_and_capped_at_8kb():
    """T4: First owner message is always retained in the request (and truncated at 8 KB with ...[truncated] if oversized)."""
    anchor = "ORIGINAL_REQUEST_ANCHOR: build deterministic ledger reconciliation."
    messages = [{"role": "user", "content": anchor}]
    for idx in range(1, 25):
        messages.append({"role": "user", "content": f"Followup {idx:02d}: " + ("padding " * 400)})

    selected = request_context._select_compacted_owner_messages(messages, max_bytes=16_000)
    assert selected[0]["content"] == anchor
    assert selected[-1]["content"] == messages[-1]["content"]

    oversized_first = "FIRST_PREFIX_" + ("X" * 12_000)
    selected_oversized = request_context._select_compacted_owner_messages(
        [
            {"role": "user", "content": oversized_first},
            {"role": "user", "content": "Recent instruction that must still fit."},
        ],
        max_bytes=20_000,
    )
    assert len(selected_oversized) == 2
    assert selected_oversized[0]["content"].startswith("FIRST_PREFIX_")
    assert selected_oversized[0]["content"].endswith("...[truncated]")
    prefix_without_marker = selected_oversized[0]["content"][: -len("...[truncated]")]
    assert len(prefix_without_marker.encode("utf-8")) == 8 * 1024
    assert selected_oversized[1]["content"] == "Recent instruction that must still fit."


def test_t5_latest_owner_message_is_retained_verbatim_in_request(tmp_path):
    """T5: Latest owner message is retained verbatim in the dispatched request after earlier messages exceed budget."""
    runtime, model, _, _ = make_runtime(tmp_path)
    _install_auto_responder(model)

    task = runtime.create({"message": "Initial goal anchor for T5."})
    _settle_task(runtime, task["id"])
    tid = task["id"]

    for turn in range(1, 18):
        out = _send(runtime, tid, f"Intermediate constraint {turn:02d}: " + ("clause " * 1100))
        assert out["id"] == tid
        _settle_task(runtime, tid)

    latest_exact = "LATEST_VERBATIM_OWNER_MESSAGE_T5: output strictly in RFC3339 UTC format."
    out = _send(runtime, tid, latest_exact)
    assert out["id"] == tid
    _settle_task(runtime, tid)

    _, last_request_messages, _ = model.calls[-1]
    user_contents = [m.get("content") for m in last_request_messages if m.get("role") == "user"]
    assert latest_exact in user_contents
    assert "Initial goal anchor for T5." in user_contents


def test_t6_earlier_middle_owner_messages_only_appear_in_compaction_summary_input(tmp_path):
    """T6: Earlier middle owner messages dropped from verbatim budget appear in compaction summary input and not in post-compaction request."""
    runtime, model, _, _ = make_runtime(tmp_path)
    _install_auto_responder(model)

    first_marker = "FIRST_OWNER_ANCHOR_T6"
    early_marker = "EARLY_DROPPED_OWNER_CONSTRAINT_T6_UNIQUE"
    latest_marker = "FINAL_OWNER_REQUEST_T6_UNIQUE"

    task = runtime.create({"message": first_marker})
    _settle_task(runtime, task["id"])
    tid = task["id"]

    _send(runtime, tid, f"{early_marker}: " + ("earlypad " * 800))
    _settle_task(runtime, tid)

    for turn in range(2, 16):
        out = _send(runtime, tid, f"Later owner turn {turn:02d}: " + ("laterpad " * 900))
        assert out["id"] == tid
        _settle_task(runtime, tid)

    out = _send(runtime, tid, latest_marker)
    assert out["id"] == tid
    _settle_task(runtime, tid)

    summary_calls = [
        msgs
        for _, msgs, _ in model.calls
        if msgs and "CONTEXT CHECKPOINT COMPACTION" in str(msgs[0].get("content") or "")
    ]
    assert summary_calls, "expected at least one compaction summary call"
    summary_system = summary_calls[0][0]["content"]
    assert "Goal" in summary_system
    assert "not retained verbatim" in summary_system.lower() or "earlier owner messages" in summary_system.lower()
    assert any(early_marker in str(msgs[-1].get("content") or "") for msgs in summary_calls)

    _, final_main_msgs, _ = model.calls[-1]
    serialized_final = encoded(final_main_msgs).decode("utf-8")
    assert first_marker in serialized_final
    assert latest_marker in serialized_final
    assert early_marker not in serialized_final


def test_t7_single_oversized_new_message_still_raises_agent_context_limit(tmp_path):
    """T7: Single new message too large on its own raises agent_context_limit without creating a continuation task."""
    runtime, model, _, _ = make_runtime(tmp_path)
    _install_auto_responder(model)

    task = runtime.create({"message": "Start T7 session."})
    _settle_task(runtime, task["id"])
    tid = task["id"]

    for turn in range(1, 13):
        out = _send(runtime, tid, f"History {turn:02d}: " + ("item " * 1400))
        assert out["id"] == tid
        _settle_task(runtime, tid)

    before_count = len(runtime.store.read()["tasks"])
    with pytest.raises(AgentError, match="agent_context_limit"):
        _send(runtime, tid, "huge " * 40_000)
    assert len(runtime.store.read()["tasks"]) == before_count
    assert runtime.get(tid).get("continued_to") is None


def test_t8_32k_window_owner_budget_is_quarter_of_effective_capacity(tmp_path):
    """T8: Under a 32k context window, compacted owner message budget equals effective_capacity // 4 (not 60,000)."""
    runtime, model, _, _ = make_runtime(tmp_path)
    _install_auto_responder(model)
    _configure(runtime, _window_model(32768, output=4096))

    task = runtime.create({"message": "Anchor 32k window request."})
    advance(runtime)
    tid = task["id"]

    def populate_compacted_owners(data):
        t = data["tasks"][tid]
        msgs = [{"role": "user", "content": "Anchor 32k window request."}, {"role": "assistant", "content": "Ack 0"}]
        for idx in range(1, 16):
            msgs.append({"role": "user", "content": f"32k-owner-{idx:02d}: " + ("z" * 3000)})
            msgs.append({"role": "assistant", "content": f"Ack {idx}"})
        t["messages"] = msgs
        t["context_checkpoint"] = {"through": len(msgs), "content": "Compacted 32k history."}

    runtime.store.transaction(populate_compacted_owners)
    stored = runtime.store.read()["tasks"][tid]
    capacity = runtime._effective_capacity(stored)[0]
    expected_quarter = capacity // 4
    assert 0 < expected_quarter < request_context.COMPACT_USER_MESSAGE_MAX_BYTES

    projected = list(runtime._context(stored))
    retained_owner_msgs = [
        m for m in projected if m.get("role") == "user" and ("Anchor 32k" in str(m.get("content")) or "32k-owner-" in str(m.get("content")))
    ]
    marker_len = len("...[truncated]".encode("utf-8"))
    raw_budget_bytes = 0
    for m in retained_owner_msgs:
        text = m["content"]
        b = len(text.encode("utf-8"))
        if text.endswith("...[truncated]"):
            b -= marker_len
        raw_budget_bytes += b

    assert raw_budget_bytes <= expected_quarter
    assert any("Anchor 32k window request." == m["content"] for m in retained_owner_msgs)
    assert not any("32k-owner-01:" in m["content"] for m in retained_owner_msgs)
