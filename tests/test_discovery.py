from xemfg.core.config import Config
from xemfg.core.discovery import exefinder, library, match
from xemfg.core.models import Source

from fakes import TIER_FILES, make_pe


def scan(world, **cfg):
    return library.discover_all(world.locations(), Config(**cfg))


def test_steam_two_libraries_and_tools_filtered(world, tmp_path):
    lib2 = world.add_library(tmp_path / "lib2")
    world.steam_game("100", "Alpha Game", TIER_FILES["native"])
    world.steam_game("200", "Beta Game", TIER_FILES["upscaler"], library=lib2, runner="proton-cachyos-slr")
    world.steam_game("1493710", "Proton Experimental", {}, exe="files/bin/wine64.exe")
    lib = scan(world)
    keys = {g.key: g for g in lib.games}
    assert set(keys) == {"steam:100", "steam:200"}
    b = keys["steam:200"]
    assert b.library == lib2.resolve()
    assert b.runner == "proton-cachyos-slr"
    assert keys["steam:100"].runner == "proton_experimental"
    assert b.prefix == lib2.resolve() / "steamapps/compatdata/200" or b.prefix.name == "200"


def test_steam_launch_options_read(world):
    world.steam_game("100", "Alpha", {}, launch_options="gamemoderun %command%")
    g = scan(world).games[0]
    assert g.launch_options == "gamemoderun %command%"


def test_faugus_entries_runner_and_launch_args(world, tmp_path):
    world.faugus_game("ff16", "FINAL FANTASY XVI", tmp_path / "Games/FFXVI", TIER_FILES["native"], exe="ffxvi.exe",
                      launch_arguments='PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dinput8=n,b"')
    g = scan(world).games[0]
    assert g.source == Source.FAUGUS and g.gameid == "ff16"
    assert g.runner == "proton-cachyos"  # "Default" resolved from config.json
    assert g.exe.name == "ffxvi.exe"
    assert "dinput8" in g.launch_options


def test_faugus_launcher_entry_resolves_real_exe(world, tmp_path):
    root = tmp_path / "Games/NIKKE"
    world.faugus_game("nikke", "NIKKE", root / "Launcher", {}, exe="nikke_launcher.exe")
    world._files(root / "NIKKE/game", {"nikke.exe": make_pe(size=1000), "UnityPlayer.dll": b"u"})
    g = scan(world).games[0]
    assert g.exe == root / "NIKKE/game/nikke.exe"


def test_faugus_prefix_walk_finds_games_and_skips_system(world, tmp_path):
    world.faugus_game("battlenet", "Battle.net", world.faugus_prefixes / "battlenet/drive_c/Program Files (x86)/Battle.net",
                      {}, exe="Battle.net.exe")
    dc = world.faugus_prefixes / "battlenet/drive_c"
    world._files(dc / "Program Files (x86)/Diablo IV", {"Diablo IV.exe": make_pe(size=5000), **TIER_FILES["upscaler"]})
    world._files(dc / "Program Files/Common Files/thing", {"thing.exe": make_pe()})
    world._files(dc / "windows/system32", {"notepad.exe": make_pe()})
    world._files(dc / "users/me/AppData", {"x.exe": make_pe()})
    games = scan(world).games
    titles = {g.title: g for g in games}
    assert "Diablo IV" in titles
    d4 = titles["Diablo IV"]
    assert d4.source == Source.FAUGUS_PREFIX and d4.gameid == "battlenet"
    assert not {"thing", "system32", "AppData"} & set(titles)


def test_exefinder_unreal_unity_largest_and_tie(tmp_path):
    ue = tmp_path / "ue"
    for rel, size in [("Game.exe", 100), ("Proj/Binaries/Win64/Proj-Win64-Shipping.exe", 50),
                      ("Engine/Binaries/Win64/CrashReportClient.exe", 900)]:
        (ue / rel).parent.mkdir(parents=True, exist_ok=True)
        (ue / rel).write_bytes(b"x" * size)
    assert exefinder.find_exe(ue).exe.name == "Proj-Win64-Shipping.exe"

    big = tmp_path / "big"
    big.mkdir()
    (big / "game.exe").write_bytes(b"x" * 1000)
    (big / "UnityCrashHandler64.exe").write_bytes(b"x" * 5000)
    (big / "launcher.exe").write_bytes(b"x" * 9000)
    (big / "_CommonRedist").mkdir()
    (big / "_CommonRedist/vc.exe").write_bytes(b"x" * 99999)
    assert exefinder.find_exe(big).exe.name == "game.exe"

    tie = tmp_path / "tie"
    (tie / "dx11").mkdir(parents=True)
    (tie / "dx12").mkdir()
    (tie / "dx11/game.exe").write_bytes(b"x" * 1000)
    (tie / "dx12/game.exe").write_bytes(b"x" * 990)
    r = exefinder.find_exe(tie)
    assert r.tie and r.exe is None and len(r.candidates) == 2


def test_exe_override_resolves_tie(world, tmp_path):
    root = world.steam_game("300", "Tie Game", {"dx11/a.exe": b"x" * 1000, "dx12/b.exe": b"x" * 990}, exe="dx11/a.exe",
                            exe_bytes=b"x" * 1000)
    g = scan(world).games[0]
    assert g.exe is None and len(g.exe_candidates) == 2
    g = scan(world, exe_overrides={"steam:300": str(root / "dx12/b.exe")}).games[0]
    assert g.exe.name == "b.exe"


def test_add_root_and_dedupe(world, tmp_path):
    roots = tmp_path / "Other"
    world._files(roots / "Solo", {"solo.exe": make_pe(size=100)})
    ff = world.faugus_game("ff16", "FINAL FANTASY XVI", tmp_path / "Other/FF", {}, exe="ffxvi.exe")
    lib = scan(world, roots=[str(roots)])
    titles = sorted(g.title for g in lib.games)
    assert titles == ["FINAL FANTASY XVI", "Solo"]  # FF found by root walk too, but dedupes to the Faugus entry
    assert ff


def test_fuzzy_match(world, tmp_path):
    world.faugus_game("ff16", "FINAL FANTASY XVI", tmp_path / "a", {})
    world.faugus_game("ff7", "FINAL FANTASY VII REBIRTH", tmp_path / "b", {})
    world.steam_game("730", "Counter-Strike 2", {})
    world.steam_game("1245620", "ELDEN RING", {})
    games = scan(world).games
    t = lambda q: [g.title for g in match.resolve(q, games)]  # noqa: E731
    assert t("ff16") == ["FINAL FANTASY XVI"]
    assert t("ffxvi") == ["FINAL FANTASY XVI"]
    assert t("730") == ["Counter-Strike 2"]
    assert t("cs2") == ["Counter-Strike 2"]
    assert t("elden") == ["ELDEN RING"]
    assert t("eldn rng") == ["ELDEN RING"]
    assert len(t("final fantasy")) == 2  # ambiguous -> picker


def test_snapshot_diff_new_games(world, tmp_path):
    world.steam_game("100", "Alpha", {})
    snap = world.paths.library_snapshot
    lib = scan(world)
    library.diff_snapshot(lib, snap)
    assert lib.new_keys == set()  # first scan: nothing is "new"
    world.steam_game("200", "Beta", {})
    lib = scan(world)
    library.diff_snapshot(lib, snap)
    assert lib.new_keys == {"steam:200"}
    lib = scan(world)
    library.diff_snapshot(lib, snap)
    assert lib.new_keys == set()
