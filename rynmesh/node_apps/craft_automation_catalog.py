"""Pinned upstream headless engines for the four office Craft applications.

Windows portable releases contain the native CLI alongside the desktop app.
The CLI is launched only by Ryn's bounded adapter; the model does not receive
executable paths, shell access or arbitrary upstream command forwarding.

Archive SHA-256 values are the fixed release asset digests published by GitHub,
reviewed against the same tags as ``CRAFT``. They pin bytes, not a code audit.
"""

from .craft_catalog import CRAFT

_WINDOWS_X64_SHA256 = {
    "pdfcraft": "6632068a4f7fdf1fb6a3b9d431e6d088bb7cbbeb066e47f815a1e498b45d7ecf",
    "wordcraft": "4576d76363b0513242e0c1e5038fabaee4b3369909de60821900a01fc6277261",
    "gridcraft": "595604b4be6f395c02bc15f99e3e6e2babde964cb9ae17aad7824981bcc14ce0",
    "deckcraft": "339ea7b9a4901fd0bc6fe1b96fedbc34e72a3c0d0897c44ad1750262d63db84f",
}

# Independently pinned expanded payloads, not hashes trusted from a writable
# installation receipt. Entries are archive member paths -> per-file SHA-256;
# hash their UTF-8 JSON with sort_keys=True, separators=(",", ":"),
# ensure_ascii=False. Directories are omitted and paths remain POSIX-relative.
_WINDOWS_X64_PAYLOAD_MANIFEST_SHA256 = {
    "pdfcraft": "5a6b0d6f5ff1fe4787a221af49c98f03b3811fdb4b916c3ec10187fd6b1970df",
    "wordcraft": "eef424bc102f11d9a3a9e5beb3c9b27c4abc088a915d34d9231aedb9244af467",
    "gridcraft": "99e80c63d06cf80dadb1d0fad43d363e0b139d68d636927d20704560450e3bcd",
    "deckcraft": "b9a41c4b86fdd41d9457b1f4093a539e9c02893a9def422ad5e4875ec5fd8d63",
}

AUTOMATION = {
    slug: {
        "windows-x64": {
            "asset": f"{slug}-{CRAFT[slug]['tag'].removeprefix('v')}-windows-x64-portable.zip",
            "sha256": checksum,
            "payload_manifest_sha256": _WINDOWS_X64_PAYLOAD_MANIFEST_SHA256[slug],
            "executable": f"{slug}-cli.exe",
        },
    }
    for slug, checksum in _WINDOWS_X64_SHA256.items()
}
