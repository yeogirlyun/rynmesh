"""Package a DeckCraft WASM build using the verified upstream Trunk shell."""
import argparse
import base64
import hashlib
import io
import zipfile
from pathlib import Path

from craft_gridcraft_cjk import publish, sha256

BASE = "dd76659f643fa98ff79c86bf439bcaeeaaf6bd3a4037a1dc6628c97366862ba1"
FONT = "4fcc5aec11cbbf737b0cfab7b63796f7f280087a7a656b2f13342cd5e5318d95"
LICENSE = "4fff27d35db0e22cd81d58da6f20e09f415cf354a3338e3cf1fc0eb9222c7174"
PREFIX = "deckcraft-web-0.6.1/"
BINDINGS = "deckcraft-web-33aef20310d74aba"


def checked(path, digest):
    data = path.read_bytes()
    if sha256(data) != digest:
        raise ValueError(f"Unexpected SHA-256 for {path}")
    return data


def integrity(data):
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode()


def package(base, dist, font, license_file, output):
    checked(base, BASE)
    with zipfile.ZipFile(base) as archive:
        files = {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}
    wasm = (dist / "deckcraft_web_bg.wasm").read_bytes()
    if checked(font, FONT) not in wasm:
        raise ValueError("Missing pinned WenKai font in DeckCraft WASM")
    html_key = PREFIX + "index.html"
    html = files[html_key].decode()
    # Change both URLs to invalidate cached modules, and keep the shell's SRI checks.
    name = "deckcraft-" + sha256(wasm)[:16]
    for ext, data in [(".js", (dist / "deckcraft_web.js").read_bytes()), ("_bg.wasm", wasm)]:
        original = files.pop(PREFIX + BINDINGS + ext)
        if html.count(BINDINGS + ext) != 2 or html.count(integrity(original)) != 1:
            raise ValueError("Unexpected upstream bootstrap")
        html = html.replace(BINDINGS + ext, name + ext).replace(integrity(original), integrity(data))
        files[PREFIX + name + ext] = data
    files[html_key] = html.encode()
    files[PREFIX + "licenses/LXGWWenKaiTC-OFL.txt"] = checked(license_file, LICENSE)
    files[PREFIX + "deckcraft-cjk.patch"] = (Path(__file__).parent / "patches/deckcraft-cjk.patch").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for key, data in sorted(files.items()):
            info = zipfile.ZipInfo(key, (2026, 11, 4, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    publish(output, buffer.getvalue())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("base", "dist", "font", "license_file", "output"):
        parser.add_argument(arg, type=Path)
    package(**vars(parser.parse_args()))
