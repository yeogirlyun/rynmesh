"""Codex-compatible skill metadata parser, local/curated skill discovery, and desktop actions."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from rynmesh.agent_runtime.extensions.store import (
    EXTENSION_ID_RE,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_PACKAGE_DEPTH,
    MAX_SKILL_PACKAGE_FILES,
    MAX_SKILL_TOTAL_BYTES,
)
from rynmesh.agent_runtime.store import AgentError

MAX_ICON_BYTES = 256 * 1024
SKIP_DISCOVERY_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "data",
    "reports",
    "logs",
    ".pytest_cache",
}

ICON_MIME_MAP = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

FALLBACK_CURATED_SKILLS: list[dict[str, Any]] = [
    {
        "id": "playwright",
        "name": "playwright",
        "title": "Playwright",
        "short_description": "Automate real browsers with Playwright scripts and assertions",
        "description": "Automate browser workflows, inspect DOM state, capture screenshots, and write deterministic end-to-end Playwright verification scripts.",
        "repo_path": "skills/.curated/playwright",
        "version": "1.0.0",
        "interface": {
            "display_name": "Playwright",
            "short_description": "Automate real browsers with Playwright scripts and assertions",
            "brand_color": "#2EAD33",
            "default_prompt": "Automate this browser workflow with Playwright and produce a reliable script with run steps.",
        },
        "content": (
            "---\nname: playwright\ndescription: Automate real browsers with Playwright scripts and assertions\n---\n"
            "# Playwright\n\n"
            "Use this skill to automate browser interactions, inspect live DOM state, capture screenshots, and verify web UI flows.\n\n"
            "## Workflow\n"
            "1. Launch a headless or headed Chromium session and navigate to the target URL.\n"
            "2. Wait for network/DOM readiness before interacting with controls.\n"
            "3. Assert visible states, console errors, and network responses.\n"
        ),
    },
    {
        "id": "figma",
        "name": "figma",
        "title": "Figma",
        "short_description": "Use Figma MCP for design-to-code work",
        "description": "Inspect Figma frames, design tokens, auto-layout hierarchy, and component properties to generate accurate production UI code.",
        "repo_path": "skills/.curated/figma",
        "version": "1.0.0",
        "interface": {
            "display_name": "Figma",
            "short_description": "Use Figma MCP for design-to-code work",
            "brand_color": "#0D99FF",
            "default_prompt": "Use $figma to inspect the target design and translate it into implementable UI decisions.",
        },
        "content": (
            "---\nname: figma\ndescription: Use Figma MCP for design-to-code work\n---\n"
            "# Figma\n\n"
            "Use this skill to inspect Figma designs, extract layout/spacing/typography tokens, and build faithful UI components.\n"
        ),
    },
    {
        "id": "pdf",
        "name": "pdf",
        "title": "PDF",
        "short_description": "Read, create, and review PDF files",
        "description": "Extract text and tables from PDFs, merge/split documents, and generate cleanly formatted PDF reports.",
        "repo_path": "skills/.curated/pdf",
        "version": "1.0.0",
        "interface": {
            "display_name": "PDF",
            "short_description": "Read, create, and review PDF files",
            "brand_color": "#E53935",
            "default_prompt": "Use $pdf to inspect or generate the requested PDF document and verify its layout.",
        },
        "content": (
            "---\nname: pdf\ndescription: Read, create, and review PDF files\n---\n"
            "# PDF\n\n"
            "Use this skill when reading, extracting tables from, or generating PDF documents.\n"
        ),
    },
    {
        "id": "cloudflare-deploy",
        "name": "cloudflare-deploy",
        "title": "Cloudflare Deploy",
        "short_description": "Deploy Workers, Pages, and platform services on Cloudflare",
        "description": "Configure Wrangler, Workers, Pages, D1, and KV bindings and deploy applications to Cloudflare safely.",
        "repo_path": "skills/.curated/cloudflare-deploy",
        "version": "1.0.0",
        "interface": {
            "display_name": "Cloudflare Deploy",
            "short_description": "Deploy Workers, Pages, and platform services on Cloudflare",
            "brand_color": "#F38020",
            "default_prompt": "Deploy this app to Cloudflare (Workers or Pages) and return URL, config, and required env vars.",
        },
        "content": (
            "---\nname: cloudflare-deploy\ndescription: Deploy Workers, Pages, and platform services on Cloudflare\n---\n"
            "# Cloudflare Deploy\n\n"
            "Use this skill to configure Wrangler and deploy Workers or Pages projects to Cloudflare.\n"
        ),
    },
    {
        "id": "gh-fix-ci",
        "name": "gh-fix-ci",
        "title": "GH Fix CI",
        "short_description": "Inspect failing GitHub Actions checks and fix CI issues",
        "description": "Fetch failing GitHub Actions logs with gh CLI, reproduce the failure locally, apply minimal root-cause fixes, and verify green checks.",
        "repo_path": "skills/.curated/gh-fix-ci",
        "version": "1.0.0",
        "interface": {
            "display_name": "GH Fix CI",
            "short_description": "Inspect failing GitHub Actions checks and fix CI issues",
            "brand_color": "#24292F",
            "default_prompt": "Use $gh-fix-ci to inspect the failing GitHub Actions checks on this branch and fix the root cause.",
        },
        "content": (
            "---\nname: gh-fix-ci\ndescription: Inspect failing GitHub Actions checks and fix CI issues\n---\n"
            "# GH Fix CI\n\n"
            "Use this skill to diagnose failing GitHub Actions runs, reproduce locally, and push a verified fix.\n"
        ),
    },
    {
        "id": "security-best-practices",
        "name": "security-best-practices",
        "title": "Security Best Practices",
        "short_description": "Audit code for security vulnerabilities and hardening opportunities",
        "description": "Perform structured security reviews covering injection, authentication, secret handling, path traversal, and privilege boundaries.",
        "repo_path": "skills/.curated/security-best-practices",
        "version": "1.0.0",
        "interface": {
            "display_name": "Security Best Practices",
            "short_description": "Audit code for security vulnerabilities and hardening opportunities",
            "brand_color": "#10B981",
            "default_prompt": "Use $security-best-practices to review this codebase for security vulnerabilities and propose concrete fixes.",
        },
        "content": (
            "---\nname: security-best-practices\ndescription: Audit code for security vulnerabilities and hardening opportunities\n---\n"
            "# Security Best Practices\n\n"
            "Use this skill to audit trust boundaries, input validation, authentication, and secret storage.\n"
        ),
    },
]


def _slugify(value: str, fallback: str = "skill") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._").lower()
    if not slug or not slug[0].isalnum():
        slug = f"ext-{slug}" if slug else fallback
    return slug[:64]


def _humanize_skill_id(value: str) -> str:
    parts = [p for p in re.split(r"[-_.\s]+", value.strip()) if p]
    if not parts:
        return value
    acronyms = {"cli": "CLI", "ci": "CI", "gh": "GH", "pdf": "PDF", "ui": "UI", "api": "API", "mcp": "MCP", "sql": "SQL", "vm": "VM"}
    out = []
    for p in parts:
        low = p.lower()
        if low in acronyms:
            out.append(acronyms[low])
        else:
            out.append(p.capitalize())
    return " ".join(out)


def _strip_quotes(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
        inner = val[1:-1]
        if val[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n")
        return inner
    return val


def parse_openai_yaml(yaml_text: str) -> dict[str, Any]:
    """Parse Codex agents/openai.yaml (interface, policy, dependencies) without external PyYAML dependency."""
    interface: dict[str, Any] = {}
    policy: dict[str, Any] = {}
    section = ""
    for raw_line in yaml_text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if indent == 0 and stripped.endswith(":"):
            section = stripped[:-1].strip().lower()
            continue
        if indent >= 2 and ":" in stripped and section in ("interface", "policy", "metadata"):
            key, val = stripped.split(":", 1)
            k = key.strip()
            v = _strip_quotes(val)
            if section in ("interface", "metadata"):
                if v:
                    interface[k] = v
            elif section == "policy":
                if v.lower() in ("true", "yes", "1"):
                    policy[k] = True
                elif v.lower() in ("false", "no", "0"):
                    policy[k] = False
    return {"interface": interface, "policy": policy}


def _file_to_data_url(path: Path) -> str | None:
    """Convert an icon file (.svg, .png, .jpg, .webp) inside a skill directory into a safe data URL."""
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size <= 0 or st.st_size > MAX_ICON_BYTES:
            return None
        suffix = path.suffix.lower()
        mime = ICON_MIME_MAP.get(suffix) or mimetypes.guess_type(path.name)[0]
        if not mime or not mime.startswith("image/"):
            return None
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except OSError:
        return None


def _resolve_icon_in_skill_dir(skill_dir: Path, raw_rel: str | None, candidates: list[str]) -> str | None:
    """Resolve an icon path relative to skill_dir or from assets/ fallback candidates as a data URL."""
    if not skill_dir or not skill_dir.is_dir():
        return None
    if raw_rel and isinstance(raw_rel, str):
        if raw_rel.startswith("data:image/"):
            return raw_rel
        norm = raw_rel.strip().replace("\\", "/")
        if norm.startswith("./"):
            norm = norm[2:]
        if norm and not norm.startswith("/") and ".." not in norm.split("/"):
            target = (skill_dir / norm).resolve()
            try:
                skill_resolved = skill_dir.resolve()
                if target == skill_resolved or skill_resolved in target.parents:
                    data_url = _file_to_data_url(target)
                    if data_url:
                        return data_url
            except OSError:
                pass
    assets_dir = skill_dir / "assets"
    if assets_dir.is_dir():
        for name in candidates:
            target = assets_dir / name
            if target.is_file():
                data_url = _file_to_data_url(target)
                if data_url:
                    return data_url
    return None


def extract_codex_skill_metadata(
    skill_dir: Path | None,
    skill_md_text: str,
    *,
    fallback_id: str = "skill",
) -> dict[str, Any]:
    """Extract Codex-compatible skill metadata from SKILL.md, agents/openai.yaml, SKILL.json, and assets/."""
    text = (skill_md_text or "").replace("\r\n", "\n")
    frontmatter: dict[str, str] = {}
    fm_interface: dict[str, str] = {}
    fm_metadata: dict[str, str] = {}
    body = text

    if text.startswith("---\n"):
        end_idx = text.find("\n---\n", 4)
        if end_idx != -1:
            fm_block = text[4:end_idx]
            body = text[end_idx + 5 :]
            sub_section = ""
            for raw_line in fm_block.splitlines():
                if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                    continue
                indent = len(raw_line) - len(raw_line.lstrip(" "))
                line = raw_line.strip()
                if indent == 0 and line.endswith(":") and ":" not in line[:-1]:
                    sub_section = line[:-1].strip().lower()
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    key = k.strip().lower()
                    val = _strip_quotes(v)
                    if indent == 0:
                        sub_section = ""
                        if val:
                            frontmatter[key] = val
                    elif sub_section == "metadata" and val:
                        fm_metadata[key] = val
                    elif sub_section == "interface" and val:
                        fm_interface[key] = val

    yaml_data: dict[str, Any] = {"interface": {}, "policy": {}}
    json_data: dict[str, Any] = {}
    if skill_dir and skill_dir.is_dir():
        for yaml_rel in ("agents/openai.yaml", "agents/openai.yml"):
            yml_path = skill_dir / yaml_rel
            try:
                if yml_path.is_file() and not os.path.islink(yml_path):
                    yaml_data = parse_openai_yaml(yml_path.read_text(encoding="utf-8", errors="replace"))
                    break
            except OSError:
                pass
        skill_json_path = skill_dir / "SKILL.json"
        try:
            if skill_json_path.is_file() and not os.path.islink(skill_json_path):
                loaded = json.loads(skill_json_path.read_text(encoding="utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    json_data = loaded
        except Exception:
            pass

    raw_yaml_iface = yaml_data.get("interface") if isinstance(yaml_data.get("interface"), dict) else {}
    raw_json_iface = json_data.get("interface") if isinstance(json_data.get("interface"), dict) else {}

    raw_name = (
        frontmatter.get("id")
        or frontmatter.get("name")
        or str(json_data.get("name") or "")
        or fallback_id
    )
    skill_id = _slugify(raw_name, fallback=_slugify(fallback_id))

    display_name = (
        raw_yaml_iface.get("display_name")
        or raw_yaml_iface.get("displayName")
        or raw_json_iface.get("display_name")
        or raw_json_iface.get("displayName")
        or fm_interface.get("display_name")
        or fm_interface.get("displayname")
        or frontmatter.get("title")
        or ""
    ).strip()

    heading_title = ""
    first_paragraph = ""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") and not heading_title:
            heading_title = s.lstrip("#").strip()
            continue
        if not s.startswith("#") and not first_paragraph:
            first_paragraph = s
            if heading_title:
                break

    title = (display_name or heading_title or _humanize_skill_id(raw_name) or skill_id)[:160]
    description = (
        frontmatter.get("description")
        or str(json_data.get("description") or "")
        or first_paragraph
        or title
    )[:1000]
    short_description = (
        raw_yaml_iface.get("short_description")
        or raw_yaml_iface.get("shortDescription")
        or raw_json_iface.get("short_description")
        or raw_json_iface.get("shortDescription")
        or fm_metadata.get("short-description")
        or fm_metadata.get("short_description")
        or fm_interface.get("short_description")
        or fm_interface.get("shortdescription")
        or frontmatter.get("summary")
        or description
    )[:240]
    version = (frontmatter.get("version") or str(json_data.get("version") or "1.0.0"))[:64]

    raw_icon_small = (
        raw_yaml_iface.get("icon_small")
        or raw_yaml_iface.get("iconSmall")
        or raw_json_iface.get("icon_small")
        or raw_json_iface.get("iconSmall")
        or fm_interface.get("icon_small")
    )
    raw_icon_large = (
        raw_yaml_iface.get("icon_large")
        or raw_yaml_iface.get("iconLarge")
        or raw_json_iface.get("icon_large")
        or raw_json_iface.get("iconLarge")
        or fm_interface.get("icon_large")
    )

    dir_stem = skill_dir.name if skill_dir else skill_id
    icon_small = (
        _resolve_icon_in_skill_dir(
            skill_dir,
            raw_icon_small,
            [
                f"{dir_stem}-small.svg",
                f"{skill_id}-small.svg",
                f"{dir_stem}-small.png",
                f"{skill_id}-small.png",
                "small.svg",
                "small.png",
                "icon-small.svg",
                "icon.svg",
            ],
        )
        if skill_dir
        else None
    )
    icon_large = (
        _resolve_icon_in_skill_dir(
            skill_dir,
            raw_icon_large,
            [
                f"{dir_stem}.png",
                f"{skill_id}.png",
                f"{dir_stem}-large.svg",
                f"{skill_id}-large.svg",
                f"{dir_stem}.svg",
                f"{skill_id}.svg",
                "large.png",
                "large.svg",
                "icon.png",
                "icon.svg",
            ],
        )
        if skill_dir
        else None
    )

    brand_color = (
        raw_yaml_iface.get("brand_color")
        or raw_yaml_iface.get("brandColor")
        or raw_json_iface.get("brand_color")
        or raw_json_iface.get("brandColor")
        or fm_interface.get("brand_color")
        or None
    )
    default_prompt = (
        raw_yaml_iface.get("default_prompt")
        or raw_yaml_iface.get("defaultPrompt")
        or raw_json_iface.get("default_prompt")
        or raw_json_iface.get("defaultPrompt")
        or fm_interface.get("default_prompt")
        or None
    )

    policy_dict = yaml_data.get("policy") if isinstance(yaml_data.get("policy"), dict) else {}
    allow_implicit = bool(policy_dict.get("allow_implicit_invocation", True))

    return {
        "id": skill_id,
        "name": raw_name[:120],
        "title": title,
        "description": description,
        "short_description": short_description,
        "summary": short_description[:240],
        "version": version,
        "content": text,
        "interface": {
            "display_name": display_name or title,
            "short_description": short_description,
            "icon_small": icon_small,
            "icon_large": icon_large,
            "brand_color": brand_color,
            "default_prompt": default_prompt,
        },
        "policy": {
            "allow_implicit_invocation": allow_implicit,
        },
    }


def compute_safe_codex_skill_manifest(pkg_dir: Path) -> dict[str, Any]:
    """Compute a bounded manifest of files inside a skill directory, skipping oversized/runtime artifacts."""
    root = pkg_dir.resolve()
    files: list[dict[str, Any]] = []
    total_bytes = 0

    def _visit(current: Path, depth: int) -> None:
        nonlocal total_bytes
        if depth > MAX_SKILL_PACKAGE_DEPTH or len(files) >= MAX_SKILL_PACKAGE_FILES:
            return
        try:
            entries = sorted(current.iterdir(), key=lambda p: (p.name != "SKILL.md", p.name))
        except OSError:
            return
        for entry in entries:
            if len(files) >= MAX_SKILL_PACKAGE_FILES:
                break
            try:
                st = os.lstat(entry)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_ISDIR(st.st_mode):
                if entry.name.lower() in SKIP_DISCOVERY_DIRS or entry.name.startswith("."):
                    continue
                _visit(entry, depth + 1)
            elif stat.S_ISREG(st.st_mode):
                if st.st_size > MAX_SKILL_FILE_BYTES:
                    continue
                if total_bytes + st.st_size > MAX_SKILL_TOTAL_BYTES:
                    continue
                rel = entry.resolve().relative_to(root).as_posix()
                try:
                    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
                except OSError:
                    continue
                total_bytes += st.st_size
                files.append({"path": rel, "size": st.st_size, "sha256": digest})

    _visit(root, 0)
    files.sort(key=lambda item: item["path"])
    overall = hashlib.sha256()
    for item in files:
        overall.update(f"{item['path']}:{item['size']}:{item['sha256']}\n".encode("utf-8"))
    return {
        "files": files,
        "total_bytes": total_bytes,
        "manifest_sha256": overall.hexdigest(),
    }


def copy_codex_skill_bounded(src_dir: Path, dst_dir: Path) -> Path:
    """Copy a skill directory into dst_dir keeping only files within ExtensionStore bounds."""
    src_root = src_dir.resolve()
    dst_root = dst_dir.resolve()
    dst_root.mkdir(parents=True, exist_ok=True)
    manifest = compute_safe_codex_skill_manifest(src_root)
    for item in manifest["files"]:
        rel_path = item["path"]
        src_file = src_root / Path(*rel_path.split("/"))
        dst_file = dst_root / Path(*rel_path.split("/"))
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, dst_file)
    return dst_root


def default_codex_home() -> Path:
    env_home = os.environ.get("CODEX_HOME", "").strip()
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


def default_local_skill_roots(workspace_roots: list[Path] | None = None) -> list[tuple[Path, str]]:
    """Return ordered (directory_path, scope) roots matching Codex's local skill discovery."""
    roots: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def _add(p: Path, scope: str) -> None:
        try:
            key = str(p.expanduser().resolve()).lower()
        except OSError:
            return
        if key not in seen:
            seen.add(key)
            roots.append((p.expanduser(), scope))

    for ws in workspace_roots or []:
        if ws:
            _add(ws / ".codex" / "skills", "repo")
            _add(ws / ".agents" / "skills", "repo")

    codex_home = default_codex_home()
    _add(codex_home / "skills" / ".system", "system")
    _add(codex_home / "skills", "user")
    _add(Path.home() / ".agents" / "skills", "user")
    return roots


def discover_local_skills(
    *,
    roots: list[tuple[Path, str]] | None = None,
    workspace_roots: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Scan local Codex/Agent skill roots and return discovered skill records."""
    search_roots = roots if roots is not None else default_local_skill_roots(workspace_roots)
    discovered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for root_dir, scope in search_roots:
        if not root_dir.is_dir():
            continue
        try:
            children = sorted(root_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                st = os.lstat(child)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                continue
            skill_md = child / "SKILL.md"
            try:
                md_st = os.lstat(skill_md)
            except OSError:
                continue
            if stat.S_ISLNK(md_st.st_mode) or not stat.S_ISREG(md_st.st_mode):
                continue
            if md_st.st_size > MAX_SKILL_FILE_BYTES:
                continue
            try:
                md_text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            meta = extract_codex_skill_metadata(child, md_text, fallback_id=child.name)
            sid = meta["id"]
            if not EXTENSION_ID_RE.match(sid) or sid in seen_ids:
                continue
            seen_ids.add(sid)
            manifest = compute_safe_codex_skill_manifest(child)
            discovered.append(
                {
                    "id": sid,
                    "name": meta["name"],
                    "title": meta["title"],
                    "description": meta["description"],
                    "short_description": meta["short_description"],
                    "summary": meta["summary"],
                    "version": meta["version"],
                    "content": meta["content"],
                    "package_dir": str(child.resolve()),
                    "skill_md_path": str(skill_md.resolve()),
                    "scope": scope,
                    "source_format": "codex_local",
                    "files": manifest["files"],
                    "manifest_sha256": manifest["manifest_sha256"],
                    "interface": meta["interface"],
                    "policy": meta["policy"],
                }
            )
    return discovered


def default_curated_skill_roots() -> list[Path]:
    """Return candidate directories for Codex curated recommended skills."""
    codex_home = default_codex_home()
    candidates = [
        codex_home / "vendor_imports" / "skills" / "skills" / ".curated",
        codex_home / "vendor_imports" / "skills" / ".curated",
    ]
    return [c for c in candidates if c.is_dir()]


def discover_recommended_skills(
    *,
    curated_roots: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """List recommended curated skills from Codex vendor_imports (or built-in fallback catalog)."""
    roots = curated_roots if curated_roots is not None else default_curated_skill_roots()
    skills: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for root_dir in roots:
        if not root_dir.is_dir():
            continue
        try:
            children = sorted(root_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                st = os.lstat(child)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                continue
            skill_md = child / "SKILL.md"
            try:
                md_st = os.lstat(skill_md)
            except OSError:
                continue
            if stat.S_ISLNK(md_st.st_mode) or not stat.S_ISREG(md_st.st_mode):
                continue
            if md_st.st_size > MAX_SKILL_FILE_BYTES:
                continue
            try:
                md_text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            meta = extract_codex_skill_metadata(child, md_text, fallback_id=child.name)
            sid = meta["id"]
            if not EXTENSION_ID_RE.match(sid) or sid in seen_ids:
                continue
            seen_ids.add(sid)
            skills.append(
                {
                    "id": sid,
                    "name": meta["name"],
                    "title": meta["title"],
                    "description": meta["description"],
                    "short_description": meta["short_description"],
                    "summary": meta["summary"],
                    "version": meta["version"],
                    "repo_path": f"skills/.curated/{child.name}",
                    "source_dir": str(child.resolve()),
                    "path": str(skill_md.resolve()),
                    "scope": "curated",
                    "content": meta["content"],
                    "interface": meta["interface"],
                    "policy": meta["policy"],
                }
            )

    if not skills:
        for item in FALLBACK_CURATED_SKILLS:
            iface = dict(item.get("interface") or {})
            skills.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "title": item["title"],
                    "description": item["description"],
                    "short_description": item["short_description"],
                    "summary": item["short_description"],
                    "version": item.get("version") or "1.0.0",
                    "repo_path": item["repo_path"],
                    "source_dir": None,
                    "path": item["repo_path"],
                    "scope": "curated",
                    "content": item["content"],
                    "interface": {
                        "display_name": iface.get("display_name") or item["title"],
                        "short_description": iface.get("short_description") or item["short_description"],
                        "icon_small": iface.get("icon_small"),
                        "icon_large": iface.get("icon_large"),
                        "brand_color": iface.get("brand_color"),
                        "default_prompt": iface.get("default_prompt"),
                    },
                    "policy": {"allow_implicit_invocation": True},
                }
            )
    return skills


def open_or_reveal_skill_path(target_path: Path, *, action: str = "open") -> dict[str, Any]:
    """Open SKILL.md in default editor or reveal it in File Explorer / Finder."""
    resolved = target_path.expanduser().resolve()
    if not resolved.exists():
        raise AgentError("agent_skill_not_found")
    act = (action or "open").strip().lower()
    if act not in ("open", "reveal"):
        raise AgentError("agent_invalid_request")

    try:
        if sys.platform.startswith("win"):
            if act == "reveal":
                if resolved.is_file():
                    subprocess.Popen(["explorer.exe", f"/select,{resolved}"])
                else:
                    subprocess.Popen(["explorer.exe", str(resolved)])
            else:
                os.startfile(str(resolved))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            if act == "reveal":
                subprocess.Popen(["open", "-R", str(resolved)])
            else:
                subprocess.Popen(["open", str(resolved)])
        else:
            folder = resolved.parent if resolved.is_file() else resolved
            subprocess.Popen(["xdg-open", str(folder if act == "reveal" else resolved)])
    except Exception as exc:
        raise AgentError("agent_skill_open_failed") from exc

    return {"ok": True, "action": act, "path": str(resolved)}
