#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Plot the last N hours of internet traffic for each host from ~/networkLogs:
one panel per host (shared time and MB/s axes), download (rx) as a filled
area, upload (tx) as a line, and the periods each job was downloading
(networkLog.<host>) shaded in a fixed color per job.
"""
import argparse
import collections
import csv
import glob
import os
from datetime import datetime, timedelta

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D

from asfsearchdownload.networkLog import LOG_DIR, now

# Fixed job -> color, so a job keeps its color from day to day.
JOB_COLORS = {'autoupdateNISAR': '#2a78d6', 'autoupdateS1': '#eb6834',
              'pullASF': '#1baf7a', 'gcovDownload': '#eda100',
              'refreshOrbits': '#e87ba4', 'fetchMates': '#008300',
              'manual': '#4a3aa7'}
SPARE_COLORS = ['#e34948']
OTHER_COLOR = '#9a9890'
SHELLS = {'tcsh', 'csh', 'bash', 'sh', 'zsh', 'unknown'}
INK, MUTED, GRID = '#262624', '#73726c', '#e8e6dc'


def readRows(path):
    ''' Rows of a tab-separated log as dicts; [] if missing '''
    if not os.path.exists(path):
        return []
    with open(path) as fp:
        return list(csv.DictReader(fp, delimiter='\t'))


def jobName(program):
    ''' autoupdateS1/ariaDownload -> autoupdateS1; a shell -> manual '''
    caller = program.split('/')[0]
    return 'manual' if caller in SHELLS else caller


def readTraffic(host, t0, t1):
    ''' Interface rows in (t0, t1]: list of (time, seconds, rxMB/s, txMB/s) '''
    rows = []
    for r in readRows(os.path.join(LOG_DIR, f'interfaceLog.{host}')):
        t = datetime.fromisoformat(r['time'])
        if t0 < t <= t1:
            rows.append((t, float(r['seconds']), float(r['rxMB/s']),
                         float(r['txMB/s'])))
    return rows


def readJobSpans(host, t0, t1, gap=timedelta(minutes=2)):
    '''
    Download periods per job overlapping (t0, t1], with back-to-back files
    (less than gap apart) merged into one span: {job: [(start, end), ...]}.
    '''
    spans = {}
    rows = readRows(os.path.join(LOG_DIR, f'networkLog.{host}'))
    for r in sorted(rows, key=lambda r: r['start']):
        start = datetime.fromisoformat(r['start'])
        end = datetime.fromisoformat(r['end'])
        if end < t0 or start > t1:
            continue
        job = spans.setdefault(jobName(r['program']), [])
        if job and start - job[-1][1] < gap:
            job[-1] = (job[-1][0], max(job[-1][1], end))
        else:
            job.append((start, end))
    return spans


def minuteOf(t):
    '''
    Minute bucket for an interface sample at t (e.g. 07:45:01 -> 07:45), which
    covers the minute ending there, as the plots' step='pre' draws it
    '''
    return t.replace(second=0, microsecond=0)


def loggedMBPerMinute(host, t0, t1):
    '''
    MB of logged downloads (networkLog) in each minute bucket of (t0, t1],
    prorated by time. Used for the part of the window before the host's
    interface monitor started.
    '''
    mb = collections.Counter()
    minute = timedelta(minutes=1)
    for r in readRows(os.path.join(LOG_DIR, f'networkLog.{host}')):
        start = datetime.fromisoformat(r['start'])
        end = datetime.fromisoformat(r['end'])
        a, b = max(start, t0), min(end, t1)
        if b <= a:
            continue
        rate = float(r['MB']) / max((end - start).total_seconds(), 1e-3)
        m = minuteOf(a) + minute
        while m - minute < b:
            mb[m] += rate * (min(b, m) - max(a, m - minute)).total_seconds()
            m += minute
    return mb


def segments(rows):
    ''' Split rows where the monitor missed samples, so gaps are not bridged '''
    seg = []
    for row in rows:
        if seg and (row[0] - seg[-1][0]).total_seconds() > 2.5 * row[1]:
            yield seg
            seg = []
        seg.append(row)
    if seg:
        yield seg


def styleAxis(ax, title):
    ''' Title, recessive grid and spines, muted ticks '''
    ax.set_title(title, loc='left', fontsize=10, color=INK)
    ax.set_ylabel('MB/s', color=MUTED)
    ax.grid(axis='y', color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    for side in ('left', 'bottom'):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)


def plotTraffic(ax, rows):
    ''' Download as a filled step area, upload as a dashed step line '''
    for seg in segments(rows):
        t = [r[0] for r in seg]
        rx = [r[2] for r in seg]
        tx = [r[3] for r in seg]
        ax.fill_between(t, rx, step='pre', color=INK, alpha=0.30, lw=0)
        ax.step(t, rx, where='pre', color=INK, lw=1.0)
        ax.step(t, tx, where='pre', color=MUTED, lw=1.0, ls='--')


def assignColors(jobs):
    ''' Known jobs get their fixed color; new ones a spare, then gray '''
    spare = list(SPARE_COLORS)
    colors = {}
    for job in jobs:
        if job in JOB_COLORS:
            colors[job] = JOB_COLORS[job]
        else:
            colors[job] = spare.pop(0) if spare else OTHER_COLOR
    return colors


def plotArgs():
    ''' Handle command line args '''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mPlot internet traffic and download jobs per '
        f'host from {LOG_DIR}\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('--hours', type=float, default=24,
                        help='Hours to plot, ending at --end [24]')
    parser.add_argument('--end', type=str, default=None,
                        help='End time, e.g. 2026-10-08T12:00 [now]')
    parser.add_argument('--hosts', type=str, nargs='+', default=None,
                        help='Hosts to plot [all with an interfaceLog]')
    parser.add_argument('--out', type=str, default='networkUse.png',
                        help='Output figure [networkUse.png]')
    return parser.parse_args()


def main():
    ''' Plot traffic and download jobs for each host '''
    args = plotArgs()
    t1 = datetime.fromisoformat(args.end).astimezone() if args.end else now()
    t0 = t1 - timedelta(hours=args.hours)
    hosts = args.hosts or sorted(
        os.path.basename(p).split('.', 1)[1]
        for p in glob.glob(os.path.join(LOG_DIR, 'interfaceLog.*')))
    if not hosts:
        print(f'No interfaceLog.* files in {LOG_DIR}')
        return
    traffic = {h: readTraffic(h, t0, t1) for h in hosts}
    spans = {h: readJobSpans(h, t0, t1) for h in hosts}
    jobs = sorted({j for s in spans.values() for j in s})
    colors = assignColors(jobs)
    #
    # Last row: all machines summed, on its own MB/s scale.
    fig, axes = plt.subplots(len(hosts) + 1, 1, sharex=True,
                             figsize=(13, 2.6 * (len(hosts) + 1) + 1.2),
                             squeeze=False, layout='constrained')
    for ax in axes[1:-1, 0]:
        ax.sharey(axes[0, 0])
    tz = t1.tzinfo
    totalGB = 0.
    rxSum, txSum = collections.Counter(), collections.Counter()
    for ax, host in zip(axes[:-1, 0], hosts):
        for job, jobSpans in spans[host].items():
            for start, end in jobSpans:
                ax.axvspan(max(start, t0), min(end, t1), color=colors[job],
                           alpha=0.22, lw=0)
        rows = traffic[host]
        plotTraffic(ax, rows)
        for t, _, rx, tx in rows:
            rxSum[minuteOf(t)] += rx
            txSum[minuteOf(t)] += tx
        rxGB = sum(r[1] * r[2] for r in rows) / 1e3
        txGB = sum(r[1] * r[3] for r in rows) / 1e3
        peak = max((r[2] for r in rows), default=0)
        # Before the interface monitor started, fall back on the download log
        # (the two never cover the same time, so nothing is counted twice).
        monitorStart = rows[0][0] - timedelta(seconds=rows[0][1]) if rows \
            else t1
        preMB = loggedMBPerMinute(host, t0, monitorStart) \
            if monitorStart > t0 else {}
        for m, mb in preMB.items():
            rxSum[m] += mb / 60
        preGB = sum(preMB.values()) / 1e3
        downGB = rxGB + preGB
        totalGB += downGB
        iface = readRows(os.path.join(LOG_DIR, f'interfaceLog.{host}'))
        iface = iface[-1]['interface'] if iface else '?'
        note = (f' (incl. {preGB:.1f} GB from download log before '
                f'{monitorStart:%H:%M})') if preGB >= 0.05 else ''
        styleAxis(ax, f'{host} ({iface}):  {downGB:.1f} GB down{note}, '
                      f'{txGB:.1f} GB up, peak {peak:.1f} MB/s')
    axes[0, 0].set_ylim(bottom=0)
    #
    ax = axes[-1, 0]
    total = []
    if rxSum:
        m, minute = min(rxSum), timedelta(minutes=1)
        while m <= minuteOf(t1):
            total.append((m, 60., rxSum[m], txSum[m]))
            m += minute
    plotTraffic(ax, total)
    peakTime, peak = max(((r[0], r[2]) for r in total),
                         key=lambda x: x[1], default=(t1, 0))
    styleAxis(ax, f'All {len(hosts)} machines:  {totalGB:,.1f} GB down, '
                  f'peak {peak:.1f} MB/s at {peakTime:%H:%M}')
    ax.set_xlim(t0, t1)
    ax.set_ylim(bottom=0)
    locator = mdates.AutoDateLocator(tz=tz)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator, tz=tz))
    #
    handles = [Patch(facecolor=INK, alpha=0.30, label='download (rx)'),
               Line2D([], [], color=MUTED, ls='--', label='upload (tx)')]
    handles += [Patch(facecolor=colors[j], alpha=0.35, label=j) for j in jobs]
    fig.legend(handles=handles, loc='outside upper center',
               ncol=min(len(handles), 5), frameon=False, fontsize=9)
    ax.set_xlabel(f'Internet traffic per host, {t0:%Y-%m-%d %H:%M} to '
                  f'{t1:%Y-%m-%d %H:%M %Z}', color=MUTED)
    fig.savefig(args.out, dpi=150, bbox_inches='tight')
    print(f'Wrote {args.out}')


if __name__ == '__main__':
    main()
