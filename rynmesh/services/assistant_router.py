"""Explicit Assistant target routing and privacy-boundary validation."""
from __future__ import annotations

from typing import Any, Callable, Mapping


class AssistantRouteError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_target(
    target_key: str,
    privacy_boundary: str,
    targets: list[Mapping[str, Any]],
) -> Mapping[str, Any]:
    target = next((entry for entry in targets if entry.get("key") == target_key), None)
    if target is None:
        raise AssistantRouteError(
            "assistant_target_offline", "That compute target is offline or no longer available."
        )
    if str(target.get("privacy_boundary")) != privacy_boundary:
        raise AssistantRouteError(
            "assistant_permission_required",
            "Create a new conversation before changing its privacy boundary.",
        )
    if not target.get("online", False):
        raise AssistantRouteError(
            "assistant_target_offline", "That compute target is currently offline."
        )
    return target


def local_targets(provider_factory: Callable[[], Any | None]) -> list[dict[str, Any]]:
    provider = provider_factory()
    if provider is None:
        return []
    provider_id = str(getattr(provider, "id", "local"))
    model_id = str(getattr(provider, "model", "model"))
    return [{
        "key": f"local:{provider_id}:{model_id}",
        "label": f"This device · {model_id}",
        "privacy_boundary": "device",
        "provider_id": provider_id,
        "model_id": model_id,
        "online": True,
        "route_modes": ["local_chat", "local_search", "local_context"],
    }]

