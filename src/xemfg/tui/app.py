"""Textual TUI: Library, Installed, Updates and Cleanup tabs. All logic comes from xemfg.core."""
from __future__ import annotations

import threading
from dataclasses import dataclass

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog, Static,
                             TabbedContent, TabPane)

from ..core.context import Context
from ..core.models import Game, Tier
from ..core.ops import apply
from ..core.ops.common import OpError
from ..core.migrate import describe_fsr4
from ..core.plan import Plan

TIER_COLOR = {Tier.NATIVE_FG: "green", Tier.UPSCALER: "yellow", Tier.DLSSG_ONLY: "magenta",
              Tier.UNKNOWN: "grey50", Tier.INCOMPATIBLE: "red"}
HEALTH_COLOR = {"ok": "green", "outdated": "cyan", "drifted": "yellow", "broken": "red", "missing": "red"}


class TuiUI:
    """Core UI callbacks for the TUI. Confirmation happens in modal screens before execution,
    so confirm() is only reached for plans the user already approved."""

    def __init__(self, app: "XemfgApp"):
        self.app = app

    def confirm(self, message: str) -> bool:
        return True

    def pick(self, prompt: str, options: list[str]) -> int | None:
        if threading.current_thread() is threading.main_thread():
            return None
        ev = threading.Event()
        box: dict = {}

        def show():
            def done(v):
                box["v"] = v
                ev.set()

            self.app.push_screen(PickScreen(prompt, options), done)

        self.app.call_from_thread(show)
        ev.wait()
        return box.get("v")

    def log(self, message: str) -> None:
        self.app.post_log(message)

    def warn(self, message: str) -> None:
        self.app.post_log(f"[yellow]{message}[/]")

    def progress(self, label: str, done: int, total: int | None) -> None:
        if total:
            self.app.post_progress(done, total)


# ------------------------------------------------------------------------------------- modals
class PlanScreen(ModalScreen[bool]):
    """Shows exactly what would change; Apply / Cancel."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, text: str, apply_label: str = "Apply"):
        super().__init__()
        self.title_text, self.text, self.apply_label = title, text, apply_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self.text, markup=False)
            with Horizontal(id="dialog-buttons"):
                if self.apply_label:
                    yield Button(self.apply_label, variant="primary", id="ok")
                yield Button("Close" if not self.apply_label else "Cancel", id="cancel")

    @on(Button.Pressed, "#ok")
    def btn_ok(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class PickScreen(ModalScreen[int | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, prompt: str, options: list[str]):
        super().__init__()
        self.prompt, self.options = prompt, options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.prompt, id="dialog-title")
            t = DataTable(id="pick", cursor_type="row")
            yield t
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        t = self.query_one("#pick", DataTable)
        t.add_column("option")
        for i, o in enumerate(self.options):
            t.add_row(o, key=str(i))
        t.focus()

    @on(DataTable.RowSelected, "#pick")
    def btn_chosen(self, ev: DataTable.RowSelected) -> None:
        self.dismiss(int(ev.row_key.value))

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass
class Row:
    game: Game
    tier: Tier
    status: str


# ------------------------------------------------------------------------------------- app
class XemfgApp(App):
    TITLE = "xemfg"
    SUB_TITLE = "XeMFG for Proton games"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #banner { padding: 0 1; background: $boost; height: auto; }
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    ContentSwitcher { height: 1fr; }
    #banner.hidden { display: none; }
    .bar { height: auto; padding: 0 1; }
    .bar Button { margin-right: 1; }
    .bar Input { width: 1fr; min-width: 16; }
    DataTable { height: 1fr; }
    #log { height: 9; border-top: solid $primary; }
    #progress { width: 22; }
    #dialog { width: 90%; height: 85%; border: thick $primary; background: $surface; padding: 1; }
    #dialog-title { text-style: bold; }
    #dialog-body { height: 1fr; border: round $primary-darken-2; }
    #dialog-buttons { height: auto; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 1; }
    PlanScreen, PickScreen { align: center middle; }
    #updates-text { padding: 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("space", "toggle_select", "Select", show=True),
        Binding("i", "install_selected", "Install selected"),
    ]

    def __init__(self, ctx_factory=None):
        super().__init__()
        self._ctx_factory = ctx_factory
        self.ctx: Context | None = None
        self.rows: dict[str, Row] = {}
        self.selected: set[str] = set()
        self.health: dict = {}
        self.check_result = None

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner", classes="hidden")
        with TabbedContent(id="tabs"):
            with TabPane("Library", id="library"):
                with Horizontal(classes="bar"):
                    yield Input(placeholder="filter (name, tier, source)", id="filter")
                    yield Button("Install selected", id="install-selected", variant="primary")
                    yield Button("Select all Native FG", id="select-native")
                    yield Button("Dry run", id="dry-selected")
                    yield ProgressBar(id="progress", show_eta=False)
                yield DataTable(id="library-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Installed", id="installed"):
                with Horizontal(classes="bar"):
                    yield Button("Repair", id="repair")
                    yield Button("Diagnose", id="diagnose")
                    yield Button("Status", id="status")
                    yield Button("FSR4 DLL", id="fsr4-swap")
                    yield Button("Uninstall", id="uninstall", variant="error")
                yield DataTable(id="installed-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Updates", id="updates"):
                with Horizontal(classes="bar"):
                    yield Button("Check again", id="check")
                    yield Button("Update all", id="update-all", variant="primary")
                yield Static("checking upstream...", id="updates-text")
            with TabPane("Cleanup", id="cleanup"):
                with Horizontal(classes="bar"):
                    yield Button("Doctor", id="doctor")
                    yield Button("Prune", id="prune")
                    yield Button("GC", id="gc")
                yield Static("Doctor checks every install; Prune drops entries whose folders are gone "
                             "(re-pointing moved games); GC frees unused cache sets and backups. "
                             "Each shows its changes before doing anything.", id="cleanup-text")
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#library-table", DataTable)
        for c in ("", "game", "source", "tier", "input", "status", "notes"):
            t.add_column(c, key=c or "sel")
        it = self.query_one("#installed-table", DataTable)
        for c in ("game", "health", "set", "fsr4 dll", "proxy", "launcher", "issue"):
            it.add_column(c, key=c)
        self.query_one("#progress", ProgressBar).update(total=1, progress=0)
        self.ctx = self._ctx_factory(TuiUI(self)) if self._ctx_factory else Context.default(TuiUI(self))
        self.load_library()
        self.quiet_check()

    # ---------------------------------------------------------------- thread-safe helpers
    def post_log(self, msg: str) -> None:
        if threading.current_thread() is threading.main_thread():
            self.query_one("#log", RichLog).write(msg)
        else:
            self.call_from_thread(self.query_one("#log", RichLog).write, msg)

    def post_progress(self, done: int, total: int) -> None:
        def upd():
            self.query_one("#progress", ProgressBar).update(total=total, progress=done)

        if threading.current_thread() is threading.main_thread():
            upd()
        else:
            self.call_from_thread(upd)

    # ---------------------------------------------------------------- library
    @work(thread=True, exclusive=True, group="scan")
    def load_library(self) -> None:
        from ..core.ops.health import list_health

        ctx = self.ctx
        lib = ctx.library(refresh=True, snapshot=True)
        rows = {}
        for g in lib.games:
            r = ctx.compat(g)
            e = ctx.registry.get(g.key)
            status = f"xemfg {e.set}" if e and e.status == "installed" else (
                "OptiScaler (not xemfg)" if r.optiscaler_proxy else "")
            rows[g.key] = Row(g, r.tier, status)
        health = {h.key: h for h in list_health(ctx)}
        self.call_from_thread(self._show_library, rows, lib.new_keys, health, lib.errors)

    def _show_library(self, rows, new_keys, health, errors) -> None:
        self.rows, self.health, self.new_keys = rows, health, new_keys
        self.refresh_library_table()
        self.refresh_installed_table()
        self.update_banner()
        for e in errors:
            self.post_log(f"[yellow]{e}[/]")

    def refresh_library_table(self) -> None:
        t = self.query_one("#library-table", DataTable)
        flt = self.query_one("#filter", Input).value.strip().lower()
        t.clear()
        for key, row in sorted(self.rows.items(), key=lambda kv: (kv[1].tier.rank, kv[1].game.title.lower())):
            g, r = row.game, self.ctx.compat(row.game)
            hay = f"{g.title} {row.tier.label} {g.source.value} {row.status}".lower()
            if flt and flt not in hay:
                continue
            sel = "[x]" if key in self.selected else "[ ]"
            new = " [bold cyan]NEW[/]" if key in getattr(self, "new_keys", set()) else ""
            notes = "; ".join(r.reasons + r.warnings + r.flags)
            t.add_row(sel, g.title + new, g.source.value, f"[{TIER_COLOR[row.tier]}]{row.tier.label}[/]",
                      r.fg_input or "", row.status, notes[:90], key=key)

    @on(Input.Changed, "#filter")
    def btn_filter(self) -> None:
        self.refresh_library_table()

    def action_toggle_select(self) -> None:
        t = self.query_one("#library-table", DataTable)
        if self.query_one(TabbedContent).active != "library" or t.row_count == 0:
            return
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        row = self.rows[key]
        if not self.ctx.compat(row.game).installable:
            self.post_log(f"[yellow]{row.game.title}: {row.tier.label}, not selectable[/]")
            return
        self.selected.symmetric_difference_update({key})
        t.update_cell(key, "sel", "[x]" if key in self.selected else "[ ]")

    @on(Button.Pressed, "#select-native")
    def btn_select_native(self) -> None:
        for k, r in self.rows.items():
            if r.tier == Tier.NATIVE_FG and not r.status.startswith("xemfg") and not r.status.startswith("OptiScaler"):
                self.selected.add(k)
        self.refresh_library_table()

    @on(Button.Pressed, "#install-selected")
    def action_install_selected(self) -> None:
        self._start_install(dry=False)

    @on(Button.Pressed, "#dry-selected")
    def btn_dry(self) -> None:
        self._start_install(dry=True)

    def _start_install(self, dry: bool) -> None:
        if not self.selected:
            t = self.query_one("#library-table", DataTable)
            if t.row_count:
                self.selected.add(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        games = [self.rows[k].game for k in self.selected if k in self.rows]
        if games:
            self.prepare_install(games, dry)

    @work(thread=True, exclusive=True, group="op")
    def prepare_install(self, games: list[Game], dry: bool) -> None:
        """Build plans (downloads and verifies files on the first run), then ask once."""
        preps, failed = [], []
        self.install_requests = {}
        for g in games:
            try:
                self.ctx.ensure_exe(g)
                # several games: a taken proxy name gets the first free one (the plan says so); one game: asks
                req = apply.Request(auto_proxy=len(games) > 1 or dry)
                if len(games) == 1 and not dry:  # one game: choose its FSR4 DLL; several: the newest official
                    from ..core.ops.fsr4swap import pick

                    try:
                        chosen = pick(self.ctx, g, for_install=True)
                    except OpError as e:
                        self.post_log(f"[yellow]{e}[/]")
                        chosen = ""
                    if chosen is None:
                        self.post_log("install cancelled")
                        return
                    if chosen:
                        req.fsr4, req.fsr4_explicit = chosen, chosen != "keep"
                self.install_requests[g.key] = req
                self.post_log(f"planning {g.title} ...")
                prep = apply.build(self.ctx, g, req, dry_run=dry)
                req.proxy = prep.opts.proxy  # the name chosen now is the one installed after the confirmation
                preps.append(prep)
            except Exception as e:
                failed.append(f"{g.title}: {e}")
        text = "\n\n".join(p.plan.render() for p in preps)
        if failed:
            text += "\n\nNOT INSTALLABLE:\n" + "\n".join(failed)
        if dry:
            self.call_from_thread(self.push_screen, PlanScreen("Dry run (nothing is written)", text, ""))
            return
        if not preps:
            self.call_from_thread(self.push_screen, PlanScreen("Nothing to install", text, ""))
            return

        def after(ok):
            if ok:
                self.run_install([p.game for p in preps])

        self.call_from_thread(self.push_screen, PlanScreen(f"Install XeMFG into {len(preps)} game(s)?", text, "Install"),
                              after)

    @work(thread=True, exclusive=True, group="op")
    def run_install(self, games: list[Game]) -> None:
        n = len(games)
        for i, g in enumerate(games):
            self.post_progress(i, n)
            self._set_status(g.key, "installing...")
            try:
                req = getattr(self, "install_requests", {}).get(g.key) or apply.Request()
                prep = apply.build(self.ctx, g, req)
                prep.plan.execute()
                self._set_status(g.key, "installed")
                self.post_log(f"[green]installed[/] {g.title}")
            except Exception as e:
                self._set_status(g.key, "failed")
                self.post_log(f"[red]{g.title}: {e}[/]")
        self.post_progress(n, n)
        self.selected.clear()
        self.load_library()

    def _set_status(self, key: str, status: str) -> None:
        def upd():
            try:
                self.query_one("#library-table", DataTable).update_cell(key, "status", status)
            except Exception:
                pass

        self.call_from_thread(upd)

    def action_rescan(self) -> None:
        self.load_library()

    # ---------------------------------------------------------------- installed
    def refresh_installed_table(self) -> None:
        t = self.query_one("#installed-table", DataTable)
        t.clear()
        for key, h in sorted(self.health.items(), key=lambda kv: kv[1].title.lower()):
            e = h.entry
            issue = next((f.message for f in h.findings if f.level != "info"), "")
            fsr = (h.state.components if h.state else e.components).get("fsr4")
            t.add_row(h.title, f"[{HEALTH_COLOR.get(h.status, 'white')}]{h.status}[/]", e.set,
                      describe_fsr4(fsr), e.options.get("proxy", ""), e.launch_target or "-", issue[:80], key=key)

    def _selected_installed(self) -> Game | None:
        t = self.query_one("#installed-table", DataTable)
        if t.row_count == 0:
            self.post_log("nothing installed")
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        g = self.ctx.library().by_key(key)
        if g is None:
            self.post_log("[red]that game is no longer listed by its launcher; see Cleanup > Prune[/]")
        return g

    @on(Button.Pressed, "#repair")
    def btn_repair(self) -> None:
        g = self._selected_installed()
        if g:
            self.plan_then_run(lambda: apply.build(self.ctx, g, apply.Request(mode="repair")).plan, f"Repair {g.title}?")

    @on(Button.Pressed, "#fsr4-swap")
    def btn_fsr4_swap(self) -> None:
        g = self._selected_installed()
        if g:
            self.fsr4_swap(g)

    @work(thread=True, exclusive=True, group="op")
    def fsr4_swap(self, g: Game) -> None:
        """Pick a DLL for the selected game, then show the plan (the slot, ini keys, backups) before swapping."""
        from ..core.ops.fsr4swap import pick, swap_plan

        try:
            choice = pick(self.ctx, g)
        except OpError as e:
            self.post_log(f"[red]{e}[/]")
            return
        if choice is None:
            return
        self.plan_then_run(lambda: swap_plan(self.ctx, g, choice), f"FSR4 DLL for {g.title}: {choice}?")

    @on(Button.Pressed, "#uninstall")
    def btn_uninstall(self) -> None:
        from ..core.ops.uninstall import build_uninstall

        g = self._selected_installed()
        if g:
            self.plan_then_run(lambda: build_uninstall(self.ctx, g), f"Uninstall XeMFG from {g.title}?")

    @on(Button.Pressed, "#diagnose")
    def btn_diagnose(self) -> None:
        g = self._selected_installed()
        if g:
            self.show_diagnosis(g)

    @on(Button.Pressed, "#status")
    def btn_status(self) -> None:
        g = self._selected_installed()
        if g:
            h = self.health.get(g.key)
            lines = [f"{f.level}: {f.message}" + (f"\n   fix: {f.fix}" if f.fix else "") for f in h.findings] if h else []
            st = h.state if h else None
            head = [f"set {h.entry.set}", "options " + ", ".join(f"{k}={v}" for k, v in h.entry.options.items())] if h else []
            if st:
                head.append(f"{len(st.files)} managed files")
            self.push_screen(PlanScreen(f"{g.title}: {h.status if h else '?'}", "\n".join(head + [""] + (lines or ["no findings"])), ""))

    @work(thread=True, group="op")
    def show_diagnosis(self, g: Game) -> None:
        from ..core.ops.diagnose import diagnose

        d = diagnose(self.ctx, g)
        lines = [f"{k}: {v}" for k, v in d.facts.items()]
        if d.stack:
            lines += ["", "fault stack:"] + ["  " + s for s in d.stack]
        lines += [""] + [f"{f.level}: {f.message}" + (f"\n   fix: {f.fix}" if f.fix else "") for f in d.findings]
        if not d.findings:
            lines.append("no problems found in the logs")
        self.call_from_thread(self.push_screen, PlanScreen(f"Diagnose {g.title}", "\n".join(lines), ""))

    @work(thread=True, group="op")
    def plan_then_run(self, build, title: str) -> None:
        try:
            plan: Plan = build()
        except (OpError, Exception) as e:
            self.post_log(f"[red]{e}[/]")
            return
        if plan.empty:
            self.call_from_thread(self.push_screen, PlanScreen(title, plan.render(), ""))
            return

        def after(ok):
            if ok:
                self.execute_plan(plan)

        self.call_from_thread(self.push_screen, PlanScreen(title, plan.render()), after)

    @work(thread=True, group="op")
    def execute_plan(self, plan: Plan) -> None:
        try:
            plan.execute(plan.base / ".xemfg" if plan.base and (plan.base / ".xemfg").exists() else None)
            self.post_log(f"[green]done:[/] {plan.title}")
        except Exception as e:
            self.post_log(f"[red]{plan.title}: {e}[/]")
        self.load_library()

    # ---------------------------------------------------------------- updates
    @work(thread=True, exclusive=True, group="check")
    def quiet_check(self) -> None:
        from ..core.ops.check import check

        r = check(self.ctx)
        self.check_result = r
        self.call_from_thread(self._show_check, r)

    def _show_check(self, r) -> None:
        txt = self.query_one("#updates-text", Static)
        tab = self.query_one(TabbedContent).get_tab("updates")
        lines = []
        if r.error:
            lines.append(f"could not reach upstream: {r.error}\nComparing with the local manifest ({r.local_current}).")
        elif r.up_to_date:
            lines.append(f"Manifest up to date: {r.local_current} is the current upstream pairing.")
        else:
            lines += ["Upstream has newer versions:"] + ["  " + d for d in r.differences]
        if r.newest_sdk and not r.sdk_known:
            lines.append(f"AMD released FSR SDK {r.newest_sdk}; xemfg's official DLL list does not have it yet.")
        n = 0
        for g in r.games:
            lines.append(f"\n{g.title}")
            for row in g.rows:
                lines.append(f"  {row.component:10} {row.installed:40} {row.newest:28} {row.status}")
                n += row.status == "update"
            lines += [f"  unverified combination: {w}" for w in g.unverified]
        lines.append("\nUpdate all moves every component that is not pinned (pin with `xemfg pin`).")
        txt.update("\n".join(lines))
        n += 0 if r.up_to_date or r.error else 1
        tab.label = f"Updates ({n})" if n else "Updates"
        self.update_banner()

    @on(Button.Pressed, "#check")
    def btn_check(self) -> None:
        self.query_one("#updates-text", Static).update("checking upstream...")
        self.quiet_check()

    @on(Button.Pressed, "#update-all")
    def btn_update_all(self) -> None:
        self.update_all()

    @work(thread=True, exclusive=True, group="op")
    def update_all(self) -> None:
        from ..core.ops.check import refresh
        from ..core.ops.components import plan_update

        try:
            sid, changed = refresh(self.ctx, self.post_log)
            self.ctx.reload_manifest()
        except Exception as e:
            self.post_log(f"[yellow]manifest refresh failed ({e}); using the local manifest[/]")
        preps, notes = [], []
        for e in self.ctx.registry.active():
            g = self.ctx.library().by_key(e.key)
            if g is None:
                continue
            try:
                up = plan_update(self.ctx, g)
            except Exception as ex:
                self.post_log(f"[red]{e.title}: {ex}[/]")
                continue
            notes += [f"{e.title}: {x}" for x in up.skipped]
            if up.prep is not None:
                preps.append(up.prep)
        for n in notes:
            self.post_log(n)
        if not preps:
            self.post_log("every install is on the newest versions (pinned components left alone)")
            return
        text = "\n\n".join(p.plan.render() for p in preps)

        def after(ok):
            if ok:
                self.run_plans([p.plan for p in preps])

        self.call_from_thread(self.push_screen, PlanScreen(f"Update {len(preps)} install(s)?", text), after)

    @work(thread=True, group="op")
    def run_plans(self, plans: list[Plan]) -> None:
        for p in plans:
            try:
                p.execute()
                self.post_log(f"[green]done:[/] {p.title}")
            except Exception as e:
                self.post_log(f"[red]{p.title}: {e}[/]")
        self.load_library()
        self.quiet_check()

    # ---------------------------------------------------------------- cleanup
    @on(Button.Pressed, "#doctor")
    def btn_doctor(self) -> None:
        lines = []
        for h in self.health.values():
            lines.append(f"{h.status:8} {h.title}")
            lines += [f"         {f.level}: {f.message}" + (f"  -> {f.fix}" if f.fix else "") for f in h.findings]
        q = self.ctx.registry.steam_queue
        if q:
            lines.append(f"\n{len(q)} queued Steam launch option(s): close Steam, then run "
                         "`xemfg launch-opts --apply-queued`")
        self.push_screen(PlanScreen("Doctor", "\n".join(lines) or "no installs registered", ""))

    @on(Button.Pressed, "#gc")
    def btn_gc(self) -> None:
        from ..core.ops.gc import build_gc

        self.plan_then_run(lambda: build_gc(self.ctx).plan, "Clean up the cache and old backups?")

    @on(Button.Pressed, "#prune")
    def btn_prune(self) -> None:
        self.prune_flow()

    @work(thread=True, group="op")
    def prune_flow(self) -> None:
        from ..core.ops.prune import drop, plan_prune, repoint

        items = plan_prune(self.ctx)
        if not items:
            self.call_from_thread(self.push_screen, PlanScreen("Prune", "nothing to prune", ""))
            return
        text = "\n".join(
            f"{it.entry.title}: " + (f"moved to {it.relocated_to.parent} ({it.how}) -> re-point"
                                     if it.relocated_to else f"folder gone ({it.entry.exe_dir}) -> drop entry")
            for it in items)

        def after(ok):
            if ok:
                for it in items:
                    (repoint if it.relocated_to else drop)(self.ctx, it)
                self.post_log("[green]pruned[/]")
                self.load_library()

        self.call_from_thread(self.push_screen, PlanScreen("Prune the registry?", text), after)

    # ---------------------------------------------------------------- banner
    def update_banner(self) -> None:
        parts = []
        new = [self.rows[k].game.title for k in getattr(self, "new_keys", set()) if k in self.rows]
        if new:
            parts.append(f"[bold cyan]New games:[/] {', '.join(new)}")
        bad = [h.title for h in self.health.values() if h.status in ("broken", "missing", "drifted")]
        if bad:
            parts.append(f"[bold red]Needs attention:[/] {', '.join(bad)} (Installed tab)")
        old = [h.title for h in self.health.values() if h.status == "outdated"]
        r = self.check_result
        if old or (r and not r.up_to_date and not r.error):
            parts.append("[bold cyan]Update available[/] (Updates tab)")
        b = self.query_one("#banner", Static)
        b.update("   ".join(parts))
        b.set_class(not parts, "hidden")


def run_tui() -> None:
    XemfgApp().run()
