"""Local Rynmesh peer model.

This module is the phase-1 transport seam: it proves the protocol behavior
with in-memory peers before replacing the backing calls with libp2p/DHT
plumbing. Honest peers only store or serve full media when the signed manifest
passes the receipt policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .crypto import SignedPayload, public_key_from_private, sha256_bytes
from .manifest import ManifestError, require_propagation_allowed, validate_manifest
from .safety import SafetyPolicy


class PeerError(RuntimeError):
    pass


@dataclass
class StoredClip:
    manifest: SignedPayload
    preview: bytes = b""
    media: bytes = b""
    source_peer_id: str = ""


@dataclass
class RynmeshPeer:
    """A minimal honest peer used by tests and local demos."""

    private_key_bytes: bytes
    policy: SafetyPolicy = field(default_factory=SafetyPolicy)
    banned_peers: set[str] = field(default_factory=set)
    _clips: dict[str, StoredClip] = field(default_factory=dict, init=False)

    @property
    def peer_id(self) -> str:
        return public_key_from_private(self.private_key_bytes)

    def ban_peer(self, peer_id: str) -> None:
        self.banned_peers.add(peer_id)

    def announce(
        self,
        signed_manifest: SignedPayload,
        *,
        source_peer_id: str,
        preview: bytes = b"",
    ) -> str:
        if source_peer_id in self.banned_peers:
            raise PeerError("source_peer_banned")
        manifest = require_propagation_allowed(signed_manifest, policy=self.policy)
        if preview:
            preview_hash = sha256_bytes(preview)
            if manifest.asset.preview_hash and manifest.asset.preview_hash != preview_hash:
                raise PeerError("preview_hash_mismatch")
        self._clips[manifest.asset.clip_id] = StoredClip(
            manifest=signed_manifest,
            preview=preview,
            source_peer_id=source_peer_id,
        )
        return manifest.asset.clip_id

    def seed_full_media(self, clip_id: str, media: bytes) -> None:
        stored = self._clips.get(clip_id)
        if stored is None:
            raise PeerError("clip_not_announced")
        validation = validate_manifest(stored.manifest, policy=self.policy)
        if not validation.propagates or validation.manifest is None:
            raise PeerError("manifest_not_allowed")
        if validation.manifest.asset.media_hash != sha256_bytes(media):
            raise PeerError("media_hash_mismatch")
        stored.media = bytes(media)

    def has_clip(self, clip_id: str) -> bool:
        return clip_id in self._clips

    def has_full_media(self, clip_id: str) -> bool:
        stored = self._clips.get(clip_id)
        return bool(stored and stored.media)

    def get_preview(self, clip_id: str) -> tuple[SignedPayload, bytes]:
        stored = self._clips.get(clip_id)
        if stored is None:
            raise PeerError("clip_not_found")
        return stored.manifest, stored.preview

    def fetch_preview_from(self, provider: "RynmeshPeer", clip_id: str) -> bytes:
        if provider.peer_id in self.banned_peers:
            raise PeerError("provider_banned")
        manifest, preview = provider.get_preview(clip_id)
        self.announce(manifest, source_peer_id=provider.peer_id, preview=preview)
        return preview

    def fetch_full_media_from(self, provider: "RynmeshPeer", clip_id: str) -> bytes:
        if provider.peer_id in self.banned_peers:
            raise PeerError("provider_banned")
        manifest, preview = provider.get_preview(clip_id)
        known = self._clips.get(clip_id)
        if known is None:
            self.announce(manifest, source_peer_id=provider.peer_id, preview=preview)
        if not provider.has_full_media(clip_id):
            raise PeerError("provider_has_no_full_media")
        media = provider._clips[clip_id].media
        self.seed_full_media(clip_id, media)
        return media

    def publish_local_clip(
        self,
        signed_manifest: SignedPayload,
        *,
        preview: bytes,
        media: bytes,
    ) -> str:
        try:
            clip_id = self.announce(signed_manifest, source_peer_id=self.peer_id, preview=preview)
            self.seed_full_media(clip_id, media)
            return clip_id
        except ManifestError as exc:
            raise PeerError(str(exc)) from exc
