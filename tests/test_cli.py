import json

from typer.testing import CliRunner

from xemfg import cli
from xemfg.core.context import Context

from fakes import ASI_LOADER_DLL, TIER_FILES
from fakeset import setup_sets

runner = CliRunner()


def patch(monkeypatch, world, fetcher):
    def default(ui):
        return Context(world.paths, world.locations(), ui, lambda: fetcher,
                       steam_running_fn=lambda: False, faugus_running_fn=lambda: False, sysfs=world.sysfs)
    monkeypatch.setattr(Context, "default", staticmethod(default))


def test_cli_flow(world, tmp_path, monkeypatch):
    m, f = setup_sets(world)
    world.faugus_game("ff16", "FINAL FANTASY XVI", tmp_path / "FF", {**TIER_FILES["native"], "dinput8.dll": ASI_LOADER_DLL},
                      launch_arguments='WINEDLLOVERRIDES="dinput8=n,b"')
    world.steam_game("9", "Eac Game", {**TIER_FILES["native"], "EasyAntiCheat/x": b""})
    patch(monkeypatch, world, f)
    r = runner.invoke(cli.app, ["scan"])
    assert r.exit_code == 0 and "Native FG" in r.output and "anti-cheat" in r.output
    r = runner.invoke(cli.app, ["install", "ff16", "--dry-run"])
    assert r.exit_code == 0 and "would download" in r.output
    assert not (tmp_path / "FF/dxgi.dll").exists()
    r = runner.invoke(cli.app, ["install", "eac"])
    assert r.exit_code == 1 and "anti-cheat" in r.output
    r = runner.invoke(cli.app, ["install", "ff16", "--yes"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "FF/dxgi.dll").exists()
    assert json.loads((world.faugus_data / "games.json").read_text())[0]["launch_arguments"] == \
        'WINEDLLOVERRIDES="dinput8,dxgi=n,b"'
    r = runner.invoke(cli.app, ["list"])
    assert "FINAL FANTASY XVI" in r.output and "ok" in r.output
    r = runner.invoke(cli.app, ["status", "ff16"])
    assert "files" in r.output and "match the recorded hashes" in r.output
    r = runner.invoke(cli.app, ["launch-opts", "ff16"])
    assert 'WINEDLLOVERRIDES="dxgi=n,b" %command%' in r.output
    r = runner.invoke(cli.app, ["doctor"])
    assert "all installs healthy" in r.output
    r = runner.invoke(cli.app, ["uninstall", "ff16"], input="n\n")
    assert (tmp_path / "FF/dxgi.dll").exists()  # not confirmed (no tty) -> nothing removed
    r = runner.invoke(cli.app, ["uninstall", "ff16", "--yes"])
    assert r.exit_code == 0 and not (tmp_path / "FF/dxgi.dll").exists()
