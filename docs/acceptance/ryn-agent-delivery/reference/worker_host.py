"""Pipe-only host for the pinned ZCode reference. No credential loading here.

The evaluation collector supplies model/tool/approval dispatch implementations.
The default CLI is a deterministic wiring check, never a real-model score.
"""
from __future__ import annotations

import argparse
import json
import queue
import subprocess
import threading
import time
from pathlib import Path

PIN = "29628c9acdb81b703bbd4080c207a0e7ce5e276e"
HERE = Path(__file__).resolve().parent
MAX_PACKET = 2 * 1024 * 1024


class ReferenceProcessInterrupted(BaseException):
    """Controller fault injection, never a business tool result."""


class ReferenceOwnerControl(Exception):
    """Original owner intervention at a native dispatch boundary."""

    def __init__(self, action):
        if action not in {"pause", "cancel", "steer"}:
            raise ValueError("reference_invalid_owner_control")
        self.action = action


def run_worker(config, dispatch, *, node: Path, upstream: Path, owner_wait_seconds=lambda: 0.0,
               process_observer=None, model_event=None, compact_event=None, cancelled_control=None):
    """Execute against host-enforced business gates and capture protocol metadata.

    dispatch must reject unknown tools, validate parameters, bind each write to
    its exact approved preview, and enforce the same resource/egress boundaries
    as the candidate. This helper cannot turn a raw connector into a safe gate.
    """
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=upstream, text=True,
                          capture_output=True, check=True).stdout.strip()
    if head != PIN:
        raise ValueError("reference_commit_mismatch")
    tracked = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=upstream, capture_output=True)
    if tracked.returncode != 0:
        raise ValueError("reference_tracked_source_modified")
    module = (HERE / "zcode-worker.mjs").as_uri()
    storage = (upstream / "apps/zcode-cli/packages/adapters/dist/storage/index.js").as_uri()
    script = (f"import {{worker}} from {json.dumps(module)}; "
              "await worker(await import('@zcode/core'),await import('@zcode/contracts'),"
              f"await import({json.dumps(storage)})); ")
    child = subprocess.Popen([str(node), "--import", "tsx", "--input-type=module", "--eval", script],
                             cwd=upstream / "apps/zcode-cli/packages/core", stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    inbox = queue.Queue(maxsize=128)
    stderr_bytes = [0]

    def read_output():
        while line := child.stdout.readline(MAX_PACKET + 1):
            if len(line.encode()) > MAX_PACKET or not line.endswith("\n"):
                inbox.put({"kind": "host_error", "error": "reference_wire_limit"})
                return
            try:
                inbox.put(json.loads(line))
            except ValueError:
                inbox.put({"kind": "host_error", "error": "reference_wire_invalid"})
                return
        inbox.put({"kind": "host_error", "error": "reference_wire_closed"})

    def discard_stderr():
        # Upstream error text is not safe evidence: never print or persist it.
        while chunk := child.stderr.read(4096):
            stderr_bytes[0] += len(chunk.encode())

    readers = [threading.Thread(target=read_output, daemon=True), threading.Thread(target=discard_stderr, daemon=True)]
    for thread in readers:
        thread.start()
    started = time.monotonic()
    initial_owner_wait = owner_wait_seconds()
    deadline = started + config["max_seconds"] + 10
    counts = {"model": 0, "tool": 0, "approval": 0}
    if config.get("observe_compaction"):
        counts["checkpoint"] = 0
    expected_id = config.get("rpc_sequence_start", 0) + 1
    owner_control = None

    def send(value):
        line = json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
        if len(line.encode()) > MAX_PACKET:
            raise ValueError("reference_wire_limit")
        child.stdin.write(line)
        child.stdin.flush()

    try:
        if process_observer is not None:
            process_observer(child)
        send({"kind": "config", "value": config})
        while True:
            excluded = owner_wait_seconds() - initial_owner_wait
            remaining = deadline - time.monotonic() + excluded
            if remaining <= 0:
                raise TimeoutError("reference_host_time_limit")
            packet = inbox.get(timeout=remaining)
            if packet.get("kind") == "result":
                return {"result": packet["result"], "dispatch_counts": counts,
                        "worker_pid": child.pid, "last_rpc_id": expected_id - 1,
                        "elapsed_seconds": time.monotonic() - started, "owner_wait_seconds": excluded,
                        "active_wall_seconds": time.monotonic() - started - excluded,
                        "stderr_bytes": stderr_bytes[0], "upstream_commit": head}
            if packet.get("kind") != "rpc":
                raise RuntimeError(packet.get("error", "reference_wire_invalid"))
            if packet.get("id") != expected_id or packet.get("method") not in counts:
                raise ValueError("reference_rpc_invalid")
            expected_id += 1
            method = packet["method"]
            counts[method] += 1
            if (method == "model" and counts[method] > config["max_model_calls"]) or counts[method] > 256:
                raise ValueError("reference_call_limit")
            if method == "checkpoint":
                evidence = packet["params"]
                if (compact_event is None or evidence.get("session_id") != config["session_id"]
                        or evidence.get("type") != "compact_completed" or not evidence.get("boundary_id")
                        or not evidence.get("summary_message_id")):
                    raise ValueError("reference_compact_checkpoint_invalid")
                action = compact_event(evidence)
                if action not in {None, "steer", "pause", "cancel"}:
                    raise ValueError("reference_invalid_owner_control")
                owner_control = action
                send({"id": packet["id"], "result": {"action": action}})
                continue
            try:
                def emit(event, rpc_id=packet["id"]):
                    nonlocal owner_control
                    if owner_control is not None or event.get("type") != "text_delta":
                        return
                    send({"id": rpc_id, "event": event})
                    action = model_event(event) if model_event is not None else None
                    if action is not None:
                        if action not in {"pause", "cancel"}:
                            raise ValueError("reference_invalid_owner_control")
                        owner_control = action
                        send({"kind": "control", "action": action})
                streaming_options = {"on_event": emit} if method == "model" and packet["params"].get("stream") is True else {}
                if "dispatch_provenance" in config:
                    context = packet.get("dispatch_provenance")
                    expected = config["dispatch_provenance"]
                    if (not isinstance(context, dict) or context.get("rpc_id") != packet["id"]
                            or context.get("id") != f"{config['session_id']}:rpc:{packet['id']}"
                            or context.get("session_id") != config["session_id"] or context.get("method") != method
                            or any(context.get(key) != expected[key] for key in
                                   ("sampling_binding", "task_id", "registration_sha256"))):
                        raise ValueError("reference_dispatch_provenance_mismatch")
                    result = dispatch(method, packet["params"], request_context=context, **streaming_options)
                else:
                    result = dispatch(method, packet["params"], **streaming_options)
            except ReferenceOwnerControl as control:
                owner_control = control.action
                send({"kind": "control", "action": owner_control})
                continue
            except Exception as exc:
                if str(exc) == "agent_cancelled" and owner_control is None and cancelled_control is not None:
                    action = cancelled_control()
                    if action not in {None, "pause", "cancel"}:
                        raise ValueError("reference_invalid_owner_control") from None
                    if action is not None:
                        owner_control = action
                        send({"kind": "control", "action": action})
                if owner_control and str(exc) == "agent_cancelled":
                    continue  # Let the native abort persist its own safe checkpoint.
                send({"id": packet["id"], "error": "host_dispatch_failed"})
                raise  # Trusted collector records safe code; raw errors never go to the model.
            if owner_control is not None:
                # Dispatch/meter/audit already settled the real response. The
                # native stop closed this RPC, so do not inject late success.
                continue
            send({"id": packet["id"], "result": result})
    except ReferenceProcessInterrupted:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        return {"interrupted": True, "worker_pid": child.pid, "exit_code": child.returncode,
                "last_rpc_id": expected_id - 1, "dispatch_counts": counts, "upstream_commit": head,
                "elapsed_seconds": time.monotonic() - started}
    finally:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                stream.close()
            except OSError:
                if child.poll() is None:
                    raise
        for thread in readers:
            thread.join(timeout=1)


def smoke(node, upstream):
    models = []
    tools = []

    def dispatch(method, params):
        if method == "approval":
            raise AssertionError("unexpected_approval")
        if method == "tool":
            assert params["name"] == "ryn_fixture_read" and params["arguments"] == {}
            tools.append(params)
            return {"value": "read-through-python-host"}
        models.append(params)
        if len(models) == 1:
            return {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "read", "type": "function", "function": {"name": "ryn_fixture_read", "arguments": "{}"}}]}}
        assert any(m["role"] == "tool" and "read-through-python-host" in m["content"] for m in params["messages"])
        return {"message": {"role": "assistant", "content": "Fixture confirmed."}}

    config = {"session_id": "reference-pipe-smoke", "provider_id": "ryn-test", "model_id": "fixture",
              "context_window": 1000000, "max_model_calls": 16, "max_seconds": 30, "real_model": False,
              "prompt": "Read the synthetic fixture and report.", "tools": [{"type": "function", "function": {
                  "name": "ryn_fixture_read", "description": "Read synthetic fixture", "parameters": {
                      "type": "object", "properties": {}, "additionalProperties": False}}}],
              "tool_metadata": {"ryn_fixture_read": {"effect": "read"}}}
    result = run_worker(config, dispatch, node=node, upstream=upstream)
    assert len(models) == 2 and len(tools) == 1
    return {"kind": "deterministic-reference-pipe-smoke", "real_model": False, **result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    outcome = smoke(args.node.resolve(), args.upstream.resolve())
    body = json.dumps(outcome, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(body, encoding="utf-8")
    print(body)
