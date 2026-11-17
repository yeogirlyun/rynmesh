"""Build a deterministic independent plugin archive and sign the public catalog.

Signing keys and output archives must stay outside the source repository.
Run from a clean develop checkout, after committing the plugin sources.
"""
import argparse
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

from rynmesh.crypto import sign_payload
from rynmesh.public_plugins.packages import verify_catalog


def build(package, *, revision, key):
    package = Path(package)
    manifest = json.loads((package / "plugin.json").read_text(encoding="utf-8"))
    paths = sorted(p for p in package.rglob("*") if p.is_file()
                   and "__pycache__" not in p.parts and p.suffix != ".pyc"
                   and p.suffix in {".py", ".json", ".html", ".js", ".css", ".svg", ".md"})
    data = io.BytesIO()
    files = {}
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for p in paths:
            name, content = p.relative_to(package).as_posix(), p.read_bytes().replace(b"\r\n", b"\n")
            files[name] = hashlib.sha256(content).hexdigest()
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, content)
    raw = data.getvalue()
    tag = f"plugin-{manifest['id']}-v{manifest['version']}"
    filename = f"{manifest['id']}-{manifest['version']}.zip"
    row = {**manifest, "revision": revision, "files": files, "size": len(raw),
           "sha256": hashlib.sha256(raw).hexdigest(),
           "url": f"https://github.com/yeogirlyun/rynmesh/releases/download/{tag}/{filename}"}
    signed = sign_payload({"kind": "ryn.public-plugin-catalog", "schema": 1, "plugins": [row]}, private_key_bytes=key).to_dict()
    verify_catalog(signed, signed["public_key"])
    return filename, raw, signed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="plugins/image-studio")
    parser.add_argument("--key", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()).resolve()
    package = Path(args.package).resolve()
    key = Path(args.key).resolve()
    output = Path(args.output).resolve()
    if key.is_relative_to(root) or output.is_relative_to(root):
        parser.error("Signing key and build output must be outside the repository")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    integrated = subprocess.check_output(["git", "rev-parse", "develop"], text=True).strip()
    if revision != integrated:
        parser.error("Build releases from the current integrated develop commit")
    if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        parser.error("Commit all tracked changes before building")
    if subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "--", str(package)], text=True).strip():
        parser.error("Do not publish untracked package files")
    name, raw, catalog = build(package, revision=revision, key=key.read_bytes())
    source = json.loads((root / "rynmesh/public_plugins/source.json").read_text(encoding="utf-8"))
    verify_catalog(catalog, source["public_key"])
    existing = root / "plugins/catalog.json"
    if existing.exists():
        previous = json.loads(existing.read_text(encoding="utf-8"))
        rows = verify_catalog(previous, source["public_key"])
        new = catalog["payload"]["plugins"][0]
        rows = [r for r in rows if r["id"] != new["id"]] + [new]
        catalog = sign_payload({"kind": "ryn.public-plugin-catalog", "schema": 1, "plugins": rows}, private_key_bytes=key.read_bytes()).to_dict()
    output.mkdir(parents=True, exist_ok=True)
    (output / name).write_bytes(raw)
    (output / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"archive": str(output / name), "revision": revision, "sha256": hashlib.sha256(raw).hexdigest()}))


if __name__ == "__main__":
    main()
