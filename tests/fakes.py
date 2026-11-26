"""Builders for a fake machine: Steam libraries, Faugus config + prefixes, game folders, mods."""
from __future__ import annotations

import json
import struct
from pathlib import Path

from xemfg.core import vdf
from xemfg.core.platform import LauncherLocations, Paths


def make_pe(machine: int = 0x8664, strings: tuple[bytes, ...] = (b"d3d12.dll",), size: int = 0) -> bytes:
    head = bytearray(0x40)
    head[:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x40)
    body = b"PE\0\0" + struct.pack("<H", machine) + b"\0" * 18
    data = bytes(head) + body + b"\0".join(strings)
    return data + b"\0" * max(0, size - len(data))


OPTI_DLL = b"MZ fake OptiScaler v10.0.0-dev (7f382da9) (20261123_005226) ReShade SpecialK64.dll"
OPTI_DLL_NIGHTLY = b"MZ fake OptiScaler v10.0.0-dev (ab1f3d49) (20261122_121824)"
RESHADE_DLL = b"MZ fake ReShade by crosire"
ASI_LOADER_DLL = b"MZ fake Ultimate-ASI-Loader IsUltimateASILoader"
SPECIALK_DLL = b"MZ fake SpecialK Kaldaien"
MS_DLL = b"MZ fake " + "Microsoft Corporation".encode("utf-16-le")

TIER_FILES = {
    "native": {"amd_fidelityfx_dx12.dll": b"ffx", "nvngx_dlss.dll": b"dlss"},
    "native30": {"ffx_fsr3_x64.dll": b"ffx3", "ffx_frameinterpolation_x64.dll": b"fi"},
    "upscaler": {"nvngx_dlss.dll": b"dlss", "libxess.dll": b"xess"},
    "dlssg": {"sl.interposer.dll": b"sl", "sl.dlss_g.dll": b"slg"},
    "nothing": {},
}


class World:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.steam_root = self.home / ".local/share/Steam"
        self.faugus_data = self.home / ".local/share/faugus-launcher"
        self.faugus_config = self.home / ".config/faugus-launcher"
        self.faugus_prefixes = self.home / "Faugus"
        self.paths = Paths(self.home / ".config/xemfg", self.home / ".local/share/xemfg", self.home / ".cache/xemfg")
        self.libraries: list[Path] = [self.steam_root]
        self.steam_apps: dict[str, dict] = {}
        self.launch_opts: dict[str, str] = {}
        self.compat: dict[str, str] = {"0": "proton_experimental"}
        self.faugus_entries: list[dict] = []
        self.sysfs = tmp / "sys"
        (self.sysfs / "class/drm").mkdir(parents=True)
        self.steam_root.joinpath("steamapps").mkdir(parents=True)
        self.write_steam()
        self.write_faugus()

    # ---- GPUs (sysfs)
    def add_gpu(self, card: int, slot: str, vendor: int, device: int, gc: tuple[int, int, int] | None = None,
                boot_vga: bool = True, driver: str = "amdgpu") -> None:
        dev = self.sysfs / "devices/pci0000:00" / slot
        dev.mkdir(parents=True)
        (dev / "vendor").write_text(f"{vendor:#06x}\n")
        (dev / "device").write_text(f"{device:#06x}\n")
        (dev / "boot_vga").write_text("1\n" if boot_vga else "0\n")
        drv = self.sysfs / "bus/pci/drivers" / driver
        drv.mkdir(parents=True, exist_ok=True)
        (dev / "driver").symlink_to(drv)
        if gc:
            ip = dev / "ip_discovery/die/0/GC/0"
            ip.mkdir(parents=True)
            for n, v in zip(("major", "minor", "revision"), gc):
                (ip / n).write_text(f"{v}\n")
        card_dir = self.sysfs / f"class/drm/card{card}"
        card_dir.mkdir()
        (card_dir / "device").symlink_to(dev)
        # connector nodes look like cards but are not
        (self.sysfs / f"class/drm/card{card}-DP-1").mkdir()

    # ---- locations
    def locations(self) -> LauncherLocations:
        return LauncherLocations([self.steam_root], [self.faugus_data], [self.faugus_config])

    # ---- Steam
    def add_library(self, path: Path) -> Path:
        (path / "steamapps").mkdir(parents=True, exist_ok=True)
        self.libraries.append(path)
        self.write_steam(localconfig=False)
        return path

    def steam_game(self, appid: str, name: str, files: dict[str, bytes], exe: str = "Game.exe",
                   library: Path | None = None, launch_options: str | None = None, runner: str | None = None,
                   exe_bytes: bytes | None = None) -> Path:
        lib = library or self.steam_root
        installdir = name.replace(":", "")
        root = lib / "steamapps/common" / installdir
        self._files(root, {exe: exe_bytes if exe_bytes is not None else make_pe(size=4096), **files})
        acf = {"AppState": {"appid": appid, "name": name, "StateFlags": "4", "installdir": installdir}}
        (lib / "steamapps" / f"appmanifest_{appid}.acf").write_text(vdf.dumps(acf))
        (lib / "steamapps/compatdata" / appid / "pfx/drive_c").mkdir(parents=True, exist_ok=True)
        if launch_options is not None:
            self.launch_opts[appid] = launch_options
        if runner:
            self.compat[appid] = runner
        self.write_steam()
        return root

    def write_steam(self, localconfig: bool = True):
        folders = {str(i): {"path": str(p), "apps": {}} for i, p in enumerate(self.libraries)}
        (self.steam_root / "steamapps/libraryfolders.vdf").write_text(vdf.dumps({"libraryfolders": folders}))
        cfg = {"InstallConfigStore": {"Software": {"Valve": {"Steam": {
            "CompatToolMapping": {k: {"name": v, "config": "", "priority": "250"} for k, v in self.compat.items()}}}}}}
        (self.steam_root / "config").mkdir(parents=True, exist_ok=True)
        (self.steam_root / "config/config.vdf").write_text(vdf.dumps(cfg))
        if not localconfig:
            return
        lc = self.localconfig_path
        lc.parent.mkdir(parents=True, exist_ok=True)
        apps = {a: {"LastPlayed": "1", "LaunchOptions": o} for a, o in self.launch_opts.items()}
        apps.setdefault("10", {"LastPlayed": "1"})
        lc.write_text(vdf.dumps({"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": apps}}}}}))

    @property
    def localconfig_path(self) -> Path:
        return self.steam_root / "userdata/12345/config/localconfig.vdf"

    def steam_launch_options(self, appid: str) -> str | None:
        d = vdf.loads(self.localconfig_path.read_text())
        return (vdf.get_ci(d, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid) or {}).get("LaunchOptions")

    # ---- Faugus
    def faugus_game(self, gameid: str, title: str, folder: Path, files: dict[str, bytes], exe: str = "Game.exe",
                    launch_arguments: str = "", runner: str = "Default", exe_bytes: bytes | None = None) -> Path:
        self._files(folder, {exe: exe_bytes if exe_bytes is not None else make_pe(size=4096), **files})
        prefix = self.faugus_prefixes / gameid
        (prefix / "drive_c/windows/system32").mkdir(parents=True, exist_ok=True)
        self.faugus_entries.append({
            "gameid": gameid, "title": title, "path": str(folder / exe), "prefix": str(prefix),
            "runner": runner, "launch_arguments": launch_arguments, "game_arguments": "",
            "mangohud": "", "playtime": 0,
        })
        self.write_faugus()
        return folder

    def write_faugus(self):
        self.faugus_data.mkdir(parents=True, exist_ok=True)
        self.faugus_config.mkdir(parents=True, exist_ok=True)
        (self.faugus_data / "games.json").write_text(json.dumps(self.faugus_entries, indent=4))
        (self.faugus_config / "config.json").write_text(json.dumps(
            {"default-prefix": str(self.faugus_prefixes), "default-runner": "proton-cachyos"}, indent=4))

    def faugus_entry(self, gameid: str) -> dict:
        data = json.loads((self.faugus_data / "games.json").read_text())
        return next(e for e in data if e["gameid"] == gameid)

    # ---- helpers
    @staticmethod
    def _files(root: Path, files: dict[str, bytes]):
        for rel, data in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)


class FakeFetcher:
    """Serves canned responses; anything unknown raises FetchError (like a 403/404)."""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def _get(self, url):
        from xemfg.core.net import FetchError

        self.calls.append(url)
        if url not in self.routes:
            raise FetchError(f"403/404 (fake): {url}")
        return self.routes[url]

    def get_text(self, url, accept=None):
        v = self._get(url)
        return v.decode() if isinstance(v, bytes) else v

    def get_json(self, url):
        return self._get(url)

    def final_url(self, url):
        v = self._get(url)
        return v

    def download(self, url, dest, progress=None):
        v = self._get(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(v if isinstance(v, bytes) else v.encode())
        if progress:
            progress(len(v), len(v))
