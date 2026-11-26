"""Per-component update, pin and rollback, and the installed-vs-newest table for check.

Components: nightly (OptiScaler nightly), optiscaler (dashdogy's OptiScaler.dll), mfgu (MFG Unlocker plugin),
cyboman (the cyboman release in OptiScaler's FSR4 slot) and fsr4 (the slot itself, for pinning).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import fsr4 as fsr4_mod
from ..context import Context
from ..manifest import FILE_COMPONENTS
from ..migrate import describe_fsr4
from ..models import Game
from ..state import GameState
from . import apply
from .common import OpError
from .run import run_plan

UPDATABLE = (*FILE_COMPONENTS, "cyboman")
PINNABLE = (*FILE_COMPONENTS, "fsr4")
ALIASES = {"opti": "optiscaler", "optiscaler.dll": "optiscaler", "plugin": "mfgu", "mfg-unlocker": "mfgu",
           "dll": "fsr4"}
LABEL = {"nightly": "OptiScaler nightly", "optiscaler": "dashdogy OptiScaler.dll", "mfgu": "MFG Unlocker",
         "cyboman": "cyboman FSR4 DLL", "fsr4": "FSR4 DLL"}


def parse_components(names: list[str] | str | None, allowed=UPDATABLE) -> list[str]:
    if not names:
        return []
    if isinstance(names, str):
        names = [n for part in names.split(",") for n in [part.strip()] if n]
    out = []
    for n in names:
        n = ALIASES.get(n.lower(), n.lower())
        if allowed is PINNABLE and n == "cyboman":
            n = "fsr4"
        if n not in allowed:
            raise OpError(f"unknown component '{n}' (one of {', '.join(allowed)})")
        if n not in out:
            out.append(n)
    return out


def _state(ctx: Context, game: Game) -> GameState:
    st = ctx.state(game.exe.parent) if game.exe else None
    if st is None:
        raise OpError(f"{game.title}: not installed by xemfg")
    if not st.components:
        raise OpError(f"{game.title}: set {st.set} is not in the manifest; run `xemfg manifest refresh` "
                      "or `xemfg repair --clean`")
    return st


def current_versions(ctx: Context, st: GameState) -> dict[str, str]:
    return apply.target_versions(ctx, st, apply.Request(mode="repair"))


def _pin_key(c: str) -> str:
    return "fsr4" if c == "cyboman" else c


def _uses_cyboman(st: GameState) -> bool:
    return st.components.get("fsr4", {}).get("kind") == "cyboman"


@dataclass
class UpdatePlan:
    game: Game
    prep: apply.Prepared | None
    changes: dict[str, tuple[str, str]] = field(default_factory=dict)  # component -> (from, to)
    skipped: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        parts = [f"{c} {a} -> {b}" for c, (a, b) in self.changes.items()]
        return "; ".join(parts) if parts else "up to date"


def plan_update(ctx: Context, game: Game, only: list[str] | None = None, to: dict[str, str] | None = None,
                allow_unverified: bool = False, dry_run: bool = False, mode: str = "update") -> UpdatePlan:
    """Move components to the newest versions (or to `to`). Pinned components stay unless named in `to`."""
    st = _state(ctx, game)
    cur = current_versions(ctx, st)
    newest = ctx.manifest.newest()
    to = dict(to or {})
    for c, v in to.items():
        if c in UPDATABLE and v not in ctx.manifest.components.get(c, {}):
            raise OpError(f"{c} {v} is not in the manifest (known: {', '.join(ctx.manifest.versions(c))}); "
                          "`xemfg manifest add` resolves a release from upstream")
    wanted = only or list(to) or list(UPDATABLE)
    up = UpdatePlan(game, None)
    versions = {}
    for c in wanted:
        if c == "cyboman" and not _uses_cyboman(st):
            if only or c in to:
                up.skipped.append("cyboman: the FSR4 slot does not hold a cyboman DLL")
            continue
        target = to.get(c) or newest.get(c)
        if not target or target == cur.get(c):
            continue
        if st.pinned(_pin_key(c)) and c not in to:
            up.skipped.append(f"{c} is pinned at {cur.get(c)} (newest {target})")
            continue
        versions[c] = target
        up.changes[c] = (cur.get(c, "?"), target)
    if not versions:
        return up
    req = apply.Request(mode=mode, versions=versions, allow_unverified=allow_unverified)
    up.prep = apply.build(ctx, game, req, dry_run=dry_run)
    return up


def _run(ctx: Context, up: UpdatePlan, allow_unverified: bool, dry_run: bool, yes: bool) -> bool:
    if up.prep is None:
        return True
    if up.prep.unverified and yes and not allow_unverified and not dry_run:
        raise OpError(f"{up.game.title}: {'; '.join(up.prep.unverified)}. This combination is not one upstream "
                      "documents; pass --allow-unverified to install it without being asked")
    return run_plan(ctx, up.prep.plan, dry_run, yes)


def update(ctx: Context, game: Game, only: list[str] | None = None, to: dict[str, str] | None = None,
           allow_unverified=False, dry_run=False, yes=False) -> tuple[bool, UpdatePlan]:
    up = plan_update(ctx, game, only, to, allow_unverified, dry_run)
    return _run(ctx, up, allow_unverified, dry_run, yes), up


# ------------------------------------------------------------------ rollback

def previous_version(st: GameState, component: str, current: str | None) -> str | None:
    """The most recent version of a component in the game's history that differs from the current one."""
    key = component
    for h in reversed(st.history):
        hc = h.get("components") or {}
        v = hc.get(key)
        if v and v != current:
            return v
    return None


def previous_combination(ctx: Context, st: GameState, current: dict[str, str]) -> dict[str, str] | None:
    for h in reversed(st.history):
        hc = h.get("components")
        if hc:
            v = {k: hc[k] for k in UPDATABLE if hc.get(k)}
        elif h.get("set") in ctx.manifest.pairings:
            v = ctx.manifest.versions_of(h["set"])
            if not _uses_cyboman(st):
                v.pop("cyboman", None)
        else:
            continue
        cur = {k: current[k] for k in v if k in current}
        if v and v != cur:
            return v
    return None


def rollback(ctx: Context, game: Game, component: str | None = None, to: str | None = None,
             allow_unverified=False, dry_run=False, yes=False) -> tuple[bool, UpdatePlan]:
    st = _state(ctx, game)
    cur = current_versions(ctx, st)
    if component:
        target = to or previous_version(st, component, cur.get(component))
        if not target:
            raise OpError(f"{game.title}: no earlier {component} version in this game's history")
        target_versions = {component: target}
    elif to:  # a whole pairing, as before
        if to not in ctx.manifest.pairings:
            raise OpError(f"{game.title}: unknown set {to} (known: {', '.join(ctx.manifest.pairings)})")
        if to not in st.previous_sets and to != st.set:
            raise OpError(f"{game.title}: set {to} was never installed here "
                          f"(recorded: {', '.join(st.previous_sets) or 'none'})")
        target_versions = ctx.manifest.versions_of(to)
        if not _uses_cyboman(st):
            target_versions.pop("cyboman", None)
    else:
        target_versions = previous_combination(ctx, st, cur)
        if not target_versions:
            raise OpError(f"{game.title}: no previous combination recorded")
    up = plan_update(ctx, game, list(target_versions), target_versions, allow_unverified, dry_run, mode="rollback")
    return _run(ctx, up, allow_unverified, dry_run, yes), up


# ------------------------------------------------------------------ pin

def pin(ctx: Context, game: Game, components: list[str], on: bool = True) -> list[str]:
    """Pin (or unpin) components of one game. Metadata only: no game file changes."""
    st = _state(ctx, game)
    out = []
    for c in components:
        if c not in st.components:
            raise OpError(f"{game.title}: no {c} recorded")
        st.components[c]["pinned"] = on
        v = describe_fsr4(st.components[c]) if c == "fsr4" else st.components[c].get("version")
        out.append(f"{c} {v}")
    st.save(game.exe.parent)
    e = ctx.registry.get(game.key)
    if e is not None:
        e.components = st.components
        ctx.registry.save()
    return out


# ------------------------------------------------------------------ check rows

@dataclass
class Row:
    component: str
    installed: str
    newest: str
    status: str  # current | update | pinned | info


def rows(ctx: Context, st: GameState, newest: dict[str, str] | None = None) -> tuple[list[Row], list[str]]:
    """Installed vs newest per component for one game, and why its mix is unverified (if it is)."""
    newest = newest or ctx.manifest.newest()
    cur = current_versions(ctx, st)
    out = []
    for c in FILE_COMPONENTS:
        have, new = cur.get(c, "?"), newest.get(c, "?")
        status = "current" if have == new else "pinned" if st.pinned(c) else "update"
        out.append(Row(c, have, new, status))
    fsr = st.components.get("fsr4", {})
    pinned = st.pinned("fsr4")
    if fsr.get("kind") == "cyboman":
        new = newest.get("cyboman", "?")
        status = "current" if fsr.get("version") == new else "pinned" if pinned else "update"
        out.append(Row("cyboman", describe_fsr4(fsr), new, status))
    else:
        sel = ctx.gpu_selection().gpu
        best = fsr4_mod.newest_official(sel.profile) if sel else None
        have = fsr.get("version") if fsr.get("kind") == "official" else None
        if best and have and fsr4_mod.vtuple(have) >= fsr4_mod.vtuple(best.version):
            status = "current"
        elif pinned:
            status = "pinned"
        else:
            status = "info"
        out.append(Row("fsr4", describe_fsr4(fsr), f"official {best.version}" if best else "-", status))
    return out, ctx.manifest.unverified({k: cur[k] for k in FILE_COMPONENTS if k in cur})
