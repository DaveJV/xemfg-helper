"""GPU detection from sysfs, and what each GPU can run.

The chip comes from the graphics core (GC) IP version amdgpu exposes under
/sys/class/drm/card*/device/ip_discovery (kernel 5.19+). The table below follows the kernel
(amdkfd/kfd_device.c, AMDGPU_FAMILY_* in amdgpu_drm.h) and Mesa (ac_identify_chip).

What each chip can run follows AMD's FSR SDK 2.4.0 release notes and OptiScaler's GPU table
(misc/IdentifyGpu.cpp, which mirrors AMD's own getFsr4ModelForAsic). cyboman archives follow the
"Will it work for me?" table of zgauthier2000/fsr4-shader-performance-overrides.

Nothing here guesses: a chip that is not in the table, or a GPU that is not AMD, is reported as
such and gets no FSR4 choices until the user sets an override.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VENDORS = {0x1002: "AMD", 0x10DE: "NVIDIA", 0x8086: "Intel"}

# FSR4 models (OptiScaler's Fsr4Model numbering)
FP8, INT8, INT8_RDNA2, INT8_LITE = "fp8", "int8", "int8-rdna2", "int8-lite"
MODEL_INDEX = {FP8: 0, INT8: 1, INT8_RDNA2: 2, INT8_LITE: 3}
MODEL_LABEL = {FP8: "FP8", INT8: "INT8", INT8_RDNA2: "INT8 RDNA2", INT8_LITE: "INT8 Lite"}

# how FSR4 support comes about
OFFICIAL = "official"  # AMD lists the GPU for this model
OPTISCALER = "optiscaler"  # OptiScaler enables it by itself; AMD does not list the GPU
FORCED = "forced"  # only with Fsr4GpuSupport set by xemfg; neither AMD nor OptiScaler enables it


@dataclass(frozen=True)
class Profile:
    """What a class of GPU can run. `key` is what `xemfg gpu --override` takes."""

    key: str
    label: str
    generation: str  # RDNA 1 | RDNA 2 | RDNA 3 | RDNA 3.5 | RDNA 4 | other
    integrated: bool | None
    model: str | None  # FSR4 model AMD's DLL runs there (None: no FSR4)
    support: str | None  # OFFICIAL | OPTISCALER | FORCED
    cyboman: str | None  # cyboman archive family (None: never offered)
    note: str = ""


PROFILES: dict[str, Profile] = {p.key: p for p in [
    Profile("rdna4", "RDNA 4 graphics card (RX 9000)", "RDNA 4", False, FP8, OFFICIAL, None,
            "cyboman's DLLs are not for RX 9000 cards"),
    Profile("rdna3", "RDNA 3 graphics card (RX 7000)", "RDNA 3", False, INT8, OFFICIAL, "rx7000"),
    Profile("strix-halo", "RDNA 3.5 built-in graphics, Strix Halo (Radeon 8060S)", "RDNA 3.5", True, INT8, OFFICIAL,
            "igpu-rdna3"),
    Profile("rdna2", "RDNA 2 graphics card (RX 6000)", "RDNA 2", False, INT8_RDNA2, OFFICIAL, "rx6000"),
    Profile("van-gogh", "RDNA 2 built-in graphics, Van Gogh (Steam Deck, Ryzen Z2 A)", "RDNA 2", True, INT8_LITE,
            OFFICIAL, "igpu-rdna2"),
    Profile("strix-point", "RDNA 3.5 built-in graphics, Strix Point (Radeon 880M / 890M)", "RDNA 3.5", True,
            INT8_LITE, OFFICIAL, "igpu-rdna3"),
    Profile("rdna3-igpu", "RDNA 3 / 3.5 built-in graphics (Radeon 780M, 760M, 860M ...)", "RDNA 3", True, INT8_LITE,
            OPTISCALER, "igpu-rdna3", "AMD does not list these chips for FSR4; OptiScaler enables FSR4 Lite"),
    Profile("rdna2-igpu", "RDNA 2 built-in graphics (Radeon 680M, 660M, 610M)", "RDNA 2", True, INT8_LITE, FORCED,
            "igpu-rdna2", "neither AMD nor OptiScaler enables FSR4 here; xemfg would have to force it"),
    Profile("rdna1-lite", "RDNA 1 graphics card (Navi 12 / Navi 14)", "RDNA 1", False, INT8_LITE, OPTISCALER, None,
            "cyboman's DLLs do not work on RDNA 1"),
    Profile("rdna1", "RDNA 1 graphics card (Navi 10)", "RDNA 1", False, None, None, None,
            "no FSR4: Navi 10 lacks the INT8 instructions FSR4 needs"),
    Profile("other", "not an AMD Radeon GPU, or a chip xemfg does not know", "other", None, None, None, None,
            "no FSR4 choices without `xemfg gpu --override`"),
]}

# GC IP version -> (chip, examples, profile)
CHIPS: dict[tuple[int, int, int], tuple[str, str, str]] = {
    (12, 0, 0): ("Navi 44", "RX 9060 / 9060 XT", "rdna4"),
    (12, 0, 1): ("Navi 48", "RX 9070 / 9070 XT", "rdna4"),
    (11, 0, 0): ("Navi 31", "RX 7900 XTX / XT / GRE", "rdna3"),
    (11, 0, 3): ("Navi 32", "RX 7800 XT / 7700 XT", "rdna3"),
    (11, 0, 2): ("Navi 33", "RX 7600 / 7600 XT", "rdna3"),
    (11, 0, 1): ("Phoenix / Hawk Point", "Radeon 780M / 760M", "rdna3-igpu"),
    (11, 0, 4): ("Phoenix 2 / Hawk Point 2", "Radeon 740M", "rdna3-igpu"),
    (11, 5, 0): ("Strix Point", "Radeon 880M / 890M", "strix-point"),
    (11, 5, 1): ("Strix Halo", "Radeon 8050S / 8060S", "strix-halo"),
    (11, 5, 2): ("Krackan Point", "Radeon 840M / 860M", "rdna3-igpu"),
    (10, 3, 0): ("Navi 21", "RX 6800 / 6900 / 6950 XT", "rdna2"),
    (10, 3, 2): ("Navi 22", "RX 6700 / 6750 XT", "rdna2"),
    (10, 3, 4): ("Navi 23", "RX 6600 / 6650 XT", "rdna2"),
    (10, 3, 5): ("Navi 24", "RX 6500 XT / 6400", "rdna2"),
    (10, 3, 1): ("Van Gogh", "Steam Deck, Ryzen Z2 A", "van-gogh"),
    (10, 3, 3): ("Rembrandt", "Radeon 680M / 660M", "rdna2-igpu"),
    (10, 3, 6): ("Raphael / Granite Ridge", "Radeon 610M", "rdna2-igpu"),
    (10, 3, 7): ("Mendocino", "Radeon 610M", "rdna2-igpu"),
    (10, 1, 10): ("Navi 10", "RX 5700 / 5600", "rdna1"),
    (10, 1, 1): ("Navi 14", "RX 5500 / 5300", "rdna1-lite"),
    (10, 1, 2): ("Navi 12", "Radeon Pro 5600M", "rdna1-lite"),
}


@dataclass
class Gpu:
    card: str  # card1
    slot: str  # PCI address, e.g. 0000:03:00.0 (stable id for the config)
    vendor_id: int
    device_id: int
    driver: str | None
    boot_vga: bool
    gc: tuple[int, int, int] | None = None
    chip: str | None = None
    examples: str = ""
    detected: str = "other"  # profile from detection
    override: str | None = None  # profile the user forced

    @property
    def vendor(self) -> str:
        return VENDORS.get(self.vendor_id, f"vendor {self.vendor_id:#06x}")

    @property
    def pci_id(self) -> str:
        return f"{self.vendor_id:04x}:{self.device_id:04x}"

    @property
    def profile(self) -> Profile:
        return PROFILES[self.override or self.detected]

    @property
    def known(self) -> bool:
        return self.detected != "other"

    def describe(self) -> str:
        if self.chip:
            name = f"{self.vendor} {self.chip} ({self.examples})"
        else:
            name = f"{self.vendor} {self.pci_id}"
        gc = f", GC {'.'.join(map(str, self.gc))}" if self.gc else ""
        kind = {True: "integrated", False: "discrete", None: "unknown kind"}[PROFILES[self.detected].integrated]
        return f"{name}, {PROFILES[self.detected].generation} {kind}{gc} [{self.card}, {self.slot}]"


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _gc_version(dev: Path) -> tuple[int, int, int] | None:
    base = dev / "ip_discovery/die/0/GC/0"
    parts = [_read(base / n) for n in ("major", "minor", "revision")]
    if all(p is not None and p.isdigit() for p in parts):
        return tuple(int(p) for p in parts)  # type: ignore[return-value]
    return None


def detect(sysfs: Path = Path("/sys")) -> list[Gpu]:
    """Every GPU with a DRM card node. Order follows the card numbers."""
    out: list[Gpu] = []
    seen: set[str] = set()
    drm = sysfs / "class/drm"
    if not drm.is_dir():
        return out
    cards = [p for p in drm.iterdir() if p.name.startswith("card") and p.name[4:].isdigit()]
    for card in sorted(cards, key=lambda p: int(p.name[4:])):
        dev = card / "device"
        vendor, device = _read(dev / "vendor"), _read(dev / "device")
        if not vendor or not device:
            continue
        try:
            slot = os.path.basename(os.path.realpath(dev))
        except OSError:
            slot = card.name
        if slot in seen:
            continue
        seen.add(slot)
        drv = dev / "driver"
        driver = os.path.basename(os.path.realpath(drv)) if drv.exists() else None
        g = Gpu(card.name, slot, int(vendor, 16), int(device, 16), driver, _read(dev / "boot_vga") == "1")
        if g.vendor_id == 0x1002:
            g.gc = _gc_version(dev)
            hit = CHIPS.get(g.gc) if g.gc else None
            if hit:
                g.chip, g.examples, g.detected = hit
        out.append(g)
    return out


@dataclass
class Selection:
    gpu: Gpu | None
    reason: str  # why this one, or why none
    choices: list[Gpu]

    @property
    def needs_choice(self) -> bool:
        return self.gpu is None and len(self.choices) > 1


def select(gpus: list[Gpu], chosen_slot: str = "", override: str = "") -> Selection:
    """The GPU games run on: the configured one, or the only one. Several and none chosen: ask."""
    if override and override not in PROFILES:
        raise ValueError(f"unknown GPU override '{override}' (one of {', '.join(PROFILES)})")
    if not gpus:
        return Selection(None, "no GPU found under /sys/class/drm", [])
    gpu = None
    if chosen_slot:
        gpu = next((g for g in gpus if g.slot == chosen_slot), None)
        if gpu is None:
            return Selection(None, f"the chosen GPU {chosen_slot} is not present any more", gpus)
        reason = "chosen with `xemfg gpu --pick`"
    elif len(gpus) == 1:
        gpu, reason = gpus[0], "the only GPU"
    else:
        return Selection(None, f"{len(gpus)} GPUs: pick the one games run on (`xemfg gpu --pick`)", gpus)
    if override:
        gpu.override = override
        reason += f"; treated as '{override}' (manual override)"
    return Selection(gpu, reason, gpus)


def override_warning(g: Gpu) -> str | None:
    if not g.override or g.override == g.detected:
        return None
    return (f"GPU override: detected {PROFILES[g.detected].label}, treated as {PROFILES[g.override].label}. "
            "DLLs and settings meant for another GPU can crash games or show a black picture.")
