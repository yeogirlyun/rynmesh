"""Bind a send review to the public model and price, excluding live capacity."""
import hashlib
import json


def service_review(manifest: dict, *, own_request: bool = False) -> dict:
    value = {key: manifest.get(key) for key in (
        "package_id", "model_alias", "model_fingerprint", "version",
        "context_window", "max_output_tokens", "pricing",
    )}
    if own_request and isinstance(value["pricing"], dict):
        value["pricing"] = {**value["pricing"], "input_per_1k": 0, "output_per_1k": 0, "minimum": 0}
    # Numeric JSON formatting (0 versus 0.0) is not a price change.
    if isinstance(value["pricing"], dict):
        value["pricing"] = {key: float(number) if isinstance(number, (int, float)) else number
                            for key, number in value["pricing"].items()}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"token": digest, "model_alias": value["model_alias"], "pricing": value["pricing"]}
