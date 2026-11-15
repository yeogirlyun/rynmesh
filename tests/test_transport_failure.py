"""S01-I4 unit tests for transport_failure: describe, classify_stream_error, bounded bodies, and image_rejection."""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from rynmesh.agent_runtime.diagnostic_log import safe_diagnostics
from rynmesh.llm_package import transport_failure
from rynmesh.llm_package.transport_failure import describe, image_rejection, read_http_failure


def _http_error(status: int, body: bytes | dict | None = None, headers: dict[str, str] | None = None) -> HTTPError:
    if isinstance(body, dict):
        raw = json.dumps(body).encode("utf-8")
    elif isinstance(body, bytes):
        raw = body
    else:
        raw = b""
    return HTTPError("https://api.example.invalid/v1/chat/completions", status, "err", headers or {}, io.BytesIO(raw))


@pytest.mark.parametrize(
    ("status", "envelope", "expected_reason", "expected_overloaded", "expected_image_rejected"),
    [
        (400, {"error": {"type": "invalid_request_error", "message": "PRIVATE_BAD_PARAM"}}, "request_rejected", False, False),
        (400, {"error": {"code": "invalid_request", "message": "PRIVATE_BAD_REQ"}}, "request_rejected", False, False),
        (404, {"error": {"code": "model_not_found", "message": "PRIVATE_MISSING_MODEL"}}, "request_rejected", False, False),
        (422, {"detail": [{"loc": ["body", "messages"], "msg": "PRIVATE_VALIDATION", "type": "missing"}]}, "request_rejected", False, False),
        (400, None, "http", False, False),
        (400, {"error": {"type": "one_api_error", "message": "PRIVATE_CHANNEL_DOWN"}}, "http", False, False),
        (401, {"error": {"type": "authentication_error", "message": "PRIVATE_BAD_KEY"}}, "auth_rejected", False, False),
        (403, None, "auth_rejected", False, False),
        (429, {"error": {"code": "insufficient_quota", "message": "PRIVATE_QUOTA"}}, "quota", False, False),
        (400, {"error": {"code": "context_length_exceeded", "type": "invalid_request_error", "message": "PRIVATE_CTX"}}, "context_overflow", False, False),
        (400, {"error": {"param": "messages[0].content[1].image_url", "type": "invalid_request_error", "message": "PRIVATE_IMG"}}, "http", False, True),
        (529, {"type": "error", "error": {"type": "overloaded_error", "message": "PRIVATE_OVERLOADED"}}, "http", True, False),
        (529, None, "http", True, False),
        (503, {"error": {"type": "server_error", "message": "The engine is currently overloaded, PRIVATE_DETAIL"}}, "http", True, False),
        (503, {"error": {"type": "server_error", "message": "Upstream maintenance PRIVATE_DETAIL"}}, "http", False, False),
    ],
)
def test_t1_describe_http_table(status, envelope, expected_reason, expected_overloaded, expected_image_rejected):
    exc = _http_error(status)
    diag = describe(exc, "request", envelope)
    assert diag["network_reason"] == expected_reason
    assert diag["http_status"] == status
    assert diag["network_phase"] == "request"
    assert (diag.get("overloaded") is True) is expected_overloaded
    assert (diag.get("image_rejected") is True) is expected_image_rejected
    assert "PRIVATE_" not in json.dumps(diag)
    cleaned = safe_diagnostics(diag)
    assert cleaned["network_reason"] == expected_reason
    assert (cleaned.get("overloaded") is True) is expected_overloaded


@pytest.mark.parametrize(
    ("stream_event", "expected_code", "expected_reason", "expected_overloaded"),
    [
        ({"type": "overloaded_error", "message": "PRIVATE_OVERLOAD"}, "upstream_stream_error", "http", True),
        ({"type": "error", "error": {"type": "overloaded_error", "message": "PRIVATE_ANTHROPIC"}}, "upstream_stream_error", "http", True),
        ({"code": "server_error", "message": "PRIVATE_SERVER_ERR"}, "upstream_stream_error", "http", False),
        ({"type": "response.failed", "response": {"status": "failed", "error": {"code": "server_error", "message": "PRIVATE_RESP"}}}, "upstream_stream_error", "http", False),
        ({"type": "api_error", "code": "generation_timeout", "message": "PRIVATE_LOCAL"}, "upstream_stream_error", "http", False),
        ({"type": "rate_limit_error", "code": "rate_limit_exceeded", "message": "PRIVATE_RL"}, "upstream_stream_error", "http", False),
        ({"type": "invalid_request_error", "message": "PRIVATE_INVALID"}, "upstream_request_rejected", "request_rejected", False),
        ({"code": "invalid_request", "message": "PRIVATE_INVALID_CODE"}, "upstream_request_rejected", "request_rejected", False),
        ({"code": "context_length_exceeded", "type": "invalid_request_error", "message": "PRIVATE_CTX"}, "upstream_request_rejected", "context_overflow", False),
    ],
)
def test_t1_classify_stream_error_table(stream_event, expected_code, expected_reason, expected_overloaded):
    assert hasattr(transport_failure, "classify_stream_error"), "classify_stream_error must exist"
    code, diag = transport_failure.classify_stream_error(stream_event)
    assert code == expected_code
    assert diag.get("network_reason") == expected_reason
    assert (diag.get("overloaded") is True) is expected_overloaded
    assert "PRIVATE_" not in json.dumps(diag)


def test_t2_40kib_and_oversized_error_bodies_still_recognise_closed_codes():
    # 40 KiB valid JSON body (> old 16 KiB limit, <= new 64 KiB limit)
    padding = "x" * (40 * 1024)
    body_40k = {
        "error": {
            "code": "insufficient_quota",
            "type": "insufficient_quota",
            "message": f"You exceeded your current quota. PRIVATE_BODY_{padding}",
        }
    }
    diag_40k = read_http_failure(_http_error(429, body_40k))
    assert diag_40k["network_reason"] == "quota"
    assert diag_40k["http_status"] == 429
    assert "PRIVATE_BODY_" not in json.dumps(diag_40k)

    # > 64 KiB truncated/huge body where code/type/param appear within the first 16 KiB
    huge_raw = (
        b'{"error":{"code":"insufficient_quota","type":"insufficient_quota",'
        b'"param":"model","message":"Quota exhausted PRIVATE_HUGE_'
        + (b"z" * (80 * 1024))
    )
    diag_huge = read_http_failure(_http_error(429, huge_raw))
    assert diag_huge["network_reason"] == "quota"
    assert "PRIVATE_HUGE_" not in json.dumps(diag_huge)


def test_t3_model_not_found_with_image_in_model_name_is_not_image_rejection():
    envelope = {
        "error": {
            "message": "The model xxx-image-preview is not available",
            "code": "model_not_found",
            "type": "invalid_request_error",
        }
    }
    assert image_rejection(envelope) is False
    diag = describe(_http_error(404), "request", envelope)
    assert "image_rejected" not in diag
    assert diag["network_reason"] == "request_rejected"

    # Separate sentences: "image" in sentence 1, "not available" in sentence 2 -> must not match fallback
    split_sentences = {
        "error": {
            "message": "Request routed to custom-image-gateway. Streaming is not available on this tier.",
            "type": "invalid_request_error",
        }
    }
    assert image_rejection(split_sentences) is False


@pytest.mark.parametrize(
    "envelope",
    [
        {"error": {"param": "messages[0].content[1].image_url", "type": "invalid_request_error", "message": "Invalid value"}},
        {"error": {"param": "messages[2].content[0].image.source", "type": "invalid_request_error", "message": "Bad input"}},
        {"error": {"param": "input[0].content[1].input_image", "type": "invalid_request_error", "message": "Rejected"}},
        {"error": {"code": "image_not_supported", "message": "Modality error"}},
        {"error": {"code": "invalid_image", "message": "Corrupt payload"}},
        {"error": {"code": "unsupported_content_type", "message": "Unsupported block"}},
    ],
)
def test_t4_structured_param_and_code_recognised_as_image_rejection(envelope):
    assert image_rejection(envelope) is True
    diag = describe(_http_error(400), "request", envelope)
    assert diag["image_rejected"] is True
    assert diag["network_reason"] == "http"
