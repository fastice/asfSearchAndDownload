#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jun 18 15:11:35 2021

@author: ian
"""

import argparse
from asfsearchdownload import helpers
import os
from datetime import datetime
from subprocess import call
import threading
import glob
import yaml
import zipfile


# fileOneZip outcomes.
FILED, SKIPPED, CHECKED, ERROR = 'filed', 'skipped', 'checked', 'error'

# S1C moved to a different orbital slot around 2026-06-09, which shifted its
# relative-orbit offset from 171 to 98. A granule's track therefore cannot be
# derived from its orbit alone -- S1C needs the acquisition date as well.
#
# 9 June is still the old slot, so the first date on the new one is the 10th.
# It is the 10th rather than the 9th plus a comparison, because the acquisition
# date carries a time: 9 June 10:15 is > datetime(2026, 6, 9) and would take
# the new constant wrongly. Nothing in fact lands near the boundary -- they
# collected no data for a couple of weeks over the change.
s1cShiftDate = datetime(2026, 6, 10)
s1cConstAfterShift = 98

# Serializes track-/orbit-dir creation. Two frames of one pass share
# track-<n>/<orbit>, and fileOneZip runs concurrently from both the batch pool
# here and autoupdateS1's filing pipeline.
_dirLock = threading.Lock()


def fileS1Args():
    ''' Handle command line args'''
    parser = argparse.ArgumentParser(
        description='\033[1m File S1 images in Data dir \033[0m',
        epilog='Part of the asfSearchAndDownload package.',
        allow_abbrev='False')
    parser.add_argument('--overwrite', action='store_true', default=False,
                        help='Overwrite existing')
    parser.add_argument('--createTrackDir', action='store_true', default=False,
                        help='Create track dir if it does not exist already')
    parser.add_argument('--zipDir', type=str,
                        default='/Volumes/insar10/ian/xfer',
                        help='Directory with zip files')
    parser.add_argument('--assemblyDir', type=str, default='.',
                        help='Root dir under which track-<n>/<orbit>/ trees are '
                        'built [default: current dir]')
    parser.add_argument('--monthSubdirs', action='store_true', default=False,
                        help='Glob zipDir/<YYYY-MM>/*.zip across all month '
                        'subdirs instead of a flat zipDir/*.zip')
    parser.add_argument('--filed', type=str, default=None,
                        help='Path to write a YAML record (tracks:/granules:) '
                        'of what was filed this run')
    parser.add_argument('--excludeTracks', type=str, default='',
                        metavar='"N N ..."',
                        help='Tracks never to unpack, as one quoted space- or '
                        'comma-separated value: --excludeTracks "114 143". '
                        'Their zips are left where they are, so removing a '
                        'track from the list files them on the next run')
    parser.add_argument('--check', action='store_true', default=False,
                        help='Dry run: report what would be filed without '
                        'unpacking, renaming, or writing anything')
    args = parser.parse_args()
    return args


def computeTrack(orbit, sat, date=None):
    '''
    Track (relative orbit) from the absolute orbit and satellite.

    Only S1C matters here: it changed orbital slot mid-2026, at absolute orbit
    8018 (~2026-06-09/10), after which its offset is 98 rather than 171. The
    shift is keyed on the orbit number (matching upstream ISCE's <=8018 test),
    so it is correct even when no date is supplied. `date` is retained for
    backwards compatibility with existing callers.
    '''
    satConst = {'S1A': 72, 'S1B': 26, 'S1C': 171, 'S1D': 41}
    const = satConst[sat]
    if sat == 'S1C' and orbit > 8018:
        const = s1cConstAfterShift
    track = orbit % 175 - const
    if track < 0:
        track += 175
    return track


def parseFileName(zipFile):
    ''' Get info from file name'''
    S1File = os.path.basename(zipFile)
    sat, mode, prodType, blank, pol, date1, date2, orbit, _, _ = \
        S1File.split('_')
    date1 = datetime.strptime(date1, '%Y%m%dT%H%M%S')
    date2 = datetime.strptime(date2, '%Y%m%dT%H%M%S')
    orbit = int(orbit)
    track = computeTrack(orbit, sat, date1)
    return track, orbit, date1, date2, sat


def filedName(zipFile):
    ''' Path of the .zip.1 marker that flags zipFile as filed.

    Only the extension changes. The old zipFile.replace('zip', 'zip.1') was a
    global replace on the absolute path, so it also rewrote any parent
    directory containing 'zip'.
    '''
    return f'{zipFile}.1'


def safeName(zipFile):
    ''' Name of the .SAFE directory a granule zip unpacks to. '''
    return f'{os.path.splitext(os.path.basename(zipFile))[0]}.SAFE'


def zipComplete(zipPath):
    '''
    True if the download finished (no aria2c .aria2 control file) and the file
    is a structurally valid zip (catches truncation cheaply).
    '''
    if not os.path.exists(zipPath) or os.path.exists(f'{zipPath}.aria2'):
        return False
    return zipfile.is_zipfile(zipPath)


def safeLooksComplete(safeDir):
    ''' Cheap sanity check that an extracted .SAFE is not a partial unzip.

    Deliberately does NOT look at measurement/: runPreProcTops consumes the
    measurement TIFFs, so an empty measurement/ is the normal state of an
    already-processed SAFE. Requiring one here would refile every processed
    granule in the archive. annotation/ and manifest.safe both survive
    processing, and a truncated unzip is unlikely to have landed both.
    '''
    return (os.path.exists(f'{safeDir}/manifest.safe')
            and len(glob.glob(f'{safeDir}/annotation/*')) > 0)


def alreadyDownloaded(assemblyDir, track, orbit, safeFile):
    ''' Determine if file downloaded earlier'''
    # checkFramesS1 splits a datatake into <orbit>_1 .. <orbit>_9, so a granule
    # may sit in any of those. Match _<digit> only: its <orbit>-<seq> output
    # dirs must not count as the granule's home.
    orbDirs = glob.glob(f'{assemblyDir}/track-{track}/{orbit}') + \
        glob.glob(f'{assemblyDir}/track-{track}/{orbit}_[0-9]*')
    #
    for orbDir in orbDirs:
        safe = f'{orbDir}/{safeFile}'
        if os.path.exists(safe):
            return safe
    return None


def runCommand(command, timeout=None):
    ''' Run command under csh and return its exit status (-1 if it could not
    be run at all).

    With shell=True a timeout kills the csh, which may leave the grandchild
    running; it unblocks the caller rather than guaranteeing a clean kill.
    '''
    try:
        return call(command, shell=True, executable='/bin/csh',
                    timeout=timeout)
    except Exception:
        # if missing files, reject to the NoResult directory
        helpers.mywarning(f'could not run \n{command}')
        return -1


def writeFiledRecord(filedFile, tracks, granules, merge=True):
    ''' Write a YAML record of the tracks touched and zip paths filed this run.

    Consumed by autoupdateS1 to decide which tracks/files later steps work on.
    With merge, any existing record is unioned in, so incremental writes during
    a run accumulate and a second run on the same day no longer clobbers the
    first. Written via a temp file so a reader on NFS never sees a partial
    record.
    '''
    if merge and os.path.exists(filedFile):
        try:
            with open(filedFile) as fp:
                old = yaml.safe_load(fp) or {}
            tracks = set(tracks) | set(old.get('tracks') or [])
            granules = set(granules) | set(old.get('granules') or [])
        except Exception as exc:
            helpers.mywarning(f'could not merge existing {filedFile}: {exc}')
    record = {'tracks': sorted(tracks),
              'granules': sorted(granules)}
    tmpFile = f'{filedFile}.tmp'
    with open(tmpFile, 'w') as fp:
        yaml.safe_dump(record, fp, default_flow_style=False)
    os.replace(tmpFile, filedFile)


def fileOneZip(zipFile, assemblyDir, overwrite=False, createTrackDir=False,
               check=False, timeout=None, excludeTracks=None, quiet=False):
    ''' Unpack one S1 SAFE zip into assemblyDir/track-<n>/<orbit>/, excluding
    cross-pol, and rename the source to .zip.1 only if the unzip succeeded.

    Thread safe: callable concurrently on granules of the same pass. Never
    raises for an expected failure, and never calls helpers.myerror -- that is
    sys.exit(), which inside a worker thread kills the thread silently.
    Returns (status, track, zipFile), status one of FILED/SKIPPED/CHECKED/ERROR.

    excludeTracks are never unpacked. The zip is left where it is rather than
    renamed to .zip.1, so dropping a track from the exclusion files it
    normally on the next pass.

    quiet passes unzip -q, which drops the per-file 'inflating:' lines and
    keeps the errors. For a caller unzipping on a background thread while
    something else writes to the same terminal, those lines are pure noise.
    '''
    zipFile = os.path.abspath(zipFile)
    mySafe = safeName(zipFile)
    try:
        track, orbit, date1, date2, sat = parseFileName(zipFile)
    except Exception:
        helpers.mywarning(f'cannot parse an S1 granule name from {zipFile}')
        return ERROR, None, zipFile
    if excludeTracks and track in excludeTracks:
        return SKIPPED, track, zipFile
    trackDir = f'{assemblyDir}/track-{track}'
    downloaded = alreadyDownloaded(assemblyDir, track, orbit, mySafe)
    # A run that died mid-unzip leaves a partial .SAFE, which alreadyDownloaded
    # cannot tell from a good one. Refile over it rather than skipping it for
    # ever (the gated rename below means its .zip is still here to refile from).
    refile = downloaded is not None and not safeLooksComplete(downloaded)
    if refile:
        helpers.mywarning(f'{downloaded} looks like a partial unzip; refiling')
    if downloaded is not None and not overwrite and not refile:
        return SKIPPED, track, zipFile
    if check:
        print(f'[check] would file {os.path.basename(zipFile)} '
              f'-> track-{track}/{orbit}')
        return CHECKED, track, zipFile
    # Never unzip a download that is still in flight or truncated: fileStage can
    # run while another invocation is downloading into the same archiveDir.
    if not zipComplete(zipFile):
        helpers.mywarning(f'{os.path.basename(zipFile)} is incomplete or not a valid '
                    'zip; leaving it for a later pass')
        return SKIPPED, track, zipFile
    try:
        with _dirLock:
            if not os.path.exists(trackDir):
                if not createTrackDir:
                    helpers.mywarning(f'{trackDir} does not exist, rerun with '
                                '--createTrackDir to create')
                    return ERROR, track, zipFile
                os.makedirs(trackDir, exist_ok=True)
            if downloaded is None:
                downloadDir = f'{trackDir}/{orbit}'
                os.makedirs(downloadDir, exist_ok=True)
            else:
                downloadDir = os.path.dirname(downloaded)
    except OSError as exc:
        helpers.mywarning(f'could not create the assembly dir for {zipFile}: {exc}')
        return ERROR, track, zipFile
    # `unzip -d <dir>` rather than the old `pushd <dir>; unzip -d ./; popd`, so
    # csh returns the unzip's own status instead of popd's (always 0). That is
    # what lets the rename below be conditional on a good unzip.
    flag = '-o' if (overwrite or refile) else '-u'
    command = f'unzip {flag} {"-q " if quiet else ""}{zipFile} ' \
        f'-x "*-slc-hv*"  -x "*-slc-vh*" -d {downloadDir}'
    status = runCommand(command, timeout=timeout)
    # unzip: 0 = ok, 1 = ok with warnings, >= 2 = a real failure.
    safeDir = f'{downloadDir}/{mySafe}'
    if status not in (0, 1) or not os.path.isdir(safeDir):
        helpers.mywarning(f'unzip failed (status {status}) for {zipFile}; leaving it '
                    'as .zip to retry on a later pass')
        return ERROR, track, zipFile
    if status == 1:
        helpers.mywarning(f'unzip completed with warnings for {zipFile}')
    if overwrite:
        for parFile in glob.glob(
                f'{downloadDir}/{date1.strftime("%Y%m%d")}*par'):
            os.remove(parFile)
    # The rename is the durable "this granule is done" marker: it drops the zip
    # out of the next run's glob, so it happens only after a good unzip.
    try:
        os.rename(zipFile, filedName(zipFile))
    except OSError as exc:
        helpers.mywarning(f'unzipped but could not rename {zipFile}: {exc}')
        return ERROR, track, zipFile
    return FILED, track, zipFile


def rearmPartialSafes(zipDir, assemblyDir, monthSubdirs=False, check=False):
    ''' Rename the .zip.1 of any incomplete .SAFE back to .zip so the normal
    pass refiles it. Returns the list of (safeDir, zipPath) re-armed.

    fileOneZip() already detects a partial unzip and refiles over it, but only
    for a granule whose zip is still called .zip -- and the rename to .zip.1 is
    the durable "this granule is done" marker that drops it from the glob. A
    .SAFE left incomplete while its zip was already renamed is therefore
    unreachable by that repair, for ever.

    Not hypothetical: one SAFE (a run killed mid-unzip, under older code that
    renamed unconditionally rather than gating on unzip status) sat partial in
    the tree from April to August 2026. Nothing noticed, because the damage
    surfaces two stages downstream -- runPreProcTops silently produces no
    SLC_tabs and trimTopsSLCsToFit then fails with a count mismatch that says
    nothing about which SAFE is bad.

    Scoped to unit directories that are not already processed. A partial .SAFE
    only matters where the unit still has to be assembled, and skipping the
    processed ones turns a stat per .SAFE into a stat per unit dir: measured on
    the Greenland tree, 7 minutes cold over NFS for all 62k .SAFEs against a
    few seconds this way. Cheap enough to run unconditionally, rather than
    behind a flag nobody would remember to set.

    "Processed" matches checkFramesS1.isProcessed(): a Completed marker, or an
    {orbit}-{seq} output dir beside the unit.
    '''
    rearmed = []
    for trackDir in sorted(glob.glob(f'{assemblyDir}/track-*')):
        # One listing per track, reused for both the unit walk and the
        # {orbit}-{seq} test. Globbing that test per unit instead re-lists the
        # track dir every time, which cost more than the whole unscoped scan.
        try:
            with os.scandir(trackDir) as entries:
                names = {e.name for e in entries if e.is_dir()}
        except OSError:
            continue
        for name in sorted(names):
            orbit, _, seq = name.partition('_')
            if f'{orbit}-{seq or "0"}' in names:
                continue                      # has an output dir: processed
            unitDir = os.path.join(trackDir, name)
            if os.path.exists(f'{unitDir}/Completed'):
                continue
            for safeDir in sorted(glob.glob(f'{unitDir}/*.SAFE')):
                rearmOneSafe(safeDir, zipDir, monthSubdirs, check, rearmed)
    return rearmed


def rearmOneSafe(safeDir, zipDir, monthSubdirs, check, rearmed):
    ''' Re-arm one .SAFE if it is a partial unzip. Appends to rearmed. '''
    if safeLooksComplete(safeDir):
        return
    zipName = os.path.basename(safeDir).replace('.SAFE', '.zip')
    pattern = (f'{zipDir}/*-*/{zipName}' if monthSubdirs
               else f'{zipDir}/{zipName}')
    if glob.glob(pattern):
        return                # still .zip: the normal pass already refiles it
    filedZips = glob.glob(f'{pattern}.1')
    if not filedZips:
        helpers.mywarning(f'{safeDir} is a partial unzip and its zip is gone; '
                    're-download that granule to repair it')
        return
    zipPath = filedZips[0]
    helpers.mywarning(f'{safeDir} is a partial unzip; re-arming '
                f'{os.path.basename(zipPath)} for refiling')
    if not check:
        os.rename(zipPath, zipPath[:-2])
    rearmed.append((safeDir, zipPath[:-2]))


def fileS1(zipDir, assemblyDir='.', monthSubdirs=False, filed=None,
           overwrite=False, createTrackDir=False, check=False, maxThreads=4,
           excludeTracks=None):
    ''' Unpack S1 SAFE zips from zipDir into assemblyDir/track-<n>/<orbit>/.

    With monthSubdirs, zips are globbed from zipDir/<YYYY-MM>/*.zip across all
    month subdirs; otherwise from a flat zipDir/*.zip. With check, report what
    would be filed without unpacking, renaming, or writing anything. Returns
    (tracks, granules): the set of tracks touched and the list of source zip
    paths filed this run.

    excludeTracks are dropped before anything else, in particular before the
    missing-track-dir check below: that calls helpers.myerror, so an excluded track
    whose directory has been removed would otherwise abort the whole run.

    A batch driver over fileOneZip, which autoupdateS1 also calls per granule
    as each download is reduced.
    '''
    assemblyDir = os.path.abspath(assemblyDir)
    excluded = set(excludeTracks or [])
    # Before the glob, so anything re-armed is picked up by this same pass.
    rearmPartialSafes(zipDir, assemblyDir, monthSubdirs=monthSubdirs,
                      check=check)
    if monthSubdirs:
        zipFiles = glob.glob(f'{zipDir}/*-*/*.zip')
    else:
        zipFiles = glob.glob(f'{zipDir}/*.zip')
    #
    threads = []
    tracks = set()
    granules = []
    lock = threading.Lock()

    def worker(zipFile):
        status, track, zipFile = fileOneZip(
            zipFile, assemblyDir, overwrite=overwrite,
            createTrackDir=createTrackDir, check=check,
            excludeTracks=excluded)
        if status in (FILED, CHECKED):
            with lock:
                tracks.add(track)
                granules.append(zipFile)

    for zipFile in zipFiles:
        # Absolute so the per-zip unzip command is cwd-independent.
        zipFile = os.path.abspath(zipFile)
        if excluded:
            try:
                if parseFileName(zipFile)[0] in excluded:
                    continue
            except Exception:
                # Unparseable names are fileOneZip's to report, not ours
                pass
        # A missing track dir is a hard error for the CLI, as it always was.
        # It has to happen here rather than in fileOneZip: helpers.myerror is
        # sys.exit(), which a worker thread would swallow.
        if not check and not createTrackDir:
            track = parseFileName(zipFile)[0]
            trackDir = f'{assemblyDir}/track-{track}'
            if not os.path.exists(trackDir):
                helpers.myerror(f'{trackDir} does not exist, rerun with '
                          '--createTrackDir to create')
        if check:
            # Single threaded so the reported order stays deterministic.
            worker(zipFile)
        else:
            threads.append(threading.Thread(target=worker, args=[zipFile]))

    if not check:
        helpers.runMyThreads(threads, maxThreads, 'unzip data')
    if filed is not None and not check:
        writeFiledRecord(filed, tracks, granules)
    return tracks, granules


def main():
    ''' File S1 SAFE zips into the per-track/per-orbit assembly tree. '''
    args = fileS1Args()
    print(vars(args))
    fileS1(zipDir=args.zipDir, assemblyDir=args.assemblyDir,
           monthSubdirs=args.monthSubdirs, filed=args.filed,
           overwrite=args.overwrite, createTrackDir=args.createTrackDir,
           check=args.check,
           excludeTracks=[int(x) for x in
                          args.excludeTracks.replace(',', ' ').split()])


if __name__ == '__main__':
    main()
