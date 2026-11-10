#!/usr/bin/env python3
"""Build the pinned Ryn GridCraft web variant with an external OFL Chinese fallback.

No installed editor, catalog, user workbook or upstream release receipt is changed.
Run ``prepare`` before a manual build, or ``build`` to run Cargo and wasm-bindgen.
The packaging step is deterministic for identical distribution files and toolchain.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

UPSTREAM_COMMIT = "fc45b7f6d8ab79ef20f74e3c8347a2fd731e6af5"
UPSTREAM_URL = "https://github.com/storytold/gridcraft"
FONT_COMMIT = "f8d157532fbfaeda587e826d4cd5b21a49186f7c"
FONT_BASE = f"https://raw.githubusercontent.com/notofonts/noto-cjk/{FONT_COMMIT}/Sans"
FONT_NAME = "NotoSansCJKsc-Regular.otf"
FONT_URL = f"{FONT_BASE}/OTF/SimplifiedChinese/{FONT_NAME}"
FONT_SHA256 = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"
LICENSE_URL = f"{FONT_BASE}/LICENSE"
LICENSE_SHA256 = "6a73f9541c2de74158c0e7cf6b0a58ef774f5a780bf191f2d7ec9cc53efe2bf2"
WASM_BINDGEN_VERSION = "0.2.129"
BASE_WEB_SHA256 = "86048ba175689ff852c141d6115e5491f0856f891300c3e1f91274b9c2709a42"
BASE_WEB_URL = f"{UPSTREAM_URL}/releases/download/v0.6.1/gridcraft-web-0.6.1.zip"
BASE_BINDINGS_NAME = "gridcraft-web-9e7653abe17db176"
THEME = "crates/ui-egui/src/theme.rs"
BUILD_ID = "gridcraft-web-0.6.1-ryn-cjk-1"
MAX_FILE = 128 * 1024 * 1024
FIXED_ZIP_TIME = (2026, 10, 18, 0, 0, 0)
OLD = '''#[cfg(target_arch = "wasm32")]
fn load_fallback(_fonts: &mut FontDefinitions, _han: HanOrder) -> Vec<String> {
    Vec::new()
}'''
NEW = '''#[cfg(target_arch = "wasm32")]
fn load_fallback(fonts: &mut FontDefinitions, _han: HanOrder) -> Vec<String> {
    // Ryn's web build supplies this OFL font as an external, SHA-256-pinned build input.
    // Keep it in the font-definition factory so opening a workbook or changing language
    // cannot discard the fallback when egui rebuilds its font families.
    let bytes = include_bytes!(concat!(env!("CRAFT_FONTS_DIR"), "/NotoSansCJKsc-Regular.otf"));
    if !font_parses(bytes, 0) {
        log::warn!("Ryn CJK fallback is not a font egui can read");
        return Vec::new();
    }
    let key = "ryn-cjk-sc".to_string();
    fonts.font_data.insert(key.clone(), Arc::new(FontData::from_static(bytes)));
    // Cover the default families too; named UI and cell roles receive the returned key.
    for chain in fonts.families.values_mut() {
        chain.push(key.clone());
    }
    vec![key]
}'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def run(command, *, cwd=None, env=None):
    return subprocess.check_output(command, cwd=cwd, env=env, text=True, encoding="utf-8").strip()


def source_patch(source):
    if run(["git", "rev-parse", "HEAD"], cwd=source) != UPSTREAM_COMMIT:
        raise ValueError(f"source must be the pinned upstream commit {UPSTREAM_COMMIT}")
    changed = set(run(["git", "diff", "--name-only", "HEAD"], cwd=source).splitlines())
    if changed - {THEME}:
        raise ValueError(f"unexpected tracked source changes: {sorted(changed - {THEME})}")
    original = run(["git", "show", f"{UPSTREAM_COMMIT}:{THEME}"], cwd=source) + "\n"
    if original.count(OLD) != 1:
        raise ValueError("pinned upstream fallback anchor does not match")
    patched = original.replace(OLD, NEW, 1)
    current = (source / THEME).read_text(encoding="utf-8")
    if current not in {original, patched}:
        raise ValueError("theme.rs has edits outside the reviewed CJK patch")
    patch = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), patched.splitlines(keepends=True),
        fromfile=f"a/{THEME}", tofile=f"b/{THEME}",
    )).encode("utf-8")
    return current, patched, patch


def checked_input(path, url, expected, *, download):
    if path.exists():
        if path.is_symlink() or not path.is_file() or sha256(path.read_bytes()) != expected:
            raise ValueError(f"existing build input is not the pinned file: {path}")
        return
    if not download:
        raise ValueError(f"missing pinned build input: {path}; run prepare first")
    request = urllib.request.Request(url, headers={"User-Agent": "Rynmesh-CJK-builder/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_FILE + 1)
    if len(data) > MAX_FILE or sha256(data) != expected:
        raise ValueError(f"download checksum mismatch: {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(data)


def inputs(font_dir, *, download):
    checked_input(font_dir / FONT_NAME, FONT_URL, FONT_SHA256, download=download)
    checked_input(font_dir / "OFL.txt", LICENSE_URL, LICENSE_SHA256, download=download)


def prepare(source, font_dir):
    current, patched, _ = source_patch(source)
    inputs(font_dir, download=True)
    if current != patched:
        with (source / THEME).open("w", encoding="utf-8", newline="\n") as output:
            output.write(patched)


def tool_versions(bindgen="wasm-bindgen"):
    result = {}
    for tool in ["rustc", "cargo", "wasm-bindgen"]:
        try:
            result[tool] = run([bindgen if tool == "wasm-bindgen" else tool, "--version"])
        except (OSError, subprocess.CalledProcessError):
            result[tool] = "unavailable"
    return result


def publish(path, data):
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"refusing to replace a different artifact: {path}")
        return
    with path.open("xb") as output:
        output.write(data)


def web_shell(base_zip):
    checked_input(base_zip, BASE_WEB_URL, BASE_WEB_SHA256, download=True)
    with zipfile.ZipFile(base_zip) as archive:
        entries = [name for name in archive.namelist() if Path(name).name == "index.html"]
        if len(entries) != 1:
            raise ValueError("pinned upstream archive has an unexpected HTML entry")
        entry = Path(entries[0])
        html = archive.read(entry.as_posix()).decode("utf-8")
        if html.count(BASE_BINDINGS_NAME) != 4:
            raise ValueError("pinned upstream bootstrap does not match the reviewed shell")
        html = html.replace(BASE_BINDINGS_NAME, "gridcraft_web")
        # Keep Trunk's generated bootstrap verbatim, replacing only asset references.
        def update_link(match):
            tag = match.group(0)
            if "gridcraft_web" in tag:
                return re.sub(r'\s+integrity="[^"]+"', "", tag)
            return tag
        html = re.sub(r"<link\b[^>]*>", update_link, html)
        icon = re.search(r'<link rel="icon" href="\./([^"/]+)"', html)
        if icon is None:
            raise ValueError("pinned upstream favicon does not match the reviewed shell")
        return html.encode("utf-8"), {icon.group(1): archive.read((entry.parent / icon.group(1)).as_posix())}


def assemble(source, font_dir, wasm, dist, bindgen, base_zip):
    current, patched, _ = source_patch(source)
    if current != patched:
        raise ValueError("source is not prepared with the reviewed CJK patch")
    inputs(font_dir, download=False)
    version = run([bindgen, "--version"])
    if version != f"wasm-bindgen {WASM_BINDGEN_VERSION}":
        raise ValueError(f"requires wasm-bindgen-cli {WASM_BINDGEN_VERSION}, found {version}")
    if dist.exists() and any(dist.iterdir()):
        raise ValueError(f"assembly requires an empty output distribution: {dist}")
    dist.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        bindgen, str(wasm), "--target", "web", "--no-typescript",
        "--out-dir", str(dist), "--out-name", "gridcraft_web",
    ], check=True)
    html, assets = web_shell(base_zip)
    publish(dist / "index.html", html)
    for name, data in assets.items():
        publish(dist / name, data)


def package(source, font_dir, dist, output_dir, bindgen="wasm-bindgen"):
    current, patched, patch = source_patch(source)
    if current != patched:
        raise ValueError("source is not prepared with the reviewed CJK patch")
    inputs(font_dir, download=False)
    if dist.is_symlink() or not (dist / "index.html").is_file():
        raise ValueError("distribution must contain a real root index.html")
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"distribution contains a symlink: {path}")
        if path.is_file():
            name = path.relative_to(dist).as_posix()
            if any(part.startswith(".") for part in Path(name).parts):
                raise ValueError(f"distribution contains hidden build files: {name}")
            if path.stat().st_size > MAX_FILE:
                raise ValueError(f"distribution file is too large: {name}")
            files[name] = path.read_bytes()
    if sum(Path(name).name == "index.html" for name in files) != 1:
        raise ValueError("compatibility archive must contain exactly one index.html")
    font_bytes = (font_dir / FONT_NAME).read_bytes()
    wasm = [data for name, data in files.items() if name.endswith(".wasm")]
    if not wasm or not any(font_bytes in data for data in wasm):
        raise ValueError("distribution WASM does not embed the exact pinned CJK font")
    for name in ["LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md"]:
        if name in files:
            raise ValueError(f"distribution unexpectedly already contains packaging file {name}")
        files[name] = (source / name).read_bytes()
    files["licenses/NotoSansCJKsc-OFL.txt"] = (font_dir / "OFL.txt").read_bytes()
    files["NOTICE"] += (
        "\nRyn CJK web variant: the WASM embeds the unmodified Noto Sans CJK SC Regular "
        f"font from notofonts/noto-cjk commit {FONT_COMMIT}.\n"
        "\u00a9 2014-2021 Adobe (http://www.adobe.com/).\n"
        "Licensed under SIL Open Font License 1.1; see licenses/NotoSansCJKsc-OFL.txt.\n"
        "Upstream application notices and license terms remain above.\n"
    ).encode("utf-8")
    files["ATTRIBUTION.md"] += (
        "\n## Ryn CJK web build input\n\n"
        f"Noto Sans CJK SC Regular | Adobe and Noto contributors | {FONT_URL} | "
        f"OFL-1.1 | SHA-256 {FONT_SHA256}.\n"
    ).encode("utf-8")
    files["SOURCE-PATCH.diff"] = patch
    provenance = {
        "schema": 1, "build_id": BUILD_ID, "id": "ryn-cjk-1",
        "base_version": "v0.6.1", "base_web_sha256": BASE_WEB_SHA256,
        "upstream": {"repository": UPSTREAM_URL, "tag": "v0.6.1", "commit": UPSTREAM_COMMIT},
        "patch_sha256": sha256(patch), "cargo_lock_sha256": sha256((source / "Cargo.lock").read_bytes()),
        "font": {"family": "Noto Sans CJK SC", "style": "Regular", "source": FONT_URL,
                 "sha256": FONT_SHA256, "license": "OFL-1.1", "license_source": LICENSE_URL,
                 "license_sha256": LICENSE_SHA256},
        "target": "wasm32-unknown-unknown", "wasm_bindgen_version": WASM_BINDGEN_VERSION,
        "toolchain": tool_versions(bindgen),
        "files": {name: sha256(data) for name, data in sorted(files.items())},
        "scope": "WASM font fallback only; no workbook, formula, import or menu-language changes",
    }
    files["CJK-PROVENANCE.json"] = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode("utf-8")
    files["SHA256SUMS.txt"] = "".join(
        f"{sha256(data)}  {name}\n" for name, data in sorted(files.items())
    ).encode("utf-8")
    manifest = {
        "entry": BUILD_ID,
        "files": {f"{BUILD_ID}/{name}": sha256(data) for name, data in sorted(files.items())},
    }
    manifest_digest = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as buffer:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(files.items()):
                member = zipfile.ZipInfo(f"{BUILD_ID}/{name}", date_time=FIXED_ZIP_TIME)
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                member.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(member, data, compresslevel=9)
        buffer.seek(0)
        archive_bytes = buffer.read()
    archive_path = output_dir / f"{BUILD_ID}.zip"
    publish(archive_path, archive_bytes)
    publish(archive_path.with_suffix(".zip.sha256"), f"{sha256(archive_bytes)}  {archive_path.name}\n".encode("ascii"))
    return {
        "archive": str(archive_path), "id": "ryn-cjk-1", "base_version": "v0.6.1",
        "base_web_sha256": BASE_WEB_SHA256, "archive_sha256": sha256(archive_bytes),
        "files_manifest_sha256": manifest_digest, "entry": BUILD_ID,
        "size": len(archive_bytes), "files": len(files),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "build", "assemble", "package"])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--font-dir", required=True, type=Path)
    parser.add_argument("--dist", type=Path, help="Distribution folder; defaults to SOURCE/dist/web")
    parser.add_argument("--output", type=Path, help="Output folder; required for build/package")
    parser.add_argument("--wasm", type=Path, help="Existing cargo WASM; required for assemble")
    parser.add_argument("--wasm-bindgen", default="wasm-bindgen", help="Path to pinned wasm-bindgen executable")
    parser.add_argument("--base-web-zip", type=Path, help="Pinned upstream web archive; downloaded if missing")
    args = parser.parse_args()
    source, font_dir = args.source.resolve(), args.font_dir.resolve()
    if font_dir.is_relative_to(source):
        parser.error("--font-dir must be outside the upstream checkout")
    try:
        if args.command in {"prepare", "build"}:
            prepare(source, font_dir)
        if args.command == "prepare":
            print(json.dumps({"source": str(source), "CRAFT_FONTS_DIR": font_dir.as_posix(), "state": "prepared"}))
            return
        dist = (args.dist or source / "dist/web").resolve()
        base_zip = (args.base_web_zip or source.parent / "gridcraft-web-0.6.1.zip").resolve()
        if args.command == "assemble":
            if not args.wasm:
                parser.error("--wasm is required for assemble")
            assemble(source, font_dir, args.wasm.resolve(), dist, args.wasm_bindgen, base_zip)
            print(json.dumps({"dist": str(dist), "state": "assembled"}))
            return
        if not args.output:
            parser.error("--output is required for build/package")
        if args.command == "build":
            bindgen = run([args.wasm_bindgen, "--version"])
            if not re.fullmatch(rf"wasm-bindgen {re.escape(WASM_BINDGEN_VERSION)}", bindgen):
                raise ValueError(f"requires wasm-bindgen-cli {WASM_BINDGEN_VERSION}, found {bindgen}")
            env = {**os.environ, "CRAFT_FONTS_DIR": font_dir.as_posix()}
            subprocess.run([
                "cargo", "build", "--release", "--locked", "--target", "wasm32-unknown-unknown", "--package", "gridcraft-web",
            ], cwd=source, env=env, check=True)
            target = Path(env.get("CARGO_TARGET_DIR", source / "target"))
            if not target.is_absolute():
                target = source / target
            assemble(source, font_dir, target / "wasm32-unknown-unknown/release/gridcraft-web.wasm", dist, args.wasm_bindgen, base_zip)
        result = package(source, font_dir, dist, args.output.resolve(), args.wasm_bindgen)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"GridCraft CJK: {error}\n")


if __name__ == "__main__":
    main()
