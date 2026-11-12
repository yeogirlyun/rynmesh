"""Publish an installer-provided plugin runtime as a signed update pack.

Writes ``<output>/latest.json`` (a signed ``ryn.service.release``) and
``<output>/blobs/<sha256>`` for every file of the release: the runtime, its
plugin declarations (``ryn-declarations.json``) and its skill files
(``ryn-skills/<skill id>/...``), so all three switch together. Upload the
directory so that latest.json is served at the URL the installer wrote to
``plugin-services/updates.json``; Ryn downloads only blobs it does not have.

    python scripts/publish_service_pack.py --runtime <dist/company-service> \\
        --company-source <company-app> --key <publisher.key> --output <dir> \\
        [--previous <last published latest.json>] [--min-host 0.9.0] [--notes "..."]

The version comes from the runtime's manifest and must be higher than the
previous release's. ``--generate-key <path>`` creates a publisher key (keep it
out of every repository) and prints the public key the installer needs.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rynmesh.crypto import SignedPayload, public_key_from_private, sign_payload  # noqa: E402
from rynmesh.node_apps.service_updates import (  # noqa: E402
    DECLARATIONS, RUNTIME_MANIFEST, SKILLS, parse_version, safe_path, verify_manifest)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def read_key(path):
    raw = Path(path).read_bytes().strip()
    key = raw if len(raw) == 32 else base64.b64decode(raw, validate=True)
    if len(key) != 32:
        raise ValueError("Expected a 32-byte Ed25519 private key (raw or base64)")
    return key


def generate_key(path):
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(base64.b64encode(key))
    return public_key_from_private(key)


def previous_version(path):
    """Version of the release published last (its signed latest.json)."""
    if not path:
        return None
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    return SignedPayload.from_dict(value).payload.get("version")


def publish(runtime, output, key, *, service="company", program="company-service.exe", version=None,
            declarations=None, skills_root=None, min_host=None, notes=None, previous=None):
    runtime, output = Path(runtime), Path(output)
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("Output directory must be new or empty: " + str(output))
    manifest = json.loads((runtime / RUNTIME_MANIFEST).read_text(encoding="utf-8"))
    revision = manifest["source_commit"]
    version = version or manifest.get("version")
    if parse_version(version) is None:
        raise ValueError("The runtime has no release version; rebuild it with the plugin build script")
    if previous is not None and parse_version(previous) is not None and parse_version(version) <= parse_version(previous):
        raise ValueError(f"Version {version} is not higher than the published {previous}")
    files, sources = {}, {}

    def add(path, source):
        safe_path(path)
        digest = sha(source)
        files[path] = {"sha256": digest, "size": Path(source).stat().st_size}
        sources[digest] = Path(source)

    for path, digest in manifest["files"].items():
        if sha(runtime / path) != digest:
            raise ValueError("Runtime does not match its manifest: " + path)
        if path == DECLARATIONS or path.startswith(SKILLS + "/"):
            raise ValueError("Runtime already contains release files: " + path)
        add(path, runtime / path)
    with tempfile.TemporaryDirectory() as scratch:
        scratch = Path(scratch)
        if declarations is not None:
            data = json.dumps(declarations, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            (scratch / DECLARATIONS).write_bytes(data)
            add(DECLARATIONS, scratch / DECLARATIONS)
        if skills_root is not None and Path(skills_root).is_dir():
            for source in sorted(Path(skills_root).rglob("*")):
                if source.is_symlink() or source.is_junction():
                    raise ValueError("Skill resource links are forbidden")
                if source.is_file():
                    add(SKILLS + "/" + source.relative_to(skills_root).as_posix(), source)
        blobs = output / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        for digest, source in sources.items():
            shutil.copy2(source, blobs / digest)
    payload = {"kind": "ryn.service.release", "schema": 1, "id": service, "version": version,
               "revision": revision, "program": program, "files": files}
    if declarations is not None:
        payload["declarations"] = DECLARATIONS
    if any(path.startswith(SKILLS + "/") for path in files):
        payload["skills"] = SKILLS
    if min_host:
        payload["min_host"] = min_host
    if notes:
        payload["notes"] = notes
    signed = sign_payload(payload, private_key_bytes=key).to_dict()
    # Same checks the installed Ryn applies before staging anything.
    verify_manifest(signed, {"public_key": public_key_from_private(key)}, service)
    (output / "latest.json").write_text(json.dumps(signed, indent=2), encoding="utf-8")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--generate-key", type=Path)
    parser.add_argument("--public-key", type=Path, help="print the public key of an existing publisher key")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--company-source", type=Path, help="company-app checkout; ships plugin declarations and skills")
    parser.add_argument("--declarations", type=Path, help="explicit JSON declarations file to ship with the pack")
    parser.add_argument("--skills-dir", type=Path, help="explicit skill packages directory to ship with the pack")
    parser.add_argument("--service", default="company")
    parser.add_argument("--program", default="company-service.exe")
    parser.add_argument("--previous", type=Path, help="latest.json published last; the new version must be higher")
    parser.add_argument("--min-host", help="lowest Ryn version this release runs with")
    parser.add_argument("--notes")
    args = parser.parse_args()
    if args.generate_key:
        print(generate_key(args.generate_key))
        raise SystemExit(0)
    if args.public_key:
        print(public_key_from_private(read_key(args.public_key)))
        raise SystemExit(0)
    if not (args.runtime and args.output and args.key):
        parser.error("--runtime, --output and --key are required")
    declarations = skills = None
    with tempfile.TemporaryDirectory() as scratch:
        if args.declarations:
            declarations = json.loads(args.declarations.read_text(encoding="utf-8"))
        if args.skills_dir:
            skills = args.skills_dir
        if args.company_source:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from prepare_full_package import company_declarations, write_company_skills
            declarations = company_declarations(args.company_source)
            skills = Path(scratch) / "skills"
            write_company_skills(declarations["skills"], args.company_source, skills)
        result = publish(args.runtime, args.output, read_key(args.key), service=args.service, program=args.program,
                         declarations=declarations, skills_root=skills, min_host=args.min_host, notes=args.notes,
                         previous=previous_version(args.previous))
    print(json.dumps({"version": result["version"], "revision": result["revision"], "files": len(result["files"]),
                      "bytes": sum(meta["size"] for meta in result["files"].values()),
                      "blobs": len({meta["sha256"] for meta in result["files"].values()})}))
