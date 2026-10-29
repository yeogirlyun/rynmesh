"""First-party CDP bridge and an optional, externally supplied browser adapter."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
DOCS_DIR = PACKAGE_ROOT / "docs"
ASSETS_DIR = PACKAGE_ROOT / "assets"
SKILLS_DIR = PACKAGE_ROOT / "skills"

BROWSER_CLIENT_MJS = SCRIPTS_DIR / "browser-client.mjs"
BROWSER_SERVICE_MJS = SCRIPTS_DIR / "browser-service.mjs"
BROWSER_ACCESSIBILITY_WASM_BR = SCRIPTS_DIR / "browser-accessibility.wasm.br"
CDP_PIPE_SERVER_MJS = SCRIPTS_DIR / "cdp-pipe-server.mjs"
def external_runtime_dir() -> Path | None:
    """Resolve an explicitly installed adapter, without scanning other apps."""
    configured = os.environ.get("RYN_CHROME_RUNTIME_DIR", "").strip()
    return Path(configured).expanduser().resolve() if configured else None


def external_runtime_available() -> bool:
    root = external_runtime_dir()
    return bool(root and all((root / "scripts" / name).is_file() for name in (
        "browser-client.mjs", "browser-service.mjs",
    )) and any((root / folder / "browser-accessibility.wasm.br").is_file()
               for folder in ("scripts", "assets")))
