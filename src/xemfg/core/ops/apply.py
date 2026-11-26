"""Build plans that bring a game to a set: install, reinstall, update, rollback, repair."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import ini as ini_mod
from .. import launchopts as lo
from ..backends import get_backend
from ..backends.base import Options, Placement
from ..backends.xemfg import UPSCALER_DLL
from ..context import Context
from ..fsutil import sha256_file, sha256_or_none
from ..launchers import faugus_writer, steam_writer
from ..models import Game, Tier
from ..mods import cyboman_build
from ..plan import Plan, TextChange
from ..platform import dll_override_name
from ..registry import Entry
from ..state import STATE_DIR, GameState, IniState, LaunchState, ManagedFile, now
from .common import ConflictError, OpError, conflict_message, next_free_proxy, proxy_owner, require_exe

INI = "OptiScaler.ini"


@dataclass
class Request:
    """What the user asked for. None = use the recorded value / the default."""

    fg_input: str | None = None
    proxy: str | None = None
    fsr4: str | None = None
    dmfg_target: int | None = None
    set_id: str | None = None
    allow_anticheat: bool = False
    force_tier: bool = False  # explicit --input on a DLSS-FG-only/unknown game
    mode: str = "install"  # install | update | rollback | repair
    reset_ini: bool = False
    fsr4_explicit: bool = False


@dataclass
class Prepared:
    game: Game
    plan: Plan
    opts: Options
    set_id: str
    needs_download: list[str] = field(default_factory=list)


def _timestamp() -> str:
    return now().replace(":", "").replace("-", "")


def resolve_options(ctx: Context, game: Game, req: Request, state: GameState | None) -> Options:
    rec = Options.from_dict(state.options) if state else None
    report = ctx.compat(game)
    cfg = ctx.config
    fg_input = req.fg_input or (rec.fg_input if rec else None) or get_backend().default_fg_input(report)
    if not fg_input:
        raise OpError(f"{game.title}: {report.tier.label} - {'; '.join(report.reasons)}. "
                      "Pass --input FSRFG|FSRFG30|Upscaler to force it.")
    if req.proxy:
        proxy = req.proxy
    elif rec:
        proxy = rec.proxy
    elif report.optiscaler_proxy and report.optiscaler_proxy.endswith(".dll"):
        proxy = report.optiscaler_proxy  # reuse the name an existing OptiScaler already has
    else:
        proxy = cfg.default_proxy
    return Options(
        proxy=proxy, fg_input=fg_input,
        fsr4=req.fsr4 or (rec.fsr4 if rec else cfg.fsr4),
        gpu_family=rec.gpu_family if rec else cfg.gpu_family,
        dmfg_target=req.dmfg_target or (rec.dmfg_target if rec else cfg.dmfg_target),
        allow_anticheat=req.allow_anticheat or (rec.allow_anticheat if rec else False),
    )


def check_tier(ctx: Context, game: Game, req: Request) -> list[str]:
    """Refuse games that are not compatible; return warnings for the plan."""
    r = ctx.compat(game)
    warnings = list(r.warnings)
    if r.tier == Tier.INCOMPATIBLE:
        only_ac = r.anti_cheat and r.reasons and r.reasons[0].startswith("anti-cheat")
        if only_ac and req.allow_anticheat:
            warnings.append(f"ANTI-CHEAT OVERRIDE: {', '.join(r.anti_cheat)} may ban accounts in online modes")
        else:
            hint = " (use --allow-anticheat only for offline single-player)" if only_ac else ""
            raise OpError(f"{game.title}: not compatible - {'; '.join(r.reasons)}{hint}")
    elif r.tier in (Tier.DLSSG_ONLY, Tier.UNKNOWN) and not req.force_tier:
        raise OpError(f"{game.title}: {r.tier.label} - {'; '.join(r.reasons)}. "
                      "Never installed automatically; pass --input to force it.")
    elif r.tier == Tier.DLSSG_ONLY:
        warnings.append("DLSS-FG-only game: needs DLSS spoofing, risky on AMD")
    return warnings


def build(ctx: Context, game: Game, req: Request, dry_run: bool = False) -> Prepared:
    exe_dir = require_exe(game)
    state = ctx.state(exe_dir)
    backend = get_backend(state.backend if state else "xemfg")
    if req.mode == "install":
        warnings = check_tier(ctx, game, req)
    else:
        if state is None:
            raise OpError(f"{game.title}: not installed by xemfg (no {STATE_DIR}/state.json)")
        warnings = []
    opts = resolve_options(ctx, game, req, state)
    set_id = req.set_id or (state.set if state and req.mode == "repair" else ctx.manifest.current)
    xset = ctx.manifest.get(set_id)
    osh = ctx.manifest.optiscaler_hashes()

    # ---- proxy conflicts
    owner = proxy_owner(exe_dir, opts.proxy, osh)
    if owner not in (None, "optiscaler"):
        raise ConflictError(conflict_message(opts.proxy, owner, next_free_proxy(exe_dir, osh, skip=opts.proxy)))
    report = ctx.compat(game)
    managed_proxy = state.options.get("proxy") if state else None
    if report.optiscaler_proxy and report.optiscaler_proxy not in (opts.proxy, managed_proxy):
        raise ConflictError(f"OptiScaler is already installed as {report.optiscaler_proxy}; "
                            f"use --proxy {report.optiscaler_proxy} so there is only one copy")

    plan = Plan(f"{req.mode} {xset.id} -> {game.title}", base=exe_dir)
    plan.warnings += warnings
    if req.mode == "install":
        _note_fsr4(ctx, game, plan)
    ts = _timestamp()
    backup_dir = f"{STATE_DIR}/backups/{ts}"

    # ---- keep an existing cyboman DLL that xemfg does not manage
    up_path = exe_dir / UPSCALER_DLL
    up_managed = state and UPSCALER_DLL in state.files and state.files[UPSCALER_DLL].component == "fsr4"
    keep_fsr4 = False
    if up_path.is_file() and not up_managed and not req.fsr4_explicit and opts.fsr4 != "none":
        cy = ctx.manifest.cyboman_by_hash().get(sha256_or_none(up_path) or "") or cyboman_build(up_path)
        if cy:
            keep_fsr4 = True
            plan.notes.append(f"keeping the existing cyboman FSR4 DLL ({cy}); pass --fsr4 to replace it")

    # ---- what goes where
    cache = ctx.cache(set_id)
    try:
        if dry_run and not _cache_ready(cache, opts):
            raise _NotCached()
        placements = backend.placements(cache, opts, keep_fsr4)
        template = backend.ini_template(cache)
    except _NotCached:
        plan.notes.append(f"would download and verify the {set_id} files into {cache.dir}")
        placements, template = _coarse_placements(xset, opts, keep_fsr4), None

    new_state = GameState(game_key=game.key, title=game.title, set=set_id, options=opts.to_dict(),
                          backend=backend.name) if state is None else _copy_state(state)
    new_state.set, new_state.options, new_state.updated_at = set_id, opts.to_dict(), now()
    new_state.title = game.title
    files: dict[str, ManagedFile] = {}

    for pl in placements:
        dest = exe_dir / pl.rel
        prior = state.files.get(pl.rel) if state else None
        src_sha = pl.sha256 or (sha256_file(pl.src) if pl.src and pl.src.exists() else None)
        cur_sha = sha256_or_none(dest)
        if src_sha and cur_sha == src_sha:
            files[pl.rel] = _record(prior, "kept" if prior is None else prior.action, src_sha, pl.component,
                                    original=cur_sha if prior is None else None)
            continue
        if prior and prior.action != "kept":
            files[pl.rel] = _record(prior, prior.action, src_sha, pl.component)
        elif cur_sha:
            bk = f"{backup_dir}/{pl.rel}"
            if prior and prior.backup:
                bk = prior.backup
            else:
                plan.copy(dest, exe_dir / bk, note="backup")
            files[pl.rel] = ManagedFile("replaced", src_sha or "", bk, cur_sha, pl.component)
        else:
            files[pl.rel] = ManagedFile("added", src_sha or "", None, None, pl.component)
        if pl.src is not None:
            plan.copy(pl.src, dest, note=pl.component)
        else:
            plan.notes.append(f"would install {pl.rel} ({pl.component})")

    # ---- managed files the new set no longer uses
    if state:
        for rel, mf in state.files.items():
            if rel in files or rel == INI:
                continue
            if keep_fsr4 and rel == UPSCALER_DLL:
                continue
            _revert_file(plan, exe_dir, rel, mf)

    # ---- an existing cyboman DLL we keep is recorded, but stays the user's
    if keep_fsr4 and UPSCALER_DLL not in files:
        files[UPSCALER_DLL] = ManagedFile("kept", sha256_or_none(up_path) or "", None, sha256_or_none(up_path), "fsr4")

    new_state.files = files
    _record_components(ctx, new_state, state, set_id, opts)

    # ---- OptiScaler.ini
    _plan_ini(plan, exe_dir, state, new_state, backend, opts, req, template, backup_dir,
              existing_setup=bool(report.optiscaler_proxy))

    # ---- launch options
    _plan_launch(ctx, plan, game, state, new_state, opts, backend)

    # ---- state + registry, after the files
    new_state.history = (state.history if state else []) + [
        {"set": set_id, "at": now(), "action": req.mode,
         "components": {k: v.get("version") for k, v in new_state.components.items()}}]

    def commit():
        new_state.save(exe_dir)
        _register(ctx, game, new_state)

    plan.after.append(commit)
    return Prepared(game, plan, opts, set_id)


class _NotCached(Exception):
    pass


def _record_components(ctx: Context, new_state: GameState, state: GameState | None, set_id: str,
                       opts: Options) -> None:
    """Schema 2: each component's version, what the FSR4 slot holds, and the GPU it was chosen for."""
    from ..migrate import file_components, fsr4_component

    comps = file_components(ctx.manifest, set_id)
    slot = new_state.files.get(UPSCALER_DLL)
    comps["fsr4"] = fsr4_component(ctx.manifest, set_id, opts.to_dict(), slot.action if slot else None,
                                   slot.sha256 if slot else None)
    for k, c in comps.items():  # pins stay
        c["pinned"] = bool(state and state.pinned(k))
    new_state.components = comps
    g = ctx.gpu_selection().gpu
    if g is not None:
        new_state.gpu = {"profile": g.profile.key, "slot": g.slot, "chip": g.chip, "override": bool(g.override)}


def _note_fsr4(ctx: Context, game: Game, plan: Plan) -> None:
    """What the game ships and which GPU xemfg sees (read-only; shown in the plan)."""
    from .. import fsr4

    sel = ctx.gpu_selection()
    plan.notes.append(f"GPU: {sel.gpu.describe()}" if sel.gpu else f"GPU: {sel.reason}")
    for d in fsr4.scan_game(game.exe.parent, game.install_dir, ctx.manifest.cyboman_by_hash()):
        if d.location != "game":
            continue
        if d.role == "upscaler":
            plan.notes.append(f"the game ships {d.rel}: {d.label()}; OptiScaler runs its own copy instead")
        elif d.role == "combined":
            plan.notes.append(f"the game ships {d.rel}: {d.label()}; left alone, FSR4 comes from OptiScaler's copy")


def _cache_ready(cache, opts: Options) -> bool:
    xs = cache.xset
    ok = (cache.dir / "nightly/.xemfg-extracted").is_file() and \
        sha256_or_none(cache.dir / xs.optiscaler.filename) == xs.optiscaler.sha256 and \
        (cache.dir / "mfgu/.xemfg-extracted").is_file()
    if ok and opts.fsr4 != "none":
        fam = xs.family(opts.gpu_family)
        vdir = fam.variant_dir(opts.fsr4)
        ok = sha256_or_none(cache.dir / f"cyboman-{opts.gpu_family}" / vdir / "amd_fidelityfx_upscaler_dx12.dll") == fam.dlls[vdir]
    return ok


def _coarse_placements(xset, opts: Options, keep_fsr4: bool) -> list[Placement]:
    out = [Placement(None, opts.proxy, "optiscaler", xset.optiscaler.sha256),
           Placement(None, "OptiScaler/plugins/MFGUnlocker.asi", "plugin", xset.mfgu.inner_sha256.get("MFGUnlocker.dll")),
           Placement(None, "OptiScaler/ (all files from " + xset.nightly.asset + ")", "nightly")]
    if opts.fsr4 != "none" and not keep_fsr4:
        fam = xset.family(opts.gpu_family)
        out.append(Placement(None, UPSCALER_DLL, "fsr4", fam.dlls[fam.variant_dir(opts.fsr4)]))
    return out


def _copy_state(s: GameState) -> GameState:
    import copy

    return copy.deepcopy(s)


def _record(prior: ManagedFile | None, action: str, sha: str | None, component: str,
            original: str | None = None) -> ManagedFile:
    if prior:
        return ManagedFile(action, sha or prior.sha256, prior.backup, prior.original_sha256, component)
    return ManagedFile(action, sha or "", None, original, component)


def _revert_file(plan: Plan, exe_dir: Path, rel: str, mf: ManagedFile) -> None:
    dest = exe_dir / rel
    if mf.action == "added":
        if dest.exists():
            plan.delete(dest, note="no longer used")
    elif mf.action == "replaced" and mf.backup:
        bk = exe_dir / mf.backup
        if bk.exists():
            plan.copy(bk, dest, note="restore original")
            plan.delete(bk, note="backup no longer needed")


def _plan_ini(plan, exe_dir, state, new_state, backend, opts, req, template, backup_dir,
              existing_setup: bool = False) -> None:
    import copy

    path = exe_dir / INI
    exists = path.is_file()
    fresh = state is None
    ist = copy.deepcopy(state.ini) if state else IniState()
    if req.mode == "repair" and state and state.ini.keys:
        owned = copy.deepcopy(state.ini.keys)  # recorded values; other keys are the user's
        if hasattr(backend, "legacy_ini_keys"):
            owned = backend.legacy_ini_keys(owned, opts)
    else:
        owned = backend.owned_ini_keys(opts)
    ist.keys = owned
    new_state.ini = ist

    if not exists or req.reset_ini:
        if template is None:
            plan.notes.append(f"would {'reset' if exists else 'create'} {INI} from the paired nightly and set xemfg keys")
            return
        base_text = ini_mod.read_text(template)
        if not exists and fresh:
            ist.created = True
    else:
        base_text = ini_mod.read_text(path)

    if exists and (req.reset_ini or (fresh and not ist.created)):
        bk = f"{backup_dir}/{INI}"
        plan.copy(path, exe_dir / bk, note="backup")
        if not ist.backup:
            ist.backup = bk

    before = ini_mod.Ini(base_text)
    wanted = copy.deepcopy(owned)
    if (fresh and not (exists and existing_setup)) or req.reset_ini:
        # applied once, only over 'auto'/missing values; not owned, so repair never resets them
        for sec, kv in backend.fresh_install_ini_defaults(opts).items():
            for k, v in kv.items():
                cur = before.get(sec, k)
                if cur is None or cur.strip().lower() == "auto":
                    wanted.setdefault(sec, {}).setdefault(k, v)
    if fresh:
        ist.original = {sec: {k: (before.get(sec, k) if exists else None) for k in kv} for sec, kv in wanted.items()}

    new_text, changes = ini_mod.plan_edit(base_text, wanted)
    old_disk = ini_mod.read_text(path) if exists else ""
    if new_text != old_disk:
        plan.write(path, new_text.encode("utf-8", errors="surrogateescape"), note="ini")
        if exists:
            diff = ini_mod.unified_diff(old_disk, new_text, INI)
        else:
            diff = "(new file from the paired nightly)\n" + "\n".join(
                f"[{c.section}] {c.key}={c.new}" for c in changes)
        plan.texts.append(TextChange(INI, diff))


def current_launch_text(ctx: Context, game: Game) -> str | None:
    lib = ctx.library()
    if game.launch_target == "faugus" and lib.faugus:
        return faugus_writer.read_launch_args(lib.faugus.games_json, game.gameid)
    if game.launch_target == "steam" and lib.steam:
        cfgs = lib.steam.userdata_configs()
        return steam_writer.read_launch_options(cfgs[0], game.appid) if cfgs else None
    return None


def _plan_launch(ctx: Context, plan: Plan, game: Game, state, new_state: GameState, opts: Options, backend) -> None:
    dll = dll_override_name(opts.proxy)
    env = backend.launch_env(opts)
    old = state.launch if state else LaunchState()
    # only what xemfg added itself; a variable that was there before the install is the user's
    remove_env = {k: None for k in lo.xemfg_added_env(old.env, old.original) if k not in env}
    remove_env["PROTON_USE_OPTISCALER"] = None
    target = game.launch_target
    current = current_launch_text(ctx, game) if target else None
    ls = LaunchState(target=target, dll=dll, env=env, had_override=old.had_override,
                     original=old.original, queued=False)
    if state is None:
        ls.had_override = bool(current and lo.override_mode(current, dll))
        ls.original = current
    if target is None:
        lines, steam_form = lo.display_lines(dll, env)
        plan.notes.append("no launcher entry to write; add these launch options yourself: " + steam_form)
        new_state.launch = ls
        return
    cur = current or ""
    if target == "faugus":
        new = cur
        if old.dll and old.dll != dll and not old.had_override:
            new = lo.faugus_apply(new, old.dll, None)
        new = lo.faugus_apply(new, dll, "n,b", {**env, **remove_env})
    else:
        new = cur
        if old.dll and old.dll != dll and not old.had_override:
            new = lo.steam_apply(new, old.dll, None)
        new = lo.steam_apply(new, dll, "n,b", {**env, **remove_env})
    ls.written = new
    new_state.launch = ls
    if new == cur:
        return
    label = "Faugus launch options" if target == "faugus" else "Steam launch options"
    if target == "faugus" and game.source.value == "faugus-prefix":
        plan.warnings.append(f"launch options go on the '{game.gameid}' entry that launches this prefix")
    plan.texts.append(TextChange(label, f"- {cur!r}\n+ {new!r}"))
    if target == "faugus":
        if ctx.faugus_running():
            plan.warnings.append("Faugus Launcher is running; close it before launching the game, "
                                 "or it may overwrite games.json")

        def write_faugus():
            faugus_writer.write_launch_args(ctx.library().faugus.games_json, game.gameid, new, ctx.paths.backups_dir)

        plan.after.append(write_faugus)
    else:
        steam = ctx.library().steam
        if ctx.steam_running():
            ls.queued = True
            plan.warnings.append("Steam is running: launch options are queued; close Steam and run "
                                 "`xemfg launch-opts --apply-queued`, or paste: " + new)

            def queue():
                ctx.registry.steam_queue[game.appid] = {"options": new, "key": game.key, "title": game.title,
                                                        "queued_at": now()}
                ctx.registry.save()

            plan.after.append(queue)
        else:
            def write_steam():
                cfgs = steam.userdata_configs()
                if not cfgs:
                    raise OpError("no Steam user config (userdata/*/config/localconfig.vdf) found")
                steam_writer.write_launch_options(cfgs[0], game.appid, new, ctx.paths.backups_dir)
                ctx.registry.steam_queue.pop(game.appid, None)
                ctx.registry.save()

            plan.after.append(write_steam)


def _register(ctx: Context, game: Game, st: GameState) -> None:
    reg = ctx.registry
    prev = reg.get(game.key)
    previous_sets = list(prev.previous_sets) if prev else []
    if prev and prev.set != st.set and prev.status == "installed":
        previous_sets = [s for s in previous_sets if s != prev.set] + [prev.set]
    e = Entry(
        key=game.key, title=game.title, source=game.source.value, exe=str(game.exe), exe_dir=str(game.exe.parent),
        exe_sha256=sha256_or_none(game.exe), set=st.set, options=st.options,
        files={k: v.sha256 for k, v in st.files.items()}, appid=game.appid, gameid=game.gameid,
        runner=game.runner, launch_target=game.launch_target, status="installed",
        previous_sets=previous_sets, installed_at=prev.installed_at if prev and prev.status == "installed" else st.installed_at,
        updated_at=now(), adopted=st.adopted, components=st.components,
    )
    reg.entries[game.key] = e
    reg.save()
