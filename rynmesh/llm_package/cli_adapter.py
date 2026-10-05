"""Text-only, non-interactive CLI adapters for a private Rynmesh provider.

The API path runs in a fresh empty directory. It never resumes an interactive
coding session or grants the caller access to the provider's project files.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

from .adapters import AdapterError
from .chat import validate_chat
from .process_guard import communicate_bounded, spawn, terminate


@lru_cache(maxsize=4)
def _mac_codex_apps(refresh_bucket: int) -> tuple[Path, ...]:
    """Resolve installed apps through Launch Services, including renamed/moved apps.

    Only reads the OS application registry; no app is launched, and no user shell
    startup scripts run. Cache briefly because the services screen polls health.
    """
    script = '''ObjC.import("AppKit"); var paths = [];
    ["com.openai.codex", "com.openai.chat"].forEach(function(id) {
      var url = $.NSWorkspace.sharedWorkspace.URLForApplicationWithBundleIdentifier(id);
      if (!url.isNil()) paths.push(ObjC.unwrap(url.path));
    }); JSON.stringify(paths);'''
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=3, check=True,
        )
        paths = json.loads(result.stdout)
        if isinstance(paths, list):
            return tuple(Path(p) for p in paths[:8] if isinstance(p, str) and PurePosixPath(p).is_absolute())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return ()


class CLIAgentAdapter:
    def __init__(self, kind: str, *, executable: str = "") -> None:
        if kind not in {"codex_cli", "claude_cli"}:
            raise ValueError("unsupported CLI adapter")
        self.kind = kind
        self.executable = executable or ("codex" if kind == "codex_cli" else "claude")
        self._lock = threading.Lock()
        self._running: dict[str, subprocess.Popen[str]] = {}
        self._cancelled: set[str] = set()

    def _command_path(self) -> str:
        found = shutil.which(self.executable)
        # Explorer-started desktop apps do not inherit Codex's private PATH.
        # The desktop installer keeps versioned CLI binaries in this folder.
        if not found and self.kind == "codex_cli" and self.executable == "codex":
            local_app_data = os.environ.get("LOCALAPPDATA", "")
            if local_app_data:
                root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
                candidates = [path for path in root.glob("*/codex.exe") if path.is_file()]
                if candidates:
                    found = str(max(candidates, key=lambda path: path.stat().st_mtime))
        # Finder-launched apps may not inherit Homebrew / shell PATH entries.
        # Query registered apps and standard command directories without running
        # shell startup files or replacing an explicitly configured executable.
        if not found and sys.platform == "darwin" and self.executable in {"codex", "claude"}:
            found = self._mac_command_path()
        if not found:
            raise AdapterError(f"{self.executable} is not installed or is not on PATH")
        return found

    def _mac_command_path(self) -> str | None:
        home = Path.home()
        name = self.executable
        candidates = []
        if self.kind == "codex_cli" and name == "codex":
            candidates.extend(app / "Contents" / "Resources" / "codex"
                              for app in _mac_codex_apps(int(time.monotonic() // 15)))
        # These are fallback command search directories, not app install paths.
        candidates.extend([
            Path("/opt/homebrew/bin") / name,
            Path("/usr/local/bin") / name,
            home / ".local" / "bin" / name,
            home / ".npm-global" / "bin" / name,
            home / ".volta" / "bin" / name,
        ])
        for path in candidates:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return None

    def health(self) -> dict[str, Any]:
        try:
            path = self._command_path()
            # An installed CLI may still need the owner to log in. Publication
            # uses a real self-test before advertising it as online.
            return {"ok": True, "runtime": self.kind, "executable": Path(path).name}
        except AdapterError as exc:
            return {"ok": False, "error": str(exc)}

    def models(self) -> list[dict[str, Any]]:
        return [{"id": self.kind}]

    def capabilities(self) -> dict[str, Any]:
        return {"chat_completions": True, "streaming": self.kind == "codex_cli",
                "tools": self.kind == "codex_cli", "tool_execution": "origin_client",
                "conversation_storage": "none", "cancel": "process_tree_termination"}

    @staticmethod
    def _prompt(messages: list[dict[str, Any]]) -> str:
        lines = ["Answer the conversation below. Return only the assistant answer. "
                 "Do not inspect files, run commands, or use tools."]
        for message in messages:
            role = message.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise AdapterError("CLI API mode supports text messages only")
            if message.get("tool_calls") or message.get("tool_call_id"):
                raise AdapterError("CLI API mode does not support client tool calls")
            content = message.get("content")
            if not isinstance(content, str):
                raise AdapterError("CLI API mode supports text messages only")
            lines.append(f"<{role}>\n{content}\n</{role}>")
        return "\n\n".join(lines)

    def _run(self, prompt: str, *, task_id: str, timeout_s: float,
             max_tokens: int) -> dict[str, Any]:
        from .codex_session import private_workspace

        if self.kind != "claude_cli":
            raise AdapterError("Codex requires the environment-free app-server")
        command = self._command_path()
        if max_tokens < 1:
            raise AdapterError("max_tokens must be positive")
        prompt = f"Keep the answer within {max_tokens} output tokens.\n\n{prompt}"
        with self._lock:
            if task_id in self._cancelled:
                self._cancelled.discard(task_id)
                raise AdapterError("task_cancelled")
        started = time.monotonic()
        with private_workspace(copy_auth=False) as (root, env):
            claude_home = root / "claude"
            claude_home.mkdir(mode=0o700)
            auth = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"
            if auth.is_file():
                if auth.stat().st_size > 1024 * 1024:
                    raise AdapterError("CLI authentication file exceeds limit")
                with (claude_home / ".credentials.json").open("xb") as output:
                    os.chmod(output.name, 0o600)
                    output.write(auth.read_bytes())
            for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
                if key in os.environ:
                    env[key] = os.environ[key]
            env.pop("OPENAI_API_KEY", None)
            env.update(CLAUDE_CONFIG_DIR=str(claude_home), CLAUDE_CODE_SIMPLE="1",
                       CLAUDE_CODE_SKIP_PROMPT_HISTORY="1", DISABLE_TELEMETRY="1",
                       DISABLE_ERROR_REPORTING="1", DISABLE_AUTOUPDATER="1")
            args = [command, "-p", "--tools", "", "--permission-mode", "dontAsk",
                    "--no-session-persistence", "--output-format", "json",
                    "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
            # subprocess receives an argument vector: user messages never
            # become shell commands or command-line flags.
            try:
                process = spawn(args, cwd=str(root), env=env, timeout=min(timeout_s, 300))
            except OSError as exc:
                raise AdapterError("could not start isolated CLI") from exc
            with self._lock:
                self._running[task_id] = process
                cancelled_before_start = task_id in self._cancelled
            if cancelled_before_start:
                process.kill()
            try:
                stdout = communicate_bounded(process, prompt.encode(), timeout=min(timeout_s, 300), root=root)
            except (OSError, RuntimeError) as exc:
                raise AdapterError("CLI execution failed or exceeded limits") from exc
            finally:
                with self._lock:
                    self._running.pop(task_id, None)
                    cancelled = task_id in self._cancelled
                    self._cancelled.discard(task_id)
            if cancelled:
                raise AdapterError("task_cancelled")
            if process.returncode:
                # Do not expose raw CLI stderr: it can contain local paths or
                # provider account details. Keep actionable, bounded errors.
                raise AdapterError(f"{self.executable} exited with status {process.returncode}; check local login and CLI version")
            if len(stdout) > 8 * 1024 * 1024:
                raise AdapterError("CLI response exceeds the 8 MiB limit")
            try:
                payload = json.loads(stdout)
                answer = payload.get("result")
                usage = payload.get("usage") or {}
            except (ValueError, TypeError, AttributeError) as exc:
                raise AdapterError("Claude Code returned invalid JSON") from exc
            if payload.get("is_error") or not isinstance(answer, str):
                raise AdapterError("Claude Code did not return a text answer")
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            return {"text": answer, "message": {"role": "assistant", "content": answer},
                    "input_tokens": max(0, input_tokens), "output_tokens": max(0, output_tokens),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "finish_reason": "stop"}

    def infer(self, *, prompt: str, max_tokens: int, task_id: str,
              timeout_s: float, model: str = "") -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt:
            raise AdapterError("prompt is required")
        return self.chat({"messages": [{"role": "user", "content": prompt}],
                          "max_tokens": max_tokens}, task_id=task_id,
                         timeout_s=timeout_s, model=model)

    def chat(self, body: dict[str, Any], *, task_id: str, timeout_s: float,
             on_event: Any = None, model: str = "") -> dict[str, Any]:
        with self._lock:
            if task_id in self._cancelled:
                self._cancelled.discard(task_id)
                raise AdapterError("task_cancelled")
        body = validate_chat(body)
        if self.kind == "codex_cli":
            from .codex_session import chat
            for key in ("temperature", "top_p", "stop", "enable_thinking"):
                if key in body:
                    raise AdapterError(f"CLI API mode does not support {key}")
            result = chat(self, body, task_id=task_id, timeout_s=min(timeout_s, 300), model=model)
            if body.get("stream") and on_event:
                # Buffered SSE: no provider turn remains alive awaiting tools.
                delta = dict(result["message"])
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(delta["tool_calls"])]
                on_event({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                on_event({"choices": [{"index": 0, "delta": {}, "finish_reason": result["finish_reason"]}],
                          "usage": {"prompt_tokens": result["input_tokens"], "completion_tokens": result["output_tokens"],
                                    "total_tokens": result["input_tokens"] + result["output_tokens"]}})
            return result
        if body.get("tools") or body.get("tool_choice", "auto") not in ("auto", "none"):
            raise AdapterError("CLI API mode does not support client tool calls")
        for key in ("temperature", "top_p", "stop", "enable_thinking"):
            if key in body:
                raise AdapterError(f"CLI API mode does not support {key}")
        if body.get("stream"):
            raise AdapterError("CLI API mode does not support streaming yet")
        return self._run(self._prompt(body["messages"]), task_id=task_id,
                         timeout_s=timeout_s, max_tokens=int(body.get("max_tokens") or 512))

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            self._cancelled.add(task_id)
            process = self._running.get(task_id)
        if process and process.poll() is None:
            terminate(process)
        return True

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            return {"running": len(self._running)}

    def shutdown(self) -> None:
        with self._lock:
            ids = list(self._running)
        for task_id in ids:
            self.cancel(task_id)
