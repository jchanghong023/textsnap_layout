"""Generate the committed-design Windows icon deterministically."""

from __future__ import annotations

import argparse
import binascii
import struct
import zlib
from pathlib import Path


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
    )


def _png(size: int) -> bytes:
    dark = (20, 26, 38, 255)
    blue = (52, 138, 255, 255)
    white = (246, 249, 255, 255)
    pixels = bytearray()
    margin = max(1, size // 8)
    stroke = max(1, size // 14)
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            colour = dark
            top_left = (margin <= x < margin + stroke and margin <= y < size // 2) or (
                margin <= y < margin + stroke and margin <= x < size // 2
            )
            bottom_right = (
                size - margin - stroke <= x < size - margin
                and size // 2 <= y < size - margin
            ) or (
                size - margin - stroke <= y < size - margin
                and size // 2 <= x < size - margin
            )
            vertical = (
                size // 2 - stroke // 2 <= x < size // 2 + (stroke + 1) // 2
                and size // 3 <= y < 2 * size // 3
            )
            horizontal = (
                size // 3 <= x < 2 * size // 3 and size // 3 <= y < size // 3 + stroke
            )
            if top_left or bottom_right:
                colour = blue
            elif vertical or horizontal:
                colour = white
            pixels.extend(colour)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(pixels), level=9))
        + _chunk(b"IEND", b"")
    )


def generate_icon(output: Path) -> None:
    images = [(size, _png(size)) for size in (16, 32, 48, 256)]
    directory_size = 6 + 16 * len(images)
    offset = directory_size
    entries = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    payload = bytearray()
    for size, image in images:
        encoded_size = 0 if size == 256 else size
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                encoded_size,
                encoded_size,
                0,
                0,
                1,
                32,
                len(image),
                offset,
            )
        )
        payload.extend(image)
        offset += len(image)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bytes(entries + payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    generate_icon(arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
