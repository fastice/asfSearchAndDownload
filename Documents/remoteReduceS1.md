# remoteReduceS1

Download a Sentinel-1 SLC zip from ASF **without** its cross-pol members, using
HTTP range requests. It replaces "download the whole zip with aria2c, then
`reduces1`" with a single step that fetches only the members that are kept,
about half the network traffic for a dual-pol SLC. Part of the
`asfSearchAndDownload` package; `autoupdateS1` uses it when its config sets
`remoteReduce: true`.

## Usage

```
remoteReduceS1 URL [--outputDir DIR] [--pattern hv] [--nConn N] [--limit MB/s]
```

| Option | Default | Description |
|---|---|---|
| `URL` | | ASF download URL of the granule zip (`https://datapool.asf.alaska.edu/SLC/...`) |
| `--outputDir` | `.` | Where the reduced zip is written |
| `--pattern` | `hv` | Members whose names contain this are not downloaded (same rule as `reduces1`) |
| `--nConn` | 1 | Parallel range requests |
| `--limit` | 0 | Combined download cap in MB/s (2^20 bytes, as aria2c's `M`) over all connections; 0 = none |

Earthdata credentials come from `~/.netrc`, as for aria2c.

## How it works

1. A `GET` of byte 0 follows the ASF -> Earthdata -> CloudFront redirects to a
   presigned URL (valid 60 min; re-signed automatically on a 403).
2. The zip's central directory is read with a few small range requests.
3. Each kept member's raw span (local header + compressed data, up to the next
   member's header, so data descriptors come along) is fetched; spans less
   than 4 MB apart are merged and runs are split into 64 MB pieces spread over
   `--nConn` connections. Every response must be a 206 with the exact
   requested `Content-Range` and length, or it is retried (5 tries per piece).
4. The pieces are written to `<name>.zip.part` at their new offsets, and a new
   central directory is written for the kept members (zip64 extras
   regenerated).
5. **Verify**: each local header must name its member, then `testzip()`
   decompresses every member and checks its CRC-32 against ASF's own. Only
   then is the file renamed to `<name>.zip`. A failure raises and leaves at
   most the `.part`.

The member bytes are copied, not recompressed, so the payload and CRCs are
identical to an aria2c download reduced with `zip -d` (checked on two 5 GB
S1C granules, 2026-11-11).

**Rate cap.** Reads are streamed in 1 MB blocks. Each block books the next free
slot on a clock shared by all connections and sleeps until it, so the cap holds
for any connection count, and an idle spell is not paid back as a burst.
Stalling the socket read lets TCP slow the sender, so the link itself is
throttled, as with aria2c's `--max-overall-download-limit`.

Each fetch appends one record to the network log (`networkLog.md`), program
`<caller>/remoteReduceS1`. Its MB/s figure includes the verify time.

## Timing (helheim, 5 GB S1C granule, 2.5 GB kept)

| | Time |
|---|---|
| aria2c -x 10 + `zip -d` | 53-77 s, 5.0 GB transferred |
| `--nConn 10`, no cap | plan 3 s + fetch 26-39 s + verify 21 s |
| `--nConn 10 --limit 20` | fetch 129 s (19.9 MB/s) |
| `--nConn 10 --limit 40` (autoupdateS1) | 89 s in all |

The verify, about 21 s and single-threaded, is the price of checking that
the ~45 separately fetched pieces landed correctly. aria2c's own check only
confirms the byte count.

## Not supported

- Resuming an interrupted fetch: a retry starts the granule again.
- Inputs other than a zip over HTTPS that honors range requests.
