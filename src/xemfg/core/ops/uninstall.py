"""Exact removal from state: only files xemfg added or replaced are touched."""
from __future__ import annotations

from .. import ini as ini_mod
from .. import launchopts as lo
from ..context import Context
from ..launchers import faugus_writer, steam_writer
from ..models import Game
from ..plan import Plan, TextChange
from ..state import STATE_DIR, GameState, now
from .common import OpError

INI = "OptiScaler.ini"


def build_uninstall(ctx: Context, game: Game, mark_registry: bool = True) -> Plan:
    exe_dir = game.exe.parent
    st = GameState.load(exe_dir)
    if st is None:
        raise OpError(f"{game.title}: not installed by xemfg (no {STATE_DIR}/state.json)")
    plan = Plan(f"uninstall XeMFG from {game.title}", base=exe_dir)
    dirs = set()
    for rel, mf in sorted(st.files.items()):
        dest = exe_dir / rel
        if mf.action == "added":
            if dest.exists():
                plan.delete(dest)
            dirs.update(p for p in dest.parents if exe_dir in p.parents)
        elif mf.action == "replaced" and mf.backup:
            bk = exe_dir / mf.backup
            if bk.exists():
                plan.copy(bk, dest, note="restore original")
            else:
                plan.warnings.append(f"backup of {rel} is missing; leaving the current file")
    # ini: created -> delete; otherwise put back only the keys xemfg changed
    ip = exe_dir / INI
    if ip.exists():
        if st.ini.created:
            plan.delete(ip)
        else:
            text = ini_mod.read_text(ip)
            ini = ini_mod.Ini(text)
            for sec, kv in st.ini.original.items():
                for k, v in kv.items():
                    if v is not None:
                        ini.set(sec, k, v)
            if ini.text() != text:
                plan.write(ip, ini.text().encode("utf-8", errors="surrogateescape"), note="restore xemfg keys")
                plan.texts.append(TextChange(INI, ini_mod.unified_diff(text, ini.text(), INI)))
    # our state dir (state, backups, journals)
    sd = exe_dir / STATE_DIR
    for p in sorted(sd.rglob("*"), reverse=True) if sd.exists() else []:
        if p.is_file():
            plan.delete(p)
        else:
            dirs.add(p)
    dirs.add(sd)
    for d in sorted(dirs, key=lambda p: -len(p.parts)):
        plan.rmdir(d)

    _launch_removal(ctx, plan, game, st)

    def commit():
        if mark_registry:
            e = ctx.registry.get(game.key)
            if e:
                e.status, e.removed_at = "removed", now()
            ctx.registry.steam_queue.pop(game.appid or "", None)
            ctx.registry.save()

    plan.after.append(commit)
    return plan


def _launch_removal(ctx: Context, plan: Plan, game: Game, st: GameState) -> None:
    from .apply import current_launch_text

    ls = st.launch
    if not ls.target:
        return
    cur = current_launch_text(ctx, game) or ""
    env = {k: None for k in lo.xemfg_added_env(ls.env, ls.original)}
    apply_fn = lo.faugus_apply if ls.target == "faugus" else lo.steam_apply
    if ls.had_override:
        # the override was there before xemfg: leave it exactly as it is
        plan.notes.append(f"{ls.dll} was already overridden before xemfg; leaving that override")
        new = apply_fn(cur, ls.dll, lo.override_mode(cur, ls.dll), env) if lo.override_mode(cur, ls.dll) \
            else apply_fn(cur, ls.dll, None, env)
    else:
        new = apply_fn(cur, ls.dll, None, env)
    if new == cur:
        return
    plan.texts.append(TextChange(f"{ls.target.capitalize()} launch options", f"- {cur!r}\n+ {new!r}"))
    if ls.target == "faugus":
        plan.after.append(lambda: faugus_writer.write_launch_args(
            ctx.library().faugus.games_json, game.gameid, new, ctx.paths.backups_dir))
    elif ctx.steam_running():
        plan.warnings.append(f"Steam is running: set the launch options to {new!r} yourself, "
                             "or close Steam and run `xemfg launch-opts --apply-queued`")

        def queue():
            ctx.registry.steam_queue[game.appid] = {"options": new, "key": game.key, "title": game.title,
                                                    "queued_at": now()}
            ctx.registry.save()

        plan.after.append(queue)
    else:
        cfgs = ctx.library().steam.userdata_configs()
        plan.after.append(lambda: steam_writer.write_launch_options(cfgs[0], game.appid, new, ctx.paths.backups_dir))


def uninstall(ctx: Context, game: Game, dry_run=False, yes=False) -> bool:
    from .run import run_plan

    return run_plan(ctx, build_uninstall(ctx, game), dry_run, yes)
