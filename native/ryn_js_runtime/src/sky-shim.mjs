/**
 * Ryn-owned guest ES module source for "@oai/sky" inside QuickJS WASM.
 *
 * This module source is evaluated strictly inside the QuickJS guest context.
 * It validates Window2-compatible parameters, normalizes responses, emits
 * validated host-returned screenshot descriptors through the guest bridge,
 * and delegates all desktop operations as inert host_call proposals.
 */

export const SKY_SHIM_MODULE_SOURCE = `
const bridge = globalThis.__ryn_internal_bridge;
if (!bridge || typeof bridge.hostCall !== "function") {
  throw new Error("Ryn QuickJS guest bridge is not initialized");
}

function normalizeNonEmptyString(value) {
  return typeof value === "string" && value.trim() !== "" ? value : undefined;
}

function requireApp(value) {
  const app = normalizeNonEmptyString(value);
  if (app === undefined) {
    throw new TypeError("app is required");
  }
  return app;
}

function normalizeNonNegativeInt(value) {
  if (typeof value === "number" && Number.isInteger(value) && value >= 0) {
    return value;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    if (Number.isInteger(parsed) && parsed >= 0) {
      return parsed;
    }
  }
  return undefined;
}

function setIfDefined(target, key, value) {
  if (value !== undefined) {
    target[key] = value;
  }
}

function optionalString(value) {
  return typeof value === "string" ? value : undefined;
}

function optionalFiniteNumber(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function normalizeWindow(raw, fallbackApp) {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return undefined;
  }
  const id = normalizeNonNegativeInt(Reflect.get(raw, "id"));
  if (id === undefined) {
    return undefined;
  }
  const app = normalizeNonEmptyString(Reflect.get(raw, "app")) ?? fallbackApp;
  if (app === undefined) {
    return undefined;
  }
  const out = { app, id };
  setIfDefined(out, "title", optionalString(Reflect.get(raw, "title")));
  return out;
}

function requireWindow(raw) {
  const win = normalizeWindow(raw);
  if (win === undefined) {
    throw new TypeError("window.app must be a non-empty string and window.id must be an integer >= 0");
  }
  return win;
}

function normalizeWindowList(raw, fallbackApp) {
  if (!Array.isArray(raw)) {
    return [];
  }
  const result = [];
  for (const item of raw) {
    const win = normalizeWindow(item, fallbackApp);
    if (win !== undefined) {
      result.push(win);
    }
  }
  return result;
}

function roundCoordinate(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new TypeError(\`\${label} must be a finite number\`);
  }
  return Math.round(value);
}

function requirePositiveInt(value, label) {
  const rounded = roundCoordinate(value, label);
  if (rounded < 1) {
    throw new TypeError(\`\${label} must be >= 1\`);
  }
  return rounded;
}

function requireElementIndex(value) {
  const idx = normalizeNonNegativeInt(value);
  if (idx === undefined) {
    throw new TypeError("element_index must be an integer >= 0");
  }
  return idx;
}

function normalizeKeySpec(value) {
  if (typeof value !== "string" || !value) {
    throw new TypeError("key is required");
  }
  const parts = value.split("+").map((part) => part.trim()).filter(Boolean);
  if (parts.length === 0) {
    throw new TypeError("key is required");
  }
  return parts.join("+");
}

function normalizeScreenshot(raw) {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new Error("ryn-computer-use did not return a screenshot");
  }
  const url = Reflect.get(raw, "url");
  if (typeof url !== "string" || url.length === 0) {
    throw new Error("ryn-computer-use did not return a screenshot URL");
  }
  if (!url.startsWith("data:") && url.length > 2048) {
    throw new Error("ryn-computer-use returned an oversized screenshot capability URL");
  }
  const id = optionalString(Reflect.get(raw, "id")) ?? "screenshot-0";
  const zIndex = optionalFiniteNumber(Reflect.get(raw, "zIndex")) ?? 0;
  const out = { id, zIndex, url };
  setIfDefined(out, "originX", optionalFiniteNumber(Reflect.get(raw, "originX")));
  setIfDefined(out, "originY", optionalFiniteNumber(Reflect.get(raw, "originY")));
  setIfDefined(out, "width", optionalFiniteNumber(Reflect.get(raw, "width")));
  setIfDefined(out, "height", optionalFiniteNumber(Reflect.get(raw, "height")));
  setIfDefined(out, "sha256", optionalString(Reflect.get(raw, "sha256")));
  setIfDefined(out, "mimeType", optionalString(Reflect.get(raw, "mimeType")));
  setIfDefined(out, "byteLength", optionalFiniteNumber(Reflect.get(raw, "byteLength")));
  setIfDefined(out, "stateToken", optionalString(Reflect.get(raw, "stateToken")));
  return out;
}

function normalizeAccessibility(raw) {
  if (raw === null || raw === undefined) {
    return null;
  }
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("ryn-computer-use did not return accessibility state");
  }
  const tree = Reflect.get(raw, "tree");
  if (typeof tree !== "string") {
    throw new Error("ryn-computer-use did not return accessibility tree");
  }
  const selectedElementsRaw = Reflect.get(raw, "selected_elements");
  let selectedElements;
  if (selectedElementsRaw !== null && selectedElementsRaw !== undefined) {
    if (!Array.isArray(selectedElementsRaw) || selectedElementsRaw.some((item) => typeof item !== "string")) {
      throw new Error("ryn-computer-use did not return selected_elements");
    }
    selectedElements = selectedElementsRaw.slice();
  }
  const out = { tree };
  setIfDefined(out, "focused_element", optionalString(Reflect.get(raw, "focused_element")));
  setIfDefined(out, "selected_text", optionalString(Reflect.get(raw, "selected_text")));
  setIfDefined(out, "selected_elements", selectedElements);
  setIfDefined(out, "document_text", optionalString(Reflect.get(raw, "document_text")));
  return out;
}

const skyImpl = {
  target: "windows",
  provider: "ryn",
  implementation: "ryn-computer-use-sky-shim",
  version: "0.1.0",

  async list_apps() {
    const raw = await bridge.hostCall("list_apps", {});
    if (!Array.isArray(raw)) {
      return [];
    }
    const apps = [];
    for (const item of raw) {
      if (typeof item !== "object" || item === null) {
        continue;
      }
      const id = normalizeNonEmptyString(Reflect.get(item, "id"));
      if (id === undefined) {
        continue;
      }
      const entry = {
        id,
        windows: normalizeWindowList(Reflect.get(item, "windows"), id),
      };
      setIfDefined(entry, "displayName", optionalString(Reflect.get(item, "displayName")));
      setIfDefined(entry, "lastUsedDate", optionalString(Reflect.get(item, "lastUsedDate")));
      setIfDefined(entry, "useCount", optionalFiniteNumber(Reflect.get(item, "useCount")));
      const isRunning = Reflect.get(item, "isRunning");
      if (typeof isRunning === "boolean") {
        entry.isRunning = isRunning;
      }
      apps.push(entry);
    }
    return apps;
  },

  async launch_app(input) {
    const app = requireApp(typeof input === "object" && input !== null ? Reflect.get(input, "app") : undefined);
    await bridge.hostCall("launch_app", { app });
  },

  async list_windows() {
    const raw = await bridge.hostCall("list_windows", {});
    return normalizeWindowList(raw);
  },

  async get_window(input) {
    if (typeof input !== "object" || input === null || Array.isArray(input)) {
      throw new TypeError("get_window input must be an object");
    }
    const id = normalizeNonNegativeInt(Reflect.get(input, "id"));
    if (id === undefined) {
      throw new TypeError("id must be an integer >= 0");
    }
    const app = normalizeNonEmptyString(Reflect.get(input, "app"));
    const params = app === undefined ? { id } : { app, id };
    const raw = await bridge.hostCall("get_window", params);
    const win = normalizeWindow(raw, app);
    if (win === undefined) {
      throw new Error("ryn-computer-use did not return a window");
    }
    return win;
  },

  async activate_window(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("activate_window input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    await bridge.hostCall("activate_window", { window: win });
  },

  async get_window_state(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("get_window_state input must be an object");
    }
    const includeScreenshot = Reflect.get(input, "include_screenshot") ?? true;
    const includeText = Reflect.get(input, "include_text") ?? false;
    if (!includeScreenshot && !includeText) {
      throw new TypeError("get_window_state must request include_text, include_screenshot, or both");
    }
    const reqWindow = requireWindow(Reflect.get(input, "window"));
    const raw = await bridge.hostCall("get_window_state", {
      window: reqWindow,
      include_screenshot: Boolean(includeScreenshot),
      include_text: Boolean(includeText),
    });
    if (typeof raw !== "object" || raw === null) {
      throw new Error("ryn-computer-use did not return window state");
    }
    const accessibility = normalizeAccessibility(Reflect.get(raw, "accessibility"));
    const rawScreenshots = Reflect.get(raw, "screenshots");
    let screenshots = [];
    if (rawScreenshots === null || rawScreenshots === undefined) {
      if (includeScreenshot) {
        throw new Error("ryn-computer-use did not return screenshots");
      }
    } else if (!Array.isArray(rawScreenshots)) {
      throw new Error("ryn-computer-use did not return screenshots");
    } else {
      screenshots = rawScreenshots.map(normalizeScreenshot);
    }
    const normWindow = normalizeWindow(Reflect.get(raw, "window"), reqWindow.app) ?? reqWindow;
    const state = {
      window: normWindow,
      screenshots,
      accessibility,
    };
    for (const shot of screenshots) {
      await bridge.emitWindowStateImage(shot, normWindow);
    }
    return state;
  },

  async click(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("click input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const clickCount = requirePositiveInt(Reflect.get(input, "click_count") ?? 1, "click_count");
    const mouseButton = Reflect.get(input, "mouse_button") ?? "left";
    if (typeof mouseButton !== "string" || !["left", "right", "middle"].includes(mouseButton)) {
      throw new TypeError("mouse_button must be left, right, or middle");
    }
    const rawElem = Reflect.get(input, "element_index") ?? Reflect.get(input, "elementIndex") ?? Reflect.get(input, "element");
    if (rawElem !== undefined && rawElem !== null) {
      const elementIndex = requireElementIndex(rawElem);
      await bridge.hostCall("click_element", {
        window: win,
        element_index: elementIndex,
        click_count: clickCount,
        mouse_button: mouseButton,
      });
      return;
    }
    const rawX = Reflect.get(input, "x");
    const rawY = Reflect.get(input, "y");
    if (rawX === undefined || rawX === null || rawY === undefined || rawY === null) {
      throw new TypeError("click requires either element_index or finite x and y coordinates");
    }
    const payload = {
      window: win,
      x: roundCoordinate(rawX, "point.x"),
      y: roundCoordinate(rawY, "point.y"),
      click_count: clickCount,
      mouse_button: mouseButton,
    };
    const screenshotId = Reflect.get(input, "screenshotId");
    if (screenshotId !== undefined && screenshotId !== null) {
      const normShotId = normalizeNonEmptyString(screenshotId);
      if (normShotId === undefined) {
        throw new TypeError("screenshotId must be a non-empty string");
      }
      payload.screenshotId = normShotId;
    }
    await bridge.hostCall("click", payload);
  },

  async scroll(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("scroll input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const payload = {
      window: win,
      x: roundCoordinate(Reflect.get(input, "x"), "scroll.x"),
      y: roundCoordinate(Reflect.get(input, "y"), "scroll.y"),
      scrollX: roundCoordinate(Reflect.get(input, "scrollX") ?? 0, "scroll.scrollX"),
      scrollY: roundCoordinate(Reflect.get(input, "scrollY") ?? 0, "scroll.scrollY"),
    };
    const screenshotId = Reflect.get(input, "screenshotId");
    if (screenshotId !== undefined && screenshotId !== null) {
      const normShotId = normalizeNonEmptyString(screenshotId);
      if (normShotId === undefined) {
        throw new TypeError("screenshotId must be a non-empty string");
      }
      payload.screenshotId = normShotId;
    }
    await bridge.hostCall("scroll", payload);
  },

  async drag(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("drag input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const payload = {
      window: win,
      from_x: roundCoordinate(Reflect.get(input, "from_x"), "from.x"),
      from_y: roundCoordinate(Reflect.get(input, "from_y"), "from.y"),
      to_x: roundCoordinate(Reflect.get(input, "to_x"), "to.x"),
      to_y: roundCoordinate(Reflect.get(input, "to_y"), "to.y"),
    };
    const screenshotId = Reflect.get(input, "screenshotId");
    if (screenshotId !== undefined && screenshotId !== null) {
      const normShotId = normalizeNonEmptyString(screenshotId);
      if (normShotId === undefined) {
        throw new TypeError("screenshotId must be a non-empty string");
      }
      payload.screenshotId = normShotId;
    }
    await bridge.hostCall("drag", payload);
  },

  async press_key(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("press_key input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const key = normalizeKeySpec(Reflect.get(input, "key"));
    await bridge.hostCall("press_key", { window: win, key });
  },

  async type_text(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("type_text input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const text = Reflect.get(input, "text");
    if (typeof text !== "string") {
      throw new TypeError("text is required");
    }
    await bridge.hostCall("type_text", { window: win, text });
  },

  async set_value(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("set_value input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const rawElem = Reflect.get(input, "element_index") ?? Reflect.get(input, "elementIndex") ?? Reflect.get(input, "element");
    const elementIndex = requireElementIndex(rawElem);
    const value = Reflect.get(input, "value");
    if (typeof value !== "string") {
      throw new TypeError("value is required");
    }
    await bridge.hostCall("set_value", { window: win, element_index: elementIndex, value });
  },

  async perform_secondary_action(input) {
    if (typeof input !== "object" || input === null) {
      throw new TypeError("perform_secondary_action input must be an object");
    }
    const win = requireWindow(Reflect.get(input, "window"));
    const rawElem = Reflect.get(input, "element_index") ?? Reflect.get(input, "elementIndex") ?? Reflect.get(input, "element");
    const elementIndex = requireElementIndex(rawElem);
    const action = normalizeNonEmptyString(Reflect.get(input, "action"));
    if (action === undefined) {
      throw new TypeError("action is required");
    }
    await bridge.hostCall("perform_secondary_action", { window: win, element_index: elementIndex, action });
  },

  async close() {
    return undefined;
  },
};

const PASSTHROUGH_PROPS = new Set([
  "then",
  "toJSON",
  "valueOf",
  "toString",
  "constructor",
  "__proto__",
]);

export const sky = new Proxy(Object.freeze(skyImpl), {
  get(target, prop, receiver) {
    if (typeof prop === "symbol" || prop in target || PASSTHROUGH_PROPS.has(prop)) {
      return Reflect.get(target, prop, receiver);
    }
    return async function unsupportedSkyMethod() {
      const err = new Error(\`Unsupported Sky method in Ryn desktop runtime: \${String(prop)}\`);
      err.code = "ryn_sky_method_unsupported";
      throw err;
    };
  },
});

export default { sky };
`;
