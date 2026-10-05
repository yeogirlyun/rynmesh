"""The desktop runtime fetcher runs outside the sidecar's build environment."""

import json
import subprocess
import sys
from pathlib import Path

from rynmesh.llm_package.runtime_native_install import asset_for


def test_runtime_pins_can_be_read_without_installed_app_dependencies():
    result = subprocess.run(
        [sys.executable, "-S", "-c",
         "import json; from rynmesh.llm_package.runtime_native_install import asset_for; "
         "print(json.dumps(asset_for('Darwin', 'arm64')))"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15, check=True,
    )
    assert json.loads(result.stdout) == list(asset_for("Darwin", "arm64"))
