"""Run the real shell entry points with isolated compiler and build fixtures."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "webapp/src-tauri/scripts"
SHELL = shutil.which("sh")
pytestmark = pytest.mark.skipif(SHELL is None, reason="POSIX desktop build scripts")


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def sandbox(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sed", "dirname", "mktemp", "rm"):
        tool = shutil.which(name)
        if tool is None:
            pytest.skip(f"desktop build fixture needs {name}")
        (bin_dir / name).symlink_to(tool)
    return {"PATH": str(bin_dir), "HOME": str(tmp_path / "home")}


def compiler(path, triple="aarch64-apple-darwin", exit_code=0):
    return executable(path, f"printf '%s\\n' 'host: {triple}'\nexit {exit_code}\n")


def resolve(env):
    return subprocess.run([SHELL, str(SCRIPTS / "rust-host-triple.sh")],
                          env=env, capture_output=True, text=True, timeout=10)


def test_active_path_toolchain_wins_over_legacy_home(sandbox):
    compiler(Path(sandbox["PATH"]) / "rustc")
    compiler(Path(sandbox["HOME"]) / ".cargo/bin/rustc", "x86_64-apple-darwin")
    result = resolve(sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "aarch64-apple-darwin\n"


def test_explicit_compiler_with_spaces_overrides_path(sandbox, tmp_path):
    compiler(Path(sandbox["PATH"]) / "rustc", "x86_64-apple-darwin")
    sandbox["RUSTC"] = str(compiler(tmp_path / "custom toolchain/rust compiler"))
    result = resolve(sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "aarch64-apple-darwin\n"


@pytest.mark.parametrize("location", ["cargo_home", "legacy_home"])
def test_toolchain_installation_fallback_without_path(sandbox, tmp_path, location):
    if location == "cargo_home":
        sandbox["CARGO_HOME"] = str(tmp_path / "custom cargo home")
        target = Path(sandbox["CARGO_HOME"]) / "bin/rustc"
    else:
        target = Path(sandbox["HOME"]) / ".cargo/bin/rustc"
    compiler(target)
    result = resolve(sandbox)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "aarch64-apple-darwin\n"


def test_invalid_explicit_compiler_is_not_silently_replaced(sandbox, tmp_path):
    compiler(Path(sandbox["PATH"]) / "rustc")
    sandbox["RUSTC"] = str(tmp_path / "missing-compiler")
    result = resolve(sandbox)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "could not query rustc" in result.stderr


def test_failed_compiler_cannot_supply_a_successful_host(sandbox):
    compiler(Path(sandbox["PATH"]) / "rustc", exit_code=7)
    result = resolve(sandbox)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "could not query rustc" in result.stderr


@pytest.mark.parametrize("metadata", ["release: 1.0", "host: bad/path", "host: one\nhost: two"])
def test_malformed_compiler_metadata_is_rejected(sandbox, metadata):
    executable(Path(sandbox["PATH"]) / "rustc", f"printf '%s\\n' '{metadata}'\n")
    result = resolve(sandbox)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "could not resolve rust host triple" in result.stderr


def test_missing_compiler_has_an_actionable_error(sandbox):
    result = resolve(sandbox)
    assert result.returncode != 0
    assert result.stdout == ""
    assert "rustc not found" in result.stderr


@pytest.mark.parametrize(("script", "explicit_target"), [
    ("build-sidecar.sh", False),
    ("fetch-llama-runtime.sh", False),
    ("fetch-llama-runtime.sh", True),
])
def test_build_entry_points_use_active_toolchain_before_building(sandbox, tmp_path, script, explicit_target):
    # Stop at Python: no dependency installation, packaging or download occurs.
    stop = executable(Path(sandbox["PATH"]) / "python3", "echo PYTHON_REACHED >&2\nexit 73\n")
    sandbox["RYNMESH_BUILD_PYTHON"] = str(stop)
    if not explicit_target:
        compiler(Path(sandbox["PATH"]) / "rustc")
    script_dir = tmp_path / "fixture/webapp/src-tauri/scripts"
    script_dir.mkdir(parents=True)
    for source in (SCRIPTS / script, SCRIPTS / "rust-host-triple.sh"):
        if source.is_file():
            shutil.copy2(source, script_dir / source.name)
    args = ["aarch64-apple-darwin"] if explicit_target else []
    result = subprocess.run([SHELL, str(script_dir / script), *args], env=sandbox,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 73, result.stderr
    assert "PYTHON_REACHED" in result.stderr
    assert not list((tmp_path / "fixture").rglob("runtime.json"))
