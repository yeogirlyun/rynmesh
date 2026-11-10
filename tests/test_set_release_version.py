import difflib
import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("set_release_version", ROOT / "scripts" / "set_release_version.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
FILES = ["pyproject.toml", "rynmesh/__init__.py", "webapp/src-tauri/Cargo.toml", "webapp/src-tauri/Cargo.lock",
         "webapp/src-tauri/tauri.conf.json", "webapp/package.json", "webapp/package-lock.json"]


def _copy(tmp_path):
    for name in FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_the_repository_is_on_one_version():
    current = module.apply(module.STABLE.search((ROOT / "webapp/src-tauri/tauri.conf.json").read_text()).group(0), check=True)
    assert current == []


def test_sets_every_release_version_and_nothing_else(tmp_path):
    root = _copy(tmp_path)
    before = {name: (root / name).read_bytes() for name in FILES}
    assert sorted(module.apply("12.34.56", root=root)) == sorted(FILES)
    assert module.apply("12.34.56", root=root, check=True) == []
    for name in FILES:
        old = before[name].decode().splitlines()
        new = (root / name).read_bytes().decode().splitlines()
        changed = [line for line in difflib.ndiff(old, new) if line.startswith(("+ ", "- "))]
        expected = 4 if name == "webapp/package-lock.json" else 2
        assert len(changed) == expected, (name, changed)
        assert all("12.34.56" in line for line in changed if line.startswith("+ "))
        # Line endings are kept as they were.
        assert (b"\r\n" in before[name]) == (b"\r\n" in (root / name).read_bytes())
    # Another crate that happens to share the old version is untouched.
    lock = (root / "webapp/src-tauri/Cargo.lock").read_text()
    assert lock.count('version = "12.34.56"') == 1


def test_rejects_anything_but_a_stable_version(tmp_path):
    for value in ["0.9", "v0.9.0", "0.9.0-rc.1", "0.9.0\n"]:
        with pytest.raises(ValueError):
            module.apply(value, root=_copy(tmp_path), check=True)
