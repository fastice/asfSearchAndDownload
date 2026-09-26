#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downloadRSLCSubset - fetch a spatial subset of a NISAR RSLC as a small, self-consistent RSLC.

    downloadRSLCSubset URL [URL ...] --outputDir DIR --outline glacier.gpkg
    downloadRSLCSubset --urlFile takuRSLC.urls.meta --outputDir DIR --physical R0 R1 T0 T1

A NISAR RSLC is ~27 GB; a glacier is typically a few percent of it. This fetches only the
HDF5 chunks under a window of the like-polarization band, over parallel HTTP range requests,
and writes a slim RSLC with the SAME internal paths in which the band, slantRange,
zeroDopplerTime, inputDataExceptionMask and validSamplesSubSwath* are all cropped to that
window. Every reader that derives geometry from the product - nisarhdf's RSLC class takes
SLCNearRange from slantRange[0], the sizes from the band shape and the first time from
zeroDopplerTime[0] - therefore sees a small image whose geometry is right by construction.
Nothing is hand-edited: a geodat made from the subset (multilookrslc --geojsonOnly) comes out
describing the subset.

Measured on Taku (track 135 frame 31, 40 MHz DHDH): 345 chunks of HH in 23 coalesced runs,
0.34 GB of a 27.2 GB granule.

Adapted from downloadNISARoptimized.py (GCOV); the fetch machinery is deliberately the same
so the two can be merged later. The load-bearing details carry over unchanged:

  * Per-request latency to ASF is a flat ~2 s regardless of size, so cost is
    (number of requests) x 2 s. Chunks must be coalesced into large runs.
  * Planning must use cache_type='blockcache'; the default cache re-fetches as the chunk
    index is walked out of order.
  * The presigned URL must be signed by a GET redirect followed to the end. A HEAD-signed
    CloudFront URL returns 403 on every later range request.
  * Every range response must be a 206 of exactly the requested length. A 200 written at an
    offset corrupts the file; a short 206 leaves zeros, and zeros inside a B-tree node make
    h5py return wrong data rather than raise.
  * Split by storage, not size. Chunked datasets go by byte range; contiguous and
    variable-length ones are read during planning and carried in memory - a VL dataset holds
    only pointers at its own offset and its strings live in a global heap elsewhere.

Window conventions. NISAR slantRange and zeroDopplerTime are pixel CENTRES, and so is
GrIMP's (initRoutines.c: Range = RNear + i * dR), so the subset's first range and time are
read straight out of the parent arrays with no half-pixel adjustment. The window is snapped
OUT to whole chunks: a gzip chunk cannot be partly decoded, so this costs no extra bytes,
and it lets each chunk be moved raw (read_direct_chunk / write_direct_chunk) with no
decompress/recompress - the band is bit-identical to the archive.

@author: ian
"""
import argparse
import asyncio
import datetime
import json
import os
import re
import sys
import time

import aiohttp
import fsspec
import h5py
import numpy as np
import requests

OK = 0
FETCHFAIL = 68
VERIFYFAIL = 69
NOOUTPUT = 64

ROOT = '/science/LSAR/RSLC'
SWATHS = f'{ROOT}/swaths'
IDENT = '/science/LSAR/identification'
GEOLOC = f'{ROOT}/metadata/geolocationGrid'

# Attributes that carry HDF5 object references; copying them verbatim leaves dangling
# pointers in the slim file.
REFERENCE_ATTRS = ('DIMENSION_LIST', 'REFERENCE_LIST', 'CLASS', 'NAME')

# Never transferred. RFI hit counts are 152 MB and no GrIMP tool reads them.
DROP_PREFIXES = (f'{ROOT}/metadata/RFI',)


def myerror(message):
    print(f'\033[1;31m{message}\033[0m', file=sys.stderr, flush=True)
    sys.exit(FETCHFAIL)


def parseArgs():
    p = argparse.ArgumentParser(
        description='\033[1mFetch a spatial subset of NISAR RSLCs as small, '
                    'self-consistent RSLC HDF5 files\033[0m',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Exactly one of --outline, --physical or --pixels selects the window.')
    p.add_argument('urls', nargs='*', help='RSLC URLs')
    p.add_argument('--urlFile', default=None,
                   help='File of URLs; the first https token on each line is used, so '
                        'searchASF .urls and .meta files both work')
    p.add_argument('--granules', nargs='+', default=None,
                   help='Restrict --urlFile to URLs containing these substrings')
    p.add_argument('--outputDir', required=True, help='Where the subset .h5 files go')
    p.add_argument('--tmpDir', default=None, help='Scratch for sparse files [outputDir]')
    w = p.add_mutually_exclusive_group(required=True)
    w.add_argument('--outline', default=None,
                   help='Polygon file (gpkg/shp/geojson); window covers it, located with '
                        "each granule's OWN geolocation grid")
    w.add_argument('--physical', nargs=4, type=float, metavar=('R0', 'R1', 'T0', 'T1'),
                   help='Slant range R0..R1 (m) and zeroDopplerTime T0..T1 (s after the '
                        "granule's epoch, i.e. seconds of day)")
    w.add_argument('--pixels', nargs=4, type=int, metavar=('C0', 'C1', 'L0', 'L1'),
                   help='Range columns C0..C1 and azimuth lines L0..L1, inclusive')
    p.add_argument('--outlineBuffer', type=float, default=1000.,
                   help='Buffer around --outline, metres [1000]')
    p.add_argument('--outlineHeights', nargs=2, type=float, default=(-100., 3500.),
                   metavar=('HMIN', 'HMAX'),
                   help='Terrain heights spanned by the outline, m; every geolocation '
                        'layer in this range is tested, since range shifts with height '
                        '[-100 3500]')
    p.add_argument('--pad', type=int, default=256,
                   help='Extra single-look pixels on every side before chunk snapping '
                        '[256; must cover half chip + half search of the tracker]')
    p.add_argument('--polarization', default='like',
                   help="Polarization to keep; 'like' picks HH or VV [like]")
    p.add_argument('--frequency', default='A', help='Frequency to keep [A]')
    p.add_argument('--connections', type=int, default=16, help='Parallel requests [16]')
    p.add_argument('--limit', type=float, default=0.,
                   help='Bandwidth cap MB/s, 0 = none [0]')
    p.add_argument('--check', action='store_true',
                   help='Plan and report, do not fetch')
    p.add_argument('--keepSparse', action='store_true', help='Keep the sparse scratch file')
    p.add_argument('--noVerify', action='store_true', help='Skip the verification gates')
    args = p.parse_args()
    urls = list(args.urls)
    if args.urlFile:
        for line in open(args.urlFile):
            m = re.search(r'https://\S+', line)
            if m:
                urls.append(m.group(0))
    if args.granules:
        urls = [u for u in urls if any(g in u for g in args.granules)]
    urls = [u for u in dict.fromkeys(urls) if '_RSLC_' in u]
    if not urls:
        myerror('downloadRSLCSubset: no RSLC URLs given')
    args.urls = urls
    args.tmpDir = args.tmpDir or args.outputDir
    os.makedirs(args.outputDir, exist_ok=True)
    os.makedirs(args.tmpDir, exist_ok=True)
    return args


def granuleStem(url):
    return os.path.basename(url).replace('.h5', '')


# ------------------------------------------------------------------ fetch machinery
# Same as downloadNISARoptimized.py; kept local so this tool does not depend on a file that
# is still being edited. Merge candidates.

def resolvePresigned(url, session):
    '''Follow ASF -> Earthdata OAuth -> ASF -> CloudFront to the end, with a GET.'''
    r = session.get(url, allow_redirects=True, stream=True, headers={'Range': 'bytes=0-0'})
    try:
        # raise rather than exit: one bad granule must not stop the batch
        if r.status_code not in (200, 206):
            raise RuntimeError(f'{r.status_code} resolving {url}')
        if 'urs.earthdata.nasa.gov' in r.url:
            raise RuntimeError('stopped at Earthdata login - check ~/.netrc')
        return r.url
    finally:
        r.close()


def coalesce(ranges, gap, size):
    rs = sorted((max(0, a), min(size, b)) for a, b in ranges if b > a)
    out = []
    for a, b in rs:
        if out and a - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


async def fetchRuns(url, runs, path, nConn, bytesPerSec):
    '''Fetch runs into a sparse file at their true offsets.'''
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    done = [0]
    t0 = time.time()
    work = []
    for a, b in runs:
        step = max(4 << 20, (b - a) // 4 + 1)
        for s in range(a, b, step):
            work.append((s, min(b, s + step)))
    wanted = sum(b - a for a, b in work)
    sem = asyncio.Semaphore(nConn)

    async def one(session, a, b):
        async with sem:
            for attempt in range(4):
                try:
                    async with session.get(url, headers={'Range': f'bytes={a}-{b-1}'}) as r:
                        if r.status != 206:
                            raise IOError(f'expected 206, got {r.status}')
                        buf = await r.read()
                    if len(buf) != b - a:
                        raise IOError(f'short read {len(buf)} != {b - a}')
                    os.pwrite(fd, buf, a)
                    done[0] += len(buf)
                    if bytesPerSec:
                        lag = done[0] / bytesPerSec - (time.time() - t0)
                        if lag > 0:
                            await asyncio.sleep(lag)
                    return
                except Exception:
                    if attempt == 3:
                        raise
                    await asyncio.sleep(1 + attempt)

    conn = aiohttp.TCPConnector(limit=nConn)
    timeout = aiohttp.ClientTimeout(total=None, sock_read=120)
    async with aiohttp.ClientSession(timeout=timeout, connector=conn) as s:
        await asyncio.gather(*(one(s, a, b) for a, b in work))
    os.close(fd)
    if done[0] != wanted:
        myerror(f'fetchRuns: fetched {done[0]} bytes, expected {wanted}')
    return done[0]


# ------------------------------------------------------------------ window

def pickPolarization(h, freq, want):
    pols = [p.decode() for p in h[f'{SWATHS}/frequency{freq}/listOfPolarizations'][()]]
    if want != 'like':
        if want not in pols:
            myerror(f'polarization {want} not in {pols}')
        return want, pols
    for p in ('HH', 'VV'):
        if p in pols:
            return p, pols
    myerror(f'no like-polarization in {pols}')


def outlineBox(h, args):
    '''Slant range / time box of the outline, from the granule's own geolocation grid.

    The grid is coarse (~0.065 s x ~485 m) so the box is widened by one node each way, and
    every height layer in --outlineHeights is tested because a point's slant range depends
    on its height; the union is conservative.
    '''
    import geopandas as gpd
    from matplotlib.path import Path
    g = h[GEOLOC]
    epsg = int(np.array(g['epsg']))
    srG, tG, hG = g['slantRange'][:], g['zeroDopplerTime'][:], g['heightAboveEllipsoid'][:]
    shp = gpd.read_file(args.outline)
    # buffer in metres, then express in the grid's CRS
    metric = shp.to_crs(shp.estimate_utm_crs())
    buffered = metric.buffer(args.outlineBuffer).to_crs(epsg)
    # union_all() is geopandas >= 1.0; unary_union the older spelling
    poly = buffered.union_all() if hasattr(buffered, 'union_all') \
        else buffered.unary_union
    polys = [poly] if poly.geom_type == 'Polygon' else list(poly.geoms)
    inside = np.zeros((len(tG), len(srG)), bool)
    for k in np.where((hG >= args.outlineHeights[0]) & (hG <= args.outlineHeights[1]))[0]:
        X, Y = g['coordinateX'][k], g['coordinateY'][k]
        pts = np.column_stack([X.ravel(), Y.ravel()])
        hit = np.zeros(len(pts), bool)
        for p in polys:
            h1 = Path(np.asarray(p.exterior.coords)[:, :2]).contains_points(pts)
            for ring in p.interiors:
                h1 &= ~Path(np.asarray(ring.coords)[:, :2]).contains_points(pts)
            hit |= h1
        inside |= hit.reshape(X.shape)
    if not inside.any():
        return None
    ti, rj = np.where(inside)
    r0 = srG[max(0, rj.min() - 1)]
    r1 = srG[min(len(srG) - 1, rj.max() + 1)]
    t0 = tG[max(0, ti.min() - 1)]
    t1 = tG[min(len(tG) - 1, ti.max() + 1)]
    return r0, r1, t0, t1


def computeWindow(h, pol, args, cz, shape):
    '''Window in single-look pixels, padded and snapped out to whole chunks.'''
    sr = h[f'{SWATHS}/frequency{args.frequency}/slantRange'][:]
    zd = h[f'{SWATHS}/zeroDopplerTime'][:]
    if args.pixels:
        c0, c1, l0, l1 = args.pixels
        phys = None
    else:
        phys = outlineBox(h, args) if args.outline else tuple(args.physical)
        if phys is None:
            return None, None
        r0, r1, t0, t1 = phys
        if r1 < sr[0] or r0 > sr[-1] or t1 < zd[0] or t0 > zd[-1]:
            return None, phys
        # searchsorted on the granule's OWN arrays: no start-pixel or skip assumption, and
        # different acquisition modes reconcile automatically.
        c0, c1 = np.searchsorted(sr, r0), np.searchsorted(sr, r1)
        l0, l1 = np.searchsorted(zd, t0), np.searchsorted(zd, t1)
    c0, l0 = max(0, c0 - args.pad), max(0, l0 - args.pad)
    c1, l1 = min(shape[1] - 1, c1 + args.pad), min(shape[0] - 1, l1 + args.pad)
    if c1 <= c0 or l1 <= l0:
        return None, phys
    # snap OUT to whole chunks: same bytes, and chunks can be moved raw
    C0, L0 = (c0 // cz[1]) * cz[1], (l0 // cz[0]) * cz[0]
    C1 = min(shape[1] - 1, (c1 // cz[1] + 1) * cz[1] - 1)
    L1 = min(shape[0] - 1, (l1 // cz[0] + 1) * cz[0] - 1)
    return (int(C0), int(C1), int(L0), int(L1)), phys


# ------------------------------------------------------------------ planning

def plan(fh, h, args):
    freq = args.frequency
    fgrp = f'{SWATHS}/frequency{freq}'
    pol, pols = pickPolarization(h, freq, args.polarization)
    band = h[f'{fgrp}/{pol}']
    if band.chunks is None:
        myerror('RSLC band is contiguous; window fetch would be latency bound')
    win, phys = computeWindow(h, pol, args, band.chunks, band.shape)
    if win is None:
        return None
    C0, C1, L0, L1 = win
    rasters = [f'{fgrp}/{pol}', f'{fgrp}/inputDataExceptionMask']
    otherFreq = [f'{SWATHS}/frequency{f}' for f in ('A', 'B') if f != freq]
    dropPols = [p for p in pols if p != pol]

    def keep(path):
        if path.startswith(DROP_PREFIXES):
            return False
        if any(path == o or path.startswith(o + '/') for o in otherFreq):
            return False
        # any path component that names a dropped polarization (swath band, calibration)
        if any(c in dropPols for c in path.split('/')):
            return False
        # other frequency's calibration/processing sub-groups
        if any(c == f'frequency{f}' for f in ('A', 'B') if f != freq
               for c in path.split('/')):
            return False
        return True

    chunked, small, groups = [], {}, {}

    def visit(name, obj):
        path = '/' + name
        if not keep(path):
            return
        if isinstance(obj, h5py.Group):
            groups[path] = {k: v for k, v in obj.attrs.items() if k not in REFERENCE_ATTRS}
            return
        if not isinstance(obj, h5py.Dataset):
            return
        attrs = {k: v for k, v in obj.attrs.items() if k not in REFERENCE_ATTRS}
        if path in rasters:
            chunked.append((path, True, attrs))
        elif obj.chunks is not None:
            chunked.append((path, False, attrs))
        else:
            small[path] = (obj[()], attrs, obj.dtype)
    h.visititems(visit)
    groups['/'] = {k: v for k, v in h.attrs.items() if k not in REFERENCE_ATTRS}

    # byte extents: window chunks for the rasters, all chunks for other chunked datasets
    want = []
    for path, isRaster, _ in chunked:
        d = h[path]
        if isRaster:
            cz = d.chunks
            for L in range(L0, L1 + 1, cz[0]):
                for C in range(C0, C1 + 1, cz[1]):
                    ci = d.id.get_chunk_info_by_coord((L, C))
                    if ci is not None and ci.byte_offset is not None:
                        want.append((ci.byte_offset, ci.byte_offset + ci.size))
        else:
            for i in range(d.id.get_num_chunks()):
                ci = d.id.get_chunk_info(i)
                want.append((ci.byte_offset, ci.byte_offset + ci.size))
    return dict(pol=pol, win=win, phys=phys, chunked=chunked, small=small,
                groups=groups, want=want, meta=list(fh._grimpRanges), size=fh.size)


# ------------------------------------------------------------------ repack

def isoTime(epochUnits, seconds):
    '''NISAR-style time string from a "seconds since <iso>" epoch.'''
    epoch = datetime.datetime.fromisoformat(epochUnits.split('since')[1].strip())
    t = epoch + datetime.timedelta(seconds=float(seconds))
    return f'{t:%Y-%m-%dT%H:%M:%S}.{t.microsecond * 1000:09d}'


def fixedBytes(value, like):
    '''Encode a string as fixed-length bytes, keeping the dataset's own style.'''
    b = value.encode() if isinstance(value, str) else value
    return np.bytes_(b)


def subsetPolygon(src, sr, zd, h0=0.):
    '''WKT polygon of the subset perimeter at height h0, from the geolocation cube.'''
    from scipy.interpolate import RegularGridInterpolator
    g = src[GEOLOC]
    hG, tG, rG = g['heightAboveEllipsoid'][:], g['zeroDopplerTime'][:], g['slantRange'][:]
    ix = RegularGridInterpolator((hG, tG, rG), g['coordinateX'][:], bounds_error=False,
                                 fill_value=None)
    iy = RegularGridInterpolator((hG, tG, rG), g['coordinateY'][:], bounds_error=False,
                                 fill_value=None)
    n = 12
    rr = np.concatenate([np.linspace(sr[0], sr[-1], n), np.full(n, sr[-1]),
                         np.linspace(sr[-1], sr[0], n), np.full(n, sr[0])])
    tt = np.concatenate([np.full(n, zd[0]), np.linspace(zd[0], zd[-1], n),
                         np.full(n, zd[-1]), np.linspace(zd[-1], zd[0], n)])
    pts = np.column_stack([np.full(rr.size, h0), tt, rr])
    x, y = ix(pts), iy(pts)
    ring = ','.join(f'{a!r} {b!r} {h0!r}' for a, b in zip(x, y))
    ring += f',{x[0]!r} {y[0]!r} {h0!r}'
    return f'POLYGON (({ring}))'


def repack(job, stem, args):
    C0, C1, L0, L1 = job['win']
    nr, na = C1 - C0 + 1, L1 - L0 + 1
    freq = args.frequency
    fgrp = f'{SWATHS}/frequency{freq}'
    pol = job['pol']
    outPath = os.path.join(args.outputDir, stem + '.subset.h5')
    tmp = outPath + '.partial'
    small = job['small']
    with h5py.File(job['sparse'], 'r') as s, h5py.File(tmp, 'w') as d:
        for path, attrs in job['groups'].items():
            g = d.require_group(path)
            for k, v in attrs.items():
                g.attrs[k] = v
        # rasters: raw chunks moved to shifted offsets - no decode, bit-identical
        for path, isRaster, attrs in job['chunked']:
            src = s[path]
            if isRaster:
                dst = d.create_dataset(path, shape=(na, nr), dtype=src.dtype,
                                       chunks=src.chunks, compression=src.compression,
                                       compression_opts=src.compression_opts,
                                       shuffle=src.shuffle, fletcher32=src.fletcher32)
                cz = src.chunks
                for L in range(L0, L1 + 1, cz[0]):
                    for C in range(C0, C1 + 1, cz[1]):
                        filt, raw = src.id.read_direct_chunk((L, C))
                        dst.id.write_direct_chunk((L - L0, C - C0), raw, filter_mask=filt)
            else:
                dst = d.create_dataset(path, shape=src.shape, dtype=src.dtype,
                                       chunks=src.chunks, compression=src.compression,
                                       compression_opts=src.compression_opts,
                                       shuffle=src.shuffle, fletcher32=src.fletcher32)
                for i in range(src.id.get_num_chunks()):
                    ci = src.id.get_chunk_info(i)
                    filt, raw = src.id.read_direct_chunk(ci.chunk_offset)
                    dst.id.write_direct_chunk(ci.chunk_offset, raw, filter_mask=filt)
            for k, v in attrs.items():
                dst.attrs[k] = v
        # grid-tied small datasets: cropped so geometry is consistent by construction
        srPath, zdPath = f'{fgrp}/slantRange', f'{SWATHS}/zeroDopplerTime'
        sr, zd = small[srPath][0], small[zdPath][0]
        srS, zdS = sr[C0:C1 + 1], zd[L0:L1 + 1]
        override = {srPath: srS, zdPath: zdS}
        for path, (data, attrs, dtype) in small.items():
            base = os.path.basename(path)
            if path.startswith(fgrp + '/validSamplesSubSwath'):
                v = np.asarray(data)[L0:L1 + 1].astype(np.int64) - C0
                # assumed half-open [first, last) as in ISCE3; clipped to the subset
                v = np.clip(v, 0, nr)
                empty = v[:, 1] <= v[:, 0]
                v[empty] = 0
                override[path] = v.astype(dtype)
            elif path == f'{fgrp}/listOfPolarizations':
                override[path] = np.array([pol.encode()], dtype=dtype)
            elif path == f'{IDENT}/listOfFrequencies':
                override[path] = np.array([freq.encode()], dtype=dtype)
            elif path == f'{IDENT}/zeroDopplerStartTime':
                override[path] = np.bytes_(isoTime(small[zdPath][1]['units'].decode()
                                                   if isinstance(small[zdPath][1]['units'], bytes)
                                                   else small[zdPath][1]['units'], zdS[0]))
            elif path == f'{IDENT}/zeroDopplerEndTime':
                override[path] = np.bytes_(isoTime(small[zdPath][1]['units'].decode()
                                                   if isinstance(small[zdPath][1]['units'], bytes)
                                                   else small[zdPath][1]['units'], zdS[-1]))
        for path, (data, attrs, dtype) in small.items():
            if path in override:
                val = override[path]
                if dtype.kind == 'S':
                    ds = d.create_dataset(path, data=val)          # length follows value
                else:
                    ds = d.create_dataset(path, data=val, dtype=dtype)
            else:
                ds = d.create_dataset(path, data=data, dtype=dtype)
            for k, v in attrs.items():
                ds.attrs[k] = v
        # footprint of the subset, not the parent
        bpPath = f'{IDENT}/boundingPolygon'
        if bpPath in d:
            attrs = dict(d[bpPath].attrs)
            del d[bpPath]
            ds = d.create_dataset(bpPath, data=np.bytes_(subsetPolygon(s, srS, zdS)))
            for k, v in attrs.items():
                ds.attrs[k] = v
        # provenance
        d.attrs['subsetOf'] = stem
        d.attrs['subsetFirstRangeSample'] = C0
        d.attrs['subsetFirstAzimuthLine'] = L0
        d.attrs['subsetRangeSize'] = nr
        d.attrs['subsetAzimuthSize'] = na
        d.attrs['subsetPolarization'] = pol
        d.attrs['subsetTool'] = 'asfsearchdownload.downloadRSLCSubset'
        if job['phys'] is not None:
            d.attrs['subsetRequestedPhysicalBox'] = np.array(job['phys'], dtype='f8')
    os.replace(tmp, outPath)
    return outPath


# ------------------------------------------------------------------ verification

def verify(outPath, job, signed, args):
    '''Gates: consistent geometry, and the band bit-identical to the archive.'''
    C0, C1, L0, L1 = job['win']
    fgrp = f'{SWATHS}/frequency{args.frequency}'
    pol = job['pol']
    problems = []
    sr0 = job['small'][f'{fgrp}/slantRange'][0]
    zd0 = job['small'][f'{SWATHS}/zeroDopplerTime'][0]
    with h5py.File(outPath, 'r') as h:
        b = h[f'{fgrp}/{pol}']
        sr, zd = h[f'{fgrp}/slantRange'][:], h[f'{SWATHS}/zeroDopplerTime'][:]
        if b.shape != (zd.size, sr.size):
            problems.append(f'band {b.shape} vs zd {zd.size} x sr {sr.size}')
        if sr[0] != sr0[C0] or sr[-1] != sr0[C1]:
            problems.append('slantRange crop does not match the parent')
        if zd[0] != zd0[L0] or zd[-1] != zd0[L1]:
            problems.append('zeroDopplerTime crop does not match the parent')
        # two chunks, compared against a direct read of the archive
        cz = b.chunks
        probes = [(0, 0), ((b.shape[0] // cz[0] // 2) * cz[0], (b.shape[1] // cz[1] // 2) * cz[1])]
        local = [b[a:a + cz[0], c:c + cz[1]] for a, c in probes]
    fs = fsspec.filesystem('http')
    with fs.open(signed, 'rb', cache_type='blockcache', block_size=4 * 1024 * 1024) as f:
        with h5py.File(f, 'r') as h:
            rb = h[f'{fgrp}/{pol}']
            for (a, c), loc in zip(probes, local):
                rem = rb[L0 + a:L0 + a + cz[0], C0 + c:C0 + c + cz[1]]
                if not np.array_equal(rem.view(np.uint64), loc.view(np.uint64)):
                    problems.append(f'band chunk at ({a},{c}) differs from the archive')
    return problems


# ------------------------------------------------------------------ driver

def doOne(url, args, session):
    stem = granuleStem(url)
    outPath = os.path.join(args.outputDir, stem + '.subset.h5')
    if os.path.exists(outPath):
        print(f'  {stem}: exists, skipping', flush=True)
        return OK
    t0 = time.time()
    signed = resolvePresigned(url, session)
    fs = fsspec.filesystem('http')
    fh = fs.open(signed, 'rb', cache_type='blockcache', block_size=4 * 1024 * 1024)
    fh._grimpRanges = []
    inner = fh.cache.fetcher

    def record(a, b):
        fh._grimpRanges.append((a, b))
        return inner(a, b)

    fh.cache.fetcher = record
    h = h5py.File(fh, 'r')
    job = plan(fh, h, args)
    h.close()
    if job is None:
        print(f'  {stem}: window does not intersect this granule, skipping', flush=True)
        return NOOUTPUT
    C0, C1, L0, L1 = job['win']
    runs = coalesce(job['want'] + job['meta'], 1 << 20, job['size'])
    runBytes = sum(b - a for a, b in runs)
    print(f'  {stem[-40:]}: {job["pol"]} window cols {C0}..{C1} lines {L0}..{L1} '
          f'({C1-C0+1} x {L1-L0+1}); {len(runs)} runs, {runBytes/1e9:.3f} GB of '
          f'{job["size"]/1e9:.2f} GB  (plan {time.time()-t0:.0f} s)', flush=True)
    if args.check:
        return OK
    sparse = os.path.join(args.tmpDir, stem + '.sparse')
    if os.path.exists(sparse):
        os.remove(sparse)
    fd = os.open(sparse, os.O_WRONLY | os.O_CREAT)
    os.truncate(fd, job['size'])
    os.close(fd)
    t1 = time.time()
    got = asyncio.run(fetchRuns(signed, runs, sparse, args.connections,
                                args.limit * 1e6 if args.limit else 0))
    print(f'    fetched {got/1e9:.3f} GB in {time.time()-t1:.0f} s '
          f'({got/max(1e-6, time.time()-t1)/1e6:.1f} MB/s)', flush=True)
    job['sparse'] = sparse
    outPath = repack(job, stem, args)
    if not args.keepSparse:
        os.remove(sparse)
    if not args.noVerify:
        problems = verify(outPath, job, signed, args)
        if problems:
            for p in problems:
                print(f'    \033[1;31mVERIFY FAIL: {p}\033[0m', flush=True)
            os.replace(outPath, outPath + '.failedVerify')
            return VERIFYFAIL
    print(f'    wrote {outPath} ({os.path.getsize(outPath)/1e6:.0f} MB) '
          f'in {time.time()-t0:.0f} s total', flush=True)
    return OK


def main():
    args = parseArgs()
    session = requests.Session()   # ~/.netrc supplies Earthdata credentials on redirect
    worst = OK
    for url in args.urls:
        try:
            rc = doOne(url, args, session)
        except Exception as e:
            print(f'  {granuleStem(url)}: \033[1;31mFAILED\033[0m {type(e).__name__}: {e}',
                  flush=True)
            rc = FETCHFAIL
        worst = max(worst, rc) if rc != NOOUTPUT else worst
    sys.exit(worst)


if __name__ == '__main__':
    main()
