"""Local personal spaces over the existing signed registry mailbox.

The registry is untrusted transport, never an authority for membership. One
coordinator serializes changes; delegated managers do not receive its key.
Membership is identity-bound and cached for at most 24 hours, not IP-bound.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .crypto import SignedPayload, b64, sign_payload, verify_signed_payload
from .jobs import (
    WorkOrder,
    WorkResult,
    default_expires_at,
    sign_work_order,
    sign_work_result,
    verify_work_order,
    verify_work_result,
)
from .llm_package.task_protocol import open_task, seal_task
from .registry import _write_json_atomic
from .services.peer_box import load_or_create_messaging_key, public_key_b64

CAPABILITY = "rynmesh.personal-space.v1"
LEASE_SECONDS = 24 * 3600
INVITE_PREFIX = "ryn-invite-v1:"


class SpaceError(ValueError):
    pass


def _name(value):
    value = str(value or "").strip()
    if not value or len(value) > 32:
        raise SpaceError("Use a name between 1 and 32 characters.")
    return value


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def _decode(value):
    if not isinstance(value, str) or len(value) > 32000:
        raise SpaceError("Invalid invitation.")
    try:
        return json.loads(base64.urlsafe_b64decode(value))
    except Exception as exc:
        raise SpaceError("Invalid invitation.") from exc


class PersonalSpace:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock
        self.path = store.home / "personal-space.json"
        self.box = load_or_create_messaging_key(store.home / "space-messaging.key")
        self.lock = threading.RLock()
        self.tick_lock = threading.Lock()
        self.last_poll = 0.0
        self.last_error = ""
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
        if self.data.get("snapshot"):
            self._verify(self.data["snapshot"], fresh=False)

    def _save(self):
        _write_json_atomic(self.path, self.data)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _verify(self, raw, *, fresh=True, authority=None, space_id=None):
        signed = SignedPayload.from_dict(raw)
        verify_signed_payload(signed)
        p = signed.payload
        if p.get("kind") != CAPABILITY or p.get("authority") != signed.public_key:
            raise SpaceError("Invalid space signature.")
        if authority and signed.public_key != authority:
            raise SpaceError("Space authority does not match the invitation.")
        if space_id and p.get("id") != space_id:
            raise SpaceError("Space identity does not match.")
        if not isinstance(p.get("members"), dict) or len(p["members"]) > 64:
            raise SpaceError("Invalid member list.")
        if p["members"].get(p["authority"], {}).get("role") != "manager":
            raise SpaceError("Invalid coordinator membership.")
        if fresh and (
            p["valid_until"] <= self.clock()
            or p["issued_at"] > self.clock() + 60
            or p["valid_until"] - p["issued_at"] > LEASE_SECONDS
        ):
            raise SpaceError("Membership needs to be refreshed from the coordinator.")
        return p

    def _snapshot(self):
        return self.data.get("snapshot", {}).get("payload", {})

    def _authority(self):
        return self._snapshot().get("authority") == self.store.peer_id

    def _sign(self, snapshot):
        snapshot = copy.deepcopy(snapshot)
        snapshot["revision"] = snapshot.get("revision", 0) + 1
        snapshot["issued_at"] = self.clock()
        snapshot["valid_until"] = self.clock() + LEASE_SECONDS
        self.data["snapshot"] = sign_payload(
            snapshot, private_key_bytes=self.store.private_key_bytes
        ).to_dict()

    def status(self):
        with self.lock:
            p = self._snapshot()
            member = p.get("members", {}).get(self.store.peer_id, {})
            fresh = bool(p) and p["valid_until"] > self.clock()
            active = bool(member) and not member.get("removed")
            return {
                "space": {k: p[k] for k in ("id", "name", "authority", "revision", "valid_until")}
                if p
                else None,
                "members": [
                    {"peer_id": key, **value} for key, value in p.get("members", {}).items()
                ],
                "self_id": self.store.peer_id,
                "coordinator": self._authority(),
                "can_manage": fresh and active and member.get("role") == "manager",
                "membership": "none"
                if not p
                else "removed"
                if not active
                else "active"
                if fresh
                else "expired",
                "ai_access": self.data.get("ai_access", "local"),
                "pending": [
                    {"id": key, "action": value["action"]}
                    for key, value in self.data.get("pending", {}).items()
                    if value["action"] != "sync"
                ],
                "last_error": self.last_error or self.data.get("operation_error", ""),
                "invitation": self.data.get("last_invitation", ""),
                "invites": [
                    {
                        "id": key,
                        "expires_at": value["expires_at"],
                        "used": bool(value.get("used_by")),
                        "revoked": bool(value.get("revoked")),
                    }
                    for key, value in self.data.get("invites", {}).items()
                ]
                if self._authority()
                else [],
            }

    def create(self, name):
        with self.lock:
            if self._snapshot() or self.data.get("pending"):
                raise SpaceError("This device already belongs to a space or has a pending request.")
            self._sign(
                {
                    "kind": CAPABILITY,
                    "id": uuid.uuid4().hex,
                    "name": _name(name),
                    "authority": self.store.peer_id,
                    "box": public_key_b64(self.box),
                    "members": {
                        self.store.peer_id: {
                            "name": _name(self.store.node_name),
                            "role": "manager",
                            "removed": False,
                        }
                    },
                }
            )
            self.data["ai_access"] = "local"
            self._save()
            return self.status()

    def _manager(self, peer):
        member = self._snapshot().get("members", {}).get(peer, {})
        if member.get("removed") or member.get("role") != "manager":
            raise SpaceError("Only a management device can perform this action.")

    def _apply(self, action, args, sender):
        p = copy.deepcopy(self._snapshot())
        if action == "join":
            token = str(args.get("token", ""))
            key = hashlib.sha256(token.encode()).hexdigest()
            invitation = self.data.get("invites", {}).get(key)
            if (
                not invitation
                or invitation.get("revoked")
                or invitation.get("used_by")
                or invitation["expires_at"] <= self.clock()
            ):
                raise SpaceError(
                    "This invitation has expired, was cancelled, or has already been used."
                )
            if len(p["members"]) >= 64 and sender not in p["members"]:
                raise SpaceError("This space has reached its device limit.")
            # Ordinary invitations never promote an existing device.
            if sender in p["members"] and not p["members"][sender].get("removed"):
                raise SpaceError("This device is already a member.")
            p["members"][sender] = {
                "name": _name(args.get("name")),
                "role": "device",
                "removed": False,
            }
            invitation["used_by"] = sender
            self._sign(p)
            return {}
        if action == "sync":
            if sender not in p["members"]:
                raise SpaceError("This device is not a member.")
            # Removed devices can learn their removal, but cannot renew access.
            changed_name = (
                not p["members"][sender].get("removed")
                and args.get("name")
                and args["name"] != p["members"][sender]["name"]
            )
            if changed_name:
                p["members"][sender]["name"] = _name(args["name"])
            if changed_name or self.clock() - p["issued_at"] > 3600:
                self._sign(p)
            return {}
        self._manager(sender)
        if action == "invite":
            hours = int(args.get("hours", 24))
            if hours not in (1, 24, 72):
                raise SpaceError("Choose a 1, 24 or 72 hour invitation.")
            token = secrets.token_urlsafe(32)
            key = hashlib.sha256(token.encode()).hexdigest()
            invites = self.data.setdefault("invites", {})
            # Only active invitations are needed; consumed orders have durable replies.
            self.data["invites"] = invites = {
                k: v for k, v in invites.items() if v["expires_at"] > self.clock()
            }
            if len(invites) >= 100:
                raise SpaceError(
                    "Too many invitations. Cancel or wait for existing invitations to expire."
                )
            expires = self.clock() + hours * 3600
            invites[key] = {"expires_at": expires, "issuer": sender}
            payload = {
                "kind": "ryn.space.invite.v1",
                "space_id": p["id"],
                "space_name": p["name"],
                "authority": p["authority"],
                "box": p["box"],
                "token": token,
                "expires_at": expires,
            }
            signed = sign_payload(payload, private_key_bytes=self.store.private_key_bytes).to_dict()
            return {"invitation": INVITE_PREFIX + _encode(signed)}
        if action == "cancel_invite":
            invitation = self.data.get("invites", {}).get(str(args.get("id")))
            if not invitation:
                raise SpaceError("Invitation not found.")
            invitation["revoked"] = True
            return {}
        target = str(args.get("peer_id", ""))
        if target == p["authority"]:
            raise SpaceError(
                "The coordinator cannot be removed or demoted. Use recovery if it is lost."
            )
        if target not in p["members"] or p["members"][target].get("removed"):
            raise SpaceError("Device not found.")
        if action == "remove":
            p["members"][target]["removed"] = True
            # Previously issued invitations cannot outlive the authority of their issuer.
            for invite in self.data.get("invites", {}).values():
                if invite.get("issuer") == target:
                    invite["revoked"] = True
        elif action == "role" and args.get("role") in ("device", "manager"):
            p["members"][target]["role"] = args["role"]
            if args["role"] == "device":
                for invite in self.data.get("invites", {}).values():
                    if invite.get("issuer") == target:
                        invite["revoked"] = True
        else:
            raise SpaceError("Unsupported space action.")
        self._sign(p)
        return {}

    def act(self, action, args):
        with self.lock:
            if not self._snapshot():
                raise SpaceError("Create or join a personal space first.")
            self._manager(self.store.peer_id)
            if self._authority():
                result = self._apply(action, args, self.store.peer_id)
                self.data["operation_error"] = ""
                if "invitation" in result:
                    self.data["last_invitation"] = result["invitation"]
                self._save()
                return self.status()
            self._queue(action, args, self._snapshot())
            return self.status()

    def join(self, invitation, name):
        with self.lock:
            if self._snapshot() or self.data.get("pending"):
                raise SpaceError("This device already belongs to a space or has a pending request.")
            if not str(invitation).startswith(INVITE_PREFIX):
                raise SpaceError("Paste a complete Ryn invitation.")
            signed = SignedPayload.from_dict(_decode(invitation[len(INVITE_PREFIX) :]))
            verify_signed_payload(signed)
            p = signed.payload
            if p.get("kind") != "ryn.space.invite.v1" or p.get("authority") != signed.public_key:
                raise SpaceError("Invalid invitation signature.")
            if p["expires_at"] <= self.clock():
                raise SpaceError("This invitation has expired.")
            self._queue(
                "join",
                {"token": p["token"], "name": _name(name)},
                {"authority": p["authority"], "id": p["space_id"], "box": p["box"]},
            )
            return self.status()

    def _queue(self, action, args, target):
        if action != "sync" and all(
            item["action"] == "sync" for item in self.data.get("pending", {}).values()
        ):
            self.data["pending"] = {}
        if self.data.get("pending"):
            raise SpaceError("Wait for the current space request to finish.")
        request_id = "wo_" + uuid.uuid4().hex
        expiry = default_expires_at(24 if action == "join" else 1)
        sealed = seal_task(
            body={
                "task_id": request_id,
                "action": action,
                "args": args,
                "space_id": target["id"],
                "reply_box": public_key_b64(self.box),
            },
            task_id=request_id,
            kind="space_request",
            sender_peer_id=self.store.peer_id,
            recipient_peer_id=target["authority"],
            sender_signing_key=self.store.private_key_bytes,
            recipient_messaging_pub=target["box"],
            expires_at=expiry,
        )
        order = WorkOrder(
            work_order_id=request_id,
            requester_peer_id=self.store.peer_id,
            provider_peer_id=target["authority"],
            capability=CAPABILITY,
            operation="exchange",
            params={"envelope": sealed.to_dict()},
            network_id="space-" + target["id"],
            expires_at=expiry,
        )
        self.data["pending"] = {
            request_id: {
                "action": action,
                "target": dict(target),
                "order": sign_work_order(
                    order, private_key_bytes=self.store.private_key_bytes
                ).to_dict(),
                "requested_name": args.get("name") if action == "join" else None,
                "deadline": self.clock() + (24 * 3600 if action == "join" else 3600),
            }
        }
        self.data["operation_error"] = ""
        self._save()

    def _accept_snapshot(self, raw, target):
        p = self._verify(raw, authority=target["authority"], space_id=target["id"])
        old = self._snapshot()
        if old and (
            p["revision"] < old["revision"]
            or (p["revision"] == old["revision"] and raw != self.data["snapshot"])
        ):
            raise SpaceError("An outdated or conflicting membership record was rejected.")
        self.data["snapshot"] = raw

    def _process(self, signed, *, publish=True):
        order = verify_work_order(signed)
        with self.lock:
            p = self._snapshot()
            if (
                not self._authority()
                or order.provider_peer_id != self.store.peer_id
                or order.network_id != "space-" + p["id"]
                or order.capability != CAPABILITY
            ):
                return
            cached = self.data.setdefault("replies", {}).get(order.work_order_id)
            if cached:
                if cached["order_hash"] != signed.subject_hash:
                    raise SpaceError("Conflicting request identity.")
                response = cached["envelope"]
            else:
                outer, body = open_task(
                    order.params["envelope"],
                    recipient_peer_id=self.store.peer_id,
                    recipient_messaging_key=self.box,
                    expected_kind="space_request",
                )
                if (
                    outer["from_peer_id"] != order.requester_peer_id
                    or outer["task_id"] != order.work_order_id
                    or body.get("space_id") != p["id"]
                ):
                    raise SpaceError("Request binding mismatch.")
                before = copy.deepcopy(self.data)
                try:
                    result = self._apply(
                        body["action"], body.get("args", {}), order.requester_peer_id
                    )
                    result["snapshot"] = self.data["snapshot"]
                except (SpaceError, ValueError, KeyError) as exc:
                    self.data = before
                    result = {"error": str(exc)}
                # Seal before committing any mutation, so invalid reply keys do not burn invitations.
                try:
                    response = seal_task(
                        body={"task_id": order.work_order_id, **result},
                        task_id=order.work_order_id,
                        kind="space_response",
                        sender_peer_id=self.store.peer_id,
                        recipient_peer_id=order.requester_peer_id,
                        sender_signing_key=self.store.private_key_bytes,
                        recipient_messaging_pub=body["reply_box"],
                        expires_at=default_expires_at(24),
                    ).to_dict()
                except Exception:
                    self.data = before
                    raise
                replies = self.data.setdefault("replies", {})
                replies[order.work_order_id] = {
                    "order_hash": signed.subject_hash,
                    "envelope": response,
                    "expires": self.clock() + 48 * 3600,
                }
                self.data["replies"] = {
                    k: v for k, v in replies.items() if v["expires"] > self.clock()
                }
                self._save()
        if publish:
            self.store.publish_work_result(
                work_order_id=order.work_order_id,
                requester_peer_id=order.requester_peer_id,
                status="completed",
                result_refs={"envelope": response},
                network_id=order.network_id,
            )
            return None
        return sign_work_result(WorkResult(
            work_order_id=order.work_order_id,
            provider_peer_id=self.store.peer_id,
            requester_peer_id=order.requester_peer_id,
            status="completed",
            result_refs={"envelope": response},
            network_id=order.network_id,
        ), private_key_bytes=self.store.private_key_bytes)

    def _direct_exchange(self, order, authority):
        endpoints = self.store.lan_peer_endpoints(authority)
        key = os.environ.get("RYNMESH_NETWORK_KEY", "").strip()
        headers = {"content-type": "application/json"}
        if key:
            headers["x-ryn-auth"] = hashlib.sha256(("rynmesh-net-key:" + key).encode()).hexdigest()
        body = json.dumps(order.to_dict(), separators=(",", ":")).encode()
        for endpoint in endpoints:
            try:
                request = urllib.request.Request(
                    endpoint.rstrip("/") + "/api/peer/space/exchange",
                    data=body, headers=headers, method="POST",
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=1.5) as response:
                    raw = response.read(100001)
                if len(raw) <= 100000:
                    signed = SignedPayload.from_dict(json.loads(raw))
                    result = verify_work_result(signed)
                    if (result.provider_peer_id == authority
                            and result.requester_peer_id == self.store.peer_id
                            and result.work_order_id == order.payload["work_order_id"]
                            and result.network_id == order.payload["network_id"]):
                        return signed
            except Exception:
                continue
        return None

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                p = copy.deepcopy(self._snapshot())
                authority = self._authority()
                if authority and self.clock() - p["issued_at"] > 3600:
                    self._sign(p)
                    self._save()
                if (
                    p
                    and not authority
                    and not self.data.get("pending")
                    and self.clock() - self.last_poll >= 30
                ):
                    self._queue("sync", {"name": self.store.node_name}, p)
                    self.last_poll = self.clock()
                pending = copy.deepcopy(self.data.get("pending", {}))
            if authority:
                orders = self.store.registry.list_work_orders(
                    network_id="space-" + p["id"],
                    provider_peer_id=self.store.peer_id,
                    capability=CAPABILITY,
                    status="open",
                    max_age_hours=25,
                )
                for signed in orders[:100]:
                    try:
                        self._process(signed)
                    except Exception:
                        # Untrusted malformed envelopes must not stall other devices.
                        continue
            for request_id, request in pending.items():
                if request["deadline"] <= self.clock():
                    with self.lock:
                        self.data["pending"].pop(request_id, None)
                        self.data["operation_error"] = (
                            "Request expired. The coordinator must be online; try again."
                        )
                        self._save()
                    continue
                order = SignedPayload.from_dict(request["order"])
                direct = self._direct_exchange(order, request["target"]["authority"])
                if direct is not None:
                    results = [direct]
                else:
                    self.store.registry.submit_work_order(order)
                    results = self.store.registry.list_work_results(
                        work_order_id=request_id,
                        network_id=order.payload["network_id"],
                        requester_peer_id=self.store.peer_id,
                        provider_peer_id=request["target"]["authority"],
                    )
                for signed in results:
                    try:
                        result = verify_work_result(signed)
                        if (
                            result.provider_peer_id != request["target"]["authority"]
                            or result.requester_peer_id != self.store.peer_id
                            or result.work_order_id != request_id
                            or result.network_id != order.payload["network_id"]
                        ):
                            continue
                        outer, body = open_task(
                            result.result_refs["envelope"],
                            recipient_peer_id=self.store.peer_id,
                            recipient_messaging_key=self.box,
                            expected_kind="space_response",
                        )
                        if (
                            outer["from_peer_id"] != request["target"]["authority"]
                            or outer["task_id"] != request_id
                        ):
                            continue
                        with self.lock:
                            if request_id not in self.data.get("pending", {}):
                                break  # A local leave/cancel superseded this in-flight reply.
                            if body.get("snapshot"):
                                self._accept_snapshot(body["snapshot"], request["target"])
                                if request["action"] == "join" and request.get("requested_name"):
                                    from .settings_store import SettingsStore

                                    name = _name(request["requested_name"])
                                    SettingsStore(self.store.home / "settings.json").patch(
                                        {"node_name": name}
                                    )
                                    self.store.node_name = name
                            if body.get("invitation"):
                                self.data["last_invitation"] = body["invitation"]
                            self.data["operation_error"] = str(body.get("error", ""))
                            self.data["pending"].pop(request_id, None)
                            self._save()
                        break
                    except (ValueError, KeyError, TypeError):
                        continue
            self.last_error = ""
        except Exception:
            self.last_error = (
                "Could not reach the coordination service. Pending requests will retry."
            )
        finally:
            self.tick_lock.release()

    def set_policy(self, access):
        with self.lock:
            if access not in ("local", "space"):
                raise SpaceError("Choose local or space access.")
            if access == "space" and self.status()["membership"] != "active":
                raise SpaceError("An active personal space is required.")
            self.data["ai_access"] = access
            self._save()
            return self.status()

    def allows_ai(self, peer_id):
        if peer_id == self.store.peer_id:
            return True
        with self.lock:
            if self.data.get("ai_access", "local") != "space":
                return False

            try:
                p = self._verify(self.data["snapshot"])
                return all(
                    key in p["members"] and not p["members"][key].get("removed")
                    for key in (peer_id, self.store.peer_id)
                )
            except (ValueError, KeyError):
                return False

    def enforces_ai(self):
        with self.lock:
            return bool(self._snapshot()) or "ai_access" in self.data

    def leave(self):
        with self.lock:
            if self._authority():
                raise SpaceError(
                    "Keep the coordinator identity. Use recovery to move it to another computer."
                )
            self.data = {"ai_access": "local"}
            self._save()
            return self.status()

    def backup(self, password):
        with self.lock:
            if not self._authority():
                raise SpaceError("Export recovery on the coordinator device.")
            if not isinstance(password, str) or not 12 <= len(password) <= 256:
                raise SpaceError("Use a recovery password of 12 to 256 characters.")
            salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
            key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
            # Pending requests/invitations are not restored. Recovery preserves the coordinator ID.
            state = {"snapshot": self.data["snapshot"], "ai_access": "local"}
            raw = json.dumps(
                {
                    "state": state,
                    "identity": b64(self.store.private_key_bytes),
                    "box": b64(self.box.private_bytes_raw()),
                    "name": self.store.node_name,
                }
            ).encode()
            encrypted = AESGCM(key).encrypt(nonce, raw, b"ryn.space.recovery.v1")
            return {
                "version": 1,
                "salt": b64(salt),
                "nonce": b64(nonce),
                "ciphertext": b64(encrypted),
            }


def restore_backup(bundle, password, destination):
    """Offline recovery into a new, empty node home. Never run both copies."""
    root = Path(destination)
    if root.exists() and any(root.iterdir()):
        raise SpaceError("Recovery requires an empty directory and a stopped node.")
    if bundle.get("version") != 1 or len(str(bundle.get("ciphertext", ""))) > 200000:
        raise SpaceError("Invalid recovery file.")
    salt = base64.b64decode(bundle["salt"], validate=True)
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    raw = AESGCM(key).decrypt(
        base64.b64decode(bundle["nonce"]),
        base64.b64decode(bundle["ciphertext"]),
        b"ryn.space.recovery.v1",
    )
    payload = json.loads(raw)
    from .crypto import public_key_from_private

    identity = base64.b64decode(payload["identity"], validate=True)
    signed = SignedPayload.from_dict(payload["state"]["snapshot"])
    verify_signed_payload(signed)
    if signed.public_key != public_key_from_private(identity):
        raise SpaceError("Recovery identity mismatch.")
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity.ed25519").write_text(payload["identity"] + "\n")
    (root / "space-messaging.key").write_bytes(base64.b64decode(payload["box"], validate=True))
    _write_json_atomic(root / "personal-space.json", payload["state"])
    for name in ("identity.ed25519", "space-messaging.key", "personal-space.json"):
        (root / name).chmod(0o600)


def main():
    import argparse
    import getpass

    parser = argparse.ArgumentParser(
        description="Restore a personal-space coordinator into a fresh node home. Stop the old coordinator first."
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    restore_backup(
        json.loads(args.backup.read_text()),
        getpass.getpass("Recovery password: "),
        args.destination,
    )
    print(
        "Recovered. Start Ryn with RYNMESH_HOME pointing to the destination. Never run the old coordinator at the same time."
    )


if __name__ == "__main__":
    main()
