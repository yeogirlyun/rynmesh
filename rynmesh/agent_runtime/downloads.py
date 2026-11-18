"""Download a public URL or a task-bound file reference returned by an HTTP plugin."""
import hashlib
import os
import re
import tempfile
import uuid
import urllib.request
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .store import AgentError, AgentToolRejected
from .web_search import pinned_stream, resolve_public_http_url


MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


def _filename(filename, index=0):
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .") or "download"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", filename.split(".", 1)[0], re.I):
        filename = "_" + filename
    path = Path(filename)
    suffix = f" ({index})" if index else ""
    return path.stem[:200 - len(path.suffix) - len(suffix)] + suffix + path.suffix


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid HTTP URL")
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def issue_refs(data, binding, context, adapter):
    """Accept file descriptors only from the successful structured result of a bound connector."""
    refs = {}
    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for child in list(value.values()):
                visit(child)
            descriptor = value.get("fileRef")
            if isinstance(descriptor, dict):
                url, name = descriptor.get("url"), descriptor.get("name") or value.get("name")
            elif value.get("state") == "available":
                url, name = value.get("link"), value.get("name")
            else:
                return
            if isinstance(url, str) and isinstance(name, str) and len(refs) < 256:
                ref = "fileRef_" + uuid.uuid4().hex
                refs[ref] = {"url": url, "name": name, "binding": binding,
                             "scope_revision": context.get("scope_revision")}
                value["fileRef"] = ref
    visit(data)
    if refs:
        def save(state):
            task = state["tasks"][context["task_id"]]
            records = task.setdefault("download_refs", {})
            records.update(refs)
            for old in list(records)[:-512]:
                del records[old]
        adapter.local.store.transaction(save)
    return [{"name": item["name"], "fileRef": ref} for ref, item in refs.items()]


def save(local, source, context, cancelled):
    try:
        return _download(local, source, context, cancelled)
    except httpx.ProxyError:
        raise AgentToolRejected("agent_download_proxy_error", correction="The system proxy could not transfer the file; check its configuration and connection.") from None
    except httpx.RequestError:
        raise AgentToolRejected("agent_download_network_error", correction="The file transfer failed before it was saved.") from None
    except AgentError:
        raise
    except ValueError:
        raise AgentToolRejected("agent_download_redirect_invalid", correction="The server redirected to an address that is not an http(s) URL.") from None


def _download(local, source, context, cancelled):
    url, name, headers, authority = source, "", {}, None
    if source.startswith("fileRef_"):
        record = local.store.inspect(lambda s: s["tasks"].get(context["task_id"], {}).get("download_refs", {}).get(source))
        if not record or record["scope_revision"] != context.get("scope_revision"):
            raise AgentToolRejected("agent_download_ref_expired", correction="List the plugin files again for a current fileRef.")
        url, name = record["url"], record["name"]
        headers, authority = local.download_authority(record["binding"], url, context)
    root = local.path(".", context)
    # Environment configuration also works for headless hosts without changing
    # persisted agent settings. Count decoded bytes, not just wire bytes.
    limit = int(os.environ.get("RYN_MAX_DOWNLOAD_BYTES", MAX_DOWNLOAD_BYTES))
    proxies = urllib.request.getproxies() if authority is None else {}
    for _ in range(6):
        if cancelled():
            raise AgentToolRejected("agent_cancelled")
        if authority is None:
            url, _, ips = resolve_public_http_url(url, tool="ryn_download_file")
        else:
            try:
                current_origin = origin(url)
            except ValueError:
                raise AgentToolRejected("agent_download_redirect_invalid", correction="The server redirected to an address that is not an http(s) URL.") from None
            if current_origin != authority:
                raise AgentToolRejected("agent_download_redirect_outside_plugin")
        proxy = proxies.get(urlsplit(url).scheme) or proxies.get("all")
        with httpx.Client(timeout=35, trust_env=False, proxy=proxy) as client:
            stream = (pinned_stream(client, "GET", url, ips, headers)
                      if authority is None and not proxy else client.stream("GET", url, headers=headers))
            with stream as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise AgentToolRejected("agent_download_http_error", correction=f"Download returned HTTP {response.status_code}.")
                if authority and "text/html" in response.headers.get("content-type", "") and not name.lower().endswith((".html", ".htm")):
                    raise AgentToolRejected("agent_download_unexpected_html")
                too_large = f"The download exceeds the limit of {limit} bytes; choose a smaller file."
                length = response.headers.get("content-length", "")
                if length.isdecimal() and int(length) > limit:
                    raise AgentToolRejected("agent_download_too_large", correction=too_large)
                meta = Message()
                meta["Content-Disposition"] = response.headers.get("content-disposition", "")
                filename = name or meta.get_filename() or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download"
                filename = _filename(filename)
                temp = None
                try:
                    with tempfile.NamedTemporaryFile(dir=root, prefix=".ryn-download-", delete=False) as out:
                        temp = Path(out.name)
                    digest = hashlib.sha256()
                    with temp.open("wb") as out:
                        for chunk in response.iter_bytes(65536):
                            if cancelled():
                                raise AgentToolRejected("agent_cancelled")
                            if out.tell() + len(chunk) > limit:
                                raise AgentToolRejected("agent_download_too_large", correction=too_large)
                            out.write(chunk)
                            digest.update(chunk)
                        size = out.tell()
                    target = local.path(filename, context)
                    candidate = target
                    for index in range(1, 10001):
                        try:
                            try:
                                os.link(temp, candidate)
                            except FileExistsError:
                                raise
                            except OSError:
                                # Reserve the name exclusively before replacing our
                                # own placeholder on filesystems without hard links.
                                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                                os.close(fd)
                                try:
                                    os.replace(temp, candidate)
                                except OSError:
                                    candidate.unlink()
                                    raise
                            return {"written": True, "path": str(candidate), "bytes": size, "sha256": digest.hexdigest()}
                        except FileExistsError:
                            candidate = target.with_name(_filename(filename, index))
                    raise AgentToolRejected("agent_download_filename_conflict")
                except OSError:
                    raise AgentToolRejected(
                        "agent_download_save_failed",
                        correction="Could not save the file under that name; choose a shorter or simpler file name.",
                        details={"name": filename},
                    ) from None
                finally:
                    if temp is not None:
                        temp.unlink(missing_ok=True)
    raise AgentToolRejected("agent_download_too_many_redirects")
