#!/usr/bin/env python3
"""Write ASF search results to a GeoPackage, one layer per product type.

Each layer is named after the product (RUNW, ROFF, RSLC, …) so QGIS
loads them as independently toggle-able layers.  A ``status`` field
distinguishes new downloads from already-archived granules.

Public API
----------
parse_nisar_meta(stem) -> dict
write_search_gpkg(gpkg_path, records)
"""

import json
import os

try:
    from osgeo import ogr, osr
    _HAVE_OGR = True
except ImportError:
    _HAVE_OGR = False

# -----------------------------------------------------------------------
# Granule metadata parser
# -----------------------------------------------------------------------

def parse_nisar_meta(stem):
    """Parse a NISAR granule stem into a metadata dict.

    Handles the two main field layouts produced by the NISAR SDS:

    Pair products — 20 fields (RIFG / RUNW / ROFF / GUNW / GOFF):
      NISAR_L1_PR_RUNW_<cyc>_<trk>_<dir>_<frm>_<sub>_<bw>_<pol>_
        <refStart>_<refStop>_<secStart>_<secStop>_<crid>_N_F_J_<ver>

    Single-acquisition — 18 fields (RSLC / GSLC / GCOV / L0B):
      NISAR_L1_PR_RSLC_<cyc>_<trk>_<dir>_<frm>_<bw>_<pol>_
        <acqStart>_<acqStop>_<crid>_N_P_J_<ver>

    Returns a dict with keys:
      product_type, level, cycle, track, direction, frame,
      polarization, bandwidth_mhz, ref_date, sec_date, acq_date, crid, version
    All values are None when not determinable.
    """
    parts = stem.split('_')
    if not parts or parts[0] != 'NISAR' or len(parts) < 4:
        return {}

    def _int(idx):
        try:
            return int(parts[idx]) if len(parts) > idx else None
        except (ValueError, TypeError):
            return None

    def _str(idx):
        return parts[idx] if len(parts) > idx else None

    def _date(token):
        """'20251122T235232' → '2025-11-22'  (first 8 chars)."""
        if token and len(token) >= 8:
            d = token[:8]
            return f'{d[:4]}-{d[4:6]}-{d[6:8]}'
        return None

    n = len(parts)

    meta = {
        'product_type': _str(3),
        'level':        _str(1),
        'cycle':        _int(4),
        'track':        _int(5),
        'direction':    _str(6),    # 'A' or 'D'
        'frame':        _int(7),
        'polarization': None,
        'bandwidth_mhz': None,
        'ref_date':     None,
        'sec_date':     None,
        'acq_date':     None,
        'crid':         None,
        'version':      None,
    }

    if n == 20:
        # Pair product: ..._sub_bw_pol_refStart_refStop_secStart_secStop_crid_N_F_J_ver
        bw_raw = _str(9)
        try:
            meta['bandwidth_mhz'] = int(bw_raw) / 100.0
        except (TypeError, ValueError):
            pass
        meta['polarization'] = _str(10)
        meta['ref_date']     = _date(_str(11))
        meta['sec_date']     = _date(_str(13))
        meta['crid']         = _str(15)
        meta['version']      = _int(19)

    elif n in (18, 19):
        # Single-acq: bandwidth at index 8 (new) or 9 (old)
        # _nisar_bw_field heuristic: first 4-digit all-digit field at 8 or 9
        bw_raw = None
        for idx in (8, 9):
            v = _str(idx)
            if v and len(v) == 4 and v.isdigit():
                bw_raw = v
                break
        try:
            meta['bandwidth_mhz'] = int(bw_raw) / 100.0 if bw_raw else None
        except (TypeError, ValueError):
            pass
        meta['polarization'] = _str(9)
        if n == 18:
            meta['acq_date'] = _date(_str(11))
            meta['crid']     = _str(13)
            meta['version']  = _int(17)
        else:   # 19 fields
            meta['acq_date'] = _date(_str(12))
            meta['crid']     = _str(14)
            meta['version']  = _int(18)

    return meta


# -----------------------------------------------------------------------
# GeoPackage writer
# -----------------------------------------------------------------------

# (name, OGR type, width)
_FIELD_DEFS = [
    ('product_type',   ogr.OFTString,  12  if _HAVE_OGR else None),
    ('track',          ogr.OFTInteger, 0   if _HAVE_OGR else None),
    ('frame',          ogr.OFTInteger, 0   if _HAVE_OGR else None),
    ('cycle',          ogr.OFTInteger, 0   if _HAVE_OGR else None),
    ('direction',      ogr.OFTString,  2   if _HAVE_OGR else None),
    ('polarization',   ogr.OFTString,  16  if _HAVE_OGR else None),
    ('bandwidth_mhz',  ogr.OFTReal,    0   if _HAVE_OGR else None),
    ('ref_date',       ogr.OFTString,  10  if _HAVE_OGR else None),
    ('sec_date',       ogr.OFTString,  10  if _HAVE_OGR else None),
    ('acq_date',       ogr.OFTString,  10  if _HAVE_OGR else None),
    ('crid',           ogr.OFTString,  12  if _HAVE_OGR else None),
    ('version',        ogr.OFTInteger, 0   if _HAVE_OGR else None),
    ('status',         ogr.OFTString,  10  if _HAVE_OGR else None),
    ('size_mb',        ogr.OFTReal,    0   if _HAVE_OGR else None),
    ('granule',        ogr.OFTString,  256 if _HAVE_OGR else None),
    ('url',            ogr.OFTString,  512 if _HAVE_OGR else None),
] if _HAVE_OGR else []


def _choose_epsg(records):
    """Pick EPSG from footprint latitudes: 3031 (Antarctica), 3413 (Arctic), 4326 (other)."""
    lats = []
    for rec in records:
        geom = rec.get('geometry')
        if not geom:
            continue
        for ring in geom.get('coordinates', []):
            for coord in ring:
                lats.append(coord[1])   # GeoJSON coords are [lon, lat]
    if not lats:
        return 4326
    mean_lat = sum(lats) / len(lats)
    if mean_lat < -60:
        return 3031   # Antarctic polar stereographic
    if mean_lat > 60:
        return 3413   # North Polar Stereographic (NSIDC)
    return 4326


def write_search_gpkg(gpkg_path, records):
    """Write search results to a GeoPackage with one layer per product type.

    Each layer is named after the product type (RUNW, ROFF, RSLC, …) so
    QGIS imports them as independently toggle-able layers.  Items without
    a footprint geometry are silently skipped.

    Parameters
    ----------
    gpkg_path : str
        Output path; overwritten if it already exists.
    records : list of dict
        Each dict must contain at minimum:
          - geometry    : GeoJSON dict (``{'type': 'Polygon', 'coordinates': …}``)
                          or *None* (item is skipped).
          - url         : str  — download URL
          - size_bytes  : int  — file size
          - status      : str  — 'found' | 'exists' | 'updated'
          - granule     : str  — granule stem (filename without extension)

        Additional optional keys (populated by ``parse_nisar_meta``):
          product_type, track, frame, cycle, direction, polarization,
          bandwidth_mhz, ref_date, sec_date, acq_date, crid, version.
    """
    if not _HAVE_OGR:
        raise ImportError(
            'osgeo.ogr is required to write GeoPackage output.\n'
            'Install GDAL/OGR, e.g.:  conda install -c conda-forge gdal'
        )

    # Group by product type; skip geometry-less items
    by_product: dict = {}
    n_no_geom = 0
    for rec in records:
        if not rec.get('geometry'):
            n_no_geom += 1
            continue
        pt = rec.get('product_type') or 'UNKNOWN'
        by_product.setdefault(pt, []).append(rec)

    if n_no_geom:
        print(f'  (skipped {n_no_geom} item(s) with no footprint geometry)')

    if not by_product:
        print('No features with geometry to write; GeoPackage not created.')
        return

    if os.path.exists(gpkg_path):
        os.remove(gpkg_path)

    driver = ogr.GetDriverByName('GPKG')
    ds = driver.CreateDataSource(gpkg_path)

    target_epsg = _choose_epsg(list(rec for recs in by_product.values() for rec in recs))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(target_epsg)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    if target_epsg != 4326:
        src_srs = osr.SpatialReference()
        src_srs.ImportFromEPSG(4326)
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        _transform = osr.CoordinateTransformation(src_srs, srs)
    else:
        _transform = None

    total_features = 0
    for product_type in sorted(by_product.keys()):
        items = by_product[product_type]

        layer = ds.CreateLayer(product_type, srs=srs, geom_type=ogr.wkbPolygon)

        for name, ftype, width in _FIELD_DEFS:
            fd = ogr.FieldDefn(name, ftype)
            if width:
                fd.SetWidth(width)
            layer.CreateField(fd)

        layer_defn = layer.GetLayerDefn()

        for rec in items:
            feat = ogr.Feature(layer_defn)

            # Geometry — reproject from WGS84 to target CRS if needed
            geom = ogr.CreateGeometryFromJson(json.dumps(rec['geometry']))
            if geom is None:
                continue
            if _transform:
                geom.Transform(_transform)
            feat.SetGeometry(geom)

            # Scalar fields
            for name, _, _ in _FIELD_DEFS:
                val = rec.get(name)
                if val is None:
                    continue
                if name == 'size_mb':
                    val = rec.get('size_bytes', 0) / 1e6
                feat.SetField(name, val)

            layer.CreateFeature(feat)
            total_features += 1

    ds = None

    layer_summary = ', '.join(
        f'{pt}×{len(items)}'
        for pt, items in sorted(by_product.items())
    )
    print(f'GeoPackage: {gpkg_path}  (EPSG:{target_epsg})')
    print(f'  {total_features} feature(s) in {len(by_product)} layer(s): {layer_summary}')
