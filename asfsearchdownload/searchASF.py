#!/usr/bin/env python3
"""Search ASF DAAC for SAR products within a date range and spatial area.

Supports NISAR (default) and Sentinel-1 platforms.
"""

import argparse
import datetime
import glob
import importlib.resources
import json
import logging
import os
import sys
import threading
import urllib.request


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

# Pre-defined circumpolar search polygon for Antarctica.
# Lon span = 360° > 180° so _wkt_to_cmr_spatial_params falls back to
# bounding_box: -180,-90,180,-60 — covering the full Antarctic swath.
_ANTARCTICA_WKT = (
    'POLYGON((-180.0 -60.0,-90.0 -60.0,0.0 -60.0,90.0 -60.0,'
    '180.0 -60.0,180.0 -90.0,90.0 -90.0,0.0 -90.0,-90.0 -90.0,'
    '-180.0 -90.0,-180.0 -60.0))'
)

# Restricted NISAR Engineering Archival collections (require science-team auth).
# These are NOT returned by a standard platform=NISAR search; we query them
# separately via authenticated CMR when earthaccess is available.
_NISAR_EA_COLLECTIONS = [
    'C4052500045-ASF',  # NISAR_EA_L1  (RSLC, RIFG, RUNW, ROFF, …)
    'C4052499921-ASF',  # NISAR_EA_L2  (GCOV, GSLC, GUNW, GOFF, …)
]

# Sentinel-1D is missing from asf_search's static collection tables
# (asf_search/CMR/datasets.py names SENTINEL-1A/B/C but not -1D, as of
# asf_search 12.2.0), so neither platform=SENTINEL-1 nor processingLevel=SLC
# reaches it and a plain search silently returns no S1D at all. Query its
# collections directly and merge. Remove this once asf_search ships S1D.
_CMR_COLLECTIONS_URL = 'https://cmr.earthdata.nasa.gov/search/collections.json'
_S1D_SHORTNAME = 'SENTINEL-1D*'


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


def _wkt_to_cmr_spatial_params(wkt, max_vertices=200):
    """Return CMR spatial params dict: {'polygon': ...} or {'bounding_box': ...}.

    Falls back to bounding box when the polygon has > max_vertices vertices or
    crosses the antimeridian (longitude span > 180°).  CMR cannot handle either.

    For circumpolar polygons (lon span > 180°) the bbox spans the full longitude
    range (-180 to 180) and extends to the pole, so searches like AllAntarctica
    capture the full orbital swath rather than a partial sector.
    """
    inner = wkt.replace('POLYGON((', '').rstrip(')')
    pairs = []
    for pair_str in inner.split(','):
        parts = pair_str.strip().split()
        if len(parts) == 2:
            try:
                pairs.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    if not pairs:
        return {}

    lons = [p[0] for p in pairs]
    lats = [p[1] for p in pairs]
    lon_span = max(lons) - min(lons)

    if len(pairs) > max_vertices or lon_span > 180:
        # Circumpolar polygon: span all longitudes; extend to the nearest pole
        # so interior coverage (e.g. tracks over the South Pole) is not missed.
        north_lat = max(lats)
        south_lat = min(lats)
        if south_lat < 0:
            south_lat = -90.0   # extend to South Pole
        else:
            north_lat = 90.0    # extend to North Pole
        bbox = f'-180.0000,{south_lat:.4f},180.0000,{north_lat:.4f}'
        return {'bounding_box': bbox}

    tokens = []
    for lon, lat in pairs:
        tokens.extend([str(lon), str(lat)])
    return {'polygon': ','.join(tokens)}


def _cmr_polygon_to_geojson(polygons_field):
    """Convert CMR 'polygons' entry to a GeoJSON Polygon dict, or None.

    CMR polygons is a list-of-lists of 'lat lon lat lon ...' strings.
    GeoJSON wants [[lon, lat], ...] with the ring closed.
    """
    if not polygons_field:
        return None
    try:
        ring_str = polygons_field[0][0]
    except (IndexError, TypeError):
        return None
    tokens = ring_str.split()
    if len(tokens) < 6 or len(tokens) % 2 != 0:
        return None
    coords = [[float(tokens[i + 1]), float(tokens[i])]   # lon, lat
              for i in range(0, len(tokens), 2)]
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return {'type': 'Polygon', 'coordinates': [coords]}


def _cmr_box_to_geojson(boxes_field):
    """Convert CMR 'boxes' entry ('S W N E') to a GeoJSON Polygon dict, or None."""
    if not boxes_field:
        return None
    try:
        parts = boxes_field[0].split()
        s, w, n, e = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
    except (IndexError, ValueError, TypeError):
        return None
    coords = [[w, s], [e, s], [e, n], [w, n], [w, s]]
    return {'type': 'Polygon', 'coordinates': [coords]}


def _search_nisar_ea(jwt, wkt, start, end, products, bandwidth, use_s3=False):
    """Search restricted NISAR_EA collections via authenticated CMR.

    Field layout of NISAR granule name (split on '_'):
      0:NISAR  1:L1  2:PR  3:<product>  4:<cycle>  5:<track>  6:<dir>
      7:<frame>  8:<subframe>  9:<BW*100>  10:<pol>  11..14:<datetimes>
      15..<proc>  19:<ver>

    Returns list of (url, track_int_or_None, frame_int_or_None, size_bytes, geom_or_None).
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

    spatial_params = _wkt_to_cmr_spatial_params(wkt)
    if 'bounding_box' in spatial_params:
        print('Note: using bounding_box fallback for CMR '
              '(polygon crosses antimeridian or exceeds vertex limit)')
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
                    **spatial_params,
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

                # Pick the data link (.h5 file): S3 or HTTPS
                url = None
                for lnk in entry.get('links', []):
                    rel  = lnk.get('rel', '')
                    href = lnk.get('href', '')
                    if use_s3:
                        if href.startswith('s3://') and href.endswith('.h5'):
                            url = href
                            break
                    elif ('fedsearch/1.1/data#' in rel
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

                # Footprint geometry from CMR spatial fields
                geom = (_cmr_polygon_to_geojson(entry.get('polygons'))
                        or _cmr_box_to_geojson(entry.get('boxes')))

                items.append((url, track, frame, size_bytes, geom))

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

def parseExcludeFrames(value):
    ''' Parse "track:frame[,frame...] track:frame..." into {track: {frames}}.

    Whitespace separates the track groups, commas the frames within a group:
    "90:191 141:317,323". Raises ValueError on anything else, since silently
    dropping a malformed entry would quietly resume downloading a frame
    someone meant to exclude.
    '''
    excluded = {}
    for group in value.split():
        trackPart, sep, framePart = group.partition(':')
        try:
            if not sep:
                raise ValueError
            track = int(trackPart)
            frames = {int(f) for f in framePart.split(',') if f}
        except ValueError:
            raise ValueError(f'--excludeFrames: cannot parse "{group}": '
                             'expected track:frame[,frame], e.g. 90:191')
        if not frames:
            raise ValueError(f'--excludeFrames: no frames given for "{group}"')
        excluded.setdefault(track, set()).update(frames)
    return excluded


def _strip_ext(name):
    """Strip .h5, .zip.1, or .zip from a filename (stem only, no path)."""
    for ext in ('.zip.1', '.zip', '.h5'):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _s1_scene_key(stem):
    """Acquisition identity of a Sentinel-1 granule name, or None.

    Everything up to the trailing 4-character product id, so two products of
    the same acquisition share it:

        S1C_IW_SLC__1SDH_20260625T090707_20260625T090734_008260_01054F_96E9
        S1C_IW_SLC__1SDH_20260625T090707_20260625T090734_008260_01054F_DC7D

    Strict, and None for anything that is not Sentinel-1, so NISAR and every
    other collection passing through this module are never deduped by it.
    """
    parts = _strip_ext(os.path.basename(stem)).split('_')
    if len(parts) != 10:
        return None
    if len(parts[0]) != 3 or not parts[0].startswith('S1'):
        return None
    if not parts[7].isdigit() or len(parts[7]) != 6 or len(parts[9]) != 4:
        return None
    return '_'.join(parts[:9])


def _drop_superseded_s1(results):
    """Keep only the newest product for each Sentinel-1 acquisition.

    ASF occasionally serves an acquisition reprocessed: same platform, same
    start and stop to the microsecond, same orbit and datatake, a new product
    id at the end of the name.  Downloading both is what breaks assembly two
    stages later -- the copies unpack side by side into one orbit directory and
    setupTrack finds 28 SAFEs against 14 SLC_tabs -- so the newer wins here,
    before anything is fetched.

    processingDate decides.  It is an ISO timestamp, so it compares lexically;
    where it is missing or equal, the later of the two in the incoming order
    wins, which is sorted by sceneName and therefore stable between runs.

    Returns (kept, dropped) with dropped a list of (superseded, kept) names.
    """
    best = {}
    for result in results:
        scene = _s1_scene_key(result.properties.get('sceneName') or '')
        if scene is None:
            continue
        held = best.get(scene)
        if held is None or (str(result.properties.get('processingDate') or '')
                            >= str(held.properties.get('processingDate') or '')):
            best[scene] = result
    if len(best) == sum(1 for r in results
                        if _s1_scene_key(r.properties.get('sceneName') or '')):
        return results, []                       # nothing shares a scene
    kept, dropped = [], []
    for result in results:
        name = result.properties.get('sceneName') or ''
        scene = _s1_scene_key(name)
        if scene is None or best[scene] is result:
            kept.append(result)
        else:
            dropped.append((name, best[scene].properties.get('sceneName')))
    return kept, dropped


def _extract_url(result, use_s3):
    """Return the download URL for an asf_search result (HTTPS or S3).

    When use_s3 is True, searches UMM RelatedUrls for an s3:// .h5 link,
    then falls back to result.properties['s3Urls'].  Returns '' if S3 is
    not available for this granule.
    """
    if not use_s3:
        return result.properties.get('url', '')
    for rel_url in (result.umm or {}).get('RelatedUrls', []):
        href = rel_url.get('URL', '')
        if href.startswith('s3://') and href.endswith('.h5'):
            return href
    s3_list = result.properties.get('s3Urls') or []
    if s3_list:
        return s3_list[0]
    return ''


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


def _drop_superseded_nisar(items):
    """Keep only the newest product for each NISAR granule.

    ASF sometimes serves the same granule twice: identical name up to the
    trailing product counter (..._001 and a reprocessed ..._002), or a newer
    CRID for the same acquisition.  Mosaicking both double-counts the frame,
    so only the newest is kept -- highest CRID, then highest counter.  The
    identity is _nisar_parse's scene_key, which excludes both fields.

    items are the (url, track, frame, size_bytes, ...) tuples built in main().
    Names _nisar_parse does not recognise pass through untouched.

    Returns (kept, dropped) with dropped a list of (superseded, kept) stems.
    """
    best = {}
    for item in items:
        scene, ver, crid = _nisar_parse(_strip_ext(os.path.basename(item[0])))
        if scene is None:
            continue
        held = best.get(scene)
        if held is None or (crid, ver) > held[1]:
            best[scene] = (item, (crid, ver))
    kept, dropped = [], []
    for item in items:
        stem = _strip_ext(os.path.basename(item[0]))
        scene, _, _ = _nisar_parse(stem)
        if scene is None or best[scene][0] is item:
            kept.append(item)
        else:
            dropped.append((stem, _strip_ext(os.path.basename(best[scene][0][0]))))
    return kept, dropped


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


class _IncompleteWatcher(logging.Handler):
    ''' Catch the asf_search log records that report a partially-delivered CMR
    response.

    Matches only "Results may be incomplete due to a search error", which
    asf_search logs from search() exactly when results.searchComplete is False
    -- the paging loop gave up before reaching the count CMR reported. That is
    the authoritative signal that granules were lost, and it is logged rather
    than raised, so the caller otherwise sees a plausible but short list.

    Deliberately NOT matched: "CMR returned page of incomplete results.Expected
    250 results, got 109". That fires per page whenever a page comes back under
    CMR_PAGE_SIZE, but the loop then continues through CMR-Search-After and
    normally reaches the full count anyway -- so it is noisy, frequently benign,
    and treating it as fatal aborts good searches. The nightly Antarctic log
    carries 28 of those against a single genuine searchComplete failure.

    Only installed under --strictComplete, so the default path (and every
    nightly download job) behaves exactly as before. Downloads tolerate a short
    result: they re-search a rolling window and dedup against the archive, so a
    granule missed tonight is fetched tomorrow. A one-shot inventory has no such
    second chance, which is what this is for. '''

    PATTERNS = ('Results may be incomplete',)

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.hits = []

    def emit(self, record):
        text = record.getMessage()
        if any(p in text for p in self.PATTERNS):
            self.hits.append(' '.join(text.split()))

    def reset(self):
        self.hits = []


def _searchStrict(doSearch, attempts=3):
    ''' Run doSearch(), re-running it while asf_search reports an incomplete CMR
    response. Returns (results, hits) with hits empty on a clean run; a caller
    that still sees hits after `attempts` tries should treat the result as
    short. '''
    watcher = _IncompleteWatcher()
    logger = logging.getLogger('asf_search')
    logger.addHandler(watcher)
    try:
        for attempt in range(1, attempts + 1):
            watcher.reset()
            results = doSearch()
            if not watcher.hits:
                return results, []
            print(f'CMR returned an incomplete response '
                  f'(attempt {attempt}/{attempts}): {watcher.hits[0]}',
                  file=sys.stderr)
        return results, list(watcher.hits)
    finally:
        logger.removeHandler(watcher)


def _search_windowed(runSearch, common, maxResults, label):
    ''' Run runSearch(common) over its date range, splitting the range in half
    and recursing whenever a query comes back saturated.

    A saturated result is silently truncated by CMR, so the granules past the
    cap are simply missing -- the old code could only warn after the fact.
    Halving is adaptive: a search that fits does one query, and only the dense
    windows pay for extra ones. Sub-windows are disjoint (`start` is exclusive
    of the previous `end` by one second) so nothing is fetched twice.

    Stops splitting at a one-second window, where there is nothing left to
    divide, and warns -- that many granules in one second means the cap is
    being hit for some reason other than window size. '''
    start = _iso_to_dt(common['start'])
    end = _iso_to_dt(common['end'])
    results = list(runSearch(common))
    if len(results) < maxResults:
        return results
    if (end - start).total_seconds() < 2:
        print(f'WARNING: {label} still saturated at {maxResults} in a '
              f'one-second window ({common["start"]}); results incomplete.',
              file=sys.stderr)
        return results
    middle = start + (end - start) / 2
    firstHalf = dict(common, end=_dt_to_iso(middle))
    secondHalf = dict(common,
                      start=_dt_to_iso(middle + datetime.timedelta(seconds=1)))
    return (_search_windowed(runSearch, firstHalf, maxResults, label)
            + _search_windowed(runSearch, secondHalf, maxResults, label))


def _iso_to_dt(text):
    ''' 'YYYY-MM-DDTHH:MM:SSZ' -> datetime. '''
    return datetime.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ')


def _dt_to_iso(when):
    ''' datetime -> 'YYYY-MM-DDTHH:MM:SSZ'. '''
    return when.strftime('%Y-%m-%dT%H:%M:%SZ')


def _s1d_collection_ids(timeout=30):
    ''' CMR concept-ids for the Sentinel-1D product collections at ASF.

    Looked up by short-name wildcard rather than hardcoded, so new S1D
    collections are picked up and a re-issued concept-id does not go stale.
    META_ collections are skipped: they carry a duplicate record per granule
    (processingLevel METADATA_SLC alongside SLC) and would double every count.
    Returns [] and warns on any failure -- a CMR hiccup should degrade the
    search to its pre-S1D behaviour, not abort it. '''
    query = (f'{_CMR_COLLECTIONS_URL}?provider=ASF&short_name={_S1D_SHORTNAME}'
             '&options[short_name][pattern]=true&page_size=100')
    try:
        with urllib.request.urlopen(query, timeout=timeout) as response:
            entries = json.load(response)['feed']['entry']
    except Exception as error:                      # network, JSON, or schema
        print(f'Warning: could not list Sentinel-1D collections ({error}); '
              'searching without S1D', file=sys.stderr)
        return []
    return [e['id'] for e in entries if '_META_' not in e['short_name']]


def _search_s1d(products, beamMode, common):
    ''' Sentinel-1D granules matching `products`, via a direct collection query.

    processingLevel is filtered here rather than passed to asf.search: the
    server-side filter is driven by the same table that omits S1D, so it
    matches nothing. The collection query is already narrow (S1D has no BURST
    collection, the usual reason an unfiltered S1 search explodes).

    Results are rebuilt as S1Product. Not knowing the S1D collections,
    asf_search hands back a generic ASFProduct, which reads three properties
    from different places than S1Product does -- frameNumber comes from
    CENTER_ESA_FRAME rather than FRAME_NUMBER (2299 where S1A/B/C would say
    378, an entirely different numbering), and groupID/pgeVersion come back
    None. Rebuilding from the same umm/meta puts S1D on the S1A/B/C
    conventions, so everything downstream can treat the satellites alike. '''
    import asf_search as asf     # imported lazily, as in main()
    from asf_search.Products import S1Product

    ids = _s1d_collection_ids()
    if not ids:
        return []
    wanted = set(products or [])
    results = _search_windowed(
        lambda c: asf.search(collections=ids, beamMode=beamMode, **c),
        common, common['maxResults'], 'Sentinel-1D')
    return [S1Product({'umm': r.umm, 'meta': r.meta}, session=r.session)
            for r in results
            if not wanted or r.properties.get('processingLevel') in wanted]


def _default_search_area():
    """Return path to the bundled Greenland search polygon.

    Uses importlib.resources.files() (Python >=3.9) with a __file__-based
    fallback for Python 3.8.
    """
    try:
        ref = importlib.resources.files('asfsearchdownload.searchRegions').joinpath(
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
    from asfsearchdownload.writeSearchGpkg import parse_nisar_meta
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

Part of the asfSearchAndDownload package.
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
        default=None,
        help='Platform to search (default: inferred from --products if '
             'given, else NISAR)',
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

    # --- flight direction ---
    parser.add_argument(
        '--flightDirection', type=str.upper, default=None,
        choices=['ASCENDING', 'DESCENDING'],
        metavar='DIR',
        help='Restrict to a single flight direction (ASCENDING or DESCENDING); '
             'default: both',
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
        '--excludeTracks', type=str, default='', metavar='"N N ..."',
        help='Tracks never to list, whatever the range allows, as one quoted '
             'space- or comma-separated value: --excludeTracks "114 143 155". '
             'One value rather than a list so it cannot swallow the trailing '
             'firstDate/lastDate/output arguments (default: exclude none)',
    )
    parser.add_argument(
        '--excludeFrames', type=str, default='', metavar='"T:F,F T:F ..."',
        help='Per-track frames never to list, as one quoted value: '
             '--excludeFrames "90:191 141:317,323". A search area drawn for a '
             'whole region clips frames that sit just outside an individual '
             "track's frameRange; those get downloaded and then binned to "
             'track-N/tmp every cycle. Scoped per track so excluding one '
             'cannot affect another (default: exclude none)',
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
    parser.add_argument(
        '--greenland', action='store_true', default=False,
        help='Search over Greenland using the bundled Greenland.lonlat polygon '
             '(equivalent to the default --searchArea)',
    )
    parser.add_argument(
        '--antarctica', action='store_true', default=False,
        help='Search over Antarctica (lon -180:180, lat -90:-60); '
             'uses a full-longitude bounding box via CMR',
    )

    # --- archive deduplication ---
    parser.add_argument(
        '--archiveDir', default=None, metavar='GLOB',
        help='Glob pattern for already-downloaded files, e.g. \'/data/NISAR/*\' '
             '(quote wildcards to prevent shell expansion). '
             'A directory hit (e.g. a bare \'.\') is expanded exactly one '
             'level to the files directly inside it -- NOT recursive, and '
             'any subdirectories found at that level are skipped, not '
             'descended into. If your archive is laid out in per-product '
             'subdirectories (e.g. ./RUNW/*.h5, ./ROFF/*.h5), pass a glob '
             'that reaches those files directly, e.g. \'./*/*\' -- a plain '
             '\'.\' will NOT see into them. Brace patterns like '
             '\'./{RUNW,ROFF}/*\' are NOT supported: this is passed straight '
             'to Python glob, which does not expand braces, and quoting stops '
             'the shell from expanding them either -- use \'./*/*\'. '
             'Extensions .h5 / .zip / .zip.1 are stripped before comparison. '
             'Matching products are skipped; for NISAR, if a newer processor '
             'version exists the URL is written to <archived_file>.updated '
             'beside the archive file.',
    )

    # --- GeoPackage export ---
    parser.add_argument(
        '--metadata', action='store_true',
        help='Also write <output>.meta: one line per granule with granule, '
             'track, frame, status, size and url. Unlike <output>, this '
             'includes granules already archived ("exists"), so a consumer '
             'can see everything ASF holds, not just what is missing. Used by '
             'findS1InsarPairs --onlinePairs to pair an undownloaded granule '
             'with one already on disk.')
    parser.add_argument(
        '--gpkg', default=None, metavar='FILE',
        help='Write footprints to a GeoPackage (QGIS-ready). '
             'One layer per product type (RUNW, ROFF, RSLC, …) so each can '
             'be toggled independently.  A "status" field marks granules as '
             '"found" (new), "exists" (already archived), or "updated" '
             '(newer version available).  Example: --gpkg search.gpkg',
    )

    parser.add_argument(
        '--strictComplete', action='store_true', default=False,
        help='Re-run the search when CMR returns an incomplete response, and '
             'exit non-zero rather than write a possibly-short result. For '
             'one-shot inventories (coverage maps, catalogs) where a short '
             'answer is indistinguishable from missing data. Off by default: '
             'rolling-window download jobs recover on the next run and should '
             'not abort.',
    )

    # --- S3 direct access ---
    parser.add_argument(
        '--s3', action='store_true', default=False,
        help='Output S3 URIs (s3://…) instead of HTTPS URLs. '
             'Granules without an available S3 link are silently skipped.',
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Apply sensor-specific defaults and cross-validate
    # ------------------------------------------------------------------

    # --sensor not given: infer it from --products (e.g. --products ROFF RUNW
    # with no --sensor means NISAR; --products SLC means SENTINEL1) so a
    # forgotten --sensor doesn't silently fall back to the wrong platform's
    # default. Falls back to NISAR when --products is also absent, or when
    # the given products don't unambiguously match one sensor (existing
    # validation below will then report the real mismatch).
    if args.sensor is None:
        if args.products:
            matches = [s for s, info in _SENSOR_PRODUCTS.items()
                      if all(p in info['choices'] for p in args.products)]
            args.sensor = matches[0] if len(matches) == 1 else 'NISAR'
        else:
            args.sensor = 'NISAR'

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
    if args.antarctica and args.greenland:
        parser.error('--antarctica and --greenland are mutually exclusive')
    if args.antarctica:
        wkt = _ANTARCTICA_WKT
    elif args.greenland:
        wkt = read_polygon(_default_search_area())
    else:
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
    # flightDirection applies to both platforms; None means no constraint.
    if args.flightDirection:
        _common['flightDirection'] = args.flightDirection

    def _runFullSearch():
        if args.sensor == 'NISAR':
            return _search_windowed(
                lambda c: asf.search(
                    platform=[asf.PLATFORM.NISAR],
                    processingLevel=args.products,
                    rangeBandwidth=args.bandwidth,
                    **c,
                ), _common, MAX_RESULTS, 'NISAR')
        # SENTINEL1
        found = _search_windowed(
            lambda c: asf.search(
                platform=[asf.PLATFORM.SENTINEL1],
                processingLevel=args.products,
                beamMode=args.beamMode,
                **c,
            ), _common, MAX_RESULTS, 'Sentinel-1')
        # platform=SENTINEL-1 does not cover Sentinel-1D (see
        # _s1d_collection_ids); fetch and merge it separately.
        s1d = _search_s1d(args.products, args.beamMode, _common)
        if s1d:
            print(f'Sentinel-1D: {len(s1d)} granule(s) added by collection '
                  'query', file=sys.stderr)
            found += s1d
        return found

    spinner = _Spinner(f'Searching ASF ({args.sensor})...').start()
    try:
        if args.strictComplete:
            results, incomplete = _searchStrict(_runFullSearch)
        else:
            results, incomplete = _runFullSearch(), []
    finally:
        spinner.stop()

    # Only reachable under --strictComplete. Refusing to write a short catalog
    # is the point: a silently-short inventory reads as missing data.
    if incomplete:
        sys.exit(f'ERROR: CMR kept returning incomplete results for '
                 f'{args.firstDate}..{args.lastDate} ({incomplete[0]}); '
                 f'refusing to write a possibly-short result. Re-run later.')

    # One chronological order for everything that follows -- the S1D results are
    # appended after the S1A/B/C ones, and a windowed search returns its chunks
    # back to back, so neither is time-ordered on its own.
    results.sort(key=lambda r: (r.properties.get('startTime') or '',
                                r.properties.get('sceneName') or ''))

    # Reprocessed Sentinel-1 products, both offered at once: take the newer and
    # never fetch the pair. Done on the sorted list so the choice is stable.
    results, superseded = _drop_superseded_s1(results)
    for name, kept in superseded:
        print(f'superseded: {name}\n  keeping: {kept}')
    if superseded:
        print(f'dropped {len(superseded)} superseded S1 product(s)')

    # No post-hoc "hit the limit" warning any more: _search_windowed splits a
    # saturated window and re-queries instead of returning a truncated list, and
    # warns itself in the one case it cannot subdivide further.

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
        url        = _extract_url(result, args.s3)
        if not url:
            continue
        track      = _to_int(props.get('pathNumber'))
        frame      = _to_int(props.get('frameNumber'))
        # 'bytes' is a dict {filename: {'bytes': N, 'format': '...'}, ...} for
        # NISAR, but a plain integer (total granule size) for Sentinel-1.
        bytes_val  = props.get('bytes')
        url_base   = os.path.basename(url)
        if isinstance(bytes_val, dict):
            if url_base in bytes_val:
                size_bytes = bytes_val[url_base].get('bytes', 0) or 0
            else:
                # fallback: first .h5 entry
                size_bytes = next(
                    (info.get('bytes', 0) or 0
                     for fname, info in bytes_val.items()
                     if fname.endswith('.h5')),
                    0,
                )
        else:
            size_bytes = _to_int(bytes_val) or 0
        all_items.append((url, track, frame, size_bytes, None))
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
                use_s3=args.s3,
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

    # Same NISAR granule served more than once (new product counter or CRID):
    # keep only the newest so it is neither downloaded nor mosaicked twice.
    if args.sensor == 'NISAR':
        all_items, superseded = _drop_superseded_nisar(all_items)
        for name, kept in superseded:
            print(f'superseded: {name}\n  keeping: {kept}')
        if superseded:
            print(f'dropped {len(superseded)} superseded NISAR product(s)')

    # ------------------------------------------------------------------
    # URL → footprint geometry map (for GeoPackage export).
    # asf_search results carry a .geometry GeoJSON dict; EA items do not.
    # ------------------------------------------------------------------
    # Public asf_search results carry .geometry; EA items carry geometry in the
    # 5th tuple element (extracted from CMR polygons/boxes fields).
    url_to_geometry = {}
    if args.gpkg or args.metadata:
        for result in results:
            _url = result.properties.get('url', '')
            if _url:
                url_to_geometry[_url] = result.geometry
        for item in all_items:
            if len(item) == 5 and item[4] is not None:
                url_to_geometry[item[0]] = item[4]

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
    exclude_tracks = {int(t) for t in
                      args.excludeTracks.replace(',', ' ').split()}
    exclude_frames = parseExcludeFrames(args.excludeFrames)
    if exclude_frames:
        print('Excluding frames: ' + ' '.join(
            f'{t}:{",".join(str(f) for f in sorted(fr))}'
            for t, fr in sorted(exclude_frames.items())))

    gpkg_records = []   # populated when --gpkg or --metadata is set

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
    n_excluded       = 0
    n_frame_excluded = 0
    volume_by_product = {}   # product_type -> total bytes (Found items only)
    try:
        out_fp = open(args.output, 'w')

        for url, track, frame, size_bytes, *_ in all_items:

            # Track / frame range filter
            if filter_track:
                if track is None or not (args.startTrack <= track <= args.endTrack):
                    continue
            # Excluded tracks: dropped whatever the range allows, since the
            # range cannot take a track out of the middle of the set
            if exclude_tracks and track in exclude_tracks:
                n_excluded += 1
                continue
            # Per-track frame exclusions: a frame the region outline clips in
            # but this track's frameRange puts out of range, so it would be
            # downloaded and then binned to track-N/tmp every cycle.
            if frame in exclude_frames.get(track, ()):
                n_frame_excluded += 1
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
                        if args.gpkg or args.metadata:
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
                            if args.gpkg or args.metadata:
                                gpkg_records.append(_gpkg_record(
                                    url, track, frame, size_bytes,
                                    stem, 'exists', url_to_geometry))
                            continue
                        else:
                            if updated_fp is None:
                                updated_fp = open(updated_path, 'w')
                            updated_fp.write(url + '\n')
                            n_updated += 1
                            if args.gpkg or args.metadata:
                                gpkg_records.append(_gpkg_record(
                                    url, track, frame, size_bytes,
                                    stem, 'updated', url_to_geometry))
                            continue

            out_fp.write(url + '\n')
            n_found += 1
            if args.gpkg or args.metadata:
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
    if n_excluded:
        excluded = ' '.join(str(t) for t in sorted(exclude_tracks))
        parts.append(f'Excluded tracks {excluded}: {n_excluded}')
    if n_frame_excluded:
        byTrack = ' '.join(f'{t}:{",".join(str(f) for f in sorted(fr))}'
                           for t, fr in sorted(exclude_frames.items()))
        parts.append(f'Excluded frames {byTrack}: {n_frame_excluded}')
    print('  '.join(parts))
    if volume_by_product:
        vol_parts = [
            f'{prod}: {nbytes / 1e9:.1f} GB'
            for prod, nbytes in sorted(volume_by_product.items())
        ]
        print('Volume: ' + '  '.join(vol_parts))

    if args.metadata:
        # Every granule the search matched, archived or not, with the track and
        # frame the search result carried. A consumer can then pair granules
        # without opening any zip -- the ascending node time needed for the
        # GrIMP burst frame is only inside the SAFE, but the ASF frame is
        # repeat-stable and serves the same purpose for pairing.
        meta_path = args.output + '.meta'
        with open(meta_path, 'w') as meta_fp:
            meta_fp.write('# granule track frame status sizeBytes url\n')
            for rec in gpkg_records:
                meta_fp.write(f"{rec.get('granule')} {rec.get('track')} "
                              f"{rec.get('frame')} {rec.get('status')} "
                              f"{rec.get('size_bytes', 0)} "
                              f"{rec.get('url')}\n")
        print(f'Metadata: {len(gpkg_records)} granule(s) -> {meta_path}')

    if args.gpkg and gpkg_records:
        from asfsearchdownload.writeSearchGpkg import write_search_gpkg
        write_search_gpkg(args.gpkg, gpkg_records)


if __name__ == '__main__':
    main()
