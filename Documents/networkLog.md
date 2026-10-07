# networkLog: download and internet-interface usage logs

`asfsearchdownload/networkLog.py` writes two tab-separated logs per host to
`~/networkLogs/` (the home partition every Linux machine shares). Set
`$NETWORK_LOG_DIR` to write somewhere else, e.g. for testing.

## networkLog.&lt;host&gt;: one row per download

| column | meaning |
|---|---|
| `start`, `end` | local time with UTC offset |
| `seconds`, `MB`, `MB/s` | MB = 1e6 bytes |
| `connections` | aria2c `-x` (time-of-day dependent) or the tool's connection setting |
| `status` | downloader exit code, 0 = ok |
| `program` | `caller/downloader`, e.g. `autoupdateNISAR/ariaDownload`, `pullASF/ariaDownload`, `refreshOrbits`; `tcsh/...` = run by hand |
| `cwd`, `name` | where, and what file |

Written by:
- `ariaDownload`, once per file. This covers autoupdateNISAR, autoupdateS1, pullASF (the
  daytime Sentinel1Phase queue) and fetchMates. Bytes are the file size afterwards, so a
  resumed partial counts in full, and a failed one (status ≠ 0) may count its preallocated size.
- `refreshS1Orbits`, `downloadNISARoptimized`, `downloadRSLCSubset`, once per file
  (bytes actually fetched).
- `logNetworkUse`, one row per `aria2c -i` session in a shell script, e.g. the Antarctic
  GCOV `gcovDownload.sh`:

  ```
  ariaStart=$(date +%s)
  aria2c -i list ...
  logNetworkUse list --start $ariaStart --program gcovDownload --dir DIR \
      --connections "4x2 19M" --status $?
  ```

Logging never raises, so a full or unmounted home cannot stop a download.

## interfaceLog.&lt;host&gt;: whole-host internet traffic

`networkMonitor` reads the rx/tx byte counters of the interface holding the **default
route**. That is the internet link (eth100 on helheim), not the private NFS network
(192.168.0.x). The interface is looked up on each call, so each machine uses its own.
Each call logs the traffic since the previous call (`time interface seconds rxMB txMB
rxMB/s txMB/s`). The first call, an interface change or a reboot writes no row. Install
on each host with:

```
* * * * * /home/ian/miniforge3/bin/networkMonitor >> /dev/null 2>&1
```

The difference between the two logs is traffic the instrumented programs do not cover
(Landsat `getusgs`, isce2grimp, GDAL `/vsicurl` reads, browsers, ...).

## plotNetworkUse: plot the last N hours

```
plotNetworkUse [--hours 24] [--end 2026-10-08T12:00] [--hosts helheim ryder petermann] [--out networkUse.png]
```

One panel per host, all on the same time and MB/s scales: download (rx) as a filled area,
upload (tx) as a dashed line, and the periods each job was downloading shaded in a fixed
color per job (autoupdateNISAR blue, autoupdateS1 orange, pullASF aqua, gcovDownload
yellow, refreshOrbits pink, fetchMates green, manual violet). Each panel title gives
that host's GB down/up and peak MB/s. By default it plots every host that has an
`interfaceLog`.
