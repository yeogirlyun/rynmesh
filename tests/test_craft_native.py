"""Real archive verification and subprocess MCP framing, failure and cancellation."""
import hashlib
import io
import json
import os
import stat
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from rynmesh.node_apps import craft_native as native
from rynmesh.node_apps.craft_automation_catalog import AUTOMATION


def _bundle(extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in {
            "portable/wordcraft-cli.exe": b"fixture executable",
            "portable/wordcraft.exe": b"fixture GUI",
            "portable/runtime.dll": b"fixture DLL",
            "portable/fonts/Regular.ttf": b"fixture font",
            "portable/LICENSE-APACHE": b"fixture upstream license",
            **(extra or {}),
        }.items():
            member = zipfile.ZipInfo(name)
            member.filename = name
            archive.writestr(member, content)
    return output.getvalue()


@pytest.fixture
def release(monkeypatch):
    def setup(data=None):
        data = _bundle() if data is None else data
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {row.filename: hashlib.sha256(archive.read(row)).hexdigest()
                     for row in archive.infolist() if not row.is_dir()}
        spec = {**AUTOMATION["wordcraft"][native.PLATFORM], "sha256": hashlib.sha256(data).hexdigest(),
                "payload_manifest_sha256": native._manifest_digest(files)}
        monkeypatch.setitem(AUTOMATION["wordcraft"], native.PLATFORM, spec)

        def download(url, path):
            assert url == "https://github.com/storytold/wordcraft/releases/download/v0.6.1/" + spec["asset"]
            Path(path).write_bytes(data)

        return download

    return setup


def test_explicit_install_preserves_payload_and_rechecks_without_network(tmp_path, release):
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "missing"
    assert not list(tmp_path.iterdir())
    result = native.install_runtime(tmp_path, "wordcraft", download=release())
    executable = native.installed_runtime(tmp_path, "wordcraft")
    assert executable == Path(result["executable"])
    assert executable.read_bytes() == b"fixture executable"
    assert (executable.parent / "fonts/Regular.ttf").read_bytes() == b"fixture font"
    assert (executable.parent / "runtime.dll").read_bytes() == b"fixture DLL"
    assert (executable.parent / "LICENSE-APACHE").read_bytes() == b"fixture upstream license"
    assert native.install_runtime(tmp_path, "wordcraft", download=lambda *_: pytest.fail("download")) == result
    status = native.runtime_status(tmp_path, "wordcraft")
    assert status["state"] == "ready" and status["installed"] and status["verified"]
    assert not list((executable.parents[2].parent).glob(".install-agent-*"))


@pytest.mark.parametrize("mutation", ["dll", "extra", "receipt", "missing"])
def test_every_installed_file_and_receipt_are_verified(tmp_path, release, mutation):
    native.install_runtime(tmp_path, "wordcraft", download=release())
    executable = native.installed_runtime(tmp_path, "wordcraft")
    if mutation == "dll":
        (executable.parent / "runtime.dll").write_bytes(b"tampered")
    elif mutation == "extra":
        (executable.parent / "extra.dll").write_bytes(b"unreviewed")
    elif mutation == "missing":
        (executable.parent / "LICENSE-APACHE").unlink()
    else:
        root = executable.parents[2]
        value = json.loads((root / "receipt.json").read_text())
        value["archive_sha256"] = "0" * 64
        (root / "receipt.json").write_text(json.dumps(value))
    with pytest.raises(native.CraftError, match="not_verified"):
        native.installed_runtime(tmp_path, "wordcraft")
    status = native.runtime_status(tmp_path, "wordcraft")
    assert status["state"] == "unverified" and not status["verified"]
    native.install_runtime(tmp_path, "wordcraft", download=release())
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "ready"
    assert not list(tmp_path.rglob(".replaced-agent-*"))


@pytest.mark.parametrize("failure", ["download", "rename"])
def test_failed_explicit_repair_preserves_previous_package(tmp_path, release, monkeypatch, failure):
    native.install_runtime(tmp_path, "wordcraft", download=release())
    executable = native.installed_runtime(tmp_path, "wordcraft")
    (executable.parent / "runtime.dll").write_bytes(b"damaged old package")
    download = release()
    if failure == "download":
        def download(url, path):
            path.write_bytes(b"bad download")
    else:
        original = Path.rename

        def fail_publish(path, target):
            if path.name.startswith(".install-agent-"):
                raise OSError("simulated atomic publish failure")
            return original(path, target)

        monkeypatch.setattr(Path, "rename", fail_publish)
    with pytest.raises((native.CraftError, OSError)):
        native.install_runtime(tmp_path, "wordcraft", download=download)
    assert (executable.parent / "runtime.dll").read_bytes() == b"damaged old package"
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "unverified"
    assert not list(tmp_path.rglob(".install-agent-*"))
    assert not list(tmp_path.rglob(".replaced-agent-*"))


def test_pin_mismatch_never_publishes(tmp_path):
    with pytest.raises(native.CraftError, match="checksum"):
        native.install_runtime(tmp_path, "wordcraft", download=lambda u, p: p.write_bytes(b"wrong"))
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "missing"
    assert not list(tmp_path.rglob(".install-agent-*"))


def test_payload_manifest_pin_is_not_a_claim_from_the_receipt(tmp_path, release):
    native.install_runtime(tmp_path, "wordcraft", download=release())
    executable = native.installed_runtime(tmp_path, "wordcraft")
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "ready"
    executable.write_bytes(b"replaced executable and forged receipt")
    root = executable.parents[2]
    receipt = root / "receipt.json"
    value = json.loads(receipt.read_bytes())
    relative = executable.relative_to(root / "payload").as_posix()
    value["files"][relative] = hashlib.sha256(executable.read_bytes()).hexdigest()
    # The attacker can rewrite this entire local file and keep archive_sha256.
    # Only the source-pinned payload-manifest digest binds executable bytes.
    receipt.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(native.CraftError, match="not_verified"):
        native.installed_runtime(tmp_path, "wordcraft")
    status = native.runtime_status(tmp_path, "wordcraft")
    assert status["state"] == "unverified" and not status["verified"]


def test_explicit_install_requires_archive_and_payload_manifest_pins(tmp_path, release, monkeypatch):
    download = release()
    spec = dict(AUTOMATION["wordcraft"][native.PLATFORM])
    spec["payload_manifest_sha256"] = "0" * 64
    monkeypatch.setitem(AUTOMATION["wordcraft"], native.PLATFORM, spec)
    with pytest.raises(native.CraftError, match="payload_mismatch"):
        native.install_runtime(tmp_path, "wordcraft", download=download)
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "missing"
    assert not list(tmp_path.rglob(".install-agent-*"))


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "a\\escape", "portable/../../escape",
                                  "portable/RUNTIME.dll", "portable/CON", "portable/file.", "portable/a:stream"])
def test_hostile_archive_never_writes_payload(tmp_path, release, name):
    with pytest.raises(native.CraftError, match="unsafe"):
        native.install_runtime(tmp_path, "wordcraft", download=release(_bundle({name: b"hostile"})))
    assert not list(tmp_path.rglob("wordcraft-cli.exe"))
    assert not list(tmp_path.rglob(".install-agent-*"))


def test_symlink_and_ambiguous_executable_rejected(tmp_path, release):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        link = zipfile.ZipInfo("portable/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../escape")
    with pytest.raises(native.CraftError, match="unsafe"):
        native.install_runtime(tmp_path, "wordcraft", download=release(output.getvalue()))
    with pytest.raises(native.CraftError, match="executable_invalid"):
        native.install_runtime(tmp_path, "wordcraft", download=release(_bundle({"other/wordcraft-cli.exe": b"duplicate"})))


def test_archive_count_and_expansion_are_bounded(tmp_path, release, monkeypatch):
    download = release()
    monkeypatch.setattr(native, "MAX_FILES", 2)
    with pytest.raises(native.CraftError, match="limit"):
        native.install_runtime(tmp_path, "wordcraft", download=download)
    monkeypatch.setattr(native, "MAX_FILES", 8192)
    monkeypatch.setattr(native, "MAX_EXPANDED", 4)
    with pytest.raises(native.CraftError, match="limit"):
        native.install_runtime(tmp_path, "wordcraft", download=download)


def test_unknown_runtime_never_downloads_or_writes(tmp_path):
    assert native.runtime_status(tmp_path, "../../private")["state"] == "unsupported"
    with pytest.raises(native.CraftError, match="unsupported"):
        native.install_runtime(tmp_path, "../../private", download=lambda *_: pytest.fail("download"))
    assert not list(tmp_path.iterdir())


def test_menu_verification_cache_saves_hashing_but_never_authorizes_startup(tmp_path, release, monkeypatch):
    native.install_runtime(tmp_path, "wordcraft", download=release())
    hashes = []
    original = native.craft_host.digest

    def digest(path):
        hashes.append(Path(path))
        return original(path)

    monkeypatch.setattr(native.craft_host, "digest", digest)
    first = native.runtime_status(tmp_path, "wordcraft")
    assert first["state"] == "ready" and first["verification_cached"] is False
    count = len(hashes)
    assert count == 5
    assert native.runtime_status(tmp_path, "wordcraft")["verification_cached"] is True
    assert len(hashes) == count
    # Process startup always calls this full verifier, even after a menu hit.
    native.installed_runtime(tmp_path, "wordcraft")
    assert len(hashes) == count * 2
    assert native.runtime_status(tmp_path, "wordcraft")["verification_cached"] is True


@pytest.mark.parametrize("change", ["tamper", "extra", "missing", "receipt"])
def test_payload_and_receipt_changes_invalidate_menu_cache(tmp_path, release, change):
    native.install_runtime(tmp_path, "wordcraft", download=release())
    executable = native.installed_runtime(tmp_path, "wordcraft")
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "ready"
    assert native.runtime_status(tmp_path, "wordcraft")["verification_cached"] is True
    if change == "tamper":
        (executable.parent / "runtime.dll").write_bytes(b"changed DLL")
    elif change == "extra":
        (executable.parent / "added.dll").write_bytes(b"unreviewed")
    elif change == "missing":
        (executable.parent / "runtime.dll").unlink()
    else:
        receipt = executable.parents[2] / "receipt.json"
        value = json.loads(receipt.read_bytes())
        value["app"] = "gridcraft"
        receipt.write_text(json.dumps(value), encoding="utf-8")
    status = native.runtime_status(tmp_path, "wordcraft")
    assert status["state"] == "unverified" and not status["verified"] and not status["verification_cached"]
    with pytest.raises(native.CraftError, match="not_verified"):
        native.installed_runtime(tmp_path, "wordcraft")


def test_receipt_content_change_rehashes_and_explicit_repair_clears_cache(tmp_path, release, monkeypatch):
    native.install_runtime(tmp_path, "wordcraft", download=release())
    executable = native.installed_runtime(tmp_path, "wordcraft")
    assert native.runtime_status(tmp_path, "wordcraft")["state"] == "ready"
    receipt = executable.parents[2] / "receipt.json"
    value = json.loads(receipt.read_bytes())
    value["review_note"] = "unknown inert field changes the receipt content stamp"
    receipt.write_text(json.dumps(value), encoding="utf-8")
    status = native.runtime_status(tmp_path, "wordcraft")
    assert status["state"] == "ready" and not status["verification_cached"]
    assert native.runtime_status(tmp_path, "wordcraft")["verification_cached"]
    (executable.parent / "runtime.dll").write_bytes(b"corrupt")
    native.install_runtime(tmp_path, "wordcraft", download=release())
    assert not native.runtime_status(tmp_path, "wordcraft")["verification_cached"]
    assert native.runtime_status(tmp_path, "wordcraft")["verification_cached"]
    # Caches are bounded across roots and pins, not one unbounded row per query.
    monkeypatch.setattr(native, "MAX_STATUS_CACHE", 2)
    for name in ("second", "third"):
        root = tmp_path / name
        native.install_runtime(root, "wordcraft", download=release())
        assert native.runtime_status(root, "wordcraft")["state"] == "ready"
    assert len(native._STATUS_CACHE) == 2
    assert not any(key[0] == os.path.normcase(str(executable.parents[2])) for key in native._STATUS_CACHE)


SERVER = r'''
import json,sys,time
mode=sys.argv[1]
for line in sys.stdin.buffer:
    value=json.loads(line)
    with open('received.ndjson','a',encoding='utf-8') as output:
        output.write(json.dumps(value)+'\n')
    if 'id' not in value:
        continue
    if value['method']=='initialize':
        result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
        if mode=='bad_init': result['protocolVersion']='future-unknown'
    elif mode=='wait':
        time.sleep(30)
        continue
    elif mode=='huge':
        sys.stdout.write('X'*4096+'\n');sys.stdout.flush();continue
    elif mode=='invalid':
        sys.stdout.write('not JSON\n');sys.stdout.flush();continue
    elif mode=='eof':
        break
    elif mode=='no_id':
        print(json.dumps({'jsonrpc':'2.0','result':{}}),flush=True);continue
    elif mode=='child':
        import subprocess
        child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
        result={'child_pid':child.pid}
    elif mode=='notification_flood':
        for _ in range(65):
            print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{}}),flush=True)
        continue
    elif value['method']=='tools/list':
        result={'tools':[{'name':'edit','inputSchema':{'type':'object'}}]}
    else:
        result={'content':[{'type':'text','text':json.dumps({'written':True,'unicode':'中文'})}],'isError':False}
    if mode=='stderr':
        sys.stderr.write('SECRET FROM STDERR'*100000);sys.stderr.flush()
    response={'jsonrpc':'2.0','id':value['id'],'result':result}
    if mode=='wrong_id' and value['method']!='initialize': response['id']+=1
    if mode=='rpc_error' and value['method']!='initialize':
        response={'jsonrpc':'2.0','id':value['id'],'error':{'code':-1,'message':'private paths and secrets'}}
    print(json.dumps(response),flush=True)
'''


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    script = tmp_path / "server.py"
    script.write_text(SERVER, encoding="utf-8")
    executable = tmp_path / "wordcraft-cli.exe"
    executable.write_bytes(b"fixture")
    original = native.process_guard.spawn
    captured = []

    def factory(mode="normal", app="wordcraft", **options):
        cli = tmp_path / f"{app}-cli.exe"
        cli.write_bytes(b"fixture")

        def spawn(args, **kwargs):
            captured.append((args, kwargs))
            return original([sys.executable, "-u", str(script), mode], **kwargs)

        monkeypatch.setattr(native.process_guard, "spawn", spawn)
        process = native.McpProcess(cli, tmp_path, app, timeout=5, **options)
        return process

    return factory, captured


def test_real_stdio_initialization_order_and_result_preservation(mcp, tmp_path):
    factory, captured = mcp
    process = factory()
    with pytest.raises(native.CraftError, match="not_initialized"):
        process.tools_list()
    with process:
        assert process.initialize()["serverInfo"]["name"] == "fixture"
        assert process.tools_list()["tools"][0]["name"] == "edit"
        value = process.tools_call("edit", {"text": "a\nb 中文"})
        assert json.loads(value["content"][0]["text"]) == {"written": True, "unicode": "中文"}
    lines = [json.loads(line) for line in (tmp_path / "received.ndjson").read_text().splitlines()]
    assert [row["method"] for row in lines] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert lines[0]["params"]["protocolVersion"] == "2025-06-18"
    assert lines[-1]["params"]["arguments"]["text"] == "a\nb 中文"
    assert captured[0][0][1:] == ["mcp", "--automation-read-root", str(tmp_path), "--automation-write-root", str(tmp_path)]
    assert process.process.poll() is not None
    assert not list(tmp_path.glob(".ryn-craft-*"))


@pytest.mark.parametrize("app", ["pdfcraft", "gridcraft", "deckcraft"])
def test_cli_flags_are_fixed_per_app(mcp, tmp_path, app):
    factory, captured = mcp
    with factory(app=app):
        pass
    assert captured[0][0][1:] == (["mcp", "--root", str(tmp_path)] if app == "pdfcraft" else ["mcp"])


def test_process_receives_no_node_or_owner_secrets(mcp, tmp_path, monkeypatch):
    monkeypatch.setenv("RYNMESH_LOCAL_TOKEN", "secret token")
    monkeypatch.setenv("OPENAI_API_KEY", "secret API key")
    factory, captured = mcp
    with factory():
        env = captured[0][1]["env"]
        assert "RYNMESH_LOCAL_TOKEN" not in env and "OPENAI_API_KEY" not in env
        assert Path(env["HOME"]).parent == tmp_path and env["HOME"] != os.environ.get("HOME")


@pytest.mark.parametrize("mode,error", [("wrong_id", "id_mismatch"), ("invalid", "protocol_invalid"),
                                         ("eof", "process_ended"), ("no_id", "id_mismatch"),
                                         ("rpc_error", "rpc_error"), ("huge", "output_limit"),
                                         ("notification_flood", "notification_limit|protocol_invalid")])
def test_protocol_failures_kill_session_without_echoing_payload(mcp, mode, error):
    factory, _ = mcp
    with factory(mode, max_output=2048 if mode == "huge" else native.MAX_OUTPUT) as process:
        with pytest.raises(native.CraftError, match=error) as caught:
            process.tools_list()
        assert "private paths" not in str(caught.value)
        assert process.closed.is_set() and process.process.poll() is not None


def test_unrecognized_initialization_closes_session(mcp):
    factory, _ = mcp
    process = factory("bad_init")
    with pytest.raises(native.CraftError, match="initialization_invalid"):
        process.initialize()
    assert process.closed.is_set() and process.process.poll() is not None


def test_timeout_and_cancellation_kill_waiting_stdio(mcp):
    factory, _ = mcp
    with factory("wait") as process:
        with pytest.raises(native.CraftError, match="timeout"):
            process.tools_list(timeout=0.15)
    cancelled = threading.Event()
    with factory("wait", cancelled=cancelled.is_set) as process:
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(native.CraftError, match="cancelled"):
                process.tools_list()
        finally:
            timer.cancel()
        assert time.monotonic() - started < 2
        assert process.process.poll() is not None


def test_stderr_is_discarded_and_not_allowed_to_block_or_leak(mcp):
    factory, _ = mcp
    with factory("stderr") as process:
        assert process.tools_list()["tools"][0]["name"] == "edit"


def test_expired_request_token_does_not_kill_idle_persistent_session(mcp):
    factory, _ = mcp
    expired = threading.Event()
    with factory() as process:
        assert process.tools_list(cancelled=expired.is_set)["tools"]
        expired.set()
        time.sleep(0.15)
        assert process.tools_list()["tools"]


def test_current_request_token_stops_persistent_session(mcp):
    factory, _ = mcp
    cancelled = threading.Event()
    with factory("wait") as process:
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            with pytest.raises(native.CraftError, match="cancelled"):
                process.tools_list(cancelled=cancelled.is_set)
        finally:
            timer.cancel()
        assert process.process.poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows job ownership")
def test_close_kills_descendants_in_windows_job(mcp):
    import ctypes
    from ctypes import wintypes

    factory, _ = mcp
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    with factory("child") as process:
        ident = process.tools_list()["child_pid"]
        handle = kernel.OpenProcess(0x100000 | 0x1000, False, ident)
        assert handle
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 0x102
            process.close()
            assert kernel.WaitForSingleObject(handle, 2000) == 0
        finally:
            kernel.CloseHandle(handle)


def test_unknown_method_and_excessive_request_are_rejected(mcp):
    factory, _ = mcp
    with factory() as process:
        with pytest.raises(native.CraftError, match="method_invalid"):
            process.request("arbitrary/execute", {})
        with pytest.raises(native.CraftError, match="input_limit"):
            process.tools_call("edit", {"text": "x" * native.MAX_REQUEST})
        assert process.process.poll() is not None
