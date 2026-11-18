"""S12-I16: exact secret keys without losing model usage statistics."""
import pytest
from test_agent_integrations import node
from rynmesh.agent_runtime.bridge import build_agent_integrations
from rynmesh.agent_runtime.tools import bounded_result


STATS = {"max_output_tokens": 8192, "tokens_per_day": 500_000,
         "context_admission_bytes_per_token": 3.5, "tokenizer": "test",
         "mytokenvalue": "public statistic", "password_policy": "configured"}
SECRETS = ["api_key", "apikey", "authorization", "password", "passphrase", "secret", "client_secret",
           "private_key", "invitation", "invitation_code", "token", "access_token", "refresh_token",
           "id_token", "auth_token", "session_token", "bearer"]


def test_T1_s12_i16_statistics_and_nested_counts_are_retained():
    value = {"models": [{"id": "model", **STATS, "usage": dict(STATS)}]}
    assert bounded_result(value) == value


@pytest.mark.parametrize("name", ["ryn_list_models", "ryn_inspect_services"])
def test_T1_s12_i16_real_registry_keeps_model_limits_and_hides_keys(node, monkeypatch, name):
    app, store = node
    _, tools, catalog = build_agent_integrations(app, store, store.home)
    monkeypatch.setattr(catalog, "list_models", lambda: [{"id": "model", **STATS, "openai_api_key": "hidden", "Authorization": "hidden"}])
    result = tools.execute(name, {}, operation_id="model-read", cancelled=lambda: False)
    assert result["models"] == [{"id": "model", **STATS}]


@pytest.mark.parametrize("key", SECRETS)
def test_T2_s12_i16_exact_names_case_and_nested_values_are_removed(key):
    assert bounded_result({key: "hidden", key.upper(): "hidden", "nested": [{key: "hidden", "safe": 1}]}) == {"nested": [{"safe": 1}]}


@pytest.mark.parametrize("separator", ["_", "-"])
@pytest.mark.parametrize("key", [key for key in SECRETS if key != "token"])
def test_T2_s12_i16_delimited_suffix_names_are_removed(key, separator):
    name = "provider"+separator+key
    assert bounded_result({name: "hidden", name.upper(): "hidden", "ok": 1}) == {"ok": 1}


def test_T1_s12_i16_token_suffix_is_not_a_secret_match():
    value = {"context_admission_bytes_per_token": 4, "provider_token": 5, "provider-token": 6,
             "near_access_token_count": 7, "token_statistics": 8}
    assert bounded_result(value) == value


def test_T2_s12_i16_string_redaction_and_bounds_are_unchanged():
    result = bounded_result({"text": "Bearer PRIVATE_BEARER sk-1234567890abcdef public token statistics"})
    assert result == {"text": "[redacted] [redacted] public token statistics"}
    assert bounded_result({"text": "x"*8000}, long_text_keys={"text": 8192}) == {"text": "x"*8000}
    assert len(bounded_result({"note": "x"*3000})["note"]) < 2100
