/**
 * Trusted Node worker hosting an isolated QuickJS-WASM guest per Ryn task session.
 *
 * Security & Architecture invariants:
 * - User/Skill JavaScript executes SOLELY inside the QuickJS WebAssembly guest.
 * - Node eval, Function, vm, and dynamic host import are NEVER invoked on guest input.
 * - Module loader inside QuickJS admits ONLY exact "@oai/sky" -> SKY_SHIM_MODULE_SOURCE.
 * - Guest has no process, require, fs, fetch, network, timers, or host realm objects.
 * - Host callbacks return QuickJSDeferredPromise handles and suspend without blocking
 *   the Node event loop or re-evaluating guest code upon approval/resume.
 */

import crypto from "node:crypto";
import { newQuickJSWASMModule, RELEASE_SYNC } from "quickjs-emscripten";
import { SKY_SHIM_MODULE_SOURCE } from "./sky-shim.mjs";

const MAX_FRAME_BYTES = 2 * 1024 * 1024;
const DEFAULT_MEMORY_LIMIT_BYTES = 32 * 1024 * 1024;
const DEFAULT_MAX_STACK_BYTES = 512 * 1024;
const DEFAULT_CPU_TIMEOUT_MS = 2000;
const DEFAULT_MAX_PENDING_JOBS = 10000;
const DEFAULT_JOB_SLICE_SIZE = 256;
const DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024;
const DEFAULT_MAX_RESULT_BYTES = 512 * 1024;
const DEFAULT_MAX_HOST_CALLS = 64;
const DEFAULT_MAX_ARG_BYTES = 256 * 1024;
const DEFAULT_MAX_IMAGES = 16;
const DEFAULT_MAX_AGGREGATE_EVENT_BYTES = 1024 * 1024;

const MAX_TRUSTED_HOST_IMAGES = 16;
const MAX_TRUSTED_HOST_IMAGE_BYTES = 256 * 1024;
const MAX_TRUSTED_HOST_IMAGE_DESCRIPTOR_BYTES = 4096;
const MAX_TRACKED_EXECUTION_IDS = 512;

const SAFE_ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;

const ALLOWED_LIMIT_KEYS = new Set([
  "memory_limit_bytes",
  "max_stack_bytes",
  "cpu_timeout_ms",
  "max_pending_jobs",
  "max_output_bytes",
  "max_result_bytes",
  "max_host_calls",
  "max_images",
  "max_aggregate_event_bytes",
]);

const ALLOWED_SKY_METHODS = new Set([
  "list_apps",
  "launch_app",
  "list_windows",
  "get_window",
  "activate_window",
  "get_window_state",
  "click",
  "click_element",
  "type_text",
  "set_value",
  "press_key",
  "scroll",
  "drag",
  "perform_secondary_action",
]);

const STATEMENT_START_RE =
  /^(?:const|let|var|if|for|while|do|switch|try|catch|finally|throw|return|break|continue|function|class|import|export)\b/;

// Capture real stdout write before locking down ambient console/stdout methods.
const trustedStdoutWrite = process.stdout.write.bind(process.stdout);
const trustedStderrWrite = process.stderr.write.bind(process.stderr);

function sendFrame(frame) {
  const serialized = JSON.stringify(frame);
  const byteLength = Buffer.byteLength(serialized, "utf8") + 1;
  if (byteLength <= MAX_FRAME_BYTES) {
    trustedStdoutWrite(serialized + "\n");
    return byteLength;
  }
  const fallbackFrame = {
    v: 1,
    type: frame && frame.type === "execution_result" ? "execution_error" : "protocol_error",
    session_id: String((frame && frame.session_id) || ""),
    generation: Number((frame && frame.generation) || 1),
    ...(frame && typeof frame.execution_id === "string" ? { execution_id: frame.execution_id } : {}),
    error: {
      name: "FrameSizeError",
      code: "ryn_js_frame_too_large",
      message: "Outbound worker frame exceeded MAX_FRAME_BYTES limit",
    },
    ...(frame && frame.type === "execution_result"
      ? {
          stdout: "",
          images: [],
          cpu_elapsed_ms: Number(frame.cpu_elapsed_ms || 0),
        }
      : {}),
  };
  const fallbackSerialized = JSON.stringify(fallbackFrame);
  trustedStdoutWrite(fallbackSerialized + "\n");
  return Buffer.byteLength(fallbackSerialized, "utf8") + 1;
}

// Prevent accidental unframed writes to stdout inside the worker process.
process.stdout.write = function blockedStdoutWrite(chunk) {
  trustedStderrWrite(typeof chunk === "string" ? chunk : Buffer.from(chunk));
  return true;
};
console.log = (...args) => {
  trustedStderrWrite(args.map((a) => String(a)).join(" ") + "\n");
};

function sha256Hex(input) {
  return crypto.createHash("sha256").update(input).digest("hex");
}

function toTerminalImageSummary(descriptor) {
  const summary = {
    id: descriptor.id,
    provenance: descriptor.provenance,
    sha256: descriptor.sha256,
  };
  if (descriptor.observation_id !== undefined) {
    summary.observation_id = descriptor.observation_id;
  }
  if (descriptor.mime_type !== undefined) {
    summary.mime_type = descriptor.mime_type;
  } else if (descriptor.mimeType !== undefined) {
    summary.mime_type = descriptor.mimeType;
  }
  if (descriptor.byte_length !== undefined) {
    summary.byte_length = descriptor.byte_length;
  } else if (descriptor.byteLength !== undefined) {
    summary.byte_length = descriptor.byteLength;
  }
  if (descriptor.base64_length !== undefined) {
    summary.base64_length = descriptor.base64_length;
  }
  if (descriptor.zIndex !== undefined) {
    summary.zIndex = descriptor.zIndex;
  }
  if (descriptor.width !== undefined) {
    summary.width = descriptor.width;
  }
  if (descriptor.height !== undefined) {
    summary.height = descriptor.height;
  }
  if (descriptor.originX !== undefined) {
    summary.originX = descriptor.originX;
  }
  if (descriptor.originY !== undefined) {
    summary.originY = descriptor.originY;
  }
  if (descriptor.stateToken !== undefined) {
    summary.stateToken = descriptor.stateToken;
  }
  if (descriptor.window !== undefined) {
    summary.window = descriptor.window;
  }
  return summary;
}

const GUEST_BOOTSTRAP_SOURCE = `
(() => {
  const rawHostCall = globalThis.__ryn_raw_host_call;
  const rawWrite = globalThis.__ryn_raw_write;
  const rawEmitImage = globalThis.__ryn_raw_emit_image;
  delete globalThis.__ryn_raw_host_call;
  delete globalThis.__ryn_raw_write;
  delete globalThis.__ryn_raw_emit_image;

  function safeJsonStringify(value, label) {
    let text;
    try {
      text = JSON.stringify(value ?? {});
    } catch (err) {
      throw new TypeError(\`\${label} must be JSON-serializable: \${err && err.message ? err.message : String(err)}\`);
    }
    if (typeof text !== "string") {
      throw new TypeError(\`\${label} must be JSON-serializable\`);
    }
    return text;
  }

  function formatValue(val) {
    if (typeof val === "string") return val;
    if (val === undefined) return "undefined";
    if (val === null) return "null";
    if (typeof val === "number" || typeof val === "boolean" || typeof val === "bigint") {
      return String(val);
    }
    try {
      return JSON.stringify(val);
    } catch {
      return Object.prototype.toString.call(val);
    }
  }

  function write(data) {
    const text = typeof data === "string" ? data : formatValue(data);
    rawWrite(text);
    return { ok: true, bytes: text.length };
  }

  async function emitImage(input) {
    const payloadJson = safeJsonStringify({ source: "guest_emit_image", input }, "emitImage input");
    const resJson = rawEmitImage(payloadJson);
    return JSON.parse(resJson);
  }

  async function emitWindowStateImage(screenshot, windowObj) {
    const payloadJson = safeJsonStringify(
      {
        source: "window_state_screenshot",
        screenshot,
        ...(windowObj !== undefined ? { window: windowObj } : {}),
      },
      "window_state screenshot"
    );
    const resJson = rawEmitImage(payloadJson);
    return JSON.parse(resJson);
  }

  async function hostCall(method, params) {
    if (typeof method !== "string" || !method) {
      throw new TypeError("hostCall method must be a non-empty string");
    }
    const argsJson = safeJsonStringify(params ?? {}, "hostCall arguments");
    return await rawHostCall(method, argsJson);
  }

  const internalBridge = Object.freeze({
    hostCall,
    emitWindowStateImage,
    write,
    emitImage,
  });

  Object.defineProperty(globalThis, "__ryn_internal_bridge", {
    value: internalBridge,
    writable: false,
    configurable: false,
    enumerable: false,
  });

  const nodeReplFacade = Object.freeze({
    provider: "ryn",
    runtime: "quickjs-wasm",
    write,
    emitImage,
  });

  Object.defineProperty(globalThis, "nodeRepl", {
    value: nodeReplFacade,
    writable: false,
    configurable: false,
    enumerable: true,
  });

  const consoleFacade = Object.freeze({
    log: (...args) => { write(args.map(formatValue).join(" ") + "\\n"); },
    info: (...args) => { write(args.map(formatValue).join(" ") + "\\n"); },
    warn: (...args) => { write(args.map(formatValue).join(" ") + "\\n"); },
    error: (...args) => { write(args.map(formatValue).join(" ") + "\\n"); },
  });

  Object.defineProperty(globalThis, "console", {
    value: consoleFacade,
    writable: false,
    configurable: false,
    enumerable: true,
  });
})();
`;

class QuickJsTaskWorker {
  constructor(quickjsModule, sessionId, initialGeneration = 1) {
    this.quickjs = quickjsModule;
    this.sessionId = sessionId;
    this.generation = initialGeneration;
    this.turnNumber = 1;
    this.cellSequence = 0;
    this.observationSequence = 0;
    this.closed = false;

    this.runtime = null;
    this.context = null;
    this.toPromiseFnHandle = null;
    this.jsonParseFnHandle = null;
    this.activeEvaluation = null;
    this.trustedHostImages = new Map();
    this.trustedHostImageBytes = 0;
    this.seenExecutionIds = new Set();

    this._initGuestRealm();
  }

  _initGuestRealm() {
    this._disposeGuestRealm();
    const runtime = this.quickjs.newRuntime();
    runtime.setMemoryLimit(DEFAULT_MEMORY_LIMIT_BYTES);
    runtime.setMaxStackSize(DEFAULT_MAX_STACK_BYTES);

    runtime.setModuleLoader(
      (moduleName) => {
        if (moduleName === "@oai/sky") {
          return { value: SKY_SHIM_MODULE_SOURCE };
        }
        return {
          error: new Error(
            `Module import rejected by Ryn QuickJS guest policy: ${String(moduleName)} (only exact "@oai/sky" is allowed)`
          ),
        };
      },
      (_baseModuleName, requestedName) => String(requestedName)
    );

    runtime.setInterruptHandler(() => {
      const active = this.activeEvaluation;
      if (!active) {
        return false;
      }
      if (active.cancelled) {
        active.interruptedReason = "ryn_js_cancelled";
        return true;
      }
      const elapsedSlice =
        active.cpuSliceStartMs !== null ? Math.max(0, Date.now() - active.cpuSliceStartMs) : 0;
      if (active.cpuElapsedMs + elapsedSlice > active.cpuTimeoutMs) {
        active.interruptedReason = "ryn_js_execution_timeout";
        return true;
      }
      return false;
    });

    const context = runtime.newContext();
    this.runtime = runtime;
    this.context = context;

    this._installGuestBindings(context);
  }

  _installGuestBindings(context) {
    const rawHostCallHandle = context.newFunction("__ryn_raw_host_call", (methodHandle, argsJsonHandle) => {
      const active = this.activeEvaluation;
      if (!active || active.settled || active.cancelled) {
        throw new Error("ryn_js_cancelled: Cannot issue host_call from an inactive or cancelled evaluation");
      }
      const method = context.getString(methodHandle);
      const argsJson = context.getString(argsJsonHandle);
      if (!ALLOWED_SKY_METHODS.has(method)) {
        throw new Error(`ryn_sky_method_unsupported: Unsupported host method ${String(method)}`);
      }
      if (Buffer.byteLength(argsJson, "utf8") > active.maxArgBytes) {
        throw new Error("ryn_js_argument_limit_exceeded: Host call arguments exceed byte limit");
      }
      if (active.callbackOrdinal >= active.maxHostCalls) {
        throw new Error("ryn_js_host_call_limit_exceeded: Maximum host calls per evaluation exceeded");
      }
      let parsedArgs;
      try {
        parsedArgs = JSON.parse(argsJson);
      } catch {
        throw new Error("ryn_js_invalid_arguments: Host call arguments must be valid JSON");
      }
      if (typeof parsedArgs !== "object" || parsedArgs === null || Array.isArray(parsedArgs)) {
        throw new Error("ryn_js_invalid_arguments: Host call arguments must be a JSON object");
      }

      active.callbackOrdinal += 1;
      const ordinal = active.callbackOrdinal;
      const callbackId = `cb_${this.generation}_${active.executionId}_${ordinal}`;
      const deferred = context.newPromise();

      active.pendingCallbacks.set(callbackId, {
        callbackId,
        ordinal,
        method,
        arguments: parsedArgs,
        deferred,
        settled: false,
      });

      active.unsentHostCalls.push({
        v: 1,
        type: "host_call",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: active.executionId,
        callback_id: callbackId,
        ordinal,
        method,
        arguments: parsedArgs,
      });

      return deferred.handle;
    });

    const rawWriteHandle = context.newFunction("__ryn_raw_write", (textHandle) => {
      const active = this.activeEvaluation;
      if (!active || active.settled || active.cancelled) {
        throw new Error("ryn_js_cancelled: Cannot write output from an inactive evaluation");
      }
      const text = context.getString(textHandle);
      const byteLen = Buffer.byteLength(text, "utf8");
      if (active.outputBytes + byteLen > active.maxOutputBytes) {
        throw new Error("ryn_js_output_limit_exceeded: Guest output exceeded maximum byte limit");
      }
      const outFrame = {
        v: 1,
        type: "output",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: active.executionId,
        stream: "stdout",
        text,
      };
      const approxFrameBytes = Buffer.byteLength(JSON.stringify(outFrame), "utf8") + 1;
      if (active.aggregateEventBytes + approxFrameBytes > active.maxAggregateEventBytes) {
        throw new Error(
          "ryn_js_aggregate_output_limit_exceeded: Cumulative evaluation event payload budget exceeded"
        );
      }
      active.outputBytes += byteLen;
      active.aggregateEventBytes += approxFrameBytes;
      active.outputs.push(text);
      sendFrame(outFrame);
    });

    const rawEmitImageHandle = context.newFunction("__ryn_raw_emit_image", (payloadJsonHandle) => {
      const active = this.activeEvaluation;
      if (!active || active.settled || active.cancelled) {
        throw new Error("ryn_js_cancelled: Cannot emit image from an inactive evaluation");
      }
      if (active.images.length >= active.maxImages) {
        throw new Error("ryn_js_image_limit_exceeded: Maximum image emissions per evaluation exceeded");
      }
      const rawJson = context.getString(payloadJsonHandle);
      if (Buffer.byteLength(rawJson, "utf8") > active.maxArgBytes) {
        throw new Error("ryn_js_image_invalid: Image descriptor exceeds maximum size");
      }
      const parsed = JSON.parse(rawJson);
      const descriptor = this._validateAndCorrelateImageRequest(parsed, active);
      const imgFrame = {
        v: 1,
        type: "image_request",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: active.executionId,
        image: descriptor,
      };
      const approxFrameBytes = Buffer.byteLength(JSON.stringify(imgFrame), "utf8") + 1;
      if (active.aggregateEventBytes + approxFrameBytes > active.maxAggregateEventBytes) {
        throw new Error(
          "ryn_js_aggregate_output_limit_exceeded: Cumulative evaluation event payload budget exceeded"
        );
      }
      active.aggregateEventBytes += approxFrameBytes;
      const summary = toTerminalImageSummary(descriptor);
      active.images.push(summary);
      sendFrame(imgFrame);
      return context.newString(JSON.stringify(summary));
    });

    context.setProp(context.global, "__ryn_raw_host_call", rawHostCallHandle);
    context.setProp(context.global, "__ryn_raw_write", rawWriteHandle);
    context.setProp(context.global, "__ryn_raw_emit_image", rawEmitImageHandle);
    rawHostCallHandle.dispose();
    rawWriteHandle.dispose();
    rawEmitImageHandle.dispose();

    const bootResult = context.evalCode(GUEST_BOOTSTRAP_SOURCE, "__ryn_bootstrap.js");
    if (bootResult.error) {
      const errObj = this._extractGuestError(bootResult.error);
      bootResult.error.dispose();
      throw new Error(`Failed to bootstrap QuickJS guest: ${errObj.message}`);
    }
    bootResult.value.dispose();

    const toPromiseRes = context.evalCode("(v) => Promise.resolve(v)", "__ryn_to_promise.js");
    if (toPromiseRes.error) {
      toPromiseRes.error.dispose();
      throw new Error("Failed to initialize QuickJS Promise.resolve helper");
    }
    this.toPromiseFnHandle = toPromiseRes.value;

    const jsonParseRes = context.evalCode("(s) => JSON.parse(s)", "__ryn_json_parse.js");
    if (jsonParseRes.error) {
      jsonParseRes.error.dispose();
      throw new Error("Failed to initialize QuickJS JSON.parse helper");
    }
    this.jsonParseFnHandle = jsonParseRes.value;
  }

  _validateWindowMatch(candidateWindow, trustedWindow) {
    if (candidateWindow === undefined || candidateWindow === null) {
      return;
    }
    if (
      typeof candidateWindow !== "object" ||
      Array.isArray(candidateWindow) ||
      typeof candidateWindow.app !== "string" ||
      candidateWindow.app !== trustedWindow.app ||
      typeof candidateWindow.id !== "number" ||
      !Number.isInteger(candidateWindow.id) ||
      candidateWindow.id !== trustedWindow.id
    ) {
      throw new Error(
        "ryn_js_image_window_mismatch: Guest cannot override or forge host screenshot window identity"
      );
    }
  }

  _validateHostDescriptorFieldsMatch(candidate, trusted) {
    if (candidate.sha256 !== undefined && String(candidate.sha256).toLowerCase() !== trusted.sha256) {
      throw new Error("ryn_js_image_untrusted: Screenshot sha256 does not match host record");
    }
    if (
      candidate.stateToken !== undefined &&
      trusted.stateToken !== undefined &&
      candidate.stateToken !== trusted.stateToken
    ) {
      throw new Error("ryn_js_image_untrusted: Screenshot stateToken does not match host record");
    }
    if (candidate.stateToken !== undefined && trusted.stateToken === undefined) {
      throw new Error("ryn_js_image_untrusted: Guest cannot forge screenshot stateToken");
    }
    const numericFields = ["width", "height", "originX", "originY", "zIndex", "byteLength"];
    for (const fieldName of numericFields) {
      if (candidate[fieldName] !== undefined && trusted[fieldName] !== undefined) {
        if (candidate[fieldName] !== trusted[fieldName]) {
          throw new Error(`ryn_js_image_untrusted: Screenshot ${fieldName} does not match host record`);
        }
      }
    }
    if (
      candidate.mimeType !== undefined &&
      trusted.mimeType !== undefined &&
      candidate.mimeType !== trusted.mimeType
    ) {
      throw new Error("ryn_js_image_untrusted: Screenshot mimeType does not match host record");
    }
  }

  _buildCorrelatedHostDescriptor(provenance, trusted) {
    return {
      provenance,
      observation_id: trusted.observationId,
      id: trusted.id,
      url: trusted.url,
      zIndex: trusted.zIndex ?? 0,
      ...(trusted.width !== undefined ? { width: trusted.width } : {}),
      ...(trusted.height !== undefined ? { height: trusted.height } : {}),
      ...(trusted.originX !== undefined ? { originX: trusted.originX } : {}),
      ...(trusted.originY !== undefined ? { originY: trusted.originY } : {}),
      ...(trusted.mimeType !== undefined ? { mimeType: trusted.mimeType, mime_type: trusted.mimeType } : {}),
      ...(trusted.byteLength !== undefined ? { byteLength: trusted.byteLength, byte_length: trusted.byteLength } : {}),
      sha256: trusted.sha256,
      ...(trusted.stateToken !== undefined ? { stateToken: trusted.stateToken } : {}),
      window: { ...trusted.window },
    };
  }

  _validateAndCorrelateImageRequest(parsed, active) {
    if (typeof parsed !== "object" || parsed === null) {
      throw new Error("ryn_js_image_invalid: Image payload must be an object");
    }
    const source = parsed.source;
    if (source === "window_state_screenshot") {
      const shot = parsed.screenshot;
      if (typeof shot !== "object" || shot === null || typeof shot.url !== "string" || !shot.url) {
        throw new Error("ryn_js_image_invalid: Invalid window_state screenshot descriptor");
      }
      const shotId = typeof shot.id === "string" && shot.id.trim() ? shot.id.trim() : "screenshot-0";
      const key = `${shotId}::${shot.url}`;
      const trusted = this.trustedHostImages.get(key);
      if (
        !trusted ||
        trusted.sessionId !== this.sessionId ||
        trusted.generation !== this.generation ||
        trusted.turnNumber !== this.turnNumber
      ) {
        throw new Error(
          "ryn_js_image_untrusted: Screenshot descriptor was not issued by host get_window_state in this turn"
        );
      }
      this._validateHostDescriptorFieldsMatch(shot, trusted);
      this._validateWindowMatch(parsed.window, trusted.window);
      if (shot.window !== undefined) {
        this._validateWindowMatch(shot.window, trusted.window);
      }
      return this._buildCorrelatedHostDescriptor("host_window_state", trusted);
    }

    const input = parsed.input;
    if (typeof input === "object" && input !== null && !Array.isArray(input)) {
      const urlCandidate =
        typeof input.image_url === "string"
          ? input.image_url.trim()
          : typeof input.url === "string"
            ? input.url.trim()
            : "";
      if (!urlCandidate) {
        throw new Error("ryn_js_image_invalid: emitImage object requires a non-empty url or image_url");
      }
      // Reject any external network URL, file:// URL, or filesystem path before correlation.
      if (
        /^(?:https?|ftp|ws|wss|file|blob|filesystem|chrome|edge|devtools):/i.test(urlCandidate) ||
        urlCandidate.startsWith("//") ||
        urlCandidate.startsWith("/") ||
        urlCandidate.startsWith("\\") ||
        /^[a-zA-Z]:[\\/]/.test(urlCandidate) ||
        urlCandidate.includes("..")
      ) {
        throw new Error("ryn_js_image_forbidden_url: External URLs and filesystem paths are forbidden in emitImage");
      }
      const idCandidate = typeof input.id === "string" && input.id.trim() ? input.id.trim() : null;
      const claimsHostFields =
        input.window !== undefined ||
        input.stateToken !== undefined ||
        input.observation_id !== undefined ||
        input.artifact_id !== undefined ||
        (typeof input.provenance === "string" && input.provenance !== "guest_inline_data_url");

      if (idCandidate !== null || !urlCandidate.startsWith("data:") || claimsHostFields) {
        if (!idCandidate) {
          throw new Error("ryn_js_image_untrusted: Host screenshot descriptor requires exact id");
        }
        const lookupKey = `${idCandidate}::${urlCandidate}`;
        const trusted = this.trustedHostImages.get(lookupKey);
        if (
          !trusted ||
          trusted.sessionId !== this.sessionId ||
          trusted.generation !== this.generation ||
          trusted.turnNumber !== this.turnNumber
        ) {
          throw new Error("ryn_js_image_untrusted: Stale or untrusted host screenshot descriptor");
        }
        this._validateHostDescriptorFieldsMatch(input, trusted);
        this._validateWindowMatch(input.window, trusted.window);
        return this._buildCorrelatedHostDescriptor("host_issued_descriptor", trusted);
      }
    }

    const urlCandidate =
      typeof input === "string"
        ? input.trim()
        : typeof input === "object" && input !== null && typeof (input.image_url ?? input.url) === "string"
          ? String(input.image_url ?? input.url).trim()
          : "";

    if (!urlCandidate) {
      throw new Error("ryn_js_image_invalid: emitImage requires a non-empty descriptor or data:image URL");
    }

    // Reject any external network URL, file:// URL, or filesystem path.
    if (
      /^(?:https?|ftp|ws|wss|file|blob|filesystem|chrome|edge|devtools):/i.test(urlCandidate) ||
      urlCandidate.startsWith("//") ||
      urlCandidate.startsWith("/") ||
      urlCandidate.startsWith("\\") ||
      /^[a-zA-Z]:[\\/]/.test(urlCandidate) ||
      urlCandidate.includes("..")
    ) {
      throw new Error("ryn_js_image_forbidden_url: External URLs and filesystem paths are forbidden in emitImage");
    }

    const dataMatch = /^data:image\/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)$/.exec(urlCandidate);
    if (dataMatch) {
      const b64 = dataMatch[2];
      if (b64.length > 128 * 1024) {
        throw new Error("ryn_js_image_limit_exceeded: Inline data URL image exceeds 128KB guest limit");
      }
      const rawBytes = Buffer.from(b64, "base64");
      const sha256 = sha256Hex(rawBytes);
      const guestImageId = `guest_img_${this.generation}_${active.executionId}_${active.images.length + 1}`;
      return {
        id: guestImageId,
        provenance: "guest_inline_data_url",
        mime_type: `image/${dataMatch[1]}`,
        mimeType: `image/${dataMatch[1]}`,
        byte_length: rawBytes.length,
        base64_length: b64.length,
        base64Length: b64.length,
        sha256,
        url: urlCandidate,
      };
    }

    throw new Error(
      "ryn_js_image_untrusted: emitImage accepts only current host-issued screenshot descriptors or bounded data:image URLs"
    );
  }

  _evictTrustedImageKey(key) {
    const existing = this.trustedHostImages.get(key);
    if (existing) {
      this.trustedHostImages.delete(key);
      this.trustedHostImageBytes = Math.max(0, this.trustedHostImageBytes - existing.byteCost);
    }
  }

  _clearTrustedHostImages() {
    this.trustedHostImages.clear();
    this.trustedHostImageBytes = 0;
  }

  _recordTrustedWindowStateImages(entry, result) {
    if (typeof result !== "object" || result === null || !Array.isArray(result.screenshots)) {
      return;
    }
    const reqWin =
      entry && typeof entry.arguments === "object" && entry.arguments !== null
        ? entry.arguments.window
        : null;
    const resWin = typeof result.window === "object" && result.window !== null ? result.window : null;
    const app =
      reqWin && typeof reqWin.app === "string" && reqWin.app.trim()
        ? reqWin.app.trim()
        : resWin && typeof resWin.app === "string" && resWin.app.trim()
          ? resWin.app.trim()
          : null;
    const winId =
      reqWin && typeof reqWin.id === "number" && Number.isInteger(reqWin.id) && reqWin.id >= 0
        ? reqWin.id
        : resWin && typeof resWin.id === "number" && Number.isInteger(resWin.id) && resWin.id >= 0
          ? resWin.id
          : null;
    if (app === null || winId === null) {
      return;
    }
    if (
      resWin &&
      ((typeof resWin.app === "string" && resWin.app.trim() && resWin.app.trim() !== app) ||
        (typeof resWin.id === "number" && Number.isInteger(resWin.id) && resWin.id !== winId))
    ) {
      return;
    }
    const title =
      resWin && typeof resWin.title === "string"
        ? resWin.title
        : reqWin && typeof reqWin.title === "string"
          ? reqWin.title
          : undefined;
    const canonicalWindow = Object.freeze({
      app,
      id: winId,
      ...(title !== undefined ? { title } : {}),
    });
    const windowKey = `${app}::${winId}`;

    // Evict any prior observation screenshots for the same window so stale snapshots cannot be re-emitted.
    for (const [k, existing] of this.trustedHostImages.entries()) {
      if (existing.windowKey === windowKey) {
        this._evictTrustedImageKey(k);
      }
    }

    this.observationSequence += 1;
    const obsSeq = this.observationSequence;

    for (let idx = 0; idx < result.screenshots.length; idx += 1) {
      const shot = result.screenshots[idx];
      if (typeof shot !== "object" || shot === null || typeof shot.url !== "string" || !shot.url) {
        continue;
      }
      const id = typeof shot.id === "string" && shot.id.trim() ? shot.id.trim() : "screenshot-0";
      const serializedShotBytes = Buffer.byteLength(JSON.stringify(shot), "utf8");
      const byteCost = Math.max(
        serializedShotBytes,
        Buffer.byteLength(shot.url, "utf8") + Buffer.byteLength(id, "utf8") + 128
      );
      if (
        (!shot.url.startsWith("data:") && byteCost > MAX_TRUSTED_HOST_IMAGE_DESCRIPTOR_BYTES) ||
        byteCost > MAX_TRUSTED_HOST_IMAGE_BYTES
      ) {
        continue;
      }
      while (
        this.trustedHostImages.size >= MAX_TRUSTED_HOST_IMAGES ||
        this.trustedHostImageBytes + byteCost > MAX_TRUSTED_HOST_IMAGE_BYTES
      ) {
        const oldestKey = this.trustedHostImages.keys().next().value;
        if (oldestKey === undefined) {
          break;
        }
        this._evictTrustedImageKey(oldestKey);
      }

      const sha256 =
        typeof shot.sha256 === "string" && /^[0-9a-fA-F]{64}$/.test(shot.sha256)
          ? shot.sha256.toLowerCase()
          : sha256Hex(shot.url);
      const mimeType =
        typeof shot.mimeType === "string" && shot.mimeType.trim()
          ? shot.mimeType.trim()
          : typeof shot.mime_type === "string" && shot.mime_type.trim()
            ? shot.mime_type.trim()
            : undefined;
      const byteLength =
        typeof shot.byteLength === "number" && Number.isFinite(shot.byteLength) && shot.byteLength >= 0
          ? shot.byteLength
          : typeof shot.byte_length === "number" && Number.isFinite(shot.byte_length) && shot.byte_length >= 0
            ? shot.byte_length
            : undefined;

      const descriptor = Object.freeze({
        sessionId: this.sessionId,
        generation: this.generation,
        turnNumber: this.turnNumber,
        observationId:
          typeof shot.observation_id === "string" && shot.observation_id.trim()
            ? shot.observation_id.trim()
            : `obs_${this.generation}_${obsSeq}_${idx}`,
        windowKey,
        window: canonicalWindow,
        id,
        url: shot.url,
        zIndex: typeof shot.zIndex === "number" && Number.isFinite(shot.zIndex) ? shot.zIndex : 0,
        ...(typeof shot.width === "number" && Number.isFinite(shot.width) ? { width: shot.width } : {}),
        ...(typeof shot.height === "number" && Number.isFinite(shot.height) ? { height: shot.height } : {}),
        ...(typeof shot.originX === "number" && Number.isFinite(shot.originX) ? { originX: shot.originX } : {}),
        ...(typeof shot.originY === "number" && Number.isFinite(shot.originY) ? { originY: shot.originY } : {}),
        ...(mimeType !== undefined ? { mimeType } : {}),
        ...(byteLength !== undefined ? { byteLength } : {}),
        sha256,
        ...(typeof shot.stateToken === "string" ? { stateToken: shot.stateToken } : {}),
        byteCost,
      });

      const compositeKey = `${id}::${shot.url}`;
      this._evictTrustedImageKey(compositeKey);
      this.trustedHostImages.set(compositeKey, descriptor);
      this.trustedHostImageBytes += byteCost;
    }
  }

  _extractGuestError(errHandle) {
    const context = this.context;
    try {
      const dumped = context.dump(errHandle);
      if (typeof dumped === "object" && dumped !== null) {
        const message = typeof dumped.message === "string" ? dumped.message : String(dumped);
        const name = typeof dumped.name === "string" ? dumped.name : "Error";
        const stack = typeof dumped.stack === "string" ? dumped.stack.slice(0, 4000) : undefined;
        let code = typeof dumped.code === "string" ? dumped.code : undefined;
        if (!code) {
          const prefixMatch = /^(ryn_[a-z0-9_]+):/.exec(message);
          if (prefixMatch) {
            code = prefixMatch[1];
          }
        }
        return {
          name,
          message: message.slice(0, 2000),
          code: code || "ryn_js_guest_exception",
          ...(stack ? { stack } : {}),
        };
      }
      return {
        name: "Error",
        message: String(dumped).slice(0, 2000),
        code: "ryn_js_guest_exception",
      };
    } catch {
      return {
        name: "Error",
        message: "QuickJS guest error",
        code: "ryn_js_guest_exception",
      };
    }
  }

  _serializeGuestValue(valHandle, maxBytes) {
    const context = this.context;
    const type = context.typeof(valHandle);
    if (type === "undefined") {
      return { value: null, valueType: "undefined" };
    }
    if (type === "function" || type === "symbol") {
      return { value: `[${type}]`, valueType: type };
    }
    let dumped;
    try {
      dumped = context.dump(valHandle);
    } catch {
      throw new Error("ryn_js_result_unserializable: Guest return value could not be dumped");
    }
    let serialized;
    try {
      serialized = JSON.stringify(dumped);
    } catch {
      throw new Error("ryn_js_result_unserializable: Guest return value is not JSON-serializable");
    }
    if (typeof serialized === "string" && Buffer.byteLength(serialized, "utf8") > maxBytes) {
      throw new Error("ryn_js_result_limit_exceeded: Guest return value exceeds byte limit");
    }
    return {
      value: serialized === undefined ? null : JSON.parse(serialized),
      valueType: type,
    };
  }

  _splitTrailingExpressionCandidate(code) {
    const trimmed = code.trim();
    if (!trimmed) {
      return null;
    }
    const lines = trimmed.split(/\r?\n/);
    let lastIdx = lines.length - 1;
    while (lastIdx >= 0 && !lines[lastIdx].trim()) {
      lastIdx -= 1;
    }
    if (lastIdx <= 0) {
      return null;
    }
    const lastLine = lines[lastIdx].trim();
    if (
      STATEMENT_START_RE.test(lastLine) ||
      lastLine.endsWith("{") ||
      lastLine.endsWith("}")
    ) {
      return null;
    }
    const exprCandidate = lastLine.replace(/;+\s*$/, "").trim();
    if (!exprCandidate) {
      return null;
    }
    const prefix = lines.slice(0, lastIdx).join("\n");
    return { prefix, exprCandidate };
  }

  _evaluateInGuest(code, filename) {
    const context = this.context;

    // 1. Parse as an async arrow expression returning a closure; body runs only on callFunction.
    const singleExprCompile = context.evalCode(`(async () => (\n${code}\n))`, filename);
    if (!singleExprCompile.error) {
      const fnHandle = singleExprCompile.value;
      const callRes = context.callFunction(fnHandle, context.undefined);
      fnHandle.dispose();
      return callRes;
    }
    singleExprCompile.error.dispose();

    // 2. Parse multi-line script with trailing expression returned inside an async closure.
    const splitCandidate = this._splitTrailingExpressionCandidate(code);
    if (splitCandidate) {
      const trailingExprSource = `(async () => {\n${splitCandidate.prefix}\nreturn (\n${splitCandidate.exprCandidate}\n);\n})`;
      const trailingCompile = context.evalCode(trailingExprSource, filename);
      if (!trailingCompile.error) {
        const fnHandle = trailingCompile.value;
        const callRes = context.callFunction(fnHandle, context.undefined);
        fnHandle.dispose();
        return callRes;
      }
      trailingCompile.error.dispose();
    }

    // 3. Parse as an async statement block closure (supports explicit `return` and top-level `await`).
    const blockCompile = context.evalCode(`(async () => {\n${code}\n})`, filename);
    if (!blockCompile.error) {
      const fnHandle = blockCompile.value;
      const callRes = context.callFunction(fnHandle, context.undefined);
      fnHandle.dispose();
      return callRes;
    }
    blockCompile.error.dispose();

    // 4. Fallback to native ES module evaluation (supports static `import`/`export` + top-level `await`).
    return context.evalCode(code, filename, { type: "module" });
  }

  _validateParentEnvelope(msg, { allowPriorGenerationForClose = false } = {}) {
    if (typeof msg.session_id !== "string" || msg.session_id !== this.sessionId) {
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: this.sessionId,
        generation: this.generation,
        ...(typeof msg.execution_id === "string" ? { execution_id: msg.execution_id } : {}),
        ...(typeof msg.callback_id === "string" ? { callback_id: msg.callback_id } : {}),
        error: {
          code: "ryn_js_session_mismatch",
          message: `Session mismatch: expected ${this.sessionId}, got ${String(msg.session_id)}`,
        },
      });
      return false;
    }

    const msgGen = msg.generation;
    const validGen =
      typeof msgGen === "number" &&
      Number.isInteger(msgGen) &&
      msgGen >= 1 &&
      (msgGen === this.generation || (allowPriorGenerationForClose && msgGen <= this.generation));

    if (!validGen) {
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: this.sessionId,
        generation: this.generation,
        ...(typeof msg.execution_id === "string" ? { execution_id: msg.execution_id } : {}),
        ...(typeof msg.callback_id === "string" ? { callback_id: msg.callback_id } : {}),
        error: {
          code: "ryn_js_stale_generation",
          message: `Generation mismatch: expected ${this.generation}, got ${String(msgGen)}`,
        },
      });
      return false;
    }

    return true;
  }

  _parseAndValidateLimits(rawLimits) {
    if (rawLimits === undefined || rawLimits === null) {
      return {
        memoryLimitBytes: DEFAULT_MEMORY_LIMIT_BYTES,
        maxStackBytes: DEFAULT_MAX_STACK_BYTES,
        cpuTimeoutMs: DEFAULT_CPU_TIMEOUT_MS,
        maxPendingJobs: DEFAULT_MAX_PENDING_JOBS,
        maxOutputBytes: DEFAULT_MAX_OUTPUT_BYTES,
        maxResultBytes: DEFAULT_MAX_RESULT_BYTES,
        maxHostCalls: DEFAULT_MAX_HOST_CALLS,
        maxImages: DEFAULT_MAX_IMAGES,
        maxAggregateEventBytes: DEFAULT_MAX_AGGREGATE_EVENT_BYTES,
      };
    }
    if (typeof rawLimits !== "object" || Array.isArray(rawLimits)) {
      throw new Error("limits must be a JSON object");
    }
    for (const [k, v] of Object.entries(rawLimits)) {
      if (!ALLOWED_LIMIT_KEYS.has(k)) {
        throw new Error(`Unsupported limit key: ${k}`);
      }
      if (typeof v !== "number" || !Number.isInteger(v) || v < 1) {
        throw new Error(`Limit ${k} must be a positive integer`);
      }
    }
    return {
      memoryLimitBytes: Math.min(
        128 * 1024 * 1024,
        Math.max(1024 * 1024, rawLimits.memory_limit_bytes ?? DEFAULT_MEMORY_LIMIT_BYTES)
      ),
      maxStackBytes: Math.min(
        2 * 1024 * 1024,
        Math.max(64 * 1024, rawLimits.max_stack_bytes ?? DEFAULT_MAX_STACK_BYTES)
      ),
      cpuTimeoutMs: Math.min(
        30000,
        Math.max(50, rawLimits.cpu_timeout_ms ?? DEFAULT_CPU_TIMEOUT_MS)
      ),
      maxPendingJobs: Math.min(
        100000,
        Math.max(64, rawLimits.max_pending_jobs ?? DEFAULT_MAX_PENDING_JOBS)
      ),
      maxOutputBytes: Math.min(
        1024 * 1024,
        Math.max(1024, rawLimits.max_output_bytes ?? DEFAULT_MAX_OUTPUT_BYTES)
      ),
      maxResultBytes: Math.min(
        1024 * 1024,
        Math.max(1024, rawLimits.max_result_bytes ?? DEFAULT_MAX_RESULT_BYTES)
      ),
      maxHostCalls: Math.min(
        256,
        Math.max(1, rawLimits.max_host_calls ?? DEFAULT_MAX_HOST_CALLS)
      ),
      maxImages: Math.min(
        64,
        Math.max(1, rawLimits.max_images ?? DEFAULT_MAX_IMAGES)
      ),
      maxAggregateEventBytes: Math.min(
        MAX_FRAME_BYTES - 256 * 1024,
        Math.max(4096, rawLimits.max_aggregate_event_bytes ?? DEFAULT_MAX_AGGREGATE_EVENT_BYTES)
      ),
    };
  }

  handleEvaluate(msg) {
    if (this.closed) {
      return;
    }
    if (!this._validateParentEnvelope(msg)) {
      return;
    }
    const executionId = typeof msg.execution_id === "string" ? msg.execution_id.trim() : "";
    if (!SAFE_ID_RE.test(executionId)) {
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: this.sessionId,
        generation: this.generation,
        error: {
          code: "ryn_js_invalid_execution_id",
          message: "execution_id must be a bounded identifier (1..128 safe ASCII chars)",
        },
      });
      return;
    }
    if (this.activeEvaluation !== null) {
      sendFrame({
        v: 1,
        type: "execution_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: executionId,
        error: {
          code: "ryn_js_busy",
          message: "Another evaluation is already active in this task session",
        },
      });
      return;
    }
    if (this.seenExecutionIds.has(executionId)) {
      sendFrame({
        v: 1,
        type: "execution_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: executionId,
        error: {
          code: "ryn_js_duplicate_execution_id",
          message: `Duplicate execution_id ${executionId} is not allowed`,
        },
      });
      return;
    }

    const code = typeof msg.code === "string" ? msg.code : "";
    if (!code.trim() || Buffer.byteLength(code, "utf8") > 256 * 1024) {
      sendFrame({
        v: 1,
        type: "execution_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: executionId,
        error: {
          code: "ryn_js_code_invalid",
          message: "Code must be a non-empty string within 256KB",
        },
      });
      return;
    }

    let parsedLimits;
    try {
      parsedLimits = this._parseAndValidateLimits(msg.limits);
    } catch (err) {
      sendFrame({
        v: 1,
        type: "execution_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: executionId,
        error: {
          code: "ryn_js_invalid_limits",
          message: err && err.message ? err.message : String(err),
        },
      });
      return;
    }

    if (this.seenExecutionIds.size >= MAX_TRACKED_EXECUTION_IDS) {
      const oldestExecId = this.seenExecutionIds.values().next().value;
      if (oldestExecId !== undefined) {
        this.seenExecutionIds.delete(oldestExecId);
      }
    }
    this.seenExecutionIds.add(executionId);

    this.runtime.setMemoryLimit(parsedLimits.memoryLimitBytes);
    this.runtime.setMaxStackSize(parsedLimits.maxStackBytes);

    this.cellSequence += 1;
    const filename = msg.script_id
      ? `__ryn_${String(msg.script_id).replace(/[^a-zA-Z0-9_-]/g, "_")}_${this.cellSequence}.mjs`
      : `__ryn_cell_${this.cellSequence}.mjs`;

    this.activeEvaluation = {
      executionId,
      scriptId: msg.script_id || filename,
      cpuTimeoutMs: parsedLimits.cpuTimeoutMs,
      cpuElapsedMs: 0,
      cpuSliceStartMs: Date.now(),
      maxPendingJobs: parsedLimits.maxPendingJobs,
      jobsExecuted: 0,
      maxOutputBytes: parsedLimits.maxOutputBytes,
      outputBytes: 0,
      outputs: [],
      maxResultBytes: parsedLimits.maxResultBytes,
      maxHostCalls: parsedLimits.maxHostCalls,
      maxArgBytes: DEFAULT_MAX_ARG_BYTES,
      maxImages: parsedLimits.maxImages,
      maxAggregateEventBytes: parsedLimits.maxAggregateEventBytes,
      aggregateEventBytes: 0,
      images: [],
      callbackOrdinal: 0,
      pendingCallbacks: new Map(),
      unsentHostCalls: [],
      promiseHandle: null,
      cancelled: false,
      interruptedReason: null,
      settled: false,
    };

    const evalRes = this._evaluateInGuest(code, filename);
    if (this.activeEvaluation.cpuSliceStartMs !== null) {
      this.activeEvaluation.cpuElapsedMs += Math.max(
        0,
        Date.now() - this.activeEvaluation.cpuSliceStartMs
      );
      this.activeEvaluation.cpuSliceStartMs = null;
    }

    if (evalRes.error) {
      const errInfo = this._extractGuestError(evalRes.error);
      evalRes.error.dispose();
      const interruptedCode = this.activeEvaluation.interruptedReason;
      if (interruptedCode) {
        errInfo.code = interruptedCode;
      }
      this._finishEvaluationWithError(errInfo);
      return;
    }

    this.activeEvaluation.cpuSliceStartMs = Date.now();
    const wrappedPromise = this.context.callFunction(this.toPromiseFnHandle, this.context.undefined, evalRes.value);
    evalRes.value.dispose();
    if (this.activeEvaluation.cpuSliceStartMs !== null) {
      this.activeEvaluation.cpuElapsedMs += Math.max(
        0,
        Date.now() - this.activeEvaluation.cpuSliceStartMs
      );
      this.activeEvaluation.cpuSliceStartMs = null;
    }
    if (wrappedPromise.error) {
      const errInfo = this._extractGuestError(wrappedPromise.error);
      wrappedPromise.error.dispose();
      this._finishEvaluationWithError(errInfo);
      return;
    }

    this.activeEvaluation.promiseHandle = wrappedPromise.value;
    this._pumpActiveEvaluation();
  }

  _pumpActiveEvaluation() {
    const active = this.activeEvaluation;
    if (!active || active.settled) {
      return;
    }

    active.cpuSliceStartMs = Date.now();
    while (this.runtime.hasPendingJob()) {
      if (active.cancelled) {
        active.interruptedReason = "ryn_js_cancelled";
        break;
      }
      const elapsedSlice = Date.now() - active.cpuSliceStartMs;
      if (active.cpuElapsedMs + elapsedSlice > active.cpuTimeoutMs) {
        active.interruptedReason = "ryn_js_execution_timeout";
        break;
      }
      if (active.jobsExecuted >= active.maxPendingJobs) {
        active.interruptedReason = "ryn_js_microtask_limit_exceeded";
        break;
      }

      const batchLimit = Math.min(DEFAULT_JOB_SLICE_SIZE, active.maxPendingJobs - active.jobsExecuted);
      const jobRes = this.runtime.executePendingJobs(batchLimit);
      if (jobRes.error) {
        const errInfo = this._extractGuestError(jobRes.error);
        jobRes.error.dispose();
        active.cpuElapsedMs += Math.max(0, Date.now() - active.cpuSliceStartMs);
        active.cpuSliceStartMs = null;
        if (active.interruptedReason) {
          errInfo.code = active.interruptedReason;
        }
        this._finishEvaluationWithError(errInfo);
        return;
      }
      const ranCount = Number(jobRes.value || 0);
      active.jobsExecuted += ranCount;
      if (ranCount === 0) {
        break;
      }
    }
    if (active.cpuSliceStartMs !== null) {
      active.cpuElapsedMs += Math.max(0, Date.now() - active.cpuSliceStartMs);
      active.cpuSliceStartMs = null;
    }

    if (active.interruptedReason) {
      this._finishEvaluationWithError({
        name: "InterruptError",
        code: active.interruptedReason,
        message: `Guest execution interrupted: ${active.interruptedReason}`,
      });
      return;
    }

    if (!active.promiseHandle) {
      this._finishEvaluationWithSuccess({ value: null, valueType: "undefined" });
      return;
    }

    const state = this.context.getPromiseState(active.promiseHandle);
    if (state.type === "fulfilled") {
      if (active.pendingCallbacks.size > 0) {
        state.value.dispose();
        active.unsentHostCalls.length = 0;
        this.generation += 1;
        this._finishEvaluationWithError(
          {
            name: "UnawaitedHostCallError",
            code: "ryn_js_unawaited_host_call",
            message: "Evaluation completed while unawaited host_call proposals remained pending",
          },
          this.generation - 1
        );
        this._initGuestRealm();
        return;
      }
      let serialized;
      try {
        serialized = this._serializeGuestValue(state.value, active.maxResultBytes);
      } catch (err) {
        state.value.dispose();
        this._finishEvaluationWithError({
          name: "SerializationError",
          code: "ryn_js_result_limit_exceeded",
          message: err && err.message ? err.message : String(err),
        });
        return;
      }
      state.value.dispose();
      this._finishEvaluationWithSuccess(serialized);
      return;
    }

    if (state.type === "rejected") {
      const hadPending = active.pendingCallbacks.size > 0;
      active.unsentHostCalls.length = 0;
      const errInfo = this._extractGuestError(state.error);
      state.error.dispose();
      if (active.interruptedReason) {
        errInfo.code = active.interruptedReason;
      }
      const errGen = this.generation;
      if (hadPending) {
        this.generation += 1;
      }
      this._finishEvaluationWithError(errInfo, errGen);
      if (hadPending) {
        this._initGuestRealm();
      }
      return;
    }

    // Promise is still pending.
    if (active.pendingCallbacks.size === 0 && !this.runtime.hasPendingJob()) {
      this._finishEvaluationWithError({
        name: "DeadlockError",
        code: "ryn_js_unresolved_promise",
        message: "Top-level Promise remained pending with no active host callback or microtask",
      });
      return;
    }

    while (active.unsentHostCalls.length > 0) {
      const nextFrame = active.unsentHostCalls.shift();
      sendFrame(nextFrame);
    }
  }

  handleHostResult(msg) {
    if (this.closed) {
      return;
    }
    if (!this._validateParentEnvelope(msg)) {
      return;
    }
    const active = this.activeEvaluation;
    const executionId = typeof msg.execution_id === "string" ? msg.execution_id : "";
    const callbackId = typeof msg.callback_id === "string" ? msg.callback_id : "";

    if (!active || active.settled || executionId !== active.executionId) {
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: executionId,
        callback_id: callbackId,
        error: {
          code: "ryn_js_stale_or_unknown_callback",
          message: "Host result does not match an active evaluation",
        },
      });
      return;
    }

    const entry = active.pendingCallbacks.get(callbackId);
    if (!entry || entry.settled) {
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: executionId,
        callback_id: callbackId,
        error: {
          code: "ryn_js_duplicate_or_unknown_callback",
          message: "Callback ID is unknown or already settled",
        },
      });
      return;
    }

    entry.settled = true;
    active.pendingCallbacks.delete(callbackId);

    // Reset active CPU slice timestamp BEFORE entering QuickJS so approval idle time is never charged.
    active.cpuSliceStartMs = Date.now();

    if (msg.ok === true) {
      if (entry.method === "get_window_state") {
        this._recordTrustedWindowStateImages(entry, msg.result);
      }
      let resultHandle;
      if (msg.result === undefined) {
        resultHandle = this.context.undefined;
      } else {
        const resultJson = JSON.stringify(msg.result);
        const jsonStrHandle = this.context.newString(resultJson);
        const parseRes = this.context.callFunction(
          this.jsonParseFnHandle,
          this.context.undefined,
          jsonStrHandle
        );
        jsonStrHandle.dispose();
        if (parseRes.error) {
          parseRes.error.dispose();
          entry.deferred.dispose();
          active.cpuElapsedMs += Math.max(0, Date.now() - active.cpuSliceStartMs);
          active.cpuSliceStartMs = null;
          this._finishEvaluationWithError({
            name: "HostResultError",
            code: "ryn_js_invalid_host_result",
            message: "Failed to materialize host result inside QuickJS guest",
          });
          return;
        }
        resultHandle = parseRes.value;
      }
      entry.deferred.resolve(resultHandle);
      if (resultHandle !== this.context.undefined) {
        resultHandle.dispose();
      }
      entry.deferred.dispose();
    } else {
      const errPayload = typeof msg.error === "object" && msg.error !== null ? msg.error : {};
      const errCode = typeof errPayload.code === "string" ? errPayload.code : "ryn_host_call_rejected";
      const errOutcome = String(
        errPayload.outcome || (errPayload.not_executed === false ? "unknown" : "rejected")
      );
      const errMsg =
        typeof errPayload.message === "string" && errPayload.message
          ? `${errCode}: ${errPayload.message}`
          : errCode;
      if (errOutcome === "unknown") {
        entry.deferred.dispose();
        active.unsentHostCalls.length = 0;
        if (active.cpuSliceStartMs !== null) {
          active.cpuElapsedMs += Math.max(0, Date.now() - active.cpuSliceStartMs);
          active.cpuSliceStartMs = null;
        }
        const priorGeneration = this.generation;
        this.generation += 1;
        this._finishEvaluationWithError(
          {
            name: "UnknownHostOutcomeError",
            code: errCode,
            message: errMsg,
            not_executed: false,
            outcome: "unknown",
          },
          priorGeneration
        );
        this._clearTrustedHostImages();
        this._initGuestRealm();
        return;
      }
      const errExpr = `(() => {
        const e = new Error(${JSON.stringify(errMsg)});
        e.code = ${JSON.stringify(errCode)};
        e.not_executed = ${JSON.stringify(Boolean(errPayload.not_executed))};
        e.outcome = ${JSON.stringify(errOutcome)};
        return e;
      })()`;
      const errRes = this.context.evalCode(errExpr, "__ryn_host_error.js");
      if (!errRes.error) {
        entry.deferred.reject(errRes.value);
        errRes.value.dispose();
      } else {
        entry.deferred.reject(errRes.error);
        errRes.error.dispose();
      }
      entry.deferred.dispose();
    }

    if (active.cpuSliceStartMs !== null) {
      active.cpuElapsedMs += Math.max(0, Date.now() - active.cpuSliceStartMs);
      active.cpuSliceStartMs = null;
    }

    this._pumpActiveEvaluation();
  }

  handleCancel(msg) {
    if (this.closed) {
      return;
    }
    if (!this._validateParentEnvelope(msg)) {
      return;
    }
    if (
      msg.execution_id !== undefined &&
      (!this.activeEvaluation ||
        this.activeEvaluation.settled ||
        msg.execution_id !== this.activeEvaluation.executionId)
    ) {
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: this.sessionId,
        generation: this.generation,
        execution_id: String(msg.execution_id),
        error: {
          code: "ryn_js_unknown_execution",
          message: "Cancel execution_id does not match active evaluation",
        },
      });
      return;
    }

    const reason = typeof msg.reason === "string" && msg.reason ? msg.reason : "ryn_js_cancelled";
    const cancelledGeneration = this.generation;
    this.generation += 1;

    if (this.activeEvaluation && !this.activeEvaluation.settled) {
      this.activeEvaluation.cancelled = true;
      this.activeEvaluation.interruptedReason = reason;
      this._finishEvaluationWithError(
        {
          name: "CancelError",
          code: reason,
          message: `Evaluation cancelled: ${reason}`,
        },
        cancelledGeneration
      );
    }
    this._clearTrustedHostImages();
    this._initGuestRealm();
  }

  handleEndTurn(msg) {
    if (this.closed) {
      return;
    }
    if (!this._validateParentEnvelope(msg)) {
      return;
    }
    this.turnNumber += 1;
    this._clearTrustedHostImages();
    if (this.activeEvaluation && !this.activeEvaluation.settled) {
      const priorGen = this.generation;
      this.generation += 1;
      this.activeEvaluation.cancelled = true;
      this._finishEvaluationWithError(
        {
          name: "TurnEndedError",
          code: "ryn_js_turn_ended",
          message: "Turn ended while evaluation had unresolved callbacks",
        },
        priorGen
      );
      this._initGuestRealm();
    }
    sendFrame({
      v: 1,
      type: "turn_ended",
      session_id: this.sessionId,
      generation: this.generation,
      turn: this.turnNumber,
    });
  }

  _cleanupActiveHandles(active) {
    if (!active) {
      return;
    }
    for (const entry of active.pendingCallbacks.values()) {
      if (!entry.settled) {
        entry.settled = true;
        try {
          entry.deferred.dispose();
        } catch {
          // Ignore disposal errors during teardown.
        }
      }
    }
    active.pendingCallbacks.clear();
    if (active.promiseHandle) {
      try {
        active.promiseHandle.dispose();
      } catch {
        // Ignore disposal errors during teardown.
      }
      active.promiseHandle = null;
    }
  }

  _finishEvaluationWithSuccess(serializedResult) {
    const active = this.activeEvaluation;
    if (!active || active.settled) {
      return;
    }
    active.settled = true;
    this._cleanupActiveHandles(active);
    this.activeEvaluation = null;

    sendFrame({
      v: 1,
      type: "execution_result",
      session_id: this.sessionId,
      generation: this.generation,
      execution_id: active.executionId,
      value: serializedResult.value,
      value_type: serializedResult.valueType,
      stdout: active.outputs.join(""),
      images: active.images,
      cpu_elapsed_ms: active.cpuElapsedMs,
    });
  }

  _finishEvaluationWithError(errInfo, generationOverride) {
    const active = this.activeEvaluation;
    if (!active || active.settled) {
      return;
    }
    active.settled = true;
    this._cleanupActiveHandles(active);
    this.activeEvaluation = null;

    sendFrame({
      v: 1,
      type: "execution_error",
      session_id: this.sessionId,
      generation: generationOverride ?? this.generation,
      active_generation: this.generation,
      execution_id: active.executionId,
      error: errInfo,
      stdout: active.outputs.join(""),
      images: active.images,
      cpu_elapsed_ms: active.cpuElapsedMs,
    });
  }

  _disposeGuestRealm() {
    if (this.activeEvaluation) {
      this._cleanupActiveHandles(this.activeEvaluation);
      this.activeEvaluation = null;
    }
    if (this.toPromiseFnHandle) {
      try {
        this.toPromiseFnHandle.dispose();
      } catch {
        // Ignore handle disposal errors on process exit.
      }
      this.toPromiseFnHandle = null;
    }
    if (this.jsonParseFnHandle) {
      try {
        this.jsonParseFnHandle.dispose();
      } catch {
        // Ignore handle disposal errors on process exit.
      }
      this.jsonParseFnHandle = null;
    }
    if (this.context) {
      try {
        this.context.dispose();
      } catch {
        // Ignore context disposal errors on process exit.
      }
      this.context = null;
    }
    if (this.runtime) {
      try {
        this.runtime.dispose();
      } catch {
        // Ignore runtime disposal errors on process exit.
      }
      this.runtime = null;
    }
  }

  close() {
    if (this.closed) {
      return;
    }
    this.closed = true;
    this._clearTrustedHostImages();
    this._disposeGuestRealm();
  }
}

function handleInboundLine(rawLine, worker) {
  if (worker.closed) {
    return;
  }
  if (!rawLine.trim()) {
    return;
  }
  if (Buffer.byteLength(rawLine, "utf8") > MAX_FRAME_BYTES) {
    sendFrame({
      v: 1,
      type: "protocol_error",
      session_id: worker.sessionId,
      generation: worker.generation,
      error: {
        code: "ryn_js_frame_too_large",
        message: "Inbound JSONL frame exceeds maximum byte limit",
      },
    });
    return;
  }

  let msg;
  try {
    msg = JSON.parse(rawLine);
  } catch {
    sendFrame({
      v: 1,
      type: "protocol_error",
      session_id: worker.sessionId,
      generation: worker.generation,
      error: {
        code: "ryn_js_malformed_frame",
        message: "Inbound line is not valid JSON",
      },
    });
    return;
  }

  if (typeof msg !== "object" || msg === null || msg.v !== 1 || typeof msg.type !== "string") {
    sendFrame({
      v: 1,
      type: "protocol_error",
      session_id: worker.sessionId,
      generation: worker.generation,
      error: {
        code: "ryn_js_invalid_frame",
        message: "Frame must be an object with v=1 and string type",
      },
    });
    return;
  }

  switch (msg.type) {
    case "evaluate":
      worker.handleEvaluate(msg);
      break;
    case "host_result":
      worker.handleHostResult(msg);
      break;
    case "cancel":
      worker.handleCancel(msg);
      break;
    case "end_turn":
      worker.handleEndTurn(msg);
      break;
    case "close":
      if (!worker._validateParentEnvelope(msg, { allowPriorGenerationForClose: true })) {
        break;
      }
      worker.close();
      process.exit(0);
      break;
    default:
      sendFrame({
        v: 1,
        type: "protocol_error",
        session_id: worker.sessionId,
        generation: worker.generation,
        error: {
          code: "ryn_js_unknown_frame_type",
          message: `Unknown frame type: ${String(msg.type)}`,
        },
      });
      break;
  }
}

async function main() {
  const sessionId = process.env.RYN_JS_SESSION_ID || "ryn_js_session";
  const initialGen = Number(process.env.RYN_JS_GENERATION || "1") || 1;

  const quickjs = await newQuickJSWASMModule(RELEASE_SYNC);
  const worker = new QuickJsTaskWorker(quickjs, sessionId, initialGen);

  sendFrame({
    v: 1,
    type: "ready",
    session_id: worker.sessionId,
    generation: worker.generation,
    engine: "quickjs-emscripten",
    release_variant: "RELEASE_SYNC",
    pid: process.pid,
  });

  let pendingChunks = [];
  let pendingBytes = 0;
  let discardingOversizedLine = false;

  process.stdin.on("data", (chunk) => {
    if (worker.closed) {
      return;
    }
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    let offset = 0;
    while (offset < buf.length) {
      const nlIndex = buf.indexOf(0x0a, offset);
      if (nlIndex === -1) {
        const tail = buf.subarray(offset);
        if (!discardingOversizedLine) {
          if (pendingBytes + tail.length > MAX_FRAME_BYTES) {
            discardingOversizedLine = true;
            pendingChunks = [];
            pendingBytes = 0;
          } else {
            pendingChunks.push(tail);
            pendingBytes += tail.length;
          }
        }
        break;
      }

      const slice = buf.subarray(offset, nlIndex);
      offset = nlIndex + 1;

      if (discardingOversizedLine) {
        discardingOversizedLine = false;
        sendFrame({
          v: 1,
          type: "protocol_error",
          session_id: worker.sessionId,
          generation: worker.generation,
          error: {
            code: "ryn_js_frame_too_large",
            message: "Inbound JSONL frame exceeds maximum byte limit",
          },
        });
        continue;
      }

      if (pendingBytes + slice.length > MAX_FRAME_BYTES) {
        pendingChunks = [];
        pendingBytes = 0;
        sendFrame({
          v: 1,
          type: "protocol_error",
          session_id: worker.sessionId,
          generation: worker.generation,
          error: {
            code: "ryn_js_frame_too_large",
            message: "Inbound JSONL frame exceeds maximum byte limit",
          },
        });
        continue;
      }

      const lineBuf =
        pendingChunks.length === 0 ? slice : Buffer.concat([...pendingChunks, slice], pendingBytes + slice.length);
      pendingChunks = [];
      pendingBytes = 0;
      const lineStr = lineBuf.toString("utf8").replace(/\r$/, "");
      handleInboundLine(lineStr, worker);
    }
  });

  const shutdown = () => {
    worker.close();
    process.exit(0);
  };

  process.stdin.on("end", shutdown);
  process.stdin.on("error", shutdown);
}

main().catch((err) => {
  trustedStderrWrite(`Fatal Ryn QuickJS worker startup error: ${err && err.stack ? err.stack : String(err)}\n`);
  process.exit(1);
});
