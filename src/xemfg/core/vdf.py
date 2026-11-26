"""Minimal Valve KeyValues (text VDF) reader/writer.

Keeps key order and case. Lookups through `get_ci` are case-insensitive,
as Steam itself treats keys. The dumper writes Steam's own tab layout.
"""
from __future__ import annotations

from typing import Any

VDict = dict[str, Any]


class VdfError(ValueError):
    pass


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif c in "{}":
            yield c
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    nxt = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\", '"': '"'}.get(nxt, "\\" + nxt))
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise VdfError("unterminated string")
            yield ("s", "".join(buf))
            i = j + 1
        elif c == "[":  # conditional like [$WIN32]; skip
            j = text.find("]", i)
            i = n if j < 0 else j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n{}"':
                j += 1
            yield ("s", text[i:j])
            i = j


def loads(text: str) -> VDict:
    stack: list[VDict] = [{}]
    key: str | None = None
    for tok in _tokens(text):
        if tok == "{":
            if key is None:
                raise VdfError("block without key")
            new: VDict = {}
            stack[-1][key] = new
            stack.append(new)
            key = None
        elif tok == "}":
            if len(stack) == 1:
                raise VdfError("unbalanced }")
            stack.pop()
        else:
            s = tok[1]
            if key is None:
                key = s
            else:
                stack[-1][key] = s
                key = None
    if len(stack) != 1:
        raise VdfError("unbalanced {")
    return stack[0]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def dumps(d: VDict, level: int = 0) -> str:
    out = []
    ind = "\t" * level
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f'{ind}"{_esc(k)}"\n{ind}{{\n{dumps(v, level + 1)}{ind}}}\n')
        else:
            out.append(f'{ind}"{_esc(k)}"\t\t"{_esc(str(v))}"\n')
    return "".join(out)


def get_ci(d: Any, *path: str) -> Any:
    """Case-insensitive nested lookup; None if any step is missing."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        hit = None
        for k in cur:
            if k.lower() == p.lower():
                hit = k
                break
        if hit is None:
            return None
        cur = cur[hit]
    return cur


def ensure_ci(d: VDict, *path: str) -> VDict:
    """Walk/create nested dicts with case-insensitive matching of existing keys."""
    cur = d
    for p in path:
        hit = next((k for k in cur if k.lower() == p.lower()), None)
        if hit is None:
            cur[p] = {}
            hit = p
        if not isinstance(cur[hit], dict):
            cur[hit] = {}
        cur = cur[hit]
    return cur
