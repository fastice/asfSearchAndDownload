# autoupdateS1

Automated **Sentinel-1 IW SLC** archive-update driver, configured by an
`autoupdate.yaml` that normally sits in the project directory. Intended to run
nightly from cron, but also runnable ad hoc from the CLI for other regions and
time periods.

It is the S1 analogue of `autoupdateNISAR` (nisargrimpworkflow), scoped for now
to **keeping the archive current** — later processing stages will be layered on
and `autoupdate.yaml` will grow to carry their config.

## What each run does

1. **Refresh orbits (state vectors).** `refreshOrbits.updateStateVectors` scrapes
   the ASF precise-orbit index (`https://s1qc.asf.alaska.edu/aux_poeorb/`) and
   downloads any `.EOF` files newer than the newest local one, per selected
   sensor, into `orbitDir`. Authenticates via `~/.netrc`. Skip with `--noOrbits`.
2. **Search.** `searchASF --sensor SENTINEL1 --products SLC --beamMode IW`,
   deduped against the existing archive via `--archiveDir <archiveDir>/*/*`
   (which strips `.zip`/`.zip.1` before comparing). URL list and coverage
   GeoPackage are written under `archiveDir/searchResults/`.
3. **Download + reduce.** Passes are downloaded **one at a time** via
   `ariaDownload` (which adjusts aria2c bandwidth by time of day). Each pass is
   filed under `archiveDir/<YYYY>-<MM>/`, where the month comes from the first
   date token in the granule name. After a download completes it is **verified**
   (no leftover `.aria2` control file and `zipfile.is_zipfile` passes); on
   failure it is re-downloaded up to `maxAttempts`. On success the unneeded
   cross-pol is stripped (`reduceSentinel1`) in a **background thread** while the
   next download starts. Granules that exhaust `maxAttempts` are remembered and
   given one **retry pass at the end of the download stage** — a whole-pass
   failure is usually a brief ASF-side outage that has cleared by then. The
   `--maxDownloads` cap does not apply to the retry pass (those granules were
   already inside the cap), and recoveries are folded back into the summary
   counts, so `failed N` reports only what is still missing after the retry.

   **Concurrent filing.** Once a granule's reduce returns, it is handed straight
   to a small pool of filing workers (`fileWorkers`, default 2) that unzip it
   into the assembly tree while the *next* granule downloads. Unzipping (~88 s)
   is faster than downloading (~155 s), so stage 4 costs almost nothing on a
   normal run. The hand-off is the statement after `remove_files_from_zip`
   returns, which is what guarantees a zip is never unzipped while `zip -d` is
   rewriting it in place. Disable with `--noFileDuringDownload` to get the old
   download-everything-then-file behaviour.

A pass already present anywhere under `archiveDir/<YYYY>-<MM>/` as `.zip` or
`.zip.1` (the `.1` marks an already-processed pass) is not re-downloaded.

4. **File.** If `assemblyDir` is configured, `fileS1` unpacks the archive zips
   (`archiveDir/<YYYY-MM>/*.zip`, across all month subdirs) into the per-track/
   per-orbit tree `assemblyDir/track-<n>/<orbit>/`, excluding the cross-pol SLC
   measurement bands, then renames each source `.zip` → `.zip.1`. It writes a
   YAML record of what it filed to `logs/filedS1.<MM-DD-YYYY>.yaml` (`tracks:`
   list of all tracks touched, `granules:` list of zip paths filed this run) for
   downstream steps to consume. Run this step **in isolation** (skipping orbits +
   search/download) with `--fileData`.
5. **Frame check.** If `assemblyDir` is configured, `checkFramesS1` vets each
   filed datatake (burst-frame coverage, gaps, over-length, out-of-range),
   restructures it into clean processing units (`<orbit>_4` split head, `<orbit>_1+`
   far-side gap segments, out-of-range → `tmp/`), and routes each unit into the
   cumulative `toProcess` / `pendingProcessing` / `problem` queues under `queueDir`
   (default `assemblyDir`). Each queued record carries the orbit, date, and in-range
   start/end/total frames. A datatake whose precise (EOF) orbit isn't published yet
   goes to `pendingProcessing`; at the start of each run those are re-checked and
   promoted to `toProcess` once the orbit arrives. Run in isolation with
   `--checkFrames`. See [checkFramesS1](checkFramesS1.md).
6. **Assemble.** With `assemble: true`, the queued units are pushed through
   `setupTrack --queue` (see [setupTrack](../../s1setup/Documents/setupTrack.md)),
   which runs the 5-step preprocessing pipeline for each one, consumes it from
   `toProcess`, records it in `processed.<date>.yaml`/`completed.yaml`, and
   routes failures to `problem.yaml` with the failing step as the comment.
   Run in isolation with `--assembleOnly`; skip with `--noAssemble`.

   `setupTrack` is invoked as a **subprocess**, not imported: `s1setup` already
   depends on this package for the queue format, so importing it here would make
   the dependency circular. It is on `PATH` as a console script, the same way
   `searchASF` and `ariaDownload` are called.

   **This stage is what keeps the disk from filling.** A filed-but-unassembled
   unit carries ~30 GB of measurement TIFFs; assembling it strips them (the
   source `.zip.1` is kept, so the unit can still be re-filed and reprocessed),
   taking it to ~100 MB. At ~37 units a night that is roughly 1.1 TB/night
   reclaimed. Disable stripping with `noStripTiffs: true` if you need in-place
   `--overWrite` reprocessing, but budget the space.

## Free-space alarm

After the stages run, the free space on the volume holding `assemblyDir` is
recorded in the summary. If it falls below `minFreeTB` (default 6), the run logs
`LOW DISK`, adds a note to the summary, and **mails it** — a third trigger
alongside failed downloads and new problem units, still exactly one email.

## Sensors

Which satellites are handled (both the SLC download filter and the orbit
refresh) comes from the `satellites` config key (default all of
S1A/S1B/S1C/S1D). The CLI flags `--S1A --S1B --S1C --S1D` override the config for
a one-off run.

## Date range

`firstDate` defaults to **today − 6 months**, `lastDate` to **today**. Override
per run with `--firstDate`/`--lastDate` (YYYY-MM-DD), or set `firstDate`/
`lastDate` in the yaml. Because the search is deduped against the archive, a wide
window is safe and simply advances as the archive fills.

## CLI

```
autoupdateS1 [config] [options]

  config            Path to autoupdate.yaml (default: ./autoupdate.yaml)
  --maxDownloads N  Soft cap on downloads per run (0 = no limit); overrides the
                    config maxDownloads key [default 300]. Soft: once N is
                    reached, the remaining frames of the pass in progress (same
                    orbit + datatake) are finished before stopping, so a pass is
                    never left half-downloaded (e.g. hitting 300 mid-pass with 5
                    frames left stops at 305).
  --firstDate D     Search start YYYY-MM-DD (overrides config; default today-6mo)
  --lastDate  D     Search end   YYYY-MM-DD (overrides config; default today)
  --region  R       Predefined region: greenland | antarctica (overrides config)
  --searchArea F    GeoJSON/.shp/lon,lat polygon (overrides config and --region)
  --S1A --S1B --S1C --S1D   Restrict to specific sensor(s) (default: all)
  --noOrbits        Skip the orbit (state-vector) refresh
  --noDownload      Skip search+download (only refresh orbits)
  --fileData        Run only the filing step: unpack archive zips into the
                    assemblyDir track tree (skips orbits + search/download)
  --checkFrames     Run only the frame-check step: vet filed datatakes and
                    queue them (skips orbits + search/download + filing)
  --assembleOnly    Run only the assemble step: push the queued units through
                    setupTrack (skips every earlier stage)
  --noAssemble      Skip the assemble step even when the config enables it
  --check           Dry run across every stage: report what would be
                    downloaded, filed, or written without modifying anything on
                    disk (search results go to a scratch temp dir; the archive
                    is only read for dedup). Composes with the other flags.
```

Companion CLI `refreshS1Orbits [--orbitDir DIR] [--S1A ...]` refreshes just the
orbit archive.

## autoupdate.yaml keys

```yaml
archiveDir: /Volumes/insar4/ian/Data/S1-Greenland          # required
assemblyDir: /Volumes/insar1/ian/S1-Greenland/data          # file stage target tree
orbitDir:   /Volumes/insar9/ian/Data/SentinelGreenland/OPOD # EOF archive
# queueDir:  /Volumes/insar1/ian/S1-Greenland/data          # frame-check queues [default: assemblyDir]
region:     Greenland      # or: Antarctica  (or use searchArea instead)
satellites: S1A S1B S1C S1D  # which sensors [all four]
productType: SLC           # searchASF --products [SLC]
beamMode:   IW             # searchASF --beamMode [IW]
direction:  both           # both | ascending | descending [both]
# searchArea: /path/to/aoi.geojson   # alternative to region
# firstDate: 2026-01-01    # optional; default today - 6 months
# lastDate:  2026-08-01    # optional; default today
# maxDownloads: 300        # soft cap per run (0 = no limit); finishes the pass [300]
# reducePattern: hv        # cross-pol substring to strip (Greenland HH+HV) [hv]
# maxAttempts: 3           # download retries per pass [3]
# fileWorkers: 2           # concurrent unzips while downloading [2]
#                          #   (to turn it off use --noFileDuringDownload)
# assemble: true           # run stage 6 (setupTrack) after the frame check [off]
# assembleMaxUnits: 0      # cap units assembled per run (0 = drain the queue)
# noStripTiffs: false      # keep measurement TIFFs after processing [strip them]
# minFreeTB: 6             # email when the assembly volume drops below this [6]
# notifyEmail: irj@uw.edu  # who to mail on unrecovered failures (opt-in;
#                          #   with no key nothing is ever mailed)
```

Only `archiveDir` is strictly required; a spatial constraint (`region` or
`searchArea`) is required unless supplied on the CLI. `direction` ascending/
descending is passed through to `searchASF --flightDirection`. `assemblyDir` is
required for the file stage (`--fileData`, and the automatic step 4); if it is
absent the normal run logs `no assemblyDir in config; skipping file stage` and
finishes after download — so existing configs keep working unchanged.

## Locking (multi-machine safe)

The project may be reachable from several machines over NFS, so the run holds
cross-host lock files on the shared tree (not in `/tmp` or `/var`, which are
per-host). Because NFS `flock`/`fcntl` is unreliable, the lock is an atomic
`O_EXCL` lock file. **Each lock lives beside the thing it guards:**

- `<projectDir>/autoupdateS1_download.lock` — held during **search + download**;
  it guards `archiveDir`, which is per-project.
- `<assemblyDir>/.assemblyTree.lock` — held during **filing and the frame check**
  (both write the assembly tree), and across the whole download stage when
  filing concurrently.

The assembly lock sits in `assemblyDir` rather than the project dir so that any
tool given `--assemblyDir` can take it — notably `s1setup.setupTrack --queue`,
which has no way to find the project dir. Without that, `checkFramesS1` could
`shutil.move` a SAFE out from under a running `setupTrack`.

Each lock is **non-blocking**: if another run already holds it, this run logs
`… another run holds <lock>; skipping …` and skips only that stage (the two locks
are independent). A lock older than `STALE_LOCK_HOURS` (48 h — longer than any
normal run) is treated as abandoned (a crashed run) and reclaimed. The lock file
records `host pid time` for debugging. `--check` runs take no locks. These are
independent of the per-host `flock -n` you may wrap the cron line in — that guards
one machine; these guard across machines.

## Cron

Use the versioned wrapper `scripts/runAutoupdateS1.sh` (activates the conda env,
guards against an unmounted project tree, then `exec autoupdateS1`):

```
# nightly at 02:15
15 2 * * *  /home/ian/PycharmProjects/packages/asfSearchAndDownload/scripts/runAutoupdateS1.sh \
            /Volumes/insar1/ian/NISAR/realNISAR/newGreenlandProject \
            >> /Volumes/insar1/ian/NISAR/realNISAR/newGreenlandProject/logs/cron.log 2>&1
```

## Logs

Each run writes a timestamped session log `logs/autoupdateS1_<YYYY-MM-DDThhmmss>.log`
in the project directory (override the location with the `logDir` config key). It
records the session header, orbit files downloaded, the search command, each SLC
downloaded and reduced, and any errors (with traceback on an unexpected failure).

The full log runs to thousands of lines on a normal night, so two companion
files are written beside it:

- **`<log>.summary`** — the whole session on one screen: counts downloaded,
  already-in-archive, failed, filed, and the frame-check queue totals, plus
  elapsed time. Always written, including after a crash (the summary matters
  most when the run did not finish). This is the file to read each morning.
- **`<log>.failures`** — written **only** when granules are still missing after
  every retry. Bare URLs, one per line, nothing else, so it can be handed
  straight back to the downloader:

  ```
  ariaDownload logs/autoupdateS1_2026-08-13T230004.failures
  ```

## Email notification

**Opt-in.** With no `notifyEmail` key in `autoupdate.yaml`, nothing is ever mailed.
Set `notifyEmail: you@example.com` and the summary is mailed to that address when
any of these happen:

- a granule is still missing after `maxAttempts` **and** the end-of-run retry pass;
- **a unit landed in the `problem` queue** and has not been reported yet;
- the session crashes outright.

A clean run sends nothing, and `--check` never mails. (Same contract as
`nisargrimpworkflow.autoupdate.notifyOnErrors`.)

Several triggers still produce **exactly one** email — the subject names each
one, e.g. `3 download(s) failed, 2 new problem unit(s) on helheim`.

Problem units are collected by scanning `problem.yaml` for records without a
`notified` flag, not from this run's own results, so units routed there by
`setupTrack --queue` since the last run are picked up too. The flag is set
**only after the send succeeds**: a failed send costs a duplicate next run,
whereas marking early would lose the notice for good. Because the flag is only
written when mail is actually configured and sent, enabling `notifyEmail` on a
project that has been accumulating problems delivers one backlog email (capped
at 50 units in the body).

Do **not** use `root` as the recipient without checking `/etc/aliases` first — on
the GrIMP workstation `root:` fans out to several people.

Delivery is best effort via the local MTA (`mail`, then `mailx`): a machine with
no working mailer logs a warning rather than failing the run.

## Debugging while catching up

The user is several months behind. Run one pass at a time with:

```
autoupdateS1 --maxDownloads 1
```

Repeat to walk forward; each run refreshes orbits, downloads a single new pass
(deduped), reduces it, and exits.
