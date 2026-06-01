# reduceSentinel1

Utilities for finding and reducing Sentinel-1 (and NISAR) archive data.

**reduces1** removes unwanted files from Sentinel-1 ZIP archives by filename pattern. The primary use-case is stripping cross-polarisation (HV or VH) data from dual-pol SLC or L0 products, roughly halving the archive size for users who only need single-pol data. It has been tested successfully on both Sentinel-1 SLC and L0 products.

**searchASF** searches the ASF DAAC for NISAR and Sentinel-1 products within a date range and spatial area, writing download URLs to text files and optionally exporting granule footprints to a GeoPackage for visualisation in QGIS.

## Installation

```bash
pip install git+https://github.com/fastice/reduceSentinel1.git@main
```

## Documentation

- [reduces1](Documents/reduceSentinel1.md) — remove files from Sentinel-1 ZIP archives by pattern
- [searchASF](Documents/searchASF.md) — search ASF DAAC for NISAR and Sentinel-1 products

## For Further Information

Please address questions to ![](https://github.com/fastice/GrIMPTools/blob/main/Email.png).
