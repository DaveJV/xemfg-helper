from xemfg.core.context import Context
from xemfg.core.state import GameState
from xemfg.tui.app import PlanScreen, XemfgApp

from fakes import TIER_FILES
from fakeset import setup_sets


def factory(world, fetcher):
    return lambda ui: Context(world.paths, world.locations(), ui, lambda: fetcher,
                              steam_running_fn=lambda: False, faugus_running_fn=lambda: False)


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
        await settle(app, pilot)
        assert isinstance(app.screen, PlanScreen)
        await pilot.click("#ok")
        await settle(app, pilot)
        assert GameState.load(tmp_path / "A").set == "t1"
        assert GameState.load(tmp_path / "B") is None
        it = app.query_one("#installed-table")
        assert it.row_count == 1
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
