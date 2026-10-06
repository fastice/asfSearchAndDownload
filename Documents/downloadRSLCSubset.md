# downloadRSLCSubset

Fetch a **spatial subset of a NISAR RSLC** as a small, self-consistent RSLC
HDF5. Part of the `asfSearchAndDownload` package; adapted from
[`downloadNISARoptimized`](downloadNISARoptimized.md) and sharing its fetch
machinery.

## What it does

A NISAR RSLC is ~27 GB, and a glacier is typically a few percent of it. This
fetches only the HDF5 chunks under a window of one polarization band, over
parallel HTTP range requests, and writes `<outputDir>/<granule>.subset.h5` with
the **same internal paths**. The band, `slantRange`, `zeroDopplerTime`,
`inputDataExceptionMask` and `validSamplesSubSwath*` are all cropped to the
window, so any reader that derives geometry from the product (e.g. `nisarhdf`'s
RSLC class, or `multilookrslc --geojsonOnly` making a geodat) sees a small image
whose geometry is right by construction — nothing is hand-edited.

- The window is snapped **out** to whole HDF5 chunks. A gzip chunk cannot be
  partly decoded, so this costs no extra bytes, and it lets each chunk be copied
  raw — the band is bit-identical to the archive.
- `slantRange` and `zeroDopplerTime` are pixel centres, as in GrIMP, so the
  subset's first range and time come straight from the parent arrays with no
  half-pixel adjustment.
- The RFI metadata (152 MB, unused by GrIMP) is never transferred.
- After writing, verification checks that the band shape matches the cropped
  axes, that the axes match the parent's, and that two probe chunks of the band are
  bit-identical to the archive. A failure renames the output
  `.subset.h5.failedVerify`.

Measured on Taku (track 135 frame 31, 40 MHz DHDH): 0.34 GB of a 27.2 GB granule.

## Selecting the window

Exactly one of:

- `--outline FILE` — a polygon (gpkg/shp/geojson). The window covers it,
  located with **each granule's own** geolocation grid, buffered by
  `--outlineBuffer` metres and tested across the `--outlineHeights` range (range
  shifts with terrain height).
- `--physical R0 R1 T0 T1` — slant range in metres and zeroDopplerTime in seconds
  of day.
- `--pixels C0 C1 L0 L1` — range columns and azimuth lines, inclusive.

`--pad` adds single-look pixels on every side before snapping; it must cover
half the chip plus half the search of the tracker that will use the subset.

## CLI

```
downloadRSLCSubset [URL ...] --outputDir DIR
        (--outline FILE | --physical R0 R1 T0 T1 | --pixels C0 C1 L0 L1) [options]

  URL ...                  RSLC URLs
  --urlFile FILE           File of URLs; the first https token on each line is
                           used, so searchASF .urls and .meta files both work
  --granules SUB [SUB ...] Restrict --urlFile to URLs containing these substrings
  --outputDir DIR          Where the subset .h5 files go (required)
  --tmpDir DIR             Scratch for sparse files [outputDir]
  --outlineBuffer M        Buffer around --outline, metres [1000]
  --outlineHeights HMIN HMAX  Terrain heights spanned by the outline [-100 3500]
  --pad N                  Extra single-look pixels on every side [256]
  --polarization POL       Polarization to keep; 'like' picks HH or VV [like]
  --frequency F            Frequency to keep [A]
  --connections N          Parallel requests [16]
  --limit MBPS             Bandwidth cap MB/s, 0 = none [0]
  --check                  Plan and report, do not fetch
  --keepSparse             Keep the sparse scratch file
  --noVerify               Skip the verification gates
```

Example:

```
downloadRSLCSubset --urlFile takuRSLC.urls.meta \
                   --outputDir RSLCsubset --outline taku.gpkg
```

Existing outputs are skipped; a granule the window does not intersect is
reported and skipped. Earthdata credentials come from `~/.netrc`.

Exit status: 0 on success, 68 if a fetch failed (or no RSLC URLs were given),
69 if verification failed.
