'''Unit tests for SAFEs filed into an already-processed orbit (late SAFEs).

Built on the track-170/4061 layout (S1D 2026-08-10): 12 slices, of which 083417
was assembled as 4061 and 083622..083802 as 4061_1 on 08-30; the other six were
filed into the processed bare dir on 09-16 and 09-28 and never assembled.
'''
import glob
import os
import shutil
import tempfile

from asfsearchdownload import checkFramesS1, queueS1

PREFIX = 'S1D_IW_SLC__1SDH_20260810T'
SLICES = ['083349_20260810T083419_004061_007660_60A4',
          '083417_20260810T083444_004061_007660_ACA9',
          '083442_20260810T083509_004061_007660_DFC3',
          '083507_20260810T083534_004061_007660_1D44',
          '083532_20260810T083559_004061_007660_BA88',
          '083556_20260810T083624_004061_007660_9A02',
          '083622_20260810T083649_004061_007660_5854',
          '083647_20260810T083714_004061_007660_CBC4',
          '083712_20260810T083739_004061_007660_F4EA',
          '083737_20260810T083804_004061_007660_0641',
          '083802_20260810T083830_004061_007660_ACB0',
          '083827_20260810T083854_004061_007660_CC11']
SAFES = [f'{PREFIX}{s}.SAFE' for s in SLICES]
ASC = '2026-08-10T08:07:24.195969'

fails = []


def check(label, got, want):
    if got != want:
        fails.append(f'{label}: got {got!r}, want {want!r}')
        print(f'  FAIL {label}: got {got!r}, want {want!r}')
    else:
        print(f'  ok   {label}')


def makeUnit(trackDir, name, safes, processed):
    unit = os.path.join(trackDir, name)
    os.makedirs(unit)
    for safe in safes:
        os.makedirs(os.path.join(unit, safe))
    with open(os.path.join(unit, 'ascendingNodeTime'), 'w') as fp:
        fp.write(ASC)
    if processed:
        with open(os.path.join(unit, 'inputSAFE'), 'w') as fp:
            fp.write(''.join(f'{s}\n' for s in processed))
        open(os.path.join(unit, 'Completed'), 'w').close()


def buildTree(root):
    ''' The 4061 state as found on 2026-11-11. '''
    trackDir = os.path.join(root, 'track-170')
    os.makedirs(trackDir)
    with open(os.path.join(trackDir, 'frameRange'), 'w') as fp:
        fp.write('580 680\n')
    bare = [SAFES[i] for i in (0, 1, 2, 3, 4, 5, 11)]
    makeUnit(trackDir, '4061', bare, [SAFES[1]])
    makeUnit(trackDir, '4061_1', SAFES[6:11], SAFES[6:11])
    for out in ('4061-0', '4061-1'):
        os.makedirs(os.path.join(trackDir, out))
    return trackDir


def safesIn(path):
    return sorted(os.path.basename(s) for s in glob.glob(f'{path}/*.SAFE'))


opts = {'check': True, 'noMoveOutOfRange': False}
root = tempfile.mkdtemp(prefix='testLateSafes.')
try:
    print('--- analyzeLateSafes on the 4061 layout ---')
    trackDir = buildTree(root)
    units, moves, toTmp = checkFramesS1.analyzeLateSafes(
        trackDir, 4061, [580, 680], opts)
    check('three late units', sorted(units), ['4061_5', '4061_6', '4061_7'])
    check('_5 is 083349', [os.path.basename(r[6]) for r in units['4061_5']],
          [SAFES[0]])
    check('_6 is 083442..083556',
          [os.path.basename(r[6]) for r in units['4061_6']], SAFES[2:6])
    check('_7 is 083827', [os.path.basename(r[6]) for r in units['4061_7']],
          [SAFES[11]])
    check('six moves', len(moves), 6)
    check('used SAFE never moves',
          any(os.path.basename(s) == SAFES[1] for s, _ in moves), False)
    check('nothing to tmp', toTmp, [])

    print('--- suffixes skip existing _N and -N ---')
    os.makedirs(os.path.join(trackDir, '4061-5'))
    os.makedirs(os.path.join(trackDir, '4061_6'))
    units, _, _ = checkFramesS1.analyzeLateSafes(trackDir, 4061, [580, 680],
                                                 opts)
    check('skips 5 and 6', sorted(u for u in units if units[u]),
          ['4061_7', '4061_8', '4061_9'])
    shutil.rmtree(root)

    print('--- republished copy of a used SAFE is left alone ---')
    root = tempfile.mkdtemp(prefix='testLateSafes.')
    trackDir = buildTree(root)
    copy = SAFES[1][:-len('ACA9.SAFE')] + 'FFFF.SAFE'
    os.makedirs(os.path.join(trackDir, '4061', copy))
    units, moves, _ = checkFramesS1.analyzeLateSafes(trackDir, 4061,
                                                     [580, 680], opts)
    check('copy not moved',
          any(os.path.basename(s) == copy for s, _ in moves), False)
    shutil.rmtree(root)

    print('--- no inputSAFE: conservative, nothing planned ---')
    root = tempfile.mkdtemp(prefix='testLateSafes.')
    trackDir = buildTree(root)
    os.remove(os.path.join(trackDir, '4061', 'inputSAFE'))
    units, moves, _ = checkFramesS1.analyzeLateSafes(trackDir, 4061,
                                                     [580, 680], opts)
    check('no units', units, {})
    shutil.rmtree(root)

    print('--- checkFrames end to end ---')
    root = tempfile.mkdtemp(prefix='testLateSafes.')
    trackDir = buildTree(root)
    queueDir = os.path.join(root, 'queues')
    os.makedirs(queueDir)
    orbitDir = os.path.join(root, 'orbits')       # empty: no EOF -> pending
    os.makedirs(orbitDir)
    window = dict(orbitDir=orbitDir, queueDir=queueDir,
                  firstDate=checkFramesS1.datetime(2026, 8, 1),
                  lastDate=checkFramesS1.datetime(2026, 8, 31))

    new = checkFramesS1.checkFrames(root, check=True, **window)
    check('--check queues three', len(new['pendingProcessing']), 3)
    check('--check moves nothing', safesIn(os.path.join(trackDir, '4061')),
          sorted(SAFES[i] for i in (0, 1, 2, 3, 4, 5, 11)))
    check('--check writes no queue',
          queueS1.readQueues(queueDir)['pendingProcessing'], [])

    new = checkFramesS1.checkFrames(root, noLateUnits=True, **window)
    check('--noLateUnits queues none',
          sum(len(v) for v in new.values()), 0)
    check('--noLateUnits moves nothing',
          len(safesIn(os.path.join(trackDir, '4061'))), 7)

    new = checkFramesS1.checkFrames(root, **window)
    check('queues three pending', sorted(e['unit'] for e in
                                         new['pendingProcessing']),
          ['track-170/4061_5', 'track-170/4061_6', 'track-170/4061_7'])
    check('bare keeps only the used SAFE',
          safesIn(os.path.join(trackDir, '4061')), [SAFES[1]])
    check('_6 holds its four', safesIn(os.path.join(trackDir, '4061_6')),
          SAFES[2:6])
    check('node time seeded',
          os.path.exists(os.path.join(trackDir, '4061_6',
                                      'ascendingNodeTime')), True)
    check('processed _1 untouched', safesIn(os.path.join(trackDir, '4061_1')),
          SAFES[6:11])
    check('on disk', sorted(queueS1.entryUnit(e) for e in queueS1.readQueues(
        queueDir)['pendingProcessing']),
        ['track-170/4061_5', 'track-170/4061_6', 'track-170/4061_7'])

    new = checkFramesS1.checkFrames(root, **window)
    check('second run idempotent', sum(len(v) for v in new.values()), 0)

    print('--- a stranded late unit (queue write lost) is routed ---')
    queueS1._atomicDump(queueS1.queuePath(queueDir, 'pendingProcessing'), [])
    new = checkFramesS1.checkFrames(root, **window)
    check('re-queues the three', len(new['pendingProcessing']), 3)
finally:
    shutil.rmtree(root, ignore_errors=True)

print(f'\n{len(fails)} failure(s)')
raise SystemExit(1 if fails else 0)
