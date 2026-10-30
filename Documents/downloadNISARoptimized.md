# downloadNISARoptimized

Download NISAR **GCOV, ROFF, RUNW, GOFF and GUNW** products as *slim* copies: the same HDF5
files, with the same internal paths, but holding only the bands you need, stored compactly. You
give it a plain text file of HTTPS links to the granules, from any source (see *Telling it what
to download*). Part of the `asfSearchAndDownload` package.

### Why use it

NISAR products are large, and much of each file is something a given analysis never reads:

- **Bands you do not use.** A GUNW carries a full-resolution wrapped interferogram and its own
  coherence alongside the unwrapped products -- about half of a 1.7 GB granule. ROFF carries
  three variance layers, an SNR layer and a DEM; in a GCOV the number-of-looks band alone is
  about a quarter of the file; and most products carry a DEM or metadata cubes that are only
  sometimes needed.
- **Precision you cannot use.** Bands are stored as float32 (24 significant bits), far beyond
  the precision of the measurements, and that extra noise in the low bits is what keeps them from
  compressing well. Rounding to 10 mantissa bits (a relative error of at most 4.9e-4, which in
  the processing tested below stayed well under the noise of the measurements; see *Rounding
  errors*) lets the same compression shrink the bands severalfold.

This tool removes both -- the unneeded bands before they cross the network, the excess
precision after:

- **Smaller downloads.** It reads the archive product's index over HTTP and fetches only the
  byte ranges of the bands it keeps, so unneeded bands are never transferred. A 40 MHz GUNW
  transfers 0.92 GB instead of 1.69 GB; a 77 MHz ROFF 0.23 GB instead of 0.49 GB. The bands it
  keeps arrive as the archive stores them, at full precision: rounding cannot be done on the
  server, so it saves storage, not transfer.
- **Much less storage.** Once fetched, the kept bands are rounded and recompressed, so the files
  on disk are 3-5x smaller than the archive products (for example GUNW 1690 MB -> 371 MB, ROFF 1065 MB ->
  233 MB, GCOV 4572 MB -> 1078 MB). Over a regional archive of thousands of granules, that is the
  difference between tens of terabytes and a few.
- **Only the area you need.** For the geocoded products (GCOV, GOFF, GUNW), `--box` fetches just
  a lon/lat box of each granule -- a 1.2 x 0.5 degree box of a GUNW is 23 MB.
- **Nothing to change downstream.** Paths, chunking and attributes are those of the archive
  product, so `nisarhdf`, h5py, GDAL's HDF5 driver and the NISAR GDAL plugin read a slim file as
  they would the full one.

What you give up: the bands you drop are not on disk (keep any of them with `--addBands`, or
choose your own with `--dropBands`), and the rounded bands differ from the archive values by the
amounts in *Rounding errors* (or keep full precision with `--float32`). If you only want to look
at a few products, or read small pieces of many without keeping copies, reading the archive
products in place with the NISAR GDAL plugin may suit you better (see *Slim products or the NISAR
GDAL plugin?*).

The default selection of bands is tuned for the GrIMP ice-velocity workflow and is listed by
`--help` and in *Choosing bands and precision*. Within GrIMP it replaces the whole-product
`aria2c` fetch, and the nightly `autoupdateNISAR` uses it for RUNW/ROFF.

## Telling it what to download

The input is **just a text file of HTTPS links to NISAR granules, one per line** (`--urls
FILE`). Nothing else is needed; the granule name is taken from the end of each link and blank
lines are ignored:

```
https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GUNW_PROVISIONAL_V1/NISAR_L2_PR_GUNW_023_157_A_143_..._001/NISAR_L2_PR_GUNW_023_157_A_143_..._001.h5
https://nisar.asf.earthdatacloud.nasa.gov/NISAR/NISAR_L2_GUNW_PROVISIONAL_V1/...
```

The list can come from anywhere: `searchASF` (below), ASF's Vertex search (download its URL
list), `earthaccess` or a CMR query, or written by hand. The links must be the HTTPS ones -- the
tool reads byte ranges through ASF's HTTPS endpoint with your Earthdata login from `~/.netrc` --
not `s3://` URIs.

Each run handles **one product type**, named with `--product` (default GCOV), so give it a list
of that product only. `searchASF` writes one list per product type, which is the easiest way to
get them:

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
cubes uncompressed; they are gzipped losslessly); GCOV float32 unless `--float16`; paged layout.

| Product | Granule (track_frame, mode) | Archive | Slim | Smaller by | Bands kept | Mantissa bits |
|---|---|---|---|---|---|---|
| GCOV | 030_001_A_136, 40+5 MHz | 4572 MB | 1078 MB + 575 MB shared factor (first cycle only) | 4.2x (2.8x first cycle) | `HHHH`, `mask`, `rtcGammaToSigmaFactor` (shared file), radarGrid `incidenceAngle`, all metadata | 10 on `HHHH` and the factor |
| GCOV `--float16` | as above | 4572 MB | 887 MB + 552 MB shared factor (first cycle only) | 5.2x (3.2x first cycle) | as above | float16 (10 bits, 5-bit exponent, clamped to [1e-4, 65504]) |
| ROFF | 025_100_D_128, 40 MHz | 1065 MB | 233 MB | 4.6x | `slantRangeOffset`, `alongTrackOffset`, `correlationSurfacePeak` (layers 1-3), `mask`, geolocation cubes, all metadata | 10 on the three offset bands; float64 cubes 30 |
| ROFF | 018_084_D_051, 77 MHz | 495 MB | 124 MB | 4.0x | as above | as above |
| RUNW | 025_100_D_128, 40 MHz | 375 MB | 105 MB | 3.6x | `unwrappedPhase`, `coherenceMagnitude`, `ionospherePhaseScreen`, `connectedComponents`, `mask`, pixelOffsets (3 bands), geolocation cubes except `coordinateX/Y`, all metadata | 10 on float32 rasters, `unwrappedPhase` 12; float64 cubes 30 |
| GOFF | 023_157_A_143, 40 MHz | 929 MB | 329 MB | 2.8x | `slantRangeOffset`, `alongTrackOffset`, `correlationSurfacePeak` (layers 1-3), `mask`, radarGrid cubes, all metadata | 10 on the three offset bands; float64 cubes 30 |
| GUNW | 023_157_A_143, 40 MHz | 1690 MB | 371 MB | 4.6x | unwrappedInterferogram (`unwrappedPhase`, `coherenceMagnitude`, `ionospherePhaseScreen`, `connectedComponents`, `mask`), pixelOffsets (3 bands), radarGrid cubes, all metadata | 10 on float32 rasters, `unwrappedPhase` 12; float64 geometry cubes 30, correction cubes 23 |

The GCOV factor is written once per track/frame/grid and reused by every later cycle, so the
steady-state GCOV cost is the granule alone (1078 MB, or 887 MB with `--float16`). The paged
layout adds 2-4% to every file. "Bands kept" lists the rasters; everything not dropped is
kept (`--help` lists what is dropped). What remains in GOFF/GUNW is mostly the radarGrid cubes
(20 heights x 817 x 818 on a 40 MHz granule, against 20 x 585 x 164 for ROFF/RUNW): the float32
cubes, kept exactly, are 125 MB of each once the baselines are gzipped.

## Rounding errors at default settings

The precision is adjustable: each bit kept halves the error and costs a little more space.
`--mantissaBits` sets the float32 rasters (10 by default, up to 23), `--phaseBits` the unwrapped
phase (12), `--geometryBits` and `--correctionBits` the float64 cubes (30 and 23, up to 52), and
`--float32` turns rounding off altogether. For example, `--mantissaBits 12` gives a quarter of the
errors below, `--mantissaBits 8` four times them. The table is for the defaults.

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
| GCOV | `HHHH`, `rtcGammaToSigmaFactor` | 10 | linear power | 0.002 dB (relative 4.9e-4) | -- |
| GCOV `--float16` | `HHHH`, `rtcGammaToSigmaFactor` | float16 | linear power; values below 1e-4 (-40 dB) set to 1e-4, above 65504 to 65504 | 0.002 dB above the floor | -- |

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

# GCOV for the GrIMP mosaics: float16, as geomosaic reads (factors already written stay reused)
downloadNISARoptimized --urls gcovUrls.c030 --outputDir GCOV --factorDir GCOV/factors --float16

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

# only a box (lon/lat) of each GUNW, written as <granule>.subset.h5
downloadNISARoptimized --product GUNW --urls found.GUNW --outputDir GUNWbox --box 51.0 -69.9 52.2 -69.4

# full precision, unused bands still dropped
downloadNISARoptimized --product RUNW --urls found.RUNW --outputDir RUNW --float32

# slim granules already on disk (originals untouched; --outputDir must be elsewhere)
ls /Volumes/insar4/ian/Data/NISAR/Antarctica40/ROFF/*.h5 > roffPaths
downloadNISARoptimized --product ROFF --local --urls roffPaths --outputDir ROFFslim
```

## Slim products or the NISAR GDAL plugin?

JPL's [NISAR GDAL plugin](https://github.com/ozzp-jpl/nisar-hdf5-gdalplugin)
(`gdal-driver-nisar`, driver name `NISAR`) is a read-only GDAL driver for NISAR HDF5 products. It
reads the *archive* products where they are -- locally or straight from S3/HTTP with chunk-aligned
coalesced range requests -- and exposes every raster to `gdalinfo`, `gdal_translate`, `gdalwarp`,
QGIS and `rasterio`, with geotransforms for L2/L3, GCPs for L1, validity masks, virtual overviews
and DEM-based interpolation of the metadata cubes. It is not part of this package: it is a conda
package pinned to the GDAL minor version it was built for (3.12.x, with HDF5 2.x), which is a
requirement we do not want to place on our code. The two tools answer different questions, and
can be combined.

| Use the plugin when | Use the slim products when |
|---|---|
| You want to look at products interactively without keeping copies (QGIS, a tile server) | You keep a local archive and want it 3-5x smaller |
| You want a different window each time, read straight from S3/HTTP | You come back to the same data: fetch it once, then read it locally -- whole frames, or with `--box` just your area of each GCOV/GOFF/GUNW |
| You need part of a radar-geometry product (RUNW, ROFF): `--box` covers only the geocoded GCOV/GOFF/GUNW (for RSLC, see `downloadRSLCSubset`) | You need whole RUNW/ROFF frames, or areas of geocoded products |
| You work in GDAL tools and can use a dedicated conda env with GDAL 3.12 | You need any HDF5 reader -- h5py, `nisarhdf`, GDAL's own `HDF5` driver, C -- on whatever GDAL/HDF5 you have |
| You want L1 GCPs, cube interpolation with a DEM, or derived bands (amplitude/phase) | You want to choose, per product, which bands to keep and at what precision |

**Do slim files work with the plugin? Yes**, with the exceptions in the table. Tested 2026-10-30
with the plugin installed on petermann (GDAL 3.10.3 / HDF5 1.14.3, older than the plugin's README
asks for):

| Slim file | Reads through the plugin? |
|---|---|
| GCOV (default, float32) | **Yes**, georeferenced |
| GCOV with `--float16` | **No** on GDAL 3.10 (`Unsupported HDF5 data type`); GDAL 3.11+ has a float16 type, so it may read with the plugin's GDAL 3.12 -- untested. Use the default float32 if you need the plugin. |
| GOFF, GUNW | **Yes**, georeferenced |
| GCOV, GOFF, GUNW subsets (`--box`) | **Yes**, georeferenced |
| ROFF | **Yes**, the data read (radar geometry) |
| RUNW | **Data yes, georeferencing no**: the plugin builds RUNW's georeferencing from the `coordinateX/Y` cubes, which slim RUNWs drop. Add `--addBands coordinateX coordinateY` to keep them. (On this GDAL the plugin also reported an error georeferencing the *archive* RUNW, so this may need its GDAL 3.12 anyway.) |

Things to know:

- **Slim files made before 2026-10-30 do not open** (`H5Fopen failed ... Ensure metadata fits in
  8MiB`). The plugin needs the paged HDF5 layout the archive products use, which the tool has
  written since then. `h5repack -S PAGE -P 1 -G 2097152 old.h5 new.h5` converts an old one.
- **The polarization lists match what a slim file holds.** The tool rewrites
  `listOfCovarianceTerms` (GCOV) and `listOfPolarizations` to the term or polarization it kept,
  so the plugin's `POL` check and `nisarhdf` see only bands that exist. (Slim files made before
  2026-10-30 still list every term of a dual-pol granule.)
- **Installing the plugin changes how GDAL opens any NISAR `.h5`**, slim or not: it takes them
  over from GDAL's own `HDF5` driver. Code that relies on the `HDF5` driver (and its flattened
  attribute names) must now ask for it explicitly -- prefix the path with `HDF5:`, or open with
  `allowed_drivers=["HDF5"]`. geomosaic had to do this (see its documentation).

## Box subsets (`--box`, GCOV/GOFF/GUNW)

`--box LONMIN LATMIN LONMAX LATMAX` (WGS84 degrees) fetches and keeps only the part of each
granule covering the box, written as `<granule>.subset.h5`; granules the box misses are
skipped. It combines with everything else (band choices, rounding, `--local`, `--check`).

- Each grid in the product -- the image grids, `metadata/radarGrid`, GCOV's coarse calibration
  grids -- is cropped by its own window, computed from its own coordinate axes and EPSG, so
  grids of different spacing stay consistent. `xCoordinates`/`yCoordinates` are cropped with
  them.
- Image-grid windows are widened to whole chunks (512 x 512), so the subset keeps the archive's
  chunk alignment; the metadata grids get one cell of margin. The subset is therefore somewhat
  larger than the box, more so in polar stereographic where a lon/lat box is a wedge.
- `identification/boundingPolygon` is replaced by the subset's footprint; root attributes
  `subsetOf`, `subsetBox` and `subsetWindows` record where it came from.
- GCOV's RTC factor is cropped and kept inside the subset (a cropped factor cannot be shared
  across cycles), so `factorFrom:` / `factorFrom=` is not needed for subsets.

Measured, one box of 1.2 deg x 0.5 deg (51.0-52.2 E, 69.9-69.4 S) on the 40 MHz GUNW/GOFF pair,
and 3 deg x 0.5 deg on the GCOV, against the full-frame slim files at the same defaults:

| Product | Subset | Full-frame slim | Fetched |
|---|---|---|---|
| GUNW | 23 MB (1024 x 1536 px) | 371 MB | 0.45 GB of 1.69 GB |
| GOFF | 36 MB (1024 x 1536 px) | 329 MB | -- (`--local`) |
| GCOV | 101 MB (6656 x 5120 px) | 1078 MB | -- (`--local`) |

Every dataset in the subsets is identical to the same window of the full-frame slim file, and
both `nisarhdf` and the NISAR GDAL plugin open them, georeferenced. The transfer is dominated by
the metadata cubes: the archive chunks them 512 x 512 on a grid of about 820 x 820, so any box
touches roughly 40% of each cube at every height -- about 300 MB on a GUNW however small the box.
Dropping cubes you do not need (`--dropBands wetTroposphericPhaseScreen ...`) is what reduces it.

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
--float16            GCOV only: HHHH and the RTC factor as float16 (what the GrIMP
                     mosaics use); default float32 rounded to --mantissaBits
--float32            no rounding at all
```

GCOV follows `--mantissaBits` unless `--float16` is given. float16 halves the band again but
clamps values to [1e-4, 65504] (-40 to +48 dB), needs a GDAL with a float16 type (3.11+) to read
through GDAL, and is not read by the NISAR GDAL plugin on GDAL 3.10. A shared factor's file
name records its precision (`_m10`, `_f32`; float16 keeps the untagged name), so granules of
different precision never share one. The float32 metadata cubes (incidence and elevation
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
| GCOV | `numberOfLooks`, `inputDataExceptionMask`, the squinted radarGrid cubes except `incidenceAngle` | `HHHH` and `rtcGammaToSigmaFactor` (factor shared), float16 with `--float16` |
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
   `nisarhdf` read it unchanged. The two big float bands are rounded to
   `--mantissaBits` (float32), or with `--float16` become float16 (values outside
   [1e-4, 65504] are clamped, and the counts are recorded).
4. Stores `rtcGammaToSigmaFactor` once per track/direction/frame/mode/grid in
   `--factorDir`, and leaves a symlink there named like the granule; later cycles
   of the same position reuse it instead of fetching it again.
5. Verifies the slim file (required datasets present; for float16, no
   underflow/inf inside the valid mask; factor grid matches) and appends a line
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

Readers see a consistent file:

- `listOfCovarianceTerms` and `listOfPolarizations` are rewritten to the kept term (and the
  polarizations it uses, `HVHV` -> `HV`), so readers that trust the lists see only what is there.
  Slim GCOVs made before 2026-10-30 still list every term.
- `nisarhdf.nisarGCOVHDF` loads, by default, the listed terms, `mask`, and `numberOfLooks` and
  `rtcGammaToSigmaFactor` when the granule holds them. For sigma0 from a slim GCOV whose factor
  is shared, pass the factor directory: `openHDF(file, sigma0=True, factorFrom=<--factorDir>)`
  (a `--box` subset carries its own factor, so it is not needed there).

## Local conversion (`--local`)

With `--local`, `--urls` lists **local** granule paths, which are converted with
the same repack as the fetch path (no network). Originals are never modified or
removed, and the run refuses to start if `--outputDir` is where they live.
Granules already slim (carrying `grimpSlim`; GCOVs made before it, in float16; ROFF/RUNW made
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
  --box LON0 LAT0 LON1 LAT1
                       GCOV/GOFF/GUNW: keep only this lon/lat box (<granule>.subset.h5)
  --float16            GCOV only: float16 HHHH and factor (GrIMP mosaics)
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
