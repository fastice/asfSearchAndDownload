#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
checkFramesS1 - vet filed Sentinel-1 datatakes and queue them for processing.

Reproduces the burst-"frame" checks of s1setup/checkframes.py against the
assemblyDir/track-<n>/<orbit>/ tree that fileS1 builds, then restructures each
datatake into clean processing units and routes them into three cumulative
queues:

  toProcess          - passes all checks and its precise (EOF) orbit is available
  pendingProcessing  - passes checks but the EOF orbit is not published yet
                       (a later step, not here, promotes these once it arrives)
  problem            - an unresolved gap / over-length / out-of-range issue

"frame" == burst number counted from the ascending-node time at the 2.759 s
burst period (identical to checkframes.py). There is no ESA/ASF S1 "frame"
concept here.

Part of the asfSearchAndDownload package.
"""
import argparse
import glob
import os
import re
import shutil
from datetime import datetime, timedelta

import utilities as u

from asfsearchdownload import refreshOrbits
from asfsearchdownload import queueS1

BURST_PERIOD = 2.759          # seconds per S1 IW burst (from checkframes.py)
MAX_FRAMES = 127              # processor limit; > 127 in-range bursts must split
DEFAULT_FRAME_RANGE = [300, 750]
GAP_SUFFIX_START = 1         # far-side gap segments -> <orbit>_1, _2, _3, ...
SPLIT_SUFFIX = 4             # long-split head       -> <orbit>_4
QUEUES = queueS1.QUEUES
QUEUE_LABEL = {'toProcess': 'toProcess', 'pendingProcessing': 'pending',
               'problem': 'problem'}


# --------------------------------------------------------------------------- #
# orbit-dir / SAFE-name helpers (reproduced from s1setup)
# --------------------------------------------------------------------------- #
def isSourceOrbitDir(name):
    ''' True if name is a raw orbit dir: 4-5 digits with optional _seq suffix. '''
    return bool(re.match(r'^\d{4,5}(_\d+)?$', name))


def parseOrbitSeq(name):
    ''' '4547' -> (4547, 0), '4547_3' -> (4547, 3). '''
    if '_' in name:
        parts = name.split('_')
        return int(parts[0]), int(parts[-1])
    return int(name), 0


def parseSafeName(safePath):
    ''' Return (sensor, startTime, stopTime) from an S1 .SAFE basename. '''
    pieces = os.path.basename(safePath).split('_')
    sensor = pieces[0]
    time1 = datetime.strptime(pieces[5], '%Y%m%dT%H%M%S')
    time2 = datetime.strptime(pieces[6], '%Y%m%dT%H%M%S')
    return sensor, time1, time2


def readFrameRange(trackDir):
    ''' Read trackDir/frameRange (two ints), else the default extended range. '''
    try:
        with open(os.path.join(trackDir, 'frameRange')) as fp:
            low, high = fp.readline().split()[:2]
            return [int(low), int(high)]
    except Exception:
        return list(DEFAULT_FRAME_RANGE)


def getAscNodeTime(orbitPath, check=False):
    ''' Return the ascending-node datetime for an orbit dir, caching it in
    <orbit>/ascendingNodeTime (unless check, which never writes). '''
    cache = os.path.join(orbitPath, 'ascendingNodeTime')
    if os.path.isfile(cache):
        with open(cache) as fp:
            return datetime.strptime(fp.readline().strip(),
                                     '%Y-%m-%dT%H:%M:%S.%f')
    xmls = sorted(glob.glob(f'{orbitPath}/*.SAFE/annotation/*iw1*.xml'))
    if not xmls:
        raise ValueError(f'no iw1 annotation XML under {orbitPath}')
    ascStr = None
    with open(xmls[0]) as fp:
        for line in fp:
            if 'ascendingNodeTime' in line:
                ascStr = line.split('>')[1].split('<')[0]
                break
    if ascStr is None:
        raise ValueError(f'no ascendingNodeTime in {xmls[0]}')
    if not check:
        with open(cache, 'w') as fp:
            print(ascStr, end='', file=fp)
    return datetime.strptime(ascStr, '%Y-%m-%dT%H:%M:%S.%f')


def getOrbitSafe(orbitPath, ascNodeTime):
    ''' Return per-SAFE rows [orbit, sensor, t1, t2, burst1, burst2, safePath]
    sorted by name, with burst numbers relative to the ascending node. '''
    rows = []
    for safe in sorted(glob.glob(f'{orbitPath}/*.SAFE')):
        sensor, time1, time2 = parseSafeName(safe)
        burst1 = round((time1 - ascNodeTime).seconds / BURST_PERIOD)
        burst2 = round((time2 - ascNodeTime).seconds / BURST_PERIOD)
        rows.append([os.path.basename(orbitPath), sensor, time1, time2,
                     burst1, burst2, safe])
    return rows


# --------------------------------------------------------------------------- #
# checks (reproduced from checkframes.py)
# --------------------------------------------------------------------------- #
def checkGaps(info):
    ''' Return indices i where info[i+1] starts more than one burst after
    info[i] ends (i.e. a datatake gap between consecutive SAFEs). '''
    gaps = []
    for i in range(len(info) - 1):
        if info[i + 1][4] - info[i][5] > 1:
            gaps.append(i)
    return gaps


def splitByGaps(info, gaps):
    ''' Split info into contiguous segments at the gap indices. '''
    segments = []
    prev = 0
    for g in gaps:
        segments.append(info[prev:g + 1])
        prev = g + 1
    segments.append(info[prev:])
    return segments


def outOfRange(info, frameRange):
    ''' Return the indices of SAFEs wholly before (early) or after (late) the
    frame range. '''
    low, high = frameRange
    early = [i for i, r in enumerate(info) if r[5] < low]
    late = [i for i, r in enumerate(info) if r[4] > high]
    return set(early) | set(late)


def rangeSpan(segment, frameRange):
    ''' Number of bursts of a contiguous segment that fall inside the frame
    range (the span clamped to [low, high]). A datatake whose SAFEs extend past
    the range is only "over-length" if this in-range span exceeds the processor
    limit -- the out-of-range remainder is trimmed later by trimTopsSLCsToFit. '''
    low, high = frameRange
    first = max(segment[0][4], low)
    last = min(segment[-1][5], high)
    return last - first + 1


def splitN(segment, frameRange):
    ''' Number of leading SAFEs that go into the long-split head: the largest
    head whose in-range burst span stays within MAX_FRAMES. Derived only from
    frameRange (which is per-track), so every orbit in a track splits at the
    same burst boundary. Existing hand-made splits are deliberately not read --
    they are not consistently numbered, so their suffix cannot identify them. '''
    first = max(segment[0][4], frameRange[0])
    n = sum(1 for r in segment if r[5] - first + 1 <= MAX_FRAMES)
    return min(max(n, 1), len(segment) - 1)


# --------------------------------------------------------------------------- #
# per-orbit analysis -> planned units + moves
# --------------------------------------------------------------------------- #
def analyzeOrbit(trackDir, orbit, frameRange, opts):
    ''' Analyze one bare orbit dir. Returns (units, moves, toTmp, residual):
        units    - dict unitName -> list of SAFE paths (post-fix membership)
        moves    - list of (safePath, destUnitName) for gap/split restructuring
        toTmp    - list of SAFE paths to move to track tmp/ (out of range)
        residual - dict unitName -> unresolved-issue reason (or None)
    In --check nothing is moved; the plan is still computed for reporting. '''
    barePath = os.path.join(trackDir, str(orbit))
    info = getOrbitSafe(barePath, getAscNodeTime(barePath, opts['check']))
    units, moves, toTmp, residual = {}, [], [], {}

    # out-of-range trim
    oor = outOfRange(info, frameRange)
    stays = []
    if opts['noMoveOutOfRange']:
        stays = [info[i] for i in sorted(oor)]
    else:
        toTmp = [info[i][6] for i in sorted(oor)]
    inRange = [r for i, r in enumerate(info) if i not in oor]

    diag = {'info': info, 'inRange': inRange, 'gaps': []}
    if not inRange:
        units[str(orbit)] = list(info)
        residual[str(orbit)] = 'out-of-range'
        return units, moves, toTmp, residual, diag

    # gap segmentation: the first (near-side) segment stays in <orbit>; each
    # later (far-side) segment moves out to <orbit>_1, _2, ...
    gaps = checkGaps(inRange)
    diag['gaps'] = gaps
    gapResidual = False
    if gaps and not opts['noBreakGap']:
        segments = splitByGaps(inRange, gaps)
        bareSeg = segments[0]
        suffix = GAP_SUFFIX_START
        for seg in segments[1:]:
            uname = f'{orbit}_{suffix}'
            units[uname] = list(seg)
            moves += [(r[6], uname) for r in seg]
            suffix += 1
    else:
        bareSeg = inRange
        gapResidual = bool(gaps)      # gaps left in place (noBreakGap)

    # over-length split of the bare segment. Skipped when a gap was left in
    # place (noBreakGap) -- the burst span across a gap is not a real length.
    overResidual = False
    splitDir = os.path.join(trackDir, f'{orbit}_{SPLIT_SUFFIX}')
    if not gapResidual and rangeSpan(bareSeg, frameRange) > MAX_FRAMES:
        if not opts['noSplit'] and len(bareSeg) > 1 \
                and not os.path.exists(splitDir):
            n = splitN(bareSeg, frameRange)
            head, bareSeg = bareSeg[:n], bareSeg[n:]
            uname = f'{orbit}_{SPLIT_SUFFIX}'
            units[uname] = list(head)
            moves += [(r[6], uname) for r in head]
        else:
            overResidual = True       # noSplit, or <orbit>_4 already exists

    units[str(orbit)] = list(bareSeg) + stays
    residual[str(orbit)] = ('gap' if gapResidual else
                            'over-length' if overResidual else
                            'out-of-range' if stays else None)

    # pre-existing _N units from earlier runs (route them too)
    for other in sorted(glob.glob(os.path.join(trackDir, f'{orbit}_*'))):
        base = os.path.basename(other)
        if isSourceOrbitDir(base) and base not in units:
            try:
                units[base] = getOrbitSafe(other,
                                           getAscNodeTime(other, opts['check']))
            except Exception:
                units[base] = []
            residual[base] = residualForDir(other, frameRange, opts['check'])
    return units, moves, toTmp, residual, diag


def residualForDir(unitPath, frameRange, check=False):
    ''' Recompute the unresolved-issue reason for an already-existing unit dir. '''
    try:
        info = getOrbitSafe(unitPath, getAscNodeTime(unitPath, check))
    except Exception as exc:
        return f'error: {exc}'
    if not info:
        return None
    if checkGaps(info):
        return 'gap'
    inRange = [r for i, r in enumerate(info)
               if i not in outOfRange(info, frameRange)]
    if not inRange:
        return 'out-of-range'
    if rangeSpan(inRange, frameRange) > MAX_FRAMES:
        return 'over-length'
    return None


# --------------------------------------------------------------------------- #
# routing & reporting
# --------------------------------------------------------------------------- #
def unitFrames(rows, frameRange):
    ''' In-range (start, end, total) burst frames for a unit's SAFE rows. '''
    low, high = frameRange
    start = max(rows[0][4], low)
    end = min(rows[-1][5], high)
    return start, end, end - start + 1


def frameStr(rows, frameRange):
    ''' "start-end (total)" in-range frames for a unit, or "none" if empty. '''
    if not rows:
        return 'none'
    start, end, total = unitFrames(rows, frameRange)
    return f'{start}-{end} ({total})'


def headerLine(trackName, orbit, dateStr, dayStr, info, inStr, tag, frameRange):
    ''' The one-line-per-orbit summary; only tag (the trailing status) varies. '''
    lo, hi = frameRange
    return (f'{trackName}/{orbit}  {dateStr}{dayStr}  {len(info)} SAFE  '
            f'frames {info[0][4]}-{info[-1][5]}  range [{lo},{hi}]  '
            f'in-range {inStr} -> {tag}')


def unitRecord(trackName, unitName, rows, frameRange):
    ''' Queue record for a unit: identity, date, and in-range frame extent. '''
    start, end, total = unitFrames(rows, frameRange)
    return {'unit': f'{trackName}/{unitName}',
            'orbit': unitName,
            'date': rows[0][2].strftime('%Y-%m-%d'),
            'startFrame': start,
            'endFrame': end,
            'totalFrames': total}


def isProcessed(trackDir, unitName):
    ''' True if the unit is already fully processed: a Completed marker or its
    {orbit}-{seq} output dir exists (mirrors setupTrack.classifyOrbits). '''
    unitPath = os.path.join(trackDir, unitName)
    orbit, seq = parseOrbitSeq(unitName)
    return (os.path.exists(os.path.join(unitPath, 'Completed')) or
            os.path.isdir(os.path.join(trackDir, f'{orbit}-{seq}')))


def routeUnit(trackDir, unitName, rows, residual, orbitDir):
    ''' Classify one processing unit. Returns (queue, reason) where queue is one
    of QUEUES or None (skip). Being already queued is handled by the caller. '''
    unitPath = os.path.join(trackDir, unitName)
    if os.path.exists(os.path.join(unitPath, 'Ignore')):
        return None, 'Ignore file'
    if not rows:
        return None, 'no SAFE'
    if residual:
        return 'problem', residual
    if os.path.exists(os.path.join(unitPath, 'Failed')):
        return 'problem', 'previous run Failed'
    if refreshOrbits.orbitFileReady(orbitDir, rows[0][1], rows[0][2]):
        return 'toProcess', 'orbit ready'
    return 'pendingProcessing', 'no orbit'


# --------------------------------------------------------------------------- #
# cumulative, idempotent queue files
#
# The format itself lives in queueS1 so setupTrack (s1setup) can consume the
# queues without importing this module. Re-exported here under the old names.
# --------------------------------------------------------------------------- #
queuePath = queueS1.queuePath
entryUnit = queueS1.entryUnit
readQueue = queueS1.readQueue
writeQueues = queueS1.writeQueues


def newDeltas():
    ''' Empty delta accumulator for applyQueueDeltas. '''
    return {'add': {name: [] for name in QUEUES},
            'remove': {name: [] for name in QUEUES}}


def queueUnit(queues, newEntries, deltas, queue, record, reason=None,
              source='checkFramesS1'):
    ''' Add one record to a queue, recording it for the caller and for the
    on-disk delta. problem entries carry the reason as their comment. '''
    if queue == 'problem':
        record = queueS1.problemRecord(record['unit'], reason or 'problem',
                                       source, base=record)
    unit = entryUnit(record)
    if unit not in {entryUnit(e) for e in queues[queue]}:
        queues[queue].append(record)
    newEntries[queue].append(record)
    deltas['add'][queue].append(record)
    return record


def promotePending(queues, newEntries, deltas, assemblyDir, orbitDir, check):
    ''' Re-evaluate every pendingProcessing unit against the current state and
    move any whose status changed (typically its orbit arrived) out of pending
    into toProcess (or problem). Mutates queues in place; returns the count moved.

    Promotions land in newEntries too, so a unit that becomes a problem here is
    visible to the caller (and therefore to the notification) rather than only
    being printed. '''
    stillPending = []
    moved = 0
    for rec in queues['pendingProcessing']:
        unit = entryUnit(rec)
        if '/' not in unit:
            stillPending.append(rec)
            continue
        trackName, unitName = unit.split('/', 1)
        trackDir = os.path.join(assemblyDir, trackName)
        unitPath = os.path.join(trackDir, unitName)
        if not glob.glob(f'{unitPath}/*.SAFE'):
            stillPending.append(rec)          # unit gone/unreadable; leave as-is
            continue
        frameRange = readFrameRange(trackDir)
        try:
            rows = getOrbitSafe(unitPath, getAscNodeTime(unitPath, check))
        except Exception:
            stillPending.append(rec)
            continue
        residual = residualForDir(unitPath, frameRange, check)
        queue, reason = routeUnit(trackDir, unitName, rows, residual, orbitDir)
        if queue in (None, 'pendingProcessing'):
            stillPending.append(rec)          # still waiting (or now ignored)
            continue
        queueUnit(queues, newEntries, deltas, queue,
                  unitRecord(trackName, unitName, rows, frameRange), reason)
        deltas['remove']['pendingProcessing'].append(unit)
        print(f'{unit}: pendingProcessing -> {queue} ({reason})')
        moved += 1
    queues['pendingProcessing'] = stillPending
    return moved


def promoteProblem(queues, newEntries, deltas, assemblyDir, orbitDir, check):
    ''' Re-evaluate every problem unit and drop it once the condition clears.

    Without this the problem queue is append-only and the caller's "already
    queued" rule means a unit in it is never re-routed -- so a transient (or one
    fixed by hand) stays stuck for ever, and having been notified once, silently.
    Returns the count cleared. '''
    stillProblem = []
    cleared = 0
    for rec in queues['problem']:
        unit = entryUnit(rec)
        if '/' not in unit:
            stillProblem.append(rec)
            continue
        trackName, unitName = unit.split('/', 1)
        trackDir = os.path.join(assemblyDir, trackName)
        unitPath = os.path.join(trackDir, unitName)
        # Never drop an entry just because the unit is not readable: that is how
        # a wrong assemblyDir would quietly empty the queue.
        if not glob.glob(f'{unitPath}/*.SAFE'):
            stillProblem.append(rec)
            continue
        if isProcessed(trackDir, unitName):
            deltas['remove']['problem'].append(unit)
            print(f'{unit}: problem -> processed, cleared')
            cleared += 1
            continue
        frameRange = readFrameRange(trackDir)
        try:
            rows = getOrbitSafe(unitPath, getAscNodeTime(unitPath, check))
        except Exception:
            stillProblem.append(rec)
            continue
        residual = residualForDir(unitPath, frameRange, check)
        queue, reason = routeUnit(trackDir, unitName, rows, residual, orbitDir)
        if queue == 'problem':
            stillProblem.append(rec)
            # Refresh the comment only for our own entries: routeUnit reports the
            # generic 'previous run Failed' for the marker setupTrack writes,
            # which would otherwise clobber its specific failure detail.
            if (isinstance(rec, dict) and reason != rec.get('comment')
                    and rec.get('source', 'checkFramesS1') == 'checkFramesS1'):
                deltas.setdefault('update', {}).setdefault('problem', {})[unit] \
                    = {'comment': reason, 'notified': None}
            continue
        if queue is None:
            stillProblem.append(rec)          # Ignore file / no SAFE: leave it
            continue
        deltas['remove']['problem'].append(unit)
        queueUnit(queues, newEntries, deltas, queue,
                  unitRecord(trackName, unitName, rows, frameRange), reason)
        print(f'{unit}: problem -> {queue} ({reason})')
        cleared += 1
    queues['problem'] = stillProblem
    return cleared


# --------------------------------------------------------------------------- #
# core
# --------------------------------------------------------------------------- #
def trackNumber(trackDir):
    '''
    Track number from a track-<n> directory path, or None if it is not one.
    '''
    name = os.path.basename(str(trackDir).rstrip('/'))
    number = name[len('track-'):] if name.startswith('track-') else ''
    return int(number) if number.isdigit() else None


def checkFrames(assemblyDir='.', track=None, orbitDir=None, firstDate=None,
                lastDate=None, queueDir=None, check=False, noSplit=False,
                noBreakGap=False, noMoveOutOfRange=False, excludeTracks=None):
    ''' Vet in-range datatakes under assemblyDir, restructure each into clean
    processing units, and route each into the cumulative toProcess /
    pendingProcessing / problem queues. Returns the new entries per queue.

    assemblyDir holds the track-<n>/ dirs and is where the queues live. If track
    (e.g. 'track-16' or '16') is given, only that track is scanned; otherwise
    every track-<n>/ under assemblyDir is.

    excludeTracks are never scanned, so nothing of theirs reaches a queue. An
    explicit track wins: asking for one by name overrides the exclusion. '''
    assemblyDir = os.path.abspath(assemblyDir)
    excluded = set(excludeTracks or [])
    if track:
        name = os.path.basename(str(track).rstrip('/'))
        if not name.startswith('track-'):
            name = f'track-{name}'
        trackDirs = [os.path.join(assemblyDir, name)]
        if not os.path.isdir(trackDirs[0]):
            u.myerror(f'checkFramesS1: no such track dir {trackDirs[0]}')
    else:
        trackDirs = sorted(glob.glob(os.path.join(assemblyDir, 'track-*')))
        if excluded:
            trackDirs = [d for d in trackDirs
                         if trackNumber(d) not in excluded]
            print('checkFramesS1: excluding tracks ' +
                  ' '.join(str(t) for t in sorted(excluded)))
    orbitDir = orbitDir or refreshOrbits.DEFAULT_ORBIT_DIR
    queueDir = queueS1.resolveQueueDir(assemblyDir, queueDir,
                                       migrate=not check)
    if firstDate is None:
        firstDate = datetime.now() - timedelta(days=185)
    if lastDate is None:
        lastDate = datetime.now() + timedelta(days=1)
    opts = {'check': check, 'noSplit': noSplit, 'noBreakGap': noBreakGap,
            'noMoveOutOfRange': noMoveOutOfRange}

    queues = queueS1.readQueues(queueDir)
    newEntries = {name: [] for name in QUEUES}
    deltas = newDeltas()
    promoted = promotePending(queues, newEntries, deltas, assemblyDir, orbitDir,
                              check)
    cleared = promoteProblem(queues, newEntries, deltas, assemblyDir, orbitDir,
                             check)
    seen = {entryUnit(e) for name in QUEUES for e in queues[name]}

    for trackDir in trackDirs:
        trackName = os.path.basename(trackDir)
        frameRange = readFrameRange(trackDir)
        # bare orbit dirs (seq 0) in the date window, ascending-node date order
        orbitList = []
        for entry in sorted(os.listdir(trackDir)):
            if not isSourceOrbitDir(entry) or parseOrbitSeq(entry)[1] != 0:
                continue
            path = os.path.join(trackDir, entry)
            if not glob.glob(f'{path}/*.SAFE'):
                continue
            try:
                asc = getAscNodeTime(path, check)
            except Exception as exc:
                # An orbit dir that cannot even be vetted would otherwise be
                # skipped silently and never processed, so queue it as a problem.
                print(f'{trackName}/{entry}: cannot read ascending node ({exc})')
                rel = f'{trackName}/{entry}'
                if rel not in seen:
                    queueUnit(queues, newEntries, deltas, 'problem',
                              {'unit': rel, 'orbit': entry},
                              f'cannot read ascending node: {exc}')
                    seen.add(rel)
                continue
            if firstDate <= asc <= lastDate:
                orbitList.append((int(entry), asc))
        orbitList.sort(key=lambda oa: oa[1])

        priorAsc = None
        for orbit, asc in orbitList:
            # days since the previous acquisition in the track (spots a miss);
            # round the repeat interval (it is not an exact whole day)
            if priorAsc is None:
                dayStr = ''
            else:
                dayStr = f'  {round((asc - priorAsc).total_seconds() / 86400):+d}d'
            priorAsc = asc
            barePath = os.path.join(trackDir, str(orbit))
            # already-processed datatakes are shown for info and left untouched
            if isProcessed(trackDir, str(orbit)):
                info = getOrbitSafe(barePath, asc)
                inRange = [r for i, r in enumerate(info)
                           if i not in outOfRange(info, frameRange)]
                print(headerLine(trackName, orbit, f'{info[0][2]:%Y-%m-%d}',
                                 dayStr, info, frameStr(inRange, frameRange),
                                 'processed', frameRange))
                continue
            try:
                units, moves, toTmp, residual, diag = analyzeOrbit(
                    trackDir, orbit, frameRange, opts)
            except Exception as exc:
                print(f'{trackName}/{orbit}: analysis error ({exc})')
                rel = f'{trackName}/{orbit}'
                if rel not in seen:
                    queueUnit(queues, newEntries, deltas, 'problem',
                              {'unit': rel, 'orbit': str(orbit),
                               'date': f'{asc:%Y-%m-%d}'},
                              f'analysis error: {exc}')
                    seen.add(rel)
                continue
            if not check:
                executeMoves(trackDir, moves, toTmp)
            # route every unit, then print: header (bare orbit) with its routing
            # tacked on, plus one line per extra gap/split unit.
            bareName = str(orbit)
            display = {}
            for unitName in sorted(units):
                rows = units[unitName]
                rel = f'{trackName}/{unitName}'
                frames = frameStr(rows, frameRange)
                if isProcessed(trackDir, unitName):
                    tag = 'processed'
                elif rel in seen:
                    tag = 'already queued'
                else:
                    queue, reason = routeUnit(trackDir, unitName, rows,
                                              residual.get(unitName), orbitDir)
                    tag = f'{QUEUE_LABEL.get(queue, "skip")} ({reason})'
                    if queue:
                        queueUnit(queues, newEntries, deltas, queue,
                                  unitRecord(trackName, unitName, rows,
                                             frameRange), reason)
                        seen.add(rel)
                display[unitName] = (frames, tag)

            info = diag['info']
            lo, hi = frameRange
            bareFrames, bareTag = display.get(bareName, ('none', 'skip'))
            print(headerLine(trackName, orbit, f'{info[0][2]:%Y-%m-%d}', dayStr,
                             info, bareFrames, bareTag, frameRange))
            for unitName in sorted(units):
                if unitName != bareName:
                    frames, tag = display[unitName]
                    print(f'    {unitName}: frames {frames} -> {tag}')
            nOOR = len(info) - len(diag['inRange'])
            if nOOR:
                print(f'    {nOOR} SAFE out of range [{lo},{hi}]')

    if not check:
        # Deltas, not a whole-file rewrite: this run read the queues minutes ago
        # and setupTrack may have consumed entries since, which a snapshot write
        # would silently undo.
        if queueS1.applyQueueDeltas(queueDir, **deltas) is None:
            u.mywarning('checkFramesS1: another writer holds the queue lock; '
                        'no queue changes were written')
    total = sum(len(v) for v in newEntries.values())
    verb = 'would queue' if check else 'queued'
    print(f'{verb} {total} new unit(s): '
          + ', '.join(f'{k} {len(newEntries[k])}' for k in QUEUES)
          + (f'; promoted {promoted} from pending' if promoted else '')
          + (f'; cleared {cleared} from problem' if cleared else ''))
    return newEntries


def executeMoves(trackDir, moves, toTmp):
    ''' Apply the restructuring: move gap/split SAFEs into their unit dirs and
    out-of-range SAFEs into track tmp/. '''
    for safe, dest in moves:
        destDir = os.path.join(trackDir, dest)
        os.makedirs(destDir, exist_ok=True)
        # every segment of a datatake shares the bare orbit's ascending node;
        # seed the cache so the new unit is self-contained (no XML re-parse).
        srcCache = os.path.join(trackDir, dest.split('_')[0], 'ascendingNodeTime')
        dstCache = os.path.join(destDir, 'ascendingNodeTime')
        if os.path.exists(srcCache) and not os.path.exists(dstCache):
            shutil.copy(srcCache, dstCache)
        shutil.move(safe, os.path.join(destDir, os.path.basename(safe)))
    if toTmp:
        tmpDir = os.path.join(trackDir, 'tmp')
        os.makedirs(tmpDir, exist_ok=True)
        for safe in toTmp:
            shutil.move(safe, os.path.join(tmpDir, os.path.basename(safe)))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parseArgs():
    parser = argparse.ArgumentParser(
        description='\033[1mCheck Sentinel-1 burst-frame coverage and queue '
        'datatakes for processing\033[0m',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('target', nargs='?', default=None,
                        help='Track to run on, e.g. track-16 (or just 16); '
                        'default: all track-<n>/ dirs under --assemblyDir')
    parser.add_argument('--assemblyDir', type=str, default='.',
                        help='Root holding the track-<n>/ dirs and the queue '
                        'files [default: .]')
    parser.add_argument('--orbitDir', type=str, default=None,
                        help='Precise-orbit (EOF) archive to test orbit '
                        f'availability [default: {refreshOrbits.DEFAULT_ORBIT_DIR}]')
    parser.add_argument('--firstDate', type=str, default=None,
                        metavar='YYYY-MM-DD',
                        help='Skip datatakes with ascending-node date before '
                        'this [default: today-6 months]')
    parser.add_argument('--lastDate', type=str, default=None,
                        metavar='YYYY-MM-DD',
                        help='Skip datatakes with ascending-node date after '
                        'this [default: today]')
    parser.add_argument('--queueDir', type=str, default=None,
                        help='Where the cumulative queue YAMLs live '
                        '[default: assemblyDir]')
    parser.add_argument('--check', action='store_true',
                        help='Dry run: report intended moves and routing, '
                        'change nothing on disk')
    parser.add_argument('--noSplit', action='store_true',
                        help='Do not split over-length (>127 burst) datatakes')
    parser.add_argument('--noBreakGap', action='store_true',
                        help='Do not break datatakes with gaps into segments')
    parser.add_argument('--noMoveOutOfRange', action='store_true',
                        help='Do not move out-of-range SAFEs to tmp/')
    parser.add_argument('--excludeTracks', type=str, default='',
                        metavar='"N N ..."',
                        help='Tracks never to scan, as one quoted space- or '
                        'comma-separated value: --excludeTracks "114 143". '
                        'Naming a track explicitly overrides this')
    return parser.parse_args()


def main():
    args = parseArgs()
    firstDate = (datetime.strptime(args.firstDate, '%Y-%m-%d')
                 if args.firstDate else None)
    lastDate = (datetime.strptime(args.lastDate, '%Y-%m-%d') + timedelta(days=1)
                if args.lastDate else None)
    checkFrames(args.assemblyDir, track=args.target, orbitDir=args.orbitDir,
                firstDate=firstDate, lastDate=lastDate, queueDir=args.queueDir,
                check=args.check, noSplit=args.noSplit,
                noBreakGap=args.noBreakGap,
                noMoveOutOfRange=args.noMoveOutOfRange,
                excludeTracks=[int(x) for x in
                               args.excludeTracks.replace(',', ' ')
                               .split()])


if __name__ == '__main__':
    main()
