from xemfg.core import compat
from xemfg.core.config import Config
from xemfg.core.discovery import library
from xemfg.core.models import Tier

from fakes import ASI_LOADER_DLL, MS_DLL, OPTI_DLL, RESHADE_DLL, TIER_FILES, make_pe


def analyze(world, title):
    lib = library.discover_all(world.locations(), Config())
    g = next(g for g in lib.games if g.title == title)
    return compat.analyze(g)


def test_tiers(world):
    world.steam_game("1", "Native", TIER_FILES["native"])
    world.steam_game("2", "Native30", TIER_FILES["native30"])
    world.steam_game("3", "Upscaler", TIER_FILES["upscaler"])
    world.steam_game("4", "DlssgOnly", TIER_FILES["dlssg"])
    world.steam_game("5", "Nothing", TIER_FILES["nothing"])
    world.steam_game("6", "Dx11", TIER_FILES["native"], exe_bytes=make_pe(strings=(b"d3d11.dll",)))
    world.steam_game("7", "Eac", {**TIER_FILES["native"], "EasyAntiCheat/x.txt": b"", "start_protected_game.exe": b"x"})
    world.steam_game("8", "Old32", TIER_FILES["native"], exe_bytes=make_pe(machine=0x14C))
    world.steam_game("9", "Linux", {"bin/game": b"\x7fELF" + b"\0" * 200_000}, exe="readme.txt", exe_bytes=b"hi")

    r = analyze(world, "Native")
    assert r.tier == Tier.NATIVE_FG and r.fg_input == "FSRFG"
    assert analyze(world, "Native30").fg_input == "FSRFG30"
    r = analyze(world, "Upscaler")
    assert r.tier == Tier.UPSCALER and r.fg_input == "Upscaler" and any("HUD" in w for w in r.warnings)
    assert analyze(world, "DlssgOnly").tier == Tier.DLSSG_ONLY
    assert analyze(world, "Nothing").tier == Tier.INCOMPATIBLE
    r = analyze(world, "Dx11")
    assert r.tier == Tier.INCOMPATIBLE and "D3D11" in r.reasons[0]
    r = analyze(world, "Eac")
    assert r.tier == Tier.INCOMPATIBLE and "EasyAntiCheat" in r.anti_cheat
    assert "32-bit" in analyze(world, "Old32").reasons[0]
    assert "Linux-native" in analyze(world, "Linux").reasons[0]


def test_unity_flag_and_api_from_player(world):
    world.steam_game("1", "UnityGame", {**TIER_FILES["upscaler"], "UnityPlayer.dll": b"d3d11.dll d3d12.dll"},
                     exe_bytes=make_pe(strings=(b"nothing",)))
    r = analyze(world, "UnityGame")
    assert r.engine == "Unity" and any("-force-d3d12" in f for f in r.flags)
    assert r.tier == Tier.UPSCALER


def test_xray_renderer_explains_incompatibility_before_battleye(world):
    world.steam_game("1", "Clear Sky", {
        "bin/xrRender_R4.dll": make_pe(strings=(b"d3d11.dll",)),
        "bin/BattlEye/BEClient.dll": b"legacy multiplayer client",
    }, exe="bin/xrEngine.exe", exe_bytes=make_pe(strings=()))
    r = analyze(world, "Clear Sky")
    assert r.tier == Tier.INCOMPATIBLE
    assert r.reasons == ["D3D11 only (no D3D12 or Vulkan)"]
    assert r.anti_cheat == ["BattlEye"]


def test_foreign_proxies_and_existing_optiscaler(world):
    world.steam_game("1", "Modded", {**TIER_FILES["native"], "dxgi.dll": OPTI_DLL, "dinput8.dll": ASI_LOADER_DLL,
                                    "winmm.dll": RESHADE_DLL, "dbghelp.dll": MS_DLL,
                                    "OptiScaler/amd_fidelityfx_upscaler_dx12.dll": b"x 4.1.2l-exact-r3-cyboman"})
    r = analyze(world, "Modded")
    assert r.optiscaler_proxy == "dxgi.dll" and r.optiscaler_version == "10.0.0-dev (7f382da9)"
    kinds = {p.name: p.kind for p in r.foreign_proxies}
    assert kinds == {"dinput8.dll": "asi-loader", "winmm.dll": "reshade", "dbghelp.dll": "system"}
    assert r.cyboman == "4.1.2l-exact-r3-cyboman"
