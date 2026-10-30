# searchASF.py

Find the NISAR and Sentinel-1 products over your area and dates, and get back exactly what you
need to download them: plain lists of HTTPS links, one granule per line, split by product type.

### Why use it

Searching the ASF DAAC by hand (Vertex, or `asf_search` in a notebook) is fine for a few
granules. Keeping a regional archive up to date is not: the same area is searched again and
again, most results are already on disk, some are reprocessed versions of granules you hold, and
the restricted NISAR collections need a separate, authenticated query. searchASF does all of
that in one command:

- **Only what you don't have.** Point `--archiveDir` at your archive and the results are split
  into granules to fetch, granules already held, and granules ASF has since reprocessed to a
  newer version -- so a nightly search downloads only what is new.
- **One clean answer per acquisition.** When ASF offers the same acquisition more than once
  (reprocessed NISAR products with a higher CRID or product counter, Sentinel-1 scenes
  processed twice), only the newest is listed.
- **Ready for the downloaders.** The lists feed straight into `ariaDownload` (whole products)
  or `downloadNISARoptimized` (slim products), one list per product type. They are just links,
  so any other downloader works too.
- **Everything ASF holds, including what a plain search misses.** It also queries the
  restricted NISAR early-access collections (with science-team Earthdata credentials) and
  Sentinel-1D, which `asf_search` does not yet reach on its own, and merges them in.
- **Complete results.** ASF caps a query at 10,000 results and silently drops the rest; long or
  dense searches are split into smaller date windows automatically until each fits.
- **Your area, in the format you have.** GeoJSON, a shapefile in any projection, or a simple
  lon/lat list; built-in Greenland and Antarctica areas. Polygon orientation and closing are
  handled for you; the few real limits (vertex count, crossing 180 degrees) are in *Search area
  formats*.
- **Filters that matter for NISAR.** Range bandwidth (5-77 MHz modes), processing version,
  tracks and frames, and per-track frame exclusions.
- **See before you download.** It prints the number of granules and total volume per product,
  and with `--gpkg` writes every footprint to a GeoPackage for QGIS, with a `status` field (new,
  already held, or updated) to colour or filter by.

---

## Usage

```
searchASF [options] firstDate lastDate output
```

| Argument | Description |
|----------|-------------|
| `firstDate` | Search start date (`YYYY-MM-DD`) |
| `lastDate` | Search end date (`YYYY-MM-DD`) |
| `output` | Output file for new download URLs |

---

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--sensor NISAR\|SENTINEL1` | `NISAR` | Platform to search. |
| `--products P [P ...]` | NISAR: `RUNW ROFF RSLC`; S1: `SLC` | Product type(s). See epilog for full lists. |
| `--bandwidth BW [BW ...]` | `40 40+5 77` | Range bandwidth(s) in MHz — NISAR only. Choices: `5 5+5 20 20+5 40 40+5 77`. |
| `--beamMode MODE [MODE ...]` | `IW` | Beam mode(s) — Sentinel-1 only. |
| `--minVersion N` | 0 (no filter) | Skip NISAR granules whose CRID number is below N (e.g. `--minVersion 5010`). |
| `--specificVersion N [N ...]` | None | Accept only granules whose CRID number matches one of the listed values. |
| `--startTrack N` | 0 | Minimum relative orbit / track number. |
| `--endTrack N` | 10000 | Maximum relative orbit / track number. |
| `--startFrame N` | 0 | Minimum frame number. |
| `--endFrame N` | 10000 | Maximum frame number. |
| `--searchArea FILE` | bundled `Greenland.lonlat` | Search polygon file (see **Search area formats** below). |
| `--greenland` | — | Search over Greenland using the bundled `Greenland.lonlat` polygon (equivalent to the default `--searchArea`). |
| `--antarctica` | — | Search over Antarctica (lon −180:180, lat −90:−60) using a full-longitude bounding box. Mutually exclusive with `--greenland`. |
| `--archiveDir PATTERN` | None | Where the already-downloaded files are: a path with wildcards (`*` any characters, `?` one character), in quotes, e.g. `'/data/NISAR/*/*'`. A pattern matching a directory covers only the files directly inside it, so for one folder per product use `'./*/*'` rather than `'.'`; lists in braces (`{RUNW,ROFF}`) are not supported. Granules found there are skipped; for NISAR, newer versions of them are written to `output.updated`. |
| `--gpkg FILE` | None | Write granule footprints to a GeoPackage (one QGIS layer per product type). |
| `--s3` | off | Output S3 URIs (`s3://…`) instead of HTTPS URLs. Granules with no available S3 link are silently skipped. See **S3 access** below. |

---

## Output files

searchASF's main output is plain text lists of download URLs, one granule per line, ready for
`ariaDownload` or `downloadNISARoptimized`. For an `output` argument of `found`:

| File | Contents |
|------|----------|
| `found` | URLs of the granules found that are not already in `--archiveDir` (all products) |
| `found.<PRODUCT>` | the same, split per product type (e.g. `found.RUNW`, `found.GUNW`) |
| `found.exists` | URLs of granules already present in `--archiveDir` |
| `found.updated` | NISAR granules already archived for which ASF now holds a newer processing version (higher CRID or product counter) |
| `found.meta` | with `--metadata`: one line per granule -- name, track, frame, status, size, URL -- including those already archived |
| the `--gpkg FILE` GeoPackage | with `--gpkg`: footprints of every granule for QGIS, one layer per product type, with a `status` field (`found`, `exists`, `updated`) |

All lists contain HTTPS URLs by default, or S3 URIs when `--s3` is given. The run also prints
the number of granules and the total download volume per product type.

------|----------|
| `output` | Download URLs/URIs for new (not-yet-archived) granules, one per line |
| `output.<PRODUCT>` | Per-product subset of `output` (e.g. `output.RUNW`, `output.ROFF`) |
| `output.exists` | URLs/URIs for granules already present in `--archiveDir` |
| `output.updated` | NISAR URLs/URIs where a newer processor version exists in the archive |
| `output.gpkg` | GeoPackage footprints (when `--gpkg` is given) |

All output files contain HTTPS URLs by default, or S3 URIs when `--s3` is given.

---

## Search area formats

`--searchArea` takes a polygon file; `--greenland` and `--antarctica` are built-in areas. A
granule is returned when its footprint **intersects** the area (it need not lie inside it).

| Extension | Format | What is used |
|-----------|--------|--------------|
| `.geojson` / `.json` | GeoJSON `FeatureCollection`, `Feature`, or bare `Polygon` / `MultiPolygon` geometry | the **first** feature only, and of a `MultiPolygon` only its **first** polygon |
| `.shp` | ESRI Shapefile (requires GDAL) | the first feature, reprojected to WGS84 lon/lat if its CRS differs; a multi-part shape is replaced by its convex hull |
| anything else (e.g. `.lonlat`) | flat coordinate file (GrIMP style) | every number in the file, read as coordinate pairs (see below) |

Built-in areas:

| Flag | Area |
|------|------|
| `--greenland` (also the default) | the bundled `asfsearchdownload/searchRegions/Greenland.lonlat` outline of Greenland (25 vertices) |
| `--antarctica` | everything south of 60 S: a bounding box, lon -180 to 180, lat -90 to -60 |

### Rules for the coordinates

- **Geographic lon/lat in degrees (WGS84).** GeoJSON is lon, lat by definition; shapefiles in
  another CRS are reprojected. Heights (a third coordinate, or a 3-D shapefile) are ignored.
- **Orientation does not matter.** CMR, which answers both searches, rejects a clockwise polygon
  (`The polygon boundary points are listed in the wrong order. Points must be provided in
  counter-clockwise order.`), so the polygon is put in counter-clockwise order before it is
  sent: by asf_search for the public collections and by searchASF itself for the restricted
  NISAR_EA collections.
- **The ring need not be closed.** The first point is repeated at the end if it is missing.
- **Only the outer ring counts.** Holes in a GeoJSON or shapefile polygon are ignored.
- **Keep it under 200 vertices.** asf_search simplifies the public search polygon to at most 300
  points by itself, but the NISAR_EA search falls back to a bounding box above 200 vertices (see
  below), which searches a much larger area.
- **Do not cross the antimeridian (180 deg) with one polygon.** A polygon whose longitudes span
  more than 180 deg is, for the NISAR_EA search, replaced by a full-longitude band reaching to
  the nearer pole (`Note: using bounding_box fallback ...` is printed). That is the intended
  behaviour for circumpolar areas like `--antarctica`, but for a region straddling 180 deg it
  is far larger than asked for. Split such a region into two searches instead.

### The flat coordinate file

Numbers may be separated by commas, spaces or line breaks, and a line may start with a bare
integer index, which is skipped; lines starting with `#` are comments. At least three points
are needed. Example (the bundled Greenland outline, one line):

```
-53.26,64.81,-48.1,60.33,-42.58,59.93,-40.94,63.63,...,-53.26,64.81
```

**Lon/lat order is guessed from the first number**: if it is larger than 90 in magnitude, or
less than -20, the pairs are read as lon, lat (GrIMP's convention); otherwise as lat, lon. The
guess is right for Greenland and Alaska written lon-first (their longitudes are below -20),
but **wrong for any lon, lat file whose first longitude is between -20 and 90** -- Svalbard,
Scandinavia, the Alps, High Mountain Asia west of 90 E, or an Antarctic outline that happens to
start between 20 W and 90 E -- which would be read with lon and lat swapped. Use GeoJSON for such regions, or start the file at a vertex whose longitude is
outside that range.

-----------|--------|
| `.geojson` / `.json` | GeoJSON — `FeatureCollection`, `Feature`, or bare `Polygon`/`MultiPolygon` geometry |
| `.shp` | ESRI Shapefile — first feature's polygon, reprojected to WGS84 if needed (requires GDAL) |
| anything else (e.g. `.lonlat`) | GrIMP flat coordinate file — lon,lat pairs separated by commas/whitespace on one or more lines |

The default search area is the bundled `Greenland.lonlat` polygon, covering the Greenland coastline.

---

## Authentication

The tool authenticates automatically using credentials from `~/.netrc` (entry for `urs.earthdata.nasa.gov`).  If [earthaccess](https://earthaccess.readthedocs.io/) is installed it is used in preference — earthaccess performs the full EDL OAuth2 flow and can access restricted NISAR science-team collections (`NISAR_EA_L1`, `NISAR_EA_L2`) that a plain `asf_search` credential cannot reach.

---

## Examples

With no `--searchArea`, `--greenland` or `--antarctica`, the search covers **Greenland** (the
bundled outline), so give an area explicitly unless Greenland is what you want. The examples
below always do.

Search for NISAR RUNW + ROFF over Greenland, 2025:
```
searchASF 2025-01-01 2025-12-31 nisar_2025.txt --greenland --products RUNW ROFF
```

Search with a custom GeoJSON region:
```
searchASF 2025-01-01 2025-12-31 nisar_jakobshavn.txt \
    --searchArea jakobshavn.geojson --products RUNW
```

Search with a shapefile boundary and export footprints:
```
searchASF 2025-01-01 2025-12-31 nisar_basin.txt \
    --searchArea basin.shp --gpkg nisar_basin.gpkg
```

Skip granules already in an archive directory:
```
searchASF 2025-01-01 2025-12-31 new_downloads.txt --greenland \
    --archiveDir '/data/NISAR/greenland/*'
```

Search Sentinel-1 IW SLC over Greenland:
```
searchASF 2025-01-01 2025-12-31 s1_2025.txt --greenland \
    --sensor SENTINEL1 --products SLC --beamMode IW
```

Search for NISAR over Antarctica:
```
searchASF 2025-01-01 2025-12-31 nisar_ant.txt \
    --antarctica --products RUNW ROFF
```

Retrieve S3 URIs instead of HTTPS URLs:
```
searchASF 2025-01-01 2025-12-31 nisar_s3.txt --greenland \
    --products RUNW ROFF --s3
```

---

## S3 access

When `--s3` is given, output files contain `s3://` URIs instead of HTTPS URLs. These URIs point to the same granules in the ASF DAAC cloud archive (AWS `us-west-2`).

**Authentication**: S3 access requires temporary AWS credentials obtained from Earthdata Login. Use [earthaccess](https://earthaccess.readthedocs.io/) to retrieve them:

```python
import earthaccess, boto3

earthaccess.login(strategy='netrc', persist=False)
creds = earthaccess.get_s3_credentials(daac='ASF')

s3 = boto3.client('s3',
    aws_access_key_id=creds['accessKeyId'],
    aws_secret_access_key=creds['secretAccessKey'],
    aws_session_token=creds['sessionToken'],
    region_name='us-west-2',
)
bucket, key = uri[5:].split('/', 1)   # strip s3://
s3.download_file(bucket, key, local_path)
```

Credentials expire after 1 hour; refresh them for long batch downloads.

**When S3 is faster**: S3 access is significantly faster than HTTPS only when running on AWS EC2 in `us-west-2` (same region as the DAAC buckets). From a non-AWS HPC or workstation the data traverses the internet either way, and aria2c over HTTPS is typically comparable in speed.

---

## Dependencies

- `asf-search` — ASF DAAC search API
- `shapely` — parses and orients the search polygon (installed with `asf-search`)
- `osgeo` (GDAL) — required for `--gpkg` output and shapefile `--searchArea`
- `earthaccess` *(optional)* — enables access to restricted NISAR_EA collections
- `requests` *(optional)* — used internally when `earthaccess` is available
