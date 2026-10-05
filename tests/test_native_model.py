import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from rynmesh.llm_package import native_model as nm
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.routes import install_llm_routes
from rynmesh.services import peer_box
from rynmesh.store import RynmeshStore


def wait(model):
    model.worker.join(timeout=5)
    assert not model.worker.is_alive()


def tiny_model(tmp_path, monkeypatch=None, *, name="My local model", arch="qwen3", split=1):
    def string(value):
        data = value.encode()
        return struct.pack("<Q", len(data)) + data
    values = {"general.name": name, "general.architecture": arch, "split.count": split}
    data = b"GGUF" + struct.pack("<IQQ", 3, 1, len(values))
    for key, value in values.items():
        data += string(key) + (struct.pack("<I", 8) + string(value) if isinstance(value, str) else struct.pack("<II", 4, value))
    data += string("test.weight") + struct.pack("<IQIQ", 1, 32, 0, 0)
    data += bytes((-len(data)) % 32) + bytes(128)
    path = tmp_path / "original.gguf"
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("system,arch,expected", [("Windows", "AMD64", "windows-cpu"), ("Darwin", "arm64", "macos-metal"), ("Darwin", "x86_64", None), ("Windows", "ARM64", None)])
def test_platform_dispatch(monkeypatch, system, arch, expected):
    monkeypatch.setattr(nm.platform, "system", lambda: system)
    monkeypatch.setattr(nm.platform, "machine", lambda: arch)
    if expected:
        assert nm.platform_key() == expected
    else:
        with pytest.raises(nm.NativeModelError):
            nm.platform_key()


def test_import_checks_full_content_not_filename(tmp_path, monkeypatch):
    path = tiny_model(tmp_path, monkeypatch)
    metadata = nm.validate_model(path)
    assert metadata["name"] == "My local model"
    assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(nm.NativeModelError, match="invalid_gguf"):
        nm.validate_model(path)
    path = path.rename(path.with_suffix(".zip"))
    with pytest.raises(nm.NativeModelError, match="unsupported_file"):
        nm.validate_model(path)


def test_archive_rejects_paths_outside_runtime(tmp_path):
    archive = tmp_path / "engine.zip"
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("../escape.exe", b"bad")
    with pytest.raises(nm.NativeModelError, match="runtime_archive_invalid"):
        nm.extract_runtime(archive, tmp_path / "runtime")
    assert not (tmp_path / "escape.exe").exists()


def test_autostart_is_persistent_and_applies_only_on_application_start(tmp_path, monkeypatch):
    model = nm.NativeModel(tmp_path)
    calls = []
    monkeypatch.setattr(model, "action", lambda *a, **k: calls.append(a))
    model.config["installed"] = True
    path = tiny_model(tmp_path)
    model.model_path.parent.mkdir(parents=True)
    model.model_path.write_bytes(path.read_bytes())
    model.metadata = nm.validate_model(path)
    model.preferences({"autostart": True})
    assert calls == []  # changing the preference does not start immediately
    restored = nm.NativeModel(tmp_path)
    monkeypatch.setattr(restored, "action", lambda *a, **k: calls.append(a))
    restored.on_startup()
    assert calls == [("start",)]
    restored.preferences({"autostart": False})
    restored = nm.NativeModel(tmp_path)
    monkeypatch.setattr(restored, "action", lambda *a, **k: calls.append(a))
    restored.on_startup()
    assert len(calls) == 1
    with pytest.raises(nm.NativeModelError):
        restored.preferences({"autostart": "false"})
    with pytest.raises(nm.NativeModelError):
        restored.preferences({"runtime_command": ["bad"]})


def test_import_copies_and_selftests_before_ready(tmp_path, monkeypatch):
    path = tiny_model(tmp_path, monkeypatch)
    model = nm.NativeModel(tmp_path / "home")
    monkeypatch.setattr(nm, "platform_key", lambda: "windows-cpu")
    monkeypatch.setattr(nm, "_memory", lambda: (16000, 8000))
    monkeypatch.setattr(nm, "_nvidia", lambda: ([], ""))
    monkeypatch.setattr(model, "_engine", lambda _: Path("engine"))
    def start(*_):
        assert model.state == "installing"
        assert path.read_bytes() == model.model_path.read_bytes()
    monkeypatch.setattr(model, "_start", start)
    model.action("import", source=str(path))
    wait(model)
    assert model.state == "running"
    assert model.config["installed"] and not model.config["sharing"]
    model.action("uninstall")
    wait(model)
    assert path.exists() and not model.model_path.exists()
    assert not model.config["autostart"]


def test_failed_start_preserves_valid_model_and_never_marks_ready(tmp_path, monkeypatch):
    path = tiny_model(tmp_path, monkeypatch)
    model = nm.NativeModel(tmp_path / "home")
    monkeypatch.setattr(nm, "platform_key", lambda: "windows-cpu")
    monkeypatch.setattr(nm, "_memory", lambda: (16000, 8000))
    monkeypatch.setattr(nm, "_nvidia", lambda: ([], ""))
    monkeypatch.setattr(model, "_engine", lambda _: (_ for _ in ()).throw(nm.NativeModelError("runtime_missing")))
    model.action("import", source=str(path))
    wait(model)
    assert model.state == "error" and model.config["installed"]
    assert model.model_path.exists() and not model.adapter.health()["ok"]


def test_stop_drains_all_consumers_and_rejects_new_requests(tmp_path, monkeypatch):
    class Process:
        def poll(self):
            return None
    model = nm.NativeModel(tmp_path)
    model.process = Process()
    model.state = "running"
    model.config.update(installed=True, autostart=True)
    killed = []
    monkeypatch.setattr(model, "_kill", lambda: killed.append(True))
    with model.adapter.request():
        model.action("stop")
        assert model.state == "stopping"
        with pytest.raises(AdapterError, match="stopped"):
            with model.adapter.request():
                pass
        assert not killed
    wait(model)
    assert killed and model.state == "stopped"
    assert model.config["autostart"]  # stop now does not change next launch preference


def test_resume_download_verifies_range_and_whole_digest(tmp_path, monkeypatch):
    target = tmp_path / "model.gguf"
    data = b"0123456789"
    target.with_name(target.name + ".part").write_bytes(data[:4])
    class Response(io.BytesIO):
        status = 206
        headers = {"Content-Range": "bytes 4-9/10"}
    def open_request(request, **kwargs):
        assert request.headers["Range"] == "bytes=4-"
        return Response(data[4:])
    monkeypatch.setattr(nm.urllib.request, "urlopen", open_request)
    nm.download("https://example.invalid/model", target, len(data), hashlib.sha256(data).hexdigest(), lambda *_: None, lambda: False)
    assert target.read_bytes() == data


def test_native_routes_preferences_catalog_and_sharing_guard(tmp_path):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    install_llm_routes(app, store=store, home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    with TestClient(app) as client:
        state = client.get("/api/local/llm/native").json()
        assert not state["installed"] and not state["autostart"]
        assert client.patch("/api/local/llm/native", json={"autostart": True}).json()["autostart"]
        assert client.patch("/api/local/llm/native", json={"sharing": True}).status_code == 409
        assert client.patch("/api/local/llm/native", json={"autostart": 1}).status_code == 400
        assert client.post("/api/local/llm/native/start", json={}).status_code == 409
        assert client.post("/api/local/llm/native/install", json={"command": "no"}).status_code == 400
        assert not client.get("/api/local/llm/api-access").json()["targets"]
        assert "runtime_command" not in json.dumps(state)
    app.state.native_model.close()


@pytest.mark.parametrize("arch", ["qwen3", "llama", "gemma2", "phi3"])
def test_import_is_not_locked_to_one_model(tmp_path, arch):
    path = tiny_model(tmp_path, name="User chosen " + arch, arch=arch)
    assert nm.validate_model(path)["name"] == "User chosen " + arch


@pytest.mark.parametrize("arch,split,error", [("clip", 1, "unsupported_model"), ("qwen3", 2, "split_model_unsupported")])
def test_rejects_unsupported_architecture_and_shards(tmp_path, arch, split, error):
    path = tiny_model(tmp_path, arch=arch, split=split)
    with pytest.raises(nm.NativeModelError, match=error):
        nm.validate_model(path)


def test_rejects_unbounded_metadata(tmp_path):
    path = tmp_path / "bad.gguf"
    path.write_bytes(b"GGUF" + struct.pack("<IQQQ", 3, 1, 1, 2**60))
    with pytest.raises(nm.NativeModelError, match="invalid_gguf"):
        nm.validate_model(path)


def test_changed_import_copy_cannot_start(tmp_path, monkeypatch):
    path = tiny_model(tmp_path)
    model = nm.NativeModel(tmp_path / "home")
    model.model_path.parent.mkdir(parents=True)
    model.model_path.write_bytes(path.read_bytes())
    model.metadata = nm.validate_model(path)
    model.config["installed"] = True
    model.model_path.write_bytes(path.read_bytes()[:-1] + b"X")
    monkeypatch.setattr(nm, "platform_key", lambda: "windows-cpu")
    model.action("start")
    wait(model)
    assert model.error == "model_changed"


def test_browser_upload_streams_import_and_cleans_staging(tmp_path, monkeypatch):
    home = tmp_path / "node"
    store = RynmeshStore(home=home, network_dir=tmp_path / "network")
    app = FastAPI()
    from rynmesh.llm_package.safety import install_body_limit
    install_body_limit(app)
    install_llm_routes(app, store=store, home=home,
        messaging_key=peer_box.load_or_create_messaging_key(home / "messaging.x25519"),
        resolve_endpoint=lambda _: "", resolve_pubkey=lambda _: "")
    model = app.state.native_model
    monkeypatch.setattr(nm, "platform_key", lambda: "windows-cpu")
    monkeypatch.setattr(nm, "_memory", lambda: (16000, 8000))
    monkeypatch.setattr(nm, "_nvidia", lambda: ([], ""))
    monkeypatch.setattr(model, "_engine", lambda _: Path("engine"))
    monkeypatch.setattr(model, "_start", lambda *_: None)
    path = tiny_model(tmp_path, name="Another model", arch="llama")
    with path.open("ab") as output:
        output.write(bytes(2 * 1024**2))  # greater than the ordinary JSON request limit
    with TestClient(app) as client:
        assert client.post("/api/local/llm/native/install", json={}).status_code == 409
        assert client.post("/api/local/llm/native/upload?filename=bad.zip", content=b"bad").status_code == 400
        response = client.post("/api/local/llm/native/upload?filename=chosen.gguf", content=path.read_bytes())
        assert response.status_code == 200
        wait(model)
        state = client.get("/api/local/llm/native").json()
        assert state["name"] == "Another model" and state["file_name"] == "chosen.gguf"
        assert state["id"].endswith(nm.digest_file(path)[:16])
        assert model.model_path.read_bytes() == path.read_bytes()
        assert not list(model.root.glob("tmp*.gguf"))
        assert client.post("/api/local/llm/native/import", json={"source": str(path)}).status_code == 409
        # Installed service metadata follows the user-selected model.
        monkeypatch.setattr(model.adapter, "health", lambda: {"ok": True})
        targets = client.get("/api/local/llm/api-access").json()["targets"]
        assert any(t["rynmesh"]["model_alias"] == "Another model" for t in targets)
    model.close()
