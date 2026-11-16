"""Tests for S11-I26: computer control platform scope (Windows x64 + gated ARM64 emulation),
non-frozen development worker mode, final-path install root, reparse/UNC classification, and subdivided reasons.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from rynmesh.agent_runtime import computer, computer_status
from rynmesh.agent_runtime.computer import binary as binary_mod
from rynmesh.agent_runtime.computer.binary import (
    EXPECTED_WORKER_NAME,
    EXPECTED_WORKER_VERSION,
    IMAGE_FILE_MACHINE_AMD64,
    IMAGE_NT_OPTIONAL_HDR64_MAGIC,
    MANIFEST_SCHEMA_VERSION,
    PACKAGED_EXECUTABLE_BASENAME,
    PROTOCOL_SEMVER,
    SUPPORTED_TARGET_TRIPLE,
    WIRE_VERSION,
    ProductionBinaryResolver,
    VerifiedArtifact,
    open_and_verify_artifact_lease,
    parse_and_validate_manifest_bytes,
    validate_lexical_windows_path,
    verify_suspended_process_handle,
    verify_windows_x64_host,
)
from rynmesh.agent_runtime.computer.client import (
    NativeWorkerClient,
    acquire_worker_artifact_lease,
)
from rynmesh.agent_runtime.computer.protocol import NativeBridgeRejected
from rynmesh.agent_runtime.extensions.store import COMPUTER_USE_PLUGIN_ID
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.agent_runtime.tools import RynTools
from tests.test_agent_native_bridge import FAKE_WORKER_SCRIPT


def _valid_manifest_dict(*, size: int, sha256_hex: str) -> dict[str, Any]:
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "releaseId": "rel_s11_i26_test",
        "wireVersion": WIRE_VERSION,
        "protocolVersion": PROTOCOL_SEMVER,
        "workerName": EXPECTED_WORKER_NAME,
        "workerVersion": EXPECTED_WORKER_VERSION,
        "targetTriple": SUPPORTED_TARGET_TRIPLE,
        "artifact": {
            "installedFilename": PACKAGED_EXECUTABLE_BASENAME,
            "size": size,
            "sha256": sha256_hex,
            "machine": IMAGE_FILE_MACHINE_AMD64,
            "optionalHeaderMagic": IMAGE_NT_OPTIONAL_HDR64_MAGIC,
        },
    }


def _make_junction(link_path: Path, target_path: Path) -> None:
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"mklink /J failed: {res.stdout} {res.stderr}"


def _make_agent_tools(tmp_path: Path) -> tuple[Any, AgentStore, RynTools]:
    key = X25519PrivateKey.generate()
    store = AgentStore(tmp_path / "agent", key)
    store.root = tmp_path / "agent"
    store.transaction(
        lambda d: d["settings"].update(enabled=True, workspace_root=str(tmp_path), desktop_access=True)
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            agent_startup_error=None,
            node_apps=None,
            messaging_key=key,
            home=tmp_path,
        )
    )
    catalog = SimpleNamespace(list_models=lambda: [], resolve=lambda _mid: None)
    tools = RynTools(app, store, {}, catalog)
    return app, store, tools


def test_t1_non_windows_hides_computer_tools_and_reports_platform_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, store, tools = _make_agent_tools(tmp_path)
    try:
        ext_store = tools.extensions.store
        ext_store.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, True)

        # Initialize extension catalog once before simulating macOS sys.platform
        _ = tools.specs()
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(platform, "system", lambda: "Darwin")
        monkeypatch.setattr(platform, "machine", lambda: "arm64")

        support = computer.platform_support()
        assert support["supported"] is False
        assert "macOS" in support["platform_name"]
        assert "本机电脑控制仅支持 Windows x64" in support["message_zh"]

        menu_names = {item["function"]["name"] for item in tools.specs()}
        assert "ryn_computer_read" not in menu_names
        assert "ryn_computer_action" not in menu_names
        assert not any("computer_use" in n or n.startswith("computer_") for n in menu_names)
        assert tools.spec("ryn_computer_read") is None
        assert tools.spec("ryn_computer_action") is None

        env_report = tools.local.perform(
            "ryn_local_environment",
            {},
            {
                "task_id": "t_env",
                "local_execution": {"workspace_root": str(tmp_path), "desktop_access": True},
            },
            lambda: False,
        )
        assert env_report["computer_control_supported"] is False
        assert "本机电脑控制仅支持 Windows x64" in env_report["computer_control_reason"]

        plugins_pub = ext_store.overview(desktop_access=True)["plugins"]
        cu_pub = next(p for p in plugins_pub if p["id"] == COMPUTER_USE_PLUGIN_ID)
        assert cu_pub["status"] == "unavailable"
        assert cu_pub["status_reason"] == "windows_only"
        assert "本机电脑控制仅支持 Windows x64" in cu_pub["status_message"]

        runtime = SimpleNamespace(store=store, tools=tools, catalog=SimpleNamespace(cached_row=lambda _: None))
        status_dto = computer_status.build_computer_status_dto(app, runtime)
        assert status_dto["native"]["platform"] == "unsupported"
        assert status_dto["native"]["platform_supported"] is False
        assert "macOS" in status_dto["native"]["platform_name"]
        assert status_dto["native"]["reason_code"] == "native_platform_unsupported"
        assert "本机电脑控制仅支持 Windows x64" in status_dto["native"]["message"]
    finally:
        tools.close()


def test_t2_non_frozen_dev_executable_reconfigure_and_env_var_handshake_marks_development_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_path = tmp_path / "fake_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    state_reconf = tmp_path / "reconf_state.jsonl"
    state_env = tmp_path / "env_state.jsonl"

    monkeypatch.setattr(sys, "frozen", False, raising=False)

    # 1. Non-frozen NativeWorkerClient.reconfigure(dev_executable=...) handshakes without manifest check
    client = NativeWorkerClient()
    try:
        client.reconfigure(dev_executable=[base_py, str(script_path), "normal", str(state_reconf)])
        session = client.ensure_session()
        assert session.is_usable()
        snap = client.cache_status()
        assert snap["status"] == "ready"
        assert snap["developmentWorker"] is True
        assert snap["releaseVerified"] is False
        assert snap["binding"]["developmentWorker"] is True
    finally:
        client.shutdown()

    # 2. Non-frozen environment variable pointing to local cargo/dev build handshakes without manifest check
    dev_cmd = [base_py, str(script_path), "normal", str(state_env)]
    monkeypatch.setenv("RYN_COMPUTER_USE_DEV_EXECUTABLE", json.dumps(dev_cmd))
    env_lease = acquire_worker_artifact_lease()
    try:
        assert env_lease.verified_artifact.development_worker is True
        assert env_lease.verified_artifact.release_verified is False
    finally:
        env_lease.close()

    env_client = NativeWorkerClient()
    try:
        env_session = env_client.ensure_session()
        assert env_session.is_usable()
        env_snap = env_client.cache_status()
        assert env_snap["status"] == "ready"
        assert env_snap["developmentWorker"] is True
        assert env_snap["releaseVerified"] is False
    finally:
        env_client.shutdown()


def test_t3_install_path_with_junction_raises_reparse_point_with_component_and_correction(
    tmp_path: Path,
) -> None:
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    real_dir = tmp_path / "real_target"
    real_dir.mkdir()
    exe_file = real_dir / PACKAGED_EXECUTABLE_BASENAME
    shutil.copyfile(base_py, exe_file)
    raw = exe_file.read_bytes()
    spec = parse_and_validate_manifest_bytes(
        json.dumps(_valid_manifest_dict(size=len(raw), sha256_hex=hashlib.sha256(raw).hexdigest())).encode("utf-8")
    )

    junc_dir = tmp_path / "linked_install_dir"
    _make_junction(junc_dir, real_dir)

    with pytest.raises(NativeBridgeRejected) as exc_junc:
        open_and_verify_artifact_lease(spec=spec, bundle_root=junc_dir)
    assert exc_junc.value.code == "agent_computer_path_reparse_point"
    assert exc_junc.value.component == "linked_install_dir"
    assert exc_junc.value.reason == "reparse_below_root"
    assert "请把 Ryn 装在本地非链接目录" in (exc_junc.value.correction or "")

    with pytest.raises(NativeBridgeRejected) as exc_unc:
        binary_mod._check_no_ancestor_reparse_points("\\\\server\\share\\ryn_computer_use.exe")
    assert exc_unc.value.code == "agent_computer_path_unc"
    assert exc_unc.value.reason == "unc_path"
    assert "请把 Ryn 装在本地非链接目录" in (exc_unc.value.correction or "")


def test_t4_arm64_windows_0xaa64_toggle_controls_emulated_x64_and_arm_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, store, tools = _make_agent_tools(tmp_path)
    try:
        monkeypatch.setattr(binary_mod, "_query_native_machine", lambda *, phase="pre_dispatch": 0xAA64)

        # Default: switch is False -> rejected with ARM message
        with pytest.raises(NativeBridgeRejected) as exc_off:
            verify_windows_x64_host()
        assert exc_off.value.code == "agent_computer_arch_unsupported"
        assert "本机是 ARM 版 Windows，电脑控制暂不支持" in (exc_off.value.correction or "")

        runtime = SimpleNamespace(store=store, tools=tools, catalog=SimpleNamespace(cached_row=lambda _: None))
        dto_off = computer_status.build_computer_status_dto(app, runtime)
        assert dto_off["native"]["platform_supported"] is False
        assert "本机是 ARM 版 Windows，电脑控制暂不支持" in dto_off["native"]["message"]

        # Enable switch in AgentStore settings ONLY (no monkeypatch of DEFAULT_ALLOW_ARM64_EMULATION)
        def enable_arm64(data: dict[str, Any]) -> None:
            data["settings"]["allow_arm64_computer_use"] = True
            data["settings_revision"] = data.get("settings_revision", 0) + 1

        store.transaction(enable_arm64)

        assert computer.platform_support()["supported"] is True
        assert computer.platform_support()["emulated_x64"] is True
        assert verify_windows_x64_host() == 0xAA64

        base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
        app_dir = tmp_path / "arm_emul_app"
        app_dir.mkdir()
        worker_exe = app_dir / PACKAGED_EXECUTABLE_BASENAME
        shutil.copyfile(base_py, worker_exe)
        raw = worker_exe.read_bytes()
        spec = parse_and_validate_manifest_bytes(
            json.dumps(_valid_manifest_dict(size=len(raw), sha256_hex=hashlib.sha256(raw).hexdigest())).encode("utf-8")
        )
        lease = open_and_verify_artifact_lease(spec=spec, bundle_root=app_dir)
        try:
            assert lease.verified_artifact.emulated_x64 is True
            assert lease.verified_artifact.pe_machine == IMAGE_FILE_MACHINE_AMD64
        finally:
            lease.close()

        dto_on = computer_status.build_computer_status_dto(app, runtime)
        assert dto_on["native"]["platform_supported"] is True
        assert dto_on["native"]["emulated_x64"] is True
        assert "ARM 版 Windows 上以仿真运行" in dto_on["native"]["message"]
    finally:
        tools.close()


def test_t4b_arm64_setting_wires_into_real_worker_startup_and_platform_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, store, tools = _make_agent_tools(tmp_path)
    ext_store = tools.extensions.store
    ext_store.set_plugin_enabled(COMPUTER_USE_PLUGIN_ID, True)

    script_path = tmp_path / "fake_arm64_worker.py"
    script_path.write_text(FAKE_WORKER_SCRIPT, encoding="utf-8")
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    state_file = tmp_path / "arm64_worker_state.jsonl"

    app_dir = tmp_path / "arm64_prod_app"
    bin_dir = app_dir / "rynmesh" / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copyfile(base_py, app_dir / "ryn_desktop.exe")
    shutil.copyfile(base_py, app_dir / PACKAGED_EXECUTABLE_BASENAME)
    raw = (app_dir / PACKAGED_EXECUTABLE_BASENAME).read_bytes()
    manifest_bytes = json.dumps(
        _valid_manifest_dict(size=len(raw), sha256_hex=hashlib.sha256(raw).hexdigest())
    ).encode("utf-8")
    (bin_dir / "computer-use-manifest.json").write_bytes(manifest_bytes)
    spec = parse_and_validate_manifest_bytes(manifest_bytes)

    client = NativeWorkerClient()
    client.reconfigure(dev_executable=[str(base_py), str(script_path), "normal", str(state_file)])
    try:
        monkeypatch.setattr(binary_mod, "_query_native_machine", lambda *, phase="pre_dispatch": 0xAA64)

        # 1. Switch initially OFF -> platform_support, ExtensionStore, and real startup all reject
        assert computer.platform_support()["supported"] is False
        cu_off = next(p for p in ext_store.overview(desktop_access=True)["plugins"] if p["id"] == COMPUTER_USE_PLUGIN_ID)
        assert cu_off["status"] == "unavailable"

        with pytest.raises(NativeBridgeRejected) as exc_start_off:
            client.ensure_session()
        assert exc_start_off.value.code == "agent_computer_arch_unsupported"

        with pytest.raises(NativeBridgeRejected) as exc_lease_off:
            open_and_verify_artifact_lease(spec=spec, bundle_root=app_dir)
        assert exc_lease_off.value.code == "agent_computer_arch_unsupported"

        # 2. Turn switch ON in settings -> platform_support(), ExtensionStore, dev startup, and packaged lease all succeed
        def set_arm64_toggle(enabled: bool):
            def _mut(data: dict[str, Any]) -> None:
                data["settings"]["allow_arm64_computer_use"] = enabled
                data["settings_revision"] = data.get("settings_revision", 0) + 1

            return _mut

        store.transaction(set_arm64_toggle(True))

        plat_on = computer.platform_support()
        assert plat_on["supported"] is True
        assert plat_on["emulated_x64"] is True

        overview_on = ext_store.overview(desktop_access=True)
        cu_on = next(p for p in overview_on["plugins"] if p["id"] == COMPUTER_USE_PLUGIN_ID)
        assert cu_on["status"] == "enabled"
        cu_srv_on = next(s for s in overview_on["mcp_servers"] if s["plugin_id"] == COMPUTER_USE_PLUGIN_ID)
        assert cu_srv_on["enabled"] is True

        session = client.ensure_session()
        assert session.is_usable()
        snap = client.cache_status()
        assert snap["status"] == "ready"
        assert snap["binding"]["emulatedX64"] is True

        with monkeypatch.context() as m:
            m.setattr(sys, "frozen", True, raising=False)
            m.setattr(sys, "executable", str(app_dir / "ryn_desktop.exe"))
            m.setattr(binary_mod, "PACKAGE_MANIFEST_PATH", bin_dir / "computer-use-manifest.json")
            prod_lease = ProductionBinaryResolver().acquire_lease()
            try:
                assert prod_lease.verified_artifact.release_verified is True
                assert prod_lease.verified_artifact.emulated_x64 is True
            finally:
                prod_lease.close()

        # 3. Turn switch OFF again -> platform_support(), ExtensionStore, and startup reject with arch_unsupported
        store.transaction(set_arm64_toggle(False))

        assert computer.platform_support()["supported"] is False
        cu_off_again = next(
            p for p in ext_store.overview(desktop_access=True)["plugins"] if p["id"] == COMPUTER_USE_PLUGIN_ID
        )
        assert cu_off_again["status"] == "unavailable"

        with pytest.raises(NativeBridgeRejected) as exc_start_off_again:
            client.ensure_session()
        assert exc_start_off_again.value.code == "agent_computer_arch_unsupported"

        with pytest.raises(NativeBridgeRejected) as exc_lease_off_again:
            open_and_verify_artifact_lease(spec=spec, bundle_root=app_dir)
        assert exc_lease_off_again.value.code == "agent_computer_arch_unsupported"
    finally:
        client.shutdown()
        tools.close()


def test_t5_parent_junction_above_install_root_passes_while_junction_inside_install_root_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()

    real_drive_dir = tmp_path / "d_drive_users"
    real_app_dir = real_drive_dir / "Alice" / "AppData" / "Local" / "RynApp"
    real_bin_dir = real_app_dir / "rynmesh" / "bin"
    real_bin_dir.mkdir(parents=True)

    shutil.copyfile(base_py, real_app_dir / "ryn_desktop.exe")
    shutil.copyfile(base_py, real_app_dir / PACKAGED_EXECUTABLE_BASENAME)
    raw = (real_app_dir / PACKAGED_EXECUTABLE_BASENAME).read_bytes()
    manifest_bytes = json.dumps(
        _valid_manifest_dict(size=len(raw), sha256_hex=hashlib.sha256(raw).hexdigest())
    ).encode("utf-8")
    (real_bin_dir / "computer-use-manifest.json").write_bytes(manifest_bytes)

    # Parent directory above RynApp is a junction ("C 盘搬家")
    moved_users_junc = tmp_path / "c_users_junction"
    _make_junction(moved_users_junc, real_drive_dir)

    junc_app_dir = moved_users_junc / "Alice" / "AppData" / "Local" / "RynApp"
    junc_host_exe = junc_app_dir / "ryn_desktop.exe"
    junc_manifest = junc_app_dir / "rynmesh" / "bin" / "computer-use-manifest.json"

    with monkeypatch.context() as m:
        m.setattr(sys, "frozen", True, raising=False)
        m.setattr(sys, "executable", str(junc_host_exe))
        m.setattr(binary_mod, "PACKAGE_MANIFEST_PATH", junc_manifest)

        resolver = ProductionBinaryResolver()
        lease = resolver.acquire_lease()
        try:
            assert lease.verified_artifact.release_verified is True
            assert os.path.normcase(lease.verified_artifact.bundle_root) == os.path.normcase(str(real_app_dir))
        finally:
            lease.close()

        # Now place a junction INSIDE RynApp (below install root) for the manifest directory -> MUST reject!
        ext_manifest_dir = tmp_path / "external_manifest_target"
        ext_manifest_dir.mkdir()
        (ext_manifest_dir / "computer-use-manifest.json").write_bytes(manifest_bytes)
        inner_junc = real_app_dir / "inner_bin_junc"
        _make_junction(inner_junc, ext_manifest_dir)

        m.setattr(binary_mod, "PACKAGE_MANIFEST_PATH", junc_app_dir / "inner_bin_junc" / "computer-use-manifest.json")
        with pytest.raises(NativeBridgeRejected) as exc_inner:
            ProductionBinaryResolver().acquire_lease()
        assert exc_inner.value.code == "agent_computer_path_reparse_point"
        assert exc_inner.value.reason == "reparse_below_root"
        assert exc_inner.value.component == "inner_bin_junc"


def test_t6_path_and_identity_errors_carry_distinct_reasons_and_chinese_status_translations(
    tmp_path: Path,
) -> None:
    # 1. Lexical path reasons
    lexical_cases = [
        ("\\\\server\\share\\ryn_computer_use.exe", "unc_path"),
        ("\\\\.\\pipe\\ryn_computer_use.exe", "device_path"),
        (f"{tmp_path}\\ryn_computer_use.exe:Zone.Identifier", "ads"),
        (f"{tmp_path}\\CON\\ryn_computer_use.exe", "reserved_name"),
        (f"{tmp_path}\\..\\ryn_computer_use.exe", "dot_segment"),
    ]
    seen_reasons: set[str] = set()
    for bad_path, expected_reason in lexical_cases:
        with pytest.raises(NativeBridgeRejected) as exc_lex:
            validate_lexical_windows_path(bad_path)
        assert exc_lex.value.code == "agent_computer_path_invalid"
        assert exc_lex.value.reason == expected_reason
        seen_reasons.add(exc_lex.value.reason)

    # 2. Handle/filesystem reasons: open_failed (with GetLastError), not_directory, final_path_mismatch, reparse_below_root
    base_py = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    app_dir = tmp_path / "reason_app"
    app_dir.mkdir()
    exe_file = app_dir / PACKAGED_EXECUTABLE_BASENAME
    shutil.copyfile(base_py, exe_file)
    raw = exe_file.read_bytes()
    spec = parse_and_validate_manifest_bytes(
        json.dumps(_valid_manifest_dict(size=len(raw), sha256_hex=hashlib.sha256(raw).hexdigest())).encode("utf-8")
    )

    # open_failed with win32_error (missing directory)
    missing_dir = tmp_path / "does_not_exist_dir"
    with pytest.raises(NativeBridgeRejected) as exc_open:
        open_and_verify_artifact_lease(spec=spec, bundle_root=missing_dir)
    assert exc_open.value.reason == "open_failed"
    assert isinstance(exc_open.value.diagnostics.get("win32_error"), int)
    assert exc_open.value.diagnostics["win32_error"] > 0
    seen_reasons.add(exc_open.value.reason)

    # not_directory (passing a file as bundle_root)
    not_a_dir = tmp_path / "plain_file.txt"
    not_a_dir.write_text("not a dir", encoding="utf-8")
    with pytest.raises(NativeBridgeRejected) as exc_notdir:
        open_and_verify_artifact_lease(spec=spec, bundle_root=not_a_dir)
    assert exc_notdir.value.code == "agent_computer_path_invalid"
    assert exc_notdir.value.reason == "not_directory"
    seen_reasons.add(exc_notdir.value.reason)

    # final_path_mismatch (candidate_executable in sibling dir)
    other_dir = tmp_path / "reason_app_other"
    other_dir.mkdir()
    other_exe = other_dir / PACKAGED_EXECUTABLE_BASENAME
    shutil.copyfile(base_py, other_exe)
    with pytest.raises(NativeBridgeRejected) as exc_mismatch:
        open_and_verify_artifact_lease(spec=spec, bundle_root=app_dir, candidate_executable=other_exe)
    assert exc_mismatch.value.code == "agent_computer_path_invalid"
    assert exc_mismatch.value.reason == "final_path_mismatch"
    seen_reasons.add(exc_mismatch.value.reason)

    # reparse_below_root (junction dir)
    junc_dir = tmp_path / "reason_junc"
    _make_junction(junc_dir, app_dir)
    with pytest.raises(NativeBridgeRejected) as exc_rep:
        open_and_verify_artifact_lease(spec=spec, bundle_root=junc_dir)
    assert exc_rep.value.reason == "reparse_below_root"
    seen_reasons.add(exc_rep.value.reason)

    # 3. Suspended process handle reasons: pid_mismatch and image_path_mismatch
    proc = subprocess.Popen(
        [str(base_py), "-c", "import time; time.sleep(5)"],
        creationflags=0x4 | subprocess.CREATE_NO_WINDOW,
    )
    try:
        fake_pid_proc = SimpleNamespace(_handle=proc._handle, pid=proc.pid + 99999)
        with pytest.raises(NativeBridgeRejected) as exc_pid:
            verify_suspended_process_handle(fake_pid_proc)
        assert exc_pid.value.code == "agent_computer_process_identity_mismatch"
        assert exc_pid.value.reason == "pid_mismatch"
        seen_reasons.add(exc_pid.value.reason)

        wrong_art = VerifiedArtifact(
            mode="development",
            verified=True,
            release_verified=False,
            executable_path=str(exe_file),
            bundle_root=str(app_dir),
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            volume_serial=1,
            file_id_high=0,
            file_id_low=1,
            pe_machine=IMAGE_FILE_MACHINE_AMD64,
            pe_magic=IMAGE_NT_OPTIONAL_HDR64_MAGIC,
            manifest_sha256=None,
            release_id=None,
            worker_name=EXPECTED_WORKER_NAME,
            worker_version=EXPECTED_WORKER_VERSION,
            protocol_version=PROTOCOL_SEMVER,
            target_triple=SUPPORTED_TARGET_TRIPLE,
        )
        with pytest.raises(NativeBridgeRejected) as exc_img:
            verify_suspended_process_handle(proc, expected_artifact=wrong_art)
        assert exc_img.value.code == "agent_computer_process_identity_mismatch"
        assert exc_img.value.reason == "image_path_mismatch"
        seen_reasons.add(exc_img.value.reason)
    finally:
        proc.kill()
        proc.wait(timeout=5)

    expected_all = {
        "unc_path",
        "device_path",
        "ads",
        "reserved_name",
        "dot_segment",
        "reparse_below_root",
        "final_path_mismatch",
        "open_failed",
        "not_directory",
        "pid_mismatch",
        "image_path_mismatch",
    }
    assert seen_reasons == expected_all

    # Every reason translates to a distinct Chinese hint in computer_status.py
    zh_messages = {r: computer_status.translate_native_reason_zh(r, win32_error=5) for r in expected_all}
    assert all(isinstance(msg, str) and len(msg) >= 6 for msg in zh_messages.values())
    assert "5" in (zh_messages["open_failed"] or "")
    assert len(set(zh_messages.values())) == len(expected_all)
