"""Offline compatibility packages retain upstream pins and distrust mutable receipts."""

import hashlib
import io
import json
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rynmesh.node_apps import craft_compat as compat
from rynmesh.node_apps import craft_host as host
from rynmesh.node_apps.craft_catalog import CRAFT


def sha(data):
    return hashlib.sha256(data).hexdigest()


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        for name, content in files.items():
            member = zipfile.ZipInfo(name)
            member.filename = name  # Preserve hostile backslashes on Windows.
            zipped.writestr(member, content)
    return output.getvalue()


@pytest.fixture
def package(tmp_path, monkeypatch):
    base = archive({"gridcraft/index.html": b"<title>Upstream</title>", "gridcraft/LICENSE": b"Upstream license"})
    monkeypatch.setitem(CRAFT, "gridcraft", {**CRAFT["gridcraft"], "web_sha256": sha(base)})
    cache = tmp_path / "cache"
    host.install(cache, "gridcraft", download=lambda url, path: path.write_bytes(base))
    upstream = host.installed(cache, "gridcraft")
    files = {
        "gridcraft/index.html": b"<title>Chinese compatible</title>",
        "gridcraft/editor.wasm": b"\x00asm\x01\x00\x00\x00",
        "gridcraft/LICENSE": b"Upstream license",
        "gridcraft/OFL.txt": b"Fixture font license",
        "gridcraft/build-provenance.json": b'{"upstream_commit":"fixture","font":"fixture"}',
    }
    raw = archive(files)
    manifest = {"entry": "gridcraft", "files": {name: sha(data) for name, data in files.items()}}
    pin = {
        "id": "ryn-cjk-1", "base_version": "v0.6.1", "base_web_sha256": sha(base),
        "archive_sha256": sha(raw), "files_manifest_sha256": compat.manifest_digest(manifest),
    }
    monkeypatch.setitem(compat.COMPAT, "gridcraft", pin)
    local = tmp_path / "compat.zip"
    local.write_bytes(raw)
    root = cache / "gridcraft/v0.6.1/compatibility/ryn-cjk-1"
    return SimpleNamespace(cache=cache, upstream=upstream, path=local, root=root, pin=pin, files=files)


def test_install_selects_verified_compatibility_and_retains_original(package, monkeypatch):
    base_root = package.cache / "gridcraft/v0.6.1"
    receipt = (base_root / "receipt.json").read_bytes()
    original = {p.relative_to(package.upstream).as_posix(): p.read_bytes() for p in package.upstream.rglob("*") if p.is_file()}
    monkeypatch.setattr(host, "download_to", lambda *_: pytest.fail("compatibility must never download"))
    result = compat.install(package.cache, "gridcraft", package.path)
    assert result["state"] == "ready" and result["compatibility"] == "ryn-cjk-1"
    web = host.installed(package.cache, "gridcraft")
    assert web == (package.root / "payload/gridcraft").resolve()
    assert (base_root / "receipt.json").read_bytes() == receipt
    assert original == {p.relative_to(package.upstream).as_posix(): p.read_bytes() for p in package.upstream.rglob("*") if p.is_file()}
    assert "font license" in (web / "OFL.txt").read_text()
    assert (web / "build-provenance.json").is_file()
    package.path.unlink()
    assert compat.install(package.cache, "gridcraft", package.path)["state"] == "ready"
    with TestClient(host.create_editor(package.cache, "gridcraft", public_host="testserver")) as client:
        response = client.get("/")
        assert response.status_code == 200 and "Chinese compatible" in response.text
        assert "connect-src 'self'" in response.headers["content-security-policy"]
        assert client.get("/editor.wasm").headers["content-type"] == "application/wasm"
        assert client.get("/receipt.json").status_code == 404


def test_missing_compatibility_preserves_upstream_behavior_and_does_no_work(package):
    assert compat.installed(package.cache, "gridcraft") is None
    assert host.installed(package.cache, "gridcraft") == package.upstream
    assert not package.root.parent.exists()
    assert compat.installed(package.cache, "wordcraft") is None


def test_payload_and_receipt_tampering_cannot_replace_the_pinned_manifest(package):
    compat.install(package.cache, "gridcraft", package.path)
    editor = package.root / "payload/gridcraft/index.html"
    editor.write_bytes(b"untrusted replacement")
    receipt_path = package.root / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["files"]["gridcraft/index.html"] = host.digest(editor)
    receipt_path.write_text(json.dumps(receipt))
    for verify in (
        lambda: host.installed(package.cache, "gridcraft"),
        lambda: compat.install(package.cache, "gridcraft", package.path),
        lambda: host.create_editor(package.cache, "gridcraft"),
    ):
        with pytest.raises(host.CraftError, match="craft_compat_not_verified"):
            verify()


@pytest.mark.parametrize("damage", ["missing_receipt", "extra_file", "bad_entry"])
def test_existing_damaged_package_fails_closed(package, damage):
    compat.install(package.cache, "gridcraft", package.path)
    if damage == "missing_receipt":
        (package.root / "receipt.json").unlink()
    elif damage == "extra_file":
        (package.root / "payload/injected.js").write_text("malicious")
    else:
        receipt = json.loads((package.root / "receipt.json").read_text())
        receipt["entry"] = "../.."
        (package.root / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(host.CraftError, match="craft_compat_not_verified"):
        host.installed(package.cache, "gridcraft")


def test_wrong_archive_is_rejected_and_stage_removed(package):
    package.path.write_bytes(b"unsigned local archive")
    with pytest.raises(host.CraftError, match="craft_compat_checksum_mismatch"):
        compat.install(package.cache, "gridcraft", package.path)
    assert not package.root.exists()
    assert not list(package.root.parent.glob(".install-*"))
    assert host.installed(package.cache, "gridcraft") == package.upstream


def test_archive_pin_alone_does_not_authorize_another_file_manifest(package):
    raw = archive({"gridcraft/index.html": b"Other build"})
    package.path.write_bytes(raw)
    package.pin["archive_sha256"] = sha(raw)
    with pytest.raises(host.CraftError, match="craft_compat_not_verified"):
        compat.install(package.cache, "gridcraft", package.path)
    assert not package.root.exists()


def test_base_pin_mismatch_and_original_tampering_are_rejected(package):
    package.pin["base_web_sha256"] = "0" * 64
    with pytest.raises(host.CraftError, match="craft_compat_base_mismatch"):
        compat.install(package.cache, "gridcraft", package.path)
    package.pin["base_web_sha256"] = CRAFT["gridcraft"]["web_sha256"]
    compat.install(package.cache, "gridcraft", package.path)
    package.pin["base_web_sha256"] = "0" * 64
    with pytest.raises(host.CraftError, match="craft_compat_base_mismatch"):
        host.installed(package.cache, "gridcraft")
    package.pin["base_web_sha256"] = CRAFT["gridcraft"]["web_sha256"]
    (package.upstream / "index.html").write_text("tampered upstream")
    with pytest.raises(host.CraftError, match="craft_build_not_verified"):
        host.installed(package.cache, "gridcraft")


def test_unpinned_and_unsupported_local_packages_are_rejected(package):
    package.pin["archive_sha256"] = "PENDING_COMPAT_ARCHIVE_SHA256"
    with pytest.raises(host.CraftError, match="craft_compat_not_pinned"):
        compat.install(package.cache, "gridcraft", package.path)
    with pytest.raises(host.CraftError, match="craft_compat_unsupported"):
        compat.install(package.cache, "photocraft", package.path)
    assert not package.root.parent.exists()


@pytest.mark.parametrize("name", ["../escaped.txt", "/absolute.txt", "C:/escape.txt", "gridcraft/../../escaped.txt", "gridcraft\\escape.txt"])
def test_hostile_archive_paths_are_rejected_even_with_a_matching_archive_pin(package, name):
    raw = archive({**package.files, name: b"must never be written"})
    package.path.write_bytes(raw)
    package.pin["archive_sha256"] = sha(raw)
    with pytest.raises(host.CraftError, match="craft_unsafe_archive"):
        compat.install(package.cache, "gridcraft", package.path)
    assert not package.root.exists()
    assert not list(package.root.parent.glob(".install-*"))
    assert not list(package.cache.rglob("escaped.txt"))


def test_compatibility_identifier_cannot_escape_the_base_directory(package):
    package.pin["id"] = "../../outside"
    with pytest.raises(host.CraftError, match="craft_compat_not_verified"):
        compat.install(package.cache, "gridcraft", package.path)
    assert not package.root.parent.exists()


def test_payload_symlink_is_rejected(package, tmp_path):
    compat.install(package.cache, "gridcraft", package.path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = package.root / "payload/link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this Windows installation")
    with pytest.raises(host.CraftError, match="craft_compat_not_verified"):
        host.installed(package.cache, "gridcraft")


def test_install_compat_cli_uses_explicit_local_archive(package, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rynmesh-craft", "install-compat", "--app", "gridcraft", "--cache", str(package.cache), "--archive", str(package.path)])
    host.main()
    assert json.loads(capsys.readouterr().out)["compatibility"] == "ryn-cjk-1"
    assert host.installed(package.cache, "gridcraft") != package.upstream


def test_install_compat_cli_requires_archive(package, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["rynmesh-craft", "install-compat", "--app", "gridcraft", "--cache", str(package.cache)])
    with pytest.raises(SystemExit) as error:
        host.main()
    assert error.value.code == 2
    assert "--archive is required" in capsys.readouterr().err


def test_module_cli_reports_compatibility_errors_without_a_traceback(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "rynmesh.node_apps.craft_host", "install-compat", "--app", "photocraft", "--cache", str(tmp_path), "--archive", str(tmp_path / "local.zip")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "Craft: craft_compat_unsupported" in result.stderr
    assert "Traceback" not in result.stderr


def test_all_three_office_compatibility_specs_match_their_fixed_upstream_releases():
    assert set(compat.COMPAT) == {"gridcraft", "wordcraft", "pdfcraft"}
    for slug, spec in compat.COMPAT.items():
        compat._check_spec(slug, spec)
        assert spec["id"] == "ryn-cjk-1"


def test_offline_office_packages_coexist_without_replacing_each_others_bytes(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    retained = {}
    for slug in ("gridcraft", "wordcraft", "pdfcraft"):
        base = archive({f"{slug}/index.html": ("Upstream " + slug).encode()})
        monkeypatch.setitem(CRAFT, slug, {**CRAFT[slug], "web_sha256": sha(base)})
        host.install(cache, slug, download=lambda url, path, content=base: path.write_bytes(content))
        files = {f"{slug}/index.html": ("Chinese " + slug).encode(), f"{slug}/font.wasm": b"\0asm", f"{slug}/OFL.txt": b"Font license"}
        raw = archive(files)
        manifest = {"entry": slug, "files": {name: sha(value) for name, value in files.items()}}
        monkeypatch.setitem(compat.COMPAT, slug, {"id": "ryn-cjk-1", "base_version": CRAFT[slug]["tag"],
            "base_web_sha256": sha(base), "archive_sha256": sha(raw), "files_manifest_sha256": compat.manifest_digest(manifest)})
        local = tmp_path / (slug + ".zip")
        local.write_bytes(raw)
        compat.install(cache, slug, local)
        root = cache / slug / CRAFT[slug]["tag"]
        retained[slug] = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        for previous, expected in retained.items():
            previous_root = cache / previous / CRAFT[previous]["tag"]
            assert expected == {path.relative_to(previous_root).as_posix(): path.read_bytes() for path in previous_root.rglob("*") if path.is_file()}
            assert host.installed(cache, previous) == (previous_root / "compatibility/ryn-cjk-1/payload" / previous).resolve()
