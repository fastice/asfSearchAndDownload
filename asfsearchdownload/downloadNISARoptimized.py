#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
downloadNISARoptimized - fetch NISAR products keeping only what the GrIMP tools read.

    downloadNISARoptimized --urls gcovUrls.c030 --outputDir DIR --factorDir DIR

A NISAR GCOV is ~3 GB and carries ~280 datasets; geomosaic reads five of them, and
numberOfLooks alone is 26% of the file. This fetches only the byte ranges the kept datasets
occupy, over parallel HTTP range requests, writes a slim HDF5 with the same internal paths
(so geomosaic reads it unchanged), converts the two big float bands to float16, and stores
rtcGammaToSigmaFactor once per track/frame/grid so later cycles reuse it.

Measured on one Antarctic granule, against 3.06 GB / 172 s for the whole product:
  first cycle  1.95 GB / 119 s   (the factor has to be fetched)
  later cycles 1.12 GB /  83 s   (the factor is reused)
Stored ~658 MB per granule plus one 239 MB fp16 factor per track/frame/grid.

Three things here are load bearing and look like details:

  * Per-request latency to the ASF endpoint is a flat ~2 s regardless of size, so cost is
    (number of requests) x 2 s. Reading the wanted chunks one at a time is ~6.8 hours per
    granule; they have to be coalesced into large runs.
  * Planning must use cache_type='blockcache'. fsspec's default single-buffer cache
    re-fetches as the chunk index is walked out of order: 259 s versus 16 s.
  * The presigned URL must be signed by a GET redirect. A HEAD-signed CloudFront URL 403s.

@author: ian
"""
import argparse
import asyncio
import glob
import hashlib
import json
import os
import socket
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

import aiohttp
import fsspec
import h5py
import numpy as np
import requests

from asfsearchdownload.helpers import myerror

# Exit codes, sysexits range, same contract as pullASF.
OK = 0
BADNAME = 65
FETCHFAIL = 68
VERIFYFAIL = 69
NOOUTPUT = 64

# Scratch. A sparse file's apparent size is the whole product but only the fetched pages are
# written: up to ~3.3 GB on a first cycle of a 5 GB product, ~1.1 GB once the factor is shared.
# At most repackWorkers + 1 are on disk at once (main() holds the fetch back while the repacks
# catch up).
SHM_DIR = '/dev/shm'
SCRATCH_GB_PER_GRANULE = 4.0
# tmpfs pages are RAM, so /dev/shm is only used if this much is left over for the repacks.
SHM_MEM_RESERVE_GB = 4.0
# A scratch name without the pid@host tag comes from a version before the tag existed; only
# its age can show that the run which wrote it is dead.
UNTAGGED_STALE_SECONDS = 86400

# Attributes that carry HDF5 object references into the destination file. Copying them
# verbatim leaves pointers to datasets that either do not exist or now sit at a different
# address, so a reader that consults dimension scales can silently get the wrong axis.
REFERENCE_ATTRS = ('DIMENSION_LIST', 'REFERENCE_LIST', 'CLASS', 'NAME')

# float16 limits. Both ends matter and in different ways: below the subnormal floor a value
# flushes to zero, above the ceiling it casts to inf, and geomosaic DROPS both - so either end
# silently removes pixels rather than perturbing them, which no dB tolerance would catch.
F16MAX = 65504.0
F16MINNORMAL = 6.104e-5

# Per-product descriptor. GCOV is the only one implemented; the shape is here so RSLC, RUNW
# and ROFF can be added without touching the fetch/repack machinery.
#   drop       - dataset paths (or group prefixes) never transferred
#   fp16       - datasets converted to float16 on repack
#   shared     - dataset stored once per (track, frame, grid) and referenced by every cycle
#   gridFrom   - dataset whose raster shape and coordinates define the grid key
PRODUCTS = {
    'GCOV': {
        'root': '/science/LSAR/GCOV',
        'grid': '/science/LSAR/GCOV/grids/frequency{f}',
        'radarGrid': '/science/LSAR/GCOV/metadata/radarGrid',
        # The big radarGrid cubes are dropped, and deliberately so: they are built at NATIVE
        # doppler, i.e. SQUINTED. Measured on cycle-30 frames, (heading of losUnitVector -
        # heading of alongTrackUnitVector) - 90 deg gives +1.44 to +1.47 deg, matching what
        # mosaicSource/CLAUDE.md records for the RUNW cubes. The axis is named
        # zeroDopplerAzimuthTime, but the AXIS being zero-doppler says nothing about the
        # VECTORS, which are not.
        #
        # GrIMP needs these quantities at ZERO doppler, and its C already computes heading,
        # incidence and range from the same handful of numbers a geodat carries. Those are
        # all kept and cost nothing: metadata/orbit + metadata/attitude total 9.5 kB, and
        # sourceData has slantRangeStart, slantRangeSpacing, zeroDopplerTimeSpacing and the
        # dimensions. So 177 MB/granule of wrong-geometry cubes buys nothing.
        #
        # incidenceAngle is the exception and is KEPT (15.5 MB, 0.3%) because gcovMosaic.c
        # reads it today via readIncidenceCube; it is second-order immune to squint
        # (delta^2/2*tan(theta) ~ 0.02 deg) so it is safe as delivered. It can be dropped
        # once the C computes psi from the orbit instead.
        'dropCubes': ('elevationAngle', 'groundTrackVelocity', 'slantRange',
                      'zeroDopplerAzimuthTime', 'losUnitVectorX', 'losUnitVectorY',
                      'losUnitVectorZ', 'alongTrackUnitVectorX', 'alongTrackUnitVectorY',
                      'alongTrackUnitVectorZ'),
        'dropGrids': ('numberOfLooks', 'inputDataExceptionMask'),
        'shared': 'rtcGammaToSigmaFactor',
    },
}


def downloadNISARoptimizedArgs():
    '''Handle command line args'''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mFetch NISAR products keeping only the datasets the GrIMP '
                    'tools read, as float16, with a shared RTC factor.\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('--urls', type=str, required=True,
                        help='file with one granule URL per line')
    parser.add_argument('--outputDir', type=str, default='.',
                        help='where slim granules are written [.]')
    parser.add_argument('--factorDir', type=str, default=None,
                        help='where shared RTC factors live [<outputDir>/factors]')
    parser.add_argument('--product', type=str, default='GCOV',
                        choices=sorted(PRODUCTS), help='NISAR product type [GCOV]')
    parser.add_argument('--polarization', type=str, default='HHHH',
                        help='covariance term to keep [HHHH]')
    parser.add_argument('--frequency', type=str, default='A', help='A or B [A]')
    parser.add_argument('--float32', action='store_true',
                        help='keep the float bands at full precision (default float16)')
    parser.add_argument('--noShareFactor', action='store_true',
                        help='store the RTC factor in each granule instead of sharing it')
    parser.add_argument('--connections', type=int, default=16,
                        help='parallel range requests per granule [16]')
    parser.add_argument('--limit', type=float, default=19.0,
                        help='MB/s cap, 0 for none [19]')
    parser.add_argument('--repackWorkers', type=int, default=3,
                        help='repack processes overlapping the next download [3]')
    parser.add_argument('--tmpDir', type=str, default=None,
                        help='where the sparse scratch files go [/dev/shm if it and free '
                             'RAM are big enough, else <outputDir>/.scratch]')
    parser.add_argument('--maxGranules', type=int, default=0,
                        help='stop after this many, 0 for all [0]')
    parser.add_argument('--check', action='store_true',
                        help='report what would be fetched and exit')
    parser.add_argument('--local', action='store_true',
                        help='treat --urls as LOCAL granule paths and convert them in place of '
                             'downloading. Originals are never touched, so --outputDir must '
                             'differ from where they live.')
    return parser.parse_args()


def granuleStem(url):
    '''Basename of the granule, without .h5.'''
    return os.path.basename(url).replace('.h5', '')


def trackFrameKey(stem):
    '''(track, direction, frame, mode, flag) from a NISAR L2 granule name.

    Deliberately excludes the cycle and the start time - those are exactly what a shared
    factor spans - but keeps the mode and the A/M flag, because cycle 030 holds a
    ..._110_A_140_4005_SHSH_A_... and a ..._110_A_140_4005_SHSH_M_... that are different
    acquisitions, not _001/_002 reprocessings of one.

    Two granules of the same position with different start times will share a factor under
    this key. That is the same assumption as cross-cycle reuse and is guarded the same way,
    by the grid hash: a position that changes grid (partial frame in one cycle, full in the
    next) gets its own factor, and geomosaic re-checks the raster size at read time anyway.
    '''
    f = stem.split('_')
    if len(f) < 13 or f[3] != 'GCOV':
        return None
    return f[5], f[6], f[7], f[9], f[10]


def gridKey(h, gridPath):
    '''Short hash of the raster geometry, so a factor is never reused across a grid change.

    173 of 657 cycle-030 granules are partial frames, so a position that arrives partial in
    one cycle and full in the next has a different grid. Reading a shared factor at the same
    pixel window would silently misregister the gamma->sigma conversion.
    '''
    x, y = h[f'{gridPath}/xCoordinates'], h[f'{gridPath}/yCoordinates']
    epsg = int(h[f'{gridPath}/projection'][()])
    sig = '{}_{}_{:.3f}_{:.3f}_{:.6f}_{:.6f}_{}'.format(
        y.shape[0], x.shape[0], x[0], y[0],
        (x[-1] - x[0]) / (x.shape[0] - 1), (y[-1] - y[0]) / (y.shape[0] - 1), epsg)
    return hashlib.sha1(sig.encode()).hexdigest()[:10], sig


def resolvePresigned(url, session):
    '''Follow the redirect chain to the presigned URL.

    The chain runs ASF -> Earthdata OAuth -> back to ASF -> CloudFront, so the first Location
    is the login page, not the signed URL: it has to be followed to the end, with netrc auth
    supplied along the way.

    A one-byte GET, not a HEAD. The redirect target is signed for the method that asked for
    it, and a HEAD-signed CloudFront URL returns 403 on every subsequent range request.
    '''
    r = session.get(url, allow_redirects=True, stream=True,
                    headers={'Range': 'bytes=0-0'})
    try:
        if r.status_code not in (200, 206):
            myerror(f'downloadNISARoptimized: {r.status_code} resolving {url}')
        if 'urs.earthdata.nasa.gov' in r.url:
            myerror('downloadNISARoptimized: stopped at Earthdata login - check ~/.netrc '
                    'has a urs.earthdata.nasa.gov entry and the app is authorized')
        return r.url
    finally:
        r.close()


def wanted(path, desc, grid, otherGrid, polarization, frequency):
    '''True if this dataset should survive into the slim product.'''
    if path.startswith(otherGrid):
        return False
    base = os.path.basename(path)
    if path.startswith(grid) and base in desc['dropGrids']:
        return False
    if path.startswith(desc['radarGrid']) and base in desc['dropCubes']:
        return False
    # unused covariance terms: four-letter polarimetric names other than the one kept
    if path.startswith(grid) and len(base) == 4 and base != polarization \
            and all(c in 'HV' for c in base):
        return False
    return True


def keptDatasets(h, desc, polarization, frequency, shareFactor):
    '''Every dataset to transfer, as (path, toFloat16).

    A blacklist, not a whitelist. Everything outside the three big rasters is 4.2 MB of a
    5.1 GB granule - 0.08% - and keeping it retires a whole class of risk: the scalar
    projection datasets whose attributes carry the EPSG (dropping the radarGrid one fails
    silently, leaving psi at zero), identification/boundingPolygon which the mosaic workflow
    reads to build release shapefiles, and listOfCovarianceTerms / processingInformation /
    sourceData without which nisarhdf cannot open the file at all.

    Split by STORAGE, not by size. Chunked datasets are fetched as byte ranges. Everything
    else - contiguous scalars, and especially variable-length strings - is read here and
    carried in memory, because a VL dataset stores only pointers at its own offset while the
    strings live in a global heap collection elsewhere in the file. Range-fetching such a
    dataset yields "bad global heap collection signature" on read, which is how this was
    found.
    '''
    grid = desc['grid'].format(f=frequency)
    otherGrid = desc['grid'].format(f='B' if frequency == 'A' else 'A')
    big = {f'{grid}/{polarization}', f'{grid}/{desc["shared"]}'}
    chunked, small, groups = [], {}, {}

    def visit(name, obj):
        path = '/' + name
        if isinstance(obj, h5py.Group):
            if obj.attrs and wanted(path, desc, grid, otherGrid, polarization, frequency):
                groups[path] = dict(obj.attrs)
            return
        if not isinstance(obj, h5py.Dataset):
            return
        if not wanted(path, desc, grid, otherGrid, polarization, frequency):
            return
        if path == f'{grid}/{desc["shared"]}' and shareFactor:
            return                      # handled separately, into the shared factor file
        if obj.chunks is not None:
            chunked.append((path, path in big))
        else:
            small[path] = (obj[()], {k: v for k, v in obj.attrs.items()
                                     if k not in REFERENCE_ATTRS}, obj.dtype)

    h.visititems(visit)
    return chunked, small, groups


def planRanges(fh, h, chunked):
    '''Byte ranges for the chunked datasets, plus whatever the open already touched.

    Extents, not reads: asking HDF5 where a chunk lives costs nothing, while reading a
    20 MB incidence cube over HTTP in small pieces cost ~44 s per granule on its own.
    '''
    want = []
    for path, _ in chunked:
        d = h[path]
        for i in range(d.id.get_num_chunks()):
            ci = d.id.get_chunk_info(i)
            want.append((ci.byte_offset, ci.byte_offset + ci.size))
    return want, list(fh._grimpRanges)


def coalesce(ranges, gap, size):
    '''Merge ranges into runs, tolerating gaps smaller than a request is worth.'''
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
                        # A 200 means the server ignored the Range header and is sending the
                        # whole product; writing that at offset a corrupts the file silently.
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


def copyDataset(src, dst, path, toFloat16, step=4096):
    '''Copy one dataset, converting to float16 if asked.

    Raw chunk copy when the type is unchanged, so nothing is decompressed and the result is
    bit-identical to the archive.
    '''
    if toFloat16 and src.dtype == np.float32:
        d = dst.create_dataset(path, shape=src.shape, dtype='f2', chunks=src.chunks,
                               compression=src.compression,
                               compression_opts=src.compression_opts, shuffle=src.shuffle)
        nClamped = 0
        for y in range(0, src.shape[0], step):
            block = src[y:y + step]
            # Clamp before casting. A finite float32 above float16's 65504 ceiling (+48 dB)
            # casts to inf, and reduceGCOV drops isinf - so the pixel silently leaves the
            # mosaic. Saturating is the more honest failure: such values are layover or
            # specular return, not usable signal. Measured rate is tiny (105 and 28 pixels of
            # ~590 M on the two frames that triggered it, 2e-5%) but it should not be silent.
            # NaN and genuine inf are left alone - only finite overflow is clamped.
            over = np.isfinite(block) & (np.abs(block) > F16MAX)
            if over.any():
                nClamped += int(over.sum())
                block = np.where(over, np.sign(block) * F16MAX, block)
            # .astype('f2') FIRST, deliberately. Assigning the float32 array straight into an
            # f2 dataset looks tidier but makes HDF5 do the conversion, which is its generic
            # software path: measured 2.433 s against 1.059 s per 16 Mpx block. numpy uses the
            # F16C instruction (0.175 s of that 1.059 s; the rest is gzip). Same asymmetry the
            # read side hits - see gcovMosaic.c's float16 fast path.
            d[y:y + step] = block.astype('f2')
        d.attrs['grimpClampedToF16Max'] = nClamped
    elif src.chunks is not None:
        d = dst.create_dataset(path, shape=src.shape, dtype=src.dtype, chunks=src.chunks,
                               compression=src.compression,
                               compression_opts=src.compression_opts,
                               shuffle=src.shuffle, fletcher32=src.fletcher32)
        for i in range(src.id.get_num_chunks()):
            ci = src.id.get_chunk_info(i)
            filt, raw = src.id.read_direct_chunk(ci.chunk_offset)
            d.id.write_direct_chunk(ci.chunk_offset, raw, filter_mask=filt)
    else:
        d = dst.create_dataset(path, data=src[()])
    for k, v in src.attrs.items():
        if k not in REFERENCE_ATTRS:
            d.attrs[k] = v
    return d


def reattachScales(dst, gridPath, polarization):
    '''Rebuild dimension scales over the datasets that survived.

    The source REFERENCE_LIST names five rasters; a slim product holds fewer, so copying it
    verbatim advertises attachments to datasets that are no longer there.
    '''
    try:
        xc, yc = dst[f'{gridPath}/xCoordinates'], dst[f'{gridPath}/yCoordinates']
        xc.make_scale('xCoordinates')
        yc.make_scale('yCoordinates')
        for name in (polarization, 'mask'):
            p = f'{gridPath}/{name}'
            if p in dst and dst[p].ndim == 2:
                dst[p].dims[0].attach_scale(yc)
                dst[p].dims[1].attach_scale(xc)
    except Exception as e:
        print(f'  warning: could not reattach dimension scales ({e})', flush=True)


def repack(job, desc, polarization, frequency, toFloat16):
    '''Build the slim granule (and the shared factor, if it is not already there).'''
    grid = desc['grid'].format(f=frequency)
    sharedPath = f'{grid}/{desc["shared"]}'
    outPath, tmp = job['outPath'], job['outPath'] + '.partial'
    with h5py.File(job['sparse'], 'r') as s:
        with h5py.File(tmp, 'w') as d:
            for path, big in job['chunked']:
                if path == sharedPath and job['shareFactor']:
                    continue            # lives in the shared file, not in the granule
                copyDataset(s[path], d, path, toFloat16 and big)
            for path, (data, attrs, dtype) in job['small'].items():
                ds = d.create_dataset(path, data=data, dtype=dtype)
                for k, v in attrs.items():
                    ds.attrs[k] = v
            for path, attrs in job['groups'].items():
                g = d.require_group(path)
                for k, v in attrs.items():
                    g.attrs[k] = v
            reattachScales(d, grid, polarization)
        if job['factorPath'] is not None and not os.path.exists(job['factorPath']):
            ftmp = job['factorPath'] + '.partial'
            with h5py.File(ftmp, 'w') as fd:
                copyDataset(s[sharedPath], fd, sharedPath, toFloat16)
                fd.attrs['gridKey'] = job['gridKey']
                fd.attrs['gridSignature'] = job['gridSig']
            os.replace(ftmp, job['factorPath'])   # write once, never rewritten in place
    os.replace(tmp, outPath)


def verifySlim(outPath, factorPath, desc, polarization, frequency):
    '''Gates that must hold before an original could ever be deleted.'''
    grid = desc['grid'].format(f=frequency)
    problems = []
    with h5py.File(outPath, 'r') as h:
        for need in (f'{grid}/{polarization}', f'{grid}/mask', f'{grid}/xCoordinates',
                     f'{grid}/projection', f'{desc["radarGrid"]}/incidenceAngle',
                     f'{desc["radarGrid"]}/projection'):
            if need not in h:
                problems.append(f'missing {need}')
        if f'{grid}/{polarization}' in h:
            g = h[f'{grid}/{polarization}']
            m = h[f'{grid}/mask'] if f'{grid}/mask' in h else None
            n = min(4096, g.shape[0])
            a = g[:n]
            if m is not None:
                inMask = (m[:n] == 1)
                v = a[inMask & np.isfinite(a) & (a > 0)]
                # Both float16 ends, symmetrically. Under the floor a value flushes to zero;
                # over the ceiling it casts to inf. geomosaic drops both, so each is a
                # coverage change rather than a value error, and neither shows up in a dB
                # comparison. Checking only the floor is how the overflow end went unnoticed.
                # Threshold sits just above the SUBNORMAL floor (5.96e-8), not above the min
                # normal. Between the two, float16 still represents the value with reduced
                # precision - it is not lost - so flagging there is a false positive. A first
                # cut at 1e-6 flagged a granule whose true minimum round-tripped at 0.000 dB
                # with zero pixels below the floor, and a failure line that cries wolf gets
                # ignored.
                if v.size and v.min() < 1e-7:
                    problems.append(f'min valid {v.min():.3g} at the float16 floor '
                                    f'({F16MINNORMAL:.2e} normal, 5.96e-08 subnormal)')
                nInf = int((inMask & np.isinf(a)).sum())
                if nInf:
                    problems.append(f'{nInf} inf pixels inside the valid mask '
                                    f'(float16 overflow should now be clamped)')
        if factorPath is not None:
            key, _ = gridKey(h, grid)
            with h5py.File(factorPath, 'r') as f:
                if f.attrs.get('gridKey') != key:
                    problems.append('factor gridKey does not match the granule')
    return problems


def scratchPath(tmpDir, stem):
    '''Sparse scratch file for one granule, tagged with this run's pid and host so the
    startup sweep can tell a leaked file from one another run is still using.'''
    return os.path.join(tmpDir, f'{stem}.{os.getpid()}@{socket.gethostname()}.sparse')


def pidAlive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweepLeakedScratch(tmpDir):
    '''Remove scratch files whose run is dead and return how many.

    A SIGKILL skips the finally that removes a scratch file, so every interrupted run leaves
    one behind, and they accumulated until tmpDir was full. A file is only removed when its
    writer is known to be gone: a dead pid on this host, or an untagged name older than
    UNTAGGED_STALE_SECONDS. Anything else may belong to a concurrent run (or to
    downloadRSLCSubset, whose scratch matches the same glob).
    '''
    host = socket.gethostname()
    removed = 0
    for f in glob.glob(os.path.join(tmpDir, 'NISAR_*.sparse')):
        base = os.path.basename(f)[:-len('.sparse')]
        try:
            if '@' in base:
                stemPid, fileHost = base.rsplit('@', 1)
                pid = stemPid.rsplit('.', 1)[-1]
                if fileHost != host or not pid.isdigit() or pidAlive(int(pid)):
                    continue
            elif time.time() - os.path.getmtime(f) < UNTAGGED_STALE_SECONDS:
                continue
            os.remove(f)
            removed += 1
        except OSError:
            pass
    return removed


def freeGb(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def memAvailableGb():
    '''MemAvailable from /proc/meminfo in GB, or None where there is none (macOS).'''
    try:
        with open('/proc/meminfo') as fp:
            for line in fp:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024 / 1e9
    except (OSError, ValueError, IndexError):
        pass
    return None


def chooseTmpDir(args):
    '''Return the scratch directory, sweeping leaked files out of it first.

    An explicit --tmpDir is used as given. Otherwise /dev/shm is used when both its free space
    and the free RAM cover the in-flight scratch - tmpfs pages are RAM, and its free space is
    only a cap (half of RAM by default), so on a small machine filling it pushes the repacks
    into swap - and <outputDir>/.scratch on disk when not, which is the laptop case (macOS has
    no /dev/shm at all).
    '''
    needGb = (args.repackWorkers + 1) * SCRATCH_GB_PER_GRANULE
    candidates = []
    if args.tmpDir is not None:
        candidates.append(args.tmpDir)
    else:
        if os.path.isdir(SHM_DIR):
            candidates.append(SHM_DIR)
        candidates.append(os.path.join(args.outputDir, '.scratch'))
    for tmpDir in candidates:
        os.makedirs(tmpDir, exist_ok=True)
        nSwept = sweepLeakedScratch(tmpDir)
        if nSwept:
            print('removed %d leaked scratch file(s) from %s' % (nSwept, tmpDir), flush=True)
        free = freeGb(tmpDir)
        if tmpDir == SHM_DIR and args.tmpDir is None:
            mem = memAvailableGb()
            if free < needGb or (mem is not None and mem < needGb + SHM_MEM_RESERVE_GB):
                print('%s too small (%.1f GB free, %s GB RAM available, need %.1f + %.1f '
                      'reserve); using disk scratch' % (
                          tmpDir, free, 'unknown' if mem is None else '%.1f' % mem,
                          needGb, SHM_MEM_RESERVE_GB), flush=True)
                continue
        if free < needGb:
            myerror('downloadNISARoptimized: only %.1f GB free in %s, need %.1f GB for %d '
                    'granules in flight; point --tmpDir somewhere larger or lower '
                    '--repackWorkers' % (free, tmpDir, needGb, args.repackWorkers + 1))
        print('scratch %s (%.1f GB free)' % (tmpDir, free), flush=True)
        return tmpDir


def fetchOne(url, args, desc, session, claimed):
    '''Resolve, plan and fetch one granule into a sparse scratch file.

    `claimed` holds factor paths already being written by a repack still in flight. Without
    it the existence check races the pipeline: two granules of the same position back to
    back both see no factor on disk and both pay ~0.83 GB to fetch it.
    '''
    stem = granuleStem(url)
    if trackFrameKey(stem) is None:
        print(f'  skipping unparseable name {stem}', flush=True)
        return None
    outPath = os.path.join(args.outputDir, stem + '.h5')
    if os.path.exists(outPath):
        return None
    signed = resolvePresigned(url, session)
    fs = fsspec.filesystem('http')
    # blockcache, not the default: the chunk index is walked out of order and a single-buffer
    # cache re-fetches, turning a 16 s plan into 259 s.
    fh = fs.open(signed, 'rb', cache_type='blockcache', block_size=4 * 1024 * 1024)
    fh._grimpRanges = []
    inner = fh.cache.fetcher

    def record(a, b):
        fh._grimpRanges.append((a, b))
        return inner(a, b)

    fh.cache.fetcher = record
    h = h5py.File(fh, 'r')
    shareFactor = not args.noShareFactor
    chunked, small, groups = keptDatasets(h, desc, args.polarization, args.frequency,
                                          shareFactor)
    grid = desc['grid'].format(f=args.frequency)
    key, sig = gridKey(h, grid)
    tf = trackFrameKey(stem)
    factorPath = None
    if shareFactor:
        # Not named like a granule, and not in the granule directory: geomosaic's yaml globs
        # are name-anchored, and a factor swept up as an input aborts the whole tile.
        factorPath = os.path.join(
            args.factorDir,
            'rtcFactor_{}_{}_{}_{}_{}_{}.h5'.format(tf[0], tf[1], tf[2], tf[3], tf[4], key))
    needFactor = (shareFactor and not os.path.exists(factorPath)
                  and factorPath not in claimed)
    if needFactor:
        claimed.add(factorPath)
    if needFactor:
        chunked = chunked + [(f'{grid}/{desc["shared"]}', True)]
    want, meta = planRanges(fh, h, chunked)
    size = fh.size
    h.close()
    runs = coalesce(want + meta, 1 << 20, size)
    runBytes = sum(b - a for a, b in runs)
    print('  {}: {} runs, {:.2f} GB of {:.2f} GB{}'.format(
        stem[-34:], len(runs), runBytes / 1e9, size / 1e9,
        '' if needFactor else ' (factor reused)'), flush=True)
    if args.check:
        return None
    sparse = scratchPath(args.tmpDir, stem)
    if os.path.exists(sparse):
        os.remove(sparse)
    # MUST be the full product size. HDF5 records the expected EOF in the superblock, so a
    # file shorter than that is refused as truncated ("stored_eof = ..."). Sizing it to the
    # fetched span instead was tried and broke every repack.
    #
    # This costs nothing: the file is sparse, and tmpfs charges only written pages - 176 GB of
    # apparent scratch held 2.1 GB real. The ENOSPC that killed 43 granules was NOT the sizing
    # but LEAKED scratch files: a SIGKILL skips the finally that removes them, and each one
    # retains its written pages, so a few interrupted runs pinned ~135 GB. The sweep at startup
    # is the actual fix.
    os.truncate(os.open(sparse, os.O_WRONLY | os.O_CREAT), size)
    t0 = time.time()
    got = asyncio.run(fetchRuns(signed, runs, sparse, args.connections,
                                args.limit * 1e6 if args.limit else 0))
    print('    fetched {:.2f} GB in {:.0f} s ({:.1f} MB/s)'.format(
        got / 1e9, time.time() - t0, got / (time.time() - t0) / 1e6), flush=True)
    return dict(stem=stem, sparse=sparse, outPath=outPath,
                factorPath=factorPath if needFactor else None,
                linkPath=(os.path.join(args.factorDir, stem + '.h5')
                          if shareFactor else None),
                factorTarget=os.path.basename(factorPath) if shareFactor else None,
                chunked=chunked, small=small, groups=groups,
                gridKey=key, gridSig=sig, shareFactor=shareFactor)


def convertOne(path, args, desc, claimed):
    """Convert a local granule to a slim product. No network.

    Same repack as the fetch path, so the two cannot drift: the only difference is that the
    datasets are read from the granule on disk instead of from a sparse file assembled out of
    range requests. Originals are left alone - the caller checks outputDir is elsewhere - so a
    conversion can be verified before anything is deleted.
    """
    stem = granuleStem(path)
    if trackFrameKey(stem) is None:
        return None
    outPath = os.path.join(args.outputDir, stem + '.h5')
    if os.path.exists(outPath):
        return None
    grid = desc['grid'].format(f=args.frequency)
    with h5py.File(path, 'r') as h:
        pol = f'{grid}/{args.polarization}'
        if pol not in h:
            print(f'  skipping {stem[-34:]}: no {args.polarization}', flush=True)
            return None
        if h[pol].dtype == np.float16:
            print(f'  skipping {stem[-34:]}: already float16', flush=True)
            return None
        shareFactor = not args.noShareFactor
        chunked, small, groups = keptDatasets(h, desc, args.polarization, args.frequency,
                                              shareFactor)
        key, sig = gridKey(h, grid)
    tf = trackFrameKey(stem)
    factorPath = None
    if shareFactor:
        factorPath = os.path.join(
            args.factorDir,
            'rtcFactor_{}_{}_{}_{}_{}_{}.h5'.format(tf[0], tf[1], tf[2], tf[3], tf[4], key))
    needFactor = (shareFactor and not os.path.exists(factorPath)
                  and factorPath not in claimed)
    if needFactor:
        claimed.add(factorPath)
        chunked = chunked + [(f'{grid}/{desc["shared"]}', True)]
    elif not shareFactor:
        chunked = chunked + [(f'{grid}/{desc["shared"]}', True)]
    return dict(stem=stem, sparse=path, outPath=outPath,
                factorPath=factorPath if needFactor else None,
                linkPath=(os.path.join(args.factorDir, stem + '.h5')
                          if shareFactor else None),
                factorTarget=os.path.basename(factorPath) if shareFactor else None,
                chunked=chunked, small=small, groups=groups,
                gridKey=key, gridSig=sig, shareFactor=shareFactor, keepSource=True)


def repackOne(job, args, desc):
    '''Repack, verify, and leave the per-granule factor symlink. Runs in a worker process.'''
    try:
        repack(job, desc, args.polarization, args.frequency, not args.float32)
        if job['linkPath'] is not None:
            if os.path.lexists(job['linkPath']):
                os.remove(job['linkPath'])
            os.symlink(job['factorTarget'], job['linkPath'])
        problems = verifySlim(job['outPath'], job['linkPath'], desc,
                              args.polarization, args.frequency)
        # Clamping is recorded, not treated as a failure: it is expected at a rate of ~1e-5%
        # and saturating is the intended behaviour, but the count belongs in the manifest so
        # a frame with an unusual amount can be found later.
        with h5py.File(job['outPath'], 'r') as h:
            p = '{}/{}'.format(desc['grid'].format(f=args.frequency), args.polarization)
            clamped = int(h[p].attrs.get('grimpClampedToF16Max', 0)) if p in h else 0
        return job['stem'], os.path.getsize(job['outPath']), problems, clamped
    except Exception as e:
        # Never let one granule kill the run: a repack that fails here leaves no output, so
        # the granule is simply retried on the next pass, but it must be recorded because a
        # later granule may have skipped fetching a factor this one had claimed.
        for p in (job['outPath'] + '.partial', job['outPath']):
            if os.path.exists(p):
                os.remove(p)
        return job['stem'], 0, [f'repack failed: {e}'], 0
    finally:
        # keepSource marks a local conversion, where 'sparse' is the ORIGINAL granule. Only a
        # fetched sparse scratch file is ever removed.
        if not job.get('keepSource') and os.path.exists(job['sparse']):
            os.remove(job['sparse'])


def main():
    args = downloadNISARoptimizedArgs()
    desc = PRODUCTS[args.product]
    if args.factorDir is None:
        args.factorDir = os.path.join(args.outputDir, 'factors')
    os.makedirs(args.outputDir, exist_ok=True)
    os.makedirs(args.factorDir, exist_ok=True)
    if not args.local:
        args.tmpDir = chooseTmpDir(args)
    urls = [u.strip() for u in open(args.urls) if u.strip()]
    if args.local:
        bad = [u for u in urls
               if os.path.realpath(os.path.dirname(u)) == os.path.realpath(args.outputDir)]
        if bad:
            myerror('downloadNISARoptimized --local: outputDir is where the originals live, '
                    'which would overwrite them (%d of %d). Point it elsewhere.'
                    % (len(bad), len(urls)))
    if args.maxGranules:
        urls = urls[:args.maxGranules]
    print('{} granules, output {}, factors {}, {}'.format(
        len(urls), args.outputDir, args.factorDir,
        'float32' if args.float32 else 'float16'), flush=True)

    session = requests.Session()                # trust_env -> ~/.netrc
    manifest = os.path.join(args.outputDir, 'slimManifest.jsonl')
    nDone, nFail, t0 = 0, 0, time.time()
    # Repack of granule N overlaps the fetch of N+1: the fetch is bandwidth bound and the
    # repack is single-threaded CPU, so with a couple of workers the CPU disappears.
    with ProcessPoolExecutor(args.repackWorkers) as pool:
        pending, claimed = [], set()
        for url in urls:
            try:
                job = (convertOne(url, args, desc, claimed) if args.local
                       else fetchOne(url, args, desc, session, claimed))
            except Exception as e:
                print(f'  FAILED {granuleStem(url)}: {e}', flush=True)
                nFail += 1
                continue
            if job is not None:
                pending.append(pool.submit(repackOne, job, args, desc))
            if len(pending) > args.repackWorkers:
                # Hold the next fetch until a repack finishes, so the scratch on disk stays
                # within what chooseTmpDir checked for instead of growing with the backlog.
                wait(pending, return_when=FIRST_COMPLETED)
            for f in [p for p in pending if p.done()]:
                pending.remove(f)
                stem, nbytes, problems, clamped = f.result()
                nDone += 1
                if problems:
                    nFail += 1
                    print(f'  VERIFY FAILED {stem}: {"; ".join(problems)}', flush=True)
                with open(manifest, 'a') as fp:
                    fp.write(json.dumps(dict(stem=stem, bytes=nbytes, problems=problems,
                                             clamped=clamped,
                                             when=time.strftime('%Y-%m-%dT%H:%M:%S'))) + '\n')
        for f in pending:
            stem, nbytes, problems, clamped = f.result()
            nDone += 1
            if problems:
                nFail += 1
                print(f'  VERIFY FAILED {stem}: {"; ".join(problems)}', flush=True)
            with open(manifest, 'a') as fp:
                fp.write(json.dumps(dict(stem=stem, bytes=nbytes, problems=problems,
                                         clamped=clamped,
                                         when=time.strftime('%Y-%m-%dT%H:%M:%S'))) + '\n')
    print('\n{} granules in {:.0f} s, {} problems'.format(nDone, time.time() - t0, nFail),
          flush=True)
    return VERIFYFAIL if nFail else OK


if __name__ == '__main__':
    sys.exit(main())
