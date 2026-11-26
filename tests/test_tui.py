from xemfg.core.context import Context
from xemfg.core.state import GameState
from xemfg.tui.app import PickScreen, PlanScreen, XemfgApp

from fakes import TIER_FILES
from fakeset import default_sysfs, setup_sets


def factory(world, fetcher):
    return lambda ui: Context(world.paths, world.locations(), ui, lambda: fetcher,
                              steam_running_fn=lambda: False, faugus_running_fn=lambda: False, sysfs=default_sysfs(world))


async def wait_for_screen(app, pilot, kind, tries=200):
    """Wait until a modal is shown (a worker may be blocked waiting for it, so do not wait for workers)."""
    for _ in range(tries):
        await pilot.pause(0.02)
        if isinstance(app.screen, kind):
            return app.screen
    raise AssertionError(f"no {kind.__name__}; log: " + " || ".join(l.text for l in app.query_one("#log").lines[-5:]))


async def settle(app, pilot):
    for _ in range(20):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        if not any(w.is_running for w in app.workers):
            return


async def test_tui_library_and_batch_install(world, tmp_path):
    m, f = setup_sets(world)
    world.faugus_game("a", "Alpha", tmp_path / "A", TIER_FILES["native"])
    world.faugus_game("b", "Beta", tmp_path / "B", TIER_FILES["upscaler"])
    world.faugus_game("c", "Gamma", tmp_path / "C", TIER_FILES["nothing"])
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(app, pilot)
        t = app.query_one("#library-table")
        assert t.row_count == 3
        assert "could not reach upstream" in str(app.query_one("#updates-text").render())
        await pilot.click("#select-native")
        assert app.selected == {"faugus:a"}
        await pilot.click("#install-selected")
        pick = await wait_for_screen(app, pilot, PickScreen)  # one game: which FSR4 DLL
        assert pick.options[0].startswith("* newest official | 4.1.2.9000")
        assert any("cyboman 4.1.2l-lossy" in o and "WARNING" in o for o in pick.options)
        await pilot.press("enter")  # the default
        await wait_for_screen(app, pilot, PlanScreen)
        await pilot.click("#ok")
        await settle(app, pilot)
        assert GameState.load(tmp_path / "A").set == "t1"
        assert GameState.load(tmp_path / "B") is None
        it = app.query_one("#installed-table")
        assert it.row_count == 1
        assert GameState.load(tmp_path / "A").components["fsr4"]["version"] == "4.1.2.9000"
        # swap the FSR4 DLL from the Installed tab
        await pilot.click("#--content-tab-installed")
        await settle(app, pilot)
        await pilot.click("#fsr4-swap")
        pick = await wait_for_screen(app, pilot, PickScreen)
        row = next(i for i, o in enumerate(pick.options) if "cyboman exact |" in o)
        for _ in range(row):
            await pilot.press("down")
        await pilot.press("enter")
        await wait_for_screen(app, pilot, PlanScreen)
        await pilot.click("#ok")
        await settle(app, pilot)
        assert GameState.load(tmp_path / "A").components["fsr4"]["variant"] == "exact"
        # uninstall from the Installed tab
        await pilot.click("#--content-tab-installed")
        await settle(app, pilot)
        await pilot.click("#uninstall")
        await settle(app, pilot)
        assert isinstance(app.screen, PlanScreen), " || ".join([l.text for l in app.query_one("#log").lines][-5:] + [str(app.query_one("#uninstall").region), str(app.query_one("#installed-table").row_count)])
        await pilot.click("#ok")
        await settle(app, pilot)
        assert GameState.load(tmp_path / "A") is None


async def test_tui_banner_shows_new_and_broken(world, tmp_path):
    m, f = setup_sets(world)
    world.faugus_game("a", "Alpha", tmp_path / "A", TIER_FILES["native"])
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(app, pilot)
    world.faugus_game("n", "Newcomer", tmp_path / "N", TIER_FILES["native"])
    from xemfg.core.ops import apply, install as ops
    from fakeset import make_ctx
    ctx = make_ctx(world, f)
    ops.install(ctx, ctx.resolve("Alpha"), apply.Request(), yes=True)
    (tmp_path / "A/dxgi.dll").unlink()
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(app, pilot)
        banner = str(app.query_one("#banner").render())
        assert "Newcomer" in banner and "Alpha" in banner
        assert not app.query_one("#banner").has_class("hidden")


async def test_tui_asks_for_a_free_proxy_name(world, tmp_path):
    from fakes import RESHADE_DLL

    m, f = setup_sets(world)
    world.faugus_game("a", "Alpha", tmp_path / "A", {**TIER_FILES["native"], "dxgi.dll": RESHADE_DLL})
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(app, pilot)
        await pilot.click("#select-native")
        await pilot.click("#install-selected")
        await wait_for_screen(app, pilot, PickScreen)  # FSR4 DLL
        await pilot.press("enter")
        pick = await wait_for_screen(app, pilot, PickScreen)  # the proxy name
        assert "dxgi.dll already belongs to ReShade" in pick.prompt and pick.options[0] == "winmm.dll"
        await pilot.press("down", "enter")
        await wait_for_screen(app, pilot, PlanScreen)
        await pilot.click("#ok")
        await settle(app, pilot)
    assert (tmp_path / "A/version.dll").exists() and (tmp_path / "A/dxgi.dll").read_bytes() == RESHADE_DLL


async def test_selected_row_checkbox_renders(world, tmp_path):
    """DataTable parses str cells as markup, so '[x]' rendered as an empty cell."""
    from textual.widgets._data_table import default_cell_formatter

    def shown(t, key):
        return default_cell_formatter(t.get_cell(key, "sel")).plain

    m, f = setup_sets(world)
    world.faugus_game("a", "Alpha", tmp_path / "A", TIER_FILES["native"])
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(160, 50)) as pilot:
        await settle(app, pilot)
        t = app.query_one("#library-table")
        assert shown(t, "faugus:a") == "[ ]"
        t.focus()
        await pilot.press("space")  # toggle: update_cell
        assert app.selected == {"faugus:a"}
        assert shown(t, "faugus:a") == "[✓]"
        await pilot.press("space")
        assert shown(t, "faugus:a") == "[ ]"
        await pilot.click("#select-native")  # full refresh: add_row
        assert shown(t, "faugus:a") == "[✓]"


async def test_bottom_pane_shows_highlighted_games_full_notes(world, tmp_path):
    m, f = setup_sets(world)
    world.faugus_game("a", "Alpha", tmp_path / "A", TIER_FILES["native"])
    world.faugus_game("b", "Beta", tmp_path / "B", TIER_FILES["upscaler"])
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        t = app.query_one("#library-table")
        assert "notes" not in [str(c.label) for c in t.columns.values()]
        log, box = app.query_one("#log"), app.query_one("#details")
        assert not log.display  # no messages yet: no empty log box

        def pane():
            return str(app.query_one("#notes").render())

        # a long note with brackets: shown whole, not cut or parsed as markup
        beta = app.ctx.compat(app.rows["faugus:b"].game)
        long = "[x] " + "a very long note that would never fit in a table column " * 3 + "[/] end"
        beta.flags.append(long)
        assert box.display and box.border_title == "Alpha"  # Alpha is first and highlighted
        assert "Native FG · game ships FSR frame generation" in pane()
        t.focus()
        await pilot.press("down")  # the pane follows the cursor
        await pilot.pause()
        assert box.border_title == "Beta"
        lines = pane().split("\n")
        assert lines[0].split() [:3] == ["Tier", "Upscaler", "only"] and beta.reasons[0] in lines[0]
        assert any(ln.startswith("Input") and "Upscaler" in ln for ln in lines)
        assert any(ln.startswith("Launcher") and "faugus" in ln for ln in lines)
        for note in beta.warnings + beta.flags:
            assert "• " + note in lines
        await pilot.press("up")
        await pilot.pause()
        assert long not in pane()

        app.post_log("hello")
        await pilot.pause()
        assert log.display and box.display  # the log appears under the details
        await pilot.click("#--content-tab-updates")
        await pilot.pause()
        assert not box.display  # no table on this tab


async def test_library_rows_order_dim_and_hide(world, tmp_path):
    from fakes import make_pe

    m, f = setup_sets(world)
    world.faugus_game("a", "Alpha", tmp_path / "A", TIER_FILES["upscaler"])
    world.faugus_game("z", "Zulu", tmp_path / "Z", TIER_FILES["native"])
    world.faugus_game("old", "Aardvark", tmp_path / "O", {}, exe_bytes=make_pe(0x14C, size=4096))
    world.faugus_game("ac", "Anticheat " + "Long Title " * 8, tmp_path / "AC",
                      {**TIER_FILES["native"], "EasyAntiCheat/x.txt": b"x"})
    app = XemfgApp(factory(world, f))
    async with app.run_test(size=(100, 30)) as pilot:
        await settle(app, pilot)
        t = app.query_one("#library-table")
        keys = [r.key.value for r in t.ordered_rows]
        assert keys[:2] == ["faugus:z", "faugus:a"]  # compatible first, then the rest
        assert set(keys[2:]) == {"faugus:old", "faugus:ac"}
        assert t.get_cell("faugus:old", "sel") == ""  # cannot be selected: no checkbox
        assert str(t.get_cell("faugus:old", "tier")) == "✗ 32-bit exe"
        assert str(t.get_cell("faugus:ac", "tier")) == "✗ anti-cheat"
        assert t.max_scroll_x == 0  # columns fit the panel: no horizontal scrollbar at 100 columns
        assert not app.query_one("#progress").display  # nothing running
        await pilot.press("h")
        assert [r.key.value for r in t.ordered_rows] == ["faugus:z", "faugus:a"]
        assert "2 hidden" in str(t.border_title)
        await pilot.press("h")
        assert t.row_count == 4
