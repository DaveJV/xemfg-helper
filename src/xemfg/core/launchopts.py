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
    current = next((m for n, m in items if n.lower() == dll.lower()), None)
    if value and (current == mode if mode is not None else current is None):
        return value  # already as wanted: keep the user's exact spelling and order
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
#
# Faugus puts launch_arguments in front of umu-run and runs shlex.split on the whole command,
# so line breaks are plain whitespace and quotes are removed. It never substitutes %command%.

class LaunchOptsError(ValueError):
    pass


def _faugus_fix_command_lines(lines: list[str]) -> list[str]:
    """Rewrite Steam-style lines ('VAR=x gamemoderun %command%') into Faugus form, one item per line.

    Arguments after %command% are game arguments; in Faugus they belong in Game Arguments, not here.
    """
    out: list[str] = []
    for line in lines:
        if "%command%" not in line:
            out.append(line)
            continue
        pre, post = line.split("%command%", 1)
        if post.strip():
            raise LaunchOptsError(
                f"the Faugus launch arguments contain a Steam-style line with game arguments after %command% "
                f"({line.strip()!r}); move '{post.strip()}' to the entry's Game Arguments in Faugus "
                "and remove %command%, then run xemfg again")
        try:
            tokens = shlex.split(pre, posix=False)  # keeps quotes, so VAR="a b" stays one token
        except ValueError as e:
            raise LaunchOptsError(f"cannot read Faugus launch argument line {line.strip()!r}: {e}") from None
        prefix: list[str] = []
        for t in tokens:
            if _ASSIGN.match(t) and not prefix:
                out.append(t)
            else:
                prefix.append(t)  # a wrapper command such as gamemoderun, with its own arguments
        if prefix:
            out.append(" ".join(prefix))
    return out


def _faugus_value(v: str) -> str:
    """Quote a value so Faugus's shlex.split gives it back unchanged (no-op for plain values)."""
    return shlex.quote(v)


def faugus_apply(text: str, dll: str, mode: str | None, env: dict[str, str | None] | None = None) -> str:
    """Set (mode) or remove (None) `dll` in WINEDLLOVERRIDES; set/remove extra env vars (None = remove).

    Raises LaunchOptsError when the text has a %command% line that cannot be converted safely.
    """
    env = dict(env or {})
    lines = _faugus_fix_command_lines(text.split("\n") if text else [])
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
                out.append(f"{m.group(1)}={_faugus_value(v)}")
            continue
        out.append(line)
    if not seen_wdo and mode is not None:
        out.append(f'{WDO}="{_set_override(None, dll, mode)}"')
    for k, v in env.items():
        if v is not None:
            out.append(f"{k}={_faugus_value(v)}")
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


def xemfg_added_env(recorded: dict[str, str], original: str | None) -> list[str]:
    """Env vars xemfg recorded as its own that were not already in the launch options before install.

    Only these may be removed; a variable the user had set before stays theirs.
    """
    return [k for k in recorded if env_value(original or "", k) is None]


def display_lines(dll: str, env: dict[str, str]) -> tuple[list[str], str]:
    """(Faugus lines, Steam form) for `xemfg launch-opts`."""
    lines = [f"{k}={v}" for k, v in env.items()] + [f'{WDO}="{dll}=n,b"']
    steam = " ".join([*(f"{k}={v}" for k, v in env.items()), f'{WDO}="{dll}=n,b"', "%command%"])
    return lines, steam


def forbidden_vars(text: str) -> list[str]:
    return [v for v in FORBIDDEN if env_value(text, v) not in (None, "", "0")]
