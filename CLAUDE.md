# CLAUDE.md — asfSearchAndDownload

Search the ASF DAAC for NISAR / Sentinel-1 products, dedupe against a local archive, export footprints to a GeoPackage for QGIS, then bulk-download with `aria2c`. Import name is `asfsearchdownload`. See the [packages CLAUDE.md](../CLAUDE.md) for pipeline context (NISAR HDF5 inputs feed `nisargrimpworkflow`).

## Modules / CLI entry points

| Script | Module | Role |
|---|---|---|
| `searchASF` | `searchASF.py:main()` | Search ASF DAAC / CMR; write download-URL lists + optional GeoPackage |
| `ariaDownload` | `ariaDownload.py:main()` | Download a URL list with `aria2c`, time-of-day throttled |
| `reduces1` | `reduceSentinel1.py:main()` | Strip files (e.g. cross-pol) from Sentinel-1 ZIP archives |
| `autoupdateS1` | `autoupdateS1.py:main()` | Nightly S1 archive-update driver: orbits → search → download → file → frame-check, config-driven |
| `refreshS1Orbits` | `refreshOrbits.py:main()` | Refresh the precise-orbit (EOF) archive from ASF `aux_poeorb` |
| `fileS1` | `fileS1.py:main()` | Unpack S1 SAFE zips into the `assemblyDir` `track-<n>/<orbit>/` tree |
| `checkFramesS1` | `checkFramesS1.py:main()` | Vet filed datatakes (burst "frames"), restructure, and queue → toProcess/pending/problem |
| `writeSearchGpkg` | `writeSearchGpkg.py` | Library only (no CLI) — used by `searchASF --gpkg` |

## Workflow

```
searchASF <firstDate> <lastDate> <output> [--sensor NISAR|SENTINEL1] ...
    → <output>                  URLs to download (new granules)
    → <output>.exists           URLs already in --archiveDir (skipped)
    → <output>.updated          URLs for newer versions of archived granules
    → <output>.<PRODUCT>         per-product-type URL lists (e.g. .RUNW, .ROFF, .SLC)
    → --gpkg search.gpkg         footprints, one OGR layer per product type (QGIS)

ariaDownload <output> [--xferDir DIR] [--overWrite] [--noRename]
    → downloads each URL with aria2c, checking xferDir for an existing
      partial (.zip.1) or full (.zip) copy first

reduces1 <file.zip> --pattern hv   (or --directory DIR --suffix zip)
    → strips matching files (e.g. HV/VH cross-pol) from S1 ZIPs in place
```

## searchASF

```
searchASF firstDate lastDate output
          [--sensor {NISAR,SENTINEL1}]            # default NISAR
          [--products PRODUCT [PRODUCT ...]]       # NISAR default: RUNW ROFF RSLC
                                                    # S1 default: SLC
          [--beamMode MODE ...]                    # S1 only, default IW
          [--bandwidth BW ...]                     # NISAR only, default 40 40+5 77
          [--minVersion N] [--specificVersion N ...]   # NISAR CRID filters
          [--startTrack N] [--endTrack N]
          [--startFrame N] [--endFrame N]
          [--searchArea FILE | --greenland | --antarctica]
          [--archiveDir GLOB] [--gpkg FILE] [--s3]
```

- **Dates**: `firstDate`/`lastDate` are `YYYY-MM-DD`; converted to `T00:00:00Z`/`T23:59:59Z` for the CMR temporal filter.
- **Products**: NISAR choices `L0B RSLC RIFG RUNW ROFF GSLC GCOV GUNW GOFF SME2`; Sentinel-1 choices `SLC GRD_HD GRD_MS GRD_HS GRD_FD GRD_MD OCN RAW BURST`.
- **Bandwidth** (`--bandwidth`, NISAR only): `5 5+5 20 20+5 40 40+5 77` MHz; only applied as a hard filter for pair products (`RIFG RUNW ROFF GUNW GOFF`, see `_PAIR_PRODUCTS`) — single-acquisition products (RSLC, GSLC, GCOV, L0B, SME2) are searched without it.
- **Search area** (`--searchArea`): accepts `.geojson`/`.json` (FeatureCollection/Feature/Geometry), `.shp` (reprojected to WGS84 via OGR), or a flat GrIMP `.lonlat` file (`lon,lat` pairs, order auto-detected). `--greenland` uses the bundled `asfsearchdownload/searchRegions/Greenland.lonlat` (also the default). `--antarctica` uses a hardcoded circumpolar WKT and is mutually exclusive with `--greenland`.
- **Track/frame filters**: `--startTrack/--endTrack/--startFrame/--endFrame` filter on `pathNumber`/`frameNumber` from the search result (or parsed from the NISAR_EA granule name).
- **Archive dedup** (`--archiveDir GLOB`): globs existing files (extensions `.h5`/`.zip`/`.zip.1` stripped for comparison). Sentinel-1 dedupes by filename stem; NISAR dedupes by `scene_key` (granule identity minus CRID/version) and tracks the highest CRID version seen — older-or-equal granules go to `<output>.exists`, newer versions go to `<output>.updated`.
- **`--gpkg FILE`**: writes one OGR layer per NISAR product type, with a `status` field (`found`/`exists`/`updated`) and metadata parsed by `parse_nisar_meta()` (track, frame, cycle, direction, polarization, bandwidth_mhz, dates, crid, version). EPSG chosen automatically from mean footprint latitude: `3031` (Antarctic, lat < -60), `3413` (Arctic, lat > 60), else `4326`.
- **`--s3`**: emit `s3://` URIs instead of HTTPS; granules with no S3 link are silently skipped.

### Authentication (Earthdata)

`searchASF` builds an `asf.ASFSession()` and tries, in order:
1. `earthaccess.login(strategy='netrc')` — full EDL OAuth2, returns a JWT. Required for the restricted **NISAR_EA** collections (`C4052500045-ASF`, `C4052499921-ASF` — science-team beta data), queried separately via authenticated CMR (`_search_nisar_ea`). Install with `pip install earthaccess`.
2. Fallback: `~/.netrc` entry for `urs.earthdata.nasa.gov` via `asf_search`'s `auth_with_creds()` — works for public collections but may not satisfy ACLs on restricted ones.
3. If neither is configured, searches unauthenticated (public data only).

NISAR_EA results are deduplicated against public-collection results by filename stem (the same granule can appear in both with different URLs).

### Granule-name parsing (`_nisar_parse` / `parse_nisar_meta`)

NISAR granule stems are split on `_`. Two layouts:
- **Pair products** (20 fields: RIFG/RUNW/ROFF/GUNW/GOFF) — bandwidth at field 9, polarization at 10, ref/sec dates at 11/13, CRID at 15, version at 19.
- **Single-acquisition** (18 or 19 fields: RSLC/GSLC/GCOV/L0B) — bandwidth at field 8 or 9 (`_nisar_bw_field` picks whichever is a 4-digit numeric token), CRID/version near the end.

CRID values look like `X05010` → parsed as integer `5010` for `--minVersion`/`--specificVersion` comparisons.

## ariaDownload

```
ariaDownload downloadLinks [--xferDir DIR] [--overWrite] [--noRename]
```

- `downloadLinks` — a file of URLs, one per line (typically `searchASF`'s `<output>`).
- For each URL, checks `xferDirs` for `<file>.zip.1` (moves/renames to `<file>` unless `--noRename`) or `<file>.zip` (copies unless `--noRename`) before downloading — avoids re-downloading partial transfers.
- `--xferDir *` (default) scans `/Volumes/insar{1,3,6,7,8,9,10,11}/ian/xfer` for existing copies.
- Throttling via `getX()`: weekday 07:00–18:00 → `-x 1`; weekend 07:00–18:00 → `-x 4`; all other times → `-x 10` (aria2c `-x` = max connections per server).
- Calls `aria2c -x <N> <url>` via `subprocess.call(..., shell=True, executable='/bin/csh')`.
- Existing files are skipped unless `--overWrite`.

## reduces1 (reduceSentinel1)

```
reduces1 [zipfile] [--pattern hv] [--directory DIR] [--suffix zip]
```

- Removes archive members whose name contains `--pattern` (default `hv`, i.e. cross-pol HV/VH) from a Sentinel-1 ZIP.
- Prefers the system `zip -d` CLI (in-place deletion); falls back to a slower stream-copy-to-new-ZIP if `zip` is not on PATH.
- `--directory DIR` processes all `*.{suffix}` files in a directory; otherwise operates on the single positional `zipfile`.
- Tested on both SLC and L0B-style products; roughly halves archive size for single-pol-only users.

## autoupdateS1 (S1 archive-update pipeline)

Nightly cron driver, configured by an `autoupdate.yaml` in the **project dir** (where the cron `cd`s; see `scripts/runAutoupdateS1.sh`). Full-detail docs: `Documents/autoupdateS1.md`, `Documents/fileS1.md`, `Documents/checkFramesS1.md`.

Stages in `runUpdate()` (each is skippable/isolatable):
1. **Orbits** — `refreshOrbits.updateStateVectors` (skip `--noOrbits`).
2. **Search** — `searchGranules` → `searchASF` (results to `archiveDir/searchResults/`).
3. **Download** — serial `downloadOne`/`ariaDownload`, soft `--maxDownloads` cap finishing the in-progress pass; parallel cross-pol `reduceSentinel1` in a thread. Files zips to `archiveDir/<YYYY>-<MM>/`; `.zip.1` marks already-processed. Granules exhausting `maxAttempts` are collected in `failedUrls` and given one **retry pass after the main loop** (outages usually clear over a long run); the `--maxDownloads` cap is not reapplied there and recoveries decrement `nFailed`. `--noDownload` stops after orbits.
   - **Concurrent filing** (default on; `--noFileDuringDownload` disables): `_reduceWorker` calls `pipeline.submit()` on the statement *after* `remove_files_from_zip` returns, so a zip is never unzipped while `zip -d` rewrites it in place. `_FilingPipeline` is a `queue.Queue` + `fileWorkers` (default 2) non-daemon threads calling `fileS1.fileOneZip`. `downloadStage` joins the reduce threads **then** drains the pipeline, in a `finally` — join order is the correctness condition, and skipping it would hang exit on the non-daemon workers.
4. **File** — `fileStage` → `fileS1` (only if `assemblyDir` in config).
5. **Frame check** — `frameCheckStage` → `checkFramesS1` (only if `assemblyDir` in config).
6. **Assemble** — `assembleStage` → `setupTrack --queue` **as a subprocess** (only if `assemble: true`). Subprocess, not import: `s1setup` already depends on this package for `queueS1`, so importing setupTrack would make it circular. Passes `--lockHeld` because runUpdate is already holding the assembly lock. This stage is the disk-control mechanism — a filed-but-unassembled unit holds ~30 GB of measurement TIFFs (~1.1 TB/night at ~37 units), and assembling strips them.
7. **Free-space check** — `checkFreeSpace` records free space on the `assemblyDir` volume and sets `summary.lowDisk` when below `minFreeTB` (default 6), which is a third mail trigger.

- **Config keys**: `archiveDir` (required), `assemblyDir` (enables stages 4–5), `orbitDir`, `queueDir` (default `assemblyDir`), `region`/`searchArea`, `satellites`, `productType`, `beamMode`, `direction`, `firstDate`/`lastDate`, `maxDownloads`, `reducePattern`, `maxAttempts`, `logDir`, `fileWorkers` (default 2), `notifyEmail` (**opt-in, no default** — with no key nothing is mailed, matching `nisargrimpworkflow.autoupdate.notifyOnErrors`; do not default it to `root`, `/etc/aliases` fans root out to several people).
- **Isolation flags**: `--fileData` (run only stage 4), `--checkFrames` (run only stage 5), `--noOrbits`, `--noDownload`, `--noFileDuringDownload`.
- **`--check`** — dry run across every stage; writes/moves nothing (threads `check` into `refreshOrbits`, `searchGranules` → temp dir, `downloadStage`, `fileS1`, `checkFrames`). Never mails.
- **Session reporting** — `_SessionSummary` is a module-level accumulator (like `log`); stages call `summary.add()`. `main()` writes `<log>.summary` in a `finally` (so a crash still gets one) and, only if granules are still missing after the retry pass, `<log>.failures` — bare URLs, one per line, feedable straight to `ariaDownload`. `mailReport` mails the summary to `notifyEmail` **only** on unrecovered failures or a crash; best effort via `mail`/`mailx`, a missing MTA warns rather than failing the run.
- **Cross-host locks** (`crossHostLock`, atomic `O_EXCL` lock file — NFS `flock` is unreliable) live on the shared tree, **each beside what it guards**: `<projectDir>/autoupdateS1_download.lock` (stages 2–3, guards `archiveDir`) and `<assemblyDir>/.assemblyTree.lock` (stages 4 **and** 5, plus the whole download stage when filing concurrently). The assembly lock is in `assemblyDir`, **not** the project dir, so `s1setup.setupTrack --queue` can take it from `--assemblyDir` alone — otherwise `checkFramesS1.executeMoves` can `shutil.move` a SAFE out from under a running setupTrack. Path via `assemblyLockPath(config)`. Non-blocking (skip the stage if held), auto-reclaim after `STALE_LOCK_HOURS` (48 h). `--check` takes no lock but prints a **bold-blue** `LOCK ACTIVE` notice if one is held (results may be mid-flight).
  - With concurrent filing, `runUpdate` nests `LOCK_DOWNLOAD` **then** `LOCK_FILE` (via `ExitStack`) and holds both for the whole download stage, passing `lockHeld=True` to `fileStage`/`frameCheckStage` so they do not try to re-acquire. Non-blocking acquires cannot deadlock. If `LOCK_FILE` is unavailable the run downloads without filing and the zips stay `.zip` for a later sweep. Caveat: a long run now blocks other hosts from filing/frame-checking for its duration, and `STALE_LOCK_HOURS` measures **run duration, not liveness** (mtime is stamped once at creation) — a run exceeding 48 h would have its lock reclaimed. Not reachable at `maxDownloads: 300` (~13 h); if that cap is raised a lot, add an mtime heartbeat first.

## fileS1

Globs `zipDir/<YYYY-MM>/*.zip` (`--monthSubdirs`, all months) — or flat `zipDir/*.zip` — and `unzip -u`s each SAFE into `assemblyDir/track-<n>/<orbit>/` (track from `orbit % 175 − satConst[sat]`), **excluding** cross-pol (`*-slc-hv*`/`*-slc-vh*`), then renames the source `.zip` → `.zip.1`. `--filed <yaml>` is an **output** (`tracks:`/`granules:` filed this run). `--assemblyDir` defaults `.`; `--createTrackDir` makes missing `track-<n>`. `--check` reports without moving. (Moved here from `s1setup`/`insarScripts`; `utilities` provides `runMyThreads`.)

- **`fileOneZip(zipFile, assemblyDir, …)`** is the per-zip entry point; `fileS1()` is a batch driver over it and `autoupdateS1`'s pipeline calls it per granule. Returns `(status, track, zipFile)`, status `FILED`/`SKIPPED`/`CHECKED`/`ERROR`. **Never raises and never calls `u.myerror`** — that is `sys.exit()`, which a worker thread swallows silently, so the CLI's hard-error-on-missing-track-dir stays in the `fileS1()` driver on the main thread.
- **The `-x` cross-pol exclusions are load-bearing, not redundant with the reduce.** `remove_files_from_zip` matches the plain substring `reducePattern` (default `hv`), so `'hv' in 's1c-iw1-slc-vh-…'` is False and **the reduce is a no-op on VV/VH scenes** — only `unzip -x` strips their cross-pol. Verified on a real `1SDV` granule. Do not "simplify" these away.
- **The `.zip` → `.zip.1` rename is gated on unzip success** (`status in (0, 1)` — unzip returns 1 for warnings — plus the `.SAFE` existing). It is the durable done-marker: a `.zip.1` drops out of the next glob and reads as already-held to `searchASF`'s dedup, so renaming after a failed unzip silently destroyed the granule. This required replacing `pushd d; unzip -d ./; popd; mv` with `unzip -d d`, because csh returns the **last** command's status.
- Skips zips that are still downloading (`.aria2` present) or structurally invalid, via `zipComplete` (which now lives here and is re-exported by `autoupdateS1`). Refiles over a `.SAFE` that fails `safeLooksComplete` (a crash mid-unzip otherwise looks "already downloaded" for ever — a trap the old unconditional rename masked).
- `alreadyDownloaded` matches `<orbit>` and `<orbit>_[0-9]*` (checkFramesS1 splits datatakes into `_1`..`_9`), but deliberately **not** its `<orbit>-<seq>` output dirs.
- `writeFiledRecord` **merges** into an existing record and writes via a temp file + rename, so incremental writes accumulate and the per-day path is no longer clobbered by a second same-day run.

## queueS1

The queue-file contract, shared with `s1setup.setupTrack`. **Imports nothing heavy on purpose** — `import checkFramesS1` costs ~2.3 s (utilities→gdal/scipy, refreshOrbits→requests), `import queueS1` costs ~0.04 s. That is what makes the `s1setup → asfSearchAndDownload` dependency acceptable; don't add heavy imports here.

- `applyQueueDeltas(queueDir, add=, remove=, update=)` — locked read-modify-write, applied per queue as **remove → add → update** (so one call can move a unit between queues). **Use this, not `writeQueues`**: a full rewrite from a stale snapshot silently undoes a concurrent writer, and `checkFrames` reads its queues minutes before it writes them. `update` with a value of `None` deletes the key. Add is first-wins, so a re-add never resets `notified`.
- `queueLock` — `O_EXCL`, 30 s wait, 15 min stale reclaim. `_atomicDump` writes temp + `os.replace` (atomic on NFSv4) and skips unchanged files so mtimes stay meaningful.
- `problemRecord(unit, comment, source, base=)` / `unnotified()` / `markNotified()` — the notification handshake. `base=None` gives a partial record with no frame keys, so **consumers must key off `unit` alone**.
- To re-notify a unit already in `problem`, **remove and re-add it** — that clears `notified` and installs the fresh comment.
- **`processed.<YYYY-MM-DD>.yaml` / `completed.yaml` are NOT queues** and are deliberately absent from `QUEUES`: they only grow, so letting them join `applyQueueDeltas` would mean parsing the whole all-time record (~1.2 MB at 8.5k units, +1–2.5k/yr) to append one line. Written by `setupTrack --queue`: `appendProcessed` flushes per unit; `mergeProcessed` folds **every** dated file into `completed.yaml` at end of run (so a file orphaned by a crash is swept up later), keyed on `(unit, finished)` so re-merge is a no-op but a genuine reprocess is a separate entry; dated files are pruned after `RETAIN_DAYS` (5), **always after the merge**.
- Success is otherwise recorded only on the filesystem — the `Completed` marker and the `<orbit>-<seq>` output dir, which is what `isProcessed()` checks and what makes re-runs idempotent. There is no "processed" queue.

## checkFramesS1

"**frame**" = **burst number** from the ascending-node time at the **2.759 s** IW burst period (reproduces `s1setup/checkframes.py`; no ESA frame concept). Per bare orbit dir in `[--firstDate,--lastDate]` (default today−6mo..today), reads the track's `frameRange` file (default `[300,750]`) and restructures:
- **Out-of-range** SAFEs (wholly outside range) → `track-*/tmp/`.
- **Gaps** (>1-burst break): near segment stays in `<orbit>`; far segments → `<orbit>_1, _2, …`.
- **Over-length**: split only when the **frameRange-clamped** in-range span > 127 (`rangeSpan`, *not* raw extent — this is the fix for spurious splits); first *n* SAFEs → `<orbit>_4`, *n* derived deterministically from `frameRange` (not read from existing, inconsistently-numbered splits). New `_N` dirs are seeded with the bare orbit's `ascendingNodeTime` cache.

Each resulting unit routes to a **cumulative, idempotent** YAML queue in `queueDir` — `toProcess` (EOF orbit ready) / `pendingProcessing` (`no orbit` yet — POEORB lands ~3 wk out) / `problem` (unresolved). Records carry `unit, orbit, date, startFrame, endFrame, totalFrames`. At startup, `promotePending` re-checks pending units and moves any whose orbit arrived → `toProcess` (full-rewrite queues). Fully-processed units (`Completed` marker or `{orbit}-{seq}` output dir, per `setupTrack`) show `-> processed` and are never restructured/re-queued. `--check` prints one date-ordered line per orbit with the day-gap (`+Nd`, rounded) and changes nothing. Positional `track` (`track-16` / `16`) scopes to one track; omit for all. Orbit availability via `refreshOrbits.orbitFileReady` (EOF validity window covers the acquisition).

## Notes

- `writeSearchGpkg.py` requires `osgeo.ogr`/`osgeo.osr` (GDAL); `searchASF` imports it lazily only when `--gpkg` is given, and `_read_polygon_shapefile` also needs GDAL for `.shp` search areas.
- `MAX_RESULTS = 10000` in `searchASF`; a warning is printed if a search hits this cap (results may be incomplete — narrow the date range or area).
- Per-product URL files (`<output>.<PRODUCT>`) and the `volume_by_product` summary (printed in GB) are always generated for "found" (new) granules, regardless of `--gpkg`.
