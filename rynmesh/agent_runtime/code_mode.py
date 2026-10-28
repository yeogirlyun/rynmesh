"""Codex-aligned CodeMode (`ryn_code_exec`) for multi-tool JS composition, persistent task state, and Codex Browser Use.

Grounded in reverse-engineered OpenAI Codex (`codex.exe` CodeMode, `codex-code-mode-host.exe`, and `oai_browser_use`):
- Runs JavaScript in an isolated Node `vm` context with no direct filesystem, raw process, or CommonJS module globals.
- Maintains a persistent per-task VM session across turns so top-level bindings (`chrome`, `openTabs`, `tab`, `agent`)
  and `store(key, value)` / `load(key)` state survive across `ryn_code_exec` calls within the same task.
- Exposes Codex's complete `setupBrowserRuntime()` + `agent.browsers.get("chrome")` (`oai_browser_use`) runtime,
  connecting to the user's live Chrome via the Codex Chrome Extension pipe (`\\\\.\\pipe\\codex-browser-use\\*`)
  with automatic fallback to a built-in CDP-to-Codex-Pipe bridge (`cdp-pipe-server.mjs`).
- Exposes all enabled read-only tools on `globalThis.tools.<name>(args)` and metadata in `ALL_TOOLS`.
- Supports `await Promise.all([...])`, `await Promise.allSettled([...])`, `as_settled(promises)`,
  and `stream_settled(promises)` with concurrent host-side read tool execution.
- Exposes `text(...)`, `image(...)` / `emitImage(...)`, `store(key, value)`, `load(key)`, and `exit()`.
- Supports leading `// @exec: {"yield_time_ms": ..., "max_output_tokens": ...}` pragma comments.
- Strictly enforces read-only tool execution, JSON schema validation, task scope, and desktop_access gates.
"""
from __future__ import annotations

import atexit
import base64
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
import weakref
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate

from .chrome_runtime import PACKAGE_ROOT as CHROME_RUNTIME_DIR
from .js_runtime.manager import sanitized_worker_env
from .local_browser import find_browser
from .protocol import sanitized
from .store import AgentToolRejected, encoded


def _resolve_node_binary() -> str:
    env_override = os.environ.get("RYN_NODE_EXECUTABLE") or os.environ.get("RYN_JS_NODE_EXECUTABLE")
    if isinstance(env_override, str) and env_override.strip() and Path(env_override.strip()).is_file():
        return str(Path(env_override.strip()).resolve())
    found = shutil.which("node")
    if found and Path(found).is_file():
        return str(Path(found).resolve())
    candidates = (
        [
            Path(r"C:\Program Files\nodejs\node.exe"),
            Path(r"C:\Program Files (x86)\nodejs\node.exe"),
            Path(r"C:\nvm4w\nodejs\node.exe"),
        ]
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
MAX_ACTIVE_TASK_WORKERS = 8

_IDENT_INVALID_RE = re.compile(r"[^A-Za-z0-9_$]")

SPEC = {
    "type": "function",
    "function": {
        "name": NAME,
        "description": (
            "Run JavaScript in a sandboxed CodeMode runtime to batch and compose read-only tools "
            "(`await Promise.all([...])`, `as_settled([...])`, `stream_settled([...])`), filter/transform "
            "results in code, persist intermediate values and top-level bindings across turns (`store(key, value)` / `load(key)`), "
            "and control Chrome/browsers via Codex Browser Use (`await setupBrowserRuntime(); const chrome = await agent.browsers.get('chrome')`) "
            "when desktop_access is enabled. All read-only tools are available on `tools.<name>(args)` and listed in `ALL_TOOLS`. "
            "Use `text(...)` / `console.log(...)` to emit output, `image(...)` / `emitImage(...)` to attach an image, or return a JSON-serializable value."
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

# Sandboxed Node runner script modelled on `codex-code-mode-host.exe` and `node_repl` + `oai_browser_use`.
_NODE_HOST_SCRIPT = r"""
'use strict';
const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const readline = require('node:readline');
const { pathToFileURL } = require('node:url');
const vm = require('node:vm');

const EarlyExitSignal = Symbol('EarlyExitSignal');
const rawStdoutWrite = process.stdout.write.bind(process.stdout);

function sendFrame(frame) {
  rawStdoutWrite(JSON.stringify(frame) + '\n');
}

function formatValue(val) {
  if (typeof val === 'string') return val;
  if (val === undefined) return 'undefined';
  if (
    val &&
    typeof val === 'object' &&
    (val.type === 'value' || val.type === 'object' || val.type === 'error' || val.type === 'function') &&
    'value' in val &&
    Object.keys(val).length <= 2
  ) {
    if (typeof val.value === 'string') return val.value;
    if (val.value === undefined) return 'undefined';
    val = val.value;
  }
  try {
    const s = JSON.stringify(val);
    return typeof s === 'string' ? s : String(val);
  } catch (_) {
    return String(val);
  }
}

function normalizeImageItem(val) {
  if (val && (ArrayBuffer.isView(val) || Buffer.isBuffer(val))) {
    const buf = Buffer.isBuffer(val)
      ? val
      : Buffer.from(val.buffer, val.byteOffset, val.byteLength);
    let mime = 'image/png';
    if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) {
      mime = 'image/jpeg';
    } else if (
      buf.length >= 12 &&
      buf.toString('ascii', 0, 4) === 'RIFF' &&
      buf.toString('ascii', 8, 12) === 'WEBP'
    ) {
      mime = 'image/webp';
    }
    return { type: 'input_image', image_url: `data:${mime};base64,${buf.toString('base64')}` };
  }
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
  throw new TypeError('image() expects a non-empty URL/path string, Uint8Array, or image object');
}

function safeSerializeReturn(val) {
  if (val === undefined || typeof val === 'function' || typeof val === 'symbol') return null;
  try {
    const raw = JSON.stringify(val, (_k, v) => {
      if (typeof v === 'bigint') return v.toString();
      if (v && (ArrayBuffer.isView(v) || Buffer.isBuffer(v))) {
        const buf = Buffer.isBuffer(v) ? v : Buffer.from(v.buffer, v.byteOffset, v.byteLength);
        return { __type: 'Uint8Array', byteLength: buf.byteLength };
      }
      return v;
    });
    return typeof raw === 'string' ? JSON.parse(raw) : null;
  } catch (_) {
    return String(val);
  }
}

const BUILTIN_GLOBALS = new Set([
  'globalThis', 'ALL_TOOLS', 'tools', 'text', 'image', 'emitImage',
  'store', 'load', 'as_settled', 'stream_settled', 'exit', 'console',
  'setupBrowserRuntime', 'nodeRepl', '__ryn_import', '__ryn_sync'
]);

const RESERVED_WORDS = new Set([
  'const', 'let', 'var', 'function', 'class', 'return', 'await', 'async',
  'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break', 'continue',
  'try', 'catch', 'finally', 'throw', 'new', 'typeof', 'instanceof', 'in',
  'of', 'void', 'delete', 'true', 'false', 'null', 'undefined'
]);

function extractTopLevelDeclaredNames(code) {
  const tokens = [];
  let i = 0;
  const n = code.length;
  let depth = 0;
  const templateBraceStack = [];

  while (i < n) {
    const ch = code[i];
    if (ch === ' ' || ch === '\t' || ch === '\r') { i++; continue; }
    if (ch === '\n') {
      tokens.push({ type: 'newline', value: '\n', depth });
      i++;
      continue;
    }
    if (ch === '/' && code[i + 1] === '/') {
      i += 2;
      while (i < n && code[i] !== '\n') i++;
      continue;
    }
    if (ch === '/' && code[i + 1] === '*') {
      i += 2;
      while (i < n && !(code[i] === '*' && code[i + 1] === '/')) i++;
      i += 2;
      continue;
    }
    if (ch === '"' || ch === "'") {
      const quote = ch;
      i++;
      while (i < n && code[i] !== quote) {
        if (code[i] === '\\') i++;
        i++;
      }
      i++;
      tokens.push({ type: 'literal', value: 'str', depth });
      continue;
    }
    if (ch === '`') {
      i++;
      while (i < n && code[i] !== '`') {
        if (code[i] === '\\') { i += 2; continue; }
        if (code[i] === '$' && code[i + 1] === '{') {
          templateBraceStack.push(depth);
          depth++;
          i += 2;
          break;
        }
        i++;
      }
      if (i < n && code[i] === '`') i++;
      tokens.push({ type: 'literal', value: 'tpl', depth });
      continue;
    }
    if (ch === '{' || ch === '(' || ch === '[') {
      tokens.push({ type: 'punct', value: ch, depth });
      depth++;
      i++;
      continue;
    }
    if (ch === '}' || ch === ')' || ch === ']') {
      depth = Math.max(0, depth - 1);
      tokens.push({ type: 'punct', value: ch, depth });
      i++;
      if (ch === '}' && templateBraceStack.length > 0 && templateBraceStack[templateBraceStack.length - 1] === depth) {
        templateBraceStack.pop();
        while (i < n && code[i] !== '`') {
          if (code[i] === '\\') { i += 2; continue; }
          if (code[i] === '$' && code[i + 1] === '{') {
            templateBraceStack.push(depth);
            depth++;
            i += 2;
            break;
          }
          i++;
        }
        if (i < n && code[i] === '`') i++;
      }
      continue;
    }
    if (/[A-Za-z_$]/.test(ch)) {
      const start = i++;
      while (i < n && /[A-Za-z0-9_$]/.test(code[i])) i++;
      tokens.push({ type: 'ident', value: code.slice(start, i), depth });
      continue;
    }
    if (/[0-9]/.test(ch)) {
      const start = i++;
      while (i < n && /[A-Za-z0-9_.]/.test(code[i])) i++;
      tokens.push({ type: 'literal', value: code.slice(start, i), depth });
      continue;
    }
    if (ch === '.' && code[i + 1] === '.' && code[i + 2] === '.') {
      tokens.push({ type: 'punct', value: '...', depth });
      i += 3;
      continue;
    }
    tokens.push({ type: 'punct', value: ch, depth });
    i++;
  }

  const names = new Set();
  function addCandidate(name) {
    if (name && !RESERVED_WORDS.has(name) && !BUILTIN_GLOBALS.has(name)) {
      names.add(name);
    }
  }

  function extractPattern(startIdx) {
    let idx = startIdx;
    while (idx < tokens.length && tokens[idx].type === 'newline') idx++;
    if (idx >= tokens.length) return idx;
    const tok = tokens[idx];
    if (tok.type === 'ident') {
      addCandidate(tok.value);
      return idx + 1;
    }
    if (tok.type === 'punct' && (tok.value === '{' || tok.value === '[')) {
      const baseDepth = tok.depth;
      const closeChar = tok.value === '{' ? '}' : ']';
      idx++;
      while (idx < tokens.length) {
        const t = tokens[idx];
        if (t.depth === baseDepth && t.type === 'punct' && t.value === closeChar) {
          return idx + 1;
        }
        if (t.depth === baseDepth + 1 && t.type === 'ident') {
          let prev = idx - 1;
          while (prev >= 0 && tokens[prev].type === 'newline') prev--;
          let next = idx + 1;
          while (next < tokens.length && tokens[next].type === 'newline') next++;
          const prevTok = tokens[prev];
          const nextTok = tokens[next];
          const isAfterInitializerEq = prevTok && prevTok.depth === baseDepth + 1 && prevTok.value === '=';
          const isObjectKeyWithAlias = closeChar === '}' && nextTok && nextTok.depth === baseDepth + 1 && nextTok.value === ':';
          if (!isAfterInitializerEq && !isObjectKeyWithAlias) {
            addCandidate(t.value);
          }
        }
        idx++;
      }
    }
    return idx;
  }

  const contPunct = new Set(['=', '+', '-', '*', '/', '%', '&', '|', '^', '!', '~', '<', '>', '?', ':', '.', ',']);
  for (let k = 0; k < tokens.length; k++) {
    const t = tokens[k];
    if (t.depth !== 0 || t.type !== 'ident') continue;
    let prev = k - 1;
    while (prev >= 0 && tokens[prev].type === 'newline') prev--;
    if (prev >= 0 && tokens[prev].type === 'punct' && tokens[prev].value === '.') continue;

    if (t.value === 'function' || t.value === 'class') {
      let m = k + 1;
      while (m < tokens.length && (tokens[m].type === 'newline' || (tokens[m].type === 'punct' && tokens[m].value === '*'))) m++;
      if (m < tokens.length && tokens[m].type === 'ident') {
        addCandidate(tokens[m].value);
      }
      continue;
    }
    if (t.value === 'const' || t.value === 'let' || t.value === 'var') {
      let m = k + 1;
      while (m < tokens.length) {
        m = extractPattern(m);
        let sawComma = false;
        while (m < tokens.length) {
          const cur = tokens[m];
          if (cur.depth === 0) {
            if (cur.type === 'punct' && cur.value === ',') {
              sawComma = true;
              m++;
              break;
            }
            if (cur.type === 'punct' && cur.value === ';') {
              m++;
              break;
            }
            if (cur.type === 'newline') {
              let p = m - 1;
              while (p >= k && tokens[p].type === 'newline') p--;
              let nx = m + 1;
              while (nx < tokens.length && tokens[nx].type === 'newline') nx++;
              const prevT = tokens[p];
              const nextT = tokens[nx];
              const prevContinues = prevT && (
                (prevT.type === 'punct' && contPunct.has(prevT.value)) ||
                (prevT.type === 'ident' && (prevT.value === 'await' || prevT.value === 'new' || prevT.value === 'typeof' || prevT.value === 'void'))
              );
              const nextContinues = nextT && nextT.depth === 0 && nextT.type === 'punct' && (contPunct.has(nextT.value) || nextT.value === '(' || nextT.value === '[');
              if (!prevContinues && !nextContinues) {
                m++;
                break;
              }
            }
          }
          m++;
        }
        if (!sawComma) break;
      }
      k = m - 1;
    }
  }
  return Array.from(names);
}

const pendingCalls = new Map();
let nextCallId = 1;

let workerConfig = {
  task_id: 'anon',
  chrome_runtime_dir: '',
  browser_executable: '',
  headless_browser: false,
  force_cdp_fallback: false,
  cdp_port: null,
  extension_pipe: '',
};

let activeExecState = null;
let storedValues = Object.create(null);
let sandbox = null;
let vmContext = null;

// Redirect host-realm console.log / display so browser-client.mjs displayBridge writes into the active cell's contentItems.
globalThis.console = Object.freeze({
  log: (...args) => { if (activeExecState) activeExecState.addText(...args); },
  info: (...args) => { if (activeExecState) activeExecState.addText(...args); },
  warn: (...args) => { if (activeExecState) activeExecState.addText(...args); },
  error: (...args) => { if (activeExecState) activeExecState.addText(...args); },
});
globalThis.display = async (val) => {
  if (activeExecState) activeExecState.addText(val);
};

// Browser runtime state
let cdpPipeMod = null;
let cdpPipeServerInstance = null;
let fallbackChromeProc = null;
let serviceMod = null;
let clientMod = null;
let cachedAgentProxy = null;
let cachedHelperObj = null;

const tomlStore = new Map([
  [
    'browser/config.toml',
    {
      approval_mode: 'never_ask',
      history_approval_mode: 'never_ask',
      iab_history_approval_mode: 'never_ask',
      download_approval_mode: 'never_ask',
      upload_approval_mode: 'never_ask',
      full_cdp_access_enabled: true,
      webmcp_enabled: true,
      disable_auto_review: true,
    },
  ],
]);

function discoverLiveExtensionPipes() {
  const found = [];
  const explicitPipe = workerConfig.extension_pipe || process.env.RYN_CHROME_EXTENSION_PIPE || '';
  if (explicitPipe) {
    found.push(explicitPipe);
  }
  if (process.platform === 'win32') {
    try {
      const entries = fs.readdirSync('\\\\.\\pipe\\');
      for (const name of entries) {
        if (name.startsWith('codex-browser-use\\')) {
          const full = '\\\\.\\pipe\\' + name;
          if (!found.includes(full)) found.push(full);
        }
      }
    } catch (_) {}
  } else {
    const sockDir = '/tmp/codex-browser-use';
    try {
      if (fs.existsSync(sockDir)) {
        for (const name of fs.readdirSync(sockDir)) {
          const full = path.join(sockDir, name);
          if (!found.includes(full)) found.push(full);
        }
      }
    } catch (_) {}
  }
  return found;
}

async function probeLoopbackCdpPort(port, timeoutMs = 400) {
  if (!Number.isInteger(port) || port <= 0 || port > 65535) return false;
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/json/version`, { timeout: timeoutMs }, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function detectExistingChromeCdpPort() {
  if (workerConfig.cdp_port) return Number(workerConfig.cdp_port);
  if (process.env.RYN_CHROME_CDP_PORT) return Number(process.env.RYN_CHROME_CDP_PORT);
  if (workerConfig.force_cdp_fallback || process.env.RYN_CHROME_FORCE_CDP_FALLBACK === '1') {
    return null;
  }
  const candidates = [];
  if (process.platform === 'win32' && process.env.LOCALAPPDATA) {
    candidates.push(path.join(process.env.LOCALAPPDATA, 'Google', 'Chrome', 'User Data', 'DevToolsActivePort'));
  } else if (process.platform === 'darwin' && process.env.HOME) {
    candidates.push(path.join(process.env.HOME, 'Library', 'Application Support', 'Google', 'Chrome', 'DevToolsActivePort'));
  } else if (process.env.HOME) {
    candidates.push(path.join(process.env.HOME, '.config', 'google-chrome', 'DevToolsActivePort'));
  }
  for (const portFile of candidates) {
    try {
      if (fs.existsSync(portFile)) {
        const firstLine = fs.readFileSync(portFile, 'utf8').split(/\r?\n/)[0].trim();
        const port = Number(firstLine);
        if (await probeLoopbackCdpPort(port)) {
          return port;
        }
      }
    } catch (_) {}
  }
  return null;
}

async function startFallbackCdpBackend() {
  if (cdpPipeServerInstance) return cdpPipeServerInstance.pipePath;
  const scriptsDir = path.join(workerConfig.chrome_runtime_dir, 'scripts');
  if (!cdpPipeMod) {
    cdpPipeMod = await import(pathToFileURL(path.join(scriptsDir, 'cdp-pipe-server.mjs')).href);
  }
  let cdpPort = await detectExistingChromeCdpPort();
  let browserExecutablePath = workerConfig.browser_executable || process.env.RYN_CHROME_EXECUTABLE || '';
  let userDataDir = '';
  if (!cdpPort) {
    fallbackChromeProc = await cdpPipeMod.launchLocalCdpChrome({
      executablePath: browserExecutablePath || undefined,
      headless: Boolean(workerConfig.headless_browser),
    });
    cdpPort = fallbackChromeProc.port;
    browserExecutablePath = fallbackChromeProc.executablePath || browserExecutablePath;
    userDataDir = fallbackChromeProc.userDataDir || '';
  }
  cdpPipeServerInstance = await cdpPipeMod.startCdpPipeServer({
    cdpPort,
    cdpHost: '127.0.0.1',
    browserExecutablePath,
    userDataDir,
  });
  if (globalThis.nodeRepl && globalThis.nodeRepl.env) {
    globalThis.nodeRepl.env.BROWSER_USE_BACKEND_PATHS = cdpPipeServerInstance.pipePath;
  }
  process.env.BROWSER_USE_BACKEND_PATHS = cdpPipeServerInstance.pipePath;
  return cdpPipeServerInstance.pipePath;
}

function assertDesktopAccessForBrowser() {
  if (!activeExecState || !activeExecState.desktopAccess) {
    const err = new Error('agent_desktop_access_required: Browser control in ryn_code_exec requires local_execution.desktop_access=true');
    err.rejectedCode = 'agent_desktop_access_required';
    if (activeExecState) activeExecState.rejectedCode = 'agent_desktop_access_required';
    throw err;
  }
}

async function ensureBrowserServiceReady() {
  assertDesktopAccessForBrowser();
  const runtimeDir = workerConfig.chrome_runtime_dir;
  const scriptsDir = path.join(runtimeDir, 'scripts');
  const wasmPath = fs.existsSync(path.join(scriptsDir, 'browser-accessibility.wasm.br'))
    ? path.join(scriptsDir, 'browser-accessibility.wasm.br')
    : path.join(runtimeDir, 'assets', 'browser-accessibility.wasm.br');

  let backendPaths = '';
  const forceFallback = Boolean(
    workerConfig.force_cdp_fallback ||
    workerConfig.cdp_port ||
    process.env.RYN_CHROME_FORCE_CDP_FALLBACK === '1' ||
    process.env.RYN_CHROME_CDP_PORT
  );
  if (!forceFallback) {
    const livePipes = discoverLiveExtensionPipes();
    if (livePipes.length > 0) {
      backendPaths = livePipes.join(path.delimiter);
    }
  }
  if (!backendPaths) {
    backendPaths = await startFallbackCdpBackend();
  }
  process.env.BROWSER_USE_BACKEND_PATHS = backendPaths;
  process.env.BROWSER_USE_ACCESSIBILITY_CORE_WASM_PATH = wasmPath;

  if (!globalThis.nodeRepl) {
    const sessionId = `sess_${workerConfig.task_id || 'ryn'}`;
    globalThis.nodeRepl = {
      cwd: process.cwd(),
      tmpDir: os.tmpdir(),
      env: {
        BROWSER_USE_SECURITY_MODE: 'disabled-for-local-testing',
        BROWSER_USE_DISABLE_AMBIENT_NETWORK: '1',
        BROWSER_USE_FULL_CDP_ACCESS_ENABLED: '1',
        BROWSER_USE_TINYSKY_ENABLED: '1',
        BROWSER_USE_ACCESSIBILITY_CORE_WASM_PATH: wasmPath,
        BROWSER_USE_BACKEND_PATHS: backendPaths,
      },
      requestMeta: {
        'x-codex-turn-metadata': JSON.stringify({
          session_id: sessionId,
          turn_id: `turn_${Date.now()}`,
          thread_id: sessionId,
          thread_source: 'user',
        }),
        conversation_id: sessionId,
        session_id: sessionId,
        turn_id: `turn_${Date.now()}`,
      },
      setResponseMeta() {},
      emitContentItem(item) {
        if (activeExecState && item && typeof item === 'object') {
          activeExecState.contentItems.push(item);
        }
      },
      emitImage(img) {
        if (activeExecState) activeExecState.addImage(img);
      },
      write(val) {
        if (activeExecState) activeExecState.addText(val);
      },
      viewImage: async (img) => {
        if (activeExecState) activeExecState.addImage(img);
      },
      createElicitation: async () => ({ action: 'accept', _meta: { approvals_reviewer: 'user' } }),
      addAfterSubmittedCodeHook: () => () => {},
      addTurnEndedHandler: () => () => {},
      config: {
        read: async () => ({ config: {} }),
        readRequirements: async () => ({ requirements: {} }),
        readToml: async (k) => tomlStore.get(k) || {},
        writeToml: async (k, v) => {
          tomlStore.set(k, v);
        },
      },
      nativePipe: {
        createConnection(pipePath) {
          return new Promise((resolve, reject) => {
            const tryConnect = (targetPath, allowRescan) => {
              const s = net.createConnection(targetPath);
              const onConnect = () => {
                s.removeListener('error', onError);
                resolve(s);
              };
              const onError = (err) => {
                s.removeListener('connect', onConnect);
                if (allowRescan && typeof targetPath === 'string' && targetPath.includes('codex-browser-use\\')) {
                  const fresh = discoverLiveExtensionPipes();
                  if (fresh.length > 0 && fresh[0] !== targetPath) {
                    tryConnect(fresh[0], false);
                    return;
                  }
                }
                reject(err);
              };
              s.once('connect', onConnect);
              s.once('error', onError);
            };
            tryConnect(pipePath, true);
          });
        },
      },
      fetch: globalThis.fetch.bind(globalThis),
      async rpc(service, payload) {
        assertDesktopAccessForBrowser();
        if (service === 'browser') {
          if (!serviceMod) {
            serviceMod = await import(pathToFileURL(path.join(scriptsDir, 'browser-service.mjs')).href);
          }
          return await serviceMod.handleRpc(payload);
        }
        throw new Error('Unknown RPC service: ' + service);
      },
    };
  } else {
    globalThis.nodeRepl.env.BROWSER_USE_BACKEND_PATHS = backendPaths;
    globalThis.nodeRepl.env.BROWSER_USE_ACCESSIBILITY_CORE_WASM_PATH = wasmPath;
  }

  if (!clientMod) {
    clientMod = await import(pathToFileURL(path.join(scriptsDir, 'browser-client.mjs')).href);
  }
}

function buildTabDecorator(userDecorateTab) {
  const normPoint = (pt) => {
    if (Array.isArray(pt) && pt.length >= 2) return [Number(pt[0]), Number(pt[1])];
    if (pt && typeof pt === 'object' && typeof pt.x === 'number' && typeof pt.y === 'number') return [pt.x, pt.y];
    return pt;
  };
  const normTarget = (target, extra) => {
    if (Array.isArray(target) || (target && typeof target === 'object')) return normPoint(target);
    if (target === undefined && extra && typeof extra === 'object') {
      if (extra.point !== undefined) return normPoint(extra.point);
      if (typeof extra.x === 'number' && typeof extra.y === 'number') return [extra.x, extra.y];
    }
    return target;
  };
  return (tab) => {
    try {
      if (tab && tab.ax) {
        if (typeof tab.ax.click === 'function' && !tab.ax.click._rynWrapped) {
          const origClick = tab.ax.click.bind(tab.ax);
          const wrappedClick = async (target, options) => origClick(normTarget(target, options), options);
          wrappedClick._rynWrapped = true;
          tab.ax.click = wrappedClick;
        }
        if (typeof tab.ax.scroll === 'function' && !tab.ax.scroll._rynWrapped) {
          const origScroll = tab.ax.scroll.bind(tab.ax);
          const wrappedScroll = async (target, direction, pages) => origScroll(normPoint(target), direction, pages);
          wrappedScroll._rynWrapped = true;
          tab.ax.scroll = wrappedScroll;
        }
        if (typeof tab.ax.drag === 'function' && !tab.ax.drag._rynWrapped) {
          const origDrag = tab.ax.drag.bind(tab.ax);
          const wrappedDrag = async (from, to) => origDrag(normPoint(from), normPoint(to));
          wrappedDrag._rynWrapped = true;
          tab.ax.drag = wrappedDrag;
        }
        if (typeof tab.ax.getState !== 'function') {
          tab.ax.getState = async (opts) => {
            const state = String((await tab.ax.get('state', opts)) ?? '');
            return Object.assign(new String(state), { state, toString: () => state, valueOf: () => state });
          };
        }
        if (typeof tab.ax.action !== 'function') {
          tab.ax.action = async (act) => {
            if (!act || typeof act !== 'object') {
              throw new Error('tab.ax.action expects an action object');
            }
            const kind = act.kind || act.type;
            const idx = normTarget(act.element_index ?? act.target ?? act.index, act);
            if (kind === 'click') return await tab.ax.click(idx, act);
            if (kind === 'set_value' || kind === 'setValue') return await tab.ax.setValue(idx, act.value ?? act.text ?? '');
            if (kind === 'type_text' || kind === 'typeText') return await tab.ax.typeText(idx, act.text ?? act.value ?? '');
            if (kind === 'press_key' || kind === 'pressKey') return await tab.ax.pressKey(idx, act.key, act);
            if (kind === 'scroll') return await tab.ax.scroll(idx, act.direction ?? 'down', act.pages);
            if (kind === 'select_text' || kind === 'selectText') return await tab.ax.selectText(idx, act.text, act);
            if (kind === 'paste') return await tab.ax.paste(idx, act.text ?? '', act);
            if (kind === 'drag') return await tab.ax.drag(normPoint(act.from), normPoint(act.to));
            if (kind === 'perform_secondary_action') return await tab.ax.performSecondaryAction(idx, act.action);
            throw new Error('Unsupported ax action kind: ' + String(kind));
          };
        }
      }
    } catch (_) {}
    if (typeof userDecorateTab === 'function') {
      userDecorateTab(tab);
    }
  };
}

async function setupBrowserRuntimeWrapper(options = {}) {
  await ensureBrowserServiceReady();
  if (cachedAgentProxy && (!options || Object.keys(options).length === 0)) {
    if (sandbox) {
      sandbox.agent = cachedAgentProxy;
      sandbox._h = cachedHelperObj;
      sandbox.nodeRepl = globalThis.nodeRepl;
    }
    return cachedAgentProxy;
  }

  const effectiveOptions = {
    ...(options || {}),
    decorateTab: buildTabDecorator(options && options.decorateTab),
  };

  let rawAgent = await clientMod.setupBrowserRuntime(effectiveOptions);
  if (!cdpPipeServerInstance) {
    let browsers = [];
    try {
      browsers = await rawAgent.browsers.list();
    } catch (_) {}
    if (!Array.isArray(browsers) || browsers.length === 0) {
      await startFallbackCdpBackend();
      rawAgent = await clientMod.setupBrowserRuntime(effectiveOptions);
    }
  }

  const helperObj = Object.freeze({
    read: async (name) => await rawAgent.documentation.get(name),
    list: async () => ['accessibility', 'playwright', 'dom-cua', 'cua', 'content', 'clipboard', 'dev', 'screenshots'],
  });

  const agentProxy = new Proxy(rawAgent, {
    get(target, prop, receiver) {
      if (prop === 'agent') return agentProxy;
      if (prop === '_h') return helperObj;
      return Reflect.get(target, prop, receiver);
    },
  });

  cachedAgentProxy = agentProxy;
  cachedHelperObj = helperObj;
  if (sandbox) {
    sandbox.agent = agentProxy;
    sandbox._h = helperObj;
    sandbox.nodeRepl = globalThis.nodeRepl;
  }
  return agentProxy;
}

async function dynamicImportWrapper(specifier) {
  const s = String(specifier || '');
  if (s.includes('browser-client') || s.includes('browser-use')) {
    return {
      setupBrowserRuntime: setupBrowserRuntimeWrapper,
      default: { setupBrowserRuntime: setupBrowserRuntimeWrapper },
    };
  }
  throw new Error(`Dynamic import of '${s}' is not permitted in CodeMode sandbox`);
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

function ensureSandboxInitialized() {
  if (vmContext && sandbox) return;
  sandbox = Object.create(null);
  sandbox.globalThis = sandbox;
  sandbox.ALL_TOOLS = Object.freeze([]);
  sandbox.tools = Object.freeze(Object.create(null));
  sandbox.text = (...values) => {
    if (activeExecState) activeExecState.addText(...values);
  };
  sandbox.image = (value) => {
    if (activeExecState) activeExecState.addImage(value);
  };
  sandbox.emitImage = sandbox.image;
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
    log: (...args) => sandbox.text(...args),
    info: (...args) => sandbox.text(...args),
    warn: (...args) => sandbox.text(...args),
    error: (...args) => sandbox.text(...args),
  });
  sandbox.setupBrowserRuntime = setupBrowserRuntimeWrapper;
  sandbox.buildTabDecorator = buildTabDecorator;
  sandbox.__ryn_import = dynamicImportWrapper;

  vmContext = vm.createContext(sandbox, {
    codeGeneration: { strings: false, wasm: false },
  });
}

function updateSandboxTools(enabledTools, execId) {
  function invokeHostTool(toolName, args) {
    const id = 'call_' + (nextCallId++);
    return new Promise((resolve, reject) => {
      pendingCalls.set(id, { resolve, reject });
      sendFrame({
        type: 'tool_call',
        exec_id: execId,
        id,
        name: toolName,
        arguments: args === undefined ? {} : args,
      });
    });
  }

  const toolsObj = Object.create(null);
  const allToolsList = [];
  for (const t of Array.isArray(enabledTools) ? enabledTools : []) {
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
  sandbox.ALL_TOOLS = Object.freeze(allToolsList);
  sandbox.tools = Object.freeze(toolsObj);
}

async function executeCell(msg) {
  ensureSandboxInitialized();
  const execId = msg.exec_id || 'exec_1';
  storedValues = Object.assign(Object.create(null), msg.stored_values || {});
  updateSandboxTools(msg.enabled_tools, execId);

  const contentItems = [];
  const state = {
    execId,
    desktopAccess: Boolean(msg.desktop_access),
    contentItems,
    rejectedCode: null,
    addText(...values) {
      if (values.length === 0) return;
      const text = values.map(formatValue).join(' ');
      contentItems.push({ type: 'input_text', text });
    },
    addImage(value) {
      contentItems.push(normalizeImageItem(value));
    },
  };
  activeExecState = state;

  const rawCode = String(msg.code || '');
  const rewrittenCode = rawCode.replace(/(?<![.$A-Za-z0-9_])import\s*\(/g, '__ryn_import(');
  const declaredNames = extractTopLevelDeclaredNames(rewrittenCode);
  const syncLines = declaredNames
    .map((ident) => `try { globalThis[${JSON.stringify(ident)}] = ${ident}; } catch (_) {}`)
    .join('\n');

  const wrappedCode = `(async () => {\nlet __ryn_sync = () => {};\ntry {\n__ryn_sync = () => {\n${syncLines}\n};\n${rewrittenCode}\n} finally {\n__ryn_sync();\n}\n})()`;
  let returnValue;
  let ok = true;
  let errorText = null;
  try {
    const script = new vm.Script(wrappedCode, { filename: 'code_mode_cell.js' });
    returnValue = await script.runInContext(vmContext);
  } catch (err) {
    if (err !== EarlyExitSignal) {
      ok = false;
      if (err && err.rejectedCode) {
        state.rejectedCode = err.rejectedCode;
      }
      errorText = err && err.stack ? String(err.stack) : String(err);
    }
  } finally {
    activeExecState = null;
  }

  if (ok && returnValue !== undefined && contentItems.length === 0) {
    state.addText(returnValue);
  }

  sendFrame({
    type: 'result',
    exec_id: execId,
    ok,
    error: errorText,
    rejected_code: state.rejectedCode,
    return_value: safeSerializeReturn(returnValue),
    content_items: contentItems,
    stored_values: storedValues,
  });
}

async function cleanupWorker() {
  try {
    if (cdpPipeServerInstance) {
      const s = cdpPipeServerInstance;
      cdpPipeServerInstance = null;
      await s.close();
    }
  } catch (_) {}
  try {
    if (fallbackChromeProc) {
      const c = fallbackChromeProc;
      fallbackChromeProc = null;
      await c.close();
    }
  } catch (_) {}
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });

rl.on('line', (line) => {
  if (!line || !line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch (_) {
    return;
  }
  if (msg.type === 'init_worker') {
    workerConfig = {
      task_id: msg.task_id || 'anon',
      chrome_runtime_dir: msg.chrome_runtime_dir || '',
      browser_executable: msg.browser_executable || '',
      headless_browser: Boolean(msg.headless_browser),
      force_cdp_fallback: Boolean(msg.force_cdp_fallback),
      cdp_port: msg.cdp_port || null,
      extension_pipe: msg.extension_pipe || '',
    };
    ensureSandboxInitialized();
    return;
  }
  if (msg.type === 'exec' || msg.type === 'init') {
    const singleShot = msg.type === 'init';
    executeCell(msg)
      .catch((err) => {
        sendFrame({
          type: 'result',
          exec_id: msg.exec_id || 'exec_1',
          ok: false,
          error: err && err.stack ? String(err.stack) : String(err),
          content_items: [],
          stored_values: storedValues,
        });
      })
      .finally(async () => {
        if (singleShot) {
          await cleanupWorker();
          process.exit(0);
        }
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

rl.on('close', () => {
  cleanupWorker().finally(() => process.exit(0));
});
"""

_NODE_HOST_SCRIPT = "\n".join(
    line.strip()
    for line in _NODE_HOST_SCRIPT.splitlines()
    if line.strip() and not line.strip().startswith("//")
)


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


class _CodeModeWorkerSession:
    """Persistent Node worker process for a task's `ryn_code_exec` cells."""

    def __init__(
        self,
        *,
        task_id: str,
        epoch: int,
        scope_revision: int,
        headless_browser: bool,
    ) -> None:
        self.task_id = task_id
        self.epoch = epoch
        self.scope_revision = scope_revision
        self.write_lock = threading.Lock()
        self.exec_lock = threading.Lock()
        self.frames_q: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.closed = False

        node_exe = _resolve_node_binary()
        env = sanitized_worker_env(session_id=f"codemode_{task_id[:16] or 'anon'}", generation=epoch + 1)
        for os_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA", "APPDATA", "PATH", "SYSTEMROOT"):
            val = os.environ.get(os_key)
            if isinstance(val, str) and val:
                env[os_key] = val

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
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

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name=f"ryn-code-mode-reader-{task_id[:8] or 'anon'}",
        )
        self.reader_thread.start()

        raw_cdp_port = os.environ.get("RYN_CHROME_CDP_PORT", "").strip()
        cdp_port = int(raw_cdp_port) if raw_cdp_port.isdigit() else None
        headless_flag = (
            os.environ.get("RYN_CHROME_HEADLESS") != "0"
            if "RYN_CHROME_HEADLESS" in os.environ
            else headless_browser
        )
        self.send({
            "type": "init_worker",
            "task_id": task_id or "anon",
            "chrome_runtime_dir": str(CHROME_RUNTIME_DIR),
            "browser_executable": os.environ.get("RYN_CHROME_EXECUTABLE") or find_browser() or "",
            "headless_browser": bool(headless_flag),
            "force_cdp_fallback": os.environ.get("RYN_CHROME_FORCE_CDP_FALLBACK") == "1",
            "cdp_port": cdp_port,
            "extension_pipe": os.environ.get("RYN_CHROME_EXTENSION_PIPE") or "",
        })

    def is_alive(self) -> bool:
        return not self.closed and self.proc.poll() is None

    def send(self, payload: dict[str, Any]) -> None:
        with self.write_lock:
            if not self.closed and self.proc.stdin and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    self.proc.stdin.flush()
                except OSError:
                    pass

    def _reader_loop(self) -> None:
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    parsed = json.loads(line_s)
                    if isinstance(parsed, dict):
                        self.frames_q.put(parsed)
                except ValueError:
                    continue
        finally:
            self.frames_q.put(None)

    def close(self) -> None:
        with self.write_lock:
            if self.closed:
                return
            self.closed = True
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass


class CodeModeRunner:
    """Executes `ryn_code_exec` cells and maintains bounded per-task `store`/`load` and VM session state."""

    def __init__(self, parent_tools: Any) -> None:
        self.parent_tools = parent_tools
        self._lock = threading.Lock()
        # Keyed by task_id -> {"context_epoch": int, "scope_revision": int, "values": dict}
        self._task_stores: dict[str, dict[str, Any]] = {}
        # Keyed by task_id -> _CodeModeWorkerSession
        self._task_workers: OrderedDict[str, _CodeModeWorkerSession] = OrderedDict()
        weak_self = weakref.ref(self)
        atexit.register(lambda: (r := weak_self()) is not None and r.close())

    def close(self) -> None:
        with self._lock:
            workers = list(self._task_workers.values())
            self._task_workers.clear()
        for w in workers:
            w.close()

    def forget_task(self, task_id: str) -> None:
        key = str(task_id or "")
        worker: _CodeModeWorkerSession | None = None
        with self._lock:
            self._task_stores.pop(key, None)
            worker = self._task_workers.pop(key, None)
        if worker is not None:
            worker.close()

    def _acquire_worker(
        self,
        task_id: str,
        epoch: int,
        scope_revision: int,
        *,
        reset: bool,
        headless_browser: bool,
    ) -> tuple[_CodeModeWorkerSession, bool]:
        if not task_id:
            return (
                _CodeModeWorkerSession(
                    task_id="",
                    epoch=epoch,
                    scope_revision=scope_revision,
                    headless_browser=headless_browser,
                ),
                True,
            )
        stale_to_close: list[_CodeModeWorkerSession] = []
        with self._lock:
            existing = self._task_workers.get(task_id)
            if (
                existing is not None
                and not reset
                and existing.is_alive()
                and existing.epoch == epoch
                and existing.scope_revision == scope_revision
            ):
                self._task_workers.move_to_end(task_id)
                return existing, False
            if existing is not None:
                self._task_workers.pop(task_id, None)
                stale_to_close.append(existing)
            while len(self._task_workers) >= MAX_ACTIVE_TASK_WORKERS:
                _, oldest = self._task_workers.popitem(last=False)
                stale_to_close.append(oldest)
            created = _CodeModeWorkerSession(
                task_id=task_id,
                epoch=epoch,
                scope_revision=scope_revision,
                headless_browser=headless_browser,
            )
            self._task_workers[task_id] = created
        for w in stale_to_close:
            w.close()
        return created, False

    def _discard_worker(self, task_id: str, worker: _CodeModeWorkerSession) -> None:
        with self._lock:
            if task_id and self._task_workers.get(task_id) is worker:
                self._task_workers.pop(task_id, None)
        worker.close()

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

        specs = (self.parent_tools.specs(context=context)
                 if getattr(self.parent_tools, "supports_model_context", False) is True else self.parent_tools.specs())
        for spec in specs:
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
        desktop_enabled = bool(((ctx.get("local_execution") or {}) if isinstance(ctx.get("local_execution"), dict) else {}).get("desktop_access"))

        stored_values = self._get_stored_values(task_id, epoch, scope_revision, reset=reset)
        specs_by_name, meta_by_name, enabled_tools = self._read_only_catalog(ctx)
        sanitized_to_raw = {item["sanitized_name"]: item["name"] for item in enabled_tools}

        local_tools = getattr(self.parent_tools, "local", None)
        browser_obj = getattr(local_tools, "_browser", None) or getattr(local_tools, "browser", None)
        headless_browser = bool(getattr(browser_obj, "headless", False))
        worker, ephemeral = self._acquire_worker(
            task_id,
            epoch,
            scope_revision,
            reset=reset,
            headless_browser=headless_browser,
        )

        child_calls_lock = threading.Lock()
        child_calls_count = 0
        child_call_records: list[dict[str, Any]] = []
        collected_image_artifacts: list[dict[str, Any]] = []
        exec_id = f"exec_{uuid.uuid4().hex[:12]}"

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
                    worker.send({
                        "type": "tool_result",
                        "id": call_id,
                        "ok": False,
                        "error": "agent_code_exec_call_limit_exceeded",
                    })
                    return

            if callable(cancelled) and cancelled():
                worker.send({"type": "tool_result", "id": call_id, "ok": False, "error": "agent_cancelled"})
                return

            if not raw_name or raw_name not in specs_by_name:
                try:
                    other_meta = self.parent_tools.metadata(requested_name)
                    err_code = (
                        "agent_code_exec_write_forbidden"
                        if other_meta.get("effect") == "write"
                        else "agent_unknown_tool"
                    )
                except Exception:
                    err_code = "agent_unknown_tool"
                worker.send({"type": "tool_result", "id": call_id, "ok": False, "error": err_code})
                return

            if not isinstance(raw_args, dict):
                worker.send({
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
                worker.send({
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
                worker.send({
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
                worker.send({
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
                worker.send({
                    "type": "tool_result",
                    "id": call_id,
                    "ok": False,
                    "error": str(exc),
                })

        deadline = time.monotonic() + (yield_time_ms / 1000.0)
        final_frame: dict[str, Any] | None = None
        discard_worker = ephemeral

        with worker.exec_lock:
            try:
                worker.send({
                    "type": "exec",
                    "exec_id": exec_id,
                    "code": code,
                    "enabled_tools": enabled_tools,
                    "stored_values": stored_values,
                    "desktop_access": desktop_enabled,
                })

                with ThreadPoolExecutor(max_workers=8, thread_name_prefix="ryn-code-mode-tool") as pool:
                    while True:
                        if callable(cancelled) and cancelled():
                            discard_worker = True
                            raise AgentToolRejected("agent_cancelled")
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            discard_worker = True
                            raise AgentToolRejected(
                                "agent_code_exec_timeout",
                                correction=f"CodeMode script exceeded yield_time_ms ({yield_time_ms} ms). Increase yield_time_ms or simplify the script.",
                            )
                        try:
                            frame = worker.frames_q.get(timeout=min(0.1, remaining))
                        except queue.Empty:
                            if not worker.is_alive() and worker.frames_q.empty():
                                discard_worker = True
                                break
                            continue
                        if frame is None:
                            discard_worker = True
                            break
                        if frame.get("exec_id") and frame.get("exec_id") != exec_id:
                            continue
                        f_type = frame.get("type")
                        if f_type == "tool_call":
                            pool.submit(handle_child_tool_call, frame)
                        elif f_type == "result":
                            final_frame = frame
                            break
            finally:
                if discard_worker:
                    self._discard_worker(task_id, worker)

        if final_frame is None:
            raise AgentToolRejected(
                "agent_code_exec_failed",
                correction="CodeMode worker terminated before producing a result.",
            )

        if isinstance(final_frame.get("stored_values"), dict):
            self._save_stored_values(task_id, epoch, scope_revision, final_frame["stored_values"])

        if final_frame.get("rejected_code") == "agent_desktop_access_required":
            raise AgentToolRejected(
                "agent_desktop_access_required",
                correction="Browser control in ryn_code_exec requires local_execution.desktop_access=true.",
            )

        raw_items = final_frame.get("content_items") if isinstance(final_frame.get("content_items"), list) else []
        text_parts: list[str] = []
        content_items: list[dict[str, Any]] = []

        from .image_capabilities import (
            ALLOWED_IMAGE_MIMES,
            safe_segment,
            verify_original_image_bytes,
        )
        safe_task = safe_segment(task_id or "task", "task")
        safe_op = safe_segment(operation_id or "op", "op")
        img_idx = 0

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "input_text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
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
