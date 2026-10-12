"""Loopback DAV test service with actual HTTP and independently observed I/O.

The caller supplies a NEW isolated root containing only this case's mounts.
Never point this server at the repository, user directories or all fixtures.
This service is an acceptance fixture, not a production DAV implementation.
"""
from __future__ import annotations

import hashlib
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET

MAX_BODY = 8 * 1024 * 1024


class FixtureDAV:
    def __init__(self, root: Path, *, port=0):
        self.root = root.resolve(strict=True)
        if not (self.root / ".ryn-acceptance-isolated").is_file():
            raise ValueError("isolated_root_marker_required")
        self.lock = threading.RLock()
        self.audit = []
        self.faults = {}
        self.barriers = {}
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_GET(self):
                fixture.handle(self)

            do_PROPFIND = do_GET
            do_PUT = do_GET
            do_MKCOL = do_GET
            do_DELETE = do_GET
            do_MOVE = do_GET
            do_COPY = do_GET

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}/dav"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        for _entered, release in self.barriers.values():
            release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def arm(self, method, path, fault):
        """One-shot failure, selected before executing the model task."""
        if fault not in {"drop_after_commit", "refuse", "barrier_before_io"}:
            raise ValueError("unknown_fixture_fault")
        with self.lock:
            self.faults[(method, path)] = fault
            if fault == "barrier_before_io":
                self.barriers[(method, path)] = (threading.Event(), threading.Event())
                return self.barriers[(method, path)]

    def locate(self, raw):
        parsed = urlsplit(raw)
        decoded = unquote(parsed.path)
        if parsed.query or parsed.fragment or not decoded.startswith("/dav/") and decoded != "/dav":
            raise ValueError("invalid_fixture_path")
        relative = decoded.removeprefix("/dav").strip("/")
        parts = relative.split("/") if relative else []
        if any(part in {"", ".", "..", ".ryn-acceptance-isolated"} or ":" in part or "\\" in part or "%" in part for part in parts):
            raise ValueError("invalid_fixture_path")
        path = self.root.joinpath(*parts)
        if not path.resolve().is_relative_to(self.root) or any(p.is_symlink() for p in (path, *path.parents) if p != self.root.parent):
            raise ValueError("invalid_fixture_path")
        return relative, path

    def _listing(self, relative, path):
        tree = ET.Element("{DAV:}multistatus")
        for target in [path, *sorted(path.iterdir())]:
            if target.name == ".ryn-acceptance-isolated" or target.is_symlink():
                continue
            child = str(target.relative_to(self.root)).replace("\\", "/")
            if child == ".":
                child = ""
            item = ET.SubElement(tree, "{DAV:}response")
            ET.SubElement(item, "{DAV:}href").text = "/dav/" + quote(child, safe="/") + ("/" if target.is_dir() else "")
            propstat = ET.SubElement(item, "{DAV:}propstat")
            prop = ET.SubElement(propstat, "{DAV:}prop")
            kind = ET.SubElement(prop, "{DAV:}resourcetype")
            if target.is_dir():
                ET.SubElement(kind, "{DAV:}collection")
            ET.SubElement(prop, "{DAV:}getcontentlength").text = str(target.stat().st_size if target.is_file() else 0)
            ET.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
        return ET.tostring(tree, encoding="utf-8", xml_declaration=True)

    def handle(self, request):
        relative, path = "", None
        status, body, mutation = 400, b"", False
        observed = {"id": uuid.uuid4().hex, "method": request.command, "path": "[invalid]", "status": 400,
                    "mutation": False, "bytes": 0, "body_sha256": None, "response_dropped": False}
        try:
            relative, path = self.locate(request.path)
            observed["path"] = relative
            with self.lock:
                fault = self.faults.pop((request.command, relative), None)
            if fault == "barrier_before_io":
                entered, release = self.barriers[(request.command, relative)]
                entered.set()
                if not release.wait(30):
                    raise TimeoutError("fixture_barrier_expired")
            if fault == "refuse":
                status = 503
            else:
                with self.lock:
                    if request.command == "GET":
                        if not path.is_file():
                            status = 404
                        else:
                            body, status = path.read_bytes(), 200
                    elif request.command == "PROPFIND":
                        if request.headers.get("Depth") != "1":
                            status = 400
                        elif not path.is_dir():
                            status = 404
                        else:
                            body, status = self._listing(relative, path), 207
                    elif request.command == "PUT":
                        size = int(request.headers.get("Content-Length", "-1"))
                        if not 0 <= size <= MAX_BODY:
                            status = 413
                        elif request.headers.get("If-None-Match") != "*":
                            status = 428
                        elif not path.parent.is_dir():
                            status = 409
                        else:
                            body = request.rfile.read(size)
                            if len(body) != size:
                                raise ValueError("short_fixture_request")
                            try:
                                with path.open("xb") as stream:
                                    stream.write(body)
                            except FileExistsError:
                                status = 412
                            else:
                                status, mutation = 201, True
                    elif request.command == "MKCOL":
                        try:
                            path.mkdir()
                        except FileExistsError:
                            status = 405
                        except FileNotFoundError:
                            status = 409
                        else:
                            status, mutation = 201, True
                    else:
                        status = 405
            observed.update(status=status, mutation=mutation, bytes=len(body),
                            body_sha256=hashlib.sha256(body).hexdigest() if body else None,
                            response_dropped=fault == "drop_after_commit" and mutation)
            with self.lock:
                self.audit.append(observed)
            if observed["response_dropped"]:
                request.close_connection = True
                request.connection.close()
                return
        except Exception:
            status, body = 400, b""
            with self.lock:
                if observed not in self.audit:
                    self.audit.append(observed)
        response = body if request.command in {"GET", "PROPFIND"} and status < 300 else b""
        try:
            request.send_response(status)
            request.send_header("Content-Type", "application/xml" if request.command == "PROPFIND" else "application/octet-stream")
            request.send_header("Content-Length", str(len(response)))
            request.send_header("Connection", "close")
            request.end_headers()
            request.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        request.close_connection = True
