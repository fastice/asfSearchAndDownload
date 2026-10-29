# downloadNISARoptimized

Fetch **NISAR GCOV** products keeping only the datasets the GrIMP tools read,
as float16, with the RTC factor stored once per track/frame/grid. Part of the
`asfSearchAndDownload` package. Replaces the whole-product `aria2c` fetch for
GCOVs.

## What it does

A GCOV is ~3 GB with ~280 datasets; `geomosaic` reads five, and
`numberOfLooks` alone is 26% of the file. For each URL this:

1. Resolves a presigned URL (GET redirect followed to the end, via `~/.netrc`
   Earthdata credentials) and plans which byte ranges the kept datasets occupy.
2. Fetches only those ranges, coalesced into large runs over parallel HTTP range
   requests, into a sparse scratch file (see [Scratch space](#scratch-space)).
3. Repacks into a slim HDF5 with the **same internal paths**, so `geomosaic` and
   `nisarhdf` read it unchanged. The two big float bands become float16 (values
   outside the float16 range are clamped, and the count is recorded).
4. Stores `rtcGammaToSigmaFactor` once per track/direction/frame/mode/grid in
   `--factorDir`, and leaves a symlink there named like the granule; later cycles
   of the same position reuse it instead of fetching it again.
5. Verifies the slim file (required datasets present, no float16
   underflow/inf inside the valid mask, factor grid matches) and appends a line
   to `<outputDir>/slimManifest.jsonl`.

Repack of one granule overlaps the fetch of the next (`--repackWorkers`
processes). Granules whose output already exists are skipped, so a rerun
resumes.

Measured on one Antarctic granule (3.06 GB / 172 s for the archive product):

| | download | stored |
|---|---|---|
| first cycle (fetches the factor) | 1.96 GB / 103 s | 681 MB + 250 MB factor |
| later cycles (factor reused) | 1.13 GB / 60 s | 681 MB (**4.5x** smaller) |

The slim product mosaics to the same valid-pixel count with at most 0.01 dB
difference.

### How geomosaic finds the shared factor

The GCOV yaml gets `factorFrom: <factorDir>`, and `geomosaic` opens
`<factorDir>/<granule basename>` — the per-granule symlink — so the C code never
derives the sharing key. Do not replace this with an HDF5 virtual dataset: on
GDAL 3.11.5/HDF5 2.2.0 it is ~3000x slower and fails silently when the source is
missing.

## ROFF (`--product ROFF`)

Keeps what `ROFFtoGrimp` reads: `slantRangeOffset`, `alongTrackOffset` and
`correlationSurfacePeak` in all three layers, plus every metadata dataset (geolocation grid,
orbit, identification). Drops the three variances, `snr` and the DEM -- `ROFFtoGrimp`'s sigmas
come from `cullst`, not the product. The kept layers stay **float32 with the same filters and
paths**, rounded to 10 mantissa bits (relative error <= 4.9e-4), so `nisarhdf` and GDAL read
them unchanged; the rounding is what lets gzip+shuffle pack them ~2x tighter. No shared
factor, so `--factorDir` is not used. Each rounded layer carries `grimpMantissaBits = 10`.

Measured:

| granule | archive | slim | fetched |
|---|---|---|---|
| Greenland 77 MHz (018_084_D_051) | 495 MB | 148 MB | 0.23 GB / 12 s |
| Antarctica 40 MHz (025_100_D_128) | 1065 MB | 285 MB | (local) |

Through `ROFFtoGrimp` (40 MHz frame, production flags) the final `range.offsets` /
`azimuth.offsets` differ from the full product by ~1e-3 px RMS, 0.06% / 0.005% of range
pixels by more than 0.01 / 0.05 px -- smaller than `ROFFtoGrimp`'s own run-to-run scatter on
identical input (0.38% / 0.06%) before `lltora` was made repeatable (mosaicSource 6675133); with
the repeatable `lltora` the full-vs-slim difference is 3.2e-4 px range / 9.5e-5 px azimuth RMS.

## RUNW (`--product RUNW`)

Keeps what `RUNWtoGrimp`, `estimateIonosphere` and `carrierCorrection` read: `unwrappedPhase`,
`coherenceMagnitude`, `ionospherePhaseScreen`, `connectedComponents`, `interferogram/mask`, all of
`pixelOffsets` except its DEM, and the geolocation grid except `coordinateX`/`coordinateY` (GrIMP
does its own geocoding; only nisarhdf's uncalled `xyCube` reads them). Drops both DEMs (nisarhdf
fills the interferogram one with zeros rather than reading it) and
`ionospherePhaseScreenUncertainty`. Every float32 raster and cube is rounded to 10 mantissa bits,
`unwrappedPhase` to 12 (its error scales with |phase|: at ~430 rad, 10 bits is 0.12 rad max,
12 bits 0.031 rad). Cubes stored uncompressed (the two baselines, 57 MB) are gzipped as well.

Measured on a 40 MHz Antarctic frame (025_100_D_128): 375 MB -> 70 MB. Through a one-frame
`SetupNISAR` sandbox (slim RUNW, full ROFF) against the full product: corrected unwrapped phase
1.2e-4 rad RMS (max 0.085), ionosphere correction 2.6e-4 rad RMS, range/azimuth offsets
identical.

## Reading slim products

`nisarhdf` limits its *default* field lists to the bands a file holds (`fieldsInFile`), printing
one `not in file, skipped:` line per list, and `wrapH5WithVRT` does the same, so full and slim
products both open and VRT. A field requested explicitly with `fields=` still fails if absent.

## GCOV granules written before the float16 floor

`verifySlim` fails a GCOV whose valid minimum is below `F16FLOOR` only if its band carries
`grimpFlooredToF16Floor`. Granules written before the clamp (8c6224c) lack it and get a
`note: ... predates the F16FLOOR clamp` line instead; reconvert them to apply the floor (the lost
pixels cluster at frame edges).

## Local conversion (`--local`)

With `--local`, `--urls` lists **local** granule paths, which are converted with
the same repack as the fetch path (no network). Originals are never modified or
removed, and the run refuses to start if `--outputDir` is where they live.
Granules already in float16 are skipped.

## CLI

```
downloadNISARoptimized --urls FILE [options]

  --urls FILE          File with one granule URL (or, with --local, path) per line
  --outputDir DIR      Where slim granules are written [.]
  --factorDir DIR      Where shared RTC factors live [<outputDir>/factors]
  --product GCOV|ROFF  NISAR product type [GCOV]
  --polarization TERM  Covariance term (GCOV) or polarization (ROFF) to keep
                       [HHHH for GCOV, HH for ROFF]
  --frequency A|B      Frequency [A]
  --float32            Keep the float bands at full precision
                       (default float16 for GCOV, 10-bit mantissa for ROFF)
  --noShareFactor      Store the RTC factor in each granule instead of sharing it
  --connections N      Parallel range requests per granule [16]
  --limit MBPS         Bandwidth cap in MB/s, 0 for none [19]
  --repackWorkers N    Repack processes overlapping the next download [3]
  --tmpDir DIR         Where the sparse scratch files go
                       [/dev/shm if big enough, else <outputDir>/.scratch]
  --maxGranules N      Stop after this many, 0 for all [0]
  --check              Report what would be fetched and exit
  --local              Convert local granules instead of downloading
```

Example:

```
downloadNISARoptimized --urls gcovUrls.c030 --outputDir GCOV --factorDir GCOV/factors
```

## Scratch space

Each granule in flight holds a sparse scratch file of up to ~3.3 GB real (its
apparent size is the whole product). The fetch is held back while repacks catch
up, so at most `--repackWorkers + 1` exist at once: 16 GB at the default of 3
(4 GB budgeted per granule).

Without `--tmpDir`, `/dev/shm` is used only if its free space covers that **and**
the machine's available RAM covers it plus a 4 GB reserve (tmpfs pages are RAM).
Otherwise — including on macOS, which has no `/dev/shm` — the scratch goes to
`<outputDir>/.scratch` on disk. An explicit `--tmpDir` is used as given. The run
stops if the chosen directory has less free space than needed; lower
`--repackWorkers` to need less.

Scratch files are named `<granule>.<pid>@<host>.sparse`. At start-up, leftover
files from interrupted runs are removed — those whose pid is dead on this host,
and untagged names from older versions once they are a day old — so concurrent
runs can safely share one scratch directory.

Exit status: 0 on success, 69 if any granule failed verification or repack;
1 on a fatal setup error.
