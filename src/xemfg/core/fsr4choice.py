"""The FSR4 DLL in OptiScaler's slot: what can be chosen, what fits the GPU, and the ini keys that go with it.

Choices (the `--fsr4` value, stored in the game's options):
    official              the newest official DLL that has the model the GPU runs (the default)
    official:<version>    an older official DLL, e.g. official:4.1.1.2740
    cyboman:<variant>     a cyboman build from the archive for the GPU: exact, lossy, 4.1.2l-exact, 4.1.2l-lossy
    original              what was in the slot before xemfg (nothing: OptiScaler then runs the game's own copy)
    keep                  a DLL you put there yourself; xemfg leaves it alone
    nightly               the DLL that came with the OptiScaler nightly (what `--fsr4 none` used to mean)
Older values still work: exact-l / lossy-l / exact / lossy (cyboman) and none (nightly).

Ini keys (OptiScaler.ini [FSR], meanings from the paired nightly's ini and OptiScaler's FSR4Models.cpp):
    Fsr4GpuSupport  0/auto = detect, 1 = FP8, 2 = INT8, 3 = INT8 RDNA2, 4 = INT8 Lite (forces the support level)
    Fsr4Model       0 = FP8, 1 = INT8, 2 = INT8 RDNA2, 3 = INT8 Lite (which model of the DLL runs)
cyboman DLLs replace the INT8 model of AMD's 4.1.1.2740 file, so they always run INT8 (as before).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import fsr4
from .gpu import FORCED, FP8, INT8, INT8_LITE, INT8_RDNA2, MODEL_INDEX, MODEL_LABEL, Profile
from .manifest import FSR4_CHOICES, CybomanFamily, ManifestError

LEGACY = {"exact-l": "cyboman:exact-l", "lossy-l": "cyboman:lossy-l", "exact": "cyboman:exact",
          "lossy": "cyboman:lossy", "none": "nightly"}
KINDS = ("official", "cyboman", "original", "keep", "nightly")
SUPPORT_INDEX = {FP8: "1", INT8: "2", INT8_RDNA2: "3", INT8_LITE: "4"}


class ChoiceError(ValueError):
    pass


@dataclass(frozen=True)
class Choice:
    kind: str
    arg: str | None = None  # official version, or cyboman variant

    def __str__(self) -> str:
        return f"{self.kind}:{self.arg}" if self.arg else self.kind


def parse(value: str | None) -> Choice:
    v = (value or "official").strip()
    v = LEGACY.get(v.lower(), v)
    kind, _, arg = v.partition(":")
    kind = kind.lower()
    if kind not in KINDS:
        raise ChoiceError(f"unknown FSR4 choice '{value}' (official, official:<version>, cyboman:<variant>, "
                          "original, keep, nightly)")
    if kind == "cyboman" and not arg:
        raise ChoiceError("cyboman needs a variant: cyboman:exact, cyboman:lossy, cyboman:4.1.2l-exact, "
                          "cyboman:4.1.2l-lossy")
    if kind in ("original", "keep", "nightly") and arg:
        raise ChoiceError(f"{kind} takes no version")
    return Choice(kind, arg or None)


def is_legacy_cyboman(value: str | None) -> bool:
    return (value or "") in ("exact-l", "lossy-l", "exact", "lossy")


# ------------------------------------------------------------------ cyboman variants

def variant_dir(family: CybomanFamily, arg: str) -> str:
    """A cyboman variant folder from a folder name (4.1.2l-exact) or an alias (exact-l, exact, lossy)."""
    if arg in family.dlls:
        return arg
    if arg in FSR4_CHOICES:
        return family.variant_dir(arg)
    m = re.match(r"^(?:[\d.]+)?(l?)-?(exact|lossy)$", arg)
    if m:
        return family.variant_dir(f"{m.group(2)}-l" if m.group(1) else m.group(2))
    raise ManifestError(f"cyboman {family.name}: no variant '{arg}' (have {', '.join(sorted(family.dlls))})")


def _lite(d: str) -> bool:
    return bool(re.match(r"^[\d.]+l-", d))


def cyboman_tradeoff(d: str, family: str) -> str:
    rdna2 = family in ("rx6000", "igpu-rdna2")
    full = "AMD's FSR 4.1.2 for RX 6000" if rdna2 else "AMD's full FSR 4"
    if d == "exact":
        return f"faster than AMD's DLL, same picture as {full} (byte for byte)"
    if d == "lossy":
        return "faster again; picture close to AMD's, not the same"
    if _lite(d) and d.endswith("-exact"):
        return "FSR 4.1.2 Lite network (AMD's smaller model): a different, less detailed picture; a little faster than AMD's Lite"
    if _lite(d) and d.endswith("-lossy"):
        return "FSR 4.1.2 Lite, faster again; picture close to Lite's, not the same"
    return "cyboman build"


FAMILY_STATUS = {  # upstream README "Will it work for me?"
    "rx7000": "measured: large gains",
    "rx6000": "runs; gains mostly at 4K",
    "igpu-rdna3": "experimental: few reports, small gains",
    "igpu-rdna2": "experimental: few reports, small gains",
}


# ------------------------------------------------------------------ the list shown at install and swap

@dataclass
class Option:
    choice: Choice
    label: str
    version: str
    gpu: str
    tradeoff: str
    warning: str = ""
    default: bool = False
    current: bool = False

    def line(self) -> str:
        bits = [self.label, self.version, self.gpu, self.tradeoff]
        s = " | ".join(b for b in bits if b)
        if self.warning:
            s += f" | WARNING: {self.warning}"
        return ("* " if self.default else "  ") + s + ("  (installed)" if self.current else "")


@dataclass
class Options:
    options: list[Option] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)  # official versions left out, and why


def official_ok(d: fsr4.OfficialDll, profile: Profile) -> str | None:
    """None when the DLL fits the GPU; else why not."""
    if profile.model is None:
        return profile.note or "no FSR4 for this GPU"
    issue = fsr4.official_issue(d, profile)
    if profile.model not in d.models:
        return f"has no {MODEL_LABEL[profile.model]} model, which {profile.label} runs" + (f"; {issue}" if issue else "")
    return issue


def list_options(profile: Profile, catalog: tuple[fsr4.OfficialDll, ...], cyboman: dict[str, CybomanFamily],
                 cyboman_tag: str, warnings: dict[str, str] | None = None, game_has_dll: bool = False,
                 slot_before: str | None = None, existing: str | None = None, current: str | None = None) -> Options:
    """Newest official first (the default), then older official, then cyboman's builds for this GPU."""
    out = Options()
    officials = sorted((d for d in catalog if d.api == "dx12"), key=lambda d: fsr4.vtuple(d.version), reverse=True)
    first = True
    for d in officials:
        why = official_ok(d, profile)
        if why:
            out.excluded.append(f"official {d.version}: {why}")
            continue
        out.options.append(Option(
            Choice("official", None if first else d.version),
            "newest official" if first else "older official", d.version,
            f"{MODEL_LABEL[profile.model]} model", "AMD's own DLL (FSR SDK " + d.sdk_tag + ")",
            default=first))
        first = False
    fam = cyboman.get(profile.cyboman or "")
    if profile.cyboman is None and profile.model is not None:
        out.excluded.append(f"cyboman: {profile.note or 'not offered for this GPU'}")
    elif fam is None and profile.cyboman:
        out.excluded.append(f"cyboman: release {cyboman_tag or '?'} has no {profile.cyboman} archive")
    elif fam is not None:
        for d in sorted(fam.dlls, key=lambda x: (_lite(x), x.endswith("lossy"))):
            out.options.append(Option(
                Choice("cyboman", d), f"cyboman {d}", f"{cyboman_tag}, AMD 4.1.1.2740 base",
                f"{fam.name} archive ({FAMILY_STATUS.get(fam.name, '')})", cyboman_tradeoff(d, fam.name),
                warning=(warnings or {}).get(d) or ("changes the picture; read its WARNING.txt"
                                                     if d.endswith("lossy") else "")))
    if existing:
        out.options.insert(0, Option(Choice("keep"), "keep your DLL", existing, "", "left exactly as it is",
                                     default=True))
        for o in out.options[1:]:
            o.default = False
    if slot_before or game_has_dll:
        what = slot_before or "nothing: OptiScaler then runs the game's own copy"
        out.options.append(Option(Choice("original"), "original", what, "", "what was there before xemfg"))
    for o in out.options:
        if current is None:
            continue
        cur = parse(current)
        o.current = (o.choice == cur or (cur.kind == o.choice.kind == "official" and cur.arg == o.version)
                     or (cur.kind == o.choice.kind == "cyboman" and o.choice.arg in (cur.arg, _alias_dir(cur.arg, fam))))
    return out


def _alias_dir(arg: str | None, fam: CybomanFamily | None) -> str | None:
    try:
        return variant_dir(fam, arg) if fam and arg else None
    except ManifestError:
        return None


# ------------------------------------------------------------------ ini keys

def ini_keys(choice: Choice, profile: Profile | None) -> dict[str, str]:
    """[FSR] Fsr4GpuSupport / Fsr4Model for the DLL in the slot on this GPU."""
    if choice.kind == "cyboman":
        return {"Fsr4GpuSupport": "2", "Fsr4Model": "1"}  # INT8 model, AMD's GPU check lifted
    if choice.kind == "official" and profile is not None and profile.model:
        support = "auto" if profile.model == FP8 else SUPPORT_INDEX[profile.model]
        if profile.support == FORCED:
            support = SUPPORT_INDEX[profile.model]
        return {"Fsr4GpuSupport": support, "Fsr4Model": str(MODEL_INDEX[profile.model])}
    return {"Fsr4GpuSupport": "auto", "Fsr4Model": "auto"}  # OptiScaler detects and picks


def template_supports(template_text: str, keys: dict[str, str]) -> str | None:
    """None when the nightly's OptiScaler.ini documents the values xemfg wants to write; else why not."""
    # FP8 and INT8 work with any OptiScaler 10 build; the RDNA2 and Lite models came with OptiScaler's
    # "4.1.2 model selection" (commit b686ef4b, 2026-10-20)
    need = []
    if keys.get("Fsr4Model") in ("2", "3"):
        need.append(("Fsr4Model", {"2": "2 = INT8 RDNA2", "3": "3 = INT8 Lite"}[keys["Fsr4Model"]]))
    if keys.get("Fsr4GpuSupport") in ("3", "4"):
        need.append(("Fsr4GpuSupport", {"3": "3 = INT8-2", "4": "4 = INT8 Lite"}[keys["Fsr4GpuSupport"]]))
    for key, doc in need:
        block = re.search(rf"((?:^;.*\r?\n)+){key}=", template_text, re.M)
        if not block or doc not in block.group(1):
            return (f"this OptiScaler nightly's ini does not document {key} with '{doc}'; it needs a nightly from "
                    "2026-10-20 or later for this FSR4 model")
    return None
