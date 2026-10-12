"""Fresh signed physical-node observations for acceptance collectors only.

The peer key authenticates the responding test node, not the physical machine.
Distinct-machine evidence also requires the independently recorded SSH/install
observations. No model request, task action or secret transfer occurs here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request

from rynmesh.crypto import SignedPayload, sign_payload, verify_signed_payload
from rynmesh.llm_package import model_settings
from rynmesh.llm_package.peer_binding import model_binding

KIND = "ryn.acceptance.physical-node-observation.v1"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_challenge(value):
    if not isinstance(value, dict) or set(value) != {"challenge", "task_id", "request", "run_nonce", "manifest_sha256"}:
        raise ValueError("remote_observation_challenge_invalid")
    for key, length in (("challenge", 32), ("run_nonce", 32), ("manifest_sha256", 64)):
        if not isinstance(value[key], str) or not re.fullmatch("[0-9a-f]{" + str(length) + "}", value[key]):
            raise ValueError("remote_observation_challenge_invalid")
    if not isinstance(value["task_id"], str) or not 0 < len(value["task_id"]) <= 256:
        raise ValueError("remote_observation_challenge_invalid")
    request = value["request"]
    if request is not None and (not isinstance(request, dict) or set(request) != {"kind", "id"}
            or request["kind"] not in {"model", "tool"} or not isinstance(request["id"], str)
            or not 0 < len(request["id"]) <= 512):
        raise ValueError("remote_observation_challenge_invalid")


def capture_observation(app, store, challenge, code_manifest):
    """Read real objects on their owning node; never accept claimed source rows."""
    validate_challenge(challenge)
    home = store.home.resolve()
    identity = home / "identity.ed25519"
    if base64.b64decode(identity.read_text(encoding="utf-8").strip(), validate=True) != store.private_key_bytes:
        raise ValueError("remote_observation_live_identity_mismatch")
    sources = app.state.llm_agent_services["source_store"]
    runtime = app.state.agent_runtime
    if sources.path.resolve() != home / "llm/sources.enc" or runtime.store.root.resolve() != home / "agent":
        raise ValueError("remote_observation_store_wrong_node")
    # Only whitelist public policy/identity fields. Never call SourceStore.public
    # on a secret-bearing record or serialize arbitrary credentials/configuration.
    source_rows = []
    for row in sources.read().values():
        models = []
        for model in row["models"]:
            generation = model_settings.effective(row, model)
            models.append({"model_id": model, "model_binding": model_binding(row, model),
                           "ready": row.get("enabled") is True and row.get("status") == "ready",
                           "generation": {k: copy.deepcopy(generation[k]) for k in
                                          ("context_window", "max_output_tokens", "request_defaults")}})
        source_rows.append({"id": row["id"], "name": row["name"], "models": models})
    nas = getattr(app.state, "nas_plugin", None)
    connections = {"directory": str(home / "plugins/nas"), "instances": []}
    if nas is not None:
        if nas.directory.resolve() != home / "plugins/nas":
            raise ValueError("remote_observation_connector_wrong_node")
        with nas.lock:
            connections["instances"] = [{"id": r["id"], "name": r["name"]} for r in nas.state["sources"]]
    tasks = runtime.store.read()["tasks"]
    code = code_manifest()
    files = {"rynmesh/" + path: digest for path, digest in code["python_files"].items()}
    code = {**code, "python_files": files, "sha256": hashlib.sha256(canonical(files)).hexdigest(),
            "capture_implementation_sha256": sha(__file__)}
    state = {"node": {"home": str(home), "identity_file_sha256": sha(identity),
                      "hostname": socket.gethostname(), "pid": os.getpid()},
             "source_store": {"path": str(sources.path.resolve()),
                              "sha256": sha(sources.path) if sources.path.exists() else None,
                              "records": source_rows},
             "connections": connections,
             "history": {"root": str(runtime.store.root.resolve()), "task_ids_readback":
                         [{"id": row["id"], "sha256": hashlib.sha256(canonical(row)).hexdigest()}
                          for row in tasks.values()]},
             "code": code}
    payload = {"kind": KIND, "challenge": copy.deepcopy(challenge), "peer_id": store.peer_id,
               "captured_at": datetime.now(timezone.utc).isoformat(), "state": state}
    return sign_payload(payload, private_key_bytes=store.private_key_bytes).to_dict()


def verify_observation(envelope, expected_challenge, expected_peer_id, *, now=None, max_age_seconds=15):
    """Bind a response to a pre-trusted peer and this exact pending dispatch."""
    validate_challenge(expected_challenge)
    if (not isinstance(envelope, dict) or set(envelope) != {"alg", "public_key", "signature", "payload"}
            or envelope["public_key"] != expected_peer_id or len(canonical(envelope)) > 2 * 1024 * 1024
            or type(max_age_seconds) not in {int, float} or not 0 < max_age_seconds <= 30):
        raise ValueError("remote_observation_envelope_invalid")
    signed = SignedPayload.from_dict(envelope)
    verify_signed_payload(signed)
    payload = signed.payload
    if (set(payload) != {"kind", "challenge", "peer_id", "captured_at", "state"}
            or payload["kind"] != KIND or payload["peer_id"] != expected_peer_id
            or payload["challenge"] != expected_challenge):
        raise ValueError("remote_observation_binding_mismatch")
    captured = datetime.fromisoformat(payload["captured_at"])
    current = now or datetime.now(timezone.utc)
    if captured.utcoffset() is None or current.utcoffset() is None:
        raise ValueError("remote_observation_timestamp_invalid")
    age = (current - captured).total_seconds()
    if not -2 <= age <= max_age_seconds:
        raise ValueError("remote_observation_not_fresh")
    state = payload["state"]
    if not isinstance(state, dict) or set(state) != {"node", "source_store", "connections", "history", "code"}:
        raise ValueError("remote_observation_state_invalid")
    return copy.deepcopy(payload)


def install_observation_route(app, store, code_manifest):
    @app.post("/api/local/two-host/observation")
    async def observation(request: Request):
        app.state.friends.local_control(request)
        raw = await request.body()
        if len(raw) > 4096:
            raise HTTPException(status_code=413, detail="observation_challenge_too_large")
        try:
            challenge = json.loads(raw)
            validate_challenge(challenge)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="observation_challenge_invalid") from None
        return capture_observation(app, store, challenge, code_manifest)
