"""Download the official fnOS test ISO and verify the published checksum."""

import hashlib
import urllib.request
from pathlib import Path

URL = "http://thunder.liveupdate.fnnas.com:8080/x86_64/trim/fnos_Mainland-PE_x86_1.2.0604_2685.iso"
# Published on https://fnnas.com/download (x86 1.2.0604).
EXPECTED_MD5 = "203a7b7e29637963c9e23c4abd302413"
target = Path(__file__).resolve().parents[1] / "build/nas-test/fnos.iso"
target.parent.mkdir(parents=True, exist_ok=True)
partial = target.with_suffix(".partial")
if not target.exists():
    size = 0
    with urllib.request.urlopen(URL, timeout=60) as response, partial.open("wb") as output:
        while chunk := response.read(4 * 1024 * 1024):
            output.write(chunk)
            size += len(chunk)
            if size % (100 * 1024 * 1024) < len(chunk):
                print(f"Downloaded {size // (1024 * 1024)} MiB", flush=True)
candidate = target if target.exists() else partial
digest = hashlib.md5()
with candidate.open("rb") as source:
    for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != EXPECTED_MD5:
    raise RuntimeError("ISO checksum mismatch; do not boot")
if candidate == partial:
    partial.replace(target)
print("Verified fnOS ISO:", target, flush=True)
