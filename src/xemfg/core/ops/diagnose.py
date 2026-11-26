"""diagnose: read the game's logs and map what they say to fixes. Read-only."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import ini as ini_mod
from .. import launchopts as lo
from ..context import Context
from ..models import Game
from ..mods import optiscaler_version
from ..state import GameState
from .health import Finding

OPTI_LINE = re.compile(r"OptiScaler: route=(\S+).*?version=(\S+ \([0-9a-f]+\))(?:.*?path=(\S+))?")
RTXMFG_BUILD = re.compile(r"RTXMFG build=([\w.]+)")
FAULT_START = re.compile(r"(unhandled exception|first-chance fault(?! s this session)|exception code|EXCEPTION_\w+|"
                         r"access violation|fault at|crashed)", re.I)
NO_FAULTS = re.compile(r"first-chance faults this session: none", re.I)
ONE_CHANGE_PER_LAUNCH = [
    "Dynamic MFG off: [XeFG] DynamicMFG=false",
    "remove other D3D12 hooks (overlays, Special K, RTSS, MangoHud's D3D12 layer)",
    "disable ReShade if installed",
    "move the plugin out: OptiScaler/plugins/MFGUnlocker.asi -> MFGUnlocker.asi.off (tests OptiScaler alone)",
]


@dataclass
class Diagnosis:
    game: Game
    log: Path | None
    findings: list[Finding] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    stack: list[str] = field(default_factory=list)

    def add(self, level, msg, fix=""):
        self.findings.append(Finding(level, msg, fix))


def latest_log(exe_dir: Path) -> Path | None:
    logs = [p for p in exe_dir.glob("RTXMFG-*.log")]
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def extract_stack(text: str, limit: int = 25) -> list[str]:
    lines = text.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if FAULT_START.search(line) and not NO_FAULTS.search(line) and "Session diagnostics" not in line:
            out.append(line)
            for nxt in lines[i + 1:i + 15]:
                if re.match(r"^\[\d\d:\d\d:\d\d\.\d+\]\s{2,}|^\s+(#|at |0x|\w+\.(dll|exe))", nxt) or nxt.startswith("    "):
                    out.append(nxt)
                else:
                    break
            if len(out) >= limit:
                break
    return out[:limit]


def diagnose(ctx: Context, game: Game) -> Diagnosis:
    exe_dir = game.exe.parent
    st = ctx.state(exe_dir)
    q = game.appid or game.gameid or game.title
    log = latest_log(exe_dir)
    d = Diagnosis(game, log)
    proxy = st.options["proxy"] if st else (ctx.compat(game).optiscaler_proxy or "dxgi.dll")

    # launch environment
    from .apply import current_launch_text

    launch = current_launch_text(ctx, game) if game.launch_target else None
    if launch is not None:
        dll = proxy[:-4] if proxy.endswith(".dll") else proxy
        if lo.override_mode(launch, dll) is None:
            d.add("broken", f"launch options lack WINEDLLOVERRIDES {dll}=n,b: Wine loads its own {proxy}, "
                            "OptiScaler never runs", f"xemfg repair {q}" if st else f"xemfg launch-opts {q}")
        for v in lo.forbidden_vars(launch):
            d.add("broken", f"{v} is set: proton-cachyos's built-in OptiScaler 0.9.x replaces the files each launch",
                  f"xemfg repair {q}" if st else f"remove {v} from the launch options")
    if game.source.value == "steam" and not game.runner:
        d.add("broken", "no Proton compatibility tool set in Steam", "Steam > Properties > Compatibility: proton-cachyos")
    if game.runner:
        d.facts["runner"] = game.runner

    if log is None:
        d.add("broken", "no RTXMFG log: MFG Unlocker never loaded",
              "check the launch option and [Plugins] LoadAsiPlugins=true; then `xemfg repair`")
        _ini_checks(d, exe_dir, q, st)
        return d
    text = log.read_text(errors="replace")
    d.facts["log"] = log.name
    m = OPTI_LINE.search(text)
    if m:
        d.facts["OptiScaler (log)"] = m.group(2)
        d.facts["route"] = m.group(1)
        on_disk = optiscaler_version(exe_dir / proxy) if (exe_dir / proxy).exists() else None
        if on_disk and on_disk != m.group(2):
            d.add("broken", f"the game loaded OptiScaler {m.group(2)}, but {proxy} is {on_disk} "
                            "(another copy loaded first: proton's built-in one or a second proxy)",
                  f"xemfg repair {q}")
        if st:
            want = ctx.manifest.get(st.set)
            from ..fsutil import sha256_or_none

            if sha256_or_none(exe_dir / proxy) != want.optiscaler.sha256:
                d.add("broken", f"{proxy} differs from the recorded set {st.set}", f"xemfg repair {q}")
    b = RTXMFG_BUILD.search(text)
    if b:
        d.facts["MFG Unlocker (log)"] = b.group(1)
        if st:
            exp = ctx.manifest.get(st.set).mfgu_version.lstrip("vV")
            if b.group(1) != exp:
                d.add("broken", f"MFG Unlocker {b.group(1)} ran, the recorded set has {exp}", f"xemfg repair {q}")
    if "OptiScaler plugin API accepted" not in text:
        d.add("broken", "no 'OptiScaler plugin API accepted': the plugin did not register with OptiScaler "
                        "(wrong OptiScaler pairing or LoadAsiPlugins off)", f"xemfg repair {q}")
    else:
        d.facts["plugin API"] = "accepted"
    dumps = sorted(exe_dir.glob("*.crash.dmp")) + sorted(exe_dir.glob("*.fault.dmp"))
    dumps = [p for p in dumps if p.stat().st_mtime >= log.stat().st_mtime - 600]
    ended = "Session end" in text
    d.stack = extract_stack(text)
    if not ended or dumps:
        what = "the log stops without 'Session end' (crash, hang or kill)" if not ended else "a crash dump was written"
        if dumps:
            what += f"; dumps: {', '.join(p.name for p in dumps)}"
        d.add("broken", what, "change ONE thing per launch, in this order: " + " | ".join(ONE_CHANGE_PER_LAUNCH))
    else:
        d.facts["last session"] = "ended normally"
    _ini_checks(d, exe_dir, q, st)
    _status_json(d, exe_dir)
    _optiscaler_log(d, exe_dir)
    return d


def _ini_checks(d: Diagnosis, exe_dir: Path, q: str, st: GameState | None) -> None:
    ip = exe_dir / "OptiScaler.ini"
    if not ip.exists():
        d.add("broken", "OptiScaler.ini is missing", f"xemfg repair {q} --reset-ini")
        return
    i = ini_mod.Ini(ini_mod.read_text(ip))
    if (i.get("Plugins", "LoadAsiPlugins") or "").lower() != "true":
        d.add("broken", "[Plugins] LoadAsiPlugins is not true: the plugin is not loaded", f"xemfg repair {q}")
    if (i.get("FrameGen", "FGOutput") or "").lower() != "xefg":
        d.add("drifted", f"[FrameGen] FGOutput = {i.get('FrameGen', 'FGOutput')} (XeMFG needs XeFG)", f"xemfg repair {q}")


def _status_json(d: Diagnosis, exe_dir: Path) -> None:
    p = exe_dir / "RTXMFG-Universal.status.json"
    try:
        s = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    d.facts["status.json route"] = str(s.get("route"))


def _optiscaler_log(d: Diagnosis, exe_dir: Path) -> None:
    p = exe_dir / "OptiScaler.log"
    if not p.exists():
        return
    errs = [line for line in p.read_text(errors="replace").splitlines() if re.search(r"\b(error|failed|exception)\b", line, re.I)]
    if errs:
        d.facts["OptiScaler.log errors"] = str(len(errs))
        d.stack += ["OptiScaler.log: " + e.strip() for e in errs[-5:]]
