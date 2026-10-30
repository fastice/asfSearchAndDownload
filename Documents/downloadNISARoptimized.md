# downloadNISARoptimized

Fetch NISAR **GCOV, ROFF, RUNW, GOFF and GUNW** products keeping only the datasets the
GrIMP tools read: GCOV as float16 with the RTC factor stored once per track/frame/grid, the
others as float32 rounded to 10 mantissa bits. Internal paths are unchanged, so every reader
(`geomosaic`, `nisarhdf`, GDAL) opens a slim product as it would the full one. Part of the
`asfSearchAndDownload` package. Replaces the whole-product `aria2c` fetch, and is what the
nightly `autoupdateNISAR` uses for RUNW/ROFF (and GUNW/GOFF, if a project lists them).

## Telling it what to download

`--urls FILE` is a text file with one granule URL per line, and `--product` says which
product type those URLs are (one product per run; the descriptor is chosen from it). The URL
lists normally come straight from `searchASF`, which writes one per product type:

```
searchASF 2026-10-20 2026-10-22 found --products GUNW GOFF --antarctica
    -> found.GUNW, found.GOFF  (and found = all of them)
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNW
downloadNISARoptimized --product GOFF --urls found.GOFF --outputDir GOFF
```

With `--local`, the same file lists local granule paths instead and they are converted with no
network. `--check` plans the fetch and prints how much of each granule would be transferred,
without downloading. Granules whose output already exists are skipped, so a rerun resumes.
`downloadNISARoptimized --help` ends with the default bands for every product.

## Sizes at default settings

One granule of each product, archive product against the slim file written with no options
(measured 2026-10-30 with this version). Defaults: float32 rasters 10 mantissa bits,
`unwrappedPhase` 12; float64 geometry cubes 30 of 52 bits, float64 correction cubes 23 of 52;
float32 metadata cubes kept exactly; every dataset stored compressed (ISCE3 writes the baseline
cubes uncompressed; they are gzipped losslessly).

| Product | Granule (track_frame, mode) | Archive | Slim | Smaller by | Bands kept | Mantissa bits |
|---|---|---|---|---|---|---|
| GCOV | 030_001_A_136, 40+5 MHz | 4572 MB | 862 MB + 540 MB shared factor (first cycle only) | 5.3x (3.3x first cycle) | `HHHH`, `mask`, `rtcGammaToSigmaFactor` (shared file), radarGrid `incidenceAngle`, all metadata | float16 (10 bits, 5-bit exponent) on `HHHH` and the factor |
| ROFF | 025_100_D_128, 40 MHz | 1065 MB | 229 MB | 4.7x | `slantRangeOffset`, `alongTrackOffset`, `correlationSurfacePeak` (layers 1-3), `mask`, geolocation cubes, all metadata | 10 on the three offset bands; float64 cubes 30 |
| ROFF | 018_084_D_051, 77 MHz | 495 MB | 121 MB | 4.1x | as above | as above |
| RUNW | 025_100_D_128, 40 MHz | 375 MB | 101 MB | 3.7x | `unwrappedPhase`, `coherenceMagnitude`, `ionospherePhaseScreen`, `connectedComponents`, `mask`, pixelOffsets (3 bands), geolocation cubes except `coordinateX/Y`, all metadata | 10 on float32 rasters, `unwrappedPhase` 12; float64 cubes 30 |
| GOFF | 023_157_A_143, 40 MHz | 929 MB | 322 MB | 2.9x | `slantRangeOffset`, `alongTrackOffset`, `correlationSurfacePeak` (layers 1-3), `mask`, radarGrid cubes, all metadata | 10 on the three offset bands; float64 cubes 30 |
| GUNW | 023_157_A_143, 40 MHz | 1690 MB | 362 MB | 4.7x | unwrappedInterferogram (`unwrappedPhase`, `coherenceMagnitude`, `ionospherePhaseScreen`, `connectedComponents`, `mask`), pixelOffsets (3 bands), radarGrid cubes, all metadata | 10 on float32 rasters, `unwrappedPhase` 12; float64 geometry cubes 30, correction cubes 23 |

The GCOV factor is written once per track/frame/grid and reused by every later cycle, so the
steady-state GCOV cost is the 862 MB. "Bands kept" lists the rasters; everything not dropped is
kept (`--help` lists what is dropped). What remains in GOFF/GUNW is mostly the radarGrid cubes
(20 heights x 817 x 818 on a 40 MHz granule, against 20 x 585 x 164 for ROFF/RUNW): the float32
cubes, kept exactly, are 125 MB of each once the baselines are gzipped.

## Rounding errors at default settings

Worst case and rms of (rounded - original) over every valid sample, in the band's own units,
on the same granules (layer 1 shown for ROFF/GOFF; layers 2-3 behave the same). The worst case is
always |value| x 2^-(bits+1), so it scales with the largest value in the band.

| Product | Band | Bits | Values | Worst case | rms |
|---|---|---|---|---|---|
| ROFF | `slantRangeOffset` | 10 | -100 to 100 m | 3.1 cm | 4.4 mm |
| ROFF | `alongTrackOffset` | 10 | -141 to 141 m | 6.2 cm | 8.1 mm |
| ROFF | `correlationSurfacePeak` | 10 | 0 to 0.92 | 2.4e-4 | 6.1e-5 |
| ROFF | geolocation `groundTrackVelocity` | 30 | 6574 to 6646 m/s | 1.9 um/s | 1.1 um/s |
| RUNW | `unwrappedPhase` | 12 | -429 to 202 rad | 31 mrad | 11 mrad |
| RUNW | `ionospherePhaseScreen` | 10 | -30 to 47 rad | 16 mrad | 5.6 mrad |
| RUNW | `coherenceMagnitude` | 10 | 0.0004 to 0.95 | 2.4e-4 | 1.2e-4 |
| RUNW | pixelOffsets `slantRangeOffset` / `alongTrackOffset` | 10 | -12 to 0.8 m / -32 to 11 m | 3.9 mm / 7.8 mm | 1.5 mm / 3 mm |
| RUNW | pixelOffsets `correlationSurfacePeak` | 10 | -0.08 to 0.89 | 2.4e-4 | 6.3e-5 |
| RUNW | geolocation `groundTrackVelocity` | 30 | 6574 to 6646 m/s | 1.9 um/s | 1.1 um/s |
| GOFF | `slantRangeOffset` | 10 | -100 to 99 m | 3.1 cm | 7.7 mm |
| GOFF | `alongTrackOffset` | 10 | -141 to 140 m | 6.2 cm | 2.2 cm |
| GOFF | `correlationSurfacePeak` | 10 | 0 to 0.78 | 2.4e-4 | 1.8e-5 |
| GOFF | radarGrid `reference/secondarySlantRange` | 30 | 8.3e5 to 1.21e6 m | 0.49 mm | 0.19 mm |
| GOFF | radarGrid `reference/secondaryZeroDopplerAzimuthTime` | 30 | 11057 to 11146 s | 3.8 us (2.5 cm along track) | 2.2 us |
| GOFF | radarGrid `groundTrackVelocity` | 30 | 6541 to 6683 m/s | 1.9 um/s | 1.1 um/s |
| GUNW | `unwrappedPhase` | 12 | -68 to 124 rad | 7.8 mrad | 0.96 mrad |
| GUNW | `ionospherePhaseScreen` | 10 | -108 to 18 rad | 31 mrad | 8.2 mrad |
| GUNW | `coherenceMagnitude` | 10 | 0.002 to 0.87 | 2.4e-4 | 3.4e-5 |
| GUNW | pixelOffsets `slantRangeOffset` / `alongTrackOffset` | 10 | 0 to 7.5 m / -0.9 to 24 m | 2 mm / 7.8 mm | 0.81 mm / 1.2 mm |
| GUNW | pixelOffsets `correlationSurfacePeak` | 10 | -0.02 to 0.77 | 2.4e-4 | 1.3e-5 |
| GUNW | radarGrid slant range, azimuth time, `groundTrackVelocity` | 30 | as GOFF | as GOFF | as GOFF |
| GUNW | radarGrid `wet` / `hydrostaticTroposphericPhaseScreen` | 23 | -1.2 to 0.13 / -1.4 to 5.6 rad | 60 nrad / 240 nrad | 5.2 nrad / 65 nrad |
| GUNW | radarGrid `slantRangeSolidEarthTidesPhase` | 23 | 1.1 to 1.5 rad | 60 nrad | 34 nrad |
| GCOV | `HHHH` (float16) | 10 | linear power, floored at 1e-4 (-40 dB) | 0.01 dB | -- |

What the rounding does downstream, measured through the GrIMP chain on the 40 MHz Antarctic
frame (025_100_D_128), slim against full, with a repeatable `lltora`. The RUNW rows were measured
when its float32 cubes were rounded too (squint polynomial 0.0005 deg, centre incidence
0.0024 deg), so they are an upper bound for the current defaults:

| Product used slim | Result | Difference |
|---|---|---|
| ROFF (ROFFtoGrimp) | final range / azimuth offsets | 3.2e-4 / 9.5e-5 px rms; 0.003% of range pixels off by > 0.01 px, none by > 0.5 px |
| RUNW (one-frame SetupNISAR, full ROFF) | corrected unwrapped phase | 1.2e-4 rad rms (max 0.085 rad, std of the field 155 rad) |
| RUNW | ionosphere correction | 2.6e-4 rad rms (max 0.005) |
| RUNW | range / azimuth offsets | identical |

For scale: in a 12-day L-band pair 1 rad of phase is about 19 mm of line-of-sight motion,
0.58 m/yr; one 40 MHz range pixel (3.1 m slant) is about 95 m/yr.

## Examples

```
# what to fetch: searchASF writes one URL list per product type (<output>.<PRODUCT>)
searchASF 2026-10-20 2026-10-22 found --products RUNW ROFF GUNW GOFF --antarctica

# slim downloads at the defaults, one product per run
downloadNISARoptimized --product ROFF --urls found.ROFF --outputDir ROFF
downloadNISARoptimized --product RUNW --urls found.RUNW --outputDir RUNW
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNW
downloadNISARoptimized --product GOFF --urls found.GOFF --outputDir GOFF
downloadNISARoptimized --urls gcovUrls.c030 --outputDir GCOV --factorDir GCOV/factors   # GCOV

# see how much each granule would transfer, without downloading
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNW --check

# keep GUNW's wrapped interferogram and its 80 m coherence
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNW --addBands wrappedInterferogram

# also drop the tropospheric and solid-earth-tide cubes from GUNW
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNW \
    --dropBands wetTroposphericPhaseScreen hydrostaticTroposphericPhaseScreen slantRangeSolidEarthTidesPhase

# keep ROFF's snr and round to 12 bits instead of 10
downloadNISARoptimized --product ROFF --urls found.ROFF --outputDir ROFF --addBands snr --mantissaBits 12

# GUNW with more precision on phase and on the float64 geometry cubes
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNW --phaseBits 14 --geometryBits 36

# full precision, unused bands still dropped
downloadNISARoptimized --product RUNW --urls found.RUNW --outputDir RUNW --float32

# slim granules already on disk (originals untouched; --outputDir must be elsewhere)
ls /Volumes/insar4/ian/Data/NISAR/Antarctica40/ROFF/*.h5 > roffPaths
downloadNISARoptimized --product ROFF --local --urls roffPaths --outputDir ROFFslim
```

## Choosing bands and precision

```
--mantissaBits N     bits (of 23) kept on ROFF/RUNW/GOFF/GUNW float32 rasters [10]
--phaseBits N        bits (of 23) kept on unwrappedPhase [mantissaBits + 2]
--geometryBits N     bits (of 52) kept on the float64 slant-range, azimuth-time and
                     groundTrackVelocity cubes [30]
--correctionBits N   bits (of 52) kept on the float64 tropospheric and solid-earth-tide
                     phase cubes [23]
--dropBands A B ...  also drop datasets or groups with these names, wherever they are
--addBands A B ...   keep names the product defaults drop
--float32            no rounding at all (and no float16 for GCOV)
```

GCOV uses float16 regardless of these. The float32 metadata cubes (incidence and elevation
angle, line-of-sight and along-track unit vectors, baselines) are never rounded.

Names match any path component, so a group name drops the whole group
(`--addBands wrappedInterferogram` keeps GUNW's wrapped bands; `--dropBands
wetTroposphericPhaseScreen hydrostaticTroposphericPhaseScreen` drops those cubes). A dataset you
drop is taken out of the verification list, so it does not fail the granule. `--addBands` of a
name that is not dropped by default prints a note and changes nothing.

Each ROFF/RUNW/GOFF/GUNW slim granule carries a root attribute `grimpSlim` (JSON: product,
polarization, the four bit settings, dropped names), which records what was done and is the
mark `--local` uses to skip an already-slim file. Every rounded dataset carries
`grimpMantissaBits`.

Default bands (also printed by `--help`):

| Product | Drops | Rounds |
|---|---|---|
| GCOV | `numberOfLooks`, `inputDataExceptionMask`, the squinted radarGrid cubes except `incidenceAngle` | `HHHH` and `rtcGammaToSigmaFactor` to float16 (factor shared) |
| ROFF | the three variances, `snr`, `digitalElevationModel` | `slantRangeOffset`, `alongTrackOffset`, `correlationSurfacePeak`; float64 geometry cubes |
| GOFF | the three variances, `snr` | `slantRangeOffset`, `alongTrackOffset`, `correlationSurfacePeak`; float64 geometry cubes |
| RUNW | both `digitalElevationModel`s, `ionospherePhaseScreenUncertainty`, `coordinateX/Y` cubes | every float32 raster; float64 geometry cubes |
| GUNW | the whole `wrappedInterferogram` group, `ionospherePhaseScreenUncertainty` | every float32 raster; float64 geometry and correction cubes |

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

## ROFF (`--product ROFF`)

Keeps what `ROFFtoGrimp` reads: `slantRangeOffset`, `alongTrackOffset` and
`correlationSurfacePeak` in all three layers, plus every metadata dataset (geolocation grid,
orbit, identification). Drops the three variances, `snr` and the DEM -- `ROFFtoGrimp`'s sigmas
come from `cullst`, not the product. The kept layers stay **float32 with the same filters and
paths**, rounded to 10 mantissa bits (relative error <= 4.9e-4), so `nisarhdf` and GDAL read
them unchanged; the rounding is what lets gzip+shuffle pack them ~2x tighter. No shared
factor, so `--factorDir` is not used. Each rounded layer carries `grimpMantissaBits = 10`.

Sizes and errors: see *Sizes at default settings* and *Rounding errors at default settings*.

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
`ionospherePhaseScreenUncertainty`. Every float32 raster is rounded to 10 mantissa bits,
`unwrappedPhase` to 12 (its error scales with |phase|: at ~430 rad, 10 bits is 0.12 rad max,
12 bits 0.031 rad); the float64 `groundTrackVelocity` cube to 30 of 52; the float32 cubes are kept
exactly, the two uncompressed baselines (57 MB) gzipped losslessly.

Measured on a 40 MHz Antarctic frame (025_100_D_128) with float32 cubes rounded (the earlier
default; now kept exactly): through a one-frame
`SetupNISAR` sandbox (slim RUNW, full ROFF) against the full product: corrected unwrapped phase
1.2e-4 rad RMS (max 0.085), ionosphere correction 2.6e-4 rad RMS, range/azimuth offsets
identical.

## GOFF and GUNW (`--product GOFF|GUNW`)

The geocoded equivalents of ROFF and RUNW, with the same choices: GOFF keeps and rounds the three
offset bands ROFFtoGrimp uses and drops the variances and `snr`; GUNW keeps
`unwrappedInterferogram` (whose `coherenceMagnitude` `globalMosaic` reads) and `pixelOffsets`,
drops `ionospherePhaseScreenUncertainty`, and also drops the whole `wrappedInterferogram` group
(complex64 interferogram, its own 80 m coherence and mask -- about half a 40 MHz granule).

Both round the float64 radarGrid geometry cubes (slant range, azimuth time, ground-track
velocity) to 30 of 52 bits, and GUNW its tropospheric and solid-earth-tide cubes to 23; the
float32 cubes are kept exactly. Sizes and errors: see *Sizes at default settings* and *Rounding
errors at default settings*.

## Reading slim products

`nisarhdf` (ROFF, RUNW, GOFF and GUNW readers) limits its *default* field lists to the bands a
file holds (`fieldsInFile`), printing
one `not in file, skipped:` line per list, and `wrapH5WithVRT` does the same, so full and slim
products both open and VRT. A field requested explicitly with `fields=` still fails if absent.

## GCOV polarizations

`--polarization` keeps **one** covariance term (default `HHHH`); every other term in the grid --
`HVHV`, `VVVV` and the off-diagonal `HHHV`-style terms -- is dropped, as is frequency B
(`--frequency` picks the grid). The RTC factor is per grid, not per term, so a shared factor
serves every term. To keep two terms today, run once per term into separate `--outputDir`s:
both runs name the output after the granule, and a run skips any granule whose output already
exists.

Two consequences of keeping one term, not yet handled:

- `listOfCovarianceTerms` is copied unchanged, so a slim dual-pol granule still lists the
  terms it no longer holds. `geomosaic` is unaffected -- it reads only the term its yaml names
  (`HH` -> `HHHH`) -- but anything that trusts the list will look for the missing ones.
- `nisarhdf.nisarGCOVHDF` loads, by default, every listed term plus `numberOfLooks` and
  `rtcGammaToSigmaFactor`, none of which a slim GCOV holds in the granule itself; opening a slim
  GCOV with default fields is therefore expected to fail (not tested -- only single-pol slim
  GCOVs exist so far, and `geomosaic` is the consumer). Passing `fields=['HHHH', 'mask']`
  avoids it.

## Local conversion (`--local`)

With `--local`, `--urls` lists **local** granule paths, which are converted with
the same repack as the fetch path (no network). Originals are never modified or
removed, and the run refuses to start if `--outputDir` is where they live.
Granules already slim (GCOV in float16; others carrying `grimpSlim`, or for ROFF/RUNW made
before that attribute, missing `snr` / `ionospherePhaseScreenUncertainty`) are skipped.

## CLI

```
downloadNISARoptimized --urls FILE [options]

  --urls FILE          File with one granule URL (or, with --local, path) per line
  --outputDir DIR      Where slim granules are written [.]
  --factorDir DIR      Where shared RTC factors live [<outputDir>/factors]
  --product P          GCOV, ROFF, RUNW, GOFF or GUNW [GCOV]
  --polarization TERM  Covariance term (GCOV) or polarization to keep
                       [HHHH for GCOV, HH otherwise]
  --frequency A|B      Frequency [A]
  --mantissaBits N     Bits (of 23) kept on float32 rasters (not GCOV) [10]
  --phaseBits N        Bits (of 23) kept on unwrappedPhase [mantissaBits + 2]
  --geometryBits N     Bits (of 52) kept on float64 slant-range/azimuth-time/
                       groundTrackVelocity cubes [30]
  --correctionBits N   Bits (of 52) kept on float64 troposphere/solid-earth-tide cubes [23]
  --dropBands A B ...  Also drop datasets/groups with these names
  --addBands A B ...   Keep names the product defaults drop
  --float32            Keep the float bands at full precision
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

See *Examples* above.

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
