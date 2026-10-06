# Per-track frame exclusions

Stops granules being downloaded that a track will only throw away.

A search area drawn for a whole region — the Greenland outline — clips in frames
that sit just outside an individual track's `frameRange`. Those granules are
downloaded, cross-pol reduced, filed, and then binned to `track-N/tmp` by
`checkFramesS1`, on every repeat cycle. As of 2026-08-16 that had accumulated
**368 SAFEs, 1.07 TB** across seven Greenland tracks.

Widening or narrowing the region outline is not the fix: it is shared by every
track, so a change made for one can starve another. These exclusions are scoped
per track instead.

---

## Where the list lives

`<assemblyDir>/track-N/excludeFrames` — one line of **ASF frame numbers**
(`#` comments allowed):

```
# ASF frame numbers the Greenland search outline clips in but this track's
# frameRange puts out of range.
317 323 328 333 338
```

Deliberately beside that track's `frameRange` rather than in `autoupdate.yaml`:
the two only make sense together, so widening a `frameRange` puts its exclusions
right there to be re-checked.

**These are ASF/ESA frame numbers, not the burst numbers in `frameRange`.** They
are different numbering schemes — track-90 uses ASF frames 194…264 for a
`frameRange` of `360 486`. Get the frame number for a granule from the search
GeoPackage (`searchResults/gpkg/*.gpkg`, columns `granule`, `track`, `frame`),
which is what `checkExcludeFrames` does.

---

## How it reaches the search

`autoupdateS1.excludedFramesSpec()` gathers every `track-*/excludeFrames` under
`assemblyDir` and passes them to the search as one argument:

```
searchASF ... --excludeFrames "90:191 141:317,323,328,333,338"
```

`searchASF` drops matching granules in the same filter that already handles
`--excludeTracks`, and reports them separately:

```
Found: 195  Excluded frames 90:191: 8
```

Nothing configured means nothing is passed and the search is unchanged.

---

## Verifying a list

The dangerous mistake is excluding a frame the track actually uses — the
acquisitions would simply stop arriving, with nothing to notice. Check before
and after any edit:

```
checkExcludeFrames --assemblyDir /Volumes/insar8/ian/Data/SentinelGreenland \
                   --gpkgDir /Volumes/insar4/ian/Data/S1-Greenland/searchResults/gpkg
```

For every listed frame it reports one of:

| verdict | meaning |
|---|---|
| `ok - accounts for N binned SAFE(s)` | the frame explains SAFEs already in `tmp/`: exactly what it is for |
| `ok - but explains no binned SAFE here` | nothing in `tmp/` matches; the entry may be wrong or simply older than every gpkg |
| `CLASH: N in-range SAFE(s)` | **the track uses this frame** — remove it |

Exit status is 1 if any clash is found.

`--tracks N [N ...]` restricts the check.

---

## Seeded lists (2026-08-16)

| track | frames | binned SAFEs it explains |
|---|---|---|
| 46 | 270 | 22 |
| 74 | 233 | 23 |
| 83 | 394 | 113 |
| 90 | 191 | 23 |
| 112 | 319 | 24 |
| 119 | 192 | 20 |
| 141 | 317, 323, 328, 333, 338 | 143 |

Measured effect on one track: a live search of track-90 over 2026-03-01 to
2026-06-01 returned 203 granules / 962.9 GB without the exclusion and 195 /
930.9 GB with it — 8 granules and 32 GB in three months, from one frame.

**Track-141 frame 378 is deliberately absent.** It appears both in `tmp/` and in
kept units, so it is in range on some datatakes and not others; excluding it
would drop wanted data. Track-141 is the largest waster (565 GB), so it is worth
understanding rather than guessing at.

---

## Limits

The seeded lists are *observed*, not derived: a frame is listed because SAFEs
carrying it were found in `tmp/`. Frames not yet seen are not listed, which
costs a missed saving but never data. Granules older than every retained gpkg
cannot be resolved to a frame at all — `checkExcludeFrames` reports those as
unmatched rather than assuming them safe.

An ASF frame's burst span is fixed geometry, so a frame that is out of range for
a track stays out of range unless that track's `frameRange` changes. That is the
case to re-run `checkExcludeFrames` for.

---

## Part of the asfSearchAndDownload package.
