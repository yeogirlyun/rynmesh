"""Set Ryn's version everywhere a release build reads it.

    python scripts/set_release_version.py 0.9.0          # write
    python scripts/set_release_version.py 0.9.0 --check  # verify only

The desktop update manifest, the Windows/macOS bundles and the Python package must all
carry the same version; the signed-update workflow refuses a tag that does not match.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STABLE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")


def _replace_once(text, pattern, value, name):
    result, count = re.subn(pattern, lambda m: m.group(1) + value + m.group(2), text, count=1, flags=re.M)
    if count != 1:
        raise ValueError(f"version not found in {name}")
    return result


def _text_files(version):
    return {
        "pyproject.toml": lambda t: _replace_once(t, r'^(version = ")[^"]+(")', version, "pyproject.toml"),
        "rynmesh/__init__.py": lambda t: _replace_once(t, r'^(__version__ = ")[^"]+(")', version, "rynmesh/__init__.py"),
        "webapp/src-tauri/Cargo.toml": lambda t: _replace_once(t, r'\A(\[package\]\s*\nname = "app"\s*\nversion = ")[^"]+(")', version, "Cargo.toml"),
        "webapp/src-tauri/Cargo.lock": lambda t: _replace_once(t, r'^(name = "app"\r?\nversion = ")[^"]+(")', version, "Cargo.lock"),
        "website/index.html": lambda t: _replace_once(
            _replace_once(t, r'(Current release: v)[0-9.]+(</span>)', version, "website release badge"),
            r'(RynMesh · MIT · v)[0-9.]+( ·)', version, "website footer"),
    }


def _json_files():
    """Each file's version fields, in file order; edited in place to keep the formatting."""
    return {
        "webapp/src-tauri/tauri.conf.json": [("version",)],
        "webapp/package.json": [("version",)],
        "webapp/package-lock.json": [("version",), ("packages", "", "version")],
    }


def _lookup(data, keys):
    for key in keys:
        data = data[key]
    return data


def _read(path):
    raw = path.read_bytes().decode("utf-8")
    return raw, ("\r\n" if "\r\n" in raw else "\n")


def _write(path, text, newline):
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", newline).encode("utf-8"))


def apply(version, root=ROOT, check=False):
    """Returns the files whose version differs (check) or that were changed (write)."""
    if not STABLE.fullmatch(version):
        raise ValueError("stable version X.Y.Z required")
    changed = []
    for name, edit in _text_files(version).items():
        path = root / name
        raw, newline = _read(path)
        updated = edit(raw.replace("\r\n", "\n"))
        if updated != raw.replace("\r\n", "\n"):
            changed.append(name)
            if not check:
                _write(path, updated, newline)
    for name, fields in _json_files().items():
        path = root / name
        raw, newline = _read(path)
        text = raw.replace("\r\n", "\n")
        updated, count = re.subn(r'("version": ")[^"]*(")', lambda m: m.group(1) + version + m.group(2), text, count=len(fields))
        data = json.loads(updated)
        if count != len(fields) or any(_lookup(data, keys) != version for keys in fields):
            raise ValueError(f"version fields not found in {name}")
        if updated != text:
            changed.append(name)
            if not check:
                _write(path, updated, newline)
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version")
    parser.add_argument("--check", action="store_true", help="only report files that differ")
    args = parser.parse_args(argv)
    changed = apply(args.version, check=args.check)
    if args.check:
        if changed:
            print("version differs in: " + ", ".join(changed))
            return 1
        print(f"all files at {args.version}")
        return 0
    print("updated: " + (", ".join(changed) or "nothing (already at this version)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
