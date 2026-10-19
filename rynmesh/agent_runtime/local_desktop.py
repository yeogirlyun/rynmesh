"""Windows window/accessibility observation and bounded input actions."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
from ctypes import wintypes

from .store import AgentToolRejected

UIA = r"""
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root=[System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$request.window_id)
$walker=[System.Windows.Automation.TreeWalker]::ControlViewWalker
$queue=[System.Collections.Generic.Queue[System.Windows.Automation.AutomationElement]]::new()
$queue.Enqueue($root)
$items=@(); $visited=0
while($queue.Count -gt 0 -and $visited -lt 100) {
 $e=$queue.Dequeue(); $visited++
 try {
  $c=$e.Current
  $runtime=($e.GetRuntimeId() -join ',')
  if($request.action -and $runtime -eq $request.runtime_id) {
   if($c.IsPassword) {throw 'password control excluded'}
   if($request.action -eq 'invoke') {
    $p=$e.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern); $p.Invoke()
   } else {
    $p=$e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern); $p.SetValue([string]$request.text)
   }
   @{delivered=$true} | ConvertTo-Json -Compress; exit 0
  }
  if(!$c.IsOffscreen -and $items.Count -lt 40) {
   $r=$c.BoundingRectangle
   $value=''; $p=$null
   if(!$c.IsPassword -and $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$p)) {$value=$p.Current.Value}
   if(!$c.IsPassword -and !$value) {
    $p=$null
    if($e.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern,[ref]$p)) {$value=$p.DocumentRange.GetText(200)}
   }
   if($value.Length -gt 200) {$value=$value.Substring(0,200)}
   $name=[string]$c.Name; if($name.Length -gt 160) {$name=$name.Substring(0,160)}
   $items+=@{runtime_id=$runtime; name=$name; type=$c.ControlType.ProgrammaticName; automation_id=$c.AutomationId;
     password=$c.IsPassword; value=$value; x=[int]($r.X+$r.Width/2); y=[int]($r.Y+$r.Height/2)}
  }
  $child=$walker.GetFirstChild($e); $siblings=0
  while($child -and $siblings -lt 160 -and $queue.Count -lt 160) {
   $queue.Enqueue($child); $child=$walker.GetNextSibling($child); $siblings++
  }
 } catch {if($request.action) {throw}}
}
if($request.action) {throw 'element no longer available'}
@{elements=@($items); incomplete=($queue.Count -gt 0); visited=$visited} | ConvertTo-Json -Depth 6 -Compress
"""


class Desktop:
    def __init__(self):
        if os.name != "nt":
            raise AgentToolRejected("agent_computer_windows_required")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.IsWindow.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self.user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.observed = {}
        # Match physical screenshot and input coordinates on high-DPI displays.
        try:
            self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            pass

    def windows(self):
        result = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(handle, _):
            if not self.user.IsWindowVisible(handle):
                return True
            length = self.user.GetWindowTextLengthW(handle)
            if length:
                text = ctypes.create_unicode_buffer(min(length + 1, 512))
                self.user.GetWindowTextW(handle, text, len(text))
                rectangle = wintypes.RECT()
                self.user.GetWindowRect(handle, ctypes.byref(rectangle))
                pid = wintypes.DWORD()
                self.user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                result.append({"window_id": int(handle), "title": text.value, "pid": pid.value,
                               "rect": [rectangle.left, rectangle.top, rectangle.right, rectangle.bottom],
                               "foreground": handle == self.user.GetForegroundWindow()})
            return len(result) < 60

        self.user.EnumWindows(callback_type(visit), 0)
        return result

    @staticmethod
    def accessibility(request):
        payload = base64.b64encode(json.dumps(request, ensure_ascii=False).encode()).decode()
        prefix = "$request=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "')) | ConvertFrom-Json); "
        command = base64.b64encode((prefix + UIA).encode("utf-16-le")).decode()
        try:
            result = subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", command],
                                    capture_output=True, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode != 0:
                raise AgentToolRejected("agent_computer_accessibility_unavailable")
            return json.loads(result.stdout.decode("utf-8-sig"))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            raise AgentToolRejected("agent_computer_accessibility_unavailable") from None

    def window(self, ident, task_id):
        saved = self.observed.get((task_id, ident))
        actual = next((w for w in self.windows() if w["window_id"] == ident), None)
        if not saved or not actual or actual["pid"] != saved["pid"] or actual["title"] != saved["title"]:
            raise AgentToolRejected("agent_computer_window_changed_read_again")
        return actual, saved

    def preview(self, args, task_id):
        window, saved = self.window(args["window_id"], task_id)
        action = args["action"]
        if action == "click":
            rect = window["rect"]
            if "x" not in args or "y" not in args or not (rect[0] <= args["x"] < rect[2] and rect[1] <= args["y"] < rect[3]):
                raise AgentToolRejected("agent_computer_click_outside_window")
        if action in {"type", "set_value"} and "text" not in args:
            raise AgentToolRejected("agent_computer_text_required")
        result = {"window_title": window["title"], "window_pid": window["pid"], "window_rect": window["rect"]}
        if action in {"invoke", "set_value"}:
            element = next((e for e in saved.get("elements", []) if e["element_id"] == args.get("element_id")), None)
            if element is None or element.get("password"):
                raise AgentToolRejected("agent_computer_element_required")
            result["element"] = element
        if action == "key":
            if not args.get("keys"):
                raise AgentToolRejected("agent_computer_keys_required")
            self.key_codes(args["keys"])
        return result

    @staticmethod
    def key_codes(keys):
        names = {"CTRL": 17, "CONTROL": 17, "ALT": 18, "SHIFT": 16, "WIN": 91, "ENTER": 13, "TAB": 9,
                 "ESC": 27, "ESCAPE": 27, "BACKSPACE": 8, "DELETE": 46, "SPACE": 32,
                 "LEFT": 37, "UP": 38, "RIGHT": 39, "DOWN": 40, "HOME": 36, "END": 35, "PAGEUP": 33, "PAGEDOWN": 34}
        result = []
        for value in keys:
            key = value.upper()
            if key in names:
                result.append(names[key])
            elif len(key) == 1 and key.isascii() and key.isalnum():
                result.append(ord(key))
            elif key.startswith("F") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
                result.append(111 + int(key[1:]))
            else:
                raise AgentToolRejected("agent_computer_key_unsupported")
        return result

    def keyboard(self, code, flags=0):
        class Keyboard(ctypes.Structure):
            _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Mouse(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]
        class Union(ctypes.Union):
            _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]
        class Input(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("value", Union)]
        item = Input(type=1, value=Union(keyboard=Keyboard(vk=0 if flags & 4 else code, scan=code if flags & 4 else 0, flags=flags)))
        if self.user.SendInput(1, ctypes.byref(item), ctypes.sizeof(item)) != 1:
            raise AgentToolRejected("agent_computer_input_blocked")

    def execute(self, name, args, task_id, resolve, cancelled):
        if name == "ryn_computer_read":
            windows = self.windows()
            for window in windows:
                self.observed[(task_id, window["window_id"])] = window
            result = {"windows": windows}
            if ident := args.get("window_id"):
                window, _ = self.window(ident, task_id)
                try:
                    tree = self.accessibility({"window_id": ident})
                    elements = [{"element_id": i, **e} for i, e in enumerate(tree["elements"])]
                    self.observed[(task_id, ident)] = {**window, "elements": elements}
                    result.update(elements=elements, incomplete=tree["incomplete"])
                except AgentToolRejected:
                    result["accessibility_error"] = "No usable accessibility tree; do not invent controls."
            if args.get("screenshot_path"):
                from PIL import ImageGrab
                path = resolve(args["screenshot_path"])
                if path.suffix.lower() != ".png" or path.exists():
                    raise AgentToolRejected("agent_screenshot_requires_new_png")
                image = ImageGrab.grab(all_screens=True)
                with path.open("xb") as stream:
                    image.save(stream, format="PNG")
                result.update(screenshot_path=str(path), screenshot_size=list(image.size))
            return result
        preview = self.preview(args, task_id)
        ident, action = args["window_id"], args["action"]
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        if action not in {"invoke", "set_value"}:
            self.user.ShowWindow(ident, 9)
            self.user.SetForegroundWindow(ident)
            if self.user.GetForegroundWindow() != ident:
                raise AgentToolRejected("agent_computer_focus_blocked")
        if action == "click":
            self.user.SetCursorPos(args["x"], args["y"])
            self.user.mouse_event(2, 0, 0, 0, 0)
            self.user.mouse_event(4, 0, 0, 0, 0)
        elif action == "type":
            raw = args["text"].encode("utf-16-le")
            for i in range(0, len(raw), 2):
                if cancelled():
                    raise AgentToolRejected("agent_cancelled")
                code = int.from_bytes(raw[i:i + 2], "little")
                self.keyboard(code, 4)
                self.keyboard(code, 4 | 2)
        elif action == "key":
            codes = self.key_codes(args["keys"])
            pressed = []
            try:
                for code in codes:
                    self.keyboard(code)
                    pressed.append(code)
            finally:
                for code in reversed(pressed):
                    self.keyboard(code, 2)
        elif action == "scroll":
            rect = preview["window_rect"]
            self.user.SetCursorPos((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
            self.user.mouse_event(0x800, 0, 0, args.get("delta_y", -3) * 120, 0)
        elif action in {"invoke", "set_value"}:
            self.accessibility({"window_id": ident, "action": action, "runtime_id": preview["element"]["runtime_id"], "text": args.get("text", "")})
        return {"window_id": ident, "action_delivered": action, "next_step": "Read the actual window state again to verify the effect."}
