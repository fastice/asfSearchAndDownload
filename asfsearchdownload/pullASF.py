#!/usr/bin/env python3
"""
pullASF - fetch one interferometric pair's granules into the archive.

    pullASF URL [URL ...] --archiveDir DIR

Ensures each granule is present, complete and reduced under
archiveDir/<YYYY-MM>/, then prints the paths. Written for queue lines of the
form

    pullASF <url1> <url2> --archiveDir <A> && runS1interferogram <f1> <f2> ...

so a failure short-circuits the interferogram. Downloading and reducing are
done by the existing autoupdateS1/reduceSentinel1 helpers; what this adds is
idempotency, an archive-wide single-download slot, and an exit-code contract.

Filing into the assembly tree is deliberately NOT done: the phase workflow
reads the zip directly, one frame at a time. A later autoupdateS1 sweep will
file these zips (renaming .zip -> .zip.1) in its own time.

Why one call per pair rather than one per granule: with a single download slot,
chaining two calls interleaves at the granule level, so a job can win the slot
for its first image early and its second last -- nothing starts running until
nearly every download has finished. Taking the slot once per pair means each
job leaves with something runnable and its compute overlaps the next fetch.
"""
import argparse
import contextlib
import os
import random
import socket
import sys
import time
import urllib.parse

import yaml

from asfsearchdownload import autoupdateS1
from asfsearchdownload.fileS1 import computeTrack, zipComplete
from asfsearchdownload.reduceSentinel1 import remove_files_from_zip

# Exit codes. Kept in the sysexits range so they cannot be confused with
# runS1interferogram's (1, or 128+n for a signal) in exitStatus.log.
OK = 0
BADNAME = 65        # not a parseable Sentinel-1 granule URL
DUPLICATE = 66      # another product id for this acquisition is already held
LOCKBUSY = 67       # download slot held elsewhere and the wait expired
DLFAIL = 68         # download failed after maxAttempts
REDUCEFAIL = 69     # reduce left the zip unsound
EXCLUDED = 70       # track is in tracksToExclude
NOARCHIVE = 64      # no archiveDir given or discoverable

LOCKNAME = '.pullASF.download.lock'
# One hold is a download plus a reduce, ~3 min, or ~10 min if all the retries
# fire. 45 min only ever reclaims a genuinely dead holder.
STALESECONDS = 45 * 60


def pullArgs():
    ''' Command line parser. '''
    parser = argparse.ArgumentParser(
        description='Fetch Sentinel-1 granules into the archive '
                    '(download + reduce), one download at a time.',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('urls', metavar='URL', type=str, nargs='+',
                        help='granule URL(s); a whole pair is fetched under a '
                        'single acquisition of the download slot')
    parser.add_argument('--archiveDir', type=str, default=None,
                        help='archive root holding the YYYY-MM subdirs')
    parser.add_argument('--config', type=str, default=None,
                        help='autoupdate.yaml to take archiveDir/reducePattern '
                        'from [<archiveDir>/autoupdate.yaml]')
    parser.add_argument('--reducePattern', type=str, default=None,
                        help="entries matching this substring are stripped "
                        "from the zip [config reducePattern, else 'hv']")
    parser.add_argument('--noReduce', action='store_true', default=False,
                        help='download only, leaving the zip full size')
    parser.add_argument('--maxAttempts', type=int, default=3,
                        help='download attempts per granule [3]')
    parser.add_argument('--waitMinutes', type=int, default=120,
                        help='how long to wait for the download slot. Covers '
                        'the whole queue ahead of you, not one download [120]')
    parser.add_argument('--allowDuplicate', action='store_true', default=False,
                        help='fetch even when another product id of the same '
                        'acquisition is already held')
    parser.add_argument('--allowExcludedTrack', action='store_true',
                        default=False,
                        help='fetch even if the track is in tracksToExclude')
    parser.add_argument('--check', action='store_true', default=False,
                        help='report what would happen, change nothing')
    return parser.parse_args()


def granuleName(url):
    ''' Granule basename from a URL, with any query string dropped (ASF vends
    ?token= forms). None if it does not look like a Sentinel-1 zip. '''
    name = os.path.basename(urllib.parse.urlparse(url).path)
    if not name.startswith('S1') or '.zip' not in name:
        return None
    return name


def archiveConfig(archiveDir, configFile=None):
    ''' The archive's own autoupdate.yaml as a dict, or {}. Never fatal: the
    config that governs an archive lives in the archive, so changing
    reducePattern or the exclusion list takes effect without regenerating
    queues - but pullASF still works on an archive that has none. '''
    path = configFile or os.path.join(archiveDir or '.', 'autoupdate.yaml')
    try:
        with open(path) as fp:
            return yaml.safe_load(fp) or {}
    except Exception:
        return {}


def excludedTracks(config):
    ''' Set of track numbers the archive does not hold, from tracksToExclude
    ("114 143 155"). '''
    raw = config.get('tracksToExclude', '')
    if isinstance(raw, str):
        raw = raw.split()
    tracks = set()
    for value in raw or []:
        try:
            tracks.add(int(value))
        except (TypeError, ValueError):
            pass
    return tracks


def trackOf(name):
    ''' Relative orbit for a granule name, or None if it will not parse. '''
    try:
        pieces = os.path.basename(name).split('_')
        return computeTrack(int(pieces[7]), pieces[0])
    except (IndexError, ValueError, KeyError):
        return None


@contextlib.contextmanager
def downloadSlot(archiveDir, waitMinutes, present, quiet=False):
    '''
    The archive-wide single-download slot, as a context manager yielding True
    when acquired.

    Polls two conditions and takes whichever comes first: the slot falling
    free, or `present()` reporting that everything we wanted has appeared -
    someone else fetched it, and we can leave without ever taking the slot.
    That second exit is what keeps the waiter pool shrinking rather than every
    waiter re-contending on each release.

    Acquisition is autoupdateS1._tryLock: a single O_CREAT|O_EXCL open, which
    the NFS server (v4.2 here) resolves atomically, so of N machines racing
    exactly one wins and the rest get EEXIST. Jittered polling keeps them from
    waking in lockstep.
    '''
    lockPath = os.path.join(archiveDir, LOCKNAME)
    deadline = time.time() + waitMinutes * 60
    acquired = False
    try:
        while True:
            if present():
                break
            acquired = autoupdateS1._tryLock(lockPath,
                                             STALESECONDS / 3600.0)
            if acquired:
                break
            if time.time() >= deadline:
                break
            if not quiet:
                print(f'waiting for the download slot ({LOCKNAME})')
            time.sleep(random.uniform(1.0, 3.0))
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lockPath)
            except OSError:
                pass


def fetchOne(url, name, archiveDir, reducePattern, maxAttempts, noReduce):
    ''' Download and reduce one granule, already known to be absent and to be
    running under the download slot. Returns (code, path|None). '''
    monthDir = autoupdateS1.monthDirFor(archiveDir, name)
    # --xferDir is not optional polish: ariaDownload's default scan starts with
    # '.', which is this month dir, and would rename an existing <name>.zip.1
    # back to .zip - un-filing a processed granule. Passing --xferDir replaces
    # that list rather than appending to it. (--noRename is NOT the fix: it
    # takes the same branch, skips both the move and the download, and leaves
    # no file at all.)
    zipPath = autoupdateS1.downloadOne(
        url, monthDir, maxAttempts,
        extraArgs=('--xferDir', archiveDir))
    if zipPath is None:
        return DLFAIL, None
    if not noReduce:
        try:
            remove_files_from_zip(zipPath, reducePattern)
        except Exception as error:
            # An intact but unreduced zip is a disk-space problem, not a data
            # problem - topsApp only needs the co-pol. An unsound one has to go,
            # so no later fileStage unzips a half-rewritten archive.
            if not zipComplete(zipPath):
                print(f'reduce left {zipPath} unsound ({error}); removing')
                try:
                    os.remove(zipPath)
                except OSError:
                    pass
                return REDUCEFAIL, None
            print(f'reduce failed on {zipPath} ({error}); keeping the full '
                  f'size zip')
    if not zipComplete(zipPath):
        return REDUCEFAIL, None
    return OK, zipPath


def pullGranules(urls, archiveDir, reducePattern='hv', maxAttempts=3,
                 waitMinutes=120, noReduce=False, allowDuplicate=False,
                 allowExcludedTrack=False, excludeTracks=(), check=False,
                 quiet=False):
    '''
    Ensure every granule in urls is present, complete and reduced under
    archiveDir/<YYYY-MM>/. Returns (code, {stem: path}), keyed on the stem so
    the caller does not have to care whether a granule ended up as .zip or
    .zip.1.

    The whole set is fetched under one acquisition of the download slot, so a
    caller never ends up holding half a pair and re-queueing behind everyone
    else.
    '''
    wanted = {}
    for url in urls:
        name = granuleName(url)
        if name is None:
            print(f'not a Sentinel-1 granule URL: {url}')
            return BADNAME, {}
        if autoupdateS1.monthDirFor(archiveDir, name) is None:
            print(f'no date in granule name: {name}')
            return BADNAME, {}
        wanted[autoupdateS1.stripArchiveExt(name)] = url

    def held(name):
        path = autoupdateS1.archiveCopy(archiveDir, name)
        return path if path and zipComplete(path) else None

    def allPresent():
        return all(held(name) for name in wanted)

    # Fast path: everything already there, so never touch the slot. This is the
    # common case once a queue is partly done, and what makes the 1.75x reuse
    # of granules across pairs harmless.
    if allPresent():
        return OK, {name: held(name) for name in wanted}

    for name in wanted:
        if held(name):
            continue
        track = trackOf(name)
        if track is not None and track in excludeTracks \
                and not allowExcludedTrack:
            print(f'{name}: track {track} is in tracksToExclude')
            return EXCLUDED, {}
        if not allowDuplicate:
            duplicate = autoupdateS1.duplicateInArchive(archiveDir, name)
            if duplicate:
                print(f'{name}: another product id for this acquisition is '
                      f'already held ({os.path.basename(str(duplicate))})')
                return DUPLICATE, {}

    if check:
        for name in wanted:
            state = 'have' if held(name) else 'would fetch'
            print(f'{state}: {name}')
        return OK, {}

    with downloadSlot(archiveDir, waitMinutes, allPresent, quiet) as acquired:
        if not acquired:
            if allPresent():          # appeared while we waited
                return OK, {name: held(name) for name in wanted}
            print(f'download slot busy after {waitMinutes} min')
            return LOCKBUSY, {}
        for name, url in wanted.items():
            # Re-checked inside the slot: in a chain (A B then B C) the second
            # call finds B already fetched and pays only for C.
            if held(name):
                continue
            code, _ = fetchOne(url, name, archiveDir, reducePattern,
                               maxAttempts, noReduce)
            if code != OK:
                return code, {}
    return OK, {name: held(name) for name in wanted}


def main():
    ''' Fetch granules into the archive. '''
    args = pullArgs()
    archiveDir = args.archiveDir
    config = archiveConfig(archiveDir, args.config)
    if archiveDir is None:
        archiveDir = config.get('archiveDir')
    if not archiveDir or not os.path.isdir(archiveDir):
        print(f'pullASF: no usable archiveDir ({archiveDir}); pass '
              f'--archiveDir')
        sys.exit(NOARCHIVE)
    reducePattern = args.reducePattern or config.get('reducePattern', 'hv')

    code, paths = pullGranules(
        args.urls, archiveDir, reducePattern=reducePattern,
        maxAttempts=args.maxAttempts, waitMinutes=args.waitMinutes,
        noReduce=args.noReduce, allowDuplicate=args.allowDuplicate,
        allowExcludedTrack=args.allowExcludedTrack,
        excludeTracks=excludedTracks(config), check=args.check)
    for name in paths:
        print(paths[name])
    if code != OK:
        print(f'pullASF failed on {socket.gethostname()} (exit {code})')
    sys.exit(code)


if __name__ == '__main__':
    main()
