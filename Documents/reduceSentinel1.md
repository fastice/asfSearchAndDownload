# reduces1

Command-line utility that removes unwanted files from Sentinel-1 ZIP archives by filename pattern. The primary use-case is stripping cross-polarisation (HV or VH) data from dual-pol SLC or L0 products, which roughly halves the archive size for users who only need single-pol data.

When the system `zip` binary is available it is used directly (~10 s for a typical L0 SLC on fast storage). Otherwise the tool falls back to a pure-Python stream-copy, which is about 3× slower.

**Tested on both Sentinel-1 SLC and L0 products.**

> **Note:** `zip` may print a directory-mismatch warning when removing entries. This does not affect the integrity of the reduced file.

---

## Usage

```
reduces1 [--pattern PATTERN] [--directory DIRECTORY] [--suffix SUFFIX] [zipfile]
```

### Positional argument

| Argument | Description |
|----------|-------------|
| `zipfile` | Path to a single ZIP file to process |

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--pattern PATTERN` | `hv` | Substring to match against filenames inside the ZIP; all matching entries are removed |
| `--directory DIRECTORY` | — | Process all ZIP files in this directory (mutually exclusive with `zipfile`) |
| `--suffix SUFFIX` | `zip` | File extension to match when scanning `--directory` |
| `-h, --help` | — | Show help and exit |

Either `zipfile` or `--directory` must be supplied; if neither is given the help message is printed.

---

## How it works

1. Open the ZIP and collect all entry names that contain `PATTERN`.
2. If the `zip` CLI is on `PATH`, call `zip -d zipfile entry1 entry2 …` in place.
3. Otherwise, stream-copy all *non-matching* entries to a temporary ZIP in the same directory, then atomically replace the original with `os.replace()`.

No temporary files are left behind on success; the original ZIP is modified in-place (step 2) or replaced atomically (step 3).

---

## Examples

Remove all `hv` (default) entries from a single ZIP:
```bash
reduces1 S1A_IW_SLC__1SDH_20200101.zip
```

Remove all `vh` entries from a single ZIP:
```bash
reduces1 --pattern vh S1A_IW_SLC__1SDH_20200101.zip
```

Remove all `hv` entries from every `.zip` file in a directory:
```bash
reduces1 --directory /data/sentinel1/
```

Process a directory where files use a non-standard suffix (e.g. `.zip1`):
```bash
reduces1 --pattern vh --suffix zip1 --directory /data/sentinel1/
```

---

## Performance

| Method | Typical time (L0 SLC, fast RAID) |
|--------|----------------------------------|
| `zip` CLI (in-place deletion) | ~10 s |
| Python fallback (stream copy) | ~30 s |

The `zip` path is preferred automatically when available. No explicit configuration is needed.

---

## Dependencies

- Python standard library only (`zipfile`, `subprocess`, `shutil`, `tempfile`, `os`)
- `zip` CLI *(optional)* — used when present for faster in-place deletion
