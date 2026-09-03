"""Tests for the Docker runtime backend and the lifecycle runtime seam."""

from __future__ import annotations

import pytest

import rynmesh.llm_package.lifecycle as llm_lifecycle
import rynmesh.llm_package.runtime_docker as llm_runtime_docker
from rynmesh.llm_package.lifecycle import LifecycleError
from rynmesh.llm_package.manifest import LLMPackageManifest


def test_available_reports_reason_without_docker_on_path(monkeypatch):
    monkeypatch.setattr(llm_runtime_docker.shutil, "which", lambda _name: None)
    ok, reason = llm_runtime_docker.available()
    assert ok is False
    assert reason
    assert "/" not in reason
    assert "\\" not in reason


def test_backend_raises_for_unknown_runtime():
    manifest = LLMPackageManifest(
        package_id="mystery", mode="managed", public_model_alias="mystery",
        runtime="some_future_runtime",
    )
    with pytest.raises(LifecycleError, match="unsupported runtime"):
        llm_lifecycle._backend(manifest)


def test_backend_resolves_docker_runtime():
    manifest = LLMPackageManifest(
        package_id="docker-one", mode="managed", public_model_alias="docker-one",
        runtime="docker_llama_cpp",
    )
    assert llm_lifecycle._backend(manifest) is llm_runtime_docker


def test_stop_on_external_manifest_never_touches_docker(monkeypatch, tmp_path):
    manifest = LLMPackageManifest(
        package_id="ext", mode="openai_compatible", public_model_alias="ext",
        runtime="external", base_url="http://127.0.0.1:18080",
    )
    manifest_path = tmp_path / "manifest.json"
    from rynmesh.llm_package.manifest import save_manifest

    save_manifest(manifest, manifest_path)

    def _boom():
        raise AssertionError("Docker must not be touched for an external manifest")

    monkeypatch.setattr(llm_runtime_docker, "_docker", _boom)
    result = llm_lifecycle.stop(manifest_path)
    assert result == {
        "managed": False, "stopped": False, "message": "external service is owner-managed",
    }
