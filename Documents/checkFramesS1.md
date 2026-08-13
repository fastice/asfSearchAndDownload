# checkFramesS1

Vet the filed Sentinel-1 datatakes under an `assemblyDir/track-<n>/<orbit>/` tree
(built by [`fileS1`](fileS1.md)), restructure each into clean processing units,
and route them into three cumulative queues. Part of the `asfSearchAndDownload`
package; the frame-check step of [`autoupdateS1`](autoupdateS1.md) calls this
in-process, and it is also runnable standalone as `checkFramesS1`.

"**frame**" here == **burst number** counted from the ascending-node time at the
2.759 s IW burst period (identical to `s1setup/checkframes.py`). There is no
ESA/ASF S1 "frame" concept involved.

## What it does

For each `track-*/` under `assemblyDir`, for each bare orbit dir whose
ascending-node date is in the window:

1. Reads the track's `frameRange` file (`low high` bursts; default `[300, 750]`).
2. Computes each SAFE's burst span from the cached `<orbit>/ascendingNodeTime`.
3. **Restructures** the datatake into independent processing units:
   - **Out-of-range** SAFEs (wholly below `low` or above `high`) → `track-*/tmp/`.
   - **Gaps** (a >1-burst break between consecutive SAFEs): the first (near-side)
     segment stays in `<orbit>`; each later (far-side) segment moves to
     `<orbit>_1`, `<orbit>_2`, ….
   - **Over-length** (in-range burst span > 127): the first *n* SAFEs move to
     `<orbit>_4`. *n* is derived from the frameRange (the largest head whose
     in-range span stays ≤127), so every orbit in a track splits at the same
     burst boundary. Existing splits are not read — they are not consistently
     numbered.
4. **Routes** every resulting unit into one cumulative queue (see below).

Over-length is measured against the **frameRange**: a datatake whose SAFEs run
past the range is only split when the *in-range* span exceeds 127 (the
out-of-range remainder is trimmed later by `trimTopsSLCsToFit`).

Directory-suffix convention: `_1, _2, …` are far-side gap segments (and also
`fileS1`'s datatake-filing suffix — they can coincide), `_4` is the over-length
split head.

## Queues

Cumulative YAML lists written to `queueDir` (default = `assemblyDir`). Each entry
is a record:

```yaml
- unit: track-90/63162        # identity (relative to assemblyDir)
  orbit: '63162'              # unit dir name (bare orbit, or _seq split/gap unit)
  date: '2026-04-05'          # acquisition date
  startFrame: 360             # first in-range burst
  endFrame: 486               # last in-range burst
  totalFrames: 127            # in-range burst count
```

`problem` entries carry three more keys, plus a fourth once notified:

```yaml
  comment: over-length              # why it is a problem
  source: checkFramesS1             # which tool queued it (or setupTrack)
  found: '2026-08-13T02:14:07'      # when
  notified: true                    # absent until an email has actually gone out
```

Entries queued **before** a unit's frame extent is known — an unreadable
ascending node, an analysis error, a unit dir that has vanished — carry only
`unit`/`orbit`/`comment`/`source`/`found`. **Consumers must key off `unit`
alone.** Entries from older runs may also be a bare string rather than a dict.

The format lives in `queueS1.py`, deliberately free of heavy imports so
`s1setup.setupTrack` can consume the queues without paying `checkFramesS1`'s
~2.5 s import. Writers go through `queueS1.applyQueueDeltas()`, which re-reads
the queues under a lock and applies only the caller's own changes — a full
rewrite from a stale snapshot would silently undo a concurrent writer such as
`setupTrack --queue`.

| Queue | Meaning |
|---|---|
| `toProcess.yaml` | Passes all checks **and** a precise (EOF) orbit covering the acquisition exists in `orbitDir`. |
| `pendingProcessing.yaml` | Passes checks but the EOF orbit is not published yet (POEORB lands ~3 weeks after acquisition). |
| `problem.yaml` | An unresolved issue: residual gap (`--noBreakGap`), still >127 in-range bursts (`--noSplit`, or `<orbit>_4` already existed), out-of-range left in place (`--noMoveOutOfRange`), a `Failed` marker, or an ascending-node/burst error. |

**Promotion.** At the start of every run, each `pendingProcessing` unit is
re-evaluated; if its status changed (typically the EOF orbit has since arrived) it
is moved out of pending into `toProcess` (or `problem`). This is how a pass
downloaded before its orbit was ready eventually becomes processable.

**Problem clearing.** Each `problem` unit is likewise re-evaluated every run and
**removed once the condition clears** — the gap was fixed by hand, the unit was
processed, or it now routes elsewhere. Without this the queue would be
append-only, and because a unit sitting in any queue is never re-routed, a
transient problem would be stuck for ever after being reported once. A unit
whose directory is unreadable is deliberately **left in place** rather than
cleared: that is how a wrong `assemblyDir` would otherwise empty the queue.

A problem entry's `comment` is refreshed when the reason changes, but only for
entries this tool queued — `setupTrack`'s specific failure detail ("setup failed
at runPreProcTops") must not be overwritten by the generic `previous run Failed`
that the `Failed` marker produces here.

A unit carrying an `Ignore` file is **skipped** (not queued). A unit already
**fully processed** (a `Completed` marker or its `{orbit}-{seq}` output dir exists,
per `setupTrack`) is reported `-> processed` for info and left untouched — never
restructured or queued. The queues are **idempotent**: a unit already present in
*any* queue is reported `already queued` and never re-added; each file is rewritten
as a unit-deduped, sorted list.

The `--check` report is ordered by ascending-node date and shows the day gap to the
previous acquisition (`+Nd`), which makes a missed pass stand out (e.g. a jump from
`+6d` to `+12d`).

## CLI

```
checkFramesS1 [track] [options]

  track                Track to run on, e.g. track-16 (or just 16). Omit to scan
                       every track-<n>/ under --assemblyDir.
  --assemblyDir DIR    Root holding the track-<n>/ dirs and the queue files
                       [default: .]
  --orbitDir DIR       Precise-orbit (EOF) archive to test orbit availability
  --firstDate D        Skip datatakes with ascending-node date before D (YYYY-MM-DD)
                       [default: today - 6 months]
  --lastDate  D        Skip datatakes with ascending-node date after D
                       [default: today]
  --queueDir DIR       Where the cumulative queue YAMLs live [default: assemblyDir]
  --check              Dry run: print the checkframes-style per-orbit report
                       (date, frame coverage, gaps, out-of-range) and the routing
                       decision for every unit; change nothing on disk (no moves,
                       no queue writes, no ascendingNodeTime cache writes)
  --noSplit            Do not split over-length (>127 burst) datatakes
  --noBreakGap         Do not break gapped datatakes into segments
  --noMoveOutOfRange   Do not move out-of-range SAFEs to tmp/
```

Standalone examples:

```
# dry run over a whole project's assembly tree
checkFramesS1 --assemblyDir /Volumes/insar1/ian/S1-Greenland/data --orbitDir /…/OPOD --check

# one track only (for debugging)
checkFramesS1 track-16 --assemblyDir /Volumes/insar1/ian/S1-Greenland/data --orbitDir /…/OPOD
```

As driven by `autoupdateS1` (runs after the filing step, or in isolation via
`autoupdateS1 --checkFrames`), reading `assemblyDir`, `orbitDir`, and optional
`queueDir` from `autoupdate.yaml`.
