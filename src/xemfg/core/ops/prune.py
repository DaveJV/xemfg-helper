"""prune: drop registry entries whose folders are gone, after trying to find where they moved."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..context import Context
from ..fsutil import sha256_or_none
from ..registry import Entry
from ..state import GameState


@dataclass
class PruneItem:
    entry: Entry
    relocated_to: Path | None  # new exe path
    how: str = ""


def find_relocation(ctx: Context, e: Entry) -> tuple[Path | None, str]:
    lib = ctx.library()
    g = lib.by_key(e.key)
    if g and g.exe and g.exe.exists() and str(g.exe) != e.exe:
        how = "same Steam appid in another library" if e.appid else "Faugus entry now points here"
        return g.exe, how
    # same exe hash anywhere in the scanned library
    if e.exe_sha256:
        name = Path(e.exe).name.lower()
        for g in lib.games:
            if g.exe and g.exe.name.lower() == name and str(g.exe) != e.exe and sha256_or_none(g.exe) == e.exe_sha256:
                return g.exe, f"same exe (sha256) found via {g.short()}"
    return None, ""


def plan_prune(ctx: Context) -> list[PruneItem]:
    out = []
    for e in ctx.registry.entries.values():
        if Path(e.exe_dir).is_dir():
            continue
        if e.status == "removed":
            out.append(PruneItem(e, None, "removed and folder gone"))
            continue
        new, how = find_relocation(ctx, e)
        out.append(PruneItem(e, new, how))
    return out


def repoint(ctx: Context, item: PruneItem) -> None:
    e, new = item.entry, item.relocated_to
    e.exe, e.exe_dir = str(new), str(new.parent)
    g = ctx.library().by_key(e.key)
    if g is None or g.exe != new:
        g = next((x for x in ctx.library().games if x.exe == new), None)
    if g is not None and g.key != e.key:
        del ctx.registry.entries[e.key]
        e.key = g.key
        e.appid, e.gameid, e.source, e.title = g.appid, g.gameid, g.source.value, g.title
        ctx.registry.entries[e.key] = e
    st = GameState.load(new.parent)
    if st and st.game_key != e.key:
        st.game_key = e.key
        st.save(new.parent)
    ctx.registry.save()


def drop(ctx: Context, item: PruneItem) -> None:
    ctx.registry.entries.pop(item.entry.key, None)
    ctx.registry.save()
