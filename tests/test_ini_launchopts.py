from xemfg.core import ini, launchopts as lo

CRLF_INI = (
    "; header comment\r\n\r\n[FrameGen]\r\n; FGInput docs\r\nEnabled = auto\r\nFGInput = auto\r\n\r\n"
    "[XeFG]\r\nDynamicMFG=auto\r\nFramerateTargetDMFG=auto\r\n\r\n"
    "[DLSSG]\r\nFramerateTargetDMFG = auto\r\nDynamicMFG = auto\r\n\r\n[Plugins]\r\n;LoadAsiPlugins=true\r\n"
)


def test_ini_section_scoped_crlf_and_style_preserved():
    new, changes = ini.plan_edit(CRLF_INI, {
        "FrameGen": {"Enabled": "true", "FGInput": "FSRFG", "FGOutput": "XeFG"},
        "XeFG": {"DynamicMFG": "true", "FramerateTargetDMFG": "171"},
        "Plugins": {"LoadAsiPlugins": "true"},
    })
    assert "\n" not in new.replace("\r\n", "")  # only CRLF
    assert "Enabled = true\r\nFGInput = FSRFG\r\nFGOutput=XeFG\r\n" in new  # style kept, missing key added
    assert "[XeFG]\r\nDynamicMFG=true\r\nFramerateTargetDMFG=171\r\n" in new
    assert "[DLSSG]\r\nFramerateTargetDMFG = auto\r\nDynamicMFG = auto\r\n" in new  # other section untouched
    assert ";LoadAsiPlugins=true\r\nLoadAsiPlugins=true\r\n" in new  # comment kept, key added
    assert "; FGInput docs\r\n" in new and new.startswith("; header comment\r\n")
    assert {(c.section, c.key) for c in changes} >= {("FrameGen", "FGOutput"), ("XeFG", "FramerateTargetDMFG")}


def test_ini_numeric_equal_is_no_change_and_idempotent():
    text = "[XeFG]\r\nFramerateTargetDMFG = 169.000000\r\n"
    new, ch = ini.plan_edit(text, {"XeFG": {"FramerateTargetDMFG": "169"}})
    assert new == text and ch == []
    new, ch = ini.plan_edit(text, {"XeFG": {"FramerateTargetDMFG": "171"}})
    again, ch2 = ini.plan_edit(new, {"XeFG": {"FramerateTargetDMFG": "171"}})
    assert again == new and ch2 == []


def test_ini_missing_section_appended(tmp_path):
    new, ch = ini.plan_edit("[A]\nx=1", {"B": {"y": "2"}})
    assert new == "[A]\nx=1\n\n[B]\ny=2\n"
    p = tmp_path / "o.ini"
    ini.write_text(p, CRLF_INI)
    assert p.read_bytes() == CRLF_INI.encode() and ini.read_text(p) == CRLF_INI


def test_override_parse_render():
    assert lo.parse_overrides("dxgi,dinput8=n,b;foo=b") == [("dxgi", "n,b"), ("dinput8", "n,b"), ("foo", "b")]
    assert lo.render_overrides([("dinput8", "n,b"), ("dxgi", "n,b"), ("foo", "b")]) == "dinput8,dxgi=n,b;foo=b"


def test_faugus_merge_keeps_existing():
    text = 'PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dinput8=n,b"'
    new = lo.faugus_apply(text, "dxgi", "n,b")
    assert new == 'PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dinput8,dxgi=n,b"'
    assert lo.faugus_apply(new, "dxgi", "n,b") == new  # idempotent
    assert lo.faugus_apply(new, "dxgi", None) == text  # removal restores
    assert lo.faugus_apply("", "dxgi", "n,b") == 'WINEDLLOVERRIDES="dxgi=n,b"'
    assert lo.faugus_apply('WINEDLLOVERRIDES="dxgi=n,b"', "dxgi", None) == ""
    env = lo.faugus_apply("A=1", "dxgi", "n,b", {"MY_VAR": "1"})
    assert env == 'A=1\nWINEDLLOVERRIDES="dxgi=n,b"\nMY_VAR=1'
    assert lo.faugus_apply(env, "dxgi", None, {"MY_VAR": None}) == "A=1"


def test_steam_merge_cases():
    W = 'WINEDLLOVERRIDES="dxgi=n,b"'
    assert lo.steam_apply("", "dxgi", "n,b") == f"{W} %command%"
    assert lo.steam_apply("-dx12", "dxgi", "n,b") == f"{W} %command% -dx12"
    assert lo.steam_apply("gamemoderun %command% -dx12", "dxgi", "n,b") == f"{W} gamemoderun %command% -dx12"
    assert lo.steam_apply('WINEDLLOVERRIDES="dinput8=n,b" %command%', "dxgi", "n,b") == \
        'WINEDLLOVERRIDES="dinput8,dxgi=n,b" %command%'
    assert lo.steam_apply(f"{W} %command%", "dxgi", None) == ""
    assert lo.steam_apply(f"{W} gamemoderun %command% -x", "dxgi", None) == "gamemoderun %command% -x"
    assert lo.override_mode('WINEDLLOVERRIDES="dinput8,dxgi=n,b" %command%', "dxgi") == "n,b"
    assert lo.override_mode("PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES=\"dxgi,dinput8=n,b\"", "dxgi") == "n,b"
    assert lo.forbidden_vars("PROTON_USE_OPTISCALER=1 %command%") == ["PROTON_USE_OPTISCALER"]
    assert lo.forbidden_vars("PROTON_USE_OPTISCALER=0 %command%") == []


def test_existing_override_left_verbatim():
    text = 'PROTON_DISABLE_NVAPI=1\nWINEDLLOVERRIDES="dxgi,dinput8=n,b"'
    assert lo.faugus_apply(text, "dxgi", "n,b") == text
    assert lo.steam_apply('WINEDLLOVERRIDES="dxgi,dinput8=n,b" %command%', "dxgi", "n,b") == \
        'WINEDLLOVERRIDES="dxgi,dinput8=n,b" %command%'


def test_faugus_converts_steam_style_lines():
    # Faugus never substitutes %command%; a pasted Steam line is split into Faugus's one-per-line form
    assert lo.faugus_apply('WINEDLLOVERRIDES="dinput8=n,b" %command%', "dxgi", "n,b") == \
        'WINEDLLOVERRIDES="dinput8,dxgi=n,b"'
    assert lo.faugus_apply("A=1 gamemoderun %command%", "dxgi", "n,b") == \
        'A=1\ngamemoderun\nWINEDLLOVERRIDES="dxgi=n,b"'
    assert lo.faugus_apply("%command%", "dxgi", "n,b") == 'WINEDLLOVERRIDES="dxgi=n,b"'
    assert "%command%" not in lo.faugus_apply('A="x y" %command%', "dxgi", None)


def test_faugus_refuses_game_args_after_command():
    import pytest
    with pytest.raises(lo.LaunchOptsError, match="Game Arguments"):
        lo.faugus_apply("A=1 %command% -dx12", "dxgi", "n,b")


def test_faugus_env_values_survive_shlex():
    import shlex
    text = lo.faugus_apply("X=1", "dxgi", "n,b", {"V": "a b", "W": "1", "Z": 'a"b', "E": ""})
    assert "W=1" in text.split("\n")  # plain values stay unquoted
    env = dict(t.split("=", 1) for t in shlex.split(text))  # what Faugus does with the text
    assert env == {"X": "1", "WINEDLLOVERRIDES": "dxgi=n,b", "V": "a b", "W": "1", "Z": 'a"b', "E": ""}
