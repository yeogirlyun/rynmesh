"""Node-side auto-updater: verify, install, self-restart, auto-rollback."""
from __future__ import annotations

from typing import Any, Callable

from rynmesh import release
from rynmesh.crypto import SignedPayload
from rynmesh.update_state import UpdateState


class Updater:
    def __init__(
        self,
        *,
        fetch_latest: Callable[[], dict[str, Any] | None],
        download_wheel: Callable[[str, str], str],
        sha256_of: Callable[[str], str],
        pip_install: Callable[[str], None],
        preflight: Callable[[str], bool],
        reexec: Callable[[], None],
        snapshot_current_wheel: Callable[[], str | None],
        record_installed: "Callable[[str, str], None] | None" = None,
        pinned_pubkeys: set[str] = frozenset(),
        auto_update: Callable[[], bool],
        current_version: str,
        state: UpdateState,
        now: Callable[[], str],
        max_attempts: int = 3,
    ) -> None:
        self._fetch_latest = fetch_latest
        self._download_wheel = download_wheel
        self._sha256_of = sha256_of
        self._pip_install = pip_install
        self._preflight = preflight
        self._reexec = reexec
        self._snapshot = snapshot_current_wheel
        self._record_installed = record_installed or (lambda wheel, version: None)
        self._pinned = set(pinned_pubkeys)
        self._auto_update = auto_update
        self._current = current_version
        self.state = state
        self._now = now
        self._max_attempts = max_attempts
        self._latest_manifest: dict[str, Any] | None = None

    def status(self) -> dict[str, Any]:
        d = self.state.data
        return {
            "currentVersion": self._current,
            "availableVersion": d.get("available_version"),
            "autoUpdate": bool(self._auto_update()),
            "lastCheck": d.get("last_check"),
            "lastError": d.get("last_error"),
        }

    def check_manifest(self) -> dict[str, Any] | None:
        return self._latest_manifest

    def check(self) -> dict[str, Any]:
        self.state.set(last_check=self._now(), last_error=None)
        try:
            raw = self._fetch_latest()
        except Exception as exc:
            self.state.set(last_error=f"fetch_failed: {exc}")
            return {"available": False, "current": self._current}
        if not raw:
            return {"available": False, "current": self._current}
        try:
            payload = release.verify_manifest(SignedPayload.from_dict(raw), self._pinned)
        except Exception as exc:
            self.state.set(last_error=f"verify_failed: {exc}")
            return {"available": False, "current": self._current}
        version = payload["version"]
        if self.state.is_quarantined(version) or not release.is_newer(version, self._current):
            return {"available": False, "current": self._current, "version": version}
        self._latest_manifest = payload
        self.state.set(available_version=version)
        return {"available": True, "version": version, "current": self._current}

    def apply(self, manifest: dict[str, Any] | None) -> dict[str, Any]:
        if not manifest:
            return {"applied": False, "error": "no manifest"}
        version = manifest["version"]
        try:
            wheel_path = self._download_wheel(manifest["wheel_sha256"], manifest["wheel_filename"])
            if self._sha256_of(wheel_path) != manifest["wheel_sha256"]:
                self.state.quarantine(version)
                self.state.set(last_error="wheel hash mismatch")
                return {"applied": False, "error": "hash mismatch"}
            previous = self._snapshot()
            self._pip_install(wheel_path)
            if not self._preflight(version):
                if previous:
                    self._pip_install(previous)
                self.state.quarantine(version)
                self.state.set(last_error="preflight failed")
                return {"applied": False, "error": "preflight failed"}
            self._record_installed(wheel_path, version)
            self.state.set(pending_version=version, pending_confirmed=False, attempts=0,
                           previous_wheel=previous, previous_version=self._current)
            self._reexec()
            return {"applied": True, "version": version}
        except Exception as exc:
            self.state.set(last_error=f"apply_failed: {exc}")
            return {"applied": False, "error": str(exc)}

    def on_startup(self) -> None:
        d = self.state.data
        if not d.get("pending_version") or d.get("pending_confirmed"):
            return
        attempts = int(d.get("attempts") or 0) + 1
        self.state.set(attempts=attempts)
        if attempts > self._max_attempts:
            bad = d.get("pending_version")
            prev = d.get("previous_wheel")
            rollback_ok = True
            if prev:
                try:
                    self._pip_install(prev)
                except Exception as exc:  # downgrade failed — stop the loop, don't brick on retry
                    rollback_ok = False
                    self.state.set(last_error=f"rollback install failed for {bad}: {exc}")
            if bad:
                self.state.quarantine(bad)
            # Clear pending REGARDLESS so we never re-enter the rollback loop.
            self.state.set(pending_version=None, pending_confirmed=True, attempts=0,
                           last_error=(self.state.data.get("last_error")
                                       if not rollback_ok
                                       else f"rolled back {bad} after crash loop"))
            if rollback_ok:
                self._reexec()

    def mark_serving(self) -> None:
        d = self.state.data
        if d.get("pending_version") and not d.get("pending_confirmed"):
            self.state.set(pending_confirmed=True, last_good=self._current,
                           previous_wheel=None, attempts=0)
