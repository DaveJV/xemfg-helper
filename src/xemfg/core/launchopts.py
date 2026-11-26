"""Launch options: WINEDLLOVERRIDES merging for Faugus (one VAR=value per line) and Steam (... %command%).

Existing overrides and env vars are always kept; only our DLL name is added or removed.
"""
from __future__ import annotations

import re
import shlex

WDO = "WINEDLLOVERRIDES"
FORBIDDEN = ("PROTON_USE_OPTISCALER",)
_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_overrides(value: str) -> list[tuple[str, str]]:
    """'dxgi,dinput8=n,b;foo=b' -> [('dxgi','n,b'), ('dinput8','n,b'), ('foo','b')]."""
    out: list[tuple[str, str]] = []
    for part in value.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            names, mode = part.split("=", 1)
        else:
            names, mode = part, ""
        for n in names.split(","):
            n = n.strip()
            if n:
                out = [(a, b) for a, b in out if a.lower() != n.lower()] + [(n, mode.strip())]
    return out


def render_overrides(items: list[tuple[str, str]]) -> str:
    groups: list[tuple[list[str], str]] = []
    for name, mode in items:
        for g in groups:
            if g[1] == mode:
                g[0].append(name)
                break
        else:
            groups.append(([name], mode))
    return ";".join(f"{','.join(n)}={m}" if m else ",".join(n) for n, m in groups)


def _unquote(v: str) -> tuple[str, str]:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1], v[0]
    return v, ""


def _set_override(value: str | None, dll: str, mode: str | None) -> str:
    items = parse_overrides(value or "")
    items = [(n, m) for n, m in items if n.lower() != dll.lower()]
    if mode is not None:
        items.append((dll, mode))
    return render_overrides(items)


def override_mode(text: str, dll: str) -> str | None:
    """The mode set for `dll` in a launch options string (either format), or None."""
    for m in re.finditer(rf"{WDO}=(\"[^\"]*\"|'[^']*'|\S*)", text):
        val, _ = _unquote(m.group(1))
        for n, mode in parse_overrides(val):
            if n.lower() == dll.lower():
                return mode
    return None


def env_value(text: str, var: str) -> str | None:
    m = re.search(rf"(?:^|\s){re.escape(var)}=(\"[^\"]*\"|'[^']*'|\S*)", text, re.M)
    return _unquote(m.group(1))[0] if m else None


# ------------------------------------------------------------------ Faugus (newline separated)

def faugus_apply(text: str, dll: str, mode: str | None, env: dict[str, str | None] | None = None) -> str:
    """Set (mode) or remove (None) `dll` in WINEDLLOVERRIDES; set/remove extra env vars (None = remove)."""
    env = dict(env or {})
    lines = text.split("\n") if text else []
    out: list[str] = []
    seen_wdo = False
    for line in lines:
        m = _ASSIGN.match(line)
        if m and m.group(1) == WDO:
            seen_wdo = True
            val, q = _unquote(m.group(2))
            new = _set_override(val, dll, mode)
            if new:
                q = q or '"'
                out.append(f"{WDO}={q}{new}{q}")
            continue
        if m and m.group(1) in env:
            v = env.pop(m.group(1))
            if v is not None:
                out.append(f"{m.group(1)}={v}")
            continue
        out.append(line)
    if not seen_wdo and mode is not None:
        out.append(f'{WDO}="{_set_override(None, dll, mode)}"')
    for k, v in env.items():
        if v is not None:
            out.append(f"{k}={v}")
    return "\n".join(x for x in out if x.strip())


# ------------------------------------------------------------------ Steam (VAR=.. %command% args)

def steam_apply(opts: str, dll: str, mode: str | None, env: dict[str, str | None] | None = None) -> str:
    env = dict(env or {})
    opts = opts or ""
    if "%command%" in opts:
        pre, post = opts.split("%command%", 1)
    elif opts.strip():
        pre, post = "", " " + opts.strip()  # plain options are game arguments
    else:
        pre, post = "", ""
    pre = pre.strip()

    m = re.search(rf"(^|\s){WDO}=(\"[^\"]*\"|'[^']*'|\S*)", pre)
    if m:
        val, q = _unquote(m.group(2))
        new = _set_override(val, dll, mode)
        rep = f"{m.group(1)}{WDO}={q or chr(34)}{new}{q or chr(34)}" if new else m.group(1)
        pre = (pre[:m.start()] + rep + pre[m.end():]).strip()
    elif mode is not None:
        pre = f'{WDO}="{_set_override(None, dll, mode)}" {pre}'.strip()
    for k, v in env.items():
        km = re.search(rf"(^|\s){re.escape(k)}=(\"[^\"]*\"|'[^']*'|\S*)", pre)
        if km:
            rep = f"{km.group(1)}{k}={shlex.quote(v)}" if v is not None else km.group(1)
            pre = (pre[:km.start()] + rep + pre[km.end():]).strip()
        elif v is not None:
            pre = f"{k}={shlex.quote(v)} {pre}".strip()
    pre = re.sub(r"\s+", " ", pre).strip()
    if not pre:
        rest = post.strip()
        return f"%command% {rest}" if rest else ""
    return f"{pre} %command%{post}".rstrip()


def wanted_env(fsr4: str) -> dict[str, str | None]:
    """Env vars the xemfg backend needs (FSR4 'none' uses OptiScaler's stock DLL on FP8 + WMMA)."""
    return {"DXIL_SPIRV_CONFIG": "wmma_rdna3_workaround"} if fsr4 == "none" else {}


def display_lines(dll: str, env: dict[str, str]) -> tuple[list[str], str]:
    """(Faugus lines, Steam form) for `xemfg launch-opts`."""
    lines = [f"{k}={v}" for k, v in env.items()] + [f'{WDO}="{dll}=n,b"']
    steam = " ".join([*(f"{k}={v}" for k, v in env.items()), f'{WDO}="{dll}=n,b"', "%command%"])
    return lines, steam


def forbidden_vars(text: str) -> list[str]:
    return [v for v in FORBIDDEN if env_value(text, v) not in (None, "", "0")]
