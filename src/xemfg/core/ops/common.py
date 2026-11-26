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


def proxy_slots(exe_dir: Path, optiscaler_hashes: set[str]) -> list[tuple[str, str | None]]:
    """Every name OptiScaler can be loaded under (OptiScaler's setup script's list), with who holds it:
    None = free, "optiscaler" = OptiScaler, else the other mod's kind (mods.KIND_LABELS)."""
    return [(n, proxy_owner(exe_dir, n, optiscaler_hashes)) for n in PROXY_NAMES]


def free_proxies(exe_dir: Path, optiscaler_hashes: set[str], skip: str | None = None) -> list[str]:
    return [n for n, owner in proxy_slots(exe_dir, optiscaler_hashes) if owner in (None, "optiscaler") and n != skip]


def next_free_proxy(exe_dir: Path, optiscaler_hashes: set[str], skip: str | None = None) -> str | None:
    free = free_proxies(exe_dir, optiscaler_hashes, skip)
    return free[0] if free else None


def taken_proxies(exe_dir: Path, optiscaler_hashes: set[str]) -> list[str]:
    """'dxgi.dll (ReShade)' for every name another mod holds."""
    return [f"{n} ({KIND_LABELS.get(o, o)})" for n, o in proxy_slots(exe_dir, optiscaler_hashes)
            if o not in (None, "optiscaler")]


def conflict_message(name: str, kind: str, free: list[str] | str | None, fix: str = "--proxy {name}") -> str:
    msg = f"{name} already belongs to {KIND_LABELS.get(kind, kind)}; xemfg never overwrites another mod's DLL."
    if isinstance(free, str):
        free = [free]
    if free:
        msg += f" Free names: {', '.join(free)}. Use {fix.format(name=free[0])} (or another free name)."
    else:
        msg += " Every name OptiScaler can use is taken by another mod."
    return msg


def require_exe(game: Game) -> Path:
    if game.exe is None:
        raise OpError(f"{game.title}: no game exe found ({game.exe_reason})")
    return game.exe.parent
