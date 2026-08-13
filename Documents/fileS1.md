# fileS1

Unpack **Sentinel-1 SAFE zips** into the per-track/per-orbit tree that downstream
GrIMP/ISCE processing expects. Part of the `asfSearchAndDownload` package; the
filing step of [`autoupdateS1`](autoupdateS1.md) calls this in-process, and it is
also runnable standalone as `fileS1`.

## What it does

For each `*.zip` found in `zipDir`, it parses the S1 filename to get the sensor,
absolute orbit and dates, computes the **relative track** (`orbit % 175 −
satConst[sat]`), and unzips the SAFE into
`assemblyDir/track-<n>/<orbit>/` — **excluding** the cross-pol (`*-slc-hv*`,
`*-slc-vh*`) SLC measurement bands to save space. Unzips run four at a time.

The source `.zip` is renamed `.zip.1` (the `.1` marks an already-processed pass)
**only if the unzip succeeded** — the rename is the durable "this granule is
done" marker, since a `.zip.1` drops out of the next run's glob and reads as
already-held to `searchASF`'s dedup. A failed unzip therefore leaves the `.zip`
in place to be retried on a later pass. (Before 2026-08 the rename was
unconditional, so a failed unzip silently consumed the granule.)

A zip that is still downloading (an aria2c `.aria2` control file beside it) or
is not a structurally valid archive is skipped, so filing can safely run while
another invocation is downloading into the same archive.

A pass already unpacked under `assemblyDir/track-<n>/<orbit>[_N]/…SAFE` is
skipped unless `--overwrite` — unless the extracted `.SAFE` looks like a partial
unzip (no `manifest.safe`, or an empty `annotation/`), in which case it is
refiled over. The check deliberately ignores `measurement/`: `runPreProcTops`
consumes the measurement TIFFs, so an empty `measurement/` is the normal state
of an already-processed granule, and treating it as incomplete would re-extract
every processed granule in the archive.

### Per-granule entry point

`fileOneZip(zipFile, assemblyDir, …)` files a single zip and returns
`(status, track, zipFile)` with status `FILED`/`SKIPPED`/`CHECKED`/`ERROR`. It is
thread safe, never raises for an expected failure, and never calls `u.myerror`
(that is `sys.exit()`, which a worker thread swallows silently). `fileS1()` is a
batch driver over it; `autoupdateS1` calls it per granule as each download is
reduced.

## zipDir layout: flat vs month subdirs

- Default: globs a **flat** `zipDir/*.zip`.
- `--monthSubdirs`: globs `zipDir/<YYYY-MM>/*.zip` across **all** month subdirs —
  the layout the S1 archive (`archiveDir/<YYYY>-<MM>/`) uses. `autoupdateS1`
  always passes this so it files the whole archive.

## Filed record (`--filed`)

`--filed <file.yaml>` writes a record of what was filed **this run**:

```yaml
tracks:   [16, 25, 90]                       # all tracks touched this run
granules: [/archive/2026-07/S1A_..._.zip, …] # source zip paths filed this run
```

`autoupdateS1` uses this to decide which tracks/files later processing steps work
on. (Format may evolve.)

It records what **actually filed**, not what the run intended to file — with the
rename now gated on a successful unzip those are no longer the same set.

Writes **merge** into any existing record rather than replacing it, so
`autoupdateS1`'s incremental writes during a run accumulate and a second run on
the same day no longer clobbers the first (the path is per-day,
`filedS1.<MM-DD-YYYY>.yaml`). The file is written via a temp file and renamed,
so a reader on NFS never sees a partial record.

## CLI

```
fileS1 [options]

  --zipDir DIR       Directory with zip files [/Volumes/insar10/ian/xfer]
  --assemblyDir DIR  Root under which track-<n>/<orbit>/ trees are built [.]
  --monthSubdirs     Glob zipDir/<YYYY-MM>/*.zip (all month subdirs) instead of
                     a flat zipDir/*.zip
  --filed FILE       Write a YAML record (tracks:/granules:) of what was filed
  --createTrackDir   Create track-<n> under assemblyDir if it does not exist
  --overwrite        Re-unpack passes already present
  --check            Dry run: report what would be filed (into which track/
                     orbit) without unpacking, renaming, or writing anything
```

Standalone example (flat dir, into the current directory):

```
fileS1 --zipDir /Volumes/insar10/ian/xfer --createTrackDir
```

As driven by `autoupdateS1` (equivalent call):

```
fileS1 --zipDir <archiveDir> --assemblyDir <assemblyDir> --monthSubdirs \
       --createTrackDir --filed <logDir>/filedS1.<MM-DD-YYYY>.yaml
```
