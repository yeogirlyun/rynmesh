import base64
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("desktop_update_manifest", Path(__file__).parents[1] / "scripts/prepare_desktop_update.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_complete_release_has_immutable_urls_and_all_platform_signatures(tmp_path):
    output = tmp_path / "output"
    for platform in release.PLATFORMS:
        bundle = tmp_path / platform
        bundle.mkdir()
        name = "Ryn.exe" if platform.startswith("windows") else "Ryn.app.tar.gz"
        (bundle / name).write_bytes(b"installer")
        (bundle / (name + ".sig")).write_text(base64.b64encode(b"signature fixture").decode())
        release.stage(bundle, output, "0.8.0", platform)
    result = release.assemble(output, "0.8.0", "Fixes")
    assert set(result["platforms"]) == release.PLATFORMS
    assert all("/releases/download/v0.8.0/" in p["url"] for p in result["platforms"].values())
    assert json.loads((output / "desktop-latest.json").read_text())["version"] == "0.8.0"
    with pytest.raises(ValueError, match="Mixed release"):
        release.assemble(output, "0.9.0")


def test_missing_platform_and_unsigned_package_cannot_publish(tmp_path):
    with pytest.raises(FileNotFoundError):
        release.assemble(tmp_path, "0.8.0")
    (tmp_path / "Ryn.exe").write_bytes(b"unsigned")
    with pytest.raises(ValueError, match="signed updater"):
        release.stage(tmp_path, tmp_path / "output", "0.8.0", "windows-x86_64")
