"""Pluggable model provider — the node's local AI seam.

Powers Search & Ask, digest briefings, and item summaries. Two backends:

- **Ollama** (default when running): free, local, private. Zero config when
  the Ollama daemon is up; model picked from `RYNMESH_OLLAMA_MODEL` or the
  first installed model.
- **Anthropic** (bring-your-own key): set `ANTHROPIC_API_KEY`. Uses the
  official ``anthropic`` SDK as an *optional* dependency (same pattern as
  ``curl_cffi`` for the reality transport) — selecting this backend without
  the SDK installed raises with the pip command to run.

Resolution: `RYNMESH_MODEL_PROVIDER=anthropic|ollama|none` selects a backend;
cloud selection is honored only when the caller explicitly permits cloud
access. Otherwise only a reachable Ollama is considered.
Everything degrades gracefully to "no provider" — features that need a model
say so instead of failing.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Callable, Protocol

__all__ = [
    "RECOMMENDED_LOCAL_MODELS",
    "AnthropicProvider",
    "ModelProvider",
    "ModelProviderError",
    "OllamaProvider",
    "list_local_models",
    "resolve_provider",
]

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# A short, honest catalog for what this node actually asks a model to do:
# one-line item summaries and a few-bullet briefing. Small models are genuinely
# the right default here — the work is summarization, not reasoning.
RECOMMENDED_LOCAL_MODELS = (
    {
        "name": "gemma3:4b",
        "size_hint": "3.3 GB",
        "tier": "fast",
        "note": "Best default. Summarizes a full digest in seconds on any Mac.",
    },
    {
        "name": "llama3.2:3b",
        "size_hint": "2.0 GB",
        "tier": "fast",
        "note": "Smallest useful option — good on older or low-memory machines.",
    },
    {
        "name": "gemma3:12b",
        "size_hint": "8.1 GB",
        "tier": "balanced",
        "note": "Noticeably better briefings, still comfortably fast.",
    },
    {
        "name": "qwen3.6:27b",
        "size_hint": "17 GB",
        "tier": "quality",
        "note": "Strongest summaries. Slow for large digests; needs plenty of RAM.",
    },
)

HttpJson = Callable[[str, dict[str, Any] | None, float], dict[str, Any]]


class ModelProviderError(RuntimeError):
    pass


class ModelProvider(Protocol):
    id: str
    model: str

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str: ...


def _http_json(url: str, payload: dict[str, Any] | None, timeout_s: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return json.loads(response.read().decode("utf-8"))


class OllamaProvider:
    id = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "",
        timeout_s: float = 120.0,
        http: HttpJson | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("RYNMESH_OLLAMA_URL", DEFAULT_OLLAMA_URL)).rstrip("/")
        self.timeout_s = timeout_s
        self._http = http or _http_json
        self.model = model or os.environ.get("RYNMESH_OLLAMA_MODEL", "")

    def installed(self) -> list[dict[str, Any]]:
        """Models present locally, newest first as Ollama reports them."""
        try:
            tags = self._http(f"{self.base_url}/api/tags", None, 3.0)
        except Exception:
            return []
        out = []
        for entry in tags.get("models", []):
            name = str(entry.get("name", ""))
            if not name:
                continue
            out.append({
                "name": name,
                "size_bytes": int(entry.get("size", 0) or 0),
                "modified": str(entry.get("modified_at", "")),
            })
        return out

    def available(self) -> bool:
        models = [entry["name"] for entry in self.installed()]
        if not models:
            return False
        if self.model and self.model in models:
            return True
        if self.model:
            # Chosen model was removed from Ollama — fall back rather than
            # failing every request with a 404 from the generate endpoint.
            self.model = ""
        self.model = models[0]
        return True

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        if not self.model and not self.available():
            raise ModelProviderError("ollama_unavailable")
        try:
            result = self._http(
                f"{self.base_url}/api/generate",
                {
                    "model": self.model,
                    "prompt": prompt,
                    "system": system,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
                self.timeout_s,
            )
        except Exception as exc:
            raise ModelProviderError(f"ollama_generate_failed: {exc}") from exc
        return str(result.get("response", "")).strip()


class AnthropicProvider:
    id = "anthropic"

    def __init__(self, *, model: str = "", client: Any | None = None) -> None:
        self.model = model or os.environ.get("RYNMESH_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        if client is not None:
            self._client = client
            return
        try:
            import anthropic
        except ImportError as exc:
            raise ModelProviderError(
                "anthropic_sdk_missing: the Anthropic backend needs the official "
                "SDK — run `pip install anthropic`."
            ) from exc
        self._client = anthropic.Anthropic()

    def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system or "You are a concise assistant inside a rynmesh node.",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            raise ModelProviderError(f"anthropic_request_failed: {exc}") from exc
        if getattr(response, "stop_reason", "") == "refusal":
            raise ModelProviderError("anthropic_refused")
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ).strip()


def list_local_models(
    *, current: str = "", http: HttpJson | None = None
) -> dict[str, Any]:
    """Catalog for the settings UI: what's installed, what's recommended.

    `installed` is the authority — recommended entries are annotated with
    whether they're present so the UI can offer an `ollama pull` for the rest.
    """
    provider = OllamaProvider(http=http)
    installed = provider.installed()
    names = {entry["name"] for entry in installed}
    recommended = [
        {**entry, "installed": entry["name"] in names} for entry in RECOMMENDED_LOCAL_MODELS
    ]
    return {
        "ollama_running": bool(installed) or _ollama_reachable(provider),
        "installed": installed,
        "recommended": recommended,
        "current": current,
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
    }


def _ollama_reachable(provider: "OllamaProvider") -> bool:
    try:
        provider._http(f"{provider.base_url}/api/tags", None, 2.0)
        return True
    except Exception:
        return False


def resolve_provider(
    *,
    preferred_model: str = "",
    allow_cloud: bool = True,
    http: HttpJson | None = None,
) -> ModelProvider | None:
    """Pick the active provider. Returns None when no model is reachable.

    `preferred_model` is the owner's explicit choice from settings and wins
    over the RYNMESH_OLLAMA_MODEL env var and over Ollama's list order.
    """
    forced = os.environ.get("RYNMESH_MODEL_PROVIDER", "").strip().lower()
    if forced == "none":
        return None
    if forced == "anthropic" and not allow_cloud:
        return None
    if forced == "anthropic":
        return AnthropicProvider()
    if forced == "ollama":
        provider = OllamaProvider(model=preferred_model, http=http)
        if not provider.available():
            raise ModelProviderError("ollama_unavailable")
        return provider

    if allow_cloud and os.environ.get("ANTHROPIC_API_KEY", "").strip():
        try:
            return AnthropicProvider()
        except ModelProviderError:
            pass  # SDK missing — fall through to Ollama
    ollama = OllamaProvider(model=preferred_model, http=http)
    if ollama.available():
        return ollama
    return None
