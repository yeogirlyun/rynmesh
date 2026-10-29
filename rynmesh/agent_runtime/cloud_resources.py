"""Current cloud ancestry checks; never authorize an item-ID path by prefix."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from urllib.parse import urlsplit

from ..nas import NasError, relative_path
from .store import AgentToolRejected

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"


def denied():
    raise AgentToolRejected("agent_resource_ancestry_changed")


class CloudAncestry:
    def __init__(self, connector, protocol):
        self.connector, self.protocol = connector, protocol

    def metadata(self, path):
        if self.protocol == "google":
            value = self.connector.request("GET", "/files/" + self.connector.file_id(path), params={
                "fields": "id,name,mimeType,parents,trashed,driveId,version,size", "supportsAllDrives": "true"})
            if value.get("trashed") or value.get("mimeType") == SHORTCUT:
                denied()
            return {"id": value.get("id"), "name": value.get("name", ""), "directory": value.get("mimeType") == FOLDER,
                    "parents": value.get("parents", []), "drive_id": value.get("driveId", "my-drive"),
                    "version": value.get("version"), "mime_type": value.get("mimeType", "")}
        value = self.connector.request("GET", self.connector.item_path(path), params={
            "$select": "id,name,parentReference,folder,file,remoteItem,deleted,eTag,cTag,root"})
        if "remoteItem" in value or "deleted" in value or not ("file" in value or "folder" in value):
            denied()
        parent = value.get("parentReference") or {}
        return {"id": value.get("id"), "name": value.get("name", ""), "directory": "folder" in value,
                "parents": [parent["id"]] if parent.get("id") else [], "drive_id": parent.get("driveId"),
                "version": value.get("cTag") or value.get("eTag"), "mime_type": (value.get("file") or {}).get("mimeType", "")}

    def check_cursor(self, path, cursor):
        if cursor and self.protocol == "onedrive":
            parsed = urlsplit(cursor)
            expected = "/v1.0" + self.connector.item_path(path) + "/children"
            if parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com" or parsed.path != expected or parsed.fragment:
                denied()

    def check(self, path, *, expected_root=None):
        parts = relative_path(path).split("/") if path else []
        if len(parts) > 16:
            denied()
        root = self.metadata("")
        if not root["id"] or not root["directory"] or not root["drive_id"]:
            denied()
        identity = {"id": root["id"], "drive_id": root["drive_id"]}
        if expected_root is not None and identity != expected_root:
            denied()
        parent = root
        for part in parts:
            current = self.metadata(part)
            if (not parent["directory"] or current["id"] != part or current["parents"] != [parent["id"]]
                    or current["drive_id"] != root["drive_id"] or not current["version"]):
                denied()
            parent = current
        return parent, identity


@contextmanager
def cloud_connection(plugin, connection_id):
    with plugin.lock:
        source = plugin.source(connection_id)
        if source["protocol"] not in {"google", "onedrive"} or not source.get("allow_ai"):
            raise AgentToolRejected("agent_resource_ai_not_allowed")
        gate = plugin.source_locks.setdefault(connection_id, threading.RLock())
    with gate:
        with plugin.lock:
            if plugin.source(connection_id) is not source or not source.get("allow_ai"):
                raise AgentToolRejected("agent_resource_permission_changed")
        try:
            rejection = None
            with plugin.connection(source, reuse=True) as connector:
                try:
                    yield CloudAncestry(connector, source["protocol"])
                except AgentToolRejected as exc:
                    # NAS normalizes exceptions from its context body. Preserve
                    # our already-safe precondition code outside that boundary.
                    rejection = exc
            if rejection is not None:
                raise rejection
        except NasError as exc:
            code = {"file_too_large": "agent_resource_size_limit",
                    "preview_requires_utf8_text": "agent_resource_requires_utf8_text"}.get(str(exc), "agent_resource_read_failed")
            raise AgentToolRejected(code) from None
