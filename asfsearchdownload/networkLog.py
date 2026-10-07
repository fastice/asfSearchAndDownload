#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Append one line per network download to a per-host log in the shared home
directory, so the traffic of every machine (ryder, helheim, petermann, ...) can
be documented side by side.

Log file: ~/networkLogs/networkLog.<host> (directory overridable with
$NETWORK_LOG_DIR). Tab separated, with a header line written on first use:

    start  end  seconds  MB  MB/s  connections  status  program  cwd  name

start/end are local times with UTC offset; MB is 1e6 bytes; status is the
downloader's exit code (0 = ok); program is "caller/downloader", e.g.
autoupdateNISAR/ariaDownload.

logNetworkUse (main below) logs a whole aria2c -i session from a shell script.

networkMonitor (monitorMain below), run from cron every minute, logs the whole
host's traffic on its INTERNET interface (the default route, e.g. eth100 on
helheim; never the private NFS link) to ~/networkLogs/interfaceLog.<host>:

    time  interface  seconds  rxMB  txMB  rxMB/s  txMB/s
"""
import argparse
import fcntl
import json
import os
import socket
from datetime import datetime

LOG_DIR = os.environ.get('NETWORK_LOG_DIR',
                         os.path.expanduser('~/networkLogs'))
FIELDS = ['start', 'end', 'seconds', 'MB', 'MB/s', 'connections', 'status',
          'program', 'cwd', 'name']


def logPath(kind='networkLog'):
    ''' This host's log file of the given kind '''
    host = socket.gethostname().split('.')[0]
    return os.path.join(LOG_DIR, f'{kind}.{host}')


def now():
    ''' Current local time with UTC offset '''
    return datetime.now().astimezone()


def callerName():
    '''
    Name of the program that launched this process (e.g. autoupdateNISAR,
    pullASF), read from the parent's /proc cmdline; a login shell gives tcsh,
    i.e. run by hand.
    '''
    try:
        with open(f'/proc/{os.getppid()}/cmdline', 'rb') as fp:
            args = [a.decode() for a in fp.read().split(b'\0') if a]
    except OSError:
        return 'unknown'
    for i, arg in enumerate(args):
        # skip interpreter options, but keep a login shell's -tcsh
        if i > 0 and arg.startswith('-'):
            continue
        base = os.path.basename(arg).lstrip('-')
        if not base.startswith('python'):
            return base
    return 'unknown'


def logTransfer(program, name, nBytes, start, end, connections='', status=0):
    '''
    Append one transfer record; start/end are datetimes from now(). Never
    raises: a logging failure must not stop a download.
    '''
    seconds = max((end - start).total_seconds(), 1e-3)
    mb = nBytes / 1e6
    row = [start.isoformat(timespec='seconds'),
           end.isoformat(timespec='seconds'), f'{seconds:.1f}', f'{mb:.1f}',
           f'{mb / seconds:.2f}', str(connections), str(status), program,
           os.getcwd(), name]
    appendRow(logPath(), FIELDS, row)


def appendRow(path, fields, row):
    ''' Append row to path, header first if new. Never raises. '''
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(path, 'a') as fp:
            # Several downloaders on one host can append at once.
            fcntl.lockf(fp, fcntl.LOCK_EX)
            if os.fstat(fp.fileno()).st_size == 0:
                fp.write('\t'.join(fields) + '\n')
            fp.write('\t'.join(row) + '\n')
    except OSError as e:
        print(f'networkLog: could not write {path}: {e}')


def main():
    '''
    Log an aria2c -i session run from a shell script, as one record. Bytes are
    the sizes of the listed files now complete in --dir (no .aria2 sibling); a
    resumed partial counts in full.
    '''
    parser = argparse.ArgumentParser(
        description='\n\n\033[1mLog a finished aria2c list download to '
        f'{logPath()}\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.')
    parser.add_argument('urlList', type=str,
                        help='The aria2c -i list that was downloaded')
    parser.add_argument('--start', type=float, required=True,
                        help='Session start, epoch seconds (date +%%s)')
    parser.add_argument('--program', type=str, required=True,
                        help='Name to log, e.g. gcovDownload')
    parser.add_argument('--dir', type=str, default='.',
                        help='aria2c -d download directory [.]')
    parser.add_argument('--connections', type=str, default='',
                        help='Connection/rate setting to record')
    parser.add_argument('--status', type=int, default=0,
                        help='aria2c exit code [0]')
    args = parser.parse_args()
    nFiles, nBytes = 0, 0
    with open(args.urlList) as fp:
        for url in (line.strip() for line in fp):
            path = os.path.join(args.dir, os.path.basename(url))
            if url and os.path.exists(path) \
                    and not os.path.exists(path + '.aria2'):
                nFiles += 1
                nBytes += os.path.getsize(path)
    start = datetime.fromtimestamp(args.start).astimezone()
    logTransfer(args.program, f'{nFiles} files from {args.urlList}', nBytes,
                start, now(), connections=args.connections,
                status=args.status)


def internetInterface():
    '''
    The interface carrying the default route (lowest metric), i.e. the
    internet link rather than the private NFS network.
    '''
    best = None
    with open('/proc/net/route') as fp:
        next(fp)
        for line in fp:
            f = line.split()
            if f[1] == '00000000' and (best is None or int(f[6]) < best[1]):
                best = (f[0], int(f[6]))
    return best[0] if best else None


def monitorMain():
    '''
    Log the internet interface's traffic since the previous call. Meant for
    cron every minute; the previous counters are kept in a per-host state file.
    '''
    argparse.ArgumentParser(
        description='\n\n\033[1mLog internet-interface traffic since the last '
        f'call to {logPath("interfaceLog")}\033[0m\n\n',
        epilog='Part of the asfSearchAndDownload package.').parse_args()
    iface = internetInterface()
    if iface is None:
        print('networkMonitor: no default route')
        return
    stats = f'/sys/class/net/{iface}/statistics'
    with open(f'{stats}/rx_bytes') as fp:
        rx = int(fp.read())
    with open(f'{stats}/tx_bytes') as fp:
        tx = int(fp.read())
    t = now()
    statePath = os.path.join(LOG_DIR, '.' + os.path.basename(
        logPath('interfaceLog')) + '.state')
    try:
        with open(statePath) as fp:
            last = json.load(fp)
    except (OSError, ValueError):
        last = None
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(statePath, 'w') as fp:
        json.dump({'time': t.timestamp(), 'iface': iface, 'rx': rx, 'tx': tx},
                  fp)
    # No row on the first call, an interface change, or a counter reset (reboot).
    if last is None or last['iface'] != iface or rx < last['rx'] \
            or tx < last['tx']:
        return
    seconds = max(t.timestamp() - last['time'], 1e-3)
    rxMB, txMB = (rx - last['rx']) / 1e6, (tx - last['tx']) / 1e6
    appendRow(logPath('interfaceLog'),
              ['time', 'interface', 'seconds', 'rxMB', 'txMB', 'rxMB/s',
               'txMB/s'],
              [t.isoformat(timespec='seconds'), iface, f'{seconds:.0f}',
               f'{rxMB:.1f}', f'{txMB:.1f}', f'{rxMB / seconds:.2f}',
               f'{txMB / seconds:.2f}'])


if __name__ == '__main__':
    main()
