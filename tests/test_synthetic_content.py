"""Tests for the synthetic content generators.

Each must produce real, valid bytes for its declared format, be
deterministic per seed, and vary across seeds.
"""
from __future__ import annotations

import io
import wave

from rynmesh.synthetic_content import audio_bytes, document_bytes, image_bytes


def test_image_bytes_is_a_valid_png() -> None:
    b = image_bytes("seed-img-1", width=64, height=48)
    assert b.startswith(b"\x89PNG\r\n\x1a\n")
    width = int.from_bytes(b[16:20], "big")
    height = int.from_bytes(b[20:24], "big")
    assert (width, height) == (64, 48)
    assert b.endswith(b"IEND\xaeB`\x82")


def test_image_bytes_is_deterministic_but_seed_dependent() -> None:
    assert image_bytes("alpha") == image_bytes("alpha")
    assert image_bytes("alpha") != image_bytes("beta")


def test_audio_bytes_is_a_valid_wav() -> None:
    b = audio_bytes("seed-wav-1", duration_s=0.25)
    assert b.startswith(b"RIFF")
    assert b[8:12] == b"WAVE"
    with wave.open(io.BytesIO(b), "rb") as r:
        assert r.getnchannels() == 1
        assert r.getsampwidth() == 2
        assert r.getframerate() in (8000, 4000, 48000)  # default 8000
        assert r.getnframes() > 0


def test_audio_bytes_is_deterministic_and_varies() -> None:
    assert audio_bytes("x", duration_s=0.2) == audio_bytes("x", duration_s=0.2)
    assert audio_bytes("x", duration_s=0.2) != audio_bytes("y", duration_s=0.2)


def test_document_bytes_is_markdown_and_seed_visible() -> None:
    b = document_bytes("the-seed", paragraphs=2)
    text = b.decode("utf-8")
    assert text.startswith("# ")
    assert "the-seed" in text


def test_document_paragraph_count_increases_size() -> None:
    short = document_bytes("doc", paragraphs=1)
    long = document_bytes("doc", paragraphs=10)
    assert len(long) > len(short)
