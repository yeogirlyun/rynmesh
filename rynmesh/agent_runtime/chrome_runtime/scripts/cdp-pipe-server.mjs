import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import fs from "node:fs";
import { spawn } from "node:child_process";

const FRAME_HEADER_BYTES = 4;
const MAX_FRAME_BYTES = 64 * 1024 * 1024;

function encodeFrame(payload) {
  const json = typeof payload === "string" ? payload : JSON.stringify(payload);
  const body = Buffer.from(json, "utf8");
  const frame = Buffer.allocUnsafe(FRAME_HEADER_BYTES + body.length);
  frame.writeUInt32LE(body.length, 0);
  body.copy(frame, FRAME_HEADER_BYTES);
  return frame;
}

class FrameDecoder {
  constructor() {
    this.buffer = Buffer.alloc(0);
  }

  push(chunk) {
    if (!chunk || chunk.length === 0) return [];
    this.buffer = this.buffer.length === 0 ? Buffer.from(chunk) : Buffer.concat([this.buffer, chunk]);
    const frames = [];
    while (this.buffer.length >= FRAME_HEADER_BYTES) {
      const len = this.buffer.readUInt32LE(0);
      if (len > MAX_FRAME_BYTES) {
        throw new Error(`Frame exceeds max bytes: ${len}`);
      }
      if (this.buffer.length < FRAME_HEADER_BYTES + len) {
        break;
      }
      const jsonStr = this.buffer.subarray(FRAME_HEADER_BYTES, FRAME_HEADER_BYTES + len).toString("utf8");
      this.buffer = this.buffer.subarray(FRAME_HEADER_BYTES + len);
      frames.push(JSON.parse(jsonStr));
    }
    return frames;
  }
}

function httpJsonRequest(port, route, method = "GET", timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        hostname: "127.0.0.1",
        port,
        path: route,
        method,
        timeout: timeoutMs,
        headers: { Connection: "close" },
      },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf8");
          if (res.statusCode && res.statusCode >= 400) {
            reject(new Error(`HTTP ${res.statusCode} for ${method} ${route}: ${raw.slice(0, 200)}`));
            return;
          }
          try {
            resolve(raw ? JSON.parse(raw) : {});
          } catch (_) {
            resolve(raw);
          }
        });
      }
    );
    req.on("timeout", () => {
      req.destroy(new Error(`HTTP timeout for ${method} ${route}`));
    });
    req.on("error", reject);
    req.end();
  });
}

export function findChromeExecutable() {
  const envExe = process.env.RYN_CHROME_EXECUTABLE || process.env.CHROME_PATH;
  if (envExe && fs.existsSync(envExe)) return envExe;
  if (process.platform === "win32") {
    const bases = [
      process.env.PROGRAMFILES,
      process.env["PROGRAMFILES(X86)"],
      process.env.LOCALAPPDATA,
    ].filter(Boolean);
    const rels = [
      "Google/Chrome/Application/chrome.exe",
      "Chromium/Application/chrome.exe",
      "Microsoft/Edge/Application/msedge.exe",
    ];
    for (const rel of rels) {
      for (const base of bases) {
        const p = path.join(base, rel);
        if (fs.existsSync(p)) return p;
      }
    }
  } else if (process.platform === "darwin") {
    const macPaths = [
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ];
    for (const p of macPaths) {
      if (fs.existsSync(p)) return p;
    }
  } else {
    const linuxBins = ["/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"];
    for (const p of linuxBins) {
      if (fs.existsSync(p)) return p;
    }
  }
  return null;
}

export async function launchLocalCdpChrome({ headless = true, userDataDir = null } = {}) {
  const exe = findChromeExecutable();
  if (!exe) {
    throw new Error("Chrome executable not found on host");
  }
  const profileDir = userDataDir || fs.mkdtempSync(path.join(os.tmpdir(), "ryn-codex-chrome-"));
  const args = [
    "--remote-debugging-port=0",
    "--remote-debugging-address=127.0.0.1",
    `--user-data-dir=${profileDir}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-sync",
    "about:blank",
  ];
  if (headless) {
    args.splice(args.length - 1, 0, "--headless=new");
  }
  const child = spawn(exe, args, {
    stdio: "ignore",
    windowsHide: true,
  });
  const portFile = path.join(profileDir, "DevToolsActivePort");
  const deadline = Date.now() + 12000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Chrome exited prematurely with code ${child.exitCode}`);
    }
    if (fs.existsSync(portFile)) {
      try {
        const lines = fs.readFileSync(portFile, "utf8").trim().split(/\r?\n/);
        const port = Number(lines[0]);
        if (Number.isInteger(port) && port > 0) {
          await httpJsonRequest(port, "/json/version", "GET", 2000);
          return {
            port,
            process: child,
            profileDir,
            async close() {
              try {
                child.kill();
              } catch (_) {}
            },
          };
        }
      } catch (_) {}
    }
    await new Promise((r) => setTimeout(r, 80));
  }
  try {
    child.kill();
  } catch (_) {}
  throw new Error("Timed out waiting for Chrome DevToolsActivePort");
}

export async function startCdpPipeServer({
  cdpPort,
  pipePath = null,
  family = "chrome",
  name = "Chrome",
  profileName = "Chrome",
  instanceId = null,
} = {}) {
  if (!Number.isInteger(cdpPort) || cdpPort <= 0) {
    throw new Error(`Invalid cdpPort: ${cdpPort}`);
  }
  const resolvedInstanceId = instanceId || crypto.randomUUID();
  const resolvedPipePath =
    pipePath ||
    (process.platform === "win32"
      ? `\\\\.\\pipe\\codex-browser-use\\ryn-${resolvedInstanceId}`
      : path.join("/tmp/codex-browser-use", `ryn-${resolvedInstanceId}`));

  if (process.platform !== "win32") {
    fs.mkdirSync(path.dirname(resolvedPipePath), { recursive: true });
    try {
      fs.unlinkSync(resolvedPipePath);
    } catch (_) {}
  }

  let nextTabId = 1;
  const targetIdToTabId = new Map();
  const tabIdToTargetId = new Map();
  const claimedTabIds = new Set();
  const tabConnections = new Map(); // tabId -> { ws, targetId, pending: Map<id, {resolve, reject, timer}> }
  const expressionCache = new Map();
  const clientSockets = new Set();
  let nextCdpId = 1;

  function broadcastNotification(method, params) {
    const frame = encodeFrame({ jsonrpc: "2.0", method, params });
    for (const sock of clientSockets) {
      if (!sock.destroyed) {
        try {
          sock.write(frame);
        } catch (_) {}
      }
    }
  }

  function registerTarget(target) {
    const targetId = String(target.id || target.targetId || "");
    if (!targetId) return null;
    let tabId = targetIdToTabId.get(targetId);
    if (!tabId) {
      tabId = nextTabId++;
      targetIdToTabId.set(targetId, tabId);
      tabIdToTargetId.set(tabId, targetId);
    }
    return tabId;
  }

  async function syncPageTargets() {
    const list = await httpJsonRequest(cdpPort, "/json/list", "GET", 5000);
    const rows = Array.isArray(list) ? list : [];
    const pageTargets = rows.filter(
      (r) =>
        r &&
        r.type === "page" &&
        r.id &&
        !String(r.url || "").startsWith("devtools://") &&
        !String(r.url || "").startsWith("chrome-extension://")
    );
    const liveTargetIds = new Set();
    const tabs = [];
    pageTargets.forEach((t, idx) => {
      const tabId = registerTarget(t);
      liveTargetIds.add(String(t.id));
      tabs.push({
        id: tabId,
        targetId: String(t.id),
        url: String(t.url || "about:blank"),
        title: String(t.title || ""),
        active: idx === 0,
        faviconUrl: t.faviconUrl || undefined,
        webSocketDebuggerUrl: t.webSocketDebuggerUrl || undefined,
        sessionControlled: claimedTabIds.has(tabId),
      });
    });
    for (const [targetId, tabId] of [...targetIdToTabId.entries()]) {
      if (!liveTargetIds.has(targetId)) {
        targetIdToTabId.delete(targetId);
        tabIdToTargetId.delete(tabId);
        claimedTabIds.delete(tabId);
        closeTabConnection(tabId);
      }
    }
    return tabs;
  }

  function closeTabConnection(tabId, notify = false) {
    const conn = tabConnections.get(tabId);
    if (!conn) return;
    tabConnections.delete(tabId);
    for (const entry of conn.pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(new Error("Debugger detached"));
    }
    conn.pending.clear();
    try {
      conn.ws.close();
    } catch (_) {}
    if (notify) {
      broadcastNotification("onCDPDetach", { tabId: Number(tabId) });
    }
  }

  async function ensureTabConnection(tabId) {
    const numericTabId = Number(tabId);
    const existing = tabConnections.get(numericTabId);
    if (existing && existing.ws.readyState === WebSocket.OPEN) {
      return existing;
    }
    if (existing) {
      closeTabConnection(numericTabId, false);
    }
    const tabs = await syncPageTargets();
    const tab = tabs.find((t) => t.id === numericTabId);
    if (!tab || !tab.webSocketDebuggerUrl) {
      throw new Error(`Tab not found or not debuggable: ${numericTabId}`);
    }
    const ws = new WebSocket(tab.webSocketDebuggerUrl);
    const pending = new Map();
    const conn = { ws, targetId: tab.targetId, pending };

    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("WebSocket connect timeout")), 5000);
      ws.addEventListener(
        "open",
        () => {
          clearTimeout(timer);
          resolve();
        },
        { once: true }
      );
      ws.addEventListener(
        "error",
        (ev) => {
          clearTimeout(timer);
          reject(new Error(ev?.message || "WebSocket connection failed"));
        },
        { once: true }
      );
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try {
        const text = typeof ev.data === "string" ? ev.data : Buffer.from(ev.data).toString("utf8");
        msg = JSON.parse(text);
      } catch (_) {
        return;
      }
      if (msg.id !== undefined) {
        const req = pending.get(msg.id);
        if (req) {
          pending.delete(msg.id);
          clearTimeout(req.timer);
          if (msg.error) {
            req.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
          } else {
            req.resolve(msg.result ?? {});
          }
        }
        return;
      }
      if (msg.method) {
        broadcastNotification("onCDPEvent", {
          source: {
            tabId: numericTabId,
            ...(msg.sessionId ? { sessionId: msg.sessionId } : {}),
          },
          method: msg.method,
          params: msg.params ?? {},
        });
      }
    });

    ws.addEventListener("close", () => {
      if (tabConnections.get(numericTabId) === conn) {
        closeTabConnection(numericTabId, true);
      }
    });

    tabConnections.set(numericTabId, conn);
    return conn;
  }

  async function sendCdpCommand(tabId, method, commandParams = {}, { sessionId = undefined, timeoutMs = 15000 } = {}) {
    const numericTabId = Number(tabId);
    if (method === "Page.close" || method === "Target.closeTarget") {
      const targetId = commandParams?.targetId || tabIdToTargetId.get(numericTabId);
      closeTabConnection(numericTabId, false);
      claimedTabIds.delete(numericTabId);
      if (targetId) {
        await httpJsonRequest(cdpPort, `/json/close/${targetId}`, "GET", 3000).catch(() => {});
      }
      return { success: true };
    }

    const conn = await ensureTabConnection(numericTabId);
    const id = nextCdpId++;
    const payload = {
      id,
      method,
      params: commandParams || {},
      ...(sessionId ? { sessionId } : {}),
    };

    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        conn.pending.delete(id);
        reject(new Error(`CDP timeout after ${timeoutMs}ms for ${method}`));
      }, Math.max(500, Number(timeoutMs) || 15000));
      conn.pending.set(id, { resolve, reject, timer });
      try {
        conn.ws.send(JSON.stringify(payload));
      } catch (err) {
        clearTimeout(timer);
        conn.pending.delete(id);
        reject(err);
      }
    });

    if (method === "Target.getTargets" && result && Array.isArray(result.targetInfos)) {
      await syncPageTargets().catch(() => {});
      for (const ti of result.targetInfos) {
        if (ti && ti.targetId && targetIdToTabId.has(ti.targetId)) {
          ti.tabId = targetIdToTabId.get(ti.targetId);
        }
      }
    }
    return result;
  }

  const handlers = {
    async ping() {
      return "pong";
    },
    async getInfo() {
      return {
        agentRequestHeaderEnabled: false,
        apiSupportOverrides: {
          "BrowserUser.getTabContext": true,
          "Tab.ax": true,
        },
        capabilities: {
          browser: [
            {
              id: "viewport",
              description:
                "Controls an explicit browser viewport override for responsive or device-size testing. Use it when a task calls for specific dimensions or breakpoint validation; otherwise leave it unset so the browser uses its normal viewport. Reset temporary overrides before finishing unless the user asked to keep them.",
            },
          ],
          tab: [
            {
              id: "pageAssets",
              description:
                "List assets already observed in the current page state and bundle selected assets into a temporary local artifact.",
            },
          ],
        },
        family,
        metadata: {
          extensionId: "hehggadaopoacecdllhhajmbjkdcmajg",
          extensionInstanceId: resolvedInstanceId,
          profileName,
        },
        name,
        type: "extension",
        version: "1.26.1005.11759",
      };
    },
    async getUserTabs() {
      const tabs = await syncPageTargets();
      return tabs.map(({ id, url, title, active, faviconUrl }) => ({
        id,
        url,
        title,
        active,
        ...(faviconUrl ? { faviconUrl } : {}),
      }));
    },
    async getTabs() {
      const tabs = await syncPageTargets();
      return tabs.map(({ id, url, title, active, faviconUrl, sessionControlled }) => ({
        id,
        url,
        title,
        active,
        sessionControlled,
        ...(faviconUrl ? { faviconUrl } : {}),
      }));
    },
    async claimUserTab({ tabId }) {
      const numericTabId = Number(tabId);
      const tabs = await syncPageTargets();
      const found = tabs.find((t) => t.id === numericTabId);
      if (!found) {
        throw new Error(`Tab not found: ${numericTabId}`);
      }
      claimedTabIds.add(numericTabId);
      return {
        id: found.id,
        url: found.url,
        title: found.title,
        active: found.active,
      };
    },
    async createTab() {
      let created;
      try {
        created = await httpJsonRequest(cdpPort, "/json/new?about:blank", "PUT", 5000);
      } catch (_) {
        created = await httpJsonRequest(cdpPort, "/json/new?about:blank", "GET", 5000);
      }
      const tabId = registerTarget(created);
      if (!tabId) {
        throw new Error("Failed to create tab via CDP");
      }
      claimedTabIds.add(tabId);
      return {
        id: tabId,
        url: String(created.url || "about:blank"),
        title: String(created.title || ""),
        active: true,
      };
    },
    async getCommittedTabUrl({ tabId }) {
      const numericTabId = Number(tabId);
      try {
        const evalRes = await sendCdpCommand(numericTabId, "Runtime.evaluate", {
          expression: "window.location.href",
          returnByValue: true,
        });
        const href = evalRes?.result?.value;
        if (typeof href === "string" && href.length > 0) {
          return href;
        }
      } catch (_) {}
      const tabs = await syncPageTargets();
      const found = tabs.find((t) => t.id === numericTabId);
      if (!found) {
        throw new Error(`Tab not found: ${numericTabId}`);
      }
      return found.url;
    },
    async attach({ tabId }) {
      await ensureTabConnection(Number(tabId));
      return {};
    },
    async attachTarget({ tabId }) {
      await ensureTabConnection(Number(tabId));
      return {};
    },
    async detach({ tabId }) {
      closeTabConnection(Number(tabId), false);
      return {};
    },
    async detachTarget() {
      return {};
    },
    async executeCdp({ target, method, commandParams, timeoutMs }) {
      const tabId = Number(target?.tabId);
      const sessionId = target?.sessionId;
      return await sendCdpCommand(tabId, method, commandParams || {}, { sessionId, timeoutMs });
    },
    async executeCdpWithCachedExpression({ target, method, commandParams, expressionCacheKey, timeoutMs }) {
      const params = { ...(commandParams || {}) };
      if (typeof params.expression === "string") {
        expressionCache.set(expressionCacheKey, params.expression);
      } else if (expressionCache.has(expressionCacheKey)) {
        params.expression = expressionCache.get(expressionCacheKey);
      } else {
        return { kind: "missing" };
      }
      const result = await sendCdpCommand(Number(target?.tabId), method, params, {
        sessionId: target?.sessionId,
        timeoutMs,
      });
      return { kind: "executed", result };
    },
    async executeTabRead({ tabId, expectedUrl, read }) {
      const numericTabId = Number(tabId);
      const kind = read?.kind;
      if (kind === "page") {
        const evalRes = await sendCdpCommand(numericTabId, "Runtime.evaluate", {
          expression: `(() => {
            const rawText = String(document.body?.innerText || "");
            const maxChars = 200000;
            return {
              url: String(window.location.href || ""),
              title: String(document.title || "").slice(0, 4096),
              text: rawText.slice(0, maxChars),
              truncated: rawText.length > maxChars
            };
          })()`,
          returnByValue: true,
        });
        const val = evalRes?.result?.value;
        if (val && expectedUrl && val.url !== expectedUrl) {
          val.url = expectedUrl;
        }
        return val;
      }
      if (kind === "selection") {
        const evalRes = await sendCdpCommand(numericTabId, "Runtime.evaluate", {
          expression: `({ text: String(window.getSelection?.()?.toString() || ""), truncated: false })`,
          returnByValue: true,
        });
        return evalRes?.result?.value ?? { text: "", truncated: false };
      }
      return null;
    },
    async getUserHistory() {
      const tabs = await syncPageTargets();
      return tabs.map((t) => ({
        url: t.url,
        title: t.title,
        dateVisited: new Date().toISOString(),
      }));
    },
    async markTab() {
      return {};
    },
    async nameSession() {
      return {};
    },
    async followSessionTab() {
      return false;
    },
    async moveMouse() {
      return {};
    },
    async browserAuthNewTargetProtection() {
      return true;
    },
    async allowDownload() {
      return {};
    },
    async setBlobDownloadOrigin() {
      return {};
    },
    async turnEnded() {
      return {};
    },
    async executeUnhandledCommand() {
      return {};
    },
  };

  const server = net.createServer((socket) => {
    clientSockets.add(socket);
    const decoder = new FrameDecoder();

    socket.on("data", async (chunk) => {
      let messages;
      try {
        messages = decoder.push(chunk);
      } catch (err) {
        socket.destroy(err);
        return;
      }
      for (const msg of messages) {
        if (!msg || typeof msg !== "object" || msg.id === undefined || !msg.method) {
          continue;
        }
        const handler = handlers[msg.method];
        if (typeof handler !== "function") {
          socket.write(
            encodeFrame({
              jsonrpc: "2.0",
              id: msg.id,
              error: { code: -1, message: `No handler registered for method: ${msg.method}` },
            })
          );
          continue;
        }
        try {
          const result = await handler(msg.params || {});
          if (!socket.destroyed) {
            socket.write(encodeFrame({ jsonrpc: "2.0", id: msg.id, result: result ?? {} }));
          }
        } catch (err) {
          if (!socket.destroyed) {
            socket.write(
              encodeFrame({
                jsonrpc: "2.0",
                id: msg.id,
                error: { code: 1, message: err instanceof Error ? err.message : String(err) },
              })
            );
          }
        }
      }
    });

    socket.on("close", () => {
      clientSockets.delete(socket);
    });
    socket.on("error", () => {
      clientSockets.delete(socket);
    });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(resolvedPipePath, () => {
      server.removeListener("error", reject);
      resolve();
    });
  });

  return {
    pipePath: resolvedPipePath,
    instanceId: resolvedInstanceId,
    cdpPort,
    async close() {
      for (const tabId of [...tabConnections.keys()]) {
        closeTabConnection(tabId, false);
      }
      for (const sock of clientSockets) {
        try {
          sock.destroy();
        } catch (_) {}
      }
      clientSockets.clear();
      await new Promise((resolve) => server.close(() => resolve()));
    },
  };
}
