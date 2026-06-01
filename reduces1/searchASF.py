#!/usr/bin/env python3
"""Search ASF DAAC for SAR products within a date range and spatial area.

Supports NISAR (default) and Sentinel-1 platforms.
"""

import argparse
import glob
import importlib.resources
import json
import os
import sys
import threading


# ---------------------------------------------------------------------------
# Sensor-specific defaults and allowed values
# ---------------------------------------------------------------------------

_SENSOR_PRODUCTS = {
    'NISAR': {
        'default': ['RUNW', 'ROFF', 'RSLC'],
        'choices': ['L0B', 'RSLC', 'RIFG', 'RUNW', 'ROFF',
                    'GSLC', 'GCOV', 'GUNW', 'GOFF', 'SME2'],
    },
    'SENTINEL1': {
        'default': ['SLC'],
        'choices': ['SLC', 'GRD_HD', 'GRD_MS', 'GRD_HS',
                    'GRD_FD', 'GRD_MD', 'OCN', 'RAW', 'BURST'],
    },
}

# NISAR range bandwidth options (MHz; "M+S" means L-band M MHz + S-band S MHz)
_NISAR_BANDWIDTHS = ['5', '5+5', '20', '20+5', '40', '40+5', '77']
_NISAR_BW_DEFAULT = ['40', '40+5', '77']

# Sentinel-1 beam modes
_S1_BEAM_MODES   = ['IW', 'EW', 'S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'WV']
_S1_BEAM_DEFAULT = ['IW']

# Sentinel for "use all" in track/frame filtering
_ALL_MAX = 10000

# NISAR pair products (interferometric — need two acquisitions; bandwidth filter applies).
# Single-acquisition products (RSLC, GSLC, GCOV, L0B, SME2) are NOT in this set and
# are searched without a bandwidth constraint.
_PAIR_PRODUCTS = frozenset({'RIFG', 'RUNW', 'ROFF', 'GUNW', 'GOFF'})

# Restricted NISAR Engineering Archival collections (require science-team auth).
# These are NOT returned by a standard platform=NISAR search; we query them
# separately via authenticated CMR when earthaccess is available.
_NISAR_EA_COLLECTIONS = [
    'C4052500045-ASF',  # NISAR_EA_L1  (RSLC, RIFG, RUNW, ROFF, …)
    'C4052499921-ASF',  # NISAR_EA_L2  (GCOV, GSLC, GUNW, GOFF, …)
]


def _wkt_to_cmr_polygon(wkt):
    """Convert 'POLYGON((lon lat, ...))' to CMR polygon param 'lon,lat,lon,lat,...'"""
    inner = wkt.replace('POLYGON((', '').rstrip(')')
    tokens = []
    for pair_str in inner.split(','):
        pair_str = pair_str.strip()
        if pair_str:
            lonlat = pair_str.split()
            if len(lonlat) == 2:
                tokens.extend(lonlat)
    return ','.join(tokens)


def _search_nisar_ea(jwt, wkt, start, end, products, bandwidth):
    """Search restricted NISAR_EA collections via authenticated CMR.

    Field layout of NISAR granule name (split on '_'):
      0:NISAR  1:L1  2:PR  3:<product>  4:<cycle>  5:<track>  6:<dir>
      7:<frame>  8:<subframe>  9:<BW*100>  10:<pol>  11..14:<datetimes>
      15..<proc>  19:<ver>

    Returns list of (url, track_int_or_None, frame_int_or_None).
    """
    import requests as _req

    headers    = {'Authorization': f'Bearer {jwt}'}
    product_set = set(products)

    # Map bandwidth strings → 4-digit granule field values (field 9)
    # e.g. '40' → '4000',  '40+5' → '4000' (L-band main only),  '77' → '7700'
    bw_codes = set()
    for bw in bandwidth:
        main = bw.split('+')[0]
        try:
            bw_codes.add(f'{int(float(main) * 100):04d}')
        except ValueError:
            pass

    cmr_polygon = _wkt_to_cmr_polygon(wkt)
    items = []

    for concept_id in _NISAR_EA_COLLECTIONS:
        page = 1
        while True:
            r = _req.get(
                'https://cmr.earthdata.nasa.gov/search/granules.json',
                params={
                    'concept_id': concept_id,
                    'page_size': 2000,
                    'page_num':  page,
                    'temporal':  f'{start}T00:00:00Z,{end}T23:59:59Z',
                    'polygon':   cmr_polygon,
                },
                headers=headers,
                timeout=120,
            )
            if r.status_code == 401:
                raise PermissionError(
                    f'CMR returned 401 for {concept_id} — '
                    'token missing or lacking access to this collection'
                )
            r.raise_for_status()
            entries = r.json().get('feed', {}).get('entry', [])
            if not entries:
                break

            for entry in entries:
                name  = entry.get('producer_granule_id') or entry.get('title', '')
                parts = name.split('_')

                # Filter by product type (field 3)
                if len(parts) > 3 and parts[3] not in product_set:
                    continue

                # Filter by bandwidth using smart field detection:
                # pair products have bandwidth at field 9; new-format single-acq
                # products (RSLC etc.) may have it at field 8.  _nisar_bw_field()
                # finds the right field for both layouts.
                if bw_codes:
                    bw_val = _nisar_bw_field(parts)
                    if bw_val is not None and bw_val not in bw_codes:
                        continue

                # Pick the HTTPS data link (.h5 file)
                url = None
                for lnk in entry.get('links', []):
                    rel  = lnk.get('rel', '')
                    href = lnk.get('href', '')
                    if ('fedsearch/1.1/data#' in rel
                            and href.startswith('https://')
                            and href.endswith('.h5')):
                        url = href
                        break
                if not url:
                    continue

                # Track = field 5, frame = field 7
                try:
                    track = int(parts[5])
                except (ValueError, IndexError):
                    track = None
                try:
                    frame = int(parts[7])
                except (ValueError, IndexError):
                    frame = None

                # granule_size is in MB in CMR
                try:
                    size_bytes = int(float(entry.get('granule_size', 0)) * 1_000_000)
                except (TypeError, ValueError):
                    size_bytes = 0

                items.append((url, track, frame, size_bytes))

            if len(entries) < 2000:
                break
            page += 1

    return items


# ---------------------------------------------------------------------------
# Terminal spinner
# ---------------------------------------------------------------------------

class _Spinner:
    _FRAMES = ['|', '/', '-', '\\']

    def __init__(self, msg, interval=0.12):
        self._msg      = msg
        self._interval = interval
        self._stop     = threading.Event()
        self._thread   = threading.Thread(target=self._spin, daemon=True)

    def _spin(self):
        idx = 0
        while not self._stop.wait(self._interval):
            frame = self._FRAMES[idx % len(self._FRAMES)]
            print(f'\r{self._msg} {frame}', end='', flush=True)
            idx += 1

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join()
        # Erase the spinner line
        print(f'\r{" " * (len(self._msg) + 2)}\r', end='', flush=True)


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

def _strip_ext(name):
    """Strip .h5, .zip.1, or .zip from a filename (stem only, no path)."""
    for ext in ('.zip.1', '.zip', '.h5'):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _nisar_bw_field(parts):
    """Return the bandwidth code string from a split NISAR granule name, or None.

    Pair products (20 fields): bandwidth is at index 9  (e.g. '4000').
    Old-format single-acq (18 fields): bandwidth is at index 9  (e.g. '4000').
    New-format single-acq (18 fields): bandwidth is at index 8  (e.g. '7700');
      index 9 contains a combined pol token like 'SHNA'.

    Strategy: a bandwidth code is a 4-character all-digit string.  Subframe
    counts are 2–3 digits; polarisation tokens contain letters.  Check index 8
    first so the new-format RSLC is detected before falling through to index 9.
    """
    for idx in (8, 9):
        if len(parts) > idx:
            v = parts[idx]
            if len(v) == 4 and v.isdigit():
                return v
    return None


def _nisar_parse(stem):
    """Parse a NISAR granule stem into (scene_key, version_int, crid_num).

    Handles two formats:

    Pair products — 20 fields (RIFG / RUNW / ROFF / GUNW / GOFF):
      NISAR_L1_PR_RUNW_<cyc>_<trk>_<dir>_<frm>_<sub>_<bw>_<pol>_
        <refStart>_<refStop>_<secStart>_<secStop>_<crid>_N_F_J_<ver>
      scene_key = fields 0–14   crid = field 15   version = field 19

    Single-acquisition — 18 fields (RSLC / GSLC / GCOV / L0B):
      NISAR_L1_PR_RSLC_<cyc>_<trk>_<dir>_<frm>_<bw>_<pol>_
        <acqStart>_<acqStop>_<crid>_N_P_J_<ver>
      scene_key = fields 0–12   crid = field 13   version = field 17

    Returns (None, None, None) if the stem is not a recognised NISAR granule.
    """
    parts = stem.split('_')
    if not parts or parts[0] != 'NISAR':
        return None, None, None
    n = len(parts)
    if n == 20:
        # Pair product (RUNW/ROFF/RIFG/GUNW/GOFF): two date pairs
        # ..._sub_bw_pol_refStart_refStop_secStart_secStop_crid_N_F_J_ver
        scene_key  = '_'.join(parts[:15])
        crid_field = 15
        ver_field  = 19
    elif n == 19:
        # Single-acq with subframe + dual-pol token (e.g. P05006-era RSLC):
        # ..._frame_sub_bw_pol1_pol2_acqStart_acqStop_crid_N_P_J_ver
        scene_key  = '_'.join(parts[:14])
        crid_field = 14
        ver_field  = 18
    elif n == 18:
        # Single-acq, two sub-variants share the same field offsets:
        #   old format: ..._frame_sub_bw_pol_acqStart_acqStop_crid_N_P_J_ver
        #   new format: ..._frame_bw_pol1_pol2_acqStart_acqStop_crid_N_P_J_ver
        # Either way crid is at 13 and ver at 17.
        scene_key  = '_'.join(parts[:13])
        crid_field = 13
        ver_field  = 17
    else:
        return None, None, None
    try:
        crid_num = int(parts[crid_field][1:])   # 'X05010' -> 5010
    except (ValueError, IndexError):
        crid_num = -1
    try:
        version = int(parts[ver_field])
    except (ValueError, IndexError):
        version = -1
    return scene_key, version, crid_num


def build_archive_index(archive_glob, sensor):
    """Glob archive_glob and return a sensor-appropriate index.

    Sentinel-1: returns (set_of_stems, {})
    NISAR:      returns ({}, {scene_key: (stem, version_int, full_path)})

    If the glob matches directories (e.g. '/archive/*/') their immediate
    contents are expanded automatically so file-level matching works
    regardless of whether the glob points at files or at containing dirs.
    """
    s1_index    = set()
    nisar_index = {}

    # Collect file paths — expand any directory hits one level
    file_paths = []
    for p in glob.glob(archive_glob):
        if os.path.isdir(p):
            file_paths.extend(
                q for q in glob.glob(os.path.join(p, '*'))
                if not os.path.isdir(q)
            )
        else:
            file_paths.append(p)

    for path in file_paths:
        basename = os.path.basename(path)
        stem = _strip_ext(basename)
        if not stem or stem == basename:
            # no recognised extension stripped — not a granule file we care about
            continue
        if sensor == 'SENTINEL1':
            s1_index.add(stem)
        else:
            scene_key, version, _ = _nisar_parse(stem)
            if scene_key is None:
                continue
            # Keep the highest version seen for each scene
            existing = nisar_index.get(scene_key)
            if existing is None or version > existing[1]:
                nisar_index[scene_key] = (stem, version, path)

    n = len(s1_index) if sensor == 'SENTINEL1' else len(nisar_index)
    print(f'Archive: {n} {sensor} granule(s) indexed from {archive_glob}')
    return s1_index, nisar_index


def _default_search_area():
    """Return path to the bundled Greenland search polygon.

    Uses importlib.resources.files() (Python >=3.9) with a __file__-based
    fallback for Python 3.8.
    """
    try:
        ref = importlib.resources.files('reduces1.searchRegions').joinpath(
            'Greenland.lonlat'
        )
        return str(ref)
    except AttributeError:
        # Python 3.8: files() not available; fall back to __file__
        return os.path.join(os.path.dirname(__file__), 'searchRegions', 'Greenland.lonlat')
    except Exception:
        return None


def _read_polygon_geojson(filepath):
    """Read the first polygon from a GeoJSON file and return WKT.

    Handles FeatureCollection, Feature, and bare Geometry objects.
    For MultiPolygon the first ring of the first polygon is used.
    """
    with open(filepath) as f:
        data = json.load(f)

    # Unwrap FeatureCollection / Feature → bare Geometry
    if data.get('type') == 'FeatureCollection':
        features = data.get('features', [])
        if not features:
            raise ValueError(f'No features in GeoJSON FeatureCollection: {filepath}')
        geom = features[0].get('geometry') or {}
    elif data.get('type') == 'Feature':
        geom = data.get('geometry') or {}
    else:
        geom = data  # bare Geometry

    geom_type = geom.get('type', '')
    if geom_type == 'Polygon':
        ring = geom['coordinates'][0]
    elif geom_type == 'MultiPolygon':
        ring = geom['coordinates'][0][0]
    else:
        raise ValueError(
            f'Expected Polygon or MultiPolygon geometry in {filepath}, got {geom_type!r}'
        )

    # GeoJSON spec: coordinates are [longitude, latitude, optional_elevation]
    coords = [(c[0], c[1]) for c in ring]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return 'POLYGON((' + ', '.join(f'{lon} {lat}' for lon, lat in coords) + '))'


def _read_polygon_shapefile(filepath):
    """Read the first polygon from a shapefile and return WKT (WGS84 lon/lat).

    Reprojects to WGS84 EPSG:4326 if the source CRS differs.
    Requires osgeo.ogr (GDAL).
    """
    try:
        from osgeo import ogr, osr
    except ImportError:
        raise ImportError(
            'osgeo.ogr is required to read shapefiles. '
            'Install GDAL, e.g.:  conda install -c conda-forge gdal'
        )

    ds = ogr.Open(filepath)
    if ds is None:
        raise ValueError(f'Could not open shapefile: {filepath}')

    layer = ds.GetLayer(0)
    feat  = layer.GetNextFeature()
    if feat is None:
        raise ValueError(f'No features in shapefile: {filepath}')

    geom = feat.GetGeometryRef()
    if geom is None:
        raise ValueError(f'First feature has no geometry in: {filepath}')

    # Reproject to WGS84 if needed
    src_srs = layer.GetSpatialRef()
    if src_srs is not None:
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not src_srs.IsSame(wgs84):
            geom = geom.Clone()
            transform = osr.CoordinateTransformation(src_srs, wgs84)
            geom.Transform(transform)

    return geom.ExportToWkt()


def _read_polygon_flat(filepath):
    """Read polygon from a flat lon,lat coordinate file (GrIMP style) and return WKT.

    Supports two sub-formats:
      - Flat comma list: all lon,lat pairs on one or a few lines,
        e.g. '-53.26,64.81,-48.1,60.33,...'  (optionally prefixed by an integer index)
      - Simple style: one 'lat lon' or 'lon,lat' pair per line
    """
    with open(filepath) as f:
        content = f.read()

    # Collect all numeric tokens; commas and whitespace are both separators.
    # Strip optional leading integer index from each line first.
    tokens = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Drop a bare leading integer index (e.g. "1" or "2") if present
        parts = line.split(None, 1)
        if parts[0].lstrip('-').isdigit() and '.' not in parts[0] and len(parts) == 2:
            line = parts[1]
        elif parts[0].lstrip('-').isdigit() and '.' not in parts[0] and len(parts) == 1:
            continue  # index-only line
        for tok in line.replace(',', ' ').split():
            try:
                tokens.append(float(tok))
            except ValueError:
                pass

    if len(tokens) < 6 or len(tokens) % 2 != 0:
        raise ValueError(
            f'Expected an even number of >=6 coordinate values in {filepath}, '
            f'got {len(tokens)}'
        )

    # Determine whether the flat list is lon,lat or lat,lon order.
    # Greenland/Alaska longitudes are strongly negative; latitudes are positive.
    # If the first value looks like a longitude (< -20 or > 20 and outside [-90,90])
    # treat as lon,lat; otherwise lat,lon.
    first = tokens[0]
    if abs(first) > 90 or (first < -20):
        # lon,lat order (GrIMP default)
        coords = [(tokens[i], tokens[i + 1]) for i in range(0, len(tokens), 2)]
    else:
        # lat,lon order
        coords = [(tokens[i + 1], tokens[i]) for i in range(0, len(tokens), 2)]

    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return 'POLYGON((' + ', '.join(f'{lon} {lat}' for lon, lat in coords) + '))'


def read_polygon(filepath):
    """Read a search-area polygon from *filepath* and return WKT.

    Supported formats (detected by file extension):

    *.geojson / *.json
        GeoJSON file.  FeatureCollection, Feature, and bare Geometry objects
        are all accepted.  The first polygon ring is used.

    *.shp
        ESRI Shapefile.  The first feature's geometry is used, reprojected to
        WGS84 (EPSG:4326) if necessary.  Requires GDAL/OGR.

    Anything else (e.g. *.lonlat)
        GrIMP flat coordinate file: all lon,lat pairs on one or a few lines,
        e.g. ``-53.26,64.81,-48.1,60.33,...``.  Optionally prefixed by a
        bare integer index per line.  Lon/lat order is auto-detected from the
        magnitude of the first value.

    Returns
    -------
    str
        WKT polygon string: ``'POLYGON((lon lat, lon lat, …))'``
    """
    _, ext = os.path.splitext(filepath.lower())

    if ext in ('.geojson', '.json'):
        return _read_polygon_geojson(filepath)
    elif ext == '.shp':
        return _read_polygon_shapefile(filepath)
    else:
        return _read_polygon_flat(filepath)


def _gpkg_record(url, track, frame, size_bytes, stem, status, url_to_geometry):
    """Build a GeoPackage record dict for one granule.

    Parameters mirror the variables already available inside the main loop.
    Metadata parsed from *stem* supplements the (track, frame) from the
    search result — the parsed values may differ for EA items that have
    their own naming format.
    """
    from reduces1.writeSearchGpkg import parse_nisar_meta
    meta = parse_nisar_meta(stem)
    # track/frame from the search result are authoritative; fill from
    # parsed stem only when the search result returned None.
    if track is not None:
        meta['track'] = track
    if frame is not None:
        meta['frame'] = frame
    meta['url']        = url
    meta['size_bytes'] = size_bytes
    meta['status']     = status
    meta['granule']    = stem
    meta['geometry']   = url_to_geometry.get(url)
    return meta


def main():
    parser = argparse.ArgumentParser(
        description='Search ASF DAAC for SAR products (NISAR or Sentinel-1)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
NISAR product choices:   L0B RSLC RIFG RUNW ROFF GSLC GCOV GUNW GOFF SME2
NISAR bandwidths (MHz):  5 5+5 20 20+5 40 40+5 77
Sentinel-1 products:     SLC GRD_HD GRD_MS GRD_HS GRD_FD GRD_MD OCN RAW BURST
Sentinel-1 beam modes:   IW EW S1 S2 S3 S4 S5 S6 WV
""",
    )

    # --- positional ---
    parser.add_argument('firstDate', help='Start date (YYYY-MM-DD)')
    parser.add_argument('lastDate',  help='End date (YYYY-MM-DD)')
    parser.add_argument('output',    help='Output file for download URLs; '
                                         'updated-version URLs go to output.updated')

    # --- sensor ---
    parser.add_argument(
        '--sensor',
        choices=['NISAR', 'SENTINEL1'],
        default='NISAR',
        help='Platform to search (default: NISAR)',
    )

    # --- products ---
    parser.add_argument(
        '--products', nargs='+', default=None,
        metavar='PRODUCT',
        help='Product type(s); NISAR default: RUNW ROFF RSLC; '
             'Sentinel-1 default: SLC  (see epilog for full lists)',
    )

    # --- Sentinel-1 beam mode ---
    parser.add_argument(
        '--beamMode', nargs='+', default=None,
        choices=_S1_BEAM_MODES,
        metavar='MODE',
        help='Beam mode(s) — Sentinel-1 only (default: IW); '
             'choices: ' + ' '.join(_S1_BEAM_MODES),
    )

    # --- NISAR bandwidth ---
    parser.add_argument(
        '--bandwidth', nargs='+', default=None,
        choices=_NISAR_BANDWIDTHS,
        metavar='BW',
        help='Range bandwidth(s) in MHz — NISAR only (default: 40 40+5 77); '
             'choices: ' + ' '.join(_NISAR_BANDWIDTHS),
    )

    # --- NISAR minimum processor version ---
    parser.add_argument(
        '--minVersion', type=int, default=0, metavar='N',
        help='Minimum NISAR processor (CRID) version number — NISAR only. '
             'Granules whose CRID number is below N are skipped. '
             'E.g. --minVersion 5010 accepts X05010, P05010, X05011, … '
             'but rejects X05009, P05006, etc.  (default: no minimum)',
    )

    # --- NISAR specific processor version(s) ---
    parser.add_argument(
        '--specificVersion', type=int, nargs='+', default=None, metavar='N',
        help='Exact NISAR processor (CRID) version number(s) to accept — NISAR only. '
             'Only granules whose CRID matches one of the listed values are kept. '
             'E.g. --specificVersion 5010  or  --specificVersion 5010 5012  '
             '(default: no restriction)',
    )

    # --- track / frame range ---
    parser.add_argument(
        '--startTrack', type=int, default=0, metavar='N',
        help='Minimum relative orbit / track number (default: all)',
    )
    parser.add_argument(
        '--endTrack', type=int, default=_ALL_MAX, metavar='N',
        help='Maximum relative orbit / track number (default: all)',
    )
    parser.add_argument(
        '--startFrame', type=int, default=0, metavar='N',
        help='Minimum frame number (default: all)',
    )
    parser.add_argument(
        '--endFrame', type=int, default=_ALL_MAX, metavar='N',
        help='Maximum frame number (default: all)',
    )

    # --- spatial area ---
    parser.add_argument(
        '--searchArea',
        default=_default_search_area(),
        help='Search polygon file.  Supported formats:\n'
             '  *.geojson / *.json  — GeoJSON polygon\n'
             '  *.shp               — ESRI shapefile (reprojected to WGS84)\n'
             '  other               — flat lon,lat coordinate file (GrIMP style)\n'
             'Default: bundled Greenland.lonlat',
    )

    # --- archive deduplication ---
    parser.add_argument(
        '--archiveDir', default=None, metavar='GLOB',
        help='Glob pattern for already-downloaded files, e.g. \'/data/NISAR/*\' '
             '(quote wildcards to prevent shell expansion). '
             'Extensions .h5 / .zip / .zip.1 are stripped before comparison. '
             'Matching products are skipped; for NISAR, if a newer processor '
             'version exists the URL is written to <archived_file>.updated '
             'beside the archive file.',
    )

    # --- GeoPackage export ---
    parser.add_argument(
        '--gpkg', default=None, metavar='FILE',
        help='Write footprints to a GeoPackage (QGIS-ready). '
             'One layer per product type (RUNW, ROFF, RSLC, …) so each can '
             'be toggled independently.  A "status" field marks granules as '
             '"found" (new), "exists" (already archived), or "updated" '
             '(newer version available).  Example: --gpkg search.gpkg',
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Apply sensor-specific defaults and cross-validate
    # ------------------------------------------------------------------

    # Products default depends on sensor
    if args.products is None:
        args.products = _SENSOR_PRODUCTS[args.sensor]['default']
    else:
        valid_products = _SENSOR_PRODUCTS[args.sensor]['choices']
        bad = [p for p in args.products if p not in valid_products]
        if bad:
            parser.error(
                f'Invalid product(s) for --sensor {args.sensor}: {bad}\n'
                f'Valid choices: {valid_products}'
            )

    # Bandwidth applies to NISAR only
    if args.bandwidth is None:
        args.bandwidth = _NISAR_BW_DEFAULT
    elif args.sensor != 'NISAR':
        parser.error('--bandwidth applies to NISAR only')

    # beamMode applies to Sentinel-1 only
    if args.beamMode is None:
        args.beamMode = _S1_BEAM_DEFAULT
    elif args.sensor != 'SENTINEL1':
        parser.error('--beamMode applies to SENTINEL1 only')

    # ------------------------------------------------------------------
    # Import asf_search
    # ------------------------------------------------------------------
    try:
        import asf_search as asf
    except ImportError:
        sys.exit('asf_search not installed. Run: pip install asf-search')

    # ------------------------------------------------------------------
    # Session — authenticate for restricted-data access.
    #
    # earthaccess uses the full EDL OAuth2 flow and is the most reliable
    # way to access NASA restricted collections (e.g. NISAR science-team
    # beta data).  If earthaccess is not installed we fall back to
    # asf_search's own auth_with_creds(), which uses the EDL
    # find_or_create_token endpoint; this works for public data but may
    # not satisfy CMR ACL checks on restricted collections.
    # ------------------------------------------------------------------
    session     = asf.ASFSession()
    _authed     = False
    _jwt        = None          # OAuth2 JWT; kept for NISAR_EA CMR search
    _netrc_path = os.path.expanduser('~/.netrc')

    # --- try earthaccess first (preferred for restricted NASA data) ---
    # earthaccess does the full EDL OAuth2 flow and returns a JWT bearer
    # token, which CMR uses to resolve user-group ACLs on restricted
    # collections.  auth_with_creds uses the older find_or_create_token
    # endpoint whose tokens may not satisfy restricted-collection checks.
    try:
        import earthaccess as _ea
        _ea.login(strategy='netrc', persist=False)
        _tok_dict = _ea.get_edl_token()            # {'access_token': ..., ...}
        _jwt = _tok_dict.get('access_token') or None
        if _jwt:
            session.auth_with_token(_jwt)
            print('Authenticated via earthaccess (OAuth2 JWT)')
            _authed = True
    except Exception:
        pass   # earthaccess not installed or netrc missing — fall through

    # --- fall back to asf_search auth_with_creds via netrc ---
    if not _authed:
        if os.path.exists(_netrc_path):
            try:
                import netrc as _netrc_mod
                _auth = _netrc_mod.netrc(_netrc_path).authenticators('urs.earthdata.nasa.gov')
                if _auth:
                    _user, _, _pw = _auth
                    session.auth_with_creds(_user, _pw)
                    print(f'Authenticated as {_user} via ~/.netrc '
                          '(Note: install earthaccess for full restricted-data access)')
                    _authed = True
                else:
                    print('~/.netrc found but no urs.earthdata.nasa.gov entry; '
                          'searching unauthenticated')
            except Exception as _e:
                print(f'Warning: netrc auth failed ({_e}); searching unauthenticated',
                      file=sys.stderr)
        else:
            print('No ~/.netrc found; searching unauthenticated')

    # ------------------------------------------------------------------
    # Build search
    # ------------------------------------------------------------------
    wkt = read_polygon(args.searchArea)

    MAX_RESULTS = 10000

    opts = asf.ASFSearchOptions(session=session)

    _common = dict(
        intersectsWith=wkt,
        start=args.firstDate + 'T00:00:00Z',
        end=args.lastDate + 'T23:59:59Z',
        maxResults=MAX_RESULTS,
        opts=opts,
    )

    spinner = _Spinner(f'Searching ASF ({args.sensor})...').start()
    try:
        if args.sensor == 'NISAR':
            results = list(asf.search(
                platform=[asf.PLATFORM.NISAR],
                processingLevel=args.products,
                rangeBandwidth=args.bandwidth,
                **_common,
            ))
        else:  # SENTINEL1
            results = list(asf.search(
                platform=[asf.PLATFORM.SENTINEL1],
                processingLevel=args.products,
                beamMode=args.beamMode,
                **_common,
            ))
    finally:
        spinner.stop()

    if len(results) >= MAX_RESULTS:
        print(
            f'WARNING: result count hit the {MAX_RESULTS} limit — '
            'results may be incomplete.',
            file=sys.stderr,
        )

    # ------------------------------------------------------------------
    # Build unified (url, track, frame) list from asf_search + NISAR_EA
    # ------------------------------------------------------------------
    def _to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    # asf_search results (public collections)
    all_items = []
    for result in results:
        props      = result.properties
        url        = props.get('url', '')
        track      = _to_int(props.get('pathNumber'))
        frame      = _to_int(props.get('frameNumber'))
        # bytes dict: {filename: {'bytes': N, 'format': '...'}, ...}
        bytes_dict = props.get('bytes') or {}
        url_base   = os.path.basename(url)
        if url_base in bytes_dict:
            size_bytes = bytes_dict[url_base].get('bytes', 0) or 0
        else:
            # fallback: first .h5 entry
            size_bytes = next(
                (info.get('bytes', 0) or 0
                 for fname, info in bytes_dict.items()
                 if fname.endswith('.h5')),
                0,
            )
        all_items.append((url, track, frame, size_bytes))
    print(f'ASF public: {len(results)} granule(s) found')

    # NISAR_EA restricted collections (only when authenticated via earthaccess)
    if args.sensor == 'NISAR' and _jwt:
        spinner2 = _Spinner('Searching NISAR_EA (restricted)...').start()
        ea_items = []
        try:
            ea_items = _search_nisar_ea(
                _jwt, wkt,
                args.firstDate, args.lastDate,
                args.products, args.bandwidth,
            )
        except Exception as _ea_err:
            print(f'\nWarning: NISAR_EA search failed ({_ea_err}); '
                  'restricted collection skipped.', file=sys.stderr)
        finally:
            spinner2.stop()
        if ea_items:
            print(f'NISAR_EA:  {len(ea_items)} granule(s) found')
        all_items.extend(ea_items)

    # Deduplicate by filename stem — the same granule can appear in both the
    # public collection and the EA collection with different URLs.
    if len(all_items) > len(results):   # only matters when EA results were added
        n_before = len(all_items)
        seen_stems: set = set()
        deduped = []
        for item in all_items:
            stem = _strip_ext(os.path.basename(item[0]))
            if stem not in seen_stems:
                seen_stems.add(stem)
                deduped.append(item)
        all_items = deduped
        n_dupes = n_before - len(all_items)
        if n_dupes:
            print(f'Deduped:   {n_dupes} duplicate(s) removed '
                  f'({len(all_items)} unique granules)')

    # ------------------------------------------------------------------
    # URL → footprint geometry map (for GeoPackage export).
    # asf_search results carry a .geometry GeoJSON dict; EA items do not.
    # ------------------------------------------------------------------
    url_to_geometry = {}
    if args.gpkg:
        for result in results:
            _url = result.properties.get('url', '')
            if _url:
                url_to_geometry[_url] = result.geometry

    # ------------------------------------------------------------------
    # Build archive index (if requested)
    # ------------------------------------------------------------------
    s1_archive, nisar_archive = (set(), {})
    if args.archiveDir:
        s1_archive, nisar_archive = build_archive_index(args.archiveDir, args.sensor)

    # ------------------------------------------------------------------
    # Filter by track / frame, check archive, write output files
    # ------------------------------------------------------------------
    filter_track = args.startTrack > 0 or args.endTrack < _ALL_MAX
    filter_frame = args.startFrame > 0 or args.endFrame < _ALL_MAX

    gpkg_records = []   # populated when --gpkg is set

    exists_path  = args.output + '.exists'
    updated_path = args.output + '.updated'
    out_fp      = None
    exists_fp   = None
    updated_fp  = None
    product_fps = {}   # product_type -> open file handle (lazy)
    n_found          = 0
    n_skipped        = 0
    n_updated        = 0
    n_ver_skip       = 0
    n_specver_skip   = 0
    volume_by_product = {}   # product_type -> total bytes (Found items only)
    try:
        out_fp = open(args.output, 'w')

        for url, track, frame, size_bytes in all_items:

            # Track / frame range filter
            if filter_track:
                if track is None or not (args.startTrack <= track <= args.endTrack):
                    continue
            if filter_frame:
                if frame is None or not (args.startFrame <= frame <= args.endFrame):
                    continue

            stem = _strip_ext(os.path.basename(url))

            # NISAR processor version filter
            if args.sensor == 'NISAR' and args.minVersion > 0:
                _, _, crid_num = _nisar_parse(stem)
                if crid_num is not None and 0 <= crid_num < args.minVersion:
                    n_ver_skip += 1
                    continue

            # NISAR specific-version filter
            if args.sensor == 'NISAR' and args.specificVersion:
                _, _, crid_num = _nisar_parse(stem)
                if crid_num is not None and crid_num >= 0 and crid_num not in args.specificVersion:
                    n_specver_skip += 1
                    continue

            # Archive deduplication
            if args.archiveDir:
                if args.sensor == 'SENTINEL1':
                    if stem in s1_archive:
                        if exists_fp is None:
                            exists_fp = open(exists_path, 'w')
                        exists_fp.write(url + '\n')
                        n_skipped += 1
                        if args.gpkg:
                            gpkg_records.append(_gpkg_record(
                                url, track, frame, size_bytes,
                                stem, 'exists', url_to_geometry))
                        continue

                else:  # NISAR
                    scene_key, new_ver, _ = _nisar_parse(stem)
                    if scene_key is not None and scene_key in nisar_archive:
                        arch_stem, arch_ver, arch_path = nisar_archive[scene_key]
                        if new_ver <= arch_ver:
                            if exists_fp is None:
                                exists_fp = open(exists_path, 'w')
                            exists_fp.write(url + '\n')
                            n_skipped += 1
                            if args.gpkg:
                                gpkg_records.append(_gpkg_record(
                                    url, track, frame, size_bytes,
                                    stem, 'exists', url_to_geometry))
                            continue
                        else:
                            if updated_fp is None:
                                updated_fp = open(updated_path, 'w')
                            updated_fp.write(url + '\n')
                            n_updated += 1
                            if args.gpkg:
                                gpkg_records.append(_gpkg_record(
                                    url, track, frame, size_bytes,
                                    stem, 'updated', url_to_geometry))
                            continue

            out_fp.write(url + '\n')
            n_found += 1
            if args.gpkg:
                gpkg_records.append(_gpkg_record(
                    url, track, frame, size_bytes,
                    stem, 'found', url_to_geometry))
            # Accumulate volume by product type and write per-product file
            stem_parts = stem.split('_')
            if stem_parts[0] == 'NISAR' and len(stem_parts) > 3:
                product = stem_parts[3]          # e.g. RUNW, ROFF, RSLC
            elif args.sensor == 'SENTINEL1':
                product = stem_parts[2] if len(stem_parts) > 2 else 'SLC'
            else:
                product = 'OTHER'
            volume_by_product[product] = volume_by_product.get(product, 0) + size_bytes
            if product not in product_fps:
                product_fps[product] = open(f'{args.output}.{product}', 'w')
            product_fps[product].write(url + '\n')

    finally:
        if out_fp:
            out_fp.close()
        if exists_fp:
            exists_fp.close()
        if updated_fp:
            updated_fp.close()
        for fp in product_fps.values():
            fp.close()

    parts = [f'Found: {n_found}']
    if n_skipped:
        parts.append(f'Skipped: {n_skipped} (see {exists_path})')
    if n_updated:
        parts.append(f'Updates: {n_updated} (see {updated_path})')
    if n_ver_skip:
        parts.append(f'Below --minVersion {args.minVersion}: {n_ver_skip}')
    if n_specver_skip:
        parts.append(f'Not in --specificVersion {args.specificVersion}: {n_specver_skip}')
    print('  '.join(parts))
    if volume_by_product:
        vol_parts = [
            f'{prod}: {nbytes / 1e9:.1f} GB'
            for prod, nbytes in sorted(volume_by_product.items())
        ]
        print('Volume: ' + '  '.join(vol_parts))

    if args.gpkg and gpkg_records:
        from reduces1.writeSearchGpkg import write_search_gpkg
        write_search_gpkg(args.gpkg, gpkg_records)


if __name__ == '__main__':
    main()
