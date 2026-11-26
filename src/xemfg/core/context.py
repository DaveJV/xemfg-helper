"""Context: everything an operation needs (paths, config, launchers, network, user interaction).

The CLI and the TUI each build one with their own UI implementation; the core never prints
or prompts directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from . import compat as compat_mod
from . import manifest as manifest_mod
from .cache import SetCache
from .config import Config
from .discovery import library as library_mod
from .discovery import match
from .discovery.roots import game_from_path
from .discovery.steam import steam_running
from .launchers.faugus_writer import faugus_running
from .models import CompatReport, Game
from .net import Fetcher
from .platform import LauncherLocations, Paths
from .registry import Registry


class UI(Protocol):
    def confirm(self, message: str) -> bool: ...
    def pick(self, prompt: str, options: list[str]) -> int | None: ...
    def log(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def progress(self, label: str, done: int, total: int | None) -> None: ...


class NoGame(LookupError):
    pass


@dataclass
class ScriptedUI:
    """Non-interactive UI: fixed answers. Used by tests and --yes."""

    answer: bool = True
    picks: list[int] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    confirms: list[str] = field(default_factory=list)

    def confirm(self, message: str) -> bool:
        self.confirms.append(message)
        return self.answer

    def pick(self, prompt: str, options: list[str]) -> int | None:
        self.messages.append(f"pick: {prompt} {options}")
        return self.picks.pop(0) if self.picks else None

    def log(self, message: str) -> None:
        self.messages.append(message)

    def warn(self, message: str) -> None:
        self.messages.append("WARN: " + message)

    def progress(self, label: str, done: int, total: int | None) -> None:
        pass


@dataclass
class Context:
    paths: Paths
    locations: LauncherLocations
    ui: UI
    fetcher_factory: Callable[[], Fetcher]
    config: Config | None = None
    steam_running_fn: Callable[[], bool] | None = None
    faugus_running_fn: Callable[[], bool] | None = None
    _manifest: manifest_mod.Manifest | None = None
    _library: library_mod.Library | None = None
    _registry: Registry | None = None
    _fetcher: Fetcher | None = None
    _compat: dict[str, CompatReport] = field(default_factory=dict)
    sysfs: Path = Path("/sys")
    _gpus: list | None = None

    def __post_init__(self):
        if self.config is None:
            self.config = Config.load(self.paths.config_file)

    @classmethod
    def default(cls, ui: UI) -> "Context":
        from .net import HttpFetcher

        return cls(Paths.default(), LauncherLocations.default(), ui, HttpFetcher)

    # ---------------------------------------------------------------- lazy pieces
    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = self.fetcher_factory()
        return self._fetcher

    @property
    def manifest(self) -> manifest_mod.Manifest:
        if self._manifest is None:
            self._manifest = manifest_mod.load(self.paths.manifest_file)
        return self._manifest

    def reload_manifest(self) -> None:
        self._manifest = None

    @property
    def registry(self) -> Registry:
        if self._registry is None:
            self._registry = Registry.load(self.paths.registry_file)
        return self._registry

    def library(self, refresh: bool = False, snapshot: bool = False) -> library_mod.Library:
        if self._library is None or refresh:
            self._library = library_mod.discover_all(self.locations, self.config)
            self._compat.clear()
            if snapshot:
                library_mod.diff_snapshot(self._library, self.paths.library_snapshot)
        return self._library

    def gpus(self) -> list:
        from .gpu import detect

        if self._gpus is None:
            self._gpus = detect(self.sysfs)
        return self._gpus

    def gpu_selection(self):
        """The GPU games run on (configured, or the only one), with the user's override applied."""
        from .gpu import select

        return select(self.gpus(), self.config.gpu, self.config.gpu_override)

    def compat(self, game: Game) -> CompatReport:
        if game.key not in self._compat:
            m = self.manifest
            self._compat[game.key] = compat_mod.analyze(game, m.optiscaler_hashes(), m.cyboman_by_hash())
        return self._compat[game.key]

    def cache(self, set_id: str) -> SetCache:
        xs = self.manifest.get(set_id)

        def prog(done, total):
            self.ui.progress("download", done, total)

        return SetCache(self.paths.set_cache(set_id), xs, self.fetcher, self.ui.log, prog)

    def steam_running(self) -> bool:
        if self.steam_running_fn:
            return self.steam_running_fn()
        return steam_running(self.library().steam)

    def faugus_running(self) -> bool:
        return self.faugus_running_fn() if self.faugus_running_fn else faugus_running()

    def save_config(self) -> None:
        self.config.save(self.paths.config_file)

    # ---------------------------------------------------------------- game resolution
    def resolve(self, query: str | None, candidates: list[Game] | None = None, prompt: str = "Which game?") -> Game:
        """Fuzzy name / appid / key / path -> one Game. Opens a picker when ambiguous or no query."""
        games = candidates if candidates is not None else self.library().games
        if query and match.looks_like_path(query):
            g = game_from_path(Path(query))
            if g is None:
                raise NoGame(f"no game at {query}")
            return self.ensure_exe(g)
        hits = match.resolve(query, games) if query else list(games)
        if not hits:
            raise NoGame(f"no game matches '{query}'")
        if len(hits) == 1:
            return self.ensure_exe(hits[0])
        idx = self.ui.pick(prompt if not query else f"'{query}' matches several games:",
                           [g.short() for g in hits])
        if idx is None:
            raise NoGame("no game selected")
        return self.ensure_exe(hits[idx])

    def ensure_exe(self, game: Game) -> Game:
        """Ask only on a tie; remember the answer."""
        if game.exe is None and game.exe_candidates:
            idx = self.ui.pick(f"{game.title}: which exe is the game?",
                               [str(p.relative_to(game.install_dir)) if game.install_dir in p.parents else str(p)
                                for p in game.exe_candidates])
            if idx is None:
                raise NoGame(f"{game.title}: no exe chosen")
            game.exe = game.exe_candidates[idx]
            game.exe_reason = "chosen"
            self.config.exe_overrides[game.key] = str(game.exe)
            self.save_config()
            self._compat.pop(game.key, None)
        return game
