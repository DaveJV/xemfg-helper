"""xemfg command line. Thin layer over xemfg.core; `xemfg` with no arguments opens the TUI."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .core.context import Context, NoGame
from .core.models import Game, Tier
from .core.ops.common import OpError

app = typer.Typer(add_completion=False, no_args_is_help=False, rich_markup_mode=None,
                  help="Install and maintain XeMFG (MFG Unlocker + dashdogy OptiScaler) for Proton games.")
manifest_app = typer.Typer(help="Manage the set manifest.")
config_app = typer.Typer(help="xemfg settings.")
app.add_typer(manifest_app, name="manifest")
app.add_typer(config_app, name="config")
con = Console(highlight=False)
err = Console(stderr=True, highlight=False)

TIER_STYLE = {Tier.NATIVE_FG: "green", Tier.UPSCALER: "yellow", Tier.DLSSG_ONLY: "magenta",
              Tier.UNKNOWN: "dim", Tier.INCOMPATIBLE: "red"}
HEALTH_STYLE = {"ok": "green", "outdated": "cyan", "drifted": "yellow", "broken": "red", "missing": "red",
                "removed": "dim"}


class TerminalUI:
    def __init__(self, yes: bool = False):
        self.yes = yes
        self._bar = None

    def confirm(self, message: str) -> bool:
        if self.yes:
            return True
        if not sys.stdin.isatty():
            err.print(f"{message} -> no (not a terminal; pass --yes)")
            return False
        return typer.confirm(message, default=False)

    def pick(self, prompt: str, options: list[str]) -> int | None:
        if not sys.stdin.isatty():
            err.print(f"{prompt} (ambiguous; options: {', '.join(options)})")
            return None
        con.print(prompt)
        for i, o in enumerate(options, 1):
            con.print(f"  [bold]{i}[/] {o}")
        ans = typer.prompt("number (empty to cancel)", default="", show_default=False)
        if not ans.strip().isdigit() or not 1 <= int(ans) <= len(options):
            return None
        return int(ans) - 1

    def log(self, message: str) -> None:
        con.print(message, markup=False)

    def warn(self, message: str) -> None:
        err.print(f"[yellow]warning:[/] {message}")

    def progress(self, label: str, done: int, total: int | None) -> None:
        if total:
            pct = done * 100 // total
            end = "\n" if done >= total else "\r"
            err.print(f"  {label}: {pct:3d}% of {total >> 20} MB", end=end)


def ctx_for(yes: bool = False) -> Context:
    return Context.default(TerminalUI(yes))


def fail(msg: str, code: int = 1):
    err.print(f"[red]error:[/] {msg}", markup=True)
    raise typer.Exit(code)


def guarded(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (OpError, NoGame) as e:
            fail(str(e))
        except Exception as e:  # show a clean message for expected runtime errors
            from .core.cache import ExtractError, HashMismatch
            from .core.manifest import ManifestError
            from .core.net import FetchError
            from .core.upstream import UpstreamError

            if isinstance(e, (HashMismatch, ExtractError, FetchError, ManifestError, UpstreamError)):
                fail(f"{type(e).__name__}: {e}")
            raise

    return wrapper


def installed_games(ctx: Context) -> list[Game]:
    lib = ctx.library()
    out = []
    for e in ctx.registry.active():
        g = lib.by_key(e.key)
        if g is None:
            from .core.discovery.roots import game_from_path
            g = game_from_path(Path(e.exe))
            if g is not None:
                g.key, g.title = e.key, e.title
        if g is not None:
            out.append(g)
    return out


def pick_installed(ctx: Context, query: Optional[str]) -> Game:
    games = installed_games(ctx)
    if not games and not query:
        raise OpError("nothing is installed yet")
    if query:
        try:
            return ctx.resolve(query, games) if games else ctx.resolve(query)
        except NoGame:
            return ctx.resolve(query)
    return ctx.resolve(None, games, prompt="Which installed game?")


# ---------------------------------------------------------------------------------------- scan
@app.command()
@guarded
def scan(new: bool = typer.Option(False, "--new", help="only games added since the last scan"),
         all_: bool = typer.Option(False, "--all", help="include tools and games without an exe")):
    """Library with compatibility tiers and install status (read-only)."""
    ctx = ctx_for()
    lib = ctx.library(snapshot=True)
    games = lib.new_games if new else lib.games
    reg = ctx.registry
    t = Table(box=None, pad_edge=False)
    for c in ("", "game", "source", "tier", "input", "status", "notes"):
        t.add_column(c, overflow="fold")
    for g in sorted(games, key=lambda g: (ctx.compat(g).tier.rank, g.title.lower())):
        r = ctx.compat(g)
        e = reg.get(g.key)
        status = ""
        if e and e.status == "installed":
            status = f"xemfg {e.set}"
        elif r.optiscaler_proxy:
            status = "OptiScaler (not by xemfg)"
        notes = "; ".join(r.reasons + r.warnings + r.flags)
        mark = "[bold cyan]NEW[/]" if g.key in lib.new_keys else ""
        t.add_row(mark, g.title, g.source.value + (f":{g.appid}" if g.appid else ""),
                  f"[{TIER_STYLE[r.tier]}]{r.tier.label}[/]", r.fg_input or "", status, notes)
    con.print(t)
    if new and not games:
        con.print("no new games since the last scan")
    for e in lib.errors:
        err.print(f"[yellow]{e}[/]")
    if lib.removed:
        con.print(f"gone since the last scan: {', '.join(x['title'] for x in lib.removed)}")


# ---------------------------------------------------------------------------------------- install
def _request(input_, proxy, fsr4, dmfg_target, allow_anticheat, mode="install"):
    from .core.ops.apply import Request

    if input_ and input_ not in ("FSRFG", "FSRFG30", "Upscaler"):
        fail("--input must be FSRFG, FSRFG30 or Upscaler")
    if fsr4 and fsr4 not in ("exact-l", "lossy-l", "exact", "lossy", "none"):
        fail("--fsr4 must be exact-l, lossy-l, exact, lossy or none")
    return Request(fg_input=input_, proxy=proxy, fsr4=fsr4, dmfg_target=dmfg_target,
                   allow_anticheat=allow_anticheat, force_tier=bool(input_), mode=mode,
                   fsr4_explicit=bool(fsr4))


@app.command()
@guarded
def install(game: Optional[str] = typer.Argument(None, help="fuzzy name, Steam appid or path"),
            all_compatible: bool = typer.Option(False, "--all-compatible", help="every Native FG game not installed yet"),
            input_: Optional[str] = typer.Option(None, "--input", help="FSRFG | FSRFG30 | Upscaler (default: from the tier)"),
            proxy: Optional[str] = typer.Option(None, "--proxy", help="proxy DLL name (default dxgi.dll)"),
            fsr4: Optional[str] = typer.Option(None, "--fsr4", help="exact-l | lossy-l | exact | lossy | none"),
            dmfg_target: Optional[int] = typer.Option(None, "--dmfg-target", help="Dynamic MFG target FPS (default 171)"),
            allow_anticheat: bool = typer.Option(False, "--allow-anticheat",
                                                 help="DANGER: install despite anti-cheat (offline play only)"),
            dry_run: bool = typer.Option(False, "--dry-run", help="show what would change, write nothing"),
            yes: bool = typer.Option(False, "--yes", "-y", help="do not ask for confirmation")):
    """Install XeMFG into a game (or every Native FG game)."""
    from .core.ops import install as ops

    ctx = ctx_for(yes)
    req = _request(input_, proxy, fsr4, dmfg_target, allow_anticheat)
    if all_compatible:
        games = ops.compatible_games(ctx)
        if not games:
            con.print("no Native FG games without XeMFG found")
            return
        con.print(f"Native FG games without XeMFG: {', '.join(g.title for g in games)}")
        results = ops.install_batch(ctx, games, req, dry_run=dry_run, yes=yes,
                                    on_progress=lambda i, n, g: con.print(f"[{i + 1}/{n}] {g.title}"))
        for r in results:
            con.print(f"  {'[green]ok[/]' if r.ok else '[red]failed[/]'} {r.game.title}: {r.message}")
        return
    g = ctx.resolve(game)
    ops.install(ctx, g, req, dry_run=dry_run, yes=yes)


@app.command()
@guarded
def adopt(game: Optional[str] = typer.Argument(None),
          dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Take over an existing manual setup (e.g. from xemfg-linux.sh) without changing game files."""
    from .core.ops.adopt import build_adopt
    from .core.ops.run import run_plan

    ctx = ctx_for(yes)
    run_plan(ctx, build_adopt(ctx, ctx.resolve(game)), dry_run, yes)


# ---------------------------------------------------------------------------------------- list / status
@app.command("list")
@guarded
def list_(all_: bool = typer.Option(False, "--all", help="include uninstalled entries")):
    """Registered installs with health (read-only)."""
    from .core.ops.health import list_health

    ctx = ctx_for()
    hs = list_health(ctx, include_removed=all_)
    if not hs:
        con.print("no installs registered (xemfg install <game>, or xemfg adopt <game> for a manual setup)")
        return
    t = Table(box=None)
    for c in ("game", "health", "set", "proxy", "input", "launcher", "issue"):
        t.add_column(c, overflow="fold")
    for h in hs:
        e = h.entry
        first = next((f.message for f in h.findings if f.level != "info"), "")
        t.add_row(h.title, f"[{HEALTH_STYLE[h.status]}]{h.status}[/]", e.set, e.options.get("proxy", ""),
                  e.options.get("fg_input", ""), e.launch_target or "-", first)
    con.print(t)


@app.command()
@guarded
def status(game: Optional[str] = typer.Argument(None)):
    """Versions, per-file hash check, runner and launch options for one game (read-only)."""
    from .core.fsutil import sha256_or_none
    from .core.ops.apply import current_launch_text
    from .core.ops.health import check_entry, inspect_unmanaged
    from .core.state import GameState

    ctx = ctx_for()
    g = ctx.resolve(game)
    r = ctx.compat(g)
    con.print(f"[bold]{g.title}[/]  ({g.source.value}{':' + (g.appid or g.gameid or '') if (g.appid or g.gameid) else ''})")
    con.print(f"  exe       {g.exe}  ({g.exe_reason})")
    con.print(f"  runner    {g.runner or '-'}" + (f"   prefix {g.prefix}" if g.prefix else ""))
    con.print(f"  tier      [{TIER_STYLE[r.tier]}]{r.tier.label}[/] {r.fg_input or ''} - {'; '.join(r.reasons)}")
    for x in r.warnings + r.flags:
        con.print(f"            {x}")
    launch = current_launch_text(ctx, g) if g.launch_target else None
    con.print(f"  launch    {launch!r}" if launch is not None else "  launch    (no launcher entry)")
    st = GameState.load(g.exe.parent) if g.exe else None
    e = ctx.registry.get(g.key)
    if st is None:
        u = inspect_unmanaged(ctx, g) if g.exe else None
        if u is None:
            con.print("  xemfg     not installed")
            return
        con.print("  xemfg     [yellow]unmanaged[/]: OptiScaler was installed without xemfg")
        con.print(f"  OptiScaler {u.optiscaler_version} as {u.proxy}")
        if u.matches_set:
            xs = ctx.manifest.get(u.matches_set)
            con.print(f"  matches   set [green]{u.matches_set}[/] (dashdogy {xs.optiscaler.tag}, MFG Unlocker "
                      f"{xs.mfgu_version}" + (f", cyboman {u.fsr4_variant[2]} {u.fsr4_variant[1]}" if u.fsr4_variant else "") + ")")
            if u.matches_set != ctx.manifest.current:
                con.print(f"            current set is {ctx.manifest.current}")
            con.print(f"  next      `xemfg adopt {game or ''}` lets xemfg manage it (writes only .xemfg/ and the registry)")
        for n in u.notes:
            con.print(f"            {n}")
        return
    xs = ctx.manifest.get(st.set)
    con.print(f"  set       {st.set} (MFG Unlocker {xs.mfgu_version}, {xs.optiscaler.repo}@{xs.optiscaler.tag}, "
              f"{xs.nightly.tag}, cyboman {xs.cyboman_tag}){'  [adopted]' if st.adopted else ''}")
    con.print(f"  options   " + ", ".join(f"{k}={v}" for k, v in st.options.items()))
    if st.previous_sets:
        con.print(f"  previous  {', '.join(st.previous_sets)}")
    bad = 0
    for rel, mf in sorted(st.files.items()):
        cur = sha256_or_none(g.exe.parent / rel)
        ok = cur == mf.sha256
        bad += not ok
        if not ok or mf.component in ("optiscaler", "plugin", "fsr4"):
            con.print(f"  {'[green]ok[/]  ' if ok else '[red]BAD[/] '}     {rel} ({mf.action}{', ' + mf.component if mf.component else ''})")
    con.print(f"  files     {len(st.files) - bad}/{len(st.files)} match the recorded hashes")
    if e:
        h = check_entry(ctx, e)
        con.print(f"  health    [{HEALTH_STYLE[h.status]}]{h.status}[/]")
        for f in h.findings:
            con.print(f"            {f.level}: {f.message}" + (f"  -> {f.fix}" if f.fix else ""))


# ---------------------------------------------------------------------------------------- check / update / rollback
@app.command()
@guarded
def check():
    """Latest upstream pairing and cyboman vs the local manifest and each install (read-only)."""
    from .core.ops.check import check as do_check

    ctx = ctx_for()
    r = do_check(ctx, log=lambda s: con.print(f"  {s}", style="dim"))
    if r.error:
        fail(f"could not check upstream: {r.error}")
    if r.up_to_date:
        con.print(f"[green]up to date[/]: manifest set {r.local_current} is the current upstream pairing")
    else:
        con.print(f"[cyan]update available[/] (local set {r.local_current}):")
        for d in r.differences:
            con.print(f"  {d}")
        con.print("  run `xemfg update --all` (refreshes the manifest first)")
    for title, s in r.outdated_installs:
        con.print(f"  outdated install: {title} on {s}")


@manifest_app.command("refresh")
@guarded
def manifest_refresh():
    """Resolve the current pairing from upstream and save it as the current set."""
    from .core.ops.check import refresh

    ctx = ctx_for()
    sid, changed = refresh(ctx, log=lambda s: con.print(f"  {s}", style="dim"))
    con.print(f"current set: {sid}" + (" (new)" if changed else " (unchanged)"))


@manifest_app.command("show")
@guarded
def manifest_show():
    """Print the sets in the manifest."""
    ctx = ctx_for()
    m = ctx.manifest
    for sid in m.ordered_ids():
        s = m.sets[sid]
        con.print(f"{'*' if sid == m.current else ' '} {sid:24} {s.notes}")


@app.command()
@guarded
def update(game: Optional[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all"),
           no_refresh: bool = typer.Option(False, "--no-refresh", help="do not refresh the manifest first"),
           dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Refresh the manifest if needed, back up, and move installs to the current set."""
    from .core.ops import install as ops
    from .core.ops.check import refresh

    ctx = ctx_for(yes)
    if not no_refresh and not dry_run:
        try:
            sid, changed = refresh(ctx, log=lambda s: con.print(f"  {s}", style="dim"))
            ctx.reload_manifest()
            con.print(f"manifest: current set {sid}{' (new)' if changed else ''}")
        except Exception as e:
            err.print(f"[yellow]manifest refresh failed ({e}); using the local manifest[/]")
    games = installed_games(ctx) if all_ else [pick_installed(ctx, game)]
    for g in games:
        r = ops.update(ctx, g, dry_run=dry_run, yes=yes)
        con.print(f"{g.title}: {r.message or 'not updated'}")


@app.command()
@guarded
def rollback(game: Optional[str] = typer.Argument(None), to: Optional[str] = typer.Option(None, "--to", help="set id"),
             dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Return a game to a previously installed set."""
    from .core.ops import install as ops

    ctx = ctx_for(yes)
    g = pick_installed(ctx, game)
    r = ops.rollback(ctx, g, to, dry_run=dry_run, yes=yes)
    con.print(f"{g.title}: {r.message or 'not changed'}")


@app.command()
@guarded
def uninstall(game: Optional[str] = typer.Argument(None),
              dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Remove exactly what xemfg installed and its launch option."""
    from .core.ops import uninstall as un

    ctx = ctx_for(yes)
    g = pick_installed(ctx, game)
    if un.uninstall(ctx, g, dry_run=dry_run, yes=yes):
        con.print(f"{g.title}: uninstalled")


# ---------------------------------------------------------------------------------------- doctor / repair / diagnose
@app.command()
@guarded
def doctor():
    """Check every registered install for problems (read-only)."""
    from .core.ops.health import list_health
    from .core.ops.prune import find_relocation

    ctx = ctx_for()
    hs = list_health(ctx)
    if not hs:
        con.print("no installs registered")
    problems = 0
    for h in hs:
        con.print(f"[{HEALTH_STYLE[h.status]}]{h.status:8}[/] {h.title}")
        for f in h.findings:
            con.print(f"           {f.level}: {f.message}" + (f"\n             fix: {f.fix}" if f.fix else ""))
        if h.status == "missing":
            new, how = find_relocation(ctx, h.entry)
            if new:
                con.print(f"           found at {new.parent} ({how}); `xemfg prune` offers to re-point it")
        problems += h.status not in ("ok",)
    if ctx.registry.steam_queue:
        con.print(f"queued Steam launch options: {len(ctx.registry.steam_queue)} (close Steam, then "
                  "`xemfg launch-opts --apply-queued`)")
    if hs and not problems:
        con.print("[green]all installs healthy[/]")


@app.command()
@guarded
def repair(game: Optional[str] = typer.Argument(None), all_: bool = typer.Option(False, "--all"),
           reset_ini: bool = typer.Option(False, "--reset-ini", help="back up the ini, start from the paired nightly's"),
           clean: bool = typer.Option(False, "--clean", help="full rollback, then fresh install on the current set"),
           dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Restore managed files, xemfg's ini keys and launch options to the recorded set."""
    from .core.ops import install as ops

    ctx = ctx_for(yes)
    games = installed_games(ctx) if all_ else [pick_installed(ctx, game)]
    for g in games:
        try:
            r = ops.repair(ctx, g, reset_ini=reset_ini, clean=clean, dry_run=dry_run, yes=yes)
            con.print(f"{g.title}: {r.message or 'not changed'}")
        except OpError as e:
            err.print(f"[red]{g.title}:[/] {e}")


@app.command()
@guarded
def diagnose(game: Optional[str] = typer.Argument(None)):
    """Read RTXMFG/OptiScaler logs and crash dumps; map findings to fixes (read-only)."""
    from .core.ops.diagnose import diagnose as do

    ctx = ctx_for()
    g = ctx.resolve(game)
    d = do(ctx, g)
    con.print(f"[bold]{g.title}[/]")
    for k, v in d.facts.items():
        con.print(f"  {k:20} {v}")
    if d.stack:
        con.print("  fault stack:")
        for line in d.stack:
            con.print(f"    {line}", markup=False)
    if not d.findings:
        con.print("[green]  no problems found in the logs[/]")
    for f in d.findings:
        con.print(f"  [{HEALTH_STYLE.get(f.level, 'yellow')}]{f.level}[/]: {f.message}")
        if f.fix:
            con.print(f"     fix: {f.fix}")


# ---------------------------------------------------------------------------------------- prune / gc
@app.command()
@guarded
def prune(dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Drop entries whose folders are gone; re-point moved games first."""
    from .core.ops.prune import drop, plan_prune, repoint

    ctx = ctx_for(yes)
    items = plan_prune(ctx)
    if not items:
        con.print("nothing to prune")
        return
    for it in items:
        if it.relocated_to:
            con.print(f"{it.entry.title}: moved -> {it.relocated_to.parent} ({it.how})")
            if not dry_run and ctx.ui.confirm("re-point the registry entry there?"):
                repoint(ctx, it)
                con.print("  re-pointed")
        else:
            con.print(f"{it.entry.title}: folder gone ({it.entry.exe_dir}){' - ' + it.how if it.how else ''}")
            if not dry_run and ctx.ui.confirm("drop this registry entry?"):
                drop(ctx, it)
                con.print("  dropped")


@app.command()
@guarded
def gc(dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Delete unused cache sets, stale downloads and backups xemfg no longer needs."""
    from .core.ops.gc import build_gc
    from .core.ops.run import run_plan

    ctx = ctx_for(yes)
    res = build_gc(ctx)
    con.print(f"keeping sets: {', '.join(sorted(res.kept_sets))}; frees {res.freed >> 20} MB")
    run_plan(ctx, res.plan, dry_run, yes)


# ---------------------------------------------------------------------------------------- launch options / config
@app.command("launch-opts")
@guarded
def launch_opts(game: Optional[str] = typer.Argument(None),
                apply_queued: bool = typer.Option(False, "--apply-queued", help="write queued Steam launch options now")):
    """Print the launch options (Faugus lines and the Steam %command% form)."""
    from .core.ops import launch

    ctx = ctx_for()
    if apply_queued:
        done = launch.apply_queued(ctx)
        con.print("\n".join(done) if done else "no queued Steam launch options")
        return
    g = ctx.resolve(game)
    lines, steam = launch.show(ctx, g)
    con.print("Faugus (one per line):")
    for line in lines:
        con.print(f"  {line}", markup=False)
    con.print("Steam:")
    con.print(f"  {steam}", markup=False)


@config_app.command("add-root")
@guarded
def config_add_root(directory: Path):
    """Add a folder with games outside Steam and Faugus."""
    ctx = ctx_for()
    d = directory.expanduser().resolve()
    if not d.is_dir():
        fail(f"{d} is not a folder")
    if str(d) not in ctx.config.roots:
        ctx.config.roots.append(str(d))
        ctx.save_config()
    con.print(f"roots: {', '.join(ctx.config.roots)}")


@config_app.command("show")
def config_show():
    """Print the current settings."""
    ctx = ctx_for()
    for k, v in vars(ctx.config).items():
        con.print(f"{k} = {v!r}")


@config_app.command("set")
@guarded
def config_set(key: str, value: str):
    """Set dmfg_target, default_proxy, fsr4 or gpu_family."""
    ctx = ctx_for()
    allowed = {"dmfg_target": int, "default_proxy": str, "fsr4": str, "gpu_family": str}
    if key not in allowed:
        fail(f"unknown key {key} (one of {', '.join(allowed)})")
    setattr(ctx.config, key, allowed[key](value))
    ctx.save_config()
    con.print(f"{key} = {getattr(ctx.config, key)!r}")


@app.callback(invoke_without_command=True)
def main_cb(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        from .tui.app import run_tui

        run_tui()


def main():
    app()


if __name__ == "__main__":
    main()
