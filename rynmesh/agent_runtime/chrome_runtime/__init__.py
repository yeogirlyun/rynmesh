"""Codex-aligned Chrome & Browser control runtime assets and helpers (`oai_browser_use`)."""
from __future__ import annotations

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
CONTROL_CHROME_SKILL_MD = SKILLS_DIR / "control-chrome" / "SKILL.md"
