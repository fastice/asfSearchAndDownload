#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Refresh the Sentinel-1 precise-orbit (state-vector) archive from ASF.

Scrapes the ASF precise-orbit index (aux_poeorb), and downloads any EOF files
newer than the newest one already present locally, per selected sensor. The
index page is public; the EOF downloads authenticate via ~/.netrc (requests'
trust_env default). Ported from stateVectorUpdate/RefreshState.ipynb.

Part of the asfSearchAndDownload package.
"""
import argparse
import glob
import os
import urllib.request
from datetime import datetime
from html.parser import HTMLParser

import requests

POEORB_URL = 'https://s1qc.asf.alaska.edu/aux_poeorb/'
DEFAULT_ORBIT_DIR = '/Volumes/insar9/ian/Data/SentinelGreenland/OPOD'
ALL_SENSORS = ('S1A', 'S1B', 'S1C', 'S1D')


def decodeDate(fileName):
    '''
    Return the orbit-validity start date from an EOF filename, e.g.
    S1A_OPER_AUX_POEORB_OPOD_..._V20260202T225942_20260204T005942.EOF -> 2026-02-02.
    '''
    return datetime.strptime(fileName.split('_V')[1].split('T')[0], '%Y%m%d')


def decodeValidity(fileName):
    '''
    Return the (start, stop) validity datetimes from an EOF filename, e.g.
    ..._V20260202T225942_20260204T005942.EOF -> (2026-02-02 22:59:42,
    2026-02-04 00:59:42). Raises ValueError/IndexError on a non-conforming name.
    '''
    startStop = fileName.split('_V')[1].rsplit('.', 1)[0]
    start, stop = startStop.split('_')
    fmt = '%Y%m%dT%H%M%S'
    return datetime.strptime(start, fmt), datetime.strptime(stop, fmt)


def orbitFileReady(orbitDir, sensor, acqTime):
    '''
    Return the path of a precise-orbit EOF in orbitDir whose validity window
    covers acqTime for the given sensor (e.g. 'S1A'), or None if none is present
    yet. Precise (POEORB) orbits are typically not published until ~3 weeks
    after acquisition, so a freshly downloaded pass will return None here.
    '''
    for path in sorted(glob.glob(os.path.join(orbitDir, f'{sensor}*.EOF'))):
        try:
            start, stop = decodeValidity(os.path.basename(path))
        except (IndexError, ValueError):
            continue
        if start <= acqTime <= stop:
            return path
    return None


class _orbitLinkParser(HTMLParser):
    '''
    Collect the .EOF hrefs (which begin with the sensor token, e.g. S1A) from
    the aux_poeorb directory index page.
    '''
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != 'a':
            return
        for name, link in attrs:
            if name == 'href' and link.startswith('S1') and link.endswith('.EOF'):
                self.links.append(link)


def fetchOrbitIndex(url=POEORB_URL):
    '''
    Return the list of EOF filenames listed on the aux_poeorb index page.
    '''
    with urllib.request.urlopen(url) as resp:
        html = resp.read().decode()
    parser = _orbitLinkParser()
    parser.feed(html)
    return parser.links


def newestLocalDate(orbitDir, sensor):
    '''
    Return the newest orbit-validity start date among local <sensor>*.EOF files
    in orbitDir, or None if there are none.
    '''
    dates = []
    for path in glob.glob(os.path.join(orbitDir, f'{sensor}*.EOF')):
        try:
            dates.append(decodeDate(os.path.basename(path)))
        except (IndexError, ValueError):
            continue
    return max(dates) if dates else None


def _download(session, url, outPath, chunkSize=1024 * 1024):
    '''
    Stream a URL to outPath via a netrc-authenticated requests session, writing
    to a .tmp file first so an interrupted download leaves no partial in place.
    '''
    tmpPath = outPath + '.tmp'
    with session.get(url, allow_redirects=True, stream=True) as r:
        r.raise_for_status()
        with open(tmpPath, 'wb') as f:
            for chunk in r.iter_content(chunk_size=chunkSize):
                if chunk:
                    f.write(chunk)
    os.replace(tmpPath, outPath)


def updateStateVectors(orbitDir=DEFAULT_ORBIT_DIR, sensors=ALL_SENSORS,
                       url=POEORB_URL, check=False):
    '''
    Download EOF orbit files newer than the newest local file, per sensor, into
    orbitDir. Returns the list of newly downloaded filenames. With check, report
    what would be downloaded without fetching or creating anything.

    A sensor with no existing local .EOF (e.g. a newly launched satellite such
    as S1D) is populated from scratch: all of its listed orbits are fetched.
    Established sensors always have local files anchoring the incremental update,
    so this full-populate branch only fires for a genuinely new sensor.
    '''
    if not check:
        os.makedirs(orbitDir, exist_ok=True)
    links = fetchOrbitIndex(url)
    downloaded = []
    session = requests.Session()  # trust_env default -> uses ~/.netrc
    for sensor in sensors:
        threshold = newestLocalDate(orbitDir, sensor)
        if threshold is None:
            print(f'refreshOrbits: no existing {sensor} .EOF in {orbitDir}; '
                  'downloading all available (first-time populate)')
            threshold = datetime.min
        for link in links:
            if not link.startswith(sensor):
                continue
            if decodeDate(link) <= threshold:
                continue
            outPath = os.path.join(orbitDir, link)
            if os.path.exists(outPath):
                continue
            if check:
                print(f'[check] would download orbit {link}')
            else:
                print(f'Downloading orbit {link}')
                _download(session, url + link, outPath)
            downloaded.append(link)
    verb = 'would download' if check else 'new'
    print(f'refreshOrbits: {len(downloaded)} {verb} orbit file(s) in {orbitDir}')
    return downloaded


def selectedSensors(args):
    '''
    Return the sensors chosen by the --S1A/--S1B/--S1C/--S1D flags on args, or
    all four when none are set.
    '''
    chosen = tuple(s for s in ALL_SENSORS if getattr(args, s, False))
    return chosen if chosen else ALL_SENSORS


def addSensorArgs(parser):
    '''
    Add the --S1A/--S1B/--S1C/--S1D sensor-select flags to an argparse parser.
    Shared with the autoupdateS1 driver so both use identical dest names.
    '''
    for sensor in ALL_SENSORS:
        parser.add_argument(f'--{sensor}', action='store_true', default=False,
                            help=f'Restrict to {sensor} (default: all sensors)')


def parseArgs():
    '''
    Handle command line args for the standalone refreshS1Orbits CLI.
    '''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mRefresh the Sentinel-1 precise-orbit (EOF) '
        'archive from ASF aux_poeorb\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('--orbitDir', type=str, default=DEFAULT_ORBIT_DIR,
                        help=f'Orbit (EOF) archive directory '
                        f'(default: {DEFAULT_ORBIT_DIR})')
    addSensorArgs(parser)
    return parser.parse_args()


def main():
    ''' Refresh S1 orbit state vectors. '''
    args = parseArgs()
    updateStateVectors(args.orbitDir, selectedSensors(args))


if __name__ == '__main__':
    main()
