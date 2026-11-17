"""S13-I8 output reserve, multilingual input, and explicit summary preparation."""
import copy
import json

import pytest

from rynmesh.ask_ryn.store import ConversationError
from rynmesh.llm_package.context_safety import estimate_context_safety_tokens
from tests.test_ask_context import setup
from tests.test_ask_runs import setup as setup_run


def context(tmp_path, *, text="Article", window=16384, maximum=8192):
    service, row, _ = setup(tmp_path, text=text, window=window)
    records = service.catalog("network")
    records[0]["service"]["max_output_tokens"] = maximum
    service.catalog = lambda _: records
    return service, row


@pytest.mark.parametrize("maximum,expected", [(8192, 2048), (512, 512)])
def test_T1_T2_reserve_uses_default_or_service_limit(tmp_path, maximum, expected):
    service, row = context(tmp_path, maximum=maximum)
    assert service.preview(row, "Question")["max_output_tokens"] == expected


def test_T3_small_window_retries_with_256_and_explains_reserve(tmp_path):
    service, row = context(tmp_path, window=2048)
    preview = service.preview(row, "Question")
    assert preview["max_output_tokens"] == 256
    assert "输出预留过大" in preview["output_reserve_warning"]
    assert preview["input_token_upper_estimate"] + preview["framing_reserve"] + 256 <= 2048


def test_T3_unusable_fallback_explains_output_reserve(tmp_path):
    service, row = context(tmp_path, window=1000)
    with pytest.raises(ConversationError, match="ask_output_reserve_too_large"):
        service.preview(row, "Question")


def test_T4_omission_is_explicit_and_original_history_is_unchanged(tmp_path):
    service, row = context(tmp_path, window=4096, maximum=256)
    row["messages"] = [{"role": "assistant", "status": "complete", "content": str(i) + "旧消息" * 500} for i in range(8)]
    original = copy.deepcopy(row)
    preview = service.preview(row, "Question")
    assert preview["history_messages_omitted"] > 0
    assert row == original
    assert preview["input_token_upper_estimate"] == estimate_context_safety_tokens(preview["prompt"])


def test_T5_4k_window_fits_2500_chinese_characters(tmp_path):
    # 256 output + 1024 reserve leaves 2816 input tokens, including framing.
    service, row = context(tmp_path, text="汉" * 2500, window=4096, maximum=256)
    preview = service.preview(row, "说明文章")
    assert preview["sources"][0]["included_bytes"] == 7500
    assert not preview["sources"][0]["budget_truncated"]
    assert preview["input_token_upper_estimate"] + 1024 + 256 <= 4096


def test_T6_large_window_is_not_capped_at_64k_bytes(tmp_path):
    service, row = context(tmp_path, text="word " * 16000, window=200000)
    preview = service.preview(row, "Question")
    assert len(preview["prompt"].encode()) > 65536
    assert not preview["sources"][0]["budget_truncated"]


def test_T7_token_clipping_reports_article_truncation(tmp_path):
    service, row = context(tmp_path, text="汉" * 10000, window=4096, maximum=256)
    preview = service.preview(row, "Question")
    assert preview["sources"][0]["budget_truncated"]
    assert preview["input_token_upper_estimate"] == estimate_context_safety_tokens(preview["prompt"])
    assert preview["input_token_upper_estimate"] <= 4096 - 1024 - 256


@pytest.mark.parametrize("structured", [False, True])
def test_summary_preview_contains_exact_omitted_history_without_mutating_original(tmp_path, structured):
    service, row = context(tmp_path, text="汉" * 6000, window=8192, maximum=256)
    if structured:
        service.catalog("network")[0]["service"]["capabilities"] = ["chat_messages_v1"]
    row["messages"] = [{"role": "assistant", "status": "complete", "content": f"turn-{i} " + "x" * 600} for i in range(12)]
    # Articles occupy enough input to omit history; the omitted history alone fits.
    original = copy.deepcopy(row)
    ordinary = service.preview(row, "Question")
    omitted = ordinary["history_messages_omitted"]
    assert omitted > 0
    preview = service.summary_preview(row, "Question")
    for message in row["messages"][:omitted]:
        assert message["content"] in preview["prompt"]
    assert preview["sources"] == []
    assert row == original
    assert preview["service_review"]["token"] == ordinary["service_review"]["token"]
    if structured:
        assert preview["chat"]["messages"] == json.loads(preview["prompt"])
        assert "汉" * 6000 not in preview["prompt"]


def test_summary_overflow_fails_without_altering_history(tmp_path):
    service, row = context(tmp_path, window=4096, maximum=256)
    row["messages"] = [{"role": "assistant", "status": "complete", "content": "汉" * 4000}]
    original = copy.deepcopy(row)
    with pytest.raises(ConversationError, match="ask_summary_too_large"):
        service.summary_preview(row, "Question")
    assert row == original


def test_length_finish_reason_survives_real_run_archiving(tmp_path):
    history, runs, orders, request = setup_run(tmp_path)
    runs.begin(request)
    runs.run_once()
    orders.results[request["task_id"]] = {"state": "succeeded", "output": "Partial response", "finish_reason": "length"}
    runs.run_once()
    answer = history.get("conversation")["messages"][-1]
    assert answer["finishReason"] == "length"
    saved = history.save(history.get("conversation"), expected_revision=history.get("conversation")["revision"])
    assert saved["messages"][-1]["finishReason"] == "length"


def test_real_run_dispatch_passes_2048_to_existing_order_and_price_review(tmp_path):
    history, runs, orders, request = setup_run(tmp_path)
    records = runs.context().catalog("network")
    records[0]["service"]["context_window"] = 16384
    records[0]["service"]["max_output_tokens"] = 8192
    runs.context().catalog = lambda _: records
    preview = runs.context().preview(history.get("conversation"), request["question"])
    request.update(prompt_sha256=preview["prompt_sha256"], service_review_token=preview["service_review"]["token"])
    runs.begin(request)
    runs.run_once()
    assert orders.sent[0]["max_tokens"] == 2048
    assert orders.sent[0]["service_review_token"] == preview["service_review"]["token"]


def test_summary_owner_route_rechecks_revision_without_dispatch_or_history_mutation(tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from rynmesh.ask_ryn.routes import install_ask_ryn
    from rynmesh.background_workers import BackgroundWorkerRegistry

    home = tmp_path / "node"
    history, _runs, orders, _request = setup_run(home / "ask-ryn")
    service, material_row = context(tmp_path / "material", text="汉" * 6000, window=8192, maximum=256)
    row = history.get("conversation")
    row["contextIds"] = material_row["contextIds"]
    row["messages"] = [{"id": f"old-{i}", "role": "assistant", "status": "complete",
                        "content": f"turn-{i} " + "x" * 600, "createdAt": row["createdAt"]} for i in range(12)]
    row = history.save(row, expected_revision=row["revision"])
    app = FastAPI()

    def owner(request):
        if request.headers.get("x-owner") != "test-owner":
            raise HTTPException(403)

    state = install_ask_ryn(app, store=SimpleNamespace(home=home), home=home,
                            workers=BackgroundWorkerRegistry(), local_control=owner, messaging_key=history.key)
    state.context, state.orders = service, orders
    client = TestClient(app)
    body = {"conversation_id": row["id"], "expected_revision": row["revision"], "question": "Continue"}
    assert client.post("/api/local/ask/summary-preview", json=body).status_code == 403
    assert client.post("/api/local/ask/summary-preview", headers={"x-owner": "test-owner"}, json={**body, "expected_revision": 0}).status_code == 409
    result = client.post("/api/local/ask/summary-preview", headers={"x-owner": "test-owner"}, json=body)
    assert result.status_code == 200, result.text
    assert "turn-0" in result.json()["prompt"]
    assert result.json()["provider_peer_id"] == row["providerPeerId"]
    assert history.get(row["id"]) == row
    assert orders.sent == []


def test_large_structured_prompt_reaches_real_chat_admission_path(tmp_path):
    from rynmesh.llm_package.chat import validate_chat
    from rynmesh.llm_package.chat_prompt import CHAT_FORMAT, decode_chat_prompt
    from rynmesh.ask_ryn.runs import AskRunService

    history, _runs, orders, request = setup_run(tmp_path / "history")
    service, material_row = context(tmp_path / "material", text="word " * 16000, window=200000)
    records = service.catalog("network")
    records[0]["service"]["capabilities"] = [CHAT_FORMAT]
    row = history.get("conversation")
    row["contextIds"] = material_row["contextIds"]
    row = history.save(row, expected_revision=row["revision"])
    preview = service.preview(row, request["question"])
    assert len(preview["prompt"].encode()) > 65536
    runs = AskRunService(history, lambda: service, lambda: orders)
    request.update(expected_revision=row["revision"], prompt_sha256=preview["prompt_sha256"],
                   service_review_token=preview["service_review"]["token"])
    runs.begin(request)
    runs.run_once()
    sent = orders.sent[0]
    assert validate_chat(sent["chat"])["messages"] == json.loads(preview["prompt"])
    assert sent["max_tokens"] == sent["chat"]["max_tokens"] == 2048
    assert sent["chat"]["stream"] is True
    # execute_order calls these same validators; the bounded legacy decoder is
    # deliberately not used for the complete chat-field payload.
    assert decode_chat_prompt(sent["prompt"], sent.get("prompt_format", "text")) is None
