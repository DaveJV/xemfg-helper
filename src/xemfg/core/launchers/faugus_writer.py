"""Write launch_arguments into Faugus's games.json (backup first, atomic write)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..fsutil import atomic_write_bytes
from ..state import now


class FaugusWriteError(RuntimeError):
    pass


def faugus_running() -> bool:
    proc = Path("/proc")
    for p in proc.iterdir() if proc.is_dir() else []:
        if not p.name.isdigit():
            continue
        try:
            args = (p / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        for a in args[:3]:
            name = Path(a.decode(errors="ignore")).name
            if name in ("faugus-launcher", "faugus_launcher.py", "faugus-launcher.py"):
                return True
    return False


def read_launch_args(games_json: Path, gameid: str) -> str | None:
    data = json.loads(games_json.read_text())
    e = next((e for e in data if e.get("gameid") == gameid), None)
    return None if e is None else (e.get("launch_arguments") or "")


def write_launch_args(games_json: Path, gameid: str, text: str, backups_dir: Path) -> None:
    raw = games_json.read_text()
    data = json.loads(raw)
    entry = next((e for e in data if e.get("gameid") == gameid), None)
    if entry is None:
        raise FaugusWriteError(f"Faugus entry '{gameid}' not found in {games_json}")
    if (entry.get("launch_arguments") or "") == text:
        return
    backups_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(games_json, backups_dir / f"games.json.{now().replace(':', '')}")
    entry["launch_arguments"] = text
    out = json.dumps(data, indent=4, ensure_ascii=False)
    if raw.endswith("\n"):
        out += "\n"
    atomic_write_bytes(games_json, out.encode())
