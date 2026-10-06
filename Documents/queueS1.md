# queueS1

Inspect and edit the **Sentinel-1 processing queues**, and the module that
defines their file format. Part of the `asfSearchAndDownload` package.
[`checkFramesS1`](checkFramesS1.md) is the producer, `setupTrack` (s1setup
package) the consumer, and [`autoupdateS1`](autoupdateS1.md) drives both.

## The queues

All queue state lives in `<assemblyDir>/autoupdate/` (overridable with
`queueDir` in `autoupdate.yaml`):

```
toProcess.yaml          vetted, precise orbit published, ready to process
pendingProcessing.yaml  vetted, waiting on the precise (EOF) orbit
problem.yaml            gap / over-length / out-of-range / failed assembly
completed.yaml          all-time record of processed units (not a queue)
processed.<YYYY-MM-DD>.yaml   per-day processed record, folded into completed.yaml
notes.yaml              things worth recording that no queue owns
.queueS1.lock  .assemblyTree.lock   the locks
```

Every entry carries a `unit` (e.g. `track-90/7086`, relative to `assemblyDir`);
frame extent fields (`startFrame`, `endFrame`, `totalFrames`) may be absent, so
consumers key off `unit` alone. `problem` entries also carry `comment`,
`source`, `found` and — once mailed — `notified`.

See [autoupdateS1](autoupdateS1.md#queue-directory) for the queue directory's
history, `notes.yaml`, and the locking scheme.

## CLI

**Use `queueS1`, not a text editor.** It takes the queue lock and writes
atomically; an editor does neither, so a write landing between another host's
read and write silently loses entries. Run it from the assembly directory,
which is where it finds `autoupdate/`.

```
queueS1 {info,list,remove,promote} [targets ...] [--queueDir DIR]

queueS1 info                        # paths, config, lock states, counts
queueS1 list [problem]              # entries, with their comments
queueS1 remove track-89/64236       # drop from whichever queue holds it
queueS1 promote track-89/64236      # problem -> toProcess, after a manual fix

  --queueDir DIR   Queue directory [found from the current directory]
```

`remove` and `promote` refuse the whole call if any unit is not in a queue, so a
typo cannot look like success, and they report when the lock is busy rather
than failing quietly.

Usually you do not need them: `checkFramesS1` re-evaluates every problem entry
on each run and clears the ones that now pass, so a unit fixed by hand returns
to `toProcess` by itself on the next `autoupdateS1 --checkFrames`.

## Library API

The module deliberately imports nothing heavy (no `utilities`, gdal or
requests): `import queueS1` costs ~0.04 s against ~2.3 s for `checkFramesS1`,
which is what lets `s1setup` depend on it cheaply. Keep it that way.

- `applyQueueDeltas(queueDir, add=, remove=, update=)` — locked
  read-modify-write, applied per queue as remove → add → update, so one call can
  move a unit between queues. **Use this, not `writeQueues`**: a full rewrite
  from a stale snapshot silently undoes a concurrent writer. Add is first-wins,
  so a re-add never resets `notified`; an `update` value of `None` deletes the
  key.
- `queueLock` — atomic `O_EXCL` lock file, 30 s wait, 15 min stale reclaim.
  Writes go through a temp file + `os.replace` and are skipped when unchanged.
- `resolveQueueDir(assemblyDir, queueDir=None)` — the single place every tool
  (`checkFramesS1`, `autoupdateS1`, `setupTrack`) finds the queue directory, so
  their locks exclude each other.
- `problemRecord` / `unnotified` / `markNotified` — the notification handshake.
  To re-notify a unit already in `problem`, remove and re-add it.
- `appendProcessed` / `mergeProcessed` — the processed record. Dated files are
  merged into `completed.yaml` (keyed on `(unit, finished)`) and pruned after 5
  days, always after the merge.
- `appendNote` / `readNotes` — `notes.yaml`, keyed on `(subject, comment)` and
  appended only if new.
