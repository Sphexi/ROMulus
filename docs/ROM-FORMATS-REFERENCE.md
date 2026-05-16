# Retro Console ROM Formats, Extensions, and Naming Conventions

A canonical reference for identifying, deduplicating, and organizing this multi-console ROM collection. Intended for use by automated tooling and Claude Code.

> **Scope:** file extensions per console family, naming conventions (No-Intro / TOSEC / GoodTools / Redump), region/language/status tags, duplicate-detection considerations, common folder-name aliases used by RetroArch / Batocera / EmulationStation / RetroPie / Onion / muOS, and the established DAT-based dedup tools.

> **Accuracy posture:** filename and extension matching is heuristic. Authoritative dedup requires hash matching (CRC32 / MD5 / SHA-1) against No-Intro, Redump, or TOSEC DAT files.

---

## 1. File Extensions Per Console Family

Columns:
- **Native/raw dump** — the primary on-disk format produced by a clean dump.
- **Compressed / archive** — formats most emulators accept directly (not a wrapper like `.zip`).
- **Container / proprietary** — emulator-specific or distribution-specific containers.
- **Disc siblings** — additional files that travel with a disc image (cue sheets, audio tracks, subchannel data, etc.).

`.zip` and `.7z` are accepted as wrappers by virtually every modern emulator for cartridge-based systems. They are listed only where they have format-specific semantics (e.g. MAME requires `.zip`).

### 1.1 Nintendo

| System | Native / raw | Compressed / archive | Proprietary container | Disc siblings | Notes |
|---|---|---|---|---|---|
| NES / Famicom | `.nes`, `.unf`/`.unif`, `.fds` (Famicom Disk System) | `.zip`, `.7z` | `.nes` is the iNES/NES 2.0 header format | — | `.nes` files have a 16-byte header describing mapper/PRG/CHR. `.fds` is FDS disk images (often headered with `FDS\x1a`). |
| Famicom Disk System | `.fds` | — | — | — | Requires `disksys.rom` BIOS. |
| Satellaview (BS-X) | `.bs`, `.sfc` | `.zip` | — | — | Requires `BS-X.bin` BIOS. |
| Sufami Turbo | `.st`, `.sfc` | `.zip` | — | — | Requires `STBIOS.bin`. |
| SNES / Super Famicom | `.sfc` (raw, modern preferred), `.smc` (often headered, +512 bytes), `.fig`, `.swc` | `.zip`, `.7z` | — | — | `.sfc` is the modern preferred extension. `.smc`/`.fig`/`.swc` indicate copier dumps; `.smc` and `.swc` are functionally identical. Detect 512-byte copier header by checking offset 8/9 for `0xAA 0xBB`. |
| N64 | `.z64` (big-endian, native), `.n64` (little-endian), `.v64` (byte-swapped), `.rom`, `.bin` | `.zip`, `.7z` | — | — | Magic bytes: `80 37 12 40` = z64; `37 80 40 12` = v64; `40 12 37 80` = n64. Most modern emulators auto-detect. |
| Nintendo 64DD | `.ndd`, `.d64` (rare) | `.zip` | — | — | 64DD disk dumps; require IPL ROM. |
| GameCube | `.iso`, `.gcm` (raw), `.gcz` (Dolphin compressed), `.rvz` (modern Dolphin compressed), `.ciso`, `.wia`, `.wbfs` (rare on GC), `.nkit.iso`, `.nkit.gcz` | `.zip` (Dolphin reads zipped images) | `.rvz` Dolphin-only; `.gcz` Dolphin-only; `.nkit` is preservation-friendly trim format | — | Dolphin's preferred modern format is `.rvz`. |
| Wii | `.iso`, `.wbfs`, `.rvz`, `.wia`, `.ciso`, `.gcz`, `.wad` (Virtual Console / channels), `.nkit.iso` | `.zip` | `.rvz` Dolphin; `.wbfs` for hardware USB loaders; `.wad` is installable channel | — | `.wad` is an installable WAD package, not a disc image. |
| Wii U | `.wud` (raw), `.wux` (compressed WUD), loose `code/content/meta` folder dump (loadiine), `.wua` (Cemu archive) | `.zip` | `.wux` is a Wii U-specific compressed image; `.wua` is Cemu's archive | NUS format = folder of `.app`, `.h3`, `.tmd`, `.tik`, `.cert` | `.rpx` is the Wii U executable inside `code/`; not a ROM container. |
| Switch | `.xci` (cartridge dump), `.nsp` (eShop / submission package) | `.nsz` (Zstandard-compressed NSP), `.xcz` (compressed XCI) | `.nca` (internal content archives), `.ncz` (compressed NCA) | — | NCAs are usually inside NSP/XCI, not loose. |
| Game Boy | `.gb` | `.zip`, `.7z` | — | — | |
| Game Boy Color | `.gbc` (often interchangeable with `.gb`) | `.zip`, `.7z` | — | — | |
| Game Boy Advance | `.gba` | `.zip`, `.7z` | — | — | `.elf`, `.mb` (multiboot) also exist for homebrew. |
| Nintendo DS | `.nds` | `.zip`, `.7z` | `.dsi` (DSiWare), `.ids`, `.app` | — | |
| Nintendo 3DS | `.3ds` (cartridge image, often called `.3DS`/CCI), `.cia` (installable), `.cxi` (executable image, internal) | `.zip` | `.cia` is the installable container; `.cci`/`.3ds` is the cartridge image; `.cxi` is the executable component inside | — | `.cxi` is technically an internal NCCH partition, occasionally distributed standalone. |
| Virtual Boy | `.vb`, `.vboy` | `.zip`, `.7z` | — | — | `.bin` was historically used but conflicts with Mega Drive; `.vb`/`.vboy` are preferred. |
| Pokémon Mini | `.min` | `.zip`, `.7z` | `.minc` (companion color file) | — | |
| Game & Watch | `.mgw` | — | — | — | Simulator-only via `gw-libretro`/`lr-gw`; recreations rather than original ROMs. MAME also has G&W LCD artwork sets in `.zip`. |

### 1.2 Sega

| System | Native / raw | Compressed / archive | Proprietary container | Disc siblings | Notes |
|---|---|---|---|---|---|
| SG-1000 | `.sg`, `.bin`, `.rom` | `.zip` | — | — | |
| SC-3000 | `.sc`, `.bin` | `.zip` | — | — | |
| Master System | `.sms`, `.bin` | `.zip` | — | — | |
| Game Gear | `.gg` | `.zip` | — | — | |
| Genesis / Mega Drive | `.md`, `.gen`, `.bin`, `.smd` (interleaved copier format), `.68k` | `.zip` | `.smd` is interleaved (Super Magic Drive copier). `.bin`/`.md`/`.gen` are typically raw and equivalent. | — | Tools like genesis-rom-utility convert between bin and smd. |
| 32X | `.32x`, `.bin` | `.zip` | — | — | |
| Sega CD / Mega CD | — | — | — | `.bin` + `.cue`, `.iso`, `.chd` (preferred for compression), `.toc`, `.ccd` + `.img` + `.sub` | Audio tracks are usually separate `.bin` files referenced from `.cue`. |
| Saturn | — | — | — | `.cue` + `.bin`, `.iso`, `.chd`, `.mds` + `.mdf`, `.ccd` + `.img` + `.sub`, `.toc`, `.m3u` for multi-disc | |
| Dreamcast | — | — | `.gdi` (Dreamcast GD-ROM image, with `.bin`/`.raw` track files), `.cdi` (CD-R distribution format), `.chd` (preferred archival) | `.gdi` + numbered `.bin`/`.raw` tracks; `.cue` + `.bin` also seen | `.gdi` is the canonical preservation-grade format; `.cdi` is for burning. |
| Pico (Sega Pico) | `.bin`, `.md` | `.zip` | — | — | Same dump format as Mega Drive. |

### 1.3 Sony

| System | Native / raw | Compressed / archive | Proprietary container | Disc siblings | Notes |
|---|---|---|---|---|---|
| PlayStation 1 | — | — | `.pbp` (EBOOT, used by PSP/PS3 to play PS1), `.chd` (MAME compression, supports multi-track) | `.bin` + `.cue` (multi-track common), `.iso` (single-track only), `.img` + `.ccd` + `.sub`, `.mds` + `.mdf`, `.toc` + `.bin`, `.ecm`, `.m3u` for multi-disc | `.cue` references one or more `.bin` files (one per track). Multi-bin is normal for games with CDDA. |
| PlayStation 2 | `.iso` (preferred by PCSX2), `.bin` + `.cue` | — | `.chd`, `.cso` (limited), `.gz` (PCSX2 compressed) | `.bin`+`.cue` | PCSX2 historically struggles with raw `.bin` — `.iso` or `.chd` preferred. |
| PSP | `.iso` | `.cso` (compressed ISO), `.zso`, `.dax` (older compressed), `.jso` | `.pbp` (EBOOT.PBP — used for PSN, Minis, PS1 classics on PSP) | — | `.pbp` is the PSN/Minis/UMD-converted format used by the XMB. |
| PS3 | `.iso` (PS3 ISO Tool), folder dumps (`PS3_GAME/USRDIR/`), `.pkg` (PSN install) | — | `.pkg` (signed install package), `.pup` (firmware update), `.sprx` (libraries), `.self`/`.elf` (executables), GoD format folder for HDD installs | — | RPCS3 typically uses extracted folders or `.pkg`. |
| PS Vita | — | — | `.vpk` (homebrew install), `.pkg` + `work.bin` (NoNpDRM legit), `MaiDump` folder format | — | |
| PS Minis | `.pbp` | — | Distributed via PSN as `.pkg` containing `.pbp` | — | PSP-format Minis are PBP files. |

### 1.4 Microsoft

| System | Native / raw | Compressed / archive | Proprietary container | Notes |
|---|---|---|---|---|
| Xbox (original) | `.iso` (XISO/redump-style ISO), `.xbe` (executable) | — | Extracted folder dumps with `default.xbe` | XISO is a reformatted ISO with the FATX-readable structure. |
| Xbox 360 | `.iso` (XGD2/XGD3 disc image), `.xex` (executable) | — | `GOD` (Games on Demand, folder structure), `.xbla` (Xbox Live Arcade — actually folder-based content packages on hard drive) | `.xex` is the Xbox 360 executable — shipped at `default.xex` inside the disc. |

### 1.5 Atari

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| Atari 2600 | `.a26`, `.bin` | `.zip` | `.bin` is universally accepted; `.a26` is Stella's canonical extension. |
| Atari 5200 | `.a52`, `.bin`, `.rom` | `.zip` | |
| Atari 7800 | `.a78`, `.bin` | `.zip` | `.a78` includes a header describing cart type. |
| Atari 8-bit (400/800/XL/XE) | `.atr` (disk), `.atx` (disk with copy protection), `.xex`/`.exe`/`.obx` (executable), `.cas` (tape), `.car`/`.rom`/`.bin` (cartridge) | `.zip` | `.atr`, `.atx` are disk images; `.xex` is the Atari 8-bit executable. |
| Atari Lynx | `.lnx` (with header), `.lyx`/`.o` (raw, no header) | `.zip` | `.lnx` has a 64-byte header. |
| Atari Jaguar | `.j64`, `.jag`, `.rom`, `.bin`, `.abs`, `.cof` | `.zip` | `.j64` is the most common modern extension. CD: `.cdi`. |
| Atari ST (16/32-bit) | `.st` (raw disk), `.msa` (Magic Shadow Archive), `.stx` (Pasti, with copy protection), `.dim` | `.zip` | Note: `.st` here is Atari ST disk image, **not** Sufami Turbo. Disambiguate by parent folder. |

### 1.6 NEC

| System | Native / raw | Compressed / archive | Disc siblings | Notes |
|---|---|---|---|---|
| PC Engine / TurboGrafx-16 | `.pce`, `.bin` | `.zip` | — | |
| SuperGrafx | `.sgx`, `.pce` | `.zip` | — | |
| PC Engine CD / TG-CD | — | — | `.cue` + `.bin`, `.iso`, `.chd`, `.ccd` + `.img`, `.toc` | Requires `syscard3.pce` BIOS. |
| PC-FX | — | — | `.cue` + `.bin`, `.chd`, `.toc` | CD-only system. |
| PC-88 | `.d88`, `.88d`, `.t88`, `.cmt` | `.zip` | — | `.d88` disk images; `.t88` tape. |
| PC-98 | `.hdi`, `.hdm`, `.hdf`, `.fdi`, `.d88`, `.nhd`, `.fdd` | `.zip` | — | `.hdi` is a hard-disk image; `.fdi`/`.hdm` are floppy. |

### 1.7 SNK

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| Neo Geo (AES/MVS) | Multi-file MAME-style `.zip` (e.g. `mslug.zip`) | `.zip` (required), `.7z` (MAME 0.155+) | Per MAME romsets; loose `.bin` rarely used. Requires `neogeo.zip` BIOS. |
| Neo Geo CD | — | — | `.cue` + `.bin`, `.iso`, `.chd`, `.ccd`+`.img` |
| Neo Geo Pocket | `.ngp` | `.zip`, `.7z` | |
| Neo Geo Pocket Color | `.ngc`, `.npc` | `.zip`, `.7z` | `.ngc` collides with Nintendo GameCube `.gcm` aliases — disambiguate by folder. |

### 1.8 Bandai

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| WonderSwan | `.ws` | `.zip`, `.7z` | |
| WonderSwan Color | `.wsc` | `.zip`, `.7z` | Some sets also use `.pc2`. |

### 1.9 Commodore

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| C64 | `.d64` (5.25" disk, ~171 KB), `.d71`, `.d81`, `.t64` (tape), `.tap` (tape, raw), `.prg` (program), `.p00`, `.crt` (cartridge) | `.zip`, `.gz` | |
| C128 | Same as C64 + `.d71`, `.d81` | `.zip` | |
| VIC-20 | `.prg`, `.crt`, `.t64`, `.tap`, `.d64` | `.zip` | |
| PET | `.prg`, `.t64`, `.tap`, `.d64` | `.zip` | |
| Plus/4 / C16 | `.prg`, `.d64`, `.t64`, `.tap`, `.crt` | `.zip` | |
| Amiga (500/1200/etc.) | `.adf` (disk), `.adz` (gzipped ADF), `.ipf` (Interchangeable Preservation Format, with copy protection), `.dms` (disk masher), `.hdf`/`.hdz` (hard disk), `.lha` (whdload archives) | `.zip` | WHDLoad packs are typically `.lha` archives; require `whdload.key`. |
| Amiga CD32 / CDTV | — | — | `.iso`, `.cue` + `.bin`, `.ccd`+`.img`+`.sub`, `.nrg`, `.mds`+`.mdf`, `.chd` |

### 1.10 Amstrad

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| Amstrad CPC | `.dsk` (disk), `.cdt` (tape — same chunked format as TZX), `.cpr` (cartridge, GX4000) | `.zip` | |
| Amstrad GX4000 | `.cpr` | `.zip` | |

### 1.11 Sinclair

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| ZX Spectrum | `.tap` (basic tape), `.tzx` (full-fidelity tape with timing/turbo loaders), `.sna` (snapshot), `.z80` (snapshot), `.dsk`, `.trd` (TR-DOS disk), `.scl`, `.szx`, `.rzx` (input recording) | `.zip` | TZX is the preservation-grade tape format. |
| ZX81 | `.p`, `.81`, `.tzx` | `.zip` | |

### 1.12 MSX

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| MSX1 / MSX2 / MSX2+ / Turbo R | `.rom` (cartridge), `.dsk` (disk), `.cas` (tape), `.mx1`, `.mx2`, `.m3u` (for multi-disk games) | `.zip` | openMSX/blueMSX are the canonical emulators. |

### 1.13 Sharp

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| X1 | `.2d`, `.dx1`, `.tap`, `.cmt` | `.zip` | |
| X68000 | `.dim`, `.img`, `.d88`, `.88d`, `.hdm`, `.dup`, `.2hd`, `.xdf`, `.hdf`, `.cmd`, `.m3u` | `.zip`, `.7z` | `.hdm` and `.xdf` are the most common floppy formats. |

### 1.14 Other classic / mini consoles

| System | Native / raw | Compressed / archive | Notes |
|---|---|---|---|
| Fairchild Channel F | `.bin`, `.chf`, `.rom` | `.zip` | |
| Magnavox Odyssey 2 / Videopac | `.bin` | `.zip` | |
| Mattel Intellivision | `.int`, `.bin`, `.rom`, `.itv` | `.zip` | `.int` includes a header. |
| ColecoVision | `.col`, `.bin`, `.rom` | `.zip` | |
| Vectrex | `.vec`, `.bin`, `.gam` | `.zip` | |
| Watara Supervision | `.sv`, `.bin` | `.zip`, `.7z` | |
| Tiger Game.com | `.tgc`, `.bin` | `.zip`, `.7z` | |
| Casio PV-1000 | `.bin` | `.zip` | |
| Bally Astrocade | `.bin` | `.zip` | |
| RCA Studio II | `.bin` | `.zip` | |
| Elektronika BK | `.bin` | `.zip` | |

### 1.15 Arcade

Arcade ROMs are generally **multi-file** romsets (one zip = one game with many internal `.bin` files for individual ROM chips). The on-disk format is determined by the emulator's romset version, not by the user.

| Platform | Container | Disc / extra | Notes |
|---|---|---|---|
| MAME | `.zip` (required), `.7z` (≥ MAME 0.155) | `.chd` for HDD/CD/laserdisc components, in a folder named after the parent ROM | Romsets are split / merged / non-merged. FBNeo only accepts non-merged. |
| FinalBurn Neo (FBNeo) | `.zip` | `.chd` for CD games | Subset of MAME with its own DAT. |
| CPS1 / CPS2 / CPS3 | `.zip` (MAME-format) | — | Same multi-file zip format as MAME. CPS3 also has CHD for some games. |
| NAOMI / NAOMI 2 | `.zip` (MAME-format) | `.chd` for GD-ROM-based titles | Requires `naomi.zip`, `naomi2.zip`, `naomigd.zip` BIOS. |
| Atomiswave | `.zip` | — | Requires `awbios.zip` BIOS (placed under `bios/dc/`). |
| Sega Model 2/3 | `.zip` | — | Specific emulators (Supermodel, Model2). |
| Taito Type X / X2 | Folder dumps | — | PC-based; not a traditional ROM. |

### 1.16 Other systems

| System | Native / raw | Compressed / archive | Disc siblings | Notes |
|---|---|---|---|---|
| 3DO | — | — | `.iso`, `.bin` + `.cue`, `.chd` | |
| CD-i (Philips) | — | — | `.bin` + `.cue`, `.chd`, `.iso` | CHD via MAME/MESS; original distribution is bin/cue. |
| Daphne / Hypseus (laserdisc) | `.daphne` (folder with `.txt`, `.frm`, `.dat`) | — | — | `.daphne` is a folder, not a file. |
| ScummVM | `.scummvm` (text hook file) | — | Each game is a folder of original assets | `.scummvm` is a one-line text file naming the target game ID. |
| DOS / DOSBox | Folder dumps with `.exe`, `.bat`, `.com` | `.zip` (eXoDOS-style) | — | Often distributed via eXoDOS / DOSBox config. |
| Pico-8 | `.p8` (cart, source-readable), `.p8.png` (cart embedded in PNG) | — | — | `.p8.png` is a real PNG with cart data steganographically embedded. |
| TIC-80 | `.tic` | — | — | |
| OpenBOR | `.pak` | — | — | Folder layout often required. |
| EasyRPG | `.ldb`, `.lmt`, `.lmu` (RPG Maker 2000/2003 project), distributed as folder | `.zip` | — | EasyRPG plays a folder, not a file. |

---

## 2. Naming Conventions

There are four major filename conventions in widespread use. Tools that consume DAT files (clrmamepro, RomVault, Retool, igir) understand all four; matching against a DAT is what actually identifies a ROM.

### 2.1 No-Intro (cartridge ROMs)

**Filename grammar (left to right, only Title and Region mandatory):**

```
[BIOS] Title (Region) (Languages) (Version) (DevStatus) (Additional) (Special) (License) [Status]
```

**Title** uses full English game title. Articles (`The`, `A`) move to the end: `Legend of Zelda, The`.

**Region tag** uses **full country names** (not codes) — this is the biggest visual difference from older conventions:

| Tag | Meaning |
|---|---|
| `(USA)` | Released in North America (Canada usually folded in) |
| `(Europe)` | Released in 2+ European countries |
| `(Japan)` | Released in Japan |
| `(World)` | Released in all 3 major territories |
| `(Australia)`, `(Brazil)`, `(Canada)`, `(China)`, `(France)`, `(Germany)`, `(Hong Kong)`, `(Italy)`, `(Korea)`, `(Netherlands)`, `(Spain)`, `(Sweden)` | Single-region releases |
| `(USA, Europe)` | Combined releases listed comma-separated |
| `(Asia)`, `(Latin America)`, `(Scandinavia)` | Multi-region groupings |
| `(Unknown)` | Region undetermined |

**Languages tag** uses ISO 639-1 codes, two-letter, first letter uppercase, separated by commas with no spaces. Only included when more than one language is present:

`(En,Fr,De,Es,It)` — English, French, German, Spanish, Italian.

Order in real No-Intro filenames roughly follows: En, Ja, Fr, De, Es, It, Nl, Pt, Sv, No, Da, Fi, Zh, Ko, Pl.

**Version tag:** `(Rev 1)`, `(Rev 2)` for revisions; `(v1.1)`, `(v1.10)` for explicit version numbers; `(Beta)`, `(Beta 2)`, `(Proto)`, `(Proto 1)`, `(Sample)`, `(Demo)` for development/sample status. Only added when not v1.0/initial.

**License tag:** `(Unl)` for unlicensed; `(Pirate)`, `(Aftermarket)` for unauthorized releases; `(Homebrew)` for community releases.

**Special tags:** `(Virtual Console)`, `(Switch Online)`, `(GameCube Edition)`, `(Wii)`, `(Disney Collection)`, etc. — annotate distribution channel.

**Status tag** (square brackets, end of filename): `[BIOS]` flags BIOS dumps; `[b]` is occasionally seen for known-bad dumps but No-Intro generally rejects bad dumps rather than tagging them.

**Multi-disc:** `(Disc 1)`, `(Disc 2)` etc. (no `of N` qualifier in No-Intro style).

Example: `Final Fantasy VII (USA) (Disc 1) (Rev 1).bin`

### 2.2 Redump (disc-based games)

Redump follows No-Intro's overall structure but adds disc-specific tags:

- `(Disc 1)`, `(Disc 2)`, `(Disc 3)` for multi-disc games (no `of N`)
- `(Rev 1)`, `(Rev 2)` for revisions in release order
- `(Alt)` when two releases differ minutely (e.g. different audio track lengths) — the later release gets `(Alt)`
- `(Rerelease)` when PVD timestamp shows ≥6 months gap from initial release
- `(Sample)`, `(Demo)`, `(Trade Demo)`, `(Beta)`, `(Proto)` for non-final releases
- `(Greatest Hits)`, `(Platinum)`, `(Player's Choice)` for budget rereleases
- `(Premium Package)`, `(Limited Edition)` for special editions
- `(Bonus Disc)` for bundled extras

Region tags identical to No-Intro. Languages tags identical.

Example: `Metal Gear Solid (USA) (Disc 1) (Rev 1).cue`

### 2.3 TOSEC (everything, especially home computers)

**Filename grammar:**

```
Title version (demo)(date)(publisher)(system)(video)(country)(language)(copyright)(devstatus)(media type)(media label)[dump flags][more info]
```

**Mandatory minimum:** `Title (date)(publisher)`.

The `date` is `YYYY` or `YYYY-MM-DD` and is **mandatory** — this is the most visible TOSEC trait. Unknown dates use `19xx`, `200x`, etc.

**Country tags use 2-letter codes**, not full names: `(US)`, `(EU)`, `(JP)`, `(GB)`, `(DE)`, `(FR)`, `(IT)`, `(ES)`, `(NL)`, `(SE)`, `(AU)`, `(BR)`, `(CN)`, `(KR)`. Multi-region: `(US-EU)`. World: no special tag, just the multi-region list.

**Language:** `(en)`, `(de)`, `(fr)` — lowercase, ISO 639-1.

**Copyright status:** `(CW)` cardware, `(FW)` freeware, `(GW)` giftware, `(LW)` licenseware, `(PD)` public domain, `(SW)` shareware, `(SW-R)` shareware registered.

**Development status:** `(alpha)`, `(beta)`, `(preview)`, `(pre-release)`, `(proto)`.

**Media type:** `(Disc 1 of 2)`, `(Disk 1 of 5)`, `(Side A)`, `(Side B)`, `(Tape 1 of 2)`, `(Part 1 of 3)`. **TOSEC uses the `1 of N` form** — distinct from No-Intro/Redump.

**Dump flags (square brackets), in alphabetical order first, then dump-process flags:**

| Flag | Meaning |
|---|---|
| `[cr]` | Cracked (copy-protection removed) |
| `[f]` | Fixed (e.g. `[f NTSC]`, `[f save]`) |
| `[h]` | Hacked (general modification) |
| `[m]` | Modified (general hack) |
| `[p]` | Pirated |
| `[t]` | Trained (cheat menu inserted) |
| `[tr]` | Translated (`[tr en]`, `[tr de-fr]`) |
| `[o]` | Overdump (extra junk bytes) |
| `[u]` | Underdump (incomplete) |
| `[v]` | Virus-infected |
| `[b]` | Bad dump (corrupt) |
| `[a]` | Alternate version |
| `[!]` | Verified good (rare in TOSEC, more GoodTools) |

**More info flag** (free-form square brackets): `[docs]`, `[req TRS-DOS]`, `[non-working]`, `[source code]`.

Example: `Bubble Bobble (1987)(Firebird Software)(GB)[cr][t +5 Maverick]`

### 2.4 GoodTools (legacy, cartridge ROMs)

GoodTools sets (GoodNES, GoodSNES, GoodGB, GoodGen, etc.) predate No-Intro and use compact codes. Many older ROM collections still carry these flags.

**Country / region codes:**

| Code | Region |
|---|---|
| `(U)` | USA |
| `(E)` | Europe |
| `(J)` | Japan |
| `(JU)` | Japan + USA |
| `(UE)` | USA + Europe |
| `(JUE)` | World |
| `(W)` | World |
| `(F)` | France |
| `(G)` | Germany |
| `(I)` | Italy |
| `(S)` | Spain |
| `(K)` | Korea |
| `(C)` | China |
| `(NL)`/`(H)` | Netherlands / Holland |
| `(B)` | Brazil |
| `(A)` | Australia |
| `(As)` | Asia |
| `(Unk)` | Unknown |
| `(PD)` | Public domain |
| `(Unl)` | Unlicensed |

**Status / dump codes (square brackets):**

| Code | Meaning |
|---|---|
| `[!]` | Verified good dump (the goal) |
| `[b]`, `[b1]`, `[b2]` | Bad dump (numbered if multiple bad variants) |
| `[h]`, `[h1]`, `[hI]`, `[hM]` | Hack (`hI` = intro hack, `hM` = menu hack) |
| `[t]`, `[t1]`, `[t+N]` | Trainer (`t+5` = with 5 trainers) |
| `[a]`, `[a1]`, `[a2]` | Alternate dump |
| `[o]`, `[o1]` | Overdump |
| `[f]`, `[f1]` | Fixed |
| `[p]`, `[p1]` | Pirate |
| `[T+Eng]`, `[T-Eng]` | English translation (+ = newest, - = older) — also `[T+Fre]`, `[T+Ger]`, etc. |
| `[BIOS]` | BIOS file |
| `(M3)`, `(M5)` | Multilingual; number is language count |
| `(V1.0)`, `(V1.1)`, `(REV01)` | Version |
| `(Beta)`, `(Prototype)`, `(Proto)`, `(Demo)`, `(Sample)` | Pre-release |

Example: `Final Fantasy V (J) [T+Eng2.0_RPGe].smc`

### 2.5 Side-by-side comparison

| Aspect | No-Intro | Redump | TOSEC | GoodTools |
|---|---|---|---|---|
| Region tag | Full names: `(USA)` | Full names: `(USA)` | 2-letter: `(US)` | 1–3 letter: `(U)` |
| Language tag | `(En,Fr,De)` | `(En,Fr,De)` | `(en-fr-de)` | embedded in name `[T+Eng]` |
| Multi-disc | `(Disc 1)` | `(Disc 1)` | `(Disc 1 of 3)` | rarely used (cart-era) |
| Date | not used | not used | mandatory `(1987)` | sometimes `(1987)` |
| Verified good | implied (no flag) | implied | implied | explicit `[!]` |
| Bad dump | excluded | excluded | `[b]` | `[b]` |
| Hack | rarely included | not included | `[h]` / `[m]` | `[h]` |
| Translation | `(Aftermarket)` if listed | not used | `[tr en]` | `[T+Eng]` |
| Brackets meaning | `[BIOS]`, `[Status]` | rarely used | dump flags | dump/quality flags |
| Parentheses meaning | metadata | metadata | metadata + dump | metadata only |

### 2.6 Recalbox / EmulationStation / aggregator-specific extras

Not part of any formal spec, but commonly seen:

- `(Hack)` or `(Romhack)` — community modification
- `(T-En)` — community translation patch (variant of `[T+Eng]`)
- `(Aftermarket)` — modern unofficial cart release
- Trailing `[Zorlon]`, `[Cool]`, `[CoolROMs]` — release group / source watermark
- `(by AuthorName)` — homebrew author attribution

---

## 3. Duplicate Detection Considerations

### 3.1 What "the same game" means depends on intent

Filename-based dedup is heuristic. The same logical title can ship as multiple files that are byte-different but semantically equivalent — and as multiple files that are byte-identical but should be kept separate (e.g. one in `gba/`, one in `gbah/`).

### 3.2 Common false-distinct cases (same content, different file)

| Variant | Example | Detection signal |
|---|---|---|
| Same ROM, different extension | `Final Fantasy VI.smc` ≡ `Final Fantasy VI.sfc` (when neither is headered) | Hash of content (CRC32/MD5/SHA1) matches |
| Headered vs unheadered | `Zelda.smc` (with 512-byte SMC header) vs `Zelda.sfc` (raw) | Hash differs by exactly 512 bytes; check magic at offset 8/9 (`AA BB`) |
| N64 byte order | `game.z64` vs `game.v64` vs `game.n64` | Different endianness; reorder + rehash to compare |
| Inside vs outside zip | `game.zip` containing `game.nes` vs loose `game.nes` | Hash extracted content |
| Re-compression | `game.iso` vs `game.chd` (lossless compression of same disc) | `chdman info` reports source CRC; or extract and hash |
| Re-container | PSP `.iso` vs `.cso` of same game | Decompress `.cso` and compare; CSO is lossless |
| Wii `.iso` vs `.rvz`/`.wbfs`/`.nkit.iso` | Same disc, different container | Dolphin shows "Internal Disc Hash"; compare against Redump DAT |
| Multi-bin vs single-bin PS1 | Multi-track `.bin`+`.cue` vs combined-track `.bin` | Cue determines logical equivalence; CHD works either way |

### 3.3 Common true-distinct cases (different content, same logical game)

| Variant | Example | Should they be deduped? |
|---|---|---|
| Region | `Chrono Trigger (USA)` vs `Chrono Trigger (Japan)` | Usually no — gameplay/content differs |
| Revision | `Pokemon Red (USA, Europe) (Rev 1)` vs `(Rev 0)` | Depends on user preference; Retool can pick "newest revision" automatically |
| Beta / Proto / Sample | `Sonic the Hedgehog (Proto)` | Usually keep separately as preservation-distinct |
| Translation | `Mother 3 (Japan)` vs `Mother 3 (Japan) (T-En by Tomato)` | Usually keep both; translations are distinct artifacts |
| Hack | `Super Mario World` vs `Super Mario World (Hack) (Kaizo)` | Always keep separate; users explicitly want both |
| Multi-disc | `FFVII (Disc 1)` + `(Disc 2)` + `(Disc 3)` | Three files, one game — link via `.m3u` playlist |
| Virtual Console / re-release | `Super Mario Bros. (USA)` vs `Super Mario Bros. (Virtual Console)` | The VC version often has tweaks; keep both if preserving |

### 3.4 Why hashes are the gold standard

Filename matching breaks down whenever:

1. The file has been renamed by a casual collector (`zelda.zip`, `Zelda OOT.z64`).
2. The file uses a different convention (TOSEC date present, No-Intro absent).
3. The extension was changed without re-dumping (`.smc` → `.sfc` rename).
4. The file is bundled inside a different archive type (`.zip` vs `.7z`).
5. Two different dumps of the same game exist (one bad, one good — same name).

Hashes — specifically **CRC32, MD5, and SHA-1 from a No-Intro / Redump / TOSEC DAT file** — are the only reliable identity. Per No-Intro and Redump DAT specs, every entry carries all three. The DAT itself is the source of truth. CRC32 is fast but has a 4.29 billion keyspace; pair with file size or another hash to reduce collision risk. SHA-1 is what No-Intro publishes and what most modern tools (igir, RomVault) match on.

**Practical workflow:**

1. Hash every file (CRC32 + SHA-1, both are cheap to compute together).
2. Look up SHA-1 in DAT: if found, you have an authoritative `(Region, Language, Revision, ...)` identity.
3. For files inside `.zip`/`.7z`, hash the contained ROM, not the archive.
4. For disc images, hash each track (bin) and the cue text — Redump DATs are track-level.
5. Headered / endian-swapped files require pre-normalization before hashing (strip SMC header; byte-swap N64 to z64).

### 3.5 Filename-only dedup is still useful

Despite the caveats, a filename-only first pass catches the obvious cases at near-zero cost:

- Normalize: lowercase, strip extension, remove trailing tags `(...)` and `[...]`, collapse whitespace.
- Group by normalized stem.
- Within a group, surface conflicts for human / hash-based review.

Always flag filename-based matches as **heuristic** in any tool output.

---

## 4. Folder Name Conventions

There is no single standard. Different frontends/distros use different short names. Below are the most common aliases observed across RetroArch, Batocera, EmulationStation, RetroPie, Onion (Miyoo Mini), muOS, ArkOS, and ROCKNIX.

### 4.1 Nintendo aliases

| System | Common folder names |
|---|---|
| NES / Famicom | `nes`, `famicom`, `fc`, `Nintendo - Nintendo Entertainment System` (RetroArch playlist) |
| FDS | `fds`, `famicomdiskystem` |
| SNES / SFC | `snes`, `sfc`, `superfamicom`, `supernintendo` |
| Satellaview | `satellaview`, `bsx` |
| Sufami Turbo | `sufami` |
| N64 | `n64`, `nintendo64` |
| 64DD | `n64dd`, `64dd` |
| GameCube | `gc`, `gamecube`, `ngc` |
| Wii | `wii` |
| Wii U | `wiiu` |
| Switch | `switch`, `nx` |
| Game Boy | `gb`, `gameboy` |
| Game Boy Color | `gbc`, `gameboycolor` |
| Game Boy Advance | `gba`, `gameboyadvance` |
| DS | `nds`, `ds` |
| 3DS | `3ds`, `n3ds` |
| Virtual Boy | `vb`, `virtualboy` |
| Pokémon Mini | `pokemini`, `pmini` |
| Game & Watch | `gameandwatch`, `gw` |

### 4.2 Sega aliases

| System | Common folder names |
|---|---|
| SG-1000 | `sg-1000`, `sg1000` |
| Master System | `mastersystem`, `sms` |
| Game Gear | `gamegear`, `gg` |
| Genesis / Mega Drive | `genesis`, `megadrive`, `md`, `Sega - Mega Drive - Genesis` |
| Sega CD / Mega CD | `segacd`, `megacd` |
| 32X | `32x`, `sega32x` |
| Saturn | `saturn`, `ss`, `segasaturn` |
| Dreamcast | `dreamcast`, `dc` |
| Pico | `pico`, `segapico` |
| SC-3000 | `sc-3000`, `sc3000` |

### 4.3 Sony aliases

| System | Common folder names |
|---|---|
| PS1 | `psx`, `ps1`, `playstation`, `psone` |
| PS2 | `ps2`, `playstation2` |
| PSP | `psp` |
| PS3 | `ps3` |
| PS Vita | `psvita`, `vita` |
| PS Minis | `psminis`, `pspminis` (often inside `psp`) |

### 4.4 Microsoft aliases

| System | Common folder names |
|---|---|
| Xbox | `xbox` |
| Xbox 360 | `xbox360` |

### 4.5 Atari aliases

| System | Common folder names |
|---|---|
| 2600 | `atari2600`, `a2600`, `2600` |
| 5200 | `atari5200`, `a5200`, `5200` |
| 7800 | `atari7800`, `a7800`, `7800` |
| 8-bit | `atari800`, `atari8`, `a800` |
| Lynx | `lynx`, `atarilynx` |
| Jaguar | `jaguar`, `atarijaguar` |
| ST | `atarist`, `st` |

### 4.6 NEC aliases

| System | Common folder names |
|---|---|
| PC Engine / TurboGrafx-16 | `pcengine`, `tg16`, `pce`, `turbografx16` |
| SuperGrafx | `pcenginesgx`, `sgx`, `supergrafx` |
| PC Engine CD / TG-CD | `pcenginecd`, `tg-cd`, `pcecd`, `turbografxcd` |
| PC-FX | `pcfx` |
| PC-88 | `pc88`, `pc-88` |
| PC-98 | `pc98`, `pc-98` |

### 4.7 SNK aliases

| System | Common folder names |
|---|---|
| Neo Geo (AES/MVS) | `neogeo` |
| Neo Geo CD | `neocd`, `neogeocd` |
| Neo Geo Pocket | `ngp`, `neogeopocket` |
| Neo Geo Pocket Color | `ngpc`, `neogeopocketcolor` |

### 4.8 Other aliases

| System | Common folder names |
|---|---|
| WonderSwan | `wonderswan`, `ws`, `wswan` |
| WonderSwan Color | `wonderswancolor`, `wsc`, `wswanc` |
| C64 | `c64`, `commodore64` |
| Amiga | `amiga`, `amiga500`, `amiga1200` |
| CD32 | `amigacd32`, `cd32` |
| CDTV | `cdtv`, `amigacdtv` |
| ZX Spectrum | `zxspectrum`, `zx`, `spectrum` |
| ZX81 | `zx81` |
| Amstrad CPC | `amstradcpc`, `cpc` |
| GX4000 | `gx4000`, `amstradgx4000` |
| MSX | `msx`, `msx1` |
| MSX2 | `msx2` |
| X68000 | `x68000`, `x68k` |
| 3DO | `3do` |
| CD-i | `cdi`, `philipscdi` |
| Daphne | `daphne`, `hypseus` |
| ScummVM | `scummvm`, `scumm` |
| DOS | `dos`, `pc`, `dosbox` |
| Pico-8 | `pico-8`, `pico8` |
| TIC-80 | `tic-80`, `tic80` |
| OpenBOR | `openbor` |
| EasyRPG | `easyrpg` |
| Channel F | `channelf`, `chanf` |
| Odyssey 2 | `odyssey2`, `o2em`, `videopac`, `odyssey` |
| Intellivision | `intellivision`, `intv` |
| ColecoVision | `colecovision`, `coleco` |
| Vectrex | `vectrex` |
| Watara Supervision | `supervision`, `watara` |
| Tiger Game.com | `gamecom`, `game.com` |
| Thomson MO/TO | `thomson`, `moto` |

### 4.9 Arcade umbrella

Arcade ROMs are split across many folder conventions because each emulator/board has its own romset:

| Folder | Contents |
|---|---|
| `mame` | Full MAME romset (all systems) |
| `arcade` | Generic — varies per distro; usually FBNeo or split MAME |
| `fbneo`, `fba`, `fbn` | FinalBurn Neo / FBA romset |
| `cps1`, `cps2`, `cps3` | Capcom Play System ROMs (MAME-format) |
| `capcom` | Sometimes used as umbrella for cps1/2/3 |
| `neogeo` | Neo Geo MVS/AES (MAME-format, requires `neogeo.zip` BIOS) |
| `neocd`, `neogeocd` | Neo Geo CD discs |
| `naomi`, `naomi2` | Sega NAOMI / NAOMI 2 (Flycast) |
| `atomiswave` | Sammy Atomiswave (Flycast) |
| `model2`, `model3` | Sega Model 2/3 (Supermodel) |
| `daphne` | Laserdisc (Dragon's Lair, Space Ace) |
| `hbmame` | Homebrew MAME romsets |
| `varcade` | Varcade — community-curated arcade subset |

### 4.10 The `h` suffix — ROM hacks

A folder ending in `h` (e.g. `gbah`, `snesh`, `mdh`, `nesh`, `gbch`, `gbh`) is a **separate library for ROM hacks / homebrew / translations**, parallel to the official-ROMs folder. This pattern is most prominent in muOS but appears informally elsewhere.

Recognized pairs:

| Official | Hacks |
|---|---|
| `gb` | `gbh` |
| `gbc` | `gbch` |
| `gba` | `gbah` |
| `nes` | `nesh` |
| `snes` | `snesh` |
| `n64` | `n64h` |
| `md` / `megadrive` | `mdh` |
| `gen` / `genesis` | `genh` |
| `gamegear` | `gamegearh` |

Why duplicate folders exist:

- Scrapers/playlists treat hacks differently (don't want them showing up in "official games" lists).
- Per-folder emulator settings can differ (e.g. hacks may need a different core or skip BIOS).
- Achievement systems (RetroAchievements) only validate against the official folder.

A dedup tool **should not** treat `gba/Game.gba` and `gbah/Game (Hack).gba` as duplicates of each other unless they are byte-identical.

### 4.11 RetroArch playlist (`.lpl`) names

When RetroArch builds playlists from a DAT, the playlist file is named after the DAT, e.g.:

```
Nintendo - Nintendo Entertainment System.lpl
Nintendo - Super Nintendo Entertainment System.lpl
Sega - Mega Drive - Genesis.lpl
Sony - PlayStation.lpl
NEC - PC Engine - TurboGrafx 16.lpl
```

These long names are the canonical "RetroArch system names" and are also used as thumbnail folder names (e.g. `thumbnails/Nintendo - Game Boy/Named_Boxarts/`).

---

## 5. Tooling Pointers

### 5.1 DAT file ecosystem

A **DAT file** is an XML/Logiqx-format database describing every known good dump for a system: filename, size, CRC32, MD5, SHA-1, optionally ROM internal name.

| Source | Coverage | Format |
|---|---|---|
| [No-Intro](https://datomatic.no-intro.org/) | Cartridge consoles, handhelds, digital store dumps | Logiqx XML, per-system |
| [Redump](http://redump.org/) | Optical-media consoles (CD, DVD, GD-ROM, BD) | Logiqx XML, per-system, track-level for CD |
| [TOSEC](https://www.tosecdev.org/) | Home computers, obscure systems, magazine cover-disks | Logiqx XML, per-system + per-category |
| [MAME](https://www.mamedev.org/) | Arcade | Built into MAME (`-listxml`); also published as DAT |
| [FBNeo](https://github.com/finalburnneo/FBNeo) | Arcade subset (CPS1/2/3, Neo Geo, etc.) | Logiqx XML |
| Hack-DAT-base | Community ROM hacks | Logiqx XML |

### 5.2 ROM managers

| Tool | Strength | Notes |
|---|---|---|
| clrmamepro | Universal, oldest, most flexible | Steep learning curve. Free but proprietary. |
| RomVault | Fast, large collections, TOSEC-friendly | Commercial license for advanced features. |
| RomCenter | Beginner-friendly, French project | Less actively developed. |
| ROMulus | Friendly UI for beginners | Development paused since 2022; slow with very large sets. |
| Retool (and Retool-Redux) | Pre-processes DATs to produce a single "best" version per game (region preference, revision preference, language preference, exclude categories) | Output is a custom DAT consumed by clrmamepro/RomVault. |
| igir | Cross-platform CLI, scriptable, modern | Good for automation pipelines. |
| DATROMTool | Cross-platform, supports Retool metadata | CLI-friendly. |
| SabreTools | DAT manipulation library | Powers many other tools. |
| RomValidator | Validates against No-Intro DATs, generates compliant DATs | Windows-only. |

### 5.3 Disc-format conversion tools

| Tool | Use |
|---|---|
| `chdman` (MAME) | Convert `.cue`+`.bin` / `.gdi` / `.iso` ↔ `.chd`. Lossless. The standard for CD/GD-ROM compression. |
| `nkit` | Convert Wii/GC ISO ↔ `.nkit.iso` (preservation-friendly trim). |
| `wit` (Wiimms ISO Tools) | Wii/GC disc image swiss-army knife. |
| Dolphin | Built-in convert to/from `.rvz`, `.gcz`, `.wia`, `.iso`. |
| `maxcso` / `ciso` | PSP `.iso` ↔ `.cso`/`.zso`. |
| `psx2psp` | Build PS1 `.pbp` from `.bin`/`.cue`/`.iso`. |
| `nsz` | Switch `.nsp`/`.xci` ↔ `.nsz`/`.xcz`. |

### 5.4 Practical dedup workflow

A reasonable pipeline for the collection at hand:

1. **Inventory pass.** Walk the tree, record `(path, size, ext, mtime)` and a fast hash (CRC32 or first/last 64KB SHA-1) for every file. Flag obvious archive containers for content-aware hashing.
2. **Normalize for hashing.** Strip SMC headers; byte-swap `.v64`/`.n64` to `.z64`; decompress one level (zip/7z) before hashing.
3. **Match against DATs.** Acquire current No-Intro + Redump + TOSEC + MAME DATs. For each file's full SHA-1 (or CRC32), look up the canonical name and system.
4. **First-cut filename dedup (heuristic, flagged).** Group by normalized stem (lowercase, strip tags). Surface conflicts.
5. **Hash-based dedup (authoritative).** Identical SHA-1 = byte-identical — choose one based on container preference (e.g. prefer `.chd` over `.bin`+`.cue`; prefer `.rvz` over `.iso`).
6. **Region/revision rollup with Retool.** Apply user preferences ("USA > Europe > Japan", "newest revision", "exclude Demos/Samples/Betas") to pick "the one" per logical title. Keep the discarded set in a quarantine folder rather than deleting.
7. **Multi-disc consolidation.** For CD games, generate `.m3u` playlists referencing each disc; group `bin`+`cue` track pairs.
8. **Folder placement.** Emit final tree into the user's frontend convention (RetroArch long names, Batocera shortnames, or Onion / muOS layout). Honor the `h`-suffix convention for hacks.

### 5.5 Caveats summarized

- **Filename matching is heuristic.** Always flag filename-only matches as such.
- **CRC32 alone has collision risk.** Pair with file size or a stronger hash.
- **DATs go stale.** Refresh from No-Intro / Redump / TOSEC before each major run.
- **Bad dumps lurk.** Anything not in a current DAT is either rare/homebrew/hack — or a bad dump. Retain rather than delete.
- **Containers are not content.** Two different `.zip` files can contain the same ROM (different compression level, different inner filename, different timestamp). Always hash the inner file.
- **Disc tracks are independent.** A Redump match is per-track. A `.bin`+`.cue` pair is only equivalent to a `.chd` if every track hash matches.
- **Hacks and translations are first-class.** Treat them as distinct titles; never silently dedupe a hack against the original.

---

## 6. Anbernic RG556 / RG406 Android — Target Layout for This Collection

This collection is destined for an **Anbernic Android handheld** (RG556 / RG406H/V/M family) running the **stock RGLauncher**. The findings below determine how we organize the share.

### 6.1 How RGLauncher actually works

The Anbernic stock Android launcher (variously called **RGLauncher**, **Retro Launcher**, or "Game Center" depending on firmware revision) is **not** folder-name-driven. Two facts that shape everything else:

1. **Fixed system catalog, user-set paths.** The launcher ships with a built-in catalog of consoles (NES, SNES, Mega Drive, PSX, Saturn, Dreamcast, …). For each system tile, the user opens it, presses Select → "ROM Setting" → "Set Path", and points the file picker at *any* folder on internal storage / SD card / USB-OTG. The folder can be called anything — `nes`, `NES`, `nintendo`, `bens-nes-stash` — the launcher reads what you point it at.
2. **No folder discovery.** RGLauncher will never auto-detect a folder named `lutro/` or `c64/` and offer to add it. The catalog is fixed at firmware build time and the user cannot extend it. Systems not in the catalog (Amiga, C64, ZX Spectrum, Lutro, EasyRPG, port engines, etc.) require a third-party frontend (Daijisho, ES-DE Android, Beacon, Pegasus) or direct emulator-app launch.

(Source: Joey's Retro Handhelds RGLauncher setup guides, Retro Game Corps RG556 setup, RetroHandhelds.gg RG406V/H setup, retrogamecorner/AnbernicAndroidFolders. Anbernic's *Linux* stock firmware on RG35XX/RG40XX is a different story — it hardcodes uppercase short names like `FC`, `SFC`, `MD`. That convention does **not** apply to the Android devices in scope here.)

### 6.2 Recommended naming convention

Because RGLauncher accepts any folder name, the question becomes "which convention works best across all the *other* tooling you might run on this device?" The dominant convention for Anbernic Android — used by the Anbernic-blessed [retrogamecorner/AnbernicAndroidFolders](https://github.com/retrogamecorner/AnbernicAndroidFolders) template, the [GlazedBelmont ES-DE Android custom-systems](https://github.com/GlazedBelmont/es-de-android-custom-systems) XMLs, and Daijisho default profiles — is the **lowercase long-form** convention:

| Console | Recommended folder | Reject |
|---|---|---|
| Sega Mega Drive / Genesis | `megadrive` | `genesis`, `md`, `gen` |
| TurboGrafx-16 / PC Engine | `pcengine` | `tg16`, `pce` |
| TG-CD / PC Engine CD | `pcenginecd` | `tg16cd`, `pcecd` |
| Master System | `mastersystem` | `sms` |
| Game & Watch | `gameandwatch` | `gw` |
| WonderSwan | `wonderswan` | `wswan`, `ws` |
| WonderSwan Color | `wonderswancolor` | `wswanc`, `wsc` |
| Pico-8 | `pico8` | `pico-8` (hyphen breaks some shells) |
| TIC-80 | `tic80` | `tic-80` |
| SG-1000 | `sg1000` | `sg-1000` |
| SC-3000 | `sc3000` | `sc-3000` |
| SNES / Super Famicom | `snes` | `sfc`, `superfamicom` |
| NES / Famicom | `nes` | `famicom`, `fc` |
| ColecoVision | `colecovision` | `coleco` |
| Atari Lynx | `lynx` | `atarilynx` |
| Saturn | `saturn` | `ss` |
| SuperGrafx | `supergrafx` | `sgfx` |
| Odyssey 2 / Videopac | `o2em` | `odyssey`, `videopac` |
| PlayStation 1 | `psx` | `ps1`, `ps` |
| PSP | `psp` | `ppsspp` |

This collection is currently a mix of muOS-style short names (`gw`, `wswan`, `sfc`, `tg16`), Onion-style (`pico-8`, `sg-1000`), and ES-DE-style long names — that's why so many alias pairs exist.

### 6.3 What's in the RGLauncher catalog

The launcher's catalog (consoles you can display *if* you point a system tile at a folder) covers:

**Recognized:** NES, SNES, N64, GameCube, Wii, 3DS, Game Boy, GBC, GBA, DS, Virtual Boy, Pokémon Mini, Game & Watch, Mega Drive, Master System, Game Gear, SG-1000, 32X, Sega CD, Saturn, Dreamcast, NAOMI, Atomiswave, PC Engine, PCE-CD, SuperGrafx, PC-FX, PS1, PS2, PSP, PSP Minis, WonderSwan, WonderSwan Color, Neo Geo Pocket, Neo Geo Pocket Color, Neo Geo, Neo Geo CD, MAME, FBNeo, CPS1, CPS2, CPS3, Atari 2600/5200/7800, Lynx, Jaguar, ColecoVision, Intellivision, Vectrex, Channel F, Odyssey 2 / Videopac, MSX (umbrella), DOS, ScummVM, OpenBOR, EasyRPG, Cave Story, FDS, Sufami Turbo, Satellaview.

**Not in catalog (RGLauncher will never display them; need ES-DE / Daijisho or direct app launch):**

- **All home computers:** Amiga (incl. CD32/CDTV), Amstrad CPC, Atari 800, Atari ST, C64/C128/VIC-20/PET/Plus-4, MSX1/MSX2/MSX2+/Turbo R *as separate entries* (umbrella `msx` is in catalog), PC-88, PC-98, X1, X68000, Thomson, ZX Spectrum, ZX81, Sharp X1
- **Engines / game ports:** Cannonball, DevilutionX, CGenius, Cave Story (technically catalogued but as a system), FreeJ2ME, Lutro, Moonlight, MPlayer, Mrboom, PrBoom, Pygame, SDLPoP, Solarus, TyrQuake, Uzebox, xash3d_fwgs, generic `ports`, `ports_scripts`
- **Misc:** 3DO, Daphne, N64DD, Watara Supervision, Atari Jaguar (catalog has it, but check firmware version), Amstrad GX4000

### 6.4 The `h`-suffix hack folders

**RGLauncher does not recognize `gbh`, `gbah`, `gbch`, `nesh`, `snesh`, `gamegearh`, `mdh`, `genh` as separate libraries.** This naming scheme is a JELOS / muOS / ES-DE-Android convention that was [explicitly rejected](https://community.muos.dev/t/additional-folder-names-for-rom-hacks/706) for inclusion in the muOS default `name.json`, and Anbernic has never adopted it.

Three options for hacks on this device:

1. **Merge** each hack folder into its parent system folder (`nesh/* → nes/`, etc.) and accept that hacks and originals appear in the same launcher list.
2. **Keep separate** and use ES-DE Android (with [GlazedBelmont's custom-systems XMLs](https://github.com/GlazedBelmont/es-de-android-custom-systems)) or Daijisho — RGLauncher just won't see them.
3. **Move outside the ROMs root** to a `_hacks/` or similar staging area, accessible only via direct emulator app.

### 6.5 Per-folder action plan for this collection

Status of every populated folder, classified for an Anbernic-Android-stock-mirror layout:

#### Keep as-is (already canonical)

`gb`, `gbc`, `gba`, `nes`, `snes`, `n64`, `gc`, `nds`, `3ds`, `dreamcast`, `psx`, `psp`, `ps2`, `gba`, `mame`, `fbneo`, `cps1`, `cps2`, `cps3`, `arcade`, `naomi`, `atomiswave`, `neogeo`, `pcengine`, `pcenginecd`, `mastersystem`, `gamegear`, `sega32x`, `segacd`, `atari2600`, `atari5200`, `atari7800`, `fds`, `atarilynx` (or rename to `lynx` — see below), `pokemini`, `vectrex`, `virtualboy`, `wii&ngc`, `ngp`, `ngpc`, `cavestory`, `openbor`, `pspminis` (catalog uses `pspminis`).

#### Merge: rename canonical folder if needed, move source content in, then delete source

| Source (delete after merge) | Target (canonical) | Rationale |
|---|---|---|
| `genesis` (1,064 files) | `megadrive` (1,774 files) | ES-DE/Anbernic blessed name |
| `tg16` (80) | `pcengine` (572) | already chose long form for CD; keep parity |
| `tg16cd` (25) | `pcenginecd` (already exists, 2 files) | same |
| `gw` (was 53 in raw count) | `gameandwatch` (53 in dedup count) | only one is populated; verify and keep that one, rename if needed |
| `wswan` (skeleton) | (none yet — create `wonderswan` if/when populated) | resolve before importing |
| `wswanc` (62) | rename folder → `wonderswancolor` | align with naming convention |
| `sfc` (skeleton) | `snes` | drop empty placeholder |
| `famicom` (skeleton) | `nes` | drop empty placeholder |
| `sg-1000` (81) | rename → `sg1000` | drop hyphen |
| `pico-8` (skeleton) | `pico8` (skeleton — both empty) | resolve before populating |
| `tic-80` (skeleton) | `tic80` (skeleton — both empty) | same |
| `coleco` (1) | `colecovision` (skeleton) | rename `colecovision` to canonical, fold the 1 file in |
| `ss` (6) | `saturn` (60) | drop short alias |
| `sgfx` (5) | `supergrafx` (skeleton — currently 1 file) | drop short alias |
| `odyssey` (116), `videopac` (skeleton) | `o2em` (skeleton — pick one) | three names for the same console; pick `o2em` per RetroArch core name, or `videopac` per ES-DE |
| `snes-msu1` / `snesmsu1` | merge into `snes` (or keep separate as MSU-1 enhanced — your choice) | both currently skeleton |
| `sufami` (skeleton) | merge into `snes` (or keep) | catalog has Sufami Turbo as separate entry — keep if you have content |
| `satellaview` (skeleton) | merge into `snes` (or keep) | same as Sufami |
| `megadrive-japan` (skeleton) | `megadrive` | regional sub-collection, not a separate system |
| `msx1` (skeleton) | `msx` (1,822 files) | RGLauncher umbrella `msx` |
| `msxturbor` (skeleton) | `msx` | same |
| `n64dd` (skeleton) | keep separate or merge into `n64` | not in RGLauncher catalog as separate; if populated, treat as a sub-library |

#### Hack folders — decide as a group

`nesh` (1,979 files), `gamegearh`, `gbh`, `gbch`, `gbah`, `snesh` (rest are skeletons). Decision required (see §6.4): merge into parents, keep for ES-DE, or stage outside ROMs.

#### Not in RGLauncher catalog — keep but won't appear in stock launcher

These have content but won't show up in RGLauncher. Options: install ES-DE Android or Daijisho on the device; or move them to a non-ROMs area on the SD/USB:

- **Computers:** `c64` (5,230), `zxspectrum` (5,252), `atari800` (5,097), `amiga1200` (4,288), `atarist` (2,847), `amstradcpc` (1,955), `amiga` (1,113), `pc88` (skeleton), `pc98` (skeleton), `x1` (skeleton), `x68000` (skeleton), `thomson` (skeleton), `zx81` (skeleton), `c128` (skeleton), `c16` (skeleton), `c20` (skeleton), `cplus4` (skeleton), `pet` (skeleton), `vic20` (skeleton), `amstradgx4000` (skeleton), `gx4000` (skeleton), `amiga500` (skeleton), `amigacd32` (skeleton), `amigacdtv` (skeleton)
- **Engines / ports:** `ports` (781), `cannonball`, `devilutionx`, `cgenius`, `freej2me`, `lutro`, `moonlight`, `mplayer`, `mrboom`, `prboom`, `pygame`, `sdlpop`, `solarus`, `tyrquake`, `uzebox`, `xash3d_fwgs`, `ports_scripts`
- **Other:** `3do` (skeleton), `daphne` (skeleton), `channelf` (skeleton), `intellivision` (skeleton — catalog *does* recognize Intellivision, so this only fails to appear because it's empty), `supervision` (skeleton), `atarijaguar` (skeleton), `pcfx` (skeleton)

Recommended: install **ES-DE Android** (and optionally Daijisho) on the device — these handle every system above. Keep these folders in place; they cost nothing on the SD and ES-DE will pick them up.

#### Probably-junk folders to investigate

| Folder | Files | Hypothesis |
|---|---|---|
| `1` | 0 | Stray folder from a copy mistake — verify and delete |
| `25game` | 0 | Skeleton from an Anbernic image |
| `anbernic` | 6 (4 in clean dedup) | Vendor/utility folder — inspect contents |
| `varcade` | 184 (was 1,292 before filtering side-files) | Community arcade subset — verify what it is |
| `hbmame` | 1 | Homebrew MAME — check the one file |
| `capcom` | skeleton | Empty — drop |
| `genh` (skeleton), `pspminis` (skeleton — catalog name), `gbah/gbh/gbch/snesh/gamegearh` (skeletons) | — | Empty hack placeholders |

#### Excluded entirely from launcher area

- `bezels`, `bios`, `downloads`, `savestates`, `screenshots`, `splash`, `lightgun`, `_dedup_reports`, `ports_scripts`: utility folders, not ROM systems. Either keep at top level (RGLauncher ignores them) or move under a dotfile-prefixed dir (`_meta/`) to keep the ROMs root tidy.
- Android system folders (`Alarms`, `DCIM`, `Audiobooks`, `Documents`, `Music`, etc.): these exist because the share is the device's user storage root. **Do not touch.**

### 6.6 Decisions you'll need to make

1. **Naming convention:** confirm we're standardizing on the long-form ES-DE / retrogamecorner names recommended in §6.2.
2. **Hack folders:** merge into parents, keep for ES-DE, or stage outside ROMs.
3. **Non-catalog systems:** install ES-DE Android / Daijisho to make them visible, or accept they stay PC-side only.
4. **Layout root:** put the ES-DE / RGLauncher folders directly at the share root (matching `/storage/emulated/0/` on the device for drag-and-drop), or nest under `Roms/`. RGLauncher's box-art scraping has a documented bug with nested paths in some firmware — flat is safer.

---

## 7. Local Folder Inventory (this collection)

As of the most recent scan, the active console folders here are:

```
c64           14,121   amiga         2,066    cps2          58
zxspectrum    13,321   genesis       1,942    naomi         53
amiga1200      9,781   msx           1,822    pcenginecd    28
arcade         9,453   gb            1,615    segacd        26
atari800       9,198   gbc           1,509    tg16cd        25
mame           6,384   atari2600     1,364    ngp           21
atarist        6,079   varcade       1,292    ppsspp        19
nes            5,150   mastersystem  1,261    openbor       15
nesh           4,814   fbneo         1,237    sgfx          15
megadrive      4,011   psp           1,113    anbernic      15
amstradcpc     3,856   pcengine        889    ss            13
snes           3,555   ports           887    cps3          10
gba            3,433   msx2+           667    wii&ngc       12
psx            2,970   neogeo          564    cannonball     5
                       gamegear        556    ...
```

A large number of folders (`sfc`, `famicom`, `gbh`, `snesh`, `wonderswan`, `pico-8`, `tic-80`, `pspminis`, etc.) are skeleton placeholders from a Batocera/Anbernic image — present but empty. These are documented above for completeness; they will populate as the collection grows.

Notable populated cross-folder pairs (likely deduplicate candidates):

- `nes` (5,150) ↔ `nesh` (4,814) — official vs hack libraries
- `megadrive` (4,011) ↔ `genesis` (1,942) — region-named aliases for the same console
- `amiga1200` (9,781) ↔ `amiga` (2,066) — full collection vs subset
- `arcade` (9,453) ↔ `mame` (6,384) ↔ `fbneo` (1,237) ↔ `cps1`/`cps2`/`cps3` — overlapping arcade umbrellas
- `pcengine` (889) ↔ `tg16` (80) — region aliases (PC Engine = TurboGrafx-16)
- `pcenginecd` (28) ↔ `tg16cd` (25) — same for CD
- `wswanc` (62) — populated; `wonderswancolor` and `wswan` are skeletons

A three-layer dedup pipeline runs at the project level (see `_dedup_reports/`):

- **Layer 1 (filename fuzzy):** `duplicates_within_folder.csv`, `duplicates_cross_folder_aliases.csv`, `duplicates_cross_folder_other.csv`, `summary.txt`
- **Layer 2 (filename + internal-header titles):** `rom_index_v2.csv`, `dups_within_v2.csv`, `dups_cross_alias_v2.csv` — adds SNES/N64/MD/GB/GBA/DS internal title extraction
- **Layer 3 (hash + No-Intro DAT match):** `rom_hashes.csv`, `rom_canonical_match.csv`, `rom_unmatched.csv`, `rom_canonical_dups.csv` — authoritative SHA-1 identification

These reports get progressively more accurate. Layer 1 is heuristic; Layer 3 with DAT match is byte-level authoritative. The full methodology is documented in `ROM-DEDUP-METHODOLOGY.md`, and the actionable plan derived from these reports lives in `ROM-LIBRARY-ANALYSIS-REPORT.md`.

### Headline numbers from the most recent run

- 54,672 ROM-like files (after side-file filter)
- 8,188 byte-identical redundant files (15.0%)
- 11,741 matched against No-Intro DATs (21% — limited by which DAT families were loaded)
- 1,040 cross-folder canonical-dup groups confirmed via DAT
- Top dup pair: `genesis` + `megadrive` with 439 confirmed canonical dups (validates the alias merge)
- Per-format normalization fired correctly: 284 SMC headers stripped, 141 N64 v64→z64 byte-swaps, 19,515 zips extracted (+ 6,673 multi-file MAME-style zips)

---

## 7.5 NES iNES header gotcha (empirical)

Worth flagging because it caught us during the real run:

Modern (post-2018) No-Intro NES DAT entries reference **unheadered** ROM content. The vast majority of NES dumps in the wild ship with the 16-byte iNES header (magic `4E 45 53 1A` = `NES\x1a`). If you hash NES files without stripping the iNES header first, your match rate against No-Intro will be catastrophic — we observed **1% matched on a 4,229-file `nes/` folder** before the fix, despite a 2,815-entry DAT being loaded.

The fix is the same shape as the SMC header strip for SNES: detect magic at offset 0, strip 16 bytes, then hash. Apply the same logic to `.nes` files inside zip archives. After implementing this, expected match rate is ~80%+ for typical No-Intro-compliant collections.

This pattern (modern DAT references unheadered content, real-world files are headered) is specific to NES — the SNES SMC header situation is the reverse (SMC was the headered copier format; modern DAT and `.sfc` are unheadered, both align).

---

## 8. Sources

- Anbernic RGLauncher setup guide (Joey's Retro Handhelds) — https://www.joeysretrohandhelds.com/guides/anbernics-rglauncher-setup-guide/
- Anbernic RG556 setup guide (Retro Game Corps) — https://retrogamecorps.com/2024/02/24/anbernic-rg556-setup-guide/
- Anbernic RG406V/H setup guide (RetroHandhelds.gg) — https://retrohandhelds.gg/anbernic-rg406v-and-rg406h-setup-guide/
- retrogamecorner/AnbernicAndroidFolders — Anbernic-blessed folder template — https://github.com/retrogamecorner/AnbernicAndroidFolders
- GlazedBelmont/es-de-android-custom-systems — ES-DE Android custom systems incl. h-suffix hacks — https://github.com/GlazedBelmont/es-de-android-custom-systems
- muOS rejected proposal for h-suffix folders (origin of the convention) — https://community.muos.dev/t/additional-folder-names-for-rom-hacks/706
- Anbernic Linux stock OS folder list (RG35XX H, contrast to Android) — https://whirlchain.com/anbernic-rg35xx-h-supported-console-roms/
- No-Intro Naming Convention wiki — https://wiki.no-intro.org/
- Official No-Intro Convention 2007-10-30 PDF — https://datomatic.no-intro.org/stuff/
- TOSEC Naming Convention (2015-03-23) — https://www.tosecdev.org/tosec-naming-convention
- GoodTools — Emulation General Wiki, https://emulation.gametechwiki.com/
- Redump Wiki — http://wiki.redump.org/
- Recalbox tags used in ROM names — https://wiki.recalbox.com/en/tutorials/games/generalities/tags-used-in-rom-names
- Recalbox multi-disc M3U guide — https://wiki.recalbox.com/en/tutorials/games/generalities/multidisc-management-with-m3u
- SNESdev wiki — ROM file formats — https://snes.nesdev.org/wiki/ROM_file_formats
- Mednafen Virtual Boy docs — https://mednafen.github.io/documentation/vb.html
- Switch / Wii U / Vita / PS3 file format references — Retro Reversing wiki
- Cadence — Wii disc image formats explainer
- Ultimate ROM File Compression Guide — Retro Game Corps
- Onion OS folder reference — https://onionui.github.io/docs/emulators/folders
- Batocera systems wiki — https://wiki.batocera.org/systems
- muOS — folder names for ROM hacks community thread
- RetroArch playlists & thumbnails docs
- Igir, Retool, RomVault, clrmamepro project docs
