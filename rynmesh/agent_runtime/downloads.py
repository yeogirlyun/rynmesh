"""Download a public URL or a task-bound file reference returned by an HTTP plugin."""
import hashlib
import os
import re
import tempfile
import uuid
from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from .store import AgentToolRejected
from .web_search import validate_public_http_url


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
    except httpx.RequestError:
        raise AgentToolRejected("agent_download_network_error", correction="The file transfer failed before it was saved.") from None


def _download(local, source, context, cancelled):
    url, name, headers, authority = source, "", {}, None
    if source.startswith("fileRef_"):
        record = local.store.inspect(lambda s: s["tasks"].get(context["task_id"], {}).get("download_refs", {}).get(source))
        if not record or record["scope_revision"] != context.get("scope_revision"):
            raise AgentToolRejected("agent_download_ref_expired", correction="List the plugin files again for a current fileRef.")
        url, name = record["url"], record["name"]
        headers, authority = local.download_authority(record["binding"], url, context)
    root = local.path(".", context)
    with httpx.Client(timeout=35, trust_env=False) as client:
        for _ in range(6):
            if cancelled():
                raise AgentToolRejected("agent_cancelled")
            if authority is None:
                url = validate_public_http_url(url)
            elif origin(url) != authority:
                raise AgentToolRejected("agent_download_redirect_outside_plugin")
            with client.stream("GET", url, headers=headers) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                if response.status_code != 200:
                    raise AgentToolRejected("agent_download_http_error", correction=f"Download returned HTTP {response.status_code}.")
                if authority and "text/html" in response.headers.get("content-type", "") and not name.lower().endswith((".html", ".htm")):
                    raise AgentToolRejected("agent_download_unexpected_html")
                meta = Message()
                meta["Content-Disposition"] = response.headers.get("content-disposition", "")
                filename = name or meta.get_filename() or unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "download"
                filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(" .") or "download"
                with tempfile.NamedTemporaryFile(dir=root, prefix=".ryn-download-", delete=False) as out:
                    temp = Path(out.name)
                try:
                    digest = hashlib.sha256()
                    with temp.open("wb") as out:
                        for chunk in response.iter_bytes(65536):
                            if cancelled():
                                raise AgentToolRejected("agent_cancelled")
                            out.write(chunk)
                            digest.update(chunk)
                        size = out.tell()
                    target = local.path(filename, context)
                    candidate = target
                    for index in range(1, 10001):
                        try:
                            os.link(temp, candidate)
                            return {"written": True, "path": str(candidate), "bytes": size, "sha256": digest.hexdigest()}
                        except FileExistsError:
                            candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
                    raise AgentToolRejected("agent_download_filename_conflict")
                finally:
                    temp.unlink(missing_ok=True)
    raise AgentToolRejected("agent_download_too_many_redirects")
