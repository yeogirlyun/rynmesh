"""Bounded inspection of local GGUF files; llama.cpp performs final load validation."""
import math
import struct
from pathlib import Path

MAX_MODEL_BYTES = 64 * 1024**3
ARCHITECTURES = {"llama", "qwen2", "qwen2moe", "qwen3", "qwen3moe", "qwen35", "qwen35moe",
                 "gemma", "gemma2", "gemma3", "phi2", "phi3"}
# Common GGML storage types: (elements per block, bytes per block).
QUANTS = {0: (1, 4), 1: (1, 2), 2: (32, 18), 3: (32, 20), 6: (32, 22),
          7: (32, 24), 8: (32, 34), 9: (32, 36), 10: (256, 84), 11: (256, 110),
          12: (256, 144), 13: (256, 176), 14: (256, 210), 15: (256, 292),
          24: (1, 1), 25: (1, 2), 26: (1, 4), 27: (1, 8), 28: (1, 8), 30: (1, 2)}


class NativeModelError(ValueError):
    pass


def inspect_model(path: Path, cancel=lambda: False) -> dict:
    if path.suffix.lower() != ".gguf" or not path.is_file():
        raise NativeModelError("unsupported_file")
    size = path.stat().st_size
    if size > MAX_MODEL_BYTES:
        raise NativeModelError("model_too_large")
    with path.open("rb") as stream:
        def read(n):
            if cancel():
                raise NativeModelError("cancelled")
            if n < 0 or stream.tell() + n > min(size, 128 * 1024**2):
                raise NativeModelError("invalid_gguf")
            data = stream.read(n)
            if len(data) != n:
                raise NativeModelError("invalid_gguf")
            return data

        def number(fmt):
            return struct.unpack("<" + fmt, read(struct.calcsize("<" + fmt)))[0]

        def string(keep=True, limit=16 * 1024**2):
            length = number("Q")
            if length > limit:
                raise NativeModelError("invalid_gguf")
            data = read(length)
            return data.decode("utf-8") if keep else None

        formats = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}

        def value(kind, keep=False, array=False):
            if kind in formats:
                return number(formats[kind])
            if kind == 8:
                return string(keep)
            if kind == 9 and not array:
                child, count = number("I"), number("Q")
                if count > 2_000_000 or child == 9:
                    raise NativeModelError("invalid_gguf")
                if child in formats:
                    read(count * struct.calcsize("<" + formats[child]))
                else:
                    for _ in range(count):
                        value(child, array=True)
                return None
            raise NativeModelError("invalid_gguf")

        try:
            if read(4) != b"GGUF" or number("I") != 3:
                raise NativeModelError("invalid_gguf")
            tensors, entries = number("Q"), number("Q")
            if not 0 < tensors <= 100_000 or not 0 < entries <= 4096:
                raise NativeModelError("invalid_gguf")
            metadata = {}
            for _ in range(entries):
                key = string(limit=1024)
                if key in metadata:
                    raise NativeModelError("invalid_gguf")
                metadata[key] = value(number("I"), key.startswith("general.") or key.endswith(".context_length"))
            arch = metadata.get("general.architecture")
            if arch not in ARCHITECTURES or metadata.get("general.type", "model") != "model":
                raise NativeModelError("unsupported_model")
            if metadata.get("split.count", 1) != 1:
                raise NativeModelError("split_model_unsupported")
            alignment = metadata.get("general.alignment", 32)
            if type(alignment) is not int or alignment < 1 or alignment > 4096 or alignment & (alignment - 1):
                raise NativeModelError("invalid_gguf")
            ranges = []
            for _ in range(tensors):
                string(keep=False, limit=1024)
                nd = number("I")
                if not 1 <= nd <= 4:
                    raise NativeModelError("invalid_gguf")
                dimensions = [number("Q") for _ in range(nd)]
                quant, offset = number("I"), number("Q")
                if quant not in QUANTS:
                    raise NativeModelError("unsupported_quantization")
                block, length = QUANTS[quant]
                elements = math.prod(dimensions)
                if not all(dimensions) or dimensions[0] % block or offset % alignment:
                    raise NativeModelError("invalid_gguf")
                ranges.append((offset, offset + elements // block * length))
            start = (stream.tell() + alignment - 1) // alignment * alignment
            previous = 0
            for begin, end in sorted(ranges):
                if begin < previous or end + start > size:
                    raise NativeModelError("invalid_gguf")
                previous = end
            context = metadata.get(f"{arch}.context_length", 4096)
            if type(context) is not int or context < 256:
                raise NativeModelError("unsupported_model")
            name = metadata.get("general.name") or path.stem
            license_id = metadata.get("general.license") or "unknown"
            if not isinstance(name, str) or not isinstance(license_id, str):
                raise NativeModelError("invalid_gguf")
            return {"name": name[:160], "file_name": path.name, "size_bytes": size,
                    "architecture": arch, "context_window": min(context, 4096), "license_id": license_id[:120]}
        except (UnicodeError, struct.error, TypeError) as exc:
            raise NativeModelError("invalid_gguf") from exc
