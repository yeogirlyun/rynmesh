from rynmesh import release
from rynmesh.crypto import public_key_from_private, sign_payload
from rynmesh.services.updater import Updater
from rynmesh.update_state import UpdateState


def _signed(version, sha, priv):
    m = release.build_manifest(version=version, wheel_sha256=sha, wheel_size=3,
                               wheel_filename="w.whl", requires_python=">=3.11",
                               published_at="2026-06-03T00:00:00Z", notes="")
    return sign_payload(m, private_key_bytes=priv).to_dict()


def _mk(tmp_path, **over):
    import os
    priv = over.pop("priv", os.urandom(32))
    pub = public_key_from_private(priv)
    state = UpdateState(tmp_path / "s.json")
    calls = {"pip": [], "reexec": 0, "preflight": []}
    deps = dict(
        fetch_latest=lambda: over.get("latest", _signed("0.3.0", "HASH", priv)),
        download_wheel=lambda sha256, filename: over.get("wheel_path", str(tmp_path / "w.whl")),
        sha256_of=lambda p: over.get("wheel_hash", "HASH"),
        pip_install=lambda p: calls["pip"].append(p),
        preflight=lambda v: calls["preflight"].append(v) or over.get("preflight_ok", True),
        reexec=lambda: calls.__setitem__("reexec", calls["reexec"] + 1),
        snapshot_current_wheel=lambda: str(tmp_path / "prev.whl"),
        pinned_pubkeys={pub},
        auto_update=lambda: over.get("auto", True),
        current_version=over.get("current", "0.2.0"),
        state=state,
        now=lambda: "2026-06-03T00:00:00Z",
        max_attempts=3,
    )
    return Updater(**deps), state, calls, priv, pub


def test_check_reports_available(tmp_path):
    u, state, *_ = _mk(tmp_path)
    out = u.check()
    assert out["available"] is True and out["version"] == "0.3.0"
    assert state.data["available_version"] == "0.3.0"


def test_check_skips_not_newer(tmp_path):
    u, *_ , priv, _ = _mk(tmp_path, current="0.3.0")
    assert u.check()["available"] is False


def test_check_rejects_unpinned(tmp_path):
    import os
    other = os.urandom(32)
    u, state, *_ = _mk(tmp_path, latest=_signed("0.3.0", "H", other))
    out = u.check()
    assert out["available"] is False and "pinned" in (state.data["last_error"] or "").lower()


def test_apply_happy_path_reexecs(tmp_path):
    u, state, calls, *_ = _mk(tmp_path)
    u.check()
    u.apply(u.check_manifest())
    assert calls["pip"] and calls["reexec"] == 1
    assert state.data["pending_version"] == "0.3.0" and state.data["pending_confirmed"] is False


def test_apply_hash_mismatch_quarantines(tmp_path):
    u, state, calls, *_ = _mk(tmp_path, wheel_hash="WRONG")
    u.check()
    u.apply(u.check_manifest())
    assert calls["reexec"] == 0 and state.is_quarantined("0.3.0")


def test_apply_preflight_fail_reverts_and_quarantines(tmp_path):
    u, state, calls, *_ = _mk(tmp_path, preflight_ok=False)
    u.check()
    u.apply(u.check_manifest())
    assert calls["reexec"] == 0 and state.is_quarantined("0.3.0")
    assert calls["pip"][-1].endswith("prev.whl")


def test_check_skips_quarantined(tmp_path):
    u, state, *_ = _mk(tmp_path)
    state.quarantine("0.3.0")
    assert u.check()["available"] is False


def test_on_startup_confirms_then_mark_serving(tmp_path):
    u, state, calls, *_ = _mk(tmp_path)
    state.set(pending_version="0.3.0", pending_confirmed=False, attempts=0)
    u.on_startup()
    assert state.data["attempts"] == 1 and calls["reexec"] == 0
    u.mark_serving()
    assert state.data["pending_confirmed"] is True and state.data["last_good"] == "0.2.0"


def test_on_startup_rolls_back_after_max_attempts(tmp_path):
    u, state, calls, *_ = _mk(tmp_path)
    state.set(pending_version="0.3.0", pending_confirmed=False, attempts=3,
              previous_wheel=str(tmp_path / "prev.whl"))
    u.on_startup()
    assert calls["pip"] and calls["pip"][-1].endswith("prev.whl")
    assert state.is_quarantined("0.3.0") and calls["reexec"] == 1
    assert state.data["pending_version"] is None


def test_on_startup_rollback_install_failure_does_not_loop(tmp_path):
    u, state, calls, *_ = _mk(tmp_path)
    # make pip_install raise ONLY for the previous-wheel downgrade
    def boom(p):
        if p.endswith("prev.whl"):
            raise RuntimeError("disk full")
        calls["pip"].append(p)
    u._pip_install = boom
    state.set(pending_version="0.3.0", pending_confirmed=False, attempts=3,
              previous_wheel=str(tmp_path / "prev.whl"))
    u.on_startup()  # must NOT raise
    assert state.data["pending_version"] is None          # loop broken
    assert state.is_quarantined("0.3.0")
    assert calls["reexec"] == 0                            # no reexec on failed downgrade
    assert "rollback install failed" in (state.data["last_error"] or "")


def test_apply_records_installed_wheel_for_future_rollback(tmp_path):
    recorded = []
    u, state, calls, priv, pub = _mk(tmp_path)
    u._record_installed = lambda wheel, version: recorded.append((wheel, version))
    u.check()
    u.apply(u.check_manifest())
    assert recorded and recorded[-1][1] == "0.3.0"  # the new version's wheel was cached


def test_apply_passes_wheel_filename_to_download(tmp_path):
    seen = {}
    u, state, calls, priv, pub = _mk(tmp_path)
    u._download_wheel = lambda sha256, filename: (seen.update(sha256=sha256, filename=filename), str(tmp_path / "w.whl"))[1]
    u.check()
    u.apply(u.check_manifest())
    assert seen["filename"] == "w.whl"  # the manifest's wheel_filename, not a sha256 name
