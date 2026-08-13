#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jun 18 15:11:35 2021

@author: ian
"""

import argparse
import utilities as u
import os
from datetime import datetime
from subprocess import call
import threading
import glob
import yaml
import zipfile


# fileOneZip outcomes.
FILED, SKIPPED, CHECKED, ERROR = 'filed', 'skipped', 'checked', 'error'

# Serializes track-/orbit-dir creation. Two frames of one pass share
# track-<n>/<orbit>, and fileOneZip runs concurrently from both the batch pool
# here and autoupdateS1's filing pipeline.
_dirLock = threading.Lock()


def fileS1Args():
    ''' Handle command line args'''
    parser = argparse.ArgumentParser(
        description='\033[1m File S1 images in Data dir \033[0m',
        epilog='Notes:  ', allow_abbrev='False')
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
    parser.add_argument('--check', action='store_true', default=False,
                        help='Dry run: report what would be filed without '
                        'unpacking, renaming, or writing anything')
    args = parser.parse_args()
    return args


def computeTrack(orbit, sat):
    satConst = {'S1A': 72, 'S1B': 26, 'S1C': 171, 'S1D': 41}
    track = orbit % 175 - satConst[sat]
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
    track = computeTrack(orbit, sat)
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
        u.mywarning(f'could not run \n{command}')
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
            u.mywarning(f'could not merge existing {filedFile}: {exc}')
    record = {'tracks': sorted(tracks),
              'granules': sorted(granules)}
    tmpFile = f'{filedFile}.tmp'
    with open(tmpFile, 'w') as fp:
        yaml.safe_dump(record, fp, default_flow_style=False)
    os.replace(tmpFile, filedFile)


def fileOneZip(zipFile, assemblyDir, overwrite=False, createTrackDir=False,
               check=False, timeout=None):
    ''' Unpack one S1 SAFE zip into assemblyDir/track-<n>/<orbit>/, excluding
    cross-pol, and rename the source to .zip.1 only if the unzip succeeded.

    Thread safe: callable concurrently on granules of the same pass. Never
    raises for an expected failure, and never calls u.myerror -- that is
    sys.exit(), which inside a worker thread kills the thread silently.
    Returns (status, track, zipFile), status one of FILED/SKIPPED/CHECKED/ERROR.
    '''
    zipFile = os.path.abspath(zipFile)
    mySafe = safeName(zipFile)
    try:
        track, orbit, date1, date2, sat = parseFileName(zipFile)
    except Exception:
        u.mywarning(f'cannot parse an S1 granule name from {zipFile}')
        return ERROR, None, zipFile
    trackDir = f'{assemblyDir}/track-{track}'
    downloaded = alreadyDownloaded(assemblyDir, track, orbit, mySafe)
    # A run that died mid-unzip leaves a partial .SAFE, which alreadyDownloaded
    # cannot tell from a good one. Refile over it rather than skipping it for
    # ever (the gated rename below means its .zip is still here to refile from).
    refile = downloaded is not None and not safeLooksComplete(downloaded)
    if refile:
        u.mywarning(f'{downloaded} looks like a partial unzip; refiling')
    if downloaded is not None and not overwrite and not refile:
        return SKIPPED, track, zipFile
    if check:
        print(f'[check] would file {os.path.basename(zipFile)} '
              f'-> track-{track}/{orbit}')
        return CHECKED, track, zipFile
    # Never unzip a download that is still in flight or truncated: fileStage can
    # run while another invocation is downloading into the same archiveDir.
    if not zipComplete(zipFile):
        u.mywarning(f'{os.path.basename(zipFile)} is incomplete or not a valid '
                    'zip; leaving it for a later pass')
        return SKIPPED, track, zipFile
    try:
        with _dirLock:
            if not os.path.exists(trackDir):
                if not createTrackDir:
                    u.mywarning(f'{trackDir} does not exist, rerun with '
                                '--createTrackDir to create')
                    return ERROR, track, zipFile
                os.makedirs(trackDir, exist_ok=True)
            if downloaded is None:
                downloadDir = f'{trackDir}/{orbit}'
                os.makedirs(downloadDir, exist_ok=True)
            else:
                downloadDir = os.path.dirname(downloaded)
    except OSError as exc:
        u.mywarning(f'could not create the assembly dir for {zipFile}: {exc}')
        return ERROR, track, zipFile
    # `unzip -d <dir>` rather than the old `pushd <dir>; unzip -d ./; popd`, so
    # csh returns the unzip's own status instead of popd's (always 0). That is
    # what lets the rename below be conditional on a good unzip.
    flag = '-o' if (overwrite or refile) else '-u'
    command = f'unzip {flag} {zipFile} -x "*-slc-hv*"  -x "*-slc-vh*" ' \
        f'-d {downloadDir}'
    status = runCommand(command, timeout=timeout)
    # unzip: 0 = ok, 1 = ok with warnings, >= 2 = a real failure.
    safeDir = f'{downloadDir}/{mySafe}'
    if status not in (0, 1) or not os.path.isdir(safeDir):
        u.mywarning(f'unzip failed (status {status}) for {zipFile}; leaving it '
                    'as .zip to retry on a later pass')
        return ERROR, track, zipFile
    if status == 1:
        u.mywarning(f'unzip completed with warnings for {zipFile}')
    if overwrite:
        for parFile in glob.glob(
                f'{downloadDir}/{date1.strftime("%Y%m%d")}*par'):
            os.remove(parFile)
    # The rename is the durable "this granule is done" marker: it drops the zip
    # out of the next run's glob, so it happens only after a good unzip.
    try:
        os.rename(zipFile, filedName(zipFile))
    except OSError as exc:
        u.mywarning(f'unzipped but could not rename {zipFile}: {exc}')
        return ERROR, track, zipFile
    return FILED, track, zipFile


def fileS1(zipDir, assemblyDir='.', monthSubdirs=False, filed=None,
           overwrite=False, createTrackDir=False, check=False, maxThreads=4):
    ''' Unpack S1 SAFE zips from zipDir into assemblyDir/track-<n>/<orbit>/.

    With monthSubdirs, zips are globbed from zipDir/<YYYY-MM>/*.zip across all
    month subdirs; otherwise from a flat zipDir/*.zip. With check, report what
    would be filed without unpacking, renaming, or writing anything. Returns
    (tracks, granules): the set of tracks touched and the list of source zip
    paths filed this run.

    A batch driver over fileOneZip, which autoupdateS1 also calls per granule
    as each download is reduced.
    '''
    assemblyDir = os.path.abspath(assemblyDir)
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
            createTrackDir=createTrackDir, check=check)
        if status in (FILED, CHECKED):
            with lock:
                tracks.add(track)
                granules.append(zipFile)

    for zipFile in zipFiles:
        # Absolute so the per-zip unzip command is cwd-independent.
        zipFile = os.path.abspath(zipFile)
        # A missing track dir is a hard error for the CLI, as it always was.
        # It has to happen here rather than in fileOneZip: u.myerror is
        # sys.exit(), which a worker thread would swallow.
        if not check and not createTrackDir:
            track = parseFileName(zipFile)[0]
            trackDir = f'{assemblyDir}/track-{track}'
            if not os.path.exists(trackDir):
                u.myerror(f'{trackDir} does not exist, rerun with '
                          '--createTrackDir to create')
        if check:
            # Single threaded so the reported order stays deterministic.
            worker(zipFile)
        else:
            threads.append(threading.Thread(target=worker, args=[zipFile]))

    if not check:
        u.runMyThreads(threads, maxThreads, 'unzip data')
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
           check=args.check)


if __name__ == '__main__':
    main()
