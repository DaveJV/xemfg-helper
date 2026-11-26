"""Extra folders added with `xemfg config add-root` (games outside Steam and Faugus)."""
from __future__ import annotations

from pathlib import Path

from ..models import Game, Source
from .exefinder import find_exe


def discover(roots: list[str], exe_overrides: dict[str, str] | None = None) -> list[Game]:
    exe_overrides = exe_overrides or {}
    games = []
    for r in roots:
        root = Path(r).expanduser()
        if not root.is_dir():
            continue
        # a root is either a game folder itself or a folder of game folders
        direct = any(p.suffix.lower() == ".exe" for p in root.iterdir() if p.is_file())
        dirs = [root] if direct else sorted(p for p in root.iterdir() if p.is_dir())
        for d in dirs:
            key = f"root:{d}"
            ov = exe_overrides.get(key)
            if ov and Path(ov).exists():
                exe, cands, reason = Path(ov), [], "chosen earlier"
            else:
                res = find_exe(d)
                exe, cands, reason = res.exe, res.candidates, res.reason
            if not exe and not cands:
                continue
            games.append(Game(key=key, title=d.name, source=Source.ROOT, install_dir=d, exe=exe,
                              exe_candidates=cands, exe_reason=reason))
    return games


def game_from_path(path: Path) -> Game | None:
    """A raw path from the command line: an exe, or a folder to search."""
    path = path.expanduser().resolve()
    if path.is_file() and path.suffix.lower() == ".exe":
        return Game(key=f"root:{path.parent}", title=path.parent.name, source=Source.PATH,
                    install_dir=path.parent, exe=path, exe_reason="given on the command line")
    if path.is_dir():
        r = find_exe(path)
        return Game(key=f"root:{path}", title=path.name, source=Source.PATH, install_dir=path,
                    exe=r.exe, exe_candidates=r.candidates, exe_reason=r.reason)
    return None
