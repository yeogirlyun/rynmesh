"""Explicit pinned Craft installation and isolated read-only web hosting.

No Rynmesh identity, control API, uploads, native launch, automatic updates,
startup downloads or background workers. One editor per origin/port.
"""

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import ssl
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import certifi
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..file_transactions import file_transaction
from .craft_catalog import CRAFT

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 768 * 1024 * 1024
MAX_FILES = 4096
REDIRECT_HOSTS = {
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class CraftError(ValueError):
    pass


def specification(slug):
    if slug not in CRAFT:
        raise CraftError("craft_unknown_app")
    return CRAFT[slug]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


class _ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlsplit

        url = urlsplit(newurl)
        if (
            url.scheme != "https"
            or url.hostname not in REDIRECT_HOSTS
            or url.username
            or url.password
        ):
            raise CraftError("craft_release_redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_to(url, path):
    """Bounded HTTPS download. TLS verification stays enabled on bundled Python."""
    opener = urllib.request.build_opener(
        _ReleaseRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Rynmesh-Craft/1.0"})
    with opener.open(request, timeout=60) as response, Path(path).open("xb") as out:
        if int(response.headers.get("Content-Length") or 0) > MAX_ARCHIVE:
            raise CraftError("craft_archive_limit")
        size = 0
        while chunk := response.read(256 * 1024):
            size += len(chunk)
            if size > MAX_ARCHIVE:
                raise CraftError("craft_archive_limit")
            out.write(chunk)


def extract(archive_path, destination):
    """Validate the entire archive before writing; retain upstream notices/assets."""
    destination = Path(destination)
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if (
            not members
            or len(members) > MAX_FILES
            or sum(i.file_size for i in members) > MAX_EXPANDED
        ):
            raise CraftError("craft_extraction_limit")
        seen = set()
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            mode = member.external_attr >> 16
            if (
                not name
                or "\\" in name
                or ":" in name
                or "\x00" in name
                or path.is_absolute()
                or ".." in path.parts
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR})
                or path.as_posix().casefold().rstrip("/") in seen
                or member.flag_bits & 1
            ):
                raise CraftError("craft_unsafe_archive")
            seen.add(path.as_posix().casefold().rstrip("/"))
        archive.extractall(destination)
    candidates = list(destination.rglob("index.html"))
    if len(candidates) != 1:
        raise CraftError("craft_ambiguous_entry")
    return candidates[0].parent


def _location(cache, slug):
    spec = specification(slug)
    return Path(cache).expanduser().resolve() / slug / spec["tag"]


def installed(cache, slug):
    """Verify every installed file before starting a server; never trust saved paths."""
    spec = specification(slug)
    root = _location(cache, slug)
    try:
        raw = (root / "receipt.json").read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError
        receipt = json.loads(raw)
        if (
            receipt["archive_sha256"] != spec["web_sha256"]
            or receipt["app"] != slug
            or receipt["version"] != spec["tag"]
        ):
            raise ValueError
        files = receipt["files"]
        if not files or len(files) > MAX_FILES:
            raise ValueError
        payload = root / "payload"
        actual = {p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()}
        if actual != set(files) or any(p.is_symlink() for p in payload.rglob("*")):
            raise ValueError
        for name, checksum in files.items():
            target = (payload / name).resolve()
            if not target.is_relative_to(payload.resolve()) or digest(target) != checksum:
                raise ValueError
        web = (payload / receipt["entry"]).resolve()
        if not web.is_relative_to(payload.resolve()) or not (web / "index.html").is_file():
            raise ValueError
        return web
    except (OSError, ValueError, KeyError, TypeError):
        raise CraftError("craft_build_not_verified") from None


def install(cache, slug, *, download=download_to):
    spec = specification(slug)
    if not spec["web"]:
        raise CraftError("craft_native_only")
    root = _location(cache, slug)
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_transaction(root.parent / ".install.lock"):
        if root.exists():
            web = installed(cache, slug)
            return {"app": slug, "version": spec["tag"], "state": "ready", "web_dir": str(web)}
        stage = Path(tempfile.mkdtemp(prefix=".install-", dir=root.parent))
        os.chmod(stage, 0o700)
        try:
            archive = stage / "release.zip"
            url = f"https://github.com/storytold/{slug}/releases/download/{spec['tag']}/{spec['web_asset']}"
            download(url, archive)
            if archive.stat().st_size > MAX_ARCHIVE or digest(archive) != spec["web_sha256"]:
                raise CraftError("craft_checksum_mismatch")
            payload = stage / "payload"
            web = extract(archive, payload)
            files = {
                p.relative_to(payload).as_posix(): digest(p)
                for p in payload.rglob("*")
                if p.is_file()
            }
            receipt = {
                "app": slug,
                "version": spec["tag"],
                "archive_sha256": spec["web_sha256"],
                "release_url": url,
                "entry": web.relative_to(payload).as_posix(),
                "files": files,
            }
            (stage / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            archive.unlink()
            stage.rename(root)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return {
        "app": slug,
        "version": spec["tag"],
        "state": "ready",
        "web_dir": str(installed(cache, slug)),
    }


def create_editor(cache, slug, *, public_host="127.0.0.1"):
    """Dedicated static origin: no node-app/control routes, state, token or cookies."""
    web = installed(cache, slug)
    app = FastAPI(
        title=specification(slug)["name"], docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[public_host])

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cross-Origin-Opener-Policy": "same-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; "
                    "connect-src 'self'; worker-src 'self' blob:; media-src 'self' blob:; "
                    "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; "
                    "sandbox allow-scripts allow-same-origin allow-downloads"
                ),
            }
        )
        return response

    @app.get("/{asset:path}")
    async def asset(request: Request, asset: str):
        path = (web / (asset or "index.html")).resolve()
        if (
            not path.is_relative_to(web)
            or not path.is_file()
            or any(part.startswith(".") for part in PurePosixPath(asset).parts)
        ):
            raise HTTPException(404)
        media_type = "application/wasm" if path.suffix == ".wasm" else mimetypes.guess_type(path)[0]
        return FileResponse(path, media_type=media_type or "application/octet-stream")

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "install", "serve"])
    parser.add_argument("--app", choices=sorted(CRAFT))
    parser.add_argument("--cache", default=str(Path.home() / ".rynmesh-craft"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--public-host", help="Exact browser hostname when explicitly binding a network interface"
    )
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.command == "list":
        print(
            json.dumps(
                [{"app": k, "version": v["tag"], "web": v["web"]} for k, v in CRAFT.items()],
                indent=2,
            )
        )
        return
    if not args.app:
        parser.error("--app is required")
    try:
        if args.command == "install":
            print(json.dumps(install(args.cache, args.app)))
        else:
            if args.host in {"0.0.0.0", "::"} and not args.public_host:
                parser.error("--public-host is required for a wildcard interface")
            if not specification(args.app)["web"]:
                raise CraftError("craft_native_only")
            import uvicorn

            port = args.port or (8920 + sorted(CRAFT).index(args.app))
            uvicorn.run(
                create_editor(args.cache, args.app, public_host=args.public_host or args.host),
                host=args.host,
                port=port,
                access_log=False,
                log_level="warning",
            )
    except (CraftError, OSError) as exc:
        parser.exit(1, f"Craft: {exc}\n")


if __name__ == "__main__":
    main()
