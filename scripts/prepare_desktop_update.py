"""Stage signed Tauri bundles, then assemble desktop-latest.json for one complete release."""
import argparse
import base64
import json
import re
import shutil
from pathlib import Path
from urllib.parse import quote

REPOSITORY = "https://github.com/yeogirlyun/rynmesh"
PLATFORMS = {"windows-x86_64", "darwin-aarch64", "darwin-x86_64"}


def version(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Expected a stable version, e.g. 0.8.0")
    return value


def stage(bundle_dir, output, release_version, platform):
    version(release_version)
    if platform not in PLATFORMS:
        raise ValueError("Unsupported platform")
    suffix = ".exe.sig" if platform.startswith("windows") else ".app.tar.gz.sig"
    signatures = list(Path(bundle_dir).rglob("*" + suffix))
    if len(signatures) != 1:
        raise ValueError("Expected exactly one signed updater bundle")
    sig_path = signatures[0]
    package = sig_path.with_suffix("")
    if not package.is_file() or not package.stat().st_size:
        raise ValueError("Missing update package")
    signature = sig_path.read_text(encoding="utf-8").strip()
    if not base64.b64decode(signature, validate=True):
        raise ValueError("Empty signature")
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    filename = f"Ryn-{release_version}-{platform}" + (".exe" if platform.startswith("windows") else ".app.tar.gz")
    shutil.copy2(package, destination / filename)
    (destination / (filename + ".sig")).write_text(signature, encoding="utf-8")
    row = {"version": release_version, "platform": platform, "url": f"{REPOSITORY}/releases/download/v{release_version}/{quote(filename)}", "signature": signature}
    (destination / (platform + ".json")).write_text(json.dumps(row, indent=2), encoding="utf-8")
    return row


def assemble(directory, release_version, notes=""):
    version(release_version)
    root = Path(directory)
    platforms = {}
    for platform in sorted(PLATFORMS):
        row = json.loads((root / (platform + ".json")).read_text(encoding="utf-8"))
        if row["version"] != release_version or row["platform"] != platform:
            raise ValueError("Mixed release versions/platforms")
        expected = f"{REPOSITORY}/releases/download/v{release_version}/"
        if not row["url"].startswith(expected) or not row["signature"]:
            raise ValueError("Invalid release artifact")
        platforms[platform] = {"url": row["url"], "signature": row["signature"]}
    result = {"version": release_version, "notes": notes, "platforms": platforms}
    (root / "desktop-latest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["stage", "assemble"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bundle-dir", type=Path)
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    parser.add_argument("--notes", default="")
    args = parser.parse_args()
    if args.command == "stage":
        if not args.bundle_dir or not args.platform:
            parser.error("stage requires --bundle-dir and --platform")
        stage(args.bundle_dir, args.output, args.version, args.platform)
    else:
        assemble(args.output, args.version, args.notes)
