"""Every FSR4 DLL xemfg has put into a game, kept for going back: ~/.local/share/xemfg/fsr4/<sha256>/.

Content-addressed and outside the download cache, so `gc` and a cleared ~/.cache never remove a DLL a game
may want back. A file is only used again after its SHA-256 matches its folder name.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .fsutil import read_json, sha256_or_none, write_json


@dataclass
class StoredDll:
    sha256: str
    path: Path
    meta: dict


class Store:
    def __init__(self, root: Path):
        self.root = root

    def find(self, sha256: str | None) -> Path | None:
        if not sha256:
            return None
        d = self.root / sha256
        for p in sorted(d.glob("*.dll")) if d.is_dir() else []:
            if sha256_or_none(p) == sha256:
                return p
        return None

    def put(self, path: Path, meta: dict) -> Path:
        """Keep a copy of `path` (verified) with what it is; returns the stored file."""
        sha = sha256_or_none(path)
        if sha is None:
            raise FileNotFoundError(path)
        have = self.find(sha)
        d = self.root / sha
        if have is None:
            d.mkdir(parents=True, exist_ok=True)
            dest = d / path.name
            tmp = d / (path.name + ".part")
            shutil.copy2(path, tmp)
            os.replace(tmp, dest)
            if sha256_or_none(dest) != sha:
                dest.unlink(missing_ok=True)
                raise OSError(f"copy of {path} into the DLL store does not match its hash")
            have = dest
        info = read_json(d / "meta.json", {}) or {}
        used = sorted(set(info.get("games", [])) | set(meta.pop("games", [])))
        write_json(d / "meta.json", {**info, **{k: v for k, v in meta.items() if v is not None}, "games": used})
        return have

    def entries(self) -> list[StoredDll]:
        out = []
        for d in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            p = self.find(d.name)
            if p is not None:
                out.append(StoredDll(d.name, p, read_json(d / "meta.json", {}) or {}))
        return out
