"""Adopt an existing manual setup (xemfg-linux.sh) so update/repair/uninstall can manage it.

Game files are not touched; only .xemfg/state.json and the registry are written.
"""
from __future__ import annotations

from pathlib import Path

from .. import ini as ini_mod
from .. import launchopts as lo
from ..backends import get_backend
from ..backends.base import Options
from ..backends.xemfg import UPSCALER_DLL
from ..context import Context
from ..fsutil import sha256_or_none
from ..models import Game
from ..plan import Plan
from ..platform import dll_override_name
from ..state import GameState, IniState, LaunchState, ManagedFile, now
from .apply import _register, current_launch_text
from .common import OpError
from .health import inspect_unmanaged

CHOICE_BY_DIR = [(r"l-exact$", "exact-l"), (r"l-lossy$", "lossy-l"), (r"^exact$", "exact"), (r"^lossy$", "lossy")]


def _choice(d: str) -> str:
    import re

    return next(c for pat, c in CHOICE_BY_DIR if re.search(pat, d))


def build_adopt(ctx: Context, game: Game) -> Plan:
    exe_dir = game.exe.parent
    if GameState.load(exe_dir):
        raise OpError(f"{game.title} is already managed by xemfg")
    u = inspect_unmanaged(ctx, game)
    if u is None:
        raise OpError(f"{game.title}: no OptiScaler found to adopt")
    sid = u.matches_set
    if not sid:
        raise OpError(f"{game.title}: the installed files do not match a known set ({'; '.join(u.notes)}). "
                      "Use `xemfg install` to replace them.")
    xs = ctx.manifest.get(sid)
    ip = exe_dir / "OptiScaler.ini"
    if not ip.is_file():
        raise OpError(f"{game.title}: OptiScaler.ini is missing")
    i = ini_mod.Ini(ini_mod.read_text(ip))
    fg_input = i.get("FrameGen", "FGInput") or "FSRFG"
    try:
        dmfg = int(float(i.get("XeFG", "FramerateTargetDMFG") or ctx.config.dmfg_target))
    except ValueError:
        dmfg = ctx.config.dmfg_target
    fsr4, family = "none", ctx.config.gpu_family
    if u.fsr4_variant:
        _, family, d = u.fsr4_variant
        fsr4 = _choice(d)
    opts = Options(u.proxy, fg_input, fsr4, family, dmfg)
    backend = get_backend()

    files: dict[str, ManagedFile] = {}
    files[u.proxy] = _added(exe_dir, u.proxy, "optiscaler", backups=_script_backup(exe_dir, u.proxy))
    opti_dir = exe_dir / "OptiScaler"
    for p in sorted(opti_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(exe_dir).as_posix()
            comp = {"OptiScaler/plugins/MFGUnlocker.asi": "plugin", UPSCALER_DLL: "fsr4" if u.fsr4_variant else "nightly",
                    UPSCALER_DLL + ".orig": "fsr4-stock"}.get(rel, "nightly")
            files[rel] = _added(exe_dir, rel, comp)

    owned = backend.owned_ini_keys(opts)
    missing = [f"[{s}] {k}" for s, kv in owned.items() for k in kv if i.get(s, k) is None]
    keys = {s: {k: (i.get(s, k) if i.get(s, k) is not None else v) for k, v in kv.items()} for s, kv in owned.items()}
    ini_bak = _script_backup(exe_dir, "OptiScaler.ini")
    ist = IniState(created=ini_bak is None, backup=None, keys=keys, original={})
    if ini_bak:
        files_ini_note = f"OptiScaler.ini existed before; its backup {ini_bak} is kept as the original"
        ist.backup = ini_bak
    dll = dll_override_name(u.proxy)
    cur = current_launch_text(ctx, game) if game.launch_target else None
    ls = LaunchState(target=game.launch_target, dll=dll, env={}, had_override=False, original=None, written=cur)

    st = GameState(game_key=game.key, title=game.title, set=sid, options=opts.to_dict(), files=files, ini=ist,
                   launch=ls, adopted=True, history=[{"set": sid, "at": now(), "action": "adopt"}])
    plan = Plan(f"adopt the existing setup in {game.title} as set {sid}", base=exe_dir)
    plan.notes.append(f"OptiScaler {u.optiscaler_version} as {u.proxy}, MFG Unlocker {xs.mfgu_version}, "
                      f"FSR4 {u.fsr4 or 'stock'} -> options: input {fg_input}, fsr4 {fsr4}, target {dmfg}")
    plan.notes.append(f"records {len(files)} files as xemfg-managed (uninstall removes them; "
                      f"OptiScaler.ini is {'deleted' if ist.created else 'restored from its backup'})")
    if ini_bak:
        plan.notes.append(files_ini_note)
    if missing:
        plan.warnings.append(f"OptiScaler.ini lacks {', '.join(missing)}; `xemfg repair` will add them")
    if cur is not None and lo.override_mode(cur, dll) is None:
        plan.warnings.append(f"launch option {dll}=n,b is not set; `xemfg repair` will add it")
    plan.notes.append("writes .xemfg/state.json and the registry only; no game or launcher file changes")

    def commit():
        st.save(exe_dir)
        _register(ctx, game, st)

    plan.after.append(commit)
    return plan


def _added(exe_dir: Path, rel: str, comp: str, backups: str | None = None) -> ManagedFile:
    sha = sha256_or_none(exe_dir / rel) or ""
    if backups:
        return ManagedFile("replaced", sha, backups, sha256_or_none(exe_dir / backups), comp)
    return ManagedFile("added", sha, None, None, comp)


def _script_backup(exe_dir: Path, name: str) -> str | None:
    """xemfg-linux.sh kept <name>.bak-<timestamp>; the oldest is the original."""
    baks = sorted(exe_dir.glob(f"{name}.bak-*"))
    return baks[0].relative_to(exe_dir).as_posix() if baks else None
