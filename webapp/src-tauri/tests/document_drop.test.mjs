import assert from "node:assert/strict";
import { File } from "node:buffer";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../src/craft_browser.rs", import.meta.url), "utf8");
const script = source.match(/const DROP_DOCUMENT: &str = r#"([\s\S]*?)"#;/)[1]
  .replace("__NAME__", JSON.stringify("销售复盘报告.pdf"));

function environment() {
  let now = 0;
  let nextId = 0;
  const timers = [];
  const canvas = new EventTarget();
  const notices = [];
  const calls = [];
  const target = canvas.dispatchEvent.bind(canvas);
  canvas.dispatchEvent = (event) => {
    calls.push({ type: event.type, at: now, file: event.dataTransfer.files[0] });
    return target(event);
  };
  const state = { canvasPresent: true, loading: false };
  class DragEvent extends Event {
    constructor(type, options) {
      super(type, options);
      this.dataTransfer = options.dataTransfer;
    }
  }
  class DataTransfer {
    files = [];
    items = { add: (file) => this.files.push(file) };
  }
  const schedule = (callback, delay) => timers.push({ callback, at: now + delay, id: nextId++ });
  const document = {
    querySelector: (selector) => selector === "canvas" ? (state.canvasPresent ? canvas : null) : state.loading,
    createElement: () => ({ style: {}, setAttribute(name, value) { this[name] = value; } }),
    body: { appendChild: (notice) => notices.push(notice) },
  };
  const window = { __rynDocument: [Uint8Array.from([37, 80, 68, 70])] };
  const context = vm.createContext({ window, document, navigator: { language: "zh-CN" },
    File, DragEvent, DataTransfer, Date: { now: () => now }, setTimeout: schedule });
  return { canvas, calls, notices, state, timers, window,
    at: (time, callback) => timers.push({ callback, at: time, id: nextId++ }),
    start: () => vm.runInContext(script, context),
    runUntil(time) {
      for (let count = 0; count < 10000; count++) {
        timers.sort((left, right) => left.at - right.at || left.id - right.id);
        if (!timers.length || timers[0].at > time) { now = time; return; }
        const timer = timers.shift();
        now = timer.at;
        timer.callback();
      }
      throw new Error("document drop did not settle within the bounded timer count");
    },
  };
}

test("an early PDF canvas waits for the late real drop listener and imports exactly once", async () => {
  const env = environment();
  const accepted = [];
  env.at(7500, () => env.canvas.addEventListener("drop", (event) => {
    event.preventDefault();
    accepted.push(event.dataTransfer.files[0]);
  }));
  env.start();
  env.runUntil(60000);
  assert.equal(accepted.length, 1);
  assert.equal(accepted[0].name, "销售复盘报告.pdf");
  assert.deepEqual(new Uint8Array(await accepted[0].arrayBuffer()), Uint8Array.from([37, 80, 68, 70]));
  assert.equal(env.calls.filter((call) => call.type === "drop").at(-1).at, 7500);
  assert.equal(env.timers.length, 0);
  assert.equal(env.notices.length, 0);
  assert.equal(env.window.__rynDocument, undefined);
});

test("dragover cancellation alone cannot acknowledge file import", () => {
  const env = environment();
  env.canvas.addEventListener("dragover", (event) => event.preventDefault());
  let accepted = 0;
  env.at(1000, () => env.canvas.addEventListener("drop", (event) => { event.preventDefault(); accepted++; }));
  env.start();
  env.runUntil(60000);
  assert.equal(accepted, 1);
  assert.equal(env.calls.filter((call) => call.type === "drop").length, 11);
  assert.equal(env.notices.length, 0);
});

test("loading Word and Grid canvases receive no drop until their loading notice disappears", () => {
  const env = environment();
  env.state.loading = true;
  env.canvas.addEventListener("drop", (event) => event.preventDefault());
  env.at(2500, () => { env.state.loading = false; });
  env.start();
  env.runUntil(60000);
  assert.equal(env.calls.filter((call) => call.type === "drop").length, 1);
  assert.equal(env.calls[0].at, 2500);
  assert.equal(env.notices.length, 0);
});

test("an unacknowledged canvas stops at sixty seconds and shows one visible fallback", () => {
  const env = environment();
  env.start();
  env.runUntil(120000);
  assert.equal(env.calls.filter((call) => call.type === "drop").length, 600);
  assert.equal(env.notices.length, 1);
  assert.equal(env.notices[0].role, "alert");
  assert.match(env.notices[0].textContent, /文件未能自动打开/);
  assert.equal(env.timers.length, 0);
});

test("a page with no canvas never receives the file and also stops at the deadline", () => {
  const env = environment();
  env.state.canvasPresent = false;
  env.start();
  env.runUntil(120000);
  assert.equal(env.calls.length, 0);
  assert.equal(env.notices.length, 1);
  assert.equal(env.timers.length, 0);
});
