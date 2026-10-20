"""Non-activating Windows desktop activity indicator, owned by the Ryn peer.

This is a display projection only: it never injects input or grants permission.
The transparent window is click-through, excluded from capture, and follows
the real task lifecycle (including pauses and errors).
"""
from __future__ import annotations

import os
import threading


class ComputerOverlay:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}
        self._thread = None
        self._closed = threading.Event()
        self.error = None

    def touch(self, task_id, phase="正在查看屏幕"):
        if os.name != "nt" or not task_id or self._closed.is_set():
            return
        with self._lock:
            self._tasks[task_id] = phase
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="ryn-computer-indicator", daemon=True)
                self._thread.start()

    def sync(self, states):
        with self._lock:
            for task_id in list(self._tasks):
                state = states.get(task_id) or {}
                status = state.get("status")
                if status not in {"running", "queued"} or state.get("pending_control"):
                    self._tasks.pop(task_id, None)
                elif state.get("kind") == "model":
                    self._tasks[task_id] = "正在思考下一步"

    def forget(self, task_id):
        with self._lock:
            self._tasks.pop(task_id, None)

    def close(self):
        self._closed.set()
        with self._lock:
            self._tasks.clear()

    def _text(self):
        with self._lock:
            phase = next(reversed(self._tasks.values()), None) if self._tasks else None
        return f"Ryn 正在控制电脑 · {phase} · 可在 Ryn 中暂停" if phase else ""

    def _run(self):
        try:
            self._windows_loop()
        except Exception as exc:
            # A display failure must never change the outcome of an input call.
            self.error = type(exc).__name__

    def _windows_loop(self):
        import ctypes as c
        from ctypes import wintypes as w

        u = c.WinDLL("user32", use_last_error=True)
        g = c.WinDLL("gdi32", use_last_error=True)
        k = c.WinDLL("kernel32", use_last_error=True)
        proc_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

        class WndClass(c.Structure):
            _fields_ = [("style", w.UINT), ("proc", proc_type), ("cls_extra", c.c_int),
                        ("wnd_extra", c.c_int), ("instance", w.HINSTANCE), ("icon", w.HICON),
                        ("cursor", w.HANDLE), ("background", w.HBRUSH),
                        ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]

        class Paint(c.Structure):
            _fields_ = [("hdc", w.HDC), ("erase", w.BOOL), ("rect", w.RECT),
                        ("restore", w.BOOL), ("inc", w.BOOL), ("reserved", c.c_byte * 32)]

        def sig(lib, name, restype, *args):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, list(args)
            return fn

        sig(k, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)
        sig(u, "RegisterClassW", w.ATOM, c.POINTER(WndClass))
        sig(u, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
            c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p)
        sig(u, "DefWindowProcW", c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        sig(u, "BeginPaint", w.HDC, w.HWND, c.POINTER(Paint))
        sig(u, "EndPaint", w.BOOL, w.HWND, c.POINTER(Paint))
        sig(u, "FillRect", c.c_int, w.HDC, c.POINTER(w.RECT), w.HBRUSH)
        sig(u, "FrameRect", c.c_int, w.HDC, c.POINTER(w.RECT), w.HBRUSH)
        sig(u, "DrawTextW", c.c_int, w.HDC, w.LPCWSTR, c.c_int, c.POINTER(w.RECT), w.UINT)
        sig(u, "GetClientRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
        sig(u, "SetLayeredWindowAttributes", w.BOOL, w.HWND, w.DWORD, w.BYTE, w.DWORD)
        sig(u, "SetWindowDisplayAffinity", w.BOOL, w.HWND, w.DWORD)
        sig(u, "SetWindowPos", w.BOOL, w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT)
        sig(u, "ShowWindow", w.BOOL, w.HWND, c.c_int)
        sig(u, "InvalidateRect", w.BOOL, w.HWND, c.POINTER(w.RECT), w.BOOL)
        sig(u, "PeekMessageW", w.BOOL, c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        sig(u, "TranslateMessage", w.BOOL, c.POINTER(w.MSG))
        sig(u, "DispatchMessageW", c.c_ssize_t, c.POINTER(w.MSG))
        sig(u, "DestroyWindow", w.BOOL, w.HWND)
        sig(u, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
        sig(g, "CreateSolidBrush", w.HBRUSH, w.DWORD)
        sig(g, "DeleteObject", w.BOOL, w.HANDLE)
        sig(g, "SelectObject", w.HANDLE, w.HDC, w.HANDLE)
        sig(g, "SetTextColor", w.DWORD, w.HDC, w.DWORD)
        sig(g, "SetBkMode", c.c_int, w.HDC, c.c_int)
        sig(g, "CreateFontW", w.HANDLE, c.c_int, c.c_int, c.c_int, c.c_int, c.c_int,
            w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR)
        sig(u, "GetSystemMetrics", c.c_int, c.c_int)

        text = ""
        # COLORREF: red is the low byte. Black is the transparent color key.
        brushes = [g.CreateSolidBrush(color) for color in (0, 0xF3C786, 0xDFA560, 0xB07645, 0x71482C)]
        panel_brush = g.CreateSolidBrush(0x50351F)
        font = g.CreateFontW(-18, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")

        @proc_type
        def wnd_proc(hwnd, msg, wp, lp):
            if msg == 0x84:  # WM_NCHITTEST / HTTRANSPARENT
                return -1
            if msg == 0x21:  # WM_MOUSEACTIVATE / MA_NOACTIVATE
                return 3
            if msg == 0xF:
                paint = Paint()
                dc = u.BeginPaint(hwnd, c.byref(paint))
                rect = w.RECT()
                u.GetClientRect(hwnd, c.byref(rect))
                u.FillRect(dc, c.byref(rect), brushes[0])
                # Four bands fade inward to form the screen halo.
                for i in range(12):
                    border = w.RECT(i, i, rect.right - i, rect.bottom - i)
                    u.FrameRect(dc, c.byref(border), brushes[1 + min(i // 3, 3)])
                width = min(620, rect.right - 32)
                panel = w.RECT((rect.right - width) // 2, 14, (rect.right + width) // 2, 58)
                u.FillRect(dc, c.byref(panel), panel_brush)
                old_font = g.SelectObject(dc, font)
                g.SetTextColor(dc, 0xFFFFFF)
                g.SetBkMode(dc, 1)
                u.DrawTextW(dc, text, -1, c.byref(panel), 0x1 | 0x4 | 0x20)
                g.SelectObject(dc, old_font)
                u.EndPaint(hwnd, c.byref(paint))
                return 0
            return u.DefWindowProcW(hwnd, msg, wp, lp)

        instance = k.GetModuleHandleW(None)
        name = f"RynComputerIndicator_{os.getpid()}_{id(self)}"
        wc = WndClass(0, wnd_proc, 0, 0, instance, None, None, None, None, name)
        if not u.RegisterClassW(c.byref(wc)):
            raise c.WinError(c.get_last_error())
        hwnd = None
        try:
            # LAYERED | TRANSPARENT | TOOLWINDOW | NOACTIVATE. Never takes focus.
            hwnd = u.CreateWindowExW(0x080800A0, name, "Ryn 电脑控制", 0x80000000,
                                    0, 0, 1, 1, None, None, instance, None)
            if not hwnd:
                raise c.WinError(c.get_last_error())
            u.SetLayeredWindowAttributes(hwnd, 0, 232, 1 | 2)
            u.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
            previous = None
            while not self._closed.is_set():
                text = self._text()
                # Cover the primary display; adjust when resolution changes.
                size = (u.GetSystemMetrics(0), u.GetSystemMetrics(1))
                current = (text, size)
                if current != previous:
                    if text:
                        u.SetWindowPos(hwnd, w.HWND(-1), 0, 0, *size, 0x10 | 0x40)
                        u.InvalidateRect(hwnd, None, True)
                    else:
                        u.ShowWindow(hwnd, 0)
                    previous = current
                msg = w.MSG()
                while u.PeekMessageW(c.byref(msg), None, 0, 0, 1):
                    u.TranslateMessage(c.byref(msg))
                    u.DispatchMessageW(c.byref(msg))
                self._closed.wait(0.1)
        finally:
            if hwnd:
                u.DestroyWindow(hwnd)
            u.UnregisterClassW(name, instance)
            for brush in [*brushes, panel_brush]:
                g.DeleteObject(brush)
            g.DeleteObject(font)
