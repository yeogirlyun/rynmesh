"""Codex-aligned CodeMode (`ryn_code_exec`) for multi-tool JS composition and persistent task state.

Grounded in reverse-engineered OpenAI Codex (`codex.exe` CodeMode & `codex-code-mode-host.exe`):
- Runs JavaScript in an isolated Node `vm` context with no filesystem, network, process, or module globals.
- Exposes all enabled read-only tools on `globalThis.tools.<name>(args)` and metadata in `ALL_TOOLS`.
- Supports `await Promise.all([...])`, `await Promise.allSettled([...])`, `as_settled(promises)`,
  and `stream_settled(promises)` with concurrent host-side read tool execution.
- Exposes `text(...)`, `image(...)` / `emitImage(...)`, `store(key, value)`, `load(key)`, and `exit()`.
- Supports leading `// @exec: {"yield_time_ms": ..., "max_output_tokens": ...}` pragma comments.
- Strictly enforces read-only tool execution, JSON schema validation, task scope, and desktop_access gates.
"""
from __future__ import annotations

import copy
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from .js_runtime.manager import sanitized_worker_env
from .protocol import sanitized
from .store import AgentError, AgentToolRejected, encoded


def _resolve_node_binary() -> str:
    env_override = os.environ.get("RYN_NODE_EXECUTABLE") or os.environ.get("RYN_JS_NODE_EXECUTABLE")
    if isinstance(env_override, str) and env_override.strip() and Path(env_override.strip()).is_file():
        return str(Path(env_override.strip()).resolve())
    found = shutil.which("node")
    if found and Path(found).is_file():
        return str(Path(found).resolve())
    candidates = (
        [Path(r"C:\Program Files\nodejs\node.exe"), Path(r"C:\Program Files (x86)\nodejs\node.exe")]
        if os.name == "nt"
        else [Path("/usr/local/bin/node"), Path("/opt/homebrew/bin/node"), Path("/usr/bin/node")]
    )
    for cand in candidates:
        if cand.is_file():
            return str(cand.resolve())
    raise AgentToolRejected(
        "agent_code_exec_node_unavailable",
        correction="Node.js runtime was not found on this host; use direct tool calls instead.",
    )

NAME = "ryn_code_exec"
MAX_CODE_CHARS = 65536
MAX_CHILD_CALLS = 16
DEFAULT_YIELD_TIME_MS = 10000
MAX_YIELD_TIME_MS = 60000
DEFAULT_MAX_OUTPUT_TOKENS = 4000
MAX_STORED_STATE_BYTES = 256 * 1024

_IDENT_INVALID_RE = re.compile(r"[^A-Za-z0-9_$]")

SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "Run JavaScript in a sandboxed CodeMode runtime to batch and compose read-only tools "
            "(`await Promise.all([...])`, `as_settled([...])`, `stream_settled([...])`), filter/transform "
            "results in code, and persist intermediate values across turns with `store(key, value)` and `load(key)`. "
            "All read-only tools are available on `tools.<name>(args)` and listed in `ALL_TOOLS`. "
            "Use `text(...)` to emit output, `image(...)` to attach an image, or return a JSON-serializable value."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "minLength": 1, "maxLength": MAX_CODE_CHARS},
                "yield_time_ms": {"type": "integer", "minimum": 50, "maximum": MAX_YIELD_TIME_MS},
                "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 16000},
                "reset": {"type": "boolean"},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

# Sandboxed Node runner script modelled on `codex-code-mode-host.exe` (0x29346f0) and `node_repl` kernel.js.
_NODE_HOST_SCRIPT = r"""
'use strict';
const vm = require('node:vm');
const readline = require('node:readline');

const EarlyExitSignal = Symbol('EarlyExitSignal');

function formatValue(val) {
  if (typeof val === 'string') return val;
  if (val === undefined) return 'undefined';
  try {
    const s = JSON.stringify(val);
    return typeof s === 'string' ? s : String(val);
  } catch (_) {
    return String(val);
  }
}

function normalizeImageItem(val) {
  if (typeof val === 'string' && val.trim()) {
    const s = val.trim();
    if (/^(data:image\/|https?:\/\/|file:\/\/)/i.test(s)) {
      return { type: 'input_image', image_url: s };
    }
    return { type: 'image', path: s };
  }
  if (val && typeof val === 'object') {
    const url = val.image_url || val.url || val.dataUrl || val.data_url;
    if (typeof url === 'string' && url.trim()) {
      return { type: 'input_image', image_url: url.trim() };
    }
    if (typeof val.bytesBase64 === 'string' && val.bytesBase64.trim()) {
      const mime = typeof val.mimeType === 'string' && val.mimeType.trim() ? val.mimeType.trim() : 'image/png';
      return { type: 'input_image', image_url: `data:${mime};base64,${val.bytesBase64.trim()}` };
    }
    if (typeof val.path === 'string' && val.path.trim()) {
      return { type: 'image', path: val.path.trim() };
    }
  }
  throw new TypeError('image() expects a non-empty URL/path string or image object');
}

function sendFrame(frame) {
  process.stdout.write(JSON.stringify(frame) + '\n');
}

const pendingCalls = new Map();
let nextCallId = 1;
let initialized = false;

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', (line) => {
  if (!line || !line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (_) {
    return;
  }
  if (!initialized && msg.type === 'init') {
    initialized = true;
    runCodeMode(msg).catch((err) => {
      sendFrame({
        type: 'result',
        ok: false,
        error: err && err.stack ? String(err.stack) : String(err),
        content_items: [],
        stored_values: {},
      });
      process.exit(0);
    });
    return;
  }
  if (msg.type === 'tool_result') {
    const entry = pendingCalls.get(msg.id);
    if (entry) {
      pendingCalls.delete(msg.id);
      if (msg.ok) {
        entry.resolve(msg.result);
      } else {
        const err = new Error(msg.error || 'Tool call failed');
        if (msg.details) err.details = msg.details;
        entry.reject(err);
      }
    }
  }
});

async function runCodeMode(init) {
  const contentItems = [];
  const storedValues = Object.assign(Object.create(null), init.stored_values || {});
  const enabledTools = Array.isArray(init.enabled_tools) ? init.enabled_tools : [];

  function invokeHostTool(toolName, args) {
    const id = 'call_' + (nextCallId++);
    return new Promise((resolve, reject) => {
      pendingCalls.set(id, { resolve, reject });
      sendFrame({
        type: 'tool_call',
        id,
        name: toolName,
        arguments: args === undefined ? {} : args,
      });
    });
  }

  const toolsObj = Object.create(null);
  const allToolsList = [];
  for (const t of enabledTools) {
    const fn = (args) => invokeHostTool(t.name, args);
    toolsObj[t.name] = fn;
    if (t.sanitized_name && !(t.sanitized_name in toolsObj)) {
      toolsObj[t.sanitized_name] = fn;
    }
    allToolsList.push(Object.freeze({
      name: t.sanitized_name || t.name,
      raw_name: t.name,
      description: t.description || '',
    }));
  }

  function addText(...values) {
    if (values.length === 0) return;
    const text = values.map(formatValue).join(' ');
    contentItems.push({ type: 'input_text', text });
  }

  function addImage(value) {
    contentItems.push(normalizeImageItem(value));
  }

  function asSettled(promises) {
    const list = Array.from(promises || []);
    return list.map((p, index) =>
      Promise.resolve(p).then(
        (value) => ({ index, status: 'fulfilled', value }),
        (reason) => ({
          index,
          status: 'rejected',
          reason: reason && reason.message ? String(reason.message) : String(reason),
        })
      )
    );
  }

  async function* streamSettled(promises) {
    const list = Array.from(promises || []);
    const remaining = new Map();
    list.forEach((p, index) => {
      const wrapped = Promise.resolve(p).then(
        (value) => ({ index, outcome: { index, status: 'fulfilled', value } }),
        (reason) => ({
          index,
          outcome: {
            index,
            status: 'rejected',
            reason: reason && reason.message ? String(reason.message) : String(reason),
          },
        })
      );
      remaining.set(index, wrapped);
    });
    while (remaining.size > 0) {
      const winner = await Promise.race(remaining.values());
      remaining.delete(winner.index);
      yield winner.outcome;
    }
  }

  const sandbox = Object.create(null);
  sandbox.ALL_TOOLS = Object.freeze(allToolsList);
  sandbox.tools = Object.freeze(toolsObj);
  sandbox.text = addText;
  sandbox.image = addImage;
  sandbox.emitImage = addImage;
  sandbox.store = (key, value) => {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('store(key, value) requires a non-empty string key');
    }
    storedValues[key] = JSON.parse(JSON.stringify(value === undefined ? null : value));
    return storedValues[key];
  };
  sandbox.load = (key) => {
    if (typeof key !== 'string' || !(key in storedValues)) return undefined;
    return JSON.parse(JSON.stringify(storedValues[key]));
  };
  sandbox.as_settled = asSettled;
  sandbox.stream_settled = streamSettled;
  sandbox.exit = () => {
    throw EarlyExitSignal;
  };
  sandbox.console = Object.freeze({
    log: (...args) => addText(...args),
    info: (...args) => addText(...args),
    warn: (...args) => addText(...args),
    error: (...args) => addText(...args),
  });

  const context = vm.createContext(sandbox, {
    codeGeneration: { strings: false, wasm: false },
  });

  const wrappedCode = `(async () => {\n${init.code}\n})()`;
  let returnValue;
  let ok = true;
  let errorText = null;
  try {
    const script = new vm.Script(wrappedCode, { filename: 'code_mode_cell.js' });
    returnValue = await script.runInContext(context);
  } catch (err) {
    if (err !== EarlyExitSignal) {
      ok = false;
      errorText = err && err.stack ? String(err.stack) : String(err);
    }
  }

  if (ok && returnValue !== undefined && contentItems.length === 0) {
    addText(returnValue);
  }

  sendFrame({
    type: 'result',
    ok,
    error: errorText,
    return_value: returnValue !== undefined ? JSON.parse(JSON.stringify(returnValue, (_k, v) => (typeof v === 'bigint' ? v.toString() : v))) : null,
    content_items: contentItems,
    stored_values: storedValues,
  });
  process.exit(0);
}
"""


def sanitize_js_tool_name(name: str) -> str:
    cleaned = _IDENT_INVALID_RE.sub("_", str(name or "").strip())
    if not cleaned:
        return "_tool"
    if cleaned[0].isdigit():
        return "_" + cleaned
    return cleaned


def parse_exec_pragma(code: str) -> tuple[str, dict[str, int]]:
    """Parse Codex's optional first-line `// @exec: {...}` pragma comment."""
    if not isinstance(code, str):
        raise AgentToolRejected("agent_tool_arguments_invalid")
    lines = code.splitlines(keepends=True)
    if not lines:
        return code, {}
    first = lines[0].strip()
    if not first.startswith("//"):
        return code, {}
    comment = first[2:].strip()
    if not comment.startswith("@exec:"):
        return code, {}
    raw_json = comment[len("@exec:") :].strip()
    if not raw_json:
        raise AgentToolRejected(
            "agent_tool_arguments_invalid",
            correction="// @exec: pragma must be followed by a JSON object with optional yield_time_ms and max_output_tokens.",
        )
    try:
        parsed = json.loads(raw_json)
    except ValueError:
        raise AgentToolRejected(
            "agent_tool_arguments_invalid",
            correction="Invalid JSON in // @exec: pragma comment.",
        ) from None
    if not isinstance(parsed, dict) or (set(parsed) - {"yield_time_ms", "max_output_tokens"}):
        raise AgentToolRejected(
            "agent_tool_arguments_invalid",
            correction="// @exec: pragma only supports yield_time_ms and max_output_tokens.",
        )
    pragma: dict[str, int] = {}
    for key in ("yield_time_ms", "max_output_tokens"):
        if key in parsed and parsed[key] is not None:
            val = parsed[key]
            if type(val) is not int or val <= 0:
                raise AgentToolRejected("agent_tool_arguments_invalid")
            pragma[key] = val
    rest = "".join(lines[1:])
    return (rest if rest.strip() else "return null;"), pragma


class CodeModeRunner:
    """Executes `ryn_code_exec` cells and maintains bounded per-task `store`/`load` state."""

    def __init__(self, parent_tools: Any) -> None:
        self.parent_tools = parent_tools
        self._lock = threading.Lock()
        # Keyed by task_id -> {"context_epoch": int, "scope_revision": int, "values": dict}
        self._task_stores: dict[str, dict[str, Any]] = {}

    def forget_task(self, task_id: str) -> None:
        with self._lock:
            self._task_stores.pop(str(task_id or ""), None)

    def _get_stored_values(self, task_id: str, epoch: int, scope_revision: int, *, reset: bool = False) -> dict[str, Any]:
        if not task_id:
            return {}
        with self._lock:
            entry = self._task_stores.get(task_id)
            if (
                reset
                or entry is None
                or entry.get("context_epoch") != epoch
                or entry.get("scope_revision") != scope_revision
            ):
                self._task_stores[task_id] = {
                    "context_epoch": epoch,
                    "scope_revision": scope_revision,
                    "values": {},
                }
                return {}
            return copy.deepcopy(entry.get("values") or {})

    def _save_stored_values(self, task_id: str, epoch: int, scope_revision: int, values: dict[str, Any]) -> None:
        if not task_id or not isinstance(values, dict):
            return
        if len(encoded(values)) > MAX_STORED_STATE_BYTES:
            raise AgentToolRejected(
                "agent_output_limit",
                correction="Persisted store() values exceed the 256 KB per-task limit.",
            )
        with self._lock:
            self._task_stores[task_id] = {
                "context_epoch": epoch,
                "scope_revision": scope_revision,
                "values": copy.deepcopy(values),
            }

    def _read_only_catalog(self, context: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, str]]]:
        specs_by_name: dict[str, dict[str, Any]] = {}
        meta_by_name: dict[str, dict[str, Any]] = {}
        enabled_tools: list[dict[str, str]] = []
        desktop_enabled = bool(((context or {}).get("local_execution") or {}).get("desktop_access"))

        for spec in self.parent_tools.specs():
            fn = spec.get("function") or {}
            t_name = str(fn.get("name") or "")
            if not t_name or t_name == NAME:
                continue
            try:
                meta = self.parent_tools.metadata(t_name)
            except Exception:
                continue
            if meta.get("effect") != "read":
                continue
            if (meta.get("computer_access") or (meta.get("extension_binding") or {}).get("computer_access")) and not desktop_enabled:
                continue
            specs_by_name[t_name] = spec
            meta_by_name[t_name] = meta
            enabled_tools.append({
                "name": t_name,
                "sanitized_name": sanitize_js_tool_name(t_name),
                "description": str(fn.get("description") or "")[:240],
            })
        return specs_by_name, meta_by_name, enabled_tools

    def execute(
        self,
        args: dict[str, Any],
        *,
        operation_id: str | None = None,
        cancelled: Any = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if callable(cancelled) and cancelled():
            raise AgentToolRejected("agent_cancelled")

        raw_code = args.get("code")
        code, pragma = parse_exec_pragma(raw_code)
        yield_time_ms = int(args.get("yield_time_ms") or pragma.get("yield_time_ms") or DEFAULT_YIELD_TIME_MS)
        yield_time_ms = max(50, min(yield_time_ms, MAX_YIELD_TIME_MS))
        max_output_tokens = int(args.get("max_output_tokens") or pragma.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS)
        max_output_tokens = max(64, min(max_output_tokens, 16000))
        max_output_chars = max_output_tokens * 4
        reset = bool(args.get("reset", False))

        ctx = context if isinstance(context, dict) else {}
        task_id = str(ctx.get("task_id") or "")
        epoch = int(ctx.get("context_epoch", 0))
        scope_revision = int(ctx.get("scope_revision", 1))

        stored_values = self._get_stored_values(task_id, epoch, scope_revision, reset=reset)
        specs_by_name, meta_by_name, enabled_tools = self._read_only_catalog(ctx)
        sanitized_to_raw = {item["sanitized_name"]: item["name"] for item in enabled_tools}

        node_exe = _resolve_node_binary()
        env = sanitized_worker_env(session_id=f"codemode_{task_id[:16] or 'anon'}", generation=epoch + 1)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            [node_exe, "-e", _NODE_HOST_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

        write_lock = threading.Lock()
        frames_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        child_calls_lock = threading.Lock()
        child_calls_count = 0
        child_call_records: list[dict[str, Any]] = []
        collected_image_artifacts: list[dict[str, Any]] = []

        def send_to_worker(payload: dict[str, Any]) -> None:
            with write_lock:
                if proc.stdin and not proc.stdin.closed:
                    try:
                        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                        proc.stdin.flush()
                    except OSError:
                        pass

        def reader_loop() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line_s = line.strip()
                    if not line_s:
                        continue
                    try:
                        parsed = json.loads(line_s)
                        if isinstance(parsed, dict):
                            frames_q.put(parsed)
                    except ValueError:
                        continue
            finally:
                frames_q.put(None)

        reader_thread = threading.Thread(target=reader_loop, daemon=True, name="ryn-code-mode-reader")
        reader_thread.start()

        def handle_child_tool_call(frame: dict[str, Any]) -> None:
            nonlocal child_calls_count
            call_id = str(frame.get("id") or "")
            requested_name = str(frame.get("name") or "")
            raw_name = requested_name if requested_name in specs_by_name else sanitized_to_raw.get(requested_name, "")
            raw_args = frame.get("arguments")
            if raw_args is None:
                raw_args = {}

            with child_calls_lock:
                child_calls_count += 1
                ordinal = child_calls_count
                if ordinal > MAX_CHILD_CALLS:
                    send_to_worker({
                        "type": "tool_result",
                        "id": call_id,
                        "ok": False,
                        "error": "agent_code_exec_call_limit_exceeded",
                    })
                    return

            if callable(cancelled) and cancelled():
                send_to_worker({"type": "tool_result", "id": call_id, "ok": False, "error": "agent_cancelled"})
                return

            if not raw_name or raw_name not in specs_by_name:
                # Check if it's a known write tool to give a clear error code
                try:
                    other_meta = self.parent_tools.metadata(requested_name)
                    err_code = (
                        "agent_code_exec_write_forbidden"
                        if other_meta.get("effect") == "write"
                        else "agent_unknown_tool"
                    )
                except Exception:
                    err_code = "agent_unknown_tool"
                send_to_worker({"type": "tool_result", "id": call_id, "ok": False, "error": err_code})
                return

            if not isinstance(raw_args, dict):
                send_to_worker({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": False,
                    "error": "agent_tool_arguments_invalid",
                })
                return

            spec = specs_by_name[raw_name]
            meta = meta_by_name[raw_name]
            try:
                validate(raw_args, spec["function"]["parameters"])
            except ValidationError as exc:
                send_to_worker({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": False,
                    "error": f"agent_tool_arguments_invalid: {exc.message}",
                })
                return

            child_op_id = f"{operation_id or 'code'}_sub_{ordinal}_{uuid.uuid4().hex[:8]}"
            exec_kwargs: dict[str, Any] = {
                "operation_id": child_op_id,
                "cancelled": cancelled if callable(cancelled) else (lambda: False),
            }
            child_ctx: dict[str, Any] | None = None
            if meta.get("scope_context_supported"):
                child_ctx = copy.deepcopy(ctx)
                child_ctx["operation_id"] = child_op_id
                if "extension_binding" in meta:
                    child_ctx["extension_binding"] = copy.deepcopy(meta["extension_binding"])
                exec_kwargs["context"] = child_ctx

            try:
                res = sanitized(self.parent_tools.execute(raw_name, copy.deepcopy(raw_args), **exec_kwargs))
                if child_ctx is not None and isinstance(child_ctx.get("_deferred_observed_files"), dict):
                    parent_deferred = ctx.get("_deferred_observed_files")
                    if isinstance(parent_deferred, dict):
                        with child_calls_lock:
                            parent_deferred.update(child_ctx["_deferred_observed_files"])
                if isinstance(res, dict):
                    for art_key in ("mcp_artifacts", "image_artifacts", "native_observation_artifacts"):
                        arts = res.get(art_key)
                        if isinstance(arts, list) and arts:
                            with child_calls_lock:
                                for a in arts:
                                    if isinstance(a, dict):
                                        collected_image_artifacts.append(copy.deepcopy(a))
                with child_calls_lock:
                    child_call_records.append({
                        "ordinal": ordinal,
                        "tool": raw_name,
                        "status": "completed",
                    })
                send_to_worker({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": True,
                    "result": res,
                })
            except AgentToolRejected as exc:
                with child_calls_lock:
                    child_call_records.append({
                        "ordinal": ordinal,
                        "tool": raw_name,
                        "status": "rejected",
                        "error": str(exc),
                    })
                send_to_worker({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": False,
                    "error": str(exc),
                    "details": getattr(exc, "details", None),
                })
            except Exception as exc:
                with child_calls_lock:
                    child_call_records.append({
                        "ordinal": ordinal,
                        "tool": raw_name,
                        "status": "failed",
                        "error": str(exc),
                    })
                send_to_worker({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": False,
                    "error": str(exc),
                })

        deadline = time.monotonic() + (yield_time_ms / 1000.0)
        final_frame: dict[str, Any] | None = None

        try:
            send_to_worker({
                "type": "init",
                "code": code,
                "enabled_tools": enabled_tools,
                "stored_values": stored_values,
            })

            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="ryn-code-mode-tool") as pool:
                while True:
                    if callable(cancelled) and cancelled():
                        raise AgentToolRejected("agent_cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AgentToolRejected(
                            "agent_code_exec_timeout",
                            correction=f"CodeMode script exceeded yield_time_ms ({yield_time_ms} ms). Increase yield_time_ms or simplify the script.",
                        )
                    try:
                        frame = frames_q.get(timeout=min(0.1, remaining))
                    except queue.Empty:
                        if proc.poll() is not None and frames_q.empty():
                            break
                        continue
                    if frame is None:
                        break
                    f_type = frame.get("type")
                    if f_type == "tool_call":
                        pool.submit(handle_child_tool_call, frame)
                    elif f_type == "result":
                        final_frame = frame
                        break
        finally:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

        if final_frame is None:
            raise AgentToolRejected(
                "agent_code_exec_failed",
                correction="CodeMode worker terminated before producing a result.",
            )

        if isinstance(final_frame.get("stored_values"), dict):
            self._save_stored_values(task_id, epoch, scope_revision, final_frame["stored_values"])

        raw_items = final_frame.get("content_items") if isinstance(final_frame.get("content_items"), list) else []
        text_parts: list[str] = []
        content_items: list[dict[str, Any]] = []
        local_tools = getattr(self.parent_tools, "local", None)
        from .image_capabilities import ALLOWED_IMAGE_MIMES, safe_segment, verify_original_image_bytes
        import base64
        safe_task = safe_segment(task_id or "task", "task")
        safe_op = safe_segment(operation_id or "op", "op")
        img_idx = 0

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "input_text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
                content_items.append({"type": "input_text", "text": item["text"]})
            elif item.get("type") in {"input_image", "image"}:
                img_url = item.get("image_url")
                if isinstance(img_url, str) and img_url.startswith("data:image/") and ";base64," in img_url and local_tools is not None and hasattr(local_tools, "path"):
                    try:
                        header, b64_part = img_url.split(";base64,", 1)
                        mime = header[5:].strip().lower()
                        if mime == "image/jpg":
                            mime = "image/jpeg"
                        raw_bytes = base64.b64decode(b64_part.strip(), validate=True)
                        verified = verify_original_image_bytes(raw_bytes, expected_mime=mime)
                        ext = ALLOWED_IMAGE_MIMES[verified["mime_type"]][0][0]
                        rel_path = f".ryn/mcp/{safe_task}/{safe_op}/image_{img_idx}{ext}"
                        img_idx += 1
                        target_path = local_tools.path(rel_path, ctx)
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        target_path.write_bytes(raw_bytes)
                        if hasattr(local_tools, "_record_observed_files"):
                            local_tools._record_observed_files(ctx, {target_path: verified["sha256"]})
                        art_entry = {
                            "path": rel_path,
                            "sha256": verified["sha256"],
                            "bytes": len(raw_bytes),
                            "mime_type": verified["mime_type"],
                            "width": verified["width"],
                            "height": verified["height"],
                        }
                        collected_image_artifacts.append(art_entry)
                        content_items.append({"type": "image", **art_entry})
                        continue
                    except Exception:
                        content_items.append({"type": "image", "error": "agent_code_exec_image_invalid"})
                        continue
                content_items.append(copy.deepcopy(item))
                if item.get("type") == "image" and isinstance(item.get("path"), str):
                    collected_image_artifacts.append({"path": item["path"]})

        combined_text = "\n".join(text_parts)
        truncated = False
        if len(combined_text) > max_output_chars:
            combined_text = combined_text[:max_output_chars]
            truncated = True

        result_payload: dict[str, Any] = {
            "status": "completed" if final_frame.get("ok") else "failed",
            "output": combined_text,
            "content_items": content_items[:32],
            "tool_calls": child_call_records,
            "stored_keys": sorted((final_frame.get("stored_values") or {}).keys()) if isinstance(final_frame.get("stored_values"), dict) else [],
            "truncated": truncated,
        }
        if final_frame.get("return_value") is not None:
            result_payload["return_value"] = final_frame["return_value"]
        if not final_frame.get("ok"):
            result_payload["error"] = "agent_code_exec_runtime_error"
            result_payload["error_detail"] = str(final_frame.get("error") or "Script execution error")[:2000]
        if collected_image_artifacts:
            result_payload["image_artifacts"] = collected_image_artifacts[:8]
            result_payload["mcp_artifacts"] = copy.deepcopy(collected_image_artifacts[:8])
        return sanitized(result_payload)
