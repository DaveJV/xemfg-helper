"""install, update, rollback, repair (thin wrappers around apply.build)."""
from __future__ import annotations

from dataclasses import dataclass

from ..context import Context
from ..models import Game, Tier
from . import apply
from .common import OpError
from .run import run_plan
from .uninstall import build_uninstall


@dataclass
class Result:
    game: Game
    ok: bool
    message: str = ""


def install(ctx: Context, game: Game, req: apply.Request, dry_run=False, yes=False) -> Result:
    prep = apply.build(ctx, game, req, dry_run=dry_run)
    done = run_plan(ctx, prep.plan, dry_run, yes)
    return Result(game, done or dry_run or prep.plan.empty, "installed" if done else "")


def compatible_games(ctx: Context, tiers=(Tier.NATIVE_FG,)) -> list[Game]:
    out = []
    for g in ctx.library().games:
        if g.exe is None:
            continue
        r = ctx.compat(g)
        if r.tier in tiers and ctx.state(g.exe.parent) is None:
            out.append(g)
    return out


def install_batch(ctx: Context, games: list[Game], req: apply.Request, dry_run=False, yes=False,
                  on_progress=None) -> list[Result]:
    """One confirmation for the whole batch, then per-game results (failures don't stop the rest)."""
    preps, results = [], []
    for g in games:
        try:
            preps.append(apply.build(ctx, g, apply.Request(**{**vars(req)}), dry_run=dry_run))
        except OpError as e:
            results.append(Result(g, False, str(e)))
    for p in preps:
        ctx.ui.log(p.plan.render())
    if dry_run or not preps:
        return results + [Result(p.game, True, "dry run") for p in preps]
    if not yes and not ctx.ui.confirm(f"Install XeMFG into {len(preps)} game(s)?"):
        return results + [Result(p.game, False, "cancelled") for p in preps]
    for i, p in enumerate(preps):
        if on_progress:
            on_progress(i, len(preps), p.game)
        try:
            # rebuild against the now-populated cache so every file is hash-compared
            prep = apply.build(ctx, p.game, apply.Request(**{**vars(req)}))
            prep.plan.execute(p.game.exe.parent / ".xemfg" if (p.game.exe.parent / ".xemfg").exists() else None)
            results.append(Result(p.game, True, "installed"))
        except Exception as e:  # keep going
            results.append(Result(p.game, False, str(e)))
    return results


def update(ctx: Context, game: Game, set_id: str | None = None, dry_run=False, yes=False, only=None, to=None,
           allow_unverified=False) -> Result:
    """Move a game's components to the newest versions (pinned ones stay). `set_id`: a whole pairing."""
    from . import components

    if set_id:
        to = {**ctx.manifest.versions_of(set_id), **(to or {})}
    done, up = components.update(ctx, game, only, to, allow_unverified, dry_run, yes)
    msg = up.message + ("; " + "; ".join(up.skipped) if up.skipped else "")
    return Result(game, done, msg if done or up.prep is None else "")


def rollback(ctx: Context, game: Game, to: str | None = None, dry_run=False, yes=False, component=None,
             allow_unverified=False) -> Result:
    """Back to the previous combination, a set (`to`), or one component's previous (or `to`) version."""
    from . import components

    done, up = components.rollback(ctx, game, component, to, allow_unverified, dry_run, yes)
    return Result(game, done, up.message if done else "")


def repair(ctx: Context, game: Game, reset_ini=False, clean=False, dry_run=False, yes=False) -> Result:
    st = ctx.state(game.exe.parent) if game.exe else None
    if st is None:
        raise OpError(f"{game.title}: not installed by xemfg")
    if clean:
        un = build_uninstall(ctx, game, mark_registry=False)
        ctx.ui.log("clean repair: full rollback to the originals, then a fresh install with the recorded options")
        if not run_plan(ctx, un, dry_run, yes):
            return Result(game, False, "")
        ctx._compat.pop(game.key, None)
        req = apply.Request(mode="install", fg_input=st.options["fg_input"], proxy=st.options["proxy"],
                            fsr4=st.options["fsr4"], dmfg_target=st.options["dmfg_target"],
                            allow_anticheat=st.options.get("allow_anticheat", False), force_tier=True,
                            fsr4_explicit=True)
        prep = apply.build(ctx, game, req)
        prep.plan.execute()
        ctx.ui.log("reinstalled")
        return Result(game, True, "clean reinstall")
    prep = apply.build(ctx, game, apply.Request(mode="repair", reset_ini=reset_ini), dry_run=dry_run)
    done = run_plan(ctx, prep.plan, dry_run, yes)
    return Result(game, done or prep.plan.empty, "repaired" if done else ("nothing to repair" if prep.plan.empty else ""))
