#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Sentinel-1 IW SLC archive-update driver, configured by autoupdate.yaml.

Intended to run as a nightly cron job (but also runnable ad hoc from the CLI
for other regions/periods). Each run:
  1. refreshes the precise-orbit (state-vector) archive (refreshOrbits),
  2. searches ASF for new S1 IW SLC passes (searchASF), deduped against the
     existing archive,
  3. downloads new passes one at a time via ariaDownload, verifies each zip,
     re-downloading on failure, and strips the unneeded cross-pol in a parallel
     thread (reduceSentinel1) while the next download starts.

New passes are filed under archiveDir/<YYYY>-<MM>/ where the month comes from
the first date token in the granule name. A pass already present as .zip or
.zip.1 (the .1 marks an already-processed file) is not re-downloaded.

Part of the asfSearchAndDownload package.
"""
import argparse
import calendar
import contextlib
import datetime
import glob
import logging
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import yaml
import utilities as u

from asfsearchdownload import refreshOrbits
from asfsearchdownload import fileS1
from asfsearchdownload import checkFramesS1
from asfsearchdownload import queueS1
from asfsearchdownload.reduceSentinel1 import remove_files_from_zip

# Per-session logger; handlers are attached by setupLogging() in main().
log = logging.getLogger('autoupdateS1')

# Predefined search regions -> searchASF spatial flag.
regionFlags = {
    'antarctica': '--antarctica',
    'greenland': '--greenland',
}

# First date token in an S1 granule name, e.g. ..._20260115T063045_...
DATE_TOKEN = re.compile(r'(\d{8})T\d{6}')

# Trailing ..._<absOrbit6>_<datatake6hex>_<uniqueId4hex>.zip of an S1 name. The
# frames of one pass share (absOrbit, datatake), so this keys pass identity.
PASS_KEY = re.compile(r'_(\d{6})_([0-9A-Fa-f]{6})_[0-9A-Fa-f]{4}\.zip$')


def parseArgs():
    '''
    Handle command line args.
    '''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mAutomated Sentinel-1 IW SLC archive-update '
        'driver, configured by autoupdate.yaml\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('config', type=str, nargs='?', default='autoupdate.yaml',
                        help='Path to autoupdate.yaml (default: ./autoupdate.yaml)')
    parser.add_argument('--maxDownloads', type=int, default=None,
                        help='Soft cap on downloads per run (0 = no limit); '
                        'overrides the config maxDownloads key [default 300]. '
                        'The cap is soft: once reached, the remaining frames of '
                        'the pass in progress (same orbit + datatake) are '
                        'finished before stopping.')
    parser.add_argument('--firstDate', type=str, default=None,
                        help='Search start date YYYY-MM-DD (overrides config; '
                        'default today-6 months)')
    parser.add_argument('--lastDate', type=str, default=None,
                        help='Search end date YYYY-MM-DD (overrides config; '
                        'default today)')
    parser.add_argument('--region', type=str, default=None,
                        help='Predefined search region '
                        f'({"/".join(regionFlags)}); overrides config')
    parser.add_argument('--searchArea', type=str, default=None,
                        help='GeoJSON/.shp/lon,lat search polygon; overrides '
                        'config and --region')
    parser.add_argument('--noOrbits', action='store_true',
                        help='Skip the state-vector (orbit) refresh')
    parser.add_argument('--noDownload', action='store_true',
                        help='Skip search+download (only refresh orbits)')
    parser.add_argument('--fileData', action='store_true',
                        help='Run only the filing step: unpack archive zips '
                        'into the assemblyDir track tree (skips orbits + '
                        'search/download)')
    parser.add_argument('--checkFrames', action='store_true',
                        help='Run only the frame-check step: vet filed '
                        'datatakes and queue them (skips orbits + '
                        'search/download + filing)')
    parser.add_argument('--assembleOnly', action='store_true',
                        help='Run only the assemble step: push the queued units '
                        'through setupTrack (skips orbits + search/download + '
                        'filing + frame check)')
    parser.add_argument('--noAssemble', action='store_true',
                        help='Skip the assemble step even when the config '
                        'enables it')
    parser.add_argument('--noFileDuringDownload', action='store_true',
                        help='Do not file each granule as it is downloaded; '
                        'file everything in one batch after the download '
                        'stage instead (the pre-2026 behaviour)')
    parser.add_argument('--check', action='store_true',
                        help='Dry run across every stage: report what would be '
                        'downloaded, filed, or written without modifying '
                        'anything on disk')
    refreshOrbits.addSensorArgs(parser)
    return parser.parse_args()


def loadConfig(configFile):
    '''
    Read the autoupdate.yaml config into a dict.
    '''
    with open(configFile) as fp:
        config = yaml.safe_load(fp) or {}
    return config


def setupLogging(logDir):
    '''
    Attach a timestamped per-session log file (autoupdateS1_<date-time>.log) in
    logDir to the module logger, plus a stdout stream so cron.log still sees
    everything. Returns the log-file path.
    '''
    os.makedirs(logDir, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y-%m-%dT%H%M%S')
    logPath = os.path.join(logDir, f'autoupdateS1_{stamp}.log')
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    log.setLevel(logging.INFO)
    log.handlers.clear()
    log.propagate = False
    fileHandler = logging.FileHandler(logPath)
    fileHandler.setFormatter(fmt)
    log.addHandler(fileHandler)
    streamHandler = logging.StreamHandler(sys.stdout)
    streamHandler.setFormatter(fmt)
    log.addHandler(streamHandler)
    return logPath


class _SessionSummary:
    '''
    A one-screen, scannable record of the session, written beside the session
    log as <log>.summary. The log stays the place to look for detail; this is
    the part worth reading every morning.

    Failed download URLs are also written to <log>.failures, one per line and
    nothing else, so the file can be handed straight back to ariaDownload.
    '''

    MAX_PROBLEMS = 50          # keep the mail readable if a backlog surfaces

    def __init__(self):
        self.started = datetime.datetime.now()
        self.entries = []
        self.failedUrls = []
        self.notes = []
        self.problems = []
        self.problemQueue = None
        self.lowDisk = False

    def add(self, label, value):
        ''' Record one "label: value" line, keeping insertion order. '''
        self.entries.append((label, value))

    def note(self, text):
        ''' Record a free-standing line (a warning worth surfacing). '''
        self.notes.append(text)

    def addFailures(self, urls):
        self.failedUrls.extend(urls)

    def addProblems(self, records, queueDir):
        ''' Problem-queue records not yet notified. Read from the queue file
        rather than from this run's new entries, so units another tool (e.g.
        setupTrack) added since the last run are picked up too. '''
        self.problems.extend(records)
        self.problemQueue = queueDir

    def _body(self, logPath):
        elapsed = datetime.datetime.now() - self.started
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        width = max([len(label) for label, _ in self.entries] + [0])
        lines = ['autoupdateS1 session summary',
                 f'started  {self.started:%Y-%m-%d %H:%M:%S}',
                 f'finished {datetime.datetime.now():%Y-%m-%d %H:%M:%S} '
                 f'({hours}h {remainder // 60}m)',
                 f'log      {logPath}',
                 '']
        lines += [f'{label:<{width}} : {value}' for label, value in self.entries]
        if self.notes:
            lines += [''] + self.notes
        if self.failedUrls:
            lines += ['',
                      f'{len(self.failedUrls)} granule(s) still missing after '
                      'all retries:']
            lines += [f'  {os.path.basename(url)}' for url in self.failedUrls]
            lines += ['', f'links for a manual retry: '
                          f'{failuresPathFor(logPath)}',
                      f'  ariaDownload {failuresPathFor(logPath)}']
        if self.problems:
            lines += ['', f'{len(self.problems)} unit(s) newly in the problem '
                          'queue:']
            for record in self.problems[:self.MAX_PROBLEMS]:
                unit = record.get('unit', '?') if isinstance(record, dict) \
                    else record
                date = record.get('date', '') if isinstance(record, dict) else ''
                comment = record.get('comment', '') if isinstance(record, dict) \
                    else ''
                lines.append(f'  {unit}  {date}  {comment}'.rstrip())
            if len(self.problems) > self.MAX_PROBLEMS:
                lines.append(f'  ... and {len(self.problems) - self.MAX_PROBLEMS}'
                             ' more')
            lines.append(f'  see {os.path.join(self.problemQueue or "", "problem.yaml")}')
        return '\n'.join(lines) + '\n'

    def write(self, logPath):
        '''
        Write <log>.summary, and <log>.failures if anything is still missing.
        Returns the summary text so the caller can mail it.
        '''
        body = self._body(logPath)
        try:
            with open(summaryPathFor(logPath), 'w') as fp:
                fp.write(body)
        except OSError as exc:
            log.warning(f'could not write the session summary: {exc}')
        if self.failedUrls:
            try:
                with open(failuresPathFor(logPath), 'w') as fp:
                    # Bare URLs only: this file is fed straight to ariaDownload.
                    fp.write('\n'.join(self.failedUrls) + '\n')
            except OSError as exc:
                log.warning(f'could not write the failures list: {exc}')
        return body


# Module-level, like `log`: a single CLI run has one session, and threading an
# accumulator through every stage would be all plumbing and no benefit.
summary = _SessionSummary()


def summaryPathFor(logPath):
    ''' <log>.summary beside the session log. '''
    return f'{os.path.splitext(logPath)[0]}.summary'


def failuresPathFor(logPath):
    ''' <log>.failures beside the session log: failed URLs, one per line. '''
    return f'{os.path.splitext(logPath)[0]}.failures'


def sessionSubject(projectDir, host, failed, nUrls, nProblems,
                   lowDisk=False):
    '''
    Subject line naming every reason this run is being mailed, so several
    triggers still produce exactly one email. The wording of each single-trigger
    case is unchanged, so existing mail filters keep matching.
    '''
    parts = []
    if nUrls:
        parts.append(f'{nUrls} download(s) failed')
    if nProblems:
        parts.append(f'{nProblems} new problem unit(s)')
    if lowDisk:
        parts.append('LOW DISK')
    if failed:
        parts.append('session failed')
    return (f'autoupdateS1 {os.path.basename(projectDir)}: '
            f'{", ".join(parts) or "errors"} on {host}')


def mailReport(recipient, subject, body):
    '''
    Mail a report via the local MTA. Best effort: a machine without a working
    mailer must not fail the run, so a failure is logged and swallowed.
    '''
    for mailer in (['mail', '-s', subject, recipient],
                   ['mailx', '-s', subject, recipient]):
        try:
            proc = subprocess.run(mailer, input=body, text=True,
                                  capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            log.info(f'mailed "{subject}" to {recipient}')
            return True
        log.warning(f'{mailer[0]} exited {proc.returncode}: '
                    f'{proc.stderr.strip()}')
    log.warning(f'could not mail "{subject}" to {recipient}: no working mailer')
    return False


# Cross-machine locks. They live in the project dir (beside autoupdate.yaml) so
# every machine sharing the NFS tree sees them -- unlike /var or /tmp, which are
# per-host. NFS flock/fcntl is unreliable, so we use an atomic O_EXCL lock file.
LOCK_DOWNLOAD = 'autoupdateS1_download.lock'   # guards search + download
LOCK_FILE = '.assemblyTree.lock'               # guards assembly-tree writes
STALE_LOCK_HOURS = 48                           # abandon a lock older than this


def assemblyLockPath(config):
    '''
    Path of the assembly-tree lock. It lives beside the tree it guards, not in
    the project dir, so any tool that knows --assemblyDir can take it -- notably
    s1setup.setupTrack, which has no way to find the project dir. Without that,
    checkFramesS1 can shutil.move a SAFE out from under a running setupTrack.

    LOCK_DOWNLOAD stays in the project dir: it guards archiveDir, which is
    per-project.
    '''
    return os.path.join(os.path.abspath(config['assemblyDir']), LOCK_FILE)


def _tryLock(lockPath, staleHours):
    '''
    Atomically create lockPath (NFS-safe O_CREAT|O_EXCL). Returns True on
    success. A lock older than staleHours is treated as abandoned (crashed run)
    and reclaimed.
    '''
    for attempt in (1, 2):
        try:
            fd = os.open(lockPath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, (f'{socket.gethostname()} pid {os.getpid()} '
                          f'{datetime.datetime.now().isoformat(timespec="seconds")}'
                          '\n').encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lockPath)
                with open(lockPath) as fp:
                    holder = fp.read().strip()
            except OSError:
                return False
            if attempt == 1 and age > staleHours * 3600:
                log.warning(f'reclaiming stale lock {lockPath} '
                            f'({age / 3600:.1f} h old, held by {holder})')
                try:
                    os.remove(lockPath)
                except OSError:
                    pass
                continue
            log.info(f'lock {lockPath} held by {holder}')
            return False
    return False


@contextlib.contextmanager
def crossHostLock(lockPath, active=True, staleHours=STALE_LOCK_HOURS,
                  quiet=False):
    '''
    Non-blocking cross-host lock. Yields True if acquired (removing the lock file
    on exit), False if another run holds it. With active=False (a dry run) it is
    a no-op that always yields True and touches nothing -- but if a live run
    currently holds the lock it prints a bold-blue notice, since the dry-run
    output may then reflect a mid-flight tree. quiet suppresses that notice, for
    callers that are inactive because *this* run already holds the lock.
    '''
    if not active:
        if os.path.exists(lockPath) and not quiet:
            try:
                with open(lockPath) as fp:
                    holder = fp.read().strip()
            except OSError:
                holder = '?'
            print(f'\033[1;34m*** LOCK ACTIVE: {os.path.basename(lockPath)} held '
                  f'by {holder}; --check output may be mid-flight ***\033[0m')
        yield True
        return
    acquired = _tryLock(lockPath, staleHours)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lockPath)
            except OSError:
                pass


def regionFlag(region):
    '''
    Map a region name to its searchASF spatial flag (case-insensitive).
    '''
    key = str(region).strip().lower()
    if key not in regionFlags:
        u.myerror(f"autoupdateS1: region must be one of {list(regionFlags)}, "
                  f"got '{region}'")
    return regionFlags[key]


def spatialFlags(config, args):
    '''
    Return the searchASF spatial-constraint tokens, CLI first: an explicit
    --searchArea, else --region, else config searchArea, else config region.
    '''
    if args.searchArea:
        return ['--searchArea', args.searchArea]
    if args.region:
        return [regionFlag(args.region)]
    if config.get('searchArea'):
        return ['--searchArea', str(config['searchArea'])]
    if config.get('region'):
        return [regionFlag(config['region'])]
    u.myerror('autoupdateS1: no region or searchArea given (CLI flag or '
              'config key)')


def configList(value, default):
    '''
    Normalize a config value that may be a space-separated string, a YAML list,
    or absent into a list of string tokens (falling back to default).
    '''
    if value is None:
        value = default
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def resolveSensors(config, args):
    '''
    Return the selected sensors. The CLI --S1A/--S1B/--S1C/--S1D flags win if any
    are set; otherwise the config `satellites` key; otherwise all four.
    '''
    cli = tuple(s for s in refreshOrbits.ALL_SENSORS if getattr(args, s, False))
    if cli:
        return cli
    if config.get('satellites') is None:
        return refreshOrbits.ALL_SENSORS
    sats = configList(config.get('satellites'), '')
    unknown = [s for s in sats if s not in refreshOrbits.ALL_SENSORS]
    if unknown:
        u.myerror(f'autoupdateS1: unknown satellite(s) {unknown} in config '
                  f'satellites; choose from {list(refreshOrbits.ALL_SENSORS)}')
    return tuple(s for s in refreshOrbits.ALL_SENSORS if s in sats)


def directionFlags(config):
    '''
    Map the config `direction` key to searchASF flight-direction tokens.
    'both' (default) applies no constraint. ascending/descending require the
    --flightDirection option in searchASF.
    '''
    direction = str(config.get('direction', 'both')).strip().lower()
    if direction in ('both', 'all', ''):
        return []
    if direction.startswith('asc'):
        return ['--flightDirection', 'ASCENDING']
    if direction.startswith('desc'):
        return ['--flightDirection', 'DESCENDING']
    u.myerror("autoupdateS1: direction must be both/ascending/descending, got "
              f"'{direction}'")


def monthsBack(d, months):
    '''
    Return the date `months` calendar months before d (clamped day-of-month).
    '''
    month = d.month - 1 - months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def asDateStr(value):
    '''
    Coerce a date/datetime (as YAML may parse an unquoted date) or string into
    a YYYY-MM-DD string.
    '''
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d')
    return str(value)


def resolveDateRange(config, args, today):
    '''
    Return (firstDate, lastDate) as YYYY-MM-DD strings. CLI overrides config;
    defaults are today-6 months and today.
    '''
    firstDate = args.firstDate or config.get('firstDate') or monthsBack(today, 6)
    lastDate = args.lastDate or config.get('lastDate') or today
    return asDateStr(firstDate), asDateStr(lastDate)


def searchGranules(config, args, today, firstDate, lastDate, check=False):
    '''
    Search ASF for S1 IW SLC granules over the configured region/time window,
    deduped against the existing archive, writing the URL list and coverage
    GeoPackage under archiveDir/searchResults. Returns the URL-list path.

    With check, the results are written to a scratch temp dir instead of the
    archive so a dry run leaves archiveDir untouched (the search itself is a
    read; the archive is only read for dedup).
    '''
    archiveDir = config['archiveDir']
    if check:
        searchDir = tempfile.mkdtemp(prefix='autoupdateS1_check_')
        gpkgDir = searchDir
    else:
        searchDir = os.path.join(archiveDir, 'searchResults')
        gpkgDir = os.path.join(searchDir, 'gpkg')
    os.makedirs(gpkgDir, exist_ok=True)
    d = today.strftime('%m-%d-%Y')
    out = os.path.join(searchDir, f'Download.{d}')
    gpkg = os.path.join(gpkgDir, f'Download.{d}.gpkg')
    # Dedup glob matches archiveDir/<YYYY-MM>/*.zip and *.zip.1 (searchASF
    # strips .zip/.zip.1 before comparing).
    archiveGlob = os.path.join(archiveDir, '*', '*')
    products = configList(config.get('productType'), 'SLC')
    beamModes = configList(config.get('beamMode'), 'IW')
    command = (['searchASF', '--sensor', 'SENTINEL1',
                '--products'] + products + ['--beamMode'] + beamModes
               + ['--archiveDir', archiveGlob, '--gpkg', gpkg]
               + directionFlags(config)
               + spatialFlags(config, args)
               + [firstDate, lastDate, out])
    log.info('search: ' + ' '.join(command))
    subprocess.run(command, check=True)
    return out


def readUrlList(urlListFile):
    '''
    Return the download URLs listed in a searchASF URL-list file.
    '''
    urls = []
    with open(urlListFile) as fp:
        for line in fp:
            url = line.strip()
            if url:
                urls.append(url)
    return urls


def filterBySensor(urls, sensors):
    '''
    Keep only URLs whose granule name begins with one of the selected sensors.
    '''
    prefixes = tuple(f'{s}_' for s in sensors)
    return [url for url in urls
            if os.path.basename(url).startswith(prefixes)]


def sortByAcqDate(urls):
    '''
    Sort URLs oldest-first by the first acquisition datetime token in the
    granule name (YYYYMMDDThhmmss sorts lexically = chronologically), so a
    capped run (--maxDownloads) fetches the oldest missing passes first.
    '''
    def key(url):
        match = DATE_TOKEN.search(os.path.basename(url))
        return match.group(0) if match else ''
    return sorted(urls, key=key)


def passKey(name):
    '''
    Return the (absOrbit, datatake) pass identity for an S1 granule name, or
    None if it cannot be parsed. Frames of the same pass share this key.
    '''
    match = PASS_KEY.search(name)
    return match.groups() if match else None


def stripArchiveExt(name):
    '''
    Strip a trailing .zip.1 or .zip from a granule basename.
    '''
    for ext in ('.zip.1', '.zip'):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def granuleInArchive(archiveDir, name):
    '''
    True if the granule already exists anywhere under archiveDir as .zip or
    .zip.1 (the .1 marks an already-processed pass). Belt-and-suspenders beyond
    the searchASF dedup.
    '''
    stem = stripArchiveExt(name)
    for ext in ('.zip', '.zip.1'):
        if glob.glob(os.path.join(archiveDir, '*', stem + ext)):
            return True
    return False


def monthDirFor(archiveDir, name):
    '''
    Return archiveDir/<YYYY>-<MM> from the first date token in the granule name,
    or None if no date token is present.
    '''
    match = DATE_TOKEN.search(name)
    if not match:
        return None
    ymd = match.group(1)
    return os.path.join(archiveDir, f'{ymd[:4]}-{ymd[4:6]}')


# Lives in fileS1 so fileOneZip can refuse a download that is still in flight;
# re-exported here because downloadOne has always used it under this name.
zipComplete = fileS1.zipComplete


def downloadOne(url, monthDir, maxAttempts=3):
    '''
    Download a single granule into monthDir via ariaDownload (which adjusts
    aria2c bandwidth by time of day), verifying the zip and re-downloading on
    failure up to maxAttempts. Returns the zip path on success, else None.
    '''
    os.makedirs(monthDir, exist_ok=True)
    name = os.path.basename(url)
    zipPath = os.path.join(monthDir, name)
    linkFile = os.path.join(monthDir, f'.{name}.url')
    with open(linkFile, 'w') as fp:
        fp.write(url + '\n')
    try:
        for attempt in range(1, maxAttempts + 1):
            if not zipComplete(zipPath):
                # ariaDownload skips an existing file, so clear any partial or
                # corrupt zip (and its .aria2 control) to force a fresh fetch.
                for stale in (zipPath, f'{zipPath}.aria2'):
                    if os.path.exists(stale):
                        os.remove(stale)
                log.info(f'ariaDownload attempt {attempt}/{maxAttempts}: {name}')
                subprocess.run(['ariaDownload', linkFile], cwd=monthDir,
                               check=True)
            if zipComplete(zipPath):
                return zipPath
            log.warning(f'{name} incomplete/corrupt after attempt {attempt}')
        # Give up: remove the partial so the next run's search re-lists it.
        for stale in (zipPath, f'{zipPath}.aria2'):
            if os.path.exists(stale):
                os.remove(stale)
        log.error(f'{name} failed after {maxAttempts} attempts')
        return None
    finally:
        if os.path.exists(linkFile):
            os.remove(linkFile)


def _reduceWorker(zipPath, pattern, pipeline=None):
    '''
    Strip the cross-pol from a downloaded zip (runs in a background thread),
    then hand it to the filing pipeline if there is one.

    The hand-off is the statement after remove_files_from_zip returns, and that
    call is fully synchronous, so no zip is ever unzipped while `zip -d` is
    rewriting it in place.
    '''
    name = os.path.basename(zipPath)
    reduced = True
    try:
        remove_files_from_zip(zipPath, pattern)
        log.info(f'reduced (cross-pol stripped): {name}')
    except Exception as exc:  # keep one bad zip from killing the run
        log.error(f'reduce failed for {name}: {exc}')
        reduced = False
    if pipeline is None:
        return
    # A failed reduce may have left a half-rewritten archive; leave it as .zip
    # for the end-of-run sweep rather than unzipping something unsound.
    if not reduced and not zipComplete(zipPath):
        log.error(f'not filing {name}: zip unsound after a failed reduce')
        return
    pipeline.submit(zipPath)


def startReduce(zipPath, pattern, threads, pipeline=None):
    '''
    Launch the cross-pol reduce for zipPath in a background thread and record
    it so the caller can join it before exiting.
    '''
    thread = threading.Thread(target=_reduceWorker,
                              args=(zipPath, pattern, pipeline))
    thread.start()
    threads.append(thread)


class _FilingPipeline:
    '''
    Files each granule into the assembly tree as soon as its reduce finishes,
    so the ~88 s unzip hides inside the ~155 s download of the next granule
    instead of running as a batch after the whole download stage.

    Producers are the reduce threads (startReduce); a fixed pool of nWorkers
    threads consumes the queue. The caller must hold LOCK_FILE for the
    pipeline's lifetime, and must join every producer before calling
    shutdown() so the drain cannot race new work.
    '''
    _SENTINEL = None

    def __init__(self, assemblyDir, filedPath=None, nWorkers=2,
                 recordEvery=25):
        self.assemblyDir = assemblyDir
        self.filedPath = filedPath
        self.nWorkers = max(1, int(nWorkers))
        self.recordEvery = recordEvery
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.tracks = set()
        self.granules = []
        self.nFiled = self.nSkipped = self.nErrors = 0
        self.threads = []

    def start(self):
        for i in range(self.nWorkers):
            thread = threading.Thread(target=self._worker, name=f'filing-{i}')
            thread.start()   # not a daemon: shutdown() drains it
            self.threads.append(thread)
        log.info(f'concurrent filing: {self.nWorkers} worker(s) -> '
                 f'{self.assemblyDir}')

    def submit(self, zipPath):
        self.queue.put(zipPath)

    def _worker(self):
        while True:
            zipPath = self.queue.get()
            if zipPath is self._SENTINEL:
                return
            name = os.path.basename(zipPath)
            try:
                status, track, _ = fileS1.fileOneZip(
                    zipPath, self.assemblyDir, createTrackDir=True)
            except Exception as exc:   # never let one zip kill a worker
                log.exception(f'filing raised for {name}: {exc}')
                with self.lock:
                    self.nErrors += 1
                continue
            self._record(status, track, zipPath, name)

    def _record(self, status, track, zipPath, name):
        snapshot = None
        with self.lock:
            if status == fileS1.FILED:
                self.nFiled += 1
                self.tracks.add(track)
                self.granules.append(zipPath)
                if (self.filedPath is not None
                        and self.nFiled % self.recordEvery == 0):
                    snapshot = (set(self.tracks), list(self.granules))
            elif status == fileS1.SKIPPED:
                self.nSkipped += 1
            else:
                self.nErrors += 1
        if status == fileS1.FILED:
            log.info(f'filed: {name} -> track-{track}')
        elif status == fileS1.SKIPPED:
            log.warning(f'filing skipped: {name}')
        else:
            log.error(f'filing failed, left as .zip for the end-of-run '
                      f'sweep: {name}')
        if snapshot is not None:
            fileS1.writeFiledRecord(self.filedPath, *snapshot)

    def shutdown(self):
        '''
        Drain the queue and join the workers. Every producer (reduce thread)
        must already be joined, so no new work can arrive behind the sentinels.
        '''
        pending = self.queue.qsize()
        if pending:
            log.info(f'draining {pending} granule(s) still to file')
        for _ in self.threads:
            self.queue.put(self._SENTINEL)
        for thread in self.threads:
            thread.join()
        if self.filedPath is not None and self.granules:
            fileS1.writeFiledRecord(self.filedPath, self.tracks, self.granules)
        log.info(f'concurrent filing: filed {self.nFiled}, '
                 f'skipped {self.nSkipped}, errors {self.nErrors}')
        return self.nFiled


def main():
    ''' Search/download/reduce new S1 IW SLC passes and refresh orbits. '''
    args = parseArgs()
    config = loadConfig(args.config)
    if 'archiveDir' not in config:
        u.myerror(f"autoupdateS1: required key 'archiveDir' missing from "
                  f'{args.config}')
    archiveDir = os.path.abspath(config['archiveDir'])
    config['archiveDir'] = archiveDir
    # Session log lives in <projectDir>/logs by default (projectDir is the
    # config file's directory); overridable via the logDir config key.
    projectDir = os.path.dirname(os.path.abspath(args.config))
    logDir = config.get('logDir') or os.path.join(projectDir, 'logs')
    logPath = setupLogging(logDir)

    sensors = resolveSensors(config, args)
    reducePattern = config.get('reducePattern', 'hv')
    maxAttempts = int(config.get('maxAttempts', 3))
    today = datetime.date.today()  # fixed once so a midnight-crossing run agrees
    log.info(f'=== autoupdateS1 session start (config {args.config}) ===')
    log.info(f'archiveDir {archiveDir}; sensors {",".join(sensors)}; '
             f'log {logPath}')
    summary.add('project', projectDir)
    summary.add('archiveDir', archiveDir)
    summary.add('sensors', ','.join(sensors))
    failed = False
    try:
        runUpdate(config, args, archiveDir, sensors, reducePattern,
                  maxAttempts, today, logDir, projectDir)
    except Exception as exc:
        failed = True
        log.exception('autoupdateS1 session failed')
        summary.note(f'SESSION FAILED: {exc!r} (see the log for the traceback)')
        raise
    finally:
        # Written even on a crash: the summary is most useful when the run did
        # not finish cleanly.
        body = summary.write(logPath)
        log.info(f'summary written to {summaryPathFor(logPath)}')
        # Mail only when something needs a human: granules still missing after
        # every retry, or an outright crash. A clean run stays silent.
        # Opt-in, matching nisargrimpworkflow.autoupdate.notifyOnErrors: with no
        # notifyEmail key nothing is ever mailed. Deliberately not defaulting to
        # root -- /etc/aliases fans root out to other people.
        recipient = config.get('notifyEmail')
        if recipient and not args.check and (summary.failedUrls
                                             or summary.problems
                                             or summary.lowDisk or failed):
            subject = sessionSubject(projectDir, socket.gethostname(), failed,
                                     len(summary.failedUrls),
                                     len(summary.problems), summary.lowDisk)
            sent = mailReport(recipient, subject, body)
            # Mark only after a confirmed send. An unmarked entry costs a
            # duplicate email next run; a prematurely marked one loses the
            # notice for good.
            if sent and summary.problems:
                queueS1.markNotified(
                    summary.problemQueue,
                    [queueS1.entryUnit(r) for r in summary.problems])
    log.info('=== autoupdateS1 session done ===')


def fileStage(config, archiveDir, logDir, projectDir, today, check=False,
              lockHeld=False):
    '''
    Unpack the archive zips (archiveDir/<YYYY-MM>/*.zip) into the per-track/
    per-orbit tree under assemblyDir, writing a YAML record (tracks:/granules:)
    of what was filed this run for downstream steps to consume. With check,
    report what would be filed without unpacking or writing the record.

    Guarded by the shared file lock so two machines never file into the same
    assembly tree at once.
    '''
    if 'assemblyDir' not in config:
        u.myerror("autoupdateS1: the file stage needs the 'assemblyDir' key "
                  'in the config')
    assemblyDir = os.path.abspath(config['assemblyDir'])
    lockPath = assemblyLockPath(config)
    # lockHeld: runUpdate already holds the file lock across the download stage
    # when it is filing concurrently, so re-acquiring here would fail.
    with crossHostLock(lockPath, active=not check and not lockHeld,
                       quiet=lockHeld) as acquired:
        if not acquired:
            log.warning(f'file stage: another run holds {lockPath}; '
                        'skipping filing')
            return
        filedPath = os.path.join(logDir, f'filedS1.{today:%m-%d-%Y}.yaml')
        tracks, granules = fileS1.fileS1(zipDir=archiveDir,
                                         assemblyDir=assemblyDir,
                                         monthSubdirs=True, filed=filedPath,
                                         createTrackDir=True, check=check)
        if check:
            log.info(f'[check] would file {len(granules)} zip(s) across tracks '
                     f'{sorted(tracks)}')
        else:
            log.info(f'filed {len(granules)} zip(s) across tracks '
                     f'{sorted(tracks)} -> {filedPath}')
            summary.add('filed by the end-of-run sweep', len(granules))
            summary.add('tracks touched', sorted(tracks))


def frameCheckStage(config, today, args, projectDir, check=False,
                    lockHeld=False):
    '''
    Vet the filed datatakes under assemblyDir (burst-frame coverage, gaps,
    over-length, out-of-range), restructure them into clean processing units,
    and route each into the cumulative toProcess / pendingProcessing / problem
    queues. With check, report the routing without changing anything.

    Shares the file lock with the filing stage: both mutate the assembly tree,
    so two machines must never run either at once on the same project.
    '''
    if 'assemblyDir' not in config:
        u.myerror("autoupdateS1: the frame-check stage needs the 'assemblyDir' "
                  'key in the config')
    assemblyDir = os.path.abspath(config['assemblyDir'])
    lockPath = assemblyLockPath(config)
    with crossHostLock(lockPath, active=not check and not lockHeld,
                       quiet=lockHeld) as acquired:
        if not acquired:
            log.warning(f'frame-check stage: another run holds {lockPath}; '
                        'skipping')
            return
        orbitDir = config.get('orbitDir', refreshOrbits.DEFAULT_ORBIT_DIR)
        queueDir = config.get('queueDir', assemblyDir)
        firstStr, lastStr = resolveDateRange(config, args, today)
        firstDate = datetime.datetime.strptime(firstStr, '%Y-%m-%d')
        lastDate = (datetime.datetime.strptime(lastStr, '%Y-%m-%d')
                    + datetime.timedelta(days=1))
        entries = checkFramesS1.checkFrames(assemblyDir, orbitDir=orbitDir,
                                            firstDate=firstDate,
                                            lastDate=lastDate,
                                            queueDir=queueDir, check=check)
        verb = 'would queue' if check else 'queued'
        log.info(f'frame check: {verb} toProcess {len(entries["toProcess"])}, '
                 f'pending {len(entries["pendingProcessing"])}, '
                 f'problem {len(entries["problem"])}')
        summary.add(f'frame check {verb}',
                    f'toProcess {len(entries["toProcess"])}, '
                    f'pending {len(entries["pendingProcessing"])}, '
                    f'problem {len(entries["problem"])}')
        # Scan the queue file rather than this run's new entries: it also holds
        # anything setupTrack routed to problem since the last run, which is
        # precisely what a within-run diff cannot see.
        if not check:
            pending = queueS1.unnotified(queueDir)
            if pending:
                log.warning(f'{len(pending)} problem unit(s) not yet notified')
                summary.addProblems(pending, queueDir)


def checkFreeSpace(config):
    '''
    Report free space on the volume holding assemblyDir and warn when it drops
    below the configured floor. Unprocessed units carry ~30 GB of measurement
    TIFFs each, so a night of downloading without assembly costs of order a TB.

    Returns True if space is low (worth mailing about).
    '''
    if 'assemblyDir' not in config:
        return False
    assemblyDir = os.path.abspath(config['assemblyDir'])
    minFreeTB = float(config.get('minFreeTB', 6))
    try:
        freeTB = shutil.disk_usage(assemblyDir).free / 1e12
    except OSError as exc:
        log.warning(f'could not check free space on {assemblyDir}: {exc}')
        return False
    summary.add('assembly volume free', f'{freeTB:.1f} TB '
                f'(floor {minFreeTB:.0f} TB)')
    if freeTB >= minFreeTB:
        log.info(f'assembly volume free: {freeTB:.1f} TB')
        return False
    log.warning(f'LOW DISK: {assemblyDir} has {freeTB:.1f} TB free, '
                f'below the {minFreeTB:.0f} TB floor')
    summary.note(f'LOW DISK: {freeTB:.1f} TB free on {assemblyDir}, below the '
                 f'{minFreeTB:.0f} TB floor. Unprocessed units hold ~30 GB each '
                 '-- assemble the queue, or strip TIFFs from processed units.')
    return True


def assembleStage(config, args, projectDir, check=False, lockHeld=False):
    '''
    Run the queued units through setupTrack, which reclaims the measurement
    TIFFs as each one finishes.

    Invoked as a subprocess rather than imported: s1setup already depends on
    this package for the queue format, so importing setupTrack here would make
    the dependency circular. setupTrack is on PATH as a console script.
    '''
    assemblyDir = os.path.abspath(config['assemblyDir'])
    queueDir = os.path.abspath(config.get('queueDir', assemblyDir))
    cmd = ['setupTrack.py', '--queue', '--assemblyDir', assemblyDir,
           '--queueDir', queueDir]
    if lockHeld:
        cmd.append('--lockHeld')
    if config.get('noStripTiffs'):
        cmd.append('--noStripTiffs')
    if config.get('assembleMaxUnits'):
        cmd += ['--maxUnits', str(int(config['assembleMaxUnits']))]
    if check:
        cmd.append('--check')
    log.info(f'assemble: {" ".join(cmd)}')
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        log.error(f'assemble stage: could not run setupTrack: {exc}')
        summary.note(f'assemble stage failed to start: {exc}')
        return
    for line in (proc.stdout or '').splitlines():
        log.info(f'  {line}')
    if proc.stderr:
        for line in proc.stderr.splitlines():
            log.warning(f'  {line}')
    # setupTrack exits 1 when any unit failed; those are already in the problem
    # queue with a comment, so the notification covers them.
    verb = 'would assemble' if check else 'assembled'
    log.info(f'assemble stage: setupTrack exited {proc.returncode}')
    summary.add(f'assemble ({verb})', f'setupTrack exit {proc.returncode}')


def downloadStage(config, args, archiveDir, sensors, reducePattern, maxAttempts,
                  today, pipeline=None):
    '''
    Search ASF then serially download new granules (parallel cross-pol reduce).
    Wrapped by runUpdate in the cross-host download lock.

    With a pipeline, each granule is also filed into the assembly tree as soon
    as its reduce finishes, rather than in a batch after the whole stage.
    '''
    # 2. Search.
    firstDate, lastDate = resolveDateRange(config, args, today)
    log.info(f'search window {firstDate} .. {lastDate}')
    urlListFile = searchGranules(config, args, today, firstDate, lastDate,
                                 check=args.check)
    urls = sortByAcqDate(filterBySensor(readUrlList(urlListFile), sensors))
    log.info(f'{len(urls)} new granule(s) for sensor(s) {",".join(sensors)}')
    summary.add('search window', f'{firstDate} .. {lastDate}')
    summary.add('new granules found', len(urls))

    # Soft download cap: CLI overrides config, default 300; 0 means no limit.
    if args.maxDownloads is not None:
        maxDownloads = args.maxDownloads
    else:
        maxDownloads = int(config.get('maxDownloads', 300))

    # 3. Serial download; parallel cross-pol reduce. The cap is soft: once
    # reached, keep going while the next granule is the same pass (orbit +
    # datatake) as the last one downloaded, so a pass is never left half-fetched.
    reduceThreads = []
    nDownloaded = nSkipped = nFailed = 0
    failedUrls = []
    lastPassKey = None
    try:
        for url in urls:
            name = os.path.basename(url)
            thisPassKey = passKey(name)
            if maxDownloads and nDownloaded >= maxDownloads \
                    and thisPassKey != lastPassKey:
                log.info(f'reached --maxDownloads {maxDownloads} at a pass '
                         f'boundary ({nDownloaded} downloaded); stopping')
                break
            if granuleInArchive(archiveDir, name):
                nSkipped += 1
                continue
            monthDir = monthDirFor(archiveDir, name)
            if monthDir is None:
                log.warning(f'cannot parse date from {name}; skipping')
                nFailed += 1
                continue
            if args.check:
                log.info(f'[check] would download: {name} -> {monthDir}')
                nDownloaded += 1
                lastPassKey = thisPassKey
                continue
            zipPath = downloadOne(url, monthDir, maxAttempts)
            if zipPath is None:
                nFailed += 1
                failedUrls.append(url)
                continue
            nDownloaded += 1
            lastPassKey = thisPassKey
            log.info(f'downloaded: {name} -> {monthDir}')
            startReduce(zipPath, reducePattern, reduceThreads, pipeline)

        # 3b. Retry the failures once at the end of the run. Exhausting all
        # attempts in a few seconds usually means a brief ASF-side outage rather
        # than a bad granule, and by the end of a long run it has typically
        # cleared.
        if failedUrls:
            log.info(f'retry pass: {len(failedUrls)} granule(s) that failed '
                     'earlier')
            nRecovered = 0
            stillFailing = []
            for url in failedUrls:
                name = os.path.basename(url)
                monthDir = monthDirFor(archiveDir, name)
                zipPath = downloadOne(url, monthDir, maxAttempts)
                if zipPath is None:
                    log.error(f'still failing after retry pass: {name}')
                    stillFailing.append(url)
                    continue
                nFailed -= 1
                nDownloaded += 1
                nRecovered += 1
                log.info(f'downloaded on retry: {name} -> {monthDir}')
                startReduce(zipPath, reducePattern, reduceThreads, pipeline)
            log.info(f'retry pass: recovered {nRecovered} of '
                     f'{len(failedUrls)}')
            summary.add('retry pass',
                        f'recovered {nRecovered} of {len(failedUrls)}')
            summary.addFailures(stillFailing)
    finally:
        # Producers first, then the consumers: nothing may still be able to
        # submit when the sentinels go in. The filing workers are not daemons,
        # so skipping this on an interrupt would hang the interpreter at exit.
        for thread in reduceThreads:
            thread.join()
        if pipeline is not None:
            pipeline.shutdown()

    verb = 'would download' if args.check else 'downloaded'
    filedNote = '' if pipeline is None else f', filed {pipeline.nFiled}'
    log.info(f'summary: {verb} {nDownloaded}, skipped {nSkipped}, '
             f'failed {nFailed}; {len(reduceThreads)} reduced{filedNote}')
    summary.add(verb, nDownloaded)
    summary.add('already in archive', nSkipped)
    summary.add('failed', nFailed)
    if pipeline is not None:
        summary.add('filed while downloading', pipeline.nFiled)
        if pipeline.nErrors:
            summary.note(f'{pipeline.nErrors} granule(s) failed to unzip and '
                         'were left as .zip for the next run')


def runUpdate(config, args, archiveDir, sensors, reducePattern, maxAttempts,
              today, logDir, projectDir):
    '''
    The search/download/reduce + orbit-refresh workflow (wrapped by main() so
    any error is logged to the session log). Cross-host locks in projectDir keep
    two machines from downloading, or from writing the assembly tree, at once.
    '''
    if args.check:
        log.info('[check] dry run: no data will be written')

    # File-only isolation: skip orbits + search/download, just file the archive.
    if args.fileData:
        fileStage(config, archiveDir, logDir, projectDir, today,
                  check=args.check)
        return

    # Frame-check-only isolation: skip everything but the vet-and-queue step.
    if args.checkFrames:
        frameCheckStage(config, today, args, projectDir, check=args.check)
        summary.lowDisk = checkFreeSpace(config)
        return

    # Assemble-only isolation: push the queued units through setupTrack. It takes
    # the assembly lock itself here, since no stage above is holding it.
    if args.assembleOnly:
        assembleStage(config, args, projectDir, check=args.check)
        summary.lowDisk = checkFreeSpace(config)
        return

    # 1. Orbit state vectors.
    if not args.noOrbits:
        orbitDir = config.get('orbitDir', refreshOrbits.DEFAULT_ORBIT_DIR)
        newOrbits = refreshOrbits.updateStateVectors(orbitDir, sensors,
                                                     check=args.check)
        verb = 'would download' if args.check else 'downloaded'
        for name in newOrbits:
            log.info(f'orbit {verb}: {name}')
        log.info(f'orbits: {len(newOrbits)} EOF file(s)')
        summary.add('orbit files', len(newOrbits))

    if args.noDownload:
        log.info('--noDownload set; done after orbit refresh')
        return

    # 2-3. Search + download, guarded so two machines never download at once.
    # When filing concurrently we also hold the file lock for the whole stage:
    # the filing workers mutate the assembly tree, so no other host may file or
    # frame-check meanwhile. Both locks are non-blocking, so nesting them cannot
    # deadlock, and this is the only place either is nested.
    dlLock = os.path.join(projectDir, LOCK_DOWNLOAD)
    active = not args.check
    canFile = 'assemblyDir' in config
    fileLock = assemblyLockPath(config) if canFile else None
    wantConcurrent = canFile and active and not args.noFileDuringDownload

    with contextlib.ExitStack() as stack:
        dlAcquired = stack.enter_context(crossHostLock(dlLock, active=active))
        fileAcquired = False
        if dlAcquired and wantConcurrent:
            fileAcquired = stack.enter_context(
                crossHostLock(fileLock, active=active))
            if not fileAcquired:
                log.warning(f'another run holds {fileLock}; downloading '
                            'without concurrent filing (the zips stay .zip for '
                            'a later sweep)')
        pipeline = None
        if dlAcquired and fileAcquired:
            filedPath = os.path.join(logDir, f'filedS1.{today:%m-%d-%Y}.yaml')
            pipeline = _FilingPipeline(
                os.path.abspath(config['assemblyDir']), filedPath=filedPath,
                nWorkers=int(config.get('fileWorkers', 2)))
            pipeline.start()
        if dlAcquired:
            downloadStage(config, args, archiveDir, sensors, reducePattern,
                          maxAttempts, today, pipeline=pipeline)
        else:
            log.warning(f'download stage: another run holds {dlLock}; '
                        'skipping search + download')

        # 4-5. Sweep any zips the pipeline did not file (a previous crashed run,
        # failed unzips, or everything if concurrent filing was unavailable),
        # then vet & queue the datatakes. Still inside the file lock when we
        # hold it, so no other host can slip a destructive frame check in.
        if canFile:
            fileStage(config, archiveDir, logDir, projectDir, today,
                      check=args.check, lockHeld=fileAcquired)
            frameCheckStage(config, today, args, projectDir, check=args.check,
                            lockHeld=fileAcquired)
            # 6. Assemble the queued units. Last, because it needs the queue the
            # frame check just wrote, and because it is the long pole.
            if config.get('assemble') and not args.noAssemble:
                assembleStage(config, args, projectDir, check=args.check,
                              lockHeld=fileAcquired)
            summary.lowDisk = checkFreeSpace(config)
        else:
            log.info('no assemblyDir in config; skipping file + frame-check '
                     'stages')


if __name__ == '__main__':
    main()
