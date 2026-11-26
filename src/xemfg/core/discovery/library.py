"""Merge all discovery sources and track which games are new since the last scan."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..fsutil import read_json, write_json
from ..models import Game
from . import faugus, roots, steam


@dataclass
class Library:
    games: list[Game]
    new_keys: set[str] = field(default_factory=set)
    removed: list[dict] = field(default_factory=list)  # games in the last snapshot but gone now
    steam: steam.SteamInstall | None = None
    faugus: faugus.FaugusInstall | None = None
    errors: list[str] = field(default_factory=list)

    def by_key(self, key: str) -> Game | None:
        return next((g for g in self.games if g.key == key), None)

    @property
    def new_games(self) -> list[Game]:
        return [g for g in self.games if g.key in self.new_keys]


def _exe_identity(g: Game) -> str:
    try:
        return str((g.exe or g.install_dir).resolve())
    except OSError:
        return str(g.exe or g.install_dir)


def discover_all(locations, config, walk_prefixes: bool = True) -> Library:
    games: list[Game] = []
    errors: list[str] = []
    st = steam.find_steam(locations.steam_roots)
    fa = faugus.find_faugus(locations.faugus_data, locations.faugus_config)
    if st:
        try:
            games += steam.discover(st, config.exe_overrides)
        except Exception as e:  # keep going with the other launchers
            errors.append(f"Steam: {e}")
    if fa:
        try:
            games += faugus.discover(fa, config.exe_overrides, walk_prefixes)
        except Exception as e:
            errors.append(f"Faugus: {e}")
    games += roots.discover(config.roots, config.exe_overrides)
    # De-duplicate: prefix-walk and add-root finds yield to a launcher entry for the same exe.
    # A Steam game and a Faugus entry for the same exe both stay (each has its own launch options).
    launcher_exes = {_exe_identity(g) for g in games if g.source.value in ("steam", "faugus") and g.exe}
    seen: set[str] = set()
    out = []
    for g in games:
        ident = _exe_identity(g)
        if g.source.value not in ("steam", "faugus"):
            if ident in launcher_exes or ident in seen:
                continue
            seen.add(ident)
        out.append(g)
    out.sort(key=lambda g: g.title.lower())
    return Library(out, steam=st, faugus=fa, errors=errors)


def diff_snapshot(lib: Library, snapshot: Path, update: bool = True) -> None:
    """Fill lib.new_keys / lib.removed by comparing with the previous scan's snapshot.

    The snapshot is xemfg's own cache file; no game or launcher file is touched.
    The very first scan marks nothing as new.
    """
    prev = read_json(snapshot, None)
    current = {g.key: {"title": g.title, "exe": str(g.exe) if g.exe else None} for g in lib.games}
    if prev is not None:
        old = prev.get("games", {})
        lib.new_keys = {k for k in current if k not in old}
        lib.removed = [dict(v, key=k) for k, v in old.items() if k not in current]
    if update:
        write_json(snapshot, {"games": current})
