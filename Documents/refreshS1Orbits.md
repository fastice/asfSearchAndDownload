# refreshS1Orbits

Refresh the local **Sentinel-1 precise-orbit (EOF) archive** from the ASF
`aux_poeorb` index. Part of the `asfSearchAndDownload` package; the orbit step
of [`autoupdateS1`](autoupdateS1.md) calls the same code in-process
(`refreshOrbits.updateStateVectors`), and it is also runnable standalone as
`refreshS1Orbits`.

## What it does

Scrapes the public index page `https://s1qc.asf.alaska.edu/aux_poeorb/` for
`S1*.EOF` links and, for each selected sensor, downloads every listed file whose
validity-start date is **newer than the newest one already in `orbitDir`**. Files
already present are skipped, so re-running is cheap.

- A sensor with **no** local `.EOF` (e.g. a newly launched satellite) is
  populated from scratch — all of its listed orbits are fetched.
- The index is public; the EOF downloads authenticate with Earthdata credentials
  from `~/.netrc`.
- Each file streams to `<name>.tmp` and is renamed into place, so an interrupted
  download never leaves a partial `.EOF` that would later read as valid.

Precise (POEORB) orbits are typically published **~3 weeks after acquisition**.
`checkFramesS1` uses `refreshOrbits.orbitFileReady(orbitDir, sensor, acqTime)` —
an EOF whose validity window covers the acquisition — to decide whether a unit
goes to `toProcess` or waits in `pendingProcessing`.

## CLI

```
refreshS1Orbits [--orbitDir DIR] [--S1A] [--S1B] [--S1C] [--S1D]

  --orbitDir DIR   Orbit (EOF) archive directory
                   [/Volumes/insar9/ian/Data/SentinelGreenland/OPOD]
  --S1A ... --S1D  Restrict to these sensors (default: all four)
```

Example — refresh only S1C and S1D:

```
refreshS1Orbits --S1C --S1D
```

The sensor flags are shared with `autoupdateS1` (`refreshOrbits.addSensorArgs`),
so both accept identical options. The standalone CLI has no `--check`; for a dry
run use `autoupdateS1 --check`, which passes `check` through to
`updateStateVectors`.
