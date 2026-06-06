# ariaDownload

Downloads files from a URL list using [aria2c](https://aria2.github.io/), with automatic time-of-day connection throttling and optional local transfer-directory search before downloading.

---

## Usage

```
ariaDownload [options] downloadLinks
```

| Argument | Description |
|----------|-------------|
| `downloadLinks` | Text file containing one download URL per line |

---

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `--overWrite` | False | Re-download and overwrite files that already exist locally |
| `--xferDir DIR` | `*` | Directory to search for already-transferred files (`.zip` or `.zip.1`). Use `*` to search all `/Volumes/insar*/ian/xfer` paths |
| `--noRename` | False | Do not move/rename `.zip.1` files found in `--xferDir`; copy `.zip` files instead |

---

## Connection throttling

The number of aria2c parallel connections (`-x`) is set automatically based on time of day and day of week:

| Time | Day | Connections |
|------|-----|-------------|
| 07:00 – 18:00 | Weekday | 1 |
| 07:00 – 18:00 | Weekend | 4 |
| All other hours | Any | 10 |

---

## Local transfer directory search

Before downloading each file, `ariaDownload` checks the local transfer directories for an already-transferred copy:

- If `<file>.zip.1` is found: moves it to the current directory (unless `--noRename`)
- If `<file>.zip` is found: copies it to the current directory (unless `--noRename`)
- If neither is found: downloads via aria2c

Use `--xferDir *` (default) to search all mounted `/Volumes/insar*/ian/xfer` paths automatically.

---

## Examples

Download all URLs in a file:
```
ariaDownload my_downloads.txt
```

Re-download even if files already exist:
```
ariaDownload my_downloads.txt --overWrite
```

Search a specific transfer directory first:
```
ariaDownload my_downloads.txt --xferDir /Volumes/insar1/ian/xfer
```

---

## Dependencies

- `aria2c` — must be on PATH
