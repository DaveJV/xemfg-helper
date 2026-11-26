"""Small filesystem helpers shared by the core."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, newline: str | None = "") -> None:
    """Write text without newline translation (newline='' keeps CRLF as-is)."""
    data = text.encode() if newline == "" else text.replace("\n", os.linesep).encode()
    atomic_write_bytes(path, data)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def write_json(path: Path, data, indent: int = 2) -> None:
    atomic_write_bytes(path, (json.dumps(data, indent=indent, ensure_ascii=False) + "\n").encode())


def file_contains(path: Path, needles: list[bytes], limit: int = 256 << 20) -> set[bytes]:
    """Return which byte needles occur in the file (ASCII and UTF-16LE), reading at most `limit`."""
    found: set[bytes] = set()
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except OSError:
        return found
    low = data.lower()
    for n in needles:
        nl = n.lower()
        if nl in low or nl.decode().encode("utf-16-le") in low:
            found.add(n)
    return found
