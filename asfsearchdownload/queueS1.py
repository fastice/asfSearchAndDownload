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
import shutil
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

    Lives beside the queues under <assemblyDir>/autoupdate (not in a project
    dir) so any tool given --assemblyDir takes the same one. Resolving through
    resolveQueueDir is what keeps that true: a caller locking the assembly top
    while another locks the queue directory would not exclude each other at
    all. Same O_EXCL pattern as queueLock but non-blocking by default and with
    a long stale window, since a legitimate assembly run lasts hours.
    '''
    with queueLock(resolveQueueDir(assemblyDir), wait=wait,
                   staleSeconds=staleSeconds,
                   name=ASSEMBLY_LOCK_NAME) as acquired:
        yield acquired


QUEUE_DIR_NAME = 'autoupdate'


def legacyQueueFiles(directory):
    '''
    Queue and lock files of an old assembly-top layout, by explicit name.

    Never a blanket *.yaml: the assembly tree's top level also holds ad-hoc
    scripts and scratch, and a glob would sweep in whatever lands there later.
    '''
    names = [f'{name}.yaml' for name in QUEUES]
    names += [COMPLETED_NAME, LOCK_NAME, ASSEMBLY_LOCK_NAME]
    paths = [os.path.join(directory, name) for name in names]
    paths += glob.glob(os.path.join(directory, PROCESSED_GLOB))
    for name in QUEUES:
        paths += glob.glob(os.path.join(directory, f'{name}.yaml.bak.*'))
    return [p for p in paths if os.path.exists(p)]


def resolveQueueDir(assemblyDir, queueDir=None, quiet=False,
                    migrate=True):
    '''
    Where the queues and locks live: <assemblyDir>/autoupdate.

    An explicit queueDir wins and is returned untouched -- it stays the escape
    hatch and is never migrated. Otherwise the directory is created on first
    use and any files of the old assembly-top layout are moved into it, which
    happens once and then never again.

    migrate=False reports where the queues are without touching anything, for
    callers in --check mode: a dry run must not move files.

    Migration is skipped while either lock is present, because something is
    running. Moving .assemblyTree.lock out from under its holder would be the
    worst outcome available: the holder's release would fail, and the next host
    would find no lock in the new directory and start writing the same tree.
    '''
    assemblyDir = os.path.abspath(assemblyDir)
    if queueDir:
        return os.path.abspath(queueDir)
    target = os.path.join(assemblyDir, QUEUE_DIR_NAME)
    if os.path.isdir(target):
        return target
    legacy = legacyQueueFiles(assemblyDir)
    if not migrate:
        return assemblyDir if legacy else target
    held = [p for p in legacy
            if os.path.basename(p) in (LOCK_NAME, ASSEMBLY_LOCK_NAME)]
    if held:
        if not quiet:
            print(f'queueS1: {os.path.basename(held[0])} is held; leaving the '
                  f'queues in {assemblyDir} for now, the next run will move '
                  f'them to {target}')
        return assemblyDir
    os.makedirs(target, exist_ok=True)
    for path in legacy:
        shutil.move(path, os.path.join(target, os.path.basename(path)))
    if not quiet:
        print(f'queueS1: moved {len(legacy)} queue file(s) to {target}')
    return target


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


# --------------------------------------------------------------------------- #
# notes
#
#   notes.yaml   things a person should see that no queue owns, because nothing
#                is broken and nothing is waiting: the run made a choice worth
#                recording. Append-only and deduped, so a nightly re-run of the
#                same condition does not grow the file for ever.
# --------------------------------------------------------------------------- #
NOTES_NAME = 'notes.yaml'


def notesPath(queueDir):
    return os.path.join(queueDir, NOTES_NAME)


def noteRecord(subject, comment, source, base=None):
    '''
    One note. subject identifies what it is about (a granule, a unit) and is
    what deduping keys on together with comment, so re-stating the same fact
    about the same subject is a no-op.
    '''
    record = dict(base) if base else {}
    record['subject'] = subject
    record['comment'] = comment
    record['source'] = source
    record['found'] = datetime.datetime.now().isoformat(timespec='seconds')
    return record


def appendNote(queueDir, record):
    '''
    Append one note unless (subject, comment) is already recorded. Returns True
    if it was written.

    The dedup matters more than it looks: the conditions that produce notes are
    persistent states, not events, so every nightly run re-derives them. Without
    it, one superseded granule left on disk would add a line a night for ever.
    '''
    path = notesPath(queueDir)
    with queueLock(queueDir) as acquired:
        if not acquired:
            return False
        notes = _readList(path)
        key = (record.get('subject'), record.get('comment'))
        for note in notes:
            if isinstance(note, dict) and \
                    (note.get('subject'), note.get('comment')) == key:
                return False
        _atomicDump(path, notes + [record])
        return True


def readNotes(queueDir):
    ''' Every note on record, oldest first. '''
    return _readList(notesPath(queueDir))


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


CONFIG_POINTER = 'configPath'


def recordConfigPath(queueDir, configPath):
    '''
    Note which autoupdate.yaml drives this queue directory.

    The config lives beside the archive and the queues beside the assembly
    tree, often on different volumes, and nothing else connects them. Standing
    in the assembly directory there is otherwise no way to find the config that
    governs it.
    '''
    path = os.path.join(queueDir, CONFIG_POINTER)
    configPath = os.path.abspath(configPath)
    try:
        if os.path.exists(path):
            with open(path) as fp:
                if fp.read().strip() == configPath:
                    return          # unchanged, leave the mtime alone
        with open(path, 'w') as fp:
            fp.write(f'{configPath}\n')
    except OSError:
        pass                        # a pointer is a convenience, never fatal


def readConfigPath(queueDir):
    ''' The autoupdate.yaml recorded for this queue directory, or None. '''
    path = os.path.join(queueDir, CONFIG_POINTER)
    try:
        with open(path) as fp:
            return fp.read().strip() or None
    except OSError:
        return None


def lockState(queueDir, name):
    '''
    (held, holder, ageSeconds) for a lock, without ever taking it.

    Read-only on purpose: --info and the CLI must report while a run is in
    progress, which is exactly when the answer matters.
    '''
    path = os.path.join(queueDir, name)
    try:
        with open(path) as fp:
            holder = fp.read().strip()
        return True, holder, time.time() - os.path.getmtime(path)
    except OSError:
        return False, None, None


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


# --------------------------------------------------------------------------- #
# CLI - the supported way to edit the queues by hand
#
# Run it from the assembly directory; the queue directory is found from there.
# Editing the YAML in a text editor bypasses the lock, so a write landing
# between another host's read and write silently loses entries.
# --------------------------------------------------------------------------- #
def findUnit(queues, unit):
    ''' Which queue holds a unit, or None. '''
    for name in QUEUES:
        if any(entryUnit(e) == unit for e in queues[name]):
            return name
    return None


def describe(queueDir):
    ''' The lines of `queueS1 info`. '''
    lines = [f'queueDir  {queueDir}']
    configPath = readConfigPath(queueDir)
    lines.append('config    '
                 + (configPath or 'unknown (no autoupdateS1 run yet)'))
    for name in (LOCK_NAME, ASSEMBLY_LOCK_NAME):
        held, holder, age = lockState(queueDir, name)
        if held:
            lines.append(f'{name:<18} HELD by {holder} ({age / 3600:.1f} h)')
        else:
            lines.append(f'{name:<18} free')
    queues = readQueues(queueDir)
    for name in QUEUES:
        lines.append(f'{name:<18} {len(queues[name])}')
    completed = _readList(completedPath(queueDir))
    lines.append(f'{"completed":<18} {len(completed)}')
    lines.append(f'{"processed today":<18} '
                 f'{len(_readList(processedPath(queueDir)))}')
    return lines


def parseQueueArgs():
    import argparse
    parser = argparse.ArgumentParser(
        description='Inspect and edit the Sentinel-1 processing queues. Run '
                    'from the assembly directory. This is the supported way '
                    'to change a queue by hand: it takes the lock and writes '
                    'atomically, which editing the YAML does not.',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('action', choices=['info', 'list', 'remove',
                                           'promote'],
                        help='info: paths, locks and counts; list [queue]; '
                             'remove UNIT...; promote UNIT... (problem -> '
                             'toProcess)')
    parser.add_argument('targets', nargs='*',
                        help='queue name for list, else unit names '
                             '(e.g. track-90/7086)')
    parser.add_argument('--queueDir', type=str, default=None,
                        help='Queue directory [default: found from the '
                             'current directory]')
    return parser.parse_args()


def main():
    args = parseQueueArgs()
    queueDir = args.queueDir or resolveQueueDir(os.getcwd(), migrate=False)
    if not os.path.isdir(queueDir):
        raise SystemExit(f'queueS1: no queue directory at {queueDir} -- run '
                         'from the assembly directory, or pass --queueDir')

    if args.action == 'info':
        print('\n'.join(describe(queueDir)))
        return

    queues = readQueues(queueDir)
    if args.action == 'list':
        names = args.targets or list(QUEUES)
        for name in names:
            if name not in QUEUES:
                raise SystemExit(f'queueS1: no queue {name!r}; '
                                 f'choose from {" ".join(QUEUES)}')
            print(f'--- {name} ({len(queues[name])})')
            for entry in queues[name]:
                unit = entryUnit(entry)
                comment = (entry.get('comment', '')
                           if isinstance(entry, dict) else '')
                print(f'  {unit}{"  " + comment if comment else ""}')
        return

    if not args.targets:
        raise SystemExit(f'queueS1: {args.action} needs at least one unit')
    # Refuse the whole call if any unit is unknown: a typo that silently did
    # nothing would read as success and leave the queue as it was.
    missing = [u for u in args.targets if findUnit(queues, u) is None]
    if missing:
        raise SystemExit('queueS1: not in any queue: ' + ' '.join(missing))

    remove = {}
    for unit in args.targets:
        remove.setdefault(findUnit(queues, unit), []).append(unit)
    add = {'toProcess': [{'unit': u} for u in args.targets]} \
        if args.action == 'promote' else None
    result = applyQueueDeltas(queueDir, remove=remove, add=add)
    if result is None:
        raise SystemExit('queueS1: the queue lock is busy; '
                         'nothing was written')
    verb = 'promoted to toProcess' if args.action == 'promote' else 'removed'
    for queue, units in remove.items():
        print(f'{verb}: {" ".join(units)}  (from {queue})')


if __name__ == '__main__':
    main()
