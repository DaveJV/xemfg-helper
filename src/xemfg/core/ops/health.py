"""Read-only health checks: list, status, doctor. Nothing here writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import ini as ini_mod
from .. import launchopts as lo
from ..context import Context
from ..fsutil import sha256_or_none
from ..models import Game
from ..mods import KIND_LABELS, cyboman_build, optiscaler_version
from ..registry import Entry
from ..state import GameState
from .common import proxy_owner

ORDER = ["ok", "outdated", "drifted", "broken", "missing", "removed"]


@dataclass
class Finding:
    level: str  # missing | broken | drifted | outdated | info
    message: str
    fix: str = ""


@dataclass
class Health:
    key: str
    title: str
    status: str
    findings: list[Finding] = field(default_factory=list)
    entry: Entry | None = None
    state: GameState | None = None
    game: Game | None = None

    def add(self, level: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(level, message, fix))
        if level in ORDER and ORDER.index(level) > ORDER.index(self.status):
            self.status = level


def check_entry(ctx: Context, e: Entry) -> Health:
    h = Health(e.key, e.title, "ok", entry=e)
    if e.status == "removed":
        h.status = "removed"
        return h
    exe_dir = Path(e.exe_dir)
    if not exe_dir.is_dir():
        h.add("missing", f"game folder is gone: {exe_dir}", "xemfg prune (finds moved games)")
        return h
    st = ctx.state(exe_dir)
    h.state = st
    if st is None:
        h.add("broken", "state file .xemfg/state.json is missing", "xemfg repair --clean")
        return h
    if st.schema < 2:
        if st.components:
            h.add("info", "state.json uses the old format; it is upgraded on the next change "
                          "(the old file is kept as .xemfg/state.v1.json)")
        else:
            h.add("info", f"set {st.set} is not in the manifest, so xemfg cannot tell the component versions")
    game = ctx.library().by_key(e.key)
    h.game = game
    osh = ctx.manifest.optiscaler_hashes()

    # files
    for rel, mf in sorted(st.files.items()):
        cur = sha256_or_none(exe_dir / rel)
        if mf.action == "kept" and mf.component == "fsr4":
            if cur != mf.sha256:
                h.add("info", f"{rel}: your own FSR4 DLL changed")
            continue
        if cur is None:
            h.add("broken", f"missing: {rel} (game update or Steam verify?)", f"xemfg repair {_q(e)}")
        elif cur != mf.sha256:
            h.add("broken", f"changed: {rel}", f"xemfg repair {_q(e)}")
    proxy = st.options.get("proxy", "")
    owner = proxy_owner(exe_dir, proxy, osh)
    if owner not in (None, "optiscaler"):
        h.add("broken", f"{proxy} now belongs to {KIND_LABELS.get(owner, owner)}",
              f"remove that mod or reinstall with another --proxy")

    # ini
    ip = exe_dir / "OptiScaler.ini"
    if not ip.is_file():
        h.add("broken", "OptiScaler.ini is missing", f"xemfg repair {_q(e)}")
    else:
        i = ini_mod.Ini(ini_mod.read_text(ip))
        for sec, kv in st.ini.keys.items():
            for k, v in kv.items():
                cur = i.get(sec, k)
                if not ini_mod.values_equal(cur, v):
                    h.add("drifted", f"ini [{sec}] {k} = {cur} (xemfg set {v})", f"xemfg repair {_q(e)}")

    # launch options, runner
    if game is not None:
        from .apply import current_launch_text

        if st.launch.target:
            cur = current_launch_text(ctx, game) or ""
            queued = ctx.registry.steam_queue.get(game.appid or "")
            if lo.override_mode(cur, st.launch.dll) is None:
                if queued:
                    h.add("drifted", "Steam launch options are queued (Steam was running)",
                          "close Steam, then xemfg launch-opts --apply-queued")
                else:
                    h.add("drifted", f"launch option {st.launch.dll}=n,b is missing", f"xemfg repair {_q(e)}")
            for var in lo.xemfg_added_env(st.launch.env, st.launch.original):
                if lo.env_value(cur, var) is not None:
                    h.add("drifted", f"{var} was added by an older xemfg and is no longer needed",
                          f"xemfg repair {_q(e)}")
            for var in lo.forbidden_vars(cur):
                h.add("drifted", f"{var} is set: proton's built-in OptiScaler (0.9.x) undoes the install",
                      f"xemfg repair {_q(e)}")
        if e.runner and game.runner and game.runner != e.runner:
            h.add("info", f"runner changed: {e.runner} -> {game.runner}")
        if game.exe and e.exe_sha256 and sha256_or_none(game.exe) != e.exe_sha256:
            h.add("info", "game exe changed since install (game update)")
    else:
        h.add("info", "no longer listed by Steam/Faugus (launch options not checked)")

    if e.set != ctx.manifest.current:
        h.add("outdated", f"on {e.set}, current is {ctx.manifest.current}", f"xemfg update {_q(e)}")
    return h


def _q(e: Entry) -> str:
    return e.appid or (e.gameid if e.key.startswith("faugus:") else f'"{e.title}"')


def list_health(ctx: Context, include_removed: bool = False) -> list[Health]:
    out = [check_entry(ctx, e) for e in ctx.registry.entries.values()
           if include_removed or e.status == "installed"]
    return sorted(out, key=lambda h: h.title.lower())


@dataclass
class Unmanaged:
    """An OptiScaler setup xemfg did not install (e.g. made with xemfg-linux.sh)."""

    proxy: str | None
    optiscaler_version: str | None
    set_match: str | None  # set whose dashdogy OptiScaler.dll matches
    plugin_set: str | None
    fsr4: str | None
    fsr4_variant: tuple[str, str, str] | None  # (set, family, dir)
    notes: list[str] = field(default_factory=list)

    @property
    def matches_set(self) -> str | None:
        return self.set_match if self.set_match and self.set_match == self.plugin_set else None


def inspect_unmanaged(ctx: Context, game: Game) -> Unmanaged | None:
    r = ctx.compat(game)
    exe_dir = game.exe.parent
    if not r.optiscaler_proxy:
        return None
    m = ctx.manifest
    proxy_sha = sha256_or_none(exe_dir / r.optiscaler_proxy)
    set_match = m.optiscaler_set_by_hash().get(proxy_sha or "")
    plugin = exe_dir / "OptiScaler/plugins/MFGUnlocker.asi"
    psha = sha256_or_none(plugin)
    plugin_set = next((sid for sid, s in m.sets.items() if s.mfgu.inner_sha256.get("MFGUnlocker.dll") == psha), None)
    up = exe_dir / "OptiScaler/amd_fidelityfx_upscaler_dx12.dll"
    usha = sha256_or_none(up)
    variant = None
    for sid in m.ordered_ids():
        for fam in m.sets[sid].families.values():
            for d, hh in fam.dlls.items():
                if hh == usha:
                    variant = (sid, fam.name, d)
    u = Unmanaged(r.optiscaler_proxy, r.optiscaler_version or optiscaler_version(exe_dir / r.optiscaler_proxy),
                  set_match, plugin_set, cyboman_build(up) if up.exists() else None, variant)
    if not plugin.exists():
        u.notes.append("no MFGUnlocker.asi plugin")
    elif plugin_set is None:
        u.notes.append("MFGUnlocker.asi is not from any known set")
    if set_match is None:
        u.notes.append(f"{r.optiscaler_proxy} is not a known dashdogy OptiScaler build (stock OptiScaler?)")
    return u
