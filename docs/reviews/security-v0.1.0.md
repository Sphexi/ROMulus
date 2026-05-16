# Security Audit — ROMulus v0.1.0 RC

**Auditor:** comprehensive-review:security-auditor agent
**HEAD audited:** 8b903ff
**Date:** 2026-05-14
**Threat model:** local-first desktop app with untrusted filesystem inputs, untrusted DAT/profile XML/YAML inputs, and four authenticated/unauthenticated HTTP integrations.

## Executive summary

Overall security posture is good for a local-first single-user desktop app at v0.1.0 RC. Sessions 4 and 8 caught and remediated the obvious issues — SQL is parameterised, atomic writes prevent half-written files, the DB is chmod 0o600 on POSIX, hash inputs to Hasheous are validated, scanner does not follow symlinks (`os.walk` defaults), and stdlib `xml.etree.ElementTree` blocks external entities by default. The four metadata clients use httpx with default TLS verification ON and default redirect-following OFF, so credentials and cover downloads ride on validated TLS.

The biggest residual risks are concentrated in **two boundaries that accept untrusted external input**: (1) user-supplied destination profiles in `~/.romulus/profiles/`, which feed `profile.base_path` and `mapping.folder` straight into a `Path` join that can escape the target directory if the YAML contains absolute paths or `..` segments; and (2) ZIP archive handling in `core/hasher.py`, which reads the largest inner entry fully into memory with no decompression-bomb guard. Both are exploitable but require either a malicious profile to be dropped into a user-writable folder (low trust threshold for a desktop app) or a maliciously crafted ROM zip placed in the scan tree. A handful of medium/low items round out the report, mostly around defense-in-depth: stdlib XML parsers should use `defusedxml` for billion-laughs protection, the SQL queries for `get_alias_folder_pairs` mishandle Windows backslashes, and `pyproject.toml` does not pin minimum dependency versions.

**Nothing in this audit blocks v0.1.0** for the intended audience (single-user desktop, scans their own library). Findings #1 (profile path traversal) and #2 (zip bomb) should ideally be addressed before public release, but the risk of "user installs ROMulus, then drops attacker-supplied profile/ROM into their own home directory" is small. The credential-storage gap (plaintext in SQLite) is already documented as deferred to v0.2.0; this audit confirms the 0o600 mitigation is correct and does not see additional credential-leakage paths.

## Findings

### High

**#1 — High — Destination profile path traversal via absolute / `..` paths in YAML**
*CWE-22 (Path Traversal). File:* `src/romulus/core/exporter.py:262-264, 279, 336-337`; `src/romulus/models/profile.py:40-62`.

`DestinationProfile.base_path` and `SystemMapping.folder` are Pydantic strings with NO format validation. `preview_export` and `export_collection` build the destination via:
```python
target / profile.base_path / mapping.folder / str(row["filename"])
```
Python's `Path.__truediv__` replaces the base when the right-hand operand is an absolute path. A user-supplied profile in `~/.romulus/profiles/` with `base_path: "/etc"` or `mapping.folder: "/Users/victim/.ssh"` will cause `dest` to land entirely outside `target` — the chosen SD-card folder. `mapping.folder: "../../.."` is equally effective.

*Exploitation:* attacker convinces user to install a "community profile" YAML for an obscure handheld. User points export at `/Volumes/SD`. Profile contains `base_path: "/Users/victim/Documents"`. Export writes hundreds of ROM-sized files into `~/Documents`, possibly clobbering existing files (note: `atomic_copy` does an unconditional `os.replace`, so any existing file at the computed dest with the same name and a different size gets overwritten — the dest-already-exists short-circuit at exporter.py:339 only triggers when sizes match).

*Fix (surgical):* add a pydantic `@field_validator` on `DestinationProfile.base_path` and `SystemMapping.folder` that rejects absolute paths and any path component equal to `".."` or containing `/` `\\` or `:`. Alternatively, in `_system_dest_dir`, resolve the computed `dest_dir` and assert it is a child of `target.resolve()` before any write:
```python
resolved = (target / profile.base_path / mapping.folder).resolve()
target_resolved = target.resolve()
if target_resolved not in resolved.parents and resolved != target_resolved:
    raise ValueError(f"profile would write outside target: {resolved}")
```

---

**#2 — High — Zip decompression bomb in hasher**
*CWE-409 (Improper Handling of Highly Compressed Data). File:* `src/romulus/core/hasher.py:107-125`.

`_read_zip_payload` opens a zip, picks the largest entry by `info.file_size`, then calls `inner.read()` — reading the entire decompressed payload into memory in one go. There is no cap on the decompressed size and no check that the on-disk archive size is plausible vs. the claimed uncompressed size. Heavy Scan runs this on every `.zip` in the library across a `ThreadPoolExecutor` with 8 workers by default (`scan_threads` config).

*Exploitation:* attacker drops a 42 KB "42.zip"-style bomb into a system folder. User triggers Heavy Scan. Eight worker threads each try to read a multi-GB payload into RAM; on a 16 GB workstation this OOM-kills the app or causes severe swap thrashing. On Linux, the process is more likely to be OOM-killed than crash gracefully.

Also note that `info.file_size` is *attacker-controlled* (it is metadata, not verified) — a malicious zip can lie about its uncompressed size and pass any pre-read check. The only safe approach is bounded streaming.

*Fix (surgical):* (a) stream-hash the inner file via `zf.open(target)` and a chunked loop instead of `inner.read()`, mirroring `_digest_stream`; (b) wrap the inner-file read in a counter that aborts when bytes-read exceeds a hard cap (e.g. 2 GB, since legitimate ROM zips are well under this); (c) optionally cap the uncompressed/compressed ratio (e.g. >100x suspicious). The streaming change alone bounds memory but not total time — combine all three.

---

### Medium

**#3 — Medium — `xml.etree.ElementTree` used without billion-laughs protection**
*CWE-776 (XML Entity Expansion). Files:* `src/romulus/core/dat_parser.py:13,146,150`; `src/romulus/core/exporter.py:38,410-456`; `src/romulus/metadata/launchbox.py:12,90`.

Python's stdlib `xml.etree.ElementTree` resolves external entities to nothing (XXE-safe by design since 3.7), but it DOES expand internal entity references — meaning a billion-laughs / quadratic-blowup DoS attack is still possible against `parse_dat_file`, `parse_launchbox_xml`, and any XML the exporter reads back (currently the exporter only writes XML, so the exporter call site is not a parse vector — flagging here only for the import statement consistency). A crafted DAT under `~/.romulus/dats/` could exhaust memory during `load_all_dats`.

*Exploitation:* attacker convinces user to add a community DAT directory containing a crafted XML that defines nested entities expanding exponentially. `ET.parse` blocks on the entity expansion and either consumes gigabytes of RAM or hangs.

*Fix (surgical):* add `defusedxml` as a dependency and replace the imports:
```python
# in dat_parser.py and launchbox.py:
import defusedxml.ElementTree as ET
```
`defusedxml` is a drop-in replacement that disables entity expansion and external-DTD resolution. The exporter's write-only `xml.etree.ElementTree` usage in `generate_gamelist_xml` does NOT need to change (it serializes, never parses).

---

**#4 — Medium — Filename overwrites via `atomic_copy` when sizes differ**
*CWE-73 (External Control of File Name). File:* `src/romulus/core/exporter.py:337-347`; `src/romulus/core/atomic.py:79-100`.

`atomic_copy` calls `os.replace(tmp_path, dest)` which atomically replaces ANY pre-existing file at `dest`. The exporter only short-circuits when `dest.exists() AND dest.stat().st_size == size_bytes`. Any pre-existing file with the same name but a different size at the target is silently overwritten without confirmation.

In normal use this is fine — the target is a clean SD card. But combined with finding #1 (profile path traversal) or with a target directory that happens to contain unrelated user files, this clobbers data. CLAUDE.md rule #4 says "Never modify files without preview" — the preview shows what *will* be copied, but does not call out that overwrites will happen.

*Exploitation:* user picks `~/Documents` as the export target by mistake. Any existing file in `~/Documents/roms/<system>/Foo.sfc` is overwritten if a ROM in the library has a colliding filename.

*Fix (surgical):* in `export_collection`, before `atomic.atomic_copy`, check `dest.exists()` and either skip-with-warning or refuse-with-error. Pre-existing files should never be silently replaced.

---

**#5 — Medium — `_sanitize_canonical_filename` doesn't block Windows reserved names or control chars**
*CWE-1219 (File Handling Issues). File:* `src/romulus/core/organizer.py:147-150`.

Filters out `<>:"/\|?*` but leaves through `CON`, `PRN`, `AUX`, `NUL`, `COM1..9`, `LPT1..9` (treated as device names on Windows), leading/trailing dots and spaces (silently mangled by Windows), and ASCII control chars `\x01..\x1f`. The input is a DAT-supplied canonical name, so a malicious DAT could ship `name="CON"` and the organizer would propose renaming a real ROM to a path Windows refuses to open.

*Exploitation:* malicious DAT contains a `<game name="PRN">` entry mapped via SHA-1 to a real ROM in the user's library. Organizer proposes renaming `Final Fantasy VI.sfc` to `PRN.sfc`. User clicks Apply. On Windows, the file becomes inaccessible (operations on `PRN` go to the printer device).

*Fix (surgical):* after the existing replace loop, also strip control chars (`c < " "` → `_`), strip leading/trailing dots and spaces, and bail out / underscore-prefix when the stem (without extension) matches the case-insensitive reserved-name set. Only applies on Windows but cheap to apply unconditionally.

---

**#6 — Medium — `screenscraper` test_connection has no rate-limit guard**
*CWE-307 (Improper Restriction of Excessive Authentication Attempts). File:* `src/romulus/metadata/screenscraper.py:74-130`; `src/romulus/ui/settings_dialog.py:134-156`.

`test_connection` is called directly from the Settings dialog button without going through `_respect_rate_limit()`. The button is disabled during the in-flight request, but there's no cooldown after — a user (or an accidentally-bound shortcut) could click Test rapidly and hit ScreenScraper's auth endpoint faster than the documented 1 req/sec limit. Additionally, `test_connection` runs *synchronously on the Qt UI thread*: the dialog will freeze if ScreenScraper is slow or unreachable, until the 15-second `DEFAULT_TIMEOUT` elapses.

*Exploitation:* low severity — a user accidentally rate-limits their own ScreenScraper account and can't enrich for a while. Not an attacker-driven vector.

*Fix (surgical):* (a) call `_respect_rate_limit()` at the top of `test_connection` (same as `lookup_game`); (b) move the call onto a `QThread` worker mirroring the pattern in `workers.py` so the UI doesn't freeze. Both are small changes — (a) is one line.

---

### Low / Informational

**#7 — Low — `get_alias_folder_pairs` SQL strips backslashes after counting**
*File:* `src/romulus/db/queries.py:655-684`.

The query computes `LENGTH(REPLACE(path, '\', '/'))` for the path-without-filename slice, but the outer `SUBSTR` is taken from `path` *as stored* — i.e. before the backslash→slash normalization that the LENGTH branch performed. On Windows, where `path` contains backslashes, the LENGTH math is correct but the SUBSTR returns backslash-laden strings; the Python-side comparison in organizer.py then folds those via `_normalize_folder` (which does `path.replace("\\", "/")`). The two normalizations are doing the same work in two places, but they happen consistently — there is no security bug, only a subtle correctness risk if the path contains UTF-8 multibyte sequences (SQLite's `LENGTH()` counts characters, not bytes, but `INSTR` and `SUBSTR` are char-based too, so this stays consistent). Flagging as a code-smell rather than an exploit — but the redundancy means a future refactor could introduce a TOCTOU between the two normalizations.

*Fix (optional):* compute the path-without-filename slice entirely in Python (in the organizer), or do the `REPLACE` once at the start of the query into a CTE.

---

**#8 — Low — Cover downloads write any content-type to disk under `.png` name**
*CWE-434 (Unrestricted File Upload — adapted to download). File:* `src/romulus/metadata/libretro.py:90-117`.

`fetch_cover` checks `response.status_code == 200` and writes `response.content` to `{cache_dir}/{system_id}/{cover_type}/{game}.png` without verifying that the body is actually a PNG. If a future libretro-thumbnails compromise served HTML or a malicious file, it would be cached with a .png extension. ROMulus itself only loads these via Qt's image loader (which would refuse to parse non-image data), so the immediate impact is bounded — but the file IS exposed by `copy_artwork` to the export target as `{stem}-image{suffix}` where `suffix` comes from `local_path.suffix` (always `.png` here). EmulationStation on the target device might do something different with mismatched content.

*Exploitation:* low — requires libretro-thumbnails CDN compromise or DNS hijack despite TLS pinning (TLS verify is on; no pin).

*Fix (optional, defense-in-depth):* check the response body starts with the PNG magic bytes `\x89PNG\r\n\x1a\n` (or the JPEG `\xff\xd8\xff`) before writing. Two-line check.

---

**#9 — Low — `pyproject.toml` does not pin minimum dependency versions**
*File:* `pyproject.toml:6-12`.

Dependencies list `PySide6`, `httpx`, `pydantic`, `structlog`, `pyyaml` with no version constraints. A freshly resolved environment could pull an ancient pydantic v1 (the code uses v2 idioms — `Field`, `BaseModel.model_validate`), or a httpx version with a known CVE. Currently no pinned dependency has open CVEs as of 2026-05, but the project will silently break on pydantic v1.

*Fix (surgical):* add minimum bounds, e.g.:
```toml
dependencies = [
    "PySide6>=6.6",
    "httpx>=0.27",
    "pydantic>=2.5",
    "structlog>=24.1",
    "pyyaml>=6.0",
]
```

---

**#10 — Low — CI workflow does not pin actions by SHA**
*File:* `.github/workflows/ci.yml:23,27`.

`actions/checkout@v4` and `actions/setup-python@v5` are pinned to major versions, not commit SHAs. Major-version tags are mutable — a compromised actions org could re-tag `v4` to a malicious commit. The supply-chain risk is small for a personal project, but GitHub's own advice is to pin to immutable SHAs for production workflows.

`GITHUB_TOKEN` permissions are not declared — defaults to the repo-level setting, which is `read-and-write` on older repos. The job only needs `contents: read`. Tighten with:
```yaml
permissions:
  contents: read
```
at the workflow level.

*Fix (optional):* SHA-pin actions and declare minimal `permissions:`.

---

**#11 — Informational — `delete_duplicate` does not verify hash before unlink**
*File:* `src/romulus/core/organizer.py:444-452`.

`_execute_delete_duplicate` removes `source` without re-checking that it is actually byte-identical to the keeper at the moment of execution. The plan is built against the hashes table; if the user manually edits a ROM between plan generation and apply, the file gets deleted anyway. Not a security issue per se — the preview UI exists exactly to handle this — but worth a TOCTOU note: there is a window between `analyze_library` and `execute_plan` where the keeper or duplicate could change.

*Fix (optional):* re-hash both files just before `os.remove` and abort the action if SHA-1 no longer matches. Adds I/O but the action set is small.

---

**#12 — Informational — Workers use `except Exception` catch-all for failure path**
*File:* `src/romulus/ui/workers.py:49-50, 64-67, 109-111, 130-133, 180-181, 195-198, 250-253, 273-277`.

Every worker's `run()` has `except Exception as exc:` blocks that capture the exception message and emit it to `self.failed`. The message is shown verbatim in `QMessageBox`. If a future code path raises an exception containing a credential, path, or PII, it ends up on the user's screen. Currently the metadata clients don't log credentials and the DB layer doesn't raise credential-bearing exceptions, so this is informational — but worth a guard in any future refactor.

*Fix (optional):* sanitize exception text before emitting to UI, or only emit `type(exc).__name__` and log the full traceback via structlog.

---

**#13 — Informational — Scanner skips per-file `OSError` silently**
*File:* `src/romulus/core/scanner.py:602-610`.

`stat()` failures are counted under `errors` but not logged. A partial-permissions library would show "N errors" in the UI with no way to know which files were unreadable. Useful for forensics, not a security finding.

*Fix (optional):* `logger.debug("scan stat failed: path=%s err=%s", file_path, exc)` before `continue`.

---

## Mitigations already in place (verified)

* **`_restrict_db_permissions` (`src/romulus/db/connection.py:27-44`)** — correctly uses `os.chmod(0o600)` on the DB and the `-wal`/`-shm` siblings, skips Windows (good — `os.chmod` on Windows would set read-only, breaking sqlite), suppresses OSError so a read-only filesystem doesn't break startup. The mitigation runs on every `get_connection` so a re-opened DB always converges back to 0o600 — covers the case where a user manually chmods the file. ✅
  * *Subtle bypass to note (not blocking):* the chmod happens *after* `sqlite3.connect`, which creates the DB file with the umask-permitted bits (typically 0o644). There is a brief window after first creation where the DB is world-readable. For a fresh-user / first-run scenario the DB has no credentials yet, so this is fine — but if a credential is being written for the first time and a malicious process is racing the create, it could win. Move the chmod call between the `mkdir` and `sqlite3.connect` (open the file empty + chmod + close + then sqlite3.connect) if paranoid. Probably not worth the complexity at v0.1.0.

* **`atomic_replace` / `atomic_copy` / `atomic_write_bytes` (`src/romulus/core/atomic.py`)** — correctly use `tempfile.mkstemp` (which creates with mode 0o600), `os.fdopen(fd, "wb")` so the fd is consumed by the context manager, and `os.replace` for the atomic move. On cross-device the fallback streams to a sibling tempfile then `os.replace`. Cleanup on exception is wrapped in `contextlib.suppress(OSError)`. The pattern is correct. ✅
  * *One subtle behavior:* `atomic_replace` on the cross-device fallback `unlink`s the source AFTER the dest write succeeds. If the unlink fails (read-only source, etc.) the file is left in both places. This is logged as a warning but not raised — meaning a partial move can succeed silently. Not a security issue.

* **`_is_valid_hash` (`src/romulus/metadata/hasheous.py:34-37`)** — correctly checks the hash matches `^[0-9a-f]+$` and is exactly 8/32/40 chars. URL injection is impossible. ✅

* **SQL parameterisation** — spot-checked `db/queries.py`, `core/scanner.py` (group_into_games), `core/organizer.py` (find_cross_extension_dupes, _execute_merge_folder), `core/exporter.py` (_build_rom_query — explicit comment about not interpolating), `metadata/__init__.py` (_get_sha1_for_game). Every SQL string uses `?` placeholders. The single dynamic-SQL helper (`update_scan_history` at queries.py:209-233) builds the SET clause from a hard-coded `allowed = {finished_at, files_found, ...}` whitelist before interpolation — safe. The exporter's dynamic IN-clause builds `?` placeholders from list length, parameters bound separately — safe. ✅

* **`os.walk(library_root)` without `followlinks=True`** — explicit comment at scanner.py:581-583, defaults to `followlinks=False`. A symlinked subdirectory under the library root will NOT be traversed, so a malicious symlink can't lead the scanner outside the library. ✅
  * However: `_resolve_system_for_directory` calls `Path.resolve()` on both `current` and `library_root`. If `current` is itself a symlink-target outside the library, `resolve()` follows it during the comparison loop. This doesn't expand the scan (os.walk already refused to descend the symlink), but it means the system-folder-name check might match an unexpected name. Low impact — flagging for completeness, not as a finding.

## Recommended pre-v0.1.0 fixes

**Block release? No.** ROMulus is a single-user local app and the realistic threat model is "user accidentally points it at the wrong folder" rather than "remote attacker." With that said:

**Highly recommended for v0.1.0 (small, surgical, high value):**
1. **Finding #1** — add the `target_resolved in resolved.parents` check in `_system_dest_dir`. Six lines. Closes the profile-path-traversal vector entirely.
2. **Finding #2** — replace `inner.read()` in `_read_zip_payload` with a chunked streaming hash AND a 2 GB cap. Bounds memory + time during Heavy Scan. ~20 lines.
3. **Finding #3** — add `defusedxml` dependency, change two import lines. Closes billion-laughs against DAT and LaunchBox parsing.
4. **Finding #4** — refuse to overwrite an existing file with a different size in `export_collection`. Two lines.
5. **Finding #9** — pin minimum dependency versions. Prevents accidental pydantic v1 install.

**Can wait for v0.2.0:**
6. **Finding #5** — Windows reserved-name sanitization. Low real-world hit rate.
7. **Finding #6** — async test-connection and rate-limit guard. Quality-of-life.
8. **Finding #8** — PNG magic-byte check on cover writes. Defense-in-depth.
9. **Finding #10** — SHA-pin CI actions, declare `permissions: contents: read`.
10. **Findings #11, #12, #13** — informational only, no security urgency.

**Explicitly NOT in scope for v0.1.0 (already deferred):**
- Plaintext ScreenScraper credentials in SQLite. CHANGELOG documents this; 0o600 chmod is the correct mitigation for the v0.1.0 threat model.
