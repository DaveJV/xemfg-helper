# xemfg-helper

Provides the `xemfg` command.

Installs and maintains **XeMFG** (Intel XeSS multi frame generation, unlocked beyond 2x on AMD
and others by dashdogy's MFG Unlocker) through **OptiScaler**, for Windows games running under
Proton on Linux. It finds your Steam and Faugus Launcher games by itself, so you never type a
game path.

```
xemfg install ff16        # fuzzy name or Steam appid; a picker opens when it is ambiguous
xemfg                     # TUI
```

## What it does

- **Discovery**: reads Steam (`libraryfolders.vdf`, `appmanifest_*.acf`, `compatdata/<appid>`,
  compat tool mapping, launch options) and Faugus Launcher (`games.json`, runners, launch
  arguments), and finds games installed inside Faugus prefixes (e.g. through Battle.net). It
  detects the real exe: Unreal `Binaries/Win64/*-Shipping.exe`, Unity `UnityPlayer.dll`, or else
  the largest non-launcher exe, and asks only on a tie. Each scan diffs against the last one and
  shows new games.
- **Compatibility tiers** (static, read-only):
  - **Native FG**: the game ships FSR frame generation (`FGInput=FSRFG`, or `FSRFG30` for FSR 3.0).
  - **Upscaler only**: DLSS, XeSS or FSR 3.1 upscaling, giving `FGInput=Upscaler` (OptiFG; the HUD may ghost).
  - **DLSS FG only**: Streamline DLSS-G without FSR FG. Needs spoofing, is risky on AMD, and is never installed automatically.
  - **Not compatible**: anti-cheat, DX11/DX10-only (PE imports), 32-bit, Linux-native, or nothing to hook.
  - Also flagged: the engine, other mods' proxy DLLs (ReShade, Ultimate ASI Loader, Special K, DXVK…),
    an existing OptiScaler and its version, and an existing cyboman FSR4 DLL.
- **Paired sets only**: each MFG Unlocker release pairs with one dashdogy/OptiScaler build and
  one OptiScaler nightly. Stock OptiScaler with the plugin can crash at launch, so xemfg reads the
  pairing from MFG Unlocker's `docs/optiscaler.md` and never guesses.
- **Verified downloads**: every file is SHA-256-checked before it touches a game folder. On a
  mismatch the download is deleted and the install aborts.
- **Safe changes**: every write is shown first (`--dry-run` shows the same thing), confirmed,
  and executed with a journal that undoes everything if a step fails. xemfg never overwrites
  another mod's DLL. OptiScaler.ini is edited in place, keeping CRLF line endings, comments and your other settings.
- **Launch options**: `WINEDLLOVERRIDES="<proxy>=n,b"` is merged into the Faugus entry or the
  Steam launch options, keeping every other override and env var. Steam rewrites
  `localconfig.vdf` when it exits, so while Steam is running the change is queued; run
  `xemfg launch-opts --apply-queued` once it is closed. `PROTON_USE_OPTISCALER` is never used
  (proton-cachyos's built-in OptiScaler is 0.9.x and re-downloads its files every launch).

## Install

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/), and `bsdtar` (libarchive) or `7z` to unpack.

```
git clone https://github.com/DaveJV/xemfg-helper
cd xemfg-helper
uv tool install .
```

On Arch/CachyOS you can also build the package in `packaging/arch/` (`makepkg -si`).

## Usage

```
xemfg                                  TUI (Library, Installed, Updates, Cleanup)
xemfg scan [--new]                     library with tiers and install status
xemfg install [game|--all-compatible]  --input FSRFG|FSRFG30|Upscaler --proxy dxgi.dll
                                       --fsr4 exact-l|lossy-l|exact|lossy|none --dmfg-target 171 --dry-run
xemfg adopt <game>                     take over a manual setup (xemfg-linux.sh); writes only .xemfg/
xemfg list                             installs with health: ok / drifted / broken / outdated / missing
xemfg status [game]                    versions, per-file hash check, runner, launch options
xemfg check                            upstream pairing + cyboman vs the local manifest and installs
xemfg update [game|--all]              refresh the manifest, back up, move installs to the current set
xemfg rollback <game> [--to <set>]     back to a previously installed set
xemfg uninstall <game>                 exact removal; launch option removed
xemfg doctor                           every install: files, proxy owner, ini keys, launch options, runner
xemfg repair <game|--all>              restore files, xemfg's ini keys and launch options
             [--reset-ini | --clean]
xemfg diagnose <game>                  RTXMFG/OptiScaler logs and crash dumps -> fixes
xemfg prune [--dry-run]                drop entries whose folders are gone (re-points moved games)
xemfg gc [--dry-run]                   delete unused cache sets, stale downloads, old backups
xemfg manifest refresh|show
xemfg launch-opts <game> [--apply-queued]
xemfg config add-root <dir> | show | set <key> <value>
```

Every command takes a fuzzy name (`ff16`, `ffxvi`, `elden`), a Steam appid, or, if you insist, a path.
With no argument a picker opens. Anything that deletes or overwrites asks first (`--yes` skips the question).
`scan`, `list`, `status`, `check`, `doctor`, `diagnose` and every `--dry-run` are read-only. The only
file they write is xemfg's own scan snapshot in `~/.cache/xemfg/library.json`.

### What an install puts in the game's exe folder

| file | from |
|---|---|
| `OptiScaler/…` | the paired OptiScaler nightly |
| `dxgi.dll` (or `--proxy`) | dashdogy's `OptiScaler.dll` for that MFG Unlocker release |
| `OptiScaler/plugins/MFGUnlocker.asi` | `MFGUnlocker.dll` from the MFG Unlocker release |
| `OptiScaler/amd_fidelityfx_upscaler_dx12.dll` | cyboman FSR4 INT8 DLL (stock kept as `.orig`) |
| `OptiScaler.ini` | edited: `[Plugins] LoadAsiPlugins=true`, `[FrameGen] Enabled=true FGInput=<tier> FGOutput=XeFG`, `[XeFG] DynamicMFG=true FramerateTargetDMFG=171`, `[FSR] UpscalerIndex=0 Fsr4GpuSupport=2 Fsr4Model=1` |
| `.xemfg/state.json`, `.xemfg/backups/` | what xemfg added/replaced, original hashes, backups |

An existing cyboman DLL is kept unless you pass `--fsr4`. The FSR4 archive is the `rx7000` one by
default (`xemfg config set gpu_family rx6000|igpu-rdna3|igpu-rdna2`). With `--fsr4 none`, OptiScaler's
own FSR4 DLL is used and OptiScaler picks the model for your GPU (`Fsr4GpuSupport=auto`, `Fsr4Model=auto`).
xemfg adds no environment variables. `DXIL_SPIRV_CONFIG=wmma_rdna3_workaround`, which older versions added,
is removed by `repair`, `update` or `uninstall` (only if xemfg added it).

### Files

- `~/.local/share/xemfg/registry.json`: every install ever made (only `prune` drops entries)
- `~/.local/share/xemfg/manifest.toml`: sets resolved by `manifest refresh` (the bundled one is the starting point)
- `~/.local/share/xemfg/backups/`: copies of `games.json` / `localconfig.vdf` before each change
- `~/.cache/xemfg/<set>/`: verified downloads (shares `~/.cache/xemfg/v1.7.2` with `xemfg-linux.sh`)
- `~/.config/xemfg/config.toml`: settings and extra game roots

## Development

```
uv sync
uv run pytest
```

The tests build fake Steam libraries, Faugus configs and prefixes, games in every tier, games with
other mods' proxies, and a small but real XeMFG set (zip archives served by a fake fetcher). They
cover install, reinstall, batch install, update then rollback, uninstall, conflict refusal,
Steam-verify-wiped files (doctor + repair), deleted and moved games, Faugus override merging, gc,
diagnose on sample logs, and the TUI.

`xemfg.core` holds all the logic; the CLI (`xemfg/cli.py`) and the TUI (`xemfg/tui/`) only call it.
Frame generation backends are a plugin interface (`core/backends/`), and only `xemfg` is implemented.
Platform specifics live in `core/platform.py`. Only Linux is implemented.

## Credits

- [OptiScaler](https://github.com/optiscaler/OptiScaler) and its
  [nightly builds](https://github.com/optiscaler/OptiScaler-nightly)
- dashdogy: [MFG Unlocker](https://github.com/dashdogy/MFG-Unlocker) and the
  [OptiScaler fork](https://github.com/dashdogy/OptiScaler) it pairs with
- cyboman and zgauthier2000: [FSR4 INT8 shader performance overrides](https://github.com/zgauthier2000/fsr4-shader-performance-overrides)

xemfg only downloads their releases from GitHub and checks their hashes; it ships none of their files.

## License

MIT
