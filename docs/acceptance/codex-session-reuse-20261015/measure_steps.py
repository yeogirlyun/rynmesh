"""Per-step latency of a Codex CLI client tool loop: stateless vs kept thread.

Runs the same scripted origin-client loop through the production
CLIAgentAdapter twice per round: once with RYN_CODEX_SESSION_REUSE=0 (the
previous stateless full-history mode) and once with reuse enabled. Each loop
is one agent window: a tool-using first owner turn, then a follow-up owner
turn in the same window. Tool calls are answered from an in-memory fixture;
nothing on disk is read or changed. The resident memory of each kept
app-server process is recorded to size RYN_CODEX_SESSION_LIMIT.

This performs real model inference with the locally logged-in Codex account,
so it only runs with --live. Output records timings, token counts, tool names
and lengths, never model text or fixture content.

    python docs/acceptance/codex-session-reuse-20261015/measure_steps.py --live --rounds 3
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from rynmesh.llm_package import codex_threads  # noqa: E402
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter  # noqa: E402

FILES = {f"notes/{name}.txt": f"{name}: " + " ".join(f"line {i} of {name}, balance {i * 7}." for i in range(40))
         for name in ("alpha", "beta", "gamma", "delta", "epsilon")}
TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read one workspace text file at the origin client.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string", "maxLength": 200}},
                    "required": ["path"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List one workspace directory at the origin client.",
     "parameters": {"type": "object", "properties": {"path": {"type": "string", "maxLength": 200}},
                    "required": ["path"], "additionalProperties": False}}},
]
# Comparable in shape to the agent system message: prose plus labelled state.
SYSTEM = ("You are Ryn, an agent working in the owner's workspace through the supplied functions only. "
          "Read before answering; call exactly one function per response until you have every file you need. "
          + "Keep answers short and grounded in tool results. " * 20
          + '\nServer control state: {"plan_revision":1,"receipts":[]}\nOwner corrections steer the task.')
TASK = ("List the notes directory, then read every file in it one call at a time, "
        "then reply with one sentence naming the file with the largest final balance.")
FOLLOW_UP = "Which file has the smallest final balance? Answer from what you already read, in one sentence."


def rss_kb(pid):
    """Resident memory of one kept app-server process, or None."""
    try:
        if os.name == "nt":
            row = subprocess.run(["tasklist", "/fi", f"PID eq {int(pid)}", "/fo", "csv", "/nh"],
                                 capture_output=True, text=True, timeout=5).stdout.strip().split('","')
            return int(row[-1].strip('"').split()[0].replace(",", "").replace(".", ""))
        return int(subprocess.run(["ps", "-o", "rss=", "-p", str(int(pid))],
                                  capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def origin_result(call):
    arguments = json.loads(call["function"]["arguments"])
    if call["function"]["name"] == "list_dir":
        return json.dumps(sorted(path for path in FILES if path.startswith(arguments["path"].rstrip("/") + "/")))
    return FILES.get(arguments["path"], json.dumps({"error": "not_found", "no_mutation": True}))


def run_loop(mode, *, model, effort, max_steps, stream):
    os.environ["RYN_CODEX_SESSION_REUSE"] = "1" if mode == "reuse" else "0"
    adapter = CLIAgentAdapter("codex_cli")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    # What the agent bridge sends for one window: the local requester and a hashed task id.
    scope = codex_threads.scope("local", codex_threads.window_key(f"measure-{mode}-{time.time_ns()}"))
    steps, turn = [], 1
    try:
        for index in range(max_steps):
            stats = dict(codex_threads.pool_for(adapter).stats)
            first_text = []
            started = time.monotonic()

            def on_event(chunk):
                if not first_text and any((choice.get("delta") or {}).get("content") for choice in chunk.get("choices", [])):
                    first_text.append(time.monotonic() - started)

            body = {"messages": messages, "tools": TOOLS, "max_tokens": 1024,
                    **({"reasoning_effort": effort} if effort else {}), **({"stream": True} if stream else {})}
            row = {"step": index + 1, "turn": turn, "mode": mode}
            try:
                result = adapter.chat(json.loads(json.dumps(body)), task_id=f"measure-{mode}-{index}",
                                      timeout_s=300, model=model, on_event=on_event if stream else None,
                                      conversation_scope=scope)
            except Exception as exc:  # recorded, not hidden: the loop stops here
                row.update(wall_ms=int((time.monotonic() - started) * 1000), error=type(exc).__name__,
                           code=getattr(exc, "code", None))
                steps.append(row)
                break
            after = codex_threads.pool_for(adapter).stats
            row.update(wall_ms=int((time.monotonic() - started) * 1000), duration_ms=result["duration_ms"],
                       input_tokens=result["input_tokens"], output_tokens=result["output_tokens"],
                       finish_reason=result["finish_reason"],
                       thread={key: after[key] - stats.get(key, 0) for key in after if after[key] != stats.get(key, 0)},
                       tools=[call["function"]["name"] for call in result["message"].get("tool_calls", [])],
                       content_chars=len(result["message"].get("content") or ""),
                       request_chars=len(json.dumps(messages, ensure_ascii=False)),
                       **({"first_text_ms": int(first_text[0] * 1000)} if first_text else {}))
            kept = [rss_kb(session.rpc.process.pid) for session in list(codex_threads.pool_for(adapter).idle)]
            if kept:
                row["kept_rss_kb"] = kept
            steps.append(row)
            messages.append(result["message"])
            for call in result["message"].get("tool_calls", []):
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": origin_result(call)})
            if not result["message"].get("tool_calls"):
                if turn == 2:
                    break
                turn = 2
                messages.append({"role": "user", "content": FOLLOW_UP})
    finally:
        adapter.shutdown()
    return steps


def summary(rows):
    def stats(values):
        values = sorted(values)
        if not values:
            return None
        return {"n": len(values), "median_ms": int(statistics.median(values)), "max_ms": values[-1],
                "p90_ms": values[min(len(values) - 1, int(len(values) * 0.9))]}
    result = {}
    for mode in ("stateless", "reuse"):
        ok = [row for row in rows if row["mode"] == mode and "error" not in row]
        first_follow_up = {}
        for row in ok:
            if row["turn"] == 2:
                first_follow_up.setdefault(row["round"], row["wall_ms"])
        result[mode] = {"all_steps": stats([row["wall_ms"] for row in ok]),
                        "first_step": stats([row["wall_ms"] for row in ok if row["step"] == 1]),
                        "later_steps": stats([row["wall_ms"] for row in ok if row["step"] > 1]),
                        "follow_up_first_step": stats(list(first_follow_up.values())),
                        "kept_rss_kb_max": max((value for row in ok for value in row.get("kept_rss_kb", []) if value), default=None),
                        "errors": [row for row in rows if row["mode"] == mode and "error" in row]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="perform real inference with the logged-in Codex CLI")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--model", default="")
    parser.add_argument("--effort", default="")
    parser.add_argument("--stream", action="store_true", help="also record time to first streamed text")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "results"))
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required: this spends real Codex inference")
    CLIAgentAdapter("codex_cli")._command_path()  # fail early when Codex is absent
    rows = []
    for round_index in range(args.rounds):
        # Alternate order so warm network/model caches do not favour one mode.
        for mode in (("stateless", "reuse") if round_index % 2 == 0 else ("reuse", "stateless")):
            for row in run_loop(mode, model=args.model, effort=args.effort or None,
                                max_steps=args.max_steps, stream=args.stream):
                rows.append({"round": round_index + 1, **row})
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"measured_at": stamp, "platform": sys.platform, "model": args.model or "codex default",
              "effort": args.effort or "default", "rounds": args.rounds, "summary": summary(rows), "steps": rows}
    (out / f"steps-{stamp}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
