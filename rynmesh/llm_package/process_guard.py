"""Bounded CLI processes. No caller-controlled commands or resource settings."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time


def spawn(args: list[str], *, cwd: str, env: dict[str, str], timeout: float):
    options = {"cwd": cwd, "env": env, "stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
               "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # Assign before the first instruction executes, including child processes.
        process = subprocess.Popen(args, creationflags=0x4 | 0x08000000, **options)
        try:
            process._ryn_job = _windows_job(process)
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
        return process
    wrapper = os.path.abspath(__file__)
    entry = "--ryn-private-cli-worker" if getattr(sys, "frozen", False) else wrapper
    if getattr(sys, "frozen", False):
        # Preserve the bootloader's own worker context, without inheriting the
        # owner's environment. Otherwise onefile treats this as a new app and
        # unpacks its entire Python runtime into the bounded CLI temp directory.
        options["env"] = _frozen_worker_env(env)
    return subprocess.Popen([sys.executable, entry, str(max(1, int(timeout))), *args],
                            start_new_session=True, **options)


def _frozen_worker_env(env: dict[str, str]) -> dict[str, str]:
    return {**env, **{key: value for key, value in os.environ.items() if key.startswith("_PYI_")}}


def terminate(process):
    if os.name == "nt":
        job = getattr(process, "_ryn_job", None)
        if job:
            import ctypes
            process._ryn_job = None
            ctypes.windll.kernel32.CloseHandle(job)
        elif process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def communicate_bounded(process, data: bytes, *, timeout: float, root):
    from .safety import MAX_OUTPUT, MAX_TEMP, check_disk, private_storage_bytes

    chunks, failures = [], []

    def read():
        total = 0
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_OUTPUT:
                    failures.append("CLI output limit exceeded")
                    process.kill()
                    return
                chunks.append(chunk)
        except OSError:
            failures.append("CLI output read failed")

    def write():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except OSError:
            failures.append("CLI input failed")

    reader = threading.Thread(target=read, daemon=True)
    writer = threading.Thread(target=write, daemon=True)
    reader.start()
    writer.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None or reader.is_alive() or writer.is_alive():
            if time.monotonic() >= deadline:
                raise RuntimeError("CLI request timed out")
            if failures:
                raise RuntimeError(failures[0])
            check_disk(root)
            if private_storage_bytes(root) > MAX_TEMP:
                raise RuntimeError("CLI temporary storage limit exceeded")
            time.sleep(0.05)
        if failures:
            raise RuntimeError(failures[0])
        return b"".join(chunks)
    finally:
        terminate(process)
        reader.join(timeout=2)
        writer.join(timeout=2)
        process.stdout.close()


def _windows_job(process, *, memory_limit=2 * 1024**3, cpu_rate=2500):
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                    ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                    ("processes", w.DWORD), ("affinity", c.c_size_t),
                    ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class IO(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in
                    ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t),
                    ("job_memory", c.c_size_t), ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

    class CPU(c.Structure):
        _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CLI resource isolation unavailable")
    try:
        limits = Extended()
        limits.basic.flags = 0x2000 | 0x8 | (0x200 if memory_limit else 0)
        limits.basic.processes = 16
        limits.job_memory = memory_limit
        cpu = CPU(0x1 | 0x4, cpu_rate)  # CLI defaults retain their resource ceiling
        if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            raise OSError("CLI memory isolation unavailable")
        if cpu_rate and not kernel.SetInformationJobObject(job, 15, c.byref(cpu), c.sizeof(cpu)):
            raise OSError("CLI CPU isolation unavailable")
        if not kernel.AssignProcessToJobObject(job, w.HANDLE(process._handle)):
            raise OSError("CLI process isolation unavailable")
        resume = c.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [w.HANDLE]
        if resume(w.HANDLE(process._handle)) != 0:
            raise OSError("CLI process resume failed")
        return w.HANDLE(job)
    except BaseException:
        kernel.CloseHandle(job)
        raise


def limited_exec(arguments):
    # Run limits in a fresh interpreter, never preexec_fn in a threaded server.
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    seconds = int(arguments[0])
    resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    os.nice(10)
    # These bootloader variables are for our worker only, not for the CLI.
    cli_env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    os.execvpe(arguments[1], arguments[1:], cli_env)


if __name__ == "__main__":
    limited_exec(sys.argv[1:])
