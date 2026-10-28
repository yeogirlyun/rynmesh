"""Read-only visual observation of workspace files for text-only models.

The file is rendered in a throwaway headless browser profile. Every request is
intercepted: only the inspected file itself is served (under a reserved, never
resolved origin, with a CSP that blocks page scripts); any other URL fails and
is reported as a self-containment problem. The model never receives pixels, so
each sampled frame is summarized as a coarse color map, element geometry in the
file's own coordinates, motion and loop-seam measurements and render errors.
"""
from __future__ import annotations

import base64
import colorsys
import io
import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx

from .store import AgentToolRejected

MAX_RENDER_BYTES = 8 * 1024 * 1024
MAX_TIMES = 8
MAX_TRACK = 24
GRID_COLUMNS = 56
ORIGIN = "https://render.ryn.invalid/"
MEDIA_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp", ".html": "text/html", ".htm": "text/html"}
CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data: " + ORIGIN + "; font-src data:"
# Hue classes for saturated colors; pale variants use the lowercase letter.
HUES = ((15, "R", "red"), (40, "O", "orange"), (70, "Y", "yellow"), (170, "G", "green"), (200, "C", "cyan"),
        (255, "B", "blue"), (290, "P", "purple"), (345, "M", "pink/magenta"), (361, "R", "red"))
NAMES = {"K": "black/very dark", "W": "white", "g": "gray", "N": "brown", "T": "tan/beige"}

INSPECT = r"""(() => {
 const root = document.documentElement, svg = root instanceof SVGSVGElement ? root : null;
 const out = {kind: svg ? 'svg' : (document.contentType || '').startsWith('image/') ? 'image' : 'html',
   parse_error: !!document.querySelector('parsererror') || (svg === null && root.namespaceURI === 'http://www.w3.org/1999/xhtml' && /This page contains the following errors/.test(document.body?.innerText || '')),
   scripts_present: document.querySelectorAll('script').length + document.querySelectorAll('[onload],[onclick],[onbegin],[onend]').length};
 // Resources the file depends on outside itself (links are navigation, not dependencies).
 const refs = new Set(), local = v => /^\s*(#|data:)/i.test(v);
 document.querySelectorAll('*').forEach(el => {
   if (el.tagName.toLowerCase() === 'a') return;
   for (const v of [el.getAttribute('href'), el.getAttribute('src'), el.getAttributeNS && el.getAttributeNS('http://www.w3.org/1999/xlink', 'href')])
     if (v && !local(v)) refs.add(v.trim().slice(0, 160));
 });
 const css = [...document.querySelectorAll('style')].map(s => s.textContent).join('\n') + '\n' + [...document.querySelectorAll('[style]')].map(e => e.getAttribute('style')).join('\n');
 for (const m of css.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)|@import\s+['"]([^'"]+)['"]/g)) { const v = (m[1] || m[2]).trim(); if (!local(v)) refs.add(v.slice(0, 160)); }
 out.external_references = [...refs].slice(0, 12);
 if (svg) {
   const vb = svg.viewBox && svg.viewBox.baseVal;
   out.viewbox = vb && vb.width ? [vb.x, vb.y, vb.width, vb.height] : null;
   out.size = [svg.width.baseVal.value || 0, svg.height.baseVal.value || 0];
 }
 return out;
})()"""

ANIMATIONS = r"""(() => {
 const label = el => {
   if (!el || !el.tagName) return '?';
   let text = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '');
   const cls = el.getAttribute && el.getAttribute('class'); if (cls && !el.id) text += '.' + cls.trim().split(/\s+/)[0];
   if (!el.id) { const paint = el.getAttribute('fill') || el.getAttribute('stroke') || el.parentElement?.getAttribute('stroke') || el.parentElement?.getAttribute('fill');
     if (paint && paint !== 'none') text += '(' + paint + ')'; }
   const named = el.getAttribute && (el.getAttribute('aria-label') || el.querySelector?.(':scope > title')?.textContent);
   let note = named || '';
   if (!note) { let p = el.previousSibling; while (p && p.nodeType === 3 && !p.textContent.trim()) p = p.previousSibling;
     if (p && p.nodeType === 8) note = p.textContent.trim(); }
   return note ? text + ' "' + note.replace(/\s+/g, ' ').slice(0, 56) + '"' : text;
 };
 window.__rynLabel = label;
 const smil = [...document.querySelectorAll('animate,animateTransform,animateMotion,animateColor,set')].slice(0, 60).map(a => {
   const href = a.getAttribute('href') || a.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
   const target = href ? document.getElementById(href.replace(/^#/, '')) : a.parentElement;
   const values = a.getAttribute('values');
   return {tag: a.tagName, target: target ? label(target) : 'missing ' + href, target_missing: !target,
     in_defs: !!(target && target.closest && target.closest('defs,symbol')),
     attribute: a.getAttribute('attributeName') || (a.tagName === 'animateMotion' ? 'motion' : ''),
     type: a.getAttribute('type') || '', dur: a.getAttribute('dur') || '', repeat: a.getAttribute('repeatCount') || a.getAttribute('repeatDur') || '1',
     begin: a.getAttribute('begin') || '', values: values ? values.replace(/\s+/g, ' ').slice(0, 120) : ((a.getAttribute('from') || '') + ' -> ' + (a.getAttribute('to') || a.getAttribute('by') || '')),
     key_times: !!a.getAttribute('keyTimes'), calc_mode: a.getAttribute('calcMode') || ''};
 });
 const css = document.getAnimations().slice(0, 40).map(a => {
   const timing = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {};
   return {target: label(a.effect && a.effect.target), name: a.animationName || a.id || '', duration_ms: timing.duration,
     iterations: timing.iterations === Infinity ? 'infinite' : timing.iterations, delay_ms: timing.delay, easing: timing.easing};
 });
 return {smil, css};
})()"""

SEEK = r"""(async (t) => {
 const svgs = document.documentElement instanceof SVGSVGElement ? [document.documentElement] : [...document.querySelectorAll('svg')];
 svgs.forEach(s => { if (s.pauseAnimations) { s.pauseAnimations(); s.setCurrentTime(t); } });
 document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; });
 await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
 return true;
})"""

TRACK = r"""((selectors, cap) => {
 const root = document.documentElement, svg = root instanceof SVGSVGElement ? root : null;
 const label = window.__rynLabel || (el => el.tagName.toLowerCase());
 const hidden = 'defs,symbol,clipPath,mask,pattern,linearGradient,radialGradient,filter,marker';
 const graphic = 'g,path,circle,ellipse,rect,line,polyline,polygon,use,image,text';
 const picked = [], seen = new Set(), notes = [];
 const add = (el, why) => { if (el && !seen.has(el) && picked.length < cap) { seen.add(el); picked.push([el, why]); } };
 for (const s of selectors) { try { const el = document.querySelector(s); if (el) add(el, 'requested'); else notes.push('no element matches ' + s); } catch (e) { notes.push('invalid selector ' + s); } }
 const targets = [...document.querySelectorAll('animate,animateTransform,animateMotion,animateColor,set')].map(a => {
   const href = a.getAttribute('href') || a.getAttributeNS('http://www.w3.org/1999/xlink', 'href');
   return href ? document.getElementById(href.replace(/^#/, '')) : a.parentElement; }).filter(Boolean);
 for (const el of targets) {
   if (el.closest(hidden) && el.id) document.querySelectorAll('use').forEach(u => {
     const h = u.getAttribute('href') || u.getAttributeNS('http://www.w3.org/1999/xlink', 'href'); if (h === '#' + el.id) add(u, 'uses animated #' + el.id); });
   add(el, 'animated');
 }
 document.getAnimations().forEach(a => a.effect && a.effect.target && add(a.effect.target, 'css animated'));
 if (svg) {
   document.querySelectorAll(graphic).forEach(el => { if (el.id && !el.closest(hidden)) add(el, 'id'); });
   document.querySelectorAll(graphic).forEach(el => { if (el.closest(hidden)) return; let p = el.previousSibling;
     while (p && p.nodeType === 3 && !p.textContent.trim()) p = p.previousSibling; if (p && p.nodeType === 8) add(el, 'commented'); });
 } else document.querySelectorAll('[id]').forEach(el => add(el, 'id'));
 window.__rynTracked = picked.map(p => p[0]);
 const seenLabels = {};
 return {elements: picked.map(([el, why]) => {
   let text = label(el); seenLabels[text] = (seenLabels[text] || 0) + 1;
   if (seenLabels[text] > 1) text += ' [' + seenLabels[text] + ']';
   // Tracked elements that contain this one: a container is never this element's contact partner.
   const within = picked.map((p, i) => p[0] !== el && p[0].contains(el) ? i : -1).filter(i => i >= 0);
   return {label: text, why, in_defs: !!el.closest(hidden), within};
 }), notes};
})"""

MEASURE = r"""(() => {
 const root = document.documentElement, svg = root instanceof SVGSVGElement ? root : null;
 const inv = svg && svg.getScreenCTM() ? svg.getScreenCTM().inverse() : null;
 const toUser = (x, y) => { if (!inv) return [x, y]; const p = new DOMPoint(x, y).matrixTransform(inv); return [p.x, p.y]; };
 const r1 = v => Math.round(v * 10) / 10;
 return (window.__rynTracked || []).map(el => {
   const item = {};
   const b = el.getBoundingClientRect();
   if (b.width || b.height) {
     const a = toUser(b.left, b.top), c = toUser(b.right, b.bottom);
     item.box = [r1(Math.min(a[0], c[0])), r1(Math.min(a[1], c[1])), r1(Math.abs(c[0] - a[0])), r1(Math.abs(c[1] - a[1]))];
     item.px = [b.left, b.top, b.width, b.height];
   }
   if (el.transform && el.transform.animVal && el.transform.animVal.numberOfItems) {
     let m = new DOMMatrix();
     for (let i = 0; i < el.transform.animVal.numberOfItems; i++) { const s = el.transform.animVal.getItem(i).matrix; m = m.multiply(new DOMMatrix([s.a, s.b, s.c, s.d, s.e, s.f])); }
     item.rot = r1(Math.atan2(m.b, m.a) * 180 / Math.PI);
   }
   if (el.getScreenCTM && !el.closest('defs,symbol')) {
     // Element space -> screen -> root user units, the same coordinates as the boxes.
     const m = el.getScreenCTM();
     if (m) {
       const point = (x, y) => { const s = new DOMPoint(x, y).matrixTransform(m); const p = toUser(s.x, s.y); return [r1(p[0]), r1(p[1])]; };
       if (el.tagName === 'line') item.ends = [point(el.x1.animVal.value, el.y1.animVal.value), point(el.x2.animVal.value, el.y2.animVal.value)];
       else if (el.tagName === 'polyline' && el.animatedPoints && el.animatedPoints.numberOfItems > 1) {
         const pts = el.animatedPoints; item.ends = [point(pts.getItem(0).x, pts.getItem(0).y), point(pts.getItem(pts.numberOfItems - 1).x, pts.getItem(pts.numberOfItems - 1).y)];
       } else if (el.tagName === 'path' && el.getTotalLength) {
         try { const len = el.getTotalLength(); if (len > 0) { const s = el.getPointAtLength(0), e = el.getPointAtLength(len); item.ends = [point(s.x, s.y), point(e.x, e.y)]; } } catch (e) {}
       }
     }
   }
   return item;
 });
})()"""


def _duration(value):
    """SMIL clock value in seconds, or None."""
    value = (value or "").strip()
    match = re.fullmatch(r"([\d.]+)\s*(ms|s|min|h)?", value)
    if match:
        number = float(match.group(1))
        return number * {"ms": 0.001, "s": 1, "min": 60, "h": 3600, None: 1}[match.group(2)]
    match = re.fullmatch(r"(?:(\d+):)?(\d+):([\d.]+)", value)
    if match:
        return int(match.group(1) or 0) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    return None


def loop_period(animations):
    """Shortest time after which every repeating animation is back at its start (bounded)."""
    durations = []
    for item in animations.get("smil", []):
        seconds = _duration(item.get("dur"))
        if seconds and seconds > 0:
            durations.append(seconds)
    for item in animations.get("css", []):
        if isinstance(item.get("duration_ms"), (int, float)) and item["duration_ms"] > 0:
            durations.append(item["duration_ms"] / 1000)
    if not durations:
        return None
    millis = [max(1, round(value * 1000)) for value in durations]
    period = millis[0]
    for value in millis[1:]:
        period = period * value // math.gcd(period, value)
        if period > 120000:
            return round(max(millis) / 1000, 3)
    return round(period / 1000, 3)


def classify(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < 0.2:
        return "K"
    if s < 0.12:
        return "W" if v > 0.86 else "g"
    hue = h * 360
    if 15 <= hue < 50 and v < 0.6:
        return "N"
    if 20 <= hue < 55 and s < 0.45 and v >= 0.6:
        return "T"
    letter = next(code for limit, code, _ in HUES if hue < limit)
    return letter.lower() if s < 0.35 and v > 0.8 else letter


def legend_name(code):
    if code in NAMES:
        return NAMES[code]
    name = next(label for _, letter, label in HUES if letter == code.upper())
    return ("pale " if code.islower() else "") + name


def color_grid(image, columns=GRID_COLUMNS, samples=6):
    """Coarse text picture: one character per cell, thin strokes favored over fill.

    Cells are sampled without blending (nearest pixel), so a dark stroke over a light
    background stays dark instead of averaging into a third color. Rows are about half as
    many as a square grid would need because text characters are roughly twice as tall as wide.
    """
    from PIL import Image
    width, height = image.size
    rows = max(1, round(columns * height / width * 0.5))
    small = image.convert("RGB").resize((columns * samples, rows * samples), Image.Resampling.NEAREST)
    pixels = small.load()
    classes = [[classify(*pixels[x, y]) for x in range(columns * samples)] for y in range(rows * samples)]
    totals = Counter(code for row in classes for code in row)
    dominant = totals.most_common(1)[0][0]
    lines, used = [], Counter()
    for row in range(rows):
        line = []
        for column in range(columns):
            cell = Counter(classes[y][x] for y in range(row * samples, (row + 1) * samples)
                           for x in range(column * samples, (column + 1) * samples))
            minority = [(count, code) for code, count in cell.items() if code != dominant]
            count, code = max(minority) if minority else (0, dominant)
            chosen = code if count >= samples * samples // 6 else dominant
            used[chosen] += 1
            line.append("." if chosen == dominant else chosen)
        lines.append("".join(line))
    legend = {".": "most common color: " + legend_name(dominant)}
    legend.update({code: legend_name(code) for code, _ in used.most_common() if code != dominant})
    return lines, legend


def changed_fraction(first, second, region=None):
    from PIL import ImageChops
    a, b = first.convert("RGB"), second.convert("RGB")
    if region:
        a, b = a.crop(region), b.crop(region)
    if a.size[0] < 1 or a.size[1] < 1:
        return 0.0, None
    mask = ImageChops.difference(a, b).convert("L").point(lambda value: 255 if value > 24 else 0)
    changed = mask.histogram()[255]
    return changed / (a.size[0] * a.size[1]), mask.getbbox()


def _distance(point, box):
    x, y = point
    dx = max(box[0] - x, 0, x - (box[0] + box[2]))
    dy = max(box[1] - y, 0, y - (box[1] + box[3]))
    return math.hypot(dx, dy)


def _box_gap(first, second):
    dx = max(first[0] - (second[0] + second[2]), 0, second[0] - (first[0] + first[2]))
    dy = max(first[1] - (second[1] + second[3]), 0, second[1] - (first[1] + first[3]))
    return math.hypot(dx, dy)


def attachments(elements, frames, view_area):
    """Moving joints and small moving parts: does each stay in contact with its partner in every frame?

    Contact is measured joint to joint, never against the bounding box of a long diagonal rod
    (a foot inside a shin's box can still be far from the shin's end). Lines and open paths
    connect at their ends; small shapes (feet, pedals, hands, handles) by their outlines' boxes.
    Large parts are not partners, so a body or group that merely contains a point proves nothing.
    """
    def series(index, key):
        return [frame[index].get(key) if index < len(frame) else None for frame in frames]

    small = lambda box: box is not None and box[2] * box[3] <= 0.03 * view_area  # noqa: E731
    kinds = {}
    for index, element in enumerate(elements):
        ends, boxes = series(index, "ends"), series(index, "box")
        if element.get("in_defs") or not frames:
            continue
        if all(ends) and any(tuple(end[0]) != tuple(end[1]) for end in ends):
            kinds[index] = "line"
        elif all(small(box) for box in boxes):
            kinds[index] = "part"
    found = []
    for index, kind in kinds.items():
        if kind == "line":
            joints = [(name, [("point", ends[side]) for ends in series(index, "ends")]) for side, name in ((0, "start"), (1, "end"))]
        else:
            joints = [("", [("box", box) for box in series(index, "box")])]
        for name, shapes in joints:
            if len({tuple(value) for _, value in shapes}) == 1:
                continue  # only moving joints say something about the animation
            best = None
            for other, partner_kind in kinds.items():
                if other == index or other in elements[index].get("within", ()) or index in elements[other].get("within", ()):
                    continue
                distances = []
                for (shape, value), frame in zip(shapes, frames, strict=True):
                    partner = frame[other]
                    if partner_kind == "line":
                        targets = partner["ends"]
                        distance = min(math.dist(value, end) if shape == "point" else _distance(end, value) for end in targets)
                    else:
                        distance = _distance(value, partner["box"]) if shape == "point" else _box_gap(value, partner["box"])
                    distances.append(round(distance, 1))
                if min(distances) > 12:
                    continue
                area = 0 if partner_kind == "line" else frames[0][other]["box"][2] * frames[0][other]["box"][3]
                rank = (sorted(distances)[len(distances) // 2], area)
                if best is None or rank < best[0]:
                    best = (rank, elements[other]["label"] + (" end" if partner_kind == "line" else ""), distances)
            if best:
                holds = max(best[2]) <= 6
                found.append(f"{elements[index]['label']}{(' ' + name) if name else ''} -> {best[1]}: distance per frame "
                             + ",".join(str(_number(value)) for value in best[2])
                             + (" (contact holds)" if holds else " (contact breaks)"))
    return found[:16]


class Renderer:
    def __init__(self, home):
        self.home = Path(home) if home else None
        self.lock = threading.Lock()

    def _start(self):
        from ..llm_package.process_guard import _windows_job
        from .local_browser import find_browser
        executable = find_browser()
        if not executable:
            raise AgentToolRejected("agent_render_browser_unavailable")
        base = (self.home / "agent" / "render") if self.home else Path.cwd() / ".ryn-render"
        profile = base / uuid.uuid4().hex
        profile.mkdir(parents=True)
        command = [executable, "--headless=new", "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
                   "--user-data-dir=" + str(profile), "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                   "--disable-sync", "--disable-background-networking", "--hide-scrollbars", "--mute-audio",
                   "--force-device-scale-factor=1", "--disable-gpu", "about:blank"]
        flags = 0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                   creationflags=flags, start_new_session=not flags)
        if flags:
            try:
                process._ryn_job = _windows_job(process)
            except BaseException:
                process.kill()
                raise
        return process, profile

    @staticmethod
    def _stop(process, profile):
        from ..llm_package.process_guard import terminate
        try:
            terminate(process)
        except Exception:
            process.kill()
        for _ in range(20):
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.1)

    def inspect(self, path, raw, *, times=None, track=None, width=None, cancelled=lambda: False):
        suffix = path.suffix.lower()
        if suffix not in MEDIA_TYPES:
            raise AgentToolRejected("agent_render_unsupported_type")
        if len(raw) > MAX_RENDER_BYTES:
            raise AgentToolRejected("agent_render_file_too_large")
        with self.lock:
            process, profile = self._start()
            try:
                return self._inspect(process, profile, path, raw, suffix, times, track or [], width, cancelled)
            finally:
                self._stop(process, profile)

    def _inspect(self, process, profile, path, raw, suffix, times, track, width, cancelled):
        from PIL import Image
        from websockets.sync.client import connect
        # Generous bounds: a busy or virtual machine can take many seconds to start a browser.
        deadline = time.monotonic() + 90
        port_file = profile / "DevToolsActivePort"
        port = None
        while port is None:
            try:
                # The browser may still be writing the file: only a complete one (port line and
                # browser endpoint line) is trusted, so a truncated port number is never used.
                lines = port_file.read_text(encoding="ascii").splitlines()
                port = int(lines[0]) if len(lines) >= 2 and lines[0].isdigit() and lines[1].startswith("/devtools/") else None
            except (OSError, ValueError):
                port = None
            if port is not None:
                break
            if process.poll() is not None or time.monotonic() > deadline - 45:
                raise AgentToolRejected("agent_render_browser_start_failed")
            if cancelled():
                raise AgentToolRejected("agent_cancelled")
            time.sleep(0.05)
        target = None
        with httpx.Client(trust_env=False, timeout=10) as client:
            while target is None:
                try:
                    target = next((item for item in client.get(f"http://127.0.0.1:{port}/json/list").json()
                                   if item.get("type") == "page"), None)
                except (httpx.HTTPError, ValueError):
                    target = None
                if target is None:
                    if process.poll() is not None or time.monotonic() > deadline - 30:
                        raise AgentToolRejected("agent_render_browser_start_failed")
                    if cancelled():
                        raise AgentToolRejected("agent_cancelled")
                    time.sleep(0.2)
        url = ORIGIN + "file" + suffix
        blocked, counter = [], [0]
        socket = None
        while socket is None:
            try:
                socket = connect(target["webSocketDebuggerUrl"], open_timeout=15, max_size=64 * 1024 * 1024, proxy=None)
            except (OSError, TimeoutError):
                # A loaded machine can miss one handshake; the overall deadline still bounds the wait.
                if process.poll() is not None or time.monotonic() > deadline - 30:
                    raise AgentToolRejected("agent_render_browser_start_failed") from None
                if cancelled():
                    raise AgentToolRejected("agent_cancelled") from None
        with socket:
            def send(method, params=None):
                counter[0] += 1
                socket.send(json.dumps({"id": counter[0], "method": method, "params": params or {}}))
                return counter[0]

            def handle(message):
                if message.get("method") != "Fetch.requestPaused":
                    return
                params = message["params"]
                if params["request"]["url"].split("#")[0] == url:
                    send("Fetch.fulfillRequest", {"requestId": params["requestId"], "responseCode": 200, "responseHeaders": [
                        {"name": "Content-Type", "value": MEDIA_TYPES[suffix]}, {"name": "Content-Security-Policy", "value": CSP},
                        {"name": "X-Content-Type-Options", "value": "nosniff"}], "body": base64.b64encode(raw).decode()})
                else:
                    # The browser asks for a favicon by itself; that is not a reference in the file.
                    if params["request"]["url"] != ORIGIN + "favicon.ico" and len(blocked) < 12:
                        blocked.append(params["request"]["url"][:160])
                    send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"})

            def call(method, params=None):
                ident = send(method, params)
                while True:
                    if cancelled():
                        raise AgentToolRejected("agent_cancelled")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AgentToolRejected("agent_render_timeout")
                    try:
                        message = json.loads(socket.recv(timeout=min(remaining, 5)))
                    except TimeoutError:
                        continue
                    if message.get("id") == ident:
                        if "error" in message:
                            raise AgentToolRejected("agent_render_protocol_error")
                        return message.get("result", {})
                    handle(message)

            def evaluate(expression, *args):
                if args:
                    expression = "(" + expression + ")(" + ",".join(json.dumps(arg) for arg in args) + ")"
                result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
                if result.get("exceptionDetails"):
                    raise AgentToolRejected("agent_render_inspection_failed")
                return result.get("result", {}).get("value")

            call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})
            view_width = int(width or 0) or 800
            call("Emulation.setDeviceMetricsOverride", {"width": view_width, "height": 600, "deviceScaleFactor": 1, "mobile": False})
            call("Page.navigate", {"url": url})
            while evaluate("document.readyState") != "complete":
                time.sleep(0.05)
            info = evaluate(INSPECT)
            # Size the viewport to the drawing so the whole frame is observed at a known scale.
            if info.get("kind") == "svg":
                box = info.get("viewbox") or ([0, 0, *info["size"]] if all(info.get("size") or [0]) else None)
                aspect = (box[3] / box[2]) if box and box[2] > 0 and box[3] > 0 else 0.625
                view_width = int(width or (info["size"][0] if info.get("size") and info["size"][0] else 800))
                view_width = max(160, min(1600, view_width))
                view_height = max(120, min(1600, round(view_width * aspect)))
                evaluate("(w,h)=>{const r=document.documentElement; r.setAttribute('width',w); r.setAttribute('height',h); r.style.display='block'; return true;}", view_width, view_height)
            else:
                view_height = 800 if info.get("kind") == "html" else 600
                view_width = max(160, min(1600, int(width or (1280 if info.get("kind") == "html" else 800))))
            call("Emulation.setDeviceMetricsOverride", {"width": view_width, "height": view_height, "deviceScaleFactor": 1, "mobile": False})
            animations = evaluate(ANIMATIONS)
            period = loop_period(animations)
            if times:
                samples = sorted({round(min(max(float(t), 0.0), 3600.0), 3) for t in times})[:MAX_TIMES]
            elif period:
                # Sixths, not quarters: 4- and 8-fold symmetric parts look identical at quarter turns.
                samples = [round(period * k / 6, 3) for k in range(6)]
            else:
                samples = [0.0]
            tracked = evaluate(TRACK, list(track), MAX_TRACK)
            frames, images = [], []
            for moment in samples:
                evaluate(SEEK, moment)
                shot = call("Page.captureScreenshot", {"format": "png"})
                images.append(Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB"))
                frames.append(evaluate(MEASURE))
            seam = None
            if period:
                # A repeating animation restarts exactly at the period, so t=period always equals t=0.
                # The seam is the jump from the loop's last moment back to its start, compared with
                # an ordinary step of the same length just before it.
                step = min(0.02, period / 100)

                def capture(moment):
                    evaluate(SEEK, moment)
                    return Image.open(io.BytesIO(base64.b64decode(call("Page.captureScreenshot", {"format": "png"})["data"]))).convert("RGB")
                before_end, end = capture(round(period - 2 * step, 4)), capture(round(period - step, 4))
                start = images[0] if samples[0] == 0 else capture(0)
                jump, _ = changed_fraction(end, start)
                ordinary, _ = changed_fraction(before_end, end)
                seam = {"compared": f"t={_number(round(period - step, 4))}s -> t=0 (wrap) against one ordinary {step}s step",
                        "wrap_changed_pixel_fraction": round(jump, 4), "ordinary_step_changed_pixel_fraction": round(ordinary, 4),
                        "seamless": jump <= max(3 * ordinary, 0.003)}
        return self._report(path, info, animations, period, samples, tracked, frames, images, seam, blocked, view_width, view_height)

    @staticmethod
    def _report(path, info, animations, period, samples, tracked, frames, images, seam, blocked, view_width, view_height):
        notes = list(tracked.get("notes", []))
        elements = tracked.get("elements", [])
        view_box = info.get("viewbox") or [0, 0, view_width, view_height]
        view_area = view_box[2] * view_box[3]
        rendered, legend, first_grid, previous = [], {}, None, None
        for moment, image in zip(samples, images, strict=True):
            grid, used = color_grid(image)
            legend.update(used)
            frame = {"t": moment}
            if first_grid is None or sum(a != b for a, b in zip(grid, first_grid, strict=False)) > len(grid) // 2:
                frame["color_map"] = grid
            else:
                # Later frames list only the rows that differ from the first frame.
                frame["rows_changed_from_first_frame"] = {str(row): line for row, (line, base) in enumerate(zip(grid, first_grid, strict=False)) if line != base}
            if previous is not None:
                fraction, box = changed_fraction(previous, image)
                frame["pixels_changed_vs_previous"] = round(fraction, 4)
                if box:
                    frame["changed_region_px"] = list(box)
            first_grid = first_grid or grid
            rendered.append(frame)
            previous = image
        blank = all(set(row) == {"."} for row in first_grid or ["."]) and all(
            not frame.get("rows_changed_from_first_frame") and "color_map" not in frame for frame in rendered[1:])
        report_elements = []
        for index, element in enumerate(elements):
            series = [frame[index] if index < len(frame) else {} for frame in frames]
            described = [_describe(item) for item in series]
            entry = {"element": element["label"] + (" [in defs]" if element.get("in_defs") else ""), "tracked_because": element["why"]}
            if len(set(described)) == 1:
                entry["all_sampled_frames"] = described[0] or "no rendered box"
            else:
                entry["by_frame"] = [f"t={_number(moment)}: {text or 'no rendered box'}" for moment, text in zip(samples, described, strict=True)]
            boxes = [item.get("px") for item in series if item.get("px")]
            if len(boxes) == len(series) and len(images) > 1:
                left = max(0, int(min(box[0] for box in boxes)))
                top = max(0, int(min(box[1] for box in boxes)))
                right = min(images[0].size[0], int(max(box[0] + box[2] for box in boxes)) + 1)
                bottom = min(images[0].size[1], int(max(box[1] + box[3] for box in boxes)) + 1)
                if right > left and bottom > top:
                    change = max(changed_fraction(images[i - 1], images[i], (left, top, right, bottom))[0] for i in range(1, len(images)))
                    entry["max_pixels_changed_in_its_area"] = round(change, 4)
                    rotating = len({item.get("rot") for item in series}) > 1
                    if "animated" in element["why"] and change < 0.002 and not rotating:
                        notes.append(f"{element['label']} is animated but shows no visible change across the sampled frames.")
            outside = [moment for moment, item in zip(samples, series, strict=True) if item.get("box") and not element.get("in_defs")
                       and _outside(item["box"], view_box)]
            if outside:
                notes.append(f"{element['label']} lies partly or wholly outside the visible area in {len(outside)} of "
                             f"{len(samples)} sampled frames (first at t={_number(outside[0])}).")
            report_elements.append(entry)
        for item in animations.get("smil", []):
            if item.get("target_missing"):
                notes.append(f"{item['tag']} targets {item['target']}, which does not exist.")
        if seam and not seam["seamless"]:
            notes.append(f"Loop seam: wrapping from the end of the {_number(period)}s loop back to t=0 changes "
                         f"{seam['wrap_changed_pixel_fraction']:.1%} of pixels, versus {seam['ordinary_step_changed_pixel_fraction']:.1%} "
                         "for an ordinary step of the same length: the loop visibly jumps.")
        if blocked or info.get("external_references"):
            notes.append("Not self-contained: the file references external resources, which were not loaded here "
                         "and will be missing or blocked when the file is opened elsewhere.")
        if info.get("scripts_present"):
            notes.append("The file contains scripts or event handlers; they were not executed, and Ryn's preview does not run them either.")
        if info.get("parse_error"):
            notes.append("The browser reported a parse error; the drawing may be incomplete.")
        if blank:
            notes.append("Every sampled frame is a single color: nothing visible was drawn.")
        smil = [f"{item['tag']} {item['attribute']}{(' ' + item['type']) if item['type'] else ''} on {item['target']}"
                f"{' [in defs]' if item['in_defs'] else ''}: {item['values']} dur={item['dur'] or '?'} repeat={item['repeat']}"
                f"{(' begin=' + item['begin']) if item['begin'] else ''}{' keyTimes' if item['key_times'] else ''}"
                for item in animations.get("smil", [])[:24]]
        css = [f"{item['name'] or 'animation'} on {item['target']}: {item['duration_ms']}ms x{item['iterations']}"
               for item in animations.get("css", [])[:16]]
        return {
            "path": str(path), "media_type": MEDIA_TYPES[path.suffix.lower()],
            "render": {"kind": info.get("kind"), "viewport_px": [view_width, view_height], "view_box": view_box,
                       "user_units_per_px": round(view_box[2] / view_width, 4) if view_width else None,
                       "parse_error": bool(info.get("parse_error")), "scripts_present": info.get("scripts_present", 0),
                       "scripts_executed": False, "blocked_requests": blocked,
                       "external_references": info.get("external_references", [])},
            "animation": {"smil": smil, "css": css, "loop_period_seconds": period, "sampled_seconds": samples, "loop_seam": seam},
            "color_legend": legend,
            "frames": rendered,
            "elements": report_elements,
            "attachments": attachments(elements, frames, view_area),
            "findings": notes[:24],
            "coverage": ("Rendered in an isolated headless browser with scripts and external requests blocked. "
                         "You did not see the image: color_map is a coarse one-character-per-cell picture (rows top to bottom), "
                         "later frames list only rows that differ from the first. Geometry is in the file's own coordinates "
                         "(SVG user units) from the browser's layout: box x,y wxh, rot degrees, ends = path/line start->end. "
                         "attachments give the distance from a moving endpoint to the nearest small part in each frame. "
                         "Judge appearance from these measurements and say what remains unobserved."),
        }


def _number(value):
    return int(value) if float(value).is_integer() else round(value, 3)


def _describe(item):
    parts = []
    if item.get("box"):
        x, y, w, h = (_number(round(v)) for v in item["box"])
        parts.append(f"box {x},{y} {w}x{h}")
    if item.get("rot") is not None:
        parts.append(f"rot {_number(item['rot'])}")
    if item.get("ends"):
        (x0, y0), (x1, y1) = item["ends"]
        parts.append(f"ends {_number(round(x0))},{_number(round(y0))}->{_number(round(x1))},{_number(round(y1))}")
    return "; ".join(parts)


def _outside(box, view):
    return (box[0] < view[0] - 1 or box[1] < view[1] - 1 or box[0] + box[2] > view[0] + view[2] + 1
            or box[1] + box[3] > view[1] + view[3] + 1)
