#!/usr/bin/env python3
# -*- coding: utf-8 -*-
'''
Fetch an S1 SAFE zip from ASF without the members matching a pattern (the
cross-pol), using HTTP range requests -- the download and reduceSentinel1 in
one step, at about half the network traffic. Kept members are copied
byte-for-byte (local header + compressed data), so the CRCs are ASF's own; a
new central directory is written for them. Output goes to <name>.zip.part and
is renamed only after every member passes a CRC check.

    remoteReduceS1 URL [--outputDir DIR] [--pattern hv] [--nConn N] [--limit MB/s]

Used by autoupdateS1 when its config sets remoteReduce: true. Part of the
asfSearchAndDownload package.
'''
import argparse
import io
import os
import struct
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import requests

from asfsearchdownload.networkLog import logTransfer, callerName, now

MB = 1 << 20
BLOCK = MB  # streaming read size; the rate limit is applied per block


class RateLimiter:
    ''' Caps the combined rate of all threads at bytesPerSec. Each block books
    the next free slot on a shared clock and sleeps until it, so the cap holds
    for any connection count and an idle spell is not paid back as a burst.
    The pause stalls the socket read, so TCP slows the sender too. '''

    def __init__(self, bytesPerSec):
        self.rate, self.next = bytesPerSec, time.monotonic()
        self.lock = threading.Lock()

    def consume(self, n):
        if not self.rate:
            return
        with self.lock:
            now = time.monotonic()
            self.next = max(self.next, now) + n / self.rate
            wait = self.next - now - n / self.rate
        if wait > 0:
            time.sleep(wait)


def resolvePresigned(url, session):
    ''' Follow ASF -> Earthdata -> CloudFront with a GET; return (url, size). '''
    r = session.get(url, allow_redirects=True, stream=True,
                    headers={'Range': 'bytes=0-0'})
    try:
        if r.status_code != 206:
            raise RuntimeError(f'{r.status_code} resolving {url}')
        if 'urs.earthdata.nasa.gov' in r.url:
            raise RuntimeError('stopped at Earthdata login - check ~/.netrc')
        return r.url, int(r.headers['Content-Range'].split('/')[-1])
    finally:
        r.close()


class RangeReader:
    ''' Fetches exact byte ranges, re-signing the URL if it expires. '''

    def __init__(self, url, session, limiter=None):
        self.origUrl, self.session = url, session
        self.limiter = limiter or RateLimiter(0)
        self.url, self.size = resolvePresigned(url, session)
        self.nRequests = 0

    def get(self, a, b):
        ''' Bytes [a, b). Requires a 206 of exactly the requested range. '''
        for attempt in range(5):
            try:
                r = self.session.get(self.url, timeout=(30, 120), stream=True,
                                     headers={'Range': f'bytes={a}-{b - 1}'})
                self.nRequests += 1
                with r:
                    if r.status_code == 403:
                        self.url, _ = resolvePresigned(self.origUrl, self.session)
                        raise IOError('403, re-signed')
                    if r.status_code != 206:
                        raise IOError(f'expected 206, got {r.status_code}')
                    if not r.headers.get('Content-Range', '').startswith(
                            f'bytes {a}-{b - 1}/'):
                        raise IOError(f'bad Content-Range {r.headers.get("Content-Range")}')
                    buf = bytearray()
                    for block in r.iter_content(BLOCK):
                        self.limiter.consume(len(block))
                        buf += block
                if len(buf) != b - a:
                    raise IOError(f'short read {len(buf)} != {b - a}')
                return bytes(buf)
            except (IOError, requests.RequestException) as exc:
                if attempt == 4:
                    raise
                print(f'  retry {a}-{b}: {exc}', file=sys.stderr)
                time.sleep(2 * (attempt + 1))


class RemoteFile(io.RawIOBase):
    ''' Minimal seekable file over RangeReader, enough for zipfile to parse
    the end record and central directory (a few requests). '''

    def __init__(self, reader):
        self.reader, self.pos = reader, 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        base = {0: 0, 1: self.pos, 2: self.reader.size}[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, buf):
        n = min(len(buf), self.reader.size - self.pos)
        if n <= 0:
            return 0
        buf[:n] = self.reader.get(self.pos, self.pos + n)
        self.pos += n
        return n


def planSpans(infos, cdStart):
    ''' Raw span [start, end) of each member: its local header up to the next
    member's header (so any data descriptor comes along). '''
    ordered = sorted(infos, key=lambda z: z.header_offset)
    ends = [z.header_offset for z in ordered[1:]] + [cdStart]
    return [(z, z.header_offset, e) for z, e in zip(ordered, ends)]


def planWork(keep, gap, chunk):
    ''' Group kept spans into requests: merge spans separated by < gap bytes
    (fetch and discard the gap), then split runs into <= chunk pieces. '''
    runs = []
    for _, a, b in keep:
        if runs and a - runs[-1][1] < gap:
            runs[-1][1] = b
        else:
            runs.append([a, b])
    return [(s, min(b, s + chunk)) for a, b in runs for s in range(a, b, chunk)]


def fetchReduced(url, outputDir, pattern='hv', nConn=1, limit=0., gap=4 * MB,
                 chunk=64 * MB, program=None):
    '''
    Fetch url's zip minus the members whose names contain pattern into
    outputDir; return the final zip path. Raises on any failure, leaving at
    most <name>.zip.part behind. limit is the combined download cap in MB/s
    (2**20 bytes, as aria2c's M) over all nConn connections; 0 = none.
    '''
    t0 = time.time()
    start = now()
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_maxsize=max(10, nConn))
    session.mount('https://', adapter)
    reader = RangeReader(url, session, RateLimiter(limit * MB))
    with zipfile.ZipFile(RemoteFile(reader)) as rz:
        infos, cdStart = rz.infolist(), rz.start_dir
    spans = planSpans(infos, cdStart)
    keep = [s for s in spans if pattern not in s[0].filename]
    tPlan = time.time() - t0

    # Map source spans to contiguous output offsets.
    outOffset, pos = {}, 0
    for z, a, b in keep:
        outOffset[a] = pos
        pos += b - a
    dataEnd = pos
    work = planWork(keep, gap, chunk)
    name = os.path.basename(url)
    partPath = os.path.join(outputDir, f'{name}.part')
    fd = os.open(partPath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    def one(job):
        a, b = job
        buf = reader.get(a, b)
        for _, sa, sb in keep:  # write the kept parts of this piece
            lo, hi = max(a, sa), min(b, sb)
            if lo < hi:
                os.pwrite(fd, buf[lo - a:hi - a], outOffset[sa] + lo - sa)
        return b - a

    try:
        with ThreadPoolExecutor(nConn) as pool:
            fetched = sum(pool.map(one, work))
    finally:
        os.close(fd)
    tFetch = time.time() - t0 - tPlan

    # Central directory for the kept members at their new offsets.
    with open(partPath, 'r+b') as f:
        f.seek(dataEnd)
        f.truncate()
        with zipfile.ZipFile(f, 'w') as zout:
            for z, a, _ in keep:
                z.header_offset = outOffset[a]
                z.extra = zipfile._strip_extra(z.extra, (1,))  # zip64 redone
                zout.filelist.append(z)
                zout.NameToInfo[z.filename] = z

    # Verify: each local header names its member, then every CRC.
    with open(partPath, 'rb') as f:
        for z, a, _ in keep:
            f.seek(outOffset[a])
            hdr = f.read(30)
            nLen = struct.unpack('<H', hdr[26:28])[0]
            if hdr[:4] != b'PK\x03\x04' or f.read(nLen).decode() != z.filename:
                raise RuntimeError(f'local header mismatch for {z.filename}')
    with zipfile.ZipFile(partPath) as zc:
        bad = zc.testzip()
    if bad is not None:
        raise RuntimeError(f'CRC failure in {bad}; leaving {partPath}')
    tVerify = time.time() - t0 - tPlan - tFetch
    finalPath = os.path.join(outputDir, name)
    os.replace(partPath, finalPath)
    print(f'{name}: kept {len(keep)}/{len(infos)} members, '
          f'{dataEnd / MB:.0f} of {reader.size / MB:.0f} MB '
          f'(fetched {fetched / MB:.0f} MB in {len(work)} pieces, '
          f'{reader.nRequests} requests)\n'
          f'  plan {tPlan:.1f} s  fetch {tFetch:.1f} s '
          f'({fetched / MB / tFetch:.1f} MB/s)  verify {tVerify:.1f} s',
          flush=True)
    logTransfer(program or f'{callerName()}/remoteReduceS1', name, fetched,
                start, now(), connections=nConn)
    return finalPath


def main():
    p = argparse.ArgumentParser(
        description='\n\n\033[1mDownload an S1 SLC zip from ASF without its '
        'cross-pol members (HTTP range requests)\033[0m\n\n',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Part of the asfSearchAndDownload package.')
    p.add_argument('url', help='ASF download URL of the granule zip')
    p.add_argument('--outputDir', default='.', help='Output directory [.]')
    p.add_argument('--pattern', default='hv',
                   help='Drop members whose names contain this [hv]')
    p.add_argument('--nConn', type=int, default=1,
                   help='Parallel connections [1]')
    p.add_argument('--limit', type=float, default=0.,
                   help='Combined download cap in MB/s, 0 = none [0]')
    a = p.parse_args()
    fetchReduced(a.url, a.outputDir, a.pattern, a.nConn, a.limit)


if __name__ == '__main__':
    main()
