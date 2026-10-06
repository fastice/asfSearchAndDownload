#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checkExcludeFrames - verify the per-track excludeFrames lists are safe.

Each <assemblyDir>/track-N/excludeFrames lists ASF frame numbers that a
region-wide search outline clips in but that track's frameRange puts out of
range. Excluding them at search time stops them being downloaded, reduced,
filed and then binned to track-N/tmp on every cycle.

The dangerous mistake is excluding a frame the track actually uses, which would
silently starve it. This checks each listed frame against the frames of the
SAFEs in that track's real unit directories, and reports a CLASH if one is in
use. It also reports how many binned SAFEs in tmp/ each exclusion accounts for,
so an entry that explains nothing can be questioned.

Frame numbers come from the search GeoPackages (searchResults/gpkg/*.gpkg),
which are the only place granule -> (track, frame) is recorded. A granule older
than every kept gpkg cannot be resolved; those are reported as unmatched rather
than assumed safe.

Run it after editing any excludeFrames, and after widening a frameRange.

@author: ian
"""
import argparse
import collections
import glob
import os
import sys

import geopandas as gpd

import utilities as u


def checkExcludeFramesArgs():
    ''' Handle command line args'''
    parser = argparse.ArgumentParser(
        description='\033[1mVerify per-track excludeFrames lists\033[0m',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('--assemblyDir', type=str, required=True,
                        help='root holding the track-N/ directories')
    parser.add_argument('--gpkgDir', type=str, required=True,
                        help='directory of search GeoPackages '
                        '(searchResults/gpkg)')
    parser.add_argument('--tracks', type=int, nargs='+', default=None,
                        metavar='N', help='restrict to these tracks [all]')
    args = parser.parse_args()
    return args.assemblyDir, args.gpkgDir, args.tracks


def granuleFrames(gpkgDir):
    ''' {granule stem: (track, frame)} unioned over every search gpkg. '''
    known = {}
    for path in sorted(glob.glob(os.path.join(gpkgDir, '*.gpkg'))):
        try:
            table = gpd.read_file(path)
        except Exception as exc:
            u.mywarning(f'could not read {path}: {exc}')
            continue
        if 'granule' not in table.columns:
            continue
        for granule, track, frame in zip(table.granule, table.track,
                                         table.frame):
            if granule is None or track is None or frame is None:
                continue
            known[str(granule).replace('.zip', '')] = (int(track), int(frame))
    return known


def readExcludeFrames(trackDir):
    ''' Frames listed in trackDir/excludeFrames, or [] if there is no file. '''
    path = os.path.join(trackDir, 'excludeFrames')
    if not os.path.exists(path):
        return []
    frames = []
    with open(path) as fp:
        for line in fp:
            line = line.split('#')[0]
            frames += [int(f) for f in line.replace(',', ' ').split()]
    return sorted(set(frames))


def safeStems(directory):
    ''' Granule stems of the .SAFEs directly inside directory. '''
    try:
        return [f[:-5] for f in os.listdir(directory) if f.endswith('.SAFE')]
    except OSError:
        return []


def trackUsage(trackDir, known):
    '''
    (inUse, binned, unmatched) frame counters for one track: frames of SAFEs in
    its unit dirs, frames of SAFEs binned in tmp/, and how many SAFEs no gpkg
    could resolve.
    '''
    inUse, binned = collections.Counter(), collections.Counter()
    unmatched = 0
    for entry in sorted(glob.glob(f'{trackDir}/*')):
        if not os.path.isdir(entry):
            continue
        counter = binned if os.path.basename(entry) == 'tmp' else inUse
        for stem in safeStems(entry):
            if stem in known:
                counter[known[stem][1]] += 1
            else:
                unmatched += 1
    return inUse, binned, unmatched


def main():
    assemblyDir, gpkgDir, wanted = checkExcludeFramesArgs()
    known = granuleFrames(gpkgDir)
    if not known:
        u.myerror(f'no granule/track/frame records found in {gpkgDir}/*.gpkg')
    print(f'{len(known)} granules resolvable from {gpkgDir}\n')

    nClash = 0
    nChecked = 0
    for trackDir in sorted(glob.glob(os.path.join(assemblyDir, 'track-*')),
                           key=lambda p: int(p.rsplit('track-', 1)[-1])):
        track = int(trackDir.rsplit('track-', 1)[-1])
        if wanted and track not in wanted:
            continue
        frames = readExcludeFrames(trackDir)
        if not frames:
            continue
        nChecked += 1
        inUse, binned, unmatched = trackUsage(trackDir, known)
        for frame in frames:
            used = inUse.get(frame, 0)
            explains = binned.get(frame, 0)
            if used:
                nClash += 1
                verdict = f'\033[1;31mCLASH: {used} in-range SAFE(s)\033[0m'
            elif explains:
                verdict = f'ok - accounts for {explains} binned SAFE(s)'
            else:
                verdict = 'ok - but explains no binned SAFE here'
            print(f'track-{track:<4} frame {frame:<5} {verdict}')
        if unmatched:
            print(f'track-{track:<4} ({unmatched} SAFE(s) older than every '
                  f'gpkg, frame unknown)')

    print(f'\n{nChecked} track(s) with exclusions, {nClash} clash(es)')
    if nClash:
        print('A clash means the track uses that frame: remove it before the '
              'next search or those acquisitions stop arriving.')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
