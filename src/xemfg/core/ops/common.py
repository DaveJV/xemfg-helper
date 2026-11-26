"""Helpers shared by the operations."""
from __future__ import annotations

from pathlib import Path

from ..models import Game
from ..mods import KIND_LABELS, classify
from ..platform import PROXY_NAMES


class OpError(RuntimeError):
    """An operation refused to run; the message says why and what to do instead."""


class ConflictError(OpError):
    pass


def proxy_owner(exe_dir: Path, name: str, optiscaler_hashes: set[str]) -> str | None:
    """None if the proxy slot is free, else the kind of mod that holds it."""
    p = exe_dir / name
    if not p.is_file():
        return None
    return classify(p, optiscaler_hashes)


def next_free_proxy(exe_dir: Path, optiscaler_hashes: set[str], skip: str | None = None) -> str | None:
    for n in PROXY_NAMES:
        if n == skip:
            continue
        if proxy_owner(exe_dir, n, optiscaler_hashes) in (None, "optiscaler"):
            return n
    return None


def conflict_message(name: str, kind: str, suggestion: str | None) -> str:
    msg = f"{name} already belongs to {KIND_LABELS.get(kind, kind)}; xemfg never overwrites another mod's DLL."
    if suggestion:
        msg += f" Use --proxy {suggestion} instead."
    return msg


def require_exe(game: Game) -> Path:
    if game.exe is None:
        raise OpError(f"{game.title}: no game exe found ({game.exe_reason})")
    return game.exe.parent
