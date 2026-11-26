"""Choose and swap the FSR4 DLL in OptiScaler's slot, for one game or several, without reinstalling."""
from __future__ import annotations

from dataclasses import dataclass

from .. import fsr4, fsr4choice
from ..context import Context
from ..fsutil import sha256_or_none
from ..mods import cyboman_build
from ..models import Game
from ..plan import Plan
from . import apply
from .common import OpError, require_exe
from .run import run_plan


def options_for(ctx: Context, game: Game, for_install: bool = False) -> fsr4choice.Options:
    """What this game can have in OptiScaler's FSR4 slot on this GPU: newest official first (the default),
    older official versions, then every cyboman build upstream ships for the GPU."""
    exe_dir = require_exe(game)
    st = ctx.state(exe_dir)
    profile = apply.gpu_profile_for(ctx, fsr4choice.Choice("official"))
    m = ctx.manifest
    xs = m.compose(apply.target_versions(ctx, st, apply.Request(mode="install" if for_install else "swap")))
    fam = xs.families.get(profile.cyboman or "")
    cache = ctx.cache(xs)
    warnings = {}
    if fam:
        for d in fam.dlls:
            if "lossy" in d and (w := apply.cyboman_warning(cache, fam.name, d, lines=2)):
                warnings[d] = w
    dlls = fsr4.scan_game(exe_dir, game.install_dir, m.cyboman_by_hash())
    game_has = any(d.role == "upscaler" and d.location == "game" and d.api == "dx12" for d in dlls)
    slot = exe_dir / apply.UPSCALER_DLL
    rec = st.files.get(apply.UPSCALER_DLL) if st else None
    before = "your earlier DLL (backed up)" if rec and rec.action == "replaced" else None
    existing = None
    if rec is not None and rec.action == "kept":
        existing = st.components.get("fsr4", {}).get("version") or "your own DLL"
    elif rec is None and slot.is_file():
        existing = m.cyboman_by_hash().get(sha256_or_none(slot) or "") or cyboman_build(slot)
    current = st.options.get("fsr4") if st else None
    return fsr4choice.list_options(profile, fsr4.catalog(m), xs.families, xs.cyboman_tag, warnings,
                                   game_has, before, existing, current)


def pick(ctx: Context, game: Game, for_install: bool = False) -> str | None:
    """Ask which FSR4 DLL (CLI and TUI both go through ctx.ui.pick). None: cancelled."""
    opts = options_for(ctx, game, for_install)
    if not opts.options:
        raise OpError(f"{game.title}: no FSR4 DLL fits this GPU ({'; '.join(opts.excluded)})")
    lines = [o.line() for o in opts.options]
    i = ctx.ui.pick(f"FSR4 DLL for {game.title} (* = default)", lines)
    return None if i is None else str(opts.options[i].choice)


@dataclass
class SwapResult:
    game: Game
    ok: bool
    message: str


def build_swap(ctx: Context, game: Game, choice: str, replace_kept=False, dry_run=False) -> apply.Prepared:
    st = ctx.state(require_exe(game))
    if st is None:
        raise OpError(f"{game.title}: not installed by xemfg; `xemfg install {game.title} --fsr4 {choice}`")
    req = apply.Request(mode="swap", fsr4=choice, fsr4_explicit=True, replace_kept=replace_kept)
    return apply.build(ctx, game, req, dry_run=dry_run)


def swap(ctx: Context, games: list[Game], choice: str, replace_kept=False, dry_run=False,
         yes=False) -> list[SwapResult]:
    """One plan review and one confirmation for all games; failures do not stop the others."""
    preps, out = [], []
    for g in games:
        try:
            preps.append(build_swap(ctx, g, choice, replace_kept, dry_run=dry_run))
        except OpError as e:
            out.append(SwapResult(g, False, str(e)))
    if len(preps) == 1:
        p = preps[0]
        done = run_plan(ctx, p.plan, dry_run, yes)
        note = next((n for n in p.plan.notes if n.startswith("FSR4 DLL:")), "done")
        return out + [SwapResult(p.game, done or dry_run, ("would be " if dry_run else "") + note
                                 if done or dry_run else "")]
    for p in preps:
        ctx.ui.log(p.plan.render())
    if dry_run or not preps:
        return out + [SwapResult(p.game, True, "dry run") for p in preps]
    if not yes and not ctx.ui.confirm(f"Change the FSR4 DLL in {len(preps)} game(s)?"):
        return out + [SwapResult(p.game, False, "cancelled") for p in preps]
    for p in preps:
        try:
            fresh = build_swap(ctx, p.game, choice, replace_kept)  # against the now-filled cache
            fresh.plan.execute()
            out.append(SwapResult(p.game, True, "done"))
        except Exception as e:
            out.append(SwapResult(p.game, False, str(e)))
    return out


def swap_plan(ctx: Context, game: Game, choice: str, replace_kept=False) -> Plan:
    return build_swap(ctx, game, choice, replace_kept).plan
