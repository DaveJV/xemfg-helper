"""Minimal but valid PE32+ DLLs with a version resource, for DLL version detection tests."""
from __future__ import annotations

import struct


def _version_info(version: str) -> bytes:
    a, b, c, d = (int(x) for x in version.split("."))
    key = "VS_VERSION_INFO\0".encode("utf-16-le")
    head_len = 6 + len(key)
    pad = (4 - head_len % 4) % 4
    fixed = struct.pack("<13I", 0xFEEF04BD, 0x10000, (a << 16) | b, (c << 16) | d, (a << 16) | b, (c << 16) | d,
                        0x3F, 0, 0x40004, 2, 0, 0, 0)
    total = head_len + pad + len(fixed)
    return struct.pack("<HHH", total, len(fixed), 0) + key + b"\0" * pad + fixed


def make_dll(version: str | None = None, payload: bytes = b"") -> bytes:
    """A DLL whose FileVersion is `version` (or no version resource), with `payload` appended."""
    falign, salign, rva = 0x200, 0x1000, 0x1000
    rsrc = b""
    if version:
        vi = _version_info(version)
        # root -> type 16 (RT_VERSION) -> id 1 -> lang 0x409 -> data entry -> data
        d1, d2, de, data = 24, 48, 72, 88  # offsets after the 24-byte root directory
        def directory(entry_id, target, subdir=True):
            return struct.pack("<IIHHHH", 0, 0, 0, 0, 0, 1) + struct.pack(
                "<II", entry_id, target | (0x80000000 if subdir else 0))
        rsrc = (directory(16, d1) + directory(1, d2) + directory(0x409, de, subdir=False)
                + struct.pack("<IIII", rva + data, len(vi), 0, 0) + vi)
    raw = rsrc + b"\0" * ((falign - len(rsrc) % falign) % falign) if rsrc else b""
    vsize = max(len(rsrc), 1)
    size_of_image = rva + ((vsize + salign - 1) // salign) * salign

    dos = bytearray(0x40)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 240, 0x2022)
    opt = struct.pack("<HBBIIIII", 0x20B, 14, 0, 0, len(raw), 0, 0, rva)
    opt += struct.pack("<QIIHHHHHHIIIIHHQQQQII", 0x180000000, salign, falign, 6, 0, 0, 0, 6, 0, 0,
                       size_of_image, falign, 0, 2, 0x160, 0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    dirs = [(0, 0)] * 16
    if rsrc:
        dirs[2] = (rva, len(rsrc))
    opt += b"".join(struct.pack("<II", a, s) for a, s in dirs)
    sect = struct.pack("<8sIIIIIIHHI", b".rsrc", vsize, rva, len(raw), falign if raw else 0, 0, 0, 0, 0, 0x40000040)
    head = bytes(dos) + coff + opt + sect
    head += b"\0" * (falign - len(head))
    return head + raw + payload


def upscaler(version: str, models: tuple[str, ...] = ("fp8",), cyboman: str | None = None) -> bytes:
    """An amd_fidelityfx_upscaler DLL as xemfg sees it: version resource + provider class names."""
    names = {"fp8": b".?AVffxProvider_FSR4@@", "int8": b".?AVffxProvider_FSR4_Int8@@",
             "int8-rdna2": b".?AVffxProvider_FSR4_Int8_Navi2@@", "int8-lite": b".?AVffxProvider_FSR4Lite_Int8@@"}
    payload = b"\0".join([b".?AVffxProvider_FSR4_Common@@"] + [names[m] for m in models])
    if cyboman:
        payload += b"\0" + cyboman.encode() + b"\0"
    return make_dll(version, payload)
