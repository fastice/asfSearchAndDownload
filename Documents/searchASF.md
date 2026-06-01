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
| `--archiveDir GLOB` | None | Glob pattern for already-downloaded files. Matching granules are skipped; for NISAR, newer-version URLs are written to `output.updated`. |
| `--gpkg FILE` | None | Write granule footprints to a GeoPackage (one QGIS layer per product type). |

---

## Output files

| File | Contents |
|------|----------|
| `output` | HTTPS URLs for new (not-yet-archived) granules, one per line |
| `output.<PRODUCT>` | Per-product subset of `output` (e.g. `output.RUNW`, `output.ROFF`) |
| `output.exists` | URLs for granules already present in `--archiveDir` |
| `output.updated` | NISAR URLs where a newer processor version exists in the archive |
| `output.gpkg` | GeoPackage footprints (when `--gpkg` is given) |

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

---

## Dependencies

- `asf-search` — ASF DAAC search API
- `osgeo` (GDAL) — required for `--gpkg` output and shapefile `--searchArea`
- `earthaccess` *(optional)* — enables access to restricted NISAR_EA collections
- `requests` *(optional)* — used internally when `earthaccess` is available
