"""Regression tests for S01-I13-fix: unprobed external local-API manifest defaults vs managed/connected defaults."""
from __future__ import annotations

from rynmesh.llm_package.manifest import LLMPackageManifest


def test_s01_i13_fix_manifest_defaults_preserve_unprobed_external_api_budget_and_managed_defaults() -> None:
    ext_openai = LLMPackageManifest(
        package_id="ext-openai",
        mode="openai_compatible",
        public_model_alias="External OpenAI",
        base_url="http://127.0.0.1:18080",
    )
    assert ext_openai.context_window == 4096
    assert ext_openai.max_output_tokens == 512
    ext_openai.validate()

    ext_ollama = LLMPackageManifest(
        package_id="ext-ollama",
        mode="ollama",
        public_model_alias="External Ollama",
        base_url="http://127.0.0.1:11434",
    )
    assert ext_ollama.context_window == 4096
    assert ext_ollama.max_output_tokens == 512
    ext_ollama.validate()

    managed = LLMPackageManifest(
        package_id="managed-pkg",
        mode="managed",
        public_model_alias="Managed Local",
    )
    assert managed.context_window == 8192
    assert managed.max_output_tokens == 2048
    managed.validate()

    legacy_loaded = LLMPackageManifest.from_dict(
        {
            "package_id": "legacy-ext",
            "mode": "openai_compatible",
            "public_model_alias": "Legacy External",
            "base_url": "http://127.0.0.1:18080",
        }
    )
    assert legacy_loaded.context_window == 4096
    assert legacy_loaded.max_output_tokens == 512
