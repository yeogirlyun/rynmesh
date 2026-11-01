"""Bounded, controller-owned process identity and terminal evidence for R02.

Windows venv python.exe can be a launcher. Keep process object handles, rather
than trusting a PID after killing that launcher. Never target process names.
"""
from __future__ import annotations

import os
import time


class ProcessIdentityError(RuntimeError):
    pass


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
    ACCESS = 0x00100000 | 0x1000 | 0x0001  # synchronize, query, terminate

    class BasicInfo(ctypes.Structure):
        _fields_ = [("exit_status", wintypes.LONG), ("peb", ctypes.c_void_p),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.LONG),
                    ("pid", ctypes.c_size_t), ("parent_pid", ctypes.c_size_t)]

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("pid", wintypes.DWORD), ("heap", ctypes.c_size_t),
                    ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent_pid", wintypes.DWORD), ("priority", wintypes.LONG),
                    ("flags", wintypes.DWORD), ("exe", wintypes.WCHAR * 260)]

    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                               wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = wintypes.LONG

    def _open(pid):
        handle = kernel.OpenProcess(ACCESS, False, pid)
        if not handle:
            raise ProcessIdentityError("consumer_process_handle_unavailable")
        return handle

    def _creation(handle):
        values = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            raise ProcessIdentityError("consumer_process_time_unavailable")
        return values[0].dwLowDateTime | (values[0].dwHighDateTime << 32)

    def _wait(handle, milliseconds=0):
        value = kernel.WaitForSingleObject(handle, milliseconds)
        if value not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
            raise ProcessIdentityError("consumer_process_wait_failed")
        return value

    def _parent(handle):
        info, length = BasicInfo(), wintypes.ULONG()
        if ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(length)) < 0:
            raise ProcessIdentityError("consumer_process_ancestry_unavailable")
        return info.parent_pid

    def _process_rows():
        snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise ProcessIdentityError("consumer_process_snapshot_unavailable")
        try:
            entry = ProcessEntry()
            entry.size = ctypes.sizeof(entry)
            found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
            rows = []
            while found:
                rows.append((entry.pid, entry.parent_pid))
                found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
            return rows
        finally:
            kernel.CloseHandle(snapshot)


class OwnedConsumerProcess:
    """One launched consumer; identity must be bound before restart is allowed."""

    def __init__(self, child):
        self.child = child
        self.launcher_pid = child.pid
        self.worker_pid = None
        self.identity_basis = None
        self.handles = {}
        self.closed = False
        self.proof = None
        if os.name == "nt":
            handle = _open(child.pid)
            try:
                if _wait(handle) != WAIT_TIMEOUT or _creation(handle) != _creation(int(child._handle)):
                    raise ProcessIdentityError("consumer_launcher_identity_changed")
                self.handles[child.pid] = handle
            except BaseException:
                kernel.CloseHandle(handle)
                raise
        elif os.name != "posix":
            raise ProcessIdentityError("consumer_process_identity_unsupported")

    def _owned_handle(self, pid):
        candidate = _open(pid)
        opened = []
        try:
            current, current_pid = candidate, pid
            for _ in range(32):
                if _wait(current) != WAIT_TIMEOUT:
                    raise ProcessIdentityError("consumer_process_identity_not_live")
                if current_pid == self.launcher_pid:
                    if _creation(current) != _creation(self.handles[self.launcher_pid]):
                        raise ProcessIdentityError("consumer_launcher_identity_changed")
                    return candidate
                parent_pid = _parent(current)
                if parent_pid in (0, os.getpid(), current_pid):
                    break
                parent = self.handles[self.launcher_pid] if parent_pid == self.launcher_pid else _open(parent_pid)
                if parent_pid != self.launcher_pid:
                    opened.append(parent)
                # A recycled PID cannot be the creator of an older child.
                if _creation(parent) > _creation(current):
                    break
                current, current_pid = parent, parent_pid
            raise ProcessIdentityError("consumer_worker_not_owned")
        except BaseException:
            kernel.CloseHandle(candidate)
            raise
        finally:
            for handle in opened:
                kernel.CloseHandle(handle)

    def bind_worker(self, pid):
        if self.closed or type(pid) is not int or pid <= 0 or pid == os.getpid():
            raise ProcessIdentityError("consumer_worker_invalid_identity")
        if self.worker_pid is not None:
            if pid != self.worker_pid:
                raise ProcessIdentityError("consumer_worker_identity_changed")
            return
        if self.child.poll() is not None:
            raise ProcessIdentityError("consumer_launcher_not_live")
        if os.name == "nt":
            if pid != self.launcher_pid:
                self.handles[pid] = self._owned_handle(pid)
            elif _wait(self.handles[pid]) != WAIT_TIMEOUT:
                raise ProcessIdentityError("consumer_worker_not_live")
        elif pid != self.launcher_pid:
            raise ProcessIdentityError("consumer_worker_identity_unsupported")
        self.worker_pid = pid
        self.identity_basis = "same_launched_process" if pid == self.launcher_pid else "verified_descendant"

    def _capture_cleanup_descendants(self):
        if os.name != "nt" or _wait(self.handles[self.launcher_pid]) != WAIT_TIMEOUT:
            return
        rows = _process_rows()
        descendants = {self.launcher_pid}
        for _ in range(32):
            added = {pid for pid, parent in rows if parent in descendants} - descendants
            if not added:
                break
            descendants.update(added)
        for pid in sorted(descendants - self.handles.keys()):
            try:
                self.handles[pid] = self._owned_handle(pid)
            except ProcessIdentityError:
                # Exited/reparented candidates are never killed using a bare PID.
                continue

    def terminate(self, *, timeout=10):
        if self.closed:
            raise ProcessIdentityError("consumer_process_proof_closed")
        if self.proof is not None:
            return self.proof
        self._capture_cleanup_descendants()
        deadline = time.monotonic() + timeout
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=max(.001, deadline - time.monotonic()))
        statuses = {}
        if os.name == "nt":
            for pid, handle in self.handles.items():
                # Terminate only retained handles whose ancestry was validated.
                if _wait(handle, min(100, max(0, int((deadline - time.monotonic()) * 1000)))) != WAIT_OBJECT_0:
                    # Launcher termination may already have started worker exit.
                    # TerminateProcess can return access denied during that exit;
                    # only the retained handle's terminal wait settles the proof.
                    kernel.TerminateProcess(handle, 1)
                statuses[pid] = _wait(handle, max(0, int((deadline - time.monotonic()) * 1000)))
                if statuses[pid] != WAIT_OBJECT_0:
                    raise ProcessIdentityError("consumer_owned_process_not_terminal")
        worker_terminal = self.worker_pid is not None and (os.name == "posix" or statuses[self.worker_pid] == WAIT_OBJECT_0)
        self.proof = {"pid": self.worker_pid, "launcher_pid": self.launcher_pid,
                      "actual_worker_pid": self.worker_pid, "returncode": self.child.returncode,
                      "launcher_confirmed_terminal": self.child.poll() is not None,
                      "confirmed_terminal": worker_terminal, "identity_basis": self.identity_basis,
                      "actual_worker_terminal_proof": {
                          "method": "retained_windows_process_handle" if os.name == "nt" else "direct_child_popen_wait",
                          "handle_opened_before_termination": os.name == "nt" and self.worker_pid is not None,
                          "wait_status": statuses.get(self.worker_pid), "confirmed_terminal": worker_terminal},
                      "owned_processes_confirmed_terminal": sorted(statuses) if os.name == "nt" else [self.launcher_pid]}
        return self.proof

    def close(self):
        if not self.closed:
            for handle in self.handles.values():
                kernel.CloseHandle(handle)
            self.handles.clear()
            self.closed = True
