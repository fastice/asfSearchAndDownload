# searchASF.py

Searches ASF DAAC for NISAR and Sentinel-1 SAR products within a date range and spatial area. Writes download URLs to text files (one per product type) and optionally exports granule footprints to a GeoPackage for visualisation in QGIS.

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
| `--archiveDir GLOB` | None | Glob pattern for already-downloaded files. Matching granules are skipped; for NISAR, newer-version URLs are written to `output.updated`. |
| `--gpkg FILE` | None | Write granule footprints to a GeoPackage (one QGIS layer per product type). |
| `--s3` | off | Output S3 URIs (`s3://…`) instead of HTTPS URLs. Granules with no available S3 link are silently skipped. See **S3 access** below. |

---

## Output files

| File | Contents |
|------|----------|
| `output` | Download URLs/URIs for new (not-yet-archived) granules, one per line |
| `output.<PRODUCT>` | Per-product subset of `output` (e.g. `output.RUNW`, `output.ROFF`) |
| `output.exists` | URLs/URIs for granules already present in `--archiveDir` |
| `output.updated` | NISAR URLs/URIs where a newer processor version exists in the archive |
| `output.gpkg` | GeoPackage footprints (when `--gpkg` is given) |

All output files contain HTTPS URLs by default, or S3 URIs when `--s3` is given.

---

## Search area formats

`--searchArea` accepts three file formats, detected by extension:

| Extension | Format |
|-----------|--------|
| `.geojson` / `.json` | GeoJSON — `FeatureCollection`, `Feature`, or bare `Polygon`/`MultiPolygon` geometry |
| `.shp` | ESRI Shapefile — first feature's polygon, reprojected to WGS84 if needed (requires GDAL) |
| anything else (e.g. `.lonlat`) | GrIMP flat coordinate file — lon,lat pairs separated by commas/whitespace on one or more lines |

The default search area is the bundled `Greenland.lonlat` polygon, covering the Greenland coastline.

---

## Authentication

The tool authenticates automatically using credentials from `~/.netrc` (entry for `urs.earthdata.nasa.gov`).  If [earthaccess](https://earthaccess.readthedocs.io/) is installed it is used in preference — earthaccess performs the full EDL OAuth2 flow and can access restricted NISAR science-team collections (`NISAR_EA_L1`, `NISAR_EA_L2`) that a plain `asf_search` credential cannot reach.

---

## Examples

Search for NISAR RUNW + ROFF over Greenland, 2025:
```
searchASF 2025-01-01 2025-12-31 nisar_2025.txt --products RUNW ROFF
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
searchASF 2025-01-01 2025-12-31 new_downloads.txt \
    --archiveDir '/data/NISAR/greenland/*'
```

Search Sentinel-1 IW SLC over Greenland:
```
searchASF 2025-01-01 2025-12-31 s1_2025.txt \
    --sensor SENTINEL1 --products SLC --beamMode IW
```

Search for NISAR over Antarctica:
```
searchASF 2025-01-01 2025-12-31 nisar_ant.txt \
    --antarctica --products RUNW ROFF
```

Retrieve S3 URIs instead of HTTPS URLs:
```
searchASF 2025-01-01 2025-12-31 nisar_s3.txt \
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
- `osgeo` (GDAL) — required for `--gpkg` output and shapefile `--searchArea`
- `earthaccess` *(optional)* — enables access to restricted NISAR_EA collections
- `requests` *(optional)* — used internally when `earthaccess` is available
