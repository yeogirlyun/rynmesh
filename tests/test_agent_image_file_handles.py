"""Real host image reads, independent of the optional QuickJS worker profile."""
import hashlib
import io

import pytest
from PIL import Image

from rynmesh.agent_runtime.image_capabilities import read_workspace_image_bytes
from rynmesh.agent_runtime.js_runtime.images import revalidate_artifact_png_bytes
from rynmesh.agent_runtime.js_runtime.protocol import JsProtocolError
from rynmesh.agent_runtime.store import AgentToolRejected


@pytest.fixture(params=["workspace", "native_artifact"])
def read_image(request):
    def read(path, root):
        if request.param == "workspace":
            return read_workspace_image_bytes(path, workspace_root=root)
        return revalidate_artifact_png_bytes(artifact_path=path, containment_root=root)[0]
    return read


def png():
    output = io.BytesIO()
    Image.new("RGB", (12, 8), color=(40, 100, 200)).save(output, format="PNG")
    return output.getvalue()


def test_image_read_from_opened_handle_accepts_unicode_path(tmp_path, read_image):
    path = tmp_path / "图片.png"
    raw = png()
    path.write_bytes(raw)
    assert read_image(path, tmp_path) == raw


def test_image_read_rejects_outside_file_and_symlink(tmp_path, read_image):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(png())
    for path in [outside, root / "link.png"]:
        if path != outside:
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("Host does not permit symlink creation")
        with pytest.raises((AgentToolRejected, JsProtocolError)):
            read_image(path, root)


def test_native_artifact_keeps_hash_verification(tmp_path):
    path = tmp_path / "screenshot.png"
    path.write_bytes(png())
    with pytest.raises(JsProtocolError, match="[Hh]ash|SHA"):
        revalidate_artifact_png_bytes(artifact_path=path, containment_root=tmp_path,
                                     expected_sha256=hashlib.sha256(b"another screenshot").hexdigest())
