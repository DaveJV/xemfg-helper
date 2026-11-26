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
fsr4_app = typer.Typer(help="The FSR4 DLL in OptiScaler's folder: list the choices, swap, see the kept copies.")
app.add_typer(fsr4_app, name="fsr4")
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
        notes = "; ".join(r.reasons + r.warnings + r.flags + _fsr4_summary(ctx, g, r))
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


def _fsr4_summary(ctx: Context, g: Game, r) -> list[str]:
    """Short FSR4 note for scan: only for games where an FSR4 DLL is present or OptiScaler is installed."""
    from .core.ops.fsr4status import report

    if g.exe is None or not r.installable and not r.optiscaler_proxy:
        return []
    try:
        rep = report(ctx, g)
    except Exception as e:  # never let a bad file break the library listing
        return [f"FSR4: could not inspect ({e})"]
    return [rep.summary] if any(a.runtime.dll for a in rep.apis) else []


def _print_fsr4(ctx: Context, g: Game) -> None:
    from .core.ops.fsr4status import report

    rep = report(ctx, g)
    style = {"current": "green", "older": "cyan", "wrong-gpu": "red", "cyboman": "green", "none": "yellow"}
    con.print(f"  gpu       {rep.gpu_note}")
    for w in rep.warnings:
        con.print(f"            [yellow]{w}[/]")
    for a in rep.apis:
        d = a.runtime.dll
        where = f" [{d.rel}]" if d else ""
        con.print(f"  fsr4 {a.api:4} [{style.get(a.verdict, 'white')}]{a.message}[/]{where}")
        con.print(f"            why this file: {a.runtime.how}", markup=False)
    for d in rep.unused:
        con.print(f"            game's own {d.rel}: {d.label()} - not loaded while OptiScaler has its own copy",
                  markup=False)
    for d in rep.dlls:
        if d.role == "combined":
            con.print(f"            {d.rel}: {d.label()}; left alone (it has no FSR4)", markup=False)
    if rep.driver_note:
        con.print(f"            {rep.driver_note}", markup=False)
    con.print(f"            {rep.log_note}", markup=False)


# ---------------------------------------------------------------------------------------- install
FSR4_HELP = ("official (newest for your GPU, the default) | official:<version> | cyboman:<variant> "
             "(exact, lossy, 4.1.2l-exact, 4.1.2l-lossy) | original | keep | nightly. `xemfg fsr4 list` shows them")


def _check_fsr4(value):
    from .core.fsr4choice import ChoiceError, parse

    if value:
        try:
            parse(value)
        except ChoiceError as e:
            fail(str(e))


def _check_proxy(name):
    from .core.platform import PROXY_NAMES

    if name and name.lower() not in PROXY_NAMES:
        fail(f"--proxy must be one of {', '.join(PROXY_NAMES)}")
    return name.lower() if name else None


def _request(input_, proxy, fsr4, dmfg_target, allow_anticheat, mode="install", replace_kept=False):
    from .core.ops.apply import Request

    proxy = _check_proxy(proxy)

    if input_ and input_ not in ("FSRFG", "FSRFG30", "Upscaler"):
        fail("--input must be FSRFG, FSRFG30 or Upscaler")
    _check_fsr4(fsr4)
    return Request(fg_input=input_, proxy=proxy, fsr4=fsr4, dmfg_target=dmfg_target,
                   allow_anticheat=allow_anticheat, force_tier=bool(input_), mode=mode,
                   fsr4_explicit=bool(fsr4), replace_kept=replace_kept)


@app.command()
@guarded
def install(game: Optional[str] = typer.Argument(None, help="fuzzy name, Steam appid or path"),
            all_compatible: bool = typer.Option(False, "--all-compatible", help="every Native FG game not installed yet"),
            input_: Optional[str] = typer.Option(None, "--input", help="FSRFG | FSRFG30 | Upscaler (default: from the tier)"),
            proxy: Optional[str] = typer.Option(None, "--proxy", help="name OptiScaler is loaded under: dxgi.dll "
                                                "(default), winmm.dll, version.dll, dbghelp.dll, d3d12.dll, "
                                                "wininet.dll or winhttp.dll; asks when another mod has it"),
            fsr4: Optional[str] = typer.Option(None, "--fsr4", help=FSR4_HELP),
            replace_kept: bool = typer.Option(False, "--replace-kept",
                                              help="replace an FSR4 DLL you put into OptiScaler's folder yourself"),
            dmfg_target: Optional[int] = typer.Option(None, "--dmfg-target", help="Dynamic MFG target FPS (default 171)"),
            allow_anticheat: bool = typer.Option(False, "--allow-anticheat",
                                                 help="DANGER: install despite anti-cheat (offline play only)"),
            dry_run: bool = typer.Option(False, "--dry-run", help="show what would change, write nothing"),
            yes: bool = typer.Option(False, "--yes", "-y", help="do not ask for confirmation")):
    """Install XeMFG into a game (or every Native FG game)."""
    from .core.ops import install as ops

    ctx = ctx_for(yes)
    req = _request(input_, proxy, fsr4, dmfg_target, allow_anticheat, replace_kept=replace_kept)
    req.auto_proxy = yes or not sys.stdin.isatty()  # no one to ask: the first free name, shown in the plan
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
    if not fsr4 and not yes and sys.stdin.isatty():
        from .core.ops.fsr4swap import pick

        try:
            chosen = pick(ctx, g, for_install=True)
        except OpError as e:
            err.print(f"[yellow]{e}[/]")
            chosen = None
        else:
            if chosen is None:
                fail("no FSR4 DLL chosen")
        if chosen:
            req.fsr4, req.fsr4_explicit = chosen, chosen != "keep"
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
    from .core.migrate import describe_fsr4

    for c in ("game", "health", "set", "fsr4 dll", "proxy", "input", "launcher", "issue"):
        t.add_column(c, overflow="fold", no_wrap=c == "game")
    for h in hs:
        e = h.entry
        first = next((f.message for f in h.findings if f.level != "info"), "")
        t.add_row(h.title, f"[{HEALTH_STYLE[h.status]}]{h.status}[/]", e.set, describe_fsr4(e.components.get("fsr4"), short=True),
                  e.options.get("proxy", ""), e.options.get("fg_input", ""), e.launch_target or "-", first)
    con.print(t)


@app.command()
@guarded
def status(game: Optional[str] = typer.Argument(None)):
    """Versions, per-file hash check, runner and launch options for one game (read-only)."""
    from .core.fsutil import sha256_or_none
    from .core.ops.apply import current_launch_text
    from .core.ops.health import check_entry, inspect_unmanaged

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
    if g.exe:
        _print_fsr4(ctx, g)
    st = ctx.state(g.exe.parent) if g.exe else None
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
    from .core.migrate import describe_components

    for i, line in enumerate(describe_components(st.components)):
        con.print(f"  {'components' if i == 0 else '':9} {line}")
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
ROW_STYLE = {"current": "green", "update": "cyan", "pinned": "yellow", "info": "white"}


def _games(ctx: Context, games: Optional[list[str]], all_: bool) -> list[Game]:
    if all_:
        return installed_games(ctx)
    if not games:
        return [pick_installed(ctx, None)]
    return [pick_installed(ctx, q) for q in games]


@app.command()
@guarded
def check():
    """Each component of each install: installed vs newest upstream (read-only)."""
    from .core.ops.check import check as do_check

    ctx = ctx_for()
    r = do_check(ctx, log=lambda s: con.print(f"  {s}", style="dim"))
    if r.error:
        err.print(f"[yellow]could not read upstream ({r.error}); comparing with the local manifest[/]")
    elif r.up_to_date:
        con.print(f"[green]manifest up to date[/]: {r.local_current} is the current upstream pairing")
    else:
        con.print(f"[cyan]upstream has newer versions[/] (local pairing {r.local_current}):")
        for d in r.differences:
            con.print(f"  {d}")
    if r.newest_nightly:
        paired = r.latest.nightly.tag if r.latest else ctx.manifest.pairings[ctx.manifest.current].nightly
        if r.newest_nightly != paired:
            con.print(f"  newest OptiScaler nightly: {r.newest_nightly} (MFG Unlocker pairs with {paired}; "
                      f"a newer one is an unverified mix: `xemfg manifest add nightly {r.newest_nightly}`)")
    if r.newest_sdk and not r.sdk_known:
        con.print(f"  [cyan]AMD released FSR SDK {r.newest_sdk}[/]; xemfg's official DLL list does not have it yet")
    for gc_ in r.games:
        con.print(f"[bold]{gc_.title}[/]")
        t = Table(box=None, pad_edge=False, show_header=False)
        for _ in range(4):
            t.add_column(overflow="fold")
        for row in gc_.rows:
            t.add_row(f"  {row.component}", row.installed, row.newest,
                      f"[{ROW_STYLE[row.status]}]{row.status}[/]")
        con.print(t)
        for why in gc_.unverified:
            con.print(f"  [yellow]unverified combination:[/] {why}")
    if any(row.status == "update" for g in r.games for row in g.rows):
        con.print("`xemfg update --all` updates everything not pinned; `--only nightly,mfgu` picks components")
    from .core.ops.fsr4status import report

    for g in installed_games(ctx):
        if g.exe is None:
            continue
        for a in report(ctx, g).apis:
            if a.verdict in ("older", "wrong-gpu", "none"):
                con.print(f"  FSR4 {g.title} ({a.api}): {a.message}")


@manifest_app.command("refresh")
@guarded
def manifest_refresh():
    """Resolve the current pairing from upstream and save it as the current set."""
    from .core.ops.check import refresh

    ctx = ctx_for()
    sid, changed = refresh(ctx, log=lambda s: con.print(f"  {s}", style="dim"))
    con.print(f"current set: {sid}" + (" (new)" if changed else " (unchanged)"))


@manifest_app.command("add")
@guarded
def manifest_add(component: str = typer.Argument(..., help="nightly | optiscaler | mfgu | cyboman | fsr4"),
                 tag: str = typer.Argument(..., help="release tag, e.g. nightly-20261126, dll-2026-11-05, or an "
                                                     "AMD FSR SDK tag (v2.5.0) for fsr4")):
    """Resolve one release of one component from upstream (hashes included) so you can switch to it."""
    from .core.ops.components import UPDATABLE, parse_components
    from .core.upstream import Upstream, add_component

    ctx = ctx_for()
    [kind] = parse_components([component], UPDATABLE)
    spec = Upstream(ctx.fetcher).component(kind, tag, log=lambda s: con.print(f"  {s}", style="dim"))
    m = ctx.manifest
    v, added = add_component(m, kind, spec)
    if added:
        m.save(ctx.paths.manifest_file)
    how = f"xemfg fsr4 set <game> official:{v}" if kind == "fsr4" else f"xemfg update <game> --only {kind} --to {v}"
    con.print(f"{kind} {v}: {'added' if added else 'already known'}. Use it with `{how}`.")


@manifest_app.command("show")
@guarded
def manifest_show():
    """Print the pairings and every known version of each component."""
    ctx = ctx_for()
    m = ctx.manifest
    con.print("pairings (combinations upstream documents):")
    for sid in m.ordered_ids():
        s = m.sets[sid]
        con.print(f"{'*' if sid == m.current else ' '} {sid:24} {s.notes}")
    con.print("components:")
    for kind in ("nightly", "optiscaler", "mfgu", "cyboman"):
        con.print(f"  {kind:10} {', '.join(m.versions(kind))}")


@app.command()
@guarded
def update(games: Optional[list[str]] = typer.Argument(None, help="one or more games (default: pick one)"),
           all_: bool = typer.Option(False, "--all", help="every install"),
           only: Optional[str] = typer.Option(None, "--only",
                                              help="components to update: nightly,optiscaler,mfgu,cyboman"),
           to: Optional[str] = typer.Option(None, "--to", help="version for the one component in --only"),
           allow_unverified: bool = typer.Option(False, "--allow-unverified",
                                                 help="install a mix upstream does not document without asking"),
           no_refresh: bool = typer.Option(False, "--no-refresh", help="do not refresh the manifest first"),
           dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Move components to the newest versions. Pinned components stay; --only picks components."""
    from .core.ops import install as ops
    from .core.ops.check import refresh
    from .core.ops.components import parse_components

    comps = parse_components(only)
    if to and len(comps) != 1:
        fail("--to needs exactly one component in --only")
    ctx = ctx_for(yes)
    if not no_refresh and not dry_run and not to:
        try:
            sid, changed = refresh(ctx, log=lambda s: con.print(f"  {s}", style="dim"))
            ctx.reload_manifest()
            con.print(f"manifest: current pairing {sid}{' (new)' if changed else ''}")
        except Exception as e:
            err.print(f"[yellow]manifest refresh failed ({e}); using the local manifest[/]")
    for g in _games(ctx, games, all_):
        try:
            r = ops.update(ctx, g, dry_run=dry_run, yes=yes, only=comps or None,
                           to={comps[0]: to} if to else None, allow_unverified=allow_unverified)
            con.print(f"{g.title}: {r.message or 'not updated'}")
        except OpError as e:
            err.print(f"[red]{g.title}:[/] {e}")


@app.command()
@guarded
def rollback(game: Optional[str] = typer.Argument(None),
             component: Optional[str] = typer.Option(None, "--component", "-c",
                                                     help="only this component: nightly | optiscaler | mfgu | cyboman"),
             to: Optional[str] = typer.Option(None, "--to", help="version (with --component) or set id"),
             allow_unverified: bool = typer.Option(False, "--allow-unverified"),
             dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Go back to the previous combination, or one component's previous version."""
    from .core.ops import install as ops
    from .core.ops.components import parse_components

    comp = parse_components([component])[0] if component else None
    ctx = ctx_for(yes)
    g = pick_installed(ctx, game)
    r = ops.rollback(ctx, g, to, dry_run=dry_run, yes=yes, component=comp, allow_unverified=allow_unverified)
    con.print(f"{g.title}: {r.message or 'not changed'}")


def _pin(games, all_, components, on: bool):
    from .core.ops import components as comp

    names = comp.parse_components(components, comp.PINNABLE)
    if not names:
        fail(f"name the components ({', '.join(comp.PINNABLE)})")
    ctx = ctx_for()
    for g in _games(ctx, games, all_):
        done = comp.pin(ctx, g, names, on)
        con.print(f"{g.title}: {'pinned' if on else 'unpinned'} {'; '.join(done)}")


@app.command()
@guarded
def pin(games: Optional[list[str]] = typer.Argument(None, help="one or more games"),
        all_: bool = typer.Option(False, "--all"),
        component: list[str] = typer.Option([], "--component", "-c",
                                            help="nightly | optiscaler | mfgu | fsr4 (repeat for several)")):
    """Keep components at their current version: update skips them and check stops suggesting it."""
    _pin(games, all_, component, True)


@app.command()
@guarded
def unpin(games: Optional[list[str]] = typer.Argument(None),
          all_: bool = typer.Option(False, "--all"),
          component: list[str] = typer.Option([], "--component", "-c")):
    """Let update move these components again."""
    _pin(games, all_, component, False)


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
           proxy: Optional[str] = typer.Option(None, "--proxy",
                                               help="move OptiScaler to this name (when another mod took the old one)"),
           dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Restore managed files, xemfg's ini keys and launch options to the recorded set."""
    from .core.ops import install as ops

    ctx = ctx_for(yes)
    games = installed_games(ctx) if all_ else [pick_installed(ctx, game)]
    for g in games:
        try:
            r = ops.repair(ctx, g, reset_ini=reset_ini, clean=clean, dry_run=dry_run, yes=yes, proxy=_check_proxy(proxy))
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
    con.print(f"keeping (in use, in a game's history, pinned, or newest): {res.summary()}")
    con.print(f"frees {res.freed >> 20} MB")
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


@app.command()
@guarded
def gpu(pick: bool = typer.Option(False, "--pick", help="choose the GPU games run on (laptops with two GPUs)"),
        override: Optional[str] = typer.Option(None, "--override", help="treat the GPU as this profile (see the list)"),
        clear_override: bool = typer.Option(False, "--clear-override", help="use the detected GPU again")):
    """Show the detected GPU(s) and what each can run; pick one or override the detection."""
    from .core.gpu import PROFILES, override_warning

    ctx = ctx_for()
    gpus = ctx.gpus()
    if pick:
        if not gpus:
            fail("no GPU found under /sys/class/drm")
        i = ctx.ui.pick("Which GPU do your games run on?", [x.describe() for x in gpus])
        if i is None:
            fail("no GPU chosen")
        ctx.config.gpu = gpus[i].slot
        ctx.save_config()
    if clear_override:
        ctx.config.gpu_override = ""
        ctx.save_config()
    if override:
        if override not in PROFILES:
            fail(f"unknown profile {override} (one of {', '.join(PROFILES)})")
        ctx.config.gpu_override = override
        ctx.save_config()
    for x in gpus:
        con.print(f"  {x.describe()}", markup=False)
    sel = ctx.gpu_selection()
    if sel.gpu is None:
        con.print(f"[yellow]{sel.reason}[/]")
        return
    p = sel.gpu.profile
    con.print(f"games run on: {sel.gpu.card} ({sel.reason})")
    con.print(f"  profile   {p.key}: {p.label}")
    model = {None: "none"}.get(p.model, p.model)
    con.print(f"  FSR4      model {model}" + (f", {p.support}" if p.support else ""))
    con.print(f"  cyboman   {p.cyboman or 'not offered'}")
    if p.note:
        con.print(f"  note      {p.note}")
    if (w := override_warning(sel.gpu)):
        err.print(f"[yellow]warning:[/] {w}")
    if not sel.gpu.known and not sel.gpu.override:
        con.print("  xemfg does not know this GPU and offers no FSR4 DLL choices for it. If you know what it is, "
                  f"`xemfg gpu --override <profile>` (profiles: {', '.join(k for k in PROFILES if k != 'other')}).")


@fsr4_app.command("list")
@guarded
def fsr4_list(game: Optional[str] = typer.Argument(None, help="an installed game (default: pick one)")):
    """The FSR4 DLLs this game can have on this GPU, with what each trades off (* = default)."""
    from .core.ops.fsr4swap import options_for

    ctx = ctx_for()
    g = pick_installed(ctx, game)
    opts = options_for(ctx, g)
    con.print(f"[bold]{g.title}[/]: {ctx.gpu_selection().gpu.describe()}")
    for o in opts.options:
        con.print(f"  {str(o.choice):24} {o.line()}", markup=False)
    for x in opts.excluded:
        con.print(f"  not offered: {x}", style="dim")
    con.print("swap with `xemfg fsr4 set <game> <choice>`; official DLLs come from AMD's FidelityFX SDK "
              "releases on GitHub, checked against their SHA-256")


@fsr4_app.command("set")
@guarded
def fsr4_set(args: list[str] = typer.Argument(..., help="game(s), then the choice (see `xemfg fsr4 list`)"),
             all_: bool = typer.Option(False, "--all", help="every install (then only the choice is given)"),
             replace_kept: bool = typer.Option(False, "--replace-kept",
                                               help="replace an FSR4 DLL you put there yourself"),
             dry_run: bool = typer.Option(False, "--dry-run"), yes: bool = typer.Option(False, "--yes", "-y")):
    """Swap the FSR4 DLL without reinstalling: xemfg fsr4 set ff16 elden cyboman:exact"""
    from .core.ops.fsr4swap import swap

    *names, choice = args
    _check_fsr4(choice)
    if not names and not all_:
        fail("name one or more games, or pass --all")
    ctx = ctx_for(yes)
    for r in swap(ctx, _games(ctx, names, all_), choice, replace_kept, dry_run, yes):
        con.print(f"{r.game.title}: {'[green]ok[/]' if r.ok else '[red]not changed[/]'} {r.message}")


@fsr4_app.command("store")
@guarded
def fsr4_store():
    """FSR4 DLLs xemfg has put into games, kept for going back (gc never removes them)."""
    ctx = ctx_for()
    entries = ctx.store.entries()
    if not entries:
        con.print("no DLLs kept yet")
    for e in entries:
        m = e.meta
        what = " ".join(str(x) for x in (m.get("kind"), m.get("variant"), m.get("version"), m.get("family")) if x)
        con.print(f"  {e.sha256[:12]}  {what}  ({m.get('api', 'dx12')}; used by {', '.join(m.get('games', []))})")
    con.print(f"in {ctx.store.root}")


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
    """Set dmfg_target, default_proxy or fsr4 (the default FSR4 DLL for new installs)."""
    ctx = ctx_for()
    allowed = {"dmfg_target": int, "default_proxy": str, "fsr4": str}
    if key not in allowed:
        fail(f"unknown key {key} (one of {', '.join(allowed)}; the GPU is set with `xemfg gpu`)")
    if key == "fsr4":
        _check_fsr4(value)
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
