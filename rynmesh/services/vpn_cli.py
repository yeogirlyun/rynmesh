"""Console entry point for the bundled ``rynmesh-vpn`` egress-tunnel script.

The tool itself is a self-contained bash script shipped as package data; this
wrapper locates it and hands off ``argv`` so that ``pip install rynmesh`` puts
a ``rynmesh-vpn`` command on PATH. Invoked via ``bash <script>`` so it works
even though wheels strip the executable bit from package data.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def script_path() -> Path:
    """Absolute path to the bundled ``rynmesh-vpn`` bash script."""
    return Path(__file__).resolve().parent / "rynmesh-vpn"


def main() -> None:
    script = script_path()
    if not script.is_file():
        sys.stderr.write(f"rynmesh-vpn: bundled script not found at {script}\n")
        raise SystemExit(1)
    bash = shutil.which("bash") or "/bin/bash"
    # exec replaces this process so signals / exit codes pass through cleanly.
    os.execv(bash, [bash, str(script), *sys.argv[1:]])
