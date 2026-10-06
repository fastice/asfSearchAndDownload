# ASF Search and Download

Utilities for searching and downloading SAR products from the ASF DAAC (NISAR and Sentinel-1).

## Installation

```bash
pip install git+https://github.com/fastice/asfSearchAndDownload.git@main
```

## Programs

### Search and download

**searchASF** searches the ASF DAAC for NISAR and Sentinel-1 products within a date range and spatial area, writing download URLs to text files and optionally exporting granule footprints to a GeoPackage for visualisation in QGIS. [Documentation](Documents/searchASF.md)

**ariaDownload** downloads files from a URL list using aria2c, with time-of-day throttling (1 connection during office hours, 4 on weekends, 10 overnight) and optional local transfer-directory search before downloading. [Documentation](Documents/ariaDownload.md)

**reduces1** removes unwanted files from Sentinel-1 ZIP archives by filename pattern. The primary use-case is stripping cross-polarisation (HV or VH) data from dual-pol SLC or L0 products, roughly halving the archive size for users who only need single-pol data. It has been tested successfully on both Sentinel-1 SLC and L0 products. [Documentation](Documents/reduceSentinel1.md)

**pullASF** fetches a pair's Sentinel-1 granules into the archive (download, then cross-pol reduce), allowing one download at a time across all hosts, with an exit-code contract so a failed fetch can short-circuit the processing step that follows it in a queue line. [Documentation](Documents/pullASF.md)

### NISAR slim downloads

**downloadNISARoptimized** fetches NISAR GCOV products keeping only the datasets the GrIMP tools read, converting the large bands to float16 and storing the RTC factor once per track/frame/grid, for about 4.5x less storage than the archive product. It can also convert already-downloaded granules (`--local`). [Documentation](Documents/downloadNISARoptimized.md)

**downloadRSLCSubset** fetches a spatial subset of a NISAR RSLC (selected by an outline, slant range/time, or pixel window) as a small, self-consistent RSLC whose geometry metadata is cropped to match, so downstream tools need no hand edits. [Documentation](Documents/downloadRSLCSubset.md)

### Sentinel-1 archive maintenance

**autoupdateS1** is the nightly, config-driven Sentinel-1 archive-update driver: it refreshes orbits, searches, downloads and reduces new granules, files them into the assembly tree, vets and queues the resulting processing units, and optionally assembles them, with cross-host locking and email on unrecovered failures. [Documentation](Documents/autoupdateS1.md)

**refreshS1Orbits** refreshes the local Sentinel-1 precise-orbit (EOF) archive from ASF, downloading any orbit files newer than those already held. [Documentation](Documents/refreshS1Orbits.md)

**fileS1** unpacks Sentinel-1 SAFE zips (minus the cross-pol bands) into the `track-<n>/<orbit>/` assembly tree and marks each zip as filed. [Documentation](Documents/fileS1.md)

**checkFramesS1** vets the filed datatakes by burst-frame extent, splits gaps and over-length passes into clean processing units, and routes each to the `toProcess`, `pendingProcessing` or `problem` queue. [Documentation](Documents/checkFramesS1.md)

**queueS1** inspects and safely edits the Sentinel-1 processing queues (info, list, remove, promote), taking the queue lock and writing atomically; its module also defines the queue file format shared with `setupTrack`. [Documentation](Documents/queueS1.md)

**checkExcludeFrames** validates the per-track frame exclusions that stop a region-wide search area from downloading frames a track would only discard. [Documentation](Documents/excludeFrames.md)

## For Further Information

Please address questions to ![](https://github.com/fastice/GrIMPTools/blob/main/Email.png).
