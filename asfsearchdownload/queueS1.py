#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
queueS1 - the Sentinel-1 processing-queue file format, shared by producer and
consumer.

checkFramesS1 (the producer) routes each processing unit into one of three
cumulative YAML queues under queueDir; setupTrack (the consumer, in the s1setup
package) works through toProcess and reports failures back into problem.

  toProcess.yaml          vetted, precise orbit published, ready to process
  pendingProcessing.yaml  vetted, waiting on the precise (EOF) orbit
  problem.yaml            gap / over-length / out-of-range / failed assembly

This module deliberately imports nothing heavy -- no utilities, no gdal, no
requests -- so setupTrack can read the queues without paying checkFramesS1's
~2.5 s import cost.

Record schema. Every entry carries at least a 'unit' (e.g. 'track-90/7086',
relative to assemblyDir). Entries queued before a unit's frame extent is known
have no startFrame/endFrame/totalFrames, so *consumers must key off 'unit'
alone*. Legacy entries may be a bare string rather than a dict; entryUnit()
handles both.

problem entries additionally carry:
  comment   why it is a problem ('over-length', 'setup failed at ...', ...)
  source    which tool queued it ('checkFramesS1' or 'setupTrack')
  found     when it was queued (ISO 8601)
  notified  absent until an email about it has actually been sent

Writers must go through applyQueueDeltas(), which re-reads the queues under a
lock and applies only the caller's own changes. A whole-file rewrite from a
stale in-memory snapshot would silently undo a concurrent writer.

Part of the asfSearchAndDownload package.
"""
import contextlib
import datetime
import glob
import os
import socket
import time

import yaml

QUEUES = ('toProcess', 'pendingProcessing', 'problem')

LOCK_NAME = '.queueS1.lock'
LOCK_WAIT = 30                 # seconds to wait for a concurrent writer
LOCK_POLL = 0.5
STALE_LOCK_SECONDS = 900       # abandon a lock older than this (crashed writer)


def queuePath(queueDir, name):
    return os.path.join(queueDir, f'{name}.yaml')


def entryUnit(entry):
    ''' The unit identifier of a queue entry (record dict or legacy string). '''
    return entry['unit'] if isinstance(entry, dict) else entry


def readQueue(queueDir, name):
    path = queuePath(queueDir, name)
    if not os.path.exists(path):
        return []
    with open(path) as fp:
        data = yaml.safe_load(fp)
    return data if isinstance(data, list) else []


def readQueues(queueDir):
    ''' All three queues as {name: [entry, ...]}. '''
    return {name: readQueue(queueDir, name) for name in QUEUES}


def mergeQueue(entries):
    ''' Dedupe by unit (first record for a unit wins) and sort by unit. '''
    byUnit = {}
    for entry in entries:
        byUnit.setdefault(entryUnit(entry), entry)
    return [byUnit[unit] for unit in sorted(byUnit)]


def _atomicDump(path, entries):
    '''
    Write entries to path via a temp file in the same directory + os.replace, so
    a reader never sees a partial queue. NFSv4 rename is atomic server-side.
    Returns True if the file changed (skip the write when it did not, so mtimes
    stay meaningful).
    '''
    body = yaml.safe_dump(entries, default_flow_style=False, sort_keys=False)
    if os.path.exists(path):
        with open(path) as fp:
            if fp.read() == body:
                return False
    tmpPath = f'{path}.tmp.{os.getpid()}'
    with open(tmpPath, 'w') as fp:
        fp.write(body)
    os.replace(tmpPath, path)
    return True


@contextlib.contextmanager
def queueLock(queueDir, wait=LOCK_WAIT, staleSeconds=STALE_LOCK_SECONDS,
              name=LOCK_NAME):
    '''
    Serialize queue writers across hosts. Atomic O_EXCL create (NFS flock is
    unreliable), polled for up to `wait` seconds, reclaiming a lock older than
    staleSeconds. Yields True if acquired, False if it timed out -- a caller that
    gets False must not write.
    '''
    lockPath = os.path.join(queueDir, name)
    deadline = time.time() + wait
    acquired = False
    while True:
        try:
            fd = os.open(lockPath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, (f'{socket.gethostname()} pid {os.getpid()} '
                          f'{datetime.datetime.now().isoformat(timespec="seconds")}'
                          '\n').encode())
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lockPath)
            except OSError:
                continue          # vanished under us; retry immediately
            if age > staleSeconds:
                try:
                    os.remove(lockPath)
                except OSError:
                    pass
                continue
            if time.time() >= deadline:
                break
            time.sleep(LOCK_POLL)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                os.remove(lockPath)
            except OSError:
                pass


ASSEMBLY_LOCK_NAME = '.assemblyTree.lock'
ASSEMBLY_STALE_SECONDS = 36 * 3600     # an assembly run can legitimately be long


@contextlib.contextmanager
def assemblyLock(assemblyDir, wait=0, staleSeconds=ASSEMBLY_STALE_SECONDS):
    '''
    Guard every writer of the assembly tree: checkFramesS1 moves SAFE dirs while
    restructuring, and setupTrack reads them for minutes at a time.

    Lives beside the tree (not in a project dir) so any tool given --assemblyDir
    can take it. Same O_EXCL pattern as queueLock but non-blocking by default and
    with a long stale window, since a legitimate assembly run lasts hours.
    '''
    with queueLock(assemblyDir, wait=wait, staleSeconds=staleSeconds,
                   name=ASSEMBLY_LOCK_NAME) as acquired:
        yield acquired


def sweepTempFiles(queueDir):
    ''' Remove temp files a crashed writer left behind. '''
    for name in QUEUES:
        for stale in glob.glob(f'{queuePath(queueDir, name)}.tmp.*'):
            try:
                os.remove(stale)
            except OSError:
                pass


def applyQueueDeltas(queueDir, add=None, remove=None, update=None):
    '''
    Re-read the queues under the lock, apply only this caller's changes, and
    write back atomically. Returns the merged {name: [entry, ...]}, or None if
    the lock could not be taken (nothing was written).

      add    {queue: [record, ...]}     appended; a unit already present keeps
                                        its existing record (so 'notified' is
                                        never reset by a re-add)
      remove {queue: [unit, ...]}       dropped from that queue
      update {queue: {unit: {k: v}}}    merged into the existing record; a value
                                        of None deletes that key

    Applied per queue in the order remove -> add -> update, so one call can move
    a unit between queues, and an update can land on a freshly added record.
    '''
    add = add or {}
    remove = remove or {}
    update = update or {}
    with queueLock(queueDir) as acquired:
        if not acquired:
            return None
        queues = readQueues(queueDir)
        for name in QUEUES:
            entries = queues[name]
            dropped = set(remove.get(name) or [])
            if dropped:
                entries = [e for e in entries if entryUnit(e) not in dropped]
            entries = mergeQueue(entries + list(add.get(name) or []))
            changes = update.get(name) or {}
            if changes:
                for entry in entries:
                    fields = changes.get(entryUnit(entry))
                    if not fields or not isinstance(entry, dict):
                        continue
                    for key, value in fields.items():
                        if value is None:
                            entry.pop(key, None)
                        else:
                            entry[key] = value
            queues[name] = entries
            _atomicDump(queuePath(queueDir, name), entries)
        return queues


def writeQueues(queueDir, queues):
    '''
    Write all three queue files from an in-memory snapshot, deduped and sorted.

    Kept for ad-hoc use and backwards compatibility. Prefer applyQueueDeltas:
    this asserts the caller's whole view of every queue, so any change another
    writer made since the snapshot was read is silently discarded.
    '''
    with queueLock(queueDir) as acquired:
        if not acquired:
            return False
        for name in QUEUES:
            _atomicDump(queuePath(queueDir, name), mergeQueue(queues[name]))
        return True


def problemRecord(unit, comment, source, base=None, date=None):
    '''
    A problem-queue record. base is the unitRecord for the unit when its frame
    extent is known; without one the record carries identity and comment only
    (a unit that failed before it could be analysed).
    '''
    record = dict(base) if base else {'unit': unit}
    record['unit'] = unit
    record.setdefault('orbit', unit.split('/')[-1])
    if date and 'date' not in record:
        record['date'] = date
    record['comment'] = comment
    record['source'] = source
    record['found'] = datetime.datetime.now().isoformat(timespec='seconds')
    record.pop('notified', None)     # a re-queued problem is news again
    return record


# --------------------------------------------------------------------------- #
# processed / completed records
#
# Not queues: these only ever grow, so they are kept out of QUEUES and never
# take part in applyQueueDeltas's read-modify-write of the working set.
#
#   processed.<YYYY-MM-DD>.yaml   this run's successes, appended per unit so a
#                                 crash keeps what already finished
#   completed.yaml                the all-time record, appended at end of run
# --------------------------------------------------------------------------- #
PROCESSED_GLOB = 'processed.*.yaml'
COMPLETED_NAME = 'completed.yaml'
RETAIN_DAYS = 5


def processedPath(queueDir, day=None):
    day = day or datetime.date.today()
    return os.path.join(queueDir, f'processed.{day:%Y-%m-%d}.yaml')


def completedPath(queueDir):
    return os.path.join(queueDir, COMPLETED_NAME)


def _readList(path):
    if not os.path.exists(path):
        return []
    with open(path) as fp:
        data = yaml.safe_load(fp)
    return data if isinstance(data, list) else []


def processedRecord(unit, elapsed=None, base=None):
    ''' One successful unit: identity, when it finished, how long it took. '''
    record = {'unit': unit}
    if isinstance(base, dict) and base.get('date'):
        record['date'] = base['date']          # acquisition date
    record['finished'] = datetime.datetime.now().isoformat(timespec='seconds')
    if elapsed is not None:
        record['elapsed'] = round(elapsed)
    record['host'] = socket.gethostname()
    return record


def appendProcessed(queueDir, record, day=None):
    '''
    Append one record to today's processed file, flushed immediately so an
    interrupted run keeps every unit that already finished.
    '''
    path = processedPath(queueDir, day)
    with queueLock(queueDir) as acquired:
        if not acquired:
            return False
        _atomicDump(path, _readList(path) + [record])
        return True


def mergeProcessed(queueDir, retainDays=RETAIN_DAYS):
    '''
    Fold every processed.<date>.yaml into the all-time completed.yaml, then drop
    dated files older than retainDays.

    Sweeps *all* dated files rather than just this run's, so one orphaned by a
    crash is picked up next time. Records are keyed on (unit, finished), which
    makes re-merging a no-op while still keeping a genuine reprocess of the same
    unit as a separate entry. Pruning happens only after a successful merge, so
    a dated file is never deleted before it has been folded in.

    Returns (nAdded, nPruned), or None if the lock could not be taken.
    '''
    with queueLock(queueDir) as acquired:
        if not acquired:
            return None
        completed = _readList(completedPath(queueDir))
        have = {(e.get('unit'), e.get('finished'))
                for e in completed if isinstance(e, dict)}
        added = 0
        for path in sorted(glob.glob(os.path.join(queueDir, PROCESSED_GLOB))):
            for record in _readList(path):
                key = (record.get('unit'), record.get('finished')) \
                    if isinstance(record, dict) else (record, None)
                if key in have:
                    continue
                completed.append(record)
                have.add(key)
                added += 1
        if added:
            _atomicDump(completedPath(queueDir), completed)
        cutoff = datetime.date.today() - datetime.timedelta(days=retainDays)
        pruned = 0
        for path in glob.glob(os.path.join(queueDir, PROCESSED_GLOB)):
            stamp = os.path.basename(path)[len('processed.'):-len('.yaml')]
            try:
                day = datetime.datetime.strptime(stamp, '%Y-%m-%d').date()
            except ValueError:
                continue          # unrecognised name: leave it alone
            if day < cutoff:
                try:
                    os.remove(path)
                    pruned += 1
                except OSError:
                    pass
        return added, pruned


def unnotified(queueDir):
    ''' problem records that have not been emailed yet. '''
    return [e for e in readQueue(queueDir, 'problem')
            if not (isinstance(e, dict) and e.get('notified'))]


def markNotified(queueDir, units):
    ''' Flag problem records as emailed. Call only after the send succeeded. '''
    if not units:
        return
    applyQueueDeltas(queueDir,
                     update={'problem': {u: {'notified': True} for u in units}})
