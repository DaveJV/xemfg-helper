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
from .. import fsr4, fsr4choice
from ..gpu import PROFILES, Profile
from ..manifest import FILE_COMPONENTS
from ..models import Game, Tier
from ..mods import KIND_LABELS, cyboman_build
from ..plan import Plan, TextChange
from ..platform import dll_override_name
from ..registry import Entry
from ..state import STATE_DIR, GameState, IniState, LaunchState, ManagedFile, now
from .common import (ConflictError, OpError, conflict_message, free_proxies, proxy_owner, require_exe,
                     taken_proxies)

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
    versions: dict[str, str] | None = None  # component -> version to install (overrides the defaults)
    allow_unverified: bool = False  # a mix upstream does not document, without asking again
    replace_kept: bool = False  # replace a DLL the user put into OptiScaler's FSR4 slot
    auto_proxy: bool = False  # proxy name taken by another mod: use the first free one instead of asking


@dataclass
class Prepared:
    game: Game
    plan: Plan
    opts: Options
    set_id: str
    needs_download: list[str] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    unverified: list[str] = field(default_factory=list)  # why the mix is not one upstream documents


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
    try:
        choice = fsr4choice.parse(req.fsr4 or (rec.fsr4 if rec else cfg.fsr4))
    except fsr4choice.ChoiceError as e:
        raise OpError(str(e)) from None
    new_choice = rec is None or bool(req.fsr4_explicit) or req.mode == "install"
    profile_key = rec.gpu_profile if rec else ""
    family = rec.gpu_family if rec else cfg.gpu_family
    if new_choice and choice.kind in ("official", "cyboman"):
        p = gpu_profile_for(ctx, choice)
        profile_key, family = p.key, p.cyboman or family
    elif not profile_key and choice.kind == "official":  # an install from before GPU detection
        profile_key = gpu_profile_for(ctx, choice).key
    return Options(
        proxy=proxy, fg_input=fg_input, fsr4=str(choice), gpu_family=family,
        dmfg_target=req.dmfg_target or (rec.dmfg_target if rec else cfg.dmfg_target),
        allow_anticheat=req.allow_anticheat or (rec.allow_anticheat if rec else False),
        gpu_profile=profile_key,
    )


def gpu_profile_for(ctx: Context, choice) -> "Profile":
    """The GPU class an official or cyboman DLL is chosen for. Several GPUs: ask; unknown GPU: refuse."""
    from ..gpu import override_warning

    sel = ctx.gpu_selection()
    if sel.needs_choice:
        i = ctx.ui.pick("Which GPU do your games run on?", [g.describe() for g in sel.choices])
        if i is None:
            raise OpError(f"{sel.reason}. Run `xemfg gpu --pick`, or pass --fsr4 nightly")
        ctx.config.gpu = sel.choices[i].slot
        ctx.save_config()
        sel = ctx.gpu_selection()
    g = sel.gpu
    if g is None:
        raise OpError(f"{sel.reason}; xemfg needs to know the GPU to pick an FSR4 DLL (or pass --fsr4 nightly)")
    if not g.known and not g.override:
        raise OpError(f"xemfg does not know this GPU ({g.describe()}) and will not guess which FSR4 DLL fits. "
                      "Pass --fsr4 nightly to use OptiScaler's own DLL, or `xemfg gpu --override <profile>`")
    p = g.profile
    if p.model is None:
        raise OpError(f"{p.label}: {p.note or 'no FSR4 for this GPU'}. Pass --fsr4 nightly")
    if choice.kind == "cyboman" and p.cyboman is None:
        raise OpError(f"cyboman's DLLs are not offered for {p.label}: {p.note}")
    if (w := override_warning(g)):
        ctx.ui.warn(w)
    return p


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


def target_versions(ctx: Context, state: GameState | None, req: Request) -> dict[str, str]:
    """Component versions to install: install takes the newest known; the other modes keep what the game has.

    Explicit `req.versions` (update, rollback, pin) win; a pairing id (`req.set_id`) means all of its components.
    """
    m = ctx.manifest
    if req.set_id:
        base = m.versions_of(req.set_id)
    elif state is not None and req.mode != "install" and state.components:
        base = {k: state.components[k]["version"] for k in FILE_COMPONENTS if k in state.components}
        fsr = state.components.get("fsr4", {})
        if fsr.get("kind") == "cyboman" and fsr.get("version"):
            base["cyboman"] = fsr["version"]
    elif state is not None and req.mode != "install" and state.set in m.pairings:
        base = m.versions_of(state.set)
    else:
        base = m.newest()
    if "cyboman" not in base and m.newest().get("cyboman"):
        base["cyboman"] = m.newest()["cyboman"]
    base.update(req.versions or {})
    return base


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
        r = ctx.compat(game)
        if r.anti_cheat and not (req.allow_anticheat or state.options.get("allow_anticheat")):
            raise OpError(f"{game.title}: anti-cheat ({', '.join(r.anti_cheat)}) found; xemfg does not replace "
                          "game files there (--allow-anticheat only for offline single-player)")
    opts = resolve_options(ctx, game, req, state)
    choice = fsr4choice.parse(opts.fsr4)
    versions = target_versions(ctx, state, req)
    xset = ctx.manifest.compose(versions)
    set_id = xset.id
    unverified = ctx.manifest.unverified(versions)
    osh = ctx.manifest.optiscaler_hashes()

    # ---- proxy conflicts: another mod (ReShade, Special K, an ASI loader ...) may hold the name
    proxy_note = _choose_proxy(ctx, game, exe_dir, opts, req, state, osh)
    report = ctx.compat(game)
    managed_proxy = state.options.get("proxy") if state else None
    if report.optiscaler_proxy and report.optiscaler_proxy not in (opts.proxy, managed_proxy):
        raise ConflictError(f"OptiScaler is already installed as {report.optiscaler_proxy}; "
                            f"use --proxy {report.optiscaler_proxy} so there is only one copy")

    plan = Plan(f"{req.mode} {xset.id} -> {game.title}", base=exe_dir)
    plan.warnings += warnings
    if proxy_note:
        plan.warnings.append(proxy_note)
    if unverified:
        plan.warnings.append("UNVERIFIED COMBINATION (upstream does not document it; it may crash): "
                             + "; ".join(unverified))
    if req.mode == "install":
        _note_fsr4(ctx, game, plan)
    ts = _timestamp()
    backup_dir = f"{STATE_DIR}/backups/{ts}"

    # ---- a DLL the user put into OptiScaler's FSR4 slot stays theirs
    up_path = exe_dir / UPSCALER_DLL
    slot_rec = state.files.get(UPSCALER_DLL) if state else None
    keep_fsr4 = choice.kind == "keep"
    if slot_rec is not None and slot_rec.action == "kept" and choice.kind != "keep":
        if not req.fsr4_explicit:
            keep_fsr4 = True  # update/repair never replace it
        elif not req.replace_kept:
            raise OpError(f"{game.title}: OptiScaler's FSR4 DLL is one you put there yourself; pass "
                          "--replace-kept to replace it (it is backed up first)")
        else:
            plan.warnings.append("REPLACING your own FSR4 DLL (it is backed up first)")
    if up_path.is_file() and slot_rec is None and not req.fsr4_explicit and choice.kind != "nightly":
        cy = ctx.manifest.cyboman_by_hash().get(sha256_or_none(up_path) or "") or cyboman_build(up_path)
        if cy:
            keep_fsr4 = True
            plan.notes.append(f"keeping the existing cyboman FSR4 DLL ({cy}); pass --fsr4 to replace it")
    if keep_fsr4 and not up_path.is_file():
        raise OpError(f"{game.title}: --fsr4 keep, but OptiScaler's FSR4 slot is empty")

    # ---- what goes where
    cache = ctx.cache(xset)
    official = _official_for(ctx, choice, opts) if not keep_fsr4 else None
    if official:  # record the exact version: repair and update never move it silently
        choice = fsr4choice.Choice("official", official[0].version)
        opts.fsr4 = str(choice)
    try:
        if dry_run and not (cache.ready("none", opts.gpu_family) and _slot_ready(cache, choice, opts, official)):
            raise _NotCached()
        slot = None if keep_fsr4 else _slot(cache, xset, choice, opts, official)
        placements = backend.placements(cache, opts, keep_fsr4, slot)
        template = backend.ini_template(cache)
    except _NotCached:
        plan.notes.append(f"would download and verify the {set_id} files into {cache.dir}")
        placements, template = _coarse_placements(xset, opts, keep_fsr4, choice, official), None
    if template is not None:
        why = fsr4choice.template_supports(ini_mod.read_text(template), fsr4choice.ini_keys(
            choice, PROFILES.get(opts.gpu_profile or "")))
        if why:
            raise OpError(f"{game.title}: {why}")
    if not keep_fsr4:
        plan.notes.append(f"FSR4 DLL: {_describe_choice(choice, opts, official)}")
    if choice.kind == "cyboman" and choice.arg and "lossy" in fsr4choice.variant_dir(
            xset.family(opts.gpu_family), choice.arg):
        plan.warnings.append(f"cyboman {choice.arg} changes the picture against AMD's: "
                             + (cyboman_warning(cache, opts.gpu_family, choice.arg) or "read its WARNING.txt"))

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
    coarse = any(pl.src is None and pl.rel.startswith("OptiScaler/ (") for pl in placements)
    if coarse and state:
        plan.notes.append("the files inside OptiScaler/ are compared once they are downloaded "
                          "(a dry run downloads nothing)")
    if state:
        for rel, mf in state.files.items():
            if rel in files or rel == INI:
                continue
            if coarse and mf.component in ("nightly", "fsr4-stock") and not (
                    rel in SLOT.values() or rel == UPSCALER_DLL + ".orig"):
                continue
            if keep_fsr4 and rel == UPSCALER_DLL:
                continue
            _revert_file(plan, exe_dir, rel, mf)

    # ---- an existing cyboman DLL we keep is recorded, but stays the user's
    if keep_fsr4 and UPSCALER_DLL not in files:
        files[UPSCALER_DLL] = ManagedFile("kept", sha256_or_none(up_path) or "", None, sha256_or_none(up_path), "fsr4")

    new_state.files = files
    _record_components(ctx, new_state, state, versions, opts, xset, choice, official, keep_fsr4)

    # ---- OptiScaler.ini
    _plan_ini(plan, exe_dir, state, new_state, backend, opts, req, template, backup_dir,
              existing_setup=bool(report.optiscaler_proxy))

    # ---- launch options
    _plan_launch(ctx, plan, game, state, new_state, opts, backend)

    # ---- state + registry, after the files
    new_state.history = (state.history if state else []) + [
        {"set": set_id, "at": now(), "action": req.mode,
         "components": history_versions(new_state.components)}]

    def commit():
        new_state.save(exe_dir)
        _register(ctx, game, new_state)
        _keep_in_store(ctx, game, exe_dir, new_state)

    plan.after.append(commit)
    return Prepared(game, plan, opts, set_id, versions=versions, unverified=unverified)


class _NotCached(Exception):
    pass


def _choose_proxy(ctx: Context, game: Game, exe_dir: Path, opts: Options, req: Request,
                  state: GameState | None, osh: set[str]) -> str | None:
    """Keep OptiScaler off a name another mod holds. Returns a note for the plan when the name changed.

    A name given with --proxy is never replaced by another; a new install asks which free name to use
    (or takes the first with auto_proxy), like OptiScaler's own setup script.
    """
    owner = proxy_owner(exe_dir, opts.proxy, osh)
    if owner in (None, "optiscaler"):
        return None
    free = free_proxies(exe_dir, osh, skip=opts.proxy)
    q = _q_game(game)
    if req.proxy or state is not None or not free:
        fix = f"xemfg repair {q} --proxy {{name}}" if state is not None else "--proxy {name}"
        raise ConflictError(conflict_message(opts.proxy, owner, free, fix))
    label = KIND_LABELS.get(owner, owner)
    if req.auto_proxy:
        chosen = free[0]
    else:
        taken = ", ".join(taken_proxies(exe_dir, osh))
        i = ctx.ui.pick(f"{opts.proxy} already belongs to {label} (taken: {taken}). "
                        "Which name should OptiScaler be loaded under?", free)
        if i is None:
            raise ConflictError(conflict_message(opts.proxy, owner, free))
        chosen = free[i]
    note = f"{opts.proxy} belongs to {label}, so OptiScaler goes in as {chosen}"
    opts.proxy = chosen
    return note


def _old_proxy_foreign(state: GameState | None, game: Game) -> bool:
    """The name OptiScaler used before now holds another mod's DLL, which needs its launch override."""
    old = state.options.get("proxy") if state else None
    mf = state.files.get(old) if state and old else None
    return bool(mf and game.exe and changed_by_someone_else(game.exe.parent / old, mf))


def _q_game(game: Game) -> str:
    return game.appid or game.gameid or f'"{game.title}"'


SLOT = {"dx12": UPSCALER_DLL, "vk": "OptiScaler/amd_fidelityfx_upscaler_vk.dll"}


def _official_for(ctx: Context, choice, opts: Options) -> list:
    """The official DLL(s) for an `official` choice: dx12, plus vk when that version has one."""
    if choice.kind != "official":
        return []
    profile = PROFILES.get(opts.gpu_profile or "")
    cat = fsr4.catalog(ctx.manifest)
    if choice.arg:
        dx = next((d for d in cat if d.api == "dx12" and d.version == choice.arg), None)
        if dx is None:
            known = ", ".join(sorted({d.version for d in cat if d.api == "dx12"}, key=fsr4.vtuple, reverse=True))
            raise OpError(f"no official FSR4 DLL {choice.arg} (known: {known})")
    else:
        dx = fsr4.newest_official(profile, "dx12", ctx.manifest) if profile else None
        if dx is None:
            raise OpError("no official FSR4 DLL fits this GPU")
    if profile is not None and (why := fsr4choice.official_ok(dx, profile)):
        raise OpError(f"official {dx.version} does not fit {profile.label}: {why}")
    vk = next((d for d in cat if d.api == "vk" and d.version == dx.version), None)
    return [dx] + ([vk] if vk else [])


def _slot_ready(cache, choice, opts: Options, official) -> bool:
    if choice.kind == "cyboman":
        return cache.ready(str(choice), opts.gpu_family)
    if choice.kind == "official":
        return all(cache.official_ready(d) for d in official)
    return True


def _slot(cache, xset, choice, opts: Options, official) -> dict | None:
    """What goes into OptiScaler's FSR4 slot instead of the nightly's own DLLs (None: the nightly's)."""
    if choice.kind == "nightly":
        return None
    if choice.kind == "original":  # whatever was there before xemfg; nothing: OptiScaler runs the game's
        return {UPSCALER_DLL: None, SLOT["vk"]: None, UPSCALER_DLL + ".orig": None}
    if choice.kind == "cyboman":
        fam = xset.family(opts.gpu_family)
        vdir = fsr4choice.variant_dir(fam, choice.arg)
        stock = cache.nightly_dir() / UPSCALER_DLL
        return {UPSCALER_DLL: Placement(cache.cyboman_dll(opts.gpu_family, vdir), UPSCALER_DLL, "fsr4", fam.dlls[vdir]),
                # AMD's file beside it, as cyboman's README and xemfg-linux.sh do
                UPSCALER_DLL + ".orig": Placement(stock, UPSCALER_DLL + ".orig", "fsr4-stock")}
    out = {UPSCALER_DLL + ".orig": None}
    for d in official:
        out[SLOT[d.api]] = Placement(cache.official_dll(d), SLOT[d.api], "fsr4", d.sha256)
    return out


def cyboman_warning(cache, family: str, variant: str, lines: int = 4) -> str | None:
    """The first lines of the WARNING.txt cyboman ships with a lossy build (when the archive is extracted)."""
    try:
        vdir = fsr4choice.variant_dir(cache.xset.family(family), variant)
    except Exception:
        return None
    p = cache.cyboman_dir() / family / vdir / "WARNING.txt"
    if not p.is_file():
        return None
    text = [x.strip() for x in p.read_text(errors="replace").splitlines() if x.strip()]
    return " ".join(text[:lines]) or None


def _describe_choice(choice, opts: Options, official) -> str:
    profile = PROFILES.get(opts.gpu_profile or "")
    gpu = f" for {profile.label}" if profile else ""
    if choice.kind == "official":
        d = official[0]
        return f"official {d.version} (AMD FSR SDK {d.sdk_tag}){gpu}" + (" + Vulkan" if len(official) > 1 else "")
    if choice.kind == "cyboman":
        return f"cyboman {choice.arg} from the {opts.gpu_family} archive{gpu}"
    if choice.kind == "original":
        return "what was in OptiScaler's slot before xemfg (nothing: OptiScaler runs the game's own copy)"
    return "the DLL that comes with the OptiScaler nightly"


def _keep_in_store(ctx: Context, game: Game, exe_dir: Path, st: GameState) -> None:
    """Every FSR4 DLL put into a game is kept in the store, so going back never needs the cache or network."""
    fsr = st.components.get("fsr4", {})
    for rel in (UPSCALER_DLL, SLOT["vk"]):
        mf = st.files.get(rel)
        if mf is None or mf.component != "fsr4" or not (exe_dir / rel).is_file():
            continue
        try:
            ctx.store.put(exe_dir / rel, {"kind": fsr.get("kind"), "version": fsr.get("version"),
                                          "variant": fsr.get("variant"), "family": fsr.get("family"),
                                          "api": "vk" if rel.endswith("_vk.dll") else "dx12", "games": [game.key]})
        except OSError as e:
            ctx.ui.warn(f"could not keep a copy of {rel} in the DLL store: {e}")


def history_versions(comps: dict) -> dict[str, str]:
    """What a history entry records: each file component's version, the FSR4 slot's kind, and the cyboman
    release when the slot holds one (what rollback and gc read back)."""
    out = {k: comps[k]["version"] for k in FILE_COMPONENTS if k in comps}
    fsr = comps.get("fsr4", {})
    if fsr:
        out["fsr4_kind"] = fsr.get("kind")
        if fsr.get("choice"):
            out["fsr4"] = fsr["choice"]
        if fsr.get("kind") == "cyboman":
            out["cyboman"] = fsr.get("version")
            out["cyboman_variant"] = fsr.get("variant")
    return out


def _record_components(ctx: Context, new_state: GameState, state: GameState | None, versions: dict[str, str],
                       opts: Options, xset, choice=None, official=None, kept: bool = False) -> None:
    """Schema 2: each component's version, what the FSR4 slot holds, and the GPU it was chosen for."""
    from ..migrate import fsr4_component

    comps = {k: {"version": versions[k]} for k in FILE_COMPONENTS}
    slot = new_state.files.get(UPSCALER_DLL)
    sha = slot.sha256 if slot else None
    if choice is None:  # adopt: from what is installed
        comps["fsr4"] = fsr4_component(ctx.manifest, xset.id, opts.to_dict(), slot.action if slot else None, sha,
                                       xset=xset, nightly=versions["nightly"])
    elif kept:
        label = ctx.manifest.cyboman_by_hash().get(sha or "") or "your own DLL"
        comps["fsr4"] = {"kind": "kept", "version": label, "sha256": sha, "choice": "keep"}
    elif choice.kind == "official":
        comps["fsr4"] = {"kind": "official", "version": official[0].version, "sha256": official[0].sha256,
                         "choice": str(choice)}
    elif choice.kind == "cyboman":
        fam = xset.family(opts.gpu_family)
        comps["fsr4"] = {"kind": "cyboman", "version": xset.cyboman_tag, "variant": fsr4choice.variant_dir(fam, choice.arg),
                         "family": opts.gpu_family, "sha256": sha, "choice": str(choice)}
    elif choice.kind == "original":
        what = "the backed-up original" if slot and slot.action == "replaced" else "none: the game's own copy runs"
        comps["fsr4"] = {"kind": "original", "version": what, "sha256": sha, "choice": "original"}
    else:
        comps["fsr4"] = {**fsr4_component(ctx.manifest, xset.id, {"fsr4": "none"}, None, sha, xset=xset,
                                          nightly=versions["nightly"]), "choice": "nightly"}
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


def _coarse_placements(xset, opts: Options, keep_fsr4: bool, choice, official) -> list[Placement]:
    out = [Placement(None, opts.proxy, "optiscaler", xset.optiscaler.sha256),
           Placement(None, "OptiScaler/plugins/MFGUnlocker.asi", "plugin", xset.mfgu.inner_sha256.get("MFGUnlocker.dll")),
           Placement(None, "OptiScaler/ (all files from " + xset.nightly.asset + ")", "nightly")]
    if keep_fsr4:
        return out
    if choice.kind == "cyboman":
        fam = xset.family(opts.gpu_family)
        out.append(Placement(None, UPSCALER_DLL, "fsr4", fam.dlls[fsr4choice.variant_dir(fam, choice.arg)]))
    elif choice.kind == "official":
        for d in official:
            out.append(Placement(None, SLOT[d.api], "fsr4", d.sha256))
    return out


def _copy_state(s: GameState) -> GameState:
    import copy

    return copy.deepcopy(s)


def _record(prior: ManagedFile | None, action: str, sha: str | None, component: str,
            original: str | None = None) -> ManagedFile:
    if prior:
        return ManagedFile(action, sha or prior.sha256, prior.backup, prior.original_sha256, component)
    return ManagedFile(action, sha or "", None, original, component)


def changed_by_someone_else(dest: Path, mf: ManagedFile) -> bool:
    """The file is there but is not what xemfg put there (another mod or a game update replaced it)."""
    cur = sha256_or_none(dest)
    return cur is not None and bool(mf.sha256) and cur != mf.sha256


def _revert_file(plan: Plan, exe_dir: Path, rel: str, mf: ManagedFile) -> None:
    dest = exe_dir / rel
    if changed_by_someone_else(dest, mf):
        plan.warnings.append(f"{rel} changed since xemfg installed it (another mod or a game update?); "
                             "left as it is")
        return
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
    swap_only = None
    if req.mode in ("repair", "swap") and state and state.ini.keys:
        owned = copy.deepcopy(state.ini.keys)  # recorded values; other keys are the user's
        if hasattr(backend, "legacy_ini_keys"):
            owned = backend.legacy_ini_keys(owned, opts)
        if req.mode == "swap":  # a DLL swap changes only the keys that depend on the DLL
            fsr = {k: v for k, v in backend.owned_ini_keys(opts).get("FSR", {}).items()
                   if k in ("Fsr4GpuSupport", "Fsr4Model")}
            owned.setdefault("FSR", {}).update(fsr)
            swap_only = {"FSR": dict(fsr)}
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
    wanted = copy.deepcopy(swap_only if swap_only is not None else owned)
    if swap_only is not None:  # the watermark default, once, only over 'auto'
        wm = backend.fresh_install_ini_defaults(opts).get("FSR", {}).get("Fsr4EnableWatermark")
        cur = before.get("FSR", "Fsr4EnableWatermark")
        if wm and wm != "auto" and (cur is None or cur.strip().lower() == "auto"):
            wanted["FSR"]["Fsr4EnableWatermark"] = wm
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
        if old.dll and old.dll != dll and not old.had_override and not _old_proxy_foreign(state, game):
            new = lo.faugus_apply(new, old.dll, None)
        new = lo.faugus_apply(new, dll, "n,b", {**env, **remove_env})
    else:
        new = cur
        if old.dll and old.dll != dll and not old.had_override and not _old_proxy_foreign(state, game):
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
