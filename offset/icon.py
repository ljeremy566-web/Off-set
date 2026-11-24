"""Zero-dependency generator for Offset's monochrome 'o.' Windows .ico asset."""
from pathlib import Path
import struct
import zlib


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def _render_icon_png(size: int) -> bytes:
    """Render a crisp rounded-square monochrome white icon with dark 'o.' emblem."""
    rows = bytearray()
    radius = size * 0.28
    cx_o, cy_o = size * 0.41, size * 0.54
    r_outer, r_inner = size * 0.22, size * 0.115
    cx_dot, cy_dot, r_dot = size * 0.72, size * 0.69, size * 0.068

    for y in range(size):
        rows.append(0)  # PNG filter type 0
        for x in range(size):
            px, py = x + 0.5, y + 0.5
            # Rounded square SDF
            dx = max(abs(px - size / 2) - (size / 2 - radius), 0.0)
            dy = max(abs(py - size / 2) - (size / 2 - radius), 0.0)
            dist_corner = (dx * dx + dy * dy) ** 0.5
            if dist_corner > radius + 0.5:
                rows.extend((0, 0, 0, 0))
                continue
            alpha = 255 if dist_corner <= radius - 0.5 else int(255 * max(0.0, min(1.0, radius + 0.5 - dist_corner)))

            # 'o' ring + '.' dot in deep obsidian (#0a0a0c) on crisp off-white (#f5f5f7)
            do = ((px - cx_o) ** 2 + (py - cy_o) ** 2) ** 0.5
            dd = ((px - cx_dot) ** 2 + (py - cy_dot) ** 2) ** 0.5
            in_ring = r_inner <= do <= r_outer
            in_dot = dd <= r_dot
            if in_ring or in_dot:
                rows.extend((10, 10, 12, alpha))
            else:
                rows.extend((245, 245, 247, alpha))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=9)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def build_ico_bytes(sizes=(16, 32, 48, 256)) -> bytes:
    """Build a standard multi-resolution Windows ICO containing PNG frames."""
    images = [(s, _render_icon_png(s)) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    payload = bytearray()
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        payload += png
        offset += len(png)
    return header + bytes(entries) + bytes(payload)


def ensure_icon_file(path: Path) -> Path:
    path = Path(path)
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(build_ico_bytes())
    return path
