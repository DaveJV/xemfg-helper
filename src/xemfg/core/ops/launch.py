"""Launch options: show them, and apply the Steam queue once Steam is closed."""
from __future__ import annotations

from .. import launchopts as lo
from ..backends import get_backend
from ..backends.base import Options
from ..context import Context
from ..launchers import steam_writer
from ..models import Game
from ..platform import dll_override_name
from ..state import GameState
from .common import OpError


def show(ctx: Context, game: Game) -> tuple[list[str], str]:
    st = GameState.load(game.exe.parent) if game.exe else None
    if st:
        opts = Options.from_dict(st.options)
    else:
        c = ctx.config
        opts = Options(c.default_proxy, "FSRFG", c.fsr4, c.gpu_family, c.dmfg_target)
    return lo.display_lines(dll_override_name(opts.proxy), get_backend().launch_env(opts))


def apply_queued(ctx: Context) -> list[str]:
    if ctx.steam_running():
        raise OpError("Steam is still running; close it first (it rewrites localconfig.vdf on exit)")
    q = ctx.registry.steam_queue
    if not q:
        return []
    steam = ctx.library().steam
    cfgs = steam.userdata_configs() if steam else []
    if not cfgs:
        raise OpError("no Steam user config found")
    done = []
    for appid, item in list(q.items()):
        steam_writer.write_launch_options(cfgs[0], appid, item["options"], ctx.paths.backups_dir)
        done.append(f"{item.get('title', appid)}: {item['options']}")
        del q[appid]
    ctx.registry.save()
    return done
