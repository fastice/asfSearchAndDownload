# ASF Search and Download

Utilities for searching and downloading SAR products from the ASF DAAC (NISAR and Sentinel-1).

**reduces1** removes unwanted files from Sentinel-1 ZIP archives by filename pattern. The primary use-case is stripping cross-polarisation (HV or VH) data from dual-pol SLC or L0 products, roughly halving the archive size for users who only need single-pol data. It has been tested successfully on both Sentinel-1 SLC and L0 products.

**ariaDownload** downloads files from a URL list using aria2c, with time-of-day throttling (1 connection during office hours, 4 on weekends, 10 overnight) and optional local transfer-directory search before downloading.

**searchASF** searches the ASF DAAC for NISAR and Sentinel-1 products within a date range and spatial area, writing download URLs to text files and optionally exporting granule footprints to a GeoPackage for visualisation in QGIS.

## Installation

```bash
pip install git+https://github.com/fastice/asfSearchAndDownload.git@main
```

## Documentation

- [ariaDownload](Documents/ariaDownload.md) — download from URL list via aria2c with time-of-day throttling
- [reduces1](Documents/reduceSentinel1.md) — remove files from Sentinel-1 ZIP archives by pattern
- [searchASF](Documents/searchASF.md) — search ASF DAAC for NISAR and Sentinel-1 products

## For Further Information

Please address questions to ![](https://github.com/fastice/GrIMPTools/blob/main/Email.png).
