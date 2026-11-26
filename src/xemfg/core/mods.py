"""Identify which mod owns a proxy DLL, by content (never by name alone)."""
from __future__ import annotations

import re
from pathlib import Path

from .fsutil import sha256_or_none

OPTISCALER_VERSION = re.compile(rb"OptiScaler v(\d+\.\d+\.\d+[\w.\-]*)(?: \(([0-9a-f]{6,})\))?")
CYBOMAN = re.compile(rb"([\w.]+-(?:exact|lossy)(?:-r\d+)?-cyboman)")

KIND_LABELS = {
    "optiscaler": "OptiScaler",
    "reshade": "ReShade",
    "asi-loader": "Ultimate ASI Loader",
    "specialk": "Special K",
    "dxvk": "DXVK",
    "mfgu-standalone": "MFG Unlocker (standalone proxy)",
    "system": "the game's own copy of a Windows DLL",
    "unknown": "unknown mod",
}


def _read(path: Path, limit: int = 128 << 20) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return b""


def _has(data: bytes, *needles: bytes) -> bool:
    for n in needles:
        if n in data or n.decode().encode("utf-16-le") in data:
            return True
    return False


def classify(path: Path, known_optiscaler_hashes: set[str] | None = None) -> str:
    """Return the kind of mod a DLL belongs to (see KIND_LABELS)."""
    if known_optiscaler_hashes and sha256_or_none(path) in known_optiscaler_hashes:
        return "optiscaler"
    data = _read(path)
    # OptiScaler embeds ReShade/SpecialK strings, so it must be checked first.
    if OPTISCALER_VERSION.search(data) and not _has(data, b"RTXMFG"):
        return "optiscaler"
    if _has(data, b"RTXMFG"):
        return "mfgu-standalone"
    if _has(data, b"Ultimate-ASI-Loader", b"IsUltimateASILoader", b"ThirteenAG"):
        return "asi-loader"
    if _has(data, b"crosire", b"ReShade.ini", b"reshade-shaders"):
        return "reshade"
    if _has(data, b"SpecialK", b"Special K", b"Kaldaien"):
        return "specialk"
    if _has(data, b"dxvk", b"DXVK_"):
        return "dxvk"
    if "Microsoft Corporation".encode("utf-16-le") in data:
        return "system"
    return "unknown"


def optiscaler_version(path: Path) -> str | None:
    m = OPTISCALER_VERSION.search(_read(path))
    if not m:
        return None
    ver = m.group(1).decode()
    return f"{ver} ({m.group(2).decode()})" if m.group(2) else ver


def cyboman_build(path: Path) -> str | None:
    m = CYBOMAN.search(_read(path))
    return m.group(1).decode() if m else None
