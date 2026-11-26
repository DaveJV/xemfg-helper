"""Write LaunchOptions into Steam's localconfig.vdf. Only while Steam is closed (it rewrites the file on exit)."""
from __future__ import annotations

import shutil
from pathlib import Path

from .. import vdf
from ..fsutil import atomic_write_bytes
from ..state import now


class SteamRunning(RuntimeError):
    pass


def read_launch_options(localconfig: Path, appid: str) -> str:
    d = vdf.loads(localconfig.read_text(errors="replace"))
    app = vdf.get_ci(d, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid) or {}
    return app.get("LaunchOptions", "")


def write_launch_options(localconfig: Path, appid: str, value: str, backups_dir: Path) -> None:
    text = localconfig.read_text(errors="replace")
    d = vdf.loads(text)
    app = vdf.ensure_ci(d, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps", appid)
    key = next((k for k in app if k.lower() == "launchoptions"), "LaunchOptions")
    if app.get(key, "") == value:
        return
    if value:
        app[key] = value
    else:
        app.pop(key, None)
    backups_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(localconfig, backups_dir / f"localconfig.vdf.{now().replace(':', '')}")
    atomic_write_bytes(localconfig, vdf.dumps(d).encode())
