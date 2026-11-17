"""Shared strict text decoding and bounded OOXML text extraction."""
from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree as ET

def _control_char_ratio(text: str, *, include_pua: bool = False) -> float:
    if not text:
        return 0.0
    suspicious = 0
    for ch in text:
        cp = ord(ch)
        if (cp < 32 and ch not in ("\t", "\n", "\r", "\f", "\b", "\x1b")) or cp == 0x7F or (0x80 <= cp <= 0x9F):
            suspicious += 1
        elif include_pua and ((0xE000 <= cp <= 0xF8FF) or cp >= 0xF0000):
            suspicious += 1
    return suspicious / len(text)


def decode_text(raw: bytes) -> tuple[str, str]:
    """Decode text bytes: UTF-8 BOM -> UTF-16 BOM -> strict UTF-8 -> gb18030 -> UnicodeDecodeError."""
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise TypeError("decode_text requires a bytes-like object")
    data = bytes(raw)
    if not data:
        return "", "utf-8"
    if data.startswith(b"\xef\xbb\xbf"):
        text = data[3:].decode("utf-8", errors="strict")
        if "\x00" in text or "\ufffd" in text or _control_char_ratio(text) > 0.05:
            raise UnicodeDecodeError("utf-8-sig", data, 0, len(data), "binary control bytes")
        return text, "utf-8-sig"
    if data.startswith(b"\xff\xfe") and not data.startswith(b"\xff\xfe\x00\x00"):
        text = data[2:].decode("utf-16-le", errors="strict")
        if "\x00" in text or "\ufffd" in text or _control_char_ratio(text) > 0.02:
            raise UnicodeDecodeError("utf-16-le", data, 0, len(data), "binary control bytes")
        return text, "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        text = data[2:].decode("utf-16-be", errors="strict")
        if "\x00" in text or "\ufffd" in text or _control_char_ratio(text) > 0.02:
            raise UnicodeDecodeError("utf-16-be", data, 0, len(data), "binary control bytes")
        return text, "utf-16-be"
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        pass
    else:
        if "\x00" in text or "\ufffd" in text or _control_char_ratio(text) > 0.05:
            raise UnicodeDecodeError("utf-8", data, 0, len(data), "binary control bytes")
        return text, "utf-8"
    text = data.decode("gb18030", errors="strict")
    if "\x00" in text or "\ufffd" in text or _control_char_ratio(text, include_pua=True) > 0.02:
        raise UnicodeDecodeError("gb18030", data, 0, len(data), "binary or excessive control/PUA characters")
    return text, "gb18030"


class OfficeLimitError(ValueError):
    """The package exceeds the extraction budget."""


def read_office(data: bytes, max_chars: int) -> str:
    """Extract inert OOXML text without opening links or evaluating formulas.

    Return at most max_chars + 1 so the caller can report truncation.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2000 or sum(item.file_size for item in entries) > len(data) * 4:
            raise OfficeLimitError()
        names = set(archive.namelist())

        def xml(name):
            raw = archive.read(name)
            if re.search(br"<!\s*(?:DOCTYPE|ENTITY)", raw.replace(b"\x00", b""), re.I):
                raise ValueError("Invalid Office XML")
            return ET.fromstring(raw)

        def order(name):
            return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]

        def pieces():
            w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
            s = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            if "word/document.xml" in names:
                for paragraph in xml("word/document.xml").iter(w + "p"):
                    yield "".join(node.text or "" for node in paragraph.iter(w + "t")) + "\n"
            elif any(name.startswith("xl/worksheets/") for name in names):
                strings = []
                if "xl/sharedStrings.xml" in names:
                    strings = ["".join(node.text or "" for node in item.iter(s + "t")) for item in xml("xl/sharedStrings.xml").iter(s + "si")]
                for name in sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)), key=order):
                    for row in xml(name).iter(s + "row"):
                        for cell in row.iter(s + "c"):
                            value = cell.findtext(s + "v", "")
                            if cell.get("t") == "s":
                                value = strings[int(value)]
                            elif cell.get("t") == "inlineStr":
                                value = "".join(node.text or "" for node in cell.iter(s + "t"))
                            yield value + "\t"
                        yield "\n"
            else:
                slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=order)
                if not slides:
                    raise ValueError("Invalid Office package")
                for name in slides:
                    for paragraph in xml(name).iter(a + "p"):
                        yield "".join(node.text or "" for node in paragraph.iter(a + "t")) + "\n"

        parts, remaining = [], max_chars + 1
        for piece in pieces():
            parts.append(piece[:remaining])
            remaining -= len(parts[-1])
            if not remaining:
                break
        return "".join(parts)

