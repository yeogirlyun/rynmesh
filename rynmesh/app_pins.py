"""Ordered application pins stored on this node."""

from pathlib import Path

from .atomic_io import atomic_write_json, read_json


def _string(record: dict, field: str, limit: int = 512) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"invalid {field}")
    return value


def _validate_pin(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("each pin must be an object")
    key = _string(value, "key")
    kind = _string(value, "kind")
    app_id = _string(value, "app_id")
    pin = {"key": key, "kind": kind, "app_id": app_id, "title": _string(value, "title", 80)}

    if kind not in {"local", "shared"}:
        raise ValueError("invalid kind")
    expected_key = f"local:{app_id}"

    if kind == "local" or "open_path" in value:
        path = _string(value, "open_path")
        if not path.startswith("/") or path.startswith("//") or "\\" in path or any(ord(c) < 32 for c in path):
            raise ValueError("open_path must be a site-relative path")
        pin["open_path"] = path

    if kind == "shared" or "provider" in value:
        provider = value.get("provider")
        if not isinstance(provider, dict):
            raise ValueError("invalid provider")
        pin["provider"] = {field: _string(provider, field) for field in ("resource", "peer_id", "member_name")}
        if kind == "shared":
            expected_key = f"shared:{pin['provider']['resource']}|{pin['provider']['peer_id']}|{app_id}"

    if key != expected_key:
        raise ValueError("key does not match the pin")
    return pin


class AppPinsStore:
    def __init__(self, home: Path) -> None:
        self.path = home / "apps" / "pins.json"

    def read(self) -> dict:
        return read_json(self.path, default={"seeded": False, "pins": []})

    def replace(self, pins: object) -> dict:
        if not isinstance(pins, list) or len(pins) > 30:
            raise ValueError("pins must be an array of at most 30 entries")
        validated = [_validate_pin(pin) for pin in pins]
        if len({pin["key"] for pin in validated}) != len(validated):
            raise ValueError("duplicate pin key")
        result = {"seeded": True, "pins": validated}
        atomic_write_json(self.path, result)
        return result
