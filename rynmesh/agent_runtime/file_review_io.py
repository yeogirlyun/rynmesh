"""Checked file replacement; Windows handles exclude writers and pin parent directories."""
import os
from contextlib import ExitStack

from .store import AgentToolRejected


def replace_checked(path, root, expected, raw, mode, stamp):
    from .file_review import MAX_FILE, digest, fingerprint, safe_path
    safe_path(root, str(path))
    # Only the explicitly requested file's parent chain is created/read.
    if raw is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, str(path))
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        disposition = kernel.SetFileInformationByHandle
        disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        disposition.restype = wintypes.BOOL

        def open_handle(p, access, share, creation, flags):
            handle = create(str(p), access, share, None, creation, flags, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), "file_busy_or_changed")
            return handle

        with ExitStack() as stack:
            # No FILE_SHARE_DELETE: ancestors cannot be replaced by a junction
            # while the target is checked and modified. OPEN_REPARSE_POINT avoids following links.
            for parent in reversed(path.parents):
                handle = open_handle(parent, 0, 3, 3, 0x02200000)
                stack.callback(close, handle)
            safe_path(root, str(path))
            if fingerprint(path) != stamp:
                raise AgentToolRejected("agent_review_file_changed")
            if expected is None and raw is None:
                if path.exists():
                    raise AgentToolRejected("agent_review_file_changed")
                return
            handle = open_handle(path, 0xC0010000, 0, 1 if expected is None else 3, 0x00200000)
            # CREATE_NEW for absent targets; exclusive existing handle prevents
            # an editor from writing between the hash comparison and restoration.
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            with os.fdopen(fd, "r+b") as stream:
                info = os.fstat(stream.fileno())
                if info.st_nlink > 1:
                    raise AgentToolRejected("agent_review_regular_file_required")
                opened_stamp = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
                if expected is not None and opened_stamp != stamp:
                    raise AgentToolRejected("agent_review_file_changed")
                actual = stream.read(MAX_FILE + 1)
                if (expected is not None and digest(actual) != expected) or len(actual) > MAX_FILE:
                    raise AgentToolRejected("agent_review_file_changed")
                if raw is None:
                    delete = wintypes.BOOL(True)
                    if not disposition(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
                        raise OSError(ctypes.get_last_error(), "file_delete_failed")
                else:
                    stream.seek(0)
                    stream.write(raw)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
        return

    # POSIX local controllers serialize through the shared review lock. O_NOFOLLOW
    # rejects final-component links and O_EXCL protects newly created destinations.
    import fcntl
    if expected is None and raw is None:
        if path.exists():
            raise AgentToolRejected("agent_review_file_changed")
        return
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if expected is None else 0)
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        safe_path(root, str(path))
        if expected is not None and fingerprint(path) != stamp:
            raise AgentToolRejected("agent_review_file_changed")
        actual = stream.read(MAX_FILE + 1)
        if expected is not None and digest(actual) != expected:
            raise AgentToolRejected("agent_review_file_changed")
        if raw is None:
            path.unlink()
        else:
            stream.seek(0)
            stream.write(raw)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
