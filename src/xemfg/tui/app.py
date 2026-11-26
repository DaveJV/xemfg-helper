"""Textual TUI: Library, Installed, Updates and Cleanup tabs. All logic comes from xemfg.core."""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
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

THEME = "tokyo-night"
# theme colour variable per tier / health / finding level (resolved with XemfgApp.color)
TIER_COLOR = {Tier.NATIVE_FG: "success", Tier.UPSCALER: "warning", Tier.DLSSG_ONLY: "secondary",
              Tier.UNKNOWN: "foreground-muted", Tier.INCOMPATIBLE: "error"}
HEALTH_COLOR = {"ok": "success", "outdated": "secondary", "drifted": "warning", "broken": "error", "missing": "error"}
LEVEL_COLOR = {"missing": "error", "broken": "error", "drifted": "warning", "outdated": "secondary"}


def short_reason(r) -> str:
    """A few words for the tier column of a game that cannot be selected."""
    if r.tier != Tier.INCOMPATIBLE:
        return r.tier.label
    why = (r.reasons or [""])[0]
    if why.startswith("anti-cheat"):
        return "✗ anti-cheat"
    if why.startswith("32-bit"):
        return "✗ 32-bit exe"
    if "no D3D12" in why:
        return "✗ no DX12"
    return "✗ nothing to hook"


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
        self.app.post_log(f"[$warning]{message}[/]")

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
        classes = "fsr4-picker" if self.prompt.startswith("FSR4 DLL for ") else None
        with Vertical(id="dialog", classes=classes):
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


class FitTable(DataTable):
    """A DataTable that asks the app to re-fit its columns whenever its own width changes
    (including the first time a hidden tab is shown)."""

    _fit_width = -1

    def on_resize(self, event: events.Resize) -> None:
        if event.size.width != self._fit_width:
            self._fit_width = event.size.width
            self.app.call_after_refresh(self.app.refit, self)


# ------------------------------------------------------------------------------------- app
class XemfgApp(App):
    TITLE = "xemfg"
    SUB_TITLE = "XeMFG for Proton games"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #banner { padding: 0 1; background: $panel; color: $foreground; height: auto; }
    #banner.hidden { display: none; }
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    ContentSwitcher { height: 1fr; }
    .bar { height: auto; max-height: 3; padding: 0 1; }
    .bar > * { margin-right: 1; }
    .bar Input { width: 1fr; min-width: 16; }
    .hidden { display: none; }
    .panel { border: round $panel; border-title-color: $foreground-muted; background: $surface; }
    .panel:focus, .panel:focus-within { border: round $primary; border-title-color: $primary; }
    DataTable.panel { height: 1fr; scrollbar-gutter: stable; }
    #details { height: auto; max-height: 12; padding: 0 1; }
    #log { height: auto; max-height: 6; padding: 0 1; overflow-y: auto; }
    #progress { width: 24; height: 1; }
    #updates-box, #cleanup-box { height: 1fr; padding: 0 1; }
    #dialog { width: 90%; height: 85%; border: round $primary; background: $surface; padding: 1; }
    #dialog-title { text-style: bold; color: $primary; }
    #dialog-body { height: 1fr; border: round $panel; }
    #dialog-buttons { height: auto; align-horizontal: right; }
    #dialog-buttons Button { margin-left: 1; }
    #dialog.fsr4-picker { height: auto; max-height: 100%; padding: 0 1; }
    .fsr4-picker #dialog-title { dock: top; }
    .fsr4-picker #dialog-buttons { dock: bottom; }
    PlanScreen, PickScreen { align: center middle; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("space", "toggle_select", "Select", show=True),
        Binding("i", "install_selected", "Install selected"),
        Binding("h", "toggle_hidden", "Hide incompatible"),
    ]

    def __init__(self, ctx_factory=None):
        super().__init__()
        self._ctx_factory = ctx_factory
        self.ctx: Context | None = None
        self.rows: dict[str, Row] = {}
        self.selected: set[str] = set()
        self.health: dict = {}
        self.check_result = None
        self.hide_incompatible = False
        self.theme = THEME
        self._colors: dict[str, str] = {}

    # ---------------------------------------------------------------- theme colours
    def color(self, name: str) -> str:
        """A theme variable as #rrggbb (translucent ones blended over the background) for Rich styles."""
        if name not in self._colors:
            v = self.get_css_variables()
            c = Color.parse(v[name])
            if c.a < 1:
                c = Color.parse(v["background"]) + c
            self._colors[name] = c.hex6
        return self._colors[name]

    def themed(self, markup: str) -> str:
        """Rich markup with $variables in tags ("[bold $error]...") resolved to theme colours."""
        return re.sub(r"\[[^\[\]]*\$[^\[\]]*\]",
                      lambda m: re.sub(r"\$([a-z][a-z0-9-]*)", lambda v: self.color(v.group(1)), m.group(0)), markup)

    def styled(self, text: str, name: str, extra: str = "") -> Text:
        return Text(text, style=f"{extra} {self.color(name)}".strip())

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="banner", classes="hidden")
        with TabbedContent(id="tabs"):
            with TabPane("Library", id="library"):
                with Horizontal(classes="bar"):
                    yield Input(placeholder="filter (name, tier, source)", id="filter", compact=True)
                    yield Button("Install selected", id="install-selected", variant="primary", compact=True)
                    yield Button("Select all Native FG", id="select-native", compact=True)
                    yield Button("Dry run", id="dry-selected", compact=True)
                    yield ProgressBar(id="progress", show_eta=False, classes="hidden")
                yield FitTable(id="library-table", cursor_type="row", zebra_stripes=True, classes="panel")
            with TabPane("Installed", id="installed"):
                with Horizontal(classes="bar"):
                    yield Button("Repair", id="repair", compact=True)
                    yield Button("Diagnose", id="diagnose", compact=True)
                    yield Button("Status", id="status", compact=True)
                    yield Button("FSR4 DLL", id="fsr4-swap", compact=True)
                    yield Button("Uninstall", id="uninstall", variant="error", compact=True)
                yield FitTable(id="installed-table", cursor_type="row", zebra_stripes=True, classes="panel")
            with TabPane("Updates", id="updates"):
                with Horizontal(classes="bar"):
                    yield Button("Check again", id="check", compact=True)
                    yield Button("Update all", id="update-all", variant="primary", compact=True)
                with VerticalScroll(id="updates-box", classes="panel"):
                    yield Static("checking upstream...", id="updates-text")
            with TabPane("Cleanup", id="cleanup"):
                with Horizontal(classes="bar"):
                    yield Button("Doctor", id="doctor", compact=True)
                    yield Button("Prune", id="prune", compact=True)
                    yield Button("GC", id="gc", compact=True)
                with VerticalScroll(id="cleanup-box", classes="panel"):
                    yield Static("Doctor checks every install; Prune drops entries whose folders are gone "
                                 "(re-pointing moved games); GC frees unused cache sets and backups. "
                                 "Each shows its changes before doing anything.", id="cleanup-text")
        with VerticalScroll(id="details", classes="panel hidden"):
            yield Static("", id="notes", markup=False)
        yield RichLog(id="log", markup=True, wrap=True, classes="panel hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#library-table", DataTable).border_title = "Games"
        self.query_one("#installed-table", DataTable).border_title = "Installed"
        self.query_one("#updates-box").border_title = "Versions"
        self.query_one("#cleanup-box").border_title = "Cleanup"
        self.query_one("#log", RichLog).border_title = "Log"
        self.query_one("#progress", ProgressBar).update(total=1, progress=0)
        self.ctx = self._ctx_factory(TuiUI(self)) if self._ctx_factory else Context.default(TuiUI(self))
        self.load_library()
        self.quiet_check()

    # ---------------------------------------------------------------- thread-safe helpers
    def post_log(self, msg: str) -> None:
        def write():
            log = self.query_one("#log", RichLog)
            log.remove_class("hidden")  # hidden until the first message: no empty box
            log.write(self.themed(msg))

        if threading.current_thread() is threading.main_thread():
            write()
        else:
            self.call_from_thread(write)

    def post_progress(self, done: int, total: int) -> None:
        def upd():
            bar = self.query_one("#progress", ProgressBar)
            bar.update(total=total, progress=done)
            bar.set_class(done >= total, "hidden")  # only while something is running

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
            self.post_log(f"[$warning]{e}[/]")

    def _row_rank(self, row: Row) -> tuple:
        installed = row.status.startswith(("xemfg", "OptiScaler", "installed"))
        group = 0 if installed else 1 if self.ctx.compat(row.game).installable else 2
        return group, row.tier.rank, row.game.title.lower()

    def refresh_library_table(self) -> None:
        t = self.query_one("#library-table", DataTable)
        flt = self.query_one("#filter", Input).value.strip().lower()
        rows = []
        for key, row in sorted(self.rows.items(), key=lambda kv: self._row_rank(kv[1])):
            g, r = row.game, self.ctx.compat(row.game)
            hay = f"{g.title} {row.tier.label} {g.source.value} {row.status}".lower()
            if flt and flt not in hay:
                continue
            ok = r.installable
            if self.hide_incompatible and not ok and not self._row_rank(row)[0] == 0:
                continue
            dim = "" if ok else self.color("foreground-muted")
            sel = ("[✓]" if key in self.selected else "[ ]") if ok else ""
            title = Text(g.title, style=dim)
            if key in getattr(self, "new_keys", set()):
                title.append(" NEW", style=f"bold {self.color('accent')}")
            tier = self.styled(row.tier.label, TIER_COLOR[row.tier]) if ok else Text(short_reason(r), style=dim)
            rows.append((key, [sel, title, Text(g.source.value, style=dim), tier,
                               Text(r.fg_input or "", style=dim), Text(row.status, style=dim)]))
        hidden = sum(1 for _ in self.rows) - len(rows)
        t.border_title = f"Games ({len(rows)})" + (f" · {hidden} hidden" if hidden else "")
        self.fill_table(t, [("sel", ""), ("game", "game"), ("source", "source"), ("tier", "tier"),
                            ("input", "input"), ("status", "status")], rows)

    def fill_table(self, t: DataTable, cols: list[tuple[str, str]], rows: list[tuple[str, list]],
                   flex: str = "game") -> None:
        """Rebuild `t` with columns sized to their content and `flex` taking the rest of the panel width,
        so there is never a horizontal scrollbar. Keeps the cursor on the same row."""
        old = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value if t.row_count else None
        pad = t.cell_padding * 2
        widths = {k: max([len(label)] + [Text.from_markup(c).cell_len if isinstance(c, str) else c.cell_len
                                         for _, cells in rows for c in [cells[i]]])
                  for i, (k, label) in enumerate(cols)}
        avail = t.scrollable_content_region.width
        if avail > 0:
            rest = avail - sum(w + pad for k, w in widths.items() if k != flex) - pad
            widths[flex] = max(8, rest)
        t.clear(columns=True)
        for k, label in cols:
            t.add_column(label, key=k, width=widths[k])
        fi = [k for k, _ in cols].index(flex)
        for key, cells in rows:
            c = cells[fi]
            if isinstance(c, Text) and c.cell_len > widths[flex]:
                c = c.copy()
                c.truncate(widths[flex], overflow="ellipsis")
                cells = cells[:fi] + [c] + cells[fi + 1:]
            t.add_row(*cells, key=key)
        keys = [k for k, _ in rows]
        if old in keys:
            t.move_cursor(row=keys.index(old), animate=False)
        self.show_notes()

    def refit(self, t: DataTable) -> None:
        """Column widths follow the panel width."""
        if self.ctx is None:
            return
        if t.id == "library-table":
            self.refresh_library_table()
        else:
            self.refresh_installed_table()

    def action_toggle_hidden(self) -> None:
        self.hide_incompatible = not self.hide_incompatible
        self.refresh_library_table()

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
            self.post_log(f"[$warning]{row.game.title}: {row.tier.label}, not selectable[/]")
            return
        self.selected.symmetric_difference_update({key})
        t.update_cell(key, "sel", "[✓]" if key in self.selected else "[ ]")

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
                        self.post_log(f"[$warning]{e}[/]")
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
                self.post_log(f"[$success]installed[/] {g.title}")
            except Exception as e:
                self._set_status(g.key, "failed")
                self.post_log(f"[$error]{g.title}: {e}[/]")
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
        rows = []
        for key, h in sorted(self.health.items(), key=lambda kv: kv[1].title.lower()):
            e = h.entry
            fsr = (h.state.components if h.state else e.components).get("fsr4")
            rows.append((key, [Text(h.title), self.styled(h.status, HEALTH_COLOR.get(h.status, "foreground")),
                               e.set, describe_fsr4(fsr), e.options.get("proxy", ""), e.launch_target or "-"]))
        t.border_title = f"Installed ({len(rows)})"
        self.fill_table(t, [("game", "game"), ("health", "health"), ("set", "set"), ("fsr4 dll", "fsr4 dll"),
                            ("proxy", "proxy"), ("launcher", "launcher")], rows)

    # ---------------------------------------------------------------- details pane
    def _cursor_key(self) -> tuple[str, str] | None:
        tab = self.query_one(TabbedContent).active
        if tab not in ("library", "installed"):
            return None
        t = self.query_one(f"#{tab}-table", DataTable)
        if t.row_count == 0:
            return None
        return tab, t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    def details_for_cursor(self) -> tuple[str, Text] | None:
        """(title, body) for the highlighted row of the active tab; None when there is nothing to show.
        Every value is added as plain Text, so brackets in it are never read as markup."""
        at = self._cursor_key()
        if at is None:
            return None
        tab, key = at
        body = Text()
        label_w = 11

        def field(label: str, value: str | Text) -> None:
            body.append(f"{label:<{label_w}}", style=f"bold {self.color('foreground-muted')}")
            body.append(value if isinstance(value, Text) else Text(value))
            body.append("\n")

        def bullets(items: list[tuple[str, str]]) -> None:
            for text, colour in items:
                body.append("• ", style=self.color(colour))
                body.append(text, style=self.color(colour) if colour != "foreground" else "")
                body.append("\n")

        if tab == "library":
            row = self.rows.get(key)
            if row is None:
                return None
            g, r = row.game, self.ctx.compat(row.game)
            e = self.ctx.registry.get(key)
            tier = self.styled(row.tier.label, TIER_COLOR[row.tier])
            if r.reasons:
                tier.append(f" · {r.reasons[0]}")
            field("Tier", tier)
            field("Input", r.fg_input or "-")
            if e and e.status == "installed":
                field("Installed", f"xemfg set {e.set} ({e.options.get('proxy', 'dxgi.dll')})")
                field("FSR4 DLL", describe_fsr4(e.components.get("fsr4")))
            elif r.optiscaler_proxy:
                field("Installed", f"OptiScaler {r.optiscaler_version or ''} (not xemfg, {r.optiscaler_proxy})")
                field("FSR4 DLL", r.cyboman and f"cyboman {r.cyboman}" or "-")
            else:
                field("Installed", "no")
            field("Launcher", f"{g.source.value}" + ("" if g.launch_target else " (set the launch option yourself)"))
            items = [(x, "foreground") for x in r.reasons[1:]] + [(x, "warning") for x in r.warnings] + \
                [(x, "foreground-muted") for x in r.flags]
            bullets(items)
            body.rstrip()
            return g.title, body
        h = self.health.get(key)
        if h is None:
            return None
        e = h.entry
        fsr = (h.state.components if h.state else e.components).get("fsr4")
        field("Health", self.styled(h.status, HEALTH_COLOR.get(h.status, "foreground")))
        field("Set", e.set)
        field("FSR4 DLL", describe_fsr4(fsr))
        field("Proxy", e.options.get("proxy", "") or "-")
        field("Launcher", e.launch_target or "none (set the launch option yourself)")
        issues = [f for f in h.findings if f.level != "info"]
        if not issues:
            body.append("no issues", style=self.color("foreground-muted"))
        for f in issues:
            bullets([(f.message, LEVEL_COLOR.get(f.level, "foreground"))])
            if f.fix:
                body.append(f"  fix: {f.fix}\n", style=self.color("foreground-muted"))
        body.rstrip()
        return h.title, body

    def show_notes(self) -> None:
        box = self.query_one("#details", VerticalScroll)
        d = self.details_for_cursor()
        if d is None:
            box.add_class("hidden")
            return
        box.border_title = d[0]
        self.query_one("#notes", Static).update(d[1])
        box.remove_class("hidden")
        box.scroll_home(animate=False)

    @on(DataTable.RowHighlighted)
    def on_row_highlighted(self) -> None:
        self.show_notes()

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self) -> None:
        self.show_notes()

    def _selected_installed(self) -> Game | None:
        t = self.query_one("#installed-table", DataTable)
        if t.row_count == 0:
            self.post_log("nothing installed")
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        g = self.ctx.library().by_key(key)
        if g is None:
            self.post_log("[$error]that game is no longer listed by its launcher; see Cleanup > Prune[/]")
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
            self.post_log(f"[$error]{e}[/]")
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
            self.post_log(f"[$error]{e}[/]")
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
            self.post_log(f"[$success]done:[/] {plan.title}")
        except Exception as e:
            self.post_log(f"[$error]{plan.title}: {e}[/]")
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
            self.post_log(f"[$warning]manifest refresh failed ({e}); using the local manifest[/]")
        preps, notes = [], []
        for e in self.ctx.registry.active():
            g = self.ctx.library().by_key(e.key)
            if g is None:
                continue
            try:
                up = plan_update(self.ctx, g)
            except Exception as ex:
                self.post_log(f"[$error]{e.title}: {ex}[/]")
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
                self.post_log(f"[$success]done:[/] {p.title}")
            except Exception as e:
                self.post_log(f"[$error]{p.title}: {e}[/]")
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
                self.post_log("[$success]pruned[/]")
                self.load_library()

        self.call_from_thread(self.push_screen, PlanScreen("Prune the registry?", text), after)

    # ---------------------------------------------------------------- banner
    def update_banner(self) -> None:
        parts = []
        new = [self.rows[k].game.title for k in getattr(self, "new_keys", set()) if k in self.rows]
        if new:
            parts.append(f"[bold $accent]New games:[/] {', '.join(new)}")
        bad = [h.title for h in self.health.values() if h.status in ("broken", "missing", "drifted")]
        if bad:
            parts.append(f"[bold $error]Needs attention:[/] {', '.join(bad)} (Installed tab)")
        old = [h.title for h in self.health.values() if h.status == "outdated"]
        r = self.check_result
        if old or (r and not r.up_to_date and not r.error):
            parts.append("[bold $accent]Update available[/] (Updates tab)")
        b = self.query_one("#banner", Static)
        b.update("   ".join(parts))
        b.set_class(not parts, "hidden")


def run_tui() -> None:
    XemfgApp().run()
