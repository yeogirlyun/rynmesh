"""Focused unit tests for standalone Ryn JS runtime resource-profile preparation and validation.

Status: UNMOUNTED standalone resource-profile test suite.
All tests in this module operate on deterministic synthetic fixtures in temporary directories
without executing Node.js, npm, or any subprocesses, and without touching network or PATH.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from rynmesh.agent_runtime.js_runtime.resources import (
    BUILD_RECEIPT_FILENAME,
    MANIFEST_FILENAME,
    NODE_EXECUTABLE_REL_PATH_WIN32,
    PINNED_QUICKJS_VERSION,
    PINNED_RUNTIME_PACKAGE_NAME,
    QUICKJS_VARIANT_PACKAGES,
    RELEASE_SYNC_WASM_REL_PATH,
    REQUIRED_PRODUCTION_PACKAGES,
    SKY_SHIM_REL_PATH,
    WORKER_REL_PATH,
    ExpectedJsRuntimeProfileSpec,
    JsResourceError,
    expected_profile_directory_relpaths,
    open_production_js_runtime_lease,
    parse_strict_bounded_json,
    resolve_development_js_runtime_profile,
    resolve_production_js_runtime_profile,
    sanitize_invocation_environment,
    validate_manifest_dict,
    validate_relative_resource_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCES_PY_PATH = REPO_ROOT / "rynmesh" / "agent_runtime" / "js_runtime" / "resources.py"
PREPARE_SCRIPT_PATH = REPO_ROOT / "webapp" / "src-tauri" / "scripts" / "prepare-js-runtime.py"


def _load_preparer_module() -> Any:
    spec = importlib.util.spec_from_file_location("prepare_js_runtime_script", PREPARE_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PREPARER = _load_preparer_module()
prepare_js_runtime_profile = _PREPARER.prepare_js_runtime_profile
build_arg_parser = _PREPARER.build_arg_parser
preparer_main = _PREPARER.main


FAKE_WASM_BYTES = b"\x00asm\x01\x00\x00\x00\x01\x04\x01\x60\x00\x00\x03\x02\x01\x00\x0a\x04\x01\x02\x00\x0b"
FAKE_NODE_PE_BYTES = b"MZ" + (b"\x90" * 126) + b"PE\x00\x00FakeRynNode22.23.3WinX64BinaryPayload"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_fake_source_fixture(base_dir: Path) -> dict[str, Any]:
    """Create a complete synthetic Node input directory and `native/ryn_js_runtime` tree with all 4 variants."""
    node_input_dir = base_dir / "node-input-v22.23.3-win-x64"
    node_input_dir.mkdir(parents=True, exist_ok=False)

    node_exe = node_input_dir / "node.exe"
    node_exe.write_bytes(FAKE_NODE_PE_BYTES)
    node_sha = _sha256_bytes(FAKE_NODE_PE_BYTES)
    node_size = len(FAKE_NODE_PE_BYTES)

    node_license = node_input_dir / "LICENSE"
    node_license.write_text("Node.js MIT / OpenJS Foundation License Fixture\n", encoding="utf-8")

    node_readme = node_input_dir / "README.md"
    node_readme.write_text("# Node.js v22.23.3 Fixture README\n", encoding="utf-8")

    provenance_path = node_input_dir / "input-provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "recorded_at": "2026-10-19T18:32:47+08:00",
                "official_archive_url": "https://nodejs.org/dist/v22.23.3/node-v22.23.3-win-x64.zip",
                "official_checksums_url": "https://nodejs.org/dist/v22.23.3/SHASUMS256.txt",
                "archive_sha256": "2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71",
                "archive_checksum_verified": True,
                "node_sha256": node_sha,
                "node_bytes": node_size,
                "file_version": "22.23.3",
                "signature_status": "Valid",
                "signer_subject": "CN=OpenJS Foundation, O=OpenJS Foundation",
                "extracted_only": ["node.exe", "LICENSE", "README.md"],
                "system_node_or_path_changed": False,
                "executed": False,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    runtime_pkg_dir = base_dir / "ryn_js_runtime"
    (runtime_pkg_dir / "src").mkdir(parents=True, exist_ok=False)

    (runtime_pkg_dir / "package.json").write_text(
        json.dumps(
            {
                "name": PINNED_RUNTIME_PACKAGE_NAME,
                "version": "0.1.0",
                "private": True,
                "type": "module",
                "dependencies": {
                    "quickjs-emscripten": PINNED_QUICKJS_VERSION,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    lock_packages: dict[str, Any] = {
        "": {
            "name": PINNED_RUNTIME_PACKAGE_NAME,
            "version": "0.1.0",
            "dependencies": {
                "quickjs-emscripten": PINNED_QUICKJS_VERSION,
            },
        }
    }
    for pkg_name, ver in sorted(REQUIRED_PRODUCTION_PACKAGES.items()):
        fake_integrity = "sha512-" + hashlib.sha256(pkg_name.encode("utf-8")).hexdigest()
        lock_packages[f"node_modules/{pkg_name}"] = {
            "version": ver,
            "resolved": f"https://registry.example.invalid/{pkg_name}-{ver}.tgz",
            "integrity": fake_integrity,
            "license": "MIT",
        }

    (runtime_pkg_dir / "package-lock.json").write_text(
        json.dumps(
            {
                "name": PINNED_RUNTIME_PACKAGE_NAME,
                "version": "0.1.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": lock_packages,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (runtime_pkg_dir / "src" / "worker.mjs").write_text(
        'import { newQuickJSWASMModule, RELEASE_SYNC } from "quickjs-emscripten";\n'
        'import { SKY_SHIM_MODULE_SOURCE } from "./sky-shim.mjs";\n'
        "export const WORKER_FIXTURE = true;\n",
        encoding="utf-8",
    )
    (runtime_pkg_dir / "src" / "sky-shim.mjs").write_text(
        'export const SKY_SHIM_MODULE_SOURCE = "export const sky = {};";\n',
        encoding="utf-8",
    )

    # Create node_modules closure for all 7 production packages
    nm_dir = runtime_pkg_dir / "node_modules"
    nm_dir.mkdir(parents=True, exist_ok=False)

    # 1. @jitl/quickjs-ffi-types
    ffi_dir = nm_dir / "@jitl" / "quickjs-ffi-types"
    (ffi_dir / "dist").mkdir(parents=True, exist_ok=False)
    (ffi_dir / "LICENSE").write_text("MIT License - @jitl/quickjs-ffi-types\n", encoding="utf-8")
    (ffi_dir / "dist" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (ffi_dir / "dist" / "index.mjs").write_text("export {};\n", encoding="utf-8")
    (ffi_dir / "dist" / "index.d.ts").write_text("export {};\n", encoding="utf-8")
    (ffi_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "@jitl/quickjs-ffi-types",
                "version": "0.32.0",
                "license": "MIT",
                "types": "dist/index.d.ts",
                "main": "dist/index.js",
                "module": "dist/index.mjs",
                "exports": {
                    "./package.json": "./package.json",
                    ".": {
                        "types": "./dist/index.d.ts",
                        "import": "./dist/index.mjs",
                        "require": "./dist/index.js",
                    },
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 2. All 4 @jitl/quickjs-wasmfile-* variants
    for variant_pkg in QUICKJS_VARIANT_PACKAGES:
        v_dir = nm_dir / Path(variant_pkg)
        (v_dir / "dist").mkdir(parents=True, exist_ok=False)
        (v_dir / "LICENSE").write_text(f"MIT License - {variant_pkg}\n", encoding="utf-8")
        (v_dir / "README.md").write_text(f"# {variant_pkg}\n", encoding="utf-8")
        (v_dir / "dist" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
        (v_dir / "dist" / "index.mjs").write_text("export default {};\n", encoding="utf-8")
        (v_dir / "dist" / "index.d.ts").write_text("export {};\n", encoding="utf-8")
        (v_dir / "dist" / "ffi.js").write_text("module.exports = {};\n", encoding="utf-8")
        (v_dir / "dist" / "ffi.mjs").write_text("export class QuickJSFFI {}\n", encoding="utf-8")
        (v_dir / "dist" / "ffi.d.ts").write_text("export class QuickJSFFI {}\n", encoding="utf-8")
        (v_dir / "dist" / "emscripten-module.cjs").write_text("module.exports = {};\n", encoding="utf-8")
        (v_dir / "dist" / "emscripten-module.cloudflare.cjs").write_text("module.exports = {};\n", encoding="utf-8")
        (v_dir / "dist" / "emscripten-module.browser.mjs").write_text("export default {};\n", encoding="utf-8")
        (v_dir / "dist" / "emscripten-module.browser.d.ts").write_text("export {};\n", encoding="utf-8")
        (v_dir / "dist" / "emscripten-module.mjs").write_text("export default async function() {}\n", encoding="utf-8")
        # Distinct valid WASM payload per variant
        variant_wasm = FAKE_WASM_BYTES + variant_pkg.encode("utf-8")
        (v_dir / "dist" / "emscripten-module.wasm").write_bytes(variant_wasm)
        (v_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": variant_pkg,
                    "version": "0.32.0",
                    "license": "MIT",
                    "types": "./dist/index.d.ts",
                    "main": "./dist/index.js",
                    "module": "./dist/index.mjs",
                    "exports": {
                        ".": {
                            "types": "./dist/index.d.ts",
                            "import": "./dist/index.mjs",
                            "require": "./dist/index.js",
                            "default": "./dist/index.js",
                        },
                        "./package.json": "./package.json",
                        "./ffi": {
                            "types": "./dist/ffi.d.ts",
                            "import": "./dist/ffi.mjs",
                            "require": "./dist/ffi.js",
                            "default": "./dist/ffi.js",
                        },
                        "./wasm": "./dist/emscripten-module.wasm",
                        "./emscripten-module": {
                            "types": "./dist/emscripten-module.browser.d.ts",
                            "iife": "./dist/emscripten-module.cjs",
                            "workerd": "./dist/emscripten-module.cloudflare.cjs",
                            "browser": "./dist/emscripten-module.browser.mjs",
                            "import": "./dist/emscripten-module.mjs",
                            "require": "./dist/emscripten-module.cjs",
                            "default": "./dist/emscripten-module.cjs",
                        },
                    },
                    "dependencies": {
                        "@jitl/quickjs-ffi-types": "0.32.0",
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # 3. quickjs-emscripten-core
    core_dir = nm_dir / "quickjs-emscripten-core"
    (core_dir / "dist").mkdir(parents=True, exist_ok=False)
    (core_dir / "LICENSE").write_text("MIT License - quickjs-emscripten-core\n", encoding="utf-8")
    (core_dir / "dist" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (core_dir / "dist" / "index.mjs").write_text("export {};\n", encoding="utf-8")
    (core_dir / "dist" / "index.d.ts").write_text("export {};\n", encoding="utf-8")
    (core_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "quickjs-emscripten-core",
                "version": "0.32.0",
                "license": "MIT",
                "types": "./dist/index.d.ts",
                "main": "./dist/index.js",
                "module": "./dist/index.mjs",
                "exports": {
                    ".": {
                        "types": "./dist/index.d.ts",
                        "import": "./dist/index.mjs",
                        "require": "./dist/index.js",
                    },
                },
                "dependencies": {
                    "@jitl/quickjs-ffi-types": "0.32.0",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 4. quickjs-emscripten umbrella
    umb_dir = nm_dir / "quickjs-emscripten"
    (umb_dir / "dist").mkdir(parents=True, exist_ok=False)
    (umb_dir / "LICENSE").write_text("MIT License - quickjs-emscripten\n", encoding="utf-8")
    (umb_dir / "dist" / "chunk-OHAYRCBA.mjs").write_text(
        'import DEBUG_SYNC from "@jitl/quickjs-wasmfile-debug-sync";\n'
        'import RELEASE_SYNC from "@jitl/quickjs-wasmfile-release-sync";\n'
        'import DEBUG_ASYNC from "@jitl/quickjs-wasmfile-debug-asyncify";\n'
        'import RELEASE_ASYNC from "@jitl/quickjs-wasmfile-release-asyncify";\n'
        "export { DEBUG_SYNC, RELEASE_SYNC, DEBUG_ASYNC, RELEASE_ASYNC };\n",
        encoding="utf-8",
    )
    (umb_dir / "dist" / "index.js").write_text("module.exports = {};\n", encoding="utf-8")
    (umb_dir / "dist" / "index.mjs").write_text('export * from "./chunk-OHAYRCBA.mjs";\n', encoding="utf-8")
    (umb_dir / "dist" / "index.d.ts").write_text("export {};\n", encoding="utf-8")
    (umb_dir / "dist" / "variants.js").write_text("module.exports = {};\n", encoding="utf-8")
    (umb_dir / "dist" / "variants.mjs").write_text("export {};\n", encoding="utf-8")
    (umb_dir / "dist" / "variants.d.ts").write_text("export {};\n", encoding="utf-8")
    (umb_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "quickjs-emscripten",
                "version": "0.32.0",
                "license": "MIT",
                "types": "./dist/index.d.ts",
                "main": "./dist/index.js",
                "module": "./dist/index.mjs",
                "exports": {
                    ".": {
                        "types": "./dist/index.d.ts",
                        "import": "./dist/index.mjs",
                        "require": "./dist/index.js",
                    },
                    "./variants": {
                        "types": "./dist/variants.d.ts",
                        "import": "./dist/variants.mjs",
                        "require": "./dist/variants.js",
                    },
                    "./package.json": "./package.json",
                },
                "dependencies": {
                    "@jitl/quickjs-wasmfile-debug-asyncify": "0.32.0",
                    "@jitl/quickjs-wasmfile-debug-sync": "0.32.0",
                    "@jitl/quickjs-wasmfile-release-asyncify": "0.32.0",
                    "@jitl/quickjs-wasmfile-release-sync": "0.32.0",
                    "quickjs-emscripten-core": "0.32.0",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "node_executable_path": node_exe,
        "expected_node_version": "22.23.3",
        "expected_node_sha256": node_sha,
        "expected_node_bytes": node_size,
        "runtime_package_root": runtime_pkg_dir,
        "node_license_path": node_license,
        "node_readme_path": node_readme,
        "provenance_json_path": provenance_path,
    }


def test_modules_do_not_import_subprocess_or_network_or_shutil_rmtree() -> None:
    """Verify via AST inspection that neither resources.py nor prepare-js-runtime.py imports spawn/network APIs."""
    forbidden_modules = {
        "subprocess",
        "socket",
        "urllib",
        "http",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "webbrowser",
    }
    for py_path in (RESOURCES_PY_PATH, PREPARE_SCRIPT_PATH):
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_mod = alias.name.split(".")[0]
                    assert top_mod not in forbidden_modules, f"{py_path.name} imports forbidden module {alias.name}"
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_mod = node.module.split(".")[0]
                assert top_mod not in forbidden_modules, f"{py_path.name} imports from forbidden module {node.module}"
            elif isinstance(node, ast.Attribute):
                assert node.attr not in {"rmtree", "popen", "system", "spawnl", "spawnv"}, (
                    f"{py_path.name} references forbidden attribute {node.attr}"
                )


def test_prepare_and_resolve_deterministic_profile_with_unicode_and_spaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prepare a complete 4-variant profile under a Chinese + space path and verify deterministic output."""
    unicode_root = tmp_path / "子陵 Ryn 运行时测试" / "构建 目录"
    unicode_root.mkdir(parents=True, exist_ok=False)
    fixture = _build_fake_source_fixture(unicode_root)

    out_dir_a = unicode_root / "输出 Profile A" / "win32-x64"
    out_dir_a.parent.mkdir(parents=True, exist_ok=False)
    res_a = prepare_js_runtime_profile(
        **fixture,
        output_profile_dir=out_dir_a,
    )

    out_dir_b = unicode_root / "输出 Profile B" / "win32-x64"
    out_dir_b.parent.mkdir(parents=True, exist_ok=False)
    res_b = prepare_js_runtime_profile(
        **fixture,
        output_profile_dir=out_dir_b,
    )

    # Exact deterministic manifest digest, file count, and byte count across independent runs
    assert res_a.manifest.manifest_sha256 == res_b.manifest.manifest_sha256
    assert res_a.manifest.total_files == res_b.manifest.total_files
    assert res_a.manifest.total_bytes == res_b.manifest.total_bytes
    assert res_a.manifest_path.read_bytes() == res_b.manifest_path.read_bytes()
    assert res_a.build_receipt_path.read_bytes() == res_b.build_receipt_path.read_bytes()

    # All 4 QuickJS WASM variants are preserved in the prepared closure
    file_paths = {f.path for f in res_a.manifest.files}
    for variant_pkg in QUICKJS_VARIANT_PACKAGES:
        wasm_rel = f"package/node_modules/{variant_pkg}/dist/emscripten-module.wasm"
        assert wasm_rel in file_paths

    # Poison environment variables (PATH, RYN_JS_NODE_EXECUTABLE, NODE_PATH, NODE_OPTIONS)
    malicious_dir = unicode_root / "malicious-bin"
    malicious_dir.mkdir()
    malicious_node = malicious_dir / "node.exe"
    malicious_node.write_bytes(b"MZ-malicious-node")
    monkeypatch.setenv("PATH", str(malicious_dir))
    monkeypatch.setenv("RYN_JS_NODE_EXECUTABLE", str(malicious_node))
    monkeypatch.setenv("NODE_PATH", str(malicious_dir))
    monkeypatch.setenv("NODE_OPTIONS", "--require ./evil.js")

    resolved = resolve_production_js_runtime_profile(
        trusted_profile_root=out_dir_a,
        expected_spec=res_a.expected_spec,
    )
    assert resolved.is_production is True
    assert resolved.is_development is False
    assert resolved.mode == "packaged_release"
    assert resolved.files_verified is True
    assert resolved.handshake_verified is False
    assert resolved.node_executable == out_dir_a.resolve() / Path(NODE_EXECUTABLE_REL_PATH_WIN32)
    assert resolved.node_executable != malicious_node
    assert resolved.worker_path == out_dir_a.resolve() / Path(WORKER_REL_PATH)
    assert resolved.wasm_path == out_dir_a.resolve() / Path(RELEASE_SYNC_WASM_REL_PATH)

    # Public metadata never leaks host filesystem paths
    pub = resolved.to_public_metadata()
    assert pub["files_verified"] is True
    assert pub["handshake_verified"] is False
    assert str(out_dir_a) not in json.dumps(pub, ensure_ascii=False)

    # Lease pins ALL inventoried manifest files (including node/node.exe) and closes cleanly
    with open_production_js_runtime_lease(
        trusted_profile_root=out_dir_a,
        expected_spec=res_a.expected_spec,
    ) as lease:
        assert not lease.closed
        expected_all_paths = {entry.path for entry in res_a.manifest.files}
        assert NODE_EXECUTABLE_REL_PATH_WIN32 in expected_all_paths
        assert WORKER_REL_PATH in expected_all_paths
        assert SKY_SHIM_REL_PATH in expected_all_paths
        assert RELEASE_SYNC_WASM_REL_PATH in expected_all_paths
        assert set(lease.pinned_relpaths) == expected_all_paths
        assert lease.pinned_handle_count == len(expected_all_paths)
        expected_dir_paths = set(expected_profile_directory_relpaths(res_a.manifest))
        assert "." in expected_dir_paths
        assert "node" in expected_dir_paths
        assert "package" in expected_dir_paths
        assert "package/src" in expected_dir_paths
        assert set(lease.pinned_directory_relpaths) == expected_dir_paths
        assert lease.pinned_directory_count == len(expected_dir_paths)
        root_dir_pin = lease.get_pinned_directory_identity(".")
        assert root_dir_pin is not None
        node_pin = lease.get_pinned_file_identity(NODE_EXECUTABLE_REL_PATH_WIN32)
        assert node_pin is not None
        assert node_pin.sha256 == res_a.manifest.node_sha256
        assert node_pin.size == res_a.manifest.node_bytes
        lease.revalidate_critical_files()
    assert lease.closed
    assert lease.pinned_handle_count == 0
    assert lease.pinned_directory_count == 0
    assert lease.get_pinned_file_identity(NODE_EXECUTABLE_REL_PATH_WIN32) is None
    assert lease.get_pinned_directory_identity(".") is None

    # Explicit source_dev resolver labels the profile as source_dev, never packaged_release
    dev_profile = resolve_development_js_runtime_profile(
        explicit_profile_root=out_dir_a,
        expected_platform="win32",
        expected_arch="x64",
    )
    assert dev_profile.mode == "source_dev"
    assert dev_profile.is_development is True
    assert dev_profile.is_production is False


def test_cli_entry_point_and_refusal_to_overwrite_existing_target(tmp_path: Path) -> None:
    """Verify CLI flag parsing, JSON output, and strict refusal to overwrite existing directories."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "cli-output-profile"

    rc = preparer_main([
        "--node-executable",
        str(fixture["node_executable_path"]),
        "--expected-node-version",
        fixture["expected_node_version"],
        "--expected-node-sha256",
        fixture["expected_node_sha256"],
        "--expected-node-bytes",
        str(fixture["expected_node_bytes"]),
        "--runtime-package-root",
        str(fixture["runtime_package_root"]),
        "--output-profile-dir",
        str(out_dir),
    ])
    assert rc == 0
    assert (out_dir / MANIFEST_FILENAME).is_file()
    assert (out_dir / BUILD_RECEIPT_FILENAME).is_file()

    # Running again against the existing output directory fails closed without deleting anything
    sentinel = out_dir / "sentinel.txt"
    sentinel.write_text("keep-me", encoding="utf-8")
    rc_repeat = preparer_main([
        "--node-executable",
        str(fixture["node_executable_path"]),
        "--expected-node-version",
        fixture["expected_node_version"],
        "--expected-node-sha256",
        fixture["expected_node_sha256"],
        "--expected-node-bytes",
        str(fixture["expected_node_bytes"]),
        "--runtime-package-root",
        str(fixture["runtime_package_root"]),
        "--output-profile-dir",
        str(out_dir),
    ])
    assert rc_repeat == 1
    assert sentinel.read_text(encoding="utf-8") == "keep-me"


def test_strict_relative_path_validation_rejects_traversal_ads_unc_and_devices() -> None:
    """Verify path validator rejects traversal, backslashes, UNC, ADS colons, NUL, and Windows devices."""
    assert validate_relative_resource_path("node/node.exe") == "node/node.exe"
    assert validate_relative_resource_path("package/node_modules/@jitl/quickjs-ffi-types/package.json") == (
        "package/node_modules/@jitl/quickjs-ffi-types/package.json"
    )

    bad_paths = [
        "",
        "/node/node.exe",
        "node/node.exe/",
        "node//node.exe",
        "node\\node.exe",
        "../node/node.exe",
        "package/../../secret.txt",
        "package/./worker.mjs",
        "C:/node/node.exe",
        "node/node.exe:Zone.Identifier",
        "package/src/worker.mjs:$DATA",
        "\\\\server\\share\\node.exe",
        "node/CON",
        "node/NUL.txt",
        "node/COM1.js",
        "node/LPT9.wasm",
        "node/trailing_dot.",
        "node/trailing_space ",
        "node/null\x00byte.js",
    ]
    for bad in bad_paths:
        with pytest.raises(JsResourceError):
            validate_relative_resource_path(bad)


def test_strict_json_and_manifest_schema_rejections(tmp_path: Path) -> None:
    """Verify duplicate JSON keys, BOM, unknown fields, bad versions/arch, and unsorted entries fail closed."""
    # Duplicate JSON keys rejected
    with pytest.raises(JsResourceError) as exc_dup:
        parse_strict_bounded_json(b'{"a": 1, "a": 2}')
    assert exc_dup.value.code == "ryn_js_resource_duplicate_json_key"

    # UTF-8 BOM rejected
    with pytest.raises(JsResourceError) as exc_bom:
        parse_strict_bounded_json(b'\xef\xbb\xbf{"a": 1}')
    assert exc_bom.value.code == "ryn_js_resource_invalid_json"

    # Excessive nesting depth rejected
    deep_json = ("[" * 25) + "1" + ("]" * 25)
    with pytest.raises(JsResourceError) as exc_depth:
        parse_strict_bounded_json(f'{{"x": {deep_json}}}'.encode("utf-8"))
    assert exc_depth.value.code == "ryn_js_resource_json_depth_exceeded"

    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "profile-schema-tests"
    res = prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)
    base_manifest = res.manifest.to_dict()

    # Unknown top-level field
    m_unknown = copy.deepcopy(base_manifest)
    m_unknown["allow_unverified"] = True
    with pytest.raises(JsResourceError) as exc_unk:
        validate_manifest_dict(m_unknown)
    assert exc_unk.value.code == "ryn_js_resource_unknown_field"

    # Unknown file entry field
    m_file_unk = copy.deepcopy(base_manifest)
    m_file_unk["files"][0]["extra"] = "bad"
    with pytest.raises(JsResourceError) as exc_funk:
        validate_manifest_dict(m_file_unk)
    assert exc_funk.value.code == "ryn_js_resource_unknown_field"

    # Wrong QuickJS version
    m_qjs = copy.deepcopy(base_manifest)
    m_qjs["quickjs_version"] = "0.31.0"
    with pytest.raises(JsResourceError) as exc_qjs:
        validate_manifest_dict(m_qjs)
    assert exc_qjs.value.code == "ryn_js_resource_version_mismatch"

    # Wrong QuickJS variant
    m_var = copy.deepcopy(base_manifest)
    m_var["quickjs_variant"] = "DEBUG_ASYNC"
    with pytest.raises(JsResourceError) as exc_var:
        validate_manifest_dict(m_var)
    assert exc_var.value.code == "ryn_js_resource_variant_mismatch"

    # Wrong wire protocol
    m_proto = copy.deepcopy(base_manifest)
    m_proto["wire_protocol"] = 2
    with pytest.raises(JsResourceError) as exc_proto:
        validate_manifest_dict(m_proto)
    assert exc_proto.value.code == "ryn_js_resource_protocol_mismatch"

    # Unsorted files list
    m_unsorted = copy.deepcopy(base_manifest)
    m_unsorted["files"] = list(reversed(m_unsorted["files"]))
    with pytest.raises(JsResourceError) as exc_sort:
        validate_manifest_dict(m_unsorted)
    assert exc_sort.value.code == "ryn_js_resource_unsorted_files"


def test_tampered_node_wasm_missing_variant_extra_package_and_export_map_failures(tmp_path: Path) -> None:
    """Verify bad Node hash, tampered WASM, missing variant, extra package, and broken export maps fail closed."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "profile-tamper-tests"
    res = prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)

    # 1. Wrong expected Node hash in preparer fails before creating output directory
    bad_hash_out = tmp_path / "should-not-be-created"
    with pytest.raises(JsResourceError) as exc_bad_node:
        prepare_js_runtime_profile(
            **(fixture | {"expected_node_sha256": "0" * 64}),
            output_profile_dir=bad_hash_out,
        )
    assert exc_bad_node.value.code == "ryn_js_resource_hash_mismatch"
    assert not bad_hash_out.exists()

    # 2. Platform or arch mismatch against ExpectedJsRuntimeProfileSpec fails closed
    wrong_arch_spec = ExpectedJsRuntimeProfileSpec(
        expected_manifest_sha256=res.expected_spec.expected_manifest_sha256,
        platform="win32",
        arch="arm64",
        node_version=res.expected_spec.node_version,
        node_sha256=res.expected_spec.node_sha256,
        node_bytes=res.expected_spec.node_bytes,
    )
    with pytest.raises(JsResourceError) as exc_arch:
        resolve_production_js_runtime_profile(
            trusted_profile_root=out_dir,
            expected_spec=wrong_arch_spec,
        )
    assert exc_arch.value.code == "ryn_js_resource_arch_mismatch"

    # 3. Tampering with RELEASE_SYNC WASM bytes on disk fails SHA-256 verification
    wasm_file = out_dir / Path(RELEASE_SYNC_WASM_REL_PATH)
    original_wasm = wasm_file.read_bytes()
    wasm_file.write_bytes(original_wasm[:-1] + b"\xff")
    with pytest.raises(JsResourceError) as exc_tamper:
        resolve_production_js_runtime_profile(
            trusted_profile_root=out_dir,
            expected_spec=res.expected_spec,
        )
    assert exc_tamper.value.code == "ryn_js_resource_hash_mismatch"
    wasm_file.write_bytes(original_wasm)

    # 4. Dropping an unexpected extra file into the prepared profile fails closed
    rogue_file = out_dir / "package" / "src" / "rogue-helper.mjs"
    rogue_file.write_text("export const evil = 1;\n", encoding="utf-8")
    with pytest.raises(JsResourceError) as exc_rogue:
        resolve_production_js_runtime_profile(
            trusted_profile_root=out_dir,
            expected_spec=res.expected_spec,
        )
    assert exc_rogue.value.code == "ryn_js_resource_unexpected_file"
    rogue_file.unlink()

    # 5. Missing one of the 4 QuickJS variant WASM files in source tree is rejected by preparer
    missing_wasm_path = (
        fixture["runtime_package_root"]
        / "node_modules"
        / "@jitl"
        / "quickjs-wasmfile-debug-asyncify"
        / "dist"
        / "emscripten-module.wasm"
    )
    saved_debug_wasm = missing_wasm_path.read_bytes()
    missing_wasm_path.unlink()
    with pytest.raises(JsResourceError) as exc_missing_wasm:
        prepare_js_runtime_profile(**fixture, output_profile_dir=tmp_path / "missing-wasm-out")
    assert exc_missing_wasm.value.code in {
        "ryn_js_resource_wasm_missing",
        "ryn_js_resource_closure_incomplete",
    }
    missing_wasm_path.write_bytes(saved_debug_wasm)

    # 6. Extra unpinned package in source node_modules is rejected by preparer
    extra_pkg_dir = fixture["runtime_package_root"] / "node_modules" / "left-pad"
    extra_pkg_dir.mkdir(parents=True, exist_ok=False)
    (extra_pkg_dir / "package.json").write_text('{"name":"left-pad","version":"1.0.0"}\n', encoding="utf-8")
    with pytest.raises(JsResourceError) as exc_extra_pkg:
        prepare_js_runtime_profile(**fixture, output_profile_dir=tmp_path / "extra-pkg-out")
    assert exc_extra_pkg.value.code == "ryn_js_prepare_unexpected_package"
    (extra_pkg_dir / "package.json").unlink()
    extra_pkg_dir.rmdir()

    # 7. Broken package.json export map pointing to a non-existent dist file is rejected
    rel_sync_pkg_json = (
        fixture["runtime_package_root"]
        / "node_modules"
        / "@jitl"
        / "quickjs-wasmfile-release-sync"
        / "package.json"
    )
    saved_pkg_json = rel_sync_pkg_json.read_bytes()
    broken_pkg = json.loads(saved_pkg_json.decode("utf-8"))
    broken_pkg["exports"]["./ffi"]["import"] = "./dist/non-existent-ffi.mjs"
    rel_sync_pkg_json.write_text(json.dumps(broken_pkg, indent=2) + "\n", encoding="utf-8")
    # Preparer copies files in memory, then when we resolve or validate exports it fails closed
    broken_out = tmp_path / "broken-export-out"
    with pytest.raises(JsResourceError) as exc_export:
        prepare_js_runtime_profile(**fixture, output_profile_dir=broken_out)
    assert exc_export.value.code == "ryn_js_resource_closure_incomplete"
    rel_sync_pkg_json.write_bytes(saved_pkg_json)


def test_reparse_point_and_symlink_rejection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that any symlink or Windows reparse point in root or lineage is rejected."""
    fixture = _build_fake_source_fixture(tmp_path)
    out_dir = tmp_path / "profile-reparse-check"
    res = prepare_js_runtime_profile(**fixture, output_profile_dir=out_dir)

    target_worker = (out_dir / Path(WORKER_REL_PATH)).resolve()
    orig_lstat = os.lstat

    def _fake_reparse_lstat(path_arg: Any) -> Any:
        st = orig_lstat(path_arg)
        if Path(path_arg).resolve() == target_worker:

            class _ReparseStatProxy:
                def __getattr__(self, name: str) -> Any:
                    if name == "st_file_attributes":
                        return getattr(st, "st_file_attributes", 0) | 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
                    return getattr(st, name)

            return _ReparseStatProxy()
        return st

    monkeypatch.setattr(os, "lstat", _fake_reparse_lstat)
    with pytest.raises(JsResourceError) as exc_reparse:
        resolve_production_js_runtime_profile(
            trusted_profile_root=out_dir,
            expected_spec=res.expected_spec,
        )
    assert exc_reparse.value.code == "ryn_js_resource_reparse_forbidden"


def test_sanitize_invocation_environment_strips_node_options_and_node_path() -> None:
    """Verify `sanitize_invocation_environment` removes NODE_OPTIONS/NODE_PATH without mutating input."""
    raw_env = {
        "SYSTEMROOT": "C:\\Windows",
        "PATH": "C:\\Windows\\System32",
        "NODE_OPTIONS": "--inspect=0.0.0.0:9229",
        "NODE_PATH": "C:\\evil_modules",
        "NODE_EXTRA_CA_CERTS": "C:\\evil.pem",
        "NODE_REPL_EXTERNAL_MODULE": "evil.js",
        "UV_THREADPOOL_SIZE": "128",
    }
    cleaned = sanitize_invocation_environment(raw_env)
    assert "NODE_OPTIONS" not in cleaned
    assert "NODE_PATH" not in cleaned
    assert "NODE_EXTRA_CA_CERTS" not in cleaned
    assert "NODE_REPL_EXTERNAL_MODULE" not in cleaned
    assert "UV_THREADPOOL_SIZE" not in cleaned
    assert cleaned["NODE_NO_WARNINGS"] == "1"
    assert cleaned["SYSTEMROOT"] == "C:\\Windows"
    assert "NODE_OPTIONS" in raw_env


def test_output_leaf_validation_dangling_link_existing_file_dir_race_and_permission_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify nonexistent leaf succeeds while existing file/dir, dangling link, racer, and permission errors fail closed."""
    fixture = _build_fake_source_fixture(tmp_path)

    # 1. Existing regular file at output_profile_dir is rejected without modifying it
    existing_file = tmp_path / "existing-regular-file"
    existing_file.write_text("do-not-touch-file", encoding="utf-8")
    with pytest.raises(JsResourceError) as exc_file:
        prepare_js_runtime_profile(**fixture, output_profile_dir=existing_file)
    assert exc_file.value.code == "ryn_js_prepare_output_exists"
    assert existing_file.read_text(encoding="utf-8") == "do-not-touch-file"

    # 2. Existing directory at output_profile_dir is rejected without modifying its contents
    existing_dir = tmp_path / "existing-directory"
    existing_dir.mkdir()
    racer_marker = existing_dir / "keep.txt"
    racer_marker.write_text("keep-dir-contents", encoding="utf-8")
    with pytest.raises(JsResourceError) as exc_dir:
        prepare_js_runtime_profile(**fixture, output_profile_dir=existing_dir)
    assert exc_dir.value.code == "ryn_js_prepare_output_exists"
    assert racer_marker.read_text(encoding="utf-8") == "keep-dir-contents"

    # 3. Missing parent directory fails closed with ryn_js_prepare_output_parent_missing
    missing_parent_target = tmp_path / "nonexistent-parent-dir" / "new-profile"
    with pytest.raises(JsResourceError) as exc_parent:
        prepare_js_runtime_profile(**fixture, output_profile_dir=missing_parent_target)
    assert exc_parent.value.code == "ryn_js_prepare_output_parent_missing"

    # 4. Dangling symlink / reparse point at prospective leaf is rejected (simulated via os.lstat without OS privilege)
    dangling_target = tmp_path / "dangling-link-leaf"
    orig_lstat = os.lstat

    def _lstat_dangling_link(path_arg: Any) -> Any:
        if os.path.normcase(str(Path(path_arg))) == os.path.normcase(str(dangling_target)):
            return os.stat_result((0o120777, 1, 1, 1, 0, 0, 16, 0, 0, 0))
        return orig_lstat(path_arg)

    monkeypatch.setattr(os, "lstat", _lstat_dangling_link)
    with pytest.raises(JsResourceError) as exc_dangling:
        prepare_js_runtime_profile(**fixture, output_profile_dir=dangling_target)
    assert exc_dangling.value.code == "ryn_js_prepare_output_exists"
    assert not dangling_target.exists()
    monkeypatch.setattr(os, "lstat", orig_lstat)

    # 5. PermissionError / non-ENOENT OSError on os.lstat of prospective leaf fails closed (not treated as nonexistent)
    perm_target = tmp_path / "permission-denied-leaf"

    def _lstat_permission_error(path_arg: Any) -> Any:
        if os.path.normcase(str(Path(path_arg))) == os.path.normcase(str(perm_target)):
            raise PermissionError(13, "Access is denied", str(path_arg))
        return orig_lstat(path_arg)

    monkeypatch.setattr(os, "lstat", _lstat_permission_error)
    with pytest.raises(JsResourceError) as exc_perm:
        prepare_js_runtime_profile(**fixture, output_profile_dir=perm_target)
    assert exc_perm.value.code == "ryn_js_prepare_output_unverifiable"
    assert not perm_target.exists()
    monkeypatch.setattr(os, "lstat", orig_lstat)

    # 6. Concurrent creation race between pre-check and mkdir refuses without deleting or modifying the racer's directory
    race_target = tmp_path / "concurrent-racer-leaf"
    orig_mkdir = Path.mkdir

    def _racy_mkdir(self_path: Path, *args: Any, **kwargs: Any) -> None:
        if os.path.normcase(str(self_path)) == os.path.normcase(str(race_target)):
            # Simulate a concurrent writer creating the directory and placing a file right before our mkdir
            orig_mkdir(self_path, parents=False, exist_ok=False)
            (self_path / "racer-owned.txt").write_text("racer-data", encoding="utf-8")
        return orig_mkdir(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _racy_mkdir)
    with pytest.raises(JsResourceError) as exc_race:
        prepare_js_runtime_profile(**fixture, output_profile_dir=race_target)
    assert exc_race.value.code == "ryn_js_prepare_output_exists"
    assert (race_target / "racer-owned.txt").read_text(encoding="utf-8") == "racer-data"
    assert not (race_target / MANIFEST_FILENAME).exists()
    monkeypatch.setattr(Path, "mkdir", orig_mkdir)

    # 7. Reserved Windows device leaf name, ADS colon, trailing dots/spaces, or dot traversal in output_profile_dir
    bad_leaf_cases = (
        ("reserved_con", "CON", "ryn_js_resource_reserved_device_name"),
        ("reserved_nul_ext", "NUL.profile", "ryn_js_resource_reserved_device_name"),
        ("ads_stream", "win32-x64:stream", "ryn_js_resource_ads_or_drive_forbidden"),
        ("trailing_dots", "win32-x64..", "ryn_js_resource_invalid_path"),
        ("trailing_space", "win32-x64 ", "ryn_js_resource_invalid_path"),
        ("dot_dot_traversal", "..", "ryn_js_resource_path_traversal"),
    )
    for case_id, bad_leaf, expected_code in bad_leaf_cases:
        bad_target_str = f"{tmp_path}{os.sep}{bad_leaf}"
        with pytest.raises(
            JsResourceError,
            match=".*",
        ) as exc_bad_leaf:
            prepare_js_runtime_profile(**fixture, output_profile_dir=bad_target_str)
        assert exc_bad_leaf.value.code == expected_code, (
            f"case {case_id} ({bad_leaf!r}): expected {expected_code}, got {exc_bad_leaf.value.code}"
        )
        # Also verify passing a Path object directly where Path doesn't collapse '..'
        if bad_leaf not in {".."}:
            with pytest.raises(JsResourceError) as exc_bad_path_obj:
                prepare_js_runtime_profile(**fixture, output_profile_dir=tmp_path / bad_leaf)
            assert exc_bad_path_obj.value.code == expected_code, (
                f"Path case {case_id} ({bad_leaf!r}): expected {expected_code}, got {exc_bad_path_obj.value.code}"
            )


